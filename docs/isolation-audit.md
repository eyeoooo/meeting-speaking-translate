# A7 音频子系统隔离审计

## 结论与当前产品边界

音频网关是 KVM 产品旁路的音频子系统。当前产品闭包是 Mac mini 本机接收、
播放、录音和 AI 处理：

- 使用 PortAudio/CoreAudio 访问音频设备。
- bridge 仅在 TCP 8787 提供 HTTP/WebSocket 音频与状态面。
- 原生字幕窗口与 bridge.html 调试面直接连接 8787，不经过 KVM backend
  （KVM 页面的会议音频 panel 已随 2026-07-31 产品拆分移除）。
- doctor 是短生命周期本地体检，只调用音频 API、`osascript` 和
  `SwitchAudioSource`。
- 操作者经任意远程桌面渠道查看和收听 Mac mini；渠道不进入仓库依赖图。

发言实现、测试和协议作为封存兼容资产保留，但不进入默认构建、菜单、panel、
静态页采集或 bridge 启动提示。该封存不改变 A7：源码与依赖审计仍未发现
CH9329、串口、HID、controlled-text 或被控端输入 dispatch 依赖。

该结论只证明“音频代码没有进入输入控制链”。它不是 CH9329 owner、KVM live
输入或 controlled-text 的授权，也不证明那些链路的运行状态。

AGENTS.md 铁律 5/6 继续完整适用：音频 panel 的连接、本页播放、同传语音与历史
补拉只使用 8787 音频协议，不生成键盘、鼠标、IME 或文字输入动作。

以下命令均从 KVM 仓库根目录执行。静态命令用于 review；进程/端口命令必须在
目标 Mac mini 的实际 bridge 上执行。

## 进程与资产清单

### 当前产品进程

| 机器 | 进程/组件 | 生命周期 | 允许的 I/O | 禁止/不存在 |
|---|---|---|---|---|
| Mac mini | `.venv/bin/python -m audio_gateway doctor` | 会前短进程 | CoreAudio 查询/1s 采集、磁盘、Python import、`osascript`、`SwitchAudioSource` | 无监听端口；不访问 KVM input |
| Mac mini | `.venv/bin/python -m audio_gateway bridge ...` | 会议期间唯一常驻音频进程 | Mac_In、本机 monitor、录音、可选 AI；监听 `0.0.0.0:8787` | 不连接 KVM backend；不打开串口/HID |
| Mac mini | 原生字幕窗口（会议助手.app 内 `MeetingCaptionsWindow`） | 会议助手.app 内窗口 | 直连 8787 WebSocket；向同一 bridge 发 token-gated `GET /history` | 无 backend route；无输入 dispatch；无发言控件 |
| Mac mini 浏览器 | `bridge.html` 调试面 | 8787 自带静态页 | 直连 8787 WebSocket / `GET /history` | 不申请麦克风；不发送上行帧 |

`bridge --record` 写 `~/AudioGateway/<时间戳>/meeting.wav`，停止后可调用 Whisper、
LLM 与 macOS `afconvert`。这些是文件/音频处理，不改变隔离边界。

### 封存兼容资产

| 资产 | 保留内容 | 当前产品隔离 |
|---|---|---|
| `audio_gateway.micagent` | 客户端实现与全部测试 | 不由默认构建、菜单、bridge 提示或 runbook 启动 |
| `UplinkPlayer` / WS binary / `mute` / `POST /mute` | 服务端协议与测试 | bridge 兼容实现保留，产品 UI 不提供触发入口 |
| `会议麦克风.app` | Swift 源码和显式 build target | 只可 `./build-apps.sh micagent` 显式构建，不在默认/all |
| static panel uplink UI（bridge.html） | 完整恢复代码 | feature flag 固定为 false；静态页不申请麦克风、不发送上行帧。（KVM 页面 React 会议面板已随 2026-07-31 产品拆分整体删除，其 flag 一并消失） |

## 封存门静态核验

```bash
rg -n \
  'UPLINK_PRODUCT_ENTRY_ENABLED = false|mothballedUplinkProductEntriesEnabled = false|_MOTHBALLED_UPLINK_PRODUCT_HINTS_ENABLED = False' \
  audio-gateway/audio_gateway/static/bridge.html \
  audio-gateway/app/GatewayMenuBar.swift \
  audio-gateway/audio_gateway/bridge.py

rg -n 'all\).*build_gateway; build_kvm' audio-gateway/app/build-apps.sh
```

预期四个恢复开关和默认双 App 构建全部命中。再确认协议/测试仍在：

```bash
rg -n 'class UplinkPlayer|add_post\("/mute"|data\.get\("type"\) == "mute"' \
  audio-gateway/audio_gateway/bridge.py
rg -n 'micagent|uplink|mute' audio-gateway/tests --glob '*.py'
```

预期有命中；封存不是删除。

## 监听端口与网络面

唯一新增监听端口是 bridge 的 TCP 8787：

```bash
lsof -nP -iTCP:8787 -sTCP:LISTEN
```

