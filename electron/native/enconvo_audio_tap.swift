import AppKit
import AudioToolbox
import AVFAudio
import CoreGraphics
import Foundation

extension String: @retroactive LocalizedError {
    public var errorDescription: String? { self }
}

extension AudioObjectID {
    static let system = AudioObjectID(kAudioObjectSystemObject)
    static let unknown = AudioObjectID(kAudioObjectUnknown)

    var isValid: Bool { self != .unknown }

    func readValue<T>(
        _ selector: AudioObjectPropertySelector,
        scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
        element: AudioObjectPropertyElement = kAudioObjectPropertyElementMain,
        defaultValue: T
    ) throws -> T {
        var address = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: scope,
            mElement: element
        )
        var dataSize: UInt32 = 0
        var status = AudioObjectGetPropertyDataSize(self, &address, 0, nil, &dataSize)
        guard status == noErr else {
            throw "Unable to read Core Audio property size \(selector): \(status)"
        }
        var value = defaultValue
        status = withUnsafeMutablePointer(to: &value) { pointer in
            AudioObjectGetPropertyData(self, &address, 0, nil, &dataSize, pointer)
        }
        guard status == noErr else {
            throw "Unable to read Core Audio property \(selector): \(status)"
        }
        return value
    }

    func readString(_ selector: AudioObjectPropertySelector) throws -> String {
        let value: CFString = try readValue(selector, defaultValue: "" as CFString)
        return value as String
    }
}

struct TapFailure: Error, LocalizedError {
    let code: String
    let message: String

    var errorDescription: String? { message }
}

final class JsonEmitter: @unchecked Sendable {
    private let output = FileHandle.standardOutput
    private let lock = NSLock()

    func send(_ value: [String: Any]) {
        guard JSONSerialization.isValidJSONObject(value),
              let data = try? JSONSerialization.data(withJSONObject: value) else { return }
        lock.lock()
        output.write(data)
        output.write(Data([0x0A]))
        lock.unlock()
    }
}

func audioProcessObjectIDs() throws -> [AudioObjectID] {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyProcessObjectList,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var dataSize: UInt32 = 0
    var status = AudioObjectGetPropertyDataSize(.system, &address, 0, nil, &dataSize)
    guard status == noErr else {
        throw TapFailure(code: "process_list_failed", message: "Unable to inspect audio processes: \(status)")
    }
    var values = [AudioObjectID](
        repeating: .unknown,
        count: Int(dataSize) / MemoryLayout<AudioObjectID>.size
    )
    status = AudioObjectGetPropertyData(.system, &address, 0, nil, &dataSize, &values)
    guard status == noErr else {
        throw TapFailure(code: "process_list_failed", message: "Unable to read audio processes: \(status)")
    }
    return values
}

func translatePID(_ pid: pid_t) throws -> AudioObjectID {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var qualifier = pid
    var result = AudioObjectID.unknown
    var dataSize = UInt32(MemoryLayout<AudioObjectID>.size)
    let status = withUnsafePointer(to: &qualifier) { qualifierPointer in
        AudioObjectGetPropertyData(
            .system,
            &address,
            UInt32(MemoryLayout<pid_t>.size),
            qualifierPointer,
            &dataSize,
            &result
        )
    }
    guard status == noErr, result.isValid else {
        throw TapFailure(code: "process_translation_failed", message: "Unable to resolve EnConvo audio process: \(status)")
    }
    return result
}

func processPath(_ pid: pid_t) -> String {
    var bytes = [UInt8](repeating: 0, count: Int(MAXPATHLEN))
    let count = bytes.withUnsafeMutableBytes { pointer in
        proc_pidpath(pid, pointer.baseAddress, UInt32(pointer.count))
    }
    guard count > 0 else { return "" }
    return String(cString: bytes)
}

func targetAudioObjects(bundleID: String, applicationName: String) throws -> [AudioObjectID] {
    let applications = NSWorkspace.shared.runningApplications
    let appByPID = Dictionary(uniqueKeysWithValues: applications.map { ($0.processIdentifier, $0) })
    let normalizedName = applicationName.lowercased()
    var matches = Set<AudioObjectID>()

    for objectID in try audioProcessObjectIDs() {
        let pid: pid_t
        do {
            pid = try objectID.readValue(kAudioProcessPropertyPID, defaultValue: -1)
        } catch {
            continue
        }
        guard pid > 0 else { continue }
        let audioBundle = (try? objectID.readString(kAudioProcessPropertyBundleID)) ?? ""
        let application = appByPID[pid]
        let appBundle = application?.bundleIdentifier ?? ""
        let appName = (application?.localizedName ?? "").lowercased()
        let executablePath = processPath(pid).lowercased()
        let nameMatch = appName == normalizedName || appName.hasPrefix(normalizedName + " ")
        let pathMatch = executablePath.contains("/\(normalizedName).app/")
        if audioBundle == bundleID || appBundle == bundleID || nameMatch || pathMatch {
            matches.insert(objectID)
        }
    }

    for application in applications where application.bundleIdentifier == bundleID {
        if let objectID = try? translatePID(application.processIdentifier) {
            matches.insert(objectID)
        }
    }

    guard !matches.isEmpty else {
        throw TapFailure(code: "target_not_running", message: "EnConvo is not exposing an audio process yet.")
    }
    return matches.sorted()
}

