from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


AUDIO_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUDIO_GATEWAY_ROOT))

from audio_gateway import main  # noqa: E402
from audio_gateway.config import GatewayConfig  # noqa: E402


class DoctorCliTests(unittest.TestCase):
    def test_parser_exposes_doctor_fix_and_startup_escape_hatches(self) -> None:
        doctor_args = main.build_parser().parse_args(["doctor", "--fix"])
        run_args = main.build_parser().parse_args(["run", "--skip-doctor"])
        bridge_args = main.build_parser().parse_args(
            ["bridge", "--skip-doctor"]
        )

        self.assertEqual("doctor", doctor_args.cmd)
        self.assertTrue(doctor_args.fix)
        self.assertTrue(run_args.skip_doctor)
        self.assertTrue(bridge_args.skip_doctor)

    def test_run_refuses_start_before_loading_audio_runtime(self) -> None:
        args = main.build_parser().parse_args(
            ["run", "--usb", "不存在的卡"]
        )
        with patch("audio_gateway.doctor.run_doctor", return_value=2) as doctor:
            with patch("builtins.print"):
                code = main.cmd_run(args)

        self.assertEqual(2, code)
        doctor.assert_called_once()
        self.assertTrue(doctor.call_args.kwargs["core_only"])

    def test_bridge_refuses_start_on_core_doctor_failure(self) -> None:
        args = main.build_parser().parse_args(["bridge"])
        with patch("audio_gateway.doctor.run_doctor", return_value=2) as doctor:
            with patch("builtins.print"):
                code = main.cmd_bridge(args)

        self.assertEqual(2, code)
        doctor.assert_called_once()
        self.assertTrue(doctor.call_args.kwargs["core_only"])

    def test_skip_doctor_is_explicit_and_prints_warning(self) -> None:
        with patch("audio_gateway.doctor.run_doctor") as doctor:
            with patch("builtins.print") as output:
                code = main._startup_doctor(
                    GatewayConfig(), skip_doctor=True
                )

        self.assertEqual(0, code)
        doctor.assert_not_called()
        self.assertIn("--skip-doctor", output.call_args.args[0])
        self.assertIn("警告", output.call_args.args[0])

    def test_doctor_command_propagates_final_exit_code(self) -> None:
        args = main.build_parser().parse_args(["doctor", "--fix"])
        with patch("audio_gateway.doctor.run_doctor", return_value=2) as doctor:
            code = main.cmd_doctor(args)

        self.assertEqual(2, code)
        doctor.assert_called_once()
        self.assertTrue(doctor.call_args.kwargs["fix"])


if __name__ == "__main__":
    unittest.main()
