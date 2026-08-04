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

final class KeyboardViewController: UIInputViewController {
    private let talk = UIButton(type: .system)
    private let status = UILabel()
    private let engine = AVAudioEngine()
    private var stream: SonioxStream?
    private var recording = false
    private var wireRate: Double = 16000
    // What insertText has already committed this take, so each provider
    // message only appends the newly-settled delta.
    private var committed = ""
    private var glue = ""

    private var shared: UserDefaults? {
        UserDefaults(suiteName: "group.com.vivieen.pocket")
    }

    // ------------------------------------------------------------- layout

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(white: 0.09, alpha: 1)

        let globe = UIButton(type: .system)
        globe.setTitle("🌐", for: .normal)
        globe.addTarget(self, action: #selector(handleInputModeList(from:with:)),
                        for: .allTouchEvents)

        let backspace = key("⌫", #selector(rubOut))
        let space = key("space", #selector(spaceBar))
        let ret = key("return", #selector(returnKey))

        talk.setTitle("Hold — Vivieen is listening", for: .normal)
        talk.setTitleColor(.white, for: .normal)
        talk.titleLabel?.font = .systemFont(ofSize: 17, weight: .semibold)
        talk.backgroundColor = UIColor(red: 0.16, green: 0.32, blue: 0.65,
                                       alpha: 1)
        talk.layer.cornerRadius = 12
        talk.addTarget(self, action: #selector(holdBegan),
                       for: .touchDown)
        talk.addTarget(self, action: #selector(holdEnded),
                       for: [.touchUpInside, .touchUpOutside, .touchCancel])

        status.font = .systemFont(ofSize: 12)
        status.textColor = UIColor(white: 0.65, alpha: 1)
        status.textAlignment = .center
        status.adjustsFontSizeToFitWidth = true

        let row = UIStackView(arrangedSubviews: [globe, backspace, space, ret])
        row.axis = .horizontal
        row.spacing = 8
        row.distribution = .fillProportionally

        let stack = UIStackView(arrangedSubviews: [status, talk, row])
        stack.axis = .vertical
        stack.spacing = 8
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor,
                                           constant: 10),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor,
                                            constant: -10),
            stack.topAnchor.constraint(equalTo: view.topAnchor, constant: 8),
            stack.bottomAnchor.constraint(
                equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -8),
            talk.heightAnchor.constraint(equalToConstant: 74),
            view.heightAnchor.constraint(greaterThanOrEqualToConstant: 170),
        ])
        sayReadiness()
    }

    private func key(_ title: String, _ action: Selector) -> UIButton {
        let button = UIButton(type: .system)
        button.setTitle(title, for: .normal)
        button.setTitleColor(.white, for: .normal)
        button.backgroundColor = UIColor(white: 0.2, alpha: 1)
        button.layer.cornerRadius = 8
        button.contentEdgeInsets = UIEdgeInsets(top: 8, left: 14,
                                                bottom: 8, right: 14)
        button.addTarget(self, action: action, for: .touchUpInside)
        return button
    }

    private func sayReadiness() {
        if !hasFullAccess {
            status.text = "Turn on Allow Full Access for Vivieen Keys "
                + "(Settings > General > Keyboard) so the ears can reach "
                + "the network"
        } else if (shared?.string(forKey: "keys.soniox") ?? "").isEmpty {
            status.text = "No hearing key yet — open Vivieen once so it "
                + "can hand its ears to the keyboard"
        } else {
            status.text = "Soniox realtime · straight from this phone"
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
        guard !recording else { return }
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playAndRecord, mode: .measurement,
                                 options: [.duckOthers])
        try? session.setActive(true)
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
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
            for i in 0..<count {
                let clamped = max(-1, min(1, channel[i]))
                var sample = Int16(clamped * 32767)
                withUnsafeBytes(of: &sample) { pcm.append(contentsOf: $0) }
            }
            self.stream?.feed(pcm)
        }
        do {
            try engine.start()
            recording = true
            talk.backgroundColor = UIColor(red: 0.72, green: 0.2,
                                           blue: 0.24, alpha: 1)
            status.text = "Listening — the words land as you speak"
        } catch {
            stream?.cancel()
            stream = nil
            status.text = "Microphone would not open: "
                + error.localizedDescription
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
    }

    @objc private func holdEnded() {
        guard recording else { return }
        recording = false
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        try? AVAudioSession.sharedInstance().setActive(
            false, options: .notifyOthersOnDeactivation)
        talk.backgroundColor = UIColor(red: 0.16, green: 0.32, blue: 0.65,
                                       alpha: 1)
        status.text = "Settling the tail…"
        // The socket stays open for the tail: Soniox finalises what it
        // heard and onDone commits it.
        stream?.stop()
    }
}
