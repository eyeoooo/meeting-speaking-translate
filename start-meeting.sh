#!/bin/bash
# 会议一键启动（在 Mac mini 的 Terminal.app 里运行——麦克风 TCC 授权在 Terminal 名下）。
#   ./start-meeting.sh                自动体检 → 启动网关（录音+监听）
#   ./start-meeting.sh --interpret    额外开启 OpenAI Realtime 同传（需 OPENAI_API_KEY）
#   ./start-meeting.sh --no-record    不录音
# 其余参数原样透传给 `audio_gateway bridge`。
set -euo pipefail

cd "$(dirname "$0")"
PY=.venv/bin/python
TOKEN_FILE="$HOME/.audio-gateway-token"

if [ ! -x "$PY" ]; then
  echo "✗ 未找到 $PY —— 先按 README 第 2 节创建 venv 并安装依赖。" >&2
  exit 1
fi

# 固定 token：每次会议不用重新抄，面板里存一次即可
if [ ! -f "$TOKEN_FILE" ]; then
  umask 077 && "$PY" -c "import secrets; print(secrets.token_urlsafe(9))" > "$TOKEN_FILE"
  echo "已生成固定网关 token → $TOKEN_FILE"
fi
TOKEN="$(cat "$TOKEN_FILE")"

echo "── 会前体检 ──────────────────────────────"
if ! "$PY" -m audio_gateway doctor --fix; then
  echo
  echo "✗ 体检未通过。TCC 项失败时请确认本命令是在 Terminal.app 里运行（不是 SSH）。" >&2
  echo "  其余项按上表指引处理后重跑本脚本。" >&2
  exit 2
fi

RECORD="--record"
EXTRA=()
for arg in "$@"; do
  case "$arg" in
    --no-record) RECORD="" ;;
    *) EXTRA+=("$arg") ;;
  esac
done

echo
echo "── 启动网关 ──────────────────────────────"
echo "调试面：http://127.0.0.1:8787/?t=$TOKEN （字幕请用「会议助手」的原生字幕窗口）"
echo "发言：在 MacBook 上运行（记得戴耳机）"
echo "  .venv/bin/python -m audio_gateway micagent --url http://\$(此机IP):8787 --token $TOKEN"
echo

exec "$PY" -m audio_gateway bridge \
  --monitor system \
  --token "$TOKEN" \
  $RECORD \
  "${EXTRA[@]}"
