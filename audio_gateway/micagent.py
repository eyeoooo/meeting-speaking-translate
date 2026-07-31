"""mic-agent：跑在操作席（MacBook）的发言上行客户端。

  采集本机默认麦克风 → int16 PCM/20ms → WS → Mac mini 网关 → Mac_Out → 会议
  可选 --listen：同时把会议声音播到本机（替代 UU 声音同步；务必戴耳机防回声）。
  静音是网关侧权威状态：本端 m+回车 只是发切换请求，任何界面按的静音都即时生效。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading

import numpy as np
import sounddevice as sd
from aiohttp import ClientSession, WSMsgType

FRAME = 960  # 20ms @ 48k


class _KeyForwarder:
    """进程级唯一 stdin reader；WS 重连只替换当前发送目标。"""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        muted: dict[str, bool],
        *,
        input_stream=None,
        output=print,
    ):
        self._loop = loop
        self._muted = muted
        self._input_stream = input_stream
        self._output = output
        self._lock = threading.Lock()
        self._ws = None
        self._thread: threading.Thread | None = None

    def bind(self, ws) -> None:
        with self._lock:
            self._ws = ws

    def unbind(self, ws) -> None:
        with self._lock:
            if self._ws is ws:
                self._ws = None

    async def send_toggle(self) -> bool:
        with self._lock:
            ws = self._ws
        if ws is None or getattr(ws, "closed", False):
            self._output("[agent] 静音请求未发送：当前未连接")
            return False
        await ws.send_json(
            {"type": "mute", "muted": not self._muted["v"]}
        )
        return True

    def _run(self) -> None:
        source = self._input_stream or sys.stdin
        for line in source:
            if not line.strip().lower().startswith("m"):
                continue
            coro = self.send_toggle()
            try:
                future = asyncio.run_coroutine_threadsafe(
                    coro, self._loop
                )
            except RuntimeError:
                coro.close()
                return

            def report_error(done) -> None:
                try:
                    done.result()
                except Exception as exc:
                    self._output(f"[agent] 静音请求发送失败: {exc}")

            future.add_done_callback(report_error)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="agent-keys",
            daemon=True,
        )
        self._thread.start()


def _pick_input(keyword: str | None) -> int | None:
    if not keyword:
        return None  # None = 系统默认输入
    kw = keyword.lower()
    for idx, d in enumerate(sd.query_devices()):
        if kw in d["name"].lower() and d["max_input_channels"] > 0:
            return idx
    raise RuntimeError(f"未找到匹配 {keyword!r} 的输入设备")


async def run_micagent(url: str, token: str, *, listen: bool, mic_keyword: str | None,
                       test_tone: bool = False) -> int:
    ws_url = url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://") + f"/ws?t={token}"
    mic_dev = _pick_input(mic_keyword)
    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue = asyncio.Queue(maxsize=64)   # 采集回调 → 发送协程
    muted = {"v": False}
    key_forwarder = _KeyForwarder(loop, muted)
    key_forwarder.start()
    last_state = None

    def cb(indata, frames, time_info, status):
        data = (np.clip(indata[:, 0], -1.0, 1.0) * 32767).astype(np.int16).tobytes()

        def _put(d: bytes = data) -> None:
            if out_q.full():
                try:
                    out_q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            out_q.put_nowait(d)

        loop.call_soon_threadsafe(_put)

    player = None
    if listen:
        from .bridge import UplinkPlayer  # 同一个"缓冲→输出"实现，播到本机默认输出
        player = UplinkPlayer(device=sd.default.device[1], samplerate=48000, blocksize=FRAME)
        player.start()
        print("[agent] --listen 开启：会议声音将从本机播放。请务必戴耳机，避免回声进会议。")

    stream = None
    if not test_tone:
        # 首次运行会触发本机麦克风授权弹窗（Terminal 名下）
        stream = sd.InputStream(device=mic_dev, channels=1, samplerate=48000,
                                blocksize=FRAME, dtype="float32", latency="low", callback=cb)

    async def tone_gen() -> None:
        """--test-tone 诊断：440Hz 正弦替代麦克风，验证传输层。"""
        t0 = 0
        while True:
            t = (np.arange(FRAME) + t0) / 48000.0
            t0 += FRAME
            data = (0.25 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16).tobytes()
            if out_q.full():
                try:
                    out_q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            out_q.put_nowait(data)
            await asyncio.sleep(0.02)

    print(f"[agent] 连接 {ws_url.split('?')[0]} …（m+回车=静音切换，Ctrl+C 退出）")
    backoff = 2
    while True:
        try:
            async with ClientSession() as session:
                async with session.ws_connect(ws_url, heartbeat=20) as ws:
                    print("[agent] ✅ 已连接，" + ("测试音推流中" if test_tone else "麦克风推流中"))
                    backoff = 2
                    key_forwarder.bind(ws)
                    tone_task = asyncio.create_task(tone_gen()) if test_tone else None
                    if stream:
                        stream.start()

                    async def sender() -> None:
                        while True:
                            data = await out_q.get()
                            await ws.send_bytes(data)

                    send_task = asyncio.create_task(sender())
                    try:
                        async for msg in ws:
                            if msg.type == WSMsgType.BINARY:
                                if player:
                                    player.feed(msg.data)
                            elif msg.type == WSMsgType.TEXT:
                                st = json.loads(msg.data)
                                if st.get("type") == "state":
                                    muted["v"] = bool(st.get("muted"))
                                    signature = (
                                        muted["v"],
                                        st.get("clients"),
                                        st.get("link"),
                                        st.get("reason"),
                                        tuple(st.get("alerts") or ()),
                                    )
                                    if signature != last_state:
                                        last_state = signature
                                        print(
                                            f"[agent] 状态: "
                                            f"{'🔇 已静音' if muted['v'] else '🎙 发言开启'}"
                                            f"（在线客户端 {st.get('clients')}，"
                                            f"link={st.get('link', 'unknown')}）"
                                        )
                                        alerts = st.get("alerts") or []
                                        if (
                                            st.get("link") != "up"
                                            or alerts
                                        ):
                                            detail = "; ".join(
                                                [
                                                    str(
                                                        st.get("reason")
                                                        or "unknown"
                                                    ),
                                                    *map(str, alerts),
                                                ]
                                            )
                                            print(
                                                "[agent][ALERT] 网关音频异常: "
                                                f"{detail}"
                                            )
                    finally:
                        key_forwarder.unbind(ws)
                        send_task.cancel()
                        if tone_task:
                            tone_task.cancel()
                        if stream:
                            stream.stop()
        except (OSError, asyncio.TimeoutError) as e:
            print(f"[agent] 连接断开（{e}），{backoff}s 后重连…")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except KeyboardInterrupt:
            break
    if stream:
        stream.close()
    if player:
        player.stop()
    return 0
