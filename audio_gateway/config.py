"""运行配置。全部可由 CLI 覆盖，见 main.py。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class GatewayConfig:
    # --- 设备绑定（名称关键字，大小写不敏感）---
    usb_keyword: str = "USB Audio Device"   # 两块免驱 USB 声卡的系统名
    monitor_keyword: str | None = None      # 本地监听设备（如 "AirPods"）；None=不监听
    mic_keyword: str | None = None          # 物理麦克风；None=不开发言通道

    # --- 音频参数 ---
    samplerate: int = 48000                 # 采集/回放采样率（两端声卡需一致）
    blocksize: int = 1024                   # 每回调帧数（48k 下约 21ms）
    channels: int = 1

    # --- 断句（STT 分段）---
    silence_rms: float = 0.010              # 低于该 RMS 视为静音
    silence_hold_s: float = 0.6             # 连续静音多久触发断句
    max_segment_s: float = 12.0             # 单段上限，防止长独白不出字幕
    min_segment_s: float = 0.4              # 过短的噪声段丢弃

    # --- STT ---
    # realtime: 边开会边转写（有 2~5s 字幕延迟，占实时 CPU）
    # batch   : 【推荐】会中零 STT 负载，实时字幕交给 macOS 原生 Live Captions，
    #           会议结束后对 meeting.wav 一次性转写 → 归档 → 纪要
    # off     : 只录音，不转写不生成纪要
    stt_mode: str = "batch"
    whisper_backend: str = "auto"           # auto | mlx | faster
    whisper_model: str = "mlx-community/whisper-large-v3-turbo"
    faster_model: str = "large-v3-turbo"    # faster-whisper 的模型名

    # --- 翻译 / 字幕 ---
    translate_backend: str = "claude"       # claude | openai | ollama | none
    target_lang: str = "zh"
    subtitle_window: bool = False           # True=Tk 置顶字幕窗；False=控制台

    # --- 参谋（实时决策建议，私密浮窗）---
    advisor: bool = False
    advisor_backend: str = "claude"         # claude | openai | ollama
    advisor_brief: str | None = None        # 会议目标/底线 markdown 文件路径
    advisor_interval_s: float = 25.0        # 常规节流：两次建议的最小间隔
    advisor_min_new_segments: int = 3       # 常规节流：至少累积几段新发言
    advisor_context_chars: int = 6000       # 滚动转写窗口大小
    advisor_hotwords: str = ""              # 逗号分隔，追加到内置热词表
    advisor_console: bool = False           # True=建议输出到终端而非浮窗

    # --- LLM ---
    claude_model: str = "claude-opus-5"
    # OpenAI（或任何 OpenAI 兼容端点：Azure / vLLM / LM Studio…）
    openai_model: str = "gpt-4o"            # 按账号可用模型覆盖：--openai-model
    openai_base_url: str = "https://api.openai.com/v1"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"
    summary_backend: str = "claude"         # claude | openai | ollama

    # --- 归档 ---
    output_root: Path = field(default_factory=lambda: Path.home() / "AudioGateway")
    keep_wav: bool = True                   # m4a 转换成功后是否保留 wav

    def new_session_dir(self) -> Path:
        d = self.output_root / datetime.now().strftime("%Y%m%d-%H%M%S")
        d.mkdir(parents=True, exist_ok=True)
        return d
