"""M3 声纹克隆引擎规格：translate 文本链路原样 + ElevenLabs 音频出口。

钉住的产品裁定：
- clone 引擎绝不改动文本链路（translations 端点、断句、面板/落盘）；
- 端点内置声线的 audio delta 必须被丢弃（两路都播=重影）；
- 单句 TTS 失败=该句静音+报错+继续，绝不整场翻车；
- 语音关闭时不合成（不出声也不计费）。
"""

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
from audio_gateway.interpreter import InterpreterState  # noqa: E402
from audio_gateway.voiceclone import (  # noqa: E402
    CLONE_OUTPUT_FORMAT,
    DEFAULT_CLONE_MODEL,
    CloneSpeechSession,
)
from test_interpreter import (  # noqa: E402
    MockRealtimeServer,
    SpyOutputPlayer,
)


class _FakeTtsResponse:
    """最小 aiohttp 响应面：status / text() / content.iter_chunked()。"""

    def __init__(
        self,
        status: int = 200,
        chunks: tuple[bytes, ...] = (),
        detail: str = "",
    ) -> None:
        self.status = status
        self._chunks = list(chunks)
        self._detail = detail
        self.content = self

    async def __aenter__(self) -> "_FakeTtsResponse":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def text(self) -> str:
        return self._detail

    async def iter_chunked(self, size: int):
        for chunk in self._chunks:
            yield chunk


class _FakeTtsSession:
    """按脚本顺序发响应的假 ElevenLabs 会话，记录每次请求全参。"""

    def __init__(self, script: list[_FakeTtsResponse]) -> None:
        self.requests: list[dict] = []
        self._script = list(script)
        self.closed = False

    async def __aenter__(self) -> "_FakeTtsSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    def post(self, url, *, params=None, headers=None, json=None):
        self.requests.append({
            "url": url,
            "params": params,
            "headers": headers,
            "json": json,
        })
        return self._script.pop(0) if self._script else _FakeTtsResponse()


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.01)


BUILTIN_PCM = np.array([0, 4_096, -4_096, 8_192], dtype="<i2").tobytes()

CLONE_PCM_A = np.array([100, -100, 200, -200], dtype="<i2").tobytes()
CLONE_PCM_B = np.array([300, -300], dtype="<i2").tobytes()


def _translations_script(ws_events):
    async def on_event(server, ws, connection, event) -> None:
        if event["type"] == "session.close":
            await ws.send_json({"type": "session.closed"})
            return
        if event["type"] != "session.update":
            return
        for payload in ws_events:
            await ws.send_json(payload)

    return on_event


class CloneConstructionTests(unittest.TestCase):
    def test_missing_key_or_voice_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            CloneSpeechSession(
                Tap("rehearsal"),
                SpyOutputPlayer(),
                api_key="mock-key",
                elevenlabs_api_key="   ",
                voice_id="voice-1",
            )
        with self.assertRaises(ValueError):
            CloneSpeechSession(
                Tap("rehearsal"),
                SpyOutputPlayer(),
                api_key="mock-key",
                elevenlabs_api_key="el-key",
                voice_id="  ",
            )

    def test_speed_out_of_range_fails_closed(self) -> None:
        # ElevenLabs 只接受 0.7–1.2；越界在构造时炸，不留到会中才发现。
        for bad in (0.5, 1.5):
            with self.assertRaises(ValueError):
                CloneSpeechSession(
                    Tap("rehearsal"),
                    SpyOutputPlayer(),
                    api_key="mock-key",
                    elevenlabs_api_key="el-key",
                    voice_id="voice-1",
                    clone_speed=bad,
                )


