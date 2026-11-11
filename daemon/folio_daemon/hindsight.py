import concurrent.futures
import hashlib
import json
import os
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional

from .cache import CacheStore
from .http_pool import SHARED_HTTP_POOL
from .local_fs import is_note_private_for_sync
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
USER_AGENT = "folio-desktop/0.2.0 (+https://github.com/Th0rgal/folio)"


def _validate_bank_id(bank_id: str) -> str:
    cleaned = (bank_id or "thomas").strip()
    if not BANK_ID_RE.match(cleaned):
        raise ValueError(f"Invalid Hindsight bank_id: {bank_id!r}")
    return cleaned


class HindsightProvider:
    """First-class Hindsight & Spark-Memory adapter with dedicated `folio` sync bank support.
    - Reads Knowledge Pages (`kp-*`), Knowledge Tree (`kf-*`), Memory Banks (`thomas`, `folio`, `ppl`, ...),
      raw archive sources (`src-*`), and Hindsight documents strictly READ-ONLY during browsing/search.
    - Syncs working local Markdown notes cleanly to a dedicated Hindsight bank (`folio` by default)
      with stable `document_id` + SHA-256 sync ledger so drafts stay semantic-searchable without
      polluting curated `thomas` memory, while supporting explicit raw-first promotion to `thomas`
      via `save_source`.
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
        stored_sync_bank = self.cache.get_kv("hindsight:sync_bank")
        self.sync_bank = _validate_bank_id(
            os.environ.get("FOLIO_SYNC_BANK")
            or (str(stored_sync_bank) if stored_sync_bank else "")
            or "folio"
        )
        self.strict_readonly = os.environ.get("FOLIO_HINDSIGHT_READONLY", "0") == "1"
        self._banks_cache: Optional[list[dict[str, Any]]] = None
        self._initialized_banks: set[str] = set()

    def set_sync_bank(self, bank_id: str) -> str:
        cleaned = _validate_bank_id(bank_id)
        self.sync_bank = cleaned
        self.cache.set_kv("hindsight:sync_bank", cleaned)
        self._banks_cache = None
        return cleaned

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
        return SHARED_HTTP_POOL.request_json(
            url,
            method=method,
            headers=headers,
            payload=payload,
            timeout=timeout,
        )

    def _mcp_call(
        self,
        bank_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        token: Optional[str] = None,
        timeout: float = 40.0,
    ) -> dict[str, Any]:
        target_bank = _validate_bank_id(bank_id)
        rpc_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }
        resp = self._request_json(
            f"/mcp/{target_bank}/",
            method="POST",
            payload=rpc_payload,
            token=token or self._primary_token(),
            timeout=timeout,
        )
        if isinstance(resp, dict) and "error" in resp and resp["error"]:
            err_msg = resp["error"].get("message") if isinstance(resp["error"], dict) else str(resp["error"])
            raise RuntimeError(f"Hindsight MCP {tool_name} failed: {err_msg}")
        if isinstance(resp, dict) and "result" in resp:
            res_obj = resp["result"]
            if isinstance(res_obj, dict) and res_obj.get("isError"):
                blocks = res_obj.get("content") or []
                txt = blocks[0].get("text") if blocks and isinstance(blocks[0], dict) else str(res_obj)
                raise RuntimeError(f"Hindsight {tool_name}: {txt}")
            blocks = res_obj.get("content") if isinstance(res_obj, dict) else []
            if blocks and isinstance(blocks[0], dict) and blocks[0].get("text"):
                txt = str(blocks[0]["text"])
                try:
                    parsed = json.loads(txt)
                    return parsed if isinstance(parsed, dict) else {"result": parsed}
                except Exception:
                    return {"raw": txt}
        return resp if isinstance(resp, dict) else {"result": resp}

    def status(self, local_docs: Optional[list[DocumentRecord]] = None) -> dict[str, Any]:
        has_hs = self.secrets.has("hindsight_api_key")
        has_sm = self.secrets.has("spark_memory_api_token")
        cached_docs = self.cache.list_documents(provider=self.provider_id)
        connected = has_hs or has_sm
        sync_summary = self.summarize_sync(local_docs) if local_docs is not None else {}
        return {
            "provider": self.provider_id,
            "provider_id": self.provider_id,
            "configured": connected,
            "connected": connected,
            "base_url": self.base_url,
            "api_url": self.base_url,
            "default_bank": self.default_bank,
            "sync_bank": self.sync_bank,
            "readonly_guard": self.strict_readonly,
            "readonly": self.strict_readonly,
            "auth_sources": {
                "hindsight_api_key": self.secrets.source_kind("hindsight_api_key"),
                "spark_memory_api_token": self.secrets.source_kind("spark_memory_api_token"),
            },
            "cached_count": len(cached_docs),
            "sync_summary": sync_summary,
        }

    def compute_note_sync_info(self, doc: DocumentRecord) -> dict[str, Any]:
        if doc.provider != "local-fs":
            return {"state": "remote", "target_bank": doc.provenance.bank_id}
        if is_note_private_for_sync(doc):
            return {
                "state": "private",
                "target_bank": self.sync_bank,
                "reason": "Tagged #private / #nosync (local-only)",
            }
        row = self.cache.get_sync_state(doc.uri)
        if not row:
            return {
                "state": "unsynced",
                "target_bank": self.sync_bank,
                "document_id": self._stable_folio_doc_id(doc),
            }
        is_current = bool(row.get("synced_hash")) and row.get("synced_hash") == doc.content_hash
        return {
            "state": "synced" if is_current else "modified",
            "target_bank": row.get("target_bank") or self.sync_bank,
            "document_id": row.get("document_id") or self._stable_folio_doc_id(doc),
            "source_id": row.get("source_id"),
            "synced_at": row.get("synced_at"),
            "last_error": row.get("last_error"),
        }

    def summarize_sync(self, local_docs: Optional[list[DocumentRecord]] = None) -> dict[str, Any]:
        docs = local_docs if local_docs is not None else self.cache.list_documents(provider="local-fs")
        states = self.cache.list_sync_states()
        synced = 0
        modified = 0
        unsynced = 0
        private_cnt = 0
        for d in docs:
            if is_note_private_for_sync(d):
                private_cnt += 1
                continue
            st = states.get(d.uri)
            if not st:
                unsynced += 1
            elif st.get("synced_hash") == d.content_hash:
                synced += 1
            else:
                modified += 1
        return {
            "sync_bank": self.sync_bank,
            "synced": synced,
            "modified": modified,
            "unsynced": unsynced,
            "private": private_cnt,
            "pending_sync": modified + unsynced,
        }

    @staticmethod
    def _stable_folio_doc_id(doc: DocumentRecord) -> str:
        fm = doc.frontmatter or {}
        for key in ("hindsight_doc_id", "document_id", "id"):
            val = str(fm.get(key) or "").strip()
            if val and BANK_ID_RE.match(val):
                return val
        digest = hashlib.sha1(doc.uri.encode("utf-8")).hexdigest()[:14]
        slug = re.sub(r"[^a-z0-9]+", "-", doc.title.lower()).strip("-")[:32] or "note"
        return f"folio-{slug}-{digest}"

    def _ensure_dedicated_bank_initialized(self, bank_id: str) -> None:
        if bank_id in ("thomas", "ppl") or bank_id in self._initialized_banks:
            return
        init_kv = f"hindsight:bank_init:{bank_id}"
        if self.cache.get_kv(init_kv):
            self._initialized_banks.add(bank_id)
            return
        try:
            label = "Folio Notes" if bank_id == "folio" else f"Folio · {bank_id}"
            self._mcp_call(
                bank_id,
                "update_bank",
                {
                    "name": label,
                    "mission": (
                        "Working Markdown research notes, mathematical derivations, "
                        "and structured drafts synced from Folio desktop."
                    ),
                },
                timeout=15.0,
            )
            self.cache.set_kv(init_kv, True)
            self._initialized_banks.add(bank_id)
            self._banks_cache = None
        except Exception:
            pass

    def sync_local_note(
        self,
        doc: DocumentRecord,
        *,
        bank_id: Optional[str] = None,
        promote_raw_source: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        if self.strict_readonly:
            raise PermissionError(
                "Hindsight read-only safety guard is enabled (FOLIO_HINDSIGHT_READONLY=1); sync refused."
            )
        if doc.provider != "local-fs":
            raise ValueError("Only local Markdown notes can be synced to Hindsight")
        if is_note_private_for_sync(doc) and not force:
            raise PermissionError(
                "Note is marked #private / #nosync (sync: false); refusing remote Hindsight sync."
            )
        if not doc.raw_markdown.strip():
            raise ValueError("Cannot sync an empty note")

        existing = self.cache.get_sync_state(doc.uri)
        target_bank = _validate_bank_id(
            "thomas" if promote_raw_source else (bank_id or (existing or {}).get("target_bank") or self.sync_bank)
        )
        doc_id = str((existing or {}).get("document_id") or self._stable_folio_doc_id(doc))
        now_iso = datetime.now(timezone.utc).isoformat()
        tags = list(dict.fromkeys([*doc.tags, "source:folio"]))

        if promote_raw_source or target_bank == "thomas":
            res = self.save_source_explicit(
                content=doc.raw_markdown,
                bank_id="thomas",
                document_id=doc_id,
                author=str(doc.provenance.author or "thomas"),
                source_type="journal",
                tags=tags,
                is_agent_summary=bool(doc.provenance.is_agent_summary),
                date_originale=doc.provenance.original_date or doc.updated_at or now_iso,
            )
            src_id = str(res.get("source_id") or (existing or {}).get("source_id") or "") or None
            returned_doc_id = str(res.get("document_id") or doc_id)
            st = self.cache.upsert_sync_state(
                uri=doc.uri,
                target_bank="thomas" if target_bank == "thomas" and not existing else (existing or {}).get("target_bank", "thomas"),
                document_id=returned_doc_id,
                source_id=src_id,
                synced_hash=doc.content_hash,
                synced_at=now_iso,
            )
            self.cache.set_kv("hindsight:docs_tree:thomas", None)
            return {"status": "promoted", "bank_id": "thomas", "sync": st, "remote": res}

        self._ensure_dedicated_bank_initialized(target_bank)
        metadata = {
            "title": doc.title,
            "doc_name": doc.title,
            "auteur": str(doc.provenance.author or "thomas"),
            "type_de_source": "folio_note",
            "folio_uri": doc.uri,
            "folder_path": doc.folder_path or "",
            "content_hash": doc.content_hash,
            "is_agent_summary": "true" if doc.provenance.is_agent_summary else "false",
        }
        res = self._mcp_call(
            target_bank,
            "sync_retain",
            {
                "content": doc.raw_markdown,
                "context": f"Folio note ({doc.folder_path or 'root'}): {doc.title}",
                "timestamp": doc.updated_at or now_iso,
                "tags": tags,
                "metadata": metadata,
                "document_id": doc_id,
            },
            timeout=45.0,
        )
        st = self.cache.upsert_sync_state(
            uri=doc.uri,
            target_bank=target_bank,
            document_id=doc_id,
            source_id=(existing or {}).get("source_id"),
            synced_hash=doc.content_hash,
            synced_at=now_iso,
        )
        # Invalidate cached bank document tree so sidebar reflects synced note immediately
        self.cache.set_kv(f"hindsight:docs_tree:{target_bank}", None)
        return {"status": "synced", "bank_id": target_bank, "sync": st, "remote": res}

    def sync_batch_notes(
        self,
        docs: list[DocumentRecord],
        *,
        bank_id: Optional[str] = None,
        include_unsynced: bool = True,
    ) -> dict[str, Any]:
        if self.strict_readonly:
            raise PermissionError("Hindsight read-only guard is enabled")
        target_bank = _validate_bank_id(bank_id or self.sync_bank)
        states = self.cache.list_sync_states()
        candidates: list[DocumentRecord] = []
        for d in docs:
            if d.provider != "local-fs" or is_note_private_for_sync(d) or not d.raw_markdown.strip():
                continue
            st = states.get(d.uri)
            if st is None:
                if include_unsynced:
                    candidates.append(d)
            elif st.get("synced_hash") != d.content_hash or st.get("target_bank") != target_bank:
                candidates.append(d)

        synced_uris: list[str] = []
        errors: list[dict[str, str]] = []
        if candidates:
            self._ensure_dedicated_bank_initialized(target_bank)
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                fut_map = {
                    pool.submit(self.sync_local_note, d, bank_id=target_bank): d for d in candidates
                }
                for fut in concurrent.futures.as_completed(fut_map):
                    d = fut_map[fut]
                    try:
                        fut.result()
                        synced_uris.append(d.uri)
                    except Exception as exc:
                        errors.append({"uri": d.uri, "title": d.title, "error": str(exc)})
        return {
            "bank_id": target_bank,
            "synced_count": len(synced_uris),
            "synced_uris": synced_uris,
            "errors": errors,
            "summary": self.summarize_sync(docs),
        }

    def _sort_banks(self, banks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def sort_key(b: dict[str, Any]) -> tuple[int, str]:
            bid = str(b.get("bank_id") or "")
            if bid == self.default_bank:
                return (0, bid.lower())
            if bid == self.sync_bank and not self.strict_readonly:
                return (0, f"~{bid.lower()}")
            if bid.lower() == "spark-memory-bank":
                return (2, bid.lower())
            return (1, bid.lower())

        return sorted(banks, key=sort_key)

    def list_banks(self, include_sync_bank: bool = False) -> list[dict[str, Any]]:
        base_banks: Optional[list[dict[str, Any]]] = self._banks_cache
        if base_banks is None:
            fallback = [
                {"bank_id": "thomas", "name": "Thomas Personal Memory"},
                {"bank_id": "ppl", "name": "Aviation (PPL)"},
            ]
            if self._primary_token():
                try:
                    res = self._request_json("/v1/default/banks", timeout=10.0)
                    banks = res.get("banks") if isinstance(res, dict) else []
                    if isinstance(banks, list) and banks:
                        base_banks = [
                            {
                                "bank_id": b.get("bank_id", ""),
                                "name": b.get("name") or b.get("bank_id", ""),
                            }
                            for b in banks
                            if b.get("bank_id")
                        ]
                        self._banks_cache = base_banks
                except Exception:
                    pass
            if base_banks is None:
                base_banks = fallback

        merged = list(base_banks)
        if include_sync_bank and not self.strict_readonly:
            existing_ids = {str(b.get("bank_id")) for b in merged}
            if self.sync_bank not in existing_ids:
                merged.append(
                    {
                        "bank_id": self.sync_bank,
                        "name": "Folio Notes (Dedicated Sync Bank)",
                    }
                )
        return self._sort_banks(merged)

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

        cleaned = re.sub(r"^(gdrive-|folio-)", "", doc_id)
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
        if stype == "folio_note" or doc_id.startswith("folio-"):
            folder = str(meta.get("folder_path") or "").strip()
            return f"Folio · {folder}" if folder else "Folio Synced Notes"
        if is_agent or "progress" in stype or "summary" in stype:
            return "Syntheses & Progress"
        if "audio" in stype or "lecture" in stype or "gdrive-" in doc_id:
            return "Lectures & Course Material"
        if "remarkable" in stype or doc_id.startswith("rm-"):
            return "reMarkable Notes"
        if doc_id.startswith("thread_"):
            return "Message Threads"
        return "Archive Documents"

    def _list_bank_documents_nodes(self, bank_id: str, limit: int = 64) -> list[TreeNode]:
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
                badge="AGENT" if is_agent else ("FOLIO" if stype == "folio_note" else None),
                is_agent_summary=is_agent,
            )
            flat_leaves.append(node)
            group = self._classify_doc_folder(doc_id, merged_meta, is_agent)
            leaves_by_group.setdefault(group, []).append(node)

        if len(flat_leaves) <= 16 or len(leaves_by_group) <= 1:
            return flat_leaves

        ordered_keys = sorted(
            leaves_by_group.keys(),
            key=lambda g: (
                0 if g.startswith("Folio") else 1,
                [
                    "Syntheses & Progress",
                    "Lectures & Course Material",
                    "reMarkable Notes",
                    "Message Threads",
                    "Archive Documents",
                ].index(g)
                if g
                in (
                    "Syntheses & Progress",
                    "Lectures & Course Material",
                    "reMarkable Notes",
                    "Message Threads",
                    "Archive Documents",
                )
                else 9,
                g,
            ),
        )
        folders: list[TreeNode] = []
        for gname in ordered_keys:
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
                        if bank_id == self.sync_bank:
                            doc_nodes = self._list_bank_documents_nodes(bank_id)
                            if doc_nodes:
                                nodes.extend(doc_nodes)
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
        raw_full = orig if orig.lstrip().startswith("---") else clean_body
        doc = DocumentRecord(
            uri=uri,
            provider=self.provider_id,
            external_id=doc_id,
            title=title,
            body=clean_body,
            raw_markdown=raw_full,
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

    def _search_single_bank_remote(self, query: str, bank_id: str, limit: int) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}

        def _kb_task() -> list[dict[str, Any]]:
            q_enc = urllib.parse.urlencode({"q": query.strip(), "limit": min(limit, 8)})
            kb_res = self._request_json(
                f"/v1/default/banks/{bank_id}/knowledge-base/search?{q_enc}",
                timeout=8.0,
            )
            return list(kb_res.get("results") or []) if isinstance(kb_res, dict) else []

        def _recall_task() -> list[dict[str, Any]]:
            rec_res = self._request_json(
                f"/v1/default/banks/{bank_id}/memories/recall",
                method="POST",
                payload={
                    "query": query.strip(),
                    "budget": "low",
                    "max_tokens": 1536,
                    "include_chunks": False,
                },
                timeout=10.0,
            )
            return list(rec_res.get("results") or []) if isinstance(rec_res, dict) else []

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f_kb = pool.submit(_kb_task)
            f_rec = pool.submit(_recall_task)
            try:
                for item in f_kb.result():
                    pid = str(item.get("id") or "")
                    if not pid:
                        continue
                    uri = f"hindsight://{bank_id}/page/{pid}"
                    out[uri] = {
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

            try:
                for idx, fact in enumerate(f_rec.result()[:8]):
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

                    title_label = str(meta.get("title") or meta.get("doc_name") or "").strip()
                    if not title_label:
                        title_label = f"{stype}: {doc_id or ext_id}"

                    if uri not in out:
                        out[uri] = {
                            "uri": uri,
                            "provider": self.provider_id,
                            "external_id": ext_id,
                            "scope": bank_id,
                            "title": title_label,
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
        return out

    def search(
        self,
        query: str,
        scope: Optional[str] = None,
        limit: int = 12,
        *,
        local_only: bool = False,
        banks: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        target_banks = (
            [_validate_bank_id(b) for b in banks if b]
            if banks
            else [_validate_bank_id(scope or self.default_bank)]
        )
        lexical: list[dict[str, Any]] = []
        for b in target_banks:
            lexical.extend(self.cache.search_fts(query, provider=self.provider_id, scope=b, limit=limit))

        if local_only or not query.strip() or not self._primary_token():
            return lexical[:limit]

        merged: dict[str, dict[str, Any]] = {item["uri"]: item for item in lexical}

        if len(target_banks) == 1:
            merged.update(self._search_single_bank_remote(query, target_banks[0], limit))
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(target_banks), 4)) as pool:
                futs = [pool.submit(self._search_single_bank_remote, query, b, limit) for b in target_banks]
                for f in concurrent.futures.as_completed(futs):
                    try:
                        for uri, hit in f.result().items():
                            if uri not in merged or float(hit.get("score") or 0) > float(merged[uri].get("score") or 0):
                                merged[uri] = hit
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
        date_originale: Optional[str] = None,
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
            "tags": tags or [],
            "is_agent_summary": bool(is_agent_summary),
        }
        if document_id and document_id.strip():
            arguments["document_id"] = document_id.strip()
        if date_originale and date_originale.strip():
            arguments["date_originale"] = date_originale.strip()
        if profile in ("quality", "local-only"):
            arguments["profile"] = profile

        return self._mcp_call(
            target_bank,
            "save_source",
            arguments,
            token=self._gateway_token(),
            timeout=45.0,
        )
