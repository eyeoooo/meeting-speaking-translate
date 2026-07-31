from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np


AUDIO_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUDIO_GATEWAY_ROOT))

from audio_gateway import main  # noqa: E402
from audio_gateway.routing import (  # noqa: E402
    BurnInProbe,
    evaluate_burnin_interval,
)


def interval_record(**overrides) -> dict:
    values = {
        "minute": 1,
        "interval_seconds": 60.0,
        "input_callbacks": 2812,
        "output_callbacks": 2812,
        "input_samples": 2_880_000,
        "output_samples": 2_880_000,
        "input_xruns": 0,
        "output_xruns": 0,
        "input_status_flags": [],
        "output_status_flags": [],
        "blocksize": 1024,
        "interrupted_reason": None,
    }
    values.update(overrides)
    return evaluate_burnin_interval(**values)


class BurnInDecisionTests(unittest.TestCase):
    def test_clock_drift_is_reported_separately_from_drops(self) -> None:
        record = interval_record(output_samples=2_879_900)

        self.assertTrue(record["passed"])
        self.assertEqual(0, record["drops"]["estimated_samples"])
        self.assertEqual(100, record["drift"]["samples"])
        self.assertNotEqual(0.0, record["drift"]["ppm_estimate"])

    def test_drop_rate_above_point_one_percent_fails(self) -> None:
        record = interval_record(
            input_samples=100_000,
            output_samples=100_000,
            input_xruns=1,
        )

        self.assertFalse(record["passed"])
        self.assertGreater(record["drops"]["rate"], 0.001)
        self.assertIn("exceeds 0.1%", record["reason"])

    def test_drop_rate_exactly_point_one_percent_passes(self) -> None:
        record = interval_record(
            input_samples=100_000,
            output_samples=100_000,
            input_xruns=1,
            blocksize=100,
        )

        self.assertTrue(record["passed"])
        self.assertEqual(0.001, record["drops"]["rate"])

    def test_missing_callback_is_stream_interruption(self) -> None:
        record = interval_record(input_callbacks=0)

        self.assertFalse(record["passed"])
        self.assertTrue(record["stream_interrupted"])
        self.assertIn("no callbacks", record["reason"])


class FakeStream:
    def __init__(self, kind: str, callback, finished_callback) -> None:
        self.kind = kind
        self.callback = callback
        self.finished_callback = finished_callback
        self.active = False

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        was_active = self.active
        self.active = False
        if was_active:
            self.finished_callback()

    def close(self) -> None:
        self.active = False


class FakeSoundDevice:
    def __init__(self, *, interrupt_after: int | None = None) -> None:
        self.streams: list[FakeStream] = []
        self.interrupt_after = interrupt_after
        self.advances = 0

    def InputStream(self, **kwargs):
        stream = FakeStream(
            "input",
            kwargs["callback"],
            kwargs["finished_callback"],
        )
        self.streams.append(stream)
        return stream

    def OutputStream(self, **kwargs):
        stream = FakeStream(
            "output",
            kwargs["callback"],
            kwargs["finished_callback"],
        )
        self.streams.append(stream)
        return stream

    def advance(self, frames: int = 10) -> None:
        self.advances += 1
        for stream in self.streams:
            if not stream.active:
                continue
            if (
                stream.kind == "output"
                and self.interrupt_after is not None
                and self.advances >= self.interrupt_after
            ):
                stream.active = False
                stream.finished_callback()
                continue
            if stream.kind == "input":
                stream.callback(
                    np.zeros((frames, 1), dtype=np.float32),
                    frames,
                    None,
                    None,
                )
            else:
                stream.callback(
                    np.empty((frames, 1), dtype=np.float32),
                    frames,
                    None,
                    None,
                )


class FakeClock:
    def __init__(self, audio: FakeSoundDevice) -> None:
        self.now = 0.0
        self.audio = audio

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.audio.advance()


