import SwiftUI
import AVFoundation

/// Pocket Mirror: the same Vivieen renderer the Mac uses, served by the
/// Mac's companion app and shown full-screen on the phone. Native code
/// handles only what WebKit cannot: pairing, cookies, and permissions.
@main
struct VivieenApp: App {
    init() {
        // PLAYBACK, not playAndRecord. A record-capable session routes
        // WebKit's output at the earpiece and wrestles WebKit for the
        // session - her voice ran, and nobody heard it (owner, real
        // iPhone 2026-08-03). MicDriver escalates to .playAndRecord only
        // while the microphone is actually open, and drops back after.
        AudioSession.playbackOnly()
    }

    @AppStorage("serverAddress") private var serverAddress = ""
    @AppStorage("pairingToken") private var pairingToken = ""

    var body: some Scene {
        WindowGroup {
            if serverAddress.isEmpty || pairingToken.isEmpty {
                PairingView()
            } else {
                CompanionView()
            }
        }
    }
}