final class AudioFeatureAnalyzer: @unchecked Sendable {
    private let emitter: JsonEmitter
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
    private var lastEmission: UInt64 = 0
    private var lastVoice: UInt64 = 0

    init(sampleRate: Double, emitter: JsonEmitter) {
        self.sampleRate = Float(sampleRate)
        self.emitter = emitter
        self.lowCoefficient = Float(1 - exp(-2 * Double.pi * 520 / sampleRate))
        self.midCoefficient = Float(1 - exp(-2 * Double.pi * 2400 / sampleRate))
    }

    func consume(_ buffer: AVAudioPCMBuffer) {
        let frameCount = Int(buffer.frameLength)
        let channelCount = Int(buffer.format.channelCount)
        guard frameCount > 0, channelCount > 0 else { return }

        var totalEnergy: Double = 0
        var lowEnergy: Double = 0
        var midEnergy: Double = 0
        var highEnergy: Double = 0
        var peak: Float = 0
        var crossings = 0

        for frame in 0..<frameCount {
            let sample = mixedSample(buffer, frame: frame, channels: channelCount)
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

        let divisor = Double(frameCount)
        let rms = Float(sqrt(totalEnergy / divisor))
        let lowRMS = Float(sqrt(lowEnergy / divisor))
        let midRMS = Float(sqrt(midEnergy / divisor))
        let highRMS = Float(sqrt(highEnergy / divisor))
        let bandTotal = max(0.000001, lowRMS + midRMS + highRMS)
        let zcr = Float(crossings) / Float(frameCount)

        smoothedRMS = smoothedRMS * 0.58 + rms * 0.42
        smoothedPeak = max(peak, smoothedPeak * 0.72)
        smoothedLow = smoothedLow * 0.55 + (lowRMS / bandTotal) * 0.45
        smoothedMid = smoothedMid * 0.55 + (midRMS / bandTotal) * 0.45
        smoothedHigh = smoothedHigh * 0.55 + (highRMS / bandTotal) * 0.45
        smoothedZCR = smoothedZCR * 0.62 + zcr * 0.38

        let now = DispatchTime.now().uptimeNanoseconds
        if smoothedRMS > 0.003 { lastVoice = now }
        guard now - lastEmission >= 25_000_000 else { return }
        lastEmission = now
        let active = lastVoice > 0 && now - lastVoice < 190_000_000
        emitter.send([
            "type": "sample",
            "rms": Double(smoothedRMS),
            "peak": Double(smoothedPeak),
            "low": Double(smoothedLow),
            "mid": Double(smoothedMid),
            "high": Double(smoothedHigh),
            "zcr": Double(smoothedZCR),
            "active": active,
            "sampleRate": Double(sampleRate),
            "timestamp": Date().timeIntervalSince1970 * 1000
        ])
    }

    private func mixedSample(_ buffer: AVAudioPCMBuffer, frame: Int, channels: Int) -> Float {
        let interleaved = buffer.format.isInterleaved
        switch buffer.format.commonFormat {
        case .pcmFormatFloat32:
            guard let data = buffer.floatChannelData else { return 0 }
            if interleaved {
                var value: Float = 0
                for channel in 0..<channels { value += data[0][frame * channels + channel] }
                return value / Float(channels)
            }
            var value: Float = 0
            for channel in 0..<channels { value += data[channel][frame] }
            return value / Float(channels)
        case .pcmFormatInt16:
            guard let data = buffer.int16ChannelData else { return 0 }
            if interleaved {
                var value: Float = 0
                for channel in 0..<channels { value += Float(data[0][frame * channels + channel]) / 32768 }
                return value / Float(channels)
            }
            var value: Float = 0
            for channel in 0..<channels { value += Float(data[channel][frame]) / 32768 }
            return value / Float(channels)
        case .pcmFormatInt32:
            guard let data = buffer.int32ChannelData else { return 0 }
            if interleaved {
                var value: Float = 0
                for channel in 0..<channels { value += Float(data[0][frame * channels + channel]) / 2_147_483_648 }
                return value / Float(channels)
            }
            var value: Float = 0
            for channel in 0..<channels { value += Float(data[channel][frame]) / 2_147_483_648 }
            return value / Float(channels)
        default:
            return 0
        }
    }
}

@available(macOS 14.2, *)
final class ProcessAudioTap {
    private let emitter: JsonEmitter
    private var tapID = AudioObjectID.unknown
    private var aggregateDeviceID = AudioObjectID.unknown
    private var ioProcID: AudioDeviceIOProcID?
    private let captureQueue = DispatchQueue(label: "com.vivieen.audio-tap", qos: .userInteractive)

