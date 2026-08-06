import Foundation
import WebKit

/// Her page stops caring where she lives.
///
/// The app used to load http://192.168.x.x:8777 directly, so the moment
/// the phone left the Wi-Fi there was nothing to load and the screen went
/// black (owner, on 5G, 2026-08-03). Everything now goes through one
/// origin - viv://app - and this handler decides how to satisfy it:
///
///   1. a cached copy, for the heavy runtime that never changes
///   2. the Mac directly, when they share a network (fast, and it fills
///      the cache)
///   3. the relay, from anywhere else
///
/// Her 67MB of sprites cross the LAN once and live on the phone; over
/// cellular only small API calls travel, which is exactly the shape a
/// free-tier mailbox can carry.
final class VivSchemeHandler: NSObject, WKURLSchemeHandler {
    static let scheme = "viv"
    static let host = "app"

    /// The address pairing gave us. Correct until the router hands the
    /// Mac a different lease, after which it is a lie.
    private let pairedAddress: String
    /// A LAN address the Mac published and we PROVED answers. Preferred
    /// over the paired one, and dropped the moment it stops answering.
    private var lanAddress: String?
    private var address: String {
        lock.lock(); let found = lanAddress; lock.unlock()
        return found ?? pairedAddress
    }
    private let token: String
    private let relay: RelayClient
    private let cacheRoot: URL
    private let session: URLSession
    /// One failed direct attempt is enough to stop trying for a while;
    /// otherwise every asset pays the timeout on cellular.
    private var directOffUntil = Date.distantPast
    /// The road the OWNER chose, when the automatic choice is wrong.
    ///
    /// "auto" is the road-finding above. "relay" is for a network the
    /// phone and the Mac both sit on but which will not carry a packet
    /// between them - hotel and guest wifi, a segmented office VLAN - where
    /// every direct attempt is a black hole no probe can tell apart from a
    /// Mac that is merely busy. "solo" is nothing but this phone.
    ///
    /// Held in MEMORY, never written down. A pin is a fact about where you
    /// are standing, and you do not stand there tomorrow: relaunching the
    /// app clears it, so a pin set in a hotel cannot poison the walk home
    /// or quietly answer "why can't it find EnConvo" a month later
    /// (owner, 2026-08-04).
    // SOLO is the default, deliberately (owner, 2026-08-05): the app
    // opens as itself - chat, push-to-talk, live talk, Settings - with
    // no Mac, no relay, no waiting. Coupling is a CHOICE made in the
    // road menu, and only then does the phone go looking for the Mac.
    private var roadPin = "solo"
    /// Bumped every time the pin changes. A request already in flight when
    /// the owner pins the phone must not be allowed to land: the whole
    /// point of reaching for "solo only" mid-conversation is to STOP
    /// something, and an answer that arrives ten minutes later out of the
    /// relay's reply window would walk right past the pin.
    private var pinGeneration: UInt64 = 0
    private let lock = NSLock()
    /// WebKit hands a scheme handler the URL and the headers of a fetch,
    /// but NEVER its body - so every POST arrived empty and she answered
    /// "I did not catch that" to a perfectly good recording (owner,
    /// 2026-08-03). The page parks the body here first and puts its ticket
    /// in the query; this is where it is redeemed.
    private var parked: [String: Data] = [:]

    /// Tickets whose fetch arrived BEFORE their body did.
    private var awaited: [String: (Data?) -> Void] = [:]

    func park(id: String, body: Data) {
        lock.lock()
        if let waiter = awaited.removeValue(forKey: id) {
            lock.unlock(); waiter(body); return
        }
        parked[id] = body
        // A ticket nobody redeems must not become a leak.
        if parked.count > 24, let oldest = parked.keys.first { parked[oldest] = nil }
        lock.unlock()
    }

    /// The page posts the body over the script bridge and fetches in the
    /// VERY NEXT statement. Those are two IPC channels with no ordering
    /// between them, so it is a race - and the bigger the body, the more
    /// reliably it loses. A 2 KB chat body arrived in time; ~90 KB of
    /// recorded audio did not, the ticket redeemed to nil, the provider
    /// got an empty multipart and said 400, and push-to-talk failed with
    /// "could not transcribe" every time (owner, 2026-08-03).
    ///
    /// So wait for it. Never on the main thread - this handler runs there
    /// and the script message it is waiting for is delivered there too,
    /// which would deadlock - hence a callback, not a sleep.
    private func redeem(_ id: String, _ then: @escaping (Data?) -> Void) {
        lock.lock()
        if let body = parked.removeValue(forKey: id) {
            lock.unlock(); then(body); return
        }
        awaited[id] = then
        lock.unlock()
        DispatchQueue.global().asyncAfter(deadline: .now() + 4) { [weak self] in
            guard let self else { return }
            self.lock.lock()
            let late = self.awaited.removeValue(forKey: id)
            self.lock.unlock()
            if let late {
                NSLog("[viv-body] ticket %@ never arrived", id)
                late(nil)
            }
        }
    }

    init(address: String, token: String, relayBase: String) {
        self.pairedAddress = address.hasSuffix("/") ? String(address.dropLast()) : address
        self.token = token
        relay = RelayClient(base: relayBase, token: token)
        cacheRoot = FileManager.default
            .urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("viv-assets", isDirectory: true)
        try? FileManager.default.createDirectory(at: cacheRoot,
                                                 withIntermediateDirectories: true)
        let config = URLSessionConfiguration.default
        // Sprites should give up fast so the relay can take over; an agent
        // turn can genuinely run for minutes, and an eight-second ceiling
        // killed every one of them ("the agent closed without answering",
        // owner 2026-08-03). The per-request timeout is set per request.
        config.timeoutIntervalForRequest = 600
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        session = URLSession(configuration: config)
        super.init()
    }

    /// Everything her page needs to STAND UP is cacheable - the page
    /// itself, its worklet, every asset, the manifest. That is what lets
    /// the app open with the Mac asleep instead of a "can't reach your
    /// Mac" screen (solo mode, 2026-08-03). Cached copies are refreshed
    /// behind the scenes whenever the Mac answers, so a stale shell heals
    /// itself on the next shared Wi-Fi.
    private func cacheable(_ path: String) -> Bool {
        bare(path) == "/"
            || bare(path) == "/settings"
            || bare(path) == "/api/avatars"
            || path.hasPrefix("/assets/")
            || path == "/live-worklet.js"
            || path.hasPrefix("/api/avatar/thumb")
            // The portrait on every Settings card is /files/<slug>/
            // keyframe.png - NOT the thumb endpoint, which is what the
            // chat carousel uses. I warmed the thumbs and left these, so
            // the one thing missing from an offline Settings page was her
            // face (owner, 2026-08-04). The path carries the slug, so it
            // needs no extra keying; only the small stills are taken.
            || isAvatarStill(path)
            // Settings IS cacheable, as long as the key remembers which
            // avatar it was rendered for: the page bakes the active face
            // and its ACTIVE badge into the markup, so a shared key froze
            // whichever one was on stage when it was stored. Keyed per
            // slug, switching avatar misses and refetches by itself.
        // NOT /settings, and NOT /api/avatars. I cached both to make them
        // open faster and it was wrong: /settings is SERVER-RENDERED with
        // the live avatar state baked into its markup - the name and the
        // ACTIVE badge are in the HTML, not fetched - so a cached copy
        // freezes whichever avatar was on stage when it was stored. Switch
        // avatar in the carousel and Settings still swore the old one was
        // active (owner, 2026-08-04). /api/avatars carries the same
        // "active" flag and went stale the same way. Only genuinely static
        // things belong above: the chat page, her sprites, the thumbnails.
    }

