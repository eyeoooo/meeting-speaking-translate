"""OpenAI 后端：请求形状、凭据校验、三处接线的选择逻辑。"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio_gateway.config import GatewayConfig  # noqa: E402
from audio_gateway.openai_client import OpenAIChatClient  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class _FakeHttpClient:
    def __init__(self, response: _FakeResponse, base_url: str):
        self._response = response
        self.base_url = base_url
        self.calls: list[tuple[str, dict, dict]] = []

    def post(self, path, *, headers, json):  # noqa: A002 - 对齐 httpx 签名
        self.calls.append((path, headers, json))
        return self._response

    def close(self) -> None:
        pass


def _client(response: _FakeResponse, *, base_url="https://api.openai.com/v1", key="k"):
    with patch.dict(os.environ, {"OPENAI_API_KEY": key}, clear=False):
        c = OpenAIChatClient("test-model", base_url)
    c._client = _FakeHttpClient(response, base_url)
    return c


class OpenAIChatClientTests(unittest.TestCase):
    def test_request_shape_and_auth_header(self) -> None:
        c = _client(_FakeResponse({"choices": [{"message": {"content": " hi "}}]}))
        out = c.complete("SYS", "USER", max_tokens=123)
        self.assertEqual("hi", out)
        path, headers, body = c._client.calls[0]
        self.assertEqual("/chat/completions", path)
        self.assertEqual("Bearer k", headers["authorization"])
        self.assertEqual("test-model", body["model"])
        self.assertEqual(123, body["max_completion_tokens"])
        self.assertEqual(
            [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}],
            body["messages"],
        )

    def test_missing_key_on_openai_host_fails_closed(self) -> None:
        c = _client(_FakeResponse({}), key="")
        c._api_key = ""
        with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
            c.complete("s", "u")

    def test_compatible_endpoint_allows_empty_key(self) -> None:
        c = _client(
            _FakeResponse({"choices": [{"message": {"content": "ok"}}]}),
            base_url="http://127.0.0.1:8000/v1",
            key="",
        )
        c._api_key = ""
        self.assertEqual("ok", c.complete("s", "u"))
        _, headers, _ = c._client.calls[0]
        self.assertNotIn("authorization", headers)

    def test_http_error_and_empty_choices_raise(self) -> None:
        c = _client(_FakeResponse({"error": "bad"}, status=401))
        with self.assertRaisesRegex(RuntimeError, "401"):
            c.complete("s", "u")
        c2 = _client(_FakeResponse({"choices": []}))
        with self.assertRaisesRegex(RuntimeError, "choices"):
            c2.complete("s", "u")


class BackendSelectionTests(unittest.TestCase):
    def test_translator_factory_selects_openai(self) -> None:
        from audio_gateway.translator import OpenAITranslator, make_translator

        cfg = GatewayConfig()
        cfg.translate_backend = "openai"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "k"}, clear=False):
            self.assertIsInstance(make_translator(cfg), OpenAITranslator)

    def test_translator_skips_chinese_source(self) -> None:
        from audio_gateway.translator import OpenAITranslator

        cfg = GatewayConfig()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "k"}, clear=False):
            t = OpenAITranslator(cfg.openai_model, cfg.openai_base_url)
        self.assertIsNone(t.translate("已经是中文", "zh"))

    def test_advisor_accepts_openai_backend(self) -> None:
        from audio_gateway import advisor as advisor_mod

        cfg = GatewayConfig()
        cfg.advisor_backend = "openai"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "k"}, clear=False):
            a = advisor_mod.Advisor(cfg, sink=object())
        self.assertIsInstance(a._brain, advisor_mod._OpenAIBrain)

    def test_summarize_routes_to_openai(self) -> None:
        from audio_gateway import summarizer as summarizer_mod

        cfg = GatewayConfig()
        cfg.summary_backend = "openai"
        seen: dict[str, object] = {}

        def fake(transcript, model, base_url):
            seen.update(transcript=transcript, model=model, base_url=base_url)
            return "# 纪要"

        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "transcript.txt"
            path.write_text("[00:01] hello", encoding="utf-8")
            with patch.object(summarizer_mod, "_summarize_openai", fake):
                out = summarizer_mod.summarize(path, cfg)
            self.assertEqual("# 纪要", out.read_text(encoding="utf-8"))
        self.assertEqual(cfg.openai_model, seen["model"])


if __name__ == "__main__":
    unittest.main()
