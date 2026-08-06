#if canImport(ActivityKit)
import ActivityKit
#endif
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

    /// Every frame from the tap, on the audio thread: feed the take if
    /// one is rolling, otherwise let it fall to the floor.
    private var voiceFace = VoiceFace()
    private var heardFrame = false

    private func route(_ pcm: Data) {
        gate.lock()
        let id = takeID
        let live = stream
        gate.unlock()
        guard !id.isEmpty, let live else { return }
        heardFrame = true
        live.feed(pcm)
        let level = Double(Self.loudness(pcm))
        lastLevel = level
        // The owner's voice shapes her mouth: the keyboard swaps her
        // viseme stills through the App Group, and the page - whose PiP
        // face may be floating over the host app - mouths along through
        // the same features the EnConvo monitor would send.
        let sample = voiceFace.consume(pcm)
        suite?.set(level, forKey: "take.level")
        suite?.set(sample.viseme, forKey: "take.viseme")
        suite?.set(Date().timeIntervalSince1970, forKey: "take.beat")
        let json = sample.json
        DispatchQueue.main.async {
            MicDriver.shared.webView?.evaluateJavaScript(
                "window.__vivTake&&__vivTake('\(json)')",
                completionHandler: nil)
        }
        islandPush(listening: true,
                   settled: suite?.string(forKey: "take.settled") ?? "")
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
            guard let self else { return }
            if self.standbyWanted { self.startStandby() }
            else { self.startKeepAlive() }
        }
        NotificationCenter.default.addObserver(
            forName: UIApplication.willEnterForegroundNotification,
            object: nil, queue: .main) { [weak self] _ in
            guard let self else { return }
            self.stopKeepAlive()
            self.standbyStop?.invalidate()
            self.gate.lock(); let taking = !self.takeID.isEmpty
            self.gate.unlock()
            if !taking, MicDriver.shared.isRunning {
                MicDriver.shared.stop()   // dot off in the foreground
            }
            self.syncIsland()
        }
        MicDriver.shared.earTap = { [weak self] pcm in
            self?.route(pcm)
        }
        // The heartbeat: five-second proof-of-life in the App Group, so
        // the keyboard can say WHEN she fell asleep instead of only that
        // she did (owner, 2026-08-06).
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) {
            [weak self] _ in
            self?.suite?.set(Date().timeIntervalSince1970,
                             forKey: "ear.alive")
        }
        suite?.set(Date().timeIntervalSince1970, forKey: "ear.alive")
        // A phone call or Siri takes the session; when it is handed
        // back, a backgrounded house must resume breathing on its own.
        NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: nil, queue: .main) { [weak self] note in
            guard let self,
                  let raw = note.userInfo?[
                    AVAudioSessionInterruptionTypeKey] as? UInt,
                  AVAudioSession.InterruptionType(rawValue: raw) == .ended,
                  UIApplication.shared.applicationState != .active
            else { return }
            self.keepAlive?.stop()
            self.keepAlive = nil
            self.startKeepAlive()
        }
        syncIsland()
    }

    // ---------------------------------------------------------- island

    #if canImport(ActivityKit)
    private var island: Activity<TakeAttributes>?
    #endif
    private var lastLevel: Double = 0
    private var lastIslandPush = Date.distantPast

    /// Her face in the Dynamic Island, behind the "island" toggle in
    /// Settings. iOS permits STARTING a Live Activity only from the
    /// foreground, so the app raises it when it opens and merely updates
    /// it from wherever a take happens.
    func syncIsland() {
        #if canImport(ActivityKit)
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            let want = ((SoloStore.shared.config["ui"] as? [String: Any])?[
                "island"] as? Bool) ?? false
            let existing = Activity<TakeAttributes>.activities.first
            if want {
                if let existing { self.island = existing; return }
                let idle = TakeAttributes.ContentState(
                    listening: false, level: 0, settled: "")
                self.island = try? Activity.request(
                    attributes: TakeAttributes(),
                    content: .init(state: idle, staleDate: nil))
            } else if let existing {
                self.island = nil
                Task { await existing.end(nil, dismissalPolicy: .immediate) }
            }
        }
        #endif
    }

    private func islandPush(listening: Bool, settled: String,
                            force: Bool = false) {
        #if canImport(ActivityKit)
        guard let island else { return }
        if !force, Date().timeIntervalSince(lastIslandPush) < 0.4 { return }
        lastIslandPush = Date()
        let state = TakeAttributes.ContentState(
            listening: listening, level: lastLevel, settled: settled)
        Task { await island.update(.init(state: state, staleDate: nil)) }
        #endif
    }

    // ------------------------------------------------- standby window

    /// "Keep her ears warm": while backgrounded, the microphone ITSELF
    /// stays open for a bounded window - real capture is the one audio
    /// iOS never suspends, so dictation works everywhere, reliably. The
    /// orange dot is the price, and the window is SHORT for that reason:
    /// ninety seconds carries a run of dictation, then the light goes
    /// out (owner: "the mic is still on even after the input is done",
    /// 2026-08-06). Each take renews the lease.
    private static let standbyWindow: TimeInterval = 90
    private var standbyStop: Timer?

    /// The DEFAULT is a microphone that opens per take and closes with
    /// it - the light on only while you speak. Warm ears are the
    /// fallback, and the app LEARNS when it needs them: one take that
    /// draws no frame proves this device refuses a background start, and
    /// from the next launch she keeps them warm by herself. The toggle
    /// forces it on for anyone who would rather not meet the failure
    /// once (owner: "after the input is done, stop the mic", 2026-08-06).
    private var standbyWanted: Bool {
        if ((SoloStore.shared.config["ui"] as? [String: Any])?["standby"]
             as? Bool) == true { return true }
        return UserDefaults.standard.bool(forKey: "standbyLearned")
    }

    /// The master switch, one tap from her face (Wispr Flow's own answer
    /// to the same wall - a big toggle, on means warm and lit, off means
    /// dark; owner's screenshots, 2026-08-06). Takes effect at once.
    func setStandby(_ on: Bool) {
        var cfg = SoloStore.shared.config
        var ui = (cfg["ui"] as? [String: Any]) ?? [:]
        ui["standby"] = on
        cfg["ui"] = ui
        SoloStore.shared.config = cfg
        UserDefaults.standard.set(false, forKey: "standbyLearned")
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if on {
                if UIApplication.shared.applicationState == .active {
                    // Arm now: a background start may be refused, and the
                    // whole point of ON is that the next take cannot fail.
                    self.keepAlive?.stop()
                    self.keepAlive = nil
                    self.startStandby()
                }
            } else {
                self.standbyStop?.invalidate()
                self.gate.lock(); let taking = !self.takeID.isEmpty
                self.gate.unlock()
                if !taking, MicDriver.shared.isRunning {
                    MicDriver.shared.stop(keepSession: true)
                }
            }
            NSLog("[viv-ear] ears %@", on ? "warm" : "off")
        }
    }

    var earsWarm: Bool { standbyWanted }

    private func learnStandby() {
        guard !UserDefaults.standard.bool(forKey: "standbyLearned") else {
            return
        }
        UserDefaults.standard.set(true, forKey: "standbyLearned")
        NSLog("[viv-ear] background start refused - warm ears from now on")
    }

    private func startStandby() {
        guard !LiveTap.shared.isLive else { return }
        if !MicDriver.shared.isRunning {
            MicDriver.shared.start(rate: 16000, earOnly: true)
        }
        renewStandby()
        NSLog("[viv-ear] standby armed")
    }

    private func renewStandby() {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.standbyStop?.invalidate()
            self.standbyStop = Timer.scheduledTimer(
                withTimeInterval: Self.standbyWindow,
                repeats: false) { [weak self] _ in
                guard let self,
                      UIApplication.shared.applicationState != .active
                else { return }
                MicDriver.shared.stop(keepSession: true)
                self.keepAlive = nil
                self.startKeepAlive()
                NSLog("[viv-ear] standby released")
            }
        }
    }

    // ------------------------------------------------------ keep-alive

    private var keepAlive: AVAudioPlayer?

    private func startKeepAlive() {
        // An ARMED ear keeps the process alive by itself - and a playback
        // session started over it would kill the very microphone the
        // background is not allowed to reopen. Silence is only for a
        // house whose ear never armed.
        guard keepAlive == nil, !MicDriver.shared.isRunning else { return }
        // RECORD-CAPABLE and mixing, on purpose: iOS lets a backgrounded
        // app start capture only on a session that never went down. The
        // silence keeps this session alive between takes, so the mic can
        // open per take - the indicator lights while you SPEAK and goes
        // dark after, instead of burning all day (owner: "the mic is
        // always on - that can be a really bad bug", 2026-08-06).
        AudioSession.speakAndListen()
        guard let player = try? AVAudioPlayer(
            contentsOf: Self.silenceFile()) else { return }
        player.numberOfLoops = -1
        // Full volume of a -56 dB waveform: real to the system, nothing
        // to the room.
        player.volume = 1
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

    /// One second of a 40 Hz whisper at -56 dB - INAUDIBLE, but not
    /// silent. Pure zeros at volume zero taught us why that matters:
    /// iOS detects inaudible playback and suspends the app anyway, and a
    /// suspended app cannot hear the keyboard's knock - "works in
    /// Vivieen, can't reach her from Notes" (owner, 2026-08-06). A real
    /// waveform below the floor of hearing keeps the process honestly
    /// alive without a sound in the room.
    private static func silenceFile() -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("viv-keepalive-tone2.wav")
        if FileManager.default.fileExists(atPath: url.path) { return url }
        let rate = 8000
        var body = Data(capacity: rate * 2)
        for i in 0..<rate {
            // -38 dB at 40 Hz: the -56 dB whisper still read as
            // "inaudible" to the suspension detector and she kept
            // falling asleep mid-afternoon (owner, 2026-08-06). A phone
            // speaker physically cannot reproduce 40 Hz at ANY level, so
            // this stays silent in the room while the system sees
            // healthy, unmistakably real audio.
            var sample = Int16(sin(2 * Double.pi * 40
                * Double(i) / Double(rate)) * 400)
            withUnsafeBytes(of: &sample) { body.append(contentsOf: $0) }
        }
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
            let mine = self.takeID == id
            if mine {
                self.takeID = ""
                self.stream = nil
            }
            self.gate.unlock()
            // The take's microphone goes DOWN with it - the indicator
            // lights only while someone is speaking - but the SESSION
            // stays up (keepSession), because that standing session is
            // the only thing a backgrounded app may start capture on.
            if mine {
                self.lastLevel = 0
                self.islandPush(listening: false, settled: settled,
                                force: true)
                if self.standbyWanted {
                    // Warm ears: the capture keeps rolling and the take
                    // renews its ten-minute lease.
                    self.renewStandby()
                } else {
                    MicDriver.shared.stop(keepSession: true)
                    // The take's session dance can stop the keep-alive
                    // player; a backgrounded house must resume breathing
                    // or the NEXT knock lands on a suspended app.
                    DispatchQueue.main.async { [weak self] in
                        guard let self,
                              UIApplication.shared.applicationState
                                != .active else { return }
                        self.keepAlive?.stop()
                        self.keepAlive = nil
                        self.startKeepAlive()
                    }
                }
            }
        }
        gate.lock()
        takeID = id
        stream = live
        gate.unlock()
        live.start()
        if !MicDriver.shared.isRunning {
            MicDriver.shared.start(rate: 16000, earOnly: true)
        }
        // A take that draws no frame in a second and a half is the
        // background capture ban, not a quiet room - say so rather than
        // streaming silence at Soniox and blaming the provider.
        heardFrame = false
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            guard let self, !self.heardFrame else { return }
            self.gate.lock(); let still = self.takeID == id; self.gate.unlock()
            guard still else { return }
            // This device will not open a microphone from the background
            // on demand. Remember it, and say what changes.
            self.learnStandby()
            suite.set("error the microphone stayed shut — open Vivieen "
                      + "once and she will keep her ears warm",
                      forKey: "take.state")
            suite.synchronize()
            self.end(id)
        }
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
