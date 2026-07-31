"""Mac mini 本机音频桥、会中 AI 管线与服务端权威状态。

  下行: Mac_In → RxBus → ①--monitor 在 Mac mini 本机播放会议原声
                         ②同时 WS 广播给所有客户端(页面/mic-agent 可选收听)
  上行: MacBook mic-agent(或任何 WS 客户端)二进制帧 → 未静音时 → Mac_Out
  控制: 静音是服务端权威状态——WS JSON {"type":"mute"} 或 POST /mute 均可切换,
        即刻对上行生效并广播给所有客户端(KVM 页面面板/agent 同步显示)。

与 KVM backend/HID 链路零接触；token 防蹭听。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import signal
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .interpreter import Segment

import numpy as np
import sounddevice as sd
from aiohttp import WSCloseCode, WSMsgType, web

from . import devices as devices_module
from .bus import RxBus
from .recorder import Recorder
from .routing import MonitorPlayer, resolve_output_channels

_STATIC = Path(__file__).parent / "static" / "bridge.html"
_BACKLOG_SAMPLES = 48000  # 缓冲积压超过 1s 即丢旧，延迟兜底
DIGITAL_ZERO_ALERT = "digital-zero (TCC/线路)"
CLIPPING_ALERT = "clipping (降源音量/查增益)"
DIGITAL_ZERO_SECONDS = 30.0
CLIPPING_WINDOW_SECONDS = 10.0
CLIPPING_DBFS = -0.5
CLIPPING_RATIO_THRESHOLD = 0.01
_SENTINEL_ALERTS = frozenset({DIGITAL_ZERO_ALERT, CLIPPING_ALERT})
# 60/流（不是 120 全局）：bridge.html 每条 delta 全量 replaceChildren()、
# 无虚拟化，240 行会在 Mac mini 上肉眼卡顿。
SEGMENT_HISTORY_LIMIT_PER_STREAM = 60
# 50：建议天然低频（≥25s 节流），50 条≈一整场会。消费方是原生字幕窗口
# （MeetingCaptionsWindow.swift）与 bridge.html 调试面。
ADVICE_HISTORY_LIMIT = 50
# TASK-20260730-008 restore point: uplink protocol/UplinkPlayer stay supported,
# but bridge startup must not advertise the mothballed product client by default.
# Re-enable together with the menu/panel/default-build promotion.
_MOTHBALLED_UPLINK_PRODUCT_HINTS_ENABLED = False
# 收尾栅栏只认这些名字：它们是本进程里唯一可能碰音频设备的工作线程。
_AUDIO_WORKER_THREAD_NAMES = frozenset({
    "monitor",
    "interpreter-player",
    "recorder",
    "wav-replay",
})


def audio_shutdown_fence(
    sources: "list[Any]",
    *,
    grace_seconds: float = 2.0,
    now: "Callable[[], float] | None" = None,
    sleep: "Callable[[float], None] | None" = None,
    enumerate_threads: "Callable[[], list[threading.Thread]] | None" = None,
) -> tuple[str, ...]:
    """退出前清点音频工作线程，返回需要上报的说明（空元组=干净）。

    这是**可观测性**，不是崩溃防护 —— 说清楚很重要：写一条 note 并不会阻止
    `Py_Finalize` 里的 SIGSEGV。真正的防护是播放器一律回调驱动
    （见 `routing.PLAYER_THREAD_CONTRACT`），本栅栏只负责在那道结构性保证被
    将来某次改动破坏时，让"有线程没收干净"变成面板上一条可读的告警，
    而不是用户屏幕上一个 macOS 崩溃弹窗。

    刻意不做的事：不 `os._exit()`（会掩盖问题、且会后管线的产物已经落盘，
    没有任何需要跑路的理由），不把 daemon 改成 False（那是拿挂死换崩溃）。
    """
    monotonic = now or time.monotonic
    nap = sleep or time.sleep
    enumerate_fn = enumerate_threads or threading.enumerate

    notes: list[str] = []
    for source in sources:
        if source is None:
            continue
        getter = getattr(source, "residual_thread_reason", None)
        if getter is None:
            continue
        try:
            reason = getter()
        except Exception as exc:  # 汇报路径本身不许把收尾带崩
            reason = f"残留线程自检失败：{exc}"
        if reason:
            notes.append(reason)

    deadline = monotonic() + max(0.0, grace_seconds)
    stragglers: list[str] = []
    while True:
        stragglers = sorted({
            thread.name
            for thread in enumerate_fn()
            if thread.is_alive() and thread.name in _AUDIO_WORKER_THREAD_NAMES
        })
        if not stragglers or monotonic() >= deadline:
            break
        nap(0.05)

    if stragglers:
        notes.append(
            "退出前仍有音频工作线程存活："
            + "、".join(stragglers)
            + "（产物已落盘；请检查播放器是否又回到了阻塞写形态）"
        )
    return tuple(dict.fromkeys(notes))


class SegmentHistory:
    """Two independent per-stream rings; pairing does not exist in the data.

    Realtime translation 协议不提供任何 source↔translation 关联字段（实测一场
    会 163 原文:134 译文、一条译文覆盖两三句原文），按序配对是不可修的猜测。
    因此：每条流一个独立环（60/流）；全局单调 ``id`` 做客户端主键（取代旧
    ``t`` 微秒 hack 的去重）；append-only——永不 backfill、永不返回 None，
    一条记录发出后绝不再被改写。
    """

    def __init__(
        self,
        limit_per_stream: int = SEGMENT_HISTORY_LIMIT_PER_STREAM,
    ) -> None:
        if limit_per_stream <= 0:
            raise ValueError("segment history limit must be positive")
        self._lock = threading.Lock()
        self._streams: dict[str, deque[dict[str, Any]]] = {
            "source": deque(maxlen=limit_per_stream),
            "translation": deque(maxlen=limit_per_stream),
        }
        self._next_id = 0

    def __len__(self) -> int:
        with self._lock:
            return sum(len(items) for items in self._streams.values())

    def add(
        self,
        *,
        stream: str,
        text: str,
        t: float,
        elapsed_ms: int | None = None,
        epoch: int = 0,
    ) -> dict[str, Any]:
        if stream not in self._streams:
            raise ValueError(f"unknown segment stream: {stream}")
        sentence = text.strip()
        if not sentence:
            # SentenceAccumulator 只发非空句；空文本必然是上游契约破裂，
            # fail-fast 好过静默广播一条空字幕行。
            raise ValueError("segment text must be non-empty")
        with self._lock:
            record = {
                "id": self._next_id,
                "stream": stream,
                "text": sentence,
                "t": float(t),
                "elapsed_ms": elapsed_ms,
                "epoch": int(epoch),
            }
            self._next_id += 1
            self._streams[stream].append(record)
            return dict(record)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            merged = [
                dict(record)
                for items in self._streams.values()
                for record in items
            ]
        merged.sort(key=lambda record: record["id"])
        return merged


def draft_broadcast_payload(stream: str, text: str, epoch: int) -> dict[str, Any]:
    """字幕草稿（未成句的灰字中间态）的 WS 消息，与 segment 是两种东西。

    草稿是可变态——同一泳道后一条永远整体取代前一条，空串表示草稿已被
    正式段收编。因此它绝不进 append-only 的 SegmentHistory，也不进
    /history、不进参谋、不落盘；断线错过就错过，正式段才是事实记录。
    """
    if stream not in ("source", "translation"):
        raise ValueError(f"unknown draft stream: {stream}")
    return {
        "type": "segment_draft",
        "stream": stream,
        "text": text,
        "epoch": int(epoch),
    }


class AdviceHistory:
    """Thread-safe bounded advice history; records keep WS message shape."""

    def __init__(self, limit: int = ADVICE_HISTORY_LIMIT) -> None:
        if limit <= 0:
            raise ValueError("advice history limit must be positive")
        self._lock = threading.Lock()
        self._items: deque[dict[str, Any]] = deque(maxlen=limit)
        self._last_t = 0.0

    def add(
        self,
        markdown: str,
        *,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            stamp = float(timestamp if timestamp is not None else time.time())
            if stamp <= self._last_t:
                stamp = self._last_t + 0.000001
            self._last_t = stamp
            item = {
                "type": "advice",
                "markdown": markdown,
                "t": stamp,
            }
            self._items.append(item)
            return dict(item)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._items]


class AdviceLog:
    """会话内 jsonl 追加写（advice.jsonl / rehearsal.jsonl 共用）；
    close() 之后的迟到写入一律丢弃。

    为什么要能"封口"：advisor.stop() 的 join 只等 10s，超时后守护线程可能
    仍在投递建议，而会后 _collect_artifacts 已经在收集产物清单——不封口就
    存在"清单收完文件还在变/清单里没有文件却存在"的竞态。shutdown 顺序是
    advisor.stop() → advice_log.close() → post_pipeline.run()。

    文件按写惰性创建（零建议的会议不产生空文件），每条即写即冲，
    进程崩溃最多丢正在写的一行。

    序列化写完整 record（剥掉 WS 路由用的 type 键）：早期版本硬取
    record["markdown"]，排练段没有这个键，KeyError 逐条炸断 Realtime
    会话——2026-07-30 M1 验收实测句 2-4 译文因此全丢。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._closed = False

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(
            {k: v for k, v in record.items() if k != "type"},
            ensure_ascii=False,
        )
        with self._lock:
            if self._closed:
                return
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def close(self) -> None:
        with self._lock:
            self._closed = True


