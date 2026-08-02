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
            document.addEventListener('click', function(e) {
              try {
                var id = e.target.closest('button') ? e.target.closest('button').id : e.target.tagName;
                window.webkit.messageHandlers.viv.postMessage('CLICK ' + id);
                if (id === 'rail-move') setTimeout(function() {
                  window.webkit.messageHandlers.viv.postMessage('MOVESTATE clip=' +
                    (MOTION.move ? (MOTION.move.video ? 'video' : 'sheets' + (MOTION.move.sheets || []).length) : 'null') +
                    ' active=' + moveShowActive() + ' until=' + Math.round(moveShowUntil - performance.now()) +
                    ' drew=' + drawMotionClip('move', performance.now()));
                }, 300);
              } catch (x) {
                try { window.webkit.messageHandlers.viv.postMessage('CLICKERR ' + x); } catch (y) {}
              }
            }, true);
            document.addEventListener('pointerdown', function(e) {
              try { window.webkit.messageHandlers.viv.postMessage('PDOWN ' +
                (e.target.closest('button') ? e.target.closest('button').id : e.target.tagName) +
                ' ' + Math.round(e.clientX) + ',' + Math.round(e.clientY)); } catch (x) {}
            }, true);
            setTimeout(function() {
              try {
                var st = document.getElementById('st');
                var rail = document.getElementById('rail');
                window.webkit.messageHandlers.viv.postMessage('STATE ' +
                  ' cls=' + document.documentElement.className +
                  ' status=' + (st ? st.textContent : '?') +
                  ' motion=' + (typeof MOTION !== 'undefined' ? Object.keys(MOTION).join('+') : 'n/a') +
                  ' moveVideo=' + (typeof MOTION !== 'undefined' && MOTION.move ? Boolean(MOTION.move.video) : '?') +
                  ' idleVideo=' + (typeof MOTION !== 'undefined' && MOTION.idle ? Boolean(MOTION.idle.video) : '?') +
                  ' rail=' + (rail ? getComputedStyle(rail).display : 'none'));
                var caps = document.createElement('video');
                window.webkit.messageHandlers.viv.postMessage('CPT vp9=[' +
                  caps.canPlayType('video/webm; codecs="vp9"') + '] hvc1=[' +
                  caps.canPlayType('video/mp4; codecs="hvc1"') + '] qt=[' +
                  caps.canPlayType('video/quicktime') + ']');
                var probeVideo = document.createElement('video');
                probeVideo.muted = true; probeVideo.playsInline = true; probeVideo.preload = 'auto';
                probeVideo.onloadedmetadata = function() {
                  window.webkit.messageHandlers.viv.postMessage('MOV meta ' +
                    probeVideo.videoWidth + 'x' + probeVideo.videoHeight); };
                probeVideo.oncanplaythrough = function() {
                  window.webkit.messageHandlers.viv.postMessage('MOV canplaythrough'); };
                probeVideo.onerror = function() {
                  window.webkit.messageHandlers.viv.postMessage('MOV error ' +
                    (probeVideo.error ? probeVideo.error.code + ' ' + (probeVideo.error.message || '') : '?')); };
                probeVideo.src = 'assets/motion-move.mov';
                probeVideo.load();
              } catch (e) { window.webkit.messageHandlers.viv.postMessage('STATE fail ' + e); }
            }, 5000);
            """, injectionTime: .atDocumentStart, forMainFrameOnly: true)
        configuration.userContentController.addUserScript(probe)
        configuration.userContentController.add(context.coordinator, name: "viv")
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.uiDelegate = context.coordinator
        webView.navigationDelegate = context.coordinator
        webView.isOpaque = false
        webView.backgroundColor = .clear
        // The avatar stage never scrolls (the page pins itself), but the
        // Character Studio and any other page needs real scrolling.
        webView.scrollView.isScrollEnabled = false
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        load(into: webView)
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {}

    private func load(into webView: WKWebView) {
        guard var components = URLComponents(string: address),
              let host = components.host else { return }
        // The decoupled web view, not the pet overlay: chat, hold-to-talk,
        // and spoken replies are all self-contained there, while the pet
        // page's gestures ride on Electron IPC the phone does not have.
        components.path = "/"
        components.query = "view=full&ios=1"
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

    final class Coordinator: NSObject, WKUIDelegate, WKNavigationDelegate,
                             WKScriptMessageHandler {
        func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
            let path = webView.url?.path ?? "/"
            webView.scrollView.isScrollEnabled = !(path == "/" || path.isEmpty)
        }

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
