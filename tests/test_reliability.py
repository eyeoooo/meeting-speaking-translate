from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np


AUDIO_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUDIO_GATEWAY_ROOT))

from audio_gateway.bridge import (  # noqa: E402
    CLIPPING_ALERT,
    DIGITAL_ZERO_ALERT,
    BridgeRuntimeState,
    LevelSentinel,
    _AudioLifecycle,
    _index,
    _reconnect,
    _state_payload,
)


class LevelSentinelTests(unittest.TestCase):
    def test_exact_zero_requires_full_thirty_seconds_and_auto_clears(self) -> None:
        sentinel = LevelSentinel(samplerate=10)

        raised, cleared = sentinel.feed(np.zeros(299, dtype=np.float32))
        self.assertEqual(frozenset(), raised)
        self.assertEqual(frozenset(), cleared)

        raised, cleared = sentinel.feed(np.zeros(1, dtype=np.float32))
        self.assertEqual(frozenset({DIGITAL_ZERO_ALERT}), raised)
        self.assertEqual(frozenset(), cleared)

        raised, cleared = sentinel.feed(
            np.array([1e-12], dtype=np.float32)
        )
        self.assertEqual(frozenset(), raised)
        self.assertEqual(frozenset({DIGITAL_ZERO_ALERT}), cleared)

    def test_clipping_uses_full_ten_second_rolling_sample_window(self) -> None:
        sentinel = LevelSentinel(samplerate=10)
        clipped = np.zeros(100, dtype=np.float32)
        clipped[:2] = 1.0

        raised, _ = sentinel.feed(clipped)

        self.assertEqual(frozenset({CLIPPING_ALERT}), raised)
        raised, cleared = sentinel.feed(np.zeros(100, dtype=np.float32))
        self.assertEqual(frozenset(), raised)
        self.assertEqual(frozenset({CLIPPING_ALERT}), cleared)

    def test_clipping_threshold_is_strictly_greater_than_one_percent(self) -> None:
        sentinel = LevelSentinel(samplerate=10)
        boundary = np.zeros(100, dtype=np.float32)
        boundary[0] = 1.0

        raised, _ = sentinel.feed(boundary)

        self.assertNotIn(CLIPPING_ALERT, raised)

    def test_reset_clears_measurement_history_and_active_alerts(self) -> None:
        sentinel = LevelSentinel(samplerate=10)
        sentinel.feed(np.zeros(300, dtype=np.float32))

        cleared = sentinel.reset()

        self.assertEqual(frozenset({DIGITAL_ZERO_ALERT}), cleared)
        self.assertEqual(frozenset(), sentinel.alerts)
        raised, _ = sentinel.feed(np.zeros(299, dtype=np.float32))
        self.assertEqual(frozenset(), raised)


class RuntimeStateTests(unittest.TestCase):
    def test_status_flag_degrades_link_and_counters_are_cumulative(self) -> None:
        state = BridgeRuntimeState()
        state.mark_up()
        state.increment_downlink_frames()
        state.increment_downlink_frames()

        self.assertTrue(
            state.record_status_flag("capture", "input overflow")
        )
        self.assertFalse(
            state.record_status_flag("capture", "input overflow")
        )
        snapshot = state.snapshot()

        self.assertEqual("degraded", snapshot["link"])
        self.assertEqual("capture: input overflow", snapshot["reason"])
        self.assertEqual(2, snapshot["downlink_frames"])
        self.assertEqual(
            ["capture: input overflow"],
            snapshot["portaudio_status_flags"],
        )
        self.assertTrue(snapshot["started_at"])

    def test_sentinel_alerts_are_added_and_removed(self) -> None:
        state = BridgeRuntimeState()
        state.update_sentinel_alerts(
            frozenset({DIGITAL_ZERO_ALERT}),
            frozenset(),
        )
        self.assertEqual(
            [DIGITAL_ZERO_ALERT],
            state.snapshot()["alerts"],
        )

        state.update_sentinel_alerts(
            frozenset(),
            frozenset({DIGITAL_ZERO_ALERT}),
        )
        self.assertEqual([], state.snapshot()["alerts"])

    def test_status_payload_exposes_all_required_counters(self) -> None:
        state = BridgeRuntimeState()
        state.mark_up()
        state.increment_downlink_frames()
        app = {
            "runtime_state": state,
            "bus": SimpleNamespace(
                dropped_by_tap=lambda: {"bridge": 2, "monitor": 1}
            ),
            "uplink": SimpleNamespace(
                received_frames=7,
                backlog_samples=960,
                enabled=False,
            ),
            "muted": False,
            "clients": {object()},
        }

        payload = _state_payload(app)

        self.assertEqual("up", payload["link"])
        self.assertEqual("ok", payload["reason"])
        self.assertEqual(1, payload["downlink_frames"])
        self.assertEqual(7, payload["uplink_frames"])
        self.assertEqual(960, payload["uplink_backlog_samples"])
        # 封存态也必须保持数值型：前端 parseGatewayState 有 typeof 硬门
        self.assertIsInstance(payload["uplink_frames"], int)
        self.assertFalse(payload["uplink_enabled"])
        self.assertEqual(
            {"bridge": 2, "monitor": 1},
            payload["tap_dropped"],
        )
        self.assertEqual(
            {"dropped": 2},
            payload["taps"]["bridge"],
        )
        self.assertEqual([], payload["portaudio_status_flags"])
        self.assertTrue(payload["started_at"])


