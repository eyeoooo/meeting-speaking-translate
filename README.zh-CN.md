# 会议助手 —— Mac mini 会议中枢

**[English README →](README.md)**

**对方说日语 → 我看中文字幕 + AI 参谋建议；我说中文/日语/英语 →
对方听到"我自己声音"的日语**（克隆声线级联同传）；全程录音、
会后自动生成纪要。形态 = 菜单栏原生 App（会议助手.app）+
Python bridge（端口 8787，token 鉴权）+ 浮动字幕窗口（⌘J）。

会议软件（Teams/Zoom）跑在 Mac mini 本机，音频经 BlackHole 虚拟声卡
接入本产品——会议软件无需任何插件：

```text
【收听方向】
Teams 扬声器 → BlackHole 2ch → bridge ├─ 本机播放（你听会议原声）
                                      ├─ 录音 / 会后纪要
                                      ├─ Realtime 同传（中文字幕）/ 参谋
                                      └─ 原生字幕窗口

【发言方向】（2026-07-31 真人复验转正）
耳麦中文/日语/英语 → 转写(热词) → Claude 翻译(敬语/术语) →
  用户克隆声线(ElevenLabs) → BlackHole 16ch → Teams 麦克风 → 会议
  · 出口恒为日语：中/英→译日，日语→原样直通，混说→合译整句
  · 八道确定性防线：回声/迟到/噪声/幻听/谚文/重复/数字直通/工作者防猝死
```

产品运行闭包由三部分组成：

- `doctor`：会前体检，校验设备、48kHz、默认输出、麦克风 TCC、磁盘与依赖。
- `bridge`：唯一音频服务，监听 8787，承载播放、录音、状态、告警、
  同传、参谋、发言与会后处理。
- 原生字幕窗口（`app/MeetingCaptionsWindow.swift`）：第一呈现面，直连
  bridge WebSocket，显示双泳道字幕、灰字草稿、发言自查与参谋建议。
  工程调试面是 bridge 自带的 `bridge.html`（`http://127.0.0.1:8787/?t=<token>`）。

操作入口：

- [会前 checklist](docs/runbook-checklist.md)
- [会中操作卡](docs/runbook-in-meeting.md)
- [故障速查表](docs/troubleshooting.md)
- [同传方案与裁定](docs/simultaneous-interpretation-plan.md)
- [发言引擎复验记录](docs/speak-engine-ab-20260731.md)

## 1. 运行基线

| 项目 | 基线 |
|---|---|
| 虚拟声卡 | BlackHole 2ch（会议→采集）与 BlackHole 16ch（发言→会议），两者必须是**不同设备**（同设备会形成翻译回环，启动时 fail-closed） |
| Teams 音频设置 | 扬声器 = `BlackHole 2ch`；麦克风 = `BlackHole 16ch`。说话时确认 Teams 的扬声器电平条在动——接错的典型症状是整场无字幕、录音为数字零。嫌两块 BlackHole 分不清，跑一次 `swift tools/make_named_devices.swift`，Teams 里改选「会议助手·扬声器」「会议助手·麦克风」（语义名聚合设备，声音与关键字匹配不受影响；`--undo` 撤销） |
| 你自己的耳朵/嘴 | 默认输入 = 你的耳麦（发言采集跟随系统默认）。会议原声：发言/排练模式下自动改道进你的耳麦（与你的日语同一台设备，2026-07-31 监听归耳裁定）；纯字幕模式跟随系统默认输出 |
| 采样率 | 48000 Hz（Realtime 链路的采集契约） |
| 麦克风 TCC | 归 会议助手.app（bundle id `dev.controller-agent.audio-gateway`）；命令行排障时归图形 Terminal |

**SSH 启动的采集进程会得到精确数字零（约 -180 dBFS）**，与接线无关——
这是 macOS TCC 的行为，不是故障。排障采集必须用本机图形 Terminal。
不要把 `--skip-doctor` 作为常规启动参数。

## 2. 安装

```bash
cd ~/audio-gateway
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
brew install switchaudio-osx
brew install blackhole-2ch blackhole-16ch
```

（通过 `app/build-apps.sh` 构建的 会议助手.app 自带 Python 运行时，
新机器首启会自动完成 venv 与依赖安装，无需 Homebrew Python。）

可选 AI 能力：

```bash
.venv/bin/python -m pip install mlx-whisper   # 会后转写；Intel 可改 faster-whisper
```

LLM / 语音后端与凭据（写入 `~/.zshenv`）：

| 能力 | 默认后端 | 凭据 / 选项 |
|---|---|---|
| 同传（收听） | OpenAI Realtime | `OPENAI_API_KEY`、`--interpret-model` |
| 参谋 | Claude | `ANTHROPIC_API_KEY`、`--advise` |
| 会后纪要 | Claude | `ANTHROPIC_API_KEY`；也可选 OpenAI/Ollama |
| 发言转写（级联） | OpenAI gpt-4o-transcribe | `OPENAI_API_KEY`（热词来自内置商务词表 + `~/AudioGateway/brief.md`） |
| 发言翻译（级联） | Claude Haiku | `ANTHROPIC_API_KEY`、`--cascade-translate-model` |
| 克隆声线 | ElevenLabs | `ELEVENLABS_API_KEY`、`ELEVENLABS_VOICE_ID`（或 `--speak-voice-id`） |

