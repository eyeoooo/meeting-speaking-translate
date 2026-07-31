"""M1 发言排练：麦克风解析护栏、状态外报、历史契约与 CLI fail-closed。

三条硬边界（实现于 bridge 的排练接线，此处钉住可测的部分）：
① 排练段绝不进参谋；② 绝不进会议 transcript；③ M1 不出声。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

AUDIO_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUDIO_GATEWAY_ROOT))

from audio_gateway import main  # noqa: E402
from audio_gateway.bridge import (  # noqa: E402
    BridgeRuntimeState,
    SegmentHistory,
    _history,
    _state_payload,
)
from audio_gateway.devices import (  # noqa: E402
    resolve_rehearsal_mic_following_default,
)
from audio_gateway.interpreter import InterpreterState  # noqa: E402


def _sd(devices, default_in):
    def query_devices(idx=None):
        if idx is None:
            return devices
        return devices[idx]

    return SimpleNamespace(
        default=SimpleNamespace(device=(default_in, 4)),
        query_devices=query_devices,
    )


DEVICES = [
    {"name": "HDP-V104", "max_output_channels": 2, "max_input_channels": 0},
    {"name": "USB Audio Device", "max_output_channels": 2, "max_input_channels": 1},
    {"name": "Lenovo Wired VoIP Headset (Teams)",
     "max_output_channels": 2, "max_input_channels": 1},
    {"name": "Mac mini扬声器", "max_output_channels": 2, "max_input_channels": 0},
]
MAC_IN = 1


class RehearsalMicResolutionTests(unittest.TestCase):
    def test_follows_default_input_headset(self) -> None:
        device, note = resolve_rehearsal_mic_following_default(
            MAC_IN, sounddevice_module=_sd(DEVICES, default_in=2)
        )
        self.assertEqual(2, device)
        self.assertIn("Lenovo", note)

    def test_hard_intercepts_meeting_capture_card(self) -> None:
        # 耳麦拔掉后 macOS 常把默认输入落回采集声卡（2026-07-30 实测）：
        # 那时"排练"会把对方的声音当成我的发言。必须拒绝，且会议照常。
        device, note = resolve_rehearsal_mic_following_default(
            MAC_IN, sounddevice_module=_sd(DEVICES, default_in=MAC_IN)
        )
        self.assertEqual(-1, device)
        self.assertIn("对方的声音", note)

    def test_missing_default_input_degrades(self) -> None:
        device, note = resolve_rehearsal_mic_following_default(
            MAC_IN, sounddevice_module=_sd(DEVICES, default_in=None)
        )
        self.assertEqual(-1, device)
        self.assertIn("耳麦", note)


class RehearsalStatePayloadTests(unittest.TestCase):
    def _app(self, **extra):
        app = {
            "runtime_state": BridgeRuntimeState(),
            "bus": SimpleNamespace(dropped_by_tap=lambda: {}),
            "uplink": SimpleNamespace(
                received_frames=0,
                backlog_samples=0,
                enabled=False,
            ),
            "muted": False,
            "clients": set(),
        }
        app.update(extra)
        return app

    def test_default_payload_reports_rehearsal_disabled(self) -> None:
        payload = _state_payload(self._app())
        self.assertEqual({"enabled": False}, payload["rehearsal"])

    def test_enabled_rehearsal_state_is_projected(self) -> None:
        state = InterpreterState(enabled=True, lang="ja")
        payload = _state_payload(self._app(rehearsal_state=state))
        self.assertTrue(payload["rehearsal"]["enabled"])
        self.assertEqual("ja", payload["rehearsal"]["lang"])


class RehearsalHistoryEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_exposes_rehearsal_segments_independently(self) -> None:
        meeting = SegmentHistory()
        meeting.add(stream="source", text="会議の原文。", t=1.0)
        rehearsal = SegmentHistory()
        rehearsal.add(stream="source", text="我要说的中文。", t=2.0)
        rehearsal.add(stream="translation", text="私が言う日本語。", t=3.0)

        request = SimpleNamespace(
            query={"t": "secret"},
            app={
                "token": "secret",
                "segment_history": meeting,
                "advice_history": None,
                "rehearsal_history": rehearsal,
            },
        )
        response = await _history(request)
        payload = json.loads(response.text)

        self.assertEqual(1, len(payload["segments"]))
        self.assertEqual(2, len(payload["rehearsal_segments"]))
        # 排练段绝不混进会议段——它不是会议内容
        self.assertEqual(
            "会議の原文。", payload["segments"][0]["text"]
        )
        self.assertEqual(
            {"我要说的中文。", "私が言う日本語。"},
            {r["text"] for r in payload["rehearsal_segments"]},
        )

    async def test_history_without_rehearsal_returns_empty_list(self) -> None:
        request = SimpleNamespace(
            query={"t": "secret"},
            app={
                "token": "secret",
                "segment_history": None,
                "advice_history": None,
            },
        )
        response = await _history(request)
        payload = json.loads(response.text)
        self.assertEqual([], payload["rehearsal_segments"])


class RehearsalLogTests(unittest.TestCase):
    def test_rehearsal_records_are_serializable(self) -> None:
        # 早期版本硬取 record["markdown"]：排练段 KeyError 逐条炸断 Realtime
        # 会话（2026-07-30 真机实测句 2-4 译文全丢）。钉死通用序列化。
        import tempfile
        from audio_gateway.bridge import AdviceLog

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rehearsal.jsonl"
            log = AdviceLog(path)
            log.append({
                "type": "rehearsal_segment",
                "id": 0,
                "stream": "source",
                "text": "大家好。",
                "t": 1.0,
                "elapsed_ms": None,
                "epoch": 0,
            })
            row = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("大家好。", row["text"])
            self.assertNotIn("type", row)


class OwnVoiceOutputTests(unittest.TestCase):
    def test_headset_with_output_side_is_selected(self) -> None:
        from audio_gateway.devices import resolve_own_voice_output

        device, note = resolve_own_voice_output(
            2, 1, sounddevice_module=_sd(DEVICES, default_in=2)
        )
        self.assertEqual(2, device)
        self.assertIn("只有你能听到", note)

    def test_input_only_mic_degrades_to_captions_only(self) -> None:
        from audio_gateway.devices import resolve_own_voice_output

        devices = [
            {"name": "USB Audio Device",
             "max_output_channels": 2, "max_input_channels": 1},
            {"name": "纯麦克风", "max_output_channels": 0, "max_input_channels": 1},
        ]
        device, note = resolve_own_voice_output(
            1, 0, sounddevice_module=_sd(devices, default_in=1)
        )
        self.assertEqual(-1, device)
        self.assertIn("只显示字幕", note)

    def test_never_routes_own_voice_to_meeting_uplink(self) -> None:
        # mic 设备恰好是发言声卡（防御式：正常已被 mic 护栏挡住）：
        # "进自己耳朵"绝不允许解析为"进会议"。
        from audio_gateway.devices import resolve_own_voice_output

        device, _ = resolve_own_voice_output(
            1, 1, sounddevice_module=_sd(DEVICES, default_in=1)
        )
        self.assertEqual(-1, device)


class SpeakTeePlayerTests(unittest.TestCase):
    class _Spy:
        def __init__(self, fail_start=False):
            self.fail_start = fail_start
            self.started = 0
            self.stopped = 0
            self.chunks = []

        def start(self):
            self.started += 1
            if self.fail_start:
                raise RuntimeError("device busy")

        def stop(self):
            self.stopped += 1

        def feed_pcm16(self, data):
            self.chunks.append(data)

    def test_mute_gates_meeting_leg_only(self) -> None:
        from audio_gateway.bridge import SpeakTeePlayer

        meeting, own = self._Spy(), self._Spy()
        muted = {"value": False}
        tee = SpeakTeePlayer(
            meeting=meeting, own=own, is_muted=lambda: muted["value"]
        )
        tee.start()
        tee.feed_pcm16(b"a")
        muted["value"] = True
        tee.feed_pcm16(b"b")
        muted["value"] = False
        tee.feed_pcm16(b"c")

        # 静音时对方立刻听不到，但自己耳机继续播——
        # 你得知道"它本来要说什么"才能决定何时解除静音
        self.assertEqual([b"a", b"c"], meeting.chunks)
        self.assertEqual([b"a", b"b", b"c"], own.chunks)

    def test_own_leg_failure_degrades_but_meeting_survives(self) -> None:
        from audio_gateway.bridge import SpeakTeePlayer

        meeting, own = self._Spy(), self._Spy(fail_start=True)
        tee = SpeakTeePlayer(
            meeting=meeting, own=own, is_muted=lambda: False
        )
        tee.start()
        tee.feed_pcm16(b"a")
        tee.stop()

        self.assertEqual([b"a"], meeting.chunks)
        self.assertEqual([], own.chunks)
        self.assertEqual(1, meeting.stopped)

    def test_without_own_leg(self) -> None:
        from audio_gateway.bridge import SpeakTeePlayer

        meeting = self._Spy()
        tee = SpeakTeePlayer(meeting=meeting, own=None, is_muted=lambda: False)
        tee.start()
        tee.feed_pcm16(b"a")
        tee.stop()
        self.assertEqual([b"a"], meeting.chunks)


class SpeakDeviceTests(unittest.TestCase):
    """本机开会：收听源与发言目标必须是两个不同的虚拟设备。"""

    def test_speak_device_same_as_capture_is_rejected(self) -> None:
        # 同一设备 = 自己的日语回流进采集 → 翻译回环。必须 fail-closed。
        args = main.build_parser().parse_args([
            "bridge", "--speak", "--usb", "BlackHole 2ch",
            "--speak-device", "BlackHole 2ch",
        ])
        resolved = SimpleNamespace(
            mac_in=5, mac_out=5, monitor=6, mic=None, monitor_note=None
        )
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "k"}, clear=True),
            patch("audio_gateway.main._startup_doctor", return_value=0),
            patch("audio_gateway.devices.resolve", return_value=resolved),
            patch("audio_gateway.devices.resolve_output", return_value=5),
            patch("audio_gateway.bridge.run_bridge") as run,
        ):
            code = main.cmd_bridge(args)
        self.assertEqual(2, code)
        run.assert_not_called()

    def test_distinct_speak_device_is_passed_through(self) -> None:
        args = main.build_parser().parse_args([
            "bridge", "--speak", "--usb", "BlackHole 2ch",
            "--speak-device", "BlackHole 16ch",
        ])
        resolved = SimpleNamespace(
            mac_in=5, mac_out=5, monitor=6, mic=None, monitor_note=None
        )
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "k"}, clear=True),
            patch("audio_gateway.main._startup_doctor", return_value=0),
            patch("audio_gateway.devices.resolve", return_value=resolved),
            patch("audio_gateway.devices.resolve_output", return_value=4),
            patch("audio_gateway.bridge.run_bridge", return_value=0) as run,
        ):
            code = main.cmd_bridge(args)
        self.assertEqual(0, code)
        self.assertEqual(4, run.call_args.kwargs["speak_device"])

    def test_default_speak_device_is_none(self) -> None:
        args = main.build_parser().parse_args(["bridge"])
        self.assertIsNone(args.speak_device)


class RehearsalCliTests(unittest.TestCase):
    def test_speak_defaults_off_and_conflicts_with_rehearse(self) -> None:
        args = main.build_parser().parse_args(["bridge"])
        self.assertFalse(args.speak)

        conflict = main.build_parser().parse_args(
            ["bridge", "--rehearse", "--speak"]
        )
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "k"}, clear=True),
            patch("audio_gateway.main._startup_doctor") as doctor,
        ):
            code = main.cmd_bridge(conflict)
        self.assertEqual(2, code)
        doctor.assert_not_called()

    def test_speak_without_api_key_fails_fast(self) -> None:
        args = main.build_parser().parse_args(["bridge", "--speak"])
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("audio_gateway.main._startup_doctor") as doctor,
        ):
            code = main.cmd_bridge(args)
        self.assertEqual(2, code)
        doctor.assert_not_called()

class RehearsalCliTests(unittest.TestCase):
    def test_rehearse_defaults_off(self) -> None:
        args = main.build_parser().parse_args(["bridge"])
        self.assertFalse(args.rehearse)
        self.assertIsNone(args.rehearse_replay)

    def test_rehearse_without_api_key_fails_before_doctor_or_network(self) -> None:
        args = main.build_parser().parse_args(["bridge", "--rehearse"])

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("audio_gateway.main._startup_doctor") as doctor,
        ):
            code = main.cmd_bridge(args)

        self.assertEqual(2, code)
        doctor.assert_not_called()


class RehearsalNoSafeMicDegradationTests(unittest.TestCase):
    """无安全麦克风：排练/发言降级为 error 态，会议照常起桥。

    回归钉子：这条路径曾写成 set_connected(False, error=...)——
    InterpreterState.set_connected 没有 error 形参，降级路径自己先
    TypeError 崩掉整个 run_bridge，把"排练不可用"放大成"会议起不来"。
    """

    def test_no_safe_mic_sets_error_without_crashing_bridge(self) -> None:
        from audio_gateway import bridge as bridge_module

        note = "排练: 系统默认输入是会议采集卡，未找到安全麦克风"
        captured: dict[str, object] = {}

        class _StopAfterWiring(RuntimeError):
            pass

        def fake_app_runner(app):
            # AppRunner 是排练接线之后、真正占端口之前的第一站：在这里
            # 截断，既证明降级路径走通（没炸 TypeError），又拿到接线完成
            # 的 app 来断言状态，不需要真的起服务。
            captured["app"] = app
            raise _StopAfterWiring

        cfg = SimpleNamespace(samplerate=24000, blocksize=1024)
        dev = SimpleNamespace(mac_in=1, mac_out=2, monitor=None, mic=None)
        with (
            patch(
                "audio_gateway.devices.resolve_rehearsal_mic_following_default",
                return_value=(-1, note),
            ),
            patch(
                "audio_gateway.bridge.web.AppRunner",
                side_effect=fake_app_runner,
            ),
        ):
            try:
                with self.assertRaises(_StopAfterWiring):
                    bridge_module.run_bridge(
                        cfg,
                        dev,
                        port=0,
                        token="x",
                        record=False,
                        rehearse=True,
                        openai_api_key="k",
                    )
            finally:
                # run_bridge 自建并 set 了事件循环；测试负责收掉。
                asyncio.get_event_loop().close()
                asyncio.set_event_loop(None)

        state = captured["app"]["rehearsal_state"].snapshot()
        self.assertFalse(state["connected"])
        self.assertEqual(note, state["error"])


if __name__ == "__main__":
    unittest.main()
