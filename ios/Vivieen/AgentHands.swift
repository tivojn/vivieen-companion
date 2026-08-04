/// Her hands on the owner's Apple data: calendar, reminders, contacts -
/// exactly the surface an App Store app may touch with permission, and
/// nothing more. Notes-the-app has no public API, wallpaper has no public
/// API, other apps' messages are sealed; those roads stay honestly closed
/// rather than half-open (feasibility check, 2026-08-05).
///
/// Full capability, not a peephole (owner, 2026-08-05): list/create/
/// update/delete for events, list/create/complete/delete for reminders,
/// and whole contact cards - phones, emails, addresses, birthday,
/// relations ("John's father" lives in relations or the note), org, and
/// the note itself. One caveat the code says out loud: Apple locks the
/// contact NOTE field behind com.apple.developer.contacts.notes, granted
/// per-app on request - the simulator build carries it, a TestFlight
/// build cannot until Apple approves. Everything else works everywhere.
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
        case "calendar_update":
            withEvents(done) { updateEvent(args, done) }
        case "calendar_delete":
            withEvents(done) { deleteEvent(args, done) }
        case "reminders_list":
            withReminders(done) { listReminders(args, done) }
        case "reminder_create":
            withReminders(done) { createReminder(args, done) }
        case "reminder_complete":
            withReminders(done) { finishReminder(args, delete: false, done) }
        case "reminder_delete":
            withReminders(done) { finishReminder(args, delete: true, done) }
        case "contacts_search":
            withContacts(done) { searchContacts(args, done) }
        case "contact_create":
            withContacts(done) { createContact(args, done) }
        case "contact_update":
            withContacts(done) { updateContact(args, done) }
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

    private static func eventRow(_ e: EKEvent) -> [String: Any] {
        ["id": e.eventIdentifier ?? "", "title": e.title ?? "",
         "start": show(e.startDate), "end": show(e.endDate),
         "all_day": e.isAllDay, "location": e.location ?? "",
         "notes": e.notes ?? "", "calendar": e.calendar?.title ?? ""]
    }

    private static func listEvents(_ args: [String: Any],
                                   _ done: ([String: Any]) -> Void) {
        let days = min(max((args["days"] as? Int) ?? 7, 1), 366)
        let start = parseDate(args["from"] as? String)
            ?? Calendar.current.startOfDay(for: Date())
        let stop = start.addingTimeInterval(TimeInterval(days) * 86400)
        let window = events.predicateForEvents(withStart: start, end: stop,
                                               calendars: nil)
        let found = events.events(matching: window).prefix(50).map(eventRow)
        done(["events": Array(found), "days": days, "from": show(start)])
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
            var row = eventRow(event)
            row["created"] = true
            done(row)
        } catch {
            done(["error": "could not save the event: \(error.localizedDescription)"])
        }
    }

    private static func updateEvent(_ args: [String: Any],
                                    _ done: ([String: Any]) -> Void) {
        guard let id = args["id"] as? String,
              let event = events.event(withIdentifier: id) else {
            done(["error": "no event with that id - list first"])
            return
        }
        if let title = args["title"] as? String, !title.isEmpty {
            event.title = title
        }
        if let start = parseDate(args["start"] as? String) {
            let length = event.endDate.timeIntervalSince(event.startDate)
            event.startDate = start
            event.endDate = start.addingTimeInterval(length)
        }
        if let minutes = args["minutes"] as? Int {
            event.endDate = event.startDate.addingTimeInterval(
                TimeInterval(min(max(minutes, 5), 24 * 60)) * 60)
        }
        if let location = args["location"] as? String { event.location = location }
        if let notes = args["notes"] as? String { event.notes = notes }
        do {
            try events.save(event, span: .thisEvent)
            var row = eventRow(event)
            row["updated"] = true
            done(row)
        } catch {
            done(["error": "could not update the event: \(error.localizedDescription)"])
        }
    }

    private static func deleteEvent(_ args: [String: Any],
                                    _ done: ([String: Any]) -> Void) {
        guard let id = args["id"] as? String,
              let event = events.event(withIdentifier: id) else {
            done(["error": "no event with that id - list first"])
            return
        }
        do {
            try events.remove(event, span: .thisEvent)
            done(["deleted": true, "title": event.title ?? ""])
        } catch {
            done(["error": "could not delete the event: \(error.localizedDescription)"])
        }
    }

    // --------------------------------------------------------- reminders

    private static func listReminders(_ args: [String: Any],
                                      _ done: @escaping ([String: Any]) -> Void) {
        let wantDone = (args["completed"] as? Bool) ?? false
        let window = wantDone
            ? events.predicateForCompletedReminders(
                withCompletionDateStarting: nil, ending: nil, calendars: nil)
            : events.predicateForIncompleteReminders(
                withDueDateStarting: nil, ending: nil, calendars: nil)
        events.fetchReminders(matching: window) { found in
            let rows = (found ?? []).prefix(50).map { r in
                ["id": r.calendarItemIdentifier, "title": r.title ?? "",
                 "due": show(r.dueDateComponents?.date),
                 "notes": r.notes ?? "", "completed": r.isCompleted,
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
            done(["created": true, "id": reminder.calendarItemIdentifier,
                  "title": title, "due": (args["due"] as? String) ?? ""])
        } catch {
            done(["error": "could not save the reminder: \(error.localizedDescription)"])
        }
    }

    private static func finishReminder(_ args: [String: Any], delete: Bool,
                                       _ done: ([String: Any]) -> Void) {
        guard let id = args["id"] as? String,
              let reminder = events.calendarItem(withIdentifier: id)
                as? EKReminder else {
            done(["error": "no reminder with that id - list first"])
            return
        }
        do {
            if delete {
                try events.remove(reminder, commit: true)
                done(["deleted": true, "title": reminder.title ?? ""])
            } else {
                reminder.isCompleted = true
                try events.save(reminder, commit: true)
                done(["completed": true, "title": reminder.title ?? ""])
            }
        } catch {
            done(["error": "could not change the reminder: \(error.localizedDescription)"])
        }
    }

    // ---------------------------------------------------------- contacts

    /// Apple locks the contact NOTE field behind an Apple-granted
    /// entitlement (com.apple.developer.contacts.notes). The simulator
    /// build carries it; a store build cannot until Apple approves the
    /// request. Ask for the note, and when iOS refuses, come back without
    /// it - with the refusal NAMED in the result, never silently.
    private static let fullKeys: [CNKeyDescriptor] = [
        CNContactGivenNameKey, CNContactFamilyNameKey, CNContactNicknameKey,
        CNContactOrganizationNameKey, CNContactJobTitleKey,
        CNContactPhoneNumbersKey, CNContactEmailAddressesKey,
        CNContactPostalAddressesKey, CNContactBirthdayKey,
        CNContactRelationsKey, CNContactUrlAddressesKey,
    ] as [CNKeyDescriptor]

    private static func contactRow(_ c: CNContact,
                                   noteLocked: Bool) -> [String: Any] {
        let label = { (raw: String?) -> String in
            CNLabeledValue<NSString>.localizedString(forLabel: raw ?? "")
        }
        var row: [String: Any] = [
            "id": c.identifier,
            "name": "\(c.givenName) \(c.familyName)"
                .trimmingCharacters(in: .whitespaces),
            "nickname": c.nickname,
            "organization": c.organizationName, "job_title": c.jobTitle,
            "phones": c.phoneNumbers.map {
                ["label": label($0.label), "value": $0.value.stringValue] },
            "emails": c.emailAddresses.map {
                ["label": label($0.label), "value": String($0.value)] },
            "addresses": c.postalAddresses.map {
                ["label": label($0.label), "value":
                    CNPostalAddressFormatter.string(from: $0.value, style:
                        .mailingAddress).replacingOccurrences(of: "\n",
                                                              with: ", ")] },
            "relations": c.contactRelations.map {
                ["label": label($0.label), "name": $0.value.name] },
            "urls": c.urlAddresses.map { String($0.value) },
        ]
        if let birthday = c.birthday?.date { row["birthday"] = show(birthday) }
        if noteLocked {
            row["note"] = "(locked: Apple grants contact-note access "
                + "per-app; this build does not have it yet)"
        } else if c.isKeyAvailable(CNContactNoteKey) {
            row["note"] = c.note
        }
        return row
    }

    private static func searchContacts(_ args: [String: Any],
                                       _ done: ([String: Any]) -> Void) {
        guard let query = args["query"] as? String, !query.isEmpty else {
            done(["error": "contacts_search needs a query"])
            return
        }
        let want = CNContact.predicateForContacts(matchingName: query)
        do {
            var noteLocked = false
            var hits: [CNContact]
            do {
                hits = try people.unifiedContacts(
                    matching: want,
                    keysToFetch: fullKeys + [CNContactNoteKey as CNKeyDescriptor])
            } catch {
                noteLocked = true
                hits = try people.unifiedContacts(matching: want,
                                                  keysToFetch: fullKeys)
            }
            done(["contacts": hits.prefix(10).map {
                contactRow($0, noteLocked: noteLocked) }])
        } catch {
            done(["error": "contact search failed: \(error.localizedDescription)"])
        }
    }

    private static func applyFields(_ args: [String: Any],
                                    to card: CNMutableContact) {
        if let name = args["name"] as? String, !name.isEmpty {
            let parts = name.split(separator: " ", maxSplits: 1)
            card.givenName = String(parts.first ?? "")
            card.familyName = parts.count > 1 ? String(parts[1]) : ""
        }
        if let phone = args["phone"] as? String, !phone.isEmpty {
            card.phoneNumbers.append(CNLabeledValue(
                label: CNLabelPhoneNumberMobile,
                value: CNPhoneNumber(stringValue: phone)))
        }
        if let email = args["email"] as? String, !email.isEmpty {
            card.emailAddresses.append(CNLabeledValue(
                label: CNLabelHome, value: email as NSString))
        }
        if let org = args["organization"] as? String, !org.isEmpty {
            card.organizationName = org
        }
        if let birthday = parseDate(args["birthday"] as? String) {
            card.birthday = Calendar.current.dateComponents(
                [.year, .month, .day], from: birthday)
        }
        // "John's father": a relation carries it with a proper label.
        if let relation = args["relation"] as? [String: Any],
           let name = relation["name"] as? String, !name.isEmpty {
            card.contactRelations.append(CNLabeledValue(
                label: (relation["label"] as? String) ?? CNLabelContactRelationFriend,
                value: CNContactRelation(name: name)))
        }
        if let note = args["note"] as? String, !note.isEmpty {
            card.note = note      // saves only where the entitlement exists
        }
    }

    private static func createContact(_ args: [String: Any],
                                      _ done: ([String: Any]) -> Void) {
        guard let name = args["name"] as? String, !name.isEmpty else {
            done(["error": "contact_create needs a name"])
            return
        }
        let card = CNMutableContact()
        applyFields(args, to: card)
        let save = CNSaveRequest()
        save.add(card, toContainerWithIdentifier: nil)
        do {
            try people.execute(save)
            done(["created": true, "id": card.identifier, "name": name])
        } catch {
            // The one likely refusal: writing the NOTE without Apple's
            // entitlement. Retry once without it, and say what was kept.
            if args["note"] != nil {
                let bare = CNMutableContact()
                var kept = args; kept.removeValue(forKey: "note")
                applyFields(kept, to: bare)
                let retry = CNSaveRequest()
                retry.add(bare, toContainerWithIdentifier: nil)
                if (try? people.execute(retry)) != nil {
                    done(["created": true, "id": bare.identifier, "name": name,
                          "note": "(dropped: Apple grants contact-note "
                            + "access per-app; this build does not have it)"])
                    return
                }
            }
            done(["error": "could not save the contact: \(error.localizedDescription)"])
        }
    }

    private static func updateContact(_ args: [String: Any],
                                      _ done: ([String: Any]) -> Void) {
        guard let id = args["id"] as? String, !id.isEmpty else {
            done(["error": "contact_update needs an id - search first"])
            return
        }
        do {
            let hit = try people.unifiedContact(withIdentifier: id,
                                                keysToFetch: fullKeys)
            guard let card = hit.mutableCopy() as? CNMutableContact else {
                done(["error": "that contact cannot be edited"])
                return
            }
            applyFields(args, to: card)
            let save = CNSaveRequest()
            save.update(card)
            try people.execute(save)
            done(["updated": true, "id": id])
        } catch {
            done(["error": "could not update the contact: \(error.localizedDescription)"])
        }
    }
}
