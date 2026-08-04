import CryptoKit
import Foundation

/// The phone's half of the rendezvous. Symmetrical with the Mac agent:
/// post an envelope into to_mac, long-poll to_client for the answer. The
/// relay itself stays blind - the channel is a hash of the pairing token
/// and the proof is another, so it never learns the token, and a wrong
/// key can never claim a channel that is already pinned.
final class RelayClient {
    static let defaultBase = "https://relay-ten-livid.vercel.app"

    private let base: String
    private let channel: String
    private let proof: String
    /// -1 asks the relay for the TIP. A launch has nothing waiting for it
    /// in the backlog, and reading from 0 meant downloading every stale
    /// reply still in the box - 3.4 MB of other sessions' pages and
    /// assets - before the first fresh answer (owner, 2026-08-03).
    private var cursor = -1
    private var quiet = 0
    private let lock = NSLock()
    private var waiters: [String: (RelayReply) -> Void] = [:]
    /// Server-sent frames gathered per request until its "done" arrives.
    private var streams: [String: String] = [:]
    private var polling = false

    struct RelayReply {
        var status: Int
        var contentType: String
        var data: Data
    }

    init(base: String, token: String) {
        self.base = base.hasSuffix("/") ? String(base.dropLast()) : base
        func hex(_ digest: SHA256Digest) -> String {
            digest.map { String(format: "%02x", $0) }.joined()
        }
        channel = String(hex(SHA256.hash(data: Data(token.utf8))).prefix(16))
        proof = hex(SHA256.hash(data: Data("viv-relay:\(token)".utf8)))
    }

    private func url(_ query: String) -> URL? {
        URL(string: "\(base)/api/relay?channel=\(channel)&\(query)")
    }

    private func post(_ query: String, _ body: [String: Any],
                      done: ((Bool) -> Void)? = nil) {
        guard let url = url(query),
              let payload = try? JSONSerialization.data(withJSONObject: body)
        else { done?(false); return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.httpBody = payload
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(proof, forHTTPHeaderField: "x-viv-proof")
        request.timeoutInterval = 30
        URLSession.shared.dataTask(with: request) { _, response, _ in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
            done?(ok)
        }.resume()
    }

    /// Where is the Mac, and what does it hold? A tiny record the Mac
    /// republishes every 45s on a 120s TTL, so a sleeping one stops
    /// claiming to be reachable rather than leaving the phone to time out
    /// against an address nobody is listening on.
    func presence(_ then: @escaping ([String: Any]?) -> Void) {
        guard let url = url("dir=presence") else { then(nil); return }
        var request = URLRequest(url: url)
        request.setValue(proof, forHTTPHeaderField: "x-viv-proof")
        request.timeoutInterval = 12
        URLSession.shared.dataTask(with: request) { data, _, _ in
            guard let data,
                  let top = try? JSONSerialization.jsonObject(with: data)
                    as? [String: Any],
                  (top["present"] as? Bool) == true,
                  let mac = top["mac"] as? [String: Any] else { then(nil); return }
            then(mac)
        }.resume()
    }

    /// Send one request to the Mac and call back with its reply.
    func send(path: String, method: String, body: Data?,
              contentType: String? = nil,
              timeout: TimeInterval = 600,
              completion: @escaping (RelayReply?) -> Void) {
        let id = UUID().uuidString
        var request: [String: Any] = ["path": path, "method": method]
        if let body, !body.isEmpty {
            if let json = try? JSONSerialization.jsonObject(with: body) {
                request["body"] = json
            } else {
                // Audio takes, photos, anything not JSON: base64 with the
                // content type, so the Mac can rebuild the exact request.
                request["raw"] = body.base64EncodedString()
                request["type"] = contentType ?? "application/octet-stream"
            }
        }
        lock.lock()
        waiters[id] = { reply in completion(reply) }
        lock.unlock()
        // Pin the cursor to the tip BEFORE anything goes out, then listen,
        // then send. Nothing can arrive before the poller is running.
        seedCursor {
            self.startPolling()
            self.post("dir=to_mac", ["items": [["id": id, "req": request]]]) { ok in
                if !ok {
                    self.lock.lock(); self.waiters[id] = nil; self.lock.unlock()
                    completion(nil)
                }
            }
        }
        // A turn through an agent, with tools, can run many minutes.
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
            self.lock.lock()
            let pending = self.waiters.removeValue(forKey: id)
            self.lock.unlock()
            if pending != nil { completion(nil) }
        }
    }

