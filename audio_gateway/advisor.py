"""参谋模块：滚动转写上下文 → LLM 实时决策建议 → 私密浮窗（只在 Mac mini 屏幕上）。

设计要点：
- 建议对延迟不敏感（字幕慢 2s 难受，建议慢 10s 无所谓），走独立线程与节流策略；
- 触发 = 命中热词（报价/期限/契约…）立即触发，否则按 最小间隔+最少新段数 节流；
- 建议永远只进操作者的眼睛：绝不经 Mac_Out 出声，说不说由人决定。
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from .postmeeting import redact_error

# 命中即触发（可用 --advisor-hotwords 追加）
DEFAULT_HOTWORDS = (
    "価格", "金額", "見積", "納期", "契約", "期限", "御社", "決定",
    "报价", "价格", "多少钱", "合同", "期限", "决定", "方案",
    "price", "cost", "deadline", "contract", "budget", "decision",
)

_HOTWORD_COOLDOWN_S = 8.0

# E-1 失败退避：8s 起指数翻倍，封顶 300s。真实事故背书：持续失败时旧实现
# 每句一次 API（163 句会议 ≈163 次调用 ≈$6），且用户全程无感。
_BACKOFF_BASE_S = 8.0
_BACKOFF_MAX_S = 300.0

# E-3 错配提示的输出标记：模型每次调用都同时看到 brief 与滚动转写，是唯一
# 能以零额外成本判断两者是否相关的组件——不再造脆弱的本地文本分类器
#（本仓已有 IME 视觉分类器三轮翻车的前科）。
_BRIEF_MISMATCH_MARK = "（背景不符）"

_SYSTEM = """\
你是我方的实时会议参谋，输出只有我一个人看到。输入是会议的日语原文转写，不是译文。\
基于【会议背景与目标】与滚动的日语原文，\
给出简短、马上能用的建议。规则：
- 没有值得说的新信息时，只输出：（观望）
- 有建议时用以下 Markdown 格式，总长不超过 150 字：
局势: 一句话
建议: 1~2 条可执行动作
话术: 适合时给一句可直接说出口的原话（用会议语言，附中文对照）
风险: 发现对方抛出的数字/期限/承诺陷阱时点名，否则省略此行
- 若【会议背景与目标】与会议实际内容明显不符（例如背景写系统迁移报价、会议在谈日程安排），\
在第一行单独输出（背景不符），从第二行起照常输出（观望）或建议
- 转写含识别错误，按上下文理解；技术名词（AWS/Redis/API 等）按行业惯例纠正。"""

# 「观望」判定放宽：去两端标点后以"观望"开头且全文很短即视为无内容。
# 旧判据 strip("（）() \n") == "观望" 会漏判「（观望）。」，把裸观望卡片推给用户。
_WATCH_STRIP_CHARS = "（）()。．.,、，！!？?：: \t\r\n"


def _is_watch(advice: str) -> bool:
    text = advice.strip()
    if len(text) >= 12:
        # 长文本即使以"观望"开头也可能带着有价值的补充说明，不拦。
        return False
    return text.strip(_WATCH_STRIP_CHARS).startswith("观望")


class AdvisorState:
    """参谋线程与 /status 之间唯一的共享状态；照抄 BridgeRuntimeState 的带锁快照模式。

    参谋跑在自己的守护线程上，bridge 的 ``_state_payload`` 在事件循环线程读——
    裸 dict 并发读写没有一致性保证，所以所有修改与快照都过同一把锁。
    时间字段（last_call_t/last_advice_t/backoff_until）一律是 ``time.time()``
    墙钟秒，供前端换算"多久之前/还要退避多久"；与节流内部用的 monotonic 无关。
    """

    def __init__(
        self,
        *,
        enabled: bool,
        brief_source: str = "builtin",
        brief_path: str = "~/AudioGateway/brief.md",
    ) -> None:
        self._lock = threading.Lock()
        self._enabled = bool(enabled)
        self._brief_source = brief_source
        self._brief_path = brief_path
        self._calls = 0
        self._delivered = 0
        self._suppressed = 0
        self._last_call_t: float | None = None
        self._last_advice_t: float | None = None
        self._last_error: str | None = None
        self._backoff_until: float | None = None
        self._brief_mismatch = False

    def record_call(self) -> None:
        with self._lock:
            self._calls += 1
            self._last_call_t = time.time()

    def record_failure(self, *, error: str, backoff_seconds: float) -> None:
        with self._lock:
            self._last_error = error
            self._backoff_until = time.time() + backoff_seconds

    def record_success(self) -> None:
        # 成功即清除错误与退避：面板/告警都以此为"参谋恢复"的唯一信号。
        with self._lock:
            self._last_error = None
            self._backoff_until = None

    def record_suppressed(self) -> None:
        with self._lock:
            self._suppressed += 1

    def record_delivery(self) -> None:
        with self._lock:
            self._delivered += 1
            self._last_advice_t = time.time()

    def record_truncated(self) -> None:
        # stop_reason=max_tokens 单独记账：不是传输故障，不退避；
        # 截断的建议宁缺毋滥（半句话术比没有话术更危险），已被丢弃。
        with self._lock:
            self._suppressed += 1
            self._last_error = "上游 stop_reason=max_tokens，截断建议已丢弃"
            self._backoff_until = None

    def set_brief_mismatch(self, mismatch: bool) -> None:
        with self._lock:
            self._brief_mismatch = bool(mismatch)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "brief_source": self._brief_source,
                "brief_path": self._brief_path,
                "calls": self._calls,
                "delivered": self._delivered,
                "suppressed": self._suppressed,
                "last_call_t": self._last_call_t,
                "last_advice_t": self._last_advice_t,
                "last_error": self._last_error,
                "backoff_until": self._backoff_until,
                "brief_mismatch": self._brief_mismatch,
            }


class ConsoleAdvisorSink:
    def post(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        print(f"\n╭─ 参谋 {stamp} " + "─" * 40)
        for line in text.splitlines():
            print(f"│ {line}")
        print("╰" + "─" * 50)

    def close(self) -> None:
        pass


class CallbackAdvisorSink:
    """Deliver advice into an embedding product surface."""

    def __init__(self, callback: Callable[[str], None]) -> None:
        self._callback = callback

    def post(self, text: str) -> None:
        self._callback(text)

    def close(self) -> None:
        pass


class TkAdvisorWindow:
    """置顶私密建议窗。root=None 时自建 Tk 根（需由 main 跑 mainloop）；
    传入字幕窗的 root 时作为 Toplevel 挂靠，共用同一个 mainloop。"""

    def __init__(self, root=None, width: int = 420, height: int = 520):
        import tkinter as tk
        self._tk = tk
        self._q: queue.Queue = queue.Queue()
        self._closed = False
        self.owns_root = root is None

        host = tk.Tk() if self.owns_root else tk.Toplevel(root)
        host.title("参谋")
        host.attributes("-topmost", True)
        host.attributes("-alpha", 0.92)
        sw = host.winfo_screenwidth()
        host.geometry(f"{width}x{height}+{sw - width - 24}+80")
        host.configure(bg="#101418")
        self._text = tk.Text(
            host, bg="#101418", fg="#d7e3f0", insertbackground="#d7e3f0",
            font=("Helvetica", 13), wrap="word", bd=0, padx=12, pady=10,
        )
        self._text.pack(fill="both", expand=True)
        self._text.insert("1.0", "参谋就位，等待会议内容…\n")
        self._text.configure(state="disabled")
        self._host = host
        self._host.after(200, self._tick)  # Toplevel 挂靠时由父 root 的 mainloop 驱动

    def post(self, text: str) -> None:
        self._q.put(text)

    def _tick(self) -> None:
        try:
            while True:
                text = self._q.get_nowait()
                stamp = time.strftime("%H:%M:%S")
                self._text.configure(state="normal")
                self._text.insert("1.0", f"── {stamp} ──\n{text}\n\n")  # 最新置顶
                if len(self._text.get("1.0", "end")) > 12000:
                    self._text.delete("end-4000c", "end")
                self._text.configure(state="disabled")
        except queue.Empty:
            pass
        if not self._closed:
            self._host.after(200, self._tick)

    def run_mainloop(self, on_key=None) -> None:
        assert self.owns_root, "挂靠模式下由字幕窗的 mainloop 驱动"
        if on_key:
            self._host.bind("<Key>", lambda e: on_key(e.char))
        self._host.mainloop()

    def close(self) -> None:
        self._closed = True
        try:
            self._host.after(0, self._host.destroy)
        except Exception:
            pass


class _ClaudeBrain:
    # Opus 5 思考默认开启，max_tokens 同时封顶 thinking+正文。实测一条 ≤150 字
    # 建议消耗 346~507 output tokens，旧上限 1024 一旦思考偏长就触顶截断。
    # 裁定：不传 thinking={"type":"disabled"}（官方文档明示 disabled 有
    # <thinking> 标签漏进正文的已知故障模式，对直接上屏的建议卡片不可接受），
    # 而是保持自适应思考并把上限提到 4096——这是上限不是花费，正常调用照旧。
    _MAX_TOKENS = 4096

    def __init__(self, model: str):
        from anthropic import Anthropic
        self._client = Anthropic()
        self._model = model
        # 最近一次调用的 stop_reason，供 Advisor 对 max_tokens 单独记账。
        self.last_stop_reason: str | None = None

    def advise(self, prompt: str) -> str | None:
        self.last_stop_reason = None
        resp = self._client.beta.messages.create(
            model=self._model,
            max_tokens=self._MAX_TOKENS,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        self.last_stop_reason = resp.stop_reason
        if resp.stop_reason == "refusal":
            return None
        if resp.stop_reason == "max_tokens":
            # 截断的建议宁可不投递：半句"话术"直接说出口比没有更危险。
            return None
        return "".join(b.text for b in resp.content if b.type == "text").strip()


class _OllamaBrain:
    def __init__(self, url: str, model: str):
        self._client = httpx.Client(base_url=url, timeout=60.0)
        self._model = model

    def advise(self, prompt: str) -> str | None:
        resp = self._client.post("/api/generate", json={
            "model": self._model,
            "system": _SYSTEM,
            "prompt": prompt,
            "stream": False,
        })
        resp.raise_for_status()
        return resp.json()["response"].strip()


class _OpenAIBrain:
    def __init__(self, model: str, base_url: str):
        from .openai_client import OpenAIChatClient
        self._client = OpenAIChatClient(model, base_url, timeout=60.0)

    def advise(self, prompt: str) -> str | None:
        return self._client.complete(_SYSTEM, prompt, max_tokens=1024) or None


class Advisor:
    """消费纯文本句子，按既有节流策略调 LLM，建议投递到 sink。"""

    def __init__(
        self,
        cfg,
        sink,
        *,
        state: AdvisorState | None = None,
        on_alert: Callable[[str | None], None] | None = None,
    ):
        self._cfg = cfg
        self._sink = sink
        self._q: queue.Queue = queue.Queue()
        self._lines: list[str] = []          # 滚动转写窗口
        self._chars = 0
        self._new_since_call = 0
        # monotonic() 起点依平台而定（macOS 上接近 0），置为"很久以前"
        # 保证会议开头的热词/首次节流窗不被冷却期误杀
        self._last_call_t = float("-inf")
        self._thread: threading.Thread | None = None
        # E-1 失败退避（monotonic 内部时钟；对外快照走 AdvisorState 的墙钟）。
        self._fail_streak = 0
        self._backoff_until = 0.0
        # E-2 可观测性：bridge 传入共享 AdvisorState；legacy run/Tk 通道不传时
        # 自建一份（无人读取也无害），避免所有记录点都要判 None。
        self._state = state if state is not None else AdvisorState(
            enabled=True,
            brief_source="file" if cfg.advisor_brief else "builtin",
            brief_path=cfg.advisor_brief or "~/AudioGateway/brief.md",
        )
        self._on_alert = on_alert
        self._alert_active = False
        # 错误文本必须脱敏后才能进状态/日志（shutdown_secrets 同款约定）。
        self._secret_values = tuple(
            value.strip()
            for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
            if (value := os.environ.get(key, "")).strip()
        )

        self._brief = "（未提供，请按通用商务/技术会议理解）"
        # E-3 brief 热重载：记 mtime，每次 _call 前比对；构造期只有磁盘 I/O，
        # 绝不加网络调用——__init__ 在 recorder.start() 之前同步执行，
        # 一次慢 API 调用 = 会议开头静默丢音频。
        self._brief_path: Path | None = None
        self._brief_mtime: float | None = None
        if cfg.advisor_brief:
            self._brief_path = Path(cfg.advisor_brief)
            self._brief = self._brief_path.read_text(encoding="utf-8")
            try:
                self._brief_mtime = self._brief_path.stat().st_mtime
            except OSError:
                self._brief_mtime = None

        extra = tuple(w.strip() for w in cfg.advisor_hotwords.split(",") if w.strip())
        self._hotwords = DEFAULT_HOTWORDS + extra

        if cfg.advisor_backend == "claude":
            self._brain = _ClaudeBrain(cfg.claude_model)
        elif cfg.advisor_backend == "ollama":
            self._brain = _OllamaBrain(cfg.ollama_url, cfg.ollama_model)
        elif cfg.advisor_backend == "openai":
            self._brain = _OpenAIBrain(cfg.openai_model, cfg.openai_base_url)
        else:
            raise ValueError(f"未知参谋后端: {cfg.advisor_backend}")

    # interpreter / Transcriber 回调只入队，不在音频或 STT 线程做重活。
    def on_sentence(self, sentence: str) -> None:
        text = sentence.strip()
        if text:
            self._q.put(text)

    def on_segment(self, seg) -> None:
        """Compatibility adapter for the legacy run/Tk troubleshooting lane."""
        self.on_sentence(seg.text)

    def _append(self, sentence: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {sentence}"
        self._lines.append(line)
        self._chars += len(line)
        while self._chars > self._cfg.advisor_context_chars and len(self._lines) > 1:
            self._chars -= len(self._lines.pop(0))

    def _should_call(self, sentence: str) -> bool:
        now = time.monotonic()
        # 退避窗内一律不打 API：热词命中也不豁免——退避的对象是"上游在故障"，
        # 与句子多重要无关，豁免热词就等于把成本炸弹留了一条引线。
        if now < self._backoff_until:
            return False
        since = now - self._last_call_t
        if any(w in sentence for w in self._hotwords) and since >= _HOTWORD_COOLDOWN_S:
            return True
        return (self._new_since_call >= self._cfg.advisor_min_new_segments
                and since >= self._cfg.advisor_interval_s)

    def _maybe_reload_brief(self) -> None:
        """E-3 热重载：mtime 变了就重读，会中改 brief 无需重启。

        错 brief 的代价已实测（14:18 那场会 5/5 条建议全被上一场会的目标拽偏）。
        只做磁盘 stat/read，失败时沿用上一次内容——参谋不能因 brief 抖动挂掉。
        """
        if self._brief_path is None:
            return
        try:
            mtime = self._brief_path.stat().st_mtime
        except OSError:
            return  # 文件被移走/暂不可读：沿用旧内容，不制造新故障
        if mtime == self._brief_mtime:
            return
        try:
            self._brief = self._brief_path.read_text(encoding="utf-8")
        except OSError:
            return
        self._brief_mtime = mtime
        print("[advisor] brief 已变更，下次建议起使用新内容")

    def _set_alert(self, active: bool) -> None:
        # 只在状态翻转时上报：告警可清除（成功→None），且不给菜单栏刷屏。
        if self._on_alert is None or active == self._alert_active:
            return
        self._alert_active = active
        self._on_alert(
            "参谋: AI 建议暂时不可用（调用失败，自动退避重试中）"
            if active
            else None
        )

    def _extract_brief_mismatch(self, advice: str) -> str:
        """剥掉模型输出的（背景不符）标记并把判断写进状态。

        模型每次都同时读 brief 与转写，是唯一零成本的错配检测器；标记只进
        advisor_state 让面板可见，不进用户看到的建议正文。
        """
        text = advice.strip()
        if text.startswith(_BRIEF_MISMATCH_MARK):
            self._state.set_brief_mismatch(True)
            return text[len(_BRIEF_MISMATCH_MARK):].lstrip("：:，, \n")
        self._state.set_brief_mismatch(False)
        return text

    def _call(self, latest: str) -> None:
        self._maybe_reload_brief()
        prompt = (
            f"【会议背景与目标】\n{self._brief}\n\n"
            f"【最近会议日语原文】\n" + "\n".join(self._lines) +
            f"\n\n【最新日语原文发言】\n{latest}\n\n请按格式给出建议。"
        )
        self._state.record_call()
        try:
            advice = self._brain.advise(prompt)
        except Exception as e:
            # E-1 关键：失败也推进节流时钟并清空新句计数，否则"持续失败 =
            # 每句一次 API"（实测一场 163 句会议就是 163 次调用）。
            self._fail_streak += 1
            now = time.monotonic()
            self._last_call_t = now
            self._new_since_call = 0
            backoff = min(
                _BACKOFF_BASE_S * 2 ** (self._fail_streak - 1),
                _BACKOFF_MAX_S,
            )
            self._backoff_until = now + backoff
            error = redact_error(e, self._secret_values)
            self._state.record_failure(error=error, backoff_seconds=backoff)
            self._set_alert(True)
            print(
                f"[advisor] 调用失败（连续第 {self._fail_streak} 次，"
                f"退避 {backoff:.0f}s）：{error}"
            )
            return
        self._fail_streak = 0
        self._backoff_until = 0.0
        self._last_call_t = time.monotonic()
        self._new_since_call = 0
        self._set_alert(False)
        if getattr(self._brain, "last_stop_reason", None) == "max_tokens":
            # 单独记账：触顶不是传输故障（不退避），截断文本已在 brain 层丢弃。
            self._state.record_truncated()
            return
        self._state.record_success()
        if advice:
            advice = self._extract_brief_mismatch(advice)
        if not advice or _is_watch(advice):
            # 「刚看过、认为无需提示」与「参谋挂了」在面板上必须可区分。
            self._state.record_suppressed()
            return
        self._state.record_delivery()
        self._sink.post(advice)

    def _run(self) -> None:
        while True:
            sentence = self._q.get()
            if sentence is None:
                return
            self._append(sentence)
            self._new_since_call += 1
            if self._should_call(sentence):
                self._call(sentence)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="advisor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._q.put(None)
        if self._thread is not None:
            self._thread.join(timeout)
