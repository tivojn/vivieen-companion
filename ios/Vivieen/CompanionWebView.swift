import SwiftUI
import WebKit

/// The Mac's renderer, unchanged, inside WKWebView. Auth rides on a
/// cookie (the server accepts it as an equal of the Electron header)
/// because WebKit cannot attach a header to every subresource and
/// websocket. Mic capture is granted natively so hold-to-talk, realtime
/// dictation, and live talk all work.
struct CompanionWebView: UIViewRepresentable {
    let address: String
    let token: String

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.allowsInlineMediaPlayback = true
        configuration.mediaTypesRequiringUserActionForPlayback = []
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.uiDelegate = context.coordinator
        webView.isOpaque = false
        webView.backgroundColor = .clear
        webView.scrollView.isScrollEnabled = false
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        load(into: webView)
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {}

    private func load(into webView: WKWebView) {
        guard let base = URL(string: address), let host = base.host else { return }
        let cookie = HTTPCookie(properties: [
            .domain: host,
            .path: "/",
            .name: "vivieen-token",
            .value: token,
            .expires: Date(timeIntervalSinceNow: 3600 * 24 * 365),
        ])
        let store = webView.configuration.websiteDataStore.httpCookieStore
        let page = base.appendingPathComponent("/")
            .absoluteString + "?pet-preview&view=full&ios=1"
        let start = { webView.load(URLRequest(url: URL(string: page)!)) }
        if let cookie {
            store.setCookie(cookie) { DispatchQueue.main.async { _ = start() } }
        } else {
            _ = start()
        }
    }

    final class Coordinator: NSObject, WKUIDelegate {
        func webView(_ webView: WKWebView,
                     requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                     initiatedByFrame frame: WKFrameInfo,
                     type: WKMediaCaptureType,
                     decisionHandler: @escaping (WKPermissionDecision) -> Void) {
            decisionHandler(type == .microphone ? .grant : .deny)
        }
    }
}
