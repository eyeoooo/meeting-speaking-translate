from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import sounddevice as sd


AUDIO_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUDIO_GATEWAY_ROOT))

from audio_gateway.bus import RxBus, Tap  # noqa: E402


class CallbackStatus:
    def __bool__(self) -> bool:
        return True

    def __str__(self) -> str:
        return "input overflow"


class TapTests(unittest.TestCase):
    def test_drop_oldest_counts_the_evicted_frame(self) -> None:
        tap = Tap("monitor", maxsize=1, drop_oldest=True)
        first = np.array([1.0], dtype=np.float32)
        second = np.array([2.0], dtype=np.float32)

        tap.push(first)
        tap.push(second)

        self.assertEqual(1, tap.dropped)
        np.testing.assert_array_equal(second, tap.q.get_nowait())

    def test_close_of_full_bounded_tap_is_nonblocking(self) -> None:
        tap = Tap("monitor", maxsize=1, drop_oldest=True)
        tap.push(np.array([1.0], dtype=np.float32))

        tap.close()

        self.assertEqual(1, tap.dropped)
        self.assertEqual([], list(tap.frames()))


class RxBusCallbackTests(unittest.TestCase):
    def test_callback_fans_out_and_accumulates_status_and_counts(self) -> None:
        statuses: list[str] = []
        bus = RxBus(
            1,
            samplerate=48000,
            blocksize=4,
            on_status=statuses.append,
        )
        tap = bus.subscribe("test")
        data = np.arange(4, dtype=np.float32).reshape(-1, 1)

        bus._callback(data, 4, None, CallbackStatus())
        bus._callback(data, 4, None, CallbackStatus())

        self.assertEqual(2, bus.callback_count)
        self.assertEqual(8, bus.captured_samples)
        self.assertEqual({"input overflow"}, bus.status_flags)
        self.assertEqual(["input overflow"], statuses)
        np.testing.assert_array_equal(data[:, 0], tap.q.get_nowait())

    def test_callback_failure_propagates_as_portaudio_abort(self) -> None:
        errors: list[str] = []
        bus = RxBus(
            1,
            samplerate=48000,
            blocksize=4,
            on_error=errors.append,
        )
        tap = bus.subscribe("broken")

        def fail(_frame) -> None:
            raise RuntimeError("synthetic fanout failure")

        tap.push = fail
        data = np.zeros((4, 1), dtype=np.float32)

        with self.assertRaises(sd.CallbackAbort):
            bus._callback(data, 4, None, None)

        self.assertIn("synthetic fanout failure", errors[0])

    def test_health_check_detects_disappeared_device(self) -> None:
        bus = RxBus(42, samplerate=48000, blocksize=4)
        bus._stream = type("Stream", (), {"active": True})()

        with patch(
            "audio_gateway.bus.sd.query_devices",
            side_effect=RuntimeError("invalid device"),
        ):
            reason = bus.health_reason()

        self.assertIn("disappeared", reason)
        self.assertIn("invalid device", reason)


if __name__ == "__main__":
    unittest.main()
