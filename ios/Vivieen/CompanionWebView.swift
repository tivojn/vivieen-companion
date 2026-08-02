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
        let probe = WKUserScript(source: """
            window.onerror = function(m, s, l) {
              try { window.webkit.messageHandlers.viv.postMessage('ERR ' + m + ' @' + s + ':' + l); } catch (e) {}
            };
            console.error = (function(o) { return function() {
              o.apply(console, arguments);
              try { window.webkit.messageHandlers.viv.postMessage('CERR ' + Array.from(arguments).join(' ')); } catch (e) {}
            }; })(console.error);
            setTimeout(function() {
              try {
                var cv = document.querySelector('canvas');
                var st = document.getElementById('st');
                window.webkit.messageHandlers.viv.postMessage('STATE canvas=' +
                  (cv ? cv.width + 'x' + cv.height + ' vis=' + getComputedStyle(cv).display : 'none') +
                  ' cls=' + document.documentElement.className +
                  ' status=' + (st ? st.textContent : '?') +
                  ' inner=' + innerWidth + 'x' + innerHeight);
              } catch (e) { window.webkit.messageHandlers.viv.postMessage('STATE fail ' + e); }
            }, 4000);
            """, injectionTime: .atDocumentStart, forMainFrameOnly: true)
        configuration.userContentController.addUserScript(probe)
        configuration.userContentController.add(context.coordinator, name: "viv")
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
        guard var components = URLComponents(string: address),
              let host = components.host else { return }
        components.path = "/"
        components.query = "pet-preview&view=full&ios=1"
        guard let page = components.url else { return }
        let cookie = HTTPCookie(properties: [
            .domain: host,
            .path: "/",
            .name: "vivieen-token",
            .value: token,
            .expires: Date(timeIntervalSinceNow: 3600 * 24 * 365),
        ])
        let store = webView.configuration.websiteDataStore.httpCookieStore
        let start = { webView.load(URLRequest(url: page)) }
        if let cookie {
            store.setCookie(cookie) { DispatchQueue.main.async { _ = start() } }
        } else {
            _ = start()
        }
    }

    final class Coordinator: NSObject, WKUIDelegate, WKScriptMessageHandler {
        func userContentController(_ controller: WKUserContentController,
                                   didReceive message: WKScriptMessage) {
            NSLog("[viv-web] %@", String(describing: message.body))
        }

        func webView(_ webView: WKWebView,
                     requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                     initiatedByFrame frame: WKFrameInfo,
                     type: WKMediaCaptureType,
                     decisionHandler: @escaping (WKPermissionDecision) -> Void) {
            decisionHandler(type == .microphone ? .grant : .deny)
        }
    }
}
