import hmac
import json
import os
import signal
import sys
import threading
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
}


class FolioEngine:
    def __init__(self, state_dir: Path, default_notes_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._events_lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
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
        if self._theme_thread:
            self._theme_thread.join(timeout=1.5)
        self.local_fs.stop_watcher()
        self.cache.close()

    def push_event(self, event_name: str, payload: Any) -> None:
        with self._events_lock:
            self._events.append({"event": event_name, "payload": payload})
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
            if name not in ("hindsight_api_key", "spark_memory_api_token", "notion_api_key"):
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
                    opener = "open" if sys.platform == "darwin" else "xdg-open"
                    try:
                        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
            return {"ok": True}

        raise ValueError(f"Unknown RPC command: {command}")


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
    notes_dir = Path(os.environ.get("FOLIO_DEFAULT_NOTES_DIR") or (Path.home() / "Notes" / "Folio"))

    engine = FolioEngine(state_dir=state_dir, default_notes_dir=notes_dir)
    engine.start_background()

    class Handler(BaseHTTPRequestHandler):
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

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self._send_cors()
            self.end_headers()

        def do_POST(self) -> None:
            if self.path not in ("/__folio_rpc", "/__orb_rpc"):
                self._json_response(404, {"error": "Not found"})
                return
            if expected_token:
                supplied = self.headers.get("X-Folio-Token") or self.headers.get("X-Orb-Token") or ""
                if not hmac.compare_digest(supplied, expected_token):
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
            if not ui_root:
                self._json_response(404, {"error": "Static UI root not configured"})
                return
            rel = clean_path.lstrip("/") or "index.html"
            candidate = (ui_root / rel).resolve()
            if not (candidate == ui_root or str(candidate).startswith(str(ui_root) + os.sep)):
                self._json_response(403, {"error": "Forbidden"})
                return
            if not candidate.is_file():
                if "." not in Path(rel).name:
                    candidate = ui_root / "index.html"
                else:
                    self._json_response(404, {"error": "Asset not found"})
                    return
            data = candidate.read_bytes()
            mime = MIME_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
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

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    def _handle_sig(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)
    try:
        server.serve_forever()
    finally:
        engine.shutdown()
    return 0
