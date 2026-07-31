from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import numpy as np
from aiohttp import WSMsgType


AUDIO_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUDIO_GATEWAY_ROOT))

from audio_gateway import interpreter as interpreter_module  # noqa: E402
from audio_gateway import main  # noqa: E402
from audio_gateway.archive import TranscriptArchiver  # noqa: E402
from audio_gateway.bridge import (  # noqa: E402
    BridgeRuntimeState,
    UplinkPlayer,
    _STATIC,
    _state_payload,
)
from audio_gateway.bus import Tap  # noqa: E402
from audio_gateway.interpreter import (  # noqa: E402
    DEFAULT_INTERPRETER_MODEL,
    MIN_APPEND_INPUT_SAMPLES,
    InterpreterState,
    Pcm16AppendBatcher,
    RealtimeInterpreter,
    build_realtime_translation_url,
    decode_audio_24k_pcm16_to_48k_float32,
    encode_audio_48k_float32_to_24k_pcm16,
)


class SpyOutputPlayer:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.chunks: list[bytes] = []

    def start(self) -> None:
        self.started += 1

    def feed_pcm16(self, data: bytes) -> None:
        self.chunks.append(data)

    def stop(self) -> None:
        self.stopped += 1


# 一场真实会议实测 163 source : 134 translation —— 一条译文经常覆盖两三句原文。
# 默认脚本把这个形状压缩成 3:2 + 断线重连 + 译文先到，任何"按序配对"的实现都
# 会在它上面错位。conn 指定该事件属于第几次连接；close 表示服务端在此断开。
# 注意 conn=2 的 elapsed_ms 从 500 重新开始：重连即新 session，服务端时钟归零，
# 所以 elapsed_ms 只能当 epoch 内的显示标记，不能当整场会议的时间轴。
DEFAULT_EVENT_SCRIPT = """
{"conn": 1, "type": "session.input_transcript.delta", "delta": "簡単な質問をしてもらってから。", "elapsed_ms": 1200}
{"conn": 1, "type": "session.output_transcript.delta", "delta": "我觉得先请他们问个简单的问题。", "elapsed_ms": 2400}
{"conn": 1, "type": "session.input_transcript.delta", "delta": "制限しないと混乱を招いて。", "elapsed_ms": 3600}
{"conn": 1, "type": "session.input_transcript.delta", "delta": "オンラインだと100名までです。", "elapsed_ms": 4800}
{"conn": 1, "type": "session.output_transcript.delta", "delta": "不限制会造成混乱，线上最多100人。", "elapsed_ms": 6000}
{"conn": 1, "close": true}
{"conn": 2, "type": "session.output_transcript.delta", "delta": "大家觉得怎么样？", "elapsed_ms": 500}
{"conn": 2, "type": "session.input_transcript.delta", "delta": "いかがでしょうか。", "elapsed_ms": 900}
"""


def script_driver(script: str):
    """Replay an NDJSON server-event script instead of hand-written callbacks."""
    rows = [
        json.loads(line)
        for line in script.splitlines()
        if line.strip()
    ]

    async def on_event(server, ws, connection, event) -> None:
        if event["type"] == "session.close":
            await ws.send_json({"type": "session.closed"})
            return
        if event["type"] != "session.update":
            return
        for row in rows:
            if row.get("conn", 1) != connection:
                continue
            if row.get("close"):
                await ws.close()
                return
            await ws.send_json({
                key: value
                for key, value in row.items()
                if key != "conn"
            })

    return on_event


