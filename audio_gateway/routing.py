"""模块 A：本地监听回放 + 发言直通（一键静音）。

扩展点：若需把系统音频/TTS 混入 Mac_Out，把 BlackHole 设为系统输出，
再在 Talkback 回调里叠加一路 BlackHole 输入即可。
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

import numpy as np
import sounddevice as sd

from .bus import Tap

StatusCallback = Callable[[str], None]
ErrorCallback = Callable[[str], None]

# 本项目的音源永远是单声道，再多的输出通道也只是复制同一份信号。
STEREO_CHANNELS = 2


def resolve_output_channels(max_output_channels: int) -> int:
    """按目标设备实际的输出通道数开流，而不是一律 channels=1。

    PortAudio 对「单声道流打到 0/2 设备」不报错，只做静默映射，实测的表现是
    戴耳机时只有一只耳朵出声 —— 没有任何日志、没有任何告警。所以通道数必须
    由设备说了算。封顶到立体声：源是单声道，第三路以后没有新信息。
    """
    return max(1, min(int(max_output_channels), STEREO_CHANNELS))


def fan_out_mono(frame: np.ndarray, channels: int) -> np.ndarray:
    """把单声道帧显式摊到每一路输出，返回新数组。

    绝不原地改写入参：同一个帧对象同时喂给录音、转写、WS 与监听，任何原地写
    都会静默污染录音和字幕，不报错，只有事后听 wav 才发现。
    """
    mono = np.asarray(frame, dtype=np.float32).reshape(-1, 1)
    if channels <= 1:
        return mono
    return np.repeat(mono, channels, axis=1)


class RingBuffer:
    """回调式输出流的单声道样本环：写侧丢最旧，读侧欠载补零。

    存在的唯一理由是把「Python 线程」挡在 PortAudio 之外（见
    ``PLAYER_THREAD_CONTRACT``）。写侧是普通 Python 线程，读侧是 PortAudio
    自己的回调线程，两边只共享一把 ``threading.Lock`` 和一个 list —— 没有任何
    一侧会阻塞在 C 调用里。

    丢最旧而不是丢最新：监听/译文都是「听感允许丢，延迟不允许涨」，积压一旦
    形成，保留旧样本只会让用户听到越来越滞后的声音。
    """

    def __init__(
        self,
        max_samples: int,
        *,
        prebuffer_samples: int = 0,
    ) -> None:
        self._max_samples = max(1, int(max_samples))
        self._chunks: list[np.ndarray] = []
        self._total = 0
        self._lock = threading.Lock()
        self.dropped_samples = 0
        # 抖动预缓冲（2026-07-30 真实 Teams 验收的产物）：TTS 增量经网络
        # 到达不均匀，零缓冲直出会让句子中间出现空洞（"没说完就卡住"）。
        # 语义：欠载后不立即续播，先攒够 prebuffer_samples 再开闸；为免
        # 尾包不足门限被永远扣住，欠载等待超过一个门限时长后开闸放行。
        # prebuffer_samples=0 = 旧行为（监听走这档：本地采集无抖动，
        # 延迟优先）。
        self._prebuffer = max(0, int(prebuffer_samples))
        self._buffering = self._prebuffer > 0
        self._held_samples = 0

    @property
    def backlog_samples(self) -> int:
        with self._lock:
            return self._total

    def push(self, samples: np.ndarray) -> None:
        arr = np.asarray(samples, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return
        with self._lock:
            self._chunks.append(arr)
            self._total += arr.size
            while self._total > self._max_samples and self._chunks:
                oldest = self._chunks[0]
                overflow = self._total - self._max_samples
                if oldest.size <= overflow:
                    self._chunks.pop(0)
                    self._total -= oldest.size
                    self.dropped_samples += oldest.size
                else:
                    self._chunks[0] = oldest[overflow:]
                    self._total -= overflow
                    self.dropped_samples += overflow

    def pull(self, frames: int) -> tuple[np.ndarray, int]:
        """取出 ``frames`` 个样本，不足部分补零；返回 (样本, 实际取到数)。"""
        out = np.zeros(int(frames), dtype=np.float32)
        filled = 0
        with self._lock:
            if self._buffering:
                if self._total >= self._prebuffer:
                    self._buffering = False
                    self._held_samples = 0
                elif self._total > 0:
                    # 有数据但没到门限：按回调节拍计等待时长（样本时钟，
                    # 不用墙钟），超过门限时长就放行——尾包不许被扣死
                    self._held_samples += int(frames)
                    if self._held_samples >= self._prebuffer:
                        self._buffering = False
                        self._held_samples = 0
                    else:
                        return out, 0
                else:
                    self._held_samples = 0
                    return out, 0
            while filled < frames and self._chunks:
                head = self._chunks[0]
                take = min(frames - filled, head.size)
                out[filled:filled + take] = head[:take]
                if take == head.size:
                    self._chunks.pop(0)
                else:
                    self._chunks[0] = head[take:]
                self._total -= take
                filled += take
            if self._prebuffer > 0 and self._total == 0 and filled < frames:
                # 欠载：回到攒批态，下一段来了先攒够再开闸
                self._buffering = True
                self._held_samples = 0
        return out, filled

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._total = 0
            self._buffering = self._prebuffer > 0
            self._held_samples = 0


PLAYER_THREAD_CONTRACT = """PortAudio 流的所有权契约（2026-07-30 SIGSEGV 事故的产物）

