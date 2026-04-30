import Foundation
import AppKit

// Usage: swift pad_icon.swift <input.png> <output.png> <canvas> <art_max>
// Renders input into a transparent canvas of (canvas x canvas), with the
// artwork's longer side scaled to art_max, centered.

let args = CommandLine.arguments
guard args.count == 5,
      let canvas = Int(args[3]),
      let artMax = Int(args[4]) else {
    FileHandle.standardError.write("usage: pad_icon <in> <out> <canvas> <art_max>\n".data(using: .utf8)!)
    exit(2)
}
let inURL = URL(fileURLWithPath: args[1])
let outURL = URL(fileURLWithPath: args[2])

guard let src = NSImage(contentsOf: inURL),
      let srcRep = NSBitmapImageRep(data: src.tiffRepresentation!) else {
    FileHandle.standardError.write("failed to load source\n".data(using: .utf8)!); exit(1)
}
let sw = CGFloat(srcRep.pixelsWide)
let sh = CGFloat(srcRep.pixelsHigh)
let scale = CGFloat(artMax) / max(sw, sh)
let drawW = sw * scale
let drawH = sh * scale
let originX = (CGFloat(canvas) - drawW) / 2.0
let originY = (CGFloat(canvas) - drawH) / 2.0

guard let outRep = NSBitmapImageRep(
    bitmapDataPlanes: nil,
    pixelsWide: canvas, pixelsHigh: canvas,
    bitsPerSample: 8, samplesPerPixel: 4,
    hasAlpha: true, isPlanar: false,
    colorSpaceName: .deviceRGB,
    bytesPerRow: 0, bitsPerPixel: 32
) else { exit(1) }

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: outRep)
NSGraphicsContext.current?.imageInterpolation = .high
// Clear the canvas to fully transparent.
NSColor.clear.setFill()
NSRect(x: 0, y: 0, width: canvas, height: canvas).fill(using: .copy)
// Draw the source image, scaled and centered.
let dst = NSRect(x: originX, y: originY, width: drawW, height: drawH)
srcRep.draw(in: dst, from: .zero, operation: .sourceOver,
            fraction: 1.0, respectFlipped: false, hints: nil)
NSGraphicsContext.restoreGraphicsState()

guard let png = outRep.representation(using: .png, properties: [:]) else { exit(1) }
try png.write(to: outURL)
print("wrote \(canvas)x\(canvas), art ~\(Int(drawW))x\(Int(drawH))")
