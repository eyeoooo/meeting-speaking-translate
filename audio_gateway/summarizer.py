"""模块 D：会议纪要提炼（Claude 流式 / Ollama），输出 Markdown。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

_PROMPT = """\
你是一名资深会议记录员。下面是一场会议的机器转写（可能含日/英/中混合，含少量识别错误）。

请输出 Markdown 格式的结构化会议纪要（用简体中文），包含以下小节：

## 会议概要
2-4 句概括。

## 核心议题
逐条列出实际讨论的主题。

## 关键决议 (Decisions)
明确达成的结论。没有则写"无明确决议"。

## 待办事项 (To-Do)
| 事项 | 责任人 | 期限 |
责任人/期限转写中未提及则填"未指明"。

## 技术名词勘误
转写常把专业词听错（尤其 IT 架构/代码重构语境：AWS、EC2、S3、Kubernetes、Java、C#、
Redis、API、CI/CD、microservice 等）。列出你依据上下文纠正的词，并在上文各小节中直接使用纠正后的写法。

只输出纪要本身。以下是同一场会议的多条独立时间轴记录：A（原文流）与 B（译文流）
由两个不同模型独立断句，行与行之间【不是】逐句对应关系，只有时间戳可交叉参照。
请以 A 为事实基准，B 仅用于消歧；若含会后批量转写段落，它使用另一条独立的音频时间轴。

{transcript}
"""


def _load_transcript(path: Path) -> str:
    """接受 transcript.jsonl 或 transcript.txt，拼成 LLM 输入。

    双泳道原则：
    - schema-2 realtime 行天然带 stream，按流分组；
    - schema-1 旧文件的 realtime 行是按到达顺序 zip 出来的伪配对（真机取证
      从第 2 行起就语义错位），必须拆成两条独立流，绝不再拼「原文 | 译文」；
      batch/whisper 行的 zh 是同一 segment 的真实翻译（合法配对），保留。
    - 排序只发生在同一 clock 内：realtime 的 t 是"到达时刻"、batch 的 t 是
      wav 音频时刻，两个原点混排会产生虚假先后关系。
    """
    if path.suffix != ".jsonl":
        return path.read_text(encoding="utf-8")

    def stamp(t0: float) -> str:
        return f"[{int(t0 // 60):02d}:{int(t0 % 60):02d}]"

    source_lines: list[tuple[float, str]] = []
    translation_lines: list[tuple[float, str]] = []
    batch_lines: list[tuple[float, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        d = json.loads(raw)
        t0 = float(d.get("t0") or 0.0)
        text = (d.get("text") or "").strip()
        zh = (d.get("zh") or "").strip()
        schema = int(d.get("schema") or 1)
        realtime = (
            d.get("clock") == "realtime"
            if schema >= 2
            else d.get("source") == "realtime"
        )
        if not realtime:
            # batch/whisper 行：zh 是同一段落的真实翻译，是合法配对。
            line = f"{stamp(t0)} ({d.get('lang') or '?'}) {text}"
            if zh:
                line += f" | {zh}"
            batch_lines.append((t0, line))
        elif schema >= 2:
            if d.get("stream") == "translation" and text:
                translation_lines.append((t0, f"{stamp(t0)} {text}"))
            elif text:
                source_lines.append((t0, f"{stamp(t0)} {text}"))
        else:
            # schema-1 realtime：伪配对，text 与 zh 各回各的独立流。
            if text:
                source_lines.append((t0, f"{stamp(t0)} {text}"))
            if zh:
                translation_lines.append((t0, f"{stamp(t0)} {zh}"))

    sections: list[str] = []
    if source_lines:
        source_lines.sort(key=lambda item: item[0])
        sections.append(
            "【A｜原文流 · 会中实时时间轴（事实基准）】\n"
            + "\n".join(line for _, line in source_lines)
        )
    if translation_lines:
        translation_lines.sort(key=lambda item: item[0])
        sections.append(
            "【B｜译文流 · 会中实时时间轴（仅用于消歧，与 A 不逐句对应）】\n"
            + "\n".join(line for _, line in translation_lines)
        )
    if batch_lines:
        batch_lines.sort(key=lambda item: item[0])
        sections.append(
            "【C｜会后批量转写 · 音频时间轴（时钟原点与 A/B 不同）】\n"
            + "\n".join(line for _, line in batch_lines)
        )
    return "\n\n".join(sections)


def _summarize_claude(transcript: str, model: str) -> str:
    from anthropic import Anthropic

    client = Anthropic()
    # 纪要可能很长 → 流式；fallbacks="default" 让偶发的分类器拒答自动降级重跑
    with client.beta.messages.stream(
        model=model,
        max_tokens=32000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": _PROMPT.format(transcript=transcript)}],
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise RuntimeError("模型拒绝生成纪要（含 fallback 链），请人工检查转写内容。")
    return "".join(b.text for b in message.content if b.type == "text")


def _summarize_openai(transcript: str, model: str, base_url: str) -> str:
    from .openai_client import OpenAIChatClient

    client = OpenAIChatClient(model, base_url, timeout=600.0)
    return client.complete(
        "你是一名资深会议记录员，输出 Markdown 会议纪要。",
        _PROMPT.format(transcript=transcript),
        max_tokens=32000,
    )


def _summarize_ollama(transcript: str, url: str, model: str) -> str:
    resp = httpx.post(f"{url}/api/generate", json={
        "model": model,
        "prompt": _PROMPT.format(transcript=transcript),
        "stream": False,
    }, timeout=600.0)
    resp.raise_for_status()
    return resp.json()["response"]


def summarize(transcript_path: Path, cfg) -> Path:
    transcript = _load_transcript(transcript_path)
    if not transcript.strip():
        raise RuntimeError(f"转写为空：{transcript_path}")
    if cfg.summary_backend == "claude":
        minutes = _summarize_claude(transcript, cfg.claude_model)
    elif cfg.summary_backend == "openai":
        minutes = _summarize_openai(transcript, cfg.openai_model, cfg.openai_base_url)
    elif cfg.summary_backend == "ollama":
        minutes = _summarize_ollama(transcript, cfg.ollama_url, cfg.ollama_model)
    else:
        raise ValueError(f"未知纪要后端: {cfg.summary_backend}")
    out = transcript_path.parent / "minutes.md"
    out.write_text(minutes, encoding="utf-8")
    return out
