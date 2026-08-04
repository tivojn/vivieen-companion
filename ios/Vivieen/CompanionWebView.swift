import SwiftUI
import WebKit
import ObjectiveC.runtime

/// WKWebView glues iOS's "form assistant" accessory bar (field arrows +
/// Done) onto every focused input - a dead pill that sat on top of the
/// chat field (owner: "what's the point of this layer with a check").
/// The single-input app has no fields to navigate; remove the bar by
/// giving WKContentView an inputAccessoryView of nil.
private func removeInputAccessory(from webView: WKWebView) {
    guard let target = webView.scrollView.subviews.first(where: {
        String(describing: type(of: $0)).hasPrefix("WKContent")
    }) else { return }
    let subclassName = "VivieenNoAccessoryContentView"
    let subclass: AnyClass
    if let existing = NSClassFromString(subclassName) {
        subclass = existing
    } else {
        guard let superclass = object_getClass(target),
              let created = objc_allocateClassPair(superclass, subclassName, 0)
        else { return }
        let selector = #selector(getter: UIResponder.inputAccessoryView)
        let block: @convention(block) (AnyObject) -> UIView? = { _ in nil }
        class_addMethod(created, selector,
                        imp_implementationWithBlock(block), "@@:")
        objc_registerClassPair(created)
        subclass = created
    }
    object_setClass(target, subclass)
}

/// A Swift string as a JS literal, quotes and all.
private func jsString(_ value: String) -> String {
    let data = try? JSONSerialization.data(withJSONObject: [value])
    let text = data.flatMap { String(data: $0, encoding: .utf8) } ?? "[\"\"]"
    return String(text.dropFirst().dropLast())
}

/// The Mac's renderer, unchanged, inside WKWebView. Auth rides on a
/// cookie (the server accepts it as an equal of the Electron header)
/// because WebKit cannot attach a header to every subresource and
/// websocket. Mic capture is granted natively so hold-to-talk, realtime
/// dictation, and live talk all work.
struct CompanionWebView: UIViewRepresentable {
    let address: String
    let token: String
    /// True once her page is actually on screen. The app's own gear hides
    /// itself then, so the page's gear is the only "settings" in sight.
    @Binding var pageLive: Bool

