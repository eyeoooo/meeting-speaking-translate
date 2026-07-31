"""播放器收尾契约：没有任何 Python 线程可以带着 PortAudio 活到解释器退出。

2026-07-30 的 SIGSEGV 事故里，一个 daemon 播放线程永久阻塞在
`Pa_WriteStream`，join(2.0) 超时后无人过问，进程照常 `Py_Finalize` →
cffi 的 Library 被回收 → `dlclose(libportaudio)` → 线程脚下的代码页被
unmap → 下一次取指 SIGSEGV（现场地址恒为 `WriteStream+68`）。

152 个既有用例一个都没接住，因为它们从不跑播放器的真实线程生命周期。
本文件补的就是这一层。**注意边界**：真机上 abort 一条被 `Pa_WriteStream`
阻塞的流会永久楔死，而在 BlackHole 上同样的调用会正常返回 —— 这是设备相关
的，假流永远复现不了。所以这里锁的是**代码形态**（谁碰流、还剩几个线程），
真正的回归判据仍然是真机跑 speak 会话后 DiagnosticReports 不长新的 .ips。
"""

from __future__ import annotations

import threading
import unittest

import numpy as np

from audio_gateway.bridge import (
    SpeakTeePlayer,
    audio_shutdown_fence,
)
from audio_gateway.interpreter import InterpreterOutputPlayer
from audio_gateway.routing import MonitorPlayer, RingBuffer


class _FakeStream:
    """假 OutputStream：记录每个 PortAudio 动作发生在哪个线程上。"""

    def __init__(self, *, channels: int, callback, finished_callback=None, **_):
        self._channels = channels
        self._callback = callback
        self._finished_callback = finished_callback
        self.active = False
        self.calls: list[tuple[str, int]] = []
        self.written: list[np.ndarray] = []

    def _log(self, action: str) -> None:
        self.calls.append((action, threading.get_ident()))

    def start(self) -> None:
        self._log("start")
        self.active = True

    def abort(self) -> None:
        self._log("abort")
        self.active = False
        if self._finished_callback is not None:
            self._finished_callback()

    def stop(self) -> None:
        self._log("stop")
        self.active = False

    def close(self) -> None:
        self._log("close")
        self.active = False

    def write(self, data):  # pragma: no cover - 存在即失败
        raise AssertionError(
            "阻塞式 stream.write() 是本次崩溃的直接成因，播放器不得再调用它"
        )

    def pump(self, frames: int, status=None) -> np.ndarray:
        """手工驱动一次 PortAudio 回调，返回设备实际收到的样本。"""
        outdata = np.zeros((frames, self._channels), dtype=np.float32)
        self._callback(outdata, frames, None, status or 0)
        self.written.append(outdata.copy())
        return outdata

    def actions(self) -> list[str]:
        return [name for name, _ in self.calls]

    def threads_for(self, action: str) -> set[int]:
        return {ident for name, ident in self.calls if name == action}


class _FakeSounddevice:
    CallbackAbort = RuntimeError

    def __init__(self, *, max_output_channels: int = 2, name: str = "Fake Out"):
        self._info = {
            "name": name,
            "max_output_channels": max_output_channels,
            "max_input_channels": 0,
        }
        self.streams: list[_FakeStream] = []

    def query_devices(self, device=None):
        return dict(self._info)

    def OutputStream(self, **kwargs):  # noqa: N802 - 镜像 sounddevice 的 API
        stream = _FakeStream(**kwargs)
        self.streams.append(stream)
        return stream


def _pcm16(values: list[int]) -> bytes:
    return np.array(values, dtype=np.int16).tobytes()


