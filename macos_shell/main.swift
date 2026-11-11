// macos_shell/main.swift — Native AppKit + WKWebView shell for Folio on macOS.
// Launches `folio-locald` on 127.0.0.1 with an ephemeral CSPRNG token and allowlisted environment,
// serves bundled UI assets via WKURLSchemeHandler (`folio://localhost/`), installs standard macOS
// menus (Cmd+A/C/V/X/Z/Q/W/M/H), applies NSVisualEffectView vibrancy + full-size content view,
// injects `window.__FOLIO_DESKTOP__` (`forMainFrameOnly: true`), and confines navigation.
import AppKit
import Darwin
import Security
import WebKit

final class FolioBundleSchemeHandler: NSObject, WKURLSchemeHandler {
    private let rootURL: URL

    init(rootURL: URL) {
        self.rootURL = rootURL.standardizedFileURL.resolvingSymlinksInPath()
    }

    private static func mimeType(for ext: String) -> String {
        switch ext.lowercased() {
        case "html": return "text/html; charset=utf-8"
        case "js", "mjs": return "text/javascript; charset=utf-8"
        case "css": return "text/css; charset=utf-8"
        case "json": return "application/json; charset=utf-8"
        case "svg": return "image/svg+xml"
        case "png": return "image/png"
        case "jpg", "jpeg": return "image/jpeg"
        case "webp": return "image/webp"
        case "wasm": return "application/wasm"
        case "woff2": return "font/woff2"
        case "woff": return "font/woff"
        case "ttf": return "font/ttf"
        default: return "application/octet-stream"
        }
    }

    func webView(_ webView: WKWebView, start urlSchemeTask: WKURLSchemeTask) {
        guard let reqURL = urlSchemeTask.request.url else {
            urlSchemeTask.didFailWithError(URLError(.badURL))
            return
        }
        var relPath = reqURL.path
        while relPath.hasPrefix("/") { relPath.removeFirst() }
        if relPath.isEmpty { relPath = "index.html" }

        let candidate = rootURL.appendingPathComponent(relPath).standardizedFileURL.resolvingSymlinksInPath()
        let rootPrefix = rootURL.path.hasSuffix("/") ? rootURL.path : (rootURL.path + "/")
        var target = candidate
        var isDir: ObjCBool = false
        let inRoot = (candidate.path == rootURL.path) || candidate.path.hasPrefix(rootPrefix)
        if !inRoot || !FileManager.default.fileExists(atPath: candidate.path, isDirectory: &isDir) || isDir.boolValue {
            if URL(fileURLWithPath: relPath).pathExtension.isEmpty {
                target = rootURL.appendingPathComponent("index.html").standardizedFileURL.resolvingSymlinksInPath()
            } else {
                let resp = HTTPURLResponse(url: reqURL, statusCode: 404, httpVersion: "HTTP/1.1", headerFields: [
                    "Content-Type": "text/plain; charset=utf-8",
                    "Access-Control-Allow-Origin": "folio://localhost"
                ])!
                urlSchemeTask.didReceive(resp)
                urlSchemeTask.didFinish()
                return
            }
        }
        guard let data = try? Data(contentsOf: target) else {
            urlSchemeTask.didFailWithError(URLError(.fileDoesNotExist))
            return
        }
        let ext = target.pathExtension.lowercased()
        let isImmut = relPath.hasPrefix("assets/") || ext == "woff2" || ext == "woff" || ext == "ttf" || ext == "wasm"
        let resp = HTTPURLResponse(url: reqURL, statusCode: 200, httpVersion: "HTTP/1.1", headerFields: [
            "Content-Type": Self.mimeType(for: target.pathExtension),
            "Content-Length": String(data.count),
            "Access-Control-Allow-Origin": "folio://localhost",
            "Cache-Control": isImmut ? "public, max-age=31536000, immutable" : "no-cache"
        ])!
        urlSchemeTask.didReceive(resp)
        urlSchemeTask.didReceive(data)
        urlSchemeTask.didFinish()
    }

    func webView(_ webView: WKWebView, stop urlSchemeTask: WKURLSchemeTask) {}
}