class MockRealtimeServer:
    def __init__(self, on_event=None, *, script: str | None = None) -> None:
        if on_event is None:
            on_event = script_driver(
                DEFAULT_EVENT_SCRIPT if script is None else script
            )
        self._on_event = on_event
        self.url = (
            "wss://mock.invalid/v1/realtime/translations"
            f"?model={DEFAULT_INTERPRETER_MODEL}"
        )
        self.events: list[tuple[int, dict]] = []
        self.event_queue: asyncio.Queue[tuple[int, dict]] = asyncio.Queue()
        self.headers: list[dict[str, str]] = []
        self.queries: list[dict[str, str]] = []
        self.connections = 0

    def session_factory(self):
        return _MockClientSession(self)

    async def next_type(
        self,
        event_type: str,
        *,
        timeout: float = 2.0,
    ) -> tuple[int, dict]:
        while True:
            connection, event = await asyncio.wait_for(
                self.event_queue.get(),
                timeout,
            )
            if event.get("type") == event_type:
                return connection, event

    def connect(self, url: str, headers: dict[str, str]):
        self.connections += 1
        connection = self.connections
        self.headers.append(dict(headers))
        self.queries.append({
            key: values[-1]
            for key, values in parse_qs(urlparse(url).query).items()
        })
        incoming: asyncio.Queue = asyncio.Queue()
        server_peer = _MockServerPeer(incoming)
        client_ws = _MockClientWebSocket(
            self,
            connection,
            incoming,
            server_peer,
        )
        return _MockWebSocketContext(client_ws)

    async def receive_client_event(
        self,
        peer,
        connection: int,
        event: dict,
    ) -> None:
        self.events.append((connection, event))
        await self.event_queue.put((connection, event))
        await self._on_event(self, peer, connection, event)


class _MockClientSession:
    def __init__(self, server: MockRealtimeServer) -> None:
        self._server = server

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def ws_connect(self, url: str, *, headers: dict[str, str], **kwargs):
        return self._server.connect(url, headers)


class _MockWebSocketContext:
    def __init__(self, ws) -> None:
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _MockServerPeer:
    def __init__(self, incoming: asyncio.Queue) -> None:
        self._incoming = incoming

    async def send_json(self, event: dict) -> None:
        await self._incoming.put(SimpleNamespace(
            type=WSMsgType.TEXT,
            data=json.dumps(event, ensure_ascii=False),
        ))

    async def close(self) -> None:
        await self._incoming.put(SimpleNamespace(
            type=WSMsgType.CLOSE,
            data=None,
        ))


class _MockClientWebSocket:
    def __init__(
        self,
        server: MockRealtimeServer,
        connection: int,
        incoming: asyncio.Queue,
        server_peer: _MockServerPeer,
    ) -> None:
        self._server = server
        self._connection = connection
        self._incoming = incoming
        self._server_peer = server_peer

    async def send_json(self, event: dict) -> None:
        await self._server.receive_client_event(
            self._server_peer,
            self._connection,
            event,
        )

    async def receive(self):
        return await self._incoming.get()

    def exception(self):
        return None


