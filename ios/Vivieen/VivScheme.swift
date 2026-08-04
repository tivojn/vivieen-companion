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

    private let address: String
    private let token: String
    private let relay: RelayClient
    private let cacheRoot: URL
    private let session: URLSession
    /// One failed direct attempt is enough to stop trying for a while;
    /// otherwise every asset pays the timeout on cellular.
    private var directOffUntil = Date.distantPast
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
        self.address = address.hasSuffix("/") ? String(address.dropLast()) : address
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
            let thumb = "/api/avatar/thumb?slug=\(slug)"
            if let held = try? Data(contentsOf: cacheURL(thumb)), !held.isEmpty {
                continue
            }
            guard let url = URL(string: address + thumb) else { continue }
            var request = URLRequest(url: url)
            request.timeoutInterval = 20
            request.setValue(token, forHTTPHeaderField: "x-vivieen-token")
            session.dataTask(with: request) { [weak self] body, response, _ in
                guard let self, let body, !body.isEmpty,
                      (response as? HTTPURLResponse)?.statusCode == 200
                else { return }
                try? body.write(to: self.cacheURL(thumb))
            }.resume()
        }
    }

    /// Somewhere to tell the page the face changed under it.
    var onAvatarChanged: (() -> Void)?

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
                        data: Data, type: String, status: Int = 200) {
        guard retire(task) else { return }
        let response = HTTPURLResponse(
            url: url, statusCode: status, httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": type,
                           "Access-Control-Allow-Origin": "*",
                           "Cache-Control": "no-store"])!
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
        return Date() < directOffUntil
    }

    private func refreshInBackground(_ path: String) {
        lock.lock()
        let skip = Date() < directOffUntil || refreshed.contains(bare(path))
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

        // Health is a LIVENESS PROBE, and a probe must be allowed to
        // fail fast: routed through the relay's ten-minute reply window
        // it never failed at all, the page's poll hung on its very first
        // ask, and solo mode could not trigger (2026-08-03). Direct with
        // a short fuse, one brief relay try, then an honest 503.
        if bare(path) == "/health", method == "GET" {
            var probe = URLRequest(url: URL(string: address + path)!)
            probe.timeoutInterval = 4
            probe.setValue(token, forHTTPHeaderField: "x-vivieen-token")
            session.dataTask(with: probe) { [weak self] data, response, error in
                guard let self else { return }
                if error == nil, let data,
                   (response as? HTTPURLResponse)?.statusCode == 200 {
                    self.finish(task, url: requested, data: data,
                                type: "application/json")
                    return
                }
                // 8s was too tight to survive cellular. A relay round
                // trip is four hops through a 700ms-polled mailbox - ~5s
                // on wifi before 5G latency is added - so the probe was
                // timing out while the Mac was perfectly reachable and
                // the phone declared itself offline (owner, 2026-08-03).
                // Still a fuse: two misses in a row is what enters solo.
                self.relay.send(path: path, method: "GET", body: nil,
                                timeout: 15) { reply in
                    if let reply, reply.status == 200 {
                        self.finish(task, url: requested, data: reply.data,
                                    type: "application/json")
                    } else {
                        self.finish(task, url: requested,
                                    data: Data("{\"offline\":true}".utf8),
                                    type: "application/json", status: 503)
                    }
                }
            }.resume()
            return
        }

        // Solo mode: these paths are the PHONE's, never the Mac's. The
        // page asks for a call by NAME of a key; the proxy injects the
        // value from the iOS Keychain, so no key ever enters the page.
        if bare(path).hasPrefix("/solo/") {
            handleSolo(task, requested: requested, path: bare(path), body: body)
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

        lock.lock()
        let skipDirect = Date() < directOffUntil
        lock.unlock()

        if skipDirect {
            viaRelay(task, requested: requested, path: path,
                     method: method, body: body)
            return
        }

        var direct = URLRequest(url: URL(string: address + path)!)
        direct.httpMethod = method
        direct.httpBody = body
        // Static things fail fast so we can fall through to the relay;
        // anything that thinks for a living gets room to think.
        direct.timeoutInterval = cacheable(path) || method == "GET" ? 8 : 600
        direct.setValue(token, forHTTPHeaderField: "x-vivieen-token")
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
            guard error == nil, let data,
                  let http = response as? HTTPURLResponse else {
                self.lock.lock()
                self.directOffUntil = Date().addingTimeInterval(20)
                self.lock.unlock()
                self.viaRelay(task, requested: requested, path: path,
                              method: method, body: body)
                return
            }
            if method == "GET", self.cacheable(path), http.statusCode == 200 {
                self.noteSlug(from: data, path: path)
                self.warmDeck(from: data, path: path)
                try? data.write(to: self.cacheURL(path))
            }
            let type = http.value(forHTTPHeaderField: "Content-Type")
                ?? self.mime(for: path)
            self.finish(task, url: requested, data: data, type: type,
                        status: http.statusCode)
        }.resume()
    }

    private func viaRelay(_ task: WKURLSchemeTask, requested: URL, path: String,
                          method: String, body: Data?) {
        relay.send(path: path, method: method, body: body,
                   contentType: task.request.value(forHTTPHeaderField: "Content-Type")) { [weak self] reply in
            guard let self else { return }
            guard let reply else {
                self.finish(task, url: requested,
                            data: Data("{\"error\":\"the relay did not answer\"}".utf8),
                            type: "application/json", status: 504)
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

    /// Pull the solo config + keys from the Mac now (fire-and-forget).
    func syncSolo() {
        SoloStore.shared.sync(address: address, token: token)
        warmPages()
    }

    /// Pull the pages the owner has not opened yet, while the Mac is in
    /// reach. Caching only what has already been visited means the FIRST
    /// visit is always the slow one - and on cellular "slow" was a
    /// half-drawn Settings page and a long wait (owner, 2026-08-04).
    private func warmPages() {
        for path in ["/settings", "/api/avatars"] {
            guard let url = URL(string: address + path) else { continue }
            var request = URLRequest(url: url)
            request.timeoutInterval = 20
            request.setValue(token, forHTTPHeaderField: "x-vivieen-token")
            session.dataTask(with: request) { [weak self] body, response, _ in
                guard let self, let body, !body.isEmpty,
                      (response as? HTTPURLResponse)?.statusCode == 200
                else { return }
                try? body.write(to: self.cacheURL(path))
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
                            path: String, body: Data?) {
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
            SoloStore.shared.sync(address: address, token: token)
            json(task, requested, ["started": true])
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

    /// One provider HTTPS call, made by the app on the page's behalf.
    /// {url, method, headers{}, body_b64, key, key_style}
    ///   key_style: "bearer" | "x-api-key" | "query:<name>" | "header:<Name>"
    private func soloCall(_ task: WKURLSchemeTask, requested: URL, body: Data?) {
        guard let body,
              let spec = try? JSONSerialization.jsonObject(with: body)
                as? [String: Any],
              let target = spec["url"] as? String,
              var url = URL(string: target),
              url.scheme == "https",
              let host = url.host else {
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
