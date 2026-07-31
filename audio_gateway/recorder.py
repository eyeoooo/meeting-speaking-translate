"""模块 C：静默录音（WAV 落盘，会后 afconvert 转 m4a）。"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import soundfile as sf

from .bus import Tap


class Recorder:
    def __init__(self, tap: Tap, wav_path: Path, samplerate: int):
        self._tap = tap
        self.wav_path = wav_path
        self._samplerate = samplerate
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        with sf.SoundFile(
            self.wav_path, mode="w", samplerate=self._samplerate,
            channels=1, subtype="PCM_16",
        ) as f:
            for frame in self._tap.frames():
                f.write(frame)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """等写盘线程收尾（WAV 文件此后可安全读取，供批量转写用）。"""
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def finalize(self, *, to_m4a: bool = True, keep_wav: bool = True) -> Path:
        """收尾 + 可选转 m4a（macOS 自带 afconvert，无第三方编码器）。"""
        self.close()
        if not to_m4a:
            return self.wav_path
        m4a = self.wav_path.with_suffix(".m4a")
        proc = subprocess.run(
            ["afconvert", "-f", "m4af", "-d", "aac", "-b", "96000",
             str(self.wav_path), str(m4a)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(f"[recorder] afconvert 失败，保留 WAV：{proc.stderr.strip()}")
            return self.wav_path
        if not keep_wav:
            self.wav_path.unlink(missing_ok=True)
        return m4a
