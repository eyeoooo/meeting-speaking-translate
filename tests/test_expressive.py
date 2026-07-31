from __future__ import annotations

import asyncio
import base64
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

AUDIO_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUDIO_GATEWAY_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audio_gateway import bridge as bridge_module  # noqa: E402
from audio_gateway import main  # noqa: E402
from audio_gateway.bus import Tap  # noqa: E402
from audio_gateway.expressive import (  # noqa: E402
    DEFAULT_EXPRESSIVE_MODEL,
    DEFAULT_EXPRESSIVE_VOICE,
    ExpressiveSpeechSession,
    build_interpreter_instructions,
    build_realtime_ga_url,
)
from audio_gateway.interpreter import (  # noqa: E402
    MIN_APPEND_INPUT_SAMPLES,
    InterpreterState,
    encode_audio_48k_float32_to_24k_pcm16,
)
from test_interpreter import (  # noqa: E402
    MockRealtimeServer,
    SpyOutputPlayer,
)


class UrlAndInstructionTests(unittest.TestCase):
    def test_ga_url_is_general_realtime_not_translations(self) -> None:
        self.assertEqual(
            "wss://api.openai.com/v1/realtime?model=gpt-realtime",
            build_realtime_ga_url(DEFAULT_EXPRESSIVE_MODEL),
        )

    def test_instructions_forbid_answering_even_question_shaped_input(
        self,
    ) -> None:
        # 正确性头号威胁：通用模型把发言当提问来回答。指令必须显式覆盖
        # "听起来像对你说的问题也要翻译而不是回答"。
        instructions = build_interpreter_instructions("zh", "ja")
        self.assertIn("Chinese", instructions)
        self.assertIn("Japanese", instructions)
        self.assertIn("do NOT respond", instructions)
        self.assertIn("translation instead", instructions)
        self.assertIn("NOT a participant", instructions)

    def test_empty_voice_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ExpressiveSpeechSession(
                Tap("rehearsal"),
                SpyOutputPlayer(),
                api_key="mock-key",
                voice="   ",
            )


class ExpressiveProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_ga_handshake_events_and_close_without_session_close(
        self,
    ) -> None:
        translated_pcm = np.array(
            [0, 4_096, -4_096, 8_192],
            dtype="<i2",
        ).tobytes()

        async def on_event(server, ws, connection, event) -> None:
            if event["type"] != "session.update":
                return
            # 陷阱语料：问句形态的发言。正确行为=译文照发（模拟服务端
            # 已被指令约束住），错误行为在真机 A/B 里以"加戏"红旗捕捉。
            await ws.send_json({
                "type": "conversation.item.input_audio_transcription.delta",
                "delta": "这个方案你觉得怎么样？",
                "item_id": "item_1",
            })
            await ws.send_json({
                "type": (
                    "conversation.item.input_audio_transcription.completed"
                ),
                "transcript": "这个方案你觉得怎么样？",
                "item_id": "item_1",
            })
            await ws.send_json({
                "type": "response.output_audio_transcript.delta",
                "delta": "この案についてどう思いますか？",
            })
            await ws.send_json({
                "type": "response.output_audio_transcript.done",
                "transcript": "この案についてどう思いますか？",
            })
            await ws.send_json({
                "type": "response.output_audio.delta",
                "delta": base64.b64encode(translated_pcm).decode("ascii"),
            })
            # GA 的生命周期噪声事件必须被安静放行，不得炸链路。
            await ws.send_json({"type": "response.done", "response": {}})
            await ws.send_json({"type": "rate_limits.updated"})

        server = MockRealtimeServer(on_event)
        tap = Tap("rehearsal", maxsize=8, drop_oldest=True)
        output = SpyOutputPlayer()
        state = InterpreterState(
            enabled=True,
            lang="ja",
            interpret_voice=True,
        )
        segments: list = []
        client = ExpressiveSpeechSession(
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
        self.assertEqual({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "output_modalities": ["audio"],
                "instructions": client.instructions,
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": {
                            "model": "gpt-4o-transcribe",
                            "language": "zh",
                        },
                        "turn_detection": {
                            "type": "server_vad",
                            "silence_duration_ms": 600,
                            "create_response": True,
                            # 真机探针证据：True 会掐断在途译文（半句）。
                            "interrupt_response": False,
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": DEFAULT_EXPRESSIVE_VOICE,
                    },
                },
            },
        }, update)
        self.assertEqual(
            "Bearer mock-key-never-sent-to-openai",
            server.headers[0]["Authorization"],
        )

        frame = (0.25 * np.sin(
            2 * np.pi * 1_000.0
            * np.arange(MIN_APPEND_INPUT_SAMPLES, dtype=np.float32)
            / 48_000
        )).astype(np.float32)
        tap.push(frame)
        # GA 客户端事件名没有 session. 前缀——发成 translations 的名字
        # 服务端只会报 unknown event。
        _, append = await server.next_type("input_audio_buffer.append")
        np.testing.assert_array_equal(
            np.frombuffer(
                encode_audio_48k_float32_to_24k_pcm16(frame),
                dtype="<i2",
            ),
            np.frombuffer(
                base64.b64decode(append["audio"], validate=True),
                dtype="<i2",
            ),
        )

        for _ in range(100):
            if output.chunks and len(segments) >= 2:
                break
            await asyncio.sleep(0.01)
        emitted = [(segment.stream, segment.text) for segment in segments]
        self.assertIn(("source", "这个方案你觉得怎么样？"), emitted)
        self.assertIn(
            ("translation", "この案についてどう思いますか？"),
            emitted,
        )
        # GA 事件不带 elapsed_ms：显示标记恒 None，沿用父类降级语义。
        self.assertEqual({None}, {segment.elapsed_ms for segment in segments})
        self.assertEqual([translated_pcm], output.chunks)

        await client.stop()
        await task
        # GA 端点没有 session.close 握手；发了它服务端只会报错。
        sent_types = {event["type"] for _, event in server.events}
        self.assertNotIn("session.close", sent_types)
        self.assertEqual(1, output.started)
        self.assertEqual(1, output.stopped)

    async def test_done_events_flush_pending_partial_sentences(self) -> None:
        async def on_event(server, ws, connection, event) -> None:
            if event["type"] != "session.update":
                return
            # 无终止符的尾巴必须由 .done/.completed 冲出来，而不是等
            # 断线冲刷才可见；done 携带的全文与 delta 重复，绝不再 feed。
            await ws.send_json({
                "type": "response.output_audio_transcript.delta",
                "delta": "納品計画を確認します",
            })
            await ws.send_json({
                "type": "response.output_audio_transcript.done",
                "transcript": "納品計画を確認します",
            })
            await ws.send_json({
                "type": "conversation.item.input_audio_transcription.delta",
                "delta": "确认交付计划",
            })
            await ws.send_json({
                "type": (
                    "conversation.item.input_audio_transcription.completed"
                ),
                "transcript": "确认交付计划",
            })

        server = MockRealtimeServer(on_event)
        segments: list = []
        client = ExpressiveSpeechSession(
            Tap("rehearsal", maxsize=8, drop_oldest=True),
            SpyOutputPlayer(),
            api_key="mock-key",
            safety_identifier="hashed-test-user",
            url=server.url,
            on_sentence=segments.append,
            session_factory=server.session_factory,
        )

        task = client.start()
        for _ in range(100):
            if len(segments) >= 2:
                break
            await asyncio.sleep(0.01)
        await client.stop()
        await task

        emitted = [(segment.stream, segment.text) for segment in segments]
        self.assertEqual([
            ("translation", "納品計画を確認します"),
            ("source", "确认交付计划"),
        ], emitted[:2])
        # 全文只发布一次：done 重复喂全文会翻倍成两条。
        self.assertEqual(2, len([
            item for item in emitted
            if item[1] in ("納品計画を確認します", "确认交付计划")
        ]))

    async def test_disconnect_reconnects_and_increments_epoch(self) -> None:
        second_connected = asyncio.Event()

        async def on_event(server, ws, connection, event) -> None:
            if event["type"] != "session.update":
                return
            if connection == 1:
                await ws.send_json({
                    "type": "response.output_audio_transcript.delta",
                    "delta": "一回目。",
                })
                await ws.close()
            else:
                await ws.send_json({
                    "type": "response.output_audio_transcript.delta",
                    "delta": "二回目。",
                })
                second_connected.set()

        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            await asyncio.sleep(0)

        server = MockRealtimeServer(on_event)
        segments: list = []
        client = ExpressiveSpeechSession(
            Tap("rehearsal", maxsize=8, drop_oldest=True),
            SpyOutputPlayer(),
            api_key="mock-key",
            safety_identifier="hashed-test-user",
            url=server.url,
            on_sentence=segments.append,
            sleep=fake_sleep,
            session_factory=server.session_factory,
        )

        task = client.start()
        await asyncio.wait_for(second_connected.wait(), 2.0)
        for _ in range(100):
            if len(segments) >= 2:
                break
            await asyncio.sleep(0.01)
        await client.stop()
        await task

        self.assertGreaterEqual(server.connections, 2)
        self.assertEqual(2.0, delays[0])
        by_text = {segment.text: segment.epoch for segment in segments}
        # 重连即新 session：epoch 硬切断跨 session 的配对想象（父类语义）。
        self.assertEqual(0, by_text["一回目。"])
        self.assertEqual(1, by_text["二回目。"])