class CloneProtocolTests(unittest.IsolatedAsyncioTestCase):
    def _make_client(
        self, server, tts_session, *, interpret_voice=True, **clone_kwargs
    ):
        output = SpyOutputPlayer()
        state = InterpreterState(
            enabled=True,
            lang="ja",
            interpret_voice=interpret_voice,
        )
        segments: list = []
        client = CloneSpeechSession(
            Tap("rehearsal", maxsize=8, drop_oldest=True),
            output,
            api_key="mock-key-never-sent-to-openai",
            elevenlabs_api_key="el-mock-key",
            voice_id="voice-clone-1",
            safety_identifier="hashed-test-user",
            url=server.url,
            state=state,
            on_sentence=segments.append,
            session_factory=server.session_factory,
            tts_session_factory=lambda: tts_session,
            **clone_kwargs,
        )
        return client, output, segments

    async def test_translation_sentences_drive_clone_and_builtin_is_dropped(
        self,
    ) -> None:
        events = [
            # source 成句：进面板，绝不进 TTS。
            {
                "type": "session.input_transcript.delta",
                "delta": "我们下周交付。",
                "elapsed_ms": 100,
            },
            {
                "type": "session.output_transcript.delta",
                "delta": "来週納品します。",
                "elapsed_ms": 200,
            },
            # 端点内置声线：必须被丢弃（克隆声线是唯一音频出口）。
            {
                "type": "session.output_audio.delta",
                "delta": base64.b64encode(BUILTIN_PCM).decode("ascii"),
            },
            {
                "type": "session.output_transcript.delta",
                "delta": "よろしくお願いします。",
                "elapsed_ms": 300,
            },
        ]
        server = MockRealtimeServer(_translations_script(events))
        tts = _FakeTtsSession([
            _FakeTtsResponse(chunks=(CLONE_PCM_A,)),
            _FakeTtsResponse(chunks=(CLONE_PCM_B,)),
        ])
        client, output, segments = self._make_client(server, tts)

        task = client.start()
        await _wait_until(lambda: len(output.chunks) >= 2)
        await client.stop()
        await task

        # 播放器收到的只有克隆声线字节，端点内置 PCM 一个字节都不许出现。
        self.assertEqual([CLONE_PCM_A, CLONE_PCM_B], output.chunks)
        self.assertNotIn(BUILTIN_PCM, output.chunks)
        # 两句译文各一次请求、按句序；source 句零请求。
        self.assertEqual(2, len(tts.requests))
        self.assertEqual(
            ["来週納品します。", "よろしくお願いします。"],
            [request["json"]["text"] for request in tts.requests],
        )
        for request in tts.requests:
            self.assertIn("voice-clone-1", request["url"])
            self.assertEqual(
                {"output_format": CLONE_OUTPUT_FORMAT},
                request["params"],
            )
            self.assertEqual("el-mock-key", request["headers"]["xi-api-key"])
            self.assertEqual(
                DEFAULT_CLONE_MODEL,
                request["json"]["model_id"],
            )
            # 未指定语速时绝不发 voice_settings：用声线自带默认语速。
            self.assertNotIn("voice_settings", request["json"])
        # 文本链路原样：source/translation 两条流都正常发布。
        self.assertEqual(
            [
                ("source", "我们下周交付。"),
                ("translation", "来週納品します。"),
                ("translation", "よろしくお願いします。"),
            ],
            [(s.stream, s.text) for s in segments],
        )

    async def test_odd_sized_chunks_are_fed_as_even_bytes(self) -> None:
        events = [
            {
                "type": "session.output_transcript.delta",
                "delta": "はい。",
                "elapsed_ms": 100,
            },
        ]
        server = MockRealtimeServer(_translations_script(events))
        # 5 字节拆成 3+2：feed_pcm16 契约要求偶数长度，实现必须攒尾字节。
        tts = _FakeTtsSession([
            _FakeTtsResponse(chunks=(b"\x01\x02\x03", b"\x04\x05")),
        ])
        client, output, _ = self._make_client(server, tts)

        task = client.start()
        await _wait_until(lambda: len(output.chunks) >= 2)
        await client.stop()
        await task

        self.assertEqual([b"\x01\x02", b"\x03\x04"], output.chunks)
        for chunk in output.chunks:
            self.assertEqual(0, len(chunk) % 2)

    async def test_model_and_speed_reach_request_body(self) -> None:
        events = [
            {
                "type": "session.output_transcript.delta",
                "delta": "はい。",
                "elapsed_ms": 100,
            },
        ]
        server = MockRealtimeServer(_translations_script(events))
        tts = _FakeTtsSession([_FakeTtsResponse(chunks=(CLONE_PCM_A,))])
        client, output, _ = self._make_client(
            server,
            tts,
            clone_model="eleven_turbo_v2_5",
            clone_speed=1.1,
        )

        task = client.start()
        await _wait_until(lambda: len(output.chunks) >= 1)
        await client.stop()
        await task

        request = tts.requests[0]
        self.assertEqual("eleven_turbo_v2_5", request["json"]["model_id"])
        self.assertEqual(
            {"speed": 1.1},
            request["json"]["voice_settings"],
        )

    async def test_tts_failure_mutes_sentence_and_continues(self) -> None:
        events = [
            {
                "type": "session.output_transcript.delta",
                "delta": "一句目です。",
                "elapsed_ms": 100,
            },
            {
                "type": "session.output_transcript.delta",
                "delta": "二句目です。",
                "elapsed_ms": 200,
            },
        ]
        server = MockRealtimeServer(_translations_script(events))
        tts = _FakeTtsSession([
            _FakeTtsResponse(status=500, detail="server exploded"),
            _FakeTtsResponse(chunks=(CLONE_PCM_A,)),
        ])
        errors: list[str] = []
        output = SpyOutputPlayer()
        state = InterpreterState(
            enabled=True,
            lang="ja",
            interpret_voice=True,
        )
        client = CloneSpeechSession(
            Tap("rehearsal", maxsize=8, drop_oldest=True),
            output,
            api_key="mock-key-never-sent-to-openai",
            elevenlabs_api_key="el-mock-key",
            voice_id="voice-clone-1",
            safety_identifier="hashed-test-user",
            url=server.url,
            state=state,
            on_error=errors.append,
            session_factory=server.session_factory,
            tts_session_factory=lambda: tts,
        )

        task = client.start()
        await _wait_until(lambda: len(output.chunks) >= 1)
        await client.stop()
        await task

        # 失败句静音、报错；下一句照常合成——单句抖动绝不整场翻车。
        self.assertEqual([CLONE_PCM_A], output.chunks)
        self.assertEqual(2, len(tts.requests))
        self.assertTrue(
            any("克隆语音合成失败" in message for message in errors),
            errors,
        )

    async def test_voice_disabled_synthesizes_nothing(self) -> None:
        events = [
            {
                "type": "session.output_transcript.delta",
                "delta": "静かな一句。",
                "elapsed_ms": 100,
            },
        ]
        server = MockRealtimeServer(_translations_script(events))
        tts = _FakeTtsSession([])
        client, output, segments = self._make_client(
            server, tts, interpret_voice=False
        )

        task = client.start()
        # 文本照常发布——语音开关只管声音，不管字幕。
        await _wait_until(lambda: len(segments) >= 1)
        await asyncio.sleep(0.05)
        await client.stop()
        await task

        self.assertEqual([], tts.requests)
        self.assertEqual([], output.chunks)