基础播放、录音和字幕/调试面不要求 Whisper 或 LLM 凭据；缺哪把钥匙
只缺对应能力，启动时 fail-closed 并给出可复制的修复指引。

## 3. 最短工作流

### 3.0 双击启动（日常入口）

```bash
cd ~/audio-gateway/app
./build-apps.sh meeting
```

产物：`~/Applications/会议助手.app`。双击 = 菜单栏会议服务就位
（并自注册为登录项）。菜单：开始这场会议（⌘R）/ 测试声音（5 秒）/
结束会议并生成纪要（⌘.）/ 取消这次录音 / 静音发言（m，仅正式发言
会议）/ 声音来源 / 实时中文字幕 + AI 建议开关 / 发言排练（只进我的
耳机）/ 正式发言（对方听到日语）/ 发言声音（标准声音 / 我的声音）/
打开字幕窗口（⌘J）/ 打开最近一次纪要 / 打开会议文件夹 / 帮助与诊断。

> **重建 app 后会重新弹一次麦克风授权**，这不是故障。`build-apps.sh`
> 用 ad-hoc 签名，每次重建 cdhash 都会变，TCC 据此认定是另一个 app。
> 对只用不重建的日常使用者，授权一次长期有效。

日常流程：

1. Teams 音频设置切到 BlackHole（见 §1 基线表）。
2. 双击 **会议助手.app**，菜单栏选择「开始这场会议」。
3. 开启字幕的会议，原生字幕窗口随会议自动出现。
4. 会议结束后从菜单栏选择「结束会议并生成纪要」。

菜单栏图标：`🎧` 未启动 / `🎧…` 启动中 / `🎧🟢` 正常 /
`🎧⚠️` 有告警 / `🎧⏳` 正在转写、生成纪要或转码 / `🎧✅` 处理完成。
Swift 端不实现业务逻辑，状态只以 Python `/status` 为准。

### 3.1 doctor

App 启动 bridge 前会执行核心体检。命令行排障可运行完整体检：

```bash
.venv/bin/python -m audio_gateway doctor        # FAIL 才阻塞，退出 2
.venv/bin/python -m audio_gateway doctor --fix  # 有限修复后复检
```

TCC、采样率、缺失设备、磁盘和依赖不会被越权修复。虚拟设备豁免
模拟校准项；"此刻没声音"是 WARN 不是启动阻断。

### 3.2 bridge（命令行排障入口）

```bash
.venv/bin/python -m audio_gateway bridge \
  --usb "BlackHole 2ch" --speak-device "BlackHole 16ch" \
  --monitor system --record
```

bridge 打印本次 token、调试面地址与告警；日常启停走菜单，
HTTP 等价入口 `POST /stop?t=<TOKEN>`（幂等）。

### 3.3 字幕与调试面

原生字幕窗口（⌘J）：中文主泳道 + 日文辅泳道（**两条独立时间轴，
绝不逐行配对**）+ 发言自查泳道（说=你的原话，訳=日语译文）+
AI 建议区 + 状态行（听不到会议声音时红字提示检查 Teams 扬声器）。
未成句的转写以灰字草稿先行上屏，成句后被正式行取代。

工程调试面 `http://127.0.0.1:8787/?t=<token>`：帧计数、电平、
丢帧、link 状态、告警与同传/参谋状态。

## 4. 同传与参谋（可选）

```bash
export OPENAI_API_KEY="..."
.venv/bin/python -m audio_gateway bridge \
  --usb "BlackHole 2ch" --monitor system --interpret
```

- 收听方向默认模型 `gpt-realtime-translate`（OpenAI Realtime
  translations 端点，语音进/文本出给字幕；译文语音默认关闭）。
- 字幕协议：`{"type":"segment", id, stream, text, t, elapsed_ms, epoch}`
  （append-only，60 句/流可补拉）+ `{"type":"segment_draft", ...}`
  灰字草稿（可变态，不进历史）。
- VAD 省费门控：默认 `--interpret-vad-dbfs -50`，静音 3 秒后暂停计费
  流，有声即恢复；本地采集、录音、播放不受影响。
- 参谋（`--advise`，需 `ANTHROPIC_API_KEY`）：只读会议日语原文，
  永不消费译文与你的发言；背景 brief 读 `~/AudioGateway/brief.md`，
  支持会中热重载；失败指数退避（8s 起、封顶 300s）。
