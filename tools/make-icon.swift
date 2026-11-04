// A vector-drawn app icon, rasterized at every native Dock/Finder size.
import Cocoa

let destination = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
                                      bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                      isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
        let transform = NSAffineTransform()
        transform.scale(by: CGFloat(pixels) / 1024)
        transform.concat()
        let tile = NSBezierPath(roundedRect: NSRect(x: 70, y: 70, width: 884, height: 884), xRadius: 198, yRadius: 198)
        NSGradient(starting: NSColor(red: 0.10, green: 0.33, blue: 0.38, alpha: 1),
                   ending: NSColor(red: 0.035, green: 0.10, blue: 0.17, alpha: 1))!.draw(in: tile, angle: -65)
        let screen = NSBezierPath(roundedRect: NSRect(x: 198, y: 290, width: 628, height: 456), xRadius: 76, yRadius: 76)
        NSColor.white.withAlphaComponent(0.92).setStroke()
        screen.lineWidth = 32
        screen.stroke()
        let play = NSBezierPath()
        play.move(to: NSPoint(x: 454, y: 390))
        play.line(to: NSPoint(x: 454, y: 646))
        play.line(to: NSPoint(x: 644, y: 518))
        play.close()
        NSColor.white.setFill()
        play.fill()
        for (x, color) in [(CGFloat(330), NSColor(red: 0.27, green: 0.88, blue: 0.77, alpha: 1)),
                           (CGFloat(530), NSColor(red: 1, green: 0.70, blue: 0.39, alpha: 1))] {
            color.setFill()
            NSBezierPath(roundedRect: NSRect(x: x, y: 198, width: 164, height: 32), xRadius: 16, yRadius: 16).fill()
        }
        NSGraphicsContext.restoreGraphicsState()
        let suffix = scale == 2 ? "@2x" : ""
        let data = bitmap.representation(using: .png, properties: [:])!
        try data.write(to: destination.appendingPathComponent("icon_\(size)x\(size)\(suffix).png"))
    }
}
