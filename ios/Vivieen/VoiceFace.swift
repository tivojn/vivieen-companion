import Foundation

/// The owner's voice, read as mouth shapes - the same arithmetic the
/// desk's EnConvo tap runs (enconvo_audio_tap.swift) and the same
/// spectral-ratio classifier the page runs (byExternalEnergy), ported so
/// the app can shape a mouth from the dictation stream it is already
/// hearing. Feed Int16 mono frames; read a sample dict for the page and
/// a stabilised viseme for the keyboard's portrait.
final class VoiceFace {
    private let sampleRate: Float
    private let lowCoefficient: Float
    private let midCoefficient: Float
    private var lowState: Float = 0
    private var midState: Float = 0
    private var previousSample: Float = 0
    private var smoothedRMS: Float = 0
    private var smoothedPeak: Float = 0
    private var smoothedLow: Float = 0
    private var smoothedMid: Float = 0
    private var smoothedHigh: Float = 0
    private var smoothedZCR: Float = 0
    private var lastVoice = Date.distantPast
    private var sequence = 0
    // The vote that keeps the mouth from flickering: a candidate must
    // win twice in a row to take the face (the page runs a fuller
    // windowed vote; two-in-a-row reads identically at this frame rate).
    private var shown = "sil"
    private var pending = ""
    private var pendingRuns = 0

    init(sampleRate: Double = 16000) {
        self.sampleRate = Float(sampleRate)
        self.lowCoefficient = Float(1 - exp(-2 * Double.pi * 520 / sampleRate))
        self.midCoefficient = Float(1 - exp(-2 * Double.pi * 2400 / sampleRate))
    }

    struct Sample {
        var rms: Double
        var peak: Double
        var low: Double
        var mid: Double
        var high: Double
        var zcr: Double
        var active: Bool
        var viseme: String
        var sequence: Int

        var json: String {
            "{\"rms\":\(rms),\"peak\":\(peak),\"low\":\(low),"
            + "\"mid\":\(mid),\"high\":\(high),\"zcr\":\(zcr),"
            + "\"active\":\(active),\"sequence\":\(sequence),"
            + "\"timestamp\":\(Int(Date().timeIntervalSince1970 * 1000)),"
            + "\"viseme\":\"\(viseme)\"}"
        }
    }

    func consume(_ pcm: Data) -> Sample {
        let count = pcm.count / 2
        var totalEnergy = 0.0, lowEnergy = 0.0, midEnergy = 0.0
        var highEnergy = 0.0
        var peak: Float = 0
        var crossings = 0
        pcm.withUnsafeBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            for i in 0..<count {
                let sample = Float(Int16(littleEndian: samples[i])) / 32768
                lowState += lowCoefficient * (sample - lowState)
                midState += midCoefficient * (sample - midState)
                let low = lowState
                let mid = midState - lowState
                let high = sample - midState
                totalEnergy += Double(sample * sample)
                lowEnergy += Double(low * low)
                midEnergy += Double(mid * mid)
                highEnergy += Double(high * high)
                peak = max(peak, abs(sample))
                if (sample >= 0) != (previousSample >= 0) { crossings += 1 }
                previousSample = sample
            }
        }
        let divisor = Double(max(1, count))
        let rms = Float(sqrt(totalEnergy / divisor))
        let lowRMS = Float(sqrt(lowEnergy / divisor))
        let midRMS = Float(sqrt(midEnergy / divisor))
        let highRMS = Float(sqrt(highEnergy / divisor))
        let bandTotal = max(0.000001, lowRMS + midRMS + highRMS)
        let zcr = Float(crossings) / Float(max(1, count))

        smoothedRMS = smoothedRMS * 0.58 + rms * 0.42
        smoothedPeak = max(peak, smoothedPeak * 0.72)
        smoothedLow = smoothedLow * 0.55 + (lowRMS / bandTotal) * 0.45
        smoothedMid = smoothedMid * 0.55 + (midRMS / bandTotal) * 0.45
        smoothedHigh = smoothedHigh * 0.55 + (highRMS / bandTotal) * 0.45
        smoothedZCR = smoothedZCR * 0.62 + zcr * 0.38

        if smoothedRMS > 0.003 { lastVoice = Date() }
        let active = Date().timeIntervalSince(lastVoice) < 0.19
        sequence += 1

        let candidate = classify(active: active)
        if candidate == shown {
            pendingRuns = 0
        } else if candidate == pending {
            pendingRuns += 1
            if pendingRuns >= 2 { shown = candidate; pendingRuns = 0 }
        } else {
            pending = candidate
            pendingRuns = 1
        }

        return Sample(rms: Double(smoothedRMS), peak: Double(smoothedPeak),
                      low: Double(smoothedLow), mid: Double(smoothedMid),
                      high: Double(smoothedHigh), zcr: Double(smoothedZCR),
                      active: active, viseme: shown, sequence: sequence)
    }

    /// byExternalEnergy, verbatim in spirit: spectral ratios pick the
    /// shape, loudness only gates silence.
    private func classify(active: Bool) -> String {
        guard active, smoothedRMS >= 0.0025 else { return "sil" }
        let total = smoothedLow + smoothedMid + smoothedHigh + 0.000001
        let low = smoothedLow / total
        let mid = smoothedMid / total
        let high = smoothedHigh / total
        if high > 0.43 || smoothedZCR > 0.16 {
            return high > 0.54 ? "SS" : "CH"
        }
        if low > 0.58 { return mid > 0.24 ? "aa" : "oh" }
        if mid > 0.40 { return high > 0.22 ? "E" : "ih" }
        return low > 0.45 ? "oh" : "ou"
    }
}
