import AVFoundation
import Foundation
import UIKit

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
    private let gate = NSLock()

    /// iOS refuses to START a capture from the background (forums
    /// 120038) - the app answered the knock and its fresh microphone
    /// delivered pure silence (owner's flat wave, 2026-08-06). So the
    /// ear is ARMED from the foreground and never stops: the engine
    /// runs, frames flow into this tap and are discarded until a take
    /// attaches Soniox. The cost is worn openly - the mic indicator
    /// stays lit while Vivieen is alive - and it is exactly the deal
    /// every always-ready dictation keyboard makes.
    func armIfNeeded() {
        DispatchQueue.main.async {
            guard !LiveTap.shared.isLive,
                  !MicDriver.shared.isRunning else { return }
            MicDriver.shared.earTap = { [weak self] pcm in
                self?.route(pcm)
            }
            MicDriver.shared.start(rate: 16000, earOnly: true)
            NSLog("[viv-ear] armed")
        }
    }

    /// Every frame from the standing tap, on the audio thread: feed the
    /// take if one is rolling, otherwise let it fall to the floor.
    private func route(_ pcm: Data) {
        gate.lock()
        let id = takeID
        let live = stream
        gate.unlock()
        guard !id.isEmpty, let live else { return }
        live.feed(pcm)
        suite?.set(Double(Self.loudness(pcm)), forKey: "take.level")
        suite?.set(Date().timeIntervalSince1970, forKey: "take.beat")
    }

    func listen() {
        let center = CFNotificationCenterGetDarwinNotifyCenter()
        let me = Unmanaged.passUnretained(self).toOpaque()
        CFNotificationCenterAddObserver(center, me, { _, observer, _, _, _ in
            guard let observer else { return }
            Unmanaged<KeyboardEar>.fromOpaque(observer)
                .takeUnretainedValue().answer()
        }, Self.knock as CFString, nil, .deliverImmediately)
        // The knock only reaches a RUNNING process. Backgrounded with no
        // audio, iOS suspends the app within moments - so the keyboard
        // worked inside Vivieen and fell deaf in every other app (owner,
        // 2026-08-06). While backgrounded, a silent player breathes on a
        // session that MIXES - nobody's music stops for it - and the ear
        // stays reachable. The other half of Wispr Flow's trick.
        NotificationCenter.default.addObserver(
            forName: UIApplication.didEnterBackgroundNotification,
            object: nil, queue: .main) { [weak self] _ in
            self?.startKeepAlive()
        }
        NotificationCenter.default.addObserver(
            forName: UIApplication.willEnterForegroundNotification,
            object: nil, queue: .main) { [weak self] _ in
            self?.stopKeepAlive()
            self?.armIfNeeded()
        }
        // Live talk and the composer borrow the microphone and hand it
        // back through stop(); the standing tap re-arms a beat later.
        NotificationCenter.default.addObserver(
            forName: MicDriver.becameIdle, object: nil,
            queue: .main) { [weak self] _ in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                self?.armIfNeeded()
            }
        }
        armIfNeeded()
    }

    // ------------------------------------------------------ keep-alive

    private var keepAlive: AVAudioPlayer?

    private func startKeepAlive() {
        // An ARMED ear keeps the process alive by itself - and a playback
        // session started over it would kill the very microphone the
        // background is not allowed to reopen. Silence is only for a
        // house whose ear never armed.
        guard keepAlive == nil, !MicDriver.shared.isRunning else { return }
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .default,
                                 options: [.mixWithOthers])
        try? session.setActive(true)
        guard let player = try? AVAudioPlayer(
            contentsOf: Self.silenceFile()) else { return }
        player.numberOfLoops = -1
        player.volume = 0
        player.play()
        keepAlive = player
        NSLog("[viv-ear] keep-alive breathing")
    }

    private func stopKeepAlive() {
        guard keepAlive != nil else { return }
        keepAlive?.stop()
        keepAlive = nil
        AudioSession.playbackOnly()
        NSLog("[viv-ear] keep-alive resting")
    }

    /// One second of 8 kHz silence, written once. Playing nothing is
    /// what keeps the process allowed to hear something.
    private static func silenceFile() -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("viv-keepalive.wav")
        if FileManager.default.fileExists(atPath: url.path) { return url }
        let rate = 8000
        let body = Data(count: rate * 2)
        var wav = Data("RIFF".utf8)
        func word(_ value: UInt32) {
            withUnsafeBytes(of: value.littleEndian) { wav.append(contentsOf: $0) }
        }
        func half(_ value: UInt16) {
            withUnsafeBytes(of: value.littleEndian) { wav.append(contentsOf: $0) }
        }
        word(UInt32(36 + body.count))
        wav.append(contentsOf: Data("WAVEfmt ".utf8))
        word(16); half(1); half(1)
        word(UInt32(rate)); word(UInt32(rate * 2)); half(2); half(16)
        wav.append(contentsOf: Data("data".utf8))
        word(UInt32(body.count))
        wav.append(body)
        try? wav.write(to: url)
        return url
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
            self.gate.lock()
            if self.takeID == id {
                self.takeID = ""
                self.stream = nil
            }
            self.gate.unlock()
            // The engine keeps running - the standing tap is what lets
            // the NEXT take start from the background at all.
        }
        // The mic must already be rolling: a background take cannot
        // start one (forums 120038). If the ear is armed, attach; if the
        // house is somehow quiet, try to arm - it only works foreground,
        // and the honest error covers the rest.
        if !MicDriver.shared.isRunning {
            DispatchQueue.main.sync { [weak self] in
                self?.keepAlive?.stop()
                self?.keepAlive = nil
            }
            armIfNeeded()
        }
        gate.lock()
        takeID = id
        stream = live
        gate.unlock()
        live.start()
        suite.set(id, forKey: "take.id")
        suite.set("", forKey: "take.settled")
        suite.set("", forKey: "take.refining")
        suite.set(0.0, forKey: "take.level")
        suite.set("listening", forKey: "take.state")
        suite.set(Date().timeIntervalSince1970, forKey: "take.beat")
        // Flush so the keyboard's very next poll sees the answer, not the
        // previous take's leavings.
        suite.synchronize()
        NSLog("[viv-ear] take %@ listening", id)
    }

    private func end(_ id: String) {
        gate.lock()
        let matches = !id.isEmpty && id == takeID
        let live = stream
        gate.unlock()
        guard matches else { return }
        // The socket's own four-second deadline lands the tail; onDone
        // writes the final state either way. The microphone keeps
        // rolling - it is the ear's, not the take's.
        live?.stop()
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
