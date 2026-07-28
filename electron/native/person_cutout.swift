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

let poseJoints: [(String, VNHumanBodyPoseObservation.JointName)] = [
    ("nose", .nose),
    ("neck", .neck),
    ("left_shoulder", .leftShoulder),
    ("left_elbow", .leftElbow),
    ("left_wrist", .leftWrist),
    ("right_shoulder", .rightShoulder),
    ("right_elbow", .rightElbow),
    ("right_wrist", .rightWrist),
    ("root", .root),
    ("left_hip", .leftHip),
    ("left_knee", .leftKnee),
    ("left_ankle", .leftAnkle),
    ("right_hip", .rightHip),
    ("right_knee", .rightKnee),
    ("right_ankle", .rightAnkle),
]

func writePose(
    _ observation: VNHumanBodyPoseObservation?,
    width: Int,
    height: Int,
    outputPath: String
) throws {
    var joints: [String: [String: Double]] = [:]
    if let observation = observation {
        for (key, name) in poseJoints {
            guard let point = try? observation.recognizedPoint(name),
                  point.confidence > 0.05 else {
                continue
            }
            joints[key] = [
                "x": Double(point.location.x) * Double(width),
                "y": (1.0 - Double(point.location.y)) * Double(height),
                "confidence": Double(point.confidence),
            ]
        }
    }
    let payload: [String: Any] = [
        "width": width,
        "height": height,
        "joints": joints,
    ]
    let outputURL = URL(fileURLWithPath: outputPath)
    try FileManager.default.createDirectory(
        at: outputURL.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    let data = try JSONSerialization.data(
        withJSONObject: payload,
        options: [.prettyPrinted, .sortedKeys]
    )
    try data.write(to: outputURL, options: .atomic)
}

func writeCutout(inputPath: String, outputPath: String, poseOutputPath: String?) throws {
    let cgImage = try loadImage(inputPath)
    let request = VNGeneratePersonSegmentationRequest()
    request.qualityLevel = .accurate
    request.outputPixelFormat = kCVPixelFormatType_OneComponent8
    let poseRequest = poseOutputPath == nil ? nil : VNDetectHumanBodyPoseRequest()
    let handler = VNImageRequestHandler(cgImage: cgImage, orientation: .up)
    var requests: [VNRequest] = [request]
    if let poseRequest = poseRequest {
        requests.append(poseRequest)
    }
    try handler.perform(requests)
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
    if let poseOutputPath = poseOutputPath {
        try writePose(
            poseRequest?.results?.first,
            width: cgImage.width,
            height: cgImage.height,
            outputPath: poseOutputPath
        )
    }
}

let arguments = CommandLine.arguments
if arguments.count != 3 && arguments.count != 4 {
    FileHandle.standardError.write(
        Data("usage: person-cutout INPUT OUTPUT [POSE_JSON]\n".utf8)
    )
    exit(2)
}

do {
    let poseOutputPath = arguments.count == 4 ? arguments[3] : nil
    try writeCutout(
        inputPath: arguments[1],
        outputPath: arguments[2],
        poseOutputPath: poseOutputPath
    )
    var payload = [
        "ok": true,
        "output": arguments[2],
    ] as [String: Any]
    if let poseOutputPath = poseOutputPath {
        payload["pose"] = poseOutputPath
    }
    let data = try JSONSerialization.data(withJSONObject: payload)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
} catch {
    FileHandle.standardError.write(Data("person-cutout: \(error.localizedDescription)\n".utf8))
    exit(1)
}
