"""CLI 编排：doctor / list-devices / run / bridge / summarize。

run 生命周期：
  RxBus(Mac_In) → [Recorder | Transcriber | MonitorPlayer]，Talkback(mic→Mac_Out)
  q/SIGTERM/SIGINT 结束 → 收尾录音(m4a) → 归档 → LLM 纪要
  m / SIGUSR1 = 一键静音切换

bridge 生命周期：
  POST /stop、SIGTERM、SIGINT → 同一优雅停止入口
  → 停流/关同传 → 批量转写 → 纪要 → m4a → 退出
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path

from .config import GatewayConfig


def _parse_interpret_vad_dbfs(value: str) -> float | None:
    if value.strip().lower() == "off":
        return None
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be a dBFS number or 'off'"
        ) from exc
    if not math.isfinite(parsed) or parsed > 0:
        raise argparse.ArgumentTypeError(
            "must be a finite dBFS value <= 0 or 'off'"
        )
    return parsed


def _build_config(args) -> GatewayConfig:
    cfg = GatewayConfig()
    for name in ("usb_keyword", "monitor_keyword", "mic_keyword", "samplerate",
                 "blocksize", "stt_mode", "whisper_backend", "translate_backend",
                 "summary_backend", "claude_model", "ollama_model", "ollama_url",
                 "openai_model", "openai_base_url",
                 "advisor_backend", "advisor_brief", "advisor_interval_s",
                 "advisor_hotwords"):
        v = getattr(args, name, None)
        if v is not None:
            setattr(cfg, name, v)
    for flag in ("subtitle_window", "advisor", "advisor_console"):
        if getattr(args, flag, False):
            setattr(cfg, flag, True)
    if getattr(args, "output_root", None):
        cfg.output_root = Path(args.output_root)
    return cfg


def _startup_doctor(cfg: GatewayConfig, *, skip_doctor: bool) -> int:
    if skip_doctor:
        print(
            "[gateway] 警告: 已显式使用 --skip-doctor，跳过设备/增益/"
            "输出音量/TCC 启动门禁；环境漂移风险由操作者承担。"
        )
        return 0
    from .doctor import run_doctor

    code = run_doctor(cfg, core_only=True, title="音频网关启动核心体检")
    if code != 0:
        print(
            "[gateway] 启动已 fail-closed 拒绝。修复上述 FAIL 后重试；"
            "仅在明确承担风险时使用 --skip-doctor。"
        )
    return code


def cmd_doctor(args) -> int:
    from .doctor import run_doctor

    cfg = _build_config(args)
    return run_doctor(cfg, fix=args.fix)


def cmd_run(args) -> int:
    cfg = _build_config(args)
    doctor_code = _startup_doctor(
        cfg, skip_doctor=getattr(args, "skip_doctor", False)
    )
    if doctor_code != 0:
        return doctor_code

    from . import devices
    from .advisor import Advisor, ConsoleAdvisorSink, TkAdvisorWindow
    from .archive import TranscriptArchiver
    from .bus import RxBus
    from .recorder import Recorder
    from .routing import MonitorPlayer, Talkback
    from .subtitles import ConsoleSubtitleSink, TkSubtitleWindow
    from .summarizer import summarize
    from .transcriber import Transcriber, TranscriptSegment, batch_transcribe
    from .translator import make_translator

    dev = devices.resolve(cfg)
    session_dir = cfg.new_session_dir()
    meeting_start = datetime.now()
    print(f"[gateway] 会话目录: {session_dir}")
    print(f"[gateway] Mac_In=#{dev.mac_in}  Mac_Out=#{dev.mac_out}  "
          f"monitor={dev.monitor}  mic={dev.mic}")

    # 参谋依赖实时转写流；如当前是 batch/off 则自动升为 realtime。
    # 未显式要求翻译时顺带关掉逐段翻译，避免为参谋隐式产生翻译 API 费用。
    if cfg.advisor and cfg.stt_mode != "realtime":
        cfg.stt_mode = "realtime"
        if getattr(args, "translate_backend", None) is None:
            cfg.translate_backend = "none"
        print("[gateway] 参谋模式需要实时转写：已自动切到 --stt realtime"
              "（实时字幕仍建议开系统 Live Captions；逐段翻译已关，可用 --translate 打开）。")

    realtime_stt = cfg.stt_mode == "realtime"
    if cfg.stt_mode == "batch":
        print("[gateway] 低延迟模式：会中实时字幕请开启 macOS 原生 Live Captions"
              "（系统设置→辅助功能→实时字幕，音源选「麦克风」= USB Audio Device），"
              "Whisper 将在会议结束后批量转写。")

    # --- 收音总线与扇出 ---
    bus = RxBus(dev.mac_in, cfg.samplerate, cfg.blocksize)
    rec_tap = bus.subscribe("recorder")                       # 不丢帧
    stt_tap = bus.subscribe("stt") if realtime_stt else None  # 不丢帧
    mon_tap = bus.subscribe("monitor", maxsize=8, drop_oldest=True) if dev.monitor is not None else None

    recorder = Recorder(rec_tap, session_dir / "meeting.wav", cfg.samplerate)
    archiver = TranscriptArchiver(session_dir, meeting_start) if cfg.stt_mode != "off" else None

    # --- 实时转写/翻译/字幕流水线（仅 realtime 模式）---
    transcriber = None
    tw = None
    trans_q: queue.Queue = queue.Queue()
    if cfg.subtitle_window and not realtime_stt:
        print("[gateway] 提示：--subtitle-window 仅在 --stt realtime 下有意义（批量模式请用原生 Live Captions），已忽略。")
    subtitle = TkSubtitleWindow() if (cfg.subtitle_window and realtime_stt) else ConsoleSubtitleSink()

    if realtime_stt:
        translator = make_translator(cfg)

        def on_segment(seg: TranscriptSegment) -> None:
            trans_q.put(seg)

        def translate_worker() -> None:  # 翻译独立 worker，不阻塞 STT 吞吐
            while True:
                seg = trans_q.get()
                if seg is None:
                    return
                translated = None
                try:
                    translated = translator.translate(seg.text, seg.lang)
                except Exception as e:
                    print(f"[translate] 失败（原文照发）：{e}")
                archiver.add(seg, translated)
                subtitle.post(seg.t0, seg.lang, seg.text, translated)

        segment_callbacks = [on_segment]
    else:
        segment_callbacks = []

    # --- 参谋（私密建议）---
    advisor = None
    advisor_sink = None
    if cfg.advisor:
        if cfg.advisor_console:
            advisor_sink = ConsoleAdvisorSink()
        else:
            # 有字幕窗则挂靠其 Tk root 共用 mainloop，否则自建 root
            parent = subtitle._root if isinstance(subtitle, TkSubtitleWindow) else None
            advisor_sink = TkAdvisorWindow(root=parent)
        advisor = Advisor(cfg, advisor_sink)
        segment_callbacks.append(advisor.on_segment)

    if realtime_stt:
        transcriber = Transcriber(stt_tap, cfg, on_segment=segment_callbacks)
        tw = threading.Thread(target=translate_worker, name="translate", daemon=True)

    monitor = MonitorPlayer(mon_tap, dev.monitor, cfg.samplerate, cfg.blocksize) if mon_tap else None
    talkback = Talkback(dev.mic, dev.mac_out, cfg.samplerate, cfg.blocksize) if dev.mic is not None else None

    stopping = threading.Event()

    def do_toggle_mute(*_):
        if talkback:
            print(f"[gateway] 发言通道 {'已静音' if talkback.toggle_mute() else '已开启'}")
        else:
            print("[gateway] 未配置 --mic，无发言通道")

    def do_stop(*_):
        stopping.set()
        subtitle.close()
        if advisor_sink is not None:
            advisor_sink.close()

    signal.signal(signal.SIGUSR1, do_toggle_mute)
    signal.signal(signal.SIGTERM, do_stop)
    signal.signal(signal.SIGINT, do_stop)

    # --- 启动 ---
    if tw:
        tw.start()
    if advisor:
        advisor.start()
    recorder.start()
    if transcriber:
        transcriber.start()
    if monitor:
        monitor.start()
    if talkback:
        talkback.start()
    bus.start()
    print("[gateway] 运行中。m+回车=静音切换  q+回车=结束会议")

    def on_key(ch: str) -> None:
        if ch == "m":
            do_toggle_mute()
        elif ch == "q":
            do_stop()

    # Tk 必须占用主线程：字幕窗优先当 UI 宿主；只有参谋窗时由它当宿主
    ui_owner = subtitle if (cfg.subtitle_window and realtime_stt) else None
    if ui_owner is None and isinstance(advisor_sink, TkAdvisorWindow) and advisor_sink.owns_root:
        ui_owner = advisor_sink
    if ui_owner is not None:
        # stdin 热键退化为窗口按键 + 信号
        ui_owner.run_mainloop(on_key=on_key)
        stopping.set()
    else:
        while not stopping.is_set():
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                do_stop()
                break
            on_key(line.strip().lower()[:1])

    # --- 收尾 ---
    print("[gateway] 结束中：停采集 → 转写 → 转码 → 纪要 …")
    if talkback:
        talkback.stop()
    bus.stop()                       # 关闭所有 tap，触发各消费线程收尾
    if transcriber:
        transcriber.join()
        trans_q.put(None)            # 哨兵：翻译 worker 处理完存量后退出
        tw.join(timeout=60)
    if advisor:
        advisor.stop()
    if monitor:
        # stop() 而不是 join()：取帧线程结束只是一半，PortAudio 流必须由
        # 本控制线程显式关掉，绝不能留给解释器退出时的 Pa_Terminate/dlclose。
        monitor.stop()

    if cfg.stt_mode == "batch":
        recorder.close()             # WAV 落盘完成，先转写再转码
        print("[gateway] 批量转写 meeting.wav（时长越长耗时越久）…")
        try:
            for seg in batch_transcribe(recorder.wav_path, cfg):
                archiver.add(seg)
        except Exception as e:
            print(f"[gateway] 批量转写失败（录音已保留，可稍后重试）：{e}")

    audio_path = recorder.finalize(keep_wav=cfg.keep_wav)
    if archiver:
        archiver.close()
    if bus.status_flags:
        print(f"[gateway] 采集期间 PortAudio 状态告警: {bus.status_flags}")
    print(f"[gateway] 录音: {audio_path}")

    if cfg.stt_mode != "off" and not args.no_summary:
        try:
            out = summarize(session_dir / "transcript.jsonl", cfg)
            print(f"[gateway] 纪要: {out}")
        except Exception as e:
            print(f"[gateway] 纪要生成失败（转写已保留，可稍后 summarize 重试）：{e}")
    return 0


def cmd_verify(args) -> int:
    """部署自检/贯通验证：
    capture  = 监听 Mac_In 电平（被控机放声 → 这里应看到信号）
    tone     = 向 Mac_Out 播放正弦测试音（被控机侧应能收到"麦克风"信号）
    talkback = 麦克风→Mac_Out 直通 N 秒（真人说话贯通）
    burnin   = Mac_In 采集 + Mac_Out 440Hz 双流烧机，逐分钟 JSONL
    """
    import math
    import numpy as np
    import sounddevice as sd

    from . import devices
    from .routing import Talkback

    cfg = _build_config(args)
    if args.what == "burnin" and (
        not math.isfinite(args.minutes) or args.minutes <= 0
    ):
        print("[verify] --minutes 必须是大于 0 的有限数值")
        return 2
    try:
        dev = devices.resolve(cfg)
    except devices.DeviceResolutionError as exc:
        print(f"[verify] 设备解析失败（fail-closed）: {exc}")
        return 2

    def dbfs(x: float) -> float:
        return 20 * math.log10(max(x, 1e-9))

    if args.what == "burnin":
        from .routing import BurnInProbe

        output_path = (
            Path(args.output).expanduser()
            if args.output
            else Path.cwd()
            / (
                "audio-gateway-burnin-"
                f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.jsonl"
            )
        )
        print(
            f"[verify] 双流烧机 {args.minutes:g} 分钟："
            f"Mac_In(#{dev.mac_in}) 采集 + "
            f"Mac_Out(#{dev.mac_out}) 440Hz 输出"
        )
        print(f"[verify] 逐分钟记录: {output_path}")
        try:
            output_file = output_path.open("x", encoding="utf-8")
        except OSError as exc:
            print(f"[verify] 无法创建烧机记录（拒绝覆盖已有文件）: {exc}")
            return 2

        probe = BurnInProbe(
            dev.mac_in,
            dev.mac_out,
            cfg.samplerate,
            cfg.blocksize,
            frequency=440.0,
        )
        with output_file:
            def write_record(record: dict) -> None:
                output_file.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
                output_file.flush()
                if record["type"] == "minute":
                    verdict = "PASS" if record["passed"] else "FAIL"
                    print(
                        f"[verify] minute {record['minute']}: {verdict}; "
                        f"drops={record['drops']['estimated_samples']} "
                        f"({record['drops']['rate']:.6%}), "
                        f"xruns={sum(record['xruns'].values())}, "
                        f"callbacks={record['callbacks']}, "
                        f"drift={record['drift']['ppm_estimate']:.2f}ppm"
                    )

            try:
                summary = probe.run(
                    args.minutes,
                    on_interval=write_record,
                )
            except Exception as exc:
                summary = {
                    "type": "summary",
                    "minutes_requested": args.minutes,
                    "passed": False,
                    "stream_interrupted": True,
                    "reason": f"burn-in runner failed: {exc}",
                }
            write_record(summary)

        verdict = "PASS" if summary["passed"] else "FAIL"
        print(
            f"[verify] 烧机结论: {verdict}; "
            f"{summary.get('reason', 'unknown')}；记录={output_path}"
        )
        return 0 if summary["passed"] else 2

    if args.what == "capture":
        print(f"[verify] 采集 Mac_In(#{dev.mac_in}) {args.seconds:.0f}s，逐秒电平：")
        got_signal = False
        with sd.InputStream(device=dev.mac_in, channels=1,
                            samplerate=cfg.samplerate, dtype="float32") as stream:
            for i in range(int(args.seconds)):
                frames = stream.read(cfg.samplerate)[0][:, 0]
                rms, peak = float(np.sqrt(np.mean(frames ** 2))), float(np.max(np.abs(frames)))
                bar = "#" * max(0, int((dbfs(rms) + 60) / 3))
                print(f"  {i+1:>2}s  RMS {dbfs(rms):6.1f} dBFS  peak {dbfs(peak):6.1f} dBFS  |{bar}")
                if dbfs(rms) > -50:
                    got_signal = True
        print("[verify] 结论: " + (
            "✅ 检测到音频信号（链路贯通）" if got_signal else
            "⚠️ 接近静音——被控机未放声/未接线/音量过低（底噪 <-50dBFS 属正常）"))
        return 0 if got_signal else 2

    if args.what == "tone":
        print(f"[verify] 向 Mac_Out(#{dev.mac_out}) 播放 {args.freq:.0f}Hz 测试音 {args.seconds:.0f}s，"
              f"被控机侧请观察麦克风电平/录音。")
        t = np.arange(int(cfg.samplerate * args.seconds)) / cfg.samplerate
        wave = (0.4 * np.sin(2 * np.pi * args.freq * t)).astype(np.float32)
        sd.play(wave, samplerate=cfg.samplerate, device=dev.mac_out, blocking=True)
        print("[verify] 播放完成。")
        return 0

    if args.what == "loopback":
        total = max(int(args.seconds), 25)
        print(f"[verify] 回环验证（配合会议软件回声测试）：0-5s 发 {args.freq:.0f}Hz 提示音 → 之后持续监听回放，共 {total}s。")
        print("        流程：被控机会议软件 麦克风/扬声器 都选 USB Audio Device，发起测试通话/回声测试后运行本命令。")
        t = np.arange(int(cfg.samplerate * 5)) / cfg.samplerate
        wave = (0.4 * np.sin(2 * np.pi * args.freq * t)).astype(np.float32)
        player = threading.Thread(
            target=lambda: sd.play(wave, samplerate=cfg.samplerate,
                                   device=dev.mac_out, blocking=True),
            daemon=True,
        )
        echo_seen = False
        with sd.InputStream(device=dev.mac_in, channels=1,
                            samplerate=cfg.samplerate, dtype="float32") as stream:
            player.start()
            for i in range(total):
                frames = stream.read(cfg.samplerate)[0][:, 0]
                rms = dbfs(float(np.sqrt(np.mean(frames ** 2))))
                phase = "发音" if i < 5 else "听回放"
                bar = "#" * max(0, int((rms + 60) / 3))
                print(f"  {i+1:>2}s [{phase}] RMS {rms:6.1f} dBFS  |{bar}")
                if i >= 6 and rms > -45:
                    echo_seen = True
        print("[verify] 结论: " + (
            "✅ 监听阶段收到回放信号——发送/接收双向经会议软件贯通" if echo_seen else
            "⚠️ 未听到回放：确认被控机会议软件麦克风+扬声器都选了 USB Audio Device、"
            "回声测试已开始，以及 声卡A绿孔→声卡B粉孔 的线"))
        return 0 if echo_seen else 2

    if args.what == "talkback":
        if dev.mic is None:
            print("[verify] 需要 --mic 指定麦克风（Mac mini 无内置麦，可用 iPhone 连续互通麦或外接）")
            return 2
        print(f"[verify] 直通 麦克风(#{dev.mic}) → Mac_Out(#{dev.mac_out}) {args.seconds:.0f}s，请说话…")
        tb = Talkback(dev.mic, dev.mac_out, cfg.samplerate, cfg.blocksize)
        tb.start()
        import time as _t
        _t.sleep(args.seconds)
        tb.stop()
        print("[verify] 直通结束。")
        return 0
    return 1


def cmd_bridge(args) -> int:
    cfg = _build_config(args)
    if args.advise and not args.interpret:
        print(
            "[bridge] --advise 启动已 fail-closed：参谋只绑定同传的"
            "日语原文流，请同时显式设置 --interpret。"
        )
        return 2
    api_key = ""
    if getattr(args, "rehearse", False) and getattr(args, "speak", False):
        print("[bridge] --rehearse 与 --speak 互斥：排练只进耳机，正式发言进会议，二选一。")
        return 2
    if (
        args.interpret
        or getattr(args, "rehearse", False)
        or getattr(args, "speak", False)
    ):
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            flag = (
                "--interpret"
                if args.interpret
                else ("--speak" if getattr(args, "speak", False) else "--rehearse")
            )
            print(
                f"[bridge] {flag} 启动已 fail-closed：未设置 "
                "OPENAI_API_KEY。请先在 Mac mini Terminal 执行 "
                "`export OPENAI_API_KEY=...`；Codex/ChatGPT 登录凭据"
                "不能代替计费 API key。"
            )
            return 2
        # 48kHz 是三条 Realtime 链路共同的采集契约（重采样按 48k→24k 写死）
        if cfg.samplerate != 48_000:
            print(
                "[bridge] Realtime 链路仅接受 48000Hz 网关采集；"
                "请移除非 48000 的 --samplerate。"
            )
            return 2
    # 以下两项只属于 --interpret（会议译文播放器的设备绑定），
    # 发言方向自带 --speak-device / 耳机解析，不受此约束。
    if args.interpret:
        if not args.interpret_device and not cfg.monitor_keyword:
            print(
                "[bridge] --interpret 需要预先绑定 Mac mini 本机译文输出设备"
                "（译文语音默认关闭，运行时开启时才启动播放器）："
                "请设置 --interpret-device，或同时设置 --monitor "
                "以复用同一监听设备。"
            )
            return 2
    elevenlabs_api_key = ""
    clone_voice_id = ""
    if getattr(args, "speak_engine", "translate") == "clone":
        elevenlabs_api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if not elevenlabs_api_key:
            print(
                "[bridge] --speak-engine clone 启动已 fail-closed：未设置 "
                "ELEVENLABS_API_KEY。请先在 Mac mini 的 ~/.zshenv 写入 "
                "`export ELEVENLABS_API_KEY=...` 后重试。"
            )
            return 2
        clone_voice_id = (
            getattr(args, "speak_voice_id", "").strip()
            or os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
        )
        if not clone_voice_id:
            print(
                "[bridge] --speak-engine clone 启动已 fail-closed：未指定"
                "克隆声线。请传 --speak-voice-id，或在 ~/.zshenv 写入 "
                "`export ELEVENLABS_VOICE_ID=...`（在 ElevenLabs 完成"
                "声音克隆后可获得 voice id）。"
            )
            return 2
    anthropic_api_key = ""
    if args.advise:
        anthropic_api_key = os.environ.get(
            "ANTHROPIC_API_KEY",
            "",
        ).strip()
        if not anthropic_api_key:
            print(
                "[bridge] --advise 启动已 fail-closed：未设置 "
                "ANTHROPIC_API_KEY。请先在 Mac mini 本机配置 Anthropic "
                "API 凭据；Claude 登录态不能代替计费 API key。"
            )
            return 2

    doctor_code = _startup_doctor(
        cfg, skip_doctor=getattr(args, "skip_doctor", False)
    )
    if doctor_code != 0:
        return doctor_code

    from . import devices
    dev = devices.resolve(cfg)  # cfg.monitor_keyword 存在时 dev.monitor 一并解析
    if dev.monitor_note:
        # 跟随/回退的结果必须出现在启动日志里：设备是运行时事实，
        # 排障时第一个要知道的就是"这场会议声音去了哪"。
        print(f"[bridge] {dev.monitor_note}")
    interpret_device = None
    if args.interpret:
        if args.interpret_device:
            try:
                interpret_device = devices.resolve_output(
                    args.interpret_device,
                    what="同传操作席输出设备",
                )
            except devices.DeviceResolutionError as exc:
                print(f"[bridge] 同传输出设备解析失败（fail-closed）: {exc}")
                return 2
        else:
            interpret_device = dev.monitor
        if interpret_device == dev.mac_out:
            print(
                "[bridge] 同传输出设备拒绝：解析结果与 Mac_Out 会议上行"
                "设备相同。译文音频永久禁止进入会议上行；"
                "请改用 Mac mini 本机扬声器或本机耳机。"
            )
            return 2

    speak_device = None
    if getattr(args, "speak_device", None):
        try:
            speak_device = devices.resolve_output(
                args.speak_device, what="正式发言输出设备"
            )
        except devices.DeviceResolutionError as exc:
            print(f"[bridge] 发言输出设备解析失败（fail-closed）: {exc}")
            return 2
        if speak_device == dev.mac_in:
            print(
                "[bridge] 发言输出设备拒绝：与会议采集源是同一设备，"
                "自己的日语会回流进采集形成翻译回环。"
            )
            return 2

    from .bridge import run_bridge
    return run_bridge(cfg, dev, port=args.port, token=args.token,
                      record=args.record,
                      no_postprocess=args.no_postprocess,
                      replay_wav=getattr(args, "replay", None),
                      monitor=dev.monitor,
                      interpret=args.interpret,
                      interpret_lang=args.interpret_lang,
                      interpret_device=interpret_device,
                      interpret_model=args.interpret_model,
                      openai_api_key=api_key,
                      interpret_voice=False,
                      interpret_vad_dbfs=args.interpret_vad_dbfs,
                      advise=args.advise,
                      anthropic_api_key=anthropic_api_key,
                      rehearse=getattr(args, "rehearse", False),
                      rehearse_replay=getattr(args, "rehearse_replay", None),
                      speak=getattr(args, "speak", False),
                      speak_device=speak_device,
                      speak_engine=getattr(args, "speak_engine", "translate"),
                      elevenlabs_api_key=elevenlabs_api_key,
                      clone_voice_id=clone_voice_id)


def cmd_micagent(args) -> int:
    import asyncio
    from .micagent import run_micagent
    try:
        return asyncio.run(run_micagent(args.url, args.token,
                                        listen=args.listen, mic_keyword=args.mic_keyword,
                                        test_tone=args.test_tone))
    except KeyboardInterrupt:
        return 0


def cmd_summarize(args) -> int:
    from .summarizer import summarize

    cfg = _build_config(args)
    out = summarize(Path(args.transcript), cfg)
    print(f"[gateway] 纪要: {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="audio_gateway", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-devices", help="列出全部音频设备")

    d = sub.add_parser("doctor", help="一键体检 macOS 音频网关环境")
    d.add_argument("--fix", action="store_true", help="修复增益/USB 输出音量/默认输出后复检")
    d.add_argument("--usb", dest="usb_keyword",
                   help='USB 声卡名称关键字（默认 "USB Audio Device"）')
    d.add_argument("--output-root", dest="output_root", help="录音根目录（用于磁盘余量检查）")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--claude-model", dest="claude_model")
    common.add_argument("--ollama-model", dest="ollama_model")
    common.add_argument("--openai-model", dest="openai_model",
                        help="OpenAI 模型名（默认 gpt-4o；按账号可用模型设置）")
    common.add_argument("--openai-base-url", dest="openai_base_url",
                        help="OpenAI 兼容端点（默认 https://api.openai.com/v1；可指向 Azure/vLLM）")
    common.add_argument("--ollama-url", dest="ollama_url")
    common.add_argument("--summary", dest="summary_backend", choices=["claude", "openai", "ollama"])

    r = sub.add_parser("run", parents=[common], help="开始会议（录音+转写+翻译+监听）")
    r.add_argument("--usb", dest="usb_keyword", help='USB 声卡名称关键字（默认 "USB Audio Device"）')
    r.add_argument("--monitor", dest="monitor_keyword", help="本地监听设备关键字（如 AirPods）")
    r.add_argument("--mic", dest="mic_keyword", help="物理麦克风关键字（启用发言通道）")
    r.add_argument("--samplerate", type=int, dest="samplerate")
    r.add_argument("--blocksize", type=int, dest="blocksize",
                   help="每回调帧数（默认 1024≈21ms；降到 256≈5ms 可压监听延迟）")
    r.add_argument("--stt", dest="stt_mode", choices=["batch", "realtime", "off"],
                   help="batch=会后转写+原生Live Captions字幕(默认,低延迟) / realtime=会中转写翻译 / off=只录音")
    r.add_argument("--whisper", dest="whisper_backend", choices=["auto", "mlx", "faster"])
    r.add_argument("--translate", dest="translate_backend", choices=["claude", "openai", "ollama", "none"])
    r.add_argument("--subtitle-window", action="store_true", help="Tk 置顶字幕窗（默认控制台字幕）")
    r.add_argument("--advisor", action="store_true",
                   help="参谋模式：滚动上下文→LLM 决策建议→私密浮窗（自动启用实时转写）")
    r.add_argument("--advisor-brief", dest="advisor_brief",
                   help="会议背景/目标/底线 markdown 文件，建议质量的关键输入")
    r.add_argument("--advisor-backend", dest="advisor_backend", choices=["claude", "openai", "ollama"])
    r.add_argument("--advisor-interval", type=float, dest="advisor_interval_s",
                   help="常规节流最小间隔秒数（默认 25；热词命中不受此限）")
    r.add_argument("--advisor-hotwords", dest="advisor_hotwords",
                   help="逗号分隔的即时触发热词，追加到内置列表（価格/納期/报价/price…）")
    r.add_argument("--advisor-console", action="store_true", help="建议输出到终端而非浮窗")
    r.add_argument("--output-root", dest="output_root")
    r.add_argument("--no-summary", action="store_true", help="结束时不自动生成纪要")
    r.add_argument("--skip-doctor", action="store_true",
                   help="显式跳过启动核心体检（高风险逃生口，会打印警告）")

    s = sub.add_parser("summarize", parents=[common], help="对已有转写生成纪要")
    s.add_argument("transcript", help="transcript.jsonl 或 transcript.txt 路径")

    b = sub.add_parser(
        "bridge",
        parents=[common],
        help="Mac mini 本机会中音频桥与 AI 面板",
    )
    b.add_argument("--port", type=int, default=8787)
    b.add_argument("--token", help="固定访问 token（默认每次随机生成并打印）")
    b.add_argument("--record", action="store_true", help="同时录音（会后可 summarize）")
    b.add_argument(
        "--replay",
        metavar="WAV",
        help="回归验收：用既有录音充当采集源实时回放（不占用采集设备），"
        "整条管线在同一段语料上跑出可比对结果",
    )
    b.add_argument(
        "--rehearse",
        action="store_true",
        help="发言排练（M1）：耳麦中文→转写+日语译文只上屏，不注入会议、"
        "不进参谋、不进会议转写（需 OPENAI_API_KEY）",
    )
    b.add_argument(
        "--rehearse-replay",
        metavar="WAV",
        help="排练回归：用中文录音充当排练麦克风（工程验收用）",
    )
    b.add_argument(
        "--speak-device",
        metavar="KEYWORD",
        help="正式发言的输出设备关键字（默认复用 --usb 解析出的 Mac_Out）。"
        "本机开会时收听源与发言目标是两个不同的虚拟设备："
        "收听 --usb 'BlackHole 2ch'，发言 --speak-device 'BlackHole 16ch'——"
        "同一个设备会让自己的日语回流进采集，形成翻译回环。",
    )
    b.add_argument(
        "--speak",
        action="store_true",
        help="正式发言（M2）：耳麦中文→日语语音进入会议（Mac_Out），"
        "同时在自己耳机播放；与 --rehearse 互斥（需 OPENAI_API_KEY）",
    )
    b.add_argument(
        "--speak-engine",
        choices=["translate", "expressive", "clone"],
        default="translate",
        help="发言引擎：translate=translations 端点（默认，正确性优先）；"
        "expressive=通用 Realtime GA + marin 声线（A/B 评测中的表现力候选）；"
        "clone=translate 文本链路 + ElevenLabs 克隆声线（M3，"
        "需 ELEVENLABS_API_KEY 与 --speak-voice-id）",
    )
    b.add_argument(
        "--speak-voice-id",
        default="",
        help="clone 引擎的 ElevenLabs voice id（默认读环境变量 "
        "ELEVENLABS_VOICE_ID）",
    )
    b.add_argument(
        "--no-postprocess",
        action="store_true",
        help="停止时跳过批量转写与纪要，只收尾录音并转 m4a",
    )
    b.add_argument(
        "--whisper",
        dest="whisper_backend",
        choices=["auto", "mlx", "faster"],
        help="会后批量转写后端（默认 auto）",
    )
    b.add_argument("--monitor", dest="monitor_keyword",
                   help='Mac mini 本机会议原声播放设备（如 "Mac mini扬声器"）')
    b.add_argument("--usb", dest="usb_keyword")
    b.add_argument("--samplerate", type=int, dest="samplerate")
    b.add_argument("--blocksize", type=int, dest="blocksize")
    b.add_argument(
        "--interpret",
        action="store_true",
        help="开启 OpenAI Realtime 同传（默认关闭；需 OPENAI_API_KEY）",
    )
    b.add_argument(
        "--interpret-lang",
        default="zh",
        help="同传输出语言（默认 zh）",
    )
    b.add_argument(
        "--interpret-device",
        help="译文音频输出设备关键字（默认复用 --monitor 设备）",
    )
    b.add_argument(
        "--interpret-model",
        default="gpt-realtime-translate",
        help="Realtime 同传模型（默认 gpt-realtime-translate）",
    )
    b.add_argument(
        "--interpret-vad-dbfs",
        type=_parse_interpret_vad_dbfs,
        default=-50.0,
        help="同传静音门控阈值 dBFS（默认 -50；设 off 禁用）",
    )
    b.add_argument(
        "--advise",
        action="store_true",
        help=(
            "参谋读取同传日语原文并在面板给中文建议"
            "（默认关闭；需 --interpret 与 ANTHROPIC_API_KEY）"
        ),
    )
    b.add_argument("--skip-doctor", action="store_true",
                   help="显式跳过启动核心体检（高风险逃生口，会打印警告）")

    m = sub.add_parser(
        "micagent",
        help="封存兼容入口：操作席发言上行客户端（不在默认产品流程）",
    )
    m.add_argument("--url", required=True, help="网关地址，如 http://100.92.179.94:8787")
    m.add_argument("--token", required=True)
    m.add_argument("--listen", action="store_true",
                   help="预留诊断：同时在客户端本机播放会议声音；避免与其他播放路径叠加")
    m.add_argument("--mic", dest="mic_keyword", help="指定本机麦克风关键字（默认系统输入）")
    m.add_argument("--test-tone", action="store_true", help="诊断：发 440Hz 测试音替代麦克风")

    v = sub.add_parser(
        "verify",
        help="部署自检/贯通验证（capture/tone/talkback/loopback/burnin）",
    )
    v.add_argument(
        "what",
        choices=["capture", "tone", "talkback", "loopback", "burnin"],
    )
    v.add_argument("--seconds", type=float, default=5.0)
    v.add_argument("--freq", type=float, default=1000.0, help="tone 频率 Hz")
    v.add_argument(
        "--minutes",
        type=float,
        default=10.0,
        help="burnin 持续分钟数（默认 10）",
    )
    v.add_argument(
        "--output",
        help="burnin JSONL 路径（默认当前目录带时间戳文件）",
    )
    v.add_argument("--usb", dest="usb_keyword")
    v.add_argument("--mic", dest="mic_keyword")
    v.add_argument("--samplerate", type=int, dest="samplerate")
    v.add_argument("--blocksize", type=int, dest="blocksize")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "list-devices":
        from . import devices

        print(devices.list_devices())
        return 0
    if args.cmd == "doctor":
        return cmd_doctor(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "summarize":
        return cmd_summarize(args)
    if args.cmd == "verify":
        return cmd_verify(args)
    if args.cmd == "bridge":
        return cmd_bridge(args)
    if args.cmd == "micagent":
        return cmd_micagent(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
