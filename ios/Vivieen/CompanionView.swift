import SwiftUI

struct CompanionView: View {
    @AppStorage("serverAddress") private var serverAddress = ""
    @AppStorage("pairingToken") private var pairingToken = ""
    @State private var showUnpair = false
    @State private var homeKey = 0
    /// The page carries its own gear, which opens Settings. Showing the
    /// app's gear at the same time put TWO gears in one corner, both
    /// looking like "settings" (owner, 2026-08-03). This one is a rescue
    /// hatch, so it appears only when the page is NOT up - which is
    /// exactly when unpairing or reloading is the thing you need.
    @State private var pageLive = false

    var body: some View {
        ZStack(alignment: .topTrailing) {
            // Keep the page below the camera cutout - her head lives at the
            // top edge and must never hide under the island. The bottom is
            // hers: the chat bar sits flush with the home indicator.
            CompanionWebView(address: serverAddress, token: pairingToken,
                             pageLive: $pageLive)
                .id(homeKey)
                .ignoresSafeArea(edges: .bottom)
            if !pageLive {
                // A black screen says nothing. Off Wi-Fi her page cannot
                // load at all - the app fetches its whole self from the
                // Mac - so say that plainly and offer the way back
                // (owner, on 5G, 2026-08-03).
                VStack(spacing: 14) {
                    Image(systemName: "wifi.exclamationmark")
                        .font(.system(size: 34, weight: .light))
                        .foregroundStyle(.secondary)
                    Text("Can't reach your Mac")
                        .font(.headline)
                    // NOT "needs the same Wi-Fi" any more: the relay
                    // carries her anywhere, and this card told the owner
                    // to go home while she was reachable the whole time
                    // (2026-08-05). What is actually true is that she is
                    // awake somewhere and this phone found neither road.
                    Text("She could not be found on this Wi-Fi or through "
                         + "the relay. Check that Vivieen is awake on your "
                         + "Mac — away from home she needs it not asleep.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 34)
                    Button("Try again") { homeKey += 1 }
                        .buttonStyle(.borderedProminent)
                        .padding(.top, 4)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color.black)

                Button {
                    showUnpair = true
                } label: {
                    Image(systemName: "gearshape")
                        .font(.system(size: 15))
                        .foregroundStyle(.secondary.opacity(0.55))
                        .padding(10)
                }
                .confirmationDialog("Vivieen", isPresented: $showUnpair) {
                    Button("Reload") { homeKey += 1 }
                    Button("Unpair from this Mac", role: .destructive) {
                        serverAddress = ""
                        pairingToken = ""
                    }
                    Button("Cancel", role: .cancel) {}
                }
            }
        }
        .background(Color.black.ignoresSafeArea())
        .persistentSystemOverlays(.hidden)
    }
}
