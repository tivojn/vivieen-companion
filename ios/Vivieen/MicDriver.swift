import AVFoundation
import WebKit

/// The live-talk microphone, captured natively. WKWebView's getUserMedia
/// in the Simulator is a MOCK device - a ~155 Hz tone at 10% amplitude,
/// never the real mic - so "she can't hear me" was literally true: she was
/// hearing a hum (vitals probe, 2026-08-03). AVAudioEngine's input node is
/// the real microphone on device AND in the Simulator, so the page asks
/// native for mic frames and keeps its own wire protocol unchanged.
final class MicDriver: NSObject {
    /// One microphone, shared. Live talk needs the SAME device the page
    /// uses, and it needs the frames natively (see LiveTap).
    static let shared = MicDriver()
    /// When set, frames go here INSTEAD of to the page. Live talk must
    /// keep working with the WebView suspended, so the audio path cannot
    /// run through JavaScript (owner, 2026-08-03).
    var onPCM: ((Data) -> Void)?
    /// The keyboard ear's STANDING tap: receives every frame alongside
    /// whoever the primary consumer is. iOS refuses to START a capture
    /// from the background (forums 120038), so the ear keeps this engine
    /// running from the foreground on and only attaches Soniox per take.
    var earTap: ((Data) -> Void)?
    var isRunning: Bool { running }
    static let becameIdle = Notification.Name("viv-mic-idle")
    private let engine = AVAudioEngine()
    private var converter: AVAudioConverter?
    private var running = false
    private var earOnly = false
    weak var webView: WKWebView?

    func start(rate: Double, earOnly wantsEarOnly: Bool = false) {
        stop()
        earOnly = wantsEarOnly
        // Recording needs the record-capable session; forced to the
        // speaker so she is heard across the room, not at the ear.
        AudioSession.speakAndListen()
        let input = engine.inputNode
        // FaceTime's own echo cancellation: with voice processing on, the
        // mic frames arrive with HER speaker output already subtracted,
        // which is what makes real barge-in possible - without it the only
        // defence was zeroing the mic for her whole reply, and the owner
        // could not interrupt a long answer at all (2026-08-06).
        do { try input.setVoiceProcessingEnabled(true) }
        catch { report("mic-aec unavailable \(error.localizedDescription)") }
        var source = input.outputFormat(forBus: 0)
        // A background restart can catch the session mid-handoff - the
        // keyboard's second take found a 0 Hz input where the first found
        // a microphone, and Soniox answered "No audio received" (owner,
        // 2026-08-06). The input usually comes up a beat later: ask
        // again before declaring it unusable.
        var attempt = 0
        while source.sampleRate <= 0, attempt < 4 {
            attempt += 1
            Thread.sleep(forTimeInterval: 0.15)
            AudioSession.speakAndListen()
            source = input.outputFormat(forBus: 0)
        }
        guard source.sampleRate > 0, source.channelCount > 0,
              let wire = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                       sampleRate: rate, channels: 1,
                                       interleaved: true),
              let converter = AVAudioConverter(from: source, to: wire)
        else {
            report("mic-error unusable input \(source.sampleRate)")
            return
        }
        self.converter = converter
        // ~100ms per tap buffer: the page batches to that cadence anyway.
        input.installTap(onBus: 0,
                         bufferSize: AVAudioFrameCount(source.sampleRate / 10),
                         format: source) { [weak self] buffer, _ in
            self?.pump(buffer: buffer, wire: wire, rate: rate)
        }
        do {
            try engine.start()
            running = true
            report("mic-started \(source.sampleRate)->\(rate)")
        } catch {
            report("mic-error \(error.localizedDescription)")
        }
    }

    private func pump(buffer: AVAudioPCMBuffer, wire: AVAudioFormat,
                      rate: Double) {
        guard let converter else { return }
        let frames = AVAudioFrameCount(
            Double(buffer.frameLength) * rate
                / max(1, buffer.format.sampleRate)) + 16
        guard let out = AVAudioPCMBuffer(pcmFormat: wire,
                                         frameCapacity: frames) else { return }
        var fed = false
        var conversionError: NSError?
        converter.convert(to: out, error: &conversionError) { _, status in
            if fed || buffer.frameLength == 0 {
                status.pointee = .noDataNow
                return nil
            }
            fed = true
            status.pointee = .haveData
            return buffer
        }
        guard conversionError == nil, out.frameLength > 0,
              let pcm = out.int16ChannelData else { return }
        let data = Data(bytes: pcm[0], count: Int(out.frameLength) * 2)
        earTap?(data)
        // Standing duty for the ear alone: nobody else asked, so the
        // page must not be fed frames it has no take for.
        if earOnly { return }
        if let sink = onPCM { sink(data); return }
        let chunk = data.base64EncodedString()
        DispatchQueue.main.async { [weak self] in
            self?.webView?.evaluateJavaScript(
                "window.__vivMicData&&__vivMicData('\(chunk)')",
                completionHandler: nil)
        }
    }

    func stop() {
        guard running || engine.isRunning else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        converter = nil
        running = false
        earOnly = false
        // Hand the route back to plain playback so her replies stay loud.
        AudioSession.playbackOnly()
        // Whoever keeps a standing tap may want the line back.
        NotificationCenter.default.post(name: Self.becameIdle, object: nil)
    }

    private func report(_ line: String) {
        NSLog("[viv-mic] %@", line)
        DispatchQueue.main.async { [weak self] in
            self?.webView?.evaluateJavaScript(
                "window.__vivMicNote&&__vivMicNote('\(line)')",
                completionHandler: nil)
        }
    }
}
