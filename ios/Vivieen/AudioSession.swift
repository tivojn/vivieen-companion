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