class AudioConversionTests(unittest.TestCase):
    def test_append_batch_is_at_least_100ms_and_pcm16_is_exact(self) -> None:
        batcher = Pcm16AppendBatcher()
        first = np.linspace(
            -0.5,
            0.5,
            MIN_APPEND_INPUT_SAMPLES - 1,
            dtype=np.float32,
        )

        self.assertEqual([], batcher.feed(first))
        chunks = batcher.feed(np.array([0.25], dtype=np.float32))

        self.assertEqual(1, len(chunks))
        decoded = np.frombuffer(chunks[0], dtype="<i2")
        expected = np.frombuffer(
            encode_audio_48k_float32_to_24k_pcm16(
                np.concatenate((first, np.array([0.25], dtype=np.float32)))
            ),
            dtype="<i2",
        )
        self.assertEqual(2_400, decoded.size)
        self.assertEqual(int(expected[0]), int(decoded[0]))
        np.testing.assert_array_equal(expected, decoded)

    def test_translated_pcm16_is_upsampled_to_48k_before_playback(self) -> None:
        source = np.array([0, 8_192, -8_192, 16_384], dtype="<i2")

        upsampled = decode_audio_24k_pcm16_to_48k_float32(
            source.tobytes()
        )

        self.assertEqual(source.size * 2, upsampled.size)
        self.assertEqual(np.float32, upsampled.dtype)

    def test_official_translation_url_shape_is_not_old_realtime_route(self) -> None:
        self.assertEqual(
            "wss://api.openai.com/v1/realtime/translations"
            "?model=gpt-realtime-translate",
            build_realtime_translation_url(DEFAULT_INTERPRETER_MODEL),
        )


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_server_covers_update_append_deltas_and_close(self) -> None:
        translated_pcm = np.array(
            [0, 4_096, -4_096, 8_192],
            dtype="<i2",
        ).tobytes()

        async def on_event(server, ws, connection, event) -> None:
            if event["type"] == "session.update":
                await ws.send_json({
                    # ASCII '.' 已按规格从终止符移除（10.5/No.3 误切），
                    # 英文句尾靠显式换行终止；strip 后发布文本不变。
                    "type": "session.input_transcript.delta",
                    "delta": "Good morning.\n",
                })
                await ws.send_json({
                    "type": "session.output_transcript.delta",
                    "delta": "早上好。",
                })
                await ws.send_json({
                    "type": "session.output_audio.delta",
                    "delta": base64.b64encode(translated_pcm).decode("ascii"),
                })
            elif event["type"] == "session.close":
                await ws.send_json({"type": "session.closed"})

        server = MockRealtimeServer(on_event)
        tap = Tap("interpreter", maxsize=8, drop_oldest=True)
        output = SpyOutputPlayer()
        state = InterpreterState(
            enabled=True,
            lang="zh",
            interpret_voice=True,
        )
        segments: list = []
        client = RealtimeInterpreter(
            tap,
            output,
            api_key="mock-key-never-sent-to-openai",
            safety_identifier="hashed-test-user",
            url=server.url,
            state=state,
            on_sentence=segments.append,
            session_factory=server.session_factory,
        )

        task = client.start()
        _, update = await server.next_type("session.update")
        # input.transcription 是 2026-07-30 真机探测的必需项：缺它服务端
        # 不会发 session.input_transcript.delta，面板"原文"会永远为空。
        self.assertEqual({
            "type": "session.update",
            "session": {
                "audio": {
                    "input": {
                        "transcription": {
                            "model": "gpt-realtime-whisper",
                        },
                    },
                    "output": {
                        "language": "zh",
                    },
                },
            },
        }, update)
        self.assertEqual(
            "Bearer mock-key-never-sent-to-openai",
            server.headers[0]["Authorization"],
        )
        self.assertEqual(
            "hashed-test-user",
            server.headers[0]["OpenAI-Safety-Identifier"],
        )
        self.assertEqual(
            DEFAULT_INTERPRETER_MODEL,
            server.queries[0]["model"],
        )

        frequency = 1_000.0
        timeline = (
            np.arange(MIN_APPEND_INPUT_SAMPLES, dtype=np.float32) / 48_000
        )
        frame = (0.25 * np.sin(
            2 * np.pi * frequency * timeline
        )).astype(np.float32)
        tap.push(frame)
        _, append = await server.next_type(
            "session.input_audio_buffer.append"
        )
        encoded = base64.b64decode(append["audio"], validate=True)
        actual_pcm = np.frombuffer(encoded, dtype="<i2")
        expected_pcm = np.frombuffer(
            encode_audio_48k_float32_to_24k_pcm16(frame),
            dtype="<i2",
        )
        self.assertEqual(2_400, actual_pcm.size)
        self.assertEqual(int(expected_pcm[0]), int(actual_pcm[0]))
        np.testing.assert_array_equal(expected_pcm, actual_pcm)

        for _ in range(100):
            snapshot = state.snapshot()
            if (
                snapshot["last_source"] == "Good morning."
                and snapshot["last_translation"] == "早上好。"
                and output.chunks
            ):
                break
            await asyncio.sleep(0.01)
        emitted = [(segment.stream, segment.text) for segment in segments]
        self.assertIn(("source", "Good morning."), emitted)
        self.assertIn(("translation", "早上好。"), emitted)
        # 该脚本不带 elapsed_ms：显示标记缺失时安静降级为 None，不是报错。
        self.assertEqual({None}, {segment.elapsed_ms for segment in segments})
        self.assertEqual([translated_pcm], output.chunks)

        await client.stop()
        await task
        _, close = await server.next_type("session.close")
        self.assertEqual({"type": "session.close"}, close)
        self.assertEqual(
            "session.close",
            server.events[-1][1]["type"],
        )
        self.assertEqual(1, output.started)
        self.assertEqual(1, output.stopped)

    async def test_disconnect_reconnects_after_two_seconds(self) -> None:
        second_connected = asyncio.Event()

        async def on_event(server, ws, connection, event) -> None:
            if event["type"] == "session.update" and connection == 1:
                await ws.close()
            elif event["type"] == "session.update":
                second_connected.set()
            elif event["type"] == "session.close":
                await ws.send_json({"type": "session.closed"})

        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            await asyncio.sleep(0)

        server = MockRealtimeServer(on_event)
        client = RealtimeInterpreter(
            Tap("interpreter", maxsize=8, drop_oldest=True),
            SpyOutputPlayer(),
            api_key="mock-key",
            safety_identifier="hashed-test-user",
            url=server.url,
            sleep=fake_sleep,
            session_factory=server.session_factory,
        )

        task = client.start()
        await asyncio.wait_for(second_connected.wait(), 2.0)
        self.assertGreaterEqual(server.connections, 2)
        self.assertEqual(2.0, delays[0])

        await client.stop()
        await task

    async def test_reconnect_backoff_is_exponential_and_capped_at_30s(
        self,
    ) -> None:
        sixth_connected = asyncio.Event()

        async def on_event(server, ws, connection, event) -> None:
            if event["type"] == "session.update" and connection <= 5:
                await ws.close()
            elif event["type"] == "session.update":
                sixth_connected.set()
            elif event["type"] == "session.close":
                await ws.send_json({"type": "session.closed"})

        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            await asyncio.sleep(0)

        server = MockRealtimeServer(on_event)
        client = RealtimeInterpreter(
            Tap("interpreter", maxsize=8, drop_oldest=True),
            SpyOutputPlayer(),
            api_key="mock-key",
            safety_identifier="hashed-test-user",
            url=server.url,
            sleep=fake_sleep,
            session_factory=server.session_factory,
        )

        task = client.start()
        await asyncio.wait_for(sixth_connected.wait(), 2.0)
        self.assertEqual([2.0, 4.0, 8.0, 16.0, 30.0], delays[:5])

        await client.stop()
        await task