- **brief 是参谋话术质量的上限**：开会/面试前把背景资料写进去——
  菜单「编辑会议背景（brief）」一键打开；结构化模板见
  `docs/brief-templates/`（面试：公司与岗位 JD、简历要点、各高频
  问题口径与薪资底线、逆问预备；商务会议：目标底线、进度与数字
  口径、风险、对方情况）。brief 全文走 prompt cache，写细不心疼钱。

### 4.1 发言方向（排练 / 正式发言 / 我的声音）

菜单勾选即可：「发言排练」= 你的日语只进自己耳机（零会议风险）；
「正式发言」= 日语注入会议（`m` 键静音是刹车）；「发言声音」=
标准声线或「我的声音」（cascade 级联，需要 `ELEVENLABS_API_KEY` +
`ELEVENLABS_VOICE_ID` + `ANTHROPIC_API_KEY`）。

级联链路：转写（gpt-4o-transcribe + 内置商务热词 + brief.md 术语）→
Claude 翻译（服务方敬语、滚动上下文、说多少译多少）→ 用户克隆声线。
入口语言自由（中/英→译日、日语→原样直通、混说→合译整句），
逐位数字串由代码直通渲染「1、2、3、4、5」。八道确定性防线：
自回声（播放窗口内）、迟到丢弃（>12s）、噪声哨兵（∅）、幻听语速闸、
谚文防火墙、同句去重、数字直通、翻译工作者防猝死。

命令行排练入口（本机 Terminal——SSH 采不到麦克风）：

```bash
.venv/bin/python -m audio_gateway bridge --rehearse --speak-engine cascade \
  --record --no-postprocess --token TEST --port 8898
```

发言引擎裁定（详见 `docs/speak-engine-ab-20260731.md`）：`translate`
为 CLI 默认与回退；菜单「我的声音」= `cascade`（真人复验转正）；
`expressive` 因"编造对话轮次"红线永不转正，仅留作 A/B 工程口；
`clone` 为 cascade 的前身，留作对照。工程 A/B 驱动：
`python -m audio_gateway.abtest --wav <语料> --engines translate,cascade`。

## 5. CLI 参考

```text
audio_gateway doctor [--fix] [--output-root DIR]
audio_gateway bridge [--port] [--token] [--record] [--no-postprocess]
                     [--usb KEYWORD] [--monitor KEYWORD]
                     [--interpret] [--interpret-lang] [--interpret-model]
                     [--interpret-device] [--interpret-vad-dbfs DBFS|off]
                     [--advise]
                     [--rehearse | --speak] [--speak-device KEYWORD]
                     [--speak-engine translate|expressive|clone|cascade]
                     [--speak-voice-id] [--clone-model] [--clone-speed]
                     [--cascade-translate-model]
                     [--replay WAV] [--rehearse-replay WAV]
                     [--whisper auto|mlx|faster] [--summary claude|openai|ollama]
                     [--skip-doctor]
```

`--replay` / `--rehearse-replay` 用既有录音充当采集源/发言麦克风做
回归验收（回放语料不占用真实设备）。完整参数见 `--help`。

会话产物（`~/AudioGateway/<时间戳>/`）：

```text
meeting.m4a / meeting.wav       录音
transcript.jsonl / .txt         转写（realtime 句 + 会后 batch 段）
rehearsal.jsonl                 发言方向转写与译文
minutes.md                      会后纪要
```

缺 Whisper 时录音仍保留；缺 LLM 凭据时只缺对应产物。单步异常写入
最终摘要并继续，不把跳过项伪装成成功。

## 6. 可靠性与故障状态

- 设备消失/流错误：bridge 立即 `link=down`，停止音频，不自动重试；
  修复后 `POST /reconnect?t=<TOKEN>` 人工重连一次。
- 会议采集连续 30 秒精确数字零：`digital-zero` 告警，字幕窗状态行
  红字提示检查 Teams 扬声器设置。
- 10 秒窗口持续削顶：`clipping (降源音量/查增益)`。
- 同传/参谋/发言任一失败只影响自身，原声监听和录音继续。

故障判定见 [故障速查表](docs/troubleshooting.md)。

## 7. 依赖与测试

核心依赖：`sounddevice`/PortAudio、`soundfile`、`numpy`/`scipy`、
`aiohttp`；可选 `httpx`/`anthropic`（AI 能力）、`mlx-whisper` 或
`faster-whisper`（会后转写）。

```bash
.venv/bin/python -m pytest tests/ -q
```

（仓库根目录运行；当前基线 234 passed。）Swift 壳编译门禁：

```bash
cd app && swiftc -warnings-as-errors -parse-as-library -typecheck \
  GatewayMenuBar.swift MeetingCaptionsWindow.swift
```

工程纪律：真机验收文化（改完必在生产机实测，回放语料回归）；
测试只许加强不许削弱；涉及朗读的改动用"机器耳朵"回环
（合成→转写→比对）验收。历史封存资产（远程麦克风客户端 micagent）
的恢复点在代码注释中，`grep TASK-20260730-008` 可定位，不属于当前
产品工作流。