不变量：**任何 Python 线程都不得在解释器收尾时把 PC 停在 libportaudio 里。**

为什么必须是这条、而不是「join 等久一点」或「收尾前 abort 一下」：
  - `Pa_WriteStream` 没有超时、没有取消语义，阻塞时长由设备决定；USB 声卡
    卡顿/拔出时它可以永不返回。任何基于 join(timeout) 的收尾都只是概率。
  - CPython 的 `Py_Finalize` 不 join daemon 线程；随后模块拆解会释放 cffi 的
    Library 对象 → `dlclose(libportaudio)` → 正在执行的线程脚下的代码页被
    unmap → 下一次取指 SIGSEGV（现场地址恒为 `WriteStream+68`）。
  - daemon=False 只会把 SIGSEGV 换成永久挂死，更糟。

所以做法只有一个：**播放器一律回调驱动**。Python 侧只往 RingBuffer 里塞
样本，由 PortAudio 自己的回调线程来取。于是：
  1. 没有任何 Python 线程会进入 PortAudio 的阻塞写；
  2. `open/start/abort/close` 全部发生在**控制线程**（start()/stop() 的调用
     者），此刻没有第二个线程在这条流里，PortAudio 的线程契约被完整满足；
  3. 收尾时间有上界：`Pa_AbortStream` 丢弃在途缓冲后立即返回。

