"""发言引擎 A/B 评测驱动（工程用，产品不暴露、不进菜单）。

用同一段中文语料（--rehearse-replay）分别驱动 translate / expressive /
clone 发言引擎各跑一场真实 bridge（真实 WebSocket、真实模型、真实语音
输出到 --speak-device），收集每场的 rehearsal.jsonl 文本段与会话目录
（clone 引擎需要 ELEVENLABS_API_KEY 与 ELEVENLABS_VOICE_ID 环境变量，
子进程原样继承）：

  - 正确性评测：两场的 translation 段与中文原文逐句对照（人工三档：
    忠实/漏译/加戏；"加戏"=无中生有或把发言当提问来回答，一票红旗）。
  - 自然度评测：报出两场会话目录，用户用「正式发言」亲耳对比——
    语音审美不由脚本裁决。

为什么是子进程而不是进程内调库：评测对象是**整条产品链路**
（CLI→bridge→引擎→播放器→落盘），进程内拼装等于评测一个不存在的产品。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
import wave
from datetime import datetime
from pathlib import Path

ENGINES = ("translate", "expressive", "clone")


def wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


def run_engine_session(
    *,
    engine: str,
    wav: Path,
    usb: str,
    speak_device: str,
    monitor: str,
    token: str,
    port: int,
    tail_seconds: float,
    python: str,
    log_dir: Path,
) -> dict:
    """跑一场；返回 {engine, session_dir, segments, log}。"""
    cmd = [
        python, "-m", "audio_gateway", "bridge",
        "--record", "--speak",
        "--rehearse-replay", str(wav),
        "--usb", usb,
        "--speak-device", speak_device,
        "--monitor", monitor,
        "--token", token,
        "--port", str(port),
        "--speak-engine", engine,
        "--skip-doctor",
        # 评测产物是 rehearsal.jsonl 与发言音频；跳过会后 whisper+纪要，
        # 不然每场平白多几分钟且与引擎对比无关。
        "--no-postprocess",
    ]
    log_path = log_dir / f"abtest-{engine}.log"
    duration = wav_duration_seconds(wav)
    print(f"[abtest] engine={engine} wav={duration:.0f}s -> {log_path}")
    session_dir: Path | None = None
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            # 等 bridge 打出会话目录（同步录音行）；20s 内起不来算失败。
            deadline = time.monotonic() + 20.0
            while session_dir is None and time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"bridge exited early (engine={engine}), see {log_path}"
                    )
                for line in log_path.read_text(encoding="utf-8").splitlines():
                    if "同步录音:" in line:
                        session_dir = Path(
                            line.split("同步录音:", 1)[1].strip()
                        ).parent
                        break
                time.sleep(0.5)
            if session_dir is None:
                raise RuntimeError(
                    f"bridge never reported session dir (engine={engine})"
                )
            print(f"[abtest] engine={engine} session={session_dir}")

            # 素材放完 + 尾部留白（最后一句的译文与语音要时间到齐）。
            time.sleep(duration + tail_seconds)
            stop_url = f"http://127.0.0.1:{port}/stop?t={token}"
            with urllib.request.urlopen(
                urllib.request.Request(stop_url, method="POST"),
                timeout=10,
            ) as response:
                response.read()
            proc.wait(timeout=120)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()

    segments: list[dict] = []
    rehearsal_path = session_dir / "rehearsal.jsonl"
    if rehearsal_path.is_file():
        for line in rehearsal_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                segments.append(json.loads(line))
    return {
        "engine": engine,
        "session_dir": str(session_dir),
        "segments": segments,
        "log": str(log_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audio_gateway.abtest",
        description="发言引擎 A/B：同一语料分别驱动两引擎，落盘对照数据",
    )
    parser.add_argument("--wav", required=True, help="中文评测语料 wav")
    parser.add_argument("--usb", default="BlackHole 2ch")
    parser.add_argument("--speak-device", default="BlackHole 16ch")
    parser.add_argument("--monitor", default="system")
    parser.add_argument("--token", default="ABTEST")
    parser.add_argument("--port", type=int, default=8788,
                        help="避开生产 8787，评测场次互不影响")
    parser.add_argument("--tail-seconds", type=float, default=30.0)
    parser.add_argument(
        "--engines",
        default=",".join(ENGINES),
        help="逗号分隔，默认全部引擎（clone 需 ELEVENLABS_* 环境变量）",
    )
    parser.add_argument(
        "--output",
        help="对照 JSON 输出路径（默认 ~/AudioGateway/abtest-<时间戳>.json）",
    )
    args = parser.parse_args(argv)

    engines = [item.strip() for item in args.engines.split(",") if item.strip()]
    unknown = [item for item in engines if item not in ENGINES]
    if unknown:
        print(f"[abtest] 未知引擎（fail-closed）：{unknown}")
        return 2
    wav = Path(args.wav).expanduser()
    if not wav.is_file():
        print(f"[abtest] 语料不存在：{wav}")
        return 2
    output = Path(
        args.output
        if args.output
        else Path.home()
        / "AudioGateway"
        / f"abtest-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    ).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)

    runs = []
    for engine in engines:
        runs.append(run_engine_session(
            engine=engine,
            wav=wav,
            usb=args.usb,
            speak_device=args.speak_device,
            monitor=args.monitor,
            token=args.token,
            port=args.port,
            tail_seconds=args.tail_seconds,
            python=sys.executable,
            log_dir=output.parent,
        ))

    report = {
        "wav": str(wav),
        "generated_at": datetime.now().isoformat(),
        "runs": runs,
    }
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"\n[abtest] 对照数据: {output}")
    for run in runs:
        sources = [s for s in run["segments"] if s["stream"] == "source"]
        translations = [
            s for s in run["segments"] if s["stream"] == "translation"
        ]
        print(
            f"[abtest] {run['engine']}: session={run['session_dir']} "
            f"source={len(sources)} translation={len(translations)}"
        )
        for seg in run["segments"]:
            lane = "说" if seg["stream"] == "source" else "訳"
            print(f"    [{lane} t={seg['t']:6.1f}] {seg['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
