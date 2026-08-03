import AVFoundation

/// The one place that owns the app's audio route.
///
/// She has two moods and they need different sessions. Speaking is pure
/// playback: loud, through the speaker, ignoring the ring/silent switch.
/// Live talk also records, which on iOS means .playAndRecord - and that
/// category defaults its OUTPUT to the earpiece and competes with WebKit
/// for the session, which is exactly why her voice was silent everywhere
/// (owner, real iPhone 2026-08-03). So: playback by default, record only
/// while the microphone is genuinely open, and back to playback after.
enum AudioSession {
    /// What the OS actually decided, in one line. Lip-sync runs off the
    /// AudioContext clock and keeps time even when nothing reaches the
    /// speaker, so "her mouth moves but I hear nothing" needs the route
    /// itself reported, not inferred (owner, 2026-08-03).
    static func describe() -> String {
        let s = AVAudioSession.sharedInstance()
        let out = s.currentRoute.outputs.first
        return "cat=\(s.category.rawValue.replacingOccurrences(of: "AVAudioSessionCategory", with: ""))"
            + " mode=\(s.mode.rawValue.replacingOccurrences(of: "AVAudioSessionMode", with: ""))"
            + " out=\(out?.portType.rawValue.replacingOccurrences(of: "AVAudioSessionPort", with: "") ?? "none")"
            + " vol=\(String(format: "%.2f", s.outputVolume))"
            + " other=\(s.isOtherAudioPlaying)"
    }

    /// Cheap guard before playing: the session can be deactivated by an
    /// interruption (a call, another app) and nobody tells the page.
    static func ensureActive() {
        let session = AVAudioSession.sharedInstance()
        if session.category != .playback && session.category != .playAndRecord {
            playbackOnly()
        } else {
            try? session.setActive(true)
        }
    }

    static func playbackOnly() {
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .default,
                                 options: [.mixWithOthers])
        try? session.setActive(true)
    }

    /// Live talk: mic in, her voice out - out loud, not at the ear.
    ///
    /// Mode stays .default on purpose. .voiceChat hands the route to the
    /// system's voice-processing pipeline, which ducks everything that is
    /// not its own uplink - and WebKit's WebAudio is exactly that, so her
    /// live-talk replies arrived as text with silence attached while
    /// ordinary chat (plain .playback) was fine (owner, 2026-08-03).
    static func speakAndListen() {
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playAndRecord, mode: .default,
                                 options: [.defaultToSpeaker,
                                           .allowBluetooth,
                                           .allowBluetoothA2DP,
                                           .mixWithOthers])
        try? session.setActive(true)
        // .defaultToSpeaker is a preference; this is the instruction.
        try? session.overrideOutputAudioPort(.speaker)
    }
}
