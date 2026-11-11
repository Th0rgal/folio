/*
 * linux_shell/main.c — Hardened GTK4 + WebKitGTK 6.0 native Wayland/Linux desktop shell for Folio.
 * Serves FOLIO_UI_ROOT over custom `folio://localhost/` scheme (instant cold paint + stable WebKit origin),
 * spawns folio-locald with PDEATHSIG + ephemeral CSPRNG token and full XDG/Nix environment allowlist,
 * wires native GtkFileDialog folder/file pickers + GtkGestureZoom touchpad pinch + single-instance CLI open,
 * pauses animations when unfocused, injects top-frame-only window.__FOLIO_DESKTOP__, and confines navigation.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#if defined(FOLIO_HAS_WEBKITGTK)
#include <glib-unix.h>
#include <gtk/gtk.h>
#include <webkit/webkit.h>
#endif

static int parse_valid_port(const char *s, unsigned *out_port) {
    if (!s || !*s) return 0;
    unsigned v = 0;
    for (const char *p = s; *p; ++p)
        if (*p < '0' || *p > '9' || (v = v * 10u + (unsigned)(*p - '0')) > 65535u) return 0;
    return v >= 1024u ? (*out_port = v, 1) : 0;
}

static unsigned pick_ephemeral_port(unsigned fallback) {
    int s = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (s < 0) return fallback;
    struct sockaddr_in sa = {
        .sin_family = AF_INET,
        .sin_port = 0,
        .sin_addr = { .s_addr = htonl(INADDR_LOOPBACK) }
    };
    socklen_t sl = sizeof(sa);
    unsigned p = (bind(s, (struct sockaddr *)&sa, sizeof(sa)) == 0 &&
                  getsockname(s, (struct sockaddr *)&sa, &sl) == 0)
                     ? ntohs(sa.sin_port)
                     : fallback;
    close(s);
    return p >= 1024u ? p : fallback;
}

static int gen_csprng_hex(char out_hex[65]) {
    unsigned char raw[32];
    int fd = open("/dev/urandom", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    for (size_t got = 0; got < sizeof(raw); ) {
        ssize_t n = read(fd, raw + got, sizeof(raw) - got);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { close(fd); return -1; }
        got += (size_t)n;
    }
    close(fd);
    static const char hex[] = "0123456789abcdef";
    for (size_t i = 0; i < 32; ++i) {
        out_hex[i * 2] = hex[(raw[i] >> 4) & 0x0F];
        out_hex[i * 2 + 1] = hex[raw[i] & 0x0F];
    }
    out_hex[64] = '\0';
    return 0;
}

static char *json_escape_string(const char *s) {
    if (!s) return NULL;
    size_t len = strlen(s), j = 0;
    if (len > 8192) return NULL;
    char *out = malloc(len * 6 + 4);
    if (!out) return NULL;
    out[j++] = '"';
    for (size_t i = 0; i < len; ++i) {
        unsigned char c = (unsigned char)s[i];
        if (c == '"' || c == '\\') { out[j++] = '\\'; out[j++] = (char)c; }
        else if (c < 0x20 || c == 0x7F) { j += (size_t)snprintf(out + j, 8, "\\u%04x", c); }
        else { out[j++] = (char)c; }
    }
    out[j++] = '"';
    out[j] = '\0';
    return out;
}

static char *uri_encode_component(const char *s) {
    if (!s) return NULL;
    size_t len = strlen(s), j = 0;
    if (len > 4096) return NULL;
    char *out = malloc(len * 3 + 1);
    if (!out) return NULL;
    static const char hex[] = "0123456789ABCDEF";
    for (size_t i = 0; i < len; ++i) {
        unsigned char c = (unsigned char)s[i];
        if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
            c == '-' || c == '_' || c == '.' || c == '~') {
            out[j++] = (char)c;
        } else {
            out[j++] = '%';
            out[j++] = hex[(c >> 4) & 0x0F];
            out[j++] = hex[c & 0x0F];
        }
    }
    out[j] = '\0';
    return out;
}

static int is_confined_shell_uri(const char *u) {
    if (!u || !*u) return 0;
    if (strncmp(u, "file:///", 8) == 0 || strcmp(u, "about:blank") == 0) return 1;
    if (strncmp(u, "folio://localhost", 17) == 0) {
        char c = u[17];
        return c == '\0' || c == '/' || c == '?' || c == '#';
    }
    if (strncmp(u, "tauri://localhost", 17) == 0) {
        char c = u[17];
        return c == '\0' || c == '/' || c == '?' || c == '#';
    }
    const char *rest = strncmp(u, "http://127.0.0.1", 16) == 0 ? u + 16 :
                       (strncmp(u, "http://localhost", 16) == 0 ? u + 16 : NULL);
    if (!rest) return 0;
    if (*rest == ':') {
        if (*++rest < '1' || *rest > '9') return 0;
        for (unsigned p = 0; *rest >= '0' && *rest <= '9'; ) {
            if ((p = p * 10u + (unsigned)(*rest++ - '0')) > 65535u) return 0;
        }
    }
    return *rest == '\0' || *rest == '/' || *rest == '?' || *rest == '#';
}

static void focus_hypr_class(const char *cls) {
    const char *rt = getenv("XDG_RUNTIME_DIR");
    const char *sig = getenv("HYPRLAND_INSTANCE_SIGNATURE");
    if (!rt || !*rt || !sig || !*sig) return;
    struct sockaddr_un a = { .sun_family = AF_UNIX };
    if ((size_t)snprintf(a.sun_path, sizeof(a.sun_path), "%s/hypr/%s/.socket.sock", rt, sig) >= sizeof(a.sun_path))
        return;
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd >= 0 && connect(fd, (struct sockaddr *)&a, sizeof(a)) == 0) {
        char cmd[128];
        int n = snprintf(cmd, sizeof(cmd), "dispatch focuswindow class:^%s$", cls);
        if (n > 0) (void)send(fd, cmd, (size_t)n, MSG_NOSIGNAL);
    }
    if (fd >= 0) close(fd);
}

static void forward_target_to_primary(const char *rt_dir, const char *target) {
    if (!rt_dir || !*rt_dir || !target || !*target) return;
    struct sockaddr_un a = { .sun_family = AF_UNIX };
    if ((size_t)snprintf(a.sun_path, sizeof(a.sun_path), "%s/folio-desktop.sock", rt_dir) >= sizeof(a.sun_path))
        return;
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd >= 0 && connect(fd, (struct sockaddr *)&a, sizeof(a)) == 0) {
        size_t len = strlen(target);
        if (len <= 4096) (void)send(fd, target, len, MSG_NOSIGNAL);
    }
    if (fd >= 0) close(fd);
}

static char *resolve_cli_target(int argc, char **argv) {
    for (int i = 1; i < argc; ++i) {
        const char *arg = argv[i];
        if (!arg || !*arg || arg[0] == '-') continue;
        if (strncmp(arg, "file://", 7) == 0) {
            const char *p = arg + 7;
            if (strncmp(p, "localhost/", 10) == 0) p += 9;
            return strdup(p);
        }
        if (strncmp(arg, "local://", 8) == 0 || strncmp(arg, "hindsight://", 12) == 0 ||
            strncmp(arg, "notion://", 9) == 0 || strncmp(arg, "kp-", 3) == 0 || strncmp(arg, "src-", 4) == 0) {
            return strdup(arg);
        }
        char real_buf[PATH_MAX];
        if (realpath(arg, real_buf) != NULL) {
            return strdup(real_buf);
        }
        return strdup(arg);
    }
    return NULL;
}

static char *build_init_script(unsigned port, const char *token) {
    char *tok_lit = json_escape_string(token);
    if (!tok_lit) return NULL;
    static const char tmpl[] =
        "(() => {\n"
        "  if (document.documentElement) document.documentElement.dataset.platform = 'linux';\n"
        "  let sup = false;\n"
        "  const isSup = (e) => e.key === 'Meta' || e.key === 'Super' || e.code === 'MetaLeft' || e.code === 'MetaRight' || e.code === 'OSLeft' || e.code === 'OSRight';\n"
        "  window.addEventListener('keydown', (e) => { if (isSup(e)) sup = true; }, true);\n"
        "  window.addEventListener('keyup', (e) => { if (isSup(e)) sup = false; }, true);\n"
        "  window.addEventListener('blur', () => { sup = false; }, true);\n"
        "  for (const P of [KeyboardEvent.prototype, MouseEvent.prototype]) {\n"
        "    const d = Object.getOwnPropertyDescriptor(P, 'metaKey');\n"
        "    if (d?.get) Object.defineProperty(P, 'metaKey', { configurable: true, enumerable: true, get() { return d.get.call(this) || ((sup || !!this.getModifierState?.('Super')) && !this.ctrlKey); } });\n"
        "  }\n"
        "  const _fetch = window.fetch.bind(window), _jsonStringify = JSON.stringify;\n"
        "  const FOLIO_LOCALD_BASE = \"http://127.0.0.1:%u\", FOLIO_LOCALD_URL = FOLIO_LOCALD_BASE + \"/__folio_rpc\", FOLIO_EVENTS_URL = FOLIO_LOCALD_BASE + \"/__folio_events\", FOLIO_TOKEN = %s;\n"
        "  class Channel { constructor() { this.onmessage = () => {}; } }\n"
        "  const listeners = new Map(), pickPending = new Map();\n"
        "  let streamCtrl = null, ps = 10000, pickSeq = 1;\n"
        "  const hasListeners = () => { for (const s of listeners.values()) if (s.size) return true; return false; };\n"
        "  const emit = (name, payload) => { const s = listeners.get(name); if (s) for (const h of Array.from(s)) { try { h({ event: name, payload }); } catch {} } };\n"
        "  const dispatch = (ev) => { if (ev && typeof ev === 'object' && typeof ev.event === 'string') emit(ev.event, ev.payload); };\n"
        "  window.__FOLIO_EMIT__ = window.__ORB_EMIT__ = emit;\n"
        "  window.__FOLIO_NATIVE_RESOLVE__ = (id, val) => { const p = pickPending.get(id); if (p) { pickPending.delete(id); p(val); } };\n"
        "  window.__FOLIO_NATIVE_PICK__ = (kind) => {\n"
        "    const mh = window.webkit?.messageHandlers?.folioNative;\n"
        "    if (!mh) return Promise.resolve(undefined);\n"
        "    const id = pickSeq++;\n"
        "    return new Promise((res) => { pickPending.set(id, res); try { mh.postMessage(_jsonStringify({ id, kind })); } catch { pickPending.delete(id); res(undefined); } });\n"
        "  };\n"
        "  window.__FOLIO_PINCH__ = window.__ORB_PINCH__ = (ph, x16, y16, s10k, dx16, dy16) => {\n"
        "    if (ph === 0) { ps = s10k > 0 ? s10k : 10000; return; }\n"
        "    if (ph !== 1 || s10k <= 0 || ps <= 0) return;\n"
        "    const x = x16 / 16, y = y16 / 16, el = document.elementFromPoint(x, y) || document.documentElement;\n"
        "    if (s10k !== ps) { const dy = -Math.log(s10k / ps) * 100; ps = s10k; el.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, ctrlKey: true, clientX: x, clientY: y, deltaY: dy })); }\n"
        "    if ((dx16 || dy16) && el.closest('.mermaid-stage, .image-viewport')) el.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, clientX: x, clientY: y, deltaX: -dx16 / 16, deltaY: -dy16 / 16 }));\n"
        "  };\n"
        "  async function drain() {\n"
        "    if (!hasListeners()) return;\n"
        "    try {\n"
        "      const r = await _fetch(FOLIO_LOCALD_URL, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Folio-Token': FOLIO_TOKEN }, body: _jsonStringify({ command: 'poll_events', args: {} }) });\n"
        "      const b = await r.json();\n"
        "      if (Array.isArray(b.result)) for (const ev of b.result) dispatch(ev);\n"
        "    } catch {}\n"
        "  }\n"
        "  const stopStream = () => { if (streamCtrl) { streamCtrl.abort(); streamCtrl = null; } };\n"
        "  function ensureStream() {\n"
        "    if (streamCtrl || !hasListeners()) return;\n"
        "    const ctrl = streamCtrl = new AbortController();\n"
        "    (async () => {\n"
        "      while (!ctrl.signal.aborted && hasListeners()) {\n"
        "        try {\n"
        "          const r = await _fetch(FOLIO_EVENTS_URL, { headers: { 'Accept': 'text/event-stream', 'X-Folio-Token': FOLIO_TOKEN }, signal: ctrl.signal });\n"
        "          if (!r.ok || !r.body) throw new Error('sse');\n"
        "          const rd = r.body.getReader(), dec = new TextDecoder(); let buf = '';\n"
        "          while (!ctrl.signal.aborted && hasListeners()) {\n"
        "            const { value, done } = await rd.read(); if (done) break;\n"
        "            buf += dec.decode(value, { stream: true }); let idx;\n"
        "            while ((idx = buf.indexOf('\\n\\n')) >= 0) {\n"
        "              const fr = buf.slice(0, idx); buf = buf.slice(idx + 2);\n"
        "              for (const ln of fr.split('\\n')) {\n"
        "                if (ln.startsWith('data: ')) {\n"
        "                  const parsed = JSON.parse(ln.slice(6));\n"
        "                  if (Array.isArray(parsed)) { for (const ev of parsed) dispatch(ev); } else { dispatch(parsed); }\n"
        "                }\n"
        "              }\n"
        "            }\n"
        "          }\n"
        "        } catch {\n"
        "          if (ctrl.signal.aborted || !hasListeners()) break;\n"
        "          await new Promise(w => setTimeout(w, 900));\n"
        "          if (!ctrl.signal.aborted && hasListeners()) await drain();\n"
        "        }\n"
        "      }\n"
        "      if (streamCtrl === ctrl) streamCtrl = null;\n"
        "    })();\n"
        "  }\n"
        "  async function invoke(command, args = {}) {\n"
        "    if (command === 'pick_directory_native' || command === 'pick_file_native') {\n"
        "      const picked = await window.__FOLIO_NATIVE_PICK__(command === 'pick_directory_native' ? 'folder' : 'file');\n"
        "      if (picked !== undefined) return { path: picked };\n"
        "    }\n"
        "    let r; for (let i = 0; ; i++) {\n"
        "      try {\n"
        "        r = await _fetch(FOLIO_LOCALD_URL, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Folio-Token': FOLIO_TOKEN }, body: _jsonStringify({ command, args: args || {} }) });\n"
        "        break;\n"
        "      } catch (e) { if (i >= 40) throw e; await new Promise(w => setTimeout(w, 60)); }\n"
        "    }\n"
        "    const b = await r.json();\n"
        "    if (!r.ok || b.error) throw new Error(b.error || command);\n"
        "    return b.result;\n"
        "  }\n"
        "  async function listen(n, h) {\n"
        "    if (!listeners.has(n)) listeners.set(n, new Set());\n"
        "    listeners.get(n).add(h); ensureStream();\n"
        "    return () => { listeners.get(n)?.delete(h); if (!listeners.get(n)?.size) listeners.delete(n); if (!hasListeners()) stopStream(); };\n"
        "  }\n"
        "  const assetUrl = (uri, rel) => FOLIO_LOCALD_BASE + '/__folio_asset?uri=' + encodeURIComponent(uri || '') + '&rel=' + encodeURIComponent(rel || '') + '&token=' + encodeURIComponent(FOLIO_TOKEN);\n"
        "  window.__TAURI_INTERNALS__ = { invoke };\n"
        "  window.__FOLIO_DESKTOP__ = window.__ORB_DESKTOP__ = window.__TAURI__ = {\n"
        "    core: { invoke, Channel, assetUrl },\n"
        "    event: { listen }\n"
        "  };\n"
        "})();\n";
    size_t need = sizeof(tmpl) + strlen(tok_lit) + 32;
    char *buf = malloc(need);
    if (buf) snprintf(buf, need, tmpl, port, tok_lit);
    free(tok_lit);
    return buf;
}

#if defined(FOLIO_HAS_WEBKITGTK)
typedef struct {
    GtkWindow *win;
    WebKitWebView *wv;
} ShellCtx;

typedef struct {
    WebKitWebView *wv;
    int req_id;
    gboolean is_folder;
} PickCtx;

static const char *mime_for_path(const char *p) {
    const char *e = strrchr(p, '.');
    if (!e) return "application/octet-stream";
    static const struct { const char *x, *m; } t[] = {
        { ".html", "text/html; charset=utf-8" },
        { ".js",   "text/javascript; charset=utf-8" },
        { ".mjs",  "text/javascript; charset=utf-8" },
        { ".css",  "text/css; charset=utf-8" },
        { ".wasm", "application/wasm" },
        { ".json", "application/json; charset=utf-8" },
        { ".svg",  "image/svg+xml" },
        { ".png",  "image/png" },
        { ".jpg",  "image/jpeg" },
        { ".jpeg", "image/jpeg" },
        { ".webp", "image/webp" },
        { ".ico",  "image/x-icon" },
        { ".woff2","font/woff2" },
        { ".woff", "font/woff" },
        { ".ttf",  "font/ttf" },
        { ".map",  "application/json; charset=utf-8" },
    };
    for (size_t i = 0; i < sizeof(t) / sizeof(t[0]); ++i)
        if (g_ascii_strcasecmp(e, t[i].x) == 0) return t[i].m;
    return "application/octet-stream";
}

static void finish_scheme_bytes(WebKitURISchemeRequest *req, guint code, const char *mime, GBytes *bytes, gboolean immut) {
    GInputStream *st = g_memory_input_stream_new_from_bytes(bytes);
    WebKitURISchemeResponse *resp = webkit_uri_scheme_response_new(st, (gint64)g_bytes_get_size(bytes));
    SoupMessageHeaders *hdr = soup_message_headers_new(SOUP_MESSAGE_HEADERS_RESPONSE);
    soup_message_headers_append(hdr, "Access-Control-Allow-Origin", "folio://localhost");
    soup_message_headers_append(hdr, "Cache-Control", immut ? "public, max-age=31536000, immutable" : "no-cache");
    webkit_uri_scheme_response_set_status(resp, code, code == 200 ? "OK" : "Not Found");
    webkit_uri_scheme_response_set_content_type(resp, mime);
    webkit_uri_scheme_response_set_http_headers(resp, hdr);
    webkit_uri_scheme_request_finish_with_response(req, resp);
    g_object_unref(resp);
    g_object_unref(st);
}

static void on_folio_scheme(WebKitURISchemeRequest *req, gpointer ud) {
    const char *root = (const char *)ud;
    const char *uri = webkit_uri_scheme_request_get_uri(req);
    const char *path = webkit_uri_scheme_request_get_path(req);
    if (!root || !is_confined_shell_uri(uri)) {
        GBytes *b = g_bytes_new_static("forbidden", 9);
        finish_scheme_bytes(req, 403, "text/plain; charset=utf-8", b, FALSE);
        g_bytes_unref(b);
        return;
    }
    while (path && *path == '/') path++;
    const char *rel = (path && *path) ? path : "index.html";
    char rreal[PATH_MAX], creal[PATH_MAX];
    char *joined = g_build_filename(root, rel, NULL);
    struct stat st;
    gboolean ok = realpath(root, rreal) != NULL &&
                  realpath(joined, creal) != NULL &&
                  (strcmp(creal, rreal) == 0 ||
                   (strncmp(creal, rreal, strlen(rreal)) == 0 && creal[strlen(rreal)] == '/')) &&
                  stat(creal, &st) == 0 && S_ISREG(st.st_mode);
    g_free(joined);
    if (!ok && strchr(rel, '.') == NULL) {
        char *idx = g_build_filename(root, "index.html", NULL);
        ok = realpath(root, rreal) != NULL && realpath(idx, creal) != NULL &&
             stat(creal, &st) == 0 && S_ISREG(st.st_mode);
        g_free(idx);
    }
    gchar *data = NULL;
    gsize len = 0;
    if (!ok || !g_file_get_contents(creal, &data, &len, NULL)) {
        GBytes *b = g_bytes_new_static("not found", 9);
        finish_scheme_bytes(req, 404, "text/plain; charset=utf-8", b, FALSE);
        g_bytes_unref(b);
        return;
    }
    GBytes *b = g_bytes_new_take(data, len);
    gboolean immut = g_str_has_prefix(rel, "assets/") ||
                     g_str_has_suffix(rel, ".woff2") ||
                     g_str_has_suffix(rel, ".woff") ||
                     g_str_has_suffix(rel, ".ttf") ||
                     g_str_has_suffix(rel, ".wasm");
    finish_scheme_bytes(req, 200, mime_for_path(creal), b, immut);
    g_bytes_unref(b);
}

static void resolve_pick_js(WebKitWebView *wv, int req_id, const char *path_or_null) {
    if (!wv) return;
    if (!path_or_null) {
        char js[96];
        snprintf(js, sizeof(js), "window.__FOLIO_NATIVE_RESOLVE__&&window.__FOLIO_NATIVE_RESOLVE__(%d,null)", req_id);
        webkit_web_view_evaluate_javascript(wv, js, -1, NULL, NULL, NULL, NULL, NULL);
        return;
    }
    char *esc = json_escape_string(path_or_null);
    if (!esc) return;
    size_t need = strlen(esc) + 96;
    char *js = malloc(need);
    if (js) {
        snprintf(js, need, "window.__FOLIO_NATIVE_RESOLVE__&&window.__FOLIO_NATIVE_RESOLVE__(%d,%s)", req_id, esc);
        webkit_web_view_evaluate_javascript(wv, js, -1, NULL, NULL, NULL, NULL, NULL);
        free(js);
    }
    free(esc);
}

static void on_file_dialog_done(GObject *src, GAsyncResult *res, gpointer ud) {
    PickCtx *pctx = (PickCtx *)ud;
    GFile *file = pctx->is_folder
        ? gtk_file_dialog_select_folder_finish(GTK_FILE_DIALOG(src), res, NULL)
        : gtk_file_dialog_open_finish(GTK_FILE_DIALOG(src), res, NULL);
    char *path = file ? g_file_get_path(file) : NULL;
    resolve_pick_js(pctx->wv, pctx->req_id, path);
    if (path) g_free(path);
    if (file) g_object_unref(file);
    g_object_unref(pctx->wv);
    free(pctx);
}

static void on_native_msg(WebKitUserContentManager *ucm, JSCValue *val, gpointer ud) {
    (void)ucm;
    ShellCtx *sctx = (ShellCtx *)ud;
    if (!sctx || !sctx->wv || !val) return;
    char *raw = jsc_value_to_string(val);
    if (!raw) return;
    int req_id = 0;
    const char *id_pos = strstr(raw, "\"id\":");
    if (id_pos) req_id = atoi(id_pos + 5);
    gboolean is_folder = strstr(raw, "\"folder\"") != NULL;
    g_free(raw);
    if (req_id <= 0) return;

    GtkFileDialog *dlg = gtk_file_dialog_new();
    gtk_file_dialog_set_modal(dlg, TRUE);
    gtk_file_dialog_set_title(dlg, is_folder ? "Mount Markdown Folder" : "Open Markdown Note");

    PickCtx *pctx = calloc(1, sizeof(PickCtx));
    if (!pctx) { g_object_unref(dlg); return; }
    pctx->wv = g_object_ref(sctx->wv);
    pctx->req_id = req_id;
    pctx->is_folder = is_folder;

    if (is_folder) {
        gtk_file_dialog_select_folder(dlg, sctx->win, NULL, on_file_dialog_done, pctx);
    } else {
        GtkFileFilter *md_filter = gtk_file_filter_new();
        gtk_file_filter_set_name(md_filter, "Markdown Notes (*.md)");
        gtk_file_filter_add_pattern(md_filter, "*.md");
        gtk_file_filter_add_pattern(md_filter, "*.markdown");
        GListStore *filters = g_list_store_new(GTK_TYPE_FILE_FILTER);
        g_list_store_append(filters, md_filter);
        gtk_file_dialog_set_filters(dlg, G_LIST_MODEL(filters));
        g_object_unref(md_filter);
        g_object_unref(filters);
        gtk_file_dialog_open(dlg, sctx->win, NULL, on_file_dialog_done, pctx);
    }
    g_object_unref(dlg);
}

static gboolean on_ipc_accept(gint fd, GIOCondition cond, gpointer ud) {
    (void)cond;
    ShellCtx *sctx = (ShellCtx *)ud;
    int cfd = accept4(fd, NULL, NULL, SOCK_CLOEXEC | SOCK_NONBLOCK);
    if (cfd < 0) return G_SOURCE_CONTINUE;
    char buf[4097];
    ssize_t n = recv(cfd, buf, sizeof(buf) - 1, 0);
    close(cfd);
    if (n > 0 && sctx && sctx->wv) {
        buf[n] = '\0';
        char *esc = json_escape_string(buf);
        if (esc) {
            size_t need = strlen(esc) + 128;
            char *js = malloc(need);
            if (js) {
                snprintf(js, need, "window.__FOLIO_EMIT__&&window.__FOLIO_EMIT__('app:open-target',{target:%s})", esc);
                webkit_web_view_evaluate_javascript(sctx->wv, js, -1, NULL, NULL, NULL, NULL, NULL);
                free(js);
            }
            free(esc);
        }
        if (sctx->win) gtk_window_present(sctx->win);
    }
    return G_SOURCE_CONTINUE;
}

static void emit_zoom(GtkGesture *g, int ph, double scale, WebKitWebView *wv) {
    double cx = 0, cy = 0;
    gtk_gesture_get_bounding_box_center(g, &cx, &cy);
    char js[160];
    snprintf(js, sizeof(js), "window.__FOLIO_PINCH__&&window.__FOLIO_PINCH__(%d,%ld,%ld,%ld,0,0)",
             ph, (long)(cx * 16.0), (long)(cy * 16.0), (long)(scale * 10000.0));
    webkit_web_view_evaluate_javascript(wv, js, -1, NULL, NULL, NULL, NULL, NULL);
}

static void on_zoom_begin(GtkGesture *g, GdkEventSequence *s, gpointer ud) {
    (void)s;
    emit_zoom(g, 0, 1.0, WEBKIT_WEB_VIEW(ud));
}

static void on_zoom_scale(GtkGestureZoom *z, double sc, gpointer ud) {
    emit_zoom(GTK_GESTURE(z), 1, sc, WEBKIT_WEB_VIEW(ud));
}

static void on_win_active(GObject *w, GParamSpec *p, gpointer ud) {
    (void)p;
    gboolean act = gtk_window_is_active(GTK_WINDOW(w));
    webkit_web_view_evaluate_javascript(
        WEBKIT_WEB_VIEW(ud),
        act ? "document.documentElement&&(document.documentElement.dataset.windowFocused='true')"
            : "document.documentElement&&(document.documentElement.dataset.windowFocused='false')",
        -1, NULL, NULL, NULL, NULL, NULL);
}

static gboolean on_decide_policy(WebKitWebView *wv, WebKitPolicyDecision *dec, WebKitPolicyDecisionType dtype, gpointer ud) {
    (void)wv; (void)ud;
    if (dtype == WEBKIT_POLICY_DECISION_TYPE_NAVIGATION_ACTION || dtype == WEBKIT_POLICY_DECISION_TYPE_NEW_WINDOW_ACTION) {
        WebKitNavigationAction *act = webkit_navigation_policy_decision_get_navigation_action(WEBKIT_NAVIGATION_POLICY_DECISION(dec));
        const char *uri = webkit_uri_request_get_uri(webkit_navigation_action_get_request(act));
        if (dtype == WEBKIT_POLICY_DECISION_TYPE_NEW_WINDOW_ACTION || !is_confined_shell_uri(uri)) {
            webkit_policy_decision_ignore(dec);
            return TRUE;
        }
    }
    return FALSE;
}

static gboolean on_close_req(GtkWindow *w, gpointer ud) {
    (void)w;
    g_main_loop_quit((GMainLoop *)ud);
    return FALSE;
}

static gboolean on_quit_sig(gpointer ud) {
    g_main_loop_quit((GMainLoop *)ud);
    return G_SOURCE_REMOVE;
}
#endif

int main(int argc, char **argv) {
    const char *port_env = getenv("FOLIO_LOCALD_PORT");
    unsigned port = (port_env && *port_env) ? 0 : pick_ephemeral_port(19485);
    if (port_env && *port_env && !parse_valid_port(port_env, &port))
        return fprintf(stderr, "folio: invalid FOLIO_LOCALD_PORT\n"), 1;

    char eph_tok[65];
    const char *token = getenv("FOLIO_LOCALD_TOKEN");
    if (!token || strlen(token) < 32) {
        if (gen_csprng_hex(eph_tok) != 0) return 1;
        token = eph_tok;
    }

    char *init_js = build_init_script(port, token);
    if (!init_js) return 1;

    if (argc > 1 && strcmp(argv[1], "--self-test") == 0) {
        int ok = strstr(init_js, "__FOLIO_DESKTOP__") != NULL &&
                 strstr(init_js, "dataset.platform = 'linux'") != NULL &&
                 strstr(init_js, "__FOLIO_PINCH__") != NULL &&
                 strstr(init_js, "__FOLIO_NATIVE_PICK__") != NULL &&
                 strstr(init_js, "const _fetch = window.fetch.bind(window)") != NULL &&
                 strstr(init_js, "/__folio_events") != NULL &&
                 strstr(init_js, "setInterval") == NULL &&
                 is_confined_shell_uri("folio://localhost/") &&
                 is_confined_shell_uri("folio://localhost/?open=kp-123") &&
                 is_confined_shell_uri("http://127.0.0.1:19485/") &&
                 is_confined_shell_uri("http://localhost:1435/?open=kp-123") &&
                 !is_confined_shell_uri("folio://evil.example/") &&
                 !is_confined_shell_uri("http://127.0.0.1:80@evil.com/") &&
                 !is_confined_shell_uri("https://evil.example");
        free(init_js);
        if (ok) {
            printf("folio-linux-shell self-test: ok\n");
            return 0;
        }
        return 1;
    }

    char *cli_target = resolve_cli_target(argc, argv);
    const char *dev_url = getenv("FOLIO_DEV_URL");
    const char *query = getenv("FOLIO_INITIAL_QUERY");
    const char *rt_dir = getenv("XDG_RUNTIME_DIR");
    int lock_fd = -1;
    int ipc_fd = -1;
    char sock_path[256] = { 0 };

    if ((!dev_url || !*dev_url) && (!query || !*query) && !getenv("FOLIO_MULTI_INSTANCE") && rt_dir && *rt_dir) {
        char lp[256];
        snprintf(lp, sizeof(lp), "%s/folio-desktop.lock", rt_dir);
        lock_fd = open(lp, O_CREAT | O_RDWR | O_CLOEXEC, 0600);
        if (lock_fd >= 0 && flock(lock_fd, LOCK_EX | LOCK_NB) != 0) {
            if (cli_target) forward_target_to_primary(rt_dir, cli_target);
            focus_hypr_class("folio");
            close(lock_fd);
            free(cli_target);
            free(init_js);
            return 0;
        }
        if (lock_fd >= 0 && (size_t)snprintf(sock_path, sizeof(sock_path), "%s/folio-desktop.sock", rt_dir) < sizeof(sock_path)) {
            unlink(sock_path);
            ipc_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
            if (ipc_fd >= 0) {
                struct sockaddr_un sa = { .sun_family = AF_UNIX };
                strncpy(sa.sun_path, sock_path, sizeof(sa.sun_path) - 1);
                if (bind(ipc_fd, (struct sockaddr *)&sa, sizeof(sa)) != 0 || listen(ipc_fd, 8) != 0) {
                    close(ipc_fd);
                    ipc_fd = -1;
                    sock_path[0] = '\0';
                } else {
                    chmod(sock_path, 0600);
                }
            }
        }
    }

    pid_t parent_pid = getpid(), locald_pid = -1;
    const char *locald_bin = getenv("FOLIO_LOCALD_BIN");
    if (locald_bin && *locald_bin && (locald_pid = fork()) == 0) {
        prctl(PR_SET_PDEATHSIG, SIGTERM);
        if (getppid() != parent_pid) _exit(0);
        static const char *const keep[] = {
            "HOME", "PATH", "TMPDIR", "USER", "LANG",
            "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CONFIG_DIRS",
            "XDG_DATA_HOME", "XDG_DATA_DIRS", "XDG_RUNTIME_DIR",
            "XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "XDG_SESSION_TYPE",
            "DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY", "DISPLAY", "BROWSER",
            "FOLIO_STATE_DIR", "FOLIO_UI_ROOT", "FOLIO_DEFAULT_NOTES_DIR", "FOLIO_NOTES_DIRS",
            "FOLIO_SECRETS_DIR", "FOLIO_SECRET_HELPER", "FOLIO_THEME_MODE",
            "FOLIO_HINDSIGHT_URL", "FOLIO_DEFAULT_BANK", "FOLIO_SYNC_BANK",
            "FOLIO_INITIAL_QUERY", "FOLIO_HINDSIGHT_READONLY",
            "FOLIO_ORBROUTER_URL", "FOLIO_ORBROUTER_API_KEY", "FOLIO_ORBROUTER_KEY_FILE",
            "ORBROUTER_API_KEY", "ORB_API_URL", "ORB_STATE_DIR",
            "HINDSIGHT_API_URL", "HINDSIGHT_BASE_URL", "HINDSIGHT_BANK_ID", "HINDSIGHT_API_KEY", "HINDSIGHT_API_KEY_FILE",
            "SPARK_MEMORY_API_TOKEN", "SPARK_MEMORY_API_TOKEN_FILE",
            "NOTION_API_KEY", "NOTION_API_KEY_FILE", "NOTION_API_TOKEN", "NOTION_API_TOKEN_FILE",
            "NOTION_API_KEY_PERSO", "NOTION_API_KEY_PERSO_FILE",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "NIX_SSL_CERT_FILE"
        };
        enum { KEEP_COUNT = sizeof(keep) / sizeof(keep[0]) };
        static char kv[KEEP_COUNT][32768];
        char pkv[48], tkv[4200], *envp[KEEP_COUNT + 3];
        size_t n = 0;
        snprintf(pkv, sizeof(pkv), "FOLIO_LOCALD_PORT=%u", port);
        envp[n++] = pkv;
        snprintf(tkv, sizeof(tkv), "FOLIO_LOCALD_TOKEN=%s", token);
        envp[n++] = tkv;
        for (size_t i = 0; i < KEEP_COUNT; ++i) {
            const char *v = getenv(keep[i]);
            if (v && (size_t)snprintf(kv[i], sizeof(kv[i]), "%s=%s", keep[i], v) < sizeof(kv[i])) {
                envp[n++] = kv[i];
            }
        }
        envp[n] = NULL;
        execle(locald_bin, locald_bin, (char *)NULL, envp);
        _exit(127);
    }

#if defined(FOLIO_HAS_WEBKITGTK)
    g_set_prgname("folio");
    g_setenv("GTK_USE_PORTAL", "1", FALSE);
    gtk_init();

    GMainLoop *loop = g_main_loop_new(NULL, FALSE);
    g_unix_signal_add(SIGTERM, on_quit_sig, loop);
    g_unix_signal_add(SIGINT, on_quit_sig, loop);
    g_unix_signal_add(SIGHUP, on_quit_sig, loop);

    GtkCssProvider *glass = gtk_css_provider_new();
    gtk_css_provider_load_from_string(glass,
        "window, window.background { background-color: rgba(0,0,0,0); background-image: none; }");
    if (gdk_display_get_default()) {
        gtk_style_context_add_provider_for_display(gdk_display_get_default(),
            GTK_STYLE_PROVIDER(glass), GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
    }
    g_object_unref(glass);

    GtkWidget *win = gtk_window_new();
    gtk_window_set_default_icon_name("folio");
    gtk_window_set_title(GTK_WINDOW(win), "Folio");
    gtk_window_set_default_size(GTK_WINDOW(win), 1470, 923);
    gtk_widget_set_size_request(win, 720, 480);
    g_signal_connect(win, "close-request", G_CALLBACK(on_close_req), loop);

    WebKitMemoryPressureSettings *mps = webkit_memory_pressure_settings_new();
    webkit_memory_pressure_settings_set_memory_limit(mps, 512);
    webkit_memory_pressure_settings_set_conservative_threshold(mps, 0.35);
    webkit_memory_pressure_settings_set_strict_threshold(mps, 0.60);
    webkit_memory_pressure_settings_set_kill_threshold(mps, 0.0);
    webkit_network_session_set_memory_pressure_settings(mps);
    webkit_memory_pressure_settings_free(mps);

    WebKitWebContext *wctx = webkit_web_context_new();
    webkit_web_context_set_cache_model(wctx, WEBKIT_CACHE_MODEL_DOCUMENT_VIEWER);
    WebKitSecurityManager *sm = webkit_web_context_get_security_manager(wctx);
    webkit_security_manager_register_uri_scheme_as_secure(sm, "folio");
    webkit_security_manager_register_uri_scheme_as_cors_enabled(sm, "folio");

    const char *ui_root = getenv("FOLIO_UI_ROOT");
    if (ui_root && *ui_root) {
        webkit_web_context_register_uri_scheme(wctx, "folio", on_folio_scheme, (gpointer)ui_root, NULL);
    }

    WebKitSettings *wks = webkit_settings_new();
    webkit_settings_set_hardware_acceleration_policy(wks, WEBKIT_HARDWARE_ACCELERATION_POLICY_ALWAYS);

    ShellCtx sctx = { .win = GTK_WINDOW(win), .wv = NULL };

    WebKitUserContentManager *ucm = webkit_user_content_manager_new();
    webkit_user_content_manager_register_script_message_handler(ucm, "folioNative", NULL);
    g_signal_connect(ucm, "script-message-received::folioNative", G_CALLBACK(on_native_msg), &sctx);

    WebKitUserScript *script = webkit_user_script_new(
        init_js, WEBKIT_USER_CONTENT_INJECT_TOP_FRAME, WEBKIT_USER_SCRIPT_INJECT_AT_DOCUMENT_START, NULL, NULL);
    WebKitUserStyleSheet *idle_css = webkit_user_style_sheet_new(
        "html[data-window-focused=\"false\"] *,html[data-window-focused=\"false\"] *::before,html[data-window-focused=\"false\"] *::after{animation-play-state:paused !important;transition:none !important;caret-color:transparent !important;}",
        WEBKIT_USER_CONTENT_INJECT_ALL_FRAMES, WEBKIT_USER_STYLE_LEVEL_USER, NULL, NULL);
    webkit_user_content_manager_add_script(ucm, script);
    webkit_user_content_manager_add_style_sheet(ucm, idle_css);
    webkit_user_script_unref(script);
    webkit_user_style_sheet_unref(idle_css);

    GtkWidget *wv = g_object_new(WEBKIT_TYPE_WEB_VIEW,
        "web-context", wctx,
        "network-session", webkit_network_session_get_default(),
        "settings", wks,
        "user-content-manager", ucm,
        NULL);
    sctx.wv = WEBKIT_WEB_VIEW(wv);
    g_object_unref(wctx);
    g_object_unref(wks);
    g_object_unref(ucm);

    GdkRGBA clear = { 0, 0, 0, 0 };
    webkit_web_view_set_background_color(WEBKIT_WEB_VIEW(wv), &clear);

    GtkGesture *zoom = gtk_gesture_zoom_new();
    gtk_event_controller_set_propagation_phase(GTK_EVENT_CONTROLLER(zoom), GTK_PHASE_CAPTURE);
    g_signal_connect(zoom, "begin", G_CALLBACK(on_zoom_begin), wv);
    g_signal_connect(zoom, "scale-changed", G_CALLBACK(on_zoom_scale), wv);
    gtk_widget_add_controller(wv, GTK_EVENT_CONTROLLER(zoom));

    if (ipc_fd >= 0) {
        g_unix_fd_add(ipc_fd, G_IO_IN, on_ipc_accept, &sctx);
    }

    g_signal_connect(win, "notify::is-active", G_CALLBACK(on_win_active), wv);
    g_signal_connect(wv, "decide-policy", G_CALLBACK(on_decide_policy), NULL);
    gtk_window_set_child(GTK_WINDOW(win), wv);

    char default_url[96];
    if (ui_root && *ui_root) {
        snprintf(default_url, sizeof(default_url), "folio://localhost/");
    } else {
        snprintf(default_url, sizeof(default_url), "http://127.0.0.1:%u/", port);
    }
    char launch_url[2048];
    const char *base_url = (dev_url && is_confined_shell_uri(dev_url)) ? dev_url : default_url;
    if (query && *query && (query[0] == '?' || query[0] == '#')) {
        snprintf(launch_url, sizeof(launch_url), "%s%s", base_url, query);
    } else if (cli_target && *cli_target) {
        char *enc = uri_encode_component(cli_target);
        if (enc) {
            snprintf(launch_url, sizeof(launch_url), "%s?open=%s", base_url, enc);
            free(enc);
        } else {
            snprintf(launch_url, sizeof(launch_url), "%s", base_url);
        }
    } else {
        snprintf(launch_url, sizeof(launch_url), "%s", base_url);
    }
    webkit_web_view_load_uri(WEBKIT_WEB_VIEW(wv), is_confined_shell_uri(launch_url) ? launch_url : default_url);
    gtk_window_present(GTK_WINDOW(win));
    gtk_widget_grab_focus(wv);
    g_main_loop_run(loop);
    g_main_loop_unref(loop);
#else
    (void)argc; (void)argv;
#endif

    if (ipc_fd >= 0) close(ipc_fd);
    if (sock_path[0]) unlink(sock_path);
    free(cli_target);
    free(init_js);
    if (locald_pid > 0) {
        kill(locald_pid, SIGTERM);
        for (int i = 0; i < 25 && waitpid(locald_pid, NULL, WNOHANG) == 0; ++i) {
            struct timespec ts = { 0, 20 * 1000 * 1000L };
            nanosleep(&ts, NULL);
        }
        if (waitpid(locald_pid, NULL, WNOHANG) == 0) {
            kill(locald_pid, SIGKILL);
            waitpid(locald_pid, NULL, 0);
        }
    }
    if (lock_fd >= 0) close(lock_fd);
    return 0;
}
