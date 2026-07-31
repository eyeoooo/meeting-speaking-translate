# 音频网关故障速查表（渠道无关 / 接收侧）

命令默认从 Mac mini 的 `~/audio-gateway` 执行。doctor/bridge 的命令行排障入口
必须由图形 Terminal 启动；SSH 进程没有相同的麦克风 TCC 身份。

发言兼容资产已封存，相关诊断不在本表。唯一恢复说明见
[README 预留附录](../README.md#附录-a-预留发言链路等远程渠道支持麦克风直连)。

## 一眼判定

| 症状 / 证据 | 优先根因 | 立即动作 |
|---|---|---|
| capture 持续 `-180.0 dBFS`，或 doctor 为 `1s 样本全体精确为 0` | TCC 未授权；SSH 启动实测必现 | 改用 Mac mini 图形 App/Terminal，修复麦克风权限 |
| peak 到 `0.0 dBFS`，panel 报 `clipping` | 输入增益或被控机源音量过高 | `doctor --fix` 恢复增益 30，再降低源音量 |
| panel 有下行电平，Mac mini 本机无声 | `--monitor`、默认输出或本机音量错误 | 核对 bridge 参数与 `Mac mini扬声器` |
| Mac mini 本机有声，操作席无声 | 远程桌面渠道或操作席输出设备 | 只在渠道侧排障，不改网关物理链 |
| panel 无下行电平 | 被控机扬声器设备、Target_Out→Mac_In 接线或 TCC | 按第 1、3、4 节分层检查 |
| USB 输出音量偏离 84 为 `WARN` | 发言方向预留校准漂移 | 当前接收侧可继续；需要保留校准时运行 `doctor --fix` |

## 1. 精确数字零 / `-180 dBFS`

### 诊断

在 Mac mini 图形 Terminal：

```bash
cd ~/audio-gateway
.venv/bin/python -m audio_gateway doctor
.venv/bin/python -m audio_gateway verify capture --seconds 5
```

确定性证据：

- doctor：`麦克风 TCC 可用性 | 1s 样本全体精确为 0 | ... | FAIL`
- capture：每秒 RMS/peak 都是 `-180.0 dBFS`

这类精确数字零不是普通静音底噪。若命令经 SSH 启动，优先判定为 TCC 身份错误，
不要先猜接线。

### 修复

1. 停止 SSH/LaunchAgent 启动的 bridge。
2. 打开 `系统设置 → 隐私与安全性 → 麦克风`。
3. 允许音频网关 App；命令行排障则允许 Terminal。
4. 完全退出并重开对应 App/Terminal。
5. 重跑 doctor，预期 `1s 采样检测到非零底噪/信号`、退出 0。
6. 再启动 bridge。

`doctor --fix` 不会也不应自动修改 TCC。

## 2. peak `0 dBFS` / clipping

```bash
cd ~/audio-gateway
.venv/bin/python -m audio_gateway doctor
```

若 `系统输入增益` 超出 `30 ±2`，或 panel/bridge 出现
`clipping (降源音量/查增益)`：

```bash
.venv/bin/python -m audio_gateway doctor --fix
echo $?
```

预期输入增益约 30、结果 `PASS`、退出 0。仍削顶时降低被控机会议应用扬声器
源音量；不要把 Mac mini 输入增益重新拉高。

## 3. doctor 设备缺失或歧义

症状是 USB 输入/输出显示 `0 个匹配` 或 `2 个匹配`。doctor 会 fail-closed：

```bash
cd ~/audio-gateway
.venv/bin/python -m audio_gateway list-devices
```

默认关键字是 `USB Audio Device`。只有名称确实不同且可唯一匹配时才覆盖：

```bash
.venv/bin/python -m audio_gateway doctor --usb "<唯一设备名关键字>"
.venv/bin/python -m audio_gateway bridge \
  --usb "<唯一设备名关键字>" \
  --monitor "Mac mini扬声器"
```

不要用设备索引猜测，也不要在设备不唯一时运行 `--fix`。

## 4. 有下行电平但无声，或有声但 panel 无电平

### panel 有下行电平，Mac mini 本机无声

1. bridge 命令必须包含 `--monitor "Mac mini扬声器"`。
2. doctor 的 `系统默认输出` 必须为 `Mac mini扬声器`。
3. 核对 Mac mini 系统输出音量与静音状态。
4. `本页播放` 默认关闭，不把它当作 `--monitor` 的替代基线。

### Mac mini 本机有声，操作席无声

网关接收侧已经成功。核对所用远程桌面渠道：

- 是否启用 Mac mini 系统声音转送。
- 操作席系统输出是否为目标耳机/扬声器。
- 渠道是否因带宽、编解码或会话重连暂停声音。

渠道设置不写回 bridge 参数，也不成为仓库工程依赖。

### panel 无下行电平

1. 被控机会议应用扬声器选择 `USB Audio Device`。
2. 检查 `Target_Out → Mac_In` 模拟线方向。
3. 运行 `verify capture --seconds 5`。
4. 若连续精确零，回到第 1 节。

## 5. USB 输出音量 84 的新语义

USB 输出音量只服务封存的反向链路。偏离 84 时 doctor 应显示：

```text
USB 声卡输出音量 | <实测> | 84 ±2（发言方向预留） | WARN
```

该 `WARN` 不影响退出码；若其他项都通过，doctor 返回 0。校准能力仍保留：

```bash
.venv/bin/python -m audio_gateway doctor --fix
```

预期 `--fix` 把值设回 84、恢复原默认输出并复检为 `PASS`。不要把该 `WARN`
误报为当前接收侧 `NO-GO`，也不要删除校准实现。

## 6. 调试面 403、字幕窗口无法连接或不停重连

### 403

1. 从「会议助手」菜单「帮助与诊断 → 运行详情（技术）」重开调试面，
   token 会自动带上（字幕窗口的 token 全自动，无需手填）。
2. 预期 `已连接`、`link=up`。

### 无法连接

```bash
curl -fsS "http://127.0.0.1:8787/status?t=<TOKEN>"
lsof -nP -iTCP:8787 -sTCP:LISTEN
```

预期 JSON `link=up`，且只有一个 Python 进程监听 `*:8787`。本机 panel 地址使用
`ws://127.0.0.1:8787/ws`。bridge 已退出时，重新完整 doctor，再启动新会话。

### 自动重连

panel 断开后等待 3 秒再创建新 WebSocket。不要连续点击连接。认证 close 会停止
自动重连并显示 403；普通断线才继续重试。

## 7. `link=down` / PortAudio 告警

设备消失或流错误后 bridge fail-closed，保留控制面但停止音频，不自动重试。
先修复声卡、采样率和接线，再执行一次：

```bash
curl -fsS -X POST "http://127.0.0.1:8787/reconnect?t=<TOKEN>"
```

- 成功：`"reconnect":"ok"`、`"link":"up"`。
- 503：恢复失败，保持 down，继续排查。
- 409 `reconnect:not-needed`：链路本来已 up。

不要连续 POST。

## 8. Realtime 同传故障

### `--interpret` 报未设置 key

这是启动前 fail-closed，尚未建立计费连接：

```bash
export OPENAI_API_KEY="<API key>"
.venv/bin/python -m audio_gateway bridge \
  --monitor "Mac mini扬声器" \
  --interpret
```

Codex/ChatGPT 登录状态不是 API key。不要把 key 写进脚本、task、issue、截图或
shell trace。

### 连接失败或反复重连

1. 先确认主链 `link=up`；同传故障不应影响录音与本机原声。
2. 查看 `interpreter.error`、alerts 与 `[interpreter][ALERT]`。
3. 客户端从 2 秒开始指数退避，最高 30 秒；不要并行启动第二个 bridge。
4. 网络/账户恢复后等待下一次自动重连。

### `/history` 补拉失败

- 实时 sentence/advice 流仍继续。
- 核对 token 与 8787。
- history 最多保留每流 60 句字幕和 50 条建议。
- 不要因为补拉失败重启仍在工作的同传会话。

### 译文语音无法开启

- 按钮必须等待服务端 state 回播。
- 播放器失败只进入 alerts，文字流继续。
- `--interpret-device` 只能指向 Mac mini 本机输出。
- 解析为 `USB Audio Device` / Mac_Out 时应 fail-closed。

### 译文延迟明显变大

- 输入至少累计 100ms 后发送，这是固定协议批量下限。
- 确认只有一个 bridge/同传会话，网络无明显丢包。
- 保持 48000 Hz；非 48000 Hz 会在启动时拒绝。
- 避免同时开启 `--monitor` 与 `本页播放` 造成双重放音错觉。

## 9. 参谋故障

- `--advise` 必须与 `--interpret` 同时使用。
- 缺 `ANTHROPIC_API_KEY` 时应在设备和网络动作前 fail-closed。
- Advisor 只接收日语原文 source，不接收中文 translation。
- `brief_source=builtin` 表示 `~/AudioGateway/brief.md` 不存在。
- 建议有热词、最少新 segment 数和时间间隔节流；短时间无建议不等于断线。
- 调用失败自动指数退避（8s 起翻倍、封顶 300s），失败同样推进节流时钟——
  持续故障不会逐句烧 API。看 `/status` 的 `advisor.last_error`（已脱敏）与
  `backoff_until`；恢复后「参谋:」降级告警自动清除，不需要人工干预。
- `advisor.calls/delivered/suppressed` 区分「没触发 / 已投递 / 看过但观望」；
  `brief_mismatch=true` 表示参谋判断 brief 与本场会议内容明显不符
  （brief 可会中直接编辑，下次调用前按 mtime 热重载）。
- 建议历史最多 50 条，通过 `/history` 恢复，并逐条落盘
  `session_dir/advice.jsonl`（列入最终产物清单）。

## 10. 会后处理长时间未退出

先读状态，不直接杀进程：

```bash
curl -fsS "http://127.0.0.1:8787/status?t=<TOKEN>"
```

- `phase=post_processing`：正在执行
  `transcribe|summarize|convert`，继续等待。
- `phase=done`：处理已完成，等待进程自然退出。
- `post_processing_notes`：记录缺依赖、缺凭据或单步异常的如实降级。
- `session_dir`：核对 `meeting.wav`、`meeting.m4a`、transcript 和 minutes 的实际存在。

`POST /stop` 幂等，重复调用不会重复转写。只有明确卡死并已保全 `meeting.wav`
后才人工处置，不用模糊进程名批量杀进程。
