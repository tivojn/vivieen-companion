import SwiftUI

/// Pocket Mirror: the same Vivieen renderer the Mac uses, served by the
/// Mac's companion app and shown full-screen on the phone. Native code
/// handles only what WebKit cannot: pairing, cookies, and permissions.
@main
struct VivieenApp: App {
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