`UplinkPlayer`（bridge.py）一直是这个形态，也从未出现在任何一份崩溃报告里。
"""


class MonitorPlayer:
    """把 Mac_In 的帧回放到本地监听设备（蓝牙耳机等），用户实时听会。

    回调驱动：取帧线程只做 tap → RingBuffer 的纯 Python 搬运，PortAudio 由它
    自己的回调线程驱动。理由见 `PLAYER_THREAD_CONTRACT`。
    """

    def __init__(
        self,
        tap: Tap,
        device: int,
        samplerate: int,
        blocksize: int,
        *,
        on_status: StatusCallback | None = None,
        on_error: ErrorCallback | None = None,
        sounddevice_module=None,
    ):
        self._tap = tap
        self._device = device
        self._samplerate = samplerate
        self._blocksize = blocksize
        self._on_status = on_status
        self._on_error = on_error
        # 注入口存在的理由与 BurnInProbe 一样：回放路径必须能在无声卡的机器上
        # 被逐比特断言，否则"监听有没有改坏声音"只能靠耳朵听。
        self._sd = sounddevice_module or sd
        self._thread: threading.Thread | None = None
        self._stream = None
        self._stop_event = threading.Event()
        self._device_name: str | None = None
        self._channels = 1
        self._lock = threading.Lock()
        # 监听是"听感允许丢、延迟不允许涨"：环深度与 tap 深度同量级
        # （bridge/main 都按 maxsize=8 drop_oldest 订阅），别让积压攒出滞后感。
        # 下限 4096（48k 下 ~85ms）：blocksize 若被配成极小值，光按它算出来的
        # 环会小到每个回调都在丢帧，听感直接碎掉。
        self._ring = RingBuffer(max(int(blocksize) * 8, 4096))
        self._stopping = False
        self._played_any = False
        self.status_flags: set[str] = set()
        self.callback_count = 0
        self.played_samples = 0
        self.received_frames = 0
        self.starved_callbacks = 0

    @property
    def active(self) -> bool:
        stream = self._stream
        return bool(stream is not None and stream.active)

    @property
    def backlog_samples(self) -> int:
        return self._ring.backlog_samples

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

    def _finished_callback(self) -> None:
        if not self._stop_event.is_set() and not self._stopping:
            self._emit_error("monitor playback stream became inactive")

    def _callback(self, outdata, frames, time_info, status) -> None:
        """PortAudio 回调线程：只碰 RingBuffer 与计数器，绝不阻塞。"""
        try:
            if status:
                self._record_status(status)
            mono, filled = self._ring.pull(int(frames))
            starving = False
            with self._lock:
                self.callback_count += 1
                self.played_samples += int(frames)
                if filled > 0:
                    self._played_any = True
                elif self._played_any:
                    self.starved_callbacks += 1
                    starving = True
            outdata[:] = fan_out_mono(mono, self._channels)
            # 收尾期 tap 先关、流后关，必然空转几个回调 —— 那不是欠载。
            # 不加这道闸，每次正常结束会议都会多吐一条假告警。
            if starving and not self._stop_event.is_set():
                self._record_status("monitor buffer underrun")
        except Exception as exc:
            self._emit_error(f"monitor playback callback failed: {exc}")
            raise self._sd.CallbackAbort from exc

    def begin_shutdown(self) -> None:
        """在任何会导致本流关闭的动作之前，先声明"这是主动收尾"。

        结束会议时链路是 bus 先关 tap：取帧循环随之退出、PortAudio 回调
        finished_callback，而此时 stop() 还没轮到执行。晚一步声明意图，
        一次完全正常的收尾就会被 _finished_callback 当成设备掉线，
        用户每次结束会议都吃一条橙色 fail-closed 告警。
        """
        self._stop_event.set()

    def _run(self) -> None:
        """取帧线程：纯 Python 搬运，不含任何 PortAudio 调用。

        它是 daemon 线程，但即使被 `Py_Finalize` 腰斩也无害 —— 它手里只有
        queue 和 numpy，没有任何 CFFI 资源。这正是把 write 挪走的意义。
        """
        try:
            for frame in self._tap.frames(stop_event=self._stop_event):
                self._ring.push(np.asarray(frame, dtype=np.float32).reshape(-1))
                with self._lock:
                    self.received_frames += 1
        except Exception as exc:
            if not self._stop_event.is_set():
                self._emit_error(f"monitor playback failed: {exc}")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            if self.active:
                return
            # 流已关但上一代取帧线程还赖着（stop() 返回过 False）。这时
            # _stop_event.clear() 会把那条僵尸线程一起复活，两代线程往同一个
            # 环里塞帧。宁可让重连显式失败，也不要一个说不清状态的监听。
            raise RuntimeError(
                "上一代 monitor 取帧线程尚未结束，拒绝重复启动"
            )
        self._stop_event.clear()
        self._stopping = False
        self._played_any = False
        self._ring.clear()
        # 流的 open/start 在控制线程完成，失败直接同步抛给调用方，
        # 不再需要 _started_event/_start_error 这套跨线程回传。
        try:
            info = self._sd.query_devices(self._device)
            if int(info["max_output_channels"]) < 1:
                raise RuntimeError(
                    f"device #{self._device} no longer has an output channel"
                )
            self._device_name = str(info["name"])
            self._channels = resolve_output_channels(info["max_output_channels"])
            stream = self._sd.OutputStream(
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
            self._close_stream()
            raise
        self._thread = threading.Thread(target=self._run, name="monitor", daemon=True)
        self._thread.start()

    def _close_stream(self) -> None:
        """只允许控制线程调用：此刻没有任何线程在这条流里。"""
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

    def health_reason(self) -> str | None:
        thread = self._thread
        stream = self._stream
        if thread is None or not thread.is_alive():
            return "monitor playback worker is not running"
        if stream is None:
            return "monitor playback stream is not open"
        try:
            if not stream.active:
                return "monitor playback stream is inactive"
            info = self._sd.query_devices(self._device)
        except Exception as exc:
            return f"monitor device #{self._device} disappeared: {exc}"
        if int(info["max_output_channels"]) < 1:
            return f"monitor device #{self._device} has no output channel"
        if self._device_name is not None and str(info["name"]) != self._device_name:
            return (
                f"monitor device #{self._device} identity changed "
                f"from {self._device_name!r} to {str(info['name'])!r}"
            )
        return None

    def stop(self) -> bool:
        """收尾；返回 True 表示取帧线程已确定性结束。

        顺序是刻意的：先让取帧线程退出（它只可能阻塞在 0.2s 超时的 queue.get
        上，必然返回），再由本线程关流。反过来做就退化成"外部线程动别人的流"。
        """
        self.begin_shutdown()
        self._stopping = True
        converged = self.join()
        self._ring.clear()
        self._close_stream()
        return converged

    def join(self, timeout: float = 2.0) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def residual_thread_reason(self) -> str | None:
        """收尾后仍未结束的取帧线程 —— 供退出前的栅栏汇报。"""
        thread = self._thread
        if thread is not None and thread.is_alive():
            return f"monitor 取帧线程未在预算内结束（{thread.name}）"
        return None


class Talkback:
    """物理麦克风 → Mac_Out(绿孔) 全双工直通。

    静音 = 输出零帧：对端会议软件仍看到活跃的麦克风信号（无声），不会断流。
    """

    def __init__(self, mic_device: int, out_device: int, samplerate: int, blocksize: int):
        self._muted = threading.Event()  # set = 静音
        self._stream = sd.Stream(
            device=(mic_device, out_device),
            channels=1,
            samplerate=samplerate,
            blocksize=blocksize,
            dtype="float32",
            latency="low",
            callback=self._callback,
        )

    def _callback(self, indata, outdata, frames, time_info, status):
        if self._muted.is_set():
            outdata.fill(0)
        else:
            outdata[:] = indata

    @property
    def muted(self) -> bool:
        return self._muted.is_set()

    def toggle_mute(self) -> bool:
        if self._muted.is_set():
            self._muted.clear()
        else:
            self._muted.set()
        return self._muted.is_set()

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        self._stream.stop()
        self._stream.close()


def evaluate_burnin_interval(
    *,
    minute: int,
    interval_seconds: float,
    input_callbacks: int,
    output_callbacks: int,
    input_samples: int,
    output_samples: int,
    input_xruns: int,
    output_xruns: int,
    input_status_flags: list[str],
    output_status_flags: list[str],
    blocksize: int,
    interrupted_reason: str | None = None,
) -> dict:
    """把一段双流计数转换为稳定、可单测的烧机判定记录。"""
    reference_samples = max(input_samples, output_samples, 1)
    estimated_drops = (input_xruns + output_xruns) * blocksize
    drop_rate = estimated_drops / reference_samples
    drift_samples = input_samples - output_samples
    drift_ppm = drift_samples / reference_samples * 1_000_000.0
    stalled = interval_seconds > 0.0 and (
        input_callbacks == 0 or output_callbacks == 0
    )
    if interrupted_reason is not None:
        reason = interrupted_reason
    elif stalled:
        reason = "one or both streams produced no callbacks"
    elif drop_rate > 0.001:
        reason = f"estimated drop rate {drop_rate:.6%} exceeds 0.1%"
    else:
        reason = "ok"
    passed = interrupted_reason is None and not stalled and drop_rate <= 0.001
    return {
        "type": "minute",
        "minute": minute,
        "interval_seconds": round(interval_seconds, 6),
        "callbacks": {
            "input": input_callbacks,
            "output": output_callbacks,
        },
        "samples": {
            "input": input_samples,
            "output": output_samples,
        },
        "drops": {
            "estimated_samples": estimated_drops,
            "rate": drop_rate,
        },
        "xruns": {
            "input": input_xruns,
            "output": output_xruns,
        },
        "status_flags": {
            "input": sorted(input_status_flags),
            "output": sorted(output_status_flags),
        },
        "drift": {
            "samples": drift_samples,
            "ppm_estimate": drift_ppm,
        },
        "stream_interrupted": interrupted_reason is not None or stalled,
        "passed": passed,
        "reason": reason,
    }


class BurnInProbe:
    """独立输入/输出 PortAudio 流烧机；只量化双时钟漂移，不做补偿。"""

    def __init__(
        self,
        input_device: int,
        output_device: int,
        samplerate: int,
        blocksize: int,
        *,
        frequency: float = 440.0,
        sounddevice_module=None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if samplerate <= 0:
            raise ValueError("samplerate must be greater than zero")
        if blocksize <= 0:
            raise ValueError("blocksize must be greater than zero")
        if not math.isfinite(frequency) or frequency <= 0:
            raise ValueError("frequency must be finite and greater than zero")
        self._input_device = input_device
        self._output_device = output_device
        self._samplerate = samplerate
        self._blocksize = blocksize
        self._frequency = frequency
        self._sd = sounddevice_module or sd
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._input_stream = None
        self._output_stream = None
        self._stopping = False
        self._interrupted_reason: str | None = None
        self._phase = 0
        self._counters = {
            "input_callbacks": 0,
            "output_callbacks": 0,
            "input_samples": 0,
            "output_samples": 0,
            "input_xruns": 0,
            "output_xruns": 0,
        }
        self._status_flags = {"input": set(), "output": set()}

    def _record_status(self, direction: str, status: object) -> None:
        if not status:
            return
        with self._lock:
            self._counters[f"{direction}_xruns"] += 1
            self._status_flags[direction].add(str(status))

    def _input_callback(self, indata, frames, time_info, status) -> None:
        self._record_status("input", status)
        with self._lock:
            self._counters["input_callbacks"] += 1
            self._counters["input_samples"] += int(frames)

    def _output_callback(self, outdata, frames, time_info, status) -> None:
        self._record_status("output", status)
        indexes = np.arange(frames, dtype=np.float64) + self._phase
        outdata[:, 0] = (
            0.25
            * np.sin(
                2.0
                * np.pi
                * self._frequency
                * indexes
                / self._samplerate
            )
        ).astype(np.float32)
        self._phase += int(frames)
        with self._lock:
            self._counters["output_callbacks"] += 1
            self._counters["output_samples"] += int(frames)

    def _finished(self, direction: str) -> None:
        if self._stopping:
            return
        with self._lock:
            if self._interrupted_reason is None:
                self._interrupted_reason = (
                    f"{direction} stream became inactive"
                )

    def _snapshot(self) -> dict:
        with self._lock:
            out = dict(self._counters)
            out["input_status_flags"] = sorted(
                self._status_flags["input"]
            )
            out["output_status_flags"] = sorted(
                self._status_flags["output"]
            )
            out["interrupted_reason"] = self._interrupted_reason
        return out

    @staticmethod
    def _delta(current: dict, previous: dict, key: str) -> int:
        return int(current[key]) - int(previous[key])

    def _open(self) -> None:
        self._stopping = False
        self._input_stream = self._sd.InputStream(
            device=self._input_device,
            channels=1,
            samplerate=self._samplerate,
            blocksize=self._blocksize,
            dtype="float32",
            latency="low",
            callback=self._input_callback,
            finished_callback=lambda: self._finished("input"),
        )
        self._output_stream = self._sd.OutputStream(
            device=self._output_device,
            channels=1,
            samplerate=self._samplerate,
            blocksize=self._blocksize,
            dtype="float32",
            latency="low",
            callback=self._output_callback,
            finished_callback=lambda: self._finished("output"),
        )
        self._input_stream.start()
        self._output_stream.start()

    def _close(self) -> None:
        self._stopping = True
        for stream in (self._input_stream, self._output_stream):
            if stream is None:
                continue
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        self._input_stream = None
        self._output_stream = None

    def _check_streams(self) -> None:
        if self._interrupted_reason is not None:
            return
        for direction, stream in (
            ("input", self._input_stream),
            ("output", self._output_stream),
        ):
            try:
                active = bool(stream is not None and stream.active)
            except Exception as exc:
                active = False
                reason = f"{direction} stream health check failed: {exc}"
            else:
                reason = f"{direction} stream is inactive"
            if not active:
                with self._lock:
                    if self._interrupted_reason is None:
                        self._interrupted_reason = reason
                return

    def run(
        self,
        minutes: float,
        *,
        on_interval: Callable[[dict], None] | None = None,
        interval_seconds: float = 60.0,
    ) -> dict:
        if not math.isfinite(minutes) or minutes <= 0:
            raise ValueError("minutes must be finite and greater than zero")
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError(
                "interval_seconds must be finite and greater than zero"
            )
        duration_seconds = minutes * 60.0
        started_at = datetime.now(timezone.utc).isoformat()
        records: list[dict] = []
        startup_error: str | None = None
        try:
            self._open()
        except Exception as exc:
            startup_error = f"stream startup failed: {exc}"
            with self._lock:
                self._interrupted_reason = startup_error

        baseline = self._snapshot()
        started = self._clock()
        previous_elapsed = 0.0
        next_boundary = min(interval_seconds, duration_seconds)
        minute = 1
        try:
            while startup_error is None:
                now = self._clock()
                elapsed = min(max(now - started, 0.0), duration_seconds)
                self._check_streams()
                current = self._snapshot()
                interrupted = current["interrupted_reason"] is not None
                if elapsed >= next_boundary or interrupted:
                    record = evaluate_burnin_interval(
                        minute=minute,
                        interval_seconds=elapsed - previous_elapsed,
                        input_callbacks=self._delta(
                            current, baseline, "input_callbacks"
                        ),
                        output_callbacks=self._delta(
                            current, baseline, "output_callbacks"
                        ),
                        input_samples=self._delta(
                            current, baseline, "input_samples"
                        ),
                        output_samples=self._delta(
                            current, baseline, "output_samples"
                        ),
                        input_xruns=self._delta(
                            current, baseline, "input_xruns"
                        ),
                        output_xruns=self._delta(
                            current, baseline, "output_xruns"
                        ),
                        input_status_flags=current[
                            "input_status_flags"
                        ],
                        output_status_flags=current[
                            "output_status_flags"
                        ],
                        blocksize=self._blocksize,
                        interrupted_reason=current[
                            "interrupted_reason"
                        ],
                    )
                    record["started_at"] = started_at
                    record["elapsed_seconds"] = round(elapsed, 6)
                    records.append(record)
                    if on_interval is not None:
                        on_interval(record)
                    baseline = current
                    previous_elapsed = elapsed
                    minute += 1
                    next_boundary = min(
                        next_boundary + interval_seconds,
                        duration_seconds,
                    )
                if interrupted or elapsed >= duration_seconds:
                    break
                self._sleep(
                    min(0.1, max(next_boundary - elapsed, 0.001))
                )
        finally:
            self._close()

        if startup_error is not None:
            record = evaluate_burnin_interval(
                minute=1,
                interval_seconds=0.0,
                input_callbacks=0,
                output_callbacks=0,
                input_samples=0,
                output_samples=0,
                input_xruns=0,
                output_xruns=0,
                input_status_flags=[],
                output_status_flags=[],
                blocksize=self._blocksize,
                interrupted_reason=startup_error,
            )
            record["started_at"] = started_at
            record["elapsed_seconds"] = 0.0
            records.append(record)
            if on_interval is not None:
                on_interval(record)

        final = self._snapshot()
        passed = bool(records) and all(record["passed"] for record in records)
        total_reference = max(
            final["input_samples"],
            final["output_samples"],
            1,
        )
        total_drift_samples = (
            final["input_samples"] - final["output_samples"]
        )
        summary = {
            "type": "summary",
            "started_at": started_at,
            "minutes_requested": minutes,
            "intervals": len(records),
            "callbacks": {
                "input": final["input_callbacks"],
                "output": final["output_callbacks"],
            },
            "samples": {
                "input": final["input_samples"],
                "output": final["output_samples"],
            },
            "xruns": {
                "input": final["input_xruns"],
                "output": final["output_xruns"],
            },
            "max_drop_rate": max(
                (record["drops"]["rate"] for record in records),
                default=0.0,
            ),
            "final_drift": {
                "samples": total_drift_samples,
                "ppm_estimate": (
                    total_drift_samples
                    / total_reference
                    * 1_000_000.0
                ),
            },
            "stream_interrupted": any(
                record["stream_interrupted"] for record in records
            ),
            "passed": passed,
            "reason": "ok" if passed else records[-1]["reason"],
        }
        return summary
