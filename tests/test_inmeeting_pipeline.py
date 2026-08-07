from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import numpy as np


AUDIO_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUDIO_GATEWAY_ROOT))

from audio_gateway import main  # noqa: E402
from audio_gateway.advisor import (  # noqa: E402
    ADVICE_POINT_MAX_CHARS,
    Advisor,
    AdvisorState,
    CallbackAdvisorSink,
    _ClaudeBrain,
    condense_advice,
)
from audio_gateway.bridge import (  # noqa: E402
    AdviceHistory,
    AdviceLog,
    BridgeRuntimeState,
    SegmentHistory,
    _history,
    draft_broadcast_payload,
    _interpret_voice,
    _route_sentence_to_advisor,
)
from audio_gateway.bus import Tap  # noqa: E402
from audio_gateway.config import GatewayConfig  # noqa: E402
from audio_gateway.interpreter import (  # noqa: E402
    InterpreterState,
    RealtimeInterpreter,
    Segment,
    SilenceVadGate,
)


class SpyPlayer:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.started = 0
        self.stopped = 0
        self.chunks: list[bytes] = []

    def start(self) -> None:
        self.started += 1
        if self.fail_start:
            raise RuntimeError("mock player unavailable")

    def stop(self) -> None:
        self.stopped += 1

    def feed_pcm16(self, data: bytes) -> None:
        self.chunks.append(data)


class HistoryTests(unittest.TestCase):
    def test_each_stream_records_independently_and_never_returns_none(
        self,
    ) -> None:
        # 双泳道规格：先到的译文就是自己的独立记录（旧实现会返回 None 并
        # 压队列等待回填），source 记录里根本没有可供回填的第二文本字段。
        history = SegmentHistory()

        translation = history.add(
            stream="translation",
            text="先到的译文。",
            t=1.0,
        )
        source = history.add(stream="source", text="遅れて届く原文。", t=2.0)

        self.assertEqual({
            "id": 0,
            "stream": "translation",
            "text": "先到的译文。",
            "t": 1.0,
            "elapsed_ms": None,
            "epoch": 0,
        }, translation)
        self.assertEqual({
            "id": 1,
            "stream": "source",
            "text": "遅れて届く原文。",
            "t": 2.0,
            "elapsed_ms": None,
            "epoch": 0,
        }, source)
        self.assertEqual([translation, source], history.snapshot())

    def test_segment_and_advice_rings_are_bounded(self) -> None:
        segments = SegmentHistory()
        advice = AdviceHistory()

        # 每流各自 60 上限：单侧洪水不挤占另一条泳道。
        for index in range(65):
            segments.add(stream="source", text=f"source-{index}", t=float(index))
        for index in range(70):
            segments.add(
                stream="translation",
                text=f"translation-{index}",
                t=float(index),
            )
        # 建议上限 10→50（阶段 E）：advice 天然低频，50 条≈一整场会。
        for index in range(55):
            advice.add(f"advice-{index}", timestamp=float(index + 1))

        self.assertEqual(120, len(segments))
        snapshot = segments.snapshot()
        source_texts = [r["text"] for r in snapshot if r["stream"] == "source"]
        translation_texts = [
            r["text"] for r in snapshot if r["stream"] == "translation"
        ]
        self.assertEqual(60, len(source_texts))
        self.assertEqual("source-5", source_texts[0])
        self.assertEqual(60, len(translation_texts))
        self.assertEqual("translation-10", translation_texts[0])
        self.assertEqual(50, len(advice.snapshot()))
        self.assertEqual("advice-5", advice.snapshot()[0]["markdown"])

    def test_ws_message_shapes_are_stable(self) -> None:
        record = SegmentHistory().add(
            stream="source",
            text="原文。",
            t=123.0,
            elapsed_ms=4500,
            epoch=1,
        )
        segment_message = {"type": "segment", **record}
        advice_message = AdviceHistory().add(
            "建议: 保持观望",
            timestamp=456.0,
        )

        self.assertEqual({
            "type": "segment",
            "id": 0,
            "stream": "source",
            "text": "原文。",
            "t": 123.0,
            "elapsed_ms": 4500,
            "epoch": 1,
        }, segment_message)
        self.assertEqual({
            "type": "advice",
            "markdown": "建议: 保持观望",
            "t": 456.0,
        }, advice_message)
        # 草稿是独立消息类型：无 id（不 append-only、后条取代前条）、无
        # t/elapsed_ms（可变中间态没有可信时间戳）；空串=清除灰字也是合法值。
        self.assertEqual({
            "type": "segment_draft",
            "stream": "source",
            "text": "皆さん",
            "epoch": 1,
        }, draft_broadcast_payload("source", "皆さん", 1))
        self.assertEqual(
            {"type": "segment_draft", "stream": "translation", "text": "",
             "epoch": 0},
            draft_broadcast_payload("translation", "", 0),
        )
        with self.assertRaises(ValueError):
            draft_broadcast_payload("mystery", "文本", 0)


class HistoryEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_endpoint_returns_segments_without_legacy_sentences(
        self,
    ) -> None:
        segments = SegmentHistory()
        segments.add(stream="source", text="原文。", t=1.0, elapsed_ms=1200)
        segments.add(stream="translation", text="译文。", t=2.0, epoch=0)
        advice = AdviceHistory()
        advice.add("建议: 追问期限", timestamp=3.0)
        request = SimpleNamespace(
            query={"t": "secret"},
            app={
                "token": "secret",
                "segment_history": segments,
                "advice_history": advice,
            },
        )

        response = await _history(request)
        payload = json.loads(response.text)

        self.assertEqual(200, response.status)
        self.assertEqual(2, payload["history_format"])
        self.assertEqual(
            [("source", "原文。"), ("translation", "译文。")],
            [(item["stream"], item["text"]) for item in payload["segments"]],
        )
        self.assertEqual([0, 1], [item["id"] for item in payload["segments"]])
        self.assertEqual("建议: 追问期限", payload["advice"][0]["markdown"])
        # sentences 兼容字段已删：唯一老客户端（KVM 页面会议音频面板）已随
        # 2026-07-31 产品拆分移除，字段不得再出现。
        self.assertNotIn("sentences", payload)
        self.assertEqual(
            "*",
            response.headers["Access-Control-Allow-Origin"],
        )

    async def test_interpret_voice_post_uses_server_authoritative_state(self) -> None:
        interpreter_state = InterpreterState(enabled=True, lang="zh")

        class Controller:
            async def set_voice_enabled(self, enabled: bool) -> bool:
                interpreter_state.set_interpret_voice(enabled)
                return True

        app = {
            "token": "secret",
            "interpreter": Controller(),
            "interpreter_state": interpreter_state,
            "runtime_state": BridgeRuntimeState(),
            "bus": SimpleNamespace(dropped_by_tap=lambda: {}),
            "uplink": SimpleNamespace(
                received_frames=0,
                backlog_samples=0,
                enabled=False,
            ),
            "muted": False,
            "clients": set(),
        }
        request = SimpleNamespace(
            query={"t": "secret"},
            app=app,
            json=AsyncMock(return_value={"enabled": True}),
        )

        response = await _interpret_voice(request)
        payload = json.loads(response.text)

        self.assertEqual(200, response.status)
        self.assertTrue(payload["interpreter"]["interpret_voice"])
        self.assertTrue(payload["interpret_voice_applied"])


class VoiceModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_false_discards_delta_and_never_starts_player(self) -> None:
        player = SpyPlayer()
        client = RealtimeInterpreter(
            Tap("interpreter"),
            player,
            api_key="mock-key",
            safety_identifier="mock-user",
        )

        client.handle_server_event({
            "type": "session.output_audio.delta",
            "delta": base64.b64encode(b"\x00\x00").decode("ascii"),
        })

        self.assertEqual(0, player.started)
        self.assertEqual([], player.chunks)

    async def test_voice_toggle_starts_feeds_and_stops_player(self) -> None:
        player = SpyPlayer()
        state = InterpreterState(enabled=True, lang="zh")
        client = RealtimeInterpreter(
            Tap("interpreter"),
            player,
            api_key="mock-key",
            safety_identifier="mock-user",
            state=state,
        )

        self.assertTrue(await client.set_voice_enabled(True))
        client.handle_server_event({
            "type": "session.output_audio.delta",
            "delta": base64.b64encode(b"\x00\x00").decode("ascii"),
        })
        self.assertTrue(await client.set_voice_enabled(False))

        self.assertEqual(1, player.started)
        self.assertEqual([b"\x00\x00"], player.chunks)
        self.assertEqual(1, player.stopped)
        self.assertFalse(state.snapshot()["interpret_voice"])

    async def test_player_start_failure_alerts_without_dropping_session(self) -> None:
        player = SpyPlayer(fail_start=True)
        alerts: list[str] = []
        state = InterpreterState(enabled=True, lang="zh")
        state.set_connected(True)
        client = RealtimeInterpreter(
            Tap("interpreter"),
            player,
            api_key="mock-key",
            safety_identifier="mock-user",
            state=state,
            on_error=alerts.append,
        )

        applied = await client.set_voice_enabled(True)
        snapshot = state.snapshot()

        self.assertFalse(applied)
        self.assertTrue(snapshot["connected"])
        self.assertFalse(snapshot["interpret_voice"])
        self.assertIn("mock player unavailable", snapshot["error"])
        self.assertEqual(1, len(alerts))