class FakeComponent:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1
        if self.fail_start:
            raise RuntimeError("simulated device failure")

    def stop(self) -> None:
        self.stops += 1

    def health_reason(self):
        return None


class FakeBus(FakeComponent):
    def stop(self, *, close_taps: bool = True) -> None:
        self.stops += 1
        self.last_close_taps = close_taps

    def discard_buffered(self) -> dict[str, int]:
        return {}


class AudioLifecycleTests(unittest.TestCase):
    def test_start_failure_stops_started_streams_without_retry(self) -> None:
        bus = FakeBus()
        uplink = FakeComponent()
        monitor = FakeComponent(fail_start=True)
        lifecycle = _AudioLifecycle(bus, uplink, monitor)

        with self.assertRaisesRegex(RuntimeError, "simulated"):
            lifecycle.start()

        self.assertEqual(1, uplink.starts)
        self.assertEqual(1, uplink.stops)
        self.assertEqual(1, monitor.starts)
        self.assertEqual(0, bus.starts)

    def test_stop_keeps_taps_open_for_manual_reconnect(self) -> None:
        bus = FakeBus()
        lifecycle = _AudioLifecycle(
            bus,
            FakeComponent(),
            FakeComponent(),
        )

        lifecycle.stop(final=False)

        self.assertFalse(bus.last_close_taps)


class ReconnectHandlerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_app(state: BridgeRuntimeState, reconnect_audio) -> dict:
        return {
            "token": "secret",
            "runtime_state": state,
            "reconnect_audio": reconnect_audio,
            "bus": SimpleNamespace(dropped_by_tap=lambda: {}),
            "uplink": SimpleNamespace(
                received_frames=0,
                backlog_samples=0,
                enabled=False,
            ),
            "muted": False,
            "clients": set(),
        }

    async def test_down_link_runs_one_manual_recovery_attempt(self) -> None:
        state = BridgeRuntimeState()
        recover = AsyncMock(return_value=True)
        request = SimpleNamespace(
            query={"t": "secret"},
            app=self.make_app(state, recover),
        )

        response = await _reconnect(request)
        payload = json.loads(response.text)

        self.assertEqual(200, response.status)
        self.assertEqual("ok", payload["reconnect"])
        recover.assert_awaited_once_with()

    async def test_up_link_refuses_unnecessary_stream_restart(self) -> None:
        state = BridgeRuntimeState()
        state.mark_up()
        recover = AsyncMock(return_value=True)
        request = SimpleNamespace(
            query={"t": "secret"},
            app=self.make_app(state, recover),
        )

        response = await _reconnect(request)

        self.assertEqual(409, response.status)
        recover.assert_not_awaited()


class PanelRenderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_panel_renders_link_reason_and_alerts_from_ws_state(self) -> None:
        # 告警渲染在静态 bridge.html 本体；_index 直接送该文件。
        # 两个断言合起来保证 served 页面含 link/reason/alerts 渲染逻辑。
        request = SimpleNamespace(
            query={"t": "secret"},
            app={"token": "secret"},
        )

        response = await _index(request)
        self.assertEqual(200, response.status)

        from audio_gateway.bridge import _STATIC
        html = _STATIC.read_text(encoding="utf-8")
        self.assertIn("st.link", html)
        self.assertIn("st.reason", html)
        self.assertIn("st.alerts", html)
        self.assertIn("⚠️", html)


if __name__ == "__main__":
    unittest.main()