class SpeakTeePlayer:
    """正式发言的双路语音出口：会议（Mac_Out）+ 自己耳机。

    会议支路受服务端静音状态门控（菜单「静音发言」→ POST /mute）：静音时
    对方立即听不到后续日语，但自己耳机继续播——你得知道"它本来要说什么"
    才能决定何时解除静音。耳机支路 best-effort：它挂了不影响对方听到你。
    """

    def __init__(
        self,
        *,
        meeting: Any,
        own: Any | None,
        is_muted: Any,
    ) -> None:
        self._meeting = meeting
        self._own = own
        self._is_muted = is_muted

    def start(self) -> None:
        self._meeting.start()
        if self._own is not None:
            try:
                self._own.start()
            except Exception as exc:
                # 耳机支路失败只降级：正式发言的存在意义是对方听到日语
                print(f"[bridge] 发言耳机支路不可用（会议不受影响）: {exc}")
                self._own = None

    def stop(self) -> bool:
        """两条支路都必须停，哪怕第一条抛异常。

        没有 try/finally 时，meeting.stop() 一抛就再也没人停耳机支路，那条流
        与它的 PortAudio 资源会一路漏到进程退出 —— 而这个异常还会被
        shutdown_sequence 的 except 降级成一条 note，现场完全看不出来。
        """
        try:
            return bool(self._meeting.stop())
        finally:
            if self._own is not None:
                try:
                    self._own.stop()
                except Exception as exc:
                    print(f"[bridge][ALERT] 发言耳机支路收尾异常: {exc}")

    def residual_thread_reason(self) -> str | None:
        for label, player in (("会议", self._meeting), ("耳机", self._own)):
            if player is None:
                continue
            reason = getattr(player, "residual_thread_reason", lambda: None)()
            if reason is not None:
                return f"发言{label}支路：{reason}"
        return None

    def feed_pcm16(self, data: bytes) -> None:
        if not self._is_muted():
            self._meeting.feed_pcm16(data)
        if self._own is not None:
            self._own.feed_pcm16(data)


def _route_sentence_to_advisor(advisor: Any, segment: "Segment") -> None:
    """Keep the advisor boundary explicit and independently testable.

    逻辑与配对时代逐字相同，只是入参换成 Segment：任何 stream=="translation"
    的 Segment 永不进 advisor——参谋只读日语原文是硬产品边界。
    """
    if segment.stream == "source":
        if advisor is not None:
            advisor.on_sentence(segment.text)
        return
    if segment.stream == "translation":
        return
    raise ValueError(f"unknown interpreter segment stream: {segment.stream}")


class LevelSentinel:
    """下行电平纯状态机；按样本而非墙钟计算，便于合成帧验证。"""

    def __init__(
        self,
        samplerate: int,
        *,
        digital_zero_seconds: float = DIGITAL_ZERO_SECONDS,
        clipping_window_seconds: float = CLIPPING_WINDOW_SECONDS,
        clipping_dbfs: float = CLIPPING_DBFS,
        clipping_ratio: float = CLIPPING_RATIO_THRESHOLD,
    ):
        if samplerate <= 0:
            raise ValueError("samplerate must be positive")
        if not 0.0 <= clipping_ratio <= 1.0:
            raise ValueError("clipping_ratio must be between zero and one")
        self._zero_limit = max(1, round(samplerate * digital_zero_seconds))
        self._clip_window = max(
            1, round(samplerate * clipping_window_seconds)
        )
        self._clip_amplitude = 10.0 ** (clipping_dbfs / 20.0)
        self._clip_ratio = clipping_ratio
        self._lock = threading.Lock()
        self._zero_samples = 0
        self._clip_chunks: deque[np.ndarray] = deque()
        self._window_samples = 0
        self._clipped_samples = 0
        self._alerts: set[str] = set()

    @property
    def alerts(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._alerts)

    def reset(self) -> frozenset[str]:
        with self._lock:
            return self._reset_locked()

    def _reset_locked(self) -> frozenset[str]:
        self._zero_samples = 0
        self._clip_chunks.clear()
        self._window_samples = 0
        self._clipped_samples = 0
        old = frozenset(self._alerts)
        self._alerts.clear()
        return old

    def feed(self, frame: np.ndarray) -> tuple[frozenset[str], frozenset[str]]:
        with self._lock:
            return self._feed_locked(frame)

    def _feed_locked(
        self,
        frame: np.ndarray,
    ) -> tuple[frozenset[str], frozenset[str]]:
        samples = np.asarray(frame).reshape(-1)
        if samples.size == 0:
            return frozenset(), frozenset()

        nonzero = np.flatnonzero(samples != 0)
        if nonzero.size == 0:
            self._zero_samples += int(samples.size)
        else:
            self._zero_samples = int(samples.size - nonzero[-1] - 1)

        clipped = np.abs(samples) >= self._clip_amplitude
        self._clip_chunks.append(clipped)
        self._window_samples += int(clipped.size)
        self._clipped_samples += int(np.count_nonzero(clipped))
        overflow = self._window_samples - self._clip_window
        while overflow > 0 and self._clip_chunks:
            oldest = self._clip_chunks[0]
            if oldest.size <= overflow:
                self._clip_chunks.popleft()
                self._window_samples -= int(oldest.size)
                self._clipped_samples -= int(np.count_nonzero(oldest))
                overflow -= int(oldest.size)
            else:
                removed = oldest[:overflow]
                self._clip_chunks[0] = oldest[overflow:]
                self._window_samples -= int(removed.size)
                self._clipped_samples -= int(np.count_nonzero(removed))
                overflow = 0

        current: set[str] = set()
        if self._zero_samples >= self._zero_limit:
            current.add(DIGITAL_ZERO_ALERT)
        if (
            self._window_samples >= self._clip_window
            and self._clipped_samples / self._window_samples
            > self._clip_ratio
        ):
            current.add(CLIPPING_ALERT)
        raised = frozenset(current - self._alerts)
        cleared = frozenset(self._alerts - current)
        self._alerts = current
        return raised, cleared