    init(bundleID: String, applicationName: String, emitter: JsonEmitter) throws {
        self.emitter = emitter
        let processObjects = try targetAudioObjects(bundleID: bundleID, applicationName: applicationName)
        let tapDescription = CATapDescription(stereoMixdownOfProcesses: processObjects)
        tapDescription.uuid = UUID()
        tapDescription.muteBehavior = .unmuted

        var createdTap = AudioObjectID.unknown
        var status = AudioHardwareCreateProcessTap(tapDescription, &createdTap)
        guard status == noErr else {
            throw TapFailure(
                code: "permission_or_audio_error",
                message: "Unable to capture EnConvo audio. Allow System Audio Recording for Vivieen, then try again. Core Audio status: \(status)"
            )
        }
        tapID = createdTap

        do {
            let outputDevice: AudioDeviceID = try AudioObjectID.system.readValue(
                kAudioHardwarePropertyDefaultSystemOutputDevice,
                defaultValue: AudioDeviceID.unknown
            )
            let outputUID = try outputDevice.readString(kAudioDevicePropertyDeviceUID)
            let aggregateDescription: [String: Any] = [
                kAudioAggregateDeviceNameKey: "Vivieen EnConvo Audio Tap",
                kAudioAggregateDeviceUIDKey: UUID().uuidString,
                kAudioAggregateDeviceMainSubDeviceKey: outputUID,
                kAudioAggregateDeviceIsPrivateKey: true,
                kAudioAggregateDeviceIsStackedKey: false,
                kAudioAggregateDeviceTapAutoStartKey: true,
                kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
                kAudioAggregateDeviceTapListKey: [[
                    kAudioSubTapDriftCompensationKey: true,
                    kAudioSubTapUIDKey: tapDescription.uuid.uuidString
                ]]
            ]

            var streamDescription: AudioStreamBasicDescription = try tapID.readValue(
                kAudioTapPropertyFormat,
                defaultValue: AudioStreamBasicDescription()
            )
            guard let format = AVAudioFormat(streamDescription: &streamDescription) else {
                throw TapFailure(code: "format_failed", message: "EnConvo audio format is unsupported.")
            }

            status = AudioHardwareCreateAggregateDevice(
                aggregateDescription as CFDictionary,
                &aggregateDeviceID
            )
            guard status == noErr else {
                throw TapFailure(code: "aggregate_failed", message: "Unable to create the private audio tap device: \(status)")
            }

            let analyzer = AudioFeatureAnalyzer(sampleRate: format.sampleRate, emitter: emitter)
            status = AudioDeviceCreateIOProcIDWithBlock(
                &ioProcID,
                aggregateDeviceID,
                captureQueue
            ) { _, inputData, _, _, _ in
                guard let buffer = AVAudioPCMBuffer(
                    pcmFormat: format,
                    bufferListNoCopy: inputData,
                    deallocator: nil
                ) else { return }
                analyzer.consume(buffer)
            }
            guard status == noErr else {
                throw TapFailure(code: "io_failed", message: "Unable to open the EnConvo audio stream: \(status)")
            }

            status = AudioDeviceStart(aggregateDeviceID, ioProcID)
            guard status == noErr else {
                throw TapFailure(code: "start_failed", message: "Unable to start EnConvo audio monitoring: \(status)")
            }
            emitter.send([
                "type": "ready",
                "processCount": processObjects.count,
                "sampleRate": format.sampleRate,
                "channels": format.channelCount
            ])
        } catch {
            stop()
            throw error
        }
    }

    func stop() {
        if aggregateDeviceID.isValid {
            AudioDeviceStop(aggregateDeviceID, ioProcID)
            if let ioProcID {
                AudioDeviceDestroyIOProcID(aggregateDeviceID, ioProcID)
                self.ioProcID = nil
            }
            AudioHardwareDestroyAggregateDevice(aggregateDeviceID)
            aggregateDeviceID = .unknown
        }
        if tapID.isValid {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = .unknown
        }
    }

