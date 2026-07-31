from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


AUDIO_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUDIO_GATEWAY_ROOT))

from audio_gateway.doctor import (  # noqa: E402
    CHECK_DEFAULT_OUTPUT,
    CHECK_DISK,
    CHECK_INPUT_VOLUME,
    CHECK_REQUIRED_DEPS,
    CHECK_SAMPLE_RATE,
    CHECK_TCC,
    CHECK_USB_INPUT,
    CHECK_USB_OUTPUT,
    CHECK_USB_OUTPUT_VOLUME,
    CHECK_WHISPER,
    DEFAULT_OUTPUT_NAME,
    FAIL,
    OSASCRIPT,
    PASS,
    REQUIRED_DEPENDENCIES,
    SWITCH_AUDIO_SOURCE,
    WARN,
    Doctor,
    DoctorIO,
    render_report,
    run_doctor,
    within_tolerance,
)


class FakeEnvironment:
    def __init__(self) -> None:
        self.devices = [
            {
                "name": "USB Audio Device",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 48000.0,
            },
            {
                "name": "USB Audio Device",
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48000.0,
            },
        ]
        # 耳麦时代拓扑（2026-07-30 起）：默认输入是耳麦，各输入设备各持增益。
        # 30 的校准只属于 USB 采集卡；耳麦 80 是它自己的事。
        self.input_volumes = {
            "USB Audio Device": 30.0,
            "Lenovo Wired VoIP Headset (Teams)": 80.0,
        }
        self.current_input = "Lenovo Wired VoIP Headset (Teams)"
        self.output_volumes = {
            "USB Audio Device": 84.0,
            DEFAULT_OUTPUT_NAME: 45.0,
            "AirPods": 35.0,
        }
        self.current_output = DEFAULT_OUTPUT_NAME
        self.samples = [[0.0], [0.00001]]
        self.free_bytes = 3 * 1024**3
        self.importable = set(REQUIRED_DEPENDENCIES)
        self.commands: list[tuple[str, ...]] = []
        self.fail_output_volume_read = False

    @property
    def input_volume(self) -> float:
        # 历史别名：旧测试用它指"USB 采集卡的增益"
        return self.input_volumes["USB Audio Device"]

    @input_volume.setter
    def input_volume(self, value: float) -> None:
        self.input_volumes["USB Audio Device"] = value

    def config(self, *, usb_keyword: str = "USB Audio Device"):
        return SimpleNamespace(
            usb_keyword=usb_keyword,
            samplerate=48000,
            output_root=Path("/recordings/not-created-yet"),
        )

    def io(self) -> DoctorIO:
        return DoctorIO(
            query_devices=lambda: list(self.devices),
            capture_input=self.capture_input,
            run_command=self.run_command,
            disk_free_bytes=lambda _path: self.free_bytes,
            import_module=self.import_module,
        )

    def capture_input(self, device: int, samplerate: int, seconds: float):
        self.last_capture = (device, samplerate, seconds)
        return self.samples

    def import_module(self, name: str):
        if name not in self.importable:
            raise ModuleNotFoundError(name)
        return object()

    def run_command(self, argv):
        command = tuple(argv)
        self.commands.append(command)
        if command[0] == SWITCH_AUDIO_SOURCE:
            is_input = "-t" in command and (
                command[command.index("-t") + 1] == "input"
            )
            if "-c" in command:
                return self.current_input if is_input else self.current_output
            if "-s" in command:
                name = command[command.index("-s") + 1]
                if is_input:
                    self.current_input = name
                    return self.current_input
                self.current_output = name
                return self.current_output
        if command[0] == OSASCRIPT:
            script = command[command.index("-e") + 1]
            if script == "input volume of (get volume settings)":
                return str(self.input_volumes[self.current_input])
            if script == "output volume of (get volume settings)":
                if self.fail_output_volume_read:
                    raise RuntimeError("simulated output-volume read failure")
                return str(self.output_volumes[self.current_output])
            if script.startswith("set volume input volume "):
                self.input_volumes[self.current_input] = float(
                    script.rsplit(" ", 1)[1]
                )
                return ""
            if script.startswith("set volume output volume "):
                self.output_volumes[self.current_output] = float(
                    script.rsplit(" ", 1)[1]
                )
                return ""
        raise AssertionError(f"unexpected command: {command}")


