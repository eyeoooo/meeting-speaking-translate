"""素材放完 ≠ 会议结束：pad_silence 语义的回归。

2026-07-30 M1 验收实测：素材结束立刻关 taps 等于拔线——interpreter 收到
哨兵就关 Realtime 会话，服务端管线里尚未吐出的转写整段丢失（句 2-4 消失、
句 1 译文截断）。bridge 场景必须泵静音直到显式 stop()。
"""

from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

import numpy as np

AUDIO_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUDIO_GATEWAY_ROOT))

from audio_gateway.replay import WavReplayBus  # noqa: E402


def _write_tone(path, seconds=0.05, samplerate=48000):
    t = np.arange(int(samplerate * seconds)) / samplerate
    samples = (0.3 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(samplerate)
        handle.writeframes(samples.tobytes())
    return len(samples)


def test_pad_silence_keeps_taps_open_after_material_ends(tmp_path):
    path = tmp_path / "short.wav"
    material_samples = _write_tone(path)

    bus = WavReplayBus(path, blocksize=512, realtime=False, pad_silence=True)
    tap = bus.subscribe("probe")
    bus.start()

    # 全速模式下素材瞬间放完；之后必须仍有帧（静音）持续到达且无哨兵
    deadline = time.monotonic() + 5.0
    got_silence_after_material = False
    drained = 0
    while time.monotonic() < deadline:
        frame = tap.q.get(timeout=2)
        assert frame is not None, "素材放完后 taps 不许关闭（等于拔线）"
        drained += len(frame)
        if drained > material_samples and float(np.abs(frame).max()) == 0.0:
            got_silence_after_material = True
            break
    assert got_silence_after_material

    # 显式 stop() 才关闭：与 RxBus 的最终收尾语义一致
    bus.stop()
    while True:
        frame = tap.q.get(timeout=2)
        if frame is None:
            break


def test_default_behavior_still_closes_taps_at_end(tmp_path):
    # 单元测试场景保持旧语义：素材放完即收尾，drain 有确定终点
    path = tmp_path / "short.wav"
    _write_tone(path)

    bus = WavReplayBus(path, blocksize=512, realtime=False)
    tap = bus.subscribe("probe")
    bus.start()
    bus.wait_until_finished(timeout=5)

    saw_sentinel = False
    while True:
        frame = tap.q.get(timeout=2)
        if frame is None:
            saw_sentinel = True
            break
    assert saw_sentinel
    bus.stop()