    deinit { stop() }
}

final class SimulatedAudioTap {
    private let emitter: JsonEmitter
    private var timer: DispatchSourceTimer?
    private var phase: Double = 0

    init(emitter: JsonEmitter) {
        self.emitter = emitter
        emitter.send(["type": "ready", "processCount": 1, "sampleRate": 48_000, "channels": 2])
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now(), repeating: .milliseconds(25))
        timer.setEventHandler { [weak self] in self?.tick() }
        timer.resume()
        self.timer = timer
    }

    private func tick() {
        phase += 0.025
        let envelope = max(0, sin(phase * 2.4))
        let active = envelope > 0.12
        emitter.send([
            "type": "sample",
            "rms": active ? 0.035 + envelope * 0.09 : 0.0004,
            "peak": active ? 0.12 + envelope * 0.28 : 0.001,
            "low": 0.44 + sin(phase * 3.1) * 0.12,
            "mid": 0.34 + sin(phase * 4.7 + 1) * 0.1,
            "high": 0.22 + sin(phase * 6.2 + 2) * 0.08,
            "zcr": 0.08 + envelope * 0.1,
            "active": active,
            "sampleRate": 48_000,
            "timestamp": Date().timeIntervalSince1970 * 1000
        ])
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }
}

func triggerRightOption(emitter: JsonEmitter) -> Int32 {
    guard CGPreflightPostEventAccess() else {
        emitter.send([
            "type": "right-option",
            "ok": false,
            "code": "accessibility_required",
            "message": "Allow Vivieen in Privacy & Security → Accessibility, then try again."
        ])
        return 6
    }
    guard let source = CGEventSource(stateID: .hidSystemState),
          let down = CGEvent(
              keyboardEventSource: source,
              virtualKey: CGKeyCode(61),
              keyDown: true
          ),
          let up = CGEvent(
              keyboardEventSource: source,
              virtualKey: CGKeyCode(61),
              keyDown: false
          ) else {
        emitter.send([
            "type": "right-option",
            "ok": false,
            "code": "event_creation_failed",
            "message": "Unable to create the native Right Option event."
        ])
        return 7
    }
    down.flags = .maskAlternate
    down.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.09)
    up.flags = []
    up.post(tap: .cghidEventTap)
    emitter.send(["type": "right-option", "ok": true])
    return 0
}

@main
struct EnconvoAudioTapCLI {
    static func main() {
        let emitter = JsonEmitter()
        let arguments = CommandLine.arguments
        if arguments.contains("--self-test") {
            emitter.send(["type": "self-test", "ok": true])
            return
        }

        if arguments.contains("--trigger-right-option") {
            exit(triggerRightOption(emitter: emitter))
        }

        var processTap: AnyObject?
        var simulation: SimulatedAudioTap?
        let bundleID = value(after: "--bundle-id", in: arguments) ?? "com.frostyeve.enconvo"
        let applicationName = value(after: "--name", in: arguments) ?? "EnConvo"

        if arguments.contains("--simulate") {
            simulation = SimulatedAudioTap(emitter: emitter)
        } else if #available(macOS 14.2, *) {
            do {
                processTap = try ProcessAudioTap(
                    bundleID: bundleID,
                    applicationName: applicationName,
                    emitter: emitter
                )
            } catch let failure as TapFailure {
                emitter.send(["type": "error", "code": failure.code, "message": failure.message])
                exit(failure.code == "target_not_running" ? 3 : 4)
            } catch {
                emitter.send(["type": "error", "code": "unexpected", "message": error.localizedDescription])
                exit(4)
            }
        } else {
            emitter.send([
                "type": "error",
                "code": "unsupported",
                "message": "Follow EnConvo requires macOS 14.2 or later."
            ])
            exit(5)
        }

        signal(SIGTERM, SIG_IGN)
        signal(SIGINT, SIG_IGN)
        let stop: () -> Void = {
            if #available(macOS 14.2, *), let tap = processTap as? ProcessAudioTap { tap.stop() }
            simulation?.stop()
            exit(0)
        }
        let terminateSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        terminateSource.setEventHandler(handler: stop)
        terminateSource.resume()
        let interruptSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        interruptSource.setEventHandler(handler: stop)
        interruptSource.resume()
        withExtendedLifetime((processTap, simulation, terminateSource, interruptSource)) {
            RunLoop.main.run()
        }
    }

    static func value(after flag: String, in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(of: flag), index + 1 < arguments.count else { return nil }
        return arguments[index + 1]
    }
}
