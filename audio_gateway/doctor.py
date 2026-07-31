"""macOS 音频网关环境体检与有限自动修复。

本模块在导入时只使用 Python 标准库。音频设备、录音和系统命令都通过
``DoctorIO`` 注入，既让依赖缺失能够被 doctor 自身报告，也让单元测试无需
真实声卡、TCC、osascript 或 SwitchAudioSource。
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"

INPUT_VOLUME_TARGET = 30
USB_OUTPUT_VOLUME_TARGET = 84
VOLUME_TOLERANCE = 2
SAMPLE_RATE_TARGET = 48000
DEFAULT_OUTPUT_NAME = "Mac mini扬声器"
MIN_DISK_FREE_BYTES = 2 * 1024**3
TCC_CAPTURE_SECONDS = 1.0

OSASCRIPT = "/usr/bin/osascript"
SWITCH_AUDIO_SOURCE = "/opt/homebrew/bin/SwitchAudioSource"

CHECK_USB_INPUT = "USB 声卡输入端"
CHECK_USB_OUTPUT = "USB 声卡输出端"
CHECK_SAMPLE_RATE = "USB 声卡采样率"
CHECK_INPUT_VOLUME = "系统输入增益"
CHECK_USB_OUTPUT_VOLUME = "USB 声卡输出音量"
CHECK_DEFAULT_OUTPUT = "系统默认输出"
CHECK_TCC = "麦克风 TCC 可用性"
CHECK_DISK = "录音磁盘余量"
CHECK_REQUIRED_DEPS = "Python 必需依赖"
CHECK_WHISPER = "Whisper 后端（可选）"

REQUIRED_DEPENDENCIES = (
    "sounddevice",
    "soundfile",
    "numpy",
    "scipy",
    "aiohttp",
)
WHISPER_BACKENDS = ("mlx_whisper", "faster_whisper")


@dataclass(frozen=True)
class CheckResult:
    """一项体检结果。"""

    name: str
    actual: str
    expected: str
    status: str
    guidance: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    """一次体检的完整、可判定结果。"""

    results: tuple[CheckResult, ...]

    @property
    def has_failures(self) -> bool:
        return any(result.status == FAIL for result in self.results)

    @property
    def exit_code(self) -> int:
        return 2 if self.has_failures else 0

    def result(self, name: str) -> CheckResult:
        for result in self.results:
            if result.name == name:
                return result
        raise KeyError(name)


@dataclass(frozen=True)
class FixResult:
    """一项有限自动修复的执行结果。"""

    name: str
    succeeded: bool
    detail: str


@dataclass(frozen=True)
class DoctorIO:
    """所有环境交互点；测试可用纯内存实现替换。"""

    query_devices: Callable[[], Sequence[Mapping[str, Any]]]
    capture_input: Callable[[int, int, float], Any]
    run_command: Callable[[Sequence[str]], str]
    disk_free_bytes: Callable[[Path], int]
    import_module: Callable[[str], Any]


def within_tolerance(actual: float, expected: float, tolerance: float) -> bool:
    """闭区间容忍比较。"""

    return abs(actual - expected) <= tolerance


def _run_command(argv: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            list(argv),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"命令不存在: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"命令超时: {argv[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(f"{Path(argv[0]).name} 失败: {detail}") from exc
    return completed.stdout.strip()


def _query_devices() -> Sequence[Mapping[str, Any]]:
    sounddevice = importlib.import_module("sounddevice")
    return list(sounddevice.query_devices())


def _capture_input(device: int, samplerate: int, seconds: float) -> Any:
    sounddevice = importlib.import_module("sounddevice")
    return sounddevice.rec(
        int(samplerate * seconds),
        samplerate=samplerate,
        channels=1,
        dtype="float32",
        device=device,
        blocking=True,
    )


def _disk_free_bytes(path: Path) -> int:
    candidate = path.expanduser()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return shutil.disk_usage(candidate).free


def system_io() -> DoctorIO:
    """构造真实 macOS 环境交互实现。"""

    return DoctorIO(
        query_devices=_query_devices,
        capture_input=_capture_input,
        run_command=_run_command,
        disk_free_bytes=_disk_free_bytes,
        import_module=importlib.import_module,
    )


# 数字环回/虚拟音频设备：本机跑会议软件时的音源。它们没有模拟前端，
# "输入增益""输出音量"这类为 3.5mm 线路校准的项目对它们没有意义——
# BlackHole 的 100 就是逐比特直通，把它压到 30 反而会衰减数字信号。
_VIRTUAL_DEVICE_MARKERS = (
    "blackhole",
    "loopback",
    "soundflower",
    "aggregate",
    "multi-output",
    "多输出",
    "聚合",
)


def is_virtual_audio_device(name: str) -> bool:
    lowered = name.casefold()
    return any(marker in lowered for marker in _VIRTUAL_DEVICE_MARKERS)


def _short_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text or exc.__class__.__name__


def _format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.2f}"


def _format_bytes_as_gb(value: int) -> str:
    return f"{value / 1024**3:.2f} GB"


def _contains_nonzero(samples: Any) -> bool:
    """对 ndarray 或普通嵌套序列执行“是否存在精确非零值”判断。"""

    flat = getattr(samples, "flat", None)
    if flat is not None:
        return any(value != 0 for value in flat)
    if isinstance(samples, (bytes, bytearray, memoryview)):
        return any(samples)
    if isinstance(samples, (str, Mapping)):
        return samples != 0
    try:
        iterator = iter(samples)
    except TypeError:
        return samples != 0
    return any(_contains_nonzero(value) for value in iterator)


class Doctor:
    """执行体检，并仅对任务书允许的三项进行自动修复。"""

    def __init__(self, config: Any, io: DoctorIO | None = None):
        self.config = config
        self.io = io or system_io()
        self._devices_loaded = False
        self._devices: list[Mapping[str, Any]] = []
        self._device_error: str | None = None
        self._usb_inputs: list[tuple[int, Mapping[str, Any]]] = []
        self._usb_outputs: list[tuple[int, Mapping[str, Any]]] = []

    def run(self, *, core_only: bool = False) -> DoctorReport:
        self._load_devices()
        results = [
            self._check_usb_endpoint(need_input=True),
            self._check_usb_endpoint(need_input=False),
        ]
        if not core_only:
            results.append(self._check_sample_rate())
        results.extend(
            (
                self._check_input_volume(),
                self._check_usb_output_volume(),
            )
        )
        if not core_only:
            results.append(self._check_default_output())
        results.append(self._check_tcc())
        if not core_only:
            results.extend(
                (
                    self._check_disk(),
                    self._check_required_dependencies(),
                    self._check_whisper_backend(),
                )
            )
        return DoctorReport(tuple(results))

    def apply_fixes(self, report: DoctorReport) -> tuple[FixResult, ...]:
        actionable = {
            result.name for result in report.results if result.status == FAIL
        }
        # 发言链路封存期间，USB 输出音量偏差是非阻断 WARN，但 --fix 仍保留
        # 校准能力。渠道升级恢复发言产品面时，可把检查恢复为 FAIL，无需重写修复。
        actionable.update(
            result.name
            for result in report.results
            if result.name == CHECK_USB_OUTPUT_VOLUME and result.status == WARN
        )
        fixes: list[FixResult] = []
        if CHECK_INPUT_VOLUME in actionable:
            if len(self._usb_inputs) != 1:
                fixes.append(
                    FixResult(
                        CHECK_INPUT_VOLUME,
                        False,
                        "USB 输入端未唯一匹配，doctor 不会猜测设备",
                    )
                )
            else:
                usb_in_name = str(self._usb_inputs[0][1].get("name", ""))
                fixes.append(
                    self._apply_fix(
                        CHECK_INPUT_VOLUME,
                        lambda: self._with_temporary_input(
                            usb_in_name,
                            lambda: self._set_volume(
                                "input", INPUT_VOLUME_TARGET
                            ),
                        ),
                        f"已设为 {INPUT_VOLUME_TARGET}（USB 采集卡）",
                    )
                )
        if CHECK_USB_OUTPUT_VOLUME in actionable:
            if len(self._usb_outputs) != 1:
                fixes.append(
                    FixResult(
                        CHECK_USB_OUTPUT_VOLUME,
                        False,
                        "USB 输出端未唯一匹配，拒绝猜测或修改",
                    )
                )
            else:
                usb_name = str(self._usb_outputs[0][1].get("name", ""))
                fixes.append(
                    self._apply_fix(
                        CHECK_USB_OUTPUT_VOLUME,
                        lambda: self._with_temporary_output(
                            usb_name,
                            lambda: self._set_volume(
                                "output", USB_OUTPUT_VOLUME_TARGET
                            ),
                        ),
                        f"已设为 {USB_OUTPUT_VOLUME_TARGET}，并恢复原默认输出",
                    )
                )
        if CHECK_DEFAULT_OUTPUT in actionable:
            fixes.append(
                self._apply_fix(
                    CHECK_DEFAULT_OUTPUT,
                    lambda: self._set_current_output(DEFAULT_OUTPUT_NAME),
                    f"已切换为 {DEFAULT_OUTPUT_NAME}",
                )
            )
        return tuple(fixes)

    def _apply_fix(
        self,
        name: str,
        operation: Callable[[], Any],
        success_detail: str,
    ) -> FixResult:
        try:
            operation()
        except Exception as exc:
            return FixResult(name, False, f"修复失败: {_short_error(exc)}")
        return FixResult(name, True, success_detail)

    def _load_devices(self) -> None:
        if self._devices_loaded:
            return
        self._devices_loaded = True
        try:
            self._devices = list(self.io.query_devices())
            self._usb_inputs = self._matching_devices(need_input=True)
            self._usb_outputs = self._matching_devices(need_input=False)
        except Exception as exc:
            self._device_error = _short_error(exc)

    def _matching_devices(
        self, *, need_input: bool
    ) -> list[tuple[int, Mapping[str, Any]]]:
        keyword = self.config.usb_keyword.casefold()
        channel_key = (
            "max_input_channels" if need_input else "max_output_channels"
        )
        matches = []
        for index, device in enumerate(self._devices):
            name = str(device.get("name", ""))
            channels = int(device.get(channel_key, 0) or 0)
            if keyword in name.casefold() and channels > 0:
                matches.append((index, device))
        return matches

    def _check_usb_endpoint(self, *, need_input: bool) -> CheckResult:
        name = CHECK_USB_INPUT if need_input else CHECK_USB_OUTPUT
        matches = self._usb_inputs if need_input else self._usb_outputs
        endpoint = "输入" if need_input else "输出"
        if self._device_error is not None:
            return CheckResult(
                name,
                f"设备查询失败: {self._device_error}",
                f"关键字 {self.config.usb_keyword!r} 恰好匹配 1 个{endpoint}端",
                FAIL,
                "检查 sounddevice/PortAudio，并运行 list-devices 核对设备。",
            )
        if len(matches) == 1:
            index, device = matches[0]
            return CheckResult(
                name,
                f"#{index} {device.get('name', '<未命名>')}",
                f"关键字 {self.config.usb_keyword!r} 恰好匹配 1 个{endpoint}端",
                PASS,
            )
        if matches:
            actual = f"{len(matches)} 个匹配: " + ", ".join(
                f"#{index} {device.get('name', '<未命名>')}"
                for index, device in matches
            )
        else:
            actual = "0 个匹配"
        return CheckResult(
            name,
            actual,
            f"关键字 {self.config.usb_keyword!r} 恰好匹配 1 个{endpoint}端",
            FAIL,
            "运行 `python -m audio_gateway list-devices`，并用更精确的 --usb；拒绝猜测。",
        )

    def _check_sample_rate(self) -> CheckResult:
        expected = SAMPLE_RATE_TARGET
        if len(self._usb_inputs) != 1 or len(self._usb_outputs) != 1:
            return CheckResult(
                CHECK_SAMPLE_RATE,
                "无法判定（USB 输入/输出端未唯一匹配）",
                f"输入/输出均为 {expected} Hz",
                FAIL,
                "先解决 USB 声卡缺失或歧义。",
            )
        try:
            input_rate = float(
                self._usb_inputs[0][1]["default_samplerate"]
            )
            output_rate = float(
                self._usb_outputs[0][1]["default_samplerate"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            return CheckResult(
                CHECK_SAMPLE_RATE,
                f"读取失败: {_short_error(exc)}",
                f"输入/输出均为 {expected} Hz",
                FAIL,
                "在“音频 MIDI 设置”中核对两端格式。",
            )
        passed = input_rate == expected and output_rate == expected
        return CheckResult(
            CHECK_SAMPLE_RATE,
            f"输入 {_format_number(input_rate)} Hz / "
            f"输出 {_format_number(output_rate)} Hz",
            f"输入/输出均为 {expected} Hz",
            PASS if passed else FAIL,
            None if passed else "在“音频 MIDI 设置”中把 USB 声卡设为 48000 Hz。",
        )

    def _check_input_volume(self) -> CheckResult:
        # 30 的校准对象是 USB 采集卡的线路输入，不是"当前默认输入"。
        # 两者曾经恰好是同一设备；耳麦时代（2026-07-30 起）默认输入是耳麦。
        if len(self._usb_inputs) == 1:
            name = str(self._usb_inputs[0][1].get("name", ""))
            if is_virtual_audio_device(name):
                return CheckResult(
                    CHECK_INPUT_VOLUME,
                    f"不适用（{name} 是数字音源，无模拟增益）",
                    "仅对模拟线路输入校准",
                    PASS,
                )
        if len(self._usb_inputs) != 1:
            return CheckResult(
                CHECK_INPUT_VOLUME,
                "无法判定（USB 输入端未唯一匹配）",
                f"{INPUT_VOLUME_TARGET} ±{VOLUME_TOLERANCE}",
                FAIL,
                "先解决 USB 输入端缺失或歧义。",
            )
        usb_name = str(self._usb_inputs[0][1].get("name", ""))
        try:
            value = self._with_temporary_input(
                usb_name, lambda: self._get_volume("input")
            )
        except Exception as exc:
            return CheckResult(
                CHECK_INPUT_VOLUME,
                f"读取失败: {_short_error(exc)}",
                f"{INPUT_VOLUME_TARGET} ±{VOLUME_TOLERANCE}",
                FAIL,
                "可运行 `doctor --fix`；若仍失败，检查 osascript 权限。",
            )
        passed = within_tolerance(
            value, INPUT_VOLUME_TARGET, VOLUME_TOLERANCE
        )
        return CheckResult(
            CHECK_INPUT_VOLUME,
            _format_number(value),
            f"{INPUT_VOLUME_TARGET} ±{VOLUME_TOLERANCE}",
            PASS if passed else FAIL,
            None if passed else "运行 `python -m audio_gateway doctor --fix`。",
        )

    def _check_usb_output_volume(self) -> CheckResult:
        expected = (
            f"{USB_OUTPUT_VOLUME_TARGET} ±{VOLUME_TOLERANCE}"
            "（发言方向预留）"
        )
        reserved_guidance = (
            "发言方向预留项，不阻塞当前接收侧产品；"
            "需要保留校准时运行 `python -m audio_gateway doctor --fix`。"
        )
        if len(self._usb_outputs) != 1:
            return CheckResult(
                CHECK_USB_OUTPUT_VOLUME,
                "无法判定（USB 输出端未唯一匹配）",
                expected,
                WARN,
                "发言方向预留项，不阻塞当前接收侧产品；"
                "若要校准，先解决 USB 输出端缺失或歧义，doctor 不会猜测设备。",
            )
        usb_name = str(self._usb_outputs[0][1].get("name", ""))
        if is_virtual_audio_device(usb_name):
            return CheckResult(
                CHECK_USB_OUTPUT_VOLUME,
                f"不适用（{usb_name} 是数字音源，无硬件音量）",
                "仅对模拟输出校准",
                PASS,
            )
        try:
            value = self._with_temporary_output(
                usb_name, lambda: self._get_volume("output")
            )
        except Exception as exc:
            return CheckResult(
                CHECK_USB_OUTPUT_VOLUME,
                f"读取失败: {_short_error(exc)}",
                expected,
                WARN,
                "发言方向预留项，不阻塞当前接收侧产品；"
                "确认 /opt/homebrew/bin/SwitchAudioSource 已安装后可再运行 doctor --fix。",
            )
        passed = within_tolerance(
            value, USB_OUTPUT_VOLUME_TARGET, VOLUME_TOLERANCE
        )
        return CheckResult(
            CHECK_USB_OUTPUT_VOLUME,
            _format_number(value),
            expected,
            PASS if passed else WARN,
            None if passed else reserved_guidance,
        )

    def _check_default_output(self) -> CheckResult:
        # 设备无关裁定（2026-07-30）：监听跟随系统默认输出，用户插耳麦后默认
        # 输出变成耳麦是正常状态，不再钉死内置扬声器。唯一危险的是默认输出
        # 落在发言声卡上——系统提示音会直接混进会议，监听也会回灌，必须 FAIL。
        expected = f"非发言声卡（{self.config.usb_keyword}）的任意输出"
        try:
            current = self._current_output()
        except Exception as exc:
            return CheckResult(
                CHECK_DEFAULT_OUTPUT,
                f"读取失败: {_short_error(exc)}",
                expected,
                FAIL,
                "确认 /opt/homebrew/bin/SwitchAudioSource 已安装。",
            )
        # 与 expected 同源：用运行时配置的 usb_keyword（模块里不存在 USB_KEYWORD 常量）。
        passed = self.config.usb_keyword.lower() not in current.lower()
        return CheckResult(
            CHECK_DEFAULT_OUTPUT,
            current,
            expected,
            PASS if passed else FAIL,
            None
            if passed
            else "系统默认输出是发言声卡，提示音会混进会议。"
            "运行 `python -m audio_gateway doctor --fix`。",
        )

    def _check_tcc(self) -> CheckResult:
        """能否打开采集流 = FAIL 判据；此刻有没有声音 = 只报告，不阻断。

        2026-07-30 产品裁定（用户指出）：正常流程本就是"先开好字幕录音、
        再开始会议"，要求"先有声音才准启动"是把因果颠倒了。而且虚拟音频
        设备（BlackHole，本机跑 Teams 时的音源）在没人播放时**就是精确零**
        ——那是它的正常状态，不是故障，旧判据让本机开会模式永远启动不了。

        真正的"录了一场空"由运行时的 digital-zero 哨兵负责（持续 30s 静音
        才报警，且会显示在菜单栏与字幕窗口）——那才是能区分"会议还没开始"
        与"链路真断了"的时机。
        """
        expected = "能打开采集流（此刻是否有声音不作为启动门禁）"
        if len(self._usb_inputs) != 1:
            return CheckResult(
                CHECK_TCC,
                "无法采集（音频输入端未唯一匹配）",
                expected,
                FAIL,
                "先解决音频输入端缺失或歧义。",
            )
        device_index = self._usb_inputs[0][0]
        try:
            samples = self.io.capture_input(
                device_index,
                SAMPLE_RATE_TARGET,
                TCC_CAPTURE_SECONDS,
            )
        except Exception as exc:
            # 打不开流才是真故障：麦克风权限被拒或设备消失
            return CheckResult(
                CHECK_TCC,
                f"采集失败: {_short_error(exc)}",
                expected,
                FAIL,
                "检查系统设置 → 隐私与安全性 → 麦克风，确认已允许本 App。",
            )
        if not _contains_nonzero(samples):
            return CheckResult(
                CHECK_TCC,
                "采集流正常，此刻没有声音",
                expected,
                WARN,
                "会议开始后若持续 30 秒仍无声音，菜单栏会给出提示。",
            )
        return CheckResult(
            CHECK_TCC,
            "采集流正常，已听到声音",
            expected,
            PASS,
        )

    def _check_disk(self) -> CheckResult:
        try:
            free = self.io.disk_free_bytes(Path(self.config.output_root))
        except Exception as exc:
            return CheckResult(
                CHECK_DISK,
                f"读取失败: {_short_error(exc)}",
                f">= {_format_bytes_as_gb(MIN_DISK_FREE_BYTES)}",
                FAIL,
                f"检查录音目录 {self.config.output_root} 所在卷。",
            )
        passed = free >= MIN_DISK_FREE_BYTES
        return CheckResult(
            CHECK_DISK,
            _format_bytes_as_gb(free),
            f">= {_format_bytes_as_gb(MIN_DISK_FREE_BYTES)}",
            PASS if passed else FAIL,
            None if passed else f"清理 {self.config.output_root} 所在卷后再启动。",
        )

    def _check_required_dependencies(self) -> CheckResult:
        available, missing = self._probe_modules(REQUIRED_DEPENDENCIES)
        actual_parts = []
        if available:
            actual_parts.append("可导入: " + ", ".join(available))
        if missing:
            actual_parts.append(
                "不可导入: "
                + ", ".join(f"{name} ({reason})" for name, reason in missing)
            )
        return CheckResult(
            CHECK_REQUIRED_DEPS,
            "; ".join(actual_parts),
            ", ".join(REQUIRED_DEPENDENCIES),
            FAIL if missing else PASS,
            (
                "在 audio-gateway 虚拟环境运行 `pip install -r requirements.txt`。"
                if missing
                else None
            ),
        )

    def _check_whisper_backend(self) -> CheckResult:
        available, missing = self._probe_modules(WHISPER_BACKENDS)
        if available:
            return CheckResult(
                CHECK_WHISPER,
                "可导入: " + ", ".join(available),
                "mlx_whisper 或 faster_whisper（至少一个）",
                PASS,
            )
        reasons = ", ".join(f"{name} ({reason})" for name, reason in missing)
        return CheckResult(
            CHECK_WHISPER,
            "均不可导入: " + reasons,
            "mlx_whisper 或 faster_whisper（至少一个）",
            WARN,
            "不阻止 doctor/run 启动；需要转写时再安装一个 Whisper 后端。",
        )

    def _probe_modules(
        self, names: Sequence[str]
    ) -> tuple[list[str], list[tuple[str, str]]]:
        available = []
        missing = []
        for name in names:
            try:
                self.io.import_module(name)
            except Exception as exc:
                missing.append((name, _short_error(exc)))
            else:
                available.append(name)
        return available, missing

    def _get_volume(self, kind: str) -> float:
        output = self.io.run_command(
            (
                OSASCRIPT,
                "-e",
                f"{kind} volume of (get volume settings)",
            )
        )
        try:
            return float(output)
        except ValueError as exc:
            raise RuntimeError(f"无法解析{kind}音量: {output!r}") from exc

    def _set_volume(self, kind: str, value: int) -> None:
        self.io.run_command(
            (
                OSASCRIPT,
                "-e",
                f"set volume {kind} volume {value}",
            )
        )

    def _current_input(self) -> str:
        current = self.io.run_command(
            (SWITCH_AUDIO_SOURCE, "-c", "-t", "input")
        ).strip()
        if not current:
            raise RuntimeError("SwitchAudioSource 未返回当前输入设备")
        return current

    def _set_current_input(self, name: str) -> None:
        self.io.run_command(
            (SWITCH_AUDIO_SOURCE, "-t", "input", "-s", name)
        )

    def _with_temporary_input(
        self, target: str, operation: Callable[[], Any]
    ) -> Any:
        """临时把默认输入切到 target 执行 operation，随后必恢复原默认输入。

        为什么需要：osascript 的 input volume 只作用于"当前默认输入设备"。
        用户插上耳麦后默认输入是耳麦（排练正需要它），此时读到的是耳麦的
        增益而不是 USB 采集卡的 30——2026-07-30 排练验收实测（读到 80 →
        FAIL 拒绝开会）。与 _with_temporary_output 同款：恢复失败必须炸，
        绝不把用户的默认输入偷偷留在采集卡上。
        """
        original = self._current_input()
        needs_restore = original != target
        result: Any = None
        failure: Exception | None = None
        try:
            if needs_restore:
                self._set_current_input(target)
            result = operation()
        except Exception as exc:
            failure = exc
        if needs_restore:
            try:
                self._set_current_input(original)
            except Exception as restore_exc:
                if failure is not None:
                    raise RuntimeError(
                        f"{_short_error(failure)}；且恢复原默认输入 {original!r} 失败: "
                        f"{_short_error(restore_exc)}"
                    ) from restore_exc
                raise RuntimeError(
                    f"恢复原默认输入 {original!r} 失败: "
                    f"{_short_error(restore_exc)}"
                ) from restore_exc
        if failure is not None:
            raise failure
        return result

    def _current_output(self) -> str:
        current = self.io.run_command(
            (SWITCH_AUDIO_SOURCE, "-c", "-t", "output")
        ).strip()
        if not current:
            raise RuntimeError("SwitchAudioSource 未返回当前输出设备")
        return current

    def _set_current_output(self, name: str) -> None:
        self.io.run_command(
            (SWITCH_AUDIO_SOURCE, "-t", "output", "-s", name)
        )

    def _with_temporary_output(
        self, target: str, operation: Callable[[], Any]
    ) -> Any:
        original = self._current_output()
        needs_restore = original != target
        result: Any = None
        failure: Exception | None = None
        try:
            if needs_restore:
                self._set_current_output(target)
            result = operation()
        except Exception as exc:
            failure = exc
        if needs_restore:
            try:
                self._set_current_output(original)
            except Exception as restore_exc:
                if failure is not None:
                    raise RuntimeError(
                        f"{_short_error(failure)}；且恢复原默认输出 {original!r} 失败: "
                        f"{_short_error(restore_exc)}"
                    ) from restore_exc
                raise RuntimeError(
                    f"恢复原默认输出 {original!r} 失败: "
                    f"{_short_error(restore_exc)}"
                ) from restore_exc
        if failure is not None:
            raise failure
        return result


def render_report(report: DoctorReport, *, title: str = "音频网关 doctor 体检") -> str:
    """输出稳定、可读、便于测试的逐项表格。"""

    def cell(value: str) -> str:
        return " ".join(value.split()).replace("|", "\\|")

    lines = [
        title,
        "| 项目 | 实测值 | 期望值 | 结果 |",
        "|---|---|---|---|",
    ]
    lines.extend(
        f"| {cell(result.name)} | {cell(result.actual)} | "
        f"{cell(result.expected)} | {result.status} |"
        for result in report.results
    )
    guided = [result for result in report.results if result.guidance]
    if guided:
        lines.append("")
        lines.append("指引:")
        lines.extend(
            f"- {result.name}: {result.guidance}" for result in guided
        )
    return "\n".join(lines)


def run_doctor(
    config: Any,
    *,
    fix: bool = False,
    core_only: bool = False,
    io: DoctorIO | None = None,
    output: Callable[[str], None] = print,
    title: str = "音频网关 doctor 体检",
) -> int:
    """执行体检，按需有限修复并以最终复检结果返回 0/2。"""

    doctor = Doctor(config, io=io)
    initial = doctor.run(core_only=core_only)
    output(render_report(initial, title=title))
    if not fix:
        return initial.exit_code

    output("")
    output("自动修复:")
    fixes = doctor.apply_fixes(initial)
    if fixes:
        for result in fixes:
            marker = "PASS" if result.succeeded else "FAIL"
            output(f"- {result.name}: {marker} - {result.detail}")
    else:
        output("- 无需执行可自动修复项")

    final = Doctor(config, io=doctor.io).run(core_only=core_only)
    output("")
    output(render_report(final, title=f"{title}（修复后复检）"))
    return final.exit_code