class VadGateTests(unittest.TestCase):
    def test_silence_enters_gate_and_voice_resumes_immediately(self) -> None:
        gate = SilenceVadGate(
            threshold_dbfs=-50.0,
            hold_seconds=3.0,
            samplerate=10,
        )
        state = InterpreterState(enabled=True, lang="zh")
        silence = np.zeros(15, dtype=np.float32)

        state.set_gated(gate.feed(silence))
        self.assertFalse(state.snapshot()["gated"])
        state.set_gated(gate.feed(silence))
        self.assertTrue(state.snapshot()["gated"])

        voice = np.full(1, 0.01, dtype=np.float32)
        state.set_gated(gate.feed(voice))
        self.assertFalse(state.snapshot()["gated"])
        self.assertEqual(0, gate.silent_samples)

    def test_threshold_boundary_is_strictly_below_and_off_disables_gate(
        self,
    ) -> None:
        amplitude = np.float32(10.0 ** (-50.0 / 20.0))
        just_below = np.nextafter(amplitude, np.float32(0.0))
        just_above = np.nextafter(amplitude, np.float32(1.0))
        gate = SilenceVadGate(
            threshold_dbfs=-50.0,
            hold_seconds=1.0,
            samplerate=4,
        )

        self.assertTrue(gate.feed(np.full(4, just_below, dtype=np.float32)))
        self.assertFalse(gate.feed(np.full(4, just_above, dtype=np.float32)))

        disabled = SilenceVadGate(
            threshold_dbfs=None,
            hold_seconds=1.0,
            samplerate=4,
        )
        self.assertFalse(disabled.feed(np.zeros(40, dtype=np.float32)))


class AdvisorBoundaryTests(unittest.TestCase):
    def test_advisor_receives_source_only_never_translation(self) -> None:
        # 硬产品边界（阶段 C 加强版）：任何 stream=="translation" 的 Segment
        # ——无论文本多像日语原文、elapsed_ms/epoch 是什么——永不进 advisor。
        advisor = Mock()

        _route_sentence_to_advisor(advisor, Segment(
            stream="source",
            text="納期は来週です。",
            elapsed_ms=1200,
            epoch=0,
        ))
        for translation in (
            Segment(stream="translation", text="交期是下周。", elapsed_ms=1300, epoch=0),
            # 译文流偶发原样回显日语（引用/专名）：仍然是译文流，禁止进参谋。
            Segment(stream="translation", text="納期は来週です。", elapsed_ms=None, epoch=1),
            Segment(stream="translation", text="（观望）", elapsed_ms=0, epoch=2),
        ):
            _route_sentence_to_advisor(advisor, translation)

        advisor.on_sentence.assert_called_once_with("納期は来週です。")

    def test_unknown_stream_fails_closed_without_touching_advisor(self) -> None:
        advisor = Mock()

        with self.assertRaises(ValueError):
            _route_sentence_to_advisor(advisor, Segment(
                stream="mystery",  # type: ignore[arg-type]
                text="未知流的文本。",
                elapsed_ms=None,
                epoch=0,
            ))

        advisor.on_sentence.assert_not_called()

    def test_advisor_keeps_hotword_context_and_uses_plain_japanese_text(
        self,
    ) -> None:
        cfg = GatewayConfig()
        cfg.advisor_backend = "claude"
        sink = Mock()
        with patch("audio_gateway.advisor._ClaudeBrain") as brain_type:
            brain_type.return_value.advise.return_value = "（观望）"
            advisor = Advisor(cfg, sink)
            advisor._append("契約条件を確認します。")
            advisor._new_since_call = 1
            self.assertTrue(advisor._should_call("納期は来週です。"))
            advisor._call("価格は百万円です。")

        prompt = brain_type.return_value.advise.call_args.args[0]
        self.assertIn("最近会议日语原文", prompt)
        self.assertIn("価格は百万円です。", prompt)

    def test_callback_sink_delivers_without_ui_dependency(self) -> None:
        received: list[str] = []
        sink = CallbackAdvisorSink(received.append)

        sink.post("建议: 先确认期限")

        self.assertEqual(["建议: 先确认期限"], received)


