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
        config.timeoutIntervalForRequest = 8
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        session = URLSession(configuration: config)
        super.init()
    }

    /// The big, immutable things: worth keeping, safe to keep. Avatar
    /// thumbnails belong here too - a face at card size never changes,
    /// and fetching three of them on every open is what made the deck
    /// feel slow (owner, 2026-08-03).
    private func cacheable(_ path: String) -> Bool {
        path.hasPrefix("/assets/")
            || path == "/live-worklet.js"
            || path.hasPrefix("/api/avatar/thumb")
    }

    private func cacheURL(_ path: String) -> URL {
        let safe = path.replacingOccurrences(of: "/", with: "_")
        return cacheRoot.appendingPathComponent(safe)
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

    private func finish(_ task: WKURLSchemeTask, url: URL,
                        data: Data, type: String, status: Int = 200) {
        let response = HTTPURLResponse(
            url: url, statusCode: status, httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": type,
                           "Access-Control-Allow-Origin": "*",
                           "Cache-Control": "no-store"])!
        task.didReceive(response)
        task.didReceive(data)
        task.didFinish()
    }

    func webView(_ webView: WKWebView, start task: WKURLSchemeTask) {
        guard let requested = task.request.url else { return }
        // viv://app/api/... -> /api/...
        var path = requested.path.isEmpty ? "/" : requested.path
        if let query = requested.query, !query.isEmpty { path += "?" + query }
        let method = task.request.httpMethod ?? "GET"
        let body = task.request.httpBody

        if method == "GET", cacheable(path),
           let cached = try? Data(contentsOf: cacheURL(path)), !cached.isEmpty {
            finish(task, url: requested, data: cached, type: mime(for: path))
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
        direct.setValue(token, forHTTPHeaderField: "x-vivieen-token")
        if body != nil {
            direct.setValue("application/json", forHTTPHeaderField: "Content-Type")
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
        relay.send(path: path, method: method, body: body) { [weak self] reply in
            guard let self else { return }
            guard let reply else {
                self.finish(task, url: requested,
                            data: Data("{\"error\":\"the relay did not answer\"}".utf8),
                            type: "application/json", status: 504)
                return
            }
            if method == "GET", self.cacheable(path), reply.status == 200 {
                try? reply.data.write(to: self.cacheURL(path))
            }
            self.finish(task, url: requested, data: reply.data,
                        type: reply.contentType, status: reply.status)
        }
    }

    func webView(_ webView: WKWebView, stop task: WKURLSchemeTask) {}
}
