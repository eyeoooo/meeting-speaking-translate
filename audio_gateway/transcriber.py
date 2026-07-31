"""模块 B：能量门限断句 → Whisper 本地转写（多语自动识别）。

采集固定 48kHz（利于录音质量），送 Whisper 前 resample_poly(1/3) 精确降到 16kHz。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

import numpy as np
from scipy.signal import resample_poly

from .bus import Tap

WHISPER_SR = 16000


@dataclass
class TranscriptSegment:
    t0: float          # 距会议开始的秒数
    t1: float
    lang: str          # whisper 识别语种，如 "ja" / "en" / "zh"
    text: str


class _WhisperBackend:
    def transcribe(self, audio16k: np.ndarray) -> tuple[str, str]:
        """整段转写，返回 (text, lang)。实时模式用。"""
        raise NotImplementedError

    def transcribe_long(self, audio16k: np.ndarray) -> list[TranscriptSegment]:
        """长音频转写，带 whisper 自身的分段时间戳。批量模式用。"""
        raise NotImplementedError


class MlxWhisperBackend(_WhisperBackend):
    def __init__(self, model: str):
        import mlx_whisper  # 延迟导入：仅 Apple Silicon 装了才需要
        self._mod = mlx_whisper
        self._model = model

    def transcribe(self, audio16k: np.ndarray) -> tuple[str, str]:
        result = self._mod.transcribe(audio16k, path_or_hf_repo=self._model)
        return result["text"].strip(), result.get("language", "")

    def transcribe_long(self, audio16k: np.ndarray) -> list[TranscriptSegment]:
        result = self._mod.transcribe(audio16k, path_or_hf_repo=self._model)
        lang = result.get("language", "")
        return [
            TranscriptSegment(t0=s["start"], t1=s["end"], lang=lang, text=s["text"].strip())
            for s in result.get("segments", []) if s["text"].strip()
        ]


class FasterWhisperBackend(_WhisperBackend):
    def __init__(self, model: str):
        from faster_whisper import WhisperModel
        self._model = WhisperModel(model, device="auto", compute_type="int8")

    def transcribe(self, audio16k: np.ndarray) -> tuple[str, str]:
        segments, info = self._model.transcribe(audio16k, vad_filter=True)
        text = "".join(s.text for s in segments).strip()
        return text, info.language or ""

    def transcribe_long(self, audio16k: np.ndarray) -> list[TranscriptSegment]:
        segments, info = self._model.transcribe(audio16k, vad_filter=True)
        lang = info.language or ""
        return [
            TranscriptSegment(t0=s.start, t1=s.end, lang=lang, text=s.text.strip())
            for s in segments if s.text.strip()
        ]


def make_backend(cfg) -> _WhisperBackend:
    order = {"mlx": ["mlx"], "faster": ["faster"], "auto": ["mlx", "faster"]}[cfg.whisper_backend]
    errors = []
    for name in order:
        try:
            if name == "mlx":
                return MlxWhisperBackend(cfg.whisper_model)
            return FasterWhisperBackend(cfg.faster_model)
        except ImportError as e:
            errors.append(f"{name}: {e}")
    raise RuntimeError(
        "没有可用的 Whisper 后端。请安装 mlx-whisper（Apple Silicon）"
        f"或 faster-whisper。尝试记录：{'; '.join(errors)}"
    )


def batch_transcribe(wav_path, cfg) -> list[TranscriptSegment]:
    """批量模式（stt_mode=batch）：会后对整个 meeting.wav 一次性转写。

    会中零 STT 负载——实时字幕交给 macOS 原生 Live Captions；
    此处产出的分段时间戳与录音同基准，直接进归档与纪要。
    """
    import soundfile as sf

    audio, sr = sf.read(wav_path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != WHISPER_SR:
        if sr % WHISPER_SR == 0:
            audio = resample_poly(audio, 1, sr // WHISPER_SR)
        else:
            audio = resample_poly(audio, WHISPER_SR, sr)
    return make_backend(cfg).transcribe_long(audio.astype(np.float32))


class Transcriber:
    """实时模式（stt_mode=realtime）：从 tap 拉帧 → 断句 → 转写 → 逐段回调。"""

    def __init__(self, tap: Tap, cfg, on_segment: list[Callable[[TranscriptSegment], None]]):
        self._tap = tap
        self._cfg = cfg
        self._on_segment = on_segment
        self._backend = make_backend(cfg)
        self._thread: threading.Thread | None = None

    def _emit(self, buf: list[np.ndarray], seg_start: int, seg_end: int) -> None:
        cfg = self._cfg
        audio48 = np.concatenate(buf)
        if len(audio48) / cfg.samplerate < cfg.min_segment_s:
            return
        audio16 = resample_poly(audio48, 1, cfg.samplerate // WHISPER_SR).astype(np.float32)
        try:
            text, lang = self._backend.transcribe(audio16)
        except Exception as e:  # STT 失败不拖垮采集/录音
            print(f"[stt] 转写失败（该段跳过）：{e}")
            return
        if not text:
            return
        seg = TranscriptSegment(
            t0=seg_start / cfg.samplerate, t1=seg_end / cfg.samplerate,
            lang=lang, text=text,
        )
        for cb in self._on_segment:
            cb(seg)

    def _run(self) -> None:
        cfg = self._cfg
        buf: list[np.ndarray] = []
        buf_samples = 0
        seg_start = 0          # 段起点（自会议开始的样本数）
        total = 0              # 已消费样本数
        silent_run = 0         # 连续静音样本数
        in_speech = False

        for frame in self._tap.frames():
            total += len(frame)
            rms = float(np.sqrt(np.mean(frame ** 2)))
            is_silent = rms < cfg.silence_rms

            if not in_speech:
                if is_silent:
                    seg_start = total  # 静音期不断推进段起点
                    continue
                in_speech = True
                silent_run = 0

            buf.append(frame)
            buf_samples += len(frame)
            silent_run = silent_run + len(frame) if is_silent else 0

            hit_pause = silent_run >= cfg.silence_hold_s * cfg.samplerate
            hit_cap = buf_samples >= cfg.max_segment_s * cfg.samplerate
            if hit_pause or hit_cap:
                self._emit(buf, seg_start, total)
                buf, buf_samples = [], 0
                seg_start = total
                in_speech = False
                silent_run = 0

        if buf:  # 会议结束时冲刷尾段
            self._emit(buf, seg_start, total)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="transcriber", daemon=True)
        self._thread.start()

    def join(self, timeout: float = 30.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
