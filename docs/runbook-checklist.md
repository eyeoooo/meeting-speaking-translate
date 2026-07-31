# 音频网关会前 checklist（渠道无关 / 接收侧）

当前产品形态：

```text
被控机会议扬声器
  → Target_Out
  → Mac_In
  → bridge
  → Mac mini 本机播放 / 录音 / 同传 / 参谋 / 会后处理

操作者
  → 任意远程桌面渠道
  → 看见并听见 Mac mini
```

产品闭环终点是 Mac mini 本机。远程桌面渠道的声音转送、耳机和带宽设置由操作者
按所用渠道配置，不进入 bridge、doctor 或字幕窗口的工程前提。

本清单不包含发言步骤。封存资产只在
[README 预留附录](../README.md#附录-a-预留发言链路等远程渠道支持麦克风直连)
记录，不得从本清单推导为产品入口。

## 0. 开始前的硬条件

以下任一项不满足即 `NO-GO`，不要用 `--skip-doctor` 绕过：

1. Mac mini 与被控机的 USB 声卡在位。
2. 被控机声卡绿孔 `Target_Out` 已连接到 Mac mini 声卡粉孔 `Mac_In`。
3. Mac mini 能打开 `~/audio-gateway`，且 `.venv/bin/python` 可执行。
4. Mac mini 图形会话可用；首次启动音频网关 App 时能够授予麦克风权限。
5. 操作者已通过任意远程桌面渠道连到 Mac mini，并知道如何在该渠道收听
   Mac mini 系统声音。
6. 8787 没有旧 bridge 占用：

```bash
lsof -nP -iTCP:8787 -sTCP:LISTEN
```

预期无输出。若有输出，先确认旧进程与 phase，不按模糊进程名批量终止。

## 1. Mac mini：完整 doctor

日常由音频网关 App 自动执行核心 doctor。会前首次或排障时，在 Mac mini
图形 Terminal 运行：

```bash
cd ~/audio-gateway
.venv/bin/python -m audio_gateway doctor
echo $?
```

预期：

- `USB 声卡输入端` 唯一匹配。
- `USB 声卡采样率` 为 48000 Hz。
- `系统输入增益` 为 `30 ±2`，结果 `PASS`。
- `系统默认输出` 为 `Mac mini扬声器`，结果 `PASS`。
- `麦克风 TCC 可用性` 检测到非零底噪/信号，结果 `PASS`。
- 必需依赖与磁盘为 `PASS`。
- `USB 声卡输出音量` 若偏离 `84 ±2`，结果为 `WARN`，文案包含
  `发言方向预留项`；该项不影响当前接收侧，退出码仍可为 0。
- Whisper 缺失可以是 `WARN`。
- 最终无 `FAIL`，`echo $?` 为 0。

可有限修复：

```bash
.venv/bin/python -m audio_gateway doctor --fix
echo $?
```

`--fix` 仍会校准输入增益、USB 输出音量和默认输出，并完整复检。输出音量即使
只是 `WARN` 也会修到 84；这只保留未来恢复能力，不把它升级为当前 GO 条件。
TCC、采样率、缺失设备、磁盘和 Python 依赖不会被自动修复。

若 TCC 行显示 `1s 样本全体精确为 0`：

1. 确认不是 SSH 启动。
2. 打开 `系统设置 → 隐私与安全性 → 麦克风`，允许音频网关 App；命令行排障
   则允许 Terminal。
3. 完全退出并重开对应 App/Terminal，再跑 doctor。

## 2. Mac mini：启动唯一 bridge

日常在「会议助手」菜单选择「开始这场会议」。命令行排障入口：

```bash
.venv/bin/python -m audio_gateway bridge \
  --monitor "Mac mini扬声器" \
  --port 8787 \
  --record
```

预期至少出现：

```text
[bridge] 下行监听已开启（会议声音在 Mac mini 本机播放）
[bridge] 控制面就绪。音频链路=up。
         调试面:    http://<地址>:8787/?t=<TOKEN>
```

bridge 不主动发布封存客户端命令。记录本次 `<TOKEN>`，不要贴进 issue、聊天或
截图。若 `link=down`，本次为 `NO-GO`，先排查设备，不继续入会。

第二个 Terminal 核验：

```bash
curl -fsS "http://127.0.0.1:8787/status?t=<TOKEN>"
lsof -nP -iTCP:8787 -sTCP:LISTEN
```

预期：

- JSON 包含 `"type":"state"`、`"link":"up"` 和 `phase=running`。
- 只有一个 Python bridge 监听 `*:8787`。
- `muted`、`uplink_frames` 等兼容字段可能仍存在，但不属于当前 UI/GO 条件。

### 2.1 可选：Realtime 同传

只在本场明确需要时启用。默认是 **Mac mini 本机播放会议原声、原生字幕窗口
显示双语字幕、译文语音关闭**：

```bash
export OPENAI_API_KEY="<API key>"
.venv/bin/python -m audio_gateway bridge \
  --monitor "Mac mini扬声器" \
  --port 8787 \
  --record \
  --interpret \
  --interpret-lang zh
```

`--interpret-device` 默认复用 `--monitor`。需要另一只 Mac mini 本机输出设备时
才显式指定。不得把 `USB Audio Device` / Mac_Out 作为译文输出；解析到同一设备
会 fail-closed。

`/status` 预期：

- `interpreter.enabled=true`
- 连接完成后 `interpreter.connected=true`
- `interpreter.interpret_voice=false`
- `interpreter.gated=false`；持续静音后可变 `true`，有声时恢复
- `interpreter.history_len` 随句子增加，最多 50
- `interpreter.error=null`

字幕窗口逐项核对：

1. 双语字幕可滚动；人工上滚后显示「回到最新」且不抢回底部。
2. 断线重连后从 `/history` 恢复历史。
3. VAD 阈值只在确认底噪需要时调整；`off` 会关闭省费门控。
4. 「译文语音」开关在调试面（bridge.html）；启用后等待服务端 state 确认。

### 2.2 可选：会中参谋

参谋只能绑定同传日语原文，不能单独启动，也不能消费中文译文：

```bash
mkdir -p ~/AudioGateway
# 可选：编辑 ~/AudioGateway/brief.md
export OPENAI_API_KEY="<OpenAI API key>"
export ANTHROPIC_API_KEY="<Anthropic API key>"
.venv/bin/python -m audio_gateway bridge \
  --monitor "Mac mini扬声器" \
  --port 8787 \
  --record \
  --interpret \
  --advise
```

预期：

- bridge 打印「参谋已开启：只读取会议日语原文」。
- `state.advisor.enabled=true`。
- brief 存在时 `brief_source=file`；不存在时为 `builtin`。
- 字幕窗口显示参谋建议（调试面区块标题为「会议参谋 · 只读日语原文」）。
- 缺 `ANTHROPIC_API_KEY` 时在 doctor、设备与网络动作前 fail-closed。

## 3. 操作者：核验任意远程桌面渠道

该步骤不规定渠道品牌或具体菜单，只验证访问结果：

1. 操作者能看到 Mac mini 的屏幕（字幕窗口，及需要时的 KVM 控制台）。
2. 操作者能听见 Mac mini 系统声音。
3. 音频没有因远程渠道重复播放而叠加。

若 Mac mini 本机已播放、调试面下行电平已跳动，但操作席听不到，问题边界在
远程桌面渠道或操作席输出设备；不要改 bridge 物理链路来补偿渠道问题。

## 4. 原生字幕窗口（会议的第一呈现面）

会议音频面板已随 2026-07-31 产品拆分从 KVM 页面移除；字幕与建议在
会议助手.app 的原生字幕窗口呈现：

1. 开启「实时中文字幕」的会议，字幕窗口随「开始这场会议」自动出现并连接
   （**零输入**，token 由 App 自动带上）。
2. 手动打开入口：菜单「打开字幕窗口」（⌘J）。
3. 会议结束后窗口显示「会议已结束 · 字幕停止更新」，留作回看，不再重连。
4. 电平 / link / 告警等技术细节看调试面：菜单「帮助与诊断 →
   运行详情（技术）」（即 `http://127.0.0.1:8787/?t=<TOKEN>`）。

预期：

- 字幕窗口状态行无「重连中」提示；字幕持续追加。
- 调试面 `已连接`、`link=up`、`⬇ 会议声音` 电平可响应。
- 不显示发言上行电平、静音按钮、上行帧或上行积压。
- 参谋启用时才显示参谋建议。
- 字幕窗口与调试面都直连 8787，不经过 KVM backend。

调试面 403 表示 token 缺失、错误或属于旧 bridge；从「运行详情」菜单项重开
即可带上当前 token。断线时字幕窗口自动重连，先看 bridge 是否仍在运行。

## 5. 被控机会议应用：接收侧

手工打开会议应用音频设置：

1. `扬声器` 选择连接到 `Target_Out` 的 `USB Audio Device`。
2. 播放应用内测试语音。
3. 预期 Mac mini 本机听到测试语音，调试面 `⬇ 会议声音` 电平跳动。
4. 用一句真人语音或应用测试语音复核清晰度；纯音不作为应用端到端证据。

会议应用麦克风不在当前产品 GO / NO-GO 清单。

## 6. 会后处理预检

若需要录音与自动纪要：

- bridge 使用 `--record`。
- `~/AudioGateway` 所在卷至少有 2 GB 空间。
- Whisper 缺失允许降级，但不会生成批量转写。
- LLM 凭据缺失允许降级，但不会生成 `minutes.md`。
- `/status` 能公开 `session_dir`、`phase` 与 `post_processing_step`。

停止时只使用「会议助手」菜单「结束会议并生成纪要」或一次 `POST /stop`。进入
`phase=post_processing` 后不要杀进程。

## 7. GO / NO-GO

只有以下全部成立才可入会：

- doctor 无 `FAIL` 且退出 0；USB 输出音量预留项允许 `WARN`。
- bridge `link=up`，8787 只有一个监听进程。
- Mac mini 本机能听到被控机会议应用测试语音。
- 操作者可通过所选远程桌面渠道看到并听到 Mac mini。
- 菜单栏无 🔴/🟠 告警；调试面接收侧电平可响应，且无发言产品控件。
- 若启用同传：连接正常、历史补拉可用、译文语音默认关闭。
- 若启用参谋：`advisor.enabled=true`，brief 来源与文件事实一致。
- 若启用录音：磁盘充足，`session_dir` 已出现。

任一项不成立即 `NO-GO`。按 [故障速查表](troubleshooting.md)处理；会中控制与
结束流程见 [会中操作卡](runbook-in-meeting.md)。

## 8. 前端部署纪律（改动 KVM 控制台前端后）

前端产物同步到 `frontend-dist` 之后，**只允许**重启前端这一个 launchd job：

```bash
launchctl kickstart -k gui/$UID jp.controller-agent.macos-kvm.frontend
```

**绝不整跑安装器**（`macos-kvm-launchd-install.mjs` 或任何等价的一键安装脚本）。
安装器会 `bootout`/`bootstrap` 全部 5 个 job——包括持有 CH9329 串口的 `input`
job。串口持有进程被强制退出后，按本仓历史（CH340 tcsetattr 驱动楔死）可能只有
重启 Mac mini 才能恢复；一次「顺手重装」的代价是整条 KVM 输入链路。

部署后必须核验用户屏幕上跑的确实是新版本：

```bash
tail -5 ~/Library/Application\ Support/ControllerAgent/macos-kvm/logs/frontend.stderr.log
```

- 必须看到新 hash 的 `index-*.js` 返回 `200`，且没有任何 404。
- 若浏览器请求了已删除的旧 chunk（404），说明它还在用缓存的旧 `index.html`：
  重新双击「KVM 控制台.app」——降级 URL 自带随部署变化的
  cache-busting query，会强制重取文档。