class RingBufferTests(unittest.TestCase):
    def test_pull_zero_pads_when_starved(self) -> None:
        ring = RingBuffer(100)
        ring.push(np.array([1.0, 2.0], dtype=np.float32))
        out, filled = ring.pull(4)
        self.assertEqual(2, filled)
        np.testing.assert_allclose([1.0, 2.0, 0.0, 0.0], out)

    def test_overflow_drops_oldest_not_newest(self) -> None:
        # 听感允许丢、延迟不允许涨：积压时必须丢最旧的，
        # 丢最新的会让用户永远听着一段越来越滞后的声音。
        ring = RingBuffer(4)
        ring.push(np.arange(4, dtype=np.float32))
        ring.push(np.array([9.0, 9.0], dtype=np.float32))
        out, filled = ring.pull(4)
        self.assertEqual(4, filled)
        np.testing.assert_allclose([2.0, 3.0, 9.0, 9.0], out)
        self.assertEqual(2, ring.dropped_samples)

    def test_partial_chunk_is_preserved_across_pulls(self) -> None:
        ring = RingBuffer(100)
        ring.push(np.array([1.0, 2.0, 3.0], dtype=np.float32))
        first, _ = ring.pull(2)
        second, filled = ring.pull(2)
        np.testing.assert_allclose([1.0, 2.0], first)
        np.testing.assert_allclose([3.0, 0.0], second)
        self.assertEqual(1, filled)


