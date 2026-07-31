// KVM 控制台.app —— 纯 KVM 控制台的薄启动器（视频 / HID / 文字投入）。
//
// 2026-07-31 产品边界收官：会议模块独立为「会议助手.app」，本 app 与会议
// 完全无关——不碰麦克风、不拉任何后台服务。唯一职责：打开 KVM 控制台网页
// （Chrome PWA 优先，降级默认浏览器），然后立即退出。
// bundle id 是全新的 dev.controller-agent.kvm-console，不声明麦克风权限。
//
// 2026-07-30 视觉验收的两个实测结论决定了这里的实现：
// ① 本 app 直接执行 /Applications/Google Chrome.app 内的二进制会被 macOS 26
//    的 App 管理保护静默拒绝（TCC kTCCServiceSystemPolicyAppBundles = 0），
//    因此绝不能用 Process 去 spawn Chrome 可执行文件；
// ② 独立 --user-data-dir 会丢掉摄像头（Elgato 采集卡）授权与登录态，
//    KVM 视频起不来，所以必须使用 Chrome 默认 profile。
// 结论：优先启动 Chrome 自己生成的 PWA（"将网页作为应用安装"，默认 profile、
// 独立窗口、由 LaunchServices 启动，不触碰任何 App bundle）；未安装时降级为
// 默认浏览器打开同一 URL（普通窗口，功能完全一致）。

import AppKit
import Foundation

let kKvmConsoleURL = ProcessInfo.processInfo.environment["KVM_CONSOLE_URL"]
    ?? "http://127.0.0.1:4110/#kvm"

final class KvmConsoleLauncher: NSObject, NSApplicationDelegate {

    // KVM 控制台前端的静态发布目录（launchd 前端 job 用 http.server 原样服务它）。
    // 只读用途：取 index-*.js 内容 hash 作 cache-busting 值。
    private var frontendDistRoot: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(
            "Library/Application Support/ControllerAgent/macos-kvm/frontend-dist")
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        openConsole()
    }

    private func installedConsoleAppURL() -> URL? {
        let candidates = [
            "Applications/Chrome Apps.localized",
            "Applications/Chrome Apps",
        ]
        let home = FileManager.default.homeDirectoryForCurrentUser
        for relative in candidates {
            let directory = home.appendingPathComponent(relative)
            guard let entries = try? FileManager.default.contentsOfDirectory(
                at: directory,
                includingPropertiesForKeys: nil
            ) else { continue }
            // 认名字里含 KVM 的那个 PWA；用户自定义命名时也能命中
            if let match = entries.first(where: {
                $0.pathExtension == "app" && $0.deletingPathExtension()
                    .lastPathComponent.uppercased().contains("KVM")
            }) {
                return match
            }
        }
        return nil
    }

    private func openConsole() {
        if let pwa = installedConsoleAppURL() {
            let config = NSWorkspace.OpenConfiguration()
            config.activates = true
            NSWorkspace.shared.openApplication(at: pwa, configuration: config) { [weak self] _, error in
                DispatchQueue.main.async {
                    if let error {
                        self?.openConsoleInDefaultBrowser(note: error.localizedDescription)
                    }
                    NSApp.terminate(nil)
                }
            }
            return
        }
        openConsoleInDefaultBrowser(note: nil)
        NSApp.terminate(nil)
    }

    private func openConsoleInDefaultBrowser(note: String?) {
        guard let url = consoleFallbackURL() else { return }
        NSWorkspace.shared.open(url)
        if let note {
            print("[kvm-console] PWA 启动失败，已用默认浏览器打开：\(note)")
        }
    }

    // 真机取证（2026-07-30 14:50）：部署新前端后，Chrome 磁盘缓存里的旧 index.html
    // 会去请求已被删除的 chunk（index-BTw34MJX.js 404）→ 白屏。同一 URL 只变
    // #fragment 的导航不会重取文档，所以降级路径带上随部署变化的 query 强制真导航
    // ——query 必须排在 # 之前，URLComponents 保证这一点。
    private func consoleFallbackURL() -> URL? {
        guard var components = URLComponents(string: kKvmConsoleURL) else {
            return URL(string: kKvmConsoleURL)
        }
        var query = components.queryItems ?? []
        query.append(URLQueryItem(name: "b", value: deployedBundleStamp()))
        components.queryItems = query
        return components.url ?? URL(string: kKvmConsoleURL)
    }

    // cache-busting 值用 dist 里 index-*.js 的内容 hash：部署内稳定（同版本仍吃
    // HTTP 缓存），跨部署必变（旧 index.html 必被换掉）。读不到目录（自定义部署、
    // 首次未安装）就退回启动时间戳——宁可多刷新一次，也不能再出白屏。
    private func deployedBundleStamp() -> String {
        let assets = frontendDistRoot.appendingPathComponent("assets")
        if let entries = try? FileManager.default.contentsOfDirectory(
            at: assets, includingPropertiesForKeys: nil),
           let bundle = entries.first(where: {
               $0.pathExtension == "js" && $0.lastPathComponent.hasPrefix("index-")
           }) {
            return bundle.deletingPathExtension().lastPathComponent
                .replacingOccurrences(of: "index-", with: "")
        }
        return String(Int(Date().timeIntervalSince1970))
    }
}

@main
enum KvmConsoleMain {
    static func main() {
        let app = NSApplication.shared
        let launcher = KvmConsoleLauncher()
        app.delegate = launcher
        app.setActivationPolicy(.accessory)   // 薄启动器：打开网页即退出，不占 Dock
        app.run()
    }
}
