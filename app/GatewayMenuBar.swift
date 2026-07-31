// 会议助手.app —— 同传/会议产品的唯一入口（菜单栏会议服务 + 原生字幕窗口）。
//
// 2026-07-31 产品边界收官：会议模块与 KVM 彻底脱钩，KVM 只是可选音源之一
// （「外部设备（USB 声卡 / 被控机）」）。KVM 控制台由独立的「KVM 控制台.app」
// 薄启动器负责，本 app 不再打开任何浏览器窗口。
//
// 为什么由本 app 拉起底层服务：麦克风 TCC 权限按「负责进程」归属，服务若由 Terminal
// 启动，权限便挂在 Terminal 名下。本 app 作为父进程（Info.plist 声明用途、ad-hoc
// 签名稳定身份），一次授权长期有效。bundle id 保持
// dev.controller-agent.audio-gateway 以维持 TCC 连续性——麦克风权限属于会议
// 产品，id 必须跟着它走。
//
// 用词原则（2026-07-30 三视角产品评审定稿）：菜单只讲用户在做的事——开会、录音、
// 字幕、纪要，绝不出现 网关/bridge/link/TCC/退出码/批量转写 等内部术语；实现约束
// （如字幕开关必须在开始前决定）用 macOS 原生的「置灰」表达，不写"重启生效"。
//
// 职责边界：只做启动、读取 /status 显示状态、调用 /stop、拉起字幕窗口。所有
// 音频、转写、纪要与体检逻辑仍在 Python 侧，本 app 不复制任何业务判断；告警码到
// 人话的映射属于展示层，故留在本文件。

import AppKit
import Foundation
import ServiceManagement

let kGatewayPort = 8787

/// 告警等级：红=此刻录下的内容作废，必须马上处理；橙=仍在正常录音，可会后再管。
enum AlertSeverity {
    case fatal
    case degraded
}

struct UserAlert {
    let severity: AlertSeverity
    let title: String       // 菜单里显示的一行
    let guidance: [String]  // 点开后的检查清单
}

final class GatewayController: NSObject, NSApplicationDelegate {
    // TASK-20260730-008 restore point: server mute/uplink support stays intact, but
    // the receive-only product menu must not expose speaking controls. Set true
    // only after the remote channel gains direct microphone support and acceptance
    // is rerun; no protocol implementation needs to be recreated.
    private let mothballedUplinkProductEntriesEnabled = false

    private var statusItem: NSStatusItem!
    private var process: Process?
    private var logURL: URL!
    private var pollTimer: Timer?
    private var tickTimer: Timer?
    private var token: String = ""
    private var lastStatus: [String: Any] = [:]
    private var starting = false
    private var stopRequested = false
    private var cancelRequested = false
    private var quitAfterStop = false
    private var lastObservedPhase = "running"
    private var lastSessionDirectory: URL?
    private var meetingStartedAt: Date?
    private var postProcessingStartedAt: Date?
    private var minutesUnread = false