    func makeCoordinator() -> Coordinator {
        Coordinator(pageLive: { live in
            DispatchQueue.main.async { self.pageLive = live }
        })
    }

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
                if (id === 'rail-close' || id === 'rail-zoom') setTimeout(function() {
                  try {
                    window.webkit.messageHandlers.viv.postMessage('ZOOMSTATE ios_zoom=' + IOS_ZOOM +
                      ' anchor=' + IOS_HEAD_ANCHOR +
                      ' cls=' + document.documentElement.className +
                      ' zbox=' + getComputedStyle(document.getElementById('zoombox')).display);
                  } catch (z) { window.webkit.messageHandlers.viv.postMessage('ZOOMSTATE fail ' + z); }
                }, 250);
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
            setInterval(function() {
              try {
                var t = document.getElementById('thread');
                if (!t || !t.children.length) return;
                var v = t.querySelector('video');
                var r = t.getBoundingClientRect();
                var c = t.lastElementChild.getBoundingClientRect();
                window.webkit.messageHandlers.viv.postMessage('THREAD n=' + t.children.length +
                  ' rect=' + Math.round(r.left) + ',' + Math.round(r.top) + ',' + Math.round(r.width) +
                  ' card=' + Math.round(c.left) + ',' + Math.round(c.width) +
                  ' sl=' + t.scrollLeft +
                  (v ? ' vidH=' + Math.round(v.getBoundingClientRect().height) +
                       ' vidCSS=' + getComputedStyle(v).maxHeight + '/' + getComputedStyle(v).height : ' novid'));
              } catch (e) {}
            }, 4000);
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
        // A websocket cannot be opened against viv://app - location.host
        // is literally "app" - and it cannot carry a cookie or a header
        // cross-origin either, so live talk had nowhere to connect and
        // nothing to identify itself with (owner, 2026-08-03). Hand the
        // page the Mac's real address and the token; sockets use them
        // directly, which also makes it plain that live talk is a
        // same-network feature.
        let lan = WKUserScript(source: """
            window.VIV_LAN = \(jsString(address));
            window.VIV_TOKEN = \(jsString(token));
            """, injectionTime: .atDocumentStart, forMainFrameOnly: true)
        configuration.userContentController.addUserScript(lan)
        configuration.userContentController.addUserScript(probe)
        configuration.userContentController.add(context.coordinator, name: "viv")
        configuration.userContentController.add(context.coordinator, name: "pip")
        configuration.userContentController.add(context.coordinator, name: "mic")
        configuration.userContentController.add(context.coordinator, name: "live")
        configuration.userContentController.add(context.coordinator, name: "audio")
        configuration.userContentController.add(context.coordinator, name: "share")
        configuration.userContentController.add(context.coordinator, name: "speech")
        // One origin, wherever she is: viv://app is served from the cache,
        // the Mac, or the relay, in that order (VivScheme).
        let scheme = VivSchemeHandler(
            address: address, token: token,
            relayBase: UserDefaults.standard.string(forKey: "relayBase")
                ?? RelayClient.defaultBase)
        // A changed face reaches the page at once, not next launch.
        scheme.onAvatarChanged = { [weak coordinator = context.coordinator] in
            coordinator?.webView?.evaluateJavaScript(
                "window.__vivAvatarChanged&&__vivAvatarChanged()",
                completionHandler: nil)
        }
        configuration.setURLSchemeHandler(scheme,
                                          forURLScheme: VivSchemeHandler.scheme)
        configuration.userContentController.add(context.coordinator, name: "body")
        context.coordinator.scheme = scheme
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.uiDelegate = context.coordinator
        webView.navigationDelegate = context.coordinator
        webView.isOpaque = false
        webView.backgroundColor = .clear
        // The avatar stage never scrolls (the page pins itself), but the
        // Character Studio and any other page needs real scrolling.
        webView.scrollView.isScrollEnabled = false
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        // Her gestures are taps and double-taps; WebKit's own double-tap
        // zoom hijacked them and left the whole page scaled and panned -
        // head cut off, controls off-screen (owner screenshot,
        // 2026-08-02). The page never zooms; SHE does, via the slider.
        webView.scrollView.minimumZoomScale = 1
        webView.scrollView.maximumZoomScale = 1
        webView.scrollView.bouncesZoom = false
        webView.scrollView.pinchGestureRecognizer?.isEnabled = false
        context.coordinator.pip.webView = webView
        context.coordinator.mic.webView = webView
        DispatchQueue.main.async {
            context.coordinator.pip.attach(to: webView)
            removeInputAccessory(from: webView)
        }
        load(into: webView, coordinator: context.coordinator)
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {}

    private func load(into webView: WKWebView, coordinator: Coordinator) {
        // The decoupled web view, not the pet overlay: chat, hold-to-talk,
        // and spoken replies are all self-contained there, while the pet
        // page's gestures ride on Electron IPC the phone does not have.
        //
        // viv://app, wherever she is: cache, then the Mac, then the relay.
        // WKURLSchemeHandler never hands over a POST BODY from fetch - so
        // dictation, chat and the agent lane all arrived empty ("I did not
        // catch that") until the page started parking bodies with the app
        // first (see the body bridge in index.html and BodyStore here).
        guard let page = URL(string:
            "\(VivSchemeHandler.scheme)://\(VivSchemeHandler.host)/?view=full&ios=1")
        else { return }
        coordinator.pageURL = page
        webView.load(URLRequest(url: page))
    }

    final class Coordinator: NSObject, WKUIDelegate, WKNavigationDelegate,
                             WKScriptMessageHandler {
        let pip = PipDriver()
        let mic = MicDriver.shared
        let speech = SpeechPlayer.shared
        weak var scheme: VivSchemeHandler?
        weak var webView: WKWebView?
        var pageURL: URL?
        private let pageLive: (Bool) -> Void

        init(pageLive: @escaping (Bool) -> Void) {
            self.pageLive = pageLive
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            self.webView = webView
            pageLive(true)
            // Every successful load is a chance to refresh solo's keys
            // and config while the Mac is in reach.
            scheme?.syncSolo()
        }

        func webView(_ webView: WKWebView,
                     didFail navigation: WKNavigation!, withError error: Error) {
            pageLive(false)
        }

        func webView(_ webView: WKWebView,
                     didFailProvisionalNavigation navigation: WKNavigation!,
                     withError error: Error) {
            // The Mac's engine may still be restarting; a silently failed
            // load left a STALE page rendered (three debugging rounds,
            // 2026-08-02). Retry until she answers.
            guard let pageURL else { return }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                webView.load(URLRequest(url: pageURL))
            }
        }

