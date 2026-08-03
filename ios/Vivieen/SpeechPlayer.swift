import AVFoundation
import WebKit

/// Her voice, played by the app instead of by WebKit.
///
/// WKWebView runs WebAudio on ITS OWN audio session, which respects the
/// ring/silent switch and ignores whatever category the host app sets -
/// so on a real iPhone her replies decoded, scheduled and lip-synced in
/// perfect time while not one sample reached the speaker. The Simulator
/// hid it completely, because it barely enforces sessions at all (owner,
/// 2026-08-03).
///
/// So the page keeps WebAudio purely as a CLOCK - it still drives the
/// mouth - and hands the actual samples here, where the app's own
/// .playback session decides the route and the mute switch has no vote.
final class SpeechPlayer: NSObject {
    /// One voice, shared - live talk plays through the same engine.
    static let shared = SpeechPlayer()
    private let engine = AVAudioEngine()
    private let node = AVAudioPlayerNode()
    private var wired = false
    /// Live talk streams at whatever rate the provider chose; a change
    /// means the graph has to be rebuilt around the new format.
    private var format: AVAudioFormat?

    private func ensure(rate: Double) {
        if wired, let format, format.sampleRate == rate { return }
        if wired {
            node.stop()
            engine.stop()
            engine.detach(node)
            wired = false
        }
        guard let fresh = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                        sampleRate: rate, channels: 1,
                                        interleaved: false) else { return }
        format = fresh
        engine.attach(node)
        engine.connect(node, to: engine.mainMixerNode, format: fresh)
        engine.prepare()
        do {
            try engine.start()
            node.play()
            wired = true
        } catch {
            NSLog("[viv-speech] engine failed: %@", error.localizedDescription)
        }
    }

    /// Signed 16-bit little-endian mono, base64 - the wire format both
    /// her turn-based replies and live talk already speak.
    func enqueue(base64: String, rate: Double) {
        guard let data = Data(base64Encoded: base64), !data.isEmpty else { return }
        AudioSession.ensureActive()
        ensure(rate: rate)
        guard let format, wired else { return }
        let frames = data.count / 2
        guard frames > 0,
              let buffer = AVAudioPCMBuffer(pcmFormat: format,
                                            frameCapacity: AVAudioFrameCount(frames)),
              let channel = buffer.floatChannelData?[0] else { return }
        buffer.frameLength = AVAudioFrameCount(frames)
        data.withUnsafeBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            for index in 0..<frames {
                channel[index] = Float(Int16(littleEndian: samples[index])) / 32768.0
            }
        }
        node.scheduleBuffer(buffer, completionHandler: nil)
    }

    /// Barge-in: drop whatever is still queued.
    func flush() {
        guard wired else { return }
        node.stop()
        node.play()
    }

    func stop() {
        guard wired else { return }
        node.stop()
        engine.stop()
        engine.detach(node)
        wired = false
        format = nil
    }
}
