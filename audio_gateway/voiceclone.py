"""M3 声纹克隆：translate 文本链路 + ElevenLabs 跨语言克隆声线。

正确性优先裁定（translate 引擎保持默认）不受影响：本类继承
RealtimeInterpreter，translations 端点、断句、重连、VAD、语音开关全部
逐字复用（父类一行未动）；替换的只有音频出口——端点内置声线的
audio delta 被丢弃，改由译文成句驱动 ElevenLabs 跨语言 TTS（用户本人
的克隆声线），出口仍是 player.feed_pcm16(24kHz mono PCM16)，
SpeakTeePlayer 的静音门控/双支路/收尾栅栏自动继承。

为什么按"成句"而不是逐字流式喂 TTS：克隆声线的韵律需要整句上下文，
且成句是本产品既有的稳定单元（SentenceAccumulator 兜底 160 字/15 秒）。
文字 delta 比端点内置语音先到，成句 TTS 首音与内置声线同量级。

失败语义：单句合成失败=该句静音（文本仍在面板与 rehearsal.jsonl 里），
报告错误后继续下一句——发言中途绝不因 TTS 抖动整场翻车。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import aiohttp

from .interpreter import RealtimeInterpreter

# 跨语言克隆用 multilingual v2：正确性/相似度优先（flash 系为低延迟档，
# 音色相似度与韵律弱一档，若真实会议嫌慢再评测降档）。
DEFAULT_CLONE_MODEL = "eleven_multilingual_v2"
ELEVENLABS_TTS_URL = (
    "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
)
# 直接请求 24k PCM 对齐 feed_pcm16 契约（24kHz mono PCM16 LE），
# 不做任何本地重采样——收听侧共用播放器类，契约绝不动。
CLONE_OUTPUT_FORMAT = "pcm_24000"
# 单句合成的总超时：超时=该句静音并继续，绝不悬挂工作者。
TTS_SENTENCE_TIMEOUT_SECONDS = 30.0


class CloneSpeechSession(RealtimeInterpreter):
    """发言会话（M3）：文本走 translations 端点，声音走 ElevenLabs 克隆。

    接口与 RealtimeInterpreter 完全对齐（start/stop/set_voice_enabled/
    state/回调），bridge 侧只是第三个 speak_engine 分支。
    """

    def __init__(
        self,
        audio_tap: Any,
        output_player: Any,
        *,
        api_key: str,
        elevenlabs_api_key: str,
        voice_id: str,
        clone_model: str = DEFAULT_CLONE_MODEL,
        tts_session_factory: Callable[[], Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if not elevenlabs_api_key.strip():
            raise ValueError(
                "ELEVENLABS_API_KEY is required for clone speak engine"
            )
        if not voice_id.strip():
            raise ValueError("clone voice id must be non-empty")
        super().__init__(audio_tap, output_player, api_key=api_key, **kwargs)
        self._el_api_key = elevenlabs_api_key.strip()
        self._voice_id = voice_id.strip()
        self._clone_model = clone_model
        self._tts_session_factory = tts_session_factory or (
            lambda: aiohttp.ClientSession()
        )
        self._tts_queue: asyncio.Queue[str] = asyncio.Queue()
        self._tts_task: asyncio.Task[None] | None = None

    @property
    def voice_id(self) -> str:
        return self._voice_id

    def handle_server_event(self, event: dict[str, Any]) -> bool:
        if event.get("type") == "session.output_audio.delta":
            # 端点内置声线在此丢弃：克隆声线是唯一音频出口，两路都播=重影。
            # 文本事件照常走父类（断句→成句→_publish_segment）。
            return False
        return super().handle_server_event(event)

    def _publish_segment(
        self,
        kind: str,
        sentence: str,
        elapsed_ms: int | None,
    ) -> None:
        # 面板/rehearsal.jsonl 的文本链路先行且原样；克隆 TTS 只是旁挂。
        super()._publish_segment(kind, sentence, elapsed_ms)
        if kind != "translation":
            return
        # 停止后父类 _flush_text 仍会发布残句（文本照常上面板/落盘），
        # 但绝不能把已取消的 TTS 工作者重新拉起来。
        if self._stop_event.is_set():
            return
        # 入队即返回：TTS 网络 IO 绝不阻塞协议解析循环。
        self._ensure_tts_worker()
        self._tts_queue.put_nowait(sentence)

    def _ensure_tts_worker(self) -> None:
        if self._tts_task is None or self._tts_task.done():
            self._tts_task = asyncio.get_running_loop().create_task(
                self._tts_worker(),
                name="clone-tts",
            )

    async def _tts_worker(self) -> None:
        # 单工作者串行合成：句序即播放序，天然无乱序。
        async with self._tts_session_factory() as session:
            while True:
                sentence = await self._tts_queue.get()
                try:
                    await asyncio.wait_for(
                        self._synthesize(session, sentence),
                        TTS_SENTENCE_TIMEOUT_SECONDS,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._report_error(
                        f"克隆语音合成失败，该句静音（字幕不受影响）：{exc}"
                    )

    async def _synthesize(self, session: Any, sentence: str) -> None:
        # 与父类音频 delta 同一道门：语音关闭/播放器未启动时不合成
        # （也省下这句的 ElevenLabs 计费）。
        if (
            not self.state.snapshot()["interpret_voice"]
            or not self._player_started
        ):
            return
        async with session.post(
            ELEVENLABS_TTS_URL.format(voice_id=self._voice_id),
            params={"output_format": CLONE_OUTPUT_FORMAT},
            headers={"xi-api-key": self._el_api_key},
            json={"text": sentence, "model_id": self._clone_model},
        ) as response:
            if response.status != 200:
                detail = (await response.text())[:200]
                raise RuntimeError(
                    f"ElevenLabs HTTP {response.status}: {detail}"
                )
            # 裸 PCM16 流的块边界可能落在奇数字节上；攒住尾字节，
            # 保证 feed_pcm16 恒收偶数长度（其契约会对奇数长度抛错）。
            carry = b""
            async for chunk in response.content.iter_chunked(4096):
                data = carry + chunk
                if len(data) % 2:
                    carry, data = data[-1:], data[:-1]
                else:
                    carry = b""
                if data:
                    self._output_player.feed_pcm16(data)

    async def stop(self) -> None:
        # 会议结束即停声：残句文本仍会被父类 _flush_text 发布到面板，
        # 但不再念出——会议已散场，迟到的日语只会造成混乱。
        # 先置停止位再取消工作者：反过来会留一条竞态窗口，在途 segment
        # 能把刚取消的工作者重新拉起来。
        self._stop_event.set()
        task = self._tts_task
        self._tts_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await super().stop()
