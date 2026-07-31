#!/bin/bash
# 默认构建两个独立产品到 ~/Applications（2026-07-31 产品边界收官）：
#   会议助手.app     同传/会议产品：菜单栏会议服务 + 原生字幕窗口。
#                    bundle id 沿用 dev.controller-agent.audio-gateway 保
#                    麦克风 TCC 连续（麦克风权限属于会议产品，id 跟着它走）。
#   KVM 控制台.app   纯 KVM 薄启动器：打开 Chrome PWA/降级默认浏览器即退出。
#                    全新 bundle id dev.controller-agent.kvm-console，无麦克风声明。
# 会议麦克风.app 是封存兼容资产，只能显式执行 ./build-apps.sh micagent 构建。
#
# 2026-07-31 可分发化：
#   - 会议助手 bundle 自带 Python 主体（Contents/Resources/gateway/），拷到
#     新机器后由 app 首启自举 venv，不再是指向 ~/audio-gateway 的空壳。
#   - 图标由 app/make-icon.swift 程序化生成（无外部素材）；icns 是纯构建产物，
#     落在 app/build/（gitignore），不入库——改图标 = 改代码，任何机器可重现。
#   - dist 目标产出 audio-gateway/dist/会议助手-<版本>.zip（gitignore）供分发。
#
# 用法：./build-apps.sh [meeting|console|micagent|all|dist]（默认 all=会议助手+KVM 控制台）
# ad-hoc 签名是必需的：未签名的 bundle 每次重建都会丢失已授予的 TCC 权限。
set -euo pipefail

cd "$(dirname "$0")"
DEST="${DEST:-$HOME/Applications}"
KVM_URL="${KVM_URL:-http://127.0.0.1:4110/#kvm}"
VERSION="1.0.0"
# CFBundleVersion 用构建时间戳：同一个营销版本号下区分"哪次构建"，
# 排障时一眼对上"部署的是哪天打的包"。
BUILD_STAMP="$(date +%Y%m%d.%H%M)"
BUILD_DIR="$PWD/build"
mkdir -p "$DEST" "$BUILD_DIR"

# 内嵌 Python 运行时（astral-sh/python-build-standalone，install_only_stripped）：
# 新机器首启不再依赖 Homebrew python——App 自带解释器，自举只剩"拷出+建 venv"。
# pin 确切版本+sha256：构建可重现，供应链可审计；压缩包缓存在 build/（gitignore），
# 有缓存时断网也能构建。
PY_RUNTIME_TAG="20260728"
PY_RUNTIME_VERSION="3.12.13"
case "$(uname -m)" in
  arm64)
    PY_RUNTIME_ARCH="aarch64"
    PY_RUNTIME_SHA256="2f18cdef4125ca1440dd1ba00ebcb267526efb532138c0860438f755ea4eebac"
    ;;
  x86_64)
    PY_RUNTIME_ARCH="x86_64"
    PY_RUNTIME_SHA256="e654c21d0ba53e2c671868d4112fac5874deca4c35226d36c5cfe53bc5c9cd71"
    ;;
  *)
    PY_RUNTIME_ARCH=""
    PY_RUNTIME_SHA256=""
    ;;
esac

fetch_python_runtime() {  # stdout=解包后的 python/ 目录；进度走 stderr
  if [ -z "$PY_RUNTIME_ARCH" ]; then
    echo "✗ 未知架构 $(uname -m)，无法内嵌 Python 运行时" >&2
    return 1
  fi
  local name="cpython-${PY_RUNTIME_VERSION}+${PY_RUNTIME_TAG}-${PY_RUNTIME_ARCH}-apple-darwin-install_only_stripped.tar.gz"
  local tarball="$BUILD_DIR/$name"
  local unpacked="$BUILD_DIR/python-runtime-$PY_RUNTIME_ARCH"
  if [ ! -x "$unpacked/bin/python3" ]; then
    if [ ! -f "$tarball" ]; then
      echo "   ↓ 下载内嵌 Python 运行时 $name" >&2
      curl -fsSL -o "$tarball.part" \
        "https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RUNTIME_TAG}/${name}"
      mv "$tarball.part" "$tarball"
    fi
    echo "${PY_RUNTIME_SHA256}  ${tarball}" | shasum -a 256 -c - >/dev/null || {
      echo "✗ 内嵌运行时 sha256 校验失败（缓存已删，重跑即重新下载）：$name" >&2
      rm -f "$tarball"
      return 1
    }
    rm -rf "$unpacked" "$unpacked.tmp"
    mkdir -p "$unpacked.tmp"
    tar -xzf "$tarball" -C "$unpacked.tmp"
    mv "$unpacked.tmp/python" "$unpacked"
    rm -rf "$unpacked.tmp"
  fi
  echo "$unpacked"
}

