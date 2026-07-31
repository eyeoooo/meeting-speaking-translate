# PR draft

Target branch: `main`

Source branch: `claude/mac-mini-audio-gateway-ai-2cbd88`

## PR title

```text
feat(audio-gateway): productize the UU meeting audio gateway
```

## PR body

### 变更摘要

把 Mac mini 物理隔离音频网关从已贯通原型收口为可交接的 UU 远程会议产品：

- 加入 `doctor` 一键体检与 `run` / `bridge` 启动 fail-closed 门禁，固化输入增益
  30、USB 输出音量 84、默认输出 `Mac mini扬声器` 和 Terminal TCC 基线。
- 加固 bridge/micagent：设备异常显式 `link=down`、电平哨兵、运行计数器、单次
  人工 `/reconnect`、双流 burn-in，以及 micagent 重连时单一按键线程。
- 在 macOS native KVM 页面加入会议音频 panel；浏览器直连网关 8787，不经过
  KVM backend，显示服务端权威静音、双向电平、link 与告警。
  （历史注：该面板已随 2026-07-31 产品拆分移除，字幕改由原生字幕窗口呈现。）
- 固化最终 UU 形态：下行由
  `bridge --monitor "Mac mini扬声器"` + UU 声音同步承载；上行由 MacBook
  `micagent` 承载。UU 的 macOS 被控端不支持麦克风直连。
- 新增会前 checklist、会中操作卡、故障速查表和 A7 隔离审计；README 已改为
  bridge + micagent + doctor + panel 的推荐入口。

### 运行基线与已知边界

- Mac mini 系统输入增益：30（doctor 容忍 `30 ±2`）。
- USB 声卡输出音量：84（出厂 53 实测过弱）。
- 麦克风 TCC 身份：Mac mini 图形桌面 `Terminal`；SSH 采集实测为精确数字零，
  与接线无关。
- Zoom 端到端测试必须使用真人语音；纯音实测会被噪声抑制整体消除。
- MacBook 必须戴耳机；micagent 没有 AEC。
- `--skip-doctor`、`--listen`（UU 推荐路径中）和 `--test-tone`（端到端验收中）
  均不进入标准操作流程。

### 四个任务的验收证据

| Task | 状态 / 证据入口 |
|---|---|
| TASK-20260730-001 doctor selfcheck | [handoff](../../.ai/handoffs/2026-07-30-002052-doctor-selfcheck.md) — 票面 `completed`；16/16 单测、真机增益破坏/修复、SSH TCC 数字零和坏设备 fail-closed 验收 |
| TASK-20260730-002 KVM meeting panel | [handoff](../../.ai/handoffs/2026-07-30-011433-kvm-meeting-panel.md) — 票面 `completed`；panel 5/5、失败集无新增、协议/无 backend 污染验收 |
| TASK-20260730-003 reliability hardening | [handoff](../../.ai/handoffs/2026-07-30-003535-reliability-hardening.md) — 票面 `completed`；46/46 单测、真机哨兵与 burn-in 10 分钟 PASS |
| TASK-20260730-004 docs/audit/merge prep | [handoff](../../.ai/handoffs/2026-07-30-013233-docs-audit-merge.md) — `in_review`；文档、静态 A7 与本轮回归证据，待验收方实操 checklist 和运行态 A7 |

这些 handoff 是对应 run 的证据指路；最终验收边界仍以 task 状态、run checks 和
验收方现场结果为准。

### A7 隔离审计

[A7 音频子系统隔离审计](isolation-audit.md)列出进程、端口、依赖与可执行核验
命令。当前静态核验结果：

- 无 serial/CH9329/HID/controlled-text Python import。
- `requirements.txt` 无输入/串口依赖。
- 音频源码不调用 `/hid/command`、`triggerDispatch`、`createAction` 或
  controlled-text/KVM input 入口。
- backend 无 audio-gateway/8787 coupling。
- 当时的会议音频面板组件只创建直接 WebSocket，无 `fetch`/`axios`/`/api/`
  路径。（该组件已随 2026-07-31 产品拆分删除。）
- 未跟踪 `.venv`、`__pycache__` 或 `.pyc`。

验收方仍须在目标 Mac mini 启动 bridge 后执行文档中的 `lsof`、`ps` 和文件句柄
检查；静态 PASS 不替代运行态证明，也不授予任何 live-HID 权限。

### 文档

- [会前 checklist](runbook-checklist.md)
- [会中操作卡](runbook-in-meeting.md)
- [故障速查表](troubleshooting.md)
- [A7 隔离审计](isolation-audit.md)
- [README](../README.md)

### 测试证据

```text
cd audio-gateway
.venv/bin/python -m unittest discover -s tests
```

结果：`Ran 46 tests`，`OK`。

```text
cd frontend
bun test
```

结果：命令仍因仓库既有测试环境问题退出 1，汇总为
`79 pass / 37 fail / 17 errors / 116 tests / 35 files`。按任务指定的
`.ai/runs/2026-07-30-011433-kvm-meeting-panel/baseline-failing-tests.txt`
同一 `(fail) suite > case` 口径：

```text
current unique failures = 21
baseline unique failures = 39
current - baseline = empty
new failures = 0
```

因此本分支没有超出既有失败集；这里不把 frontend 全量写成“全绿”。

`bun run test:product`：本任务按约定未执行，必须由验收方在发布/部署前运行并
全绿。

### Review / merge checklist

- [ ] 验收方从零按会前 checklist 走一遍，命令、点击路径和预期输出一致。
- [ ] 在目标 Mac mini bridge 运行态逐条执行 A7 进程/端口/文件句柄命令。
- [ ] 真人语音完成 Zoom 双向验收；不以纯音替代。
- [ ] `bun run test:product` 全绿。
- [ ] refresh `origin/main` 后复核 merge base 和 PR diff。
- [ ] PR 不含 `.venv`、`__pycache__`、`.pyc`、burn-in JSONL 或会议录音。
- [ ] 工程 owner 决定是否 merge；本任务不部署、不 push、不创建或合并 PR。

### 当前合入准备边界

本地检查时 `main` 与本地缓存的 `origin/main` 都指向 `06b07cd`，当前分支在其上
有 6 个已提交任务提交，另有 TASK-20260730-004 的文档增量。沙箱无权写 worktree
的 `FETCH_HEAD`，本轮 `git fetch origin main --prune` 未能刷新远端状态；创建
PR 前须由有权限的验收方重新 fetch。本文不声称远端 main 仍未变化。
