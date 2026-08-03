import Foundation

/// Soniox is a REALTIME service: its only transcription surface is a
/// WebSocket. The solo proxy speaks HTTPS and nothing else, so choosing
/// Soniox for hearing left the phone with no ears at all once the Mac was
/// gone - solo fell through to whichever other key happened to be synced,
/// and push-to-talk failed with somebody else's error (owner, 2026-08-03).
///
/// A faithful port of the Mac's flow, including the one detail there that
/// was measured rather than documented: finalisation needs an empty TEXT
/// frame; the empty BINARY frame the docs suggest just times out.
enum SonioxTap {
    private static let endpoint = "wss://stt-rt.soniox.com/transcribe-websocket"

    /// One take in, one transcript out. `done` is called exactly once.
    static func transcribe(wav: Data, apiKey: String, model: String,
                           language: String,
                           done: @escaping (String?, String?) -> Void) {
        guard let url = URL(string: endpoint) else {
            done(nil, "bad Soniox endpoint"); return
        }
        let gate = NSLock()
        var finals = ""
        var settled = false
        let task = URLSession.shared.webSocketTask(with: url)

        func finish(_ text: String?, _ error: String?) {
            gate.lock()
            if settled { gate.unlock(); return }
            settled = true
            gate.unlock()
            task.cancel(with: .goingAway, reason: nil)
            NSLog("[viv-soniox] done chars=%d err=%@",
                  text?.count ?? -1, error ?? "none")
            done(text, error)
        }

        var config: [String: Any] = [
            "api_key": apiKey,
            "model": model.hasPrefix("stt-rt") ? model : "stt-rt-v5",
            "audio_format": "auto",
        ]
        if !language.isEmpty, language != "auto" {
            config["language_hints"] = [language]
        }
        guard let raw = try? JSONSerialization.data(withJSONObject: config),
              let opening = String(data: raw, encoding: .utf8) else {
            finish(nil, "bad Soniox config"); return
        }

        task.resume()
        NSLog("[viv-soniox] open wav=%d model=%@", wav.count,
              config["model"] as? String ?? "?")
        // Config, then the take, then the empty TEXT frame that ends it.
        task.send(.string(opening)) { error in
            if let error { finish(nil, error.localizedDescription); return }
            task.send(.data(wav)) { error in
                if let error { finish(nil, error.localizedDescription); return }
                task.send(.string("")) { _ in }
            }
        }

        func listen() {
            task.receive { result in
                switch result {
                case .failure(let error):
                    gate.lock(); let heard = finals; gate.unlock()
                    // A socket that drops AFTER the words arrived still
                    // delivered them; only silence is a failure.
                    finish(heard.isEmpty ? nil : heard,
                           heard.isEmpty ? error.localizedDescription : nil)
                case .success(let message):
                    var body: Data?
                    switch message {
                    case .string(let text): body = Data(text.utf8)
                    case .data(let data): body = data
                    @unknown default: body = nil
                    }
                    if let body,
                       let payload = try? JSONSerialization.jsonObject(with: body)
                        as? [String: Any] {
                        if let why = payload["error_message"] as? String, !why.isEmpty {
                            finish(nil, String(why.prefix(160))); return
                        }
                        gate.lock()
                        for token in (payload["tokens"] as? [[String: Any]]) ?? [] {
                            if (token["is_final"] as? Bool) == true {
                                finals += (token["text"] as? String) ?? ""
                            }
                        }
                        let heard = finals
                        gate.unlock()
                        if (payload["finished"] as? Bool) == true {
                            finish(heard.trimmingCharacters(
                                in: .whitespacesAndNewlines), nil)
                            return
                        }
                    }
                    listen()
                }
            }
        }
        listen()

        // Nothing in solo may wait without end - there is no Mac to look at.
        DispatchQueue.global().asyncAfter(deadline: .now() + 45) {
            gate.lock(); let heard = finals; gate.unlock()
            finish(heard.isEmpty ? nil : heard,
                   heard.isEmpty ? "Soniox did not answer in 45s" : nil)
        }
    }
}