make_plist() {  # $1=bundle路径 $2=可执行名 $3=bundle id $4=显示名 $5=额外键
  # LSMinimumSystemVersion=14.0：实机是 macOS 26，但代码只用到 macOS 13 API
  # （SMAppService），保守写 14.0 给一代余量而不虚标。
  cat > "$1/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>$2</string>
  <key>CFBundleIdentifier</key><string>$3</string>
  <key>CFBundleName</key><string>$4</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$BUILD_STAMP</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>NSHumanReadableCopyright</key><string>© 2026 Controller-agent 项目</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
$5
</dict>
</plist>
PLIST
}

make_icon() {  # $1=图标变体(meeting|console) $2=目标icns路径
  # make-icon.swift 画 1024px 母图 → sips 缩出 iconset 全尺寸 → iconutil 出 icns。
  # 编译执行而非 swift 解释执行：@main 声明式入口才过得了仓库的
  # -parse-as-library 门禁（顶层语句脚本过不了）。
  local tool="$BUILD_DIR/make-icon"
  local png="$BUILD_DIR/icon-$1.png"
  local iconset="$BUILD_DIR/icon-$1.iconset"
  if [ ! -x "$tool" ] || [ make-icon.swift -nt "$tool" ]; then
    /usr/bin/swiftc -O -parse-as-library make-icon.swift -o "$tool" -framework AppKit
  fi
  "$tool" "$png" "$1"
  rm -rf "$iconset"
  mkdir -p "$iconset"
  local s
  for s in 16 32 128 256 512; do
    sips -z "$s" "$s" "$png" --out "$iconset/icon_${s}x${s}.png" >/dev/null
    sips -z "$((s * 2))" "$((s * 2))" "$png" --out "$iconset/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$iconset" -o "$2"
}

copy_gateway_payload() {  # $1=bundle路径
  # bundle 自包含：Python 主体随 app 走，新机器首启由 app 从这里拷到
  # Application Support 再自举 venv（见 GatewayMenuBar.swift）。
  # __pycache__/.pyc 是本机字节码，拷进去只会撑大包并埋跨 Python 版本的雷。
  local res="$1/Contents/Resources/gateway"
  rm -rf "$res"
  mkdir -p "$res/examples"
  rsync -a --exclude='__pycache__/' --exclude='*.pyc' ../audio_gateway "$res/"
  cp ../requirements.txt "$res/"
  cp ../examples/meeting-brief.example.md "$res/examples/"
  # 内嵌 CPython 随包走：缺了它，分发包在无 Homebrew 的新机上就是空壳，
  # 所以取不到运行时=构建失败，绝不静默出一个"看起来能装"的包。
  local runtime
  runtime="$(fetch_python_runtime)" || exit 1
  rm -rf "$1/Contents/Resources/python"
  ditto "$runtime" "$1/Contents/Resources/python"
}

build_swift_app() {  # $1=源码(可空格分隔多文件) $2=可执行名 $3=bundle名 $4=bundle id $5=额外plist键 $6=图标变体 $7=payload函数(可选)
  local bundle="$DEST/$3.app"
  echo "── 构建 $3.app"
  rm -rf "$bundle"
  mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources"
  # -parse-as-library：入口是 @main（多文件后顶层语句只允许在 main.swift，
  # 显式入口是唯一不靠文件名的写法）
  # shellcheck disable=SC2086
  /usr/bin/swiftc -O -parse-as-library $1 -o "$bundle/Contents/MacOS/$2" \
    -framework AppKit -framework Carbon 2>&1 | grep -v "^$" || true
  [ -x "$bundle/Contents/MacOS/$2" ] || { echo "✗ 编译失败：$1" >&2; return 1; }
  make_icon "$6" "$bundle/Contents/Resources/AppIcon.icns"
  make_plist "$bundle" "$2" "$4" "$3" "$5"
  # payload 必须在签名前进 bundle：签名封的是整个包的内容清单
  [ -n "${7:-}" ] && "$7" "$bundle"
  # ad-hoc 签名：稳定 bundle 身份，TCC 授权不会因重建而失效
  /usr/bin/codesign --force --deep --sign - "$bundle"
  echo "   ✓ $bundle"
}

GATEWAY_MENUBAR_KEYS='  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>用来听见会议里的声音，才能录音、生成字幕和会后纪要。</string>'

# TASK-20260730-008 restore point: the explicit micagent target and its TCC
# description stay buildable, but are intentionally absent from the default/all set.
MICAGENT_MENUBAR_KEYS='  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>会议麦克风预留客户端需要麦克风权限，以采集发言音频。</string>'

