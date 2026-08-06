/// Vivieen Keys - her ears as a keyboard, in every app on the phone.
///
/// The same road solo push-to-talk already drives, STREAMED: hold the
/// big key and the socket to Soniox realtime opens at once, the mic's
/// PCM flows while you speak, and the words appear AT THE CURSOR as you
/// say them - refining text rides as iOS marked text (underlined),
/// settled text is committed behind it. Release, and the tail finalises.
/// The Mac is never involved. Wispr Flow's trick, on her stack.
///
/// What it needs from the owner, once each:
///   Settings > General > Keyboard > Keyboards > Add > Vivieen Keys,
///   then Allow Full Access (that switch is what grants an extension
///   the network - without it Soniox is unreachable and the key says so),
///   and the microphone consent on first hold.
///
/// The Soniox key, model, and language arrive through the shared App
/// Group container, mirrored there by the app on every launch and sync -
/// this process cannot read the app's keychain items, and should not:
/// one key, scoped to one purpose.
import UIKit
import AVFoundation

/// The listening line: bars that rise and fall with the voice actually
/// arriving. Not a decorative animation on a timer - it is fed the same
/// RMS the microphone tap computes, so a silent room reads flat and the
/// owner can SEE that she is being heard (owner: "it should respond with
/// a dynamic wave hint while speaking", 2026-08-05).
final class WaveView: UIView {
    var bar: UIColor = .label { didSet { setNeedsDisplay() } }
    private var levels = [CGFloat](repeating: 0, count: 27)

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .clear
        isUserInteractionEnabled = false
    }
    required init?(coder: NSCoder) { fatalError() }

    /// One new sample at the right, everything else slides left.
    func push(_ level: CGFloat) {
        levels.removeFirst()
        levels.append(max(0, min(1, level)))
        setNeedsDisplay()
    }

    func rest() {
        levels = [CGFloat](repeating: 0, count: levels.count)
        setNeedsDisplay()
    }

    override func draw(_ rect: CGRect) {
        guard let context = UIGraphicsGetCurrentContext() else { return }
        let count = CGFloat(levels.count)
        let width: CGFloat = 3
        let gap = (rect.width - count * width) / max(1, count - 1)
        context.setFillColor(bar.cgColor)
        for (i, level) in levels.enumerated() {
            // A floor, so the line reads as a line even in silence.
            let scaled = 3 + pow(level, 0.6) * (rect.height - 3)
            let x = CGFloat(i) * (width + gap)
            let y = (rect.height - scaled) / 2
            let bubble = UIBezierPath(
                roundedRect: CGRect(x: x, y: y, width: width, height: scaled),
                cornerRadius: width / 2)
            context.setAlpha(0.25 + 0.75 * level)
            context.addPath(bubble.cgPath)
            context.fillPath()
        }
    }
}

