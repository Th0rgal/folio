import concurrent.futures
import hmac
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .cache import CacheStore
from .fs_watch import NativeDirectoryWatcher
from .hindsight import HindsightProvider
from .http_pool import SHARED_HTTP_POOL
from .local_fs import LocalFsProvider
from .notion import NotionProvider
from .orb_knowledge import OrbKnowledgeEngine
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
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".pdf": "application/pdf",
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
        self._theme_watcher: NativeDirectoryWatcher | None = None

        self.cache = CacheStore(self.state_dir / "folio_cache.sqlite3")
        self.secrets = CredentialResolver(state_dir=self.state_dir)
        self.local_fs = LocalFsProvider(
            self.cache,
            default_dir=default_notes_dir,
            on_change=lambda payload: self.push_event("workspace:changed", payload),
        )
        self.hindsight = HindsightProvider(self.cache, self.secrets)
        self.notion = NotionProvider(self.cache, self.secrets)
        self.copilot = OrbKnowledgeEngine(
            cache=self.cache,
            secrets=self.secrets,
            local_fs=self.local_fs,
            hindsight=self.hindsight,
            notion=self.notion,
            push_event=self.push_event,
        )

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

    @staticmethod
    def _omarchy_watch_roots() -> list[Path]:
        base = Path.home() / ".local" / "state" / "omarchy"
        candidates = [
            base / "current" / "theme",
            base / "current",
            base,
        ]
        out: list[Path] = []
        for p in candidates:
            try:
                if p.exists():
                    resolved = p.resolve()
                    if resolved.is_dir() and resolved not in out:
                        out.append(resolved)
            except OSError:
                pass
        return out

    def _on_omarchy_fs_change(self) -> None:
        mode = self._read_omarchy_mode()
        if mode and mode != self._last_omarchy_mode:
            self._last_omarchy_mode = mode
            self.push_event("theme:changed", {"mode": mode})
        if self._theme_watcher is not None:
            self._theme_watcher.notify_roots_changed()

    def start_background(self) -> None:
        self.local_fs.start_watcher()
        if self._theme_watcher is None:
            self._theme_watcher = NativeDirectoryWatcher(
                self._omarchy_watch_roots,
                self._on_omarchy_fs_change,
                recursive=False,
                debounce_sec=0.05,
                fallback_interval_sec=1.5,
                name="folio-theme-watcher",
            )
            self._theme_watcher.start()

    def shutdown(self) -> None:
        if self._theme_watcher is not None:
            self._theme_watcher.stop()
            self._theme_watcher = None
        with self._events_lock:
            for sub in list(self._subscribers):
                try:
                    sub.put_nowait(None)
                except Exception:
                    pass
            self._subscribers.clear()
        self.local_fs.stop_watcher()
        SHARED_HTTP_POOL.close()
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

    def _enrich_doc_payload(self, doc: Any) -> dict[str, Any]:
        payload = doc.to_dict()
        payload["backlinks"] = self.cache.get_backlinks(doc)
        payload["sync"] = self.hindsight.compute_note_sync_info(doc)
        return payload

    def _resolve_doc_by_target(self, target: str, bank_id: str) -> Any:
        if target.startswith("local://"):
            return self.local_fs.get_document(target)
        if target.startswith("hindsight://") or target.startswith(("kp-", "src-")):
            return self.hindsight.get_document(target, scope=bank_id)
        if target.startswith("notion://"):
            return self.notion.get_document(target)
        doc = self.local_fs.get_document(target)
        if not doc:
            doc = self.cache.find_by_slug_or_id(target)
        if not doc and target.startswith(("kp-", "src-")):
            doc = self.hindsight.get_document(target, scope=bank_id)
        return doc

    def _reveal_local_file(self, file_path: Path) -> bool:
        target = file_path.resolve()
        if not target.exists():
            return False
        if sys.platform == "darwin":
            opener = resolve_system_binary("open")
            if opener:
                subprocess.Popen([opener, "-R", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            return False

        file_uri = target.as_uri()
        dbus_send = resolve_system_binary("dbus-send")
        if dbus_send:
            try:
                res = subprocess.run(
                    [
                        dbus_send,
                        "--session",
                        "--print-reply",
                        "--dest=org.freedesktop.FileManager1",
                        "/org/freedesktop/FileManager1",
                        "org.freedesktop.FileManager1.ShowItems",
                        f"array:string:{file_uri}",
                        "string:",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2.5,
                    check=False,
                )
                if res.returncode == 0:
                    return True
            except Exception:
                pass

        parent_dir = str(target.parent if target.is_file() else target)
        for tool_name, extra_args in (("gio", ["open", parent_dir]), ("xdg-open", [parent_dir])):
            bin_path = resolve_system_binary(tool_name)
            if bin_path:
                try:
                    subprocess.Popen([bin_path, *extra_args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
                except Exception:
                    pass
        return False

    def resolve_local_note_asset(self, note_uri: str, rel_asset: str) -> Path | None:
        note_path = self.local_fs.resolve_filesystem_path(note_uri)
        if not note_path:
            return None
        rest = note_uri[len("local://") :]
        mount_id, _, _ = rest.partition("/")
        mount = next((m for m in self.cache.list_mounts() if m["mount_id"] == mount_id), None)
        if not mount:
            return None
        mount_root = Path(mount["root_path"]).resolve()
        cleaned_rel = urllib.parse.unquote(rel_asset).strip()
        if cleaned_rel.startswith("/"):
            candidate = (mount_root / cleaned_rel.lstrip("/")).resolve()
        else:
            candidate = (note_path.parent / cleaned_rel).resolve()
        if not str(candidate).startswith(str(mount_root) + os.sep) or not candidate.is_file():
            return None
        return candidate

    def dispatch(self, command: str, args: dict[str, Any]) -> Any:
        if command in ("__orb_poll_events", "poll_events"):
            return self.drain_events()

        if command == "get_bootstrap":
            active_bank = str(args.get("bank_id") or self.hindsight.default_bank)
            local_docs = self.cache.list_documents(provider="local-fs")
            return {
                "providers": {
                    "local_fs": self.local_fs.status(),
                    "hindsight": self.hindsight.status(local_docs=local_docs),
                    "notion": self.notion.status(),
                    "copilot": self.copilot.status(discover=False),
                },
                "banks": self.hindsight.list_banks(include_sync_bank=True),
                "sync_bank": self.hindsight.sync_bank,
                "active_bank": active_bank,
                "local_tree": [n.to_dict() for n in self.local_fs.list_tree()],
                "macros": self.cache.get_kv(
                    "latex:macros",
                    {"\\R": "\\mathbb{R}", "\\E": "\\mathbb{E}", "\\Prob": "\\mathbb{P}"},
                ),
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

        if command == "list_link_candidates":
            return {
                "items": self.cache.list_link_candidates(
                    query=str(args.get("query") or ""),
                    limit=int(args.get("limit") or 24),
                )
            }

        if command == "open_document":
            target = str(args.get("uri") or args.get("id") or "").strip()
            bank_id = str(args.get("bank_id") or self.hindsight.default_bank)
            if not target:
                raise ValueError("Missing document uri/id")
            doc = self._resolve_doc_by_target(target, bank_id)
            if not doc:
                raise KeyError(f"Document not found: {target}")
            return self._enrich_doc_payload(doc)

        if command == "open_filesystem_path":
            raw_target = str(args.get("path") or args.get("target") or "").strip()
            bank_id = str(args.get("bank_id") or self.hindsight.default_bank)
            if not raw_target:
                raise ValueError("Missing target path or URI")
            if raw_target.startswith("file://"):
                raw_target = urllib.parse.unquote(urllib.parse.urlsplit(raw_target).path)
            elif raw_target.startswith("folio://"):
                parsed = urllib.parse.urlsplit(raw_target)
                qs = urllib.parse.parse_qs(parsed.query)
                raw_target = (
                    (qs.get("uri") or qs.get("target") or [""])[0]
                    or urllib.parse.unquote(parsed.path.lstrip("/"))
                )
            if raw_target.startswith(("local://", "hindsight://", "notion://", "kp-", "src-")):
                doc = self._resolve_doc_by_target(raw_target, bank_id)
            else:
                doc = self.local_fs.open_filesystem_path(raw_target)
            return {
                "document": self._enrich_doc_payload(doc) if doc else None,
                "tree": [n.to_dict() for n in self.local_fs.list_tree()],
            }

        if command == "save_local_document":
            uri = str(args["uri"])
            raw_markdown = str(args["raw_markdown"])
            doc = self.local_fs.save_document(uri, raw_markdown)
            return self._enrich_doc_payload(doc)

        if command == "create_local_document":
            tags_arg = args.get("tags")
            doc = self.local_fs.create_document(
                title=str(args.get("title") or "Untitled Note"),
                folder=str(args.get("folder") or ""),
                mount_id=args.get("mount_id"),
                initial_body=args.get("initial_body"),
                tags=[str(t) for t in tags_arg] if isinstance(tags_arg, list) else None,
            )
            payload = self._enrich_doc_payload(doc)
            payload["tree"] = [n.to_dict() for n in self.local_fs.list_tree()]
            return payload

        if command == "create_local_folder":
            res = self.local_fs.create_folder(
                folder=str(args.get("folder") or ""),
                mount_id=args.get("mount_id"),
            )
            return {
                **res,
                "folders": self.local_fs.list_folders(),
                "tree": [n.to_dict() for n in self.local_fs.list_tree()],
            }

        if command == "rename_local_document":
            doc = self.local_fs.rename_or_move_document(
                str(args["uri"]),
                new_title=args.get("new_title"),
                new_folder=args.get("new_folder"),
                raw_markdown_override=args.get("raw_markdown"),
            )
            return {
                "document": self._enrich_doc_payload(doc),
                "folders": self.local_fs.list_folders(),
                "tree": [n.to_dict() for n in self.local_fs.list_tree()],
            }

        if command == "open_daily_note":
            doc = self.local_fs.open_or_create_daily_note(args.get("date"))
            return {
                "document": self._enrich_doc_payload(doc),
                "tree": [n.to_dict() for n in self.local_fs.list_tree()],
            }

        if command == "fork_to_local":
            source_uri = str(args.get("uri") or "").strip()
            bank_id = str(args.get("bank_id") or self.hindsight.default_bank)
            folder = str(args.get("folder") or "Notes")
            src_doc = self._resolve_doc_by_target(source_uri, bank_id)
            if not src_doc:
                raise KeyError(f"Source document not found: {source_uri}")
            forked = self.local_fs.fork_remote_to_local(src_doc, folder=folder)
            return {
                "document": self._enrich_doc_payload(forked),
                "tree": [n.to_dict() for n in self.local_fs.list_tree()],
            }

        if command == "reveal_in_file_manager":
            uri = str(args.get("uri") or "").strip()
            fpath = self.local_fs.resolve_filesystem_path(uri)
            if not fpath:
                raise ValueError(f"Cannot resolve local path for {uri}")
            ok = self._reveal_local_file(fpath)
            return {"ok": ok, "path": str(fpath)}

        if command == "delete_local_document":
            res = self.local_fs.delete_local_document(str(args["uri"]))
            return {
                **res,
                "tree": [n.to_dict() for n in self.local_fs.list_tree()],
                "sync_summary": self.hindsight.summarize_sync(),
            }

        if command == "mount_directory":
            res = self.local_fs.mount_directory(
                Path(str(args["path"])),
                label=args.get("label"),
            )
            return {
                "mount": res,
                "providers_local": self.local_fs.status(),
                "tree": [n.to_dict() for n in self.local_fs.list_tree()],
            }

        if command == "unmount_directory":
            self.local_fs.unmount_directory(str(args["mount_id"]))
            return {
                "providers_local": self.local_fs.status(),
                "tree": [n.to_dict() for n in self.local_fs.list_tree()],
            }

        if command == "unified_search":
            query = str(args.get("query") or "")
            bank_id = str(args.get("bank_id") or self.hindsight.default_bank)
            include_hindsight = bool(args.get("include_hindsight", True))
            include_notion = bool(args.get("include_notion", True))
            local_only = bool(args.get("local_only", False))
            banks_arg = args.get("banks")
            banks_list = [str(b) for b in banks_arg if b] if isinstance(banks_arg, list) else None
            limit = int(args.get("limit") or 24)

            local_hits = self.local_fs.search(query, limit=limit)
            if local_only:
                hs_hits = (
                    self.hindsight.search(
                        query, scope=bank_id, limit=limit // 2, local_only=True, banks=banks_list
                    )
                    if include_hindsight
                    else []
                )
                notion_hits = (
                    self.cache.search_fts(query, provider="notion", limit=8) if include_notion else []
                )
            else:
                hs_hits = []
                notion_hits = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    fut_hs = (
                        pool.submit(
                            self.hindsight.search,
                            query,
                            bank_id,
                            max(8, limit // 2),
                            local_only=False,
                            banks=banks_list,
                        )
                        if include_hindsight
                        else None
                    )
                    fut_no = pool.submit(self.notion.search, query, 8) if include_notion else None
                    if fut_hs is not None:
                        hs_hits = fut_hs.result()
                    if fut_no is not None:
                        notion_hits = fut_no.result()

            combined = [*local_hits, *hs_hits, *notion_hits]
            seen: set[str] = set()
            deduped: list[dict[str, Any]] = []
            for item in sorted(combined, key=lambda x: float(x.get("score") or 0.0), reverse=True):
                u = item["uri"]
                if u not in seen:
                    seen.add(u)
                    deduped.append(item)
            return {"results": deduped[:limit], "bank_id": bank_id, "local_only": local_only}

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

        if command == "hindsight_sync_note":
            uri = str(args.get("uri") or "").strip()
            if not uri.startswith("local://"):
                raise ValueError("hindsight_sync_note requires a local:// note URI")
            if args.get("raw_markdown") is not None:
                doc = self.local_fs.save_document(uri, str(args["raw_markdown"]))
            else:
                doc = self.local_fs.get_document(uri)
            if not doc:
                raise KeyError(f"Local note not found: {uri}")
            res = self.hindsight.sync_local_note(
                doc,
                bank_id=args.get("bank_id"),
                promote_raw_source=bool(args.get("promote_raw_source", False)),
                force=bool(args.get("force", False)),
            )
            return {
                **res,
                "document": self._enrich_doc_payload(doc),
                "sync_summary": self.hindsight.summarize_sync(),
                "local_tree": [n.to_dict() for n in self.local_fs.list_tree()],
            }

        if command == "hindsight_sync_batch":
            local_docs = self.cache.list_documents(provider="local-fs")
            res = self.hindsight.sync_batch_notes(
                local_docs,
                bank_id=args.get("bank_id"),
                include_unsynced=bool(args.get("include_unsynced", True)),
            )
            return {
                **res,
                "local_tree": [n.to_dict() for n in self.local_fs.list_tree()],
            }

        if command == "hindsight_set_sync_bank":
            new_bank = self.hindsight.set_sync_bank(str(args.get("bank_id") or "folio"))
            return {
                "sync_bank": new_bank,
                "banks": self.hindsight.list_banks(include_sync_bank=True),
                "sync_summary": self.hindsight.summarize_sync(),
            }

        if command == "hindsight_sync_status":
            return self.hindsight.summarize_sync()

        if command == "notion_sync":
            res = self.notion.sync_recent(limit=int(args.get("limit") or 20))
            return {**res, "tree": [n.to_dict() for n in self.notion.list_tree()]}

        if command == "set_session_credential":
            name = str(args.get("name") or "")
            value = str(args.get("value") or "")
            allowed_slots = (
                "hindsight_api_key",
                "spark_memory_api_token",
                "notion_api_key",
                "notion_api_key_perso",
                "orbrouter_api_key",
            )
            if name not in allowed_slots:
                raise ValueError(f"Unsupported credential slot: {name}")
            self.secrets.set_ephemeral(name, value)
            self.hindsight._banks_cache = None
            self.copilot._models_cache = None
            return {
                "hindsight": self.hindsight.status(),
                "notion": self.notion.status(),
                "copilot": self.copilot.status(discover=True),
            }

        if command == "copilot_status":
            return self.copilot.status(discover=bool(args.get("discover", True)))

        if command == "copilot_query":
            hist = args.get("history")
            return self.copilot.start_query(
                prompt=str(args.get("prompt") or ""),
                profile=str(args.get("profile") or "builtin/smart"),
                scope=str(args.get("scope") or "vault"),
                active_uri=args.get("active_uri"),
                active_markdown=args.get("active_markdown"),
                selection=args.get("selection"),
                history=hist if isinstance(hist, list) else None,
                bank_id=args.get("bank_id"),
                auto_apply=bool(args.get("auto_apply", False)),
                allow_external_for_private=bool(args.get("allow_external_for_private", False)),
                request_id=args.get("request_id"),
                blocking=bool(args.get("blocking", False)),
            )

        if command == "copilot_cancel":
            return self.copilot.cancel_query(str(args.get("request_id") or ""))

        if command == "copilot_apply_proposal":
            return self.copilot.apply_proposal(str(args.get("proposal_id") or ""))

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
                    opener_name = "open" if sys.platform == "darwin" else "xdg-open"
                    opener = resolve_system_binary(opener_name)
                    if opener:
                        try:
                            subprocess.Popen(
                                [opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                            )
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
            if clean_path == "/__folio_asset":
                if not self._check_token(allow_query=True):
                    self._json_response(403, {"error": "Forbidden: invalid locald token"})
                    return
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                note_uri = (qs.get("uri") or [""])[0]
                rel_asset = (qs.get("rel") or [""])[0]
                asset_path = engine.resolve_local_note_asset(note_uri, rel_asset)
                if not asset_path:
                    self._json_response(404, {"error": "Local asset not found"})
                    return
                data = asset_path.read_bytes()
                mime = MIME_TYPES.get(asset_path.suffix.lower(), "application/octet-stream")
                self.send_response(200)
                self._send_cors()
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
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
            assert len(boot["providers"]["copilot"]["profiles"]) == 4
            hits = eng.dispatch("unified_search", {"query": "Bayesian", "include_hindsight": False})
            assert len(hits["results"]) >= 1
            daily = eng.dispatch("open_daily_note", {"date": "2026-11-11"})
            assert daily["document"]["uri"].endswith("Journal/2026-11-11.md")
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