class IsolationAndArchiveTests(unittest.TestCase):
    def test_voice_disabled_discards_audio_without_touching_uplink(self) -> None:
        output = SpyOutputPlayer()
        meeting_uplink = Mock(spec=UplinkPlayer)
        client = RealtimeInterpreter(
            Tap("interpreter"),
            output,
            api_key="mock-key",
            safety_identifier="hashed-test-user",
        )
        translated_pcm = np.array(
            [0, 1_000, -1_000],
            dtype="<i2",
        ).tobytes()

        client.handle_server_event({
            "type": "session.output_audio.delta",
            "delta": base64.b64encode(translated_pcm).decode("ascii"),
        })

        self.assertEqual([], output.chunks)
        self.assertEqual(0, output.started)
        meeting_uplink.feed.assert_not_called()
        source = inspect.getsource(interpreter_module)
        self.assertNotIn("UplinkPlayer", source)
        self.assertNotIn("Mac_Out", source)
        self.assertNotIn("mac_out", source)

    def test_status_payload_contains_complete_interpreter_state(self) -> None:
        interpreter_state = InterpreterState(enabled=True, lang="zh")
        interpreter_state.set_connected(True)
        interpreter_state.set_sentence("source", "Good morning.")
        interpreter_state.set_sentence("translation", "早上好。")
        app = {
            "runtime_state": BridgeRuntimeState(),
            "bus": SimpleNamespace(dropped_by_tap=lambda: {}),
            "uplink": SimpleNamespace(
                received_frames=0,
                backlog_samples=0,
                enabled=False,
            ),
            "interpreter_state": interpreter_state,
            "muted": False,
            "clients": set(),
        }

        payload = _state_payload(app)

        self.assertEqual({
            "enabled": True,
            "connected": True,
            "lang": "zh",
            "last_source": "Good morning.",
            "last_translation": "早上好。",
            "interpret_voice": False,
            "gated": False,
            "vad_dbfs": -50.0,
            "history_len": 0,
            "error": None,
        }, payload["interpreter"])

    def test_interpreter_connection_error_is_exposed_in_status_alerts(
        self,
    ) -> None:
        runtime_state = BridgeRuntimeState()
        runtime_state.set_component_alert(
            "interpreter",
            "interpreter: mock connection failure",
        )
        app = {
            "runtime_state": runtime_state,
            "bus": SimpleNamespace(dropped_by_tap=lambda: {}),
            "uplink": SimpleNamespace(
                received_frames=0,
                backlog_samples=0,
                enabled=False,
            ),
            "muted": False,
            "clients": set(),
        }

        payload = _state_payload(app)

        self.assertIn(
            "interpreter: mock connection failure",
            payload["alerts"],
        )

    def test_static_bridge_panel_is_conditional_and_renders_both_texts(
        self,
    ) -> None:
        html = _STATIC.read_text(encoding="utf-8")

        self.assertIn('id="interpreter"', html)
        self.assertIn('id="subtitleStream"', html)
        self.assertIn('id="btnVoice"', html)
        self.assertIn('id="advisor"', html)
        self.assertIn("!info.enabled", html)

    def test_realtime_archive_records_explicit_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            archiver = TranscriptArchiver(
                session_dir,
                datetime(2026, 7, 30, 9, 0, 0),
            )
            archiver.add_realtime_segment(
                stream="source",
                text="Hello.",
                t=1.25,
                lang=None,
            )
            archiver.close()

            record = json.loads(
                (session_dir / "transcript.jsonl")
                .read_text(encoding="utf-8")
                .strip()
            )

        self.assertEqual("realtime", record["source"])
        self.assertEqual("realtime", record["clock"])
        self.assertEqual(2, record["schema"])
        self.assertEqual("source", record["stream"])
        self.assertEqual("Hello.", record["text"])
        # 双泳道：realtime 行不存在 zh 字段——配对概念已从数据层移除。
        self.assertNotIn("zh", record)


