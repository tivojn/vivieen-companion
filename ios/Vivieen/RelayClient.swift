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
    private var cursor = 0
    private let lock = NSLock()
    private var waiters: [String: (RelayReply) -> Void] = [:]
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

    /// Send one request to the Mac and call back with its reply.
    func send(path: String, method: String, body: Data?,
              completion: @escaping (RelayReply?) -> Void) {
        let id = UUID().uuidString
        var request: [String: Any] = ["path": path, "method": method]
        if let body, let text = String(data: body, encoding: .utf8),
           let json = try? JSONSerialization.jsonObject(with: body) {
            _ = text
            request["body"] = json
        }
        lock.lock()
        waiters[id] = { reply in completion(reply) }
        lock.unlock()
        // Nothing can arrive before the poller is running.
        startPolling()
        post("dir=to_mac", ["items": [["id": id, "req": request]]]) { ok in
            if !ok {
                self.lock.lock(); self.waiters[id] = nil; self.lock.unlock()
                completion(nil)
            }
        }
        // A turn through an agent can be minutes; give up well after that.
        DispatchQueue.global().asyncAfter(deadline: .now() + 300) {
            self.lock.lock()
            let pending = self.waiters.removeValue(forKey: id)
            self.lock.unlock()
            if pending != nil { completion(nil) }
        }
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
            if let next = top["next"] as? Int { self.cursor = next }
            for item in (top["items"] as? [[String: Any]]) ?? [] {
                self.deliver(item)
            }
        }.resume()
    }

    private func deliver(_ item: [String: Any]) {
        guard let id = item["id"] as? String,
              (item["done"] as? Bool) == true else { return }
        lock.lock()
        let waiter = waiters.removeValue(forKey: id)
        lock.unlock()
        guard let waiter else { return }
        var payload = Data()
        if let b64 = item["b64"] as? String {
            payload = Data(base64Encoded: b64) ?? Data()
        } else if let body = item["body"] as? String {
            payload = Data(body.utf8)
        }
        waiter(RelayReply(status: item["status"] as? Int ?? 200,
                          contentType: item["type"] as? String
                            ?? "application/octet-stream",
                          data: payload))
    }
}
