import CryptoKit
import Foundation
import Security

/// Solo mode's memory: the provider configuration and the keys, synced
/// from the Mac and kept where a phone keeps secrets.
///
/// The sync payload crosses the network (and possibly the blind relay)
/// with every secret AES-GCM encrypted under a key DERIVED from the
/// pairing token - which the relay never sees - so nothing readable ever
/// rests on third-party disk. On arrival, secrets go into the iOS
/// Keychain and only their NAMES are ever shown to the page; the page
/// asks the native proxy to make provider calls and the proxy injects
/// the value. A compromised page script could spend the keys, but never
/// read them.
final class SoloStore {
    static let shared = SoloStore()
    private let service = "com.vivieen.pocket.solo"
    private let lock = NSLock()

    // ------------------------------------------------------------ keychain

    private func setSecret(_ name: String, _ value: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: name,
        ]
        SecItemDelete(query as CFDictionary)
        guard !value.isEmpty else { return }
        var add = query
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] =
            kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(add as CFDictionary, nil)
        if status != errSecSuccess {
            NSLog("[viv-solo] keychain add %@ failed: %d", name, status)
            fallbackWrite(name, value, status)
        }
    }

    // ------------------------------------------------- simulator fallback
    //
    // The Simulator cannot hold these. An ad-hoc signed build has no
    // keychain access group - Xcode strips the entitlement because there
    // is no team prefix to expand - so every write returns -34018 and
    // solo mode silently owned no keys at all: offline chat could never
    // start, and the turn fell through to a Mac that was not there.
    //
    // SIMULATOR ONLY, deliberately. A device build is signed with a real
    // profile, gets its access group from it, and never reaches this
    // code - so the shipping security story is unchanged: on a phone the
    // secrets live in the Keychain or nowhere. Here they live in the app
    // container, which is a development convenience, not a vault.
    private func fallbackWrite(_ name: String, _ value: String, _ status: OSStatus) {
        #if targetEnvironment(simulator)
        guard status == errSecMissingEntitlement else { return }
        var box = UserDefaults.standard.dictionary(forKey: "soloSimSecrets")
            as? [String: String] ?? [:]
        box[name] = value
        UserDefaults.standard.set(box, forKey: "soloSimSecrets")
        NSLog("[viv-solo] simulator fallback holds %@", name)
        #endif
    }

    private func fallbackRead(_ name: String) -> String {
        #if targetEnvironment(simulator)
        let box = UserDefaults.standard.dictionary(forKey: "soloSimSecrets")
            as? [String: String] ?? [:]
        return box[name] ?? ""
        #else
        return ""
        #endif
    }

    // ------------------------------------------------- keyboard mirror
    //
    // The Vivieen Keys keyboard runs in its own process and cannot read
    // this store's keychain items (different default access group) - so
    // the app mirrors the DICTATION essentials, and only those, into the
    // shared App Group container: the Soniox key, model, and language.
    // One key, scoped to one purpose, refreshed on every sync and every
    // launch; everything else stays in the Keychain.
    static let groupSuite = "group.com.vivieen.pocket"

    func mirrorForKeyboard() {
        guard let shared = UserDefaults(suiteName: Self.groupSuite) else {
            return
        }
        let stt = (config["stt"] as? [String: Any]) ?? [:]
        shared.set(secret("stt.api_key"), forKey: "keys.soniox")
        shared.set((stt["model"] as? String) ?? "", forKey: "keys.model")
        shared.set((stt["language"] as? String) ?? "", forKey: "keys.language")
    }

    /// The solo Settings engine's door: a key the owner pastes on the
    /// PHONE lands in the iOS Keychain exactly like a synced one. An
    /// empty value clears the entry.
    func storeSecret(_ name: String, _ value: String) {
        setSecret(name, value)
    }

    func secret(_ name: String) -> String {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: name,
            kSecReturnData as String: true,
        ]
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data else { return fallbackRead(name) }
        return String(data: data, encoding: .utf8) ?? fallbackRead(name)
    }

    // ------------------------------------------------------------ config

    var config: [String: Any] {
        get {
            guard let raw = UserDefaults.standard.data(forKey: "soloConfig"),
                  let json = try? JSONSerialization.jsonObject(with: raw)
                    as? [String: Any] else { return [:] }
            return json
        }
        set {
            if let raw = try? JSONSerialization.data(withJSONObject: newValue) {
                UserDefaults.standard.set(raw, forKey: "soloConfig")
            }
        }
    }

    /// Hosts the proxy may speak to: every base URL in the synced config
    /// plus the majors, so a fresh sync cannot brick calling.
    func allowedHosts() -> Set<String> {
        var hosts: Set<String> = [
            "api.openai.com", "api.anthropic.com", "api.x.ai",
            "generativelanguage.googleapis.com", "api.elevenlabs.io",
            "api.deepgram.com", "api.groq.com", "api.cartesia.ai",
            "api.mistral.ai", "api.together.xyz", "api.deepseek.com",
            "openrouter.ai",
        ]
        for (_, value) in config {
            guard let block = value as? [String: Any],
                  let base = block["base_url"] as? String,
                  let host = URL(string: base)?.host else { continue }
            hosts.insert(host)
        }
        return hosts
    }

    // ------------------------------------------------------------ sync

    /// Pull /api/sync/solo from the Mac, decrypt, store. Fire-and-forget.
    func sync(address: String, token: String) {
        guard let url = URL(string: address + "/api/sync/solo") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 15
        request.setValue(token, forHTTPHeaderField: "x-vivieen-token")
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            guard let self, let data,
                  (response as? HTTPURLResponse)?.statusCode == 200,
                  let top = try? JSONSerialization.jsonObject(with: data)
                    as? [String: Any] else { return }
            self.lock.lock(); defer { self.lock.unlock() }
            if let cfg = top["config"] as? [String: Any] {
                self.config = cfg
            }
            guard let secrets = top["secrets"] as? [String: [String: String]]
            else { return }
            let key = Self.deriveKey(token: token)
            var stored = 0
            for (name, box) in secrets {
                // Python's AESGCM.encrypt returns ciphertext||tag; a
                // CryptoKit SealedBox wants nonce||ciphertext||tag.
                guard let nonceB64 = box["n"], let cipherB64 = box["c"],
                      let nonceData = Data(base64Encoded: nonceB64),
                      let cipherData = Data(base64Encoded: cipherB64),
                      let sealed = try? AES.GCM.SealedBox(
                        combined: nonceData + cipherData),
                      let plain = try? AES.GCM.open(sealed, using: key),
                      let value = String(data: plain, encoding: .utf8)
                else { continue }
                self.setSecret(name, value)
                stored += 1
            }
            NSLog("[viv-solo] sync: %d secrets, config %d blocks",
                  stored, (top["config"] as? [String: Any])?.count ?? 0)
            self.mirrorForKeyboard()
        }.resume()
    }

    /// HKDF-SHA256 over the pairing token; salt and info pin the purpose
    /// so this key can never be confused with the token itself.
    static func deriveKey(token: String) -> SymmetricKey {
        HKDF<SHA256>.deriveKey(
            inputKeyMaterial: SymmetricKey(data: Data(token.utf8)),
            salt: Data("viv-solo-sync".utf8),
            info: Data("v1".utf8), outputByteCount: 32)
    }
}
