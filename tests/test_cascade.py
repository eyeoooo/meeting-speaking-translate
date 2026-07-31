"""级联发言引擎规格：自建 ASR → Claude 翻译 → 克隆 TTS。

钉住的产品裁定：
- 转写会话没有 response 生成——"接话"在协议层不存在；
- 热词/术语表注入 ASR prompt 与翻译 system（translations 端点给不了的）；
- 滚动上下文只进翻译成功的句对；
- 单句翻译失败=该句静音+报错+继续，绝不整场翻车；
- 出口复用克隆声线定稿参数（ElevenLabsSpeaker，一个字不改）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

AUDIO_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUDIO_GATEWAY_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audio_gateway import bridge as bridge_module  # noqa: E402
from audio_gateway import main  # noqa: E402
from audio_gateway.bus import Tap  # noqa: E402
from audio_gateway.cascade import (  # noqa: E402
    CASCADE_TRANSCRIPTION_URL,
    DEFAULT_CASCADE_TRANSLATE_MODEL,
    CascadeSpeechSession,
    build_translation_system,
    build_translation_user,
)
from audio_gateway.interpreter import InterpreterState  # noqa: E402
from test_interpreter import (  # noqa: E402
    MockRealtimeServer,
    SpyOutputPlayer,
)
from test_voiceclone import (  # noqa: E402
    _FakeTtsResponse,
    _FakeTtsSession,
    _wait_until,
)

CLONE_PCM = b"\x10\x00\xf0\xff\x20\x00\xe0\xff"


def _transcription_script(events):
    async def on_event(server, ws, connection, event) -> None:
        if event["type"] != "session.update":
            return
        for payload in events:
            await ws.send_json(payload)

    return on_event


def _fake_translator_factory(results, calls):
    """results 按序弹出（Exception 即抛出）；calls 记录构造参数与逐句调用。"""

    def factory(api_key, model, system_text):
        calls.append({
            "api_key": api_key,
            "model": model,
            "system": system_text,
        })

        async def translate(text, context):
            calls.append((text, list(context)))
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        return translate

    return factory


class CascadeConstructionTests(unittest.TestCase):
    def test_missing_keys_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            CascadeSpeechSession(
                Tap("rehearsal"),
                SpyOutputPlayer(),
                api_key="mock-key",
                anthropic_api_key="   ",
                elevenlabs_api_key="el-key",
                voice_id="voice-1",
                translator_factory=_fake_translator_factory([], []),
            )
        with self.assertRaises(ValueError):
            CascadeSpeechSession(
                Tap("rehearsal"),
                SpyOutputPlayer(),
                api_key="mock-key",
                anthropic_api_key="ant-key",
                elevenlabs_api_key="   ",
                voice_id="voice-1",
                translator_factory=_fake_translator_factory([], []),
            )

    def test_translation_prompt_carries_rules_and_glossary(self) -> None:
        system = build_translation_system("KVM=ケーブーエム")
        self.assertIn("逐位", system)
        self.assertIn("只输出日语译文", system)
        self.assertIn("KVM=ケーブーエム", system)
        user = build_translation_user(
            [("你好。", "こんにちは。")], "第七批设备"
        )
        self.assertIn("中：你好。", user)
        self.assertIn("日：こんにちは。", user)
        self.assertTrue(user.endswith("第七批设备"))


class CascadeProtocolTests(unittest.IsolatedAsyncioTestCase):
    def _make_client(
        self,
        server,
        tts_session,
        translator_factory,
        *,
        glossary="",
        on_error=None,
    ):
        output = SpyOutputPlayer()
        state = InterpreterState(
            enabled=True,
            lang="ja",
            interpret_voice=True,
        )
        segments: list = []
        client = CascadeSpeechSession(
            Tap("rehearsal", maxsize=8, drop_oldest=True),
            output,
            api_key="mock-key-never-sent-to-openai",
            anthropic_api_key="ant-mock-key",
            elevenlabs_api_key="el-mock-key",
            voice_id="voice-clone-1",
            glossary=glossary,
            safety_identifier="hashed-test-user",
            url=server.url,
            state=state,
            on_sentence=segments.append,
            on_error=on_error,
            session_factory=server.session_factory,
            translator_factory=translator_factory,
            tts_session_factory=lambda: tts_session,
        )
        return client, output, segments

    async def test_session_update_is_transcription_only(self) -> None:
        server = MockRealtimeServer(_transcription_script([]))
        tts = _FakeTtsSession([])
        calls: list = []
        client, _, _ = self._make_client(
            server,
            tts,
            _fake_translator_factory([], calls),
            glossary="KVM=ケーブーエム",
        )

        task = client.start()
        _, update = await server.next_type("session.update")
        await client.stop()
        await task

        session = update["session"]
        # 转写专用会话：无 output、无 voice、无 instructions——
        # "接话"在协议层不存在。
        self.assertEqual("transcription", session["type"])
        self.assertNotIn("output", session["audio"])
        transcription = session["audio"]["input"]["transcription"]
        self.assertEqual("gpt-4o-transcribe", transcription["model"])
        self.assertEqual("zh", transcription["language"])
        # 热词注入口：数字纪律 + 术语表。
        self.assertIn("阿拉伯数字", transcription["prompt"])
        self.assertIn("KVM=ケーブーエム", transcription["prompt"])
        self.assertEqual(
            "server_vad",
            session["audio"]["input"]["turn_detection"]["type"],
        )
        # 翻译 system 同样带术语表，且作为构造参数只建一次（cache 前缀）。
        self.assertIn("KVM=ケーブーエム", calls[0]["system"])
        self.assertEqual(DEFAULT_CASCADE_TRANSLATE_MODEL, calls[0]["model"])
        self.assertTrue(
            CASCADE_TRANSCRIPTION_URL.endswith("intent=transcription")
        )

    async def test_utterances_flow_through_translate_and_tts(self) -> None:
        events = [
            {
                "type": "conversation.item.input_audio_transcription.delta",
                "delta": "我们下周",
            },
            {
                "type": "conversation.item.input_audio_transcription.delta",
                "delta": "交付三百台",
            },
            {
                "type": (
                    "conversation.item.input_audio_transcription.completed"
                ),
                "transcript": "我们下周交付三百台",
            },
            # 生命周期噪声事件必须安静放行
            {"type": "conversation.item.added"},
            {
                "type": (
                    "conversation.item.input_audio_transcription.completed"
                ),
                "transcript": "价格不超过百分之五",
            },
        ]
        server = MockRealtimeServer(_transcription_script(events))
        tts = _FakeTtsSession([
            _FakeTtsResponse(chunks=(CLONE_PCM,)),
            _FakeTtsResponse(chunks=(CLONE_PCM,)),
        ])
        calls: list = []
        translator = _fake_translator_factory(
            ["来週300台納品します。", "価格は5%を超えません。"],
            calls,
        )
        client, output, segments = self._make_client(server, tts, translator)

        task = client.start()
        await _wait_until(lambda: len(output.chunks) >= 2)
        await client.stop()
        await task

        # 文本链路：source 由 completed 换行冲刷成句，译文逐句发布。
        self.assertEqual(
            [
                ("source", "我们下周交付三百台"),
                ("translation", "来週300台納品します。"),
                ("translation", "価格は5%を超えません。"),
            ],
            [(s.stream, s.text) for s in segments],
        )
        # TTS 收到的就是译文成句，按句序。
        self.assertEqual(
            ["来週300台納品します。", "価格は5%を超えません。"],
            [request["json"]["text"] for request in tts.requests],
        )
        # 滚动上下文：第二句携带第一句的（中,日）对。
        sentence_calls = [c for c in calls if isinstance(c, tuple)]
        self.assertEqual(
            ("我们下周交付三百台", []),
            sentence_calls[0],
        )
        self.assertEqual(
            (
                "价格不超过百分之五",
                [("我们下周交付三百台", "来週300台納品します。")],
            ),
            sentence_calls[1],
        )

    async def test_translation_failure_mutes_sentence_and_continues(
        self,
    ) -> None:
        events = [
            {
                "type": (
                    "conversation.item.input_audio_transcription.completed"
                ),
                "transcript": "一句目",
            },
            {
                "type": (
                    "conversation.item.input_audio_transcription.completed"
                ),
                "transcript": "二句目",
            },
        ]
        server = MockRealtimeServer(_transcription_script(events))
        tts = _FakeTtsSession([_FakeTtsResponse(chunks=(CLONE_PCM,))])
        errors: list[str] = []
        calls: list = []
        translator = _fake_translator_factory(
            [RuntimeError("translator exploded"), "二文目です。"],
            calls,
        )
        client, output, segments = self._make_client(
            server, tts, translator, on_error=errors.append
        )

        task = client.start()
        await _wait_until(lambda: len(output.chunks) >= 1)
        await client.stop()
        await task

        # 失败句静音+报错；下一句照常，且失败句不进滚动上下文。
        self.assertEqual(
            ["二文目です。"],
            [request["json"]["text"] for request in tts.requests],
        )
        self.assertTrue(
            any("级联翻译失败" in message for message in errors),
            errors,
        )
        sentence_calls = [c for c in calls if isinstance(c, tuple)]
        self.assertEqual(("二句目", []), sentence_calls[1])
        self.assertIn(
            ("translation", "二文目です。"),
            [(s.stream, s.text) for s in segments],
        )


class CascadeWiringTests(unittest.TestCase):
    def test_run_bridge_fails_closed_without_credentials(self) -> None:
        # 白名单/凭据判定在一切资源分配之前（占位 cfg/dev 证明）。
        with self.assertRaises(ValueError):
            bridge_module.run_bridge(
                SimpleNamespace(),
                SimpleNamespace(),
                port=0,
                token="x",
                record=False,
                speak_engine="cascade",
            )
        with self.assertRaises(ValueError):
            bridge_module.run_bridge(
                SimpleNamespace(),
                SimpleNamespace(),
                port=0,
                token="x",
                record=False,
                speak_engine="cascade",
                elevenlabs_api_key="el-key",
                clone_voice_id="voice-1",
            )

    def test_cli_accepts_cascade_engine(self) -> None:
        args = main.build_parser().parse_args(
            ["bridge", "--speak-engine", "cascade"]
        )
        self.assertEqual("cascade", args.speak_engine)
        self.assertIsNone(args.cascade_translate_model)

    def test_cmd_bridge_fails_closed_without_anthropic_key(self) -> None:
        args = main.build_parser().parse_args([
            "bridge", "--speak", "--speak-engine", "cascade",
        ])
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "mock-key",
                "ELEVENLABS_API_KEY": "el-key",
                "ELEVENLABS_VOICE_ID": "voice-1",
            },
            clear=True,
        ):
            self.assertEqual(2, main.cmd_bridge(args))

    def test_cmd_bridge_forwards_cascade_credentials(self) -> None:
        args = main.build_parser().parse_args([
            "bridge", "--speak", "--speak-engine", "cascade",
            "--cascade-translate-model", "claude-sonnet-5",
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
                {
                    "OPENAI_API_KEY": "mock-key",
                    "ELEVENLABS_API_KEY": "el-key",
                    "ELEVENLABS_VOICE_ID": "voice-from-env",
                    "ANTHROPIC_API_KEY": "ant-key",
                },
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
        self.assertEqual("cascade", run.call_args.kwargs["speak_engine"])
        self.assertEqual(
            "ant-key",
            run.call_args.kwargs["anthropic_api_key"],
        )
        self.assertEqual(
            "el-key",
            run.call_args.kwargs["elevenlabs_api_key"],
        )
        self.assertEqual(
            "claude-sonnet-5",
            run.call_args.kwargs["cascade_translate_model"],
        )


if __name__ == "__main__":
    unittest.main()