    /// Resolve the tip once, before the first request leaves. Letting the
    /// first long-poll resolve it left a window where the reply could land
    /// ahead of the tip and be skipped for good - and the whole point of
    /// starting at the tip is to never read the backlog.
    private func seedCursor(_ then: @escaping () -> Void) {
        lock.lock(); let need = cursor < 0; lock.unlock()
        guard need, let url = url("dir=to_client&after=-1&wait=0") else {
            then(); return
        }
        var request = URLRequest(url: url)
        request.setValue(proof, forHTTPHeaderField: "x-viv-proof")
        request.timeoutInterval = 20
        URLSession.shared.dataTask(with: request) { [weak self] data, _, _ in
            guard let self else { return }
            var tip = 0
            if let data,
               let top = try? JSONSerialization.jsonObject(with: data)
                 as? [String: Any], let next = top["next"] as? Int, next >= 0 {
                tip = next
            }
            // An unreachable relay or an older one that does not know -1:
            // read from the start rather than never read at all.
            self.lock.lock(); if self.cursor < 0 { self.cursor = tip }
            self.lock.unlock()
            then()
        }.resume()
    }

    private func startPolling() {
        lock.lock()
        if polling { lock.unlock(); return }
        polling = true
        lock.unlock()
        poll()
    }

    private func poll() {
        guard let url = url("dir=to_client&after=\(cursor)&wait=25") else { return }
        var request = URLRequest(url: url)
        request.setValue(proof, forHTTPHeaderField: "x-viv-proof")
        request.timeoutInterval = 40
        URLSession.shared.dataTask(with: request) { [weak self] data, _, _ in
            guard let self else { return }
            defer {
                // Keep the ear open only while somebody is listening.
                self.lock.lock()
                let idle = self.waiters.isEmpty
                if idle { self.polling = false }
                self.lock.unlock()
                if !idle {
                    DispatchQueue.global().asyncAfter(deadline: .now() + 0.2) {
                        self.poll()
                    }
                }
            }
            guard let data,
                  let top = try? JSONSerialization.jsonObject(with: data)
                    as? [String: Any] else { return }
            let items = (top["items"] as? [[String: Any]]) ?? []
            // A mailbox emptied under us leaves this cursor past the end,
            // and reading past the end returns nothing forever. Never let
            // that be permanent: after a long silence, rewind and resync.
            // Adopt the cursor even on an EMPTY poll. The relay answers
            // a tip request with no items and the resolved position, and
            // taking it only on a non-empty poll left the cursor pinned
            // at -1, asking for the tip forever and skipping every reply
            // that landed in between.
            if let next = top["next"] as? Int, next >= 0 { self.cursor = next }
            if items.isEmpty {
                self.quiet += 1
                // The rewind stays at ZERO here, unlike the Mac's: this
                // end is waiting on a reply that may already have landed
                // in a stretch it cannot see, and re-reading costs it
                // nothing but time. The Mac re-EXECUTES, so it resyncs to
                // the tip instead.
                if self.quiet >= 8, self.cursor > 0 { self.cursor = 0; self.quiet = 0 }
            } else {
                self.quiet = 0
            }
            for item in items { self.deliver(item) }
        }.resume()
    }

    private func deliver(_ item: [String: Any]) {
        guard let id = item["id"] as? String else { return }
        // An agent turn arrives as a run of server-sent frames and then a
        // bare "done". Nobody was gathering the frames, so the page got an
        // empty 200 and said the agent closed without answering (owner,
        // 2026-08-03). Collect them; hand over the whole stream at the end.
        if let frame = item["sse"] as? String {
            lock.lock()
            streams[id, default: ""] += frame
            lock.unlock()
            return
        }
        guard (item["done"] as? Bool) == true else { return }
        lock.lock()
        let waiter = waiters.removeValue(forKey: id)
        let gathered = streams.removeValue(forKey: id)
        lock.unlock()
        guard let waiter else { return }
        var payload = Data()
        var type = item["type"] as? String ?? "application/octet-stream"
        if let gathered, (item["stream"] as? Bool) == true {
            payload = Data(gathered.utf8)
            type = "text/event-stream"
        } else if let b64 = item["b64"] as? String {
            payload = Data(base64Encoded: b64) ?? Data()
        } else if let body = item["body"] as? String {
            payload = Data(body.utf8)
        }
        waiter(RelayReply(status: item["status"] as? Int ?? 200,
                          contentType: type, data: payload))
    }
}
