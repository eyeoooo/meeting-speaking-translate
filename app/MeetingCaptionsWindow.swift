// 会议字幕窗口 —— 语音产品的原生呈现面（2026-07-30 用户裁定：不在浏览器运行）。
//
// 为什么是原生窗口而不是浏览器面板：语音模块是独立产品，KVM 只是音源之一；
// 产品不能依赖"用户恰好开着某个网页"。本窗口由菜单栏 app 直连 bridge 的
// WebSocket（token 就在 app 手里，天然零配置），浮在最上层，会议软件全屏时
// 字幕仍可见。
//
// 呈现规格沿用双流裁定：原文与译文是两条独立时间轴（Realtime 协议无配对字段，
// 任何按序配对都是猜测），所以是两条泳道各自滚动，中文主位、日文辅位，
// 绝不把两条流拼成"一行原文配一行译文"。

import AppKit
import Foundation

/// 单条泳道：标题 + 只读滚动文本。追加行数封顶，避免长会议无限膨胀。
/// 末尾可挂一行灰字草稿（未成句的转写中间态）：后一条整体取代前一条，
/// 空串即清除——与正式行的 append-only 语义互不干扰。
private final class CaptionLane {
    let container = NSView()
    private let textView: NSTextView
    private let scrollView: NSScrollView
    private let font: NSFont
    private let maxLines: Int
    private var lines: [String] = []
    private var draft = ""

    init(title: String, fontSize: CGFloat, maxLines: Int = 120) {
        self.maxLines = maxLines
        self.font = .systemFont(ofSize: fontSize)

        let label = NSTextField(labelWithString: title)
        label.font = .systemFont(ofSize: 11, weight: .semibold)
        label.textColor = .secondaryLabelColor

        scrollView = NSTextView.scrollableTextView()
        textView = scrollView.documentView as! NSTextView
        textView.isEditable = false
        textView.isRichText = false
        textView.font = .systemFont(ofSize: fontSize)
        textView.textContainerInset = NSSize(width: 6, height: 6)
        textView.autoresizingMask = [.width]
        scrollView.hasVerticalScroller = true

        label.translatesAutoresizingMaskIntoConstraints = false
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(label)
        container.addSubview(scrollView)
        NSLayoutConstraint.activate([
            label.topAnchor.constraint(equalTo: container.topAnchor, constant: 4),
            label.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 6),
            scrollView.topAnchor.constraint(equalTo: label.bottomAnchor, constant: 4),
            scrollView.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            scrollView.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])
    }

    func append(_ line: String) {
        lines.append(line)
        if lines.count > maxLines {
            lines.removeFirst(lines.count - maxLines)
        }
        render()
    }

    /// 整块替换（AI 建议通道用）：屏上永远只有当前一条，不堆历史。
    /// 会中的人只有一瞥的注意力；历史仍在 advice.jsonl 与网页面板里。
    func replace(_ text: String) {
        lines = text.components(separatedBy: "\n")
        render()
    }

    func setDraft(_ text: String) {
        guard draft != text else { return }
        draft = text
        render()
    }

    func reset() {
        lines = []
        draft = ""
        textView.string = ""
    }

    private func render() {
        // 只有用户本来就贴着底部时才自动跟随——往回翻旧字幕时绝不抢滚动条
        let atBottom = isPinnedToBottom()
        let body = NSMutableAttributedString(
            string: lines.joined(separator: "\n"),
            attributes: [.font: font, .foregroundColor: NSColor.labelColor]
        )
        if !draft.isEmpty {
            body.append(NSAttributedString(
                string: (body.length == 0 ? "" : "\n") + draft,
                attributes: [
                    .font: font,
                    .foregroundColor: NSColor.secondaryLabelColor,
                ]
            ))
        }
        textView.textStorage?.setAttributedString(body)
        if atBottom {
            textView.scrollToEndOfDocument(nil)
        }
    }

    private func isPinnedToBottom() -> Bool {
        guard let documentView = scrollView.documentView else { return true }
        let visible = scrollView.contentView.documentVisibleRect
        return visible.maxY >= documentView.bounds.maxY - 24
    }
}

/// 会议字幕窗口：两条泳道 + AI 建议区 + 状态行。
/// 生命周期由菜单栏 app 驱动：会议开始 connect(token:)，会议结束 markEnded()。
final class MeetingCaptionsWindowController: NSWindowController, NSWindowDelegate {
    private let zhLane = CaptionLane(title: "中文（同传）", fontSize: 17)
    private let jaLane = CaptionLane(title: "日本語（原文）", fontSize: 12)
    // 建议是替换语义（一次一条），字号取比字幕大——为"一瞥"设计
    private let adviceLane = CaptionLane(title: "AI 建议", fontSize: 15, maxLines: 4)
    // M1 发言排练：我的中文转写与日语译文（对方听不到，仅自查）。
    // 两条流粒度天然不等，同一泳道按到达序交错，前缀区分 说/訳。
    private let rehearsalLane = CaptionLane(
        title: "我的发言 · 说=你的中文 訳=日语译文",
        fontSize: 13,
        maxLines: 80
    )
    private let statusLabel = NSTextField(labelWithString: "未连接")

