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


class NotionProvider:
    """External source adapter for Notion pages & databases via Notion REST API v1.
    Normalizes Notion blocks (headings, paragraphs, lists, to-dos, callouts, code,
    equations, quotes, dividers) into clean Markdown + KaTeX and indexes them in SQLite FTS5.
    Gracefully stays dormant when `NOTION_API_KEY` is unconfigured."""

    provider_id = "notion"

    def __init__(self, cache: CacheStore, secrets: CredentialResolver) -> None:
        self.cache = cache
        self.secrets = secrets
        self.api_base = (os.environ.get("FOLIO_NOTION_API_URL") or "https://api.notion.com/v1").rstrip("/")

    def _token(self) -> Optional[str]:
        return self.secrets.get("notion_api_key")

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Optional[dict[str, Any]] = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        tok = self._token()
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

    def status(self) -> dict[str, Any]:
        configured = self.secrets.has("notion_api_key")
        docs = self.cache.list_documents(provider=self.provider_id)
        return {
            "provider": self.provider_id,
            "provider_id": self.provider_id,
            "configured": configured,
            "connected": configured,
            "auth_source": self.secrets.source_kind("notion_api_key"),
            "cached_count": len(docs),
        }

    def sync_recent(self, limit: int = 20) -> dict[str, Any]:
        if not self._token():
            return {"synced": 0, "configured": False}
        res = self._request_json(
            "/search",
            method="POST",
            payload={
                "page_size": min(max(limit, 1), 50),
                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            },
        )
        synced = 0
        for item in res.get("results") or []:
            obj_type = item.get("object")
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            if obj_type == "page":
                doc = self._ingest_page_object(item)
                if doc:
                    synced += 1
            elif obj_type == "database":
                doc = self._ingest_database_object(item)
                if doc:
                    synced += 1
        return {"synced": synced, "configured": True}

    def list_tree(self, scope: Optional[str] = None) -> list[TreeNode]:
        docs = self.cache.list_documents(provider=self.provider_id)
        if not docs and self._token():
            try:
                self.sync_recent(limit=15)
                docs = self.cache.list_documents(provider=self.provider_id)
            except Exception:
                pass
        return [
            TreeNode(
                id=d.uri,
                name=d.title,
                kind="page",
                uri=d.uri,
                provider=self.provider_id,
                badge="DB" if d.provenance.source_type == "notion_database" else "PAGE",
            )
            for d in docs
        ]

    def get_document(self, uri_or_id: str, scope: Optional[str] = None) -> Optional[DocumentRecord]:
        page_id = uri_or_id
        if page_id.startswith("notion://"):
            page_id = page_id.split("/")[-1]
        uri = f"notion://workspace/{page_id}"
        cached = self.cache.get_document(uri)
        if cached or not self._token():
            return cached
        try:
            page_obj = self._request_json(f"/pages/{page_id}")
            return self._ingest_page_object(page_obj)
        except Exception:
            return None

    def search(self, query: str, scope: Optional[str] = None, limit: int = 12) -> list[dict[str, Any]]:
        return self.cache.search_fts(query, provider=self.provider_id, limit=limit)

    def _ingest_page_object(self, page: dict[str, Any]) -> Optional[DocumentRecord]:
        page_id = str(page.get("id") or "")
        title = self._extract_notion_title(page) or f"Notion Page {page_id[:8]}"
        blocks_res = self._request_json(f"/blocks/{page_id}/children?page_size=100")
        md_lines = [f"# {title}", ""]
        for blk in blocks_res.get("results") or []:
            rendered = self._block_to_markdown(blk)
            if rendered is not None:
                md_lines.append(rendered)
        body = "\n".join(md_lines).strip() + "\n"
        updated_at = str(page.get("last_edited_time") or datetime.now(timezone.utc).isoformat())
        uri = f"notion://workspace/{page_id}"
        tags = extract_tags({}, body)
        prov = ProvenanceInfo(
            author="notion",
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
            folder_path="notion/pages",
            frontmatter={"notion_id": page_id, "url": page.get("url", "")},
            tags=tags,
            wikilinks=extract_wikilinks(body),
            provenance=prov,
            readonly=True,
            content_hash=sha256_text(body),
            updated_at=updated_at,
        )
        self.cache.upsert_document(doc, scope="workspace")
        return doc

    def _ingest_database_object(self, db_obj: dict[str, Any]) -> Optional[DocumentRecord]:
        db_id = str(db_obj.get("id") or "")
        title_rich = db_obj.get("title") or []
        title = "".join(t.get("plain_text", "") for t in title_rich).strip() or f"Notion Database {db_id[:8]}"
        rows_res = self._request_json(f"/databases/{db_id}/query", method="POST", payload={"page_size": 25})
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
        uri = f"notion://workspace/{db_id}"
        prov = ProvenanceInfo(
            author="notion",
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
            folder_path="notion/databases",
            frontmatter={"notion_id": db_id, "url": db_obj.get("url", "")},
            tags=[],
            wikilinks=[],
            provenance=prov,
            readonly=True,
            content_hash=sha256_text(body),
            updated_at=updated_at,
        )
        self.cache.upsert_document(doc, scope="workspace")
        return doc

    @classmethod
    def _extract_notion_title(cls, page: dict[str, Any]) -> str:
        props = page.get("properties") or {}
        for prop in props.values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                return cls._rich_text_to_md(prop.get("title") or [])
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
        if btype == "divider":
            return "\n---\n"
        return None