class AdvisorGlanceabilityTests(unittest.TestCase):
    """一眼可扫（2026-08-07 用户裁定）：会中的人只有一瞥的注意力。

    契约=最多两行（要点/话术），由代码执行；重复建议 3 分钟冷却。
    """

    def test_contract_lines_survive_and_everything_else_is_dropped(self) -> None:
        # 模型若退回旧四段格式，契约外的行必须被代码丢掉。
        out = condense_advice(
            "局势: 正在谈交期\n"
            "要点: ⚠ 对方要的交期早两周\n"
            "话术: 納期は持ち帰って確認します。（交期带回确认）\n"
            "风险: 对方在压价"
        )
        self.assertEqual(
            "⚠ 对方要的交期早两周\n"
            "话术: 納期は持ち帰って確認します。（交期带回确认）",
            out,
        )

    def test_long_point_is_truncated_and_long_script_is_dropped(self) -> None:
        # 要点超限截断（标题被剪仍是标题）；话术真失控（剥掉中文对照后
        # 纯日语仍超限）才整行丢弃——绝不截半句（半句话术照着念出口
        # 比没有更危险，与 max_tokens 截断同一裁定）。
        out = condense_advice(
            f"要点: {'长' * 60}\n话术: {'あ' * 200}"
        )
        self.assertIsNotNone(out)
        lines = out.splitlines()
        self.assertEqual(1, len(lines))
        self.assertEqual("长" * ADVICE_POINT_MAX_CHARS + "…", lines[0])

    def test_overlong_script_sheds_gloss_to_keep_the_speakable_part(self) -> None:
        # 面试对抗实锤：模型给的好话术（日语原话+中文对照 135 字）曾被
        # 旧 80 字闸静默砍掉——话术是念的不是读的。超限时先剥（中文
        # 对照）保住可念的日语，只有纯日语仍超限才丢。
        speakable = "は" * 100
        out = condense_advice(
            f"要点: 报年限与技术栈\n话术: {speakable}（{'长' * 100}）"
        )
        self.assertEqual(
            f"报年限与技术栈\n话术: {speakable}", out
        )
        # 正常体量（≤160 字含对照）原样保留
        kept = "御社が第一志望です。（贵司是第一志望）"
        out2 = condense_advice(f"要点: 别承诺独家\n话术: {kept}")
        self.assertEqual(f"别承诺独家\n话术: {kept}", out2)

    def test_freeform_output_falls_back_to_first_line_only(self) -> None:
        # 模型完全不守格式时：第一行非空文本兜底成要点，永远最多两行。
        out = condense_advice("对方刚抛出新报价\n然后是三段分析…\n再来一段")
        self.assertEqual("对方刚抛出新报价", out)

    def test_empty_output_condenses_to_none(self) -> None:
        self.assertIsNone(condense_advice("   \n  \n"))

    def test_repeat_advice_is_suppressed_within_cooldown(self) -> None:
        # 模型每 25s 重看一遍上下文，很容易把同一提醒再说一遍——
        # 重复冷却由代码拦截：同一建议 3 分钟内只上屏一次，窗口过后放行。
        cfg = GatewayConfig()
        cfg.advisor_backend = "claude"
        state = AdvisorState(enabled=True)
        sink = Mock()
        brain = Mock()
        brain.advise.return_value = "要点: 可按阶梯价口径回应"
        brain.last_stop_reason = "end_turn"
        with patch("audio_gateway.advisor._ClaudeBrain") as brain_type:
            brain_type.return_value = brain
            advisor = Advisor(cfg, sink, state=state)
        clock = {"now": 1000.0}
        with patch(
            "audio_gateway.advisor.time.monotonic",
            side_effect=lambda: clock["now"],
        ):
            advisor._call("価格の根拠を教えてください")
            clock["now"] += 30.0
            advisor._call("価格の根拠をもう一度")
            self.assertEqual(1, sink.post.call_count)
            self.assertEqual(1, state.snapshot()["suppressed"])
            clock["now"] += 200.0  # 冷却窗（180s）已过
            advisor._call("価格の根拠は？")
        self.assertEqual(2, sink.post.call_count)
        self.assertEqual(2, state.snapshot()["delivered"])

    def test_direct_questions_trigger_immediately_with_cooldown(self) -> None:
        # 基础功能（2026-08-07 用户裁定）：回复提示的价值窗口是对方问完
        # 的那几秒。提问/请求句（か/？/ください/お願いします，句读位置
        # 不限）绕过常规节流即刻触发；冷却 3s 只挡 ASR 拆句重复——
        # 面试对抗实锤：8s 冷却会把连续提问成批吞掉。陈述句走常规节流。
        cfg = GatewayConfig()
        cfg.advisor_backend = "claude"
        with patch("audio_gateway.advisor._ClaudeBrain"):
            advisor = Advisor(cfg, Mock())
        clock = {"now": 1000.0}
        with patch(
            "audio_gateway.advisor.time.monotonic",
            side_effect=lambda: clock["now"],
        ):
            advisor._new_since_call = 1  # 低于常规节流的最少新句数
            self.assertTrue(
                advisor._should_call("進捗状況を教えていただけますか。")
            )
            self.assertTrue(advisor._should_call("進捗はどう?"))
            self.assertTrue(
                advisor._should_call("レポートを本日中にお送りください。")
            )
            # 面试第一题的请求形（对抗第一轮曾整题静默）
            self.assertTrue(
                advisor._should_call("簡単に自己紹介をお願いします。")
            )
            # ASR 并句：问句在句中也要触发
            self.assertTrue(advisor._should_call(
                "希望年収を教えてください。ちなみに予算は450万円程度ですが。"
            ))
            # 陈述句：不即触发，走常规节流
            self.assertFalse(advisor._should_call("承知しました。"))
            # 3s 冷却：窗口内不触发（挡拆句重复），过窗即放行（连续提问）
            advisor._last_call_t = clock["now"]
            self.assertFalse(
                advisor._should_call("次回は水曜でよろしいですか。")
            )
            clock["now"] += 3.5
            self.assertTrue(
                advisor._should_call("次回は水曜でよろしいですか。")
            )

    def test_ask_in_cooldown_shadow_fires_after_cooldown(self) -> None:
        # 模拟面试全链路实锤：寒暄句抢走触发后，紧随的真正请求句
        # 落进冷却阴影被整题丢弃（面试第一题零卡片）。规格：被冷却
        # 压住的触发句挂起，冷却到期由工作者补触发。
        cfg = GatewayConfig()
        cfg.advisor_backend = "claude"
        brain = Mock()
        brain.advise.return_value = "（观望）"
        brain.last_stop_reason = "end_turn"
        with patch("audio_gateway.advisor._ClaudeBrain") as brain_type:
            brain_type.return_value = brain
            advisor = Advisor(cfg, Mock())
        with patch("audio_gateway.advisor._ASK_COOLDOWN_S", 0.3):
            advisor.start()
            advisor.on_sentence("本日はよろしくお願いします。")
            deadline = time.monotonic() + 2.0
            while brain.advise.call_count < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(1, brain.advise.call_count)
            # 冷却阴影内的第二个请求句：不丢，0.3s 后补触发
            advisor.on_sentence("まず、簡単に自己紹介をお願いします。")
            deadline = time.monotonic() + 3.0
            while brain.advise.call_count < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            advisor.stop()
        self.assertEqual(2, brain.advise.call_count)
        second_prompt = brain.advise.call_args_list[1].args[0]
        self.assertIn("自己紹介", second_prompt)

    def test_cyrillic_anomaly_is_retried_then_dropped(self) -> None:
        # 30 轮终考实锤：话术里出现「триつ目」（西里尔字母），照着念
        # 会当场卡壳。规格：检出异常文字→补问一枪；两次都异常→整卡
        # 放弃（宁缺毋滥）；补问干净则投递补问结果。
        cfg = GatewayConfig()
        cfg.advisor_backend = "claude"
        state = AdvisorState(enabled=True)
        sink = Mock()
        brain = Mock()
        brain.advise.side_effect = [
            "要点: 说三个轴\n话术: триつ目は金融分野です。",
            "要点: 说三个轴\n话术: 三つ目は金融分野です。",
        ]
        brain.last_stop_reason = "end_turn"
        with patch("audio_gateway.advisor._ClaudeBrain") as brain_type:
            brain_type.return_value = brain
            advisor = Advisor(cfg, sink, state=state)
        advisor._call("転職の軸を教えてください。")
        self.assertEqual(2, brain.advise.call_count)
        delivered = sink.post.call_args.args[0]
        self.assertIn("三つ目は金融分野です。", delivered)
        self.assertNotIn("три", delivered)

        # 两次都异常：整卡放弃，计入 suppressed
        sink.reset_mock()
        brain.advise.side_effect = [
            "要点: 说轴\n话术: триつ目です。",
            "要点: 说轴\n话术: 두 번째です。",
        ]
        advisor._call("軸をもう一度教えてください。")
        sink.post.assert_not_called()
        self.assertEqual(1, state.snapshot()["delivered"])

    def test_missing_script_on_a_question_triggers_one_retry(self) -> None:
        # 提问必须有话术（基础职责）；模型偶发只给要点时由代码补一枪，
        # 拿到话术用补问结果；非提问触发的卡不补问。
        cfg = GatewayConfig()
        cfg.advisor_backend = "claude"
        state = AdvisorState(enabled=True)
        sink = Mock()
        brain = Mock()
        brain.advise.side_effect = [
            "要点: 用对账引擎举证",
            "要点: 用对账引擎举证\n话术: 直近では消込エンジンを実装しました。（最近实现了对账引擎）",
        ]
        brain.last_stop_reason = "end_turn"
        with patch("audio_gateway.advisor._ClaudeBrain") as brain_type:
            brain_type.return_value = brain
            advisor = Advisor(cfg, sink, state=state)

        advisor._call("ご自身で実装された部分はありますか。")

        self.assertEqual(2, brain.advise.call_count)
        self.assertIn("话术行缺失", brain.advise.call_args.args[0])
        delivered = sink.post.call_args.args[0]
        self.assertIn("话术: 直近では消込エンジンを実装しました。", delivered)
        self.assertEqual(2, state.snapshot()["calls"])

        # 陈述句触发（常规节流路径）缺话术不补问
        brain.advise.side_effect = ["要点: 对方在赶进度，可主动汇报风险"]
        advisor._call("スケジュールが厳しくなってきました。")
        self.assertEqual(3, brain.advise.call_count)

    def test_delivered_advice_is_fed_back_into_the_prompt(self) -> None:
        # 换措辞的同义重复（实测相似度仅 0.56）代码闸拦不住——只有模型
        # 看得懂"同一个意思"，但它必须先看到自己说过什么。规格：上过屏
        # 的建议原文回灌进下一次调用的提示词。
        cfg = GatewayConfig()
        cfg.advisor_backend = "claude"
        sink = Mock()
        brain = Mock()
        brain.advise.return_value = "要点: ⚠ 别当场答应交期前倒"
        brain.last_stop_reason = "end_turn"
        with patch("audio_gateway.advisor._ClaudeBrain") as brain_type:
            brain_type.return_value = brain
            advisor = Advisor(cfg, sink)

        advisor._call("納品を前倒しできませんか")
        first_prompt = brain.advise.call_args.args[0]
        self.assertNotIn("你最近已提示过的内容", first_prompt)

        brain.advise.return_value = "（观望）"
        advisor._call("前倒しの件、いかがでしょうか")
        second_prompt = brain.advise.call_args.args[0]
        self.assertIn("你最近已提示过的内容", second_prompt)
        self.assertIn("⚠ 别当场答应交期前倒", second_prompt)


