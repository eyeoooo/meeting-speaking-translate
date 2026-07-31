"""监听设备"跟随系统默认"的解析规则与防回灌护栏。

产品裁定（2026-07-30）：设备是运行时事实，不是产品配置。监听输出跟随
"会议开始那一刻的系统默认输出"，唯一例外是默认输出恰好是发言声卡——
那会把会议下行原样打回被控机麦克风形成啸叫，必须硬拦。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

AUDIO_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUDIO_GATEWAY_ROOT))

from audio_gateway.devices import (  # noqa: E402
    MONITOR_FOLLOW_SYSTEM,
    monitor_should_reroute_to_headset,
    resolve_monitor_following_default,
)


def _sd(devices, default_out):
    def query_devices(idx=None):
        if idx is None:
            return devices
        return devices[idx]

    return SimpleNamespace(
        default=SimpleNamespace(device=(0, default_out)),
        query_devices=query_devices,
    )


DEVICES = [
    {"name": "HDP-V104", "max_output_channels": 2, "max_input_channels": 0},
    {"name": "USB Audio Device", "max_output_channels": 2, "max_input_channels": 1},
    {"name": "Type-C 耳麦", "max_output_channels": 2, "max_input_channels": 1},
    {"name": "Mac mini扬声器", "max_output_channels": 2, "max_input_channels": 0},
]
MAC_OUT = 1


def test_follows_system_default_when_it_is_not_the_uplink_card():
    device, note = resolve_monitor_following_default(
        MAC_OUT, sounddevice_module=_sd(DEVICES, default_out=2)
    )
    assert device == 2
    assert "Type-C 耳麦" in note


def test_hard_intercepts_uplink_card_and_falls_back_to_builtin_speaker():
    # 用户误把 USB 声卡设成系统默认输出：绝不跟随（会啸叫回灌会议），
    # 回退到内置扬声器并把原因写进 note。
    device, note = resolve_monitor_following_default(
        MAC_OUT, sounddevice_module=_sd(DEVICES, default_out=MAC_OUT)
    )
    assert device == 3
    assert "回灌" in note


def test_gives_up_monitoring_when_no_safe_output_exists():
    devices = [
        {"name": "USB Audio Device", "max_output_channels": 2, "max_input_channels": 1},
    ]
    device, note = resolve_monitor_following_default(
        0, sounddevice_module=_sd(devices, default_out=0)
    )
    assert device == -1
    assert "无本机监听" in note


def test_missing_default_output_degrades_to_no_monitor():
    device, note = resolve_monitor_following_default(
        MAC_OUT, sounddevice_module=_sd(DEVICES, default_out=None)
    )
    assert device == -1
    assert "无本机监听" in note


def test_sentinel_value_is_stable_contract():
    # Swift 侧以 --monitor system 传参，这个字符串就是跨语言契约
    assert MONITOR_FOLLOW_SYSTEM == "system"


# ---- 监听归耳（2026-07-31 真实会议 20260731-171929 教训）----
# 用户戴 Lenovo 耳麦发言，系统默认输出却是 Mac mini 扬声器：自己的日语
# 在耳机里、客户的声音在房间外放，用户"只听得见自己"。耳麦一旦确认，
# 会议原声必须与发言回放同进这台耳麦。

FOLLOW_NOTE = "监听跟随系统默认输出：#7 Mac mini扬声器"


def test_reroutes_to_headset_when_follow_mode_picked_another_device():
    # 复现事故现场：monitor=扬声器(7)，耳麦=3 → 必须改道
    assert monitor_should_reroute_to_headset(7, FOLLOW_NOTE, 3)


def test_reroutes_even_when_follow_mode_found_no_default_output():
    # 系统没有默认输出（monitor=None）但耳麦在：会议原声不能凭空消失
    assert monitor_should_reroute_to_headset(None, "系统没有可用的默认输出设备", 3)


def test_no_reroute_when_already_on_the_headset():
    assert not monitor_should_reroute_to_headset(3, FOLLOW_NOTE, 3)


def test_no_reroute_without_a_confirmed_headset():
    # 纯字幕/无安全耳机形态（own_device=-1）：维持跟随系统默认
    assert not monitor_should_reroute_to_headset(7, FOLLOW_NOTE, -1)


def test_explicit_monitor_keyword_is_never_overridden():
    # 显式 --monitor <设备> 时 monitor_note 为 None：用户点名的设备不改道
    assert not monitor_should_reroute_to_headset(7, None, 3)