class InterpreterCliTests(unittest.TestCase):
    def test_bridge_interpreter_cli_defaults_to_disabled_zh_and_model(self) -> None:
        args = main.build_parser().parse_args(["bridge"])

        self.assertFalse(args.interpret)
        self.assertEqual("zh", args.interpret_lang)
        self.assertIsNone(args.interpret_device)
        self.assertEqual(DEFAULT_INTERPRETER_MODEL, args.interpret_model)
        self.assertEqual(-50.0, args.interpret_vad_dbfs)
        self.assertFalse(args.advise)

    def test_interpret_without_api_key_fails_before_doctor_or_network(self) -> None:
        args = main.build_parser().parse_args([
            "bridge",
            "--interpret",
            "--monitor",
            "Operator Headphones",
        ])

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "audio_gateway.main._startup_doctor",
                return_value=0,
            ) as doctor,
        ):
            code = main.cmd_bridge(args)

        self.assertEqual(2, code)
        doctor.assert_not_called()

    def test_interpreter_device_may_not_resolve_to_meeting_uplink(self) -> None:
        args = main.build_parser().parse_args([
            "bridge",
            "--interpret",
            "--monitor",
            "Unsafe shared output",
        ])
        resolved = SimpleNamespace(
            mac_in=1,
            mac_out=7,
            monitor=7,
            mic=None,
            monitor_note=None,
        )

        with (
            patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "mock-key"},
                clear=True,
            ),
            patch(
                "audio_gateway.main._startup_doctor",
                return_value=0,
            ),
            patch(
                "audio_gateway.devices.resolve",
                return_value=resolved,
            ),
            patch("audio_gateway.bridge.run_bridge") as run_bridge,
        ):
            code = main.cmd_bridge(args)

        self.assertEqual(2, code)
        run_bridge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