class AdvisorReliabilityTests(unittest.TestCase):
    """阶段 E-1/E-2/E-3：失败退避、观望抑制、可观测状态、brief 热重载。"""

    def _advisor(
        self,
        brain: Mock,
        *,
        state: AdvisorState | None = None,
        on_alert=None,
        cfg: "GatewayConfig | None" = None,
    ) -> tuple[Advisor, Mock]:
        if cfg is None:
            cfg = GatewayConfig()
        cfg.advisor_backend = "claude"
        sink = Mock()
        with patch("audio_gateway.advisor._ClaudeBrain") as brain_type:
            brain_type.return_value = brain
            advisor = Advisor(cfg, sink, state=state, on_alert=on_alert)
        return advisor, sink

    def test_persistent_failures_back_off_instead_of_calling_per_sentence(
        self,
    ) -> None:
        # 成本炸弹回归测试：真实事故是持续失败时 163 句会议吃了 163 次 API
        # 调用（失败不推进节流时钟）。修复后：失败推进时钟 + 指数退避，
        # 1 句/秒喂满 163 句最多只允许个位数调用。
        state = AdvisorState(enabled=True)
        brain = Mock()
        brain.advise.side_effect = RuntimeError("overloaded_error (529)")
        advisor, sink = self._advisor(brain, state=state)
        clock = {"now": 0.0}

        with patch(
            "audio_gateway.advisor.time.monotonic",
            side_effect=lambda: clock["now"],
        ):
            for index in range(163):
                clock["now"] = float(index)
                sentence = f"第{index}句の発言です。"
                advisor._append(sentence)
                advisor._new_since_call += 1
                if advisor._should_call(sentence):
                    advisor._call(sentence)

        self.assertLessEqual(brain.advise.call_count, 6)
        # 退避不是熔断：窗口过后仍会再试，绝不能只调用一次就永远沉默。
        self.assertGreaterEqual(brain.advise.call_count, 2)
        snap = state.snapshot()
        self.assertEqual(brain.advise.call_count, snap["calls"])
        self.assertEqual(0, snap["delivered"])
        self.assertIn("overloaded_error", snap["last_error"])
        self.assertIsNotNone(snap["backoff_until"])
        sink.post.assert_not_called()

    def test_watch_variants_are_suppressed_and_counted(self) -> None:
        # 「（观望）」与「（观望）。」都必须被抑制：旧判据漏判带句号的变体，
        # 把裸"观望"卡片推给了用户。
        state = AdvisorState(enabled=True)
        brain = Mock()
        brain.advise.side_effect = ["（观望）", "（观望）。"]
        brain.last_stop_reason = "end_turn"
        advisor, sink = self._advisor(brain, state=state)

        advisor._append("契約条件を確認します。")
        advisor._call("納期は来週です。")
        advisor._call("価格は百万円です。")

        snap = state.snapshot()
        self.assertEqual(2, snap["calls"])
        self.assertEqual(2, snap["suppressed"])
        self.assertEqual(0, snap["delivered"])
        self.assertIsNone(snap["last_error"])
        sink.post.assert_not_called()

    def test_failure_raises_alert_and_success_clears_it(self) -> None:
        # 告警必须可清除：一次瞬时 429 不能让菜单栏永久橙色。
        state = AdvisorState(enabled=True)
        alerts: list[str | None] = []
        brain = Mock()
        brain.advise.side_effect = [
            RuntimeError("boom"),
            "局势: 对方给出期限\n建议: 先确认口径",
        ]
        brain.last_stop_reason = "end_turn"
        advisor, sink = self._advisor(brain, state=state, on_alert=alerts.append)

        advisor._append("納期は明日です。")
        advisor._call("納期は明日です。")
        self.assertEqual(1, len(alerts))
        # 中文前缀"参谋:"：Swift 按它映射中文 guidance，且 CJK 在 sorted()
        # 里排在 ASCII 之后，不会把 interpreter: 字幕告警挤出 first。
        self.assertTrue(alerts[0].startswith("参谋:"))

        advisor._backoff_until = 0.0  # 跳过退避窗，直接验证恢复路径
        advisor._call("納期は明日です。")

        self.assertEqual([alerts[0], None], alerts)
        snap = state.snapshot()
        self.assertIsNone(snap["last_error"])
        self.assertIsNone(snap["backoff_until"])
        self.assertEqual(1, snap["delivered"])
        sink.post.assert_called_once()

    def test_error_text_is_redacted_before_entering_state(self) -> None:
        with patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "sk-ant-secret123"},
            clear=False,
        ):
            state = AdvisorState(enabled=True)
            brain = Mock()
            brain.advise.side_effect = RuntimeError(
                "401 unauthorized for key sk-ant-secret123"
            )
            # secrets 在构造期采集，必须在 patch 环境里构造。
            advisor, _ = self._advisor(brain, state=state)
            advisor._call("価格の件です。")

        snap = state.snapshot()
        self.assertNotIn("sk-ant-secret123", snap["last_error"])
        self.assertIn("[REDACTED]", snap["last_error"])

    def test_max_tokens_truncation_is_accounted_without_backoff(self) -> None:
        # stop_reason=max_tokens 单独记账：不是传输故障（不退避），
        # 截断建议已在 brain 层丢弃、绝不上屏。
        state = AdvisorState(enabled=True)
        brain = Mock()
        brain.advise.return_value = None
        brain.last_stop_reason = "max_tokens"
        advisor, sink = self._advisor(brain, state=state)

        advisor._call("価格は百万円です。")

        snap = state.snapshot()
        self.assertEqual(1, snap["calls"])
        self.assertEqual(1, snap["suppressed"])
        self.assertIn("max_tokens", snap["last_error"])
        self.assertIsNone(snap["backoff_until"])
        self.assertEqual(0.0, advisor._backoff_until)
        sink.post.assert_not_called()

    def test_brief_reloads_on_mtime_change_before_next_call(self) -> None:
        # E-3：会中改 brief 无需重启（14:18 那场会 5/5 条建议被 10:53 写的
        # 旧 brief 拽偏——AWS 迁移报价 vs 实际在定卡拉OK日程）。
        with tempfile.TemporaryDirectory() as tmp:
            brief = Path(tmp) / "brief.md"
            brief.write_text("目标A：谈定二期报价", encoding="utf-8")
            cfg = GatewayConfig()
            cfg.advisor_brief = str(brief)
            brain = Mock()
            brain.advise.return_value = "（观望）"
            brain.last_stop_reason = "end_turn"
            advisor, _ = self._advisor(brain, cfg=cfg)

            brief.write_text("目标B：确认日程安排", encoding="utf-8")
            # 强制 mtime 前移：低分辨率文件系统上连续两次写可能同 mtime。
            future = time.time() + 5
            os.utime(brief, (future, future))
            advisor._call("10時はいかがですか")

        # brief 经 kwargs 进 brain（Claude 后端放进带缓存的 system 块），
        # 不再拼进 user prompt。
        brief_kw = brain.advise.call_args.kwargs["brief"]
        self.assertIn("目标B：确认日程安排", brief_kw)
        self.assertNotIn("目标A", brief_kw)
        self.assertNotIn("目标A", brain.advise.call_args.args[0])

    def test_brief_mismatch_marker_is_stripped_and_surfaced_in_state(
        self,
    ) -> None:
        # 模型是唯一零成本的错配检测器（每次调用都同时读 brief 与转写）；
        # 标记只进 advisor_state 供面板显示，不进用户看到的建议正文。
        state = AdvisorState(enabled=True)
        brain = Mock()
        brain.advise.return_value = (
            "（背景不符）\n要点: 可顺势确认时间"
        )
        brain.last_stop_reason = "end_turn"
        advisor, sink = self._advisor(brain, state=state)

        advisor._call("10時はいかがですか")

        self.assertTrue(state.snapshot()["brief_mismatch"])
        delivered = sink.post.call_args.args[0]
        self.assertNotIn("（背景不符）", delivered)
        self.assertIn("可顺势确认时间", delivered)

    def test_claude_brain_reports_stop_reason_and_drops_truncated_text(
        self,
    ) -> None:
        # anthropic 不在开发依赖里：以假模块注入，验证 stop_reason 分账与
        # max_tokens 上限（思考+正文共用，1024 已实测会截断）。
        fake_anthropic = ModuleType("anthropic")
        fake_anthropic.Anthropic = Mock()  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            brain = _ClaudeBrain("claude-opus-5")

        client = brain._client
        client.beta.messages.create.return_value = SimpleNamespace(
            stop_reason="max_tokens",
            content=[SimpleNamespace(type="text", text="半截建议")],
        )
        self.assertIsNone(brain.advise("p", brief="面试背景：应聘后端岗"))
        # brief 进 system 缓存块（简历/JD 体量大且整场不变）：规则块与
        # 背景块分开打 cache_control，热重载 brief 不失效规则块缓存。
        system = client.beta.messages.create.call_args.kwargs["system"]
        self.assertEqual(2, len(system))
        self.assertEqual({"type": "ephemeral"}, system[0]["cache_control"])
        self.assertEqual({"type": "ephemeral"}, system[1]["cache_control"])
        self.assertIn("面试背景：应聘后端岗", system[1]["text"])
        self.assertEqual("max_tokens", brain.last_stop_reason)

        client.beta.messages.create.return_value = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=" 建议: ok ")],
        )
        self.assertEqual("建议: ok", brain.advise("p"))
        self.assertEqual("end_turn", brain.last_stop_reason)
        kwargs = client.beta.messages.create.call_args.kwargs
        self.assertGreaterEqual(kwargs["max_tokens"], 2048)
        # 不传 thinking={"type":"disabled"}：官方文档明示该组合会把 <thinking>
        # 标签漏进正文，对直接上屏的建议卡片不可接受。
        self.assertNotIn("thinking", kwargs)


