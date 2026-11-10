import hmac
import json
import os
import queue
import shutil
import signal
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .cache import CacheStore
from .hindsight import HindsightProvider
from .local_fs import LocalFsProvider
from .notion import NotionProvider
from .secrets import CredentialResolver


MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".wasm": "application/wasm",
}

IMMUTABLE_ASSET_SUFFIXES: frozenset[str] = frozenset({".woff2", ".woff", ".ttf", ".wasm"})


def resolve_system_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    nix_sys = Path("/run/current-system/sw/bin") / name
    if nix_sys.is_file() and os.access(nix_sys, os.X_OK):
        return str(nix_sys)
    return None


def compute_static_cache_control(rel_path: str, suffix: str) -> str:
    norm = rel_path.lstrip("/")
    if norm.startswith("assets/") or suffix.lower() in IMMUTABLE_ASSET_SUFFIXES:
        return "public, max-age=31536000, immutable"
    return "no-cache"


class FolioEngine:
    def __init__(self, state_dir: Path, default_notes_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._events_lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._subscribers: set[queue.Queue[dict[str, Any] | None]] = set()
        self._last_omarchy_mode = self._read_omarchy_mode()
        self._theme_stop = threading.Event()
        self._theme_thread: threading.Thread | None = None

        self.cache = CacheStore(self.state_dir / "folio_cache.sqlite3")
        self.secrets = CredentialResolver(state_dir=self.state_dir)
        self.local_fs = LocalFsProvider(
            self.cache,
            default_dir=default_notes_dir,
            on_change=lambda payload: self.push_event("workspace:changed", payload),
        )
        self.hindsight = HindsightProvider(self.cache, self.secrets)
        self.notion = NotionProvider(self.cache, self.secrets)

    @staticmethod
    def _read_omarchy_mode() -> str | None:
        override = os.environ.get("FOLIO_THEME_MODE", "").strip().lower()
        if override in ("dark", "light"):
            return override
        colors = Path.home() / ".local" / "state" / "omarchy" / "current" / "theme" / "colors.toml"
        if colors.is_file():
            try:
                for line in colors.read_text(encoding="utf-8", errors="ignore").splitlines():
                    s = line.strip()
                    if s.startswith("mode"):
                        _, _, rhs = s.partition("=")
                        mode = rhs.strip().strip('"').strip("'").lower()
                        if mode in ("dark", "light"):
                            return mode
            except Exception:
                pass
        return None

    def _watch_omarchy_theme(self) -> None:
        while not self._theme_stop.wait(0.6):
            mode = self._read_omarchy_mode()
            if mode and mode != self._last_omarchy_mode:
                self._last_omarchy_mode = mode
                self.push_event("theme:changed", {"mode": mode})

    def start_background(self) -> None:
        self.local_fs.start_watcher()
        if not self._theme_thread or not self._theme_thread.is_alive():
            self._theme_stop.clear()
            self._theme_thread = threading.Thread(
                target=self._watch_omarchy_theme, daemon=True, name="folio-theme-watcher"
            )
            self._theme_thread.start()

    def shutdown(self) -> None:
        self._theme_stop.set()
        with self._events_lock:
            for sub in list(self._subscribers):
                try:
                    sub.put_nowait(None)
                except Exception:
                    pass
            self._subscribers.clear()
        if self._theme_thread:
            self._theme_thread.join(timeout=1.5)
        self.local_fs.stop_watcher()
        self.cache.close()

    def subscribe_events(self) -> queue.Queue[dict[str, Any] | None]:
        q: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=256)
        with self._events_lock:
            for pending in self._events:
                try:
                    q.put_nowait(pending)
                except queue.Full:
                    break
            self._events.clear()
            self._subscribers.add(q)
        return q

    def unsubscribe_events(self, q: queue.Queue[dict[str, Any] | None]) -> None:
        with self._events_lock:
            self._subscribers.discard(q)

    def push_event(self, event_name: str, payload: Any) -> None:
        item = {"event": event_name, "payload": payload}
        with self._events_lock:
            if self._subscribers:
                stale: list[queue.Queue[dict[str, Any] | None]] = []
                for sub in self._subscribers:
                    try:
                        sub.put_nowait(item)
                    except queue.Full:
                        stale.append(sub)
                for sub in stale:
                    self._subscribers.discard(sub)
            else:
                self._events.append(item)
                if len(self._events) > 128:
                    self._events = self._events[-128:]

    def drain_events(self) -> list[dict[str, Any]]:
        with self._events_lock:
            out = list(self._events)
            self._events.clear()
            return out

    def dispatch(self, command: str, args: dict[str, Any]) -> Any:
        if command in ("__orb_poll_events", "poll_events"):
            return self.drain_events()

        if command == "get_bootstrap":
            active_bank = str(args.get("bank_id") or self.hindsight.default_bank)
            return {
                "providers": {
                    "local_fs": self.local_fs.status(),
                    "hindsight": self.hindsight.status(),
                    "notion": self.notion.status(),
                },
                "banks": self.hindsight.list_banks(),
                "active_bank": active_bank,
                "local_tree": [n.to_dict() for n in self.local_fs.list_tree()],
                "macros": self.cache.get_kv("latex:macros", {"\\R": "\\mathbb{R}", "\\E": "\\mathbb{E}", "\\Prob": "\\mathbb{P}"}),
                "omarchy_mode": self._read_omarchy_mode(),
            }

        if command == "get_system_theme":
            return {"mode": self._read_omarchy_mode()}

        if command == "list_tree":
            provider = str(args.get("provider") or "local-fs")
            scope = args.get("scope")
            if provider == "local-fs":
                return [n.to_dict() for n in self.local_fs.list_tree(scope=scope)]
            if provider == "hindsight":
                return [n.to_dict() for n in self.hindsight.list_tree(scope=scope)]
            if provider == "notion":
                return [n.to_dict() for n in self.notion.list_tree(scope=scope)]
            raise ValueError(f"Unknown provider: {provider}")

        if command == "open_document":
            target = str(args.get("uri") or args.get("id") or "").strip()
            bank_id = str(args.get("bank_id") or self.hindsight.default_bank)
            if not target:
                raise ValueError("Missing document uri/id")
            doc = None
            if target.startswith("local://"):
                doc = self.local_fs.get_document(target)
            elif target.startswith("hindsight://") or target.startswith("kp-") or target.startswith("src-"):
                doc = self.hindsight.get_document(target, scope=bank_id)
            elif target.startswith("notion://"):
                doc = self.notion.get_document(target)
            else:
                doc = self.local_fs.get_document(target)
                if not doc:
                    doc = self.cache.find_by_slug_or_id(target)
                if not doc and (target.startswith("kp-") or target.startswith("src-")):
                    doc = self.hindsight.get_document(target, scope=bank_id)
            if not doc:
                raise KeyError(f"Document not found: {target}")
            payload = doc.to_dict()
            payload["backlinks"] = self.cache.get_backlinks(doc)
            return payload

        if command == "save_local_document":
            uri = str(args["uri"])
            raw_markdown = str(args["raw_markdown"])
            doc = self.local_fs.save_document(uri, raw_markdown)
            payload = doc.to_dict()
            payload["backlinks"] = self.cache.get_backlinks(doc)
            return payload

        if command == "create_local_document":
            doc = self.local_fs.create_document(
                title=str(args.get("title") or "Untitled Note"),
                folder=str(args.get("folder") or ""),
                mount_id=args.get("mount_id"),
                initial_body=args.get("initial_body"),
            )
            payload = doc.to_dict()
            payload["backlinks"] = []
            return payload

        if command == "delete_local_document":
            return self.local_fs.delete_local_document(str(args["uri"]))

        if command == "mount_directory":
            res = self.local_fs.mount_directory(
                Path(str(args["path"])),
                label=args.get("label"),
            )
            return {"mount": res, "tree": [n.to_dict() for n in self.local_fs.list_tree()]}

        if command == "unmount_directory":
            self.local_fs.unmount_directory(str(args["mount_id"]))
            return {"tree": [n.to_dict() for n in self.local_fs.list_tree()]}

        if command == "unified_search":
            query = str(args.get("query") or "")
            bank_id = str(args.get("bank_id") or self.hindsight.default_bank)
            include_hindsight = bool(args.get("include_hindsight", True))
            include_notion = bool(args.get("include_notion", True))
            limit = int(args.get("limit") or 24)

            local_hits = self.local_fs.search(query, limit=limit)
            hs_hits = self.hindsight.search(query, scope=bank_id, limit=limit // 2) if include_hindsight else []
            notion_hits = self.notion.search(query, limit=8) if include_notion else []

            combined = [*local_hits, *hs_hits, *notion_hits]
            seen: set[str] = set()
            deduped: list[dict[str, Any]] = []
            for item in sorted(combined, key=lambda x: float(x.get("score") or 0.0), reverse=True):
                u = item["uri"]
                if u not in seen:
                    seen.add(u)
                    deduped.append(item)
            return {"results": deduped[:limit], "bank_id": bank_id}

        if command == "hindsight_save_source":
            return self.hindsight.save_source_explicit(
                content=str(args.get("content") or ""),
                bank_id=args.get("bank_id"),
                document_id=args.get("document_id"),
                author=str(args.get("author") or "thomas"),
                source_type=str(args.get("source_type") or "paste"),
                tags=list(args.get("tags") or []),
                is_agent_summary=bool(args.get("is_agent_summary", False)),
                profile=args.get("profile"),
            )

        if command == "notion_sync":
            res = self.notion.sync_recent(limit=int(args.get("limit") or 20))
            return {**res, "tree": [n.to_dict() for n in self.notion.list_tree()]}

        if command == "set_session_credential":
            name = str(args.get("name") or "")
            value = str(args.get("value") or "")
            if name not in ("hindsight_api_key", "spark_memory_api_token", "notion_api_key", "notion_api_key_perso"):
                raise ValueError(f"Unsupported credential slot: {name}")
            self.secrets.set_ephemeral(name, value)
            self.hindsight._banks_cache = None
            return {
                "hindsight": self.hindsight.status(),
                "notion": self.notion.status(),
            }

        if command == "save_latex_macros":
            macros = args.get("macros") or {}
            if not isinstance(macros, dict):
                raise ValueError("macros must be an object")
            clean = {str(k): str(v) for k, v in macros.items() if str(k).startswith("\\")}
            self.cache.set_kv("latex:macros", clean)
            return clean

        if command in ("set_window_theme", "open_external_url"):
            if command == "open_external_url":
                url = str(args.get("url") or "")
                if url.startswith(("https://", "http://", "mailto:")):
                    import subprocess
                    opener_name = "open" if sys.platform == "darwin" else "xdg-open"
                    opener = resolve_system_binary(opener_name)
                    if opener:
                        try:
                            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            pass
            return {"ok": True}

        raise ValueError(f"Unknown RPC command: {command}")


def create_http_handler(
    engine: FolioEngine,
    port: int,
    expected_token: str = "",
    ui_root: Path | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send_cors(self) -> None:
            origin = self.headers.get("Origin") or ""
            if origin.startswith(("http://127.0.0.1", "http://localhost", "tauri://localhost", "folio://")):
                self.send_header("Access-Control-Allow-Origin", origin)
            else:
                self.send_header("Access-Control-Allow-Origin", f"http://127.0.0.1:{port}")
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Orb-Token, X-Folio-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _check_token(self, allow_query: bool = False) -> bool:
            if not expected_token:
                return True
            supplied = self.headers.get("X-Folio-Token") or self.headers.get("X-Orb-Token") or ""
            if not supplied and allow_query:
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                supplied = (qs.get("token") or [""])[0]
            return hmac.compare_digest(supplied, expected_token)

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self._send_cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:
            clean_path = self.path.split("?", 1)[0].split("#", 1)[0]
            if clean_path not in ("/__folio_rpc", "/__orb_rpc"):
                self._json_response(404, {"error": "Not found"})
                return
            if not self._check_token(allow_query=False):
                self._json_response(403, {"error": "Forbidden: invalid locald token"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                req_body = json.loads(raw.decode("utf-8"))
                cmd = str(req_body.get("command") or "")
                cmd_args = req_body.get("args") or {}
                result = engine.dispatch(cmd, cmd_args)
                self._json_response(200, {"result": result})
            except Exception as exc:
                self._json_response(400, {"error": str(exc)})

        def do_GET(self) -> None:
            clean_path = self.path.split("?", 1)[0].split("#", 1)[0]
            if clean_path == "/__health":
                self._json_response(200, {"status": "ok", "service": "folio-locald"})
                return
            if clean_path in ("/__folio_events", "/__orb_events"):
                if not self._check_token(allow_query=True):
                    self._json_response(403, {"error": "Forbidden: invalid locald token"})
                    return
                sub_q = engine.subscribe_events()
                try:
                    self.send_response(200)
                    self._send_cors()
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    self.wfile.write(b": ready\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            ev = sub_q.get(timeout=15.0)
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        if ev is None:
                            break
                        frame = f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")
                        self.wfile.write(frame)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    engine.unsubscribe_events(sub_q)
                return
            if not ui_root:
                self._json_response(404, {"error": "Static UI root not configured"})
                return
            rel = clean_path.lstrip("/") or "index.html"
            candidate = (ui_root / rel).resolve()
            if not (candidate == ui_root or str(candidate).startswith(str(ui_root) + os.sep)):
                self._json_response(403, {"error": "Forbidden"})
                return
            effective_rel = rel
            if not candidate.is_file():
                if "." not in Path(rel).name:
                    candidate = ui_root / "index.html"
                    effective_rel = "index.html"
                else:
                    self._json_response(404, {"error": "Asset not found"})
                    return
            data = candidate.read_bytes()
            mime = MIME_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
            cache_ctl = compute_static_cache_control(effective_rel, candidate.suffix)
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache_ctl)
            self.end_headers()
            self.wfile.write(data)

        def _json_response(self, code: int, obj: dict[str, Any]) -> None:
            payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self._send_cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def run_server() -> int:
    if "--self-test" in sys.argv[1:]:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            eng = FolioEngine(Path(tmp) / "state", Path(tmp) / "notes")
            boot = eng.dispatch("get_bootstrap", {})
            assert boot["providers"]["local_fs"]["connected"] is True
            assert len(boot["local_tree"]) >= 1
            hits = eng.dispatch("unified_search", {"query": "Bayesian", "include_hindsight": False})
            assert len(hits["results"]) >= 1
            assert compute_static_cache_control("assets/index-abc123.js", ".js").endswith("immutable")
            assert compute_static_cache_control("index.html", ".html") == "no-cache"
            assert MIME_TYPES[".wasm"] == "application/wasm"
            eng.shutdown()
        print("folio-locald self-test: ok")
        return 0

    port = int(os.environ.get("FOLIO_LOCALD_PORT") or os.environ.get("ORB_LOCALD_PORT") or "19485")
    expected_token = (
        os.environ.get("FOLIO_LOCALD_TOKEN")
        or os.environ.get("ORB_LOCALD_TOKEN")
        or ""
    )
    ui_root_raw = os.environ.get("FOLIO_UI_ROOT") or ""
    ui_root = Path(ui_root_raw).resolve() if ui_root_raw and Path(ui_root_raw).is_dir() else None

    xdg_state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    state_dir = Path(os.environ.get("FOLIO_STATE_DIR") or (xdg_state / "folio"))
    notes_dir = Path(os.environ.get("FOLIO_DEFAULT_NOTES_DIR") or (Path.home() / "Notes"))

    engine = FolioEngine(state_dir=state_dir, default_notes_dir=notes_dir)
    engine.start_background()

    handler_cls = create_http_handler(engine, port=port, expected_token=expected_token, ui_root=ui_root)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)

    def _handle_sig(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        server.serve_forever()
    finally:
        engine.shutdown()
    return 0


main = run_server