    // 菜单项（需按状态改标题/可用性，故持有引用）
    private let stateItem = NSMenuItem(title: "待命中 · 未开始录音", action: nil, keyEquivalent: "")
    private let hintItem = NSMenuItem(title: "可断开远程，请勿关机", action: nil, keyEquivalent: "")
    private let alertItem = NSMenuItem(title: "", action: #selector(showAlertGuidance), keyEquivalent: "")
    private let startItem = NSMenuItem(title: "开始这场会议", action: #selector(startMeeting), keyEquivalent: "r")
    private let soundTestItem = NSMenuItem(title: "测试声音（5 秒）", action: #selector(testSound), keyEquivalent: "")
    private let stopItem = NSMenuItem(title: "结束会议并生成纪要", action: #selector(finishMeeting), keyEquivalent: ".")
    private let cancelItem = NSMenuItem(title: "取消这次录音（不生成纪要）", action: #selector(cancelMeeting), keyEquivalent: "")
    private let subtitleToggle = NSMenuItem(title: "实时中文字幕", action: #selector(toggleSubtitles), keyEquivalent: "")
    private let adviceToggle = NSMenuItem(title: "AI 建议", action: #selector(toggleAdvice), keyEquivalent: "")
    // 声音来源：这台 Mac mini 的会议声音从哪来。产品裁定（2026-07-30）——
    // 会议模块是通用中枢，KVM 被控机只是音源之一；本机跑 Teams/Zoom 时
    // 声音在系统内部，要靠虚拟音频设备（BlackHole）接进来。
    private let sourceLocalItem = NSMenuItem(
        title: "本机会议软件（Teams / Zoom）", action: #selector(useLocalSource), keyEquivalent: "")
    private let sourceExternalItem = NSMenuItem(
        title: "外部设备（USB 声卡 / 被控机）", action: #selector(useExternalSource), keyEquivalent: "")
    private let rehearseToggle = NSMenuItem(title: "发言排练（只进我的耳机）", action: #selector(toggleRehearse), keyEquivalent: "")
    private let speakToggle = NSMenuItem(title: "正式发言（对方听到日语）", action: #selector(toggleSpeak), keyEquivalent: "")
    private let settingsHint = NSMenuItem(title: "会议进行中不可更改", action: nil, keyEquivalent: "")
    private let minutesItem = NSMenuItem(title: "打开最近一次纪要", action: #selector(openLatestMinutes), keyEquivalent: "")
    private let muteItem = NSMenuItem(title: "静音发言", action: #selector(toggleMute), keyEquivalent: "m")

    // 字幕与建议要调用付费 API，关掉往往是"这场会不想花这个钱/不信任译文"的明确决定。
    // 存进 UserDefaults：重启 app 不该把它悄悄打开——2026-07-30 验收时就这样白连了一次
    // Realtime。默认全开，首次使用即完整功能。
    private static let subtitlesKey = "meeting.subtitlesEnabled"
    private static let adviceKey = "meeting.adviceEnabled"
    // 排练是发言方向 M1：默认关——它多开一路付费 Realtime 会话，
    // 且只在用户想练日语发言时才有意义。
    private static let rehearseKey = "meeting.rehearseEnabled"
    // 正式发言（M2）：日语进会议。与排练互斥——"对方听没听到"必须无歧义。
    private static let speakKey = "meeting.speakEnabled"
    // 声音来源：true=本机会议软件（BlackHole 虚拟设备），false=外部 USB 声卡
    private static let localSourceKey = "meeting.localSource"

    // 收听源与发言目标必须是两个不同的虚拟设备：同一个会让自己的日语
    // 回流进采集，形成翻译回环。
    private static let localCaptureKeyword = "BlackHole 2ch"
    private static let localSpeakKeyword = "BlackHole 16ch"

    private var subtitlesEnabled: Bool {
        get { UserDefaults.standard.bool(forKey: Self.subtitlesKey) }
        set { UserDefaults.standard.set(newValue, forKey: Self.subtitlesKey) }
    }
    private var adviceEnabled: Bool {
        get { UserDefaults.standard.bool(forKey: Self.adviceKey) }
        set { UserDefaults.standard.set(newValue, forKey: Self.adviceKey) }
    }
    private var rehearseEnabled: Bool {
        get { UserDefaults.standard.bool(forKey: Self.rehearseKey) }
        set { UserDefaults.standard.set(newValue, forKey: Self.rehearseKey) }
    }
    private var speakEnabled: Bool {
        get { UserDefaults.standard.bool(forKey: Self.speakKey) }
        set { UserDefaults.standard.set(newValue, forKey: Self.speakKey) }
    }
    private var localSource: Bool {
        get { UserDefaults.standard.bool(forKey: Self.localSourceKey) }
        set { UserDefaults.standard.set(newValue, forKey: Self.localSourceKey) }
    }

    // gatewayRoot 解析（2026-07-31 可分发化）：
    // ① 开发机既有 ~/audio-gateway 且已建 .venv → 直接用，历史行为一字不改；
    // ② 否则视为"拷来的新机器"，运行根落在 Application Support，源码首启从
    //    bundle Resources/gateway 拷出，venv 建在同目录（目录布局与 ① 完全同构，
    //    后续所有代码不需要知道自己跑在哪种形态上）。
    // 只在首次访问时解析一次并固定：会议中途换根等于换掉正在运行的服务。
    private lazy var gatewayRoot: URL = {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let dev = home.appendingPathComponent("audio-gateway")
        if FileManager.default.isExecutableFile(
            atPath: dev.appendingPathComponent(".venv/bin/python").path) {
            return dev
        }
        return home.appendingPathComponent("Library/Application Support/会议助手/gateway")
    }()

    private var venvPython: URL {
        gatewayRoot.appendingPathComponent(".venv/bin/python")
    }

    // 首启环境自举：非 nil = 正在准备（或失败原因），期间「开始会议/测试声音」
    // 保持禁用——环境没就绪时开始会议只会录出一场事故（fail-closed）。
    private var envPrepStatus: String?
    // BlackHole 引导每次运行只弹一次：它是引导不是门禁，重复弹就成了骚扰。
    private var blackHoleGuidanceShown = false

    private var meetingsRoot: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("AudioGateway")
    }

    // 原生字幕窗口：语音产品的第一呈现面，浏览器面板降级为工程调试面。
    // 惰性创建——不开字幕的会议不付出窗口成本。
    private lazy var captionsWindow = MeetingCaptionsWindowController()

    @objc private func openCaptionsWindow() {
        // accessory app 的面板不激活 app 时可能排不到最前
        NSApp.activate(ignoringOtherApps: true)
        captionsWindow.showWindow(nil)
        captionsWindow.window?.makeKeyAndOrderFront(nil)
        if process?.isRunning == true {
            captionsWindow.connect(token: token)
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // 会议中枢必须在开机后就位：重启过的 Mac mini 若没人记得双击，
        // 下一场会议就没有字幕没有录音（2026-07-30 用户实际问出"为什么
        // 还要再双击"）。SMAppService 自注册为登录项，幂等，用户可在
        // 系统设置里随时关掉。
        if SMAppService.mainApp.status != .enabled {
            try? SMAppService.mainApp.register()
        }
        // token 必须先于一切就绪：旧实现把加载留在 startMeeting() 里，启动阶段
        // token 还是 ""，凡是启动期要用 token 的路径（如「运行详情」面板）都会
        // 拿到空值。
        token = loadOrCreateToken()
        UserDefaults.standard.register(defaults: [
            Self.subtitlesKey: true,
            Self.adviceKey: true,
        ])
        logURL = FileManager.default.temporaryDirectory.appendingPathComponent("audio-gateway-app.log")
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        buildMenu()
        // 新机器首启：源码从 bundle 拷出、venv 现建。开发机（~/audio-gateway
        // 已含 .venv）在这里直接短路返回，行为与历史零差别。
        ensureEnvironmentReady()
        render()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.pollStatus()
        }
        // 图标上的计时每秒走字——不打开菜单也能确认"还在录"
        tickTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.render()
        }
        // 默认 runloop mode 会在菜单展开（.eventTracking）和确认框（.modalPanel）
        // 期间停摆——2026-07-30 真机验收实测：确认框一开，计时就冻在 00:50 不动，
        // 而"计时在走"正是本产品用来证明"还在录"的唯一信号，冻结即等于谎报。
        pollTimer.map { RunLoop.main.add($0, forMode: .common) }
        tickTimer.map { RunLoop.main.add($0, forMode: .common) }
    }

    // MARK: - 菜单

    private func buildMenu() {
        let menu = NSMenu()
        menu.autoenablesItems = false

        stateItem.isEnabled = false
        hintItem.isEnabled = false
        settingsHint.isEnabled = false
        alertItem.target = self
        [startItem, soundTestItem, stopItem, cancelItem,
         subtitleToggle, adviceToggle, rehearseToggle, speakToggle,
         sourceLocalItem, sourceExternalItem,
         minutesItem, muteItem].forEach { $0.target = self }

        menu.addItem(stateItem)
        menu.addItem(hintItem)
        menu.addItem(alertItem)
        menu.addItem(.separator())

        menu.addItem(startItem)
        menu.addItem(soundTestItem)
        menu.addItem(stopItem)
        menu.addItem(cancelItem)
        // 「静音发言」在正式发言的会议中显示：一键切断会议支路（耳机继续播，
        // 你得知道它本来要说什么才能决定何时解除静音）。
        menu.addItem(muteItem)
        menu.addItem(.separator())

        let settingsHeader = NSMenuItem(title: "会议设置", action: nil, keyEquivalent: "")
        settingsHeader.isEnabled = false
        menu.addItem(settingsHeader)
        let sourceHeader = NSMenuItem(title: "声音来源", action: nil, keyEquivalent: "")
        sourceHeader.isEnabled = false
        menu.addItem(sourceHeader)
        menu.addItem(sourceLocalItem)
        menu.addItem(sourceExternalItem)
        menu.addItem(subtitleToggle)
        menu.addItem(adviceToggle)
        menu.addItem(rehearseToggle)
        menu.addItem(speakToggle)
        menu.addItem(settingsHint)
        menu.addItem(.separator())

        // 字幕窗口是语音产品的原生呈现面（2026-07-30 裁定：不依赖浏览器）。
        let captions = NSMenuItem(
            title: "打开字幕窗口",
            action: #selector(openCaptionsWindow),
            keyEquivalent: "j"
        )
        captions.target = self
        menu.addItem(captions)
        menu.addItem(minutesItem)
        let folder = NSMenuItem(title: "打开会议文件夹", action: #selector(openMeetingsFolder), keyEquivalent: "")
        folder.target = self
        menu.addItem(folder)
        menu.addItem(.separator())

        // 工程动作收进子菜单：日常菜单里不出现"调试""日志"这类开发者词汇
        let help = NSMenuItem(title: "帮助与诊断", action: nil, keyEquivalent: "")
        let helpMenu = NSMenu()
        let detail = NSMenuItem(title: "运行详情（技术）", action: #selector(openPanel), keyEquivalent: "")
        let log = NSMenuItem(title: "技术日志", action: #selector(openLog), keyEquivalent: "")
        let copyDiag = NSMenuItem(title: "复制诊断信息", action: #selector(copyDiagnosticsAction), keyEquivalent: "")
        [detail, log, copyDiag].forEach { $0.target = self; helpMenu.addItem($0) }
        if mothballedUplinkProductEntriesEnabled {
            let copy = NSMenuItem(title: "复制 micagent 命令", action: #selector(copyAgentCommand), keyEquivalent: "")
            copy.target = self
            helpMenu.addItem(copy)
        }
        help.submenu = helpMenu
        menu.addItem(help)

        let quitItem = NSMenuItem(title: "退出", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)
        statusItem.menu = menu
    }

    // MARK: - 会议生命周期

    @objc private func startMeeting() {
        guard !starting, process?.isRunning != true, envPrepStatus == nil else { return }
        let python = venvPython
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            environmentMissingAlert(python.path)
            return
        }
        // BlackHole 引导（2026-07-31 分发裁定）：本机会议软件模式全靠 BlackHole
        // 虚拟声卡把 Teams/Zoom 的声音接进来，驱动没装这场会必然录空白。
        // 已装零打扰；未装每次运行只提示一次，且允许明知故犯（比如先试跑界面）。
        if localSource && !blackHoleGuidanceShown && !blackHoleInstalled() {
            blackHoleGuidanceShown = true
            let brew = "brew install blackhole-2ch blackhole-16ch"
            let box = NSAlert()
            box.messageText = "还没安装 BlackHole 虚拟声卡"
            box.informativeText =
                "「本机会议软件」模式需要它才能听到 Teams/Zoom 的声音。\n\n"
                + "请在「终端」执行：\n\(brew)\n\n"
                + "装好后在会议软件的音频设置里，把扬声器选成「BlackHole 2ch」。"
            box.alertStyle = .warning
            box.addButton(withTitle: "复制安装命令")
            box.addButton(withTitle: "仍要开始")
            box.addButton(withTitle: "先不开始")
            switch box.runModal() {
            case .alertFirstButtonReturn:
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(brew, forType: .string)
                return
            case .alertThirdButtonReturn:
                return
            default:
                break
            }
        }
        starting = true
        stopRequested = false
        quitAfterStop = false
        minutesUnread = false
        lastObservedPhase = "running"
        lastSessionDirectory = nil
        lastStatus = [:]
        meetingStartedAt = Date()
        postProcessingStartedAt = nil

        // token 已在 applicationDidFinishLaunching 加载（文件跨会议复用同一值）。
        var args = ["-m", "audio_gateway", "bridge",
                    // "system" = 跟随会议开始那一刻的系统默认输出（2026-07-30
                    // 设备无关裁定：插耳麦系统切默认输出，声音就跟过去，产品
                    // 不问"耳机插哪"）。防回灌护栏在 Python 侧 devices.py。
                    "--monitor", "system",
                    "--token", token,
                    "--record"]
        if localSource {
            // 本机会议软件：Teams 输出设为 BlackHole 2ch，麦克风设为 BlackHole 16ch
            args.append(contentsOf: ["--usb", Self.localCaptureKeyword])
            args.append(contentsOf: ["--speak-device", Self.localSpeakKeyword])
        }
        if subtitlesEnabled { args.append("--interpret") }
        if subtitlesEnabled && adviceEnabled { args.append("--advise") }
        if rehearseEnabled { args.append("--rehearse") }
        if speakEnabled { args.append("--speak") }

        let task = Process()
        task.executableURL = python
        task.arguments = args
        task.currentDirectoryURL = gatewayRoot
        // 继承登录环境（OPENAI_API_KEY / ANTHROPIC_API_KEY 在 ~/.zshenv）
        task.environment = loginEnvironment()

        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        if let handle = try? FileHandle(forWritingTo: logURL) {
            task.standardOutput = handle
            task.standardError = handle
        }
        task.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self else { return }
                let completedSession = self.lastSessionDirectory
                let completionNotes =
                    (self.lastStatus["post_processing_notes"] as? [String]) ?? []
                let completedGracefully =
                    self.stopRequested
                    || self.lastObservedPhase == "post_processing"
                    || self.lastObservedPhase == "done"
                let shouldQuit = self.quitAfterStop
                let wasCancelled = self.cancelRequested

                // 先把状态清干净再弹完成框：完成框是 modal，会卡在这里直到用户点掉，
                // 而 tickTimer 现在（.common mode）仍在渲染。若把清理留在弹框之后，
                // 菜单会拿收尾瞬间的 link=down 一直渲染"声音通道断了"这条假告警
                // ——2026-07-30 真机验收实测过一次。
                // 字幕窗口停止重连并标注结束态；窗口留着给用户回看最后的字幕。
                self.captionsWindow.markEnded()
                self.starting = false
                self.process = nil
                self.meetingStartedAt = nil
                self.postProcessingStartedAt = nil
                self.lastStatus = [:]
                self.lastObservedPhase = "running"
                self.stopRequested = false
                self.cancelRequested = false
                self.quitAfterStop = false
                // 「纪要已就绪」必须以磁盘上真有这个文件为准：取消的会议不写纪要，
                // 正常结束也可能因为没转写到内容而写不出，两种情况都不能让状态行
                // 指向一个打不开的文件。
                if completedGracefully, !wasCancelled, let session = completedSession {
                    self.minutesUnread = FileManager.default.fileExists(
                        atPath: session.appendingPathComponent("minutes.md").path
                    )
                }
                self.render()

                if proc.terminationStatus != 0 {
                    self.startFailureAlert()
                } else if completedGracefully, let completedSession {
                    self.showCompletionAlert(
                        sessionDirectory: completedSession,
                        notes: completionNotes,
                        cancelled: wasCancelled
                    )
                }
                if shouldQuit { NSApp.terminate(nil) }
            }
        }
        do {
            try task.run()
            process = task
            // 字幕窗口随会议自动出现并连接：语音产品的默认呈现面，
            // 不依赖用户记得去点菜单，更不依赖浏览器开着。
            if subtitlesEnabled || rehearseEnabled || speakEnabled {
                openCaptionsWindow()
            }
        } catch {
            starting = false
            alert("无法开始录音", error.localizedDescription)
        }
        render()
    }

    @objc private func finishMeeting() {
        requestStop(cancelRecording: false)
    }

    @objc private func cancelMeeting() {
        guard process?.isRunning == true, !stopRequested else { return }
        let confirm = NSAlert()
        confirm.messageText = "取消这次录音？"
        confirm.informativeText = "录音文件会保留在会议文件夹里，但不会生成纪要。"
        confirm.alertStyle = .warning
        confirm.addButton(withTitle: "取消录音")
        confirm.addButton(withTitle: "继续开会")
        guard confirm.runModal() == .alertFirstButtonReturn else { return }
        requestStop(cancelRecording: true)
    }

    /// cancelRecording=true 时请求跳过会后处理；两者都走同一条优雅停止路径。
    private func requestStop(cancelRecording: Bool) {
        guard process?.isRunning == true, !stopRequested else { return }
        var path = "http://127.0.0.1:\(kGatewayPort)/stop?t=\(token)"
        if cancelRecording { path += "&postprocess=0" }
        guard let url = URL(string: path) else { return }
        stopRequested = true
        cancelRequested = cancelRecording
        render()

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 5.0
        URLSession.shared.dataTask(with: request) { [weak self] _, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                guard self.process?.isRunning == true else { return }
                if let error {
                    self.stopRequested = false
                    self.cancelRequested = false
                    self.alert("结束失败", "录音仍在继续，内容没有丢失。请稍后再试。\n（\(error.localizedDescription)）")
                    self.render()
                    return
                }
                let statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
                guard (200...299).contains(statusCode) else {
                    self.stopRequested = false
                    self.cancelRequested = false
                    self.alert("结束失败", "录音仍在继续，内容没有丢失。请稍后再试。")
                    self.render()
                    return
                }
                // /stop 只触发生命周期；所有展示状态仍只从 /status 读取。
                self.pollStatus()
            }
        }.resume()
    }

    /// 开会前自检：不启动服务，只确认这台机器现在能不能采到声音。
    @objc private func testSound() {
        guard process?.isRunning != true, envPrepStatus == nil else { return }
        let python = venvPython
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            environmentMissingAlert(python.path)
            return
        }
        soundTestItem.title = "正在测试声音…"
        soundTestItem.isEnabled = false

        let task = Process()
        task.executableURL = python
        task.arguments = ["-m", "audio_gateway", "verify", "capture", "--seconds", "5"]
        task.currentDirectoryURL = gatewayRoot
        task.environment = loginEnvironment()
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = pipe

        DispatchQueue.global().async { [weak self] in
            var output = ""
            do {
                try task.run()
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                task.waitUntilExit()
                output = String(data: data, encoding: .utf8) ?? ""
            } catch {
                output = error.localizedDescription
            }
            let ok = task.terminationStatus == 0
            DispatchQueue.main.async {
                self?.soundTestItem.title = "测试声音（5 秒）"
                self?.soundTestItem.isEnabled = true
                let result = NSAlert()
                result.messageText = ok ? "能听到声音" : "没有采到声音"
                result.informativeText = ok
                    ? "这台电脑正常收到了会议声音，可以开始会议。"
                    : (self?.localSource == true
                        ? "本机会议软件模式下，会议软件没在出声时听不到是正常的。\n\n"
                          + "要确认链路，请在 Teams/Zoom 的音频设置里把\n"
                          + "扬声器选成「BlackHole 2ch」、麦克风选成「BlackHole 16ch」，\n"
                          + "然后播放一段声音再测。"
                        : "请依次确认：\n1. 音源设备正在出声\n2. 音频线插牢（绿孔→粉孔）\n"
                          + "3. 音源那侧的输出设备选的是 USB 声卡\n\n"
                          + "仍不行时，到「帮助与诊断」复制诊断信息。")
                result.alertStyle = ok ? .informational : .warning
                result.addButton(withTitle: "知道了")
                if !ok { result.addButton(withTitle: "复制诊断信息") }
                if result.runModal() == .alertSecondButtonReturn && !ok {
                    self?.copyDiagnostics(detail: output)
                }
            }
        }
    }

    /// 从登录 shell 取环境，确保 ~/.zshenv 里的 API key 能被子进程看到。
    private func loginEnvironment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let shell = Process()
        shell.executableURL = URL(fileURLWithPath: "/bin/zsh")
        shell.arguments = ["-lc", "printenv"]
        let pipe = Pipe()
        shell.standardOutput = pipe
        guard (try? shell.run()) != nil else { return env }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        shell.waitUntilExit()
        for line in (String(data: data, encoding: .utf8) ?? "").split(separator: "\n") {
            guard let eq = line.firstIndex(of: "=") else { continue }
            env[String(line[line.startIndex..<eq])] = String(line[line.index(after: eq)...])
        }
        return env
    }

    private func loadOrCreateToken() -> String {
        let url = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".audio-gateway-token")
        if let existing = try? String(contentsOf: url, encoding: .utf8) {
            let trimmed = existing.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { return trimmed }
        }
        let generated = UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(12)
        try? String(generated).write(to: url, atomically: true, encoding: .utf8)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        return String(generated)
    }

    // MARK: - 首启环境自举（分发形态：bundle 自带源码，venv 首启现建）

    private func ensureEnvironmentReady() {
        guard !FileManager.default.isExecutableFile(atPath: venvPython.path) else { return }
        envPrepStatus = "首次准备环境 · 正在复制运行文件…"
        soundTestItem.isEnabled = false
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.bootstrapEnvironment()
        }
    }

    /// 后台自举：拷源码 → 备好解释器（内嵌优先）→ 建 venv → pip install。
    /// 任一步失败都必须给出"一条可复制的修复命令"——新机器上没有维护者在场，
    /// 只弹一句"环境缺失"等于装死。
    private func bootstrapEnvironment() {
        do {
            try materializeGatewaySources()
        } catch {
            envPrepFailed(
                "无法复制运行文件：\(error.localizedDescription)",
                fixCommand: "重新解压 会议助手.zip，把完整的 会议助手.app 拖进应用程序后再打开")
            return
        }
        // 解释器来源：① bundle 内嵌 CPython（分发形态，新机零依赖）；
        // ② 系统/Homebrew python3（开发用构建可能没随附运行时）。
        guard let python = materializedRuntimePython() ?? suitableSystemPython() else {
            envPrepFailed(
                "这台电脑缺少 Python 3.10 或更新版本（系统自带的太旧）",
                fixCommand: "brew install python@3.12")
            return
        }
        // 修复命令 = 手工重放整个自举，成功后重开 App 即接上正常流程
        let manualBootstrap = "cd '\(gatewayRoot.path)' && '\(python)' -m venv .venv"
            + " && .venv/bin/python -m pip install -r requirements.txt"
        setEnvPrepStatus("首次准备环境 · 正在创建运行环境…")
        guard runBootstrapStep(python, ["-m", "venv", ".venv"]) else {
            envPrepFailed("创建 Python 运行环境失败", fixCommand: manualBootstrap)
            return
        }
        setEnvPrepStatus("首次准备环境 · 正在安装依赖（约 1-3 分钟）…")
        guard runBootstrapStep(venvPython.path,
                               ["-m", "pip", "install", "-r", "requirements.txt"]) else {
            envPrepFailed("安装依赖失败（常见原因：网络不通）", fixCommand: manualBootstrap)
            return
        }
        DispatchQueue.main.async {
            self.envPrepStatus = nil
            self.soundTestItem.isEnabled = true
            self.render()
        }
    }

    /// bundle Resources/gateway → 运行根。已有源码则不重拷：
    /// 覆盖会毁掉用户在运行根里做过的本地调整（如会议 brief）。
    private func materializeGatewaySources() throws {
        let fm = FileManager.default
        if fm.fileExists(atPath: gatewayRoot.appendingPathComponent("audio_gateway/__main__.py").path) {
            return
        }
        guard let payload = Bundle.main.resourceURL?.appendingPathComponent("gateway"),
              fm.fileExists(atPath: payload.appendingPathComponent("audio_gateway").path)
        else {
            throw NSError(domain: "MeetingAssistant", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "App 包内没有随附的运行文件（可能是开发用构建）",
            ])
        }
        try fm.createDirectory(at: gatewayRoot, withIntermediateDirectories: true)
        for name in ["audio_gateway", "requirements.txt", "examples"] {
            let src = payload.appendingPathComponent(name)
            let dst = gatewayRoot.appendingPathComponent(name)
            guard fm.fileExists(atPath: src.path) else { continue }
            if fm.fileExists(atPath: dst.path) { try fm.removeItem(at: dst) }
            try fm.copyItem(at: src, to: dst)
        }
    }

    /// bundle 自带的 CPython 运行时 → 拷到运行根后返回解释器路径。
    /// 必须拷出而不是原地使用：venv 会用绝对路径指回解释器，指进 bundle
    /// 的话 App 一旦移动/重装，已建好的环境就整个失效。
    /// ditto 保符号链接与权限（FileManager.copyItem 语义不够明确，不赌）。
    private func materializedRuntimePython() -> String? {
        let fm = FileManager.default
        let dst = gatewayRoot.appendingPathComponent("runtime")
        let dstPython = dst.appendingPathComponent("bin/python3").path
        if fm.isExecutableFile(atPath: dstPython) { return dstPython }
        guard let src = Bundle.main.resourceURL?.appendingPathComponent("python"),
              fm.isExecutableFile(atPath: src.appendingPathComponent("bin/python3").path)
        else { return nil }
        setEnvPrepStatus("首次准备环境 · 正在安放 Python 运行时…")
        guard runBootstrapStep("/usr/bin/ditto", [src.path, dst.path]),
              fm.isExecutableFile(atPath: dstPython)
        else { return nil }
        return dstPython
    }

    /// 找一个 ≥3.10 的系统 python3。/usr/bin/python3 到 macOS 26 仍是 3.9，
    /// 所以 Homebrew 路径优先；带版本号的候选覆盖"装了 python@3.12 但没有
    /// python3 软链"的形态（MACMINI 真机就是这样）。
    private func suitableSystemPython() -> String? {
        let candidates = [
            "/opt/homebrew/bin/python3",
            "/opt/homebrew/bin/python3.13",
            "/opt/homebrew/bin/python3.12",
            "/opt/homebrew/bin/python3.11",
            "/opt/homebrew/bin/python3.10",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ]
        for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
            let probe = Process()
            probe.executableURL = URL(fileURLWithPath: path)
            probe.arguments = [
                "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)",
            ]
            probe.standardOutput = FileHandle.nullDevice
            probe.standardError = FileHandle.nullDevice
            guard (try? probe.run()) != nil else { continue }
            probe.waitUntilExit()
            if probe.terminationStatus == 0 { return path }
        }
        return nil
    }

    /// 在运行根下执行一步自举命令；输出并入 app 日志，失败时诊断有据可查。
    private func runBootstrapStep(_ executable: String, _ arguments: [String]) -> Bool {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: executable)
        task.arguments = arguments
        task.currentDirectoryURL = gatewayRoot
        // pip 要走用户的网络/代理配置，取登录环境而不是 app 的裸环境
        task.environment = loginEnvironment()
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        if let handle = try? FileHandle(forWritingTo: logURL) {
            handle.seekToEndOfFile()
            task.standardOutput = handle
            task.standardError = handle
        }
        guard (try? task.run()) != nil else { return false }
        task.waitUntilExit()
        return task.terminationStatus == 0
    }

    private func setEnvPrepStatus(_ line: String) {
        DispatchQueue.main.async {
            self.envPrepStatus = line
            self.render()
        }
    }

    /// fail-closed：状态行常驻失败原因，弹窗给一条可复制命令；「开始会议」保持
    /// 禁用，修好后重开 App 重试（自举幂等，已成功的步骤会被跳过）。
    private func envPrepFailed(_ reason: String, fixCommand: String) {
        DispatchQueue.main.async {
            self.envPrepStatus = "环境准备失败 · 修复后重新打开本 App"
            self.render()
            let box = NSAlert()
            box.messageText = "首次准备环境失败"
            box.informativeText = reason
                + "\n\n请在「终端」执行下面这条命令，然后重新打开本 App：\n\n"
                + fixCommand
            box.alertStyle = .critical
            box.addButton(withTitle: "复制修复命令")
            box.addButton(withTitle: "知道了")
            if box.runModal() == .alertFirstButtonReturn {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(fixCommand, forType: .string)
            }
        }
    }

    /// BlackHole 装没装看 HAL 插件目录即可：驱动是系统级安装，位置固定，
    /// 不必唤起 CoreAudio 枚举设备。
    private func blackHoleInstalled() -> Bool {
        let hal = "/Library/Audio/Plug-Ins/HAL"
        return FileManager.default.fileExists(atPath: "\(hal)/BlackHole2ch.driver")
            && FileManager.default.fileExists(atPath: "\(hal)/BlackHole16ch.driver")
    }

    // MARK: - 状态轮询（唯一真相在 Python 侧 /status）

    private func pollStatus() {
        guard process?.isRunning == true, !token.isEmpty else { return }
        guard let url = URL(string: "http://127.0.0.1:\(kGatewayPort)/status?t=\(token)") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: request) { [weak self] data, _, _ in
            guard let data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else { return }
            DispatchQueue.main.async {
                guard self?.process?.isRunning == true else { return }
                self?.acceptStatus(json)
            }
        }.resume()
    }

    private func acceptStatus(_ json: [String: Any]) {
        starting = false
        lastStatus = json
        if let phase = json["phase"] as? String {
            if phase == "post_processing" && lastObservedPhase != "post_processing" {
                postProcessingStartedAt = Date()
            }
            lastObservedPhase = phase
        }
        if let path = json["session_dir"] as? String, !path.isEmpty {
            lastSessionDirectory = URL(fileURLWithPath: path)
        }
        render()
    }

    @objc private func toggleMute() {
        guard mothballedUplinkProductEntriesEnabled || speakEnabled,
              process?.isRunning == true else { return }
        let muted = (lastStatus["muted"] as? Bool) ?? false
        guard let url = URL(string: "http://127.0.0.1:\(kGatewayPort)/mute?t=\(token)") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "content-type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["muted": !muted])
        URLSession.shared.dataTask(with: request) { [weak self] _, _, _ in
            DispatchQueue.main.async { self?.pollStatus() }
        }.resume()
    }

    // 字幕/建议必须在服务启动时决定。会议中用置灰表达这条约束，
    // 不弹"重启生效"——那是把实现细节丢给用户。
    @objc private func toggleSubtitles() {
        guard process?.isRunning != true else { return }
        subtitlesEnabled.toggle()
        render()
    }

    @objc private func useLocalSource() {
        guard process?.isRunning != true else { return }
        localSource = true
        render()
    }

    @objc private func useExternalSource() {
        guard process?.isRunning != true else { return }
        localSource = false
        render()
    }

    @objc private func toggleRehearse() {
        guard process?.isRunning != true else { return }
        rehearseEnabled.toggle()
        if rehearseEnabled { speakEnabled = false }
        render()
    }

    @objc private func toggleSpeak() {
        guard process?.isRunning != true else { return }
        speakEnabled.toggle()
        if speakEnabled { rehearseEnabled = false }
        render()
    }

    @objc private func toggleAdvice() {
        guard process?.isRunning != true else { return }
        adviceEnabled.toggle()
        render()
    }

    // MARK: - 告警：内部码 → 用户能照做的话

    private func userAlerts() -> [UserAlert] {
        var result: [UserAlert] = []
        let raw = (lastStatus["alerts"] as? [String]) ?? []
        let link = lastStatus["link"] as? String
        let interpreter = lastStatus["interpreter"] as? [String: Any]

        let noSoundGuidance = [
            "会议软件/音源设备是否正在出声",
            "两台电脑之间的音频线是否插牢（绿孔 → 粉孔）",
            "音源那侧的输出设备是否选对（本机 Teams 应选 BlackHole 2ch）",
            "本 App 是否已获得麦克风权限（系统设置 → 隐私与安全性 → 麦克风）",
        ]

        for item in raw {
            if item.contains("digital-zero") {
                result.append(UserAlert(
                    severity: .fatal,
                    title: "🔴 没采到声音，这段在录空白 — 点此检查",
                    guidance: noSoundGuidance))
            } else if item.contains("clipping") {
                result.append(UserAlert(
                    severity: .degraded,
                    title: "🟠 声音过大可能失真 — 点此调整",
                    guidance: [
                        "把音源那侧的输出音量调小到 50~60%",
                        "字幕识别会因削顶而出错，录音也会有爆音",
                    ]))
            } else if item.hasPrefix("参谋:") {
                // AI 参谋失败是降级不是事故：录音/字幕/纪要全都不受影响，
                // 服务端会指数退避自动重试、恢复后自动清除本告警。
                // 服务端用中文前缀还有一层用意：alerts 是 sorted() 的，CJK
                // 排在 ASCII 之后，参谋告警不会把 "interpreter:"（字幕中断）
                // 的引导挤出 alerts.first——菜单栏只显示第一条。
                result.append(UserAlert(
                    severity: .degraded,
                    title: "🟠 AI 建议暂时不可用，录音与字幕正常 — 点此查看",
                    guidance: [
                        "AI 参谋调用失败，正在自动退避重试；录音、字幕与纪要不受影响。",
                        "长时间不恢复时，常见原因是 ANTHROPIC_API_KEY 失效或网络受限。",
                        "会议可照常进行，恢复后建议会自动继续，无需任何操作。",
                    ]))
            } else {
                result.append(UserAlert(
                    severity: .degraded,
                    title: "🟠 有一项异常，录音仍在继续 — 点此查看",
                    guidance: [item]))
            }
        }
        if let link, link == "down" {
            result.append(UserAlert(
                severity: .fatal,
                title: "🔴 声音通道断了，正在录空白 — 点此检查",
                guidance: noSoundGuidance))
        } else if let link, link == "degraded" {
            result.append(UserAlert(
                severity: .degraded,
                title: "🟠 声音有卡顿，可能漏字 — 点此查看",
                guidance: ["设备或链路不稳定；录音继续，但字幕可能漏句。",
                           "详情见「帮助与诊断 → 运行详情」。"]))
        }
        if let interpreter,
           (interpreter["enabled"] as? Bool) == true,
           (interpreter["connected"] as? Bool) != true,
           lastObservedPhase == "running" {
            result.append(UserAlert(
                severity: .degraded,
                title: "🟠 字幕中断了，录音正常 — 点此查看",
                guidance: ["字幕服务正在自动重连，录音与纪要不受影响。",
                           "若长时间不恢复，会后仍可从录音生成纪要。"]))
        }
        return result
    }

    @objc private func showAlertGuidance() {
        let alerts = userAlerts()
        guard let first = alerts.first else { return }
        let panel = NSAlert()
        panel.messageText = first.title
            .replacingOccurrences(of: "🔴 ", with: "")
            .replacingOccurrences(of: "🟠 ", with: "")
            .replacingOccurrences(of: " — 点此检查", with: "")
            .replacingOccurrences(of: " — 点此调整", with: "")
            .replacingOccurrences(of: " — 点此查看", with: "")
        panel.informativeText = first.guidance
            .enumerated()
            .map { "\($0.offset + 1). \($0.element)" }
            .joined(separator: "\n")
        panel.alertStyle = first.severity == .fatal ? .critical : .warning
        panel.addButton(withTitle: "知道了")
        panel.addButton(withTitle: "复制诊断信息")
        if panel.runModal() == .alertSecondButtonReturn {
            copyDiagnostics(detail: nil)
        }
    }

    // MARK: - 渲染

    private func elapsed(since date: Date?) -> String {
        guard let date else { return "" }
        let total = Int(Date().timeIntervalSince(date))
        return String(format: "%02d:%02d", total / 60, total % 60)
    }

    private func render() {
        let running = process?.isRunning == true
        let phase = running ? ((lastStatus["phase"] as? String) ?? lastObservedPhase) : "idle"
        let postProcessing = phase == "post_processing"
        let done = phase == "done"
        let alerts = userAlerts()
        let fatal = alerts.contains { $0.severity == .fatal }
        let degraded = !alerts.isEmpty
        let interpreter = lastStatus["interpreter"] as? [String: Any]
        let subtitlesLive = (interpreter?["enabled"] as? Bool) == true

        // 菜单栏图标：录音时长每秒跳动，本身就是"还活着"的证明。
        // 红=此刻在录空白必须马上处理；橙=仍在正常录音，可会后再管。
        let icon: String
        if !running {
            icon = minutesUnread ? "🎧📄" : "🎧"
        } else if postProcessing {
            icon = "🎧⏳ " + elapsed(since: postProcessingStartedAt)
        } else if done {
            icon = cancelRequested ? "🎧" : "🎧📄"
        } else if starting || lastStatus["link"] == nil {
            icon = "🎧…"
        } else if fatal {
            icon = "🎧🔴"
        } else if degraded {
            icon = "🎧🟠"
        } else {
            icon = "🎧 " + elapsed(since: meetingStartedAt)
        }
        statusItem.button?.title = icon

        // 状态行（首启自举进行/失败时它就是唯一的进度反馈面）
        if !running {
            stateItem.title = envPrepStatus
                ?? (minutesUnread ? "纪要已就绪 · 点下方打开" : "待命中 · 未开始录音")
        } else if postProcessing {
            let step = lastStatus["post_processing_step"] as? String
            stateItem.title = postProcessingTitle(step) + " · 已等 " + elapsed(since: postProcessingStartedAt)
        } else if done {
            stateItem.title = cancelRequested
                ? "已取消 · 录音留在会议文件夹"
                : "纪要已就绪 · 点下方打开"
        } else if starting || lastStatus["link"] == nil {
            stateItem.title = "正在检查麦克风和声音…"
        } else if stopRequested {
            stateItem.title = "正在收尾录音…"
        } else {
            var line = "正在录音 " + elapsed(since: meetingStartedAt)
            if fatal { line += " · 收不到声音" }
            else if subtitlesLive {
                line += (interpreter?["connected"] as? Bool) == true ? " · 中文字幕已开" : " · 字幕重连中"
            } else { line += " · 声音正常" }
            stateItem.title = line
        }

        hintItem.isHidden = !postProcessing
        // 声音告警只描述"正在录的这一刻"。会议已进入收尾/结束后它就不再可行动，
        // 留着只会让人以为刚生成的录音有救——2026-07-30 实测残留过一次。
        let liveAlerts = (postProcessing || done) ? [] : alerts
        alertItem.isHidden = liveAlerts.isEmpty
        alertItem.title = liveAlerts.first?.title ?? ""
        alertItem.isEnabled = !liveAlerts.isEmpty

        // 动作项：未开始只见「开始/测试」，会议中只见「结束/取消」。
        // 收尾与结束态两组都不留——已经结束的会议再摆一个灰着的「取消这次录音」，
        // 只会让人怀疑是不是还没结束干净。
        let idle = !running
        let finishing = postProcessing || done
        startItem.isHidden = !idle
        soundTestItem.isHidden = !idle
        stopItem.isHidden = idle || finishing
        cancelItem.isHidden = idle || finishing
        startItem.isEnabled = idle && !starting && envPrepStatus == nil
        stopItem.isEnabled = running && !finishing && !stopRequested
        stopItem.title = stopRequested ? "正在结束…" : "结束会议并生成纪要"
        cancelItem.isEnabled = stopItem.isEnabled
        // 只在"正式发言"的会议里出现：其它场景没有会议支路可静音
        muteItem.isHidden = !(running && speakEnabled)
        muteItem.isEnabled = running && speakEnabled && phase == "running"

        subtitleToggle.state = subtitlesEnabled ? .on : .off
        adviceToggle.state = (subtitlesEnabled && adviceEnabled) ? .on : .off
        sourceLocalItem.state = localSource ? .on : .off
        sourceExternalItem.state = localSource ? .off : .on
        sourceLocalItem.isEnabled = idle
        sourceExternalItem.isEnabled = idle
        rehearseToggle.state = rehearseEnabled ? .on : .off
        speakToggle.state = speakEnabled ? .on : .off
        subtitleToggle.isEnabled = idle
        adviceToggle.isEnabled = idle && subtitlesEnabled
        // 排练/正式发言不依赖字幕开关：它们是独立的 Realtime 会话
        rehearseToggle.isEnabled = idle
        speakToggle.isEnabled = idle
        settingsHint.isHidden = idle

        minutesItem.isHidden = latestMinutesURL() == nil
        minutesItem.isEnabled = !minutesItem.isHidden
    }

    private func postProcessingTitle(_ step: String?) -> String {
        // 取消录音时后端只做转码，不转写也不写纪要；沿用"共 3 步"会凭空多报两步。
        if cancelRequested { return "正在保存录音" }
        switch step {
        case "transcribe": return "整理纪要 · 第 1 步/共 3 步：转写录音"
        case "summarize":  return "整理纪要 · 第 2 步/共 3 步：撰写纪要"
        case "convert":    return "整理纪要 · 第 3 步/共 3 步：转换音频"
        default:           return "整理纪要 · 正在收尾"
        }
    }

    // MARK: - 产物与诊断

    private func latestMinutesURL() -> URL? {
        guard let sessions = try? FileManager.default.contentsOfDirectory(
            at: meetingsRoot,
            includingPropertiesForKeys: [.contentModificationDateKey]
        ) else { return nil }
        let candidates = sessions
            .filter { $0.hasDirectoryPath }
            .map { $0.appendingPathComponent("minutes.md") }
            .filter { FileManager.default.fileExists(atPath: $0.path) }
        return candidates.sorted {
            let a = (try? $0.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
            let b = (try? $1.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
            return a > b
        }.first
    }

    @objc private func openLatestMinutes() {
        guard let url = latestMinutesURL() else { return }
        minutesUnread = false
        NSWorkspace.shared.open(url)
        render()
    }

    @objc private func openMeetingsFolder() {
        NSWorkspace.shared.open(meetingsRoot)
    }

    @objc private func openPanel() {
        guard !token.isEmpty,
              let url = URL(string: "http://127.0.0.1:\(kGatewayPort)/?t=\(token)") else { return }
        NSWorkspace.shared.open(url)
    }

    @objc private func openLog() { NSWorkspace.shared.open(logURL) }

    @objc private func copyDiagnosticsAction() { copyDiagnostics(detail: nil) }

    private func copyDiagnostics(detail: String?) {
        var lines = ["会议助手诊断信息"]
        lines.append("时间：\(ISO8601DateFormatter().string(from: Date()))")
        lines.append("服务运行中：\(process?.isRunning == true)")
        lines.append("阶段：\(lastObservedPhase)")
        if let data = try? JSONSerialization.data(withJSONObject: lastStatus, options: [.sortedKeys]),
           let text = String(data: data, encoding: .utf8) {
            lines.append("状态：\(text)")
        }
        if let detail, !detail.isEmpty { lines.append("输出：\n\(detail)") }
        if let tail = try? String(contentsOf: logURL, encoding: .utf8) {
            lines.append("日志尾部：\n" + tail.suffix(2000))
        }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(lines.joined(separator: "\n"), forType: .string)
    }

    @objc private func copyAgentCommand() {
        guard mothballedUplinkProductEntriesEnabled else { return }
        let host = primaryIPv4() ?? "<Mac mini IP>"
        let command = ".venv/bin/python -m audio_gateway micagent"
            + " --url http://\(host):\(kGatewayPort) --token \(token)"
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(command, forType: .string)
    }

    @objc private func quit() {
        if lastObservedPhase == "post_processing", process?.isRunning == true {
            let confirm = NSAlert()
            confirm.messageText = "纪要还在生成中"
            confirm.informativeText = "现在退出会中断整理，需要重新处理。录音文件不会丢失。"
            confirm.alertStyle = .warning
            confirm.addButton(withTitle: "继续等待")
            confirm.addButton(withTitle: "仍要退出")
            guard confirm.runModal() == .alertSecondButtonReturn else { return }
        }
        if process?.isRunning == true {
            quitAfterStop = true
            requestStop(cancelRecording: false)
            return
        }
        NSApp.terminate(nil)
    }

    private func primaryIPv4() -> String? {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/sbin/ipconfig")
        task.arguments = ["getifaddr", "en0"]
        let pipe = Pipe()
        task.standardOutput = pipe
        guard (try? task.run()) != nil else { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        let value = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return (value?.isEmpty == false) ? value : nil
    }

    private func alert(_ title: String, _ text: String) {
        let box = NSAlert()
        box.messageText = title
        box.informativeText = text
        box.alertStyle = .warning
        box.runModal()
    }

    private func environmentMissingAlert(_ path: String) {
        let box = NSAlert()
        // 启动后 venv 才消失的罕见形态才会走到这里（首启缺 venv 由自举兜底）
        box.messageText = "运行环境缺失，暂时无法开始"
        box.informativeText = "重新打开本 App 会自动准备环境；仍失败请联系配置这台电脑的人。\n缺少：\(path)"
        box.alertStyle = .critical
        box.addButton(withTitle: "知道了")
        box.addButton(withTitle: "复制诊断信息")
        if box.runModal() == .alertSecondButtonReturn {
            copyDiagnostics(detail: "缺少运行环境：\(path)")
        }
    }

    /// 从启动日志里取出真正的失败项，而不是猜一个最可能的原因写死。
    /// 2026-07-30 教训：文案硬写"打不开声音输入"，真实原因却是增益校准
    /// 不适用于数字音源——用户照着错误提示查了一圈全是对的。
    private func startupFailureReasons() -> [String] {
        guard let log = try? String(contentsOf: logURL, encoding: .utf8) else {
            return []
        }
        var reasons: [String] = []
        for line in log.split(separator: "\n") {
            // doctor 的指引行形如 "- 系统输入增益: 运行 ..."，它已经是人话
            if line.hasPrefix("- ") && line.contains(":") {
                reasons.append(String(line.dropFirst(2)))
            }
        }
        return Array(reasons.suffix(4))
    }

    private func startFailureAlert() {
        let box = NSAlert()
        box.messageText = "无法开始录音"
        let reasons = startupFailureReasons()
        box.informativeText = reasons.isEmpty
            ? "启动自检没通过。点「复制诊断信息」把详情发给维护者。"
            : "启动自检没通过：\n\n"
                + reasons.map { "• " + $0 }.joined(separator: "\n")
        box.alertStyle = .warning
        box.addButton(withTitle: "知道了")
        box.addButton(withTitle: "测试声音")
        box.addButton(withTitle: "复制诊断信息")
        switch box.runModal() {
        case .alertSecondButtonReturn: testSound()
        case .alertThirdButtonReturn: copyDiagnostics(detail: nil)
        default: break
        }
    }

    private func showCompletionAlert(
        sessionDirectory: URL,
        notes: [String],
        cancelled: Bool
    ) {
        let completion = NSAlert()
        // 取消的会议没有纪要，也不该把后端的收尾说明当结果读给用户听。
        completion.messageText = cancelled ? "这次录音已取消" : "纪要已生成"
        if cancelled {
            completion.informativeText = "没有生成纪要。录音已保存，需要时可以从会议文件夹里找回。"
        } else {
            completion.informativeText = notes.isEmpty
                ? sessionDirectory.path
                : notes.joined(separator: "\n")
        }
        completion.alertStyle = .informational
        let minutes = sessionDirectory.appendingPathComponent("minutes.md")
        let hasMinutes = !cancelled
            && FileManager.default.fileExists(atPath: minutes.path)
        if hasMinutes { completion.addButton(withTitle: "打开纪要") }
        completion.addButton(withTitle: "打开文件夹")
        completion.addButton(withTitle: "关闭")
        let response = completion.runModal()
        if hasMinutes && response == .alertFirstButtonReturn {
            minutesUnread = false
            NSWorkspace.shared.open(minutes)
        } else if (hasMinutes && response == .alertSecondButtonReturn)
                    || (!hasMinutes && response == .alertFirstButtonReturn) {
            NSWorkspace.shared.open(sessionDirectory)
        }
    }
}

// @main + -parse-as-library：app 从单文件长成多文件（字幕窗口独立成文件）后，
// 顶层语句只允许出现在 main.swift 里，显式入口是唯一不靠文件名的写法。
@main
enum MeetingAssistantMain {
    static func main() {
        let app = NSApplication.shared
        let controller = GatewayController()
        app.delegate = controller
        app.setActivationPolicy(.accessory)   // 菜单栏常驻，不占 Dock
        app.run()
    }
}
