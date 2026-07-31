"""OpenAI（及任何 OpenAI 兼容端点）共享 chat-completions 客户端。

翻译 / 纪要 / 参谋三处共用。走 REST 而非 openai SDK：与既有 Ollama 后端同构、
不新增依赖，且 --openai-base-url 可直接指向 Azure / vLLM / LM Studio 等兼容端点。

凭据：OPENAI_API_KEY 环境变量（兼容端点若无鉴权可留空）。
注意 Codex CLI 的 ~/.codex/auth.json 是 ChatGPT OAuth 会话，**不是** API key，
不能用于本路径。
"""

from __future__ import annotations

import os

import httpx


class OpenAIChatClient:
    def __init__(self, model: str, base_url: str, *, timeout: float = 120.0):
        self._model = model
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self._api_key = os.environ.get("OPENAI_API_KEY", "").strip()

    def complete(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        elif "api.openai.com" in str(self._client.base_url):
            raise RuntimeError(
                "未设置 OPENAI_API_KEY。请 export OPENAI_API_KEY=... "
                "（Codex CLI 的 ChatGPT OAuth 不是 API key，不能复用）。"
            )
        resp = self._client.post("/chat/completions", headers=headers, json={
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_completion_tokens": max_tokens,
        })
        if resp.status_code >= 400:
            raise RuntimeError(f"OpenAI 调用失败 {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenAI 返回无 choices: {str(data)[:300]}")
        return (choices[0].get("message", {}).get("content") or "").strip()

    def close(self) -> None:
        self._client.close()