    /// A per-avatar still under /files - the portraits Settings draws.
    /// Deliberately narrow: /files also serves sheets and previews, which
    /// are tens of megabytes and belong nowhere near this.
    private func isAvatarStill(_ path: String) -> Bool {
        let key = bare(path)
        guard key.hasPrefix("/files/") else { return false }
        return key.hasSuffix("/keyframe.png") || key.hasSuffix("/source.png")
            || key.hasSuffix("/head.png")
    }

    /// The path without its query. Boot fetches carry cache-busters
    /// (?v=...), which would make every launch a different cache key and
    /// the cache useless exactly when it is needed.
    private func bare(_ path: String) -> String {
        path.firstIndex(of: "?").map { String(path[path.startIndex..<$0]) } ?? path
    }

    /// Which avatar /assets/* currently resolves to. The Mac serves every
    /// face from the SAME paths, so a key built from the path alone
    /// serves the last avatar's sprites forever - two faces on one body,
    /// and a carousel where everybody is the same woman (owner,
    /// 2026-08-03). Learned from each manifest as it passes through.
    private var activeSlug = UserDefaults.standard
        .string(forKey: "cachedAvatarSlug") ?? "unknown"

    /// Warm every face's thumbnail as soon as we learn the roster, so the
    /// carousel's first open is as fast as its second. Quiet, cheap, and
    /// skipped for anything already held.
    private func warmDeck(from data: Data, path: String) {
        guard bare(path) == "/api/avatars",
              let top = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any],
              let rows = top["avatars"] as? [[String: Any]] else { return }
        for row in rows {
            guard let slug = row["slug"] as? String, !slug.isEmpty else { continue }
            // Both faces of the same avatar: the carousel's thumb and the
            // still Settings actually draws.
            for asset in ["/api/avatar/thumb?slug=\(slug)",
                          "/files/\(slug)/keyframe.png"] {
                if let held = try? Data(contentsOf: cacheURL(asset)),
                   !held.isEmpty { continue }
                guard let url = URL(string: address + asset) else { continue }
                var request = URLRequest(url: url)
                request.timeoutInterval = 20
                request.setValue(token, forHTTPHeaderField: "x-vivieen-token")
                session.dataTask(with: request) { [weak self] body, response, _ in
                    guard let self, let body, !body.isEmpty,
                          (response as? HTTPURLResponse)?.statusCode == 200
                    else { return }
                    try? body.write(to: self.cacheURL(asset))
                }.resume()
            }
        }
    }

    /// Somewhere to tell the page the face changed under it.
    var onAvatarChanged: (() -> Void)?

    /// Choosing a face in the carousel POSTs the slug and then reloads the
    /// page - and every one of those reloaded requests is cache-first,
    /// keyed by the slug we last saw. So the phone re-drew the old avatar
    /// from its own cache, for the whole launch: the manifest key still
    /// named the old face, and refreshInBackground had already spent its
    /// one attempt per path (owner, 2026-08-04).
    ///
    /// The request itself carries the answer. Take it from there, before
    /// the reload, so the very next fetch keys on the new face.
    private func noteActivation(asked: Data?, answered: Data) {
        // The MAC is the authority on which face is on stage, so read its
        // answer first; the request we sent is only the fallback.
        let field = { (blob: Data?, key: String) -> String? in
            guard let blob,
                  let top = try? JSONSerialization.jsonObject(with: blob)
                    as? [String: Any] else { return nil }
            let value = top[key] as? String
            return (value?.isEmpty == false) ? value : nil
        }
        guard let slug = field(answered, "active") ?? field(asked, "slug")
        else { return }
        lock.lock()
        let changed = slug != activeSlug
        activeSlug = slug
        // Everything learned under the old face is about the old face.
        refreshed.removeAll()
        lock.unlock()
        UserDefaults.standard.set(slug, forKey: "cachedAvatarSlug")
        // The manifest is the one file the new face cannot share, and its
        // key just changed - but /api/avatars is not slug-keyed and would
        // keep insisting the old one is on stage.
        try? FileManager.default.removeItem(at: snapshotURL("/api/avatars"))
        NSLog("[viv-scheme] avatar activated: %@ (was new: %@)",
              slug, changed ? "yes" : "no")
        // No onAvatarChanged here: the page reloads itself the moment this
        // request returns, and two reloads race each other.
    }

    private func noteSlug(from data: Data, path: String) {
        guard bare(path) == "/assets/manifest.json",
              let top = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any],
              let avatar = top["avatar"] as? [String: Any],
              let slug = avatar["slug"] as? String, !slug.isEmpty else { return }
        lock.lock()
        let changed = slug != activeSlug
        activeSlug = slug
        lock.unlock()
        UserDefaults.standard.set(slug, forKey: "cachedAvatarSlug")
        // The page drew the OLD face from cache a moment ago. Now that we
        // know better, say so - waiting for the next launch is how the
        // deck and the stage disagreed for a whole session.
        if changed {
            NSLog("[viv-scheme] active avatar changed to %@", slug)
            DispatchQueue.main.async { [weak self] in self?.onAvatarChanged?() }
        }
    }

    private func cacheURL(_ path: String) -> URL {
        let key = bare(path)
        // Everything the active avatar owns lives under its own slug.
        // Everything else keeps its QUERY, because for those the query is
        // identity, not a cache-buster: /api/avatar/thumb?slug=cleo and
        // ?slug=vvn are different faces, and collapsing them is what made
        // every card in the deck the same person.
        var name: String
        if key == "/" {
            name = "__page"
        } else if key.hasPrefix("/assets/") || key == "/settings"
                    || key == "/api/avatars" {
            lock.lock(); let slug = activeSlug; lock.unlock()
            name = slug + "_" + key.replacingOccurrences(of: "/", with: "_")
        } else {
            name = path.replacingOccurrences(of: "/", with: "_")
                .replacingOccurrences(of: "?", with: "$")
                .replacingOccurrences(of: "&", with: "-")
        }
        if name.count > 180 { name = String(name.suffix(180)) }
        return cacheRoot.appendingPathComponent(name)
    }

    /// Live state we keep a LAST-KNOWN copy of. Not cache-first - these
    /// must be live whenever the Mac is there, or Settings would show
    /// yesterday's configuration as if it were today's. But when the Mac
    /// is gone, last-known beats an empty page: without this, Settings
    /// opened instantly and then sat there bare, with no avatars, no
    /// models, no persona (owner, 2026-08-04).
    private func snapshotable(_ path: String) -> Bool {
        let key = bare(path)
        return key == "/api/config" || key == "/api/media/defaults"
            || key == "/api/store" || key == "/api/avatars"
    }

    private func snapshotURL(_ path: String) -> URL {
        var name = "snap_" + bare(path).replacingOccurrences(of: "/", with: "_")
        if name.count > 180 { name = String(name.suffix(180)) }
        return cacheRoot.appendingPathComponent(name)
    }

    /// The last good answer, if we ever had one.
    private func snapshot(_ path: String) -> Data? {
        guard snapshotable(path),
              let held = try? Data(contentsOf: snapshotURL(path)),
              !held.isEmpty else { return nil }
        return held
    }

    private func keepSnapshot(_ data: Data, path: String) {
        guard snapshotable(path), !data.isEmpty else { return }
        try? data.write(to: snapshotURL(path))
    }

    /// Stamp the health answer with the ROAD it came down, so the page can
    /// say so. The owner kept having to guess whether a slow reply meant
    /// the Mac was far away, asleep, or simply not there - the line named
    /// the brain but never the route (owner, 2026-08-04).
    /// The pin rides along on every health answer, so the page never has
    /// to ask separately and can never disagree with this file about what
    /// the owner chose.
    private func stampRoad(_ data: Data, _ road: String) -> Data {
        guard var top = (try? JSONSerialization.jsonObject(with: data))
                as? [String: Any] else { return data }
        top["road"] = road
        top["pin"] = pin()
        return (try? JSONSerialization.data(withJSONObject: top)) ?? data
    }

    /// "The Mac is not answering", with the pin attached. A relay pin whose
    /// relay has gone quiet lands here, and without the pin the page would
    /// read it as an ordinary dead Mac, enter solo, and start spending
    /// metered keys under a chip still claiming the relay - a pin turning
    /// itself into a bill (workflow review, 2026-08-04).
    private func offlineHealth(_ task: WKURLSchemeTask, requested: URL,
                               pinned: Bool) {
        var body: [String: Any] = ["offline": true, "pin": pin()]
        if pinned { body["pinned"] = true }
        let data = (try? JSONSerialization.data(withJSONObject: body))
            ?? Data("{\"offline\":true}".utf8)
        finish(task, url: requested, data: data,
               type: "application/json", status: 503)
    }

    /// The long way round to the Mac, for the liveness probe only. Used
    /// both when the direct road just failed and when it is fused off, so
    /// the two cases report the same road - because they are.
    private func healthViaRelay(_ task: WKURLSchemeTask, path: String,
                                requested: URL, generation: UInt64) {
        relay.send(path: path, method: "GET", body: nil,
                   timeout: 15) { [weak self] reply in
            guard let self else { return }
            // The owner may have pinned this phone while the mailbox was
            // thinking. An answer from the Mac that arrives after that is
            // not an answer we are allowed to use.
            guard self.generation() == generation else {
                self.offlineHealth(task, requested: requested, pinned: true)
                return
            }
            if let reply, reply.status == 200 {
                self.finish(task, url: requested,
                            data: self.stampRoad(reply.data, "internet"),
                            type: "application/json")
            } else {
                self.offlineHealth(task, requested: requested, pinned: false)
            }
        }
    }

    private func mime(for path: String) -> String {
        switch (path as NSString).pathExtension.lowercased() {
        case "html", "": return "text/html; charset=utf-8"
        case "js": return "application/javascript; charset=utf-8"
        case "css": return "text/css; charset=utf-8"
        case "json": return "application/json"
        case "png": return "image/png"
        case "jpg", "jpeg": return "image/jpeg"
        case "webp": return "image/webp"
        case "mp4", "m4v": return "video/mp4"
        case "mov": return "video/quicktime"
        case "webm": return "video/webm"
        case "wav": return "audio/wav"
        default: return "application/octet-stream"
        }
    }

    /// WebKit throws - and the throw is fatal - if a task is answered
    /// after it was stopped, and a relay round trip is slow enough that
    /// this happens routinely (crash log, 2026-08-03). Only tasks WebKit
    /// still wants get spoken to, and each gets answered exactly once.
    private var liveTasks = Set<ObjectIdentifier>()

    private func adopt(_ task: WKURLSchemeTask) {
        lock.lock(); liveTasks.insert(ObjectIdentifier(task)); lock.unlock()
    }

    private func retire(_ task: WKURLSchemeTask) -> Bool {
        lock.lock()
        let wanted = liveTasks.remove(ObjectIdentifier(task)) != nil
        lock.unlock()
        return wanted
    }

    private func finish(_ task: WKURLSchemeTask, url: URL,
                        data: Data, type: String, status: Int = 200,
                        extra: [String: String] = [:]) {
        guard retire(task) else { return }
        var fields = ["Content-Type": type,
                      "Access-Control-Allow-Origin": "*",
                      "Cache-Control": "no-store"]
        for (key, value) in extra { fields[key] = value }
        let response = HTTPURLResponse(
            url: url, statusCode: status, httpVersion: "HTTP/1.1",
            headerFields: fields)!
        // Even so, WebKit can stop a task between the check and the call.
        // An ObjC exception here would abort the process, so catch it.
        VivObjC.catching {
            task.didReceive(response)
            task.didReceive(data)
            task.didFinish()
        }
    }

    /// Refresh one cached path from the Mac, silently, at most once per
    /// launch per path. Failure is fine - the cache stays as it was.
    private var refreshed = Set<String>()

    private func skipDirectNow() -> Bool {
        lock.lock(); defer { lock.unlock() }
        return Date() < directOffUntil || roadPin != "auto"
    }

    private func pin() -> String {
        lock.lock(); defer { lock.unlock() }
        return roadPin
    }

    private func generation() -> UInt64 {
        lock.lock(); defer { lock.unlock() }
        return pinGeneration
    }

    private func refreshInBackground(_ path: String) {
        lock.lock()
        let skip = Date() < directOffUntil || roadPin != "auto"
            || refreshed.contains(bare(path))
        if !skip { refreshed.insert(bare(path)) }
        lock.unlock()
        if skip { return }
        var request = URLRequest(url: URL(string: address + path)!)
        request.timeoutInterval = 10
        request.setValue(token, forHTTPHeaderField: "x-vivieen-token")
        session.dataTask(with: request) { [weak self] data, response, _ in
            guard let self, let data, !data.isEmpty,
                  (response as? HTTPURLResponse)?.statusCode == 200 else { return }
            self.noteSlug(from: data, path: path)
            self.warmDeck(from: data, path: path)
            try? data.write(to: self.cacheURL(path))
        }.resume()
    }

    func webView(_ webView: WKWebView, start task: WKURLSchemeTask) {
        guard let requested = task.request.url else { return }
        adopt(task)
        // viv://app/api/... -> /api/...
        var path = requested.path.isEmpty ? "/" : requested.path
        // The ticket rides in the query and belongs to us, not the Mac.
        var ticket: String?
        var query = requested.query ?? ""
        if !query.isEmpty {
            let kept = query.split(separator: "&").filter { pair in
                if pair.hasPrefix("__vivbody=") {
                    ticket = String(pair.dropFirst("__vivbody=".count))
                    return false
                }
                return true
            }
            query = kept.joined(separator: "&")
        }
        if !query.isEmpty { path += "?" + query }
        let method = task.request.httpMethod ?? "GET"
        // A ticketed body may still be in flight; wait for it (see redeem)
        // and only then decide what to do with the request.
        if let ticket {
            let carry = (path, method, requested)
            redeem(ticket) { [weak self] parked in
                self?.route(task, path: carry.0, method: carry.1,
                            requested: carry.2,
                            body: parked ?? task.request.httpBody)
            }
            return
        }
        route(task, path: path, method: method, requested: requested,
              body: task.request.httpBody)
    }

    private func route(_ task: WKURLSchemeTask, path: String, method: String,
                       requested: URL, body: Data?) {

        let chosen = pin()
        let era = generation()

        // Solo mode: these paths are the PHONE's, never the Mac's. The
        // page asks for a call by NAME of a key; the proxy injects the
        // value from the iOS Keychain, so no key ever enters the page.
        // FIRST, because a pin is set through this door and must be
        // reachable no matter what the pin currently says.
        if bare(path).hasPrefix("/solo/") {
            handleSolo(task, requested: requested, path: bare(path),
                       method: method, body: body)
            return
        }

        // Her hands: the owner's calendar, reminders, and contacts live on
        // THIS device - a hands call is never proxied anywhere. The page
        // runs the directive loop; whichever brain asked (the Mac's or the
        // phone's own), the data itself stays here.
        if bare(path) == "/hands/run", method == "POST" {
            guard let body,
                  let spec = try? JSONSerialization.jsonObject(with: body)
                    as? [String: Any],
                  let tool = spec["tool"] as? String else {
                json(task, requested, ["error": "bad hands spec"], status: 400)
                return
            }
            AgentHands.run(tool: tool,
                           args: (spec["args"] as? [String: Any]) ?? [:]) {
                [weak self] result in
                self?.json(task, requested, result,
                           status: result["error"] == nil ? 200 : 500)
            }
            return
        }

        // The STRUCTURE ships with the app (owner, 2026-08-05): the page
        // and Settings come from the bundle - never the Mac, never the
        // page cache - and only DATA syncs. This is what makes launch
        // instant and total: a powered-off Mac changes nothing about the
        // shell, and the old "the phone cached a stale page" trap cannot
        // exist.
        if method == "GET",
           let name = ["/": "index", "/settings": "settings"][bare(path)],
           let file = Bundle.main.url(forResource: name, withExtension: "html"),
           let html = try? Data(contentsOf: file) {
            finish(task, url: requested, data: html,
                   type: "text/html; charset=utf-8")
            return
        }

        // Health is a LIVENESS PROBE, and a probe must be allowed to
        // fail fast: routed through the relay's ten-minute reply window
        // it never failed at all, the page's poll hung on its very first
        // ask, and solo mode could not trigger (2026-08-03). Direct with
        // a short fuse, one brief relay try, then an honest 503.
        if bare(path) == "/health", method == "GET" {
            // Pinned to this phone, the honest answer to "is the Mac
            // there" is "not for us". Saying so through the SAME door a
            // sleeping Mac uses is what makes the pin hold: the page's own
            // solo machinery takes over, and its soloExit - which fires on
            // any healthy reply and would otherwise undo the pin a second
            // and a half later - can never run. "pinned" is there so the
            // page blames the right thing, and never says the Mac is
            // asleep when the owner is the one who stepped away.
            if chosen == "solo" {
                offlineHealth(task, requested: requested, pinned: true)
                return
            }
            // A probe that takes a road the real requests are NOT taking
            // would lie: the badge would read "lan" while every turn was
            // fused onto the relay for the next twenty seconds. Honour
            // the same fuse route() honours, and the road this reports is
            // the road her answer actually comes down (owner, 2026-08-04).
            // It costs nothing either: a probe that skips a dead LAN also
            // stops burning four seconds of every poll on it.
            if skipDirectNow() {
                healthViaRelay(task, path: path, requested: requested,
                               generation: era)
                return
            }
            var probe = URLRequest(url: URL(string: address + path)!)
            probe.timeoutInterval = 4
            probe.setValue(token, forHTTPHeaderField: "x-vivieen-token")
            session.dataTask(with: probe) { [weak self] data, response, error in
                guard let self else { return }
                guard self.generation() == era else {
                    self.offlineHealth(task, requested: requested, pinned: true)
                    return
                }
                if error == nil, let data,
                   (response as? HTTPURLResponse)?.statusCode == 200 {
                    self.finish(task, url: requested,
                                data: self.stampRoad(data, "lan"),
                                type: "application/json")
                    return
                }
                // The probe is the CHEAP canary, so let it do the work of
                // the expensive one. A chat POST gets a ten-minute direct
                // timeout because a turn is allowed to think - which means
                // the first message after walking out of the house used to
                // hang on a blackholed LAN for ten minutes before the relay
                // was tried at all. This four-second miss arms the same
                // twenty-second fuse a real failure arms, so the POST never
                // takes that road in the first place (owner, 2026-08-04).
                self.lock.lock()
                self.directOffUntil = Date().addingTimeInterval(20)
                let stale = self.lanAddress
                self.lanAddress = nil
                self.lock.unlock()
                if stale != nil { NSLog("[viv-scheme] LAN address stopped answering") }
                self.discoverMac()
                // 8s was too tight to survive cellular. A relay round
                // trip is four hops through a 700ms-polled mailbox - ~5s
                // on wifi before 5G latency is added - so the probe was
                // timing out while the Mac was perfectly reachable and
                // the phone declared itself offline (owner, 2026-08-03).
                // Still a fuse: two misses in a row is what enters solo.
                self.healthViaRelay(task, path: path, requested: requested,
                                    generation: era)
            }.resume()
            return
        }

        // The manifest is the ONLY thing that can teach us the active
        // avatar, so it must reach the Mac whenever the Mac is there.
        // CACHE FIRST, ALWAYS - including the manifest. It used to be
        // fetched from the Mac on every launch "so a changed avatar is
        // learned", which meant that on cellular nothing could be drawn
        // until a direct attempt timed out and the relay answered: no
        // avatar, just BOOTING (owner, 2026-08-04). This app is a solo app
        // first. The refresh below still learns a changed avatar, and now
        // tells the page the moment it does.
        if method == "GET", cacheable(path),
           let cached = try? Data(contentsOf: cacheURL(path)), !cached.isEmpty {
            finish(task, url: requested, data: cached, type: mime(for: bare(path)))
            // Stale-while-revalidate: the cached copy answers NOW; a quiet
            // background fetch keeps it honest whenever the Mac is
            // actually there. Without this, caching the page would pin
            // the app to whatever shell it saw first.
            refreshInBackground(path)
            return
        }

        // Pinned to this phone. The cache above already answered anything
        // that lives here; a snapshot answers the rest of what she knows.
        // Everything genuinely on the Mac is refused NOW - never handed to
        // the relay, whose reply window is ten minutes. A pin that makes
        // Settings spin for ten minutes and then blames the relay would be
        // worse than no pin (owner, 2026-08-04).
        if chosen == "solo" {
            if soloSettings(task, requested: requested, path: path,
                            method: method, body: body) {
                return
            }
            if method == "GET", let held = snapshot(path) {
                finish(task, url: requested, data: held,
                       type: "application/json")
                return
            }
            refusePinned(task, requested: requested)
            return
        }

        if skipDirectNow() {
            // The boot burst is two dozen cacheable GETs fired in one
            // breath. Landing inside the fuse window sent every one of
            // them down the relay's ten-minute road even when the Mac was
            // one hop away - a fresh install took ten minutes on the
            // owner's own wifi (2026-08-05). So sprites get one grace
            // beat: ask the relay where the Mac is, give the probe a
            // moment, and only then commit to the slow road. TURNS get the
            // same beat: one flaky probe used to arm the fuse and the next
            // spoken prompt crawled to EnConvo through the relay while the
            // Mac sat on the same wifi (owner, 2026-08-05) - 2.5 seconds
            // to re-prove the LAN is cheap against a relay round trip.
            let bareNow = bare(path)
            let turnPost = method == "POST" &&
                ["/reply", "/api/enconvo/chat", "/stt", "/say"]
                    .contains(bareNow)
            if (method == "GET" && cacheable(path)) || turnPost {
                discoverMac()
                DispatchQueue.global().asyncAfter(deadline: .now() + 2.5) {
                    [weak self] in
                    guard let self else { return }
                    guard self.generation() == era else {
                        self.refusePinned(task, requested: requested)
                        return
                    }
                    if self.skipDirectNow() {
                        self.viaRelay(task, requested: requested, path: path,
                                      method: method, body: body,
                                      generation: era)
                    } else {
                        self.attemptDirect(task, requested: requested,
                                           path: path, method: method,
                                           body: body, generation: era,
                                           retried: true)
                    }
                }
                return
            }
            viaRelay(task, requested: requested, path: path,
                     method: method, body: body, generation: era)
            return
        }

        attemptDirect(task, requested: requested, path: path, method: method,
                      body: body, generation: era, retried: false)
    }

    /// One direct try against the address we currently believe in. On
    /// failure the Mac may simply have MOVED: ask the relay, give the
    /// probe a beat, and if a different address stands proven take a
    /// second direct try instead of the relay's reply window. `retried`
    /// keeps it to one extra attempt.
    private func attemptDirect(_ task: WKURLSchemeTask, requested: URL,
                               path: String, method: String, body: Data?,
                               generation era: UInt64, retried: Bool) {
        let used = address
        var direct = URLRequest(url: URL(string: used + path)!)
        direct.httpMethod = method
        direct.httpBody = body
        // Static things fail fast so we can fall through to the relay;
        // anything that thinks for a living gets room to think.
        direct.timeoutInterval = cacheable(path) || method == "GET" ? 8 : 600
        direct.setValue(token, forHTTPHeaderField: "x-vivieen-token")
        // A <video> asks in RANGES - a probe for bytes 0-1 first, then the
        // pieces as it plays - and answering a range probe with the whole
        // file as a 200 makes iOS refuse to play at all: a black card with
        // a slashed play button (owner's generated clip, 2026-08-05). The
        // Mac serves proper 206es; carry the question through.
        if let range = task.request.value(forHTTPHeaderField: "Range") {
            direct.setValue(range, forHTTPHeaderField: "Range")
        }
        // Keep the page's own content type - a multipart upload carries a
        // boundary, and calling it JSON makes the Mac unable to parse it.
        if body != nil {
            direct.setValue(
                task.request.value(forHTTPHeaderField: "Content-Type")
                    ?? "application/json",
                forHTTPHeaderField: "Content-Type")
        }
        session.dataTask(with: direct) { [weak self] data, response, error in
            guard let self else { return }
            // Pinned while this was in the air: do not land it.
            guard self.generation() == era else {
                self.refusePinned(task, requested: requested)
                return
            }
            guard error == nil, let data,
                  let http = response as? HTTPURLResponse else {
                self.lock.lock()
                self.directOffUntil = Date().addingTimeInterval(20)
                // Only drop the proven address if IT is what just failed -
                // a fresher one adopted while this request was in the air
                // must not be destroyed by an old failure.
                if self.lanAddress == used {
                    self.lanAddress = nil
                    NSLog("[viv-scheme] LAN address stopped answering")
                }
                self.lock.unlock()
                // It may simply have moved. Ask, prove, adopt.
                self.discoverMac()
                if retried {
                    self.viaRelay(task, requested: requested, path: path,
                                  method: method, body: body, generation: era)
                    return
                }
                DispatchQueue.global().asyncAfter(deadline: .now() + 2.5) {
                    [weak self] in
                    guard let self else { return }
                    guard self.generation() == era else {
                        self.refusePinned(task, requested: requested)
                        return
                    }
                    let proven = self.address
                    if proven != used, !self.skipDirectNow() {
                        self.attemptDirect(task, requested: requested,
                                           path: path, method: method,
                                           body: body, generation: era,
                                           retried: true)
                    } else {
                        self.viaRelay(task, requested: requested, path: path,
                                      method: method, body: body,
                                      generation: era)
                    }
                }
                return
            }
            if http.statusCode == 200, self.bare(path) == "/api/avatar/activate" {
                self.noteActivation(asked: body, answered: data)
            }
            if method == "GET", http.statusCode == 200 {
                // Keep a last-known copy of live state on the fast path
                // too - a phone that never needs the relay would otherwise
                // never build one, and Settings would still open bare the
                // first time the Mac went away.
                self.keepSnapshot(data, path: path)
            }
            if method == "GET", self.cacheable(path), http.statusCode == 200 {
                self.noteSlug(from: data, path: path)
                self.warmDeck(from: data, path: path)
                try? data.write(to: self.cacheURL(path))
            }
            let type = http.value(forHTTPHeaderField: "Content-Type")
                ?? self.mime(for: path)
            // The range answer must keep its shape: the 206, the
            // Content-Range, and a Content-Length that matches the bytes
            // actually handed over (upstream's may describe a compressed
            // body URLSession already unpacked).
            var extra = ["Content-Length": String(data.count)]
            for key in ["Content-Range", "Accept-Ranges"] {
                if let value = http.value(forHTTPHeaderField: key) {
                    extra[key] = value
                }
            }
            self.finish(task, url: requested, data: data, type: type,
                        status: http.statusCode, extra: extra)
        }.resume()
    }

    // ------------------------------------------------- solo settings
    //
    // In solo the app IS the settings engine (owner, 2026-08-05: "it
    // should be functioning independently from macOS"). The same
    // settings page the Mac serves is answered here: lanes are read and
    // written against SoloStore, pasted keys land in the iOS Keychain,
    // and "Validate key & load" asks the PROVIDER directly from the
    // phone. Data syncs when coupled; nothing here needs the Mac.

    private func soloSettings(_ task: WKURLSchemeTask, requested: URL,
                              path: String, method: String,
                              body: Data?) -> Bool {
        switch (method, bare(path)) {
        case ("GET", "/api/config"):
            json(task, requested, soloConfigAnswer())
        case ("POST", "/api/config"):
            soloConfigSave(body)
            json(task, requested, soloConfigAnswer())
        case ("POST", "/api/models"):
            soloModels(task, requested: requested, body: body)
        case ("POST", "/api/test"):
            json(task, requested,
                 ["ok": false,
                  "detail": "Tests run through your Mac — solo validates "
                          + "keys against the provider instead."])
        default:
            return false
        }
        return true
    }

    /// The page's /api/config answer, built from the phone's own store.
    private func soloConfigAnswer() -> [String: Any] {
        var cfg = SoloStore.shared.config
        for kind in ["llm", "tts", "stt", "image", "video"] {
            var block = (cfg[kind] as? [String: Any]) ?? [:]
            block["api_key"] = ""
            block["has_key"] =
                !SoloStore.shared.secret("\(kind).api_key").isEmpty
            cfg[kind] = block
        }
        var live = (cfg["live"] as? [String: Any]) ?? [:]
        live["xai_api_key"] = ""
        live["eleven_api_key"] = ""
        live["has_xai_api_key"] =
            !SoloStore.shared.secret("live.xai_api_key").isEmpty
        live["has_eleven_api_key"] =
            !SoloStore.shared.secret("live.eleven_api_key").isEmpty
        cfg["live"] = live
        // The platform keyring is the Mac's bookkeeping; solo keys are
        // per-lane, so the ring reads empty rather than lying.
        cfg["keys"] = [String: String]()
        cfg["has_keys"] = [String: Bool]()
        if cfg["persona"] == nil { cfg["persona"] = [String: String]() }
        return ["config": cfg, "catalog": soloCatalog(),
                "globals": [String: String](),
                "routes": [String: String](),
                "active": activeSlug]
    }

    /// The provider roster: the last synced one if the Mac ever answered,
    /// the bundled copy on a fresh install - the page renders either way.
    private func soloCatalog() -> Any {
        if let held = snapshot("/api/config"),
           let top = try? JSONSerialization.jsonObject(with: held)
             as? [String: Any],
           let catalog = top["catalog"] {
            return catalog
        }
        if let url = Bundle.main.url(forResource: "catalog",
                                     withExtension: "json"),
           let data = try? Data(contentsOf: url),
           let catalog = try? JSONSerialization.jsonObject(with: data) {
            return catalog
        }
        return [String: Any]()
    }

    private func soloConfigSave(_ body: Data?) {
        guard let body,
              let asked = try? JSONSerialization.jsonObject(with: body)
                as? [String: Any] else { return }
        var cfg = SoloStore.shared.config
        for kind in ["llm", "tts", "stt", "image", "video"] {
            guard var block = asked[kind] as? [String: Any] else { continue }
            let key = (block["api_key"] as? String) ?? ""
            if key == "__clear__" {
                SoloStore.shared.storeSecret("\(kind).api_key", "")
            } else if !key.isEmpty {
                SoloStore.shared.storeSecret("\(kind).api_key", key)
            }
            block["api_key"] = nil
            block["has_key"] = nil
            var kept = (cfg[kind] as? [String: Any]) ?? [:]
            for (field, value) in block { kept[field] = value }
            cfg[kind] = kept
        }
        if var live = asked["live"] as? [String: Any] {
            for field in ["xai_api_key", "eleven_api_key"] {
                let key = (live[field] as? String) ?? ""
                if key == "__clear__" {
                    SoloStore.shared.storeSecret("live.\(field)", "")
                } else if !key.isEmpty {
                    SoloStore.shared.storeSecret("live.\(field)", key)
                }
                live[field] = nil
                live["has_" + field] = nil
            }
            var kept = (cfg["live"] as? [String: Any]) ?? [:]
            for (field, value) in live { kept[field] = value }
            cfg["live"] = kept
        }
        if let persona = asked["persona"] as? [String: Any] {
            cfg["persona"] = persona
        }
        if let ui = asked["ui"] as? [String: Any] { cfg["ui"] = ui }
        SoloStore.shared.config = cfg
        // The dictation keyboard reads the shared mirror, and a Soniox
        // key pasted in solo deserves to reach it just like a synced one.
        SoloStore.shared.mirrorForKeyboard()
    }

    /// "Validate key & load", answered by the PROVIDER, from the phone.
    private func soloModels(_ task: WKURLSchemeTask, requested: URL,
                            body: Data?) {
        guard let body,
              let asked = try? JSONSerialization.jsonObject(with: body)
                as? [String: Any] else {
            json(task, requested, ["error": "bad request", "models": [],
                                   "voices": [], "validated": false])
            return
        }
        let kind = (asked["kind"] as? String) ?? "llm"
        let cfg = (asked["cfg"] as? [String: Any]) ?? [:]
        let provider = (cfg["provider"] as? String)
            ?? ((SoloStore.shared.config[kind] as? [String: Any])?["provider"]
                as? String) ?? ""
        let spec = soloProviderSpec(kind: kind, id: provider)
        let needsKey = (spec["key"] as? Bool) ?? true
        var key = (cfg["api_key"] as? String) ?? ""
        if key.isEmpty { key = SoloStore.shared.secret("\(kind).api_key") }
        if needsKey, key.isEmpty {
            json(task, requested,
                 ["error": "Enter an API key before loading models.",
                  "models": [], "voices": [], "validated": false])
            return
        }
        var base = (cfg["base_url"] as? String) ?? ""
        if base.isEmpty { base = (spec["base"] as? String) ?? "" }
        while base.hasSuffix("/") { base = String(base.dropLast()) }
        var listURL: URL?
        var headers: [String: String] = [:]
        var extract: ([String: Any]) -> [String] = { top in
            ((top["data"] as? [[String: Any]]) ?? [])
                .compactMap { $0["id"] as? String }
        }
        switch provider {
        case "ollama":
            listURL = URL(string: base + "/api/tags")
            extract = { top in
                ((top["models"] as? [[String: Any]]) ?? [])
                    .compactMap { $0["name"] as? String }
            }
        case "gemini":
            listURL = URL(string: base + "/models?pageSize=200&key=" + key)
            extract = { top in
                ((top["models"] as? [[String: Any]]) ?? [])
                    .compactMap { ($0["name"] as? String) }
                    .map { $0.hasPrefix("models/")
                        ? String($0.dropFirst("models/".count)) : $0 }
            }
        case "anthropic":
            listURL = URL(string: base + "/models")
            headers = ["x-api-key": key, "anthropic-version": "2023-06-01"]
        case "elevenlabs":
            listURL = URL(string: "https://api.elevenlabs.io/v1/voices")
            headers = ["xi-api-key": key]
        default:
            // The OpenAI wire shape is the market's lingua franca - the
            // same /models works for most of the fleet.
            listURL = URL(string: base + "/models")
            headers = ["Authorization": "Bearer " + key]
        }
        guard let url = listURL else {
            json(task, requested, ["error": "no endpoint for this provider",
                                   "models": [], "voices": [],
                                   "validated": false])
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 20
        for (field, value) in headers {
            request.setValue(value, forHTTPHeaderField: field)
        }
        session.dataTask(with: request) { [weak self] data, response, error in
            guard let self else { return }
            guard error == nil, let data,
                  let http = response as? HTTPURLResponse else {
                self.json(task, requested,
                          ["error": error?.localizedDescription
                             ?? "the provider did not answer",
                           "models": [], "voices": [], "validated": false])
                return
            }
            guard http.statusCode == 200,
                  let top = try? JSONSerialization.jsonObject(with: data)
                    as? [String: Any] else {
                self.json(task, requested,
                          ["error": "the provider refused "
                             + "(HTTP \(http.statusCode))",
                           "models": [], "voices": [], "validated": false])
                return
            }
            if provider == "elevenlabs" {
                let voices = ((top["voices"] as? [[String: Any]]) ?? [])
                    .compactMap { row -> [String: String]? in
                        guard let id = row["voice_id"] as? String else {
                            return nil
                        }
                        return ["id": id,
                                "name": (row["name"] as? String) ?? id]
                    }
                self.json(task, requested,
                          ["models": [], "voices": voices,
                           "validated": true])
                return
            }
            self.json(task, requested,
                      ["models": extract(top).sorted(), "voices": [],
                       "validated": true])
        }.resume()
    }

    private func soloProviderSpec(kind: String, id: String) -> [String: Any] {
        guard let catalog = soloCatalog() as? [String: Any],
              let list = catalog[kind] as? [[String: Any]] else { return [:] }
        return list.first { ($0["id"] as? String) == id } ?? [:]
    }

    /// The one sentence a refusal-by-choice is allowed to be. It says
    /// "pinned" because two pages print an error string verbatim, and
    /// nobody should read the bare word "locked" and think something broke.
    private func refusePinned(_ task: WKURLSchemeTask, requested: URL) {
        json(task, requested,
             ["error": "pinned to this phone", "pinned": true],
             status: 503)
    }

    private func viaRelay(_ task: WKURLSchemeTask, requested: URL, path: String,
                          method: String, body: Data?, generation era: UInt64) {
        // Pinned since this request set out - it is not allowed to arrive.
        guard generation() == era, pin() != "solo" else {
            refusePinned(task, requested: requested)
            return
        }
        // The direct road already failed, so the Mac is asleep or far
        // away. Waiting out the relay's window before showing anything is
        // how Settings sat empty for minutes on cellular. If we hold a
        // last-known copy, answer with it NOW and let the relay refresh it
        // behind the page (owner, 2026-08-04).
        if method == "GET", let held = snapshot(path) {
            NSLog("[viv-scheme] %@ from last-known snapshot", path)
            finish(task, url: requested, data: held, type: "application/json")
            relay.send(path: path, method: "GET", body: nil,
                       timeout: 60) { [weak self] reply in
                guard let self, let reply, reply.status == 200 else { return }
                self.keepSnapshot(reply.data, path: path)
                if self.cacheable(path) {
                    self.noteSlug(from: reply.data, path: path)
                    try? reply.data.write(to: self.cacheURL(path))
                }
            }
            return
        }
        relay.send(path: path, method: method, body: body,
                   contentType: task.request.value(forHTTPHeaderField: "Content-Type")) { [weak self] reply in
            guard let self else { return }
            guard let reply else {
                // Nothing answered. If we have ever seen this, show that
                // rather than an empty page.
                if method == "GET", let held = self.snapshot(path) {
                    NSLog("[viv-scheme] %@ from last-known snapshot", path)
                    self.finish(task, url: requested, data: held,
                                type: "application/json")
                    return
                }
                self.finish(task, url: requested,
                            data: Data("{\"error\":\"the relay did not answer\"}".utf8),
                            type: "application/json", status: 504)
                return
            }
            if reply.status == 200, self.bare(path) == "/api/avatar/activate" {
                self.noteActivation(asked: body, answered: reply.data)
            }
            if method == "GET", reply.status == 200 {
                self.keepSnapshot(reply.data, path: path)
            }
            if method == "GET", reply.status != 200, let held = self.snapshot(path) {
                NSLog("[viv-scheme] %@ from last-known snapshot", path)
                self.finish(task, url: requested, data: held,
                            type: "application/json")
                return
            }
            if method == "GET", self.cacheable(path), reply.status == 200 {
                self.noteSlug(from: reply.data, path: path)
                self.warmDeck(from: reply.data, path: path)
                try? reply.data.write(to: self.cacheURL(path))
            }
            self.finish(task, url: requested, data: reply.data,
                        type: reply.contentType, status: reply.status)
        }
    }

    func webView(_ webView: WKWebView, stop task: WKURLSchemeTask) {
        _ = retire(task)
    }

    /// Ask the relay where the Mac is, then PROVE the answer before
    /// trusting it. A published address is a claim; only a reply is
    /// evidence. Runs at boot and whenever the direct road has just
    /// failed, which is exactly when a moved Mac would be the reason.
    private var lastDiscoverAt = Date.distantPast
    func discoverMac() {
        guard pin() != "solo" else { return }
        // Every request that finds the road closed calls this, so it
        // throttles itself - and that is the point: a relay stretch keeps
        // ASKING every few seconds, instead of only at the moment of the
        // first failure, so a Mac that reappears mid-download is noticed.
        lock.lock()
        let tooSoon = Date().timeIntervalSince(lastDiscoverAt) < 4
        if !tooSoon { lastDiscoverAt = Date() }
        lock.unlock()
        if tooSoon { return }
        relay.presence { [weak self] mac in
            guard let self, let mac else { return }
            let candidates = (mac["lan"] as? [String]) ?? []
            guard !candidates.isEmpty else { return }
            for candidate in candidates {
                let trimmed = candidate.hasSuffix("/")
                    ? String(candidate.dropLast()) : candidate
                // The paired address is probed like any other claim: it
                // may have STARTED answering - a Mac that just woke, or a
                // pairing that rode the relay before the LAN settled - and
                // proving it clears the fuse exactly as a new address
                // would. Skipping it left a fresh install crawling the
                // relay for ten minutes with the Mac one hop away
                // (owner, 2026-08-05).
                guard let probe = URL(string: trimmed + "/health") else { continue }
                var request = URLRequest(url: probe)
                request.timeoutInterval = 3
                request.setValue(self.token, forHTTPHeaderField: "x-vivieen-token")
                self.session.dataTask(with: request) { _, response, _ in
                    guard (response as? HTTPURLResponse)?.statusCode == 200
                    else { return }
                    self.lock.lock()
                    let changed = self.lanAddress != trimmed
                    self.lanAddress = trimmed
                    self.directOffUntil = .distantPast   // it is here after all
                    self.lock.unlock()
                    if changed {
                        NSLog("[viv-scheme] Mac found on the LAN at %@", trimmed)
                    }
                }.resume()
            }
        }
    }

    /// Pull the solo config + keys from the Mac now (fire-and-forget).
    func syncSolo() {
        // Pinned to this phone means pinned: no key refresh, no page
        // warming, not even the presence read. A pin that quietly kept
        // talking to the Mac would not be a pin (owner, 2026-08-04). The
        // cost is stated where the pin is chosen - keys stop refreshing
        // until you let go of it.
        let chosen = pin()
        guard chosen != "solo" else { return }
        // Under a relay pin discoverMac still earns its keep - it reads the
        // relay and probes, so the moment that hostile wifi is behind you
        // the LAN address is already known and letting go of the pin is
        // instant. The other two are direct-only calls onto the very LAN
        // the pin exists to avoid.
        discoverMac()
        guard chosen == "auto" else { return }
        SoloStore.shared.sync(address: address, token: token)
        warmPages()
    }

    /// Pull the pages the owner has not opened yet, while the Mac is in
    /// reach. Caching only what has already been visited means the FIRST
    /// visit is always the slow one - and on cellular "slow" was a
    /// half-drawn Settings page and a long wait (owner, 2026-08-04).
    private func warmPages() {
        for path in ["/settings", "/api/avatars", "/api/config",
                     "/api/media/defaults"] {
            guard let url = URL(string: address + path) else { continue }
            var request = URLRequest(url: url)
            request.timeoutInterval = 20
            request.setValue(token, forHTTPHeaderField: "x-vivieen-token")
            session.dataTask(with: request) { [weak self] body, response, _ in
                guard let self, let body, !body.isEmpty,
                      (response as? HTTPURLResponse)?.statusCode == 200
                else { return }
                try? body.write(to: self.cacheURL(path))
                self.keepSnapshot(body, path: path)
                self.warmDeck(from: body, path: path)
                NSLog("[viv-scheme] warmed %@ (%d bytes)", path, body.count)
            }.resume()
        }
    }

    // ------------------------------------------------------------ solo

    private func json(_ task: WKURLSchemeTask, _ requested: URL,
                      _ object: [String: Any], status: Int = 200) {
        let data = (try? JSONSerialization.data(withJSONObject: object)) ?? Data()
        finish(task, url: requested, data: data,
               type: "application/json", status: status)
    }

    private func handleSolo(_ task: WKURLSchemeTask, requested: URL,
                            path: String, method: String, body: Data?) {
        switch path {
        case "/solo/config":
            var config = SoloStore.shared.config
            // The page learns WHICH keys exist, never what they are.
            var has: [String: Bool] = [:]
            for name in ["llm.api_key", "tts.api_key", "stt.api_key",
                         "image.api_key", "video.api_key",
                         "live.xai_api_key", "live.eleven_api_key"] {
                has[name] = !SoloStore.shared.secret(name).isEmpty
            }
            config["has"] = has
            json(task, requested, config)
        case "/solo/sync":
            // This is a direct LAN call, so a RELAY pin forbids it just as
            // firmly as a solo one - the relay pin exists precisely because
            // this LAN will not carry a packet to the Mac.
            if pin() != "auto" {
                json(task, requested, ["started": false, "pinned": true])
                return
            }
            SoloStore.shared.sync(address: address, token: token)
            json(task, requested, ["started": true])
        case "/solo/road":
            // GET reads the pin, POST sets it. Nothing is written to disk:
            // see roadPin. A pin is where you are standing today.
            guard method == "POST" else {
                json(task, requested, ["pin": pin()])
                return
            }
            // A POST whose body lost the ticket race must NOT quietly
            // degrade into a read: the sheet would report the old pin as
            // if the tap had worked (workflow review, 2026-08-04).
            guard let body,
                  let spec = try? JSONSerialization.jsonObject(with: body)
                    as? [String: Any],
                  let want = spec["pin"] as? String,
                  ["auto", "relay", "solo"].contains(want) else {
                json(task, requested,
                     ["pin": pin(), "error": "that did not arrive — try again"],
                     status: 409)
                return
            }
            guard want != pin() else { json(task, requested, ["pin": want]); return }
            lock.lock()
            roadPin = want
            pinGeneration &+= 1
            // Letting go of a pin has to let go of what the pinned road
            // taught us, or the app stays frozen on the shape it had while
            // pinned - a cache that already spent its one refresh for this
            // launch and will not look again.
            refreshed.removeAll()
            // Coupling is LAN FIRST (owner, 2026-08-05): open the direct
            // road immediately and let discoverMac prove a fresh address
            // in parallel - a failed direct try now retries against the
            // proven address before accepting the relay, so the stale
            // lease costs seconds, not a ten-minute ceiling.
            directOffUntil = .distantPast
            if want != "auto" { lanAddress = nil }
            lock.unlock()
            NSLog("[viv-scheme] road pinned to %@", want)
            if want == "auto" { syncSolo() }
            json(task, requested, ["pin": want])
        case "/solo/call":
            soloCall(task, requested: requested, body: body)
        case "/solo/soniox":
            sonioxCall(task, requested: requested, body: body)
        case "/solo/pick":
            // Choosing a model with no Mac in reach. Only the model name
            // moves - never a key, never a provider the phone has no
            // credential for.
            guard let body,
                  let spec = try? JSONSerialization.jsonObject(with: body)
                    as? [String: Any],
                  let lane = spec["lane"] as? String,
                  let model = spec["model"] as? String,
                  ["llm", "tts", "stt", "image", "video"].contains(lane) else {
                json(task, requested, ["error": "bad pick"], status: 400)
                return
            }
            var config = SoloStore.shared.config
            var block = (config[lane] as? [String: Any]) ?? [:]
            block["model"] = model
            config[lane] = block
            SoloStore.shared.config = config
            NSLog("[viv-solo] picked %@ model %@", lane, model)
            json(task, requested, ["ok": true, "lane": lane, "model": model])
        default:
            json(task, requested, ["error": "unknown solo path"], status: 404)
        }
    }

    /// Hearing through Soniox, which is a WebSocket and therefore cannot
    /// go through the HTTPS proxy at all. The page sends the take; the key
    /// and the model come from the synced store, never from the page.
    private func sonioxCall(_ task: WKURLSchemeTask, requested: URL, body: Data?) {
        guard let body,
              let spec = try? JSONSerialization.jsonObject(with: body)
                as? [String: Any],
              let payload = spec["wav_b64"] as? String,
              let wav = Data(base64Encoded: payload), !wav.isEmpty else {
            json(task, requested, ["error": "no take to transcribe"], status: 400)
            return
        }
        let key = SoloStore.shared.secret("stt.api_key")
        guard !key.isEmpty else {
            json(task, requested,
                 ["error": "no Soniox key on this phone yet - open the app "
                    + "near your Mac once to sync"], status: 401)
            return
        }
        let block = (SoloStore.shared.config["stt"] as? [String: Any]) ?? [:]
        SonioxTap.transcribe(
            wav: wav, apiKey: key,
            model: (block["model"] as? String) ?? "",
            language: (block["language"] as? String) ?? ""
        ) { [weak self] heard, error in
            guard let self else { return }
            if let error {
                self.json(task, requested, ["error": error], status: 502)
                return
            }
            self.json(task, requested, ["text": heard ?? ""])
        }
    }

    /// RFC1918 and .local - the addresses a home router can hand out.
    private static func isPrivateHost(_ host: String) -> Bool {
        if host.hasSuffix(".local") { return true }
        let parts = host.split(separator: ".").compactMap { Int($0) }
        guard parts.count == 4 else { return false }
        if parts[0] == 10 { return true }
        if parts[0] == 192 && parts[1] == 168 { return true }
        if parts[0] == 172 && (16...31).contains(parts[1]) { return true }
        return false
    }

    /// One provider HTTPS call, made by the app on the page's behalf.
    /// {url, method, headers{}, body_b64, key, key_style}
    ///   key_style: "bearer" | "x-api-key" | "query:<name>" | "header:<Name>"
    private func soloCall(_ task: WKURLSchemeTask, requested: URL, body: Data?) {
        guard let body,
              let spec = try? JSONSerialization.jsonObject(with: body)
                as? [String: Any],
              let target = spec["url"] as? String,
              var url = URL(string: target),
              let host = url.host,
              // Cleartext may speak only to the owner's own network: the
              // Mac's Ollama answers on plain http at a LAN address (#28).
              // The public internet stays https-only.
              url.scheme == "https"
                || (url.scheme == "http" && Self.isPrivateHost(host)) else {
            json(task, requested, ["error": "bad call spec"], status: 400)
            return
        }
        guard SoloStore.shared.allowedHosts().contains(host) else {
            json(task, requested,
                 ["error": "host not allowed: \(host)"], status: 403)
            return
        }
        let keyName = spec["key"] as? String ?? ""
        let keyValue = keyName.isEmpty ? "" : SoloStore.shared.secret(keyName)
        if !keyName.isEmpty && keyValue.isEmpty {
            json(task, requested,
                 ["error": "no key stored for \(keyName) - open the app "
                    + "near your Mac once to sync"], status: 401)
            return
        }
        let style = spec["key_style"] as? String ?? "bearer"
        if style.hasPrefix("query:"), !keyValue.isEmpty,
           var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            var items = parts.queryItems ?? []
            items.append(URLQueryItem(
                name: String(style.dropFirst("query:".count)), value: keyValue))
            parts.queryItems = items
            url = parts.url ?? url
        }
        var request = URLRequest(url: url)
        request.httpMethod = spec["method"] as? String ?? "POST"
        request.timeoutInterval = 300
        for (name, value) in (spec["headers"] as? [String: String]) ?? [:] {
            // The page supplies protocol headers, never credentials.
            if name.lowercased() == "authorization" { continue }
            request.setValue(value, forHTTPHeaderField: name)
        }
        if !keyValue.isEmpty {
            switch style {
            case "bearer":
                request.setValue("Bearer \(keyValue)",
                                 forHTTPHeaderField: "Authorization")
            case "x-api-key":
                request.setValue(keyValue, forHTTPHeaderField: "x-api-key")
            case "xi-api-key":
                request.setValue(keyValue, forHTTPHeaderField: "xi-api-key")
            case "token":
                request.setValue("Token \(keyValue)",
                                 forHTTPHeaderField: "Authorization")
            default:
                if style.hasPrefix("header:") {
                    request.setValue(keyValue, forHTTPHeaderField:
                        String(style.dropFirst("header:".count)))
                }
            }
        }
        if let payload = spec["body_b64"] as? String {
            request.httpBody = Data(base64Encoded: payload)
        }
        // Solo failures happen with no Mac to look at, so this call has to
        // narrate itself: which host, how many bytes went, what came back.
        NSLog("[viv-solo] call %@ body=%d key=%@", host,
              request.httpBody?.count ?? -1, keyName)
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            guard let self else { return }
            NSLog("[viv-solo] call %@ -> status=%d bytes=%d err=%@", host,
                  (response as? HTTPURLResponse)?.statusCode ?? -1,
                  data?.count ?? -1, error?.localizedDescription ?? "none")
            if let error {
                self.json(task, requested,
                          ["error": error.localizedDescription], status: 502)
                return
            }
            let http = response as? HTTPURLResponse
            self.json(task, requested, [
                "status": http?.statusCode ?? 0,
                "type": http?.value(forHTTPHeaderField: "Content-Type") ?? "",
                "body_b64": (data ?? Data()).base64EncodedString(),
            ])
        }.resume()
    }
}
