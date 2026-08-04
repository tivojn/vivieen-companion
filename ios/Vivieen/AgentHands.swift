/// Her hands on the owner's Apple data: calendar, reminders, contacts -
/// exactly the surface an App Store app may touch with permission, and
/// nothing more. Notes has no public API, wallpaper has no public API,
/// other apps' messages are sealed; those roads stay honestly closed
/// rather than half-open (feasibility check, 2026-08-05).
///
/// Every tool takes a JSON args dict and answers a JSON dict; the page's
/// directive loop feeds the result back to whichever brain asked - the
/// Mac's or the phone's own. Dates arrive as ISO-ish strings in the
/// owner's local time; be liberal in what we accept, the models write
/// "2026-08-07T15:00" one day and "2026-08-07" the next.
import EventKit
import Contacts

enum AgentHands {
    static let events = EKEventStore()
    static let people = CNContactStore()

    static func run(tool: String, args: [String: Any],
                    done: @escaping ([String: Any]) -> Void) {
        switch tool {
        case "calendar_list":
            withEvents(done) { listEvents(args, done) }
        case "calendar_create":
            withEvents(done) { createEvent(args, done) }
        case "reminders_list":
            withReminders(done) { listReminders(args, done) }
        case "reminder_create":
            withReminders(done) { createReminder(args, done) }
        case "contacts_search":
            withContacts(done) { searchContacts(args, done) }
        default:
            done(["error": "no such tool: \(tool)"])
        }
    }

    // ------------------------------------------------------- permissions

    private static func withEvents(_ done: @escaping ([String: Any]) -> Void,
                                   _ then: @escaping () -> Void) {
        events.requestFullAccessToEvents { granted, _ in
            granted ? then() : done(["error":
                "calendar access is off - Settings > Privacy > Calendars"])
        }
    }

    private static func withReminders(_ done: @escaping ([String: Any]) -> Void,
                                      _ then: @escaping () -> Void) {
        events.requestFullAccessToReminders { granted, _ in
            granted ? then() : done(["error":
                "reminders access is off - Settings > Privacy > Reminders"])
        }
    }

    private static func withContacts(_ done: @escaping ([String: Any]) -> Void,
                                     _ then: @escaping () -> Void) {
        people.requestAccess(for: .contacts) { granted, _ in
            granted ? then() : done(["error":
                "contacts access is off - Settings > Privacy > Contacts"])
        }
    }

    // ------------------------------------------------------------- dates

    private static func parseDate(_ raw: String?) -> Date? {
        guard let raw, !raw.isEmpty else { return nil }
        let iso = ISO8601DateFormatter()
        if let hit = iso.date(from: raw) { return hit }
        for pattern in ["yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm",
                        "yyyy-MM-dd HH:mm", "yyyy-MM-dd"] {
            let fmt = DateFormatter()
            fmt.locale = Locale(identifier: "en_US_POSIX")
            fmt.dateFormat = pattern
            fmt.timeZone = .current
            if let hit = fmt.date(from: raw) { return hit }
        }
        return nil
    }

    private static func show(_ date: Date?) -> String {
        guard let date else { return "" }
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.dateFormat = "yyyy-MM-dd'T'HH:mm"
        fmt.timeZone = .current
        return fmt.string(from: date)
    }

    // ------------------------------------------------------------ events

    private static func listEvents(_ args: [String: Any],
                                   _ done: ([String: Any]) -> Void) {
        let days = min(max((args["days"] as? Int) ?? 7, 1), 60)
        let start = Calendar.current.startOfDay(for: Date())
        let stop = start.addingTimeInterval(TimeInterval(days) * 86400)
        let window = events.predicateForEvents(withStart: start, end: stop,
                                               calendars: nil)
        let found = events.events(matching: window).prefix(50).map { e in
            ["title": e.title ?? "", "start": show(e.startDate),
             "end": show(e.endDate), "all_day": e.isAllDay,
             "location": e.location ?? "",
             "calendar": e.calendar?.title ?? ""] as [String: Any]
        }
        done(["events": Array(found), "days": days])
    }

    private static func createEvent(_ args: [String: Any],
                                    _ done: ([String: Any]) -> Void) {
        guard let title = args["title"] as? String, !title.isEmpty,
              let start = parseDate(args["start"] as? String) else {
            done(["error": "calendar_create needs title and start"])
            return
        }
        let minutes = min(max((args["minutes"] as? Int) ?? 60, 5), 24 * 60)
        let event = EKEvent(eventStore: events)
        event.title = title
        event.startDate = start
        event.endDate = start.addingTimeInterval(TimeInterval(minutes) * 60)
        event.location = args["location"] as? String
        event.notes = args["notes"] as? String
        event.calendar = events.defaultCalendarForNewEvents
        do {
            try events.save(event, span: .thisEvent)
            done(["created": true, "title": title, "start": show(start),
                  "calendar": event.calendar?.title ?? ""])
        } catch {
            done(["error": "could not save the event: \(error.localizedDescription)"])
        }
    }

    // --------------------------------------------------------- reminders

    private static func listReminders(_ args: [String: Any],
                                      _ done: @escaping ([String: Any]) -> Void) {
        let open = events.predicateForIncompleteReminders(
            withDueDateStarting: nil, ending: nil, calendars: nil)
        events.fetchReminders(matching: open) { found in
            let rows = (found ?? []).prefix(50).map { r in
                ["title": r.title ?? "",
                 "due": show(r.dueDateComponents?.date),
                 "list": r.calendar?.title ?? ""] as [String: Any]
            }
            done(["reminders": Array(rows)])
        }
    }

    private static func createReminder(_ args: [String: Any],
                                       _ done: ([String: Any]) -> Void) {
        guard let title = args["title"] as? String, !title.isEmpty else {
            done(["error": "reminder_create needs a title"])
            return
        }
        let reminder = EKReminder(eventStore: events)
        reminder.title = title
        reminder.notes = args["notes"] as? String
        reminder.calendar = events.defaultCalendarForNewReminders()
        if let due = parseDate(args["due"] as? String) {
            reminder.dueDateComponents = Calendar.current.dateComponents(
                [.year, .month, .day, .hour, .minute], from: due)
            reminder.addAlarm(EKAlarm(absoluteDate: due))
        }
        do {
            try events.save(reminder, commit: true)
            done(["created": true, "title": title,
                  "due": (args["due"] as? String) ?? ""])
        } catch {
            done(["error": "could not save the reminder: \(error.localizedDescription)"])
        }
    }

    // ---------------------------------------------------------- contacts

    private static func searchContacts(_ args: [String: Any],
                                       _ done: ([String: Any]) -> Void) {
        guard let query = args["query"] as? String, !query.isEmpty else {
            done(["error": "contacts_search needs a query"])
            return
        }
        let wanted = [CNContactGivenNameKey, CNContactFamilyNameKey,
                      CNContactPhoneNumbersKey, CNContactEmailAddressesKey]
            as [CNKeyDescriptor]
        do {
            let hits = try people.unifiedContacts(
                matching: CNContact.predicateForContacts(matchingName: query),
                keysToFetch: wanted)
            let rows = hits.prefix(10).map { c in
                ["name": "\(c.givenName) \(c.familyName)"
                    .trimmingCharacters(in: .whitespaces),
                 "phones": c.phoneNumbers.map { $0.value.stringValue },
                 "emails": c.emailAddresses.map { String($0.value) }]
                as [String: Any]
            }
            done(["contacts": rows])
        } catch {
            done(["error": "contact search failed: \(error.localizedDescription)"])
        }
    }
}