final class FolioAppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private var window: NSWindow?
    private var webView: WKWebView?
    private var daemonProcess: Process?
    private let port: Int
    private let token: String

    override init() {
        let envPort = Int(ProcessInfo.processInfo.environment["FOLIO_LOCALD_PORT"] ?? "") ?? 19485
        self.port = (1024...65535).contains(envPort) ? envPort : 19485
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        self.token = bytes.map { String(format: "%02x", $0) }.joined()
        super.init()
    }

    private func buildInitScript() -> String {
        let escapedToken = String(reflecting: token)
        return """
        (() => {
          const _fetch = window.fetch.bind(window), _jsonStringify = JSON.stringify;
          const FOLIO_LOCALD_BASE = "http://127.0.0.1:\(port)", FOLIO_LOCALD_URL = FOLIO_LOCALD_BASE + "/__folio_rpc", FOLIO_EVENTS_URL = FOLIO_LOCALD_BASE + "/__folio_events", FOLIO_TOKEN = \(escapedToken);
          const listeners = new Map(); let streamCtrl = null;
          const hasListeners = () => { for (const s of listeners.values()) if (s.size) return true; return false; };
          const emit = (name, payload) => { const s = listeners.get(name); if (s) for (const h of Array.from(s)) { try { h({ event: name, payload }); } catch {} } };
          const dispatch = (ev) => { if (ev && typeof ev === 'object' && typeof ev.event === 'string') emit(ev.event, ev.payload); };
          async function drain() {
            if (!hasListeners()) return;
            try {
              const r = await _fetch(FOLIO_LOCALD_URL, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Folio-Token': FOLIO_TOKEN }, body: _jsonStringify({ command: 'poll_events', args: {} }) });
              const b = await r.json();
              if (Array.isArray(b.result)) for (const ev of b.result) dispatch(ev);
            } catch {}
          }
          const stopStream = () => { if (streamCtrl) { streamCtrl.abort(); streamCtrl = null; } };
          function ensureStream() {
            if (streamCtrl || !hasListeners()) return;
            const ctrl = streamCtrl = new AbortController();
            (async () => {
              while (!ctrl.signal.aborted && hasListeners()) {
                try {
                  const r = await _fetch(FOLIO_EVENTS_URL, { headers: { 'Accept': 'text/event-stream', 'X-Folio-Token': FOLIO_TOKEN }, signal: ctrl.signal });
                  if (!r.ok || !r.body) throw new Error('sse');
                  const rd = r.body.getReader(), dec = new TextDecoder(); let buf = '';
                  while (!ctrl.signal.aborted && hasListeners()) {
                    const { value, done } = await rd.read(); if (done) break;
                    buf += dec.decode(value, { stream: true }); let idx;
                    while ((idx = buf.indexOf('\\n\\n')) >= 0) {
                      const fr = buf.slice(0, idx); buf = buf.slice(idx + 2);
                      for (const ln of fr.split('\\n')) { if (ln.startsWith('data: ')) dispatch(JSON.parse(ln.slice(6))); }
                    }
                  }
                } catch {
                  if (ctrl.signal.aborted || !hasListeners()) break;
                  await new Promise(w => setTimeout(w, 900));
                  if (!ctrl.signal.aborted && hasListeners()) await drain();
                }
              }
              if (streamCtrl === ctrl) streamCtrl = null;
            })();
          }
          async function invoke(command, args = {}) {
            if (command === "set_window_theme") {
              window.webkit?.messageHandlers?.folioTheme?.postMessage(args?.theme ?? "dark");
            }
            let r; for (let i = 0; ; i++) {
              try {
                r = await _fetch(FOLIO_LOCALD_URL, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Folio-Token': FOLIO_TOKEN }, body: _jsonStringify({ command, args: args || {} }) });
                break;
              } catch (e) { if (i >= 20) throw e; await new Promise(w => setTimeout(w, 80)); }
            }
            const b = await r.json();
            if (!r.ok || b.error) throw new Error(b.error || command);
            return b.result;
          }
          async function listen(n, h) {
            if (!listeners.has(n)) listeners.set(n, new Set());
            listeners.get(n).add(h); ensureStream();
            return () => { listeners.get(n)?.delete(h); if (!listeners.get(n)?.size) listeners.delete(n); if (!hasListeners()) stopStream(); };
          }
          const assetUrl = (uri, rel) => FOLIO_LOCALD_BASE + '/__folio_asset?uri=' + encodeURIComponent(uri || '') + '&rel=' + encodeURIComponent(rel || '') + '&token=' + encodeURIComponent(FOLIO_TOKEN);
          window.__FOLIO_DESKTOP__ = window.__ORB_DESKTOP__ = { core: { invoke, assetUrl }, event: { listen } };
        })();
        """
    }

    private func installMenus() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Hide Folio", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Quit Folio", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        mainMenu.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu

        let winItem = NSMenuItem()
        mainMenu.addItem(winItem)
        let winMenu = NSMenu(title: "Window")
        winMenu.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winItem.submenu = winMenu

        NSApp.mainMenu = mainMenu
    }

    private func spawnDaemon() {
        guard let bin = ProcessInfo.processInfo.environment["FOLIO_LOCALD_BIN"], !bin.isEmpty else { return }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: bin)
        let keep = [
            "HOME", "PATH", "TMPDIR", "USER", "LANG",
            "FOLIO_STATE_DIR", "FOLIO_UI_ROOT", "FOLIO_DEFAULT_NOTES_DIR", "FOLIO_NOTES_DIRS",
            "FOLIO_SECRETS_DIR", "FOLIO_SECRET_HELPER", "FOLIO_THEME_MODE",
            "FOLIO_HINDSIGHT_URL", "FOLIO_DEFAULT_BANK", "FOLIO_SYNC_BANK",
            "FOLIO_ORBROUTER_URL", "FOLIO_ORBROUTER_API_KEY", "FOLIO_ORBROUTER_KEY_FILE",
            "ORBROUTER_API_KEY", "ORB_API_URL", "ORB_STATE_DIR",
            "HINDSIGHT_API_URL", "HINDSIGHT_BASE_URL", "HINDSIGHT_BANK_ID",
            "HINDSIGHT_API_KEY", "HINDSIGHT_API_KEY_FILE",
            "SPARK_MEMORY_API_TOKEN", "SPARK_MEMORY_API_TOKEN_FILE",
            "FOLIO_HINDSIGHT_READONLY",
            "NOTION_API_KEY", "NOTION_API_KEY_FILE", "NOTION_API_TOKEN", "NOTION_API_TOKEN_FILE",
            "NOTION_API_KEY_PERSO", "NOTION_API_KEY_PERSO_FILE",
            "SSL_CERT_FILE", "SSL_CERT_DIR"
        ]
        var env: [String: String] = [
            "FOLIO_LOCALD_PORT": String(port),
            "FOLIO_LOCALD_TOKEN": token
        ]
        let hostEnv = ProcessInfo.processInfo.environment
        for k in keep {
            if let v = hostEnv[k] { env[k] = v }
        }
        proc.environment = env
        try? proc.run()
        self.daemonProcess = proc
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        installMenus()
        spawnDaemon()

        let cfg = WKWebViewConfiguration()
        let ucm = WKUserContentController()
        let script = WKUserScript(source: buildInitScript(), injectionTime: .atDocumentStart, forMainFrameOnly: true)
        ucm.addUserScript(script)
        cfg.userContentController = ucm

        if let uiRoot = ProcessInfo.processInfo.environment["FOLIO_UI_ROOT"] {
            cfg.setURLSchemeHandler(FolioBundleSchemeHandler(rootURL: URL(fileURLWithPath: uiRoot)), forURLScheme: "folio")
        }

        let rect = NSRect(x: 0, y: 0, width: 1380, height: 880)
        let win = NSWindow(
            contentRect: rect,
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        win.title = "Folio"
        win.titleVisibility = .hidden
        win.titlebarAppearsTransparent = true
        win.isOpaque = false
        win.backgroundColor = .clear
        win.minSize = NSSize(width: 720, height: 480)

        let vibrancy = NSVisualEffectView(frame: rect)
        vibrancy.material = .sidebar
        vibrancy.blendingMode = .behindWindow
        vibrancy.state = .active
        vibrancy.autoresizingMask = [.width, .height]

        let wv = WKWebView(frame: rect, configuration: cfg)
        wv.navigationDelegate = self
        wv.setValue(false, forKey: "drawsBackground")
        wv.autoresizingMask = [.width, .height]
        vibrancy.addSubview(wv)
        win.contentView = vibrancy

        let targetUrl: URL
        if let devUrl = ProcessInfo.processInfo.environment["FOLIO_DEV_URL"], let u = URL(string: devUrl) {
            targetUrl = u
        } else if ProcessInfo.processInfo.environment["FOLIO_UI_ROOT"] != nil {
            targetUrl = URL(string: "folio://localhost/")!
        } else {
            targetUrl = URL(string: "http://127.0.0.1:\(port)/")!
        }
        wv.load(URLRequest(url: targetUrl))

        win.center()
        win.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        self.window = win
        self.webView = wv
    }

    func applicationWillTerminate(_ notification: Notification) {
        daemonProcess?.terminate()
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        let scheme = (url.scheme ?? "").lowercased()
        let host = (url.host ?? "").lowercased()
        if scheme == "folio" || (scheme == "http" && (host == "127.0.0.1" || host == "localhost")) {
            decisionHandler(.allow)
        } else {
            if scheme == "https" || scheme == "http" {
                NSWorkspace.shared.open(url)
            }
            decisionHandler(.cancel)
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = FolioAppDelegate()
app.delegate = delegate
app.run()
