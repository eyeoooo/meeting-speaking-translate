"""模块 B：字幕输出——控制台日志流，或 Tkinter 置顶半透明浮窗。

Tk 在 macOS 上必须跑主线程：TkSubtitleWindow.run_mainloop() 由 main.py
在主线程调用，工作线程只往队列里投递。
"""

from __future__ import annotations

import queue
import time


class ConsoleSubtitleSink:
    def post(self, t0: float, lang: str, text: str, translated: str | None) -> None:
        stamp = time.strftime("%H:%M:%S", time.gmtime(t0))
        print(f"[{stamp}] ({lang or '?'}) {text}")
        if translated:
            print(f"          译: {translated}")

    def run_mainloop(self, on_key=None) -> None:  # 与 Tk 窗口同接口；控制台无事可做
        pass

    def close(self) -> None:
        pass


class TkSubtitleWindow:
    """置顶、无边框、半透明字幕窗。窗口聚焦时按 m=静音切换、q=结束会议。"""

    def __init__(self, width: int = 900, height: int = 120):
        import tkinter as tk
        self._tk = tk
        self._q: queue.Queue = queue.Queue()
        self._closed = False

        root = tk.Tk()
        root.title("字幕")
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.82)
        root.overrideredirect(True)
        sw = root.winfo_screenwidth()
        root.geometry(f"{width}x{height}+{(sw - width) // 2}+60")
        root.configure(bg="black")

        self._orig = tk.Label(root, text="", fg="#cccccc", bg="black",
                              font=("Helvetica", 15), wraplength=width - 40)
        self._orig.pack(pady=(12, 0))
        self._zh = tk.Label(root, text="等待发言…", fg="#ffd75f", bg="black",
                            font=("Helvetica", 21, "bold"), wraplength=width - 40)
        self._zh.pack(pady=(4, 12))
        self._root = root

    def post(self, t0: float, lang: str, text: str, translated: str | None) -> None:
        self._q.put((text, translated))

    def _tick(self, on_key) -> None:
        try:
            while True:
                text, translated = self._q.get_nowait()
                self._orig.config(text=text)
                self._zh.config(text=translated or text)
        except queue.Empty:
            pass
        if not self._closed:
            self._root.after(150, self._tick, on_key)

    def run_mainloop(self, on_key=None) -> None:
        if on_key:
            self._root.bind("<Key>", lambda e: on_key(e.char))
        self._root.after(150, self._tick, on_key)
        self._root.mainloop()

    def close(self) -> None:
        self._closed = True
        try:
            self._root.after(0, self._root.destroy)
        except Exception:
            pass
