import Foundation
#if canImport(ActivityKit)
import ActivityKit

/// The one shape the app and the island share: whether she is
/// listening, how loud the room is, and what has settled so far.
struct TakeAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        var listening: Bool
        var level: Double
        var settled: String
    }
}
#endif