final class KeyboardViewController: UIInputViewController,
                                    AVCaptureAudioDataOutputSampleBufferDelegate {
    /// Apple's own requirements for a keyboard that RECORDS (an Apple
    /// engineer, developer forums thread 775077, 2025): open access in the
    /// Info.plist, the keyboard enabled by the owner - and this override,
    /// declaring a dictation key. Without it the device refuses the audio
    /// session at the audio-unit level with 'what' (2003329396) no matter
    /// how many times we retry; the simulator never enforced it, which is
    /// why every take worked there and none worked on the phone.
    override var hasDictationKey: Bool {
        get { true }
        set {}
    }

    private let talk = UIButton(type: .system)
    private let status = UILabel()
    private let heard = UILabel()
    private let wave = WaveView()
    private let clock = UILabel()
    private var takeStart: Date?
    private var clockTimer: Timer?
    // The take's capture session and the queue its buffers arrive on.
    private var capture: AVCaptureSession?
    private let captureQueue = DispatchQueue(label: "com.vivieen.keys.mic")
    // The line's own testimony, painted faintly while recording: the
    // format the device actually delivers and the level actually heard -
    // a flat wave then answers "which" instead of "why" (2026-08-06).
    private var lastRms: Float = 0
    private var lastFormat = ""
    private var stream: SonioxStream?
    private var recording = false
    // True from touch-down to release: an activation retry must die the
    // moment the finger lifts, or a slow retry would start a take with
    // nobody holding the key.
    private var holdActive = false
    private var wireRate: Double = 16000
    // What insertText has already committed this take, so each provider
    // message only appends the newly-settled delta.
    private var committed = ""
    private var glue = ""

    private var shared: UserDefaults? {
        UserDefaults(suiteName: "group.com.vivieen.pocket")
    }

    // ---------------------------------------------------------- the palette
    //
    // The same house Vivieen lives in: neutral surfaces, one restrained
    // accent, and colour kept for things that MEAN something - red while
    // recording, and nothing else. The first draft was a slab of black
    // with a bright blue button in it, which belonged to no app at all
    // (owner: "ugly aesthetic ui", 2026-08-05). These are the page's own
    // tokens, hand-carried into UIKit, and they follow light and dark.
    private var dark: Bool { traitCollection.userInterfaceStyle == .dark }
    private var skin: UIColor {                     // the keyboard bed
        dark ? UIColor(red: 0.043, green: 0.051, blue: 0.063, alpha: 1)
             : UIColor(red: 0.969, green: 0.969, blue: 0.961, alpha: 1)
    }
    private var ink: UIColor {                      // primary text
        dark ? UIColor(red: 0.945, green: 0.941, blue: 0.933, alpha: 1)
             : UIColor(red: 0.216, green: 0.208, blue: 0.184, alpha: 1)
    }
    private var faint: UIColor {                    // secondary text
        dark ? UIColor(red: 0.667, green: 0.690, blue: 0.733, alpha: 1)
             : UIColor(red: 0.471, green: 0.467, blue: 0.455, alpha: 1)
    }
    private var tint: UIColor {                     // key fill
        dark ? UIColor(white: 1, alpha: 0.08) : UIColor(white: 0.216, alpha: 0.055)
    }
    private var tintLine: UIColor {                 // key edge
        dark ? UIColor(white: 1, alpha: 0.18) : UIColor(white: 0.216, alpha: 0.14)
    }
    private let hot = UIColor(red: 0.886, green: 0.282, blue: 0.227, alpha: 1)

    // ------------------------------------------------------------- layout

    override func viewDidLoad() {
        super.viewDidLoad()
        paintSurfaces()

        talk.setTitle("Tap or hold to speak", for: .normal)
        talk.titleLabel?.font = .systemFont(ofSize: 16, weight: .semibold)
        talk.layer.cornerRadius = 22
        talk.layer.borderWidth = 1
        talk.addTarget(self, action: #selector(holdBegan),
                       for: .touchDown)
        talk.addTarget(self, action: #selector(holdEnded),
                       for: [.touchUpInside, .touchUpOutside, .touchCancel])

        status.font = .systemFont(ofSize: 12)
        status.textAlignment = .center
        status.adjustsFontSizeToFitWidth = true
        status.minimumScaleFactor = 0.85
        status.numberOfLines = 2

        // The live transcript, in her own voice-of-text: what has settled
        // reads solid, what is still being decided reads faint, and it
        // scrolls with the words rather than growing the keyboard.
        heard.font = .systemFont(ofSize: 14)
        heard.textAlignment = .center
        heard.numberOfLines = 1
        heard.lineBreakMode = .byTruncatingHead
        heard.isHidden = true

        // Fluid's lesson, minimalism as a feature: ONE pill. The voice is
        // drawn in the middle of the key itself, the take's length sits on
        // the left, and there is nothing else on the board (owner, "keep it
        // very simple", 2026-08-05). Space, return, backspace, globe - all
        // gone; the system draws its own switcher under the board on
        // modern phones, and a conditional globe covers the rest.
        clock.font = .monospacedDigitSystemFont(ofSize: 13, weight: .semibold)
        clock.isHidden = true
        wave.isHidden = true
        for widget in [wave, clock] as [UIView] {
            widget.translatesAutoresizingMaskIntoConstraints = false
            widget.isUserInteractionEnabled = false
            talk.addSubview(widget)
        }

        let stack = UIStackView(arrangedSubviews: [status, heard, talk])
        stack.axis = .vertical
        stack.spacing = 8
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor,
                                           constant: 10),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor,
                                            constant: -10),
            stack.topAnchor.constraint(equalTo: view.topAnchor, constant: 10),
            stack.bottomAnchor.constraint(
                equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -8),
            talk.heightAnchor.constraint(equalToConstant: 96),
            wave.centerXAnchor.constraint(equalTo: talk.centerXAnchor),
            wave.centerYAnchor.constraint(equalTo: talk.centerYAnchor),
            wave.widthAnchor.constraint(equalToConstant: 190),
            wave.heightAnchor.constraint(equalToConstant: 30),
            clock.leadingAnchor.constraint(equalTo: talk.leadingAnchor,
                                           constant: 18),
            clock.centerYAnchor.constraint(equalTo: talk.centerYAnchor),
            view.heightAnchor.constraint(greaterThanOrEqualToConstant: 170),
        ])
        if needsInputModeSwitchKey {
            let globe = UIButton(type: .system)
            globe.setTitle("🌐", for: .normal)
            globe.titleLabel?.font = .systemFont(ofSize: 15)
            globe.addTarget(self, action:
                #selector(handleInputModeList(from:with:)),
                for: .allTouchEvents)
            globe.translatesAutoresizingMaskIntoConstraints = false
            view.addSubview(globe)
            NSLayoutConstraint.activate([
                globe.leadingAnchor.constraint(equalTo: view.leadingAnchor,
                                               constant: 12),
                globe.bottomAnchor.constraint(
                    equalTo: view.safeAreaLayoutGuide.bottomAnchor,
                    constant: -6),
            ])
        }
        sayReadiness()
    }

    /// Repaint everything the palette touches. Called on load and again
    /// whenever iOS flips light/dark under us.
    private func paintSurfaces() {
        view.backgroundColor = skin
        status.textColor = faint
        heard.textColor = ink
        wave.bar = ink
        talk.backgroundColor = recording ? hot : tint
        talk.layer.borderColor = (recording ? hot : tintLine).cgColor
        talk.setTitleColor(recording ? .white : ink, for: .normal)
        wave.bar = recording ? .white : ink
        clock.textColor = recording ? .white : faint
    }

    override func traitCollectionDidChange(_ previous: UITraitCollection?) {
        super.traitCollectionDidChange(previous)
        if previous?.userInterfaceStyle != traitCollection.userInterfaceStyle {
            paintSurfaces()
        }
    }

    private func sayReadiness() {
        heard.isHidden = true
        if !hasFullAccess {
            status.text = "Switch on Allow Full Access for Vivieen Keys "
                + "(Settings › General › Keyboard › Keyboards) — without it "
                + "an extension cannot reach the network."
        } else if (shared?.string(forKey: "keys.soniox") ?? "").isEmpty {
            // Do NOT say "open Vivieen once" - the owner did, repeatedly,
            // and it changed nothing (2026-08-05). The shared container is
            // the thing that is missing, and only the App Group entitlement
            // can open it.
            status.text = "The app cannot hand its ears over yet — this "
                + "build has no shared container. Vivieen Keys needs the "
                + "App Group enabled on the developer account."
        } else {
            status.text = "Tap to talk · or hold, release to finish"
        }
    }

    // ------------------------------------------------------------- take

    /// One key, two grips. A quick TAP starts a take that keeps rolling -
    /// the next tap ends it. A HOLD is push-to-talk: release to finish.
    /// Nobody reads a manual on a keyboard, so the key itself says only
    /// what the CURRENT state needs: "Tap to talk · or hold", then
    /// "tap to end" while a tapped take rolls (owner, 2026-08-05).
    private var pressStart: Date?
    private var swallowRelease = false
    private static let tapWindow: TimeInterval = 0.3

    @objc private func holdBegan() {
        // Pressing a rolling take is the tap that ENDS it; the release
        // that follows belongs to this press and must not re-finish.
        if recording || capture != nil {
            swallowRelease = true
            finishTake()
            return
        }
        guard hasFullAccess,
              !(shared?.string(forKey: "keys.soniox") ?? "").isEmpty else {
            sayReadiness()
            return
        }
        holdActive = true
        pressStart = Date()
        AVAudioApplication.requestRecordPermission { [weak self] granted in
            DispatchQueue.main.async {
                guard let self else { return }
                guard granted else {
                    self.status.text = "Microphone is off for keyboards — "
                        + "Settings > Privacy > Microphone"
                    return
                }
                self.beginRecording()
            }
        }
    }

    private func beginRecording() {
        guard !recording, holdActive, capture == nil else { return }
        lastRms = 0
        lastFormat = ""

        // Space-join like dictation does: no leading space at a fresh
        // field or after whitespace. Decided once, when the take begins.
        let before = textDocumentProxy.documentContextBeforeInput ?? ""
        glue = before.isEmpty || before.hasSuffix(" ")
            || before.hasSuffix("\n") ? "" : " "
        committed = ""

        // AVCaptureSession, not AVAudioEngine. The engine needs the shared
        // AVAudioSession activated, and on a real phone a keyboard
        // extension's activation is refused at the audio-unit level with
        // 'what' (2003329396) - retried, dictation-key declared, refused
        // all the same (owner's phone, 2026-08-05). A capture session
        // manages its own audio session and is the one road reported to
        // open inside an extension. The simulator allowed the engine,
        // which is why every take worked there and none on the device.
        guard let mic = AVCaptureDevice.default(for: .audio) else {
            status.text = "This device offered no microphone"
            return
        }
        let take = AVCaptureSession()
        do {
            let feed = try AVCaptureDeviceInput(device: mic)
            guard take.canAddInput(feed) else {
                status.text = "The microphone would not open: input refused"
                return
            }
            take.addInput(feed)
        } catch {
            status.text = "The microphone would not open: "
                + error.localizedDescription
            return
        }
        let out = AVCaptureAudioDataOutput()
        guard take.canAddOutput(out) else {
            status.text = "The microphone would not open: output refused"
            return
        }
        out.setSampleBufferDelegate(self, queue: captureQueue)
        take.addOutput(out)
        capture = take
        status.text = "Opening the microphone…"
        captureQueue.async { [weak self] in
            take.startRunning()
            DispatchQueue.main.async {
                guard let self else { return }
                guard self.capture === take else { return }
                guard self.holdActive else {      // released while opening
                    self.captureQueue.async { take.stopRunning() }
                    self.capture = nil
                    return
                }
                guard take.isRunning else {
                    self.capture = nil
                    self.status.text = "The microphone would not open"
                    return
                }
                self.recording = true
                self.wave.rest()
                self.wave.isHidden = false
                self.heard.isHidden = false
                self.heard.text = ""
                self.talk.setTitle("", for: .normal)
                self.takeStart = Date()
                self.clock.text = "0:00.0"
                self.clock.isHidden = false
                self.clockTimer = Timer.scheduledTimer(
                    withTimeInterval: 0.1, repeats: true) { [weak self] _ in
                    guard let self, let start = self.takeStart else { return }
                    let elapsed = Date().timeIntervalSince(start)
                    self.clock.text = String(format: "%d:%04.1f",
                        Int(elapsed) / 60,
                        elapsed.truncatingRemainder(dividingBy: 60))
                    self.status.text = self.lastFormat.isEmpty ? "" :
                        self.lastFormat + String(format: " · lvl %.3f",
                                                 self.lastRms)
                }
                self.paintSurfaces()
                self.status.text = ""
            }
        }
    }

    /// Buffers arrive here on captureQueue. The FIRST one carries the only
    /// trustworthy statement of the device's format, so the Soniox line
    /// opens from it - asking the session up front was the old road's
    /// habit, and the session is exactly what an extension cannot have.
    func captureOutput(_ output: AVCaptureOutput,
                       didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        guard capture != nil,
              let description = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(
                description)?.pointee else { return }
        if stream == nil {
            wireRate = asbd.mSampleRate
            guard let live = SonioxStream(
                apiKey: shared?.string(forKey: "keys.soniox") ?? "",
                model: shared?.string(forKey: "keys.model") ?? "",
                language: shared?.string(forKey: "keys.language") ?? "",
                rate: Int(asbd.mSampleRate)) else {
                DispatchQueue.main.async { [weak self] in
                    self?.status.text = "Could not open the hearing line"
                }
                return
            }
            live.onText = { [weak self] settled, refining in
                self?.paint(settled: settled, refining: refining, done: false)
            }
            live.onDone = { [weak self] settled, error in
                guard let self else { return }
                self.paint(settled: settled, refining: "", done: true)
                if let error { self.status.text = "Soniox: \(error)" }
                else { self.sayReadiness() }
                // The line is spent; the next take must open its own, or
                // the delegate would feed a closed socket forever.
                self.stream = nil
            }
            live.start()
            stream = live
        }
        var lengthAtOffset = 0
        var totalLength = 0
        var pointer: UnsafeMutablePointer<CChar>?
        guard let block = CMSampleBufferGetDataBuffer(sampleBuffer),
              CMBlockBufferGetDataPointer(
                block, atOffset: 0, lengthAtOffsetOut: &lengthAtOffset,
                totalLengthOut: &totalLength, dataPointerOut: &pointer)
                == kCMBlockBufferNoErr,
              let bytes = pointer, totalLength > 0 else { return }
        let raw = UnsafeRawPointer(bytes)
        // The ASBD's own byte layout is the authority - deriving the
        // stride from flags guessed wrong on the real phone, and reading
        // Int16 frames as Float32 turns speech into ~1e-39s: a flat wave,
        // silence to Soniox, an empty take (owner's screenshots,
        // 2026-08-06). mBytesPerFrame covers ALL interleaved channels;
        // the first channel of each frame is the one that goes up.
        let isFloat = (asbd.mFormatFlags & kAudioFormatFlagIsFloat) != 0
        let bits = Int(asbd.mBitsPerChannel)
        var stride = Int(asbd.mBytesPerFrame)
        if stride <= 0 {
            stride = max(1, Int(asbd.mChannelsPerFrame)) * max(bits, 16) / 8
        }
        let frames = totalLength / max(1, stride)
        guard frames > 0 else { return }
        // One channel, Int16 little-endian - the shape Soniox is promised.
        var pcm = Data(capacity: frames * 2)
        var sum: Float = 0
        for i in 0..<frames {
            let offset = i * stride
            let value: Float
            if isFloat, bits == 32 {
                value = max(-1, min(1,
                    raw.loadUnaligned(fromByteOffset: offset, as: Float32.self)))
            } else if bits == 32 {
                value = Float(raw.loadUnaligned(
                    fromByteOffset: offset, as: Int32.self)) / 2147483648
            } else {
                value = Float(raw.loadUnaligned(
                    fromByteOffset: offset, as: Int16.self)) / 32768
            }
            sum += value * value
            var sample = Int16(max(-32768, min(32767, value * 32767)))
            withUnsafeBytes(of: &sample) { pcm.append(contentsOf: $0) }
        }
        stream?.feed(pcm)
        // The wave is fed the REAL level, so silence looks like silence.
        // Gained up because speech RMS sits low.
        let rms = sqrt(sum / Float(frames))
        lastRms = rms
        lastFormat = "\(Int(asbd.mSampleRate / 1000)) kHz · "
            + (isFloat ? "f\(bits)" : "i\(bits)")
            + " · ch \(asbd.mChannelsPerFrame)"
        DispatchQueue.main.async { [weak self] in
            self?.wave.push(CGFloat(rms) * 6)
        }
    }

    /// Live painting: settled text is COMMITTED via insertText (only the
    /// newly-settled delta each time), the refining tail rides as marked
    /// text so it can rewrite itself until Soniox settles it.
    private func paint(settled: String, refining: String, done: Bool) {
        let proxy = textDocumentProxy
        if !settled.isEmpty || !refining.isEmpty {
            let delta = String(settled.dropFirst(committed.count))
            if !delta.isEmpty {
                proxy.unmarkText()
                proxy.insertText(committed.isEmpty ? glue + delta : delta)
                committed = settled
            }
        }
        if done {
            proxy.unmarkText()
        } else if !refining.isEmpty {
            proxy.setMarkedText(refining, selectedRange:
                NSRange(location: refining.count, length: 0))
        }
        // The keyboard shows it too. The host field is the real
        // destination, but a glance down at the keys should also prove
        // she is hearing - settled solid, the tail still being decided
        // in grey, the way the desk shows dictation.
        let line = NSMutableAttributedString(
            string: settled, attributes: [.foregroundColor: ink])
        if !refining.isEmpty {
            line.append(NSAttributedString(
                string: refining, attributes: [.foregroundColor: faint]))
        }
        heard.attributedText = line
    }

    @objc private func holdEnded() {
        if swallowRelease { swallowRelease = false; return }
        let quick = pressStart.map {
            Date().timeIntervalSince($0) < Self.tapWindow } ?? false
        pressStart = nil
        // A quick press was a TAP: the take keeps rolling, and the key
        // says how to end it. holdActive stays up so a take still opening
        // is not torn down by its own tap.
        if quick, recording || capture != nil {
            status.text = "tap to end"
            return
        }
        finishTake()
    }

    private func finishTake() {
        holdActive = false
        if let take = capture {
            capture = nil
            captureQueue.async { take.stopRunning() }
        }
        guard recording else {
            // Ended while the line was still opening: nothing was
            // committed, so the socket is cancelled, not finalised.
            stream?.cancel()
            stream = nil
            return
        }
        recording = false
        clockTimer?.invalidate()
        clockTimer = nil
        takeStart = nil
        clock.isHidden = true
        wave.rest()
        wave.isHidden = true
        talk.setTitle("Tap or hold to speak", for: .normal)
        paintSurfaces()
        status.text = "Catching the last of it…"
        // The socket stays open for the tail: Soniox finalises what it
        // heard and onDone commits it.
        stream?.stop()
    }
}
