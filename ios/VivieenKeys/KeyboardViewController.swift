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

final class KeyboardViewController: UIInputViewController {
    private let talk = UIButton(type: .system)
    private let status = UILabel()
    private let heard = UILabel()
    private let wave = WaveView()
    private var utility: [UIButton] = []
    private let engine = AVAudioEngine()
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

        let globe = key("􀆪", #selector(handleInputModeList(from:with:)))
        globe.setTitle("🌐", for: .normal)
        let backspace = key("⌫", #selector(rubOut))
        let space = key("space", #selector(spaceBar))
        let ret = key("return", #selector(returnKey))

        talk.setTitle("Hold to speak", for: .normal)
        talk.titleLabel?.font = .systemFont(ofSize: 16, weight: .semibold)
        talk.layer.cornerRadius = 14
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

        wave.isHidden = true

        let row = UIStackView(arrangedSubviews: [globe, backspace, space, ret])
        row.axis = .horizontal
        row.spacing = 8
        row.distribution = .fillProportionally

        let stack = UIStackView(arrangedSubviews: [status, heard, wave, talk, row])
        stack.axis = .vertical
        stack.spacing = 8
        stack.setCustomSpacing(10, after: wave)
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
            talk.heightAnchor.constraint(equalToConstant: 72),
            wave.heightAnchor.constraint(equalToConstant: 22),
            view.heightAnchor.constraint(greaterThanOrEqualToConstant: 208),
        ])
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
        for case let button as UIButton in utility {
            button.backgroundColor = tint
            button.setTitleColor(ink, for: .normal)
            button.layer.borderColor = tintLine.cgColor
        }
    }

    override func traitCollectionDidChange(_ previous: UITraitCollection?) {
        super.traitCollectionDidChange(previous)
        if previous?.userInterfaceStyle != traitCollection.userInterfaceStyle {
            paintSurfaces()
        }
    }

    private func key(_ title: String, _ action: Selector) -> UIButton {
        let button = UIButton(type: .system)
        button.setTitle(title, for: .normal)
        button.titleLabel?.font = .systemFont(ofSize: 14.5, weight: .medium)
        button.layer.cornerRadius = 9
        button.layer.borderWidth = 1
        button.contentEdgeInsets = UIEdgeInsets(top: 9, left: 14,
                                                bottom: 9, right: 14)
        button.addTarget(self, action: action, for: .touchUpInside)
        utility.append(button)
        return button
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
            status.text = "Hold the key and speak · the words arrive as "
                + "you say them"
        }
    }

    // ------------------------------------------------------------- keys

    @objc private func rubOut() { textDocumentProxy.deleteBackward() }
    @objc private func spaceBar() { textDocumentProxy.insertText(" ") }
    @objc private func returnKey() { textDocumentProxy.insertText("\n") }

    // ------------------------------------------------------------- take

    @objc private func holdBegan() {
        guard hasFullAccess,
              !(shared?.string(forKey: "keys.soniox") ?? "").isEmpty else {
            sayReadiness()
            return
        }
        holdActive = true
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

    private func beginRecording(attempt: Int = 0) {
        guard !recording, holdActive else { return }
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.playAndRecord, mode: .measurement,
                                    options: [.duckOthers])
            try session.setActive(true)
        } catch {
            return retryMicrophone(after: error, attempt: attempt)
        }
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        // A dead line answers 0 Hz: the session LOOKED active but the
        // input never came up. Same lost race, same retry.
        guard format.sampleRate > 0, format.channelCount > 0 else {
            return retryMicrophone(after: nil, attempt: attempt)
        }
        wireRate = format.sampleRate

        // Space-join like dictation does: no leading space at a fresh
        // field or after whitespace. Decided once, when the take begins.
        let before = textDocumentProxy.documentContextBeforeInput ?? ""
        glue = before.isEmpty || before.hasSuffix(" ")
            || before.hasSuffix("\n") ? "" : " "
        committed = ""

        guard let live = SonioxStream(
            apiKey: shared?.string(forKey: "keys.soniox") ?? "",
            model: shared?.string(forKey: "keys.model") ?? "",
            language: shared?.string(forKey: "keys.language") ?? "",
            rate: Int(wireRate)) else {
            status.text = "Could not open the hearing line"
            return
        }
        stream = live
        live.onText = { [weak self] settled, refining in
            self?.paint(settled: settled, refining: refining, done: false)
        }
        live.onDone = { [weak self] settled, error in
            guard let self else { return }
            self.paint(settled: settled, refining: "", done: true)
            if let error { self.status.text = "Soniox: \(error)" }
            else { self.sayReadiness() }
        }
        live.start()

        input.installTap(onBus: 0, bufferSize: 2048, format: format) {
            [weak self] buffer, _ in
            guard let self, let channel = buffer.floatChannelData?[0] else {
                return
            }
            let count = Int(buffer.frameLength)
            var pcm = Data(capacity: count * 2)
            var sum: Float = 0
            for i in 0..<count {
                let clamped = max(-1, min(1, channel[i]))
                sum += clamped * clamped
                var sample = Int16(clamped * 32767)
                withUnsafeBytes(of: &sample) { pcm.append(contentsOf: $0) }
            }
            self.stream?.feed(pcm)
            // The wave is fed the REAL level, so silence looks like
            // silence. Gained up because speech RMS sits low.
            let rms = count > 0 ? sqrt(sum / Float(count)) : 0
            DispatchQueue.main.async { self.wave.push(CGFloat(rms) * 6) }
        }
        do {
            try engine.start()
            recording = true
            wave.rest()
            wave.isHidden = false
            heard.isHidden = false
            heard.text = ""
            talk.setTitle("Listening — release to finish", for: .normal)
            paintSurfaces()
            status.text = "Speak — the words arrive as you say them"
        } catch {
            input.removeTap(onBus: 0)
            stream?.cancel()
            stream = nil
            retryMicrophone(after: error, attempt: attempt)
        }
    }

    /// The host app owns the audio session at the moment the keyboard
    /// rises, and the first activation can lose that race - CoreAudio
    /// answers 'what' (2003329396) and nothing more (owner's phone,
    /// 2026-08-05). The session is usually free a beat later, so try
    /// again while the finger is still down instead of surfacing the
    /// cryptic error on the first miss.
    private func retryMicrophone(after error: Error?, attempt: Int) {
        try? AVAudioSession.sharedInstance().setActive(
            false, options: .notifyOthersOnDeactivation)
        guard attempt < 3 else {
            status.text = "The microphone would not open: "
                + (error?.localizedDescription
                   ?? "the audio line never came up — release and try again")
            return
        }
        status.text = "Opening the microphone…"
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { [weak self] in
            guard let self, self.holdActive, !self.recording else { return }
            self.beginRecording(attempt: attempt + 1)
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
        holdActive = false
        guard recording else { return }
        recording = false
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        try? AVAudioSession.sharedInstance().setActive(
            false, options: .notifyOthersOnDeactivation)
        wave.rest()
        wave.isHidden = true
        talk.setTitle("Hold to speak", for: .normal)
        paintSurfaces()
        status.text = "Catching the last of it…"
        // The socket stays open for the tail: Soniox finalises what it
        // heard and onDone commits it.
        stream?.stop()
    }
}
