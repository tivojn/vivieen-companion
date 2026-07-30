// Posts a right-Option key event so the pet's head press can drive EnConvo's
// voice command exactly as if the user pressed the hotkey themselves.
//
// Modifier-only hotkeys are observed as flagsChanged events, not keyDown, so
// the synthesized event is forced to that type with the option flag set on
// press and cleared on release. Requires the Accessibility permission; the
// "check" action reports trust without posting anything.
import AppKit
import ApplicationServices

@main
struct KeyTap {
    static func main() {
        let arguments = CommandLine.arguments
        let action = arguments.count > 1 ? arguments[1] : "tap"
        let keyCode = CGKeyCode(arguments.count > 2 ? UInt16(arguments[2]) ?? 61 : 61)

        if action == "check" {
            print(AXIsProcessTrusted() ? "trusted" : "untrusted")
            exit(AXIsProcessTrusted() ? 0 : 2)
        }
        guard AXIsProcessTrusted() else {
            FileHandle.standardError.write(
                "accessibility-permission-missing\n".data(using: .utf8)!)
            exit(2)
        }

        func post(_ down: Bool) {
            guard let event = CGEvent(
                keyboardEventSource: nil, virtualKey: keyCode, keyDown: down)
            else {
                FileHandle.standardError.write(
                    "event-creation-failed\n".data(using: .utf8)!)
                exit(3)
            }
            event.type = .flagsChanged
            event.flags = down ? .maskAlternate : []
            event.post(tap: .cghidEventTap)
        }

        switch action {
        case "down": post(true)
        case "up": post(false)
        case "tap":
            post(true)
            usleep(40_000)
            post(false)
        default:
            FileHandle.standardError.write(
                "unknown-action\n".data(using: .utf8)!)
            exit(4)
        }
    }
}