class BridgeRuntimeState:
    """桥接进程累计状态；所有快照字段都可安全跨音频线程读取。"""

    def __init__(self) -> None:
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._lock = threading.Lock()
        self._link = "down"
        self._reason = "audio streams not started"
        self._alerts: set[str] = set()
        self._component_alerts: dict[str, str] = {}
        self._portaudio_status_flags: set[str] = set()
        self._downlink_frames = 0

    @property
    def link(self) -> str:
        with self._lock:
            return self._link

    def mark_up(self, reason: str = "ok") -> bool:
        return self._set_link("up", reason)

    def mark_down(self, reason: str) -> bool:
        return self._set_link("down", reason)

    def mark_degraded(self, reason: str) -> bool:
        return self._set_link("degraded", reason)

    def _set_link(self, link: str, reason: str) -> bool:
        if link not in {"up", "degraded", "down"}:
            raise ValueError(f"invalid link state: {link}")
        with self._lock:
            changed = self._link != link or self._reason != reason
            self._link = link
            self._reason = reason
        return changed

    def record_status_flag(self, source: str, status: str) -> bool:
        qualified = f"{source}: {status}"
        with self._lock:
            is_new = qualified not in self._portaudio_status_flags
            self._portaudio_status_flags.add(qualified)
            if self._link == "up":
                self._link = "degraded"
                self._reason = qualified
        return is_new

    def increment_downlink_frames(self) -> None:
        with self._lock:
            self._downlink_frames += 1

    def update_sentinel_alerts(
        self,
        raised: frozenset[str],
        cleared: frozenset[str],
    ) -> None:
        with self._lock:
            self._alerts.update(raised)
            self._alerts.difference_update(cleared)

    def clear_sentinel_alerts(self) -> frozenset[str]:
        with self._lock:
            cleared = frozenset(self._alerts & _SENTINEL_ALERTS)
            self._alerts.difference_update(_SENTINEL_ALERTS)
        return cleared

    def set_component_alert(
        self,
        component: str,
        message: str | None,
    ) -> None:
        with self._lock:
            if message:
                self._component_alerts[component] = message
            else:
                self._component_alerts.pop(component, None)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "link": self._link,
                "reason": self._reason,
                "alerts": sorted(
                    self._alerts | set(self._component_alerts.values())
                ),
                "downlink_frames": self._downlink_frames,
                "portaudio_status_flags": sorted(
                    self._portaudio_status_flags
                ),
                "started_at": self.started_at,
            }


