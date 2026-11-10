import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from .cache import CacheStore
from .markdown_utils import (
    extract_hindsight_refs,
    extract_tags,
    extract_title,
    extract_wikilinks,
    parse_frontmatter,
    sha256_text,
    strip_provenance_headers,
)
from .models import DocumentRecord, ProvenanceInfo, TreeNode
from .secrets import CredentialResolver


BANK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
USER_AGENT = "folio-desktop/0.1.0 (+https://github.com/Th0rgal/folio)"


def _validate_bank_id(bank_id: str) -> str:
    cleaned = (bank_id or "thomas").strip()
    if not BANK_ID_RE.match(cleaned):
        raise ValueError(f"Invalid Hindsight bank_id: {bank_id!r}")
    return cleaned


class HindsightProvider:
    """First-class Hindsight & Spark-Memory adapter.
    - Reads Knowledge Pages (`kp-*`), Knowledge Tree (`kf-*`), Memory Banks (`thomas`, `ppl`, ...),
      raw archive sources (`src-*`), and Hindsight documents strictly READ-ONLY during browsing/search.
    - Distinguishes human-authored sources (`author: thomas`, `is_agent_summary: false`) from
      agent syntheses/Knowledge Pages (`is_agent_summary: true`) with explicit provenance metadata.
    - Supports explicit user-initiated raw-first `save_source` via MCP `tools/call` (never triggered
      automatically; disabled whenever `FOLIO_HINDSIGHT_READONLY=1`).
    """

    provider_id = "hindsight"

    def __init__(self, cache: CacheStore, secrets: CredentialResolver) -> None:
        self.cache = cache
        self.secrets = secrets
        self.base_url = (
            os.environ.get("FOLIO_HINDSIGHT_URL")
            or os.environ.get("HINDSIGHT_API_URL")
            or os.environ.get("HINDSIGHT_BASE_URL")
            or "https://memory.thomas.md"
        ).rstrip("/")
        self.default_bank = _validate_bank_id(
            os.environ.get("FOLIO_DEFAULT_BANK")
            or os.environ.get("HINDSIGHT_BANK_ID")
            or "thomas"
        )
        self.strict_readonly = os.environ.get("FOLIO_HINDSIGHT_READONLY", "0") == "1"
        self._banks_cache: Optional[list[dict[str, Any]]] = None

    def _primary_token(self) -> Optional[str]:
        return self.secrets.get("hindsight_api_key") or self.secrets.get("spark_memory_api_token")

    def _gateway_token(self) -> Optional[str]:
        return self.secrets.get("spark_memory_api_token") or self.secrets.get("hindsight_api_key")

    def _request_json(
        self,
        path_or_url: str,
        *,
        method: str = "GET",
        payload: Optional[dict[str, Any]] = None,
        token: Optional[str] = None,
        timeout: float = 18.0,
    ) -> Any:
        tok = token or self._primary_token()
        if not tok:
            raise RuntimeError("Hindsight credentials not configured (HINDSIGHT_API_KEY / SPARK_MEMORY_API_TOKEN)")
        url = path_or_url if path_or_url.startswith("http") else f"{self.base_url}{path_or_url}"
        headers = {
            "Authorization": f"Bearer {tok}",
            "Accept": "application/json, text/event-stream",
            "User-Agent": USER_AGENT,
        }
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            ctype = resp.headers.get("Content-Type", "")
            if "text/event-stream" in ctype or raw.lstrip().startswith("event:"):
                for line in raw.splitlines():
                    if line.startswith("data: "):
                        return json.loads(line[6:])
            return json.loads(raw)

    def status(self) -> dict[str, Any]:
        has_hs = self.secrets.has("hindsight_api_key")
        has_sm = self.secrets.has("spark_memory_api_token")
        cached_docs = self.cache.list_documents(provider=self.provider_id)
        connected = has_hs or has_sm
        return {
            "provider": self.provider_id,
            "provider_id": self.provider_id,
            "configured": connected,
            "connected": connected,
            "base_url": self.base_url,
            "api_url": self.base_url,
            "default_bank": self.default_bank,
            "readonly_guard": self.strict_readonly,
            "readonly": self.strict_readonly,
            "auth_sources": {
                "hindsight_api_key": self.secrets.source_kind("hindsight_api_key"),
                "spark_memory_api_token": self.secrets.source_kind("spark_memory_api_token"),
            },
            "cached_count": len(cached_docs),
        }

    def _sort_banks(self, banks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def sort_key(b: dict[str, Any]) -> tuple[int, str]:
            bid = str(b.get("bank_id") or "")
            if bid == self.default_bank:
                return (0, bid.lower())
            if bid.lower() == "spark-memory-bank":
                return (2, bid.lower())
            return (1, bid.lower())

        return sorted(banks, key=sort_key)

    def list_banks(self) -> list[dict[str, Any]]:
        if self._banks_cache is not None:
            return self._banks_cache
        fallback = self._sort_banks(
            [
                {"bank_id": "thomas", "name": "Thomas Personal Memory"},
                {"bank_id": "ppl", "name": "Aviation (PPL)"},
            ]
        )
        if not self._primary_token():
            return fallback
        try:
            res = self._request_json("/v1/default/banks", timeout=10.0)
            banks = res.get("banks") if isinstance(res, dict) else []
            if isinstance(banks, list) and banks:
                out = self._sort_banks(
                    [
                        {
                            "bank_id": b.get("bank_id", ""),
                            "name": b.get("name") or b.get("bank_id", ""),
                        }
                        for b in banks
                        if b.get("bank_id")
                    ]
                )
                self._banks_cache = out
                return out
        except Exception:
            pass
        return fallback

    @staticmethod
    def _format_hindsight_doc_label(doc_id: str, meta: dict[str, Any], context: str = "") -> str:
        for key in ("title", "doc_name", "name", "subject"):
            val = str(meta.get(key) or "").strip()
            if val:
                return val

        session_key = str(meta.get("session_key") or "").strip()
        part = str(meta.get("part") or "").strip()
        parts = str(meta.get("parts") or "").strip()
        if session_key and part:
            suffix = f"{part}/{parts}" if parts else part
            return f"{session_key} · Part {suffix}"

        m_thread = re.match(r"^thread_(.+?)_(\d{6,})$", doc_id)
        if m_thread:
            who = m_thread.group(1).replace("_", " ").strip()
            return f"SMS · {who}" if who else doc_id

        if doc_id.startswith("rm-"):
            short_tail = doc_id.rsplit("-", 1)[-1][:8]
            return f"reMarkable Page · {short_tail}"

        cleaned = re.sub(r"^gdrive-", "", doc_id)
        if re.match(r"^[A-Za-z0-9_-]{4,80}$", cleaned) and ("-" in cleaned or "_" in cleaned):
            acronyms = {"ppl", "nav", "met", "agk", "law", "hpl", "ops", "fpl", "com", "pof", "vfr", "ifr", "icao", "sms"}
            words = [w for w in re.split(r"[-_]+", cleaned) if w]
            if words:
                pretty = " ".join(
                    w.upper() if w.lower() in acronyms else (w.capitalize() if w.islower() else w)
                    for w in words
                )
                return pretty

        if context and context not in ("general",) and "=" not in context and len(context) <= 48:
            return f"{doc_id[:24]} ({context})"
        return doc_id

    @staticmethod
    def _classify_doc_folder(doc_id: str, meta: dict[str, Any], is_agent: bool) -> str:
        stype = str(meta.get("type_de_source") or "").lower()
        if is_agent or "progress" in stype or "summary" in stype:
            return "Syntheses & Progress"
        if "audio" in stype or "lecture" in stype or "gdrive-" in doc_id:
            return "Lectures & Course Material"
        if "remarkable" in stype or doc_id.startswith("rm-"):
            return "reMarkable Notes"
        if doc_id.startswith("thread_"):
            return "Message Threads"
        return "Archive Documents"

    def _list_bank_documents_nodes(self, bank_id: str, limit: int = 48) -> list[TreeNode]:
        cache_key = f"hindsight:docs_tree:{bank_id}"
        items: Optional[list[dict[str, Any]]] = None
        if self._primary_token():
            try:
                res = self._request_json(f"/v1/default/banks/{bank_id}/documents?limit={limit}", timeout=12.0)
                raw_items = res.get("items") if isinstance(res, dict) else None
                if isinstance(raw_items, list):
                    items = raw_items
                    self.cache.set_kv(cache_key, items)
            except Exception:
                items = None

        if items is None:
            cached = self.cache.get_kv(cache_key)
            if isinstance(cached, list):
                items = cached

        if not items:
            return []

        leaves_by_group: dict[str, list[TreeNode]] = {}
        flat_leaves: list[TreeNode] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            doc_id = str(item.get("id") or "").strip()
            if not doc_id:
                continue
            rp = item.get("retain_params") or {}
            rp_meta = rp.get("metadata") if isinstance(rp, dict) else {}
            doc_meta = item.get("document_metadata") or {}
            merged_meta: dict[str, Any] = {
                **(rp_meta if isinstance(rp_meta, dict) else {}),
                **(doc_meta if isinstance(doc_meta, dict) else {}),
            }
            if str(merged_meta.get("ocr_routing_mode") or "").lower() == "blank":
                continue
            auteur = str(merged_meta.get("auteur") or "thomas")
            stype = str(merged_meta.get("type_de_source") or "")
            is_agent = (
                str(merged_meta.get("is_agent_summary", "")).lower() == "true"
                or auteur.lower().startswith("agent")
                or stype == "agent_summary"
            )
            context_str = str(rp.get("context") or "") if isinstance(rp, dict) else ""
            label = self._format_hindsight_doc_label(doc_id, merged_meta, context_str)
            uri = f"hindsight://{bank_id}/document/{doc_id}"
            node = TreeNode(
                id=uri,
                name=label,
                kind="page",
                uri=uri,
                provider=self.provider_id,
                badge="AGENT" if is_agent else None,
                is_agent_summary=is_agent,
            )
            flat_leaves.append(node)
            group = self._classify_doc_folder(doc_id, merged_meta, is_agent)
            leaves_by_group.setdefault(group, []).append(node)

        if len(flat_leaves) <= 16 or len(leaves_by_group) <= 1:
            return flat_leaves

        group_order = [
            "Syntheses & Progress",
            "Lectures & Course Material",
            "reMarkable Notes",
            "Message Threads",
            "Archive Documents",
        ]
        folders: list[TreeNode] = []
        for gname in group_order:
            kids = leaves_by_group.get(gname)
            if not kids:
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", gname.lower()).strip("-")
            folders.append(
                TreeNode(
                    id=f"hs-docgroup:{bank_id}:{slug}",
                    name=gname,
                    kind="folder",
                    provider=self.provider_id,
                    badge=str(len(kids)),
                    children=kids,
                )
            )
        return folders

    def list_tree(self, scope: Optional[str] = None) -> list[TreeNode]:
        bank_id = _validate_bank_id(scope or self.default_bank)
        cache_key = f"hindsight:tree:{bank_id}"
        if self._primary_token():
            try:
                raw_tree = self._request_json(f"/v1/default/banks/{bank_id}/knowledge-base/tree", timeout=15.0)
                if isinstance(raw_tree, dict) and isinstance(raw_tree.get("roots"), list):
                    self.cache.set_kv(cache_key, raw_tree["roots"])
                    nodes = [self._convert_kb_node(n, bank_id) for n in raw_tree["roots"]]
                    src_node = self._recent_sources_folder(bank_id)
                    if src_node:
                        nodes.append(src_node)
                    if nodes:
                        return nodes
                    doc_nodes = self._list_bank_documents_nodes(bank_id)
                    if doc_nodes:
                        return doc_nodes
                    return []
            except Exception:
                pass

        cached_roots = self.cache.get_kv(cache_key)
        if isinstance(cached_roots, list) and cached_roots:
            return [self._convert_kb_node(n, bank_id) for n in cached_roots]

        doc_nodes = self._list_bank_documents_nodes(bank_id)
        if doc_nodes:
            return doc_nodes

        cached_docs = self.cache.list_documents(provider=self.provider_id, scope=bank_id)
        return [
            TreeNode(
                id=d.uri,
                name=d.title,
                kind="page",
                uri=d.uri,
                provider=self.provider_id,
                badge=d.external_id[:12],
                is_agent_summary=d.provenance.is_agent_summary,
            )
            for d in cached_docs
        ]

    def _convert_kb_node(self, raw: dict[str, Any], bank_id: str) -> TreeNode:
        kind = raw.get("kind", "page")
        node_id = str(raw.get("id") or "")
        name = str(raw.get("name") or node_id)
        if kind == "folder":
            children = [self._convert_kb_node(ch, bank_id) for ch in (raw.get("children") or [])]
            return TreeNode(
                id=f"hs-folder:{bank_id}:{node_id}",
                name=name,
                kind="folder",
                provider=self.provider_id,
                badge=str(len(children)) if children else None,
                children=children,
            )
        uri = f"hindsight://{bank_id}/page/{node_id}"
        return TreeNode(
            id=uri,
            name=name,
            kind="page",
            uri=uri,
            provider=self.provider_id,
            badge="KP",
            is_agent_summary=True,
        )

    def _recent_sources_folder(self, bank_id: str) -> Optional[TreeNode]:
        if bank_id != "thomas" or not self._gateway_token():
            return None
        try:
            res = self._request_json(
                "/api/sources?limit=15",
                token=self._gateway_token(),
                timeout=8.0,
            )
            items = res.get("items") if isinstance(res, dict) else []
            if not isinstance(items, list) or not items:
                return None
            children: list[TreeNode] = []
            for item in items:
                sid = str(item.get("source_id") or "")
                if not sid:
                    continue
                meta = item.get("metadata") or {}
                auteur = str(item.get("auteur") or "thomas")
                stype = str(item.get("type_de_source") or "source")
                is_agent = bool(meta.get("is_agent_summary")) or auteur.lower().startswith("agent") or stype == "agent_summary"
                doc_id = str(item.get("document_id") or sid)
                label = f"{stype}: {doc_id[:34]}"
                uri = f"hindsight://{bank_id}/source/{sid}"
                children.append(
                    TreeNode(
                        id=uri,
                        name=label,
                        kind="source",
                        uri=uri,
                        provider=self.provider_id,
                        badge="AGENT" if is_agent else "RAW",
                        is_agent_summary=is_agent,
                    )
                )
            if not children:
                return None
            return TreeNode(
                id=f"hs-sources:{bank_id}",
                name="Recent Raw Archive Sources",
                kind="folder",
                provider=self.provider_id,
                badge=str(len(children)),
                children=children,
            )
        except Exception:
            return None

    def get_document(self, uri_or_id: str, scope: Optional[str] = None) -> Optional[DocumentRecord]:
        bank_id = _validate_bank_id(scope or self.default_bank)
        kind = "page"
        item_id = uri_or_id.strip()

        if item_id.startswith("hindsight://"):
            rest = item_id[len("hindsight://") :]
            parts = [p for p in rest.split("/") if p]
            if len(parts) >= 3:
                bank_id = _validate_bank_id(parts[0])
                kind = parts[1]
                item_id = "/".join(parts[2:])
            elif len(parts) == 2:
                bank_id = _validate_bank_id(parts[0])
                item_id = parts[1]
        elif item_id.startswith("src-"):
            kind = "source"
        elif item_id.startswith("kp-"):
            kind = "page"
        else:
            kind = "document"

        canonical_uri = f"hindsight://{bank_id}/{kind}/{item_id}"
        if not self._primary_token():
            return self.cache.get_document(canonical_uri)

        try:
            if kind == "page":
                return self._fetch_knowledge_page(bank_id, item_id)
            if kind == "source":
                return self._fetch_raw_source(bank_id, item_id)
            return self._fetch_hindsight_document(bank_id, item_id)
        except Exception:
            return self.cache.get_document(canonical_uri)

    def _fetch_knowledge_page(self, bank_id: str, page_id: str) -> Optional[DocumentRecord]:
        safe_page = urllib.parse.quote(page_id, safe="")
        data = self._request_json(f"/v1/default/banks/{bank_id}/knowledge-base/pages/{safe_page}", timeout=18.0)
        if not isinstance(data, dict):
            return None
        raw_md = str(data.get("markdown") or data.get("body") or "")
        fm, body = parse_frontmatter(raw_md)
        if not body and data.get("body"):
            body = str(data["body"])
        title = str(data.get("name") or extract_title(fm, body, page_id))
        updated_at = str(data.get("timestamp") or fm.get("timestamp") or datetime.now(timezone.utc).isoformat())
        tags = list(dict.fromkeys([*(str(t) for t in (data.get("tags") or [])), *extract_tags(fm, body)]))
        src_refs, kp_refs = extract_hindsight_refs(raw_md)
        kp_refs = [k for k in kp_refs if k != page_id]
        wikilinks = extract_wikilinks(body)
        for ref in [*kp_refs, *src_refs]:
            if ref not in wikilinks:
                wikilinks.append(ref)

        # Knowledge pages are synthesized/maintained views grounded in cited `src-*` raw sources.
        prov = ProvenanceInfo(
            author="hindsight:knowledge-page",
            source_type="knowledge_page",
            is_agent_summary=True,
            source_id=None,
            document_id=page_id,
            bank_id=bank_id,
            original_date=updated_at,
            updated_at=updated_at,
            related_sources=src_refs,
            related_pages=kp_refs,
            tags=tags,
        )
        uri = f"hindsight://{bank_id}/page/{page_id}"
        doc = DocumentRecord(
            uri=uri,
            provider=self.provider_id,
            external_id=page_id,
            title=title,
            body=body,
            raw_markdown=raw_md,
            folder_path=f"{bank_id}/knowledge-pages",
            frontmatter={
                **fm,
                "id": page_id,
                "bank_id": bank_id,
                "description": data.get("description") or fm.get("description") or "",
            },
            tags=tags,
            wikilinks=wikilinks,
            provenance=prov,
            readonly=True,
            content_hash=sha256_text(raw_md),
            updated_at=updated_at,
        )
        self.cache.upsert_document(doc, scope=bank_id)
        return doc

    def _fetch_raw_source(self, bank_id: str, source_id: str) -> Optional[DocumentRecord]:
        safe_src = urllib.parse.quote(source_id, safe="")
        src_meta = self._request_json(
            f"/api/sources/{safe_src}",
            token=self._gateway_token(),
            timeout=15.0,
        )
        if not isinstance(src_meta, dict):
            return None
        doc_id = str(src_meta.get("document_id") or "")
        auteur = str(src_meta.get("auteur") or "thomas")
        stype = str(src_meta.get("type_de_source") or "source")
        date_orig = str(src_meta.get("date_originale") or src_meta.get("date_import") or "")
        meta_obj = src_meta.get("metadata") or {}
        tags = [str(t) for t in (src_meta.get("tags") or [])]
        is_agent = (
            bool(meta_obj.get("is_agent_summary"))
            or auteur.lower().startswith("agent")
            or stype == "agent_summary"
        )

        body_text = ""
        if doc_id:
            try:
                safe_doc = urllib.parse.quote(doc_id, safe="")
                hdoc = self._request_json(f"/v1/default/banks/{bank_id}/documents/{safe_doc}", timeout=15.0)
                if isinstance(hdoc, dict) and hdoc.get("original_text"):
                    _, body_text = strip_provenance_headers(str(hdoc["original_text"]))
            except Exception:
                pass

        if not body_text and stype not in ("remarkable_page", "voice"):
            try:
                src_full = self._request_json(
                    f"/api/sources/{safe_src}?include_raw=true",
                    token=self._gateway_token(),
                    timeout=15.0,
                )
                if isinstance(src_full, dict) and src_full.get("raw_content"):
                    body_text = str(src_full["raw_content"])
            except Exception:
                pass

        if not body_text:
            body_text = f"*(Binary or external raw archive `{source_id}` — document `{doc_id}`)*"

        src_refs, kp_refs = extract_hindsight_refs(body_text + "\n" + json.dumps(meta_obj, ensure_ascii=False))
        title = f"{meta_obj.get('doc_name') or stype} — {source_id}"
        uri = f"hindsight://{bank_id}/source/{source_id}"
        prov = ProvenanceInfo(
            author=auteur,
            source_type=stype,
            is_agent_summary=is_agent,
            source_id=source_id,
            document_id=doc_id or source_id,
            bank_id=bank_id,
            original_date=date_orig,
            updated_at=str(src_meta.get("date_import") or date_orig),
            related_sources=[s for s in src_refs if s != source_id],
            related_pages=kp_refs,
            tags=tags,
        )
        doc = DocumentRecord(
            uri=uri,
            provider=self.provider_id,
            external_id=source_id,
            title=title,
            body=body_text,
            raw_markdown=body_text,
            folder_path=f"{bank_id}/sources",
            frontmatter={
                "source_id": source_id,
                "document_id": doc_id,
                "author": auteur,
                "type_de_source": stype,
                "date_originale": date_orig,
                "is_agent_summary": is_agent,
            },
            tags=tags,
            wikilinks=[*kp_refs, *[s for s in src_refs if s != source_id]],
            provenance=prov,
            readonly=True,
            content_hash=sha256_text(body_text),
            updated_at=prov.updated_at or "",
        )
        self.cache.upsert_document(doc, scope=bank_id)
        return doc

    def _fetch_hindsight_document(self, bank_id: str, doc_id: str) -> Optional[DocumentRecord]:
        safe_doc = urllib.parse.quote(doc_id, safe="")
        hdoc = self._request_json(f"/v1/default/banks/{bank_id}/documents/{safe_doc}", timeout=15.0)
        if not isinstance(hdoc, dict):
            return None
        orig = str(hdoc.get("original_text") or "")
        prov_hdr, stripped_body = strip_provenance_headers(orig)
        fm, clean_body = parse_frontmatter(stripped_body)
        rp = hdoc.get("retain_params") or {}
        rp_meta = rp.get("metadata") if isinstance(rp, dict) else {}
        raw_doc_meta = hdoc.get("document_metadata") or {}
        doc_meta: dict[str, Any] = {
            **(rp_meta if isinstance(rp_meta, dict) else {}),
            **(raw_doc_meta if isinstance(raw_doc_meta, dict) else {}),
        }
        context_str = str(rp.get("context") or "") if isinstance(rp, dict) else ""
        fallback_label = self._format_hindsight_doc_label(doc_id, doc_meta, context_str)
        title = extract_title({**doc_meta, **fm}, clean_body, fallback_label)
        auteur = str(doc_meta.get("auteur") or fm.get("author") or "thomas")
        stype = str(doc_meta.get("type_de_source") or prov_hdr.get("type") or "document")
        source_id = str(doc_meta.get("source_id") or prov_hdr.get("source_id") or "") or None
        is_agent = (
            str(doc_meta.get("is_agent_summary", "")).lower() == "true"
            or auteur.lower().startswith("agent")
            or stype == "agent_summary"
        )
        tags = list(dict.fromkeys([*(str(t) for t in (hdoc.get("tags") or [])), *extract_tags(fm, clean_body)]))
        src_refs, kp_refs = extract_hindsight_refs(clean_body)
        uri = f"hindsight://{bank_id}/document/{doc_id}"
        event_date = str(rp.get("event_date") or "") if isinstance(rp, dict) else ""
        updated_at = str(hdoc.get("updated_at") or hdoc.get("created_at") or event_date or "")
        prov = ProvenanceInfo(
            author=auteur,
            source_type=stype,
            is_agent_summary=is_agent,
            source_id=source_id,
            document_id=doc_id,
            bank_id=bank_id,
            original_date=str(doc_meta.get("date_originale") or prov_hdr.get("date") or event_date or updated_at),
            updated_at=updated_at,
            related_sources=src_refs,
            related_pages=kp_refs,
            tags=tags,
        )
        doc = DocumentRecord(
            uri=uri,
            provider=self.provider_id,
            external_id=doc_id,
            title=title,
            body=clean_body,
            raw_markdown=clean_body,
            folder_path=f"{bank_id}/documents",
            frontmatter={**fm, "document_id": doc_id, "source_id": source_id, "author": auteur},
            tags=tags,
            wikilinks=[*kp_refs, *src_refs],
            provenance=prov,
            readonly=True,
            content_hash=sha256_text(clean_body),
            updated_at=updated_at,
        )
        self.cache.upsert_document(doc, scope=bank_id)
        return doc

    def search(self, query: str, scope: Optional[str] = None, limit: int = 12) -> list[dict[str, Any]]:
        bank_id = _validate_bank_id(scope or self.default_bank)
        lexical = self.cache.search_fts(query, provider=self.provider_id, scope=bank_id, limit=limit)
        if not query.strip() or not self._primary_token():
            return lexical

        merged: dict[str, dict[str, Any]] = {item["uri"]: item for item in lexical}

        # 1. Knowledge Page semantic/lexical search (`/knowledge-base/search`)
        try:
            q_enc = urllib.parse.urlencode({"q": query.strip(), "limit": min(limit, 8)})
            kb_res = self._request_json(
                f"/v1/default/banks/{bank_id}/knowledge-base/search?{q_enc}",
                timeout=12.0,
            )
            for item in kb_res.get("results") or []:
                pid = str(item.get("id") or "")
                if not pid:
                    continue
                uri = f"hindsight://{bank_id}/page/{pid}"
                merged[uri] = {
                    "uri": uri,
                    "provider": self.provider_id,
                    "external_id": pid,
                    "scope": bank_id,
                    "title": str(item.get("name") or pid),
                    "folder_path": f"{bank_id}/knowledge-pages",
                    "snippet": str(item.get("snippet") or "")[:240],
                    "tags": [],
                    "is_agent_summary": True,
                    "author": "hindsight:knowledge-page",
                    "provenance": {
                        "author": "hindsight:knowledge-page",
                        "source_type": "knowledge_page",
                        "is_agent_summary": True,
                        "document_id": pid,
                        "bank_id": bank_id,
                        "updated_at": item.get("updated_at"),
                    },
                    "updated_at": str(item.get("updated_at") or ""),
                    "match_kind": "hindsight-kb",
                    "score": float(item.get("score") or 0.85),
                }
        except Exception:
            pass

        # 2. Hindsight semantic memory recall (`/memories/recall`)
        try:
            rec_res = self._request_json(
                f"/v1/default/banks/{bank_id}/memories/recall",
                method="POST",
                payload={
                    "query": query.strip(),
                    "budget": "low",
                    "max_tokens": 1536,
                    "include_chunks": False,
                },
                timeout=16.0,
            )
            for idx, fact in enumerate((rec_res.get("results") or [])[:8]):
                doc_id = str(fact.get("document_id") or "")
                meta = fact.get("metadata") or {}
                src_id = str(meta.get("source_id") or "")
                auteur = str(meta.get("auteur") or "thomas")
                stype = str(meta.get("type_de_source") or fact.get("type") or "memory")
                is_agent = (
                    str(meta.get("is_agent_summary", "")).lower() == "true"
                    or auteur.lower().startswith("agent")
                    or stype == "agent_summary"
                )
                if src_id:
                    uri = f"hindsight://{bank_id}/source/{src_id}"
                    ext_id = src_id
                elif doc_id:
                    uri = f"hindsight://{bank_id}/document/{doc_id}"
                    ext_id = doc_id
                else:
                    fid = str(fact.get("id") or f"fact-{idx}")
                    uri = f"hindsight://{bank_id}/document/{fid}"
                    ext_id = fid

                if uri not in merged:
                    merged[uri] = {
                        "uri": uri,
                        "provider": self.provider_id,
                        "external_id": ext_id,
                        "scope": bank_id,
                        "title": f"{stype}: {doc_id or ext_id}",
                        "folder_path": f"{bank_id}/memories",
                        "snippet": str(fact.get("text") or "")[:240],
                        "tags": [str(t) for t in (fact.get("tags") or [])],
                        "is_agent_summary": is_agent,
                        "author": auteur,
                        "provenance": {
                            "author": auteur,
                            "source_type": stype,
                            "is_agent_summary": is_agent,
                            "source_id": src_id or None,
                            "document_id": doc_id or ext_id,
                            "bank_id": bank_id,
                            "original_date": meta.get("date_originale") or fact.get("mentioned_at"),
                        },
                        "updated_at": str(fact.get("mentioned_at") or ""),
                        "match_kind": "semantic-recall",
                        "score": max(0.3, 0.9 - idx * 0.05),
                    }
        except Exception:
            pass

        ranked = sorted(merged.values(), key=lambda x: float(x.get("score") or 0.0), reverse=True)
        return ranked[:limit]

    def save_source_explicit(
        self,
        *,
        content: str,
        bank_id: Optional[str] = None,
        document_id: Optional[str] = None,
        author: str = "thomas",
        source_type: str = "paste",
        tags: Optional[list[str]] = None,
        is_agent_summary: bool = False,
        profile: Optional[str] = None,
    ) -> dict[str, Any]:
        if self.strict_readonly:
            raise PermissionError(
                "Hindsight read-only safety guard is enabled (FOLIO_HINDSIGHT_READONLY=1); save_source refused."
            )
        if not content.strip():
            raise ValueError("Cannot retain empty content")
        target_bank = _validate_bank_id(bank_id or self.default_bank)
        arguments: dict[str, Any] = {
            "content": content,
            "auteur": author.strip() or "thomas",
            "type_de_source": source_type.strip() or "paste",
            "bank_id": target_bank,
            "tags": tags or [],
            "is_agent_summary": bool(is_agent_summary),
        }
        if document_id and document_id.strip():
            arguments["document_id"] = document_id.strip()
        if profile in ("quality", "local-only"):
            arguments["profile"] = profile

        rpc_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "save_source",
                "arguments": arguments,
            },
        }
        resp = self._request_json(
            f"/mcp/{target_bank}/",
            method="POST",
            payload=rpc_payload,
            token=self._gateway_token(),
            timeout=45.0,
        )
        if isinstance(resp, dict) and "result" in resp:
            blocks = resp["result"].get("content") or []
            if blocks and isinstance(blocks[0], dict) and blocks[0].get("text"):
                try:
                    return json.loads(blocks[0]["text"])
                except Exception:
                    return {"raw": blocks[0]["text"]}
        return resp if isinstance(resp, dict) else {"result": resp}