class DoctorDecisionTests(unittest.TestCase):
    def test_volume_tolerance_is_inclusive(self) -> None:
        self.assertTrue(within_tolerance(28, 30, 2))
        self.assertTrue(within_tolerance(32, 30, 2))
        self.assertFalse(within_tolerance(27.99, 30, 2))
        self.assertFalse(within_tolerance(32.01, 30, 2))

    def test_full_report_passes_with_nonzero_noise_and_optional_warning(self) -> None:
        env = FakeEnvironment()

        report = Doctor(env.config(), io=env.io()).run()

        self.assertEqual(0, report.exit_code)
        self.assertEqual(PASS, report.result(CHECK_USB_INPUT).status)
        self.assertEqual(PASS, report.result(CHECK_USB_OUTPUT).status)
        self.assertEqual(PASS, report.result(CHECK_SAMPLE_RATE).status)
        self.assertEqual(PASS, report.result(CHECK_INPUT_VOLUME).status)
        self.assertEqual(PASS, report.result(CHECK_USB_OUTPUT_VOLUME).status)
        self.assertEqual(PASS, report.result(CHECK_DEFAULT_OUTPUT).status)
        self.assertEqual(PASS, report.result(CHECK_TCC).status)
        self.assertEqual(PASS, report.result(CHECK_DISK).status)
        self.assertEqual(PASS, report.result(CHECK_REQUIRED_DEPS).status)
        self.assertEqual(WARN, report.result(CHECK_WHISPER).status)
        self.assertEqual((0, 48000, 1.0), env.last_capture)

        table = render_report(report)
        self.assertIn("| 项目 | 实测值 | 期望值 | 结果 |", table)
        self.assertIn(CHECK_TCC, table)
        self.assertIn("PASS", table)
        self.assertIn("WARN", table)

    def test_silence_at_startup_warns_but_never_blocks(self) -> None:
        # 2026-07-30 产品裁定：正常流程是"先开好字幕录音、再开始会议"，
        # 此刻没有声音是会议开始前的常态；虚拟音频设备（本机 Teams 的音源）
        # 在没人播放时更是恒为精确零。真正的"录了一场空"交给运行时
        # digital-zero 哨兵（持续 30s 才报警）。
        env = FakeEnvironment()
        env.samples = [[0.0], [0.0]]

        report = Doctor(env.config(), io=env.io()).run()

        result = report.result(CHECK_TCC)
        self.assertEqual(WARN, result.status)
        self.assertEqual(0, report.exit_code)
        self.assertIn("此刻没有声音", result.actual)
        # 文案不许再出现 KVM 时代的"被控机/音频线"字样
        self.assertNotIn("被控机", result.guidance or "")

    def test_capture_open_failure_is_still_fail_closed(self) -> None:
        # 打不开采集流才是真故障（麦克风权限被拒/设备消失），必须阻断
        env = FakeEnvironment()

        def boom(*_args):
            raise RuntimeError("Error opening InputStream: Internal PortAudio error")

        env.capture_input = boom

        report = Doctor(env.config(), io=env.io()).run()

        result = report.result(CHECK_TCC)
        self.assertEqual(FAIL, result.status)
        self.assertEqual(2, report.exit_code)
        self.assertIn("隐私与安全性", result.guidance)

    def test_ambiguous_usb_input_is_a_failure(self) -> None:
        env = FakeEnvironment()
        env.devices.append(
            {
                "name": "USB Audio Device duplicate",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 48000.0,
            }
        )

        report = Doctor(env.config(), io=env.io()).run()

        result = report.result(CHECK_USB_INPUT)
        self.assertEqual(FAIL, result.status)
        self.assertIn("2 个匹配", result.actual)
        self.assertEqual(FAIL, report.result(CHECK_SAMPLE_RATE).status)
        self.assertEqual(2, report.exit_code)

    def test_rate_disk_and_required_dependency_failures_are_reported(self) -> None:
        env = FakeEnvironment()
        env.devices[1]["default_samplerate"] = 44100.0
        env.free_bytes = 1024**3
        env.importable.remove("scipy")

        report = Doctor(env.config(), io=env.io()).run()

        self.assertEqual(FAIL, report.result(CHECK_SAMPLE_RATE).status)
        self.assertEqual(FAIL, report.result(CHECK_DISK).status)
        dependencies = report.result(CHECK_REQUIRED_DEPS)
        self.assertEqual(FAIL, dependencies.status)
        self.assertIn("scipy", dependencies.actual)
        self.assertEqual(2, report.exit_code)

    def test_whisper_backend_presence_changes_warning_to_pass(self) -> None:
        env = FakeEnvironment()
        env.importable.add("mlx_whisper")

        report = Doctor(env.config(), io=env.io()).run()

        self.assertEqual(PASS, report.result(CHECK_WHISPER).status)
        self.assertEqual(0, report.exit_code)

    def test_volume_probes_restore_original_defaults(self) -> None:
        # 输入/输出两个探针各自"切过去-读-切回来"，用户的默认设备一台都不许动。
        env = FakeEnvironment()
        env.current_output = "AirPods"

        report = Doctor(env.config(), io=env.io()).run(core_only=True)

        self.assertEqual(PASS, report.result(CHECK_USB_OUTPUT_VOLUME).status)
        self.assertEqual(PASS, report.result(CHECK_INPUT_VOLUME).status)
        self.assertEqual("AirPods", env.current_output)
        self.assertEqual(
            "Lenovo Wired VoIP Headset (Teams)", env.current_input
        )
        switch_commands = [
            command
            for command in env.commands
            if command[0] == SWITCH_AUDIO_SOURCE and "-s" in command
        ]
        self.assertEqual(
            [
                (
                    SWITCH_AUDIO_SOURCE,
                    "-t",
                    "input",
                    "-s",
                    "USB Audio Device",
                ),
                (
                    SWITCH_AUDIO_SOURCE,
                    "-t",
                    "input",
                    "-s",
                    "Lenovo Wired VoIP Headset (Teams)",
                ),
                (
                    SWITCH_AUDIO_SOURCE,
                    "-t",
                    "output",
                    "-s",
                    "USB Audio Device",
                ),
                (
                    SWITCH_AUDIO_SOURCE,
                    "-t",
                    "output",
                    "-s",
                    "AirPods",
                ),
            ],
            switch_commands,
        )

    def test_headset_as_default_input_reads_usb_card_gain(self) -> None:
        # 2026-07-30 排练验收真机翻车场景：默认输入=耳麦（增益 80），
        # USB 采集卡自己是 30。检查对象必须是采集卡，不许拿耳麦的 80 说事。
        env = FakeEnvironment()
        self.assertEqual(
            "Lenovo Wired VoIP Headset (Teams)", env.current_input
        )
        env.input_volumes["Lenovo Wired VoIP Headset (Teams)"] = 80.0

        report = Doctor(env.config(), io=env.io()).run(core_only=True)

        result = report.result(CHECK_INPUT_VOLUME)
        self.assertEqual(PASS, result.status)
        self.assertEqual("30", result.actual)

    def test_output_volume_probe_restores_after_read_failure(self) -> None:
        env = FakeEnvironment()
        env.current_output = "AirPods"
        env.fail_output_volume_read = True

        report = Doctor(env.config(), io=env.io()).run(core_only=True)

        result = report.result(CHECK_USB_OUTPUT_VOLUME)
        self.assertEqual(WARN, result.status)
        self.assertIn("发言方向预留项", result.guidance)
        self.assertEqual(0, report.exit_code)
        self.assertEqual("AirPods", env.current_output)

    def test_output_volume_deviation_is_reserved_warning_and_non_blocking(
        self,
    ) -> None:
        env = FakeEnvironment()
        env.output_volumes["USB Audio Device"] = 53.0

        report = Doctor(env.config(), io=env.io()).run()

        result = report.result(CHECK_USB_OUTPUT_VOLUME)
        self.assertEqual(WARN, result.status)
        self.assertIn("发言方向预留", result.expected)
        self.assertIn("不阻塞当前接收侧产品", result.guidance)
        self.assertEqual(0, report.exit_code)

    def test_core_only_contains_exact_startup_gate_checks(self) -> None:
        env = FakeEnvironment()

        report = Doctor(env.config(), io=env.io()).run(core_only=True)

        self.assertEqual(
            [
                CHECK_USB_INPUT,
                CHECK_USB_OUTPUT,
                CHECK_INPUT_VOLUME,
                CHECK_USB_OUTPUT_VOLUME,
                CHECK_TCC,
            ],
            [result.name for result in report.results],
        )

    def test_fix_corrects_three_authorized_items_and_rechecks(self) -> None:
        # 设备无关裁定（2026-07-30）：默认输出落在发言声卡上才是故障——
        # 系统提示音会混进会议。--fix 把它抢救回内置扬声器。
        env = FakeEnvironment()
        env.input_volume = 70.0
        env.output_volumes["USB Audio Device"] = 53.0
        env.current_output = "USB Audio Device"
        output: list[str] = []

        code = run_doctor(
            env.config(),
            fix=True,
            io=env.io(),
            output=output.append,
        )

        self.assertEqual(0, code)
        self.assertEqual(30.0, env.input_volume)
        self.assertEqual(84.0, env.output_volumes["USB Audio Device"])
        self.assertEqual(DEFAULT_OUTPUT_NAME, env.current_output)
        joined = "\n".join(output)
        self.assertIn("自动修复:", joined)
        self.assertIn("修复后复检", joined)

    def test_headset_as_default_output_passes_and_is_left_alone(self) -> None:
        # 用户插耳麦后系统默认输出切到耳麦是正常状态（监听跟随它），
        # doctor 不许再把它掰回内置扬声器。
        env = FakeEnvironment()
        env.current_output = "Lenovo Wired VoIP Headset (Teams)"

        report = Doctor(env.config(), io=env.io()).run()

        self.assertEqual(PASS, report.result(CHECK_DEFAULT_OUTPUT).status)

        code = run_doctor(env.config(), fix=True, io=env.io(), output=lambda _: None)
        self.assertEqual(0, code)
        self.assertEqual(
            "Lenovo Wired VoIP Headset (Teams)", env.current_output
        )

    def test_fix_does_not_modify_tcc_or_hide_its_failure(self) -> None:
        # TCC 依然不可被 --fix 越权修改：采集流打不开时照样 fail-closed
        env = FakeEnvironment()

        def boom(*_args):
            raise RuntimeError("Error opening InputStream: Internal PortAudio error")

        env.capture_input = boom
        output: list[str] = []

        code = run_doctor(
            env.config(),
            fix=True,
            io=env.io(),
            output=output.append,
        )

        self.assertEqual(2, code)
        self.assertIn("无需执行可自动修复项", "\n".join(output))
        self.assertIn("隐私与安全性", "\n".join(output))


if __name__ == "__main__":
    unittest.main()
