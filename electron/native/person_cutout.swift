import AppKit
import CoreImage
import CoreVideo
import Foundation
import Vision

struct CutoutFailure: Error, LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

func loadImage(_ path: String) throws -> CGImage {
    guard let image = NSImage(contentsOfFile: path) else {
        throw CutoutFailure(message: "Unable to open input image")
    }
    var proposed = CGRect(origin: .zero, size: image.size)
    guard let cgImage = image.cgImage(forProposedRect: &proposed, context: nil, hints: nil) else {
        throw CutoutFailure(message: "Unable to decode input image")
    }
    return cgImage
}

func writeCutout(inputPath: String, outputPath: String) throws {
    let cgImage = try loadImage(inputPath)
    let request = VNGeneratePersonSegmentationRequest()
    request.qualityLevel = .accurate
    request.outputPixelFormat = kCVPixelFormatType_OneComponent8
    let handler = VNImageRequestHandler(cgImage: cgImage, orientation: .up)
    try handler.perform([request])
    guard let observation = request.results?.first else {
        throw CutoutFailure(message: "No person was detected")
    }

    let source = CIImage(cgImage: cgImage)
    let rawMask = CIImage(cvPixelBuffer: observation.pixelBuffer)
    let scaleX = source.extent.width / rawMask.extent.width
    let scaleY = source.extent.height / rawMask.extent.height
    let scaledMask = rawMask.transformed(by: CGAffineTransform(scaleX: scaleX, y: scaleY))
    let mask = scaledMask
        .clampedToExtent()
        .applyingFilter("CIMorphologyMaximum", parameters: ["inputRadius": 0.75])
        .applyingFilter("CIGaussianBlur", parameters: [kCIInputRadiusKey: 0.85])
        .cropped(to: source.extent)
    let clear = CIImage(color: .clear).cropped(to: source.extent)
    guard let composited = CIFilter(
        name: "CIBlendWithMask",
        parameters: [
            kCIInputImageKey: source,
            kCIInputBackgroundImageKey: clear,
            kCIInputMaskImageKey: mask,
        ]
    )?.outputImage?.cropped(to: source.extent) else {
        throw CutoutFailure(message: "Unable to compose the person mask")
    }

    let outputURL = URL(fileURLWithPath: outputPath)
    try FileManager.default.createDirectory(
        at: outputURL.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    let context = CIContext(options: [.cacheIntermediates: false])
    let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) ?? CGColorSpaceCreateDeviceRGB()
    try context.writePNGRepresentation(
        of: composited,
        to: outputURL,
        format: .RGBA8,
        colorSpace: colorSpace
    )
}

let arguments = CommandLine.arguments
if arguments.count != 3 {
    FileHandle.standardError.write(Data("usage: person-cutout INPUT OUTPUT\n".utf8))
    exit(2)
}

do {
    try writeCutout(inputPath: arguments[1], outputPath: arguments[2])
    let payload = ["ok": true, "output": arguments[2]] as [String: Any]
    let data = try JSONSerialization.data(withJSONObject: payload)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
} catch {
    FileHandle.standardError.write(Data("person-cutout: \(error.localizedDescription)\n".utf8))
    exit(1)
}
