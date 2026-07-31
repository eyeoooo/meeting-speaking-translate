"""模块 C：转写文本归档（人读 txt + 结构化 jsonl，均带时间戳）。"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transcriber import TranscriptSegment


class TranscriptArchiver:
    def __init__(self, session_dir: Path, meeting_start: datetime):
        self._start = meeting_start
        self._txt = open(session_dir / "transcript.txt", "a", encoding="utf-8")
        self._jsonl = open(session_dir / "transcript.jsonl", "a", encoding="utf-8")
        self._lock = threading.Lock()
        self._txt.write(f"# 会议开始 {meeting_start:%Y-%m-%d %H:%M:%S}\n")

    def add(
        self,
        seg: TranscriptSegment,
        translated: str | None = None,
        *,
        source: str | None = None,
    ) -> None:
        # batch/whisper 路径的 zh 是同一 segment 的真实翻译（whisper+translator
        # 或会后批量），是合法配对——保留；clock="batch" 表示 t 是 wav 的真实
        # 音频时刻，与 realtime 的"到达时刻"是两个原点，绝不能混排。
        rel = time.strftime("%H:%M:%S", time.gmtime(seg.t0))
        source_label = f" [{source}]" if source else ""
        with self._lock:
            self._txt.write(
                f"[{rel}]{source_label} ({seg.lang or '?'}) {seg.text}\n"
            )
            if translated:
                self._txt.write(f"         译: {translated}\n")
            self._txt.flush()
            record = {
                "schema": 2, "clock": "batch",
                "t0": round(seg.t0, 2), "t1": round(seg.t1, 2),
                "lang": seg.lang, "text": seg.text, "zh": translated,
            }
            if source is not None:
                record["source"] = source
            self._jsonl.write(
                json.dumps(record, ensure_ascii=False) + "\n"
            )
            self._jsonl.flush()

    def add_realtime_segment(
        self,
        *,
        stream: str,
        text: str,
        t: float,
        lang: str | None,
    ) -> None:
        """Archive one realtime segment as an independent row.

        双泳道协议：Realtime 两条流没有任何关联字段，所以 realtime 行不写 zh
        ——配对字段在数据层不存在。t 是句子"到达时刻"（比发声晚 1-3s），用
        clock="realtime" 与 batch 的音频时刻隔离。txt 两段式且无父子缩进：
        缩进本身就在宣称配对。
        """
        rel = f"{int(t) // 60:02d}:{int(t) % 60:02d}"
        label = "中" if stream == "translation" else "日"
        with self._lock:
            self._txt.write(f"[{rel}] {label} {text}\n")
            self._txt.flush()
            self._jsonl.write(json.dumps({
                "schema": 2,
                "clock": "realtime",
                "stream": stream,
                "t0": round(t, 2),
                "t1": round(t, 2),
                "lang": lang,
                "text": text,
                "source": "realtime",
            }, ensure_ascii=False) + "\n")
            self._jsonl.flush()

    def close(self) -> None:
        with self._lock:
            self._txt.close()
            self._jsonl.close()
