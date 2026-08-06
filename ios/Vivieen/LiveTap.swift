import Foundation
import WebKit

/// Live talk with the Mac switched off.
///
/// The page cannot hold this socket. Not because of policy - because a
/// backgrounded WKWebView is suspended, and a page-held socket dies with
/// it. So the whole audio path lives here: the microphone feeds this
/// class directly, her voice goes straight to SpeechPlayer, and the page
/// is only told what to DISPLAY. Put the phone in your pocket and the
/// conversation keeps going (owner, 2026-08-03).
///
/// A port of the Mac's two provider legs, event for event, so the page's
/// existing live-talk UI works untouched: it receives exactly the message
/// shapes the Mac's /live/voice socket sends.
final class LiveTap: NSObject {
    static let shared = LiveTap()

    weak var webView: WKWebView?
    private var socket: URLSessionWebSocketTask?
    private let gate = NSLock()
    private var live = false
    private var provider = ""
    /// Her own voice must not be heard as yours. Mic frames inside this
    /// window go up silent - the same guard the page applies when the Mac
    /// is carrying the call.
    private var echoUntil = Date.distantPast
    private var wireRate: Double = 16000
    /// A quiet line hangs itself up after this long. Only a VOICE resets
    /// the clock: the stream never stops - zeroed frames while she speaks,
    /// room tone while nobody does - so counting any audio at all would
    /// mean the hangup could never fire. Ported from the Mac, which the
    /// native leg had simply been missing (owner, 2026-08-03).
    private static let silenceHangup: TimeInterval = 15
    private var lastVoice = Date()

    var isLive: Bool { gate.lock(); defer { gate.unlock() }; return live }

    // ---------------------------------------------------------------- up

    func start() {
        gate.lock()
        if live { gate.unlock(); return }
        live = true
        gate.unlock()

        let cfg = (SoloStore.shared.config["live"] as? [String: Any]) ?? [:]
        let want = (cfg["provider"] as? String) ?? "xai"
        let xaiKey = SoloStore.shared.secret("live.xai_api_key")
        let elevenKey = SoloStore.shared.secret("live.eleven_api_key")
        let people = SoloStore.shared.config["persona"] as? [String: Any]
        let persona = (people?["system"] as? String) ?? ""

        if want == "elevenlabs", !elevenKey.isEmpty {
            let agent = (cfg["eleven_agent_id"] as? String) ?? ""
            guard !agent.isEmpty else {
                fail("ElevenLabs live needs an agent — open Settings on your "
                     + "Mac once so this phone learns which one")
                return
            }
            openEleven(agent: agent, key: elevenKey)
        } else if !xaiKey.isEmpty {
            let voice = (cfg["xai_voice"] as? String) ?? "eve"
            // The owner picked ElevenLabs and got Grok's Eve, with nothing
            // said - the ElevenLabs key had simply never reached this phone,
            // and this branch quietly took the other road (owner: "might be
            // Eve", 2026-08-04). A substitution nobody announced is the bug,
            // not the substitution itself: make the call, and name it.
            if want == "elevenlabs" {
                toPage(["type": "agent_text", "final": true,
                        "text": "· ElevenLabs has not reached this phone — "
                              + "answering in Grok's \(voice) voice. Open the "
                              + "app once beside your Mac to carry the key over. ·"])
            }
            // Split-brain (#24) runs on the Mac's own models, which this
            // phone cannot reach without the Mac. Same rule as above:
            // stand in, and say so.
            if want == "vivieen" {
                toPage(["type": "agent_text", "final": true,
                        "text": "· Her own models live on your Mac, which is "
                              + "out of reach — answering in Grok's \(voice) "
                              + "voice instead. ·"])
            }
            openXAI(key: xaiKey,
                    model: (cfg["xai_model"] as? String) ?? "grok-voice-think-fast-1.0",
                    voice: voice,
                    persona: persona)
        } else {
            fail("no live-talk key has reached this phone yet — open the app "
                 + "once beside your Mac")
        }
    }

    private func openEleven(agent: String, key: String) {
        guard let url = URL(string:
            "wss://api.elevenlabs.io/v1/convai/conversation?agent_id=\(agent)")
        else { fail("bad ElevenLabs URL"); return }
        var request = URLRequest(url: url)
        request.setValue(key, forHTTPHeaderField: "xi-api-key")
        provider = "elevenlabs"
        wireRate = 16000
        connect(request) { [weak self] in
            self?.ready(inputRate: 16000)
        }
    }