# KVM 控制台薄启动器：打开网页即退出，不常驻，也绝不声明麦克风。
KVM_CONSOLE_KEYS='  <key>LSUIElement</key><true/>'

build_meeting() {
  build_swift_app "GatewayMenuBar.swift MeetingCaptionsWindow.swift" \
    audio-gateway-menubar "会议助手" \
    dev.controller-agent.audio-gateway "$GATEWAY_MENUBAR_KEYS" \
    meeting copy_gateway_payload
  # 清理被本 App 取代的历史 bundle（音频网关=合并前形态，KVM 工作台=拆分前形态）
  rm -rf "$DEST/音频网关.app" "$DEST/KVM 工作台.app"
}

build_console() {
  build_swift_app KvmConsoleLauncher.swift kvm-console-launcher "KVM 控制台" \
    dev.controller-agent.kvm-console "$KVM_CONSOLE_KEYS" console
}

build_micagent() {
  # 封存资产沿用会议家族图标：它不值得一个独立视觉身份
  build_swift_app MicAgentMenuBar.swift mic-agent-menubar "会议麦克风" \
    dev.controller-agent.audio-gateway.micagent "$MICAGENT_MENUBAR_KEYS" meeting
}

build_dist() {
  # 分发包：两 App + README.txt → dist/会议助手-<版本>.zip。
  # 用 ditto 而不是 zip：保留符号链接与 bundle 结构，这是 .app 归档的正规做法。
  local distroot="$PWD/../dist"
  local stage="$BUILD_DIR/会议助手-$VERSION"
  rm -rf "$stage"
  mkdir -p "$stage" "$distroot"
  # 子 shell 里改 DEST：分发构建落进 staging，不污染 ~/Applications
  ( DEST="$stage"; build_meeting; build_console )
  cat > "$stage/README.txt" <<README
会议助手 $VERSION
================

一台 Mac 上的会议同传助手：录音、实时双语字幕、AI 建议、会后自动纪要。
「KVM 控制台」是同仓库的独立小工具（打开 KVM 网页控制台），与会议助手无关，
不需要可删。

系统要求
--------
- macOS 14 或更新（Apple Silicon 推荐）
- 无需安装 Python：App 自带运行时（首次启动自动就位）
- 网络连接（字幕/建议/纪要调用 OpenAI 与 Anthropic API）
- 可选：BlackHole 虚拟声卡（本机 Teams/Zoom 模式需要）：
  brew install blackhole-2ch blackhole-16ch

安装
----
1. 把 会议助手.app 拖进 ~/Applications 或 /Applications。
2. 本包未做 Apple 公证：首次打开请「右键 → 打开」，或先执行
   xattr -dr com.apple.quarantine ~/Applications/会议助手.app
3. 首次启动时菜单栏会显示「首次准备环境…」：App 会把运行文件拷到
   ~/Library/Application Support/会议助手/gateway 并自动创建 Python
   运行环境、安装依赖（约 1-3 分钟，需要网络）。失败会弹窗给出
   可复制的修复命令。
4. 第一次「开始这场会议」时 macOS 会请求麦克风权限，请点允许。

API Key（必需）
---------------
字幕/翻译走 OpenAI，AI 建议与纪要走 Anthropic。请在 ~/.zshenv 写入：
  export OPENAI_API_KEY=sk-...
  export ANTHROPIC_API_KEY=sk-ant-...
保存后重新登录（或重启 App）生效；没有 key 时录音功能仍可用，
字幕与纪要会不可用。
README
  local zip="$distroot/会议助手-$VERSION.zip"
  rm -f "$zip"
  ditto -c -k --keepParent "$stage" "$zip"
  echo "✓ 分发包：$zip"
}


case "${1:-all}" in
  meeting)   build_meeting ;;
  workbench) build_meeting ;;     # 兼容旧目标名（拆分前的 KVM 工作台）
  gateway)   build_meeting ;;     # 兼容旧目标名
  console)   build_console ;;
  micagent)  build_micagent ;;
  # Restore point: add build_micagent back here only after uplink product promotion.
  all)       build_meeting; build_console ;;
  dist)      build_dist ;;
  *) echo "用法: $0 [meeting|console|micagent|all|dist]" >&2; exit 1 ;;
esac

echo
echo "完成。首次启动「会议助手」并点「开始这场会议」时 macOS 会弹麦克风授权 —— 点允许后长期有效。"
echo "（「KVM 控制台」不需要任何权限；此后不再需要从 Terminal 启动，start-meeting.sh 保留作排障用。）"
