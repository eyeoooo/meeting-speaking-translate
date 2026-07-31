"""WavReplayBus 必须与 RxBus 行为一致，否则用它跑出来的回归结论不可信。"""

from __future__ import annotations

import wave

import numpy as np
import pytest

from audio_gateway.replay import WavReplayBus


def _write_wav(path, samples: np.ndarray, samplerate: int = 48000, channels: int = 1):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(samplerate)
        handle.writeframes(samples.astype(np.int16).tobytes())


def _drain(tap) -> np.ndarray:
    frames = []
    while True:
        frame = tap.q.get(timeout=5)
        if frame is None:
            break
        frames.append(frame)
    return np.concatenate(frames) if frames else np.array([], dtype=np.float32)


def _tone(seconds: float = 0.5, samplerate: int = 48000, freq: float = 440.0):
    t = np.arange(int(samplerate * seconds)) / samplerate
    return (0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)


def test_replays_samples_without_distortion(tmp_path):
    samples = _tone()
    path = tmp_path / "tone.wav"
    _write_wav(path, samples)

    bus = WavReplayBus(path, blocksize=1024, realtime=False)
    tap = bus.subscribe("probe")
    bus.start()
    bus.wait_until_finished(timeout=10)
    played = _drain(tap)
    bus.stop()

    expected = samples.astype(np.float32) / 32768.0
    # 回放必须是逐样本一致的：任何失真都会让"回放验证"本身变成噪声源
    assert np.abs(played[: len(expected)] - expected).max() == pytest.approx(0.0)
    assert tap.dropped == 0


def test_pads_final_block_so_frame_length_is_constant(tmp_path):
    # 故意选一个不能被 blocksize 整除的长度
    samples = _tone(seconds=0.021)
    path = tmp_path / "odd.wav"
    _write_wav(path, samples)

    bus = WavReplayBus(path, blocksize=1024, realtime=False)
    tap = bus.subscribe("probe")
    bus.start()
    bus.wait_until_finished(timeout=10)

    lengths = set()
    while True:
        frame = tap.q.get(timeout=5)
        if frame is None:
            break
        lengths.add(len(frame))
    bus.stop()

    assert lengths == {1024}


def test_fans_out_to_every_tap(tmp_path):
    path = tmp_path / "tone.wav"
    _write_wav(path, _tone(seconds=0.1))

    bus = WavReplayBus(path, blocksize=512, realtime=False)
    taps = [bus.subscribe(f"tap{i}") for i in range(3)]
    bus.start()
    bus.wait_until_finished(timeout=10)
    drained = [_drain(tap) for tap in taps]
    bus.stop()

    assert all(np.array_equal(drained[0], other) for other in drained[1:])


def test_downmixes_stereo_to_first_channel(tmp_path):
    left = _tone(seconds=0.1, freq=440.0)
    right = _tone(seconds=0.1, freq=880.0)
    interleaved = np.empty(left.size * 2, dtype=np.int16)
    interleaved[0::2] = left
    interleaved[1::2] = right
    path = tmp_path / "stereo.wav"
    _write_wav(path, interleaved, channels=2)

    bus = WavReplayBus(path, blocksize=512, realtime=False)
    tap = bus.subscribe("probe")
    bus.start()
    bus.wait_until_finished(timeout=10)
    played = _drain(tap)
    bus.stop()

    expected = left.astype(np.float32) / 32768.0
    assert np.abs(played[: len(expected)] - expected).max() == pytest.approx(0.0)


def test_rejects_non_16bit_wav(tmp_path):
    path = tmp_path / "eight.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)
        handle.setframerate(48000)
        handle.writeframes(b"\x00" * 100)

    with pytest.raises(ValueError, match="16-bit"):
        WavReplayBus(path)


def test_missing_file_is_reported_clearly(tmp_path):
    with pytest.raises(FileNotFoundError):
        WavReplayBus(tmp_path / "nope.wav")
