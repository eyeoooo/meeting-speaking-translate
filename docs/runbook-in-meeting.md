# 音频网关会中操作卡（渠道无关 / 接收侧）

本卡适用于已按 [会前 checklist](runbook-checklist.md)完成 `GO` 判定的当前产品：
Mac mini 本机接收、播放、录音与 AI 处理；操作者通过任意远程桌面渠道查看和
收听 Mac mini。

发言链路不在本卡操作范围。保留实现与恢复点只见
[README 预留附录](../README.md#附录-a-预留发言链路等远程渠道支持麦克风直连)。

## 正常状态

每隔一段时间扫一眼菜单栏图标与原生字幕窗口：

- 菜单栏：`🎧` 后的录音计时每秒走字（这就是「还在录」的证明），无 🔴/🟠 告警
- 字幕窗口（若开启字幕）：字幕持续追加，无「重连中」提示
- 会议声音由 Mac mini 本机 `--monitor` 播放
- 需要看电平/link 等技术细节时，打开「帮助与诊断 → 运行详情（技术）」
  （bridge.html 调试面）：连接 `已连接`、`link=up`、`⬇ 会议声音` 电平跳动
- 产品不显示发言静音、上行电平或上行统计

若启用同传：

- 状态为 `已连接`，字幕持续追加。
- 译文语音为 `关（默认）`，除非本场明确开启。
- `VAD 已暂停计费流` 只表示静音段不再 append，不影响本机原声与录音。
- 人工上滚后显示「回到最新」，不会被新句抢回底部。

若启用参谋：

- 只有 `advisor.enabled=true` 时显示区块。
- 标题明确为「只读日语原文」。
- brief 来源与 `~/AudioGateway/brief.md` 的真实存在状态一致。

## 远程桌面渠道边界

远程渠道只负责把 Mac mini 的画面与系统声音送到操作者：

- Mac mini 本机有声、调试面有下行电平，但操作席无声：排查远程渠道和操作席
  输出设备，不改网关物理链。
- Mac mini 本机无声或调试面无下行电平：进入网关接收侧排障。
- 更换远程渠道不需要修改 bridge、字幕窗口、音频协议或 KVM backend。

## 临场故障处置

### 1. 字幕窗口连接断开

字幕窗口状态行显示 `连接断开，自动重连…`（调试面为 `连接断开，3 秒后自动重连`）。

1. 查看 Mac mini 的音频网关菜单或 bridge 日志，确认进程仍在运行。
2. 等待一次自动重连，不连续点击连接。
3. 若返回 403，从当前 bridge 重新取得同一次 token。
4. 若 bridge 已退出，重新执行完整 doctor，再启动新会话。

不要并行启动第二个 bridge；8787 只能有一个监听者。

### 2. 菜单栏出现 🔴「声音通道断了」（调试面 `link=down`）

`link=down` 表示 bridge 已 fail-closed 停止音频流，不会自动重试。

1. 告知参会者接收侧音频故障。
2. 检查两块 USB 声卡与接收方向模拟线。
3. 在“音频 MIDI 设置”确认输入/输出仍为 48000 Hz。
4. 只有物理/设备问题已修复后，执行一次：

```bash
curl -fsS -X POST "http://127.0.0.1:8787/reconnect?t=<TOKEN>"
```

成功预期：`"reconnect":"ok"`、`"link":"up"`，bridge 打印
`人工恢复成功：link=up。`。503 时保持 down 并继续排查；不要盲目重复 POST。
409 `reconnect:not-needed` 表示链路本来已 up。

### 3. 出现 `digital-zero (TCC/线路)`

该告警表示接收侧连续 30 秒精确数字零。

1. 确认被控机会议应用确实在播放声音。
2. 检查 `Target_Out → Mac_In` 接线。
3. 若 bridge 经 SSH 启动，结束它；SSH 采集实测会因 TCC 身份得到精确零。
4. 在 Mac mini 图形 Terminal 运行：

```bash
cd ~/audio-gateway
.venv/bin/python -m audio_gateway doctor
```

5. TCC 失败时允许音频网关 App/Terminal 使用麦克风，完全重启对应进程。
6. doctor 退出 0 后重新开始会议（字幕窗口与调试面的 token 由 App 自动带上）。

### 4. 出现 `clipping (降源音量/查增益)`

1. 降低被控机会议应用的扬声器源音量。
2. 尽快结束当前 bridge。
3. 在 Mac mini 图形 Terminal 运行：

```bash
cd ~/audio-gateway
.venv/bin/python -m audio_gateway doctor --fix
```

4. 确认 `系统输入增益` 回到 `30 ±2` 且为 `PASS`，再启动 bridge。

### 5. 操作者听不到会议声音

按证据分层：

1. 被控机会议应用扬声器是否为 `USB Audio Device`。
2. 调试面（bridge.html）接收侧电平是否跳动。
3. Mac mini 本机是否能直接听到会议声音。
4. bridge 是否带 `--monitor "Mac mini扬声器"`。
5. Mac mini 默认输出是否仍为 `Mac mini扬声器`。
6. 若 1–5 正常而操作席无声，只排查所用远程桌面渠道的系统声音转送与操作席
   输出设备。

### 6. 同传连接失败

先确认主链 `link=up`。同传故障不应影响录音与本机原声：

1. 查看 `/status` 的 `interpreter.error`、`alerts` 与 bridge 日志。
2. 客户端从 2 秒开始指数退避，最高 30 秒；不要反复重启 bridge。
3. 核对 `OPENAI_API_KEY` 与账号可用模型。
4. 网络/账户恢复后等待下一次自动重连。
5. `/history` 补拉失败时实时流仍继续；保留告警并核对 8787 token。

### 7. 译文语音失败或延迟变大

- 译文语音按钮等待服务端 state，不做本地乐观更新。
- 播放器启动失败只进入 alerts，文字字幕继续。
- `--interpret-device` 必须是 Mac mini 本机输出，不能解析为
  `USB Audio Device` / Mac_Out。
- 输入至少累计 100ms 后发送，这是协议下限，不改成逐 21ms 回调帧。
- 保持 48000 Hz，确认只有一个 bridge/同传会话。

### 8. 参谋没有建议

1. `advisor.enabled` 必须为 `true`。
2. 同传必须已启用并持续产生日语原文 source。
3. 参谋不会读取中文 translation。
4. 先看 `/status` 的 `advisor` 计数，三种状态一眼可分：
   - `last_error` 非空、`backoff_until` 未来时刻 → 调用失败，正在指数退避
     （8s 起翻倍、封顶 300s），恢复后自动继续并清除告警；
   - `calls` 在涨、`suppressed` 同步在涨 → 参谋看过了，认为暂无需提示；
   - `calls` 恒为 0 → 触发条件未满足或参谋没收到 source，查同传。
5. 热词、最小新 segment 数和调用间隔会节流；短暂空白不等于故障。
6. `brief_source=builtin` 表示文件不存在，是如实降级，不是假装读过文件。
7. brief 可会中直接编辑，下一次建议前按 mtime 自动重载；
   `advisor.brief_mismatch=true` 表示参谋判断 brief 与本场内容明显不符。
8. 建议逐条落盘 `session_dir/advice.jsonl`，会后可复核参谋当时说了什么。

## 结束会议

日常结束只使用会议助手菜单，避免在会后处理期间误杀 bridge：

1. 在会议应用离开会议。
2. 在「会议助手」菜单选择 **结束会议并生成纪要**。
3. 菜单图标变为 `🎧⏳`，状态依次显示：
   - 批量转写
   - 生成纪要
   - 转换 m4a
4. 此时不要退出 App、不要再次停止、不要强制结束 Python。
5. 完成后 App 显示 `🎧✅` 并弹出「会后处理完成」；打开会话目录，核对实际
   产物与降级说明。
6. 最后断开所用远程桌面渠道；该动作不影响已经完成的网关产物。

菜单无法操作时，`POST /stop` 是幂等等价入口：

```bash
curl -fsS -X POST "http://127.0.0.1:8787/stop?t=<TOKEN>"
```

返回 `stop=accepted` 或 `stop=already-requested` 后只观察 `/status`：

```bash
curl -fsS "http://127.0.0.1:8787/status?t=<TOKEN>"
```

- `phase=running`：尚未接受停止。
- `phase=post_processing`：正在正常处理，禁止杀进程。
- `phase=done`：处理完成，等待进程退出。
- `post_processing_step=transcribe|summarize|convert|null`：当前具体 step。

`Ctrl+C`/`SIGTERM` 也走同一优雅停止路径，但只作排障入口。

完成通知后可核验 8787 已释放：

```bash
lsof -nP -iTCP:8787 -sTCP:LISTEN
```

预期无输出。若仍有输出，先读取 `/status` 与 App 日志；只有明确故障并保全
`meeting.wav` 后才由人工处置，不用模糊进程名批量杀进程。