class AdviceLogTests(unittest.TestCase):
    def test_append_writes_jsonl_and_close_drops_late_writes(self) -> None:
        # advisor.stop() join 超时后守护线程仍可能投递；close() 之后的写入
        # 必须丢弃，保证 _collect_artifacts 收集后文件不再变化。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "advice.jsonl"
            log = AdviceLog(path)
            log.append({"type": "advice", "markdown": "建议A", "t": 1.5})
            log.close()
            log.append({"type": "advice", "markdown": "迟到的建议", "t": 2.5})

            lines = path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(1, len(lines))
        self.assertEqual({"t": 1.5, "markdown": "建议A"}, json.loads(lines[0]))

    def test_file_is_created_lazily_on_first_advice(self) -> None:
        # 零建议的会议不该多出一个空 advice.jsonl 混进产物清单。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "advice.jsonl"
            AdviceLog(path)
            self.assertFalse(path.exists())


class InMeetingCliTests(unittest.TestCase):
    def test_vad_off_parser_and_defaults(self) -> None:
        defaults = main.build_parser().parse_args(["bridge"])
        disabled = main.build_parser().parse_args([
            "bridge",
            "--interpret-vad-dbfs",
            "off",
        ])

        self.assertEqual(-50.0, defaults.interpret_vad_dbfs)
        self.assertIsNone(disabled.interpret_vad_dbfs)
        self.assertFalse(defaults.advise)

    def test_advise_requires_interpreter_before_doctor(self) -> None:
        args = main.build_parser().parse_args(["bridge", "--advise"])

        with patch(
            "audio_gateway.main._startup_doctor",
            return_value=0,
        ) as doctor:
            code = main.cmd_bridge(args)

        self.assertEqual(2, code)
        doctor.assert_not_called()

    def test_advise_without_anthropic_credential_fails_before_doctor(
        self,
    ) -> None:
        args = main.build_parser().parse_args([
            "bridge",
            "--interpret",
            "--advise",
            "--monitor",
            "Mac mini speaker",
        ])

        with (
            patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "mock-openai-key"},
                clear=True,
            ),
            patch(
                "audio_gateway.main._startup_doctor",
                return_value=0,
            ) as doctor,
        ):
            code = main.cmd_bridge(args)

        self.assertEqual(2, code)
        doctor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