class EventHandlingTests(unittest.TestCase):
    def _client(self, **kwargs) -> ExpressiveSpeechSession:
        return ExpressiveSpeechSession(
            Tap("rehearsal"),
            kwargs.pop("output", SpyOutputPlayer()),
            api_key="mock-key",
            safety_identifier="hashed-test-user",
            **kwargs,
        )

    def test_error_event_raises(self) -> None:
        client = self._client()
        with self.assertRaises(RuntimeError):
            client.handle_server_event({
                "type": "error",
                "error": {"message": "boom"},
            })

    def test_unknown_lifecycle_events_pass_silently(self) -> None:
        client = self._client()
        for event_type in (
            "session.created",
            "session.updated",
            "response.created",
            "response.content_part.added",
            "conversation.item.added",
            "input_audio_buffer.speech_started",
            "rate_limits.updated",
        ):
            self.assertFalse(
                client.handle_server_event({"type": event_type})
            )

    def test_voice_disabled_discards_audio_before_player(self) -> None:
        output = SpyOutputPlayer()
        client = self._client(output=output)
        client.handle_server_event({
            "type": "response.output_audio.delta",
            "delta": base64.b64encode(
                np.array([0, 1_000], dtype="<i2").tobytes()
            ).decode("ascii"),
        })
        self.assertEqual([], output.chunks)
        self.assertEqual(0, output.started)


class SpeakEngineWiringTests(unittest.TestCase):
    def test_cli_defaults_to_translate_engine(self) -> None:
        args = main.build_parser().parse_args(["bridge"])
        self.assertEqual("translate", args.speak_engine)

    def test_cli_accepts_expressive_and_rejects_unknown(self) -> None:
        args = main.build_parser().parse_args(
            ["bridge", "--speak-engine", "expressive"]
        )
        self.assertEqual("expressive", args.speak_engine)
        with self.assertRaises(SystemExit):
            main.build_parser().parse_args(
                ["bridge", "--speak-engine", "vivid"]
            )

    def test_run_bridge_fails_closed_on_unknown_engine(self) -> None:
        # 白名单判定必须在任何资源分配之前：静默回退=用户以为在 A/B
        # 其实在 A/A。cfg/dev 传占位对象即可证明先于设备使用而炸。
        with self.assertRaises(ValueError):
            bridge_module.run_bridge(
                SimpleNamespace(),
                SimpleNamespace(),
                port=0,
                token="x",
                record=False,
                speak_engine="vivid",
            )

    def test_cmd_bridge_forwards_engine_choice(self) -> None:
        args = main.build_parser().parse_args([
            "bridge",
            "--speak",
            "--speak-engine",
            "expressive",
        ])
        resolved = SimpleNamespace(
            mac_in=1,
            mac_out=7,
            monitor=None,
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
            patch("audio_gateway.bridge.run_bridge", return_value=0) as run,
        ):
            code = main.cmd_bridge(args)

        self.assertEqual(0, code)
        self.assertEqual(
            "expressive",
            run.call_args.kwargs["speak_engine"],
        )

    def test_cmd_bridge_default_engine_is_translate(self) -> None:
        args = main.build_parser().parse_args(["bridge"])
        resolved = SimpleNamespace(
            mac_in=1,
            mac_out=7,
            monitor=None,
            mic=None,
            monitor_note=None,
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "audio_gateway.main._startup_doctor",
                return_value=0,
            ),
            patch(
                "audio_gateway.devices.resolve",
                return_value=resolved,
            ),
            patch("audio_gateway.bridge.run_bridge", return_value=0) as run,
        ):
            code = main.cmd_bridge(args)

        self.assertEqual(0, code)
        self.assertEqual(
            "translate",
            run.call_args.kwargs["speak_engine"],
        )


if __name__ == "__main__":
    unittest.main()
