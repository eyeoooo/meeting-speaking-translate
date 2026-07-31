"""模块 B：日/英 → 中文实时翻译（后端可插拔：claude / ollama / none）。"""

from __future__ import annotations

import httpx

_SYSTEM = (
    "你是同声传译。把用户给出的会议发言忠实翻译成简体中文，"
    "保留 AWS、Redis、API、Java、C# 等技术名词原文，只输出译文，不要解释。"
)


class NullTranslator:
    def translate(self, text: str, lang: str) -> str | None:
        return None


class OllamaTranslator:
    def __init__(self, url: str, model: str):
        self._client = httpx.Client(base_url=url, timeout=30.0)
        self._model = model

    def translate(self, text: str, lang: str) -> str | None:
        if lang.startswith("zh"):
            return None
        resp = self._client.post("/api/generate", json={
            "model": self._model,
            "prompt": f"{_SYSTEM}\n\n发言（{lang}）：{text}\n译文：",
            "stream": False,
        })
        resp.raise_for_status()
        return resp.json()["response"].strip()


class ClaudeTranslator:
    def __init__(self, model: str):
        from anthropic import Anthropic
        self._client = Anthropic()  # ANTHROPIC_API_KEY 或 ant auth login profile
        self._model = model

    def translate(self, text: str, lang: str) -> str | None:
        if lang.startswith("zh"):
            return None
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        if response.stop_reason == "refusal":
            return None
        return "".join(b.text for b in response.content if b.type == "text").strip()


class OpenAITranslator:
    def __init__(self, model: str, base_url: str):
        from .openai_client import OpenAIChatClient
        self._client = OpenAIChatClient(model, base_url, timeout=30.0)

    def translate(self, text: str, lang: str) -> str | None:
        if lang.startswith("zh"):
            return None
        return self._client.complete(_SYSTEM, text, max_tokens=1024) or None


def make_translator(cfg):
    if cfg.translate_backend == "none":
        return NullTranslator()
    if cfg.translate_backend == "ollama":
        return OllamaTranslator(cfg.ollama_url, cfg.ollama_model)
    if cfg.translate_backend == "openai":
        return OpenAITranslator(cfg.openai_model, cfg.openai_base_url)
    if cfg.translate_backend == "claude":
        return ClaudeTranslator(cfg.claude_model)
    raise ValueError(f"未知翻译后端: {cfg.translate_backend}")