        func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
            let path = webView.url?.path ?? "/"
            webView.scrollView.isScrollEnabled = !(path == "/" || path.isEmpty)
        }

        func userContentController(_ controller: WKUserContentController,
                                   didReceive message: WKScriptMessage) {
            if message.name == "pip" {
                guard let body = message.body as? String else { return }
                if body == "start" { pip.start() }
                else if body == "stop" { pip.stop() }
                else { pip.enqueue(dataURL: body) }
                return
            }
            if message.name == "body" {
                // "<ticket>:<base64 body>" - parked until the matching
                // request arrives, because WebKit will not carry it.
                guard let text = message.body as? String,
                      let split = text.firstIndex(of: ":") else { return }
                let ticket = String(text[text.startIndex..<split])
                let encoded = String(text[text.index(after: split)...])
                if let data = Data(base64Encoded: encoded) {
                    scheme?.park(id: ticket, body: data)
                }
                return
            }
            if message.name == "speech" {
                // "<rate>:<base64 pcm16>", or "flush" for barge-in.
                guard let body = message.body as? String else { return }
                if body == "flush" { speech.flush(); return }
                if body == "stop" { speech.stop(); return }
                guard let split = body.firstIndex(of: ":") else { return }
                let rate = Double(body[body.startIndex..<split]) ?? 24000
                speech.enqueue(base64: String(body[body.index(after: split)...]),
                               rate: rate)
                return
            }
            if message.name == "audio" {
                // The page asks what the OS actually did with the route.
                let line = AudioSession.describe()
                mic.webView?.evaluateJavaScript(
                    "window.__vivAudioState&&__vivAudioState('\(line)')",
                    completionHandler: nil)
                return
            }
            if message.name == "share" {
                // Anything she delivered - image, clip, pdf - handed to
                // the system sheet, so it can be saved, aired or sent on.
                guard let url = message.body as? String else { return }
                share(urlString: url)
                return
            }
            // Live talk with no Mac: the page asks, the app holds the
            // socket. It has to be the app - a backgrounded WebView is
            // suspended and would take a page-held socket with it.
            if message.name == "live" {
                guard let body = message.body as? String else { return }
                if body == "start" {
                    LiveTap.shared.webView = message.webView
                    LiveTap.shared.start()
                } else {
                    LiveTap.shared.stop(body == "stop" ? "ended" : body)
                }
                return
            }
            if message.name == "mic" {
                guard let body = message.body as? String else { return }
                if body.hasPrefix("start:") {
                    mic.start(rate: Double(body.dropFirst(6)) ?? 16000)
                } else if body == "stop" {
                    mic.stop()
                }
                return
            }
            NSLog("[viv-web] %@", String(describing: message.body))
        }

        /// Pull the file off the Mac, then let iOS decide what can be done
        /// with it: Save to Photos, Save to Files, AirDrop, Messages. One
        /// path serves images, clips and documents alike.
        private func share(urlString: String) {
            guard let webView = mic.webView,
                  let url = URL(string: urlString, relativeTo: webView.url)
            else { return }
            let task = URLSession.shared.dataTask(with: url) { data, response, _ in
                guard let data else { return }
                let suggested = response?.suggestedFilename
                    ?? url.lastPathComponent
                let file = FileManager.default.temporaryDirectory
                    .appendingPathComponent(suggested.isEmpty ? "vivieen" : suggested)
                try? data.write(to: file)
                DispatchQueue.main.async {
                    guard let root = webView.window?.rootViewController
                    else { return }
                    let sheet = UIActivityViewController(
                        activityItems: [file], applicationActivities: nil)
                    // iPad needs an anchor or it refuses to present.
                    sheet.popoverPresentationController?.sourceView = webView
                    sheet.popoverPresentationController?.sourceRect = CGRect(
                        x: webView.bounds.midX, y: webView.bounds.maxY - 80,
                        width: 1, height: 1)
                    root.present(sheet, animated: true)
                }
            }
            task.resume()
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
