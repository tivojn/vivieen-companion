import SwiftUI
import UIKit

/// One-time pairing: the Mac menu ("Pair iPhone…") shows an address and a
/// code, and its Copy button puts them on the clipboard as two lines —
/// Paste here fills both fields at once via Universal Clipboard.
struct PairingView: View {
    @AppStorage("serverAddress") private var serverAddress = ""
    @AppStorage("pairingToken") private var pairingToken = ""
    @State private var address = ""
    @State private var code = ""
    @State private var checking = false
    @State private var error = ""

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("http://192.168.1.20:8777", text: $address)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Pairing code", text: $code)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Your Mac")
                } footer: {
                    Text("On the Mac: right-click Vivieen → iPhone on This Network, then Pair iPhone. Both devices join the same Wi-Fi.")
                }
                if !error.isEmpty {
                    Section { Text(error).foregroundStyle(.red) }
                }
                Section {
                    Button(action: pasteBoth) { Text("Paste address and code") }
                    Button(action: connect) {
                        if checking { ProgressView() } else { Text("Connect") }
                    }
                    .disabled(address.isEmpty || code.isEmpty || checking)
                }
            }
            .navigationTitle("Meet Vivieen")
        }
    }

    private func pasteBoth() {
        let lines = (UIPasteboard.general.string ?? "")
            .split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }
        if lines.count >= 2 {
            address = lines[0]
            code = lines[1]
        } else if lines.count == 1 {
            if lines[0].hasPrefix("http") { address = lines[0] } else { code = lines[0] }
        }
    }

    private func connect() {
        guard var components = URLComponents(string: address.trimmingCharacters(in: .whitespaces)) else {
            error = "That address doesn't look right."
            return
        }
        if components.scheme == nil { components.scheme = "http" }
        guard let base = components.url else {
            error = "That address doesn't look right."
            return
        }
        checking = true
        error = ""
        var request = URLRequest(url: base.appendingPathComponent("health"), timeoutInterval: 6)
        request.setValue(code.trimmingCharacters(in: .whitespaces), forHTTPHeaderField: "X-Vivieen-Token")
        URLSession.shared.dataTask(with: request) { _, response, failure in
            DispatchQueue.main.async {
                checking = false
                if let failure {
                    error = "Couldn't reach the Mac: \(failure.localizedDescription)"
                    return
                }
                guard let http = response as? HTTPURLResponse else {
                    error = "No answer from that address."
                    return
                }
                switch http.statusCode {
                case 200:
                    serverAddress = base.absoluteString
                    pairingToken = code.trimmingCharacters(in: .whitespaces)
                case 403:
                    error = "The Mac answered, but the pairing code is wrong."
                default:
                    error = "The Mac answered with status \(http.statusCode)."
                }
            }
        }.resume()
    }
}
