import SwiftUI
import AVFoundation

/// Pocket Mirror: the same Vivieen renderer the Mac uses, served by the
/// Mac's companion app and shown full-screen on the phone. Native code
/// handles only what WebKit cannot: pairing, cookies, and permissions.
@main
struct VivieenApp: App {
    init() {
        // Live talk must survive backgrounding: with the audio background
        // mode and a play-and-record session, the call's mic and her voice
        // keep flowing while another app is in front.
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playAndRecord,
                                 options: [.defaultToSpeaker, .allowBluetooth,
                                           .mixWithOthers])
        try? session.setActive(true)
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