class CloneWiringTests(unittest.TestCase):
    def test_run_bridge_fails_closed_without_key_or_voice(self) -> None:
        # 白名单/凭据判定必须在任何资源分配之前（占位 cfg/dev 证明）。
        with self.assertRaises(ValueError):
            bridge_module.run_bridge(
                SimpleNamespace(),
                SimpleNamespace(),
                port=0,
                token="x",
                record=False,
                speak_engine="clone",
            )
        with self.assertRaises(ValueError):
            bridge_module.run_bridge(
                SimpleNamespace(),
                SimpleNamespace(),
                port=0,
                token="x",
                record=False,
                speak_engine="clone",
                elevenlabs_api_key="el-key",
                clone_voice_id="   ",
            )

    def test_cli_accepts_clone_engine(self) -> None:
        args = main.build_parser().parse_args(
            ["bridge", "--speak-engine", "clone"]
        )
        self.assertEqual("clone", args.speak_engine)

    def _resolved_devices(self) -> SimpleNamespace:
        return SimpleNamespace(
            mac_in=1,
            mac_out=7,
            monitor=None,
            mic=None,
            monitor_note=None,
        )

    def test_cmd_bridge_fails_closed_without_elevenlabs_key(self) -> None:
        args = main.build_parser().parse_args([
            "bridge", "--speak", "--speak-engine", "clone",
        ])
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "mock-key"},
            clear=True,
        ):
            self.assertEqual(2, main.cmd_bridge(args))

    def test_cmd_bridge_fails_closed_without_voice_id(self) -> None:
        args = main.build_parser().parse_args([
            "bridge", "--speak", "--speak-engine", "clone",
        ])
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "mock-key",
                "ELEVENLABS_API_KEY": "el-key",
            },
            clear=True,
        ):
            self.assertEqual(2, main.cmd_bridge(args))

    def test_cmd_bridge_forwards_clone_credentials(self) -> None:
        args = main.build_parser().parse_args([
            "bridge", "--speak", "--speak-engine", "clone",
        ])
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "mock-key",
                    "ELEVENLABS_API_KEY": "el-key",
                    "ELEVENLABS_VOICE_ID": "voice-from-env",
                },
                clear=True,
            ),
            patch(
                "audio_gateway.main._startup_doctor",
                return_value=0,
            ),
            patch(
                "audio_gateway.devices.resolve",
                return_value=self._resolved_devices(),
            ),
            patch("audio_gateway.bridge.run_bridge", return_value=0) as run,
        ):
            code = main.cmd_bridge(args)

        self.assertEqual(0, code)
        self.assertEqual("clone", run.call_args.kwargs["speak_engine"])
        self.assertEqual(
            "el-key",
            run.call_args.kwargs["elevenlabs_api_key"],
        )
        self.assertEqual(
            "voice-from-env",
            run.call_args.kwargs["clone_voice_id"],
        )

    def test_cli_voice_id_flag_beats_environment(self) -> None:
        args = main.build_parser().parse_args([
            "bridge", "--speak", "--speak-engine", "clone",
            "--speak-voice-id", "voice-from-flag",
            "--clone-model", "eleven_turbo_v2_5",
            "--clone-speed", "1.1",
        ])
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "mock-key",
                    "ELEVENLABS_API_KEY": "el-key",
                    "ELEVENLABS_VOICE_ID": "voice-from-env",
                },
                clear=True,
            ),
            patch(
                "audio_gateway.main._startup_doctor",
                return_value=0,
            ),
            patch(
                "audio_gateway.devices.resolve",
                return_value=self._resolved_devices(),
            ),
            patch("audio_gateway.bridge.run_bridge", return_value=0) as run,
        ):
            code = main.cmd_bridge(args)

        self.assertEqual(0, code)
        self.assertEqual(
            "voice-from-flag",
            run.call_args.kwargs["clone_voice_id"],
        )
        self.assertEqual(
            "eleven_turbo_v2_5",
            run.call_args.kwargs["clone_model"],
        )
        self.assertEqual(1.1, run.call_args.kwargs["clone_speed"])


if __name__ == "__main__":
    unittest.main()
