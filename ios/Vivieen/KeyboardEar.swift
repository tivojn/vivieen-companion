import AVFoundation
import Foundation

/// The keyboard's ears live HERE, in the app.
///
/// A keyboard extension is never handed the microphone: Apple's sandbox
/// lets a capture session start and simply never feeds it a frame -
/// proven on the owner's phone (2026-08-06) after the engine, the
/// dictation-key declaration and AVCaptureSession all "ran" in silence.
/// The simulator does not enforce the rule, which is why every road
/// worked there and none on the device.
///
/// So Vivieen Keys is a TRIGGER. The key writes a command into the App
/// Group and knocks on the Darwin door; this class - inside the app,
/// which MAY record - opens the microphone, streams Soniox realtime,
/// and writes every settled word, refining tail and live level back
/// into the shared container for the keyboard to paint. Wispr Flow's
/// actual trick, on her stack. The cost is honest: the app must be
/// alive (foreground, or backgrounded and not yet suspended) - the
/// keyboard says "open Vivieen once" when nobody answers the knock.
final class KeyboardEar {
    static let shared = KeyboardEar()
    static let knock = "com.vivieen.pocket.keys.knock"

    private let suite = UserDefaults(suiteName: SoloStore.groupSuite)
    private var stream: SonioxStream?
    private var takeID = ""
    private let queue = DispatchQueue(label: "com.vivieen.keys.ear")

    func listen() {
        let center = CFNotificationCenterGetDarwinNotifyCenter()
        let me = Unmanaged.passUnretained(self).toOpaque()
        CFNotificationCenterAddObserver(center, me, { _, observer, _, _, _ in
            guard let observer else { return }
            Unmanaged<KeyboardEar>.fromOpaque(observer)
                .takeUnretainedValue().answer()
        }, Self.knock as CFString, nil, .deliverImmediately)
    }

    private func answer() {
        queue.async { [weak self] in self?.act() }
    }

    private func act() {
        guard let suite else { return }
        let command = suite.string(forKey: "take.cmd") ?? ""
        let parts = command.split(separator: " ")
        guard parts.count == 2 else { return }
        switch String(parts[0]) {
        case "start": begin(String(parts[1]))
        case "stop": end(String(parts[1]))
        default: break
        }
    }

    private func begin(_ id: String) {
        guard let suite, id != takeID else { return }
        if LiveTap.shared.isLive {
            suite.set(id, forKey: "take.id")
            suite.set("error live talk holds the microphone",
                      forKey: "take.state")
            return
        }
        end(takeID)                          // any straggling take
        takeID = id
        let stt = (SoloStore.shared.config["stt"] as? [String: Any]) ?? [:]
        let key = suite.string(forKey: "keys.soniox") ?? ""
        guard !key.isEmpty else {
            suite.set(id, forKey: "take.id")
            suite.set("error no Soniox key on this phone yet",
                      forKey: "take.state")
            return
        }
        guard let live = SonioxStream(
            apiKey: key,
            model: (stt["model"] as? String) ?? "",
            language: (stt["language"] as? String) ?? "",
            rate: 16000) else {
            suite.set(id, forKey: "take.id")
            suite.set("error could not open the hearing line",
                      forKey: "take.state")
            return
        }
        live.onText = { [weak self] settled, refining in
            guard let self, self.takeID == id, let suite = self.suite
            else { return }
            suite.set(settled, forKey: "take.settled")
            suite.set(refining, forKey: "take.refining")
            suite.set(Date().timeIntervalSince1970, forKey: "take.beat")
        }
        live.onDone = { [weak self] settled, error in
            guard let self, let suite = self.suite else { return }
            suite.set(settled, forKey: "take.settled")
            suite.set("", forKey: "take.refining")
            suite.set(error == nil ? "done" : "error \(error ?? "")",
                      forKey: "take.state")
            suite.set(Date().timeIntervalSince1970, forKey: "take.beat")
            if self.takeID == id {
                self.takeID = ""
                self.stream = nil
                MicDriver.shared.onPCM = nil
                MicDriver.shared.stop()
            }
        }
        live.start()
        stream = live
        suite.set(id, forKey: "take.id")
        suite.set("", forKey: "take.settled")
        suite.set("", forKey: "take.refining")
        suite.set(0.0, forKey: "take.level")
        suite.set("listening", forKey: "take.state")
        suite.set(Date().timeIntervalSince1970, forKey: "take.beat")
        MicDriver.shared.onPCM = { [weak self] pcm in
            guard let self, self.takeID == id else { return }
            self.stream?.feed(pcm)
            self.suite?.set(Double(Self.loudness(pcm)), forKey: "take.level")
            self.suite?.set(Date().timeIntervalSince1970, forKey: "take.beat")
        }
        MicDriver.shared.start(rate: 16000)
        NSLog("[viv-ear] take %@ listening", id)
    }

    private func end(_ id: String) {
        guard !id.isEmpty, id == takeID else { return }
        MicDriver.shared.onPCM = nil
        MicDriver.shared.stop()
        // The socket's own four-second deadline lands the tail; onDone
        // writes the final state either way.
        stream?.stop()
        NSLog("[viv-ear] take %@ stopping", id)
    }

    private static func loudness(_ pcm: Data) -> Float {
        let count = pcm.count / 2
        guard count > 0 else { return 0 }
        var sum: Float = 0
        pcm.withUnsafeBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            for i in 0..<count {
                let v = Float(Int16(littleEndian: samples[i])) / 32768
                sum += v * v
            }
        }
        return (sum / Float(count)).squareRoot()
    }
}
