import SwiftUI
import UIKit

/// One-time pairing: the Mac menu ("Pair iPhone…") shows an address and a
/// code, and its Copy button puts them on the clipboard as two lines —
/// Paste here fills both fields at once via Universal Clipboard.
///
/// The address is a CONVENIENCE, not a requirement (owner, stranded on 5G
/// outside with a fresh install, 2026-08-05). The pairing code is also the
/// relay's channel key, and the Mac publishes its own LAN address into the
/// relay's presence record every 45 seconds - so the code alone is enough
/// to find a Mac from anywhere in the world. Direct is tried first because
/// it is instant on the same Wi-Fi; the relay is the fallback, and it is
/// also what the app itself uses once paired.
struct PairingView: View {
    @AppStorage("serverAddress") private var serverAddress = ""
    @AppStorage("pairingToken") private var pairingToken = ""
    @State private var address = ""
    @State private var code = ""
    @State private var checking = false
    @State private var error = ""
    /// Held only while a relay knock is in flight, so the client cannot be
    /// collected out from under its own callback.
    @State private var relay: RelayClient?

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
                    Text("On the Mac: right-click Vivieen → iPhone on This Network, then Pair iPhone. On the same Wi-Fi, both lines connect instantly. Away from home, the code alone is enough — leave the address blank and she is found through the relay.")
                }
                if !error.isEmpty {
                    Section { Text(error).foregroundStyle(.red) }
                }
                Section {
                    Button(action: pasteBoth) { Text("Paste address and code") }
                    Button(action: connect) {
                        if checking { ProgressView() } else { Text("Connect") }
                    }
                    .disabled(code.isEmpty || checking)
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
        let token = code.trimmingCharacters(in: .whitespaces)
        guard !token.isEmpty else {
            error = "The pairing code is the one thing she needs."
            return
        }
        var typed = address.trimmingCharacters(in: .whitespaces)
        // The placeholder is an EXAMPLE address, and it is the easiest
        // thing in the world to type it in believing it is yours. It is
        // nobody's Mac: treat it as blank rather than spending the direct
        // probe on it (owner, 2026-08-05).
        if typed == "http://192.168.1.20:8777" { typed = "" }
        // No address, or one that cannot be parsed: go straight to the
        // relay, which knows where the Mac is.
        guard !typed.isEmpty, var components = URLComponents(string: typed)
        else {
            checking = true
            error = ""
            pairViaRelay(token)
            return
        }
        if components.scheme == nil { components.scheme = "http" }
        guard let base = components.url else {
            checking = true
            error = ""
            pairViaRelay(token)
            return
        }
        checking = true
        error = ""
        var request = URLRequest(url: base.appendingPathComponent("health"), timeoutInterval: 6)
        request.setValue(code.trimmingCharacters(in: .whitespaces), forHTTPHeaderField: "X-Vivieen-Token")
        URLSession.shared.dataTask(with: request) { _, response, failure in
            DispatchQueue.main.async {
                if failure != nil {
                    // That address is unreachable from here - which is the
                    // ordinary case away from home. Ask the relay instead.
                    pairViaRelay(token)
                    return
                }
                guard let http = response as? HTTPURLResponse else {
                    pairViaRelay(token)
                    return
                }
                switch http.statusCode {
                case 200:
                    checking = false
                    serverAddress = base.absoluteString
                    pairingToken = token
                case 403:
                    checking = false
                    error = "The Mac answered, but the pairing code is wrong."
                default:
                    // Something answered that address, but it was not her.
                    pairViaRelay(token)
                }
            }
        }.resume()
    }

    /// The code IS the relay channel: ask the relay where the Mac is. Its
    /// presence record carries the Mac's own LAN address, so pairing this
    /// way still stores the fast road for when the owner gets home - and
    /// the app falls back to the relay whenever that road is shut.
    ///
    /// Twice, patiently. The relay sleeps between conversations, and the
    /// first knock pays its cold start: one 30s try, then another, before
    /// anyone is told nobody is home.
    private func pairViaRelay(_ token: String, attempt: Int = 1) {
        let client = RelayClient(base: RelayClient.defaultBase, token: token)
        relay = client                  // held, so nothing dies mid-flight
        client.presence(timeout: 30) { mac in
            DispatchQueue.main.async {
                guard let mac else {
                    if attempt < 2 {
                        error = "Knocking again — the relay was asleep…"
                        pairViaRelay(token, attempt: attempt + 1)
                        return
                    }
                    checking = false
                    relay = nil
                    error = "No Mac answered for that code, twice. Check "
                        + "the code, that Vivieen is awake on the Mac with "
                        + "iPhone access switched on, and that this phone "
                        + "has a working connection."
                    return
                }
                checking = false
                relay = nil
                error = ""
                // Her own LAN address if she published one; otherwise
                // a placeholder the router will never answer, which is
                // honest - every turn then rides the relay.
                let lan = (mac["lan"] as? [String])?.first
                serverAddress = lan ?? "http://vivieen.invalid"
                pairingToken = token
            }
        }
    }
}