class UplinkPlayer:
    """上行帧 → Mac_Out。回调驱动，缓冲空时补零（静音不断流）。"""

    enabled = True

    def __init__(
        self,
        device: int,
        samplerate: int,
        blocksize: int,
        *,
        on_status=None,
        on_error=None,
    ):
        self._device = device
        self._samplerate = samplerate
        self._blocksize = blocksize
        self._on_status = on_status
        self._on_error = on_error
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream: sd.OutputStream | None = None
        self._stopping = False
        self._device_name: str | None = None
        self._channels = 1
        self.received_frames = 0
        self.callback_count = 0
        self.played_samples = 0
        self.status_flags: set[str] = set()

    @property
    def active(self) -> bool:
        stream = self._stream
        return bool(stream is not None and stream.active)

    @property
    def backlog_samples(self) -> int:
        with self._lock:
            return sum(len(chunk) for chunk in self._chunks)

    def feed(self, data: bytes) -> None:
        arr = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        with self._lock:
            self._chunks.append(arr)
            self.received_frames += 1
            total = sum(len(a) for a in self._chunks)
            while total > _BACKLOG_SAMPLES and len(self._chunks) > 1:
                total -= len(self._chunks.pop(0))

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()

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

    def _callback(self, outdata, frames, time_info, status):
        try:
            if status:
                self._record_status(status)
            # 先在单声道缓冲里组装，再一次性摊给全部输出通道：
            # 设备是 0/2 时只写 outdata[:, 0] 会只有一只耳朵出声。
            mono = np.zeros(int(frames), dtype=np.float32)
            i = 0
            with self._lock:
                self.callback_count += 1
                self.played_samples += int(frames)
                while i < frames and self._chunks:
                    a = self._chunks[0]
                    take = min(frames - i, len(a))
                    mono[i:i + take] = a[:take]
                    if take == len(a):
                        self._chunks.pop(0)
                    else:
                        self._chunks[0] = a[take:]
                    i += take
            outdata[:] = mono.reshape(-1, 1)
        except Exception as exc:
            self._emit_error(f"uplink playback callback failed: {exc}")
            raise sd.CallbackAbort from exc

    def _finished_callback(self) -> None:
        if not self._stopping:
            self._emit_error("uplink playback stream became inactive")

    def start(self) -> None:
        if self.active:
            return
        self._stopping = False
        try:
            info = sd.query_devices(self._device)
            if int(info["max_output_channels"]) < 1:
                raise RuntimeError(
                    f"device #{self._device} no longer has an output channel"
                )
            self._device_name = str(info["name"])
            self._channels = resolve_output_channels(info["max_output_channels"])
            stream = sd.OutputStream(
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
            return "uplink playback stream is not open"
        try:
            if not stream.active:
                return "uplink playback stream is inactive"
            info = sd.query_devices(self._device)
        except Exception as exc:
            return f"uplink device #{self._device} disappeared: {exc}"
        if int(info["max_output_channels"]) < 1:
            return f"uplink device #{self._device} has no output channel"
        if self._device_name is not None and str(info["name"]) != self._device_name:
            return (
                f"uplink device #{self._device} identity changed "
                f"from {self._device_name!r} to {str(info['name'])!r}"
            )
        return None

    def stop(self) -> None:
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
        self.clear()


class MothballedUplinkPlayer:
    """发言链路封存期的 UplinkPlayer 占位：不开 Mac_Out 输出流。

    为什么不能只是"不 start"：`_state_payload`、`_AudioLifecycle`、`_ws` 的
    feed 与 finally 里的 clear 四处都无条件持有这个对象，缺一个方法就是崩溃。
    为什么要一整条流都不开：廉价 USB 声卡在"有活动流、信号全零"时耳放本底噪
    显著高于空闲态，而封存期这条流整场只吐零 —— 零收益、纯风险。

    `health_reason()` 必须返回 None：它每秒被 health_watch 轮询一次，
    返回任何字符串都会把整场会议 fail-closed 掉。
    """

    enabled = False

    def __init__(self) -> None:
        self.received_frames = 0
        self.backlog_samples = 0
        self.callback_count = 0
        self.played_samples = 0
        self.status_flags: set[str] = set()

    @property
    def active(self) -> bool:
        return False

    def feed(self, data: bytes) -> None:
        # 上行协议本身保持原样受理（客户端不会因此断开），帧在此丢弃。
        return None

    def clear(self) -> None:
        return None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def health_reason(self) -> str | None:
        return None


class _Client:
    def __init__(self, ws: web.WebSocketResponse, peer: str):
        self.ws = ws
        self.peer = peer
        self.q: asyncio.Queue = asyncio.Queue(maxsize=64)


class _AudioLifecycle:
    """整条桥接音频链的单次 start/stop；调用方决定是否人工重连。"""

    def __init__(self, bus: RxBus, uplink: UplinkPlayer, monitor) -> None:
        self.bus = bus
        self.uplink = uplink
        self.monitor = monitor

    def start(self) -> None:
        started: list[object] = []
        try:
            self.uplink.start()
            started.append(self.uplink)
            if self.monitor is not None:
                self.monitor.start()
                started.append(self.monitor)
            self.bus.start()
            started.append(self.bus)
        except Exception:
            for component in reversed(started):
                if component is self.bus:
                    self.bus.stop(close_taps=False)
                else:
                    component.stop()
            raise

    def stop(self, *, final: bool = False) -> None:
        # 先声明"这是主动收尾"再关 bus：关 tap 会让 monitor 的取帧循环退出、
        # PortAudio 回调 finished，若晚一步声明，一次完全正常的结束会议就会被
        # _finished_callback 当成设备掉线，每次都弹一条橙色 fail-closed 告警。
        if final and self.monitor is not None:
            self.monitor.begin_shutdown()
        self.bus.stop(close_taps=final)
        self.uplink.stop()
        if self.monitor is not None:
            self.monitor.stop()
        if not final:
            self.bus.discard_buffered()

    def residual_thread_reason(self) -> str | None:
        if self.monitor is None:
            return None
        getter = getattr(self.monitor, "residual_thread_reason", None)
        return getter() if getter is not None else None

    def health_failures(self) -> list[tuple[str, str]]:
        components = [
            ("capture", self.bus),
            ("uplink", self.uplink),
        ]
        if self.monitor is not None:
            components.append(("monitor", self.monitor))
        failures = []
        for source, component in components:
            reason = component.health_reason()
            if reason is not None:
                failures.append((source, reason))
        return failures


def _state_payload(app: web.Application) -> dict:
    state = app["runtime_state"].snapshot()
    tap_dropped = app["bus"].dropped_by_tap()
    interpreter_state = app.get("interpreter_state")
    interpreter = (
        interpreter_state.snapshot()
        if interpreter_state is not None
        else {
            "enabled": False,
            "connected": False,
            "lang": "zh",
            "last_source": "",
            "last_translation": "",
            "interpret_voice": False,
            "gated": False,
            "vad_dbfs": -50.0,
            "history_len": 0,
            "error": None,
        }
    )
    # 排练与 interpreter 同款快照；缺省时只需 enabled=False（客户端以此隐藏泳道）。
    rehearsal_state = app.get("rehearsal_state")
    rehearsal = (
        rehearsal_state.snapshot()
        if rehearsal_state is not None
        else {"enabled": False}
    )
    # advisor 与 interpreter 同款快照模式：AdvisorState 带锁 snapshot()，
    # 缺省 dict 的字段集必须与 snapshot() 完全一致（前端白名单按键取值）。
    advisor_state = app.get("advisor_state")
    advisor = (
        advisor_state.snapshot()
        if advisor_state is not None
        else {
            "enabled": False,
            "brief_source": "builtin",
            "brief_path": "~/AudioGateway/brief.md",
            "calls": 0,
            "delivered": 0,
            "suppressed": 0,
            "last_call_t": None,
            "last_advice_t": None,
            "last_error": None,
            "backoff_until": None,
            "brief_mismatch": False,
        }
    )
    phase_state = app.get("phase_state")
    phase = (
        phase_state.snapshot()
        if phase_state is not None
        else {
            "phase": "running",
            "post_processing_step": None,
            "session_dir": None,
            "artifacts": [],
            "post_processing_notes": [],
        }
    )
    return {
        "type": "state",
        "muted": app["muted"],
        "clients": len(app["clients"]),
        # uplink_frames/backlog 必须恒为 int：前端 parseGatewayState 有
        # typeof !== 'number' 硬门，类型不对整个面板直接白屏。
        "uplink_frames": app["uplink"].received_frames,
        "uplink_backlog_samples": app["uplink"].backlog_samples,
        "uplink_enabled": app["uplink"].enabled,
        "tap_dropped": tap_dropped,
        "taps": {
            name: {"dropped": dropped}
            for name, dropped in tap_dropped.items()
        },
        "interpreter": interpreter,
        "rehearsal": rehearsal,
        "advisor": advisor,
        **phase,
        **state,
    }


def _phase_name(app: web.Application) -> str:
    phase_state = app.get("phase_state")
    if phase_state is None:
        return "running"
    return str(phase_state.snapshot()["phase"])


async def _broadcast_json(app: web.Application, payload: dict) -> None:
    for c in list(app["clients"]):
        try:
            await c.ws.send_json(payload)
        except (ConnectionError, RuntimeError):
            pass


async def _broadcast_state(app: web.Application) -> None:
    await _broadcast_json(app, _state_payload(app))


async def _index(request: web.Request) -> web.StreamResponse:
    if request.query.get("t") != request.app["token"]:
        return web.Response(status=403, text="403: URL 需带 ?t=<token>（见网关启动输出）")
    # 告警渲染（link/reason/alerts）在 bridge.html 本体实现
    return web.FileResponse(_STATIC)


async def _status(request: web.Request) -> web.Response:
    if request.query.get("t") != request.app["token"]:
        raise web.HTTPForbidden(text="bad token")
    return web.json_response(_state_payload(request.app))


async def _stop(request: web.Request) -> web.Response:
    app = request.app
    if request.query.get("t") != app["token"]:
        raise web.HTTPForbidden(text="bad token")
    # postprocess=0：会议作废时只收尾录音，不做转写与纪要（菜单「取消这次录音」）。
    # 必须在协调器启动收尾前置位，否则 pipeline 已经开跑。
    reason = "POST /stop"
    if request.query.get("postprocess") in {"0", "false", "no"}:
        pipeline = app.get("post_pipeline")
        if pipeline is not None:
            pipeline.request_skip()
        reason = "POST /stop (取消录音)"
    accepted = await app["stop_coordinator"].request(reason)
    return web.json_response(
        {
            **_state_payload(app),
            "stop": "accepted" if accepted else "already-requested",
        },
        status=202 if accepted else 200,
        headers={"Access-Control-Allow-Origin": "*"},
    )


async def _history(request: web.Request) -> web.Response:
    app = request.app
    if request.query.get("t") != app["token"]:
        raise web.HTTPForbidden(text="bad token")
    segment_history = app.get("segment_history")
    advice_history = app.get("advice_history")
    return web.json_response(
        {
            "segments": (
                segment_history.snapshot()
                if segment_history is not None
                else []
            ),
            "advice": (
                advice_history.snapshot()
                if advice_history is not None
                else []
            ),
            # 排练段独立于会议段：新开窗口/断线重连时同样要能补拉。
            "rehearsal_segments": (
                rehearsal_history.snapshot()
                if (rehearsal_history := app.get("rehearsal_history"))
                is not None
                else []
            ),
            # 双泳道协议版本标记：只用于日志/验收断言，不上屏。
            # （sentences:[] 兼容字段已删：唯一老客户端——KVM 页面的会议音频
            # 面板——已随 2026-07-31 产品拆分移除。）
            "history_format": 2,
        },
        headers={"Access-Control-Allow-Origin": "*"},
    )


async def _close_websocket_clients(app: web.Application) -> None:
    """Close each WS only after the coordinator's phase broadcast completes."""
    for client in list(app["clients"]):
        try:
            await client.ws.close(
                code=WSCloseCode.GOING_AWAY,
                message=b"bridge post-processing",
            )
        except Exception as exc:
            print(
                f"[bridge] 客户端 {client.peer} 关闭异常，继续收尾：{exc}"
            )


async def _mute(request: web.Request) -> web.Response:
    app = request.app
    if request.query.get("t") != app["token"]:
        raise web.HTTPForbidden(text="bad token")
    if _phase_name(app) != "running":
        raise web.HTTPConflict(text="bridge is post-processing")
    body = await request.json()
    app["muted"] = bool(body.get("muted"))
    print(f"[bridge] 发言通道 {'已静音' if app['muted'] else '已开启'}")
    await _broadcast_state(app)
    return web.json_response(_state_payload(app))


async def _apply_interpret_voice(
    app: web.Application,
    enabled: bool,
) -> bool:
    interpreter = app.get("interpreter")
    if interpreter is None:
        return False
    applied = await interpreter.set_voice_enabled(enabled)
    await _broadcast_state(app)
    return applied


async def _interpret_voice(request: web.Request) -> web.Response:
    app = request.app
    if request.query.get("t") != app["token"]:
        raise web.HTTPForbidden(text="bad token")
    if _phase_name(app) != "running":
        raise web.HTTPConflict(text="bridge is post-processing")
    body = await request.json()
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise web.HTTPBadRequest(text="enabled must be a bool")
    if app.get("interpreter") is None:
        raise web.HTTPConflict(text="interpreter is not enabled")
    applied = await _apply_interpret_voice(app, enabled)
    return web.json_response(
        {
            **_state_payload(app),
            "interpret_voice_applied": applied,
        },
        status=200 if applied else 503,
        headers={"Access-Control-Allow-Origin": "*"},
    )


async def _reconnect(request: web.Request) -> web.Response:
    app = request.app
    if request.query.get("t") != app["token"]:
        raise web.HTTPForbidden(text="bad token")
    if _phase_name(app) != "running":
        raise web.HTTPConflict(text="bridge is post-processing")
    if app["runtime_state"].link == "up":
        return web.json_response(
            {
                **_state_payload(app),
                "reconnect": "not-needed",
            },
            status=409,
        )
    recovered = await app["reconnect_audio"]()
    return web.json_response(
        {
            **_state_payload(app),
            "reconnect": "ok" if recovered else "failed",
        },
        status=200 if recovered else 503,
    )


async def _ws(request: web.Request) -> web.WebSocketResponse:
    app = request.app
    if request.query.get("t") != app["token"]:
        raise web.HTTPForbidden(text="bad token")
    if _phase_name(app) != "running":
        raise web.HTTPServiceUnavailable(text="bridge is post-processing")
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
    await ws.prepare(request)
    client = _Client(ws, request.remote or "?")
    app["clients"].add(client)
    print(f"[bridge] 客户端接入: {client.peer}（共 {len(app['clients'])}）")
    await ws.send_json(_state_payload(app))
    await _broadcast_state(app)

    async def sender() -> None:
        while True:
            frame = await client.q.get()
            await ws.send_bytes(frame)

    task = asyncio.create_task(sender())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                if (
                    not app["muted"]
                    and app["runtime_state"].link != "down"
                ):
                    app["uplink"].feed(msg.data)
            elif msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "mute":
                    app["muted"] = bool(data.get("muted"))
                    print(f"[bridge] 发言通道 {'已静音' if app['muted'] else '已开启'}")
                    await _broadcast_state(app)
                elif data.get("type") == "interpret_voice":
                    enabled = data.get("enabled")
                    if isinstance(enabled, bool):
                        await _apply_interpret_voice(app, enabled)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        task.cancel()
        app["clients"].discard(client)
        if not app["clients"]:
            app["uplink"].clear()
        # close_code 用于事后区分断开原因（1000 主动关 / 1001 页面卸载 / 1006 异常）：
        # 真机排查「WS 只连上一瞬」时缺这个字段就无法归因。
        print(
            f"[bridge] 客户端断开: {client.peer}（余 {len(app['clients'])}）"
            f"close_code={ws.close_code}"
        )
        await _broadcast_state(app)
    return ws


def _local_ips() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except socket.gaierror:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("100.100.100.100", 1))  # tailscale CGNAT 段，仅取本机源地址
        ips.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    return sorted(ip for ip in ips if not ip.startswith("127."))


