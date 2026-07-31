"""Bridge 会后处理：可观测 phase、批量转写、纪要与录音转码。

本模块不启动 HTTP、音频或模型连接。生产入口由 ``bridge.run_bridge`` 编排，
测试可注入转写/纪要函数，避免真实 Whisper 或 LLM 调用。
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

POST_PROCESSING_STEPS = frozenset({"transcribe", "summarize", "convert"})


@dataclass(frozen=True)
class PostMeetingResult:
    artifacts: tuple[str, ...]
    notes: tuple[str, ...]


class BridgePhaseState:
    """Thread-safe bridge phase state exposed verbatim by ``/status``."""

    def __init__(self, session_dir: Path | None) -> None:
        self._lock = threading.Lock()
        self._phase = "running"
        self._step: str | None = None
        self._session_dir = str(session_dir) if session_dir is not None else None
        self._artifacts: tuple[str, ...] = ()
        self._notes: tuple[str, ...] = ()

    def begin_post_processing(self) -> bool:
        """Enter post-processing once; return False for every repeated stop."""
        with self._lock:
            if self._phase != "running":
                return False
            self._phase = "post_processing"
            self._step = None
            return True

    def set_step(self, step: str) -> None:
        if step not in POST_PROCESSING_STEPS:
            raise ValueError(f"invalid post-processing step: {step}")
        with self._lock:
            if self._phase != "post_processing":
                raise RuntimeError(
                    f"cannot set post-processing step while phase={self._phase}"
                )
            self._step = step

    def finish(
        self,
        *,
        artifacts: tuple[str, ...] = (),
        notes: tuple[str, ...] = (),
    ) -> None:
        with self._lock:
            if self._phase != "post_processing":
                raise RuntimeError(f"cannot finish while phase={self._phase}")
            self._phase = "done"
            self._step = None
            self._artifacts = tuple(artifacts)
            self._notes = tuple(notes)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "phase": self._phase,
                "post_processing_step": self._step,
                "session_dir": self._session_dir,
                "artifacts": list(self._artifacts),
                "post_processing_notes": list(self._notes),
            }


class GracefulStopCoordinator:
    """Idempotently start one phase broadcast followed by one shutdown."""

    def __init__(
        self,
        phase_state: BridgePhaseState,
        broadcast: Callable[[], Any],
        shutdown: Callable[[str], Any],
        *,
        logger: Callable[[str], None] = print,
    ) -> None:
        self._phase_state = phase_state
        self._broadcast = broadcast
        self._shutdown = shutdown
        self._logger = logger
        self._task: asyncio.Task[None] | None = None

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    async def request(self, reason: str) -> bool:
        if not self._phase_state.begin_post_processing():
            return False
        self._task = asyncio.create_task(
            self._broadcast_then_shutdown(reason)
        )
        return True

    async def _broadcast_then_shutdown(self, reason: str) -> None:
        try:
            await self._broadcast()
        except Exception as exc:
            self._logger(
                f"[bridge] 停止 phase 广播失败，继续收尾：{exc}"
            )
        await self._shutdown(reason)


def _has_transcript_records(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open("r", encoding="utf-8") as handle:
        return any(line.strip() for line in handle)


def redact_error(
    exc: BaseException,
    secret_values: tuple[str, ...] = (),
) -> str:
    text = str(exc)
    for secret_value in secret_values:
        if secret_value:
            text = text.replace(secret_value, "[REDACTED]")
    return text


class PostMeetingPipeline:
    """Synchronous post-meeting pipeline intended to run in an executor."""

    def __init__(
        self,
        *,
        cfg: Any,
        session_dir: Path | None,
        recorder: Any | None,
        archiver: Any | None,
        no_postprocess: bool,
        on_step: Callable[[str], None],
        batch_transcribe_fn: Callable[[Path, Any], list[Any]] | None = None,
        summarize_fn: Callable[[Path, Any], Path] | None = None,
        environ: Mapping[str, str] | None = None,
        logger: Callable[[str], None] = print,
    ) -> None:
        if batch_transcribe_fn is None:
            from .transcriber import batch_transcribe

            batch_transcribe_fn = batch_transcribe
        if summarize_fn is None:
            from .summarizer import summarize

            summarize_fn = summarize
        self._cfg = cfg
        self._session_dir = session_dir
        self._recorder = recorder
        self._archiver = archiver
        self._no_postprocess = no_postprocess
        # 运行时取消：会议临时作废时，操作者可要求只收尾录音、不做转写与纪要
        # （产品入口＝菜单「取消这次录音」→ POST /stop?postprocess=0）。
        self._runtime_skip = False
        self._on_step = on_step
        self._batch_transcribe = batch_transcribe_fn
        self._summarize = summarize_fn
        self._environ = dict(os.environ if environ is None else environ)
        self._logger = logger
        self._archiver_closed = False
        self._secret_values = tuple(
            value.strip()
            for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
            if (value := self._environ.get(key, "")).strip()
        )

    def _error(self, exc: BaseException) -> str:
        return redact_error(exc, self._secret_values)

    def _close_archiver(self, notes: list[str]) -> None:
        if self._archiver is None or self._archiver_closed:
            return
        try:
            self._archiver.close()
        except Exception as exc:
            notes.append(f"转写归档收尾失败：{self._error(exc)}")
        finally:
            self._archiver_closed = True

    def _collect_artifacts(self) -> tuple[str, ...]:
        session_dir = self._session_dir
        if session_dir is None or not session_dir.is_dir():
            return ()
        names = (
            "meeting.m4a",
            "meeting.wav",
            "transcript.txt",
            "transcript.jsonl",
            # 会中参谋建议的落盘（bridge.AdviceLog 惰性创建：零建议不产生文件）。
            "advice.jsonl",
            "rehearsal.jsonl",
            "minutes.md",
        )
        return tuple(
            str(path)
            for name in names
            if (path := session_dir / name).is_file()
        )

    def request_skip(self) -> None:
        """在 run() 之前调用即可跳过转写与纪要；录音仍正常收尾。"""
        self._runtime_skip = True

    def run(self) -> PostMeetingResult:
        notes: list[str] = []
        recorder = self._recorder
        session_dir = self._session_dir

        if recorder is None or session_dir is None:
            self._close_archiver(notes)
            notes.append("未启用 --record，已跳过会后批量处理")
            result = PostMeetingResult(self._collect_artifacts(), tuple(notes))
            self._log_result(result)
            return result

        try:
            recorder.close()
        except Exception as exc:
            notes.append(f"录音 WAV 收尾异常：{self._error(exc)}")

        if self._no_postprocess or self._runtime_skip:
            notes.append(
                "已按 --no-postprocess 跳过批量转写与纪要，只收尾录音"
                if self._no_postprocess
                else "已按操作者请求取消本次录音的转写与纪要，只收尾录音"
            )
            self._close_archiver(notes)
        else:
            self._on_step("transcribe")
            try:
                segments = self._batch_transcribe(recorder.wav_path, self._cfg)
                if self._archiver is None:
                    notes.append("转写归档器不可用，批量段落未写入")
                else:
                    for segment in segments:
                        self._archiver.add(segment, source="batch")
                    notes.append(f"批量转写完成：{len(segments)} 段")
            except Exception as exc:
                notes.append(
                    f"批量转写已跳过或失败：{self._error(exc)}"
                )

            self._close_archiver(notes)
            self._on_step("summarize")
            transcript_path = session_dir / "transcript.jsonl"
            try:
                if not _has_transcript_records(transcript_path):
                    notes.append("无可用转写内容，已跳过纪要")
                else:
                    minutes_path = self._summarize(
                        transcript_path,
                        self._cfg,
                    )
                    notes.append(f"纪要完成：{minutes_path}")
            except Exception as exc:
                notes.append(
                    f"纪要已跳过或失败：{self._error(exc)}"
                )

        self._on_step("convert")
        try:
            audio_path = recorder.finalize(keep_wav=self._cfg.keep_wav)
            if Path(audio_path).suffix.lower() != ".m4a":
                notes.append("m4a 转换未完成，已保留 meeting.wav")
        except Exception as exc:
            notes.append(
                "m4a 转换失败，已保留 meeting.wav："
                f"{self._error(exc)}"
            )

        result = PostMeetingResult(self._collect_artifacts(), tuple(notes))
        self._log_result(result)
        return result

    def _log_result(self, result: PostMeetingResult) -> None:
        self._logger(
            "[bridge] 会后处理摘要: "
            + ("；".join(result.notes) if result.notes else "无附加说明")
        )
        self._logger("[bridge] 最终产物清单:")
        if result.artifacts:
            for artifact in result.artifacts:
                self._logger(f"[bridge]   - {artifact}")
        else:
            self._logger("[bridge]   - （无）")
