import AVKit
import CoreMedia
import UIKit
import WebKit

/// System Picture-in-Picture for a canvas avatar: the renderer pumps small
/// JPEG frames over the script bridge, this drives them into an
/// AVSampleBufferDisplayLayer behind AVPictureInPictureController - the
/// real OS floating window: draggable, pinch-resizable, slides into the
/// screen edge to hide, survives the app going background.
final class PipDriver: NSObject {
    private let layer = AVSampleBufferDisplayLayer()
    private let host = UIView(frame: CGRect(x: 0, y: 0, width: 9, height: 16))
    private var pip: AVPictureInPictureController?
    private var pendingStart = false
    private var hasFrame = false
    weak var webView: WKWebView?

    func attach(to view: UIView) {
        guard pip == nil, AVPictureInPictureController.isPictureInPictureSupported() else { return }
        host.alpha = 0.0001
        host.isUserInteractionEnabled = false
        layer.frame = host.bounds
        layer.videoGravity = .resizeAspect
        host.layer.addSublayer(layer)
        view.addSubview(host)
        let source = AVPictureInPictureController.ContentSource(
            sampleBufferDisplayLayer: layer, playbackDelegate: self)
        let controller = AVPictureInPictureController(contentSource: source)
        controller.delegate = self
        pip = controller
    }

    func start() {
        guard let pip else {
            notifyUnsupported()
            return
        }
        if hasFrame {
            pip.startPictureInPicture()
        } else {
            pendingStart = true
        }
    }

    func stop() {
        pip?.stopPictureInPicture()
    }

    func enqueue(dataURL: String) {
        guard let comma = dataURL.range(of: ","),
              let data = Data(base64Encoded: String(dataURL[comma.upperBound...])),
              let image = UIImage(data: data)?.cgImage,
              let buffer = PipDriver.pixelBuffer(from: image) else { return }
        var format: CMVideoFormatDescription?
        CMVideoFormatDescriptionCreateForImageBuffer(
            allocator: nil, imageBuffer: buffer, formatDescriptionOut: &format)
        guard let format else { return }
        var timing = CMSampleTimingInfo(
            duration: CMTime(value: 1, timescale: 8),
            presentationTimeStamp: CMClockGetTime(CMClockGetHostTimeClock()),
            decodeTimeStamp: .invalid)
        var sample: CMSampleBuffer?
        CMSampleBufferCreateReadyWithImageBuffer(
            allocator: nil, imageBuffer: buffer, formatDescription: format,
            sampleTiming: &timing, sampleBufferOut: &sample)
        guard let sample else { return }
        if layer.status == .failed { layer.flush() }
        layer.enqueue(sample)
        hasFrame = true
        if pendingStart {
            pendingStart = false
            pip?.startPictureInPicture()
        }
    }

    private func notifyUnsupported() {
        webView?.evaluateJavaScript(
            "window.__pipUnsupported&&window.__pipUnsupported()", completionHandler: nil)
    }

    private static func pixelBuffer(from image: CGImage) -> CVPixelBuffer? {
        let width = image.width, height = image.height
        var pixelBuffer: CVPixelBuffer?
        CVPixelBufferCreate(
            nil, width, height, kCVPixelFormatType_32BGRA,
            [kCVPixelBufferCGImageCompatibilityKey: true,
             kCVPixelBufferCGBitmapContextCompatibilityKey: true] as CFDictionary,
            &pixelBuffer)
        guard let buffer = pixelBuffer else { return nil }
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        guard let context = CGContext(
            data: CVPixelBufferGetBaseAddress(buffer),
            width: width, height: height, bitsPerComponent: 8,
            bytesPerRow: CVPixelBufferGetBytesPerRow(buffer),
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue
                | CGBitmapInfo.byteOrder32Little.rawValue) else { return nil }
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        return buffer
    }
}

extension PipDriver: AVPictureInPictureControllerDelegate {
    func pictureInPictureControllerDidStopPictureInPicture(
        _ controller: AVPictureInPictureController) {
        webView?.evaluateJavaScript(
            "window.__pipStopped&&window.__pipStopped()", completionHandler: nil)
    }
}

extension PipDriver: AVPictureInPictureSampleBufferPlaybackDelegate {
    func pictureInPictureController(
        _ controller: AVPictureInPictureController, setPlaying playing: Bool) {}

    func pictureInPictureControllerTimeRangeForPlayback(
        _ controller: AVPictureInPictureController) -> CMTimeRange {
        CMTimeRange(start: .negativeInfinity, duration: .positiveInfinity)
    }

    func pictureInPictureControllerIsPlaybackPaused(
        _ controller: AVPictureInPictureController) -> Bool { false }

    func pictureInPictureController(
        _ controller: AVPictureInPictureController,
        didTransitionToRenderSize newRenderSize: CMVideoDimensions) {}

    func pictureInPictureController(
        _ controller: AVPictureInPictureController,
        skipByInterval skipInterval: CMTime,
        completion completionHandler: @escaping () -> Void) {
        completionHandler()
    }
}
