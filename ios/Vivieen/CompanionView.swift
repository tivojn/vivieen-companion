import SwiftUI

struct CompanionView: View {
    @AppStorage("serverAddress") private var serverAddress = ""
    @AppStorage("pairingToken") private var pairingToken = ""
    @State private var showUnpair = false

    var body: some View {
        ZStack(alignment: .topTrailing) {
            CompanionWebView(address: serverAddress, token: pairingToken)
                .ignoresSafeArea()
            Button {
                showUnpair = true
            } label: {
                Image(systemName: "gearshape")
                    .font(.system(size: 15))
                    .foregroundStyle(.secondary.opacity(0.55))
                    .padding(10)
            }
            .confirmationDialog("Vivieen", isPresented: $showUnpair) {
                Button("Unpair from this Mac", role: .destructive) {
                    serverAddress = ""
                    pairingToken = ""
                }
                Button("Cancel", role: .cancel) {}
            }
        }
        .background(Color(.systemBackground))
        .persistentSystemOverlays(.hidden)
    }
}
