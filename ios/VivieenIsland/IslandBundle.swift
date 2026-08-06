import ActivityKit
import SwiftUI
import WidgetKit

/// Her face in the Dynamic Island. A quiet chip while she waits, and
/// during a keyboard take the island breathes with the OWNER's level
/// and shows the words as they settle. Enabled by the "island" toggle
/// in Settings; the app starts the activity when it opens (iOS permits
/// starting one only from the foreground) and updates it from anywhere.
@main
struct VivieenIslandBundle: WidgetBundle {
    var body: some Widget { TakeIslandWidget() }
}

struct TakeIslandWidget: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: TakeAttributes.self) { context in
            // The lock-screen banner.
            HStack(spacing: 10) {
                FaceChip().frame(width: 36, height: 36)
                Text(line(for: context.state))
                    .font(.footnote)
                    .lineLimit(2)
                Spacer()
                if context.state.listening {
                    Bars(level: context.state.level)
                }
            }
            .padding(12)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    FaceChip().frame(width: 44, height: 44)
                }
                DynamicIslandExpandedRegion(.center) {
                    Text(line(for: context.state))
                        .font(.footnote)
                        .lineLimit(3)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    if context.state.listening {
                        Bars(level: context.state.level)
                    }
                }
            } compactLeading: {
                FaceChip().frame(width: 22, height: 22)
            } compactTrailing: {
                if context.state.listening {
                    Bars(level: context.state.level).frame(width: 24)
                }
            } minimal: {
                FaceChip().frame(width: 22, height: 22)
            }
        }
    }

    private func line(for state: TakeAttributes.ContentState) -> String {
        if !state.listening { return "Vivieen is with you" }
        return state.settled.isEmpty
            ? "Listening…" : String(state.settled.suffix(90))
    }
}

/// The small mirrored face - the App Group copy the app keeps downsized
/// precisely so a memory-capped widget can afford to draw it.
struct FaceChip: View {
    var body: some View {
        if let group = FileManager.default.containerURL(
            forSecurityApplicationGroupIdentifier: "group.com.vivieen.pocket"),
           let image = UIImage(contentsOfFile:
            group.appendingPathComponent("avatar-small.png").path) {
            Image(uiImage: image)
                .resizable()
                .scaledToFill()
                .clipShape(Circle())
        } else {
            Circle().fill(.secondary.opacity(0.4))
        }
    }
}

struct Bars: View {
    let level: Double
    private let profile: [CGFloat] = [0.5, 0.85, 1, 0.8, 0.6]
    var body: some View {
        HStack(spacing: 2) {
            ForEach(0..<5, id: \.self) { index in
                Capsule()
                    .fill(Color.red.opacity(0.9))
                    .frame(width: 3,
                           height: 5 + min(1, CGFloat(level) * 7)
                             * 16 * profile[index])
            }
        }
    }
}
