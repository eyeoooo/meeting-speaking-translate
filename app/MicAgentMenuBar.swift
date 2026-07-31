// 会议麦克风.app —— MacBook 操作席端菜单栏发言客户端启动器。
//
// TASK-20260730-008 封存兼容资产：默认构建与产品文档不发布本 app；当远程渠道
// 支持麦克风直连并完成重新验收后，可显式恢复。实现保留，避免重写协议与 TCC。
// 本 app 是 micagent 的父进程：麦克风 TCC 归本 app（一次授权），并提供菜单栏
// 静音与全局热键。
//
// 静音的唯一真相在网关服务端；本 app 只发切换请求并回读状态，不本地判断。

import AppKit
import Carbon.HIToolbox
import Foundation

final class MicAgentController: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var process: Process?
    private var logURL: URL!
    private var pollTimer: Timer?
    private var muted = false
    private var connected = false
    private var hotKeyRef: EventHotKeyRef?

    private let connectItem = NSMenuItem(title: "连接网关", action: #selector(toggleConnection), keyEquivalent: "")
    private let muteItem = NSMenuItem(title: "静音", action: #selector(toggleMute), keyEquivalent: "")
    private let stateItem = NSMenuItem(title: "未连接", action: nil, keyEquivalent: "")

    private var settings: (url: String, token: String) {
        let defaults = UserDefaults.standard
        return (defaults.string(forKey: "gatewayURL") ?? "",
                defaults.string(forKey: "gatewayToken") ?? "")
    }

    private var agentRoot: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("audio-gateway")
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        logURL = FileManager.default.temporaryDirectory.appendingPathComponent("mic-agent-app.log")
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        buildMenu()
        registerHotKey()
        render()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.pollStatus()
        }
    }

    private func buildMenu() {
        let menu = NSMenu()
        [connectItem, muteItem].forEach { $0.target = self }
        stateItem.isEnabled = false

        menu.addItem(stateItem)
        menu.addItem(.separator())
        menu.addItem(connectItem)
        menu.addItem(muteItem)
        menu.addItem(.separator())

        let configure = NSMenuItem(title: "设置网关地址与 token…", action: #selector(configure), keyEquivalent: ",")
        let log = NSMenuItem(title: "查看日志", action: #selector(openLog), keyEquivalent: "")
        [configure, log].forEach { $0.target = self; menu.addItem($0) }

        let hint = NSMenuItem(title: "全局热键：⌃⌥M 切换静音", action: nil, keyEquivalent: "")
        hint.isEnabled = false
        menu.addItem(hint)
        let earphone = NSMenuItem(title: "⚠️ 务必戴耳机（无回声消除）", action: nil, keyEquivalent: "")
        earphone.isEnabled = false
        menu.addItem(earphone)

        menu.addItem(.separator())
        let quit = NSMenuItem(title: "退出", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        statusItem.menu = menu
    }

    // MARK: - 连接

    @objc private func toggleConnection() {
        if process?.isRunning == true { stopAgent() } else { startAgent() }
    }

    private func startAgent() {
        let config = settings
        guard !config.url.isEmpty, !config.token.isEmpty else {
            configure()
            return
        }
        let python = agentRoot.appendingPathComponent(".venv/bin/python")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            alert("未找到 \(python.path)", "请在本机 ~/audio-gateway 创建 venv 并安装依赖。")
            return
        }
        let task = Process()
        task.executableURL = python
        task.arguments = ["-m", "audio_gateway", "micagent",
                          "--url", config.url, "--token", config.token]
        task.currentDirectoryURL = agentRoot

        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        if let handle = try? FileHandle(forWritingTo: logURL) {
            task.standardOutput = handle
            task.standardError = handle
        }
        task.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                self?.process = nil
                self?.connected = false
                if proc.terminationStatus != 0 {
                    self?.alert("发言客户端已停止（退出码 \(proc.terminationStatus)）",
                                "点菜单「查看日志」查看原因；常见为网关地址/token 不对。")
                }
                self?.render()
            }
        }
        do {
            try task.run()
            process = task
        } catch {
            alert("启动失败", error.localizedDescription)
        }
        render()
    }

    private func stopAgent() {
        process?.interrupt()
        DispatchQueue.global().asyncAfter(deadline: .now() + 3) { [weak self] in
            if self?.process?.isRunning == true { self?.process?.terminate() }
        }
    }

    // MARK: - 状态与静音（服务端权威）

    private func pollStatus() {
        let config = settings
        guard process?.isRunning == true,
              let base = URL(string: config.url),
              let url = URL(string: "status?t=\(config.token)", relativeTo: base)
        else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: request) { [weak self] data, _, _ in
            guard let data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else {
                DispatchQueue.main.async { self?.connected = false; self?.render() }
                return
            }
            DispatchQueue.main.async {
                self?.connected = true
                self?.muted = (json["muted"] as? Bool) ?? false
                self?.render()
            }
        }.resume()
    }

    @objc private func toggleMute() {
        let config = settings
        guard process?.isRunning == true,
              let base = URL(string: config.url),
              let url = URL(string: "mute?t=\(config.token)", relativeTo: base)
        else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "content-type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["muted": !muted])
        URLSession.shared.dataTask(with: request) { [weak self] _, _, _ in
            DispatchQueue.main.async { self?.pollStatus() }
        }.resume()
    }

    // MARK: - 全局热键 ⌃⌥M

    private func registerHotKey() {
        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                      eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, userData in
            guard let userData else { return noErr }
            let controller = Unmanaged<MicAgentController>.fromOpaque(userData).takeUnretainedValue()
            DispatchQueue.main.async { controller.toggleMute() }
            return noErr
        }, 1, &eventType, Unmanaged.passUnretained(self).toOpaque(), nil)

        let hotKeyID = EventHotKeyID(signature: OSType(0x4D494341), id: 1)  // 'MICA'
        RegisterEventHotKey(UInt32(kVK_ANSI_M),
                            UInt32(controlKey | optionKey),
                            hotKeyID, GetApplicationEventTarget(), 0, &hotKeyRef)
    }

    // MARK: - 渲染与设置

    private func render() {
        let running = process?.isRunning == true
        let icon: String
        if !running { icon = "🎙" }
        else if !connected { icon = "🎙…" }
        else if muted { icon = "🎙🔇" }
        else { icon = "🎙🟢" }
        statusItem.button?.title = icon

        connectItem.title = running ? "断开" : "连接网关"
        muteItem.isEnabled = running && connected
        muteItem.title = muted ? "取消静音（⌃⌥M）" : "静音（⌃⌥M）"
        stateItem.title = !running ? "未连接"
            : (connected ? (muted ? "🔇 已静音（仍在听会）" : "🎙 发言开启") : "连接中…")
    }

    @objc private func configure() {
        let config = settings
        let alert = NSAlert()
        alert.messageText = "网关连接设置"
        alert.informativeText = "在 Mac mini 的「音频网关」菜单里选「复制 micagent 命令」，即可看到地址与 token。"
        alert.addButton(withTitle: "保存")
        alert.addButton(withTitle: "取消")

        let container = NSView(frame: NSRect(x: 0, y: 0, width: 320, height: 58))
        let urlField = NSTextField(frame: NSRect(x: 0, y: 30, width: 320, height: 24))
        urlField.placeholderString = "http://100.92.179.94:8787"
        urlField.stringValue = config.url
        let tokenField = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        tokenField.placeholderString = "token"
        tokenField.stringValue = config.token
        container.addSubview(urlField)
        container.addSubview(tokenField)
        alert.accessoryView = container

        if alert.runModal() == .alertFirstButtonReturn {
            let defaults = UserDefaults.standard
            defaults.set(urlField.stringValue.trimmingCharacters(in: .whitespaces), forKey: "gatewayURL")
            defaults.set(tokenField.stringValue.trimmingCharacters(in: .whitespaces), forKey: "gatewayToken")
        }
    }

    @objc private func openLog() { NSWorkspace.shared.open(logURL) }

    @objc private func quit() {
        if process?.isRunning == true { stopAgent() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { NSApp.terminate(nil) }
    }

    private func alert(_ title: String, _ text: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        alert.alertStyle = .warning
        alert.runModal()
    }
}

// 与 GatewayMenuBar 同理：构建脚本统一 -parse-as-library，入口必须显式 @main。
@main
enum MicAgentMain {
    static func main() {
        let app = NSApplication.shared
        let controller = MicAgentController()
        app.delegate = controller
        app.setActivationPolicy(.accessory)
        app.run()
    }
}