    private func openXAI(key: String, model: String, voice: String,
                         persona: String) {
        guard let url = URL(string: "wss://api.x.ai/v1/realtime?model=\(model)")
        else { fail("bad xAI URL"); return }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        provider = "xai"
        wireRate = 24000
        connect(request) { [weak self] in
            guard let self else { return }
            let session: [String: Any] = ["type": "session.update", "session": [
                "voice": voice,
                "instructions": persona,
                "turn_detection": ["type": "server_vad",
                                   "silence_duration_ms": 700],
                "audio": [
                    "input": ["format": ["type": "audio/pcm", "rate": 24000],
                              "transport": "json"],
                    "output": ["format": ["type": "audio/pcm", "rate": 24000],
                               "transport": "json"],
                ],
            ]]
            self.sendJSON(session)
            self.ready(inputRate: 24000)
        }
    }

    private func connect(_ request: URLRequest, then: @escaping () -> Void) {
        let task = URLSession.shared.webSocketTask(with: request)
        socket = task
        task.resume()
        NSLog("[viv-live] %@ socket open", provider)
        listen()
        then()
    }

    /// RMS of a little-endian 16-bit mono frame, 0...1.
    private static func loudness(_ pcm: Data) -> Double {
        let count = pcm.count / 2
        guard count > 0 else { return 0 }
        var sum = 0.0
        pcm.withUnsafeBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            for i in 0..<count {
                let v = Double(Int16(littleEndian: samples[i])) / 32768.0
                sum += v * v
            }
        }
        return (sum / Double(count)).squareRoot()
    }

    /// Check in every few seconds; close a line nobody is speaking on.
    private func watchSilence() {
        DispatchQueue.global().asyncAfter(deadline: .now() + 3) { [weak self] in
            guard let self, self.isLive else { return }
            self.gate.lock()
            let quietFor = Date().timeIntervalSince(self.lastVoice)
            self.gate.unlock()
            if quietFor > LiveTap.silenceHangup {
                NSLog("[viv-live] quiet %.0fs - hanging up", quietFor)
                self.close(reason: "silence")
                return
            }
            self.watchSilence()
        }
    }

    private func ready(inputRate: Double) {
        // Her voice plays natively, so it survives the app going away.
        AudioSession.speakAndListen()
        MicDriver.shared.onPCM = { [weak self] pcm in self?.push(pcm) }
        MicDriver.shared.start(rate: inputRate)
        gate.lock(); lastVoice = Date(); gate.unlock()
        watchSilence()
        toPage(["type": "ready", "provider": provider,
                "input_rate": inputRate, "output_rate": inputRate,
                "native": true])
    }

    /// One microphone frame, guarded and sent. Called on the audio thread.
    private func push(_ pcm: Data) {
        gate.lock()
        let quiet = Date() < echoUntil
        let up = live
        gate.unlock()
        guard up else { return }
        let heard = LiveTap.loudness(pcm)
        // BARGE-IN: voice processing (MicDriver) subtracts her own
        // playback from the mic, so a frame that is still loud inside the
        // echo window is the OWNER interrupting - it goes up for real.
        // The 0.03 floor is the guard rail for a device where the
        // cancellation did not engage: residual echo stays under it, a
        // voice across the room does not (owner: "livetalk doesn't take
        // my speech when he's talking", 2026-08-06).
        let interrupting = quiet && heard > 0.03
        if (!quiet && heard > 0.012) || interrupting {
            gate.lock(); lastVoice = Date(); gate.unlock()
        }
        // Inside the echo window send SILENCE rather than nothing: the
        // provider's turn detector reads a gap in the stream as trouble.
        let frame = (quiet && !interrupting) ? Data(count: pcm.count) : pcm
        let b64 = frame.base64EncodedString()
        if provider == "elevenlabs" {
            sendJSON(["user_audio_chunk": b64])
        } else {
            sendJSON(["type": "input_audio_buffer.append", "audio": b64])
        }
    }

    // -------------------------------------------------------------- down

    private func listen() {
        socket?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.close(reason: error.localizedDescription)
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
                    self.handle(payload)
                }
                if self.isLive { self.listen() }
            }
        }
    }

    private func handle(_ payload: [String: Any]) {
        let kind = (payload["type"] as? String) ?? ""
        if provider == "elevenlabs" {
            switch kind {
            case "ping":
                // ElevenLabs keep-alive. Miss it and the line drops.
                let id = (payload["ping_event"] as? [String: Any])?["event_id"]
                sendJSON(["type": "pong", "event_id": id ?? 0])
            case "audio":
                let data = (payload["audio_event"] as? [String: Any])?[
                    "audio_base_64"] as? String ?? ""
                if !data.isEmpty { play(data, rate: 16000) }
            case "user_transcript":
                let text = (payload["user_transcription_event"]
                    as? [String: Any])?["user_transcript"] as? String ?? ""
                toPage(["type": "user_text", "text": text])
            case "agent_response":
                let text = (payload["agent_response_event"]
                    as? [String: Any])?["agent_response"] as? String ?? ""
                toPage(["type": "agent_text", "text": text, "final": true])
            case "interruption":
                SpeechPlayer.shared.flush()
                gate.lock(); echoUntil = .distantPast; gate.unlock()
                toPage(["type": "interrupt"])
            default: break
            }
            return
        }
        switch kind {
        case "response.output_audio.delta":
            let data = (payload["delta"] as? String)
                ?? (payload["audio"] as? String) ?? ""
            if !data.isEmpty { play(data, rate: 24000) }
        case "response.output_audio_transcript.delta":
            toPage(["type": "agent_text",
                    "text": payload["delta"] as? String ?? "", "final": false])
        case "response.output_audio_transcript.done":
            toPage(["type": "agent_text",
                    "text": payload["transcript"] as? String ?? "",
                    "final": true])
        case "conversation.item.input_audio_transcription.updated":
            toPage(["type": "user_text",
                    "text": payload["transcript"] as? String ?? ""])
        case "input_audio_buffer.speech_started":
            SpeechPlayer.shared.flush()
            gate.lock(); echoUntil = .distantPast; gate.unlock()
            toPage(["type": "interrupt"])
        case "error":
            let why = (payload["error"] as? [String: Any])?["message"]
                as? String ?? "provider error"
            fail(why)
        default: break
        }
    }

    private func play(_ b64: String, rate: Double) {
        SpeechPlayer.shared.enqueue(base64: b64, rate: rate)
        // 16-bit mono: bytes / 2 / rate seconds of sound, plus a beat for
        // the room. The mic stays polite for exactly that long.
        let bytes = Double(Data(base64Encoded: b64)?.count ?? 0)
        let seconds = bytes / 2 / rate
        gate.lock()
        let from = max(Date(), echoUntil)
        echoUntil = from.addingTimeInterval(seconds + 0.15)
        // HER speech is liveness too. The quiet clock counted only the
        // owner's voice, so a thirty-second answer read as thirty silent
        // seconds and the line hung up MID-REPLY at fifteen (owner,
        // 2026-08-06). An idle desk still hangs up - an idle desk has no
        // agent audio either.
        lastVoice = Date()
        gate.unlock()
        // The page has no audio to analyse when the app is doing the
        // playing, so it cannot time her mouth the usual way. Send it the
        // one fact it needs: how long this chunk will sound for.
        toPage(["type": "mouth", "seconds": seconds])
    }

    // ------------------------------------------------------------- close

    func stop(_ reason: String = "ended") { close(reason: reason) }

    private func close(reason: String) {
        gate.lock()
        if !live { gate.unlock(); return }
        live = false
        gate.unlock()
        NSLog("[viv-live] closing: %@", reason)
        MicDriver.shared.onPCM = nil
        MicDriver.shared.stop()
        SpeechPlayer.shared.flush()
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        toPage(["type": "closed", "reason": reason])
    }

    private func fail(_ why: String) {
        NSLog("[viv-live] failed: %@", why)
        toPage(["type": "error", "message": why])
        close(reason: "error")
    }

    // ------------------------------------------------------------ wiring

    private func sendJSON(_ object: [String: Any]) {
        guard let raw = try? JSONSerialization.data(withJSONObject: object),
              let text = String(data: raw, encoding: .utf8) else { return }
        socket?.send(.string(text)) { [weak self] error in
            if let error { self?.close(reason: error.localizedDescription) }
        }
    }

    /// The page is a DISPLAY here, nothing more. It may be suspended when
    /// this fires; that is fine, the conversation does not depend on it.
    private func toPage(_ event: [String: Any]) {
        guard let raw = try? JSONSerialization.data(withJSONObject: event),
              let json = String(data: raw, encoding: .utf8) else { return }
        let escaped = json
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "'", with: "\\'")
            .replacingOccurrences(of: "\n", with: " ")
        DispatchQueue.main.async { [weak self] in
            self?.webView?.evaluateJavaScript(
                "window.__vivLive&&__vivLive('\(escaped)')",
                completionHandler: nil)
        }
    }
}
