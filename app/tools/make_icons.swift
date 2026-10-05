// Draws the Middy logo (letter M inside a ring, same as .logo-m in styles.css) with CoreGraphics and writes:
//   build/icons/app.iconset/icon_<n>x<n>[@2x].png  (16..1024, macOS-style rounded square, dark panel background)
//   renderer/icons/trayTemplate.png + trayTemplate@2x.png (18/36 px, black on transparent: macOS template image)
// Run: swift tools/make_icons.swift   (no dependency, no install)
import AppKit
import CoreText

func png(_ image: NSImage, _ path: String) {
    let rep = image.representations.first as! NSBitmapImageRep
    try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: path))
}

func logo(size: CGFloat, template: Bool) -> NSImage {
    // draw into a bitmap of EXACT pixel size (lockFocus on a Retina screen would render every file at 2x)
    let px = Int(size)
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    rep.size = NSSize(width: size, height: size)
    let img = NSImage(size: NSSize(width: size, height: size))
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let ctx = NSGraphicsContext.current!.cgContext
    ctx.setAllowsAntialiasing(true); ctx.setShouldAntialias(true)
    let s = size
    if !template {
        // macOS app icon: rounded square (radius ~22.4 % of side), dark panel #292b31, accent ring #C7DA35
        let inset = s * 0.10                               // Apple's icon grid leaves a margin around the tile
        let tile = CGRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
        let path = CGPath(roundedRect: tile, cornerWidth: tile.width * 0.224, cornerHeight: tile.height * 0.224, transform: nil)
        ctx.addPath(path); ctx.setFillColor(CGColor(red: 0x29/255.0, green: 0x2b/255.0, blue: 0x31/255.0, alpha: 1)); ctx.fillPath()
        let ringW = s * 0.045
        let ring = tile.insetBy(dx: tile.width * 0.20, dy: tile.height * 0.20)
        ctx.setStrokeColor(CGColor(red: 0xC7/255.0, green: 0xDA/255.0, blue: 0x35/255.0, alpha: 1)); ctx.setLineWidth(ringW)
        ctx.strokeEllipse(in: ring.insetBy(dx: ringW / 2, dy: ringW / 2))
    }
    if !template { }
    // letter M: heavy system font. Centered by the glyph's REAL ink box (CoreText image bounds), not by the baseline —
    // the baseline-centred version sat low and touched the ring (anh 22:36). Letter height = 0.5 x ring inner diameter.
    let color: NSColor = template ? .black : .white
    let ringInner: CGFloat
    if template {
        let ringW = s * 0.09
        ctx.setStrokeColor(CGColor.black); ctx.setLineWidth(ringW)
        let r = CGRect(x: 0, y: 0, width: s, height: s).insetBy(dx: ringW / 2 + s * 0.02, dy: ringW / 2 + s * 0.02)
        ctx.strokeEllipse(in: r)
        ringInner = r.width - ringW
    } else {
        let inset = s * 0.10, tile = CGRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
        let ringW = s * 0.045
        let ring = tile.insetBy(dx: tile.width * 0.20, dy: tile.height * 0.20).insetBy(dx: ringW / 2, dy: ringW / 2)
        ringInner = ring.width - ringW
    }
    let target = ringInner * (template ? 0.56 : 0.50)                   // ink height of the M (~10 % smaller than before)
    var fontSize = target
    var font = NSFont.systemFont(ofSize: fontSize, weight: .heavy)
    func inkBounds(_ f: NSFont) -> CGRect {
        let line = CTLineCreateWithAttributedString(NSAttributedString(string: "M", attributes: [.font: f, .foregroundColor: color]))
        return CTLineGetImageBounds(line, ctx)
    }
    var ink = inkBounds(font)
    fontSize = fontSize * target / ink.height                           // scale so the ink height == target
    font = NSFont.systemFont(ofSize: fontSize, weight: .heavy); ink = inkBounds(font)
    let line = CTLineCreateWithAttributedString(NSAttributedString(string: "M", attributes: [.font: font, .foregroundColor: color]))
    // place the ink box centre on the ring centre (s/2, s/2)
    ctx.saveGState()
    ctx.textPosition = CGPoint(x: s / 2 - ink.midX, y: s / 2 - ink.midY)
    CTLineDraw(line, ctx)
    ctx.restoreGState()
    NSGraphicsContext.restoreGraphicsState()
    img.addRepresentation(rep)
    return img
}

let root = FileManager.default.currentDirectoryPath
let iconset = root + "/build/icons/app.iconset"
try? FileManager.default.removeItem(atPath: iconset)
try! FileManager.default.createDirectory(atPath: iconset, withIntermediateDirectories: true)
for n in [16, 32, 128, 256, 512] {
    png(logo(size: CGFloat(n), template: false), "\(iconset)/icon_\(n)x\(n).png")
    png(logo(size: CGFloat(n * 2), template: false), "\(iconset)/icon_\(n)x\(n)@2x.png")
}
png(logo(size: 1024, template: false), root + "/build/icons/icon_1024.png")
png(logo(size: 18, template: true), root + "/renderer/icons/trayTemplate.png")
png(logo(size: 36, template: true), root + "/renderer/icons/trayTemplate@2x.png")
print("icons written")