class BurnInProbeTests(unittest.TestCase):
    def test_mocked_dual_stream_run_passes_and_emits_interval(self) -> None:
        audio = FakeSoundDevice()
        clock = FakeClock(audio)
        emitted: list[dict] = []
        probe = BurnInProbe(
            1,
            2,
            samplerate=100,
            blocksize=10,
            sounddevice_module=audio,
            clock=clock,
            sleep=clock.sleep,
        )

        summary = probe.run(
            1 / 60,
            interval_seconds=1.0,
            on_interval=emitted.append,
        )

        self.assertTrue(summary["passed"])
        self.assertEqual(1, len(emitted))
        self.assertGreater(emitted[0]["callbacks"]["input"], 0)
        self.assertGreater(emitted[0]["callbacks"]["output"], 0)

    def test_mocked_stream_interruption_fails_without_retry(self) -> None:
        audio = FakeSoundDevice(interrupt_after=3)
        clock = FakeClock(audio)
        emitted: list[dict] = []
        probe = BurnInProbe(
            1,
            2,
            samplerate=100,
            blocksize=10,
            sounddevice_module=audio,
            clock=clock,
            sleep=clock.sleep,
        )

        summary = probe.run(
            1 / 60,
            interval_seconds=1.0,
            on_interval=emitted.append,
        )

        self.assertFalse(summary["passed"])
        self.assertTrue(summary["stream_interrupted"])
        self.assertEqual(2, len(audio.streams))
        self.assertEqual(1, len(emitted))
        self.assertIn("output stream", emitted[0]["reason"])


class BurnInCliTests(unittest.TestCase):
    def test_parser_exposes_burnin_minutes_output_and_blocksize(self) -> None:
        args = main.build_parser().parse_args(
            [
                "verify",
                "burnin",
                "--minutes",
                "3",
                "--output",
                "result.jsonl",
                "--blocksize",
                "256",
            ]
        )

        self.assertEqual("burnin", args.what)
        self.assertEqual(3.0, args.minutes)
        self.assertEqual("result.jsonl", args.output)
        self.assertEqual(256, args.blocksize)

    def test_cli_writes_minute_and_summary_jsonl(self) -> None:
        minute = interval_record()
        summary = {
            "type": "summary",
            "passed": True,
            "reason": "ok",
        }
        probe = MagicMock()

        def run(_minutes, *, on_interval):
            on_interval(minute)
            return summary

        probe.run.side_effect = run
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "burnin.jsonl"
            args = main.build_parser().parse_args(
                [
                    "verify",
                    "burnin",
                    "--minutes",
                    "1",
                    "--output",
                    str(output),
                ]
            )
            with patch(
                "audio_gateway.devices.resolve",
                return_value=SimpleNamespace(
                    mac_in=1,
                    mac_out=2,
                    monitor=None,
                    mic=None,
                ),
            ):
                with patch(
                    "audio_gateway.routing.BurnInProbe",
                    return_value=probe,
                ):
                    with patch("builtins.print"):
                        code = main.cmd_verify(args)

            records = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(0, code)
        self.assertEqual(["minute", "summary"], [r["type"] for r in records])
        probe.run.assert_called_once()

    def test_nonpositive_minutes_fail_before_device_access(self) -> None:
        args = main.build_parser().parse_args(
            ["verify", "burnin", "--minutes", "0"]
        )
        with patch("audio_gateway.devices.resolve") as resolve:
            with patch("builtins.print"):
                code = main.cmd_verify(args)

        self.assertEqual(2, code)
        resolve.assert_not_called()

    def test_missing_device_is_explicit_fail_closed_exit_two(self) -> None:
        from audio_gateway.devices import DeviceResolutionError

        args = main.build_parser().parse_args(
            ["verify", "burnin", "--minutes", "1"]
        )
        with patch(
            "audio_gateway.devices.resolve",
            side_effect=DeviceResolutionError("synthetic missing device"),
        ):
            with patch("builtins.print") as output:
                code = main.cmd_verify(args)

        self.assertEqual(2, code)
        self.assertIn("fail-closed", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
