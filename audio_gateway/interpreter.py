"""OpenAI Realtime 同传客户端与独立操作席译文播放器。

采集侧固定接收 48kHz float32 单声道帧，累计到至少 100ms 后降采样为
24kHz PCM16，再通过 Realtime translation WebSocket 发送。服务端返回的
24kHz PCM16 译文音频只交给本模块持有的独立输出播放器。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import getpass
import hashlib
import json
import math
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

import aiohttp
import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

from .bus import Tap
from .routing import RingBuffer, fan_out_mono, resolve_output_channels

REALTIME_TRANSLATION_ENDPOINT = (
    "wss://api.openai.com/v1/realtime/translations"
)
DEFAULT_INTERPRETER_MODEL = "gpt-realtime-translate"
SOURCE_TRANSCRIPTION_MODEL = "gpt-realtime-whisper"
INPUT_SAMPLE_RATE = 48_000
REALTIME_SAMPLE_RATE = 24_000
OUTPUT_SAMPLE_RATE = 48_000
# 译文语音抖动预缓冲：400ms。真实 Teams 验收（2026-07-30）实测 TTS 增量
# 经网络到达不均匀，零缓冲直出句中出现空洞；400ms 换句内连贯，
# 首音延迟增加同额，仍远小于句级延迟预算。
PLAYBACK_PREBUFFER_SECONDS = 0.4
PLAYBACK_PREBUFFER_SAMPLES = int(
    OUTPUT_SAMPLE_RATE * PLAYBACK_PREBUFFER_SECONDS
)
# 突发型语音源（ElevenLabs 整句 PCM 几乎一次到齐）的预缓冲：抖动风险
# 远低于实时细流（OpenAI TTS delta 逐段到达），150ms 足够吸收调度毛刺。
# clone/cascade 发言路径用它换回 0.25s 首音延迟（2026-07-31 延迟优化）。
BURST_PREBUFFER_SECONDS = 0.15
MIN_APPEND_MILLISECONDS = 100
MIN_APPEND_INPUT_SAMPLES = (
    INPUT_SAMPLE_RATE * MIN_APPEND_MILLISECONDS // 1_000
)
RECONNECT_INITIAL_SECONDS = 2.0
RECONNECT_MAX_SECONDS = 30.0
DEFAULT_VAD_DBFS = -50.0
VAD_HOLD_SECONDS = 3.0
# 断句兜底：迟迟等不到终止符的 pending 强制吐出，防止整段话卡到断线才可见。
# 160 字对 CJK 已是异常长的连排；15s 覆盖"标点被转写吞掉"的真实场景。
SENTENCE_MAX_PENDING_CHARS = 160
SENTENCE_MAX_PENDING_SECONDS = 15.0
# 译文/发言语音的积压上限。服务端 TTS delta 比实时快得多，一整句的音频会在
# 一两秒内全部到齐，所以缓冲必须容得下一整段发言；上限只是防跑飞。
# 旧实现是 queue(maxsize=64) × 单次 delta（实测 ~400ms）≈ 25s，这里取齐。
PLAYBACK_BACKLOG_SECONDS = 30.0
PLAYBACK_BACKLOG_SAMPLES = int(OUTPUT_SAMPLE_RATE * PLAYBACK_BACKLOG_SECONDS)


@dataclass(frozen=True)
class Segment:
    """一条泳道里独立发布的一句话；配对概念在数据模型里不存在。

    协议层没有任何 source↔translation 关联字段（实测一场会 163 原文:134 译文，
    一条译文经常覆盖两三句原文），所以每条 Segment 只承载一条流的文本。
    elapsed_ms 是服务端 session 内的显示标记——VAD 门控吞掉静音、每次重连
    从 0 重计——绝不能当整场会议的时间轴或排序键。epoch 每断线重连递增，
    把跨 session 的任何"配对/对齐"想象硬性切断。
    """

    stream: Literal["source", "translation"]
    text: str
    elapsed_ms: int | None
    epoch: int


StateCallback = Callable[[dict[str, Any]], None]
SentenceCallback = Callable[[Segment], None]
# 草稿回调 (stream, text, epoch)：text 是当前未成句的半句，空串表示该泳道
# 草稿已被正式段收编。草稿是可变中间态，与 append-only 的 Segment 分开走。
DraftCallback = Callable[[str, str, int], None]
ErrorCallback = Callable[[str], None]
SleepCallback = Callable[[float], Awaitable[None]]
SessionFactory = Callable[[], Any]


def build_realtime_translation_url(model: str) -> str:
    """Return the official translation endpoint with only the model query."""
    return f"{REALTIME_TRANSLATION_ENDPOINT}?{urlencode({'model': model})}"


def make_safety_identifier(local_user: str | None = None) -> str:
    """Hash a stable local identifier without sending the raw OS username."""
    value = local_user if local_user is not None else getpass.getuser()
    return hashlib.sha256(
        f"audio-gateway:{value}".encode("utf-8")
    ).hexdigest()


def encode_audio_48k_float32_to_24k_pcm16(frame: np.ndarray) -> bytes:
    """Downsample one mono float32 block 2:1, clip, and encode little-endian."""
    samples = np.asarray(frame, dtype=np.float32).reshape(-1)
    if samples.size == 0:
        return b""
    downsampled = resample_poly(samples, 1, 2)
    clipped = np.clip(downsampled, -1.0, 1.0)
    pcm = np.rint(clipped * 32767.0).astype("<i2")
    return pcm.tobytes()


def decode_audio_24k_pcm16_to_48k_float32(data: bytes) -> np.ndarray:
    """Decode little-endian PCM16 and upsample 2:1 for the output device."""
    if len(data) % 2:
        raise ValueError("24kHz PCM16 audio byte length must be even")
    samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    if samples.size == 0:
        return np.empty(0, dtype=np.float32)
    return resample_poly(samples, 2, 1).astype(np.float32)


class Pcm16AppendBatcher:
    """Accumulate 48kHz frames and emit exact 100ms-or-larger PCM16 blocks."""

    def __init__(
        self,
        *,
        input_samples_per_chunk: int = MIN_APPEND_INPUT_SAMPLES,
    ) -> None:
        if input_samples_per_chunk < MIN_APPEND_INPUT_SAMPLES:
            raise ValueError("Realtime append chunks must be at least 100ms")
        self._input_samples_per_chunk = input_samples_per_chunk
        self._pending = np.empty(0, dtype=np.float32)

    @property
    def pending_samples(self) -> int:
        return int(self._pending.size)

    def feed(self, frame: np.ndarray) -> list[bytes]:
        samples = np.asarray(frame, dtype=np.float32).reshape(-1)
        if samples.size:
            self._pending = np.concatenate((self._pending, samples))
        chunks: list[bytes] = []
        while self._pending.size >= self._input_samples_per_chunk:
            chunk = self._pending[:self._input_samples_per_chunk]
            self._pending = self._pending[self._input_samples_per_chunk:]
            chunks.append(
                encode_audio_48k_float32_to_24k_pcm16(chunk)
            )
        return chunks

    def clear(self) -> None:
        self._pending = np.empty(0, dtype=np.float32)


class SilenceVadGate:
    """Sample-counted RMS gate for the paid Realtime append path only."""

    def __init__(
        self,
        *,
        threshold_dbfs: float | None = DEFAULT_VAD_DBFS,
        hold_seconds: float = VAD_HOLD_SECONDS,
        samplerate: int = INPUT_SAMPLE_RATE,
    ) -> None:
        if samplerate <= 0:
            raise ValueError("samplerate must be positive")
        if hold_seconds <= 0:
            raise ValueError("hold_seconds must be positive")
        if threshold_dbfs is not None and not math.isfinite(threshold_dbfs):
            raise ValueError("threshold_dbfs must be finite or None")
        self.threshold_dbfs = threshold_dbfs
        self._hold_samples = max(1, round(samplerate * hold_seconds))
        self._silent_samples = 0
        self._gated = False

    @property
    def gated(self) -> bool:
        return self._gated

    @property
    def silent_samples(self) -> int:
        return self._silent_samples

    def reset(self) -> None:
        self._silent_samples = 0
        self._gated = False

    def feed(self, frame: np.ndarray) -> bool:
        samples = np.asarray(frame, dtype=np.float32).reshape(-1)
        if self.threshold_dbfs is None:
            self.reset()
            return False
        if samples.size == 0:
            return self._gated

        rms = float(np.sqrt(np.mean(
            samples.astype(np.float64) ** 2
        )))
        dbfs = 20.0 * math.log10(max(rms, 1e-12))
        # Product contract is strictly "below" the threshold. A frame exactly
        # on the configured boundary counts as voice and resumes immediately.
        if dbfs < self.threshold_dbfs:
            self._silent_samples += int(samples.size)
            if self._silent_samples >= self._hold_samples:
                self._gated = True
        else:
            self._silent_samples = 0
            self._gated = False
        return self._gated


class SentenceAccumulator:
    """Collect text deltas until a terminator, with size/time force-flush.

    ASCII '.' 被刻意排除在终止符之外："10.5億円" / "No.3" 会被误切，且两条流
    （原文/译文）的误切率不同，会进一步放大天然句数差（实测 163:134）。
    句读语义由 。！？!?…‥;； 与换行承担；缺终止符的长中间态由超长/超时
    兜底强制吐出，防止整段话卡在 pending 里直到断线冲刷才可见。
    """

    _TERMINATORS = frozenset("!?。！？…‥;；\n")

    def __init__(
        self,
        *,
        max_pending_chars: int = SENTENCE_MAX_PENDING_CHARS,
        max_pending_seconds: float = SENTENCE_MAX_PENDING_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_pending_chars <= 0:
            raise ValueError("max_pending_chars must be positive")
        if max_pending_seconds <= 0:
            raise ValueError("max_pending_seconds must be positive")
        self._pending = ""
        self._pending_since = 0.0
        self._max_pending_chars = max_pending_chars
        self._max_pending_seconds = max_pending_seconds
        self._clock = clock

    def feed(self, delta: str) -> list[str]:
        now = self._clock()
        if not self._pending:
            self._pending_since = now
        self._pending += delta
        sentences: list[str] = []
        start = 0
        for index, char in enumerate(self._pending):
            if char not in self._TERMINATORS:
                continue
            sentence = self._pending[start:index + 1].strip()
            if sentence:
                sentences.append(sentence)
            start = index + 1
        if start:
            self._pending = self._pending[start:]
            # 剩下的半句是刚开始累计的新内容，超时窗口从现在起算。
            self._pending_since = now
        if self._pending and (
            len(self._pending) >= self._max_pending_chars
            or now - self._pending_since >= self._max_pending_seconds
        ):
            forced = self._pending.strip()
            self._pending = ""
            if forced:
                sentences.append(forced)
        return sentences

    def flush(self) -> list[str]:
        sentence = self._pending.strip()
        self._pending = ""
        return [sentence] if sentence else []

    @property
    def pending(self) -> str:
        """当前未成句的半句——字幕草稿行的数据源，只读。"""
        return self._pending


class InterpreterState:
    """Thread-safe state projected into bridge /status and WS state messages."""

    def __init__(
        self,
        *,
        enabled: bool,
        lang: str,
        interpret_voice: bool = False,
        vad_dbfs: float | None = DEFAULT_VAD_DBFS,
    ) -> None:
        self._lock = threading.Lock()
        self._enabled = enabled
        self._connected = False
        self._lang = lang
        self._last_source = ""
        self._last_translation = ""
        self._connection_error: str | None = None
        self._playback_error: str | None = None
        self._interpret_voice = interpret_voice
        self._gated = False
        self._vad_dbfs = vad_dbfs
        self._history_len = 0

    def set_connected(self, connected: bool) -> None:
        with self._lock:
            self._connected = connected
            if connected:
                self._connection_error = None

    def set_error(self, error: str | None) -> None:
        with self._lock:
            self._connection_error = error
            if error:
                self._connected = False

    def set_playback_error(self, error: str | None) -> None:
        with self._lock:
            self._playback_error = error

    def set_interpret_voice(self, enabled: bool) -> None:
        with self._lock:
            self._interpret_voice = enabled

    def set_gated(self, gated: bool) -> bool:
        with self._lock:
            changed = self._gated != gated
            self._gated = gated
            return changed

    def set_history_len(self, history_len: int) -> None:
        with self._lock:
            self._history_len = max(0, int(history_len))

    def set_sentence(self, kind: str, sentence: str) -> None:
        with self._lock:
            if kind == "source":
                self._last_source = sentence
            elif kind == "translation":
                self._last_translation = sentence
            else:
                raise ValueError(f"unknown interpreter sentence kind: {kind}")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "connected": self._connected,
                "lang": self._lang,
                "last_source": self._last_source,
                "last_translation": self._last_translation,
                "interpret_voice": self._interpret_voice,
                "gated": self._gated,
                "vad_dbfs": self._vad_dbfs,
                "history_len": self._history_len,
                "error": self._playback_error or self._connection_error,
            }


class InterpreterOutputPlayer:
    """Queue translated audio for one explicitly resolved listening device.

    回调驱动、**没有播放线程**。理由见 `routing.PLAYER_THREAD_CONTRACT`：
    旧实现让一个 daemon 线程阻塞在 `stream.write()`（实测单次 TTS delta
    ~400ms），收尾时 join 超时 → 线程带着 libportaudio 的 PC 活到
    `Py_Finalize` → `dlclose` 抽走代码页 → SIGSEGV。改成回调后，Python 侧
    再也不会进入 PortAudio，`stop()` 由控制线程确定性地关流。
    """

    def __init__(
        self,
        device: int,
        *,
        on_status: Callable[[str], None] | None = None,
        on_error: ErrorCallback | None = None,
        sounddevice_module: Any | None = None,
        prebuffer_seconds: float = PLAYBACK_PREBUFFER_SECONDS,
    ) -> None:
        self._device = device
        self._on_status = on_status
        self._on_error = on_error
        # 注入口与 MonitorPlayer 同款：没有它，播放器的生命周期在无声卡的
        # CI 上一行都断言不到 —— 这正是本次崩溃能溜过 152 个用例的原因。
        self._sd = sounddevice_module or sd
        self._ring = RingBuffer(
            PLAYBACK_BACKLOG_SAMPLES,
            prebuffer_samples=int(OUTPUT_SAMPLE_RATE * prebuffer_seconds),
        )
        self._stream: Any | None = None
        self._stopping = False
        self._channels = 1
        self.callback_count = 0
        self.played_samples = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        stream = self._stream
        return bool(stream is not None and stream.active)

    @property
    def backlog_samples(self) -> int:
        return self._ring.backlog_samples

    @property
    def dropped_samples(self) -> int:
        return self._ring.dropped_samples

    def _callback(self, outdata, frames, time_info, status) -> None:
        try:
            if status and self._on_status is not None:
                self._on_status(str(status))
            mono, _filled = self._ring.pull(int(frames))
            with self._lock:
                self.callback_count += 1
                self.played_samples += int(frames)
            outdata[:] = fan_out_mono(mono, self._channels)
        except Exception as exc:
            if self._on_error is not None:
                self._on_error(f"interpreter playback callback failed: {exc}")
            raise self._sd.CallbackAbort from exc

    def _finished_callback(self) -> None:
        if not self._stopping and self._on_error is not None:
            self._on_error("interpreter playback stream became inactive")

    def start(self) -> None:
        if self.active:
            return
        # 上一代播放器可能留下过期译文样本；一次显式启用从空缓冲开始。
        self._ring.clear()
        self._stopping = False
        try:
            info = self._sd.query_devices(self._device)
            if int(info["max_output_channels"]) < 1:
                raise RuntimeError(
                    f"interpreter device #{self._device} has no output channel"
                )
            self._channels = resolve_output_channels(info["max_output_channels"])
            stream = self._sd.OutputStream(
                device=self._device,
                channels=self._channels,
                samplerate=OUTPUT_SAMPLE_RATE,
                dtype="float32",
                latency="low",
                callback=self._callback,
                finished_callback=self._finished_callback,
            )
            self._stream = stream
            stream.start()
        except Exception:
            self._close_stream()
            raise

    def feed_pcm16(self, data: bytes) -> None:
        samples = decode_audio_24k_pcm16_to_48k_float32(data)
        if samples.size == 0:
            return
        self._ring.push(samples)

    def _close_stream(self) -> None:
        """只允许控制线程调用。abort 而非 stop：丢弃在途缓冲＝立刻掐断日语。

        这一次 abort 是合法的：本播放器没有任何线程会进入这条流，控制线程是
        此刻唯一的调用者。事故里的非法用法是"外部线程 abort 一条正被另一个
        Python 线程 `Pa_WriteStream` 阻塞着的流"，那条路径已不复存在。
        """
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.abort()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def stop(self) -> bool:
        """收尾；返回 True 表示本播放器已不再持有任何 PortAudio 资源。"""
        self._stopping = True
        self._ring.clear()
        self._close_stream()
        return True

    def residual_thread_reason(self) -> str | None:
        # 回调式播放器结构上不拥有任何 Python 线程，故永远无残留可报。
        return None


class RealtimeInterpreter:
    """Maintain one reconnecting Realtime translation WebSocket session."""

    def __init__(
        self,
        audio_tap: Tap,
        output_player: Any,
        *,
        api_key: str,
        lang: str = "zh",
        model: str = DEFAULT_INTERPRETER_MODEL,
        safety_identifier: str | None = None,
        url: str | None = None,
        state: InterpreterState | None = None,
        on_state: StateCallback | None = None,
        on_sentence: SentenceCallback | None = None,
        on_draft: DraftCallback | None = None,
        on_error: ErrorCallback | None = None,
        sleep: SleepCallback = asyncio.sleep,
        close_timeout: float = 5.0,
        session_factory: SessionFactory | None = None,
        vad_dbfs: float | None = DEFAULT_VAD_DBFS,
        vad_hold_seconds: float = VAD_HOLD_SECONDS,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY is required for Realtime interpreter")
        self._audio_tap = audio_tap
        self._output_player = output_player
        self._api_key = api_key.strip()
        self._lang = lang
        self._model = model
        self._safety_identifier = (
            safety_identifier or make_safety_identifier()
        )
        self._url = url or build_realtime_translation_url(model)
        self.state = state or InterpreterState(
            enabled=True,
            lang=lang,
            vad_dbfs=vad_dbfs,
        )
        self._on_state = on_state
        self._on_sentence = on_sentence
        self._on_draft = on_draft
        self._on_error = on_error
        self._sleep = sleep
        self._close_timeout = close_timeout
        self._session_factory = session_factory
        self._audio_queue: asyncio.Queue[np.ndarray] = asyncio.Queue(
            maxsize=64
        )
        self._stop_event = asyncio.Event()
        self._feeder_stop = threading.Event()
        self._feeder_thread: threading.Thread | None = None
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._source_text = SentenceAccumulator()
        self._translation_text = SentenceAccumulator()
        # 每泳道上一次发出的草稿，内容不变不重发（尤其是清空后的连续空串）。
        self._last_draft = {"source": "", "translation": ""}
        # 每断线重连递增；随每条 Segment 带出，是双泳道协议的 session 边界。
        self._epoch = 0
        self._vad = SilenceVadGate(
            threshold_dbfs=vad_dbfs,
            hold_seconds=vad_hold_seconds,
        )
        self._voice_lock = asyncio.Lock()
        self._player_started = False

    @property
    def url(self) -> str:
        return self._url

    def start(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        selected_loop = loop or asyncio.get_running_loop()
        self._task = selected_loop.create_task(self.run())
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        if task is not None and task is not asyncio.current_task():
            await task

    async def set_voice_enabled(self, enabled: bool) -> bool:
        """Apply the runtime voice mode and keep state server-authoritative."""
        if not isinstance(enabled, bool):
            raise TypeError("interpret_voice enabled must be a bool")
        async with self._voice_lock:
            if enabled:
                if self._player_started:
                    self.state.set_interpret_voice(True)
                    self._notify_state()
                    return True
                try:
                    await asyncio.to_thread(self._output_player.start)
                except Exception as exc:
                    message = f"output device start failed: {exc}"
                    self.state.set_interpret_voice(False)
                    self.state.set_playback_error(message)
                    self._report_playback_error(message)
                    return False
                self._player_started = True
                self.state.set_playback_error(None)
                self.state.set_interpret_voice(True)
                self._notify_state()
                return True

            if self._player_started:
                await asyncio.to_thread(self._output_player.stop)
                self._player_started = False
            self.state.set_interpret_voice(False)
            self.state.set_playback_error(None)
            self._notify_state()
            return True

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._start_feeder()
        try:
            if self.state.snapshot()["interpret_voice"]:
                await self.set_voice_enabled(True)

            backoff = RECONNECT_INITIAL_SECONDS
            while not self._stop_event.is_set():
                try:
                    await self._run_connection()
                    if self._stop_event.is_set():
                        break
                    raise ConnectionError("translation session closed")
                except asyncio.CancelledError:
                    self._stop_event.set()
                    raise
                except Exception as exc:
                    if self._stop_event.is_set():
                        break
                    self._report_error(str(exc))
                    await self._sleep_or_stop(backoff)
                    backoff = min(RECONNECT_MAX_SECONDS, backoff * 2.0)
        finally:
            self._feeder_stop.set()
            if self._feeder_thread is not None:
                self._feeder_thread.join(timeout=1.0)
            if self._player_started:
                await asyncio.to_thread(self._output_player.stop)
                self._player_started = False
            self.state.set_interpret_voice(False)
            self.state.set_gated(False)
            self.state.set_connected(False)
            self._notify_state()

    def handle_server_event(self, event: dict[str, Any]) -> bool:
        """Parse only the translation protocol events defined by the task."""
        event_type = event.get("type")
        if event_type == "session.output_audio.delta":
            if (
                not self.state.snapshot()["interpret_voice"]
                or not self._player_started
            ):
                return False
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise ValueError("session.output_audio.delta missing string delta")
            try:
                data = base64.b64decode(delta, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError(
                    "session.output_audio.delta contains invalid base64"
                ) from exc
            self._output_player.feed_pcm16(data)
            return False
        if event_type == "session.output_transcript.delta":
            self._consume_text_delta(
                "translation",
                event.get("delta"),
                event.get("elapsed_ms"),
            )
            return False
        if event_type == "session.input_transcript.delta":
            self._consume_text_delta(
                "source",
                event.get("delta"),
                event.get("elapsed_ms"),
            )
            return False
        if event_type == "session.closed":
            return True
        if event_type == "error":
            error = event.get("error")
            raise RuntimeError(
                f"Realtime server error: {json.dumps(error, ensure_ascii=False)}"
            )
        return False

    async def _run_connection(self) -> None:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "OpenAI-Safety-Identifier": self._safety_identifier,
        }
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10.0)
        session_context = (
            self._session_factory()
            if self._session_factory is not None
            else aiohttp.ClientSession(timeout=timeout)
        )
        async with session_context as session:
            async with session.ws_connect(
                self._url,
                headers=headers,
                heartbeat=20.0,
                max_msg_size=4 << 20,
            ) as ws:
                await ws.send_json({
                    "type": "session.update",
                    "session": {
                        "audio": {
                            # 原文转写必须显式开启：2026-07-30 真机探测确认，
                            # 只配 output.language 时服务端不发
                            # session.input_transcript.delta（面板"原文"会永远为空）。
                            "input": {
                                "transcription": {
                                    "model": SOURCE_TRANSCRIPTION_MODEL,
                                    # 不发 language：translations 端点实测拒绝该参数
                                    # （2026-07-30 unknown_parameter），输入语种只能
                                    # 由模型自检。机器朗读的合成音频会被检错语种，
                                    # 真人语音无此问题（会议方向已验证）。
                                },
                            },
                            "output": {
                                "language": self._lang,
                            },
                        },
                    },
                })
                self.state.set_connected(True)
                self._notify_state()
                sender = asyncio.create_task(self._send_audio(ws))
                stop_waiter = asyncio.create_task(self._stop_event.wait())
                try:
                    while True:
                        receiver = asyncio.create_task(ws.receive())
                        done, _ = await asyncio.wait(
                            {receiver, sender, stop_waiter},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if stop_waiter in done:
                            receiver.cancel()
                            await asyncio.gather(
                                receiver,
                                return_exceptions=True,
                            )
                            sender.cancel()
                            await asyncio.gather(
                                sender,
                                return_exceptions=True,
                            )
                            await self._graceful_close(ws)
                            return
                        if sender in done:
                            receiver.cancel()
                            await asyncio.gather(
                                receiver,
                                return_exceptions=True,
                            )
                            exception = sender.exception()
                            if exception is not None:
                                raise exception
                            raise ConnectionError(
                                "Realtime audio sender stopped unexpectedly"
                            )

                        message = receiver.result()
                        if message.type == aiohttp.WSMsgType.TEXT:
                            try:
                                event = json.loads(message.data)
                            except json.JSONDecodeError as exc:
                                raise RuntimeError(
                                    "Realtime server returned invalid JSON"
                                ) from exc
                            if not isinstance(event, dict):
                                raise RuntimeError(
                                    "Realtime server event must be an object"
                                )
                            if self.handle_server_event(event):
                                raise ConnectionError(
                                    "translation session closed by server"
                                )
                        elif message.type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                        }:
                            raise ConnectionError(
                                "Realtime WebSocket disconnected"
                            )
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            raise ConnectionError(
                                f"Realtime WebSocket error: {ws.exception()}"
                            )
                finally:
                    sender.cancel()
                    stop_waiter.cancel()
                    await asyncio.gather(
                        sender,
                        stop_waiter,
                        return_exceptions=True,
                    )
                    self.state.set_connected(False)
                    self._flush_text()
                    self._notify_state()

    async def _send_audio(
        self,
        ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        batcher = Pcm16AppendBatcher()
        while not self._stop_event.is_set():
            try:
                frame = await asyncio.wait_for(
                    self._audio_queue.get(),
                    timeout=0.2,
                )
            except TimeoutError:
                continue
            gated = self._vad.feed(frame)
            self._update_gated(gated)
            if gated:
                batcher.clear()
                continue
            for pcm16 in batcher.feed(frame):
                await ws.send_json({
                    "type": "session.input_audio_buffer.append",
                    "audio": base64.b64encode(pcm16).decode("ascii"),
                })

    async def _sleep_or_stop(self, delay: float) -> None:
        sleeper = asyncio.create_task(self._sleep(delay))
        stopper = asyncio.create_task(self._stop_event.wait())
        try:
            await asyncio.wait(
                {sleeper, stopper},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            sleeper.cancel()
            stopper.cancel()
            await asyncio.gather(
                sleeper,
                stopper,
                return_exceptions=True,
            )

    async def _graceful_close(
        self,
        ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        await ws.send_json({"type": "session.close"})
        deadline = asyncio.get_running_loop().time() + self._close_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    "timed out waiting for session.closed"
                )
            message = await asyncio.wait_for(ws.receive(), remaining)
            if message.type == aiohttp.WSMsgType.TEXT:
                event = json.loads(message.data)
                if isinstance(event, dict) and self.handle_server_event(event):
                    return
            elif message.type in {
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            }:
                raise ConnectionError(
                    "Realtime WebSocket closed before session.closed"
                )

    def _start_feeder(self) -> None:
        self._feeder_stop.clear()

        def feed() -> None:
            for frame in self._audio_tap.frames(
                stop_event=self._feeder_stop,
                poll_seconds=0.1,
            ):
                loop = self._loop
                if loop is None or loop.is_closed():
                    return
                copy = np.asarray(frame, dtype=np.float32).copy()
                try:
                    loop.call_soon_threadsafe(self._enqueue_audio, copy)
                except RuntimeError:
                    return

        self._feeder_thread = threading.Thread(
            target=feed,
            name="interpreter-input",
            daemon=True,
        )
        self._feeder_thread.start()

    def _enqueue_audio(self, frame: np.ndarray) -> None:
        if self._audio_queue.full():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._audio_queue.put_nowait(frame)

    def _consume_text_delta(
        self,
        kind: str,
        delta: Any,
        elapsed_ms: Any = None,
    ) -> None:
        if not isinstance(delta, str):
            raise ValueError(f"{kind} transcript delta must be a string")
        # 官方明文 elapsed_ms 不是唯一标识，只作 epoch 内显示标记原样带出；
        # 形状异常安静降级为 None，显示标记绝不允许炸掉转写链路。
        marker = (
            int(elapsed_ms)
            if isinstance(elapsed_ms, (int, float))
            and not isinstance(elapsed_ms, bool)
            else None
        )
        accumulator = (
            self._source_text
            if kind == "source"
            else self._translation_text
        )
        for sentence in accumulator.feed(delta):
            self._publish_segment(kind, sentence, marker)
        # 正式句发布之后再发草稿：客户端按到达序应用，灰字永远是"这句话
        # 之后还没说完的部分"。成句被收编时 pending 变短/清空，同样走这里。
        self._emit_draft(kind, accumulator.pending)

    def _emit_draft(self, kind: str, text: str) -> None:
        stripped = text.strip()
        if self._last_draft.get(kind) == stripped:
            return
        self._last_draft[kind] = stripped
        if self._on_draft is not None:
            self._on_draft(kind, stripped, self._epoch)

    def _flush_text(self) -> None:
        # 断线冲刷的残句属于已结束的 session，没有可信的 elapsed_ms。
        for sentence in self._source_text.flush():
            self._publish_segment("source", sentence, None)
        for sentence in self._translation_text.flush():
            self._publish_segment("translation", sentence, None)
        # 残句已作为正式段发出，清掉两条泳道的灰字草稿。
        self._emit_draft("source", "")
        self._emit_draft("translation", "")
        # 每次连接结束递增 epoch：重连即全新 session（服务端时钟归零、
        # 断句状态清空），任何跨 session 的配对想象在这里被硬性切断。
        self._epoch += 1

    def _publish_segment(
        self,
        kind: str,
        sentence: str,
        elapsed_ms: int | None,
    ) -> None:
        self.state.set_sentence(kind, sentence)
        if self._on_sentence is not None:
            self._on_sentence(Segment(
                stream=kind,  # type: ignore[arg-type]
                text=sentence,
                elapsed_ms=elapsed_ms,
                epoch=self._epoch,
            ))
        self._notify_state()

    def _report_error(self, message: str) -> None:
        self.state.set_error(message)
        print(f"[interpreter][ALERT] {message}", flush=True)
        if self._on_error is not None:
            self._on_error(message)
        self._notify_state()

    def _report_playback_error(self, message: str) -> None:
        print(f"[interpreter][ALERT] {message}", flush=True)
        if self._on_error is not None:
            self._on_error(message)
        self._notify_state()

    def _update_gated(self, gated: bool) -> None:
        if not self.state.set_gated(gated):
            return
        if gated:
            print(
                "[interpreter] VAD 门控开启：静音持续达到 3s，"
                "暂停 Realtime append。",
                flush=True,
            )
        else:
            print(
                "[interpreter] VAD 门控恢复：检测到有声，"
                "Realtime append 立即继续。",
                flush=True,
            )
        self._notify_state()

    def _notify_state(self) -> None:
        if self._on_state is not None:
            self._on_state(self.state.snapshot())
