// Renders a tray icon the way the macOS menu bar shows a template image (dark + light bars), for review only — labelled as a
// mock, not a screenshot.  swift tools/menubar_mock.swift <icon@2x.png> <out.png> <label>
import AppKit
let args = CommandLine.arguments
let icon = NSImage(contentsOfFile: args[1])!
let W = 520, H = 60
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: W, pixelsHigh: H, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let ctx = NSGraphicsContext.current!.cgContext
for (i, bg) in [NSColor(white: 0.12, alpha: 1), NSColor(white: 0.93, alpha: 1)].enumerated() {
    let y = CGFloat(i * 30)
    ctx.setFillColor(bg.cgColor); ctx.fill(CGRect(x: 0, y: y, width: CGFloat(W), height: 30))
    // template rendering: the icon's alpha as a mask, tinted like the bar's text colour
    let tint: NSColor = i == 0 ? .white : .black
    let r = CGRect(x: 250, y: y + 6, width: 18, height: 18)
    ctx.saveGState(); ctx.clip(to: r, mask: icon.cgImage(forProposedRect: nil, context: nil, hints: nil)!); ctx.setFillColor(tint.cgColor); ctx.fill(r); ctx.restoreGState()
    let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 13), .foregroundColor: tint]
    NSAttributedString(string: args[3], attributes: attrs).draw(at: NSPoint(x: 12, y: y + 8))
    NSAttributedString(string: "  Sun 27 Sep  22:30", attributes: attrs).draw(at: NSPoint(x: 380, y: y + 8))
}
NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: args[2]))
print("mock written")
