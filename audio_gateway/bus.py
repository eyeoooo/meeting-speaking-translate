"""模块 A：Mac_In(粉孔) 采集 + 单源多路扇出。

一个 InputStream 回调把同一帧分发到多个订阅队列：
  - 录音 / STT 队列：不设上限，绝不丢帧；
  - 监听队列：有界 + 丢旧保新（听感允许丢，延迟不允许涨）。
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

import numpy as np
import sounddevice as sd

_SENTINEL = None  # 队列结束标记
StatusCallback = Callable[[str], None]
ErrorCallback = Callable[[str], None]


class Tap:
    """一路订阅。drop_oldest=True 时队满丢最旧帧（用于监听）。"""

    def __init__(self, name: str, maxsize: int = 0, drop_oldest: bool = False):
        self.name = name
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.drop_oldest = drop_oldest
        self.dropped = 0

    def push(self, frame: np.ndarray) -> None:
        try:
            self.q.put_nowait(frame)
        except queue.Full:
            if self.drop_oldest:
                try:
                    self.q.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    pass
                try:
                    self.q.put_nowait(frame)
                except queue.Full:
                    self.dropped += 1
            else:
                self.dropped += 1

    def close(self) -> None:
        try:
            self.q.put_nowait(_SENTINEL)
        except queue.Full:
            try:
                self.q.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
            self.q.put_nowait(_SENTINEL)

    def discard_pending(self) -> int:
        discarded = 0
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            if item is _SENTINEL:
                self.q.put_nowait(_SENTINEL)
                break
            discarded += 1
        self.dropped += discarded
        return discarded

    def frames(
        self,
        *,
        stop_event: threading.Event | None = None,
        poll_seconds: float = 0.2,
    ):
        """阻塞迭代，直到收到结束标记。"""
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            try:
                item = self.q.get(
                    timeout=poll_seconds if stop_event is not None else None
                )
            except queue.Empty:
                continue
            if item is _SENTINEL:
                return
            yield item


class RxBus:
    def __init__(
        self,
        device: int,
        samplerate: int,
        blocksize: int,
        channels: int = 1,
        *,
        on_status: StatusCallback | None = None,
        on_error: ErrorCallback | None = None,
    ):
        self._device = device
        self._samplerate = samplerate
        self._blocksize = blocksize
        self._channels = channels
        self._on_status = on_status
        self._on_error = on_error
        self._taps: list[Tap] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._stopping = False
        self._taps_closed = False
        self._device_name: str | None = None
        self.status_flags: set[str] = set()
        self.callback_count = 0
        self.captured_samples = 0

    def subscribe(self, name: str, *, maxsize: int = 0, drop_oldest: bool = False) -> Tap:
        tap = Tap(name, maxsize=maxsize, drop_oldest=drop_oldest)
        with self._lock:
            self._taps.append(tap)
        return tap

    @property
    def taps(self) -> tuple[Tap, ...]:
        with self._lock:
            return tuple(self._taps)

    @property
    def active(self) -> bool:
        stream = self._stream
        return bool(stream is not None and stream.active)

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

    def _record_status(self, status: object) -> None:
        text = str(status)
        with self._lock:
            is_new = text not in self.status_flags
            self.status_flags.add(text)
        if is_new and self._on_status is not None:
            self._on_status(text)

    def _emit_error(self, reason: str) -> None:
        if self._on_error is not None:
            self._on_error(reason)

    def _callback(self, indata, frames, time_info, status):
        try:
            if status:
                self._record_status(status)
            frame = (
                np.copy(indata[:, 0])
                if self._channels == 1
                else np.copy(indata)
            )
            with self._lock:
                self.callback_count += 1
                self.captured_samples += int(frames)
                taps = tuple(self._taps)
            for tap in taps:
                tap.push(frame)
        except Exception as exc:
            self._emit_error(f"capture callback failed: {exc}")
            raise sd.CallbackAbort from exc

    def _finished_callback(self) -> None:
        if not self._stopping:
            self._emit_error("capture stream became inactive")

    def start(self) -> None:
        if self.active:
            return
        self._stopping = False
        try:
            info = sd.query_devices(self._device)
            if int(info["max_input_channels"]) < self._channels:
                raise RuntimeError(
                    f"device #{self._device} no longer has "
                    f"{self._channels} input channel(s)"
                )
            self._device_name = str(info["name"])
            stream = sd.InputStream(
                device=self._device,
                channels=self._channels,
                samplerate=self._samplerate,
                blocksize=self._blocksize,
                dtype="float32",
                latency="low",
                callback=self._callback,
                finished_callback=self._finished_callback,
            )
            self._stream = stream
            stream.start()
        except Exception:
            stream = self._stream
            self._stream = None
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            raise

    def health_reason(self) -> str | None:
        stream = self._stream
        if stream is None:
            return "capture stream is not open"
        try:
            if not stream.active:
                return "capture stream is inactive"
            info = sd.query_devices(self._device)
        except Exception as exc:
            return f"capture device #{self._device} disappeared: {exc}"
        if int(info["max_input_channels"]) < self._channels:
            return f"capture device #{self._device} has no input channel"
        if self._device_name is not None and str(info["name"]) != self._device_name:
            return (
                f"capture device #{self._device} identity changed "
                f"from {self._device_name!r} to {str(info['name'])!r}"
            )
        return None

    def stop(self, *, close_taps: bool = True) -> None:
        self._stopping = True
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if close_taps and not self._taps_closed:
            self._taps_closed = True
            for tap in self.taps:
                tap.close()
