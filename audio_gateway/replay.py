"""把一段录音当作采集源重放进管线，用于不依赖真人说话的回归验证。

为什么需要它：字幕、同传与参谋的缺陷只在**真实语音**下才暴露（mock 事件里原文与
译文同步成对返回，配对错位永远测不出来）。以前每验证一次就得请人对着被控机放一遍
音频，既慢又不可重复。本模块用 ``~/AudioGateway/<session>/meeting.wav`` 这类既有录音
充当 ``RxBus``，让整条链路（同传 → 字幕 → 参谋 → 落盘）在同一段语料上跑出可比对的结果。

它只替换采集这一层，下游代码看到的仍是同样的 ``Tap``，因此验证的是真实管线，
不是另写一套仿真。
"""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import numpy as np

from .bus import Tap


class WavReplayBus:
    """以 ``RxBus`` 的接口重放 WAV，供离线回归使用。

    刻意保持与 ``RxBus`` 相同的订阅/启停语义与统计字段，使 bridge 与
    ``MonitorPlayer`` 等下游无需任何分支即可接入。
    """

    def __init__(
        self,
        wav_path: str | Path,
        *,
        blocksize: int = 1024,
        realtime: bool = True,
        loop: bool = False,
        pad_silence: bool = False,
    ) -> None:
        self._path = Path(wav_path)
        if not self._path.is_file():
            raise FileNotFoundError(f"回放素材不存在：{self._path}")
        self._blocksize = int(blocksize)
        if self._blocksize <= 0:
            raise ValueError("blocksize must be positive")
        # realtime=False 时全速推帧：适合只关心转写/配对结果、不关心时序的回归。
        # VAD 与任何按墙钟节流的逻辑在全速下行为会变，涉及它们时必须用 realtime=True。
        self._realtime = bool(realtime)
        self._loop = bool(loop)
        # pad_silence=True：素材放完后继续实时泵零帧直到 stop()。
        # bridge 场景必须开：素材结束≠会议结束，立刻关 taps 等于拔线——
        # interpreter 收到哨兵就关 Realtime 会话，服务端管线里尚未吐出的
        # 转写会整段丢失（2026-07-30 M1 验收实测：句 2-4 消失、句 1 译文截断）。
        self._pad_silence = bool(pad_silence)

        with wave.open(str(self._path), "rb") as handle:
            if handle.getsampwidth() != 2:
                raise ValueError(
                    f"只支持 16-bit PCM WAV，当前 {handle.getsampwidth() * 8}-bit"
                )
            self._samplerate = handle.getframerate()
            self._channels = handle.getnchannels()
            frames = handle.readframes(handle.getnframes())

        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if self._channels > 1:
            data = data.reshape(-1, self._channels)[:, 0]
        self._samples = data

        self._taps: list[Tap] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._stopping = False

        # 与 RxBus 同名的可观测字段，便于 /status 与断言复用
        self.status_flags: set[str] = set()
        self.callback_count = 0
        self.captured_samples = 0

    # ---- RxBus 兼容接口 -------------------------------------------------

    @property
    def samplerate(self) -> int:
        return self._samplerate

    @property
    def duration_s(self) -> float:
        return len(self._samples) / float(self._samplerate)

    @property
    def active(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def subscribe(
        self,
        name: str,
        *,
        maxsize: int = 0,
        drop_oldest: bool = False,
    ) -> Tap:
        tap = Tap(name, maxsize=maxsize, drop_oldest=drop_oldest)
        with self._lock:
            self._taps.append(tap)
        return tap

    @property
    def taps(self) -> tuple[Tap, ...]:
        with self._lock:
            return tuple(self._taps)

    def dropped_by_tap(self) -> dict[str, int]:
        return {tap.name: tap.dropped for tap in self.taps}

    def discard_buffered(
        self,
        *,
        drop_oldest_only: bool = True,
    ) -> dict[str, int]:
        return {
            tap.name: tap.discard_pending()
            for tap in self.taps
            if tap.drop_oldest or not drop_oldest_only
        }

    def start(self) -> None:
        if self.active:
            return
        self._stopping = False
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._pump,
            name="wav-replay",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, close_taps: bool = True) -> None:
        # 与 RxBus.stop 同签名：bridge 的 _AudioLifecycle 会带 close_taps 调用
        self._stopping = True
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
        self._thread = None
        if close_taps:
            for tap in self.taps:
                tap.close()

    def health_reason(self) -> str | None:
        # 素材放完不是故障：回放结束等价于"会议安静了"，
        # 绝不能让 health_watch 把整场验收 fail-closed 掉。
        return None

    def wait_until_finished(self, timeout: float | None = None) -> bool:
        """等待素材放完（loop=True 时永不返回 True，除非被 stop）。"""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    # ---- 内部 -----------------------------------------------------------

    def _pump(self) -> None:
        block = self._blocksize
        period = block / float(self._samplerate)
        started = time.monotonic()
        emitted = 0

        while not self._stop_event.is_set():
            for offset in range(0, len(self._samples), block):
                if self._stop_event.is_set():
                    break
                frame = self._samples[offset:offset + block]
                if len(frame) < block:
                    # 末尾补零，保持下游看到的帧长恒定，与真实回调一致
                    frame = np.pad(frame, (0, block - len(frame)))
                frame = np.copy(frame)

                with self._lock:
                    self.callback_count += 1
                    self.captured_samples += block
                    taps = tuple(self._taps)
                for tap in taps:
                    tap.push(frame)

                emitted += 1
                if self._realtime:
                    target = started + emitted * period
                    delay = target - time.monotonic()
                    if delay > 0:
                        self._stop_event.wait(delay)
            if not self._loop:
                break

        if self._pad_silence:
            silence = np.zeros(block, dtype=np.float32)
            while not self._stop_event.is_set():
                with self._lock:
                    self.callback_count += 1
                    self.captured_samples += block
                    taps = tuple(self._taps)
                for tap in taps:
                    tap.push(np.copy(silence))
                emitted += 1
                if self._realtime:
                    target = started + emitted * period
                    delay = target - time.monotonic()
                    if delay > 0:
                        self._stop_event.wait(delay)

        if not self._stopping:
            for tap in self.taps:
                tap.close()
