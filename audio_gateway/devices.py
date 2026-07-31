"""模块 A：USB 声卡 / 监听设备 / 麦克风 的识别与绑定（fail-closed）。"""

from __future__ import annotations

from dataclasses import dataclass

import sounddevice as sd


class DeviceResolutionError(RuntimeError):
    pass


@dataclass
class ResolvedDevices:
    mac_in: int                 # USB 声卡粉孔（收音，input）
    mac_out: int                # USB 声卡绿孔（发言，output）
    monitor: int | None = None  # 本地监听（output，如蓝牙耳机）
    mic: int | None = None      # 物理麦克风（input）
    monitor_note: str | None = None  # 监听解析说明（跟随/回退时给启动日志）


def _candidates(keyword: str, *, need_input: bool) -> list[int]:
    kw = keyword.lower()
    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if kw not in dev["name"].lower():
            continue
        ch = dev["max_input_channels"] if need_input else dev["max_output_channels"]
        if ch > 0:
            out.append(idx)
    return out


def _resolve_one(keyword: str, *, need_input: bool, what: str) -> int:
    found = _candidates(keyword, need_input=need_input)
    if len(found) == 1:
        return found[0]
    kind = "输入" if need_input else "输出"
    if not found:
        raise DeviceResolutionError(
            f"未找到{what}（关键字 {keyword!r}，{kind}端）。"
            f"请运行 `python -m audio_gateway list-devices` 核对设备名。"
        )
    names = ", ".join(f"[{i}] {sd.query_devices(i)['name']}" for i in found)
    raise DeviceResolutionError(
        f"{what} 关键字 {keyword!r} 命中多个{kind}设备：{names}。"
        f"请用更精确的关键字，拒绝猜测。"
    )


def resolve(cfg) -> ResolvedDevices:
    mac_in = _resolve_one(cfg.usb_keyword, need_input=True, what="USB 声卡 Mac_In(粉孔)")
    mac_out = _resolve_one(cfg.usb_keyword, need_input=False, what="USB 声卡 Mac_Out(绿孔)")
    monitor_note: str | None = None
    if cfg.monitor_keyword == MONITOR_FOLLOW_SYSTEM:
        monitor, monitor_note = resolve_monitor_following_default(mac_out)
        if monitor < 0:
            monitor = None
    elif cfg.monitor_keyword:
        monitor = _resolve_one(cfg.monitor_keyword, need_input=False, what="本地监听设备")
    else:
        monitor = None
    mic = (
        _resolve_one(cfg.mic_keyword, need_input=True, what="物理麦克风")
        if cfg.mic_keyword else None
    )
    return ResolvedDevices(
        mac_in=mac_in,
        mac_out=mac_out,
        monitor=monitor,
        mic=mic,
        monitor_note=monitor_note,
    )


def resolve_output(keyword: str, *, what: str = "输出设备") -> int:
    """Resolve one output-only device with the same fail-closed rules."""
    return _resolve_one(keyword, need_input=False, what=what)


# 监听设备的"跟随系统默认"哨兵值。产品裁定（2026-07-30）：正常的会议软件从不问
# "你耳机插哪"，插上耳麦系统自动切默认输出，声音就该跟过去。设备是运行时事实，
# 不是产品配置，所以不再硬编码具体设备名。
MONITOR_FOLLOW_SYSTEM = "system"


def resolve_monitor_following_default(
    mac_out: int,
    *,
    sounddevice_module=None,
) -> tuple[int, str]:
    """按"会议开始那一刻的系统默认输出"解析监听设备，带防回灌护栏。

    返回 (device_index, note)。护栏：若系统默认输出恰好是 Mac_Out（USB 声卡）——
    比如用户误把 USB 声卡设成了默认输出——监听流会原样打回被控机麦克风，
    形成会议内啸叫。此时绝不跟随，回退到内置扬声器并在 note 里说明；
    连内置扬声器都找不到才放弃监听（返回 -1），录音与字幕照常。
    """
    sdm = sounddevice_module or sd
    try:
        default_out = sdm.default.device[1]
        if default_out is None or int(default_out) < 0:
            raise ValueError("no default output")
        default_out = int(default_out)
        name = str(sdm.query_devices(default_out)["name"])
    except Exception:
        return -1, "系统没有可用的默认输出设备，本场会议无本机监听"

    if default_out != mac_out:
        return default_out, f"监听跟随系统默认输出：#{default_out} {name}"

    # 防回灌回退：优先内置扬声器
    for idx, dev in enumerate(sdm.query_devices()):
        if dev["max_output_channels"] <= 0 or idx == mac_out:
            continue
        if "扬声器" in dev["name"] or "speaker" in dev["name"].lower():
            return idx, (
                f"系统默认输出是发言声卡（会回灌会议），已改用 #{idx} {dev['name']} 监听"
            )
    return -1, "系统默认输出是发言声卡且找不到替代设备，本场会议无本机监听"