class InterpreterOutputPlayerShutdownTests(unittest.TestCase):
    def test_player_owns_no_thread_and_never_blocking_writes(self) -> None:
        # 崩溃的充要前置条件是"存在一个卡在 Pa_WriteStream 里的播放线程"。
        # 结构上不再创建该线程，这个条件就永远无法成立。
        fake = _FakeSounddevice()
        before = {t.ident for t in threading.enumerate()}
        player = InterpreterOutputPlayer(0, sounddevice_module=fake)
        player.start()
        player.feed_pcm16(_pcm16([1000] * 480))
        after = {t.ident for t in threading.enumerate()}

        self.assertEqual(before, after, "播放器不得创建任何工作线程")
        self.assertFalse(
            any(t.name == "interpreter-player" for t in threading.enumerate())
        )
        self.assertTrue(player.stop())
        # _FakeStream.write 会直接 AssertionError，走到这里即证明没调过。
        self.assertNotIn("write", fake.streams[0].actions())

    def test_stop_closes_stream_on_the_calling_thread(self) -> None:
        # "谁开流谁碰流"：abort/close 必须发生在调用 stop() 的那个线程上，
        # 而不是从外部去动别人正在写的流。
        fake = _FakeSounddevice()
        player = InterpreterOutputPlayer(0, sounddevice_module=fake)
        player.start()
        stream = fake.streams[0]

        stop_thread_ident: list[int] = []

        def do_stop() -> None:
            stop_thread_ident.append(threading.get_ident())
            player.stop()

        worker = threading.Thread(target=do_stop)
        worker.start()
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive())

        self.assertEqual(["start", "abort", "close"], stream.actions())
        self.assertEqual(set(stop_thread_ident), stream.threads_for("abort"))
        self.assertEqual(set(stop_thread_ident), stream.threads_for("close"))

    def test_stop_is_idempotent_and_reports_convergence(self) -> None:
        fake = _FakeSounddevice()
        player = InterpreterOutputPlayer(0, sounddevice_module=fake)
        player.start()
        self.assertTrue(player.stop())
        self.assertTrue(player.stop())
        self.assertIsNone(player.residual_thread_reason())
        self.assertEqual(["start", "abort", "close"], fake.streams[0].actions())

    def test_fed_audio_reaches_the_device_callback(self) -> None:
        fake = _FakeSounddevice(max_output_channels=2)
        player = InterpreterOutputPlayer(0, sounddevice_module=fake)
        player.start()
        # 24k PCM16 → 48k float32，两倍上采样。喂满抖动预缓冲门限（400ms）
        # ——低于门限的首包会被扣在攒批态，这是防句中空洞的刻意行为。
        player.feed_pcm16(_pcm16([16384] * (24_000 // 2)))
        out = fake.streams[0].pump(48)
        # 单声道源必须摊到全部输出通道，否则戴耳机只有一只耳朵出声。
        np.testing.assert_allclose(out[:, 0], out[:, 1])
        self.assertGreater(float(np.abs(out).max()), 0.1)
        player.stop()

    def test_stop_discards_pending_translated_audio(self) -> None:
        # 结束/关闭译文语音之后，绝不能再有日语尾巴继续流向会议。
        fake = _FakeSounddevice()
        player = InterpreterOutputPlayer(0, sounddevice_module=fake)
        player.start()
        player.feed_pcm16(_pcm16([16384] * 2400))
        self.assertGreater(player.backlog_samples, 0)
        player.stop()
        self.assertEqual(0, player.backlog_samples)

    def test_start_failure_leaves_no_stream_behind(self) -> None:
        fake = _FakeSounddevice(max_output_channels=0)
        player = InterpreterOutputPlayer(0, sounddevice_module=fake)
        with self.assertRaises(RuntimeError):
            player.start()
        self.assertFalse(player.active)
        self.assertEqual([], fake.streams)
        self.assertIsNone(player.residual_thread_reason())


class _BlockingTap:
    """取帧循环无视 stop_event 的病态 tap，用来制造 join 超时。"""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()

    def frames(self, *, stop_event=None, poll_seconds=0.2):
        self.entered.set()
        # 有上限，免得用例本身漏一个永生线程出去。
        self.release.wait(timeout=30.0)
        return
        yield  # pragma: no cover - 使本函数成为生成器


class _ScriptedTap:
    def __init__(self, frames: list[np.ndarray]) -> None:
        self._frames = frames
        self.done = threading.Event()

    def frames(self, *, stop_event=None, poll_seconds=0.2):
        for frame in self._frames:
            if stop_event is not None and stop_event.is_set():
                break
            yield frame
        self.done.set()


class MonitorPlayerShutdownTests(unittest.TestCase):
    def test_tap_frames_reach_the_device_through_the_callback(self) -> None:
        tap = _ScriptedTap([np.full(64, 0.5, dtype=np.float32)])
        fake = _FakeSounddevice(max_output_channels=2)
        player = MonitorPlayer(
            tap, 0, 48000, 64, sounddevice_module=fake
        )
        player.start()
        self.assertTrue(tap.done.wait(timeout=5.0))
        out = fake.streams[0].pump(64)
        np.testing.assert_allclose(np.full(64, 0.5, dtype=np.float32), out[:, 0])
        np.testing.assert_allclose(out[:, 0], out[:, 1])
        self.assertTrue(player.stop())

    def test_stop_converges_and_closes_stream_on_calling_thread(self) -> None:
        tap = _ScriptedTap([np.zeros(64, dtype=np.float32)])
        fake = _FakeSounddevice()
        player = MonitorPlayer(tap, 0, 48000, 64, sounddevice_module=fake)
        player.start()
        stream = fake.streams[0]
        self.assertTrue(player.stop())
        self.assertIsNone(player.residual_thread_reason())
        self.assertEqual(["start", "abort", "close"], stream.actions())
        self.assertEqual(
            {threading.get_ident()}, stream.threads_for("close")
        )
        self.assertFalse(
            any(t.name == "monitor" and t.is_alive() for t in threading.enumerate())
        )

    def test_join_timeout_still_closes_the_stream_and_is_reported(self) -> None:
        # 这是事故那条路径的兜底：取帧线程赖着不走。
        # 旧实现在这里静默返回，然后进程带着它去 Py_Finalize 崩掉。
        # 新契约要求：(1) stop() 有界返回并如实报告未收敛；
        #             (2) 流仍然被本线程确定性关闭 —— 因为那条线程手里
        #                 根本没有 PortAudio 资源，关流不必等它。
        tap = _BlockingTap()
        fake = _FakeSounddevice()
        player = MonitorPlayer(tap, 0, 48000, 64, sounddevice_module=fake)
        player.start()
        self.assertTrue(tap.entered.wait(timeout=5.0))
        stream = fake.streams[0]

        try:
            converged = player.stop()
            self.assertFalse(converged, "赖着的取帧线程必须被如实报告为未收敛")
            self.assertIn("monitor", player.residual_thread_reason() or "")
            self.assertEqual(["start", "abort", "close"], stream.actions())
            self.assertEqual(
                {threading.get_ident()}, stream.threads_for("close")
            )
            # 重连不许把僵尸取帧线程连同新流一起复活 —— 宁可显式失败。
            with self.assertRaises(RuntimeError):
                player.start()
        finally:
            tap.release.set()

    def test_start_failure_does_not_leak_a_stream(self) -> None:
        tap = _ScriptedTap([])
        fake = _FakeSounddevice(max_output_channels=0)
        player = MonitorPlayer(tap, 0, 48000, 64, sounddevice_module=fake)
        with self.assertRaises(RuntimeError):
            player.start()
        self.assertFalse(player.active)
        self.assertEqual([], fake.streams)
        self.assertFalse(
            any(t.name == "monitor" and t.is_alive() for t in threading.enumerate())
        )

    def test_failure_after_open_still_closes_the_stream(self) -> None:
        # 设备在 open 与 start 之间消失：半开的流必须当场关掉，
        # 不能留给退出时的 Pa_Terminate。
        fake = _FakeSounddevice()
        original = fake.OutputStream

        def flaky(**kwargs):
            stream = original(**kwargs)
            stream.start = _boom
            return stream

        def _boom():
            raise RuntimeError("device vanished")

        fake.OutputStream = flaky  # type: ignore[method-assign]
        player = MonitorPlayer(
            _ScriptedTap([]), 0, 48000, 64, sounddevice_module=fake
        )
        with self.assertRaises(RuntimeError):
            player.start()
        self.assertEqual(["abort", "close"], fake.streams[0].actions())
        self.assertFalse(player.active)

    def test_shutdown_starvation_does_not_emit_a_false_alarm(self) -> None:
        # tap 先关、流后关是正常收尾顺序，中间必然空转几个回调。
        # 那不是欠载，不许因此每次结束会议都吐一条橙色告警。
        seen: list[str] = []
        tap = _ScriptedTap([np.full(64, 0.25, dtype=np.float32)])
        fake = _FakeSounddevice()
        player = MonitorPlayer(
            tap, 0, 48000, 64,
            on_status=seen.append,
            sounddevice_module=fake,
        )
        player.start()
        self.assertTrue(tap.done.wait(timeout=5.0))
        stream = fake.streams[0]
        stream.pump(64)
        player.begin_shutdown()
        stream.pump(64)
        stream.pump(64)
        self.assertEqual([], seen)
        player.stop()


class AudioShutdownFenceTests(unittest.TestCase):
    def test_clean_shutdown_produces_no_notes(self) -> None:
        self.assertEqual(
            (),
            audio_shutdown_fence([None], enumerate_threads=lambda: []),
        )

    def test_surviving_audio_thread_becomes_a_visible_note(self) -> None:
        straggler = threading.Thread(target=lambda: None, name="interpreter-player")
        straggler.is_alive = lambda: True  # type: ignore[method-assign]
        notes = audio_shutdown_fence(
            [],
            grace_seconds=0.0,
            enumerate_threads=lambda: [straggler],
        )
        self.assertEqual(1, len(notes))
        self.assertIn("interpreter-player", notes[0])
        self.assertIn("产物已落盘", notes[0])

    def test_unrelated_threads_are_ignored(self) -> None:
        other = threading.Thread(target=lambda: None, name="asyncio_0")
        other.is_alive = lambda: True  # type: ignore[method-assign]
        self.assertEqual(
            (),
            audio_shutdown_fence(
                [], grace_seconds=0.0, enumerate_threads=lambda: [other]
            ),
        )

    def test_grace_period_is_bounded_and_reports_late_exit(self) -> None:
        clock = {"t": 0.0}
        state = {"alive": True}
        thread = threading.Thread(target=lambda: None, name="monitor")
        thread.is_alive = lambda: state["alive"]  # type: ignore[method-assign]

        def sleep(seconds: float) -> None:
            clock["t"] += seconds
            if clock["t"] >= 0.3:
                state["alive"] = False

        notes = audio_shutdown_fence(
            [],
            grace_seconds=2.0,
            now=lambda: clock["t"],
            sleep=sleep,
            enumerate_threads=lambda: [thread],
        )
        # 宽限期内自己走掉的线程不该被当成故障上报。
        self.assertEqual((), notes)
        self.assertLess(clock["t"], 2.0)

    def test_component_residual_reason_is_collected(self) -> None:
        class _Source:
            def residual_thread_reason(self) -> str:
                return "monitor 取帧线程未在预算内结束（monitor）"

        notes = audio_shutdown_fence(
            [_Source()], grace_seconds=0.0, enumerate_threads=lambda: []
        )
        self.assertEqual(1, len(notes))
        self.assertIn("未在预算内结束", notes[0])

    def test_a_broken_self_check_cannot_break_shutdown(self) -> None:
        class _Boom:
            def residual_thread_reason(self):
                raise RuntimeError("kaboom")

        notes = audio_shutdown_fence(
            [_Boom()], grace_seconds=0.0, enumerate_threads=lambda: []
        )
        self.assertIn("kaboom", notes[0])


class SpeakTeeShutdownTests(unittest.TestCase):
    def test_own_leg_is_stopped_even_if_meeting_leg_raises(self) -> None:
        # meeting.stop() 抛异常时旧实现直接跳过 own.stop()，
        # 耳机支路的流会一路漏到 Py_Finalize。
        class _Boom:
            def stop(self):
                raise RuntimeError("meeting down")

        class _Own:
            stopped = 0

            def stop(self):
                type(self).stopped += 1
                return True

        own = _Own()
        tee = SpeakTeePlayer(meeting=_Boom(), own=own, is_muted=lambda: False)
        with self.assertRaises(RuntimeError):
            tee.stop()
        self.assertEqual(1, _Own.stopped)

    def test_residual_reason_names_the_failing_leg(self) -> None:
        class _Quiet:
            def residual_thread_reason(self):
                return None

        class _Stuck:
            def residual_thread_reason(self):
                return "线程未结束"

        tee = SpeakTeePlayer(
            meeting=_Quiet(), own=_Stuck(), is_muted=lambda: False
        )
        self.assertEqual("发言耳机支路：线程未结束", tee.residual_thread_reason())


if __name__ == "__main__":
    unittest.main()


class PrebufferRingTests(unittest.TestCase):
    """抖动预缓冲（2026-07-30 真实 Teams 验收：句中空洞的修复）。"""

    def test_holds_until_threshold_then_drains(self) -> None:
        from audio_gateway.routing import RingBuffer

        ring = RingBuffer(48_000, prebuffer_samples=1000)
        ring.push(np.ones(600, dtype=np.float32))
        # 未达门限：出静音，数据仍被扣着
        out, filled = ring.pull(100)
        self.assertEqual(0, filled)
        self.assertEqual(0.0, float(np.abs(out).max()))
        # 达门限：开闸
        ring.push(np.ones(500, dtype=np.float32))
        out, filled = ring.pull(100)
        self.assertEqual(100, filled)
        self.assertEqual(1.0, float(out.max()))

    def test_starvation_rearms_buffering(self) -> None:
        from audio_gateway.routing import RingBuffer

        ring = RingBuffer(48_000, prebuffer_samples=1000)
        ring.push(np.ones(1200, dtype=np.float32))
        ring.pull(1200)          # 恰好整除排空：欠载要等下一个空 tick 才判定
        ring.pull(100)           # 空 tick → 欠载 → 回攒批态（真实回调时序）
        ring.push(np.ones(200, dtype=np.float32))
        _, filled = ring.pull(100)
        # 小包再次被扣住，直到重新攒够——这正是把许多小空洞
        # 合并成少量平滑续播的机制
        self.assertEqual(0, filled)

    def test_tail_below_threshold_is_released_by_hold_timeout(self) -> None:
        from audio_gateway.routing import RingBuffer

        ring = RingBuffer(48_000, prebuffer_samples=1000)
        ring.push(np.ones(300, dtype=np.float32))
        # 按回调节拍持续等待：累计等待达一个门限时长后放行尾包
        released = 0
        for _ in range(30):
            _, filled = ring.pull(100)
            released += filled
        self.assertEqual(300, released)

    def test_zero_prebuffer_keeps_legacy_behavior(self) -> None:
        from audio_gateway.routing import RingBuffer

        ring = RingBuffer(48_000)
        ring.push(np.ones(10, dtype=np.float32))
        _, filled = ring.pull(10)
        self.assertEqual(10, filled)