    private var socketTask: URLSessionWebSocketTask?
    private var session: URLSession?
    private var token: String = ""
    private var reconnectDelay: TimeInterval = 3
    private var wantConnected = false
    private var seenSegmentIds = Set<Int>()
    // 排练段与会议段是两个独立的 id 空间（两套 SegmentHistory 都从 0 起），
    // 必须分开去重，否则互相吞段。
    private var seenRehearsalIds = Set<Int>()

    convenience init() {
        let window = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 720, height: 480),
            styleMask: [.titled, .closable, .resizable, .utilityWindow],
            backing: .buffered,
            defer: true
        )
        window.title = "会议字幕"
        // 浮在普通窗口之上：会议软件全屏时字幕仍可见——这是产品存在的理由。
        // .floating 只解决"盖在别的窗口上"，不解决"跟进全屏空间"：
        // 2026-07-30 真机验收实测，用户在全屏空间里点「打开字幕窗口」，
        // 窗口默默开在普通桌面空间，看起来就是"没反应"。
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 480, height: 320)
        self.init(window: window)
        window.delegate = self
        buildContent()
    }

    private func buildContent() {
        guard let content = window?.contentView else { return }

        statusLabel.font = .systemFont(ofSize: 11)
        statusLabel.textColor = .secondaryLabelColor

        // 中文主位（宽 60%），日文辅位；建议区横贯底部
        let lanes = NSStackView(views: [zhLane.container, jaLane.container])
        lanes.orientation = .horizontal
        lanes.distribution = .fill
        lanes.spacing = 8
        zhLane.container.setContentHuggingPriority(.defaultLow, for: .horizontal)
        NSLayoutConstraint.activate([
            zhLane.container.widthAnchor.constraint(
                equalTo: lanes.widthAnchor, multiplier: 0.58),
        ])

        // 排练泳道默认隐藏，收到第一条排练段才出现——不排练的会议不占空间
        rehearsalLane.container.isHidden = true
        let root = NSStackView(views: [
            lanes, rehearsalLane.container, adviceLane.container, statusLabel,
        ])
        root.orientation = .vertical
        root.spacing = 8
        root.edgeInsets = NSEdgeInsets(top: 8, left: 8, bottom: 8, right: 8)
        root.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(root)
        NSLayoutConstraint.activate([
            root.topAnchor.constraint(equalTo: content.topAnchor),
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            root.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            lanes.heightAnchor.constraint(
                equalTo: root.heightAnchor, multiplier: 0.62),
        ])
    }

    // MARK: - 生命周期（由菜单栏 app 调用）

    func connect(token: String) {
        self.token = token
        wantConnected = true
        reconnectDelay = 3
        seenSegmentIds.removeAll()
        seenRehearsalIds.removeAll()
        zhLane.reset()
        jaLane.reset()
        adviceLane.reset()
        rehearsalLane.reset()
        rehearsalLane.container.isHidden = true
        setStatus("连接中…")
        openSocket()
        fetchHistory()
    }

    func markEnded() {
        wantConnected = false
        socketTask?.cancel(with: .normalClosure, reason: nil)
        socketTask = nil
        // 会议结束后灰字草稿已无后续，留着会像"卡住了"；正式行原样保留。
        zhLane.setDraft("")
        jaLane.setDraft("")
        setStatus("会议已结束 · 字幕停止更新")
    }

    func windowWillClose(_ notification: Notification) {
        // 关窗只是不看了，不影响会议；下次打开重新拉全量历史
        wantConnected = false
        socketTask?.cancel(with: .normalClosure, reason: nil)
        socketTask = nil
    }

    // MARK: - WebSocket

    private func openSocket() {
        guard wantConnected,
              let url = URL(string: "ws://127.0.0.1:\(kGatewayPort)/ws?t=\(token)")
        else { return }
        let session = URLSession(configuration: .ephemeral)
        self.session = session
        let task = session.webSocketTask(with: url)
        socketTask = task
        task.resume()
        receiveLoop(task)
    }

    private func receiveLoop(_ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let message):
                if case .string(let text) = message {
                    DispatchQueue.main.async { self.handleMessage(text) }
                }
                // 二进制帧是下行 PCM（浏览器面板的播放通道），本窗口不消费
                self.receiveLoop(task)
            case .failure:
                DispatchQueue.main.async { self.scheduleReconnect() }
            }
        }
    }

    private func scheduleReconnect() {
        guard wantConnected else { return }
        setStatus("连接断开，自动重连…")
        let delay = reconnectDelay
        // 指数退避封顶 30s：会议还没开始时不空转打日志
        reconnectDelay = min(reconnectDelay * 2, 30)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.openSocket()
        }
    }

    private func fetchHistory() {
        guard let url = URL(string: "http://127.0.0.1:\(kGatewayPort)/history?t=\(token)")
        else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let self, let data,
                  let json = try? JSONSerialization.jsonObject(with: data)
                    as? [String: Any]
            else { return }
            DispatchQueue.main.async {
                for record in (json["segments"] as? [[String: Any]]) ?? [] {
                    self.applySegment(record)
                }
                for record in (json["rehearsal_segments"] as? [[String: Any]]) ?? [] {
                    self.applyRehearsalSegment(record)
                }
                // 建议通道是替换语义：断线重连/中途打开窗口只取最后一条。
                if let markdown = ((json["advice"] as? [[String: Any]]) ?? [])
                    .last?["markdown"] as? String {
                    self.adviceLane.replace(markdown)
                }
            }
        }.resume()
    }

    private func handleMessage(_ text: String) {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any],
              let type = json["type"] as? String
        else { return }
        switch type {
        case "segment":
            reconnectDelay = 3
            applySegment(json)
        case "segment_draft":
            reconnectDelay = 3
            applyDraft(json)
        case "rehearsal_segment":
            reconnectDelay = 3
            applyRehearsalSegment(json)
        case "advice":
            if let markdown = json["markdown"] as? String {
                adviceLane.replace("\(timestampNow())  \(markdown)")
            }
        case "state":
            reconnectDelay = 3
            applyState(json)
        default:
            break
        }
    }

    private func applySegment(_ record: [String: Any]) {
        guard let stream = record["stream"] as? String,
              let text = record["text"] as? String
        else { return }
        // /history 补拉与 WS 实时流会重叠，按服务端 id 去重
        if let id = record["id"] as? Int {
            if seenSegmentIds.contains(id) { return }
            seenSegmentIds.insert(id)
        }
        let stamp = formatElapsed(record)
        let line = stamp.isEmpty ? text : "[\(stamp)] \(text)"
        if stream == "translation" {
            zhLane.append(line)
        } else if stream == "source" {
            jaLane.append(line)
        }
    }

    private func applyDraft(_ record: [String: Any]) {
        // 草稿无 id 不去重：同泳道后一条整体取代前一条，空串清除灰字。
        guard let stream = record["stream"] as? String,
              let text = record["text"] as? String
        else { return }
        if stream == "translation" {
            zhLane.setDraft(text)
        } else if stream == "source" {
            jaLane.setDraft(text)
        }
    }

    private func applyRehearsalSegment(_ record: [String: Any]) {
        guard let stream = record["stream"] as? String,
              let text = record["text"] as? String
        else { return }
        if let id = record["id"] as? Int {
            if seenRehearsalIds.contains(id) { return }
            seenRehearsalIds.insert(id)
        }
        if rehearsalLane.container.isHidden {
            rehearsalLane.container.isHidden = false
        }
        let prefix = stream == "source" ? "说" : "訳"
        let stamp = formatElapsed(record)
        rehearsalLane.append(
            stamp.isEmpty ? "\(prefix) \(text)" : "[\(stamp)] \(prefix) \(text)"
        )
    }

    private func applyState(_ json: [String: Any]) {
        // digital-zero = 会议采集持续全零：录音在录空白，字幕永远不会出来。
        // 2026-07-31 Teams 实战整场作废：告警只到菜单角标，用户全程没看见。
        // 字幕窗是用户会中盯着的面，所以告警期间状态行必须让位给它，
        // 压过一切常规状态文案；告警恢复后下一条 state 自然把状态行还原。
        let alerts = (json["alerts"] as? [String]) ?? []
        if alerts.contains(where: { $0.contains("digital-zero") }) {
            setStatus(
                "🔴 听不到会议声音，请检查 Teams 扬声器是否设为 BlackHole 2ch",
                emphasized: true
            )
            return
        }
        let interpreter = json["interpreter"] as? [String: Any]
        let enabled = (interpreter?["enabled"] as? Bool) ?? false
        let connected = (interpreter?["connected"] as? Bool) ?? false
        if !enabled {
            setStatus("本场会议未开字幕（录音照常）")
        } else if connected {
            setStatus("字幕运行中 · 同传延迟约 1-3 秒")
        } else {
            setStatus("字幕服务重连中，录音不受影响")
        }
    }

    // MARK: - 小工具

    private func setStatus(_ text: String, emphasized: Bool = false) {
        statusLabel.stringValue = text
        statusLabel.font = emphasized
            ? .systemFont(ofSize: 13, weight: .bold)
            : .systemFont(ofSize: 11)
        statusLabel.textColor = emphasized ? .systemRed : .secondaryLabelColor
    }

    private func formatElapsed(_ record: [String: Any]) -> String {
        // elapsed_ms 只是 epoch 内显示标记；缺失时退回 t（monotonic 起点秒数）
        let seconds: Double
        if let ms = record["elapsed_ms"] as? Double {
            seconds = ms / 1000.0
        } else if let ms = record["elapsed_ms"] as? Int {
            seconds = Double(ms) / 1000.0
        } else if let t = record["t"] as? Double {
            seconds = t
        } else {
            return ""
        }
        let total = Int(seconds)
        return String(format: "%02d:%02d", total / 60, total % 60)
    }

    private func timestampNow() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm"
        return formatter.string(from: Date())
    }
}
