"""阶段 C 双泳道规格测试：配对这个概念在数据层必须不存在。

事实依据：一场真实会议实测 163 条日文原文 : 134 条中文译文——一条译文经常
覆盖两三句原文，而 OpenAI Realtime translations 协议不提供任何关联字段
（server events 只有 {type,event_id,delta,elapsed_ms}，官方明文 elapsed_ms
不是唯一标识、无 .done 事件）。按序配对是不可修的猜测，正解是两条独立泳道。

本文件把这个结论钉成规格：任何一条 segment 记录都只承载一条流的文本；
两条流句数不等是常态而非异常；断线重连不携带任何跨 session 配对态。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

AUDIO_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUDIO_GATEWAY_ROOT))

from audio_gateway.archive import TranscriptArchiver  # noqa: E402
from audio_gateway.bridge import SegmentHistory  # noqa: E402
from audio_gateway.bus import Tap  # noqa: E402
from audio_gateway.interpreter import (  # noqa: E402
    RealtimeInterpreter,
    Segment,
    SentenceAccumulator,
)
from audio_gateway.summarizer import _PROMPT, _load_transcript  # noqa: E402

# 复用 test_interpreter.py 的 NDJSON 事件脚本 mock（同目录，pytest prepend 导入）。
from test_interpreter import (  # noqa: E402
    DEFAULT_EVENT_SCRIPT,
    MockRealtimeServer,
    SpyOutputPlayer,
)

# 2:1 合并形状（事实 C 的最小复刻）：T23 一条译文覆盖 S2+S3 两句原文。
# 任何"按序配对"的实现都会把 T23 或后续译文错挂到相邻 source 上。
MERGED_2TO1_SCRIPT = """
{"conn": 1, "type": "session.input_transcript.delta", "delta": "簡単な質問をしてもらってから。", "elapsed_ms": 1200}
{"conn": 1, "type": "session.output_transcript.delta", "delta": "我觉得先请他们问个简单的问题。", "elapsed_ms": 2400}
{"conn": 1, "type": "session.input_transcript.delta", "delta": "制限しないと混乱を招いて。", "elapsed_ms": 3600}
{"conn": 1, "type": "session.input_transcript.delta", "delta": "オンラインだと100名までです。", "elapsed_ms": 4800}
{"conn": 1, "type": "session.output_transcript.delta", "delta": "不限制会造成混乱，线上最多100人。", "elapsed_ms": 6000}
{"conn": 1, "type": "session.input_transcript.delta", "delta": "いかがでしょうか。", "elapsed_ms": 7200}
{"conn": 1, "type": "session.output_transcript.delta", "delta": "大家觉得怎么样？", "elapsed_ms": 8400}
"""

# 译文先到：旧实现会把先到译文压队列、等下一条 source 到达后错误回填。
TRANSLATION_FIRST_SCRIPT = """
{"conn": 1, "type": "session.output_transcript.delta", "delta": "抢先到达的译文一。", "elapsed_ms": 500}
{"conn": 1, "type": "session.output_transcript.delta", "delta": "抢先到达的译文二。", "elapsed_ms": 900}
{"conn": 1, "type": "session.input_transcript.delta", "delta": "遅れて届く原文。", "elapsed_ms": 1300}
"""

SEGMENT_RECORD_FIELDS = frozenset(
    {"id", "stream", "text", "t", "elapsed_ms", "epoch"}
)


async def collect_segments(script: str, expected: int) -> list[Segment]:
    """Drive RealtimeInterpreter over an NDJSON script and collect Segments."""
    server = MockRealtimeServer(script=script)
    segments: list[Segment] = []
    enough = asyncio.Event()

    def on_segment(segment: Segment) -> None:
        segments.append(segment)
        if len(segments) >= expected:
            enough.set()

    async def fast_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    client = RealtimeInterpreter(
        Tap("interpreter", maxsize=8, drop_oldest=True),
        SpyOutputPlayer(),
        api_key="mock-key-never-sent-to-openai",
        safety_identifier="hashed-test-user",
        url=server.url,
        sleep=fast_sleep,
        on_sentence=on_segment,
        session_factory=server.session_factory,
    )
    task = client.start()
    await asyncio.wait_for(enough.wait(), 2.0)
    await client.stop()
    await task
    return segments


def fill_history(segments: list[Segment]) -> SegmentHistory:
    """Feed segments into SegmentHistory the same way bridge glue does."""
    history = SegmentHistory()
    for index, segment in enumerate(segments):
        history.add(
            stream=segment.stream,
            text=segment.text,
            t=float(index),
            elapsed_ms=segment.elapsed_ms,
            epoch=segment.epoch,
        )
    return history


class SegmentStreamPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_merged_translation_never_contaminates_neighbour_source(
        self,
    ) -> None:
        segments = await collect_segments(MERGED_2TO1_SCRIPT, 7)
        history = fill_history(segments)
        snapshot = history.snapshot()

        # 核心断言：任何一条记录都不同时承载 source 与 translation 文本——
        # 配对这个概念在数据层已不存在（没有可供错配的第二文本字段）。
        for record in snapshot:
            self.assertEqual(SEGMENT_RECORD_FIELDS, set(record))
        # 2:1 合并下两条泳道各自完整、顺序为到达顺序，互不改写。
        self.assertEqual(
            [
                "簡単な質問をしてもらってから。",
                "制限しないと混乱を招いて。",
                "オンラインだと100名までです。",
                "いかがでしょうか。",
            ],
            [r["text"] for r in snapshot if r["stream"] == "source"],
        )
        self.assertEqual(
            [
                "我觉得先请他们问个简单的问题。",
                "不限制会造成混乱，线上最多100人。",
                "大家觉得怎么样？",
            ],
            [r["text"] for r in snapshot if r["stream"] == "translation"],
        )
        # 全局单调 id 即客户端主键：严格递增、无微秒 hack。
        self.assertEqual(
            list(range(7)),
            [r["id"] for r in snapshot],
        )
        # elapsed_ms 只随记录带出（epoch 内显示标记），逐条对应脚本值。
        self.assertEqual(
            [1200, 2400, 3600, 4800, 6000, 7200, 8400],
            [s.elapsed_ms for s in segments],
        )

    async def test_translation_leading_source_does_not_shift_late(self) -> None:
        segments = await collect_segments(TRANSLATION_FIRST_SCRIPT, 3)

        self.assertEqual(
            ["translation", "translation", "source"],
            [s.stream for s in segments],
        )
        history = SegmentHistory()
        records = [
            history.add(
                stream=s.stream,
                text=s.text,
                t=float(i),
                elapsed_ms=s.elapsed_ms,
                epoch=s.epoch,
            )
            for i, s in enumerate(segments)
        ]
        # 旧实现对先到译文返回 None 并压队列等待回填；新实现永不返回 None：
        # 先到的译文就是它自己的独立记录，迟到的 source 不会"领养"它。
        for record in records:
            self.assertIsNotNone(record)
            self.assertEqual(SEGMENT_RECORD_FIELDS, set(record))
        self.assertEqual("遅れて届く原文。", records[2]["text"])
        self.assertEqual("source", records[2]["stream"])
        self.assertEqual(
            ["抢先到达的译文一。", "抢先到达的译文二。"],
            [r["text"] for r in history.snapshot() if r["stream"] == "translation"],
        )

    async def test_reconnect_does_not_carry_pairing_state(self) -> None:
        # DEFAULT_EVENT_SCRIPT：conn1 五句（3 source:2 translation）+ 断线，
        # conn2 译文先到且 elapsed_ms 从 500 重计。
        segments = await collect_segments(DEFAULT_EVENT_SCRIPT, 7)

        conn1, conn2 = segments[:5], segments[5:]
        first_epoch = conn1[0].epoch
        # conn1 内 epoch 一致；断线重连后 epoch 必须递增——跨 session 的
        # 任何"配对/对齐"想象在 epoch 边界被硬性切断。
        self.assertEqual({first_epoch}, {s.epoch for s in conn1})
        for segment in conn2:
            self.assertGreater(segment.epoch, first_epoch)
        # conn2 译文先到：它不回填 conn1 的未配对 source（旧实现会把
        # 「大家觉得怎么样？」错挂到「オンラインだと100名までです。」上，
        # 正是真机取证 transcript.jsonl 第 3 行的错位形状）。
        self.assertEqual("translation", conn2[0].stream)
        self.assertEqual("大家觉得怎么样？", conn2[0].text)
        self.assertEqual(500, conn2[0].elapsed_ms)
        history = fill_history(segments)
        snapshot = history.snapshot()
        # elapsed_ms 重计（500 < 6000）不得导致重排：顺序只由到达序 id 决定。
        self.assertEqual([r["id"] for r in snapshot], sorted(
            r["id"] for r in snapshot
        ))
        self.assertEqual(
            "いかがでしょうか。",
            [r for r in snapshot if r["stream"] == "source"][-1]["text"],
        )


class SegmentHistoryRingTests(unittest.TestCase):
    def test_ring_eviction_of_unpaired_source_cannot_misattribute(self) -> None:
        # 旧实现的静默错配路径：环淘汰未配对 source 后，译文整体前移一位。
        # 新实现两环独立，source 淘汰只影响 source 自己。
        history = SegmentHistory(limit_per_stream=3)
        returned = [
            history.add(stream="source", text=f"原文{index}。", t=float(index))
            for index in range(5)
        ]
        translation = history.add(
            stream="translation",
            text="迟到的译文。",
            t=10.0,
        )

        for record in returned:
            self.assertIsNotNone(record)
        self.assertEqual("translation", translation["stream"])
        self.assertEqual("迟到的译文。", translation["text"])
        snapshot = history.snapshot()
        self.assertEqual(
            ["原文2。", "原文3。", "原文4。"],
            [r["text"] for r in snapshot if r["stream"] == "source"],
        )
        self.assertEqual(
            ["迟到的译文。"],
            [r["text"] for r in snapshot if r["stream"] == "translation"],
        )
        # append-only：已返回的记录是副本，后续写入/淘汰不改写它们。
        self.assertEqual("原文0。", returned[0]["text"])
        ids = [r["id"] for r in snapshot]
        self.assertEqual(sorted(ids), ids)

    def test_add_validates_stream_and_rejects_empty_text(self) -> None:
        history = SegmentHistory()

        with self.assertRaises(ValueError):
            history.add(stream="mystery", text="文本。", t=0.0)
        with self.assertRaises(ValueError):
            history.add(stream="source", text="   ", t=0.0)
        self.assertEqual(0, len(history))


class SentenceAccumulatorSpecTests(unittest.TestCase):
    def test_decimal_point_does_not_split_sentence(self) -> None:
        # ASCII '.' 会把 10.5 / No.3 误切，且两条流误切率不同，放大句数差；
        # 它已从终止符中移除。
        accumulator = SentenceAccumulator()

        self.assertEqual([], accumulator.feed("予算は10.5"))
        self.assertEqual(["予算は10.5億円です。"], accumulator.feed("億円です。"))

        abbreviations = SentenceAccumulator()
        self.assertEqual(
            ["No.3の案でいきましょう。"],
            abbreviations.feed("No.3の案でいきましょう。"),
        )

    def test_ellipsis_and_semicolons_terminate(self) -> None:
        accumulator = SentenceAccumulator()

        self.assertEqual(["そうですね…"], accumulator.feed("そうですね…"))
        self.assertEqual(["A；", "B;"], accumulator.feed("A；B;"))
        self.assertEqual(["続き‥"], accumulator.feed("続き‥"))

    def test_two_streams_produce_unequal_sentence_counts(self) -> None:
        # 故意钉成规格：两条流句数天然不等（实测 163:134）。将来任何人想再写
        # 配对逻辑，必须先让这条测试失败——那就是在改规格，不是在修 bug。
        source = SentenceAccumulator()
        translation = SentenceAccumulator()

        source_sentences: list[str] = []
        for delta in (
            "制限しないと",
            "混乱を招いて。",
            "オンラインだと100名までです。",
            "いかがでしょうか。",
        ):
            source_sentences += source.feed(delta)
        translation_sentences: list[str] = []
        for delta in ("不限制会造成混乱，", "线上最多100人。", "大家觉得怎么样？"):
            translation_sentences += translation.feed(delta)

        self.assertEqual(3, len(source_sentences))
        self.assertEqual(2, len(translation_sentences))
        self.assertNotEqual(len(source_sentences), len(translation_sentences))

    def test_overlong_pending_is_force_flushed(self) -> None:
        accumulator = SentenceAccumulator(max_pending_chars=10)

        self.assertEqual(["あ" * 10], accumulator.feed("あ" * 10))

    def test_stale_pending_is_flushed_by_time(self) -> None:
        now = {"value": 0.0}
        accumulator = SentenceAccumulator(
            max_pending_seconds=5.0,
            clock=lambda: now["value"],
        )

        self.assertEqual([], accumulator.feed("途中まで"))
        now["value"] = 6.0
        self.assertEqual(["途中まで、"], accumulator.feed("、"))


class ArchiveSchemaTests(unittest.TestCase):
    def test_realtime_segment_rows_are_independent_and_unpaired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            archiver = TranscriptArchiver(
                session_dir,
                datetime(2026, 7, 30, 14, 18, 0),
            )
            archiver.add_realtime_segment(
                stream="source",
                text="オンラインだと100名までです。",
                t=65.0,
                lang=None,
            )
            archiver.add_realtime_segment(
                stream="translation",
                text="线上最多100人。",
                t=66.5,
                lang="zh",
            )
            archiver.close()
            rows = [
                json.loads(line)
                for line in (session_dir / "transcript.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            txt = (session_dir / "transcript.txt").read_text(encoding="utf-8")

        for row in rows:
            self.assertEqual(2, row["schema"])
            self.assertEqual("realtime", row["clock"])
            self.assertEqual("realtime", row["source"])
            # realtime 行不写 zh：配对字段在数据层不存在。
            self.assertNotIn("zh", row)
        self.assertEqual(["source", "translation"], [r["stream"] for r in rows])
        # 两段式、无父子缩进（缩进本身就在宣称配对）。
        self.assertIn("[01:05] 日 オンラインだと100名までです。", txt)
        self.assertIn("[01:06] 中 线上最多100人。", txt)
        self.assertNotIn("译:", txt)

    def test_batch_rows_keep_legit_zh_and_gain_schema_clock(self) -> None:
        # 批量 whisper 行的 zh 是同一 segment 的真实翻译，是合法配对——保留。
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            archiver = TranscriptArchiver(
                session_dir,
                datetime(2026, 7, 30, 14, 18, 0),
            )
            archiver.add(
                SimpleNamespace(t0=2.0, t1=3.5, lang="ja", text="会後の段落"),
                "会后译文",
                source="batch",
            )
            archiver.close()
            row = json.loads(
                (session_dir / "transcript.jsonl")
                .read_text(encoding="utf-8")
                .strip()
                .splitlines()[-1]
            )

        self.assertEqual(2, row["schema"])
        self.assertEqual("batch", row["clock"])
        self.assertEqual("batch", row["source"])
        self.assertEqual("会后译文", row["zh"])


class SummarizerSchemaTests(unittest.TestCase):
    def _write_jsonl(self, rows: list[dict]) -> Path:
        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "transcript.jsonl"
        path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        return path

    def test_summarizer_rejects_schema1_pairing(self) -> None:
        # 已污染的 schema-1 历史文件：含空 text 行、乱序 t0。真机取证证明其
        # text/zh 从第 2 行起就语义错位——绝不能再拼「原文 | 译文」。
        path = self._write_jsonl([
            {
                "t0": 5.0, "t1": 5.0, "lang": None,
                "text": "制限しないと混乱を招いて。",
                "zh": "看起来很有趣呢。",
                "source": "realtime",
            },
            {
                "t0": 1.0, "t1": 1.0, "lang": None,
                "text": "簡単な質問をしてもらってから。",
                "zh": "我觉得先请他们问个简单的问题。",
                "source": "realtime",
            },
            {
                "t0": 9.0, "t1": 9.0, "lang": None,
                "text": "",
                "zh": "大家觉得怎么样？",
                "source": "realtime",
            },
        ])

        out = _load_transcript(path)

        # 不再存在任何「原文 | 译文」拼接形式。
        self.assertNotIn(" | ", out)
        # 中文一条不丢——包括 text 为空、只有 zh 的行。
        for zh in (
            "看起来很有趣呢。",
            "我觉得先请他们问个简单的问题。",
            "大家觉得怎么样？",
        ):
            self.assertIn(zh, out)
        # 拆成两条独立流：A（原文）在前、B（译文）在后，组内按时间有序。
        self.assertLess(
            out.index("簡単な質問をしてもらってから。"),
            out.index("制限しないと混乱を招いて。"),
        )
        self.assertLess(
            out.index("我觉得先请他们问个简单的问题。"),
            out.index("看起来很有趣呢。"),
        )
        self.assertLess(
            out.index("看起来很有趣呢。"),
            out.index("大家觉得怎么样？"),
        )
        self.assertLess(out.index("事实基准"), out.index("消歧"))
        # 提示词不再宣称逐句对照，且明确 A 为事实基准。
        self.assertNotIn("原文 | 中文译文", _PROMPT)
        self.assertIn("不是】逐句对应", _PROMPT)
        self.assertIn("以 A 为事实基准", _PROMPT)

    def test_schema2_sorts_only_within_same_clock(self) -> None:
        # realtime 的 t 是句子到达时刻、batch 的 t 是 wav 音频时刻：
        # 两个原点绝不能混排。batch 行 t0 最小也不得插进 realtime 流。
        path = self._write_jsonl([
            {
                "schema": 2, "clock": "realtime", "stream": "source",
                "t0": 100.0, "t1": 100.0, "lang": None,
                "text": "実時間の原文。", "source": "realtime",
            },
            {
                "schema": 2, "clock": "realtime", "stream": "translation",
                "t0": 40.0, "t1": 40.0, "lang": "zh",
                "text": "实时译文。", "source": "realtime",
            },
            {
                "schema": 2, "clock": "batch",
                "t0": 10.0, "t1": 12.0, "lang": "ja",
                "text": "会後の批量段落", "zh": "批量段落的合法译文",
                "source": "batch",
            },
        ])

        out = _load_transcript(path)

        # 三组各自成段；batch 组排在 realtime 两条泳道之后，不按 t0 混排。
        self.assertLess(out.index("実時間の原文。"), out.index("实时译文。"))
        self.assertLess(out.index("実時間の原文。"), out.index("会後の批量段落"))
        self.assertLess(out.index("实时译文。"), out.index("会後の批量段落"))
        # batch 行的 zh 是同一段落的真实翻译（合法配对），保留拼接。
        self.assertIn("会後の批量段落 | 批量段落的合法译文", out)
        self.assertIn("音频时间轴", out)


if __name__ == "__main__":
    unittest.main()
