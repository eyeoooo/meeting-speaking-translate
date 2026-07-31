# Mac mini 物理隔离音频网关

音频网关把被控机会议音频经 USB 声卡与 3.5mm 模拟线旁路到 Mac mini，
目标电脑无需安装软件或驱动。当前产品边界是 **Mac mini 本机接收、播放、
录音与 AI 处理闭环**：

```text
【音源】(本机 Teams/Zoom 经 BlackHole，或外部设备/被控机经 USB 声卡)
   │
   ▼                                            【Mac mini 会议助手】
会议声音 → (Target_Out 绿孔 → Mac_In 粉孔 或 BlackHole) → bridge
                                                   ├─ 本机播放
                                                   ├─ 录音 / 会后纪要
                                                   ├─ Realtime 同传 / 参谋
                                                   └─ 原生字幕窗口

【操作者】── 任意远程桌面渠道 ──▶ 看见并听见 Mac mini
```

会议模块是独立产品（会议助手.app），与 KVM 彻底脱钩：KVM 被控机只是
可选音源之一（「外部设备（USB 声卡 / 被控机）」）。KVM 控制台是另一个
独立产品（KVM 控制台.app），只负责视频 / HID / 文字投入，不含任何会议功能。

远程桌面渠道只是访问 Mac mini 的方式，不是音频网关的组件、依赖或协议。
网关的产品验收终点是 Mac mini 本机正确播放与处理；渠道侧的画面、系统声音转送
和耳机选择由操作者按所用渠道配置。当前产品不发布发言入口，保留资产与恢复方法
集中在 [预留附录](#附录-a-预留发言链路等远程渠道支持麦克风直连)。

产品运行闭包由三部分组成：

- `doctor`：Mac mini 会前体检，校验设备、48kHz、输入增益 30、默认输出、
  麦克风 TCC、磁盘与依赖。USB 输出音量 84 是发言方向预留校准项，偏离时只
  `WARN`，不阻塞当前接收侧产品。
- `bridge`：Mac mini 唯一音频服务，监听 8787，承载接收侧播放、录音、状态、
  告警、同传、参谋与会后处理。
- 原生字幕窗口（`app/MeetingCaptionsWindow.swift`）：会议助手.app 内的
  第一呈现面，直连 bridge WebSocket，显示字幕与参谋建议。工程调试面是
  bridge 自带的 `bridge.html`（`http://127.0.0.1:8787/?t=<token>`）。
  （KVM 页面内的会议音频面板已随 2026-07-31 产品拆分移除。）

操作入口：

- [会前 checklist](docs/runbook-checklist.md)
- [会中操作卡](docs/runbook-in-meeting.md)
- [故障速查表](docs/troubleshooting.md)
- [A7 隔离审计](docs/isolation-audit.md)

`docs/productization-plan-20260729.md` 与 `docs/pr-draft.md` 是历史档案，保留当时
决策语境，不代表当前产品入口。

## 1. 硬件与运行基线

当前产品使用接收方向：

```text
被控机 USB 声卡绿孔 Target_Out ──▶ Mac mini USB 声卡粉孔 Mac_In
```

反向模拟线和相关代码可以继续留在现场，但不属于当前产品运行步骤。

| 项目 | 当前基线 |
|---|---|
| Mac mini 系统输入增益 | 30（doctor `PASS` 容忍范围 30 ±2） |
| Mac mini 系统默认输出 | `Mac mini扬声器` |
| 采样率 | USB 输入/输出均 48000 Hz |
| Mac mini 麦克风 TCC | 会议助手.app；命令行排障时为图形 Terminal |
| 接收侧 | `bridge --monitor "Mac mini扬声器"`，会议声音在 Mac mini 本机播放 |
| USB 输出音量 | 84 ±2（发言方向预留；偏离为 `WARN`，`--fix` 仍可校准） |
| 端到端测试信号 | 被控机会议应用的测试语音或真人语音；纯音可能被降噪消除 |

SSH 启动的采集进程实测会得到精确数字零（约 `-180 dBFS`），与接线无关。
不要把 `--skip-doctor` 作为常规启动参数。

## 2. 安装

在 Mac mini 执行：

```bash
cd ~/audio-gateway
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
brew install switchaudio-osx
```

可选 AI 能力：

```bash
.venv/bin/python -m pip install mlx-whisper
# Intel/兜底可改装 faster-whisper
```

LLM 后端可独立选择：

| 能力 | 默认后端 | 凭据 / 选项 |
|---|---|---|
| 同传 | OpenAI Realtime | `OPENAI_API_KEY`、`--interpret-model` |
| 参谋 | Claude | `ANTHROPIC_API_KEY`、`--advise` |
| 会后纪要 | Claude | `ANTHROPIC_API_KEY`；也可选 OpenAI/Ollama |

Codex CLI 的 `~/.codex/auth.json` 是 ChatGPT OAuth 会话，**不是** API key。
基础接收、播放、录音和字幕/调试面不要求 Whisper 或 LLM 凭据。

## 3. 最短工作流

### 3.0 双击启动（日常入口）

默认构建两个独立产品：

```bash
cd ~/audio-gateway/app
./build-apps.sh
```

产物在 `~/Applications`：

| App | 作用 |
|---|---|
| **会议助手.app** | 同传/会议产品。双击 = 菜单栏会议服务就位。菜单：开始这场会议（⌘R）/ 测试声音（5 秒）/ 结束会议并生成纪要（⌘.）/ 取消这次录音 / 声音来源（本机会议软件 / 外部设备）/ 实时中文字幕 + AI 建议开关 / 打开字幕窗口（⌘J）/ 打开最近一次纪要 / 打开会议文件夹 / 帮助与诊断。字幕在**原生字幕窗口**呈现，不依赖浏览器。bundle id 沿用 `dev.controller-agent.audio-gateway`，麦克风 TCC 授权连续 |
| **KVM 控制台.app** | 纯 KVM 薄启动器。双击 = 打开 KVM 控制台网页（Chrome PWA 优先、降级默认浏览器，`#kvm` 自动登录）后即退出。全新 bundle id `dev.controller-agent.kvm-console`，无麦克风权限声明 |

默认构建不会产出预留发言客户端，菜单也不显示发言静音或客户端命令。

**KVM 控制台窗口形态**：启动器优先启动 Chrome PWA（`~/Applications/Chrome Apps.localized/`
下名字含 `KVM` 的 app）——独立窗口、无地址栏、沿用 Chrome 默认 profile（摄像头等授权继承）。
未安装 PWA 时自动降级为默认浏览器打开同一 URL，功能完全一致。

PWA 一次性安装（已在本机完成；换机时重做）：Chrome 打开 `http://127.0.0.1:4110/#kvm`
→ 右上三点菜单 → 投放、保存和分享 → **将网页作为应用安装** → 名称含 `KVM` → 安装。

> 为什么不由 App 直接启动 Chrome：macOS 26 的「App 管理」保护会拒绝 ad-hoc 签名的
> App 执行其它 App bundle 内的二进制（实测 TCC `kTCCServiceSystemPolicyAppBundles=0`，
> 静默失败、窗口打不开）。PWA 走 LaunchServices，不触碰任何 App bundle，无需敏感权限。

> **重建 app 后会重新弹一次麦克风授权**，这不是故障。`build-apps.sh` 用 ad-hoc 签名，
> 每次重建 cdhash 都会变，TCC 据此认定是另一个 app。bundle id 不变只保证条目不重复，
> 不保证授权延续。对只用不重建的日常使用者，授权一次长期有效；改完代码重新部署时，
> 记得留一次点「允许」的机会（2026-07-30 验收实测两次）。

日常流程：

1. 在 Mac mini 双击 **会议助手.app**，菜单栏选择「开始这场会议」。
2. 开启字幕的会议，原生字幕窗口随会议自动出现（也可从菜单「打开字幕窗口」）。
3. 操作者通过任意远程桌面渠道查看并收听 Mac mini。
4. 会议结束后从菜单栏选择「结束会议并生成纪要」。

需要操控被控机时，另行双击 **KVM 控制台.app**——它与会议流程互不依赖。

菜单栏图标：`🎧` 未启动 / `🎧…` 启动中 / `🎧🟢` 正常 /
`🎧⚠️` 有告警 / `🎧⏳` 正在转写、生成纪要或转码 / `🎧✅` 处理完成。
Swift 端不实现转写或纪要逻辑，状态只以 Python `/status` 为准。

### 3.1 Mac mini：doctor

App 会在启动 bridge 前执行核心 doctor。命令行排障可在 Mac mini 图形 Terminal
运行完整体检：

```bash
cd ~/audio-gateway
.venv/bin/python -m audio_gateway doctor
echo $?
```

判定：

- `FAIL` 才阻塞启动，最终有 `FAIL` 时退出 2。
- USB 输出音量偏离 84 时为 `WARN`，文案标明「发言方向预留项」，退出码仍为 0。
- Whisper 缺失为 `WARN`，不阻塞基础接收侧。
- 输入增益、USB 输出音量和默认输出仍可有限修复：

```bash
.venv/bin/python -m audio_gateway doctor --fix
```

`--fix` 会复检；即使输出音量原本只是 `WARN`，仍会把它校准到 84。TCC、采样率、
缺失设备、磁盘和依赖不会被越权修复。

### 3.2 Mac mini：bridge

命令行排障入口：

```bash
.venv/bin/python -m audio_gateway bridge \
  --monitor "Mac mini扬声器" \
  --port 8787 \
  --record
```

预期：

```text
[bridge] 下行监听已开启（会议声音在 Mac mini 本机播放）
[bridge] 控制面就绪。音频链路=up。
```

bridge 打印本次 token、调试面、恢复入口和停止入口；不会主动发布预留发言
客户端命令。录音写入 `~/AudioGateway/<时间戳>/meeting.wav`。

通过菜单栏「结束会议并生成纪要」、`POST /stop`、`SIGINT` 或 `SIGTERM` 停止时，
四者进入同一优雅路径：

```text
停止音频与同传 → 批量 Whisper 转写 → LLM 纪要 → afconvert 生成 m4a → 退出
```

处理期间 8787 继续提供 `/status`。显式 `--no-postprocess` 只跳过批量转写与
纪要，录音仍正常收尾并转成 m4a。

### 3.3 远程访问：渠道无关

操作者可以使用任意远程桌面渠道连接 Mac mini。只核对两个事实：

1. 能看见 Mac mini 上的原生字幕窗口（及需要时的 KVM 控制台）。
2. 能听见 Mac mini 本机正在播放的会议声音。

具体渠道的声音转送开关、编解码、带宽和耳机路由不写入网关 runbook，也不成为
bridge、doctor 或字幕窗口的工程前提。若 Mac mini 本机可听但操作席听不到，
应在远程渠道侧排障，网关本身仍是接收成功。

### 3.4 字幕与调试面

- **原生字幕窗口**：会议助手.app 的第一呈现面。开启字幕的会议随会议自动
  出现，也可从菜单「打开字幕窗口」（⌘J）手动打开；连接与 token 全自动，
  无需任何输入。
- **工程调试面**：bridge 自带的 `bridge.html`（菜单「帮助与诊断 →
  运行详情（技术）」，即 `http://127.0.0.1:8787/?t=<token>`），显示连接、
  接收侧电平、link、告警、字幕流与参谋，属于会议模块自身的调试工具。

（KVM 页面内的会议音频面板已随 2026-07-31 产品拆分移除；KVM 控制台
不再含任何会议功能。）

当前产品不渲染发言上行电平、静音按钮或上行帧统计；协议字段仍可被解析，
便于未来恢复。

### 3.5 被控机会议应用

在被控机会议应用的音频设置中：

- 扬声器选择连接到 `Target_Out` 的 `USB Audio Device`。
- 播放测试语音，确认 `Mac_In → bridge → Mac mini 本机输出` 可听。
- 调试面（bridge.html）的接收侧电平应同步跳动。

当前产品不把会议应用麦克风或远端听到操作者列为 GO 条件。

## 4. 同传通道（可选）

`bridge --interpret` 把 `Mac_In` 的会议原声送入 OpenAI Realtime translation，
接收日语原文、中文译文和可选中文语音。默认形态是：**Mac mini 本机播放会议
原声、原生字幕窗口显示双语字幕（服务端保留最近 50 句/流可补拉）、译文语音关闭**。

```bash
export OPENAI_API_KEY="<API key>"
.venv/bin/python -m audio_gateway bridge \
  --monitor "Mac mini扬声器" \
  --interpret \
  --interpret-lang zh
```

- 默认模型：`gpt-realtime-translate`。
- `--interpret-device` 默认复用 `--monitor` 解析出的 Mac mini 本机设备。
- 译文语音默认关闭，只能从调试面（bridge.html）、WS 或 `POST /interpret_voice` 显式开启；
  开关等待服务端 state 回播，不做本地乐观更新。
- 译文设备不得解析为 `USB Audio Device` / Mac_Out；命中时 fail-closed。
- 同传失败只影响同传任务，原声监听和录音继续。

双语字幕按 source/translation 到达顺序配对。每句以
`{"type":"sentence", ...}` 推送；新连接和重连通过
`GET /history?t=<TOKEN>` 补拉最近 50 句。`/status` 与 WS state 的
`interpreter` 字段包含 `history_len`、`interpret_voice`、`gated` 和错误状态。

### 4.1 VAD 省费门控

默认 `--interpret-vad-dbfs -50`。会议原声低于阈值持续 3 秒后，只暂停发往
Realtime 的 append；Mac mini 本地采集、录音和播放不受影响。检测到有声立即恢复。

```bash
--interpret-vad-dbfs -45
--interpret-vad-dbfs off
```

### 4.2 会中参谋

参谋默认关闭，必须与同传一起显式开启。它只消费 interpreter 的日语原文
source，绝不消费中文 translation：

```bash
export OPENAI_API_KEY="<OpenAI API key>"
export ANTHROPIC_API_KEY="<Anthropic API key>"
.venv/bin/python -m audio_gateway bridge \
  --monitor "Mac mini扬声器" \
  --interpret \
  --advise
```

会议背景固定读取 `~/AudioGateway/brief.md`；不存在时使用内置通用 brief。
brief 支持会中热重载：每次调用前按 mtime 比对，改完文件即生效，无需重启；
参谋判断 brief 与会议内容明显不符时，`advisor.brief_mismatch` 置 true 供面板提示。
建议以 `{"type":"advice","markdown":"...","t":...}` 推送并保留最近 50 条，
也随 `/history` 补拉，同时逐条落盘 `session_dir/advice.jsonl`（进最终产物清单）。
触发机制保持热词 + 最小间隔/最少新句数节流；在此之上新增失败退避：调用失败
也推进节流时钟，并按 8s 起指数翻倍、封顶 300s 退避（持续故障不再逐句烧 API）。
`/status` 与 WS state 的 `advisor` 字段包含
`calls/delivered/suppressed/last_call_t/last_advice_t/last_error/backoff_until/brief_mismatch`；
错误文本已脱敏。失败会挂一条「参谋:」前缀的降级告警，成功后自动清除。

## 5. CLI 与生命周期

### doctor

```text
audio_gateway doctor [--fix] [--usb USB_KEYWORD] [--output-root OUTPUT_ROOT]
```

### bridge

```text
audio_gateway bridge [--port PORT] [--token TOKEN] [--record]
                     [--no-postprocess] [--whisper auto|mlx|faster]
                     [--monitor MONITOR_KEYWORD] [--usb USB_KEYWORD]
                     [--samplerate SAMPLERATE] [--blocksize BLOCKSIZE]
                     [--summary claude|openai|ollama]
                     [--interpret] [--interpret-lang LANG]
                     [--interpret-device OUTPUT_KEYWORD]
                     [--interpret-model MODEL]
                     [--interpret-vad-dbfs DBFS|off]
                     [--advise] [--skip-doctor]
```

日常停止用音频网关菜单。HTTP 等价入口：

```bash
curl -fsS -X POST "http://127.0.0.1:8787/stop?t=<TOKEN>"
```

`/stop` 幂等；重复调用只返回当前 phase，不重复处理音频。`Ctrl+C`/`SIGTERM`
保留为排障入口，走同一优雅停止路径。

设备修复后只人工重连一次：

```bash
curl -fsS -X POST "http://127.0.0.1:8787/reconnect?t=<TOKEN>"
```

会话产物：

```text
meeting.m4a
meeting.wav
transcript.txt
transcript.jsonl
minutes.md
```

`transcript.jsonl` 把会中 Realtime 句子标为 `"source":"realtime"`，会后
Whisper 段落标为 `"source":"batch"`。`/status` 在整个生命周期公开
`phase=running|post_processing|done`、
`post_processing_step=transcribe|summarize|convert|null`、`session_dir`、
最终产物和降级说明。

缺 Whisper 时录音仍保留；缺 LLM 凭据时只缺 `minutes.md`。单步异常写入最终
摘要并继续后续步骤，不把跳过项伪装成成功。

## 6. 可靠性与故障状态

- 设备消失/流错误：bridge 立即 `link=down`，停止音频，不自动重试。
- 接收侧连续 30 秒精确数字零：`digital-zero (TCC/线路)`。
- 10 秒窗口持续削顶：`clipping (降源音量/查增益)`。
- 调试面显示接收侧帧、电平、tap 丢帧、link 和 alerts。
- 双流 burn-in 与反向 tone 仍作为兼容资产测试保留，不属于当前产品 GO 条件。

故障判定见 [故障速查表](docs/troubleshooting.md)。

## 7. 依赖、隔离与回归

核心依赖：

- `sounddevice` / PortAudio：音频设备 I/O
- `soundfile`：WAV
- `numpy` / `scipy`：PCM 与重采样
- `aiohttp`：bridge HTTP/WS
- `httpx` / `anthropic`：可选 AI 能力
- `mlx-whisper` 或 `faster-whisper`：可选 STT

音频子系统不依赖 CH9329、串口、HID、controlled-text 或 KVM backend 输入链。
字幕窗口与调试面直连 8787；音频操作不授予任何 live 输入权限。审计命令见
[A7 隔离审计](docs/isolation-audit.md)。

```bash
cd audio-gateway
.venv/bin/python -m unittest discover -s tests

cd ../frontend
bun test
bunx tsc --noEmit
```

仓库级 `bun run test:product` 是发布/部署前 no-bypass 硬门。

## 附录 A. 预留：发言链路（等远程渠道支持麦克风直连）

本附录是唯一的发言操作与恢复入口。当前状态是 **mothballed / 非产品**：

- 不删除 `micagent` 模块、`UplinkPlayer`、WS 二进制上行、`mute` 消息、
  `POST /mute`、相关状态字段、测试或反向物理协议。
- 默认构建不产出会议麦克风 App；音频网关菜单不显示静音或复制客户端命令。
- 静态 `bridge.html` 调试面不挂载静音、上行电平或上行统计，也不申请
  麦克风权限、不发送上行帧。（KVM 页面的 React 会议面板已随 2026-07-31
  产品拆分整体删除。）
- bridge 启动不主动打印客户端命令。
- doctor 的 USB 输出音量 84 校准改为非阻断 `WARN`，但 `--fix` 仍会修复。

显式兼容构建仍可用：

```bash
cd ~/audio-gateway/app
./build-apps.sh micagent
```

显式兼容 CLI 仍可用于独立诊断，不属于当前 runbook：

```bash
.venv/bin/python -m audio_gateway micagent \
  --url "http://<GATEWAY_IP>:8787" \
  --token "<TOKEN>"
```

`--listen`、`--mic`、`--test-tone`、服务端静音与本地 `run --mic ...` 行为均保留。
这些入口不得被默认构建、菜单、调试面或正文工作流间接启用。

渠道能力升级后，恢复点集中在以下注释旁：

1. `audio_gateway/static/bridge.html`：`UPLINK_PRODUCT_ENTRY_ENABLED`。
2. `app/GatewayMenuBar.swift`：`mothballedUplinkProductEntriesEnabled`。
3. `audio_gateway/bridge.py`：`_MOTHBALLED_UPLINK_PRODUCT_HINTS_ENABLED`。
4. `app/build-apps.sh`：把 `build_micagent` 加回默认 `all`。
5. doctor：把 USB 输出音量的非阻断分类恢复为产品门禁。
6. 重新发布会前/会中/故障文档，并完成真人语音、静音联动、菜单、调试面、
   默认构建与 A7 现场验收。

（原第一恢复点——KVM 页面 React 会议面板的 `MOTHBALLED_UPLINK_PRODUCT_UI_ENABLED`
——已随 2026-07-31 产品拆分连同该面板删除；如需 UI 恢复点，落在原生字幕窗口。）

只有以上恢复点、文档与现场验收同批完成，才可把该链路重新称为产品能力。
