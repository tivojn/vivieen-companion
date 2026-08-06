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
    private var levels = [CGFloat](repeating: 0, count: 48)

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
    /// A keyboard extension is NEVER handed the microphone. The engine,
    /// the dictation-key declaration and AVCaptureSession all "ran" in
    /// silence on the owner's phone (2026-08-06) - the sandbox lets a
    /// session start and never feeds it a frame, and only the simulator
    /// pretends otherwise. So this keyboard is a TRIGGER: it knocks on
    /// the Darwin door, the APP records and streams Soniox (KeyboardEar),
    /// and the words come back through the App Group to land at the
    /// cursor. Wispr Flow's actual trick, on her stack.
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
    // The relay take: its id, the poll that reads the app's answers, and
    // the moment we knocked (an unanswered knock means the app is not
    // alive to hear - say so instead of spinning).
    private var takeID = ""
    private var poll: Timer?
    private var askedAt = Date.distantPast
    private var finishing = false
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

        // No button. The WAVE is the surface (owner: "just leave a
        // dynamic wave shape, lengthwise, put your note along with the
        // waves", 2026-08-06): a full-width line of bars, the hint
        // riding quietly above it, and the whole field is the touch
        // target.
        talk.setTitle("Tap or hold to speak", for: .normal)
        talk.titleLabel?.font = .systemFont(ofSize: 12.5, weight: .medium)
        talk.layer.cornerRadius = 0
        talk.layer.borderWidth = 0
        talk.backgroundColor = .clear
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
        talk.contentVerticalAlignment = .top
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
            talk.heightAnchor.constraint(equalToConstant: 64),
            wave.leadingAnchor.constraint(equalTo: talk.leadingAnchor,
                                          constant: 12),
            wave.trailingAnchor.constraint(equalTo: talk.trailingAnchor,
                                           constant: -12),
            wave.bottomAnchor.constraint(equalTo: talk.bottomAnchor,
                                         constant: -6),
            wave.heightAnchor.constraint(equalToConstant: 30),
            clock.leadingAnchor.constraint(equalTo: talk.leadingAnchor,
                                           constant: 14),
            clock.centerYAnchor.constraint(equalTo: wave.centerYAnchor),
            view.heightAnchor.constraint(greaterThanOrEqualToConstant: 140),
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
        talk.setTitleColor(faint, for: .normal)
        wave.bar = recording ? hot : faint
        clock.textColor = recording ? hot : faint
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
            // The build stamps itself: a week of "did the update take?"
            // screenshots, answered by the corner of the keyboard
            // (2026-08-06).
            let build = Bundle.main.object(
                forInfoDictionaryKey: "CFBundleVersion") as? String ?? "?"
            status.text = "Tap to talk · or hold, release to finish · b\(build)"
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
        if recording || !takeID.isEmpty {
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
        beginRecording()
    }

    private func beginRecording() {
        guard !recording, holdActive, takeID.isEmpty else { return }

        // Space-join like dictation does: no leading space at a fresh
        // field or after whitespace. Decided once, when the take begins.
        let before = textDocumentProxy.documentContextBeforeInput ?? ""
        glue = before.isEmpty || before.hasSuffix(" ")
            || before.hasSuffix("\n") ? "" : " "
        committed = ""
        finishing = false

        // Knock: the APP records (KeyboardEar). This process only asks,
        // paints, and inserts.
        let id = UUID().uuidString
        takeID = id
        shared?.set("", forKey: "take.settled")
        shared?.set("", forKey: "take.refining")
        shared?.set("", forKey: "take.state")
        shared?.set(0.0, forKey: "take.level")
        shared?.set("start \(id)", forKey: "take.cmd")
        // Flush before knocking: the notification crosses processes
        // faster than an unsynchronised default does, and the app would
        // read the PREVIOUS command.
        shared?.synchronize()
        knock()
        askedAt = Date()

        recording = true
        wave.rest()
        heard.isHidden = false
        heard.text = ""
        talk.setTitle("", for: .normal)
        takeStart = Date()
        clock.text = "0:00.0"
        clock.isHidden = false
        clockTimer = Timer.scheduledTimer(
            withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self, let start = self.takeStart else { return }
            let elapsed = Date().timeIntervalSince(start)
            self.clock.text = String(format: "%d:%04.1f",
                Int(elapsed) / 60,
                elapsed.truncatingRemainder(dividingBy: 60))
        }
        paintSurfaces()
        status.text = ""
        poll = Timer.scheduledTimer(withTimeInterval: 0.08,
                                    repeats: true) { [weak self] _ in
            self?.follow()
        }
    }

    private func knock() {
        CFNotificationCenterPostNotification(
            CFNotificationCenterGetDarwinNotifyCenter(),
            CFNotificationName("com.vivieen.pocket.keys.knock" as CFString),
            nil, nil, true)
    }

    /// The poll: read what the app has heard and paint it. Runs from the
    /// knock until the take lands or nobody answers.
    private func follow() {
        guard let shared, !takeID.isEmpty else { return }
        guard shared.string(forKey: "take.id") == takeID else {
            // Nobody has answered yet. Past a beat and a half, nobody
            // will: the app is not running, and only it may record.
            if Date().timeIntervalSince(askedAt) > 1.5 {
                landTake(message: "Open Vivieen once — she hears through "
                                + "the app")
            }
            return
        }
        let state = shared.string(forKey: "take.state") ?? ""
        let settled = shared.string(forKey: "take.settled") ?? ""
        let refining = shared.string(forKey: "take.refining") ?? ""
        if state == "listening" || state == "done" {
            paint(settled: settled, refining: state == "done" ? "" : refining,
                  done: state == "done")
            wave.push(CGFloat(shared.double(forKey: "take.level")) * 6)
        }
        if state == "done" {
            landTake(message: nil)
        } else if state.hasPrefix("error") {
            landTake(message: String(state.dropFirst("error ".count)))
        } else if finishing,
                  Date().timeIntervalSince(askedAt) > 8 {
            // The app answered once but the finalise never landed - keep
            // what was committed rather than spinning forever.
            landTake(message: nil)
        }
    }

    /// Every take ends here exactly once: UI down, poll down, verdict up.
    private func landTake(message: String?) {
        takeID = ""
        finishing = false
        poll?.invalidate()
        poll = nil
        recording = false
        clockTimer?.invalidate()
        clockTimer = nil
        takeStart = nil
        clock.isHidden = true
        wave.rest()
        talk.setTitle("Tap or hold to speak", for: .normal)
        paintSurfaces()
        if let message { status.text = message } else { sayReadiness() }
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
        if quick, recording || !takeID.isEmpty {
            talk.setTitle("tap to end", for: .normal)
            return
        }
        finishTake()
    }

    private func finishTake() {
        holdActive = false
        guard !takeID.isEmpty else { return }
        // Ask the app to land the tail; the poll carries it home (or the
        // eight-second cap in follow() does, if the finalise never comes).
        shared?.set("stop \(takeID)", forKey: "take.cmd")
        shared?.synchronize()
        knock()
        finishing = true
        askedAt = Date()
        status.text = "Catching the last of it…"
    }
}
