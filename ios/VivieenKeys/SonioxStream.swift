/// Soniox realtime, streamed - the words arrive WHILE the owner speaks.
///
/// The batch tap (SonioxTap) sends a finished take and waits; a keyboard
/// wants the opposite: open the socket on press, feed raw PCM as the mic
/// produces it, and surface every token the moment it lands - interim
/// tokens refine in place, final tokens are settled text. Release sends
/// the empty TEXT frame (the measured detail: the documented empty
/// binary frame just times out) and the last finals flush back.
import Foundation

final class SonioxStream {
    private static let endpoint = "wss://stt-rt.soniox.com/transcribe-websocket"

    /// Called on the main queue with (settled, refining) after every
    /// provider message - settled only ever grows.
    var onText: ((String, String) -> Void)?
    /// Called exactly once, on the main queue: (settled, error?).
    var onDone: ((String, String?) -> Void)?

    private let task: URLSessionWebSocketTask
    private let opening: String
    private var finals = ""
    private var settled = false
    private let gate = NSLock()

    init?(apiKey: String, model: String, language: String, rate: Int) {
        guard let url = URL(string: Self.endpoint) else { return nil }
        var config: [String: Any] = [
            "api_key": apiKey,
            "model": model.hasPrefix("stt-rt") ? model : "stt-rt-v5",
            "audio_format": "pcm_s16le",
            "sample_rate": rate,
            "num_channels": 1,
        ]
        if !language.isEmpty, language != "auto" {
            config["language_hints"] = [language]
        }
        guard let raw = try? JSONSerialization.data(withJSONObject: config),
              let text = String(data: raw, encoding: .utf8) else { return nil }
        opening = text
        task = URLSession.shared.webSocketTask(with: url)
    }

    func start() {
        task.resume()
        task.send(.string(opening)) { [weak self] error in
            if let error { self?.finish(error.localizedDescription) }
        }
        listen()
    }

    func feed(_ pcm: Data) {
        task.send(.data(pcm)) { _ in }
    }

    /// The owner released the key: end the take, let the tail finalise.
    func stop() {
        task.send(.string("")) { _ in }
    }

    func cancel() {
        finish(nil, silent: true)
    }

    private func listen() {
        task.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.finish(error.localizedDescription)
            case .success(let message):
                if case .string(let raw) = message { self.digest(raw) }
                self.gate.lock()
                let live = !self.settled
                self.gate.unlock()
                if live { self.listen() }
            }
        }
    }

    private func digest(_ raw: String) {
        guard let data = raw.data(using: .utf8),
              let payload = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any] else { return }
        if let trouble = payload["error_message"] as? String {
            finish(trouble)
            return
        }
        var interim = ""
        for token in (payload["tokens"] as? [[String: Any]]) ?? [] {
            let text = (token["text"] as? String) ?? ""
            if (token["is_final"] as? Bool) == true {
                finals += text
            } else {
                interim += text
            }
        }
        let settledText = finals
        DispatchQueue.main.async { self.onText?(settledText, interim) }
        if (payload["finished"] as? Bool) == true {
            finish(nil)
        }
    }

    private func finish(_ error: String?, silent: Bool = false) {
        gate.lock()
        if settled { gate.unlock(); return }
        settled = true
        gate.unlock()
        task.cancel(with: .goingAway, reason: nil)
        guard !silent else { return }
        let text = finals
        DispatchQueue.main.async { self.onDone?(text, error) }
    }
}
