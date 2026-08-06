import ARKit
import Foundation
import UIKit
import WebKit

/// Mirror mode: the TrueDepth camera reads the OWNER's face and her
/// avatar wears it - jaw, lips, smile - at 30 frames a second, entirely
/// on-device. Started only from the page's mirror button (the camera
/// indicator must mean a choice), stopped the moment the app leaves the
/// foreground, because iOS takes the camera there anyway.
final class FaceTap: NSObject, ARSessionDelegate {
    static let shared = FaceTap()
    weak var webView: WKWebView?
    private var session: ARSession?
    private var lastPush = Date.distantPast

    static var supported: Bool { ARFaceTrackingConfiguration.isSupported }

    override private init() {
        super.init()
        NotificationCenter.default.addObserver(
            forName: UIApplication.didEnterBackgroundNotification,
            object: nil, queue: .main) { [weak self] _ in
            self?.stop()
        }
    }

    func start() {
        guard Self.supported else {
            DispatchQueue.main.async { [weak self] in
                self?.webView?.evaluateJavaScript(
                    "window.setCaption&&setCaption("
                    + "'(this phone has no face-tracking camera)')",
                    completionHandler: nil)
            }
            return
        }
        guard session == nil else { return }
        let live = ARSession()
        live.delegate = self
        live.run(ARFaceTrackingConfiguration())
        session = live
        NSLog("[viv-face] mirror running")
    }

    func stop() {
        guard session != nil else { return }
        session?.pause()
        session = nil
        NSLog("[viv-face] mirror paused")
    }

    func session(_ session: ARSession, didUpdate anchors: [ARAnchor]) {
        guard let face = anchors.compactMap({ $0 as? ARFaceAnchor }).last,
              face.isTracked else { return }
        let now = Date()
        guard now.timeIntervalSince(lastPush) >= 0.033 else { return }
        lastPush = now
        let shapes = face.blendShapes
        func read(_ key: ARFaceAnchor.BlendShapeLocation) -> Double {
            Double(truncating: shapes[key] ?? 0)
        }
        let json = "{\"jaw\":\(read(.jawOpen)),"
            + "\"pucker\":\(read(.mouthPucker)),"
            + "\"funnel\":\(read(.mouthFunnel)),"
            + "\"smileL\":\(read(.mouthSmileLeft)),"
            + "\"smileR\":\(read(.mouthSmileRight)),"
            + "\"blinkL\":\(read(.eyeBlinkLeft)),"
            + "\"blinkR\":\(read(.eyeBlinkRight))}"
        DispatchQueue.main.async { [weak self] in
            self?.webView?.evaluateJavaScript(
                "window.__vivFace&&__vivFace('\(json)')",
                completionHandler: nil)
        }
    }
}
