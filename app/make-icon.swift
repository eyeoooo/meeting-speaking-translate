// make-icon.swift —— 程序化生成 App 图标（1024×1024 PNG 母图，无外部素材）。
//
// 为什么程序化：图标随仓库走，任何构建机（含 MACMINI）都能重现同一视觉，
// 不引入设计资产管线；icns 因此是纯构建产物（app/build/，gitignore），
// 改图标 = 改这份代码，可 review、可 diff。
// 为什么 @main 而不是脚本顶层语句：仓库 Swift 门禁统一跑
// -parse-as-library -typecheck，顶层语句过不了；build-apps.sh 先编译再执行。
//
// 视觉规范：跟随 macOS Big Sur+ 图标语言——1024 画布中央 824pt 圆角方块，
// 四周留透明边距。会议助手 = 深蓝渐变底 + 耳机 + 字幕气泡（两行短横线、
// 两种颜色，示意原文/译文双流）；KVM 控制台 = 同底色 + 显示器 + 终端提示符，
// 同底色保持家族感，主体不同保证菜单栏/Dock 里一眼可分。
//
// 用法：make-icon <输出.png> <meeting|console>

import AppKit

enum IconVariant: String {
    case meeting
    case console
}

@main
struct MakeIcon {
    static func main() {
        let args = CommandLine.arguments
        guard args.count == 3, let variant = IconVariant(rawValue: args[2]) else {
            FileHandle.standardError.write(Data("用法: make-icon <输出.png> <meeting|console>\n".utf8))
            exit(2)
        }
        guard let ctx = CGContext(
            data: nil, width: 1024, height: 1024,
            bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpace(name: CGColorSpace.sRGB)!,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else {
            FileHandle.standardError.write(Data("无法创建绘图上下文\n".utf8))
            exit(1)
        }
        drawBackground(ctx)
        switch variant {
        case .meeting: drawMeeting(ctx)
        case .console: drawConsole(ctx)
        }
        guard let image = ctx.makeImage(),
              let png = NSBitmapImageRep(cgImage: image)
                  .representation(using: .png, properties: [:])
        else {
            FileHandle.standardError.write(Data("无法编码 PNG\n".utf8))
            exit(1)
        }
        do {
            try png.write(to: URL(fileURLWithPath: args[1]))
        } catch {
            FileHandle.standardError.write(Data("写出失败：\(error.localizedDescription)\n".utf8))
            exit(1)
        }
    }

    // MARK: - 调色板（两 App 共用，家族感的来源）

    static func color(_ hex: UInt32, alpha: CGFloat = 1) -> CGColor {
        CGColor(
            srgbRed: CGFloat((hex >> 16) & 0xFF) / 255,
            green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255,
            alpha: alpha)
    }

    static let light = color(0xEAEEF7)        // 主体线条：柔白，深底上不刺眼
    static let accentTop = color(0x5B8DFF)    // 主色：冷蓝渐变，呼应"远程/技术"
    static let accentBottom = color(0x2E5BE8)
    static let cyan = color(0x45C4CE)         // 辅色：只点缀一处，克制

    // MARK: - 底座

    /// 深蓝渐变圆角方块 + 顶部一点冷光：纯平死黑在浅色 Dock 里会糊成一团。
    static func drawBackground(_ ctx: CGContext) {
        let rect = CGRect(x: 100, y: 100, width: 824, height: 824)
        let path = CGPath(roundedRect: rect, cornerWidth: 185, cornerHeight: 185, transform: nil)
        ctx.saveGState()
        ctx.addPath(path)
        ctx.clip()
        fillVerticalGradient(ctx, rect: rect, top: color(0x2B3450), bottom: color(0x0F1220))
        let glow = CGGradient(
            colorsSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
            colors: [color(0xFFFFFF, alpha: 0.10), color(0xFFFFFF, alpha: 0)] as CFArray,
            locations: [0, 1])!
        ctx.drawRadialGradient(
            glow,
            startCenter: CGPoint(x: 512, y: 980), startRadius: 0,
            endCenter: CGPoint(x: 512, y: 980), endRadius: 700, options: [])
        ctx.restoreGState()
    }

    static func fillVerticalGradient(_ ctx: CGContext, rect: CGRect, top: CGColor, bottom: CGColor) {
        let gradient = CGGradient(
            colorsSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
            colors: [top, bottom] as CFArray, locations: [0, 1])!
        ctx.drawLinearGradient(
            gradient,
            start: CGPoint(x: rect.midX, y: rect.maxY),
            end: CGPoint(x: rect.midX, y: rect.minY), options: [])
    }

    static func fillPathVerticalGradient(_ ctx: CGContext, path: CGPath, top: CGColor, bottom: CGColor) {
        ctx.saveGState()
        ctx.addPath(path)
        ctx.clip()
        fillVerticalGradient(ctx, rect: path.boundingBox, top: top, bottom: bottom)
        ctx.restoreGState()
    }

    // MARK: - 会议助手：耳机 + 双语字幕气泡

    static func drawMeeting(_ ctx: CGContext) {
        // 头带：上半圆弧，两端略过水平线向下延伸，圆头端点自然接进耳罩
        ctx.setStrokeColor(light)
        ctx.setLineWidth(58)
        ctx.setLineCap(.round)
        ctx.addArc(
            center: CGPoint(x: 512, y: 520), radius: 238,
            startAngle: -0.08 * .pi, endAngle: 1.08 * .pi, clockwise: false)
        ctx.strokePath()
        // 耳罩：竖长圆角块，主色渐变——整图唯一的大面积主色
        for dx in [-238.0, 238.0] {
            let cup = CGRect(x: 512 + dx - 60, y: 345, width: 120, height: 170)
            let path = CGPath(roundedRect: cup, cornerWidth: 54, cornerHeight: 54, transform: nil)
            fillPathVerticalGradient(ctx, path: path, top: accentTop, bottom: accentBottom)
        }
        // 字幕气泡：白底 + 朝耳机方向的小尾巴（"字幕来自听到的声音"）
        let bubble = CGMutablePath()
        bubble.addPath(CGPath(
            roundedRect: CGRect(x: 312, y: 140, width: 400, height: 220),
            cornerWidth: 52, cornerHeight: 52, transform: nil))
        bubble.move(to: CGPoint(x: 356, y: 350))
        bubble.addLine(to: CGPoint(x: 430, y: 350))
        bubble.addLine(to: CGPoint(x: 376, y: 424))
        bubble.closeSubpath()
        ctx.setFillColor(color(0xF3F6FC))
        ctx.addPath(bubble)
        ctx.fillPath()
        // 两行短横线：不同长度 + 不同颜色 = 原文/译文两条独立时间轴
        // （与字幕窗口"双泳道"的产品裁定同构）
        ctx.setFillColor(accentBottom)
        ctx.addPath(CGPath(
            roundedRect: CGRect(x: 372, y: 282, width: 280, height: 34),
            cornerWidth: 17, cornerHeight: 17, transform: nil))
        ctx.fillPath()
        ctx.setFillColor(cyan)
        ctx.addPath(CGPath(
            roundedRect: CGRect(x: 372, y: 210, width: 192, height: 34),
            cornerWidth: 17, cornerHeight: 17, transform: nil))
        ctx.fillPath()
    }

    // MARK: - KVM 控制台：显示器 + 终端提示符

    static func drawConsole(_ ctx: CGContext) {
        // 底座与支架
        ctx.setFillColor(light)
        ctx.addPath(CGPath(
            roundedRect: CGRect(x: 392, y: 208, width: 240, height: 44),
            cornerWidth: 22, cornerHeight: 22, transform: nil))
        ctx.fillPath()
        ctx.fill(CGRect(x: 482, y: 244, width: 60, height: 90))
        // 屏幕外框（白）+ 屏内深色渐变：屏比底还深一档，才有"发光屏幕"层次
        ctx.addPath(CGPath(
            roundedRect: CGRect(x: 232, y: 320, width: 560, height: 400),
            cornerWidth: 48, cornerHeight: 48, transform: nil))
        ctx.fillPath()
        let screen = CGPath(
            roundedRect: CGRect(x: 268, y: 356, width: 488, height: 328),
            cornerWidth: 28, cornerHeight: 28, transform: nil)
        fillPathVerticalGradient(ctx, path: screen, top: color(0x35415F), bottom: color(0x141A2C))
        // 终端提示符：chevron + 光标条——控制台的身份标记
        ctx.setStrokeColor(accentTop)
        ctx.setLineWidth(34)
        ctx.setLineCap(.round)
        ctx.setLineJoin(.round)
        ctx.move(to: CGPoint(x: 348, y: 588))
        ctx.addLine(to: CGPoint(x: 424, y: 520))
        ctx.addLine(to: CGPoint(x: 348, y: 452))
        ctx.strokePath()
        ctx.setFillColor(cyan)
        ctx.addPath(CGPath(
            roundedRect: CGRect(x: 462, y: 503, width: 150, height: 34),
            cornerWidth: 17, cornerHeight: 17, transform: nil))
        ctx.fillPath()
    }
}