def run_bridge(
    cfg,
    dev,
    *,
    port: int,
    token: str | None,
    record: bool,
    no_postprocess: bool = False,
    monitor: int | None = None,
    interpret: bool = False,
    interpret_lang: str = "zh",
    interpret_device: int | None = None,
    interpret_model: str = "gpt-realtime-translate",
    openai_api_key: str = "",
    interpret_voice: bool = False,
    interpret_vad_dbfs: float | None = -50.0,
    advise: bool = False,
    anthropic_api_key: str = "",
    replay_wav: str | None = None,
    rehearse: bool = False,
    rehearse_replay: str | None = None,
    speak: bool = False,
    speak_device: int | None = None,
    speak_engine: str = "translate",
    elevenlabs_api_key: str = "",
    clone_voice_id: str = "",
    clone_model: str | None = None,
    clone_speed: float | None = None,
) -> int:
    # 发言引擎白名单在一切资源分配之前判定：错误引擎名是配置事故，
    # 绝不能静默回退到任何一个引擎（回退=用户以为在 A/B 其实在 A/A）。
    if speak_engine not in {"translate", "expressive", "clone"}:
        raise ValueError(
            f"unknown speak engine: {speak_engine!r} "
            "(expected 'translate', 'expressive' or 'clone')"
        )
    if speak_engine == "clone" and not elevenlabs_api_key.strip():
        raise ValueError("clone speak engine requires ELEVENLABS_API_KEY")
    if speak_engine == "clone" and not clone_voice_id.strip():
        raise ValueError("clone speak engine requires a voice id")
    if interpret and not openai_api_key.strip():
        raise ValueError("interpret requires OPENAI_API_KEY")
    if interpret and interpret_device is None:
        raise ValueError("interpret requires an output device")
    if advise and not interpret:
        raise ValueError("advise requires interpret")
    if advise and not anthropic_api_key.strip():
        raise ValueError("advise requires ANTHROPIC_API_KEY")
    if rehearse and not openai_api_key.strip():
        raise ValueError("rehearse requires OPENAI_API_KEY")
    if speak and not openai_api_key.strip():
        raise ValueError("speak requires OPENAI_API_KEY")
    if speak and rehearse:
        # 排练=只进耳机，正式发言=进会议。同开会让"这句话对方听没听到"
        # 变成猜谜，菜单侧也做了互斥，这里兜底。
        raise ValueError("speak and rehearse are mutually exclusive")

    from .advisor import Advisor, AdvisorState, CallbackAdvisorSink
    from .archive import TranscriptArchiver
    from .interpreter import (
        InterpreterOutputPlayer,
        InterpreterState,
        RealtimeInterpreter,
    )
    from .postmeeting import (
        BridgePhaseState,
        GracefulStopCoordinator,
        PostMeetingPipeline,
        PostMeetingResult,
        redact_error,
    )

    token = token or secrets.token_urlsafe(6)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    runtime_state = BridgeRuntimeState()
    sentinel = LevelSentinel(cfg.samplerate)
    sentinel_state_lock = threading.Lock()
    lifecycle_holder: dict[str, _AudioLifecycle] = {}
    app = web.Application()
    failure_lock = asyncio.Lock()
    reconnect_lock = asyncio.Lock()
    interpreter_state = InterpreterState(
        enabled=interpret,
        lang=interpret_lang,
        interpret_voice=interpret_voice,
        vad_dbfs=interpret_vad_dbfs,
    )
    segment_history = SegmentHistory()
    advice_history = AdviceHistory()
    # M1 排练（发言方向第一期）：耳麦中文 → 转写+日语译文只上屏，不出声。
    # 与会议方向是两个独立 Realtime 会话、两套独立的段历史，互不污染。
    rehearsal_history = SegmentHistory()
    rehearsal_state = InterpreterState(enabled=rehearse or speak, lang="ja")
    brief_path = Path.home() / "AudioGateway" / "brief.md"
    brief_exists = brief_path.is_file()
    # E-2：advisor 线程与事件循环共享的带锁状态对象，禁止裸改 dict。
    advisor_state = AdvisorState(
        enabled=advise,
        brief_source="file" if brief_exists else "builtin",
        brief_path="~/AudioGateway/brief.md",
    )

    def schedule(coro, *args) -> None:
        if loop.is_closed():
            return

        def create_task() -> None:
            if not loop.is_closed():
                asyncio.create_task(coro(*args))

        try:
            loop.call_soon_threadsafe(create_task)
        except RuntimeError:
            pass

    async def report_status(source: str, status: str) -> None:
        if runtime_state.record_status_flag(source, status):
            current_link = runtime_state.link
            print(
                f"[bridge][ALERT] PortAudio {source} 状态异常: {status}; "
                f"link={current_link}（不会自动重试）"
            )
        await _broadcast_state(app)

    async def fail_closed(source: str, reason: str) -> None:
        full_reason = f"{source}: {reason}"
        async with failure_lock:
            if runtime_state.link == "down":
                return
            runtime_state.mark_down(full_reason)
            print(
                f"[bridge][ALERT] 音频链路已 fail-closed: {full_reason}; "
                "link=down。修复设备后手动 POST /reconnect。"
            )
            lifecycle = lifecycle_holder.get("value")
            if lifecycle is not None:
                lifecycle.stop(final=False)
            await _broadcast_state(app)

    def status_callback(source: str):
        return lambda status: schedule(report_status, source, status)

    def error_callback(source: str):
        return lambda reason: schedule(fail_closed, source, reason)

    if replay_wav:
        # 回放模式：用既有录音充当采集源，整条管线（同传→字幕→参谋→落盘）
        # 在同一段语料上跑出可比对的结果——验证的是真实管线，不是仿真。
        # realtime=True 是硬要求：VAD 与按墙钟节流的逻辑在全速回放下行为会变。
        from .replay import WavReplayBus

        bus = WavReplayBus(
            replay_wav,
            blocksize=cfg.blocksize,
            realtime=True,
            pad_silence=True,
        )
        print(
            f"[bridge] 回放模式：{replay_wav}"
            f"（{bus.duration_s:.0f}s @ {bus.samplerate}Hz），采集设备未占用"
        )
    else:
        bus = RxBus(
            dev.mac_in,
            cfg.samplerate,
            cfg.blocksize,
            on_status=status_callback("capture"),
            on_error=error_callback("capture"),
        )
    bridge_tap = bus.subscribe("bridge", maxsize=64, drop_oldest=True)
    interpreter_tap = (
        bus.subscribe(
            "interpreter",
            maxsize=64,
            drop_oldest=True,
        )
        if interpret
        else None
    )
    monitor_player = None
    if monitor is not None:
        mon_tap = bus.subscribe("monitor", maxsize=8, drop_oldest=True)
        monitor_player = MonitorPlayer(
            mon_tap,
            monitor,
            cfg.samplerate,
            cfg.blocksize,
            on_status=status_callback("monitor"),
            on_error=error_callback("monitor"),
        )
    recorder = None
    session_dir = cfg.new_session_dir() if (record or interpret) else None
    phase_state = BridgePhaseState(session_dir)
    if record and session_dir is not None:
        recorder = Recorder(
            bus.subscribe("recorder"),
            session_dir / "meeting.wav",
            cfg.samplerate,
        )
        print(f"[bridge] 同步录音: {session_dir / 'meeting.wav'}")
    transcript_archiver = (
        TranscriptArchiver(session_dir, datetime.now())
        if (
            session_dir is not None
            and (interpret or (record and not no_postprocess))
        )
        else None
    )
    if transcript_archiver is not None:
        print(
            f"[bridge] 转写归档: "
            f"{session_dir / 'transcript.jsonl'}"
        )

    # 发言链路封存期不打开 Mac_Out 输出流：廉价 USB 声卡在"有活动流、信号全零"
    # 时耳放本底噪显著高于空闲态，而封存期这条流整场只吐零——零收益、纯风险。
    uplink = (
        UplinkPlayer(
            dev.mac_out,
            cfg.samplerate,
            cfg.blocksize,
            on_status=status_callback("uplink"),
            on_error=error_callback("uplink"),
        )
        if _MOTHBALLED_UPLINK_PRODUCT_HINTS_ENABLED
        else MothballedUplinkPlayer()
    )
    lifecycle = _AudioLifecycle(bus, uplink, monitor_player)
    lifecycle_holder["value"] = lifecycle

    app["token"] = token
    app["uplink"] = uplink
    app["bus"] = bus
    app["runtime_state"] = runtime_state
    app["phase_state"] = phase_state
    app["interpreter_state"] = interpreter_state
    app["segment_history"] = segment_history
    app["advice_history"] = advice_history
    app["advisor_state"] = advisor_state
    app["rehearsal_state"] = rehearsal_state
    app["rehearsal_history"] = rehearsal_history
    app["clients"] = set()
    app["muted"] = False

    interpreter = None
    interpreter_task = None
    # 收尾栅栏要清点的播放器。收听侧与发言侧各一个句柄，缺一个就等于
    # 那条链路的残留线程永远没人汇报。
    interpreter_output_player: Any = None
    speak_tee: Any = None
    advisor = None
    interpreter_started_at = time.monotonic()

    def on_interpreter_segment(segment) -> None:
        # 时间基准：monotonic - interpreter_started_at 是整场会议唯一可信的
        # 时间轴；elapsed_ms 只随记录带出供 UI 显示（VAD 吞静音、重连从 0
        # 重计，官方明文不是唯一标识），绝不用于排序或落盘键。
        t = time.monotonic() - interpreter_started_at
        # Hard product boundary: translation segments never enter Advisor.
        _route_sentence_to_advisor(advisor, segment)
        record = segment_history.add(
            stream=segment.stream,
            text=segment.text,
            t=t,
            elapsed_ms=segment.elapsed_ms,
            epoch=segment.epoch,
        )
        interpreter_state.set_history_len(len(segment_history))
        schedule(
            _broadcast_json,
            app,
            {"type": "segment", **record},
        )
        # 来一条写一条：双泳道落盘没有"等待配对"的中间态，也就不需要任何
        # 冲刷函数；断线残句由 interpreter._flush_text() 走同一条路径进来。
        if transcript_archiver is not None:
            transcript_archiver.add_realtime_segment(
                stream=segment.stream,
                text=segment.text,
                t=t,
                lang=(
                    interpret_lang
                    if segment.stream == "translation"
                    else None
                ),
            )

    def on_interpreter_draft(stream: str, text: str, epoch: int) -> None:
        # 草稿只走 WS 直发（见 draft_broadcast_payload 的产品边界）。
        schedule(
            _broadcast_json,
            app,
            draft_broadcast_payload(stream, text, epoch),
        )

    def on_interpreter_state(snapshot: dict) -> None:
        error = snapshot.get("error")
        runtime_state.set_component_alert(
            "interpreter",
            f"interpreter: {error}" if error else None,
        )
        schedule(_broadcast_state, app)

    if interpret:
        assert interpreter_tap is not None
        assert interpret_device is not None
        output_player = InterpreterOutputPlayer(interpret_device)
        interpreter_output_player = output_player
        interpreter = RealtimeInterpreter(
            interpreter_tap,
            output_player,
            api_key=openai_api_key,
            lang=interpret_lang,
            model=interpret_model,
            state=interpreter_state,
            on_state=on_interpreter_state,
            on_sentence=on_interpreter_segment,
            on_draft=on_interpreter_draft,
            vad_dbfs=interpret_vad_dbfs,
        )
        app["interpreter"] = interpreter

    # ---- 发言方向：排练（M1/M1.5）与正式发言（M2）------------------------
    #
    # 与会议（收听）方向是两个独立 Realtime 会话；发言段走独立的历史环、独立
    # 的 WS 消息类型（rehearsal_segment）与独立落盘（rehearsal.jsonl）。
    # 硬边界：① 绝不进参谋（参谋只读会议对方原文）；② 绝不进会议
    # transcript.jsonl；③ 语音路由按模式：排练→只进自己耳机，正式发言→
    # Mac_Out 进会议+自己耳机（会议支路受静音门控）。
    rehearsal = None
    rehearsal_task = None
    rehearsal_bus = None
    rehearsal_log = None
    rehearsal_started_at = time.monotonic()

    def on_rehearsal_segment(segment) -> None:
        t = time.monotonic() - rehearsal_started_at
        record = rehearsal_history.add(
            stream=segment.stream,
            text=segment.text,
            t=t,
            elapsed_ms=segment.elapsed_ms,
            epoch=segment.epoch,
        )
        rehearsal_state.set_history_len(len(rehearsal_history))
        schedule(
            _broadcast_json,
            app,
            {"type": "rehearsal_segment", **record},
        )
        # 独立落盘供 M1 测量（延迟分布、转写/翻译质量抽查），
        # 不与会议 transcript 混写——排练不是会议内容。
        if rehearsal_log is not None:
            rehearsal_log.append(record)

    def on_rehearsal_state(snapshot: dict) -> None:
        error = snapshot.get("error")
        # 中文前缀同「参谋:」：CJK 排 ASCII 之后，排练故障永不顶掉字幕告警。
        runtime_state.set_component_alert(
            "rehearsal",
            "排练: 发言排练暂时不可用（自动重连中，会议不受影响）"
            if error
            else None,
        )
        schedule(_broadcast_state, app)

    class _NullRehearsalPlayer:
        """无安全输出设备时的占位：音频 delta 在进播放器前就被丢弃
        （interpret_voice=False），任何方法被调用都说明语义被破坏了。"""

        def start(self) -> None:
            raise AssertionError("排练播放器不该被启动（无声形态）")

        def stop(self) -> None:
            pass

        def feed_pcm16(self, data: bytes) -> None:
            raise AssertionError("排练音频不该被播放（无声形态）")

    if rehearse or speak:
        if rehearse_replay:
            from .replay import WavReplayBus

            rehearsal_bus = WavReplayBus(
                rehearse_replay,
                blocksize=cfg.blocksize,
                realtime=True,
                pad_silence=True,
            )
            print(f"[bridge] 排练回归模式：{rehearse_replay}")
        else:
            mic_idx, mic_note = devices_module.resolve_rehearsal_mic_following_default(
                dev.mac_in,
            )
            print(f"[bridge] {mic_note}", flush=True)
            if mic_idx < 0:
                # 没有安全麦克风：排练/正式发言都无从谈起，会议本身照常
                rehearse = False
                speak = False
                rehearsal_state.set_error(mic_note)
                rehearsal_state.set_connected(False)
            else:
                # 排练麦克风故障只降级不 fail-closed：会议录音字幕照常。
                def on_rehearsal_mic_error(reason: str) -> None:
                    runtime_state.set_component_alert(
                        "rehearsal",
                        "排练: 麦克风中断（会议不受影响）",
                    )
                    print(f"[bridge] 排练麦克风中断: {reason}", flush=True)
                    schedule(_broadcast_state, app)

                rehearsal_bus = RxBus(
                    mic_idx,
                    cfg.samplerate,
                    cfg.blocksize,
                    on_status=status_callback("rehearsal-mic"),
                    on_error=on_rehearsal_mic_error,
                )
        if rehearsal_bus is not None:
            # 语音路由（M1.5/M2）：
            #   排练 → 只进自己耳机（与麦克风同一台耳麦，零声学泄漏）；
            #   正式发言 → 进会议（Mac_Out）+ 同时进自己耳机——听得到
            #   "自己"的日语说到哪，才能掌握发言节奏。
            # 找不到安全耳机输出时排练退回纯字幕；正式发言仍进会议。
            own_device = -1
            if not rehearse_replay:
                own_device, own_note = (
                    devices_module.resolve_own_voice_output(
                        mic_idx,
                        speak_device if speak_device is not None else dev.mac_out,
                    )
                )
                print(f"[bridge] {own_note}", flush=True)
            own_player = (
                InterpreterOutputPlayer(own_device)
                if own_device >= 0
                else None
            )
            player: Any
            if speak:
                # 会议支路受服务端静音状态门控（菜单「静音发言」/POST /mute）。
                # 本机开会时发言目标与收听源必须是不同设备（否则自己的
                # 日语回流进采集形成翻译回环）；未显式指定则沿用同一块声卡
                # 的输出端（KVM 场景：绿孔进被控机）。
                meeting_out = (
                    speak_device if speak_device is not None else dev.mac_out
                )
                meeting_player = InterpreterOutputPlayer(meeting_out)
                player = SpeakTeePlayer(
                    meeting=meeting_player,
                    own=own_player,
                    is_muted=lambda: bool(app["muted"]),
                )
                print(
                    "[bridge] 正式发言已开启（M2）：日语进入会议，"
                    "同时在你的耳机中同步播放。",
                    flush=True,
                )
            elif own_player is not None:
                player = own_player
            else:
                player = _NullRehearsalPlayer()
            speak_tee = player
            voice_on = speak or own_player is not None
            if voice_on:
                rehearsal_state.set_interpret_voice(True)
            rehearsal_tap = rehearsal_bus.subscribe(
                "rehearsal", maxsize=64, drop_oldest=True
            )
            if speak_engine == "expressive":
                # A/B 追加项（默认不启用）：通用 GA 端点 + marin 声线换
                # 表现力；模型/URL/指令由 ExpressiveSpeechSession 自带，
                # 不复用 interpret_model（那是 translations 端点的模型名）。
                from .expressive import ExpressiveSpeechSession

                rehearsal = ExpressiveSpeechSession(
                    rehearsal_tap,
                    player,
                    api_key=openai_api_key,
                    lang="ja",
                    state=rehearsal_state,
                    on_state=on_rehearsal_state,
                    on_sentence=on_rehearsal_segment,
                    vad_dbfs=interpret_vad_dbfs,
                )
            elif speak_engine == "clone":
                # M3 声纹克隆：文本链路与 translate 引擎逐字相同
                # （translations 端点 + interpret_model），只把音频出口
                # 换成 ElevenLabs 克隆声线（详见 voiceclone.py 模块头）。
                from .voiceclone import (
                    DEFAULT_CLONE_MODEL,
                    DEFAULT_CLONE_SPEED,
                    CloneSpeechSession,
                )

                rehearsal = CloneSpeechSession(
                    rehearsal_tap,
                    player,
                    api_key=openai_api_key,
                    elevenlabs_api_key=elevenlabs_api_key,
                    voice_id=clone_voice_id,
                    clone_model=clone_model or DEFAULT_CLONE_MODEL,
                    clone_speed=(
                        clone_speed
                        if clone_speed is not None
                        else DEFAULT_CLONE_SPEED
                    ),
                    lang="ja",
                    model=interpret_model,
                    state=rehearsal_state,
                    on_state=on_rehearsal_state,
                    on_sentence=on_rehearsal_segment,
                    vad_dbfs=interpret_vad_dbfs,
                )
            else:
                rehearsal = RealtimeInterpreter(
                    rehearsal_tap,
                    player,
                    api_key=openai_api_key,
                    lang="ja",
                    model=interpret_model,
                    state=rehearsal_state,
                    on_state=on_rehearsal_state,
                    on_sentence=on_rehearsal_segment,
                    vad_dbfs=interpret_vad_dbfs,
                )
            if session_dir is not None:
                rehearsal_log = AdviceLog(session_dir / "rehearsal.jsonl")

    # 建议落盘与广播共用同一条 AdviceHistory 记录（同一个 t 主键）。
    # advise ⇒ interpret ⇒ session_dir 存在；防御式判 None 只为测试注入。
    advice_log = (
        AdviceLog(session_dir / "advice.jsonl")
        if (advise and session_dir is not None)
        else None
    )

    def on_advice(markdown: str) -> None:
        record = advice_history.add(markdown)
        if advice_log is not None:
            advice_log.append(record)
        schedule(_broadcast_json, app, record)

    def on_advisor_alert(message: str | None) -> None:
        # 中文前缀"参谋:"是刻意的双保险：① Swift userAlerts() 按它映射成
        # 中文 guidance；② alerts 是服务端 sorted()，CJK 排在 ASCII 之后，
        # 参谋降级不会把 "interpreter:"（字幕中断）的引导挤出 alerts.first。
        runtime_state.set_component_alert("advisor", message)
        schedule(_broadcast_state, app)

    if advise:
        cfg.advisor = True
        cfg.advisor_backend = "claude"
        cfg.advisor_brief = str(brief_path) if brief_exists else None
        advisor = Advisor(
            cfg,
            CallbackAdvisorSink(on_advice),
            state=advisor_state,
            on_alert=on_advisor_alert,
        )

    async def report_sentinel_change(
        raised: frozenset[str],
        cleared: frozenset[str],
    ) -> None:
        active = set(runtime_state.snapshot()["alerts"])
        for alert in sorted(raised & active):
            print(f"[bridge][ALERT] {alert}", flush=True)
        for alert in sorted(cleared - active):
            print(f"[bridge] 告警恢复: {alert}", flush=True)
        await _broadcast_state(app)

    async def reconnect_audio() -> bool:
        async with reconnect_lock:
            if runtime_state.link == "up":
                return True
            runtime_state.mark_degraded("manual reconnect in progress")
            print("[bridge] 收到人工恢复请求：音频链路只尝试重开一次。")
            await _broadcast_state(app)
            lifecycle.stop(final=False)
            with sentinel_state_lock:
                sentinel.reset()
                cleared = runtime_state.clear_sentinel_alerts()
            for alert in sorted(cleared):
                print(f"[bridge] 告警恢复（重连重置窗口）: {alert}", flush=True)
            try:
                lifecycle.start()
            except Exception as exc:
                runtime_state.mark_down(
                    f"manual reconnect failed: {exc}"
                )
                lifecycle.stop(final=False)
                print(
                    "[bridge][ALERT] 人工恢复失败；保持 link=down，"
                    f"未自动重试: {exc}"
                )
                await _broadcast_state(app)
                return False
            runtime_state.mark_up("manual reconnect succeeded")
            print("[bridge] 人工恢复成功：link=up。")
            await _broadcast_state(app)
            return True

    health_task: asyncio.Task[None] | None = None

    def on_post_processing_step(step: str) -> None:
        phase_state.set_step(step)
        schedule(_broadcast_state, app)

    post_pipeline = PostMeetingPipeline(
        cfg=cfg,
        session_dir=session_dir,
        recorder=recorder,
        archiver=transcript_archiver,
        no_postprocess=no_postprocess,
        on_step=on_post_processing_step,
    )
    app["post_pipeline"] = post_pipeline   # 供 /stop?postprocess=0 运行时取消
    shutdown_secrets = tuple(
        value
        for value in (openai_api_key, anthropic_api_key)
        if value
    )

    def shutdown_error(exc: BaseException) -> str:
        return redact_error(exc, shutdown_secrets)

    async def shutdown_sequence(reason: str) -> None:
        shutdown_notes: list[str] = []
        print(f"\n[bridge] 结束中（{reason}）…")

        if health_task is not None:
            health_task.cancel()
            await asyncio.gather(health_task, return_exceptions=True)

        await _close_websocket_clients(app)

        if interpreter is not None:
            try:
                await interpreter.stop()
                if interpreter_task is not None:
                    await asyncio.gather(
                        interpreter_task,
                        return_exceptions=True,
                    )
            except Exception as exc:
                shutdown_notes.append(
                    "同传 session.close/session.closed 收尾异常："
                    f"{shutdown_error(exc)}"
                )
        if rehearsal is not None:
            try:
                await rehearsal.stop()
                if rehearsal_task is not None:
                    await asyncio.gather(
                        rehearsal_task,
                        return_exceptions=True,
                    )
            except Exception as exc:
                shutdown_notes.append(
                    f"排练会话收尾异常：{shutdown_error(exc)}"
                )
        if rehearsal_bus is not None:
            try:
                await asyncio.to_thread(rehearsal_bus.stop)
            except Exception as exc:
                shutdown_notes.append(
                    f"排练麦克风收尾异常：{shutdown_error(exc)}"
                )
        if rehearsal_log is not None:
            # 与 advice_log 同理：在产物清单收集之前封口。
            rehearsal_log.close()
        if advisor is not None:
            try:
                await asyncio.to_thread(advisor.stop)
            except Exception as exc:
                shutdown_notes.append(
                    f"参谋收尾异常：{shutdown_error(exc)}"
                )
        if advice_log is not None:
            # advisor.stop() 的 join 只等 10s：超时后守护线程可能仍在投递。
            # 在 _collect_artifacts（post_pipeline.run 内）之前封口，保证
            # 产物清单收集之后 advice.jsonl 不再变化。
            advice_log.close()

        try:
            await asyncio.to_thread(lifecycle.stop, final=True)
        except Exception as exc:
            shutdown_notes.append(
                f"音频流收尾异常：{shutdown_error(exc)}"
            )

        try:
            result = await asyncio.to_thread(post_pipeline.run)
        except Exception as exc:
            shutdown_notes.append(
                f"会后处理未预期异常：{shutdown_error(exc)}"
            )
            result = PostMeetingResult((), ())

        # 产物已落盘，再清点音频线程：栅栏的结论要能进 notes / 面板，
        # 而不是等进程崩了让用户去翻 DiagnosticReports。
        try:
            fence_notes = await asyncio.to_thread(
                audio_shutdown_fence,
                [lifecycle, speak_tee, interpreter_output_player],
            )
        except Exception as exc:
            fence_notes = (f"音频线程清点失败：{shutdown_error(exc)}",)
        for note in fence_notes:
            print(f"[bridge][ALERT] {note}")
        shutdown_notes.extend(fence_notes)

        merged_notes = tuple(shutdown_notes) + result.notes
        phase_state.finish(
            artifacts=result.artifacts,
            notes=merged_notes,
        )
        await _broadcast_state(app)
        print(
            "[bridge] phase=done"
            + (
                f"；附加说明：{'；'.join(shutdown_notes)}"
                if shutdown_notes
                else ""
            )
        )
        # 给 2 秒轮询的菜单栏 App 留出一次可见的 done 状态窗口。
        await asyncio.sleep(2.5)
        loop.stop()

    stop_coordinator = GracefulStopCoordinator(
        phase_state,
        lambda: _broadcast_state(app),
        shutdown_sequence,
    )

    app["reconnect_audio"] = reconnect_audio
    app["stop_coordinator"] = stop_coordinator
    app.router.add_get("/", _index)
    app.router.add_get("/ws", _ws)
    app.router.add_get("/status", _status)
    app.router.add_get("/history", _history)
    app.router.add_post("/stop", _stop)
    app.router.add_post("/mute", _mute)
    app.router.add_post("/interpret_voice", _interpret_voice)
    app.router.add_post("/reconnect", _reconnect)

    def pump() -> None:
        for frame in bridge_tap.frames():
            runtime_state.increment_downlink_frames()
            with sentinel_state_lock:
                raised, cleared = sentinel.feed(frame)
                if raised or cleared:
                    runtime_state.update_sentinel_alerts(raised, cleared)
            if raised or cleared:
                schedule(report_sentinel_change, raised, cleared)
            data = (np.clip(frame, -1.0, 1.0) * 32767).astype(np.int16).tobytes()

            def _put(d: bytes = data) -> None:
                for c in list(app["clients"]):
                    if c.q.full():
                        try:
                            c.q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    c.q.put_nowait(d)

            loop.call_soon_threadsafe(_put)

    async def health_watch() -> None:
        while True:
            await asyncio.sleep(1.0)
            if runtime_state.link != "down":
                failures = lifecycle.health_failures()
                if failures:
                    source, reason = failures[0]
                    await fail_closed(source, reason)
                    continue
            await _broadcast_state(app)

    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "0.0.0.0", port)
    loop.run_until_complete(site.start())

    if interpreter is not None:
        if advisor is not None:
            advisor.start()
            print(
                "[bridge] 参谋已开启：只读取会议日语原文；"
                f"brief={'~/AudioGateway/brief.md' if brief_exists else '内置通用 brief'}。"
            )
        interpreter_task = interpreter.start(loop)
        print(
            "[bridge] Realtime 同传已开启："
            f"lang={interpret_lang} model={interpret_model} "
            f"output_device=#{interpret_device}；"
            f"译文语音={'开' if interpret_voice else '关（默认）'}；"
            "会议原声在 Mac mini 本机播放，译文播放器与会议上行物理隔离。"
        )
    if rehearsal is not None and rehearsal_bus is not None:
        try:
            rehearsal_bus.start()
        except Exception as exc:
            # 麦克风在解析与启动之间消失：降级不阻断，会议照常。
            rehearsal_state.set_error(str(exc))
            rehearsal_state.set_connected(False)
            print(f"[bridge] 排练麦克风启动失败（会议不受影响）: {exc}", flush=True)
            rehearsal = None
        else:
            rehearsal_task = rehearsal.start(loop)
            print(
                "[bridge] 正式发言已就绪：说中文，对方听到日语。"
                "菜单「静音发言」可随时切断会议支路。"
                if speak
                else "[bridge] 发言排练已开启：中文转写+日语译文上屏"
                "（有耳机则同步播放），不注入会议、不进参谋、不进会议转写。"
            )
            # 引擎名必须出现在启动日志：A/B 场次事后归因全靠这一行。
            print(f"[bridge] 发言引擎: {speak_engine}", flush=True)
    if recorder:
        recorder.start()
    threading.Thread(target=pump, name="bridge-pump", daemon=True).start()
    try:
        lifecycle.start()
    except Exception as exc:
        runtime_state.mark_down(f"audio startup failed: {exc}")
        lifecycle.stop(final=False)
        print(
            "[bridge][ALERT] 控制面已启动，但音频链路启动失败；"
            f"link=down，未自动重试: {exc}"
        )
    else:
        runtime_state.mark_up()
        if monitor_player:
            print("[bridge] 下行监听已开启（会议声音在 Mac mini 本机播放）")

    health_task = loop.create_task(health_watch())
    print(
        "[bridge] 控制面就绪。"
        f"音频链路={runtime_state.snapshot()['link']}。"
    )
    for ip in _local_ips() or ["<本机IP>"]:
        print(f"         面板:      http://{ip}:{port}/?t={token}")
        if _MOTHBALLED_UPLINK_PRODUCT_HINTS_ENABLED:
            print(
                "         mic-agent: python -m audio_gateway micagent"
                f" --url http://{ip}:{port} --token {token}"
            )
        print(
            "         恢复入口:  "
            f"curl -X POST 'http://{ip}:{port}/reconnect?t={token}'"
        )
        print(
            "         停止入口:  "
            f"curl -X POST 'http://{ip}:{port}/stop?t={token}'"
        )
    print(
        "[bridge] 用「会议助手」菜单“结束会议并生成纪要”结束；"
        "Ctrl+C/SIGTERM 走同一优雅停止管线。"
    )

    installed_signal_handlers: list[signal.Signals] = []
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                stop_signal,
                lambda current=stop_signal: asyncio.create_task(
                    stop_coordinator.request(current.name)
                ),
            )
            installed_signal_handlers.append(stop_signal)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        loop.create_task(stop_coordinator.request("SIGINT"))
        loop.run_forever()
    finally:
        if phase_state.snapshot()["phase"] != "done":
            loop.create_task(stop_coordinator.request("event loop exit"))
            loop.run_forever()
        if stop_coordinator.task is not None:
            loop.run_until_complete(
                asyncio.gather(
                    stop_coordinator.task,
                    return_exceptions=True,
                )
            )
        for stop_signal in installed_signal_handlers:
            loop.remove_signal_handler(stop_signal)
        loop.run_until_complete(runner.cleanup())
        loop.close()
    return 0
