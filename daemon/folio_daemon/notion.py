import concurrent.futures
import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from .cache import CacheStore
from .markdown_utils import extract_tags, extract_wikilinks, sha256_text
from .models import DocumentRecord, ProvenanceInfo, TreeNode
from .secrets import CredentialResolver


NOTION_VERSION = "2022-06-28"
WORKSPACE_SLOTS: tuple[tuple[str, str, str], ...] = (
    ("perso", "notion_api_key_perso", "Personal"),
    ("work", "notion_api_key", "Workspace"),
)


class NotionProvider:
    """External source adapter for Notion pages & databases via Notion REST API v1.
    Supports multiple Notion workspaces (`NOTION_API_KEY_PERSO` + `NOTION_API_KEY`),
    builds nested workspace & parent-page folder trees from fast `/search` indexes,
    lazily fetches & caches full Markdown + KaTeX blocks on open, and indexes in SQLite FTS5."""

    provider_id = "notion"

    def __init__(self, cache: CacheStore, secrets: CredentialResolver) -> None:
        self.cache = cache
        self.secrets = secrets
        self.api_base = (os.environ.get("FOLIO_NOTION_API_URL") or "https://api.notion.com/v1").rstrip("/")

    def _active_workspaces(self) -> list[tuple[str, str, str]]:
        """Returns list of `(slot, token, fallback_label)` for distinct configured Notion tokens."""
        out: list[tuple[str, str, str]] = []
        seen_tokens: set[str] = set()
        for slot, secret_name, fallback_label in WORKSPACE_SLOTS:
            tok = self.secrets.get(secret_name)
            if tok and tok not in seen_tokens:
                seen_tokens.add(tok)
                out.append((slot, tok, fallback_label))
        return out

    def _token(self, slot: Optional[str] = None) -> Optional[str]:
        active = self._active_workspaces()
        if slot:
            for s, tok, _ in active:
                if s == slot:
                    return tok
        return active[0][1] if active else None

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Optional[dict[str, Any]] = None,
        token: Optional[str] = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        tok = token or self._token()
        if not tok:
            raise RuntimeError("Notion integration not configured (set NOTION_API_KEY)")
        headers = {
            "Authorization": f"Bearer {tok}",
            "Notion-Version": NOTION_VERSION,
            "Accept": "application/json",
            "User-Agent": "folio-desktop/0.1.0",
        }
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"{self.api_base}{path}", data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def _resolve_workspace_name(self, slot: str, token: str, fallback: str) -> str:
        kv_key = f"notion:ws_name:{slot}"
        cached = self.cache.get_kv(kv_key)
        if isinstance(cached, str) and cached.strip():
            return cached.strip()
        try:
            me = self._request_json("/users/me", token=token, timeout=8.0)
            ws_name = str(((me.get("bot") or {}).get("workspace_name")) or me.get("name") or fallback).strip()
            if ws_name:
                self.cache.set_kv(kv_key, ws_name)
                return ws_name
        except Exception:
            pass
        return fallback

    def status(self) -> dict[str, Any]:
        active = self._active_workspaces()
        configured = len(active) > 0
        docs = self.cache.list_documents(provider=self.provider_id)
        sources = [
            self.secrets.source_kind(secret_name)
            for _, secret_name, _ in WORKSPACE_SLOTS
            if self.secrets.has(secret_name)
        ]
        return {
            "provider": self.provider_id,
            "provider_id": self.provider_id,
            "configured": configured,
            "connected": configured,
            "workspace_count": len(active),
            "auth_source": sources[0] if sources else "unconfigured",
            "cached_count": len(docs),
        }

    def _sync_workspace_index(self, slot: str, token: str, fallback_label: str, limit: int = 40) -> list[dict[str, Any]]:
        ws_name = self._resolve_workspace_name(slot, token, fallback_label)
        res = self._request_json(
            "/search",
            method="POST",
            token=token,
            payload={
                "page_size": min(max(limit, 1), 60),
                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            },
            timeout=12.0,
        )
        entries: list[dict[str, Any]] = []
        for item in res.get("results") or []:
            obj_type = str(item.get("object") or "")
            item_id = str(item.get("id") or "")
            if not item_id or obj_type not in ("page", "database"):
                continue
            if obj_type == "database":
                title_rich = item.get("title") or []
                title = "".join(t.get("plain_text", "") for t in title_rich).strip() or f"Database {item_id[:8]}"
            else:
                title = self._extract_notion_title(item).strip() or f"Untitled ({item_id[:8]})"
            parent = item.get("parent") or {}
            ptype = parent.get("type")
            parent_id = None
            if ptype == "page_id":
                parent_id = parent.get("page_id")
            elif ptype == "database_id":
                parent_id = parent.get("database_id")
            entries.append(
                {
                    "id": item_id,
                    "slot": slot,
                    "workspace_name": ws_name,
                    "object": obj_type,
                    "title": title,
                    "parent_id": parent_id,
                    "url": str(item.get("url") or ""),
                    "last_edited_time": str(item.get("last_edited_time") or ""),
                    "created_time": str(item.get("created_time") or ""),
                }
            )
        self.cache.set_kv(f"notion:index:{slot}", entries)
        return entries

    def sync_recent(self, limit: int = 24) -> dict[str, Any]:
        active = self._active_workspaces()
        if not active:
            return {"synced": 0, "configured": False}
        total_indexed = 0
        eager_limit = max(2, min(6, limit // max(1, len(active))))
        for slot, tok, fallback_label in active:
            try:
                entries = self._sync_workspace_index(slot, tok, fallback_label, limit=max(limit, 40))
                total_indexed += len(entries)
                for entry in entries[:eager_limit]:
                    uri = f"notion://{slot}/{entry['id']}"
                    if not self.cache.get_document(uri):
                        try:
                            self._fetch_and_cache_item(slot, tok, entry["id"], entry.get("object"))
                        except Exception:
                            pass
            except Exception:
                pass
        return {"synced": total_indexed, "configured": True}

    @classmethod
    def _build_workspace_nodes(cls, slot: str, entries: list[dict[str, Any]]) -> list[TreeNode]:
        by_id: dict[str, dict[str, Any]] = {e["id"]: e for e in entries if e.get("id")}
        children_map: dict[Optional[str], list[dict[str, Any]]] = {}
        for e in entries:
            pid = e.get("parent_id")
            effective_parent = pid if (pid and pid in by_id and pid != e["id"]) else None
            children_map.setdefault(effective_parent, []).append(e)

        visited: set[str] = set()

        def make_node(entry: dict[str, Any]) -> TreeNode:
            eid = entry["id"]
            visited.add(eid)
            uri = f"notion://{slot}/{eid}"
            raw_kids = [k for k in children_map.get(eid, []) if k["id"] not in visited]
            child_nodes = [make_node(k) for k in raw_kids]
            is_db = entry.get("object") == "database"
            return TreeNode(
                id=uri,
                name=entry.get("title") or eid[:8],
                kind="folder" if child_nodes else "page",
                uri=uri,
                provider=cls.provider_id,
                badge=str(len(child_nodes)) if child_nodes else ("DB" if is_db else None),
                children=child_nodes,
            )

        return [make_node(root_entry) for root_entry in children_map.get(None, [])]

    def list_tree(self, scope: Optional[str] = None) -> list[TreeNode]:
        active = self._active_workspaces()
        if not active:
            docs = self.cache.list_documents(provider=self.provider_id)
            return [
                TreeNode(
                    id=d.uri,
                    name=d.title,
                    kind="page",
                    uri=d.uri,
                    provider=self.provider_id,
                    badge="DB" if d.provenance.source_type == "notion_database" else None,
                )
                for d in docs
            ]

        def load_slot(item: tuple[str, str, str]) -> Optional[TreeNode]:
            slot, tok, fallback_label = item
            ws_name = self._resolve_workspace_name(slot, tok, fallback_label)
            entries = self.cache.get_kv(f"notion:index:{slot}")
            if not isinstance(entries, list) or not entries:
                try:
                    entries = self._sync_workspace_index(slot, tok, fallback_label, limit=40)
                except Exception:
                    entries = []
            children = self._build_workspace_nodes(slot, entries)
            return TreeNode(
                id=f"notion-ws:{slot}",
                name=ws_name,
                kind="folder",
                provider=self.provider_id,
                badge=str(len(entries)) if entries else "0",
                children=children,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(active))) as pool:
            results = list(pool.map(load_slot, active))
        return [n for n in results if n is not None]

    def _fetch_and_cache_item(
        self,
        slot: str,
        token: str,
        item_id: str,
        obj_hint: Optional[str] = None,
    ) -> Optional[DocumentRecord]:
        ws_name = self._resolve_workspace_name(slot, token, slot)
        if obj_hint == "database":
            db_obj = self._request_json(f"/databases/{item_id}", token=token)
            return self._ingest_database_object(db_obj, slot=slot, token=token, ws_name=ws_name)
        try:
            page_obj = self._request_json(f"/pages/{item_id}", token=token)
            return self._ingest_page_object(page_obj, slot=slot, token=token, ws_name=ws_name)
        except Exception:
            db_obj = self._request_json(f"/databases/{item_id}", token=token)
            return self._ingest_database_object(db_obj, slot=slot, token=token, ws_name=ws_name)

    def get_document(self, uri_or_id: str, scope: Optional[str] = None) -> Optional[DocumentRecord]:
        raw = uri_or_id.strip()
        slot_hint: Optional[str] = None
        item_id = raw
        if raw.startswith("notion://"):
            rest = raw[len("notion://") :]
            parts = [p for p in rest.split("/") if p]
            if len(parts) >= 2:
                slot_hint = parts[0]
                item_id = parts[-1]
            elif len(parts) == 1:
                item_id = parts[0]

        for candidate_uri in (
            [f"notion://{slot_hint}/{item_id}"] if slot_hint else []
        ) + [f"notion://{s}/{item_id}" for s, _, _ in WORKSPACE_SLOTS] + [f"notion://workspace/{item_id}"]:
            cached = self.cache.get_document(candidate_uri)
            if cached:
                return cached

        active = self._active_workspaces()
        if not active:
            return None

        ordered = sorted(active, key=lambda x: 0 if x[0] == slot_hint else 1)
        for slot, tok, _ in ordered:
            index_entries = self.cache.get_kv(f"notion:index:{slot}")
            obj_hint = None
            if isinstance(index_entries, list):
                for entry in index_entries:
                    if entry.get("id") == item_id:
                        obj_hint = entry.get("object")
                        break
            try:
                doc = self._fetch_and_cache_item(slot, tok, item_id, obj_hint=obj_hint)
                if doc:
                    return doc
            except Exception:
                continue
        return None

    def search(self, query: str, scope: Optional[str] = None, limit: int = 12) -> list[dict[str, Any]]:
        fts_hits = self.cache.search_fts(query, provider=self.provider_id, limit=limit)
        seen_uris = {h["uri"] for h in fts_hits}
        q_lower = query.strip().lower()
        if q_lower:
            for slot, _, _ in self._active_workspaces():
                entries = self.cache.get_kv(f"notion:index:{slot}")
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    title = str(entry.get("title") or "")
                    if q_lower in title.lower():
                        uri = f"notion://{slot}/{entry['id']}"
                        if uri in seen_uris:
                            continue
                        seen_uris.add(uri)
                        fts_hits.append(
                            {
                                "uri": uri,
                                "title": title,
                                "snippet": f"{entry.get('workspace_name') or 'Notion'} · {entry.get('object') or 'page'}",
                                "provider": self.provider_id,
                                "score": 0.72,
                                "is_agent_summary": False,
                                "badge": entry.get("workspace_name") or "Notion",
                            }
                        )
                        if len(fts_hits) >= limit:
                            return fts_hits
        return fts_hits[:limit]

    def _ingest_page_object(
        self,
        page: dict[str, Any],
        *,
        slot: str = "workspace",
        token: Optional[str] = None,
        ws_name: str = "Notion",
    ) -> Optional[DocumentRecord]:
        page_id = str(page.get("id") or "")
        title = self._extract_notion_title(page) or f"Notion Page {page_id[:8]}"
        blocks_res = self._request_json(f"/blocks/{page_id}/children?page_size=100", token=token)
        md_lines = [f"# {title}", ""]
        for blk in blocks_res.get("results") or []:
            rendered = self._block_to_markdown(blk)
            if rendered is not None:
                md_lines.append(rendered)
        body = "\n".join(md_lines).strip() + "\n"
        updated_at = str(page.get("last_edited_time") or datetime.now(timezone.utc).isoformat())
        uri = f"notion://{slot}/{page_id}"
        tags = extract_tags({}, body)
        prov = ProvenanceInfo(
            author=ws_name,
            source_type="notion_page",
            is_agent_summary=False,
            document_id=page_id,
            original_date=str(page.get("created_time") or updated_at),
            updated_at=updated_at,
            tags=tags,
        )
        doc = DocumentRecord(
            uri=uri,
            provider=self.provider_id,
            external_id=page_id,
            title=title,
            body=body,
            raw_markdown=body,
            folder_path=ws_name,
            frontmatter={"url": page.get("url", "")},
            tags=tags,
            wikilinks=extract_wikilinks(body),
            provenance=prov,
            readonly=True,
            content_hash=sha256_text(body),
            updated_at=updated_at,
        )
        self.cache.upsert_document(doc, scope=slot)
        return doc

    def _ingest_database_object(
        self,
        db_obj: dict[str, Any],
        *,
        slot: str = "workspace",
        token: Optional[str] = None,
        ws_name: str = "Notion",
    ) -> Optional[DocumentRecord]:
        db_id = str(db_obj.get("id") or "")
        title_rich = db_obj.get("title") or []
        title = "".join(t.get("plain_text", "") for t in title_rich).strip() or f"Notion Database {db_id[:8]}"
        rows_res = self._request_json(
            f"/databases/{db_id}/query", method="POST", token=token, payload={"page_size": 25}
        )
        rows = rows_res.get("results") or []
        props_schema = list((db_obj.get("properties") or {}).keys())[:5]
        md_lines = [f"# {title}", ""]
        if props_schema:
            md_lines.append("| " + " | ".join(props_schema) + " |")
            md_lines.append("| " + " | ".join("---" for _ in props_schema) + " |")
            for row in rows:
                rprops = row.get("properties") or {}
                cells = [self._prop_plain(rprops.get(col) or {}) for col in props_schema]
                md_lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
        body = "\n".join(md_lines).strip() + "\n"
        updated_at = str(db_obj.get("last_edited_time") or datetime.now(timezone.utc).isoformat())
        uri = f"notion://{slot}/{db_id}"
        prov = ProvenanceInfo(
            author=ws_name,
            source_type="notion_database",
            is_agent_summary=False,
            document_id=db_id,
            updated_at=updated_at,
        )
        doc = DocumentRecord(
            uri=uri,
            provider=self.provider_id,
            external_id=db_id,
            title=title,
            body=body,
            raw_markdown=body,
            folder_path=ws_name,
            frontmatter={"url": db_obj.get("url", "")},
            tags=[],
            wikilinks=[],
            provenance=prov,
            readonly=True,
            content_hash=sha256_text(body),
            updated_at=updated_at,
        )
        self.cache.upsert_document(doc, scope=slot)
        return doc

    @classmethod
    def _extract_notion_title(cls, page: dict[str, Any]) -> str:
        props = page.get("properties") or {}
        for prop in props.values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                return cls._rich_text_to_md(prop.get("title") or []).replace("**", "").replace("*", "")
        return ""

    @classmethod
    def _prop_plain(cls, prop: dict[str, Any]) -> str:
        ptype = prop.get("type")
        if ptype in ("title", "rich_text"):
            return cls._rich_text_to_md(prop.get(ptype) or [])
        if ptype == "select":
            return str((prop.get("select") or {}).get("name") or "")
        if ptype == "multi_select":
            return ", ".join(str(x.get("name") or "") for x in (prop.get("multi_select") or []))
        if ptype == "status":
            return str((prop.get("status") or {}).get("name") or "")
        if ptype == "number":
            val = prop.get("number")
            return "" if val is None else str(val)
        if ptype == "checkbox":
            return "Yes" if prop.get("checkbox") else "No"
        if ptype == "date":
            return str((prop.get("date") or {}).get("start") or "")
        return ""

    @classmethod
    def _rich_text_to_md(cls, rich_list: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for span in rich_list:
            stype = span.get("type")
            if stype == "equation":
                expr = (span.get("equation") or {}).get("expression", "")
                parts.append(f"${expr}$")
                continue
            txt = str(span.get("plain_text") or "")
            ann = span.get("annotations") or {}
            if ann.get("code"):
                txt = f"`{txt}`"
            if ann.get("bold"):
                txt = f"**{txt}**"
            if ann.get("italic"):
                txt = f"*{txt}*"
            href = span.get("href")
            if href:
                txt = f"[{txt}]({href})"
            parts.append(txt)
        return "".join(parts)

    @classmethod
    def _block_to_markdown(cls, block: dict[str, Any]) -> Optional[str]:
        btype = str(block.get("type") or "")
        payload = block.get(btype) or {}
        if btype == "paragraph":
            return cls._rich_text_to_md(payload.get("rich_text") or []) + "\n"
        if btype in ("heading_1", "heading_2", "heading_3"):
            hashes = "#" * int(btype[-1])
            return f"{hashes} {cls._rich_text_to_md(payload.get('rich_text') or [])}\n"
        if btype == "bulleted_list_item":
            return f"- {cls._rich_text_to_md(payload.get('rich_text') or [])}"
        if btype == "numbered_list_item":
            return f"1. {cls._rich_text_to_md(payload.get('rich_text') or [])}"
        if btype == "to_do":
            mark = "x" if payload.get("checked") else " "
            return f"- [{mark}] {cls._rich_text_to_md(payload.get('rich_text') or [])}"
        if btype == "toggle":
            return f"- **{cls._rich_text_to_md(payload.get('rich_text') or [])}**"
        if btype == "child_page":
            title = str(payload.get("title") or "Subpage")
            return f"- 📄 **{title}**"
        if btype == "child_database":
            title = str(payload.get("title") or "Database")
            return f"- 🗂️ **{title}**"
        if btype == "quote":
            return f"> {cls._rich_text_to_md(payload.get('rich_text') or [])}\n"
        if btype == "callout":
            return f"> [!note]\n> {cls._rich_text_to_md(payload.get('rich_text') or [])}\n"
        if btype == "code":
            lang = str(payload.get("language") or "").lower()
            if lang == "plain text":
                lang = ""
            code_txt = "".join(str(x.get("plain_text") or "") for x in (payload.get("rich_text") or []))
            return f"```{lang}\n{code_txt}\n```\n"
        if btype == "equation":
            expr = str(payload.get("expression") or "")
            return f"$$\n{expr}\n$$\n"
        if btype == "bookmark":
            url = str(payload.get("url") or "")
            return f"[{url}]({url})\n" if url else None
        if btype == "divider":
            return "\n---\n"
        return None