正常运行预期恰好一行 Python bridge 监听 `*:8787 (LISTEN)`。doctor 已退出，
默认产品没有第二个音频 Python 常驻进程。

解析唯一 PID：

```bash
AUDIO_GATEWAY_PID="$(lsof -tiTCP:8787 -sTCP:LISTEN)"
test -n "$AUDIO_GATEWAY_PID"
test "$(printf '%s\n' "$AUDIO_GATEWAY_PID" | wc -l | tr -d ' ')" = "1"
ps -p "$AUDIO_GATEWAY_PID" -o pid=,ppid=,command=
lsof -nP -a -p "$AUDIO_GATEWAY_PID" -iTCP
```

预期命令包含 `python -m audio_gateway bridge`。TCP socket 只应包括 8787 LISTEN、
字幕窗口/调试面到 8787 的连接，以及显式 AI 后端连接；不应出现 KVM input
daemon 端口。

源码网络入口/出口：

```bash
rg -n 'TCPSite\(runner, "0\.0\.0\.0", port\)|ClientSession\(\)|/history' \
  audio-gateway/audio_gateway/bridge.py \
  audio-gateway/audio_gateway/micagent.py
```

命中包括 bridge 的 `TCPSite`、封存客户端的 `ClientSession`，以及 bridge 自身的
`/history` 端点（消费方是原生字幕窗口与 bridge.html 调试面）。源码存在的封存
客户端不是默认运行态进程。

## 静态依赖核验

### 1. Python import 不含输入/串口模块

```bash
if rg -n -i '^\s*(from|import)\s+.*(serial|ch9329|hid|controlled[-_ ]?text)' \
  audio-gateway/audio_gateway --glob '*.py'; then
  echo 'FAIL: found forbidden input/serial import'
  exit 1
else
  echo 'PASS: no forbidden input/serial import'
fi
```

### 2. requirements 不含输入/串口库

```bash
if rg -n -i '(pyserial|serialport|hidapi|pyusb|ch9329|controlled[-_ ]?text)' \
  audio-gateway/requirements.txt; then
  echo 'FAIL: found forbidden input/serial dependency'
  exit 1
else
  echo 'PASS: no forbidden input/serial dependency'
fi
```

### 3. 不调用 KVM 输入入口

```bash
if rg -n -i '(/hid/command|serial-hid|triggerDispatch|createAction|controlled-text|macos-kvm.*input)' \
  audio-gateway/audio_gateway --glob '*.py'; then
  echo 'FAIL: found KVM input call/reference'
  exit 1
else
  echo 'PASS: no KVM input call/reference'
fi
```

### 4. 会议模块不经 KVM backend / frontend

2026-07-31 产品拆分后，KVM 前端不再含任何会议组件，核验收敛为两条：

```bash
if rg -n -i '(audio[-_ ]gateway|8787)' backend; then
  echo 'FAIL: backend contains audio gateway coupling'
  exit 1
else
  echo 'PASS: backend has no audio gateway coupling'
fi

if rg -n 'MeetingAudio|8787' frontend/src; then
  echo 'FAIL: KVM frontend still contains meeting-audio coupling'
  exit 1
else
  echo 'PASS: KVM frontend has no meeting-audio coupling'
fi
```

`/history` 是 8787 自身端点，不经过 KVM backend。`interpret_voice` 控制也通过
同一 WebSocket。封存的 mute 发送代码仍在 false feature branch 内，不能由产品
UI 触发。

## 运行时文件句柄核验

bridge 运行时检查它没有打开串口设备：

```bash
lsof -nP -p "$AUDIO_GATEWAY_PID" | \
  rg -i '/dev/(cu|tty)\.|usbserial|ch9329|serial-hid'
```

预期无输出，`rg` 退出码为 1。任何命中都使 A7 失败。

核对在跑的音频命令：

```bash
ps -axo pid=,ppid=,command= | \
  rg '[p]ython.*-m audio_gateway (doctor|bridge|micagent)'
```

当前产品会议中预期只有一个 bridge；doctor 已退出，也不应有封存客户端进程。
显式兼容诊断必须使用独立验收范围，不能混入当前产品 A7 运行态。

## 产物卫生

```bash
if git ls-files audio-gateway | \
  rg '(^|/)(\.venv|__pycache__)(/|$)|\.pyc$'; then
  echo 'FAIL: generated Python artifact is tracked'
  exit 1
else
  echo 'PASS: no tracked .venv/__pycache__/pyc'
fi
```

## 审计判定

A7 只有在以下证据同时成立时通过：

1. 封存 feature flags、默认双 App 构建与协议保留扫描符合预期。
2. import、requirements 和 input-call 扫描全部 PASS。
3. backend coupling 扫描 PASS，panel 仅直连 8787。
4. 运行态恰好一个 bridge 监听 8787，文件句柄无串口/HID。
5. 默认产品运行态没有封存客户端进程。
6. PR 未携带 `.venv`、`__pycache__` 或 `.pyc`。

本文记录审计方法；静态 PASS 不能替代目标 Mac mini 运行态证明，也不授予任何
live-HID 权限。
