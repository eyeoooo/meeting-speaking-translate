from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


AUDIO_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUDIO_GATEWAY_ROOT))

from audio_gateway import main  # noqa: E402
from audio_gateway.archive import TranscriptArchiver  # noqa: E402
from audio_gateway.config import GatewayConfig  # noqa: E402
from audio_gateway.postmeeting import (  # noqa: E402
    BridgePhaseState,
    GracefulStopCoordinator,
    PostMeetingPipeline,
)

try:
    from audio_gateway.bridge import _stop  # noqa: E402
except ModuleNotFoundError as bridge_import_error:
    _stop = None
    BRIDGE_IMPORT_ERROR: ModuleNotFoundError | None = bridge_import_error
else:
    BRIDGE_IMPORT_ERROR = None


class FakeRecorder:
    def __init__(self, root: Path, events: list[str]) -> None:
        self.wav_path = root / "meeting.wav"
        self.wav_path.write_bytes(b"mock wav")
        self._root = root
        self._events = events
        self.close_calls = 0
        self.finalize_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        self._events.append("close")

    def finalize(self, *, keep_wav: bool = True) -> Path:
        self.finalize_calls += 1
        self._events.append("convert")
        path = self._root / "meeting.m4a"
        path.write_bytes(b"mock m4a")
        if not keep_wav:
            self.wav_path.unlink(missing_ok=True)
        return path


class BridgePhaseStateTests(unittest.TestCase):
    def test_running_post_processing_done_state_machine(self) -> None:
        phase = BridgePhaseState(Path("/tmp/mock-session"))

        self.assertEqual("running", phase.snapshot()["phase"])
        self.assertIsNone(phase.snapshot()["post_processing_step"])
        self.assertTrue(phase.begin_post_processing())
        self.assertFalse(phase.begin_post_processing())

        for step in ("transcribe", "summarize", "convert"):
            phase.set_step(step)
            self.assertEqual(step, phase.snapshot()["post_processing_step"])

        phase.finish(artifacts=("meeting.m4a",), notes=("ok",))
        self.assertFalse(phase.begin_post_processing())
        self.assertEqual(
            {
                "phase": "done",
                "post_processing_step": None,
                "session_dir": "/tmp/mock-session",
                "artifacts": ["meeting.m4a"],
                "post_processing_notes": ["ok"],
            },
            phase.snapshot(),
        )

    def test_invalid_transition_fails_closed(self) -> None:
        phase = BridgePhaseState(None)

        with self.assertRaises(RuntimeError):
            phase.set_step("transcribe")
        phase.begin_post_processing()
        with self.assertRaises(ValueError):
            phase.set_step("upload")


class StopCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_request_is_idempotent_and_broadcasts_before_shutdown(
        self,
    ) -> None:
        events: list[str] = []
        phase = BridgePhaseState(None)

        async def broadcast() -> None:
            events.append("broadcast")

        async def shutdown(reason: str) -> None:
            events.append(f"shutdown:{reason}")
            phase.finish()

        coordinator = GracefulStopCoordinator(
            phase,
            broadcast,
            shutdown,
        )

        self.assertTrue(await coordinator.request("first"))
        self.assertFalse(await coordinator.request("second"))
        await coordinator.task

        self.assertEqual(["broadcast", "shutdown:first"], events)
        self.assertEqual("done", phase.snapshot()["phase"])

    async def test_post_stop_endpoint_returns_accepted_then_already_requested(
        self,
    ) -> None:
        if _stop is None:
            self.skipTest(f"bridge dependencies unavailable: {BRIDGE_IMPORT_ERROR}")
        phase = BridgePhaseState(Path("/tmp/session"))

        async def broadcast() -> None:
            return None

        async def shutdown(_reason: str) -> None:
            await asyncio.sleep(0)
            phase.finish()

        coordinator = GracefulStopCoordinator(
            phase,
            broadcast,
            shutdown,
        )
        uplink = SimpleNamespace(
            received_frames=0,
            backlog_samples=0,
            enabled=False,
        )
        bus = Mock()
        bus.dropped_by_tap.return_value = {}
        app = {
            "token": "test-token",
            "stop_coordinator": coordinator,
            "phase_state": phase,
            "runtime_state": SimpleNamespace(
                snapshot=lambda: {
                    "link": "up",
                    "reason": "ok",
                    "alerts": [],
                    "downlink_frames": 0,
                    "portaudio_status_flags": [],
                    "started_at": "2026-07-30T00:00:00+00:00",
                }
            ),
            "bus": bus,
            "uplink": uplink,
            "clients": set(),
            "muted": False,
        }
        request = SimpleNamespace(
            app=app,
            query={"t": "test-token"},
        )

        first = await _stop(request)
        second = await _stop(request)
        await coordinator.task

        self.assertEqual(202, first.status)
        self.assertEqual("accepted", json.loads(first.text)["stop"])
        self.assertEqual(200, second.status)
        self.assertEqual(
            "already-requested",
            json.loads(second.text)["stop"],
        )


class PostMeetingPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.cfg = GatewayConfig(output_root=self.root)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _pipeline(
        self,
        *,
        recorder: FakeRecorder,
        archiver: TranscriptArchiver | None,
        batch: Mock,
        summary: Mock,
        steps: list[str],
        logs: list[str],
        no_postprocess: bool = False,
        environ: dict[str, str] | None = None,
    ) -> PostMeetingPipeline:
        return PostMeetingPipeline(
            cfg=self.cfg,
            session_dir=self.root,
            recorder=recorder,
            archiver=archiver,
            no_postprocess=no_postprocess,
            on_step=steps.append,
            batch_transcribe_fn=batch,
            summarize_fn=summary,
            environ=environ or {},
            logger=logs.append,
        )

    def test_runtime_skip_bypasses_transcribe_and_summary(self) -> None:
        """菜单「取消这次录音」→ /stop?postprocess=0：只收尾录音，不转写不出纪要。"""
        events: list[str] = []
        steps: list[str] = []
        logs: list[str] = []
        recorder = FakeRecorder(self.root, events)
        batch = Mock()
        summary = Mock()
        pipeline = self._pipeline(
            recorder=recorder,
            archiver=None,
            batch=batch,
            summary=summary,
            steps=steps,
            logs=logs,
        )

        pipeline.request_skip()
        result = pipeline.run()

        batch.assert_not_called()
        summary.assert_not_called()
        self.assertNotIn("transcribe", steps)
        self.assertNotIn("summarize", steps)
        self.assertTrue(
            any("取消本次录音" in note for note in result.notes),
            result.notes,
        )
        self.assertIn("close", events)   # 录音仍必须正常收尾

    def test_success_keeps_realtime_and_batch_provenance(self) -> None:
        events: list[str] = []
        steps: list[str] = []
        logs: list[str] = []
        recorder = FakeRecorder(self.root, events)
        archiver = TranscriptArchiver(
            self.root,
            datetime(2026, 7, 30, 12, 0, 0),
        )
        # 双泳道：原文与译文是两条独立记录，不再有配对写法。
        archiver.add_realtime_segment(
            stream="source",
            text="会中原文",
            t=1.0,
            lang=None,
        )
        archiver.add_realtime_segment(
            stream="translation",
            text="会中译文",
            t=1.5,
            lang="zh",
        )

        def batch_impl(_path: Path, _cfg: GatewayConfig):
            events.append("transcribe")
            return [
                SimpleNamespace(
                    t0=2.0,
                    t1=3.0,
                    lang="ja",
                    text="会后批量段落",
                )
            ]

        def summary_impl(path: Path, _cfg: GatewayConfig) -> Path:
            events.append("summarize")
            self.assertTrue(path.is_file())
            output = self.root / "minutes.md"
            output.write_text("# mock minutes\n", encoding="utf-8")
            return output

        pipeline = self._pipeline(
            recorder=recorder,
            archiver=archiver,
            batch=Mock(side_effect=batch_impl),
            summary=Mock(side_effect=summary_impl),
            steps=steps,
            logs=logs,
            environ={"ANTHROPIC_API_KEY": "mock-key"},
        )

        result = pipeline.run()

        self.assertEqual(
            ["close", "transcribe", "summarize", "convert"],
            events,
        )
        self.assertEqual(
            ["transcribe", "summarize", "convert"],
            steps,
        )
        records = [
            json.loads(line)
            for line in (self.root / "transcript.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(["realtime", "realtime", "batch"], [
            record["source"] for record in records
        ])
        for name in (
            "meeting.m4a",
            "transcript.txt",
            "transcript.jsonl",
            "minutes.md",
        ):
            self.assertIn(str(self.root / name), result.artifacts)
        self.assertTrue(any("最终产物清单" in line for line in logs))

    def test_missing_whisper_is_reported_and_conversion_continues(self) -> None:
        events: list[str] = []
        steps: list[str] = []
        logs: list[str] = []
        recorder = FakeRecorder(self.root, events)
        archiver = TranscriptArchiver(
            self.root,
            datetime(2026, 7, 30, 12, 0, 0),
        )
        archiver.add_realtime_segment(
            stream="source",
            text="已有会中原文",
            t=1.0,
            lang=None,
        )
        batch = Mock(side_effect=RuntimeError("没有可用的 Whisper 后端"))

        def summary_impl(_path: Path, _cfg: GatewayConfig) -> Path:
            path = self.root / "minutes.md"
            path.write_text("# fallback minutes\n", encoding="utf-8")
            return path

        result = self._pipeline(
            recorder=recorder,
            archiver=archiver,
            batch=batch,
            summary=Mock(side_effect=summary_impl),
            steps=steps,
            logs=logs,
            environ={"ANTHROPIC_API_KEY": "mock-key"},
        ).run()

        self.assertEqual("convert", events[-1])
        self.assertTrue(any(
            "没有可用的 Whisper 后端" in note
            for note in result.notes
        ))
        self.assertTrue((self.root / "meeting.m4a").is_file())

    def test_missing_llm_credential_skips_summary_without_blocking_audio(
        self,
    ) -> None:
        events: list[str] = []
        summary = Mock()
        recorder = FakeRecorder(self.root, events)
        archiver = TranscriptArchiver(
            self.root,
            datetime(2026, 7, 30, 12, 0, 0),
        )
        batch = Mock(return_value=[
            SimpleNamespace(t0=0.0, t1=1.0, lang="ja", text="原文"),
        ])
        summary = Mock(
            side_effect=RuntimeError(
                "未设置 ANTHROPIC_API_KEY，无法生成纪要"
            )
        )

        result = self._pipeline(
            recorder=recorder,
            archiver=archiver,
            batch=batch,
            summary=summary,
            steps=[],
            logs=[],
            environ={},
        ).run()

        summary.assert_called_once()
        self.assertTrue(any(
            "ANTHROPIC_API_KEY" in note
            for note in result.notes
        ))
        self.assertTrue((self.root / "meeting.m4a").is_file())
        self.assertFalse((self.root / "minutes.md").exists())

    def test_summary_exception_is_redacted_and_conversion_continues(
        self,
    ) -> None:
        events: list[str] = []
        secret = "mock-anthropic-secret"
        recorder = FakeRecorder(self.root, events)
        archiver = TranscriptArchiver(
            self.root,
            datetime(2026, 7, 30, 12, 0, 0),
        )
        batch = Mock(return_value=[
            SimpleNamespace(t0=0.0, t1=1.0, lang="ja", text="原文"),
        ])
        summary = Mock(
            side_effect=RuntimeError(f"mock upstream rejected {secret}")
        )

        result = self._pipeline(
            recorder=recorder,
            archiver=archiver,
            batch=batch,
            summary=summary,
            steps=[],
            logs=[],
            environ={"ANTHROPIC_API_KEY": secret},
        ).run()

        self.assertEqual("convert", events[-1])
        self.assertNotIn(secret, "\n".join(result.notes))
        self.assertIn("[REDACTED]", "\n".join(result.notes))
        self.assertTrue((self.root / "meeting.m4a").is_file())

    def test_no_postprocess_calls_neither_whisper_nor_llm(self) -> None:
        events: list[str] = []
        steps: list[str] = []
        batch = Mock()
        summary = Mock()
        recorder = FakeRecorder(self.root, events)

        result = self._pipeline(
            recorder=recorder,
            archiver=None,
            batch=batch,
            summary=summary,
            steps=steps,
            logs=[],
            no_postprocess=True,
        ).run()

        batch.assert_not_called()
        summary.assert_not_called()
        self.assertEqual(["close", "convert"], events)
        self.assertEqual(["convert"], steps)
        self.assertTrue(any(
            "--no-postprocess" in note
            for note in result.notes
        ))
        self.assertFalse((self.root / "transcript.txt").exists())
        self.assertFalse((self.root / "transcript.jsonl").exists())
        self.assertFalse((self.root / "minutes.md").exists())

    def test_bridge_parser_accepts_no_postprocess(self) -> None:
        args = main.build_parser().parse_args([
            "bridge",
            "--no-postprocess",
        ])

        self.assertTrue(args.no_postprocess)


if __name__ == "__main__":
    unittest.main()