def resolve_rehearsal_mic_following_default(
    mac_in: int,
    *,
    sounddevice_module=None,
) -> tuple[int, str]:
    """按系统默认输入解析排练麦克风，带防错拾护栏。

    返回 (device_index, note)；device_index=-1 表示本场不开排练（会议照常）。
    护栏：默认输入恰好是会议采集声卡（Mac_In）时绝不使用——耳麦拔掉后 macOS
    常会把默认输入落回 USB 声卡（2026-07-30 实测过），那时"排练"会把**会议
    对方的声音**当成"我的发言"转写翻译，输出完全错乱且花的是双倍 API 钱。
    """
    sdm = sounddevice_module or sd
    try:
        default_in = sdm.default.device[0]
        if default_in is None or int(default_in) < 0:
            raise ValueError("no default input")
        default_in = int(default_in)
        name = str(sdm.query_devices(default_in)["name"])
    except Exception:
        return -1, "系统没有可用的默认输入设备（插上耳麦后重开会议即可排练）"

    if default_in == mac_in:
        return -1, (
            "系统默认输入是会议采集声卡，排练会把对方的声音当成你的发言——"
            "本场不开排练。插上耳麦（系统会自动切默认输入）后重开会议即可。"
        )
    return default_in, f"排练麦克风跟随系统默认输入：#{default_in} {name}"


def resolve_own_voice_output(
    mic_device: int,
    mac_out: int,
    *,
    sounddevice_module=None,
) -> tuple[int, str]:
    """解析"只进我自己耳朵"的语音输出设备。

    首选与排练麦克风同一台物理设备的输出侧（耳麦本身，如 Lenovo 耳麦
    in=1/out=2）：戴在头上，日语只进自己耳朵，零声学泄漏进会议。
    找不到安全设备就返回 -1（保持 M1 的纯字幕形态），绝不退到扬声器——
    桌面扬声器外放会被耳麦麦克风拾回，把自己的日语再当成输入转一遍。
    mac_out（发言声卡）在这里同样禁止：那是"进会议"，不是"进耳朵"。
    """
    sdm = sounddevice_module or sd
    try:
        info = sdm.query_devices(mic_device)
    except Exception:
        return -1, "排练语音：读取麦克风设备信息失败，本场只显示字幕"
    if mic_device != mac_out and int(info.get("max_output_channels", 0)) > 0:
        return mic_device, (
            f"排练语音进耳机：#{mic_device} {info['name']}（只有你能听到）"
        )
    return -1, (
        "排练语音：当前麦克风设备没有耳机输出，"
        "本场只显示字幕（换带耳机口的耳麦即可听到日语）"
    )


def monitor_should_reroute_to_headset(
    monitor: int | None,
    monitor_note: str | None,
    own_device: int,
) -> bool:
    """发言/排练已确认耳麦时，"跟随系统默认"的监听是否应改道进同一台耳麦。

    2026-07-31 真实会议（20260731-171929）教训：用户戴 Lenovo 耳麦发言，
    系统默认输出却是 Mac mini 扬声器——自己的日语在耳机里、客户的声音在
    房间外放，用户"只听得见自己"。会议里"你的耳朵"只有一个物理位置：
    耳麦一旦确认（own_device 有效），会议原声必须与你的日语同进这台耳麦。
    monitor_note 只有"跟随系统默认"模式才会置值；显式 --monitor <设备>
    时为 None——用户点名的设备不改道。
    """
    if own_device < 0:
        return False
    if monitor_note is None:
        return False
    return monitor is None or monitor != own_device


def list_devices() -> str:
    lines = ["idx  in/out  default_sr  name", "---  ------  ----------  ----"]
    for idx, dev in enumerate(sd.query_devices()):
        lines.append(
            f"{idx:>3}  {dev['max_input_channels']:>2}/{dev['max_output_channels']:<3} "
            f"{int(dev['default_samplerate']):>10}  {dev['name']}"
        )
    return "\n".join(lines)
