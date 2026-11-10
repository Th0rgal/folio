import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from .models import DocumentRecord, ProvenanceInfo


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS documents (
    uri TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    folder_path TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    raw_markdown TEXT NOT NULL,
    frontmatter_json TEXT NOT NULL DEFAULT '{}',
    tags_json TEXT NOT NULL DEFAULT '[]',
    wikilinks_json TEXT NOT NULL DEFAULT '[]',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    readonly INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_docs_provider_scope ON documents(provider, scope);
CREATE INDEX IF NOT EXISTS idx_docs_external_id ON documents(external_id);

CREATE TABLE IF NOT EXISTS wikilinks (
    source_uri TEXT NOT NULL,
    target_slug TEXT NOT NULL,
    target_raw TEXT NOT NULL,
    PRIMARY KEY (source_uri, target_raw)
);

CREATE INDEX IF NOT EXISTS idx_wikilinks_target_slug ON wikilinks(target_slug);

CREATE TABLE IF NOT EXISTS workspace_mounts (
    mount_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    root_path TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    uri UNINDEXED,
    provider UNINDEXED,
    scope UNINDEXED,
    title,
    tags,
    body,
    tokenize='unicode61 remove_diacritics 2'
);
"""


def normalize_link_slug(raw: str) -> str:
    cleaned = raw.strip().replace("\\", "/")
    if cleaned.lower().endswith(".md"):
        cleaned = cleaned[:-3]
    tail = cleaned.split("/")[-1].strip().lower()
    return re.sub(r"[\s_]+", "-", tail)


class CacheStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_kv(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value_json FROM kv_state WHERE key = ?", (key,)).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value_json"])
            except Exception:
                return default

    def set_kv(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv_state(key, value_json) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                (key, payload),
            )
            self._conn.commit()

    def list_mounts(self) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT mount_id, label, root_path, added_at FROM workspace_mounts ORDER BY added_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def add_mount(self, mount_id: str, label: str, root_path: str, added_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO workspace_mounts(mount_id, label, root_path, added_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(root_path) DO UPDATE SET label = excluded.label",
                (mount_id, label, root_path, added_at),
            )
            self._conn.commit()

    def remove_mount(self, mount_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM workspace_mounts WHERE mount_id = ?", (mount_id,))
            prefix = f"local://{mount_id}/%"
            uris = [
                r["uri"]
                for r in self._conn.execute("SELECT uri FROM documents WHERE uri LIKE ?", (prefix,)).fetchall()
            ]
            for uri in uris:
                self._delete_uri_locked(uri)
            self._conn.commit()

    def upsert_document(self, doc: DocumentRecord, scope: str = "") -> bool:
        with self._lock:
            existing = self._conn.execute(
                "SELECT content_hash FROM documents WHERE uri = ?", (doc.uri,)
            ).fetchone()
            if existing and doc.content_hash and existing["content_hash"] == doc.content_hash:
                return False

            self._conn.execute(
                """
                INSERT INTO documents(
                    uri, provider, external_id, scope, title, folder_path, body, raw_markdown,
                    frontmatter_json, tags_json, wikilinks_json, provenance_json, readonly,
                    content_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(uri) DO UPDATE SET
                    provider = excluded.provider,
                    external_id = excluded.external_id,
                    scope = excluded.scope,
                    title = excluded.title,
                    folder_path = excluded.folder_path,
                    body = excluded.body,
                    raw_markdown = excluded.raw_markdown,
                    frontmatter_json = excluded.frontmatter_json,
                    tags_json = excluded.tags_json,
                    wikilinks_json = excluded.wikilinks_json,
                    provenance_json = excluded.provenance_json,
                    readonly = excluded.readonly,
                    content_hash = excluded.content_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    doc.uri,
                    doc.provider,
                    doc.external_id,
                    scope,
                    doc.title,
                    doc.folder_path,
                    doc.body,
                    doc.raw_markdown,
                    json.dumps(doc.frontmatter, ensure_ascii=False),
                    json.dumps(doc.tags, ensure_ascii=False),
                    json.dumps(doc.wikilinks, ensure_ascii=False),
                    json.dumps(doc.provenance.to_dict(), ensure_ascii=False),
                    1 if doc.readonly else 0,
                    doc.content_hash,
                    doc.updated_at,
                ),
            )
            self._conn.execute("DELETE FROM documents_fts WHERE uri = ?", (doc.uri,))
            self._conn.execute(
                "INSERT INTO documents_fts(uri, provider, scope, title, tags, body) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    doc.uri,
                    doc.provider,
                    scope,
                    doc.title,
                    " ".join(doc.tags),
                    doc.body,
                ),
            )
            self._conn.execute("DELETE FROM wikilinks WHERE source_uri = ?", (doc.uri,))
            for target in doc.wikilinks:
                slug = normalize_link_slug(target)
                if slug:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO wikilinks(source_uri, target_slug, target_raw) VALUES (?, ?, ?)",
                        (doc.uri, slug, target),
                    )
            self._conn.commit()
            return True

    def delete_document(self, uri: str) -> None:
        with self._lock:
            self._delete_uri_locked(uri)
            self._conn.commit()

    def _delete_uri_locked(self, uri: str) -> None:
        self._conn.execute("DELETE FROM documents WHERE uri = ?", (uri,))
        self._conn.execute("DELETE FROM documents_fts WHERE uri = ?", (uri,))
        self._conn.execute("DELETE FROM wikilinks WHERE source_uri = ?", (uri,))

    def get_document(self, uri: str) -> Optional[DocumentRecord]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM documents WHERE uri = ?", (uri,)).fetchone()
            return self._row_to_doc(row) if row else None

    def find_by_slug_or_id(self, target: str) -> Optional[DocumentRecord]:
        slug = normalize_link_slug(target)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM documents WHERE external_id = ? OR uri = ? LIMIT 1",
                (target.strip(), target.strip()),
            ).fetchone()
            if row:
                return self._row_to_doc(row)
            rows = self._conn.execute(
                "SELECT * FROM documents ORDER BY CASE provider WHEN 'local-fs' THEN 0 WHEN 'hindsight' THEN 1 ELSE 2 END, updated_at DESC"
            ).fetchall()
            for candidate in rows:
                cand_title_slug = normalize_link_slug(candidate["title"])
                cand_id_slug = normalize_link_slug(candidate["external_id"])
                if slug in (cand_title_slug, cand_id_slug):
                    return self._row_to_doc(candidate)
        return None

    def get_backlinks(self, doc: DocumentRecord) -> list[dict[str, Any]]:
        slugs = {
            normalize_link_slug(doc.title),
            normalize_link_slug(doc.external_id),
        }
        if doc.external_id.endswith(".md"):
            slugs.add(normalize_link_slug(doc.external_id.split("/")[-1]))
        slugs.discard("")
        if not slugs:
            return []
        placeholders = ",".join("?" for _ in slugs)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT DISTINCT d.uri, d.provider, d.title, d.external_id, d.provenance_json, w.target_raw
                FROM wikilinks w
                JOIN documents d ON d.uri = w.source_uri
                WHERE w.target_slug IN ({placeholders}) AND d.uri != ?
                ORDER BY d.updated_at DESC
                LIMIT 30
                """,
                (*slugs, doc.uri),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                prov = json.loads(r["provenance_json"] or "{}")
                out.append(
                    {
                        "uri": r["uri"],
                        "provider": r["provider"],
                        "title": r["title"],
                        "external_id": r["external_id"],
                        "target_raw": r["target_raw"],
                        "is_agent_summary": bool(prov.get("is_agent_summary")),
                    }
                )
            return out

    def list_documents(self, provider: Optional[str] = None, scope: Optional[str] = None) -> list[DocumentRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM documents {where} ORDER BY folder_path ASC, title ASC",
                params,
            ).fetchall()
            return [self._row_to_doc(r) for r in rows]

    def search_fts(
        self,
        query: str,
        provider: Optional[str] = None,
        scope: Optional[str] = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        tokens = [re.sub(r'[^\w-]', "", tok) for tok in query.strip().split()]
        tokens = [t for t in tokens if t]
        if not tokens:
            return self._recent_items(provider=provider, scope=scope, limit=limit)
        fts_expr = " AND ".join(f'"{t}"*' for t in tokens[:8])
        clauses = ["documents_fts MATCH ?"]
        params: list[Any] = [fts_expr]
        if provider:
            clauses.append("d.provider = ?")
            params.append(provider)
        if scope is not None:
            clauses.append("d.scope = ?")
            params.append(scope)
        params.append(limit)
        sql = f"""
            SELECT
                d.uri, d.provider, d.external_id, d.scope, d.title, d.folder_path,
                d.tags_json, d.provenance_json, d.updated_at,
                snippet(documents_fts, 5, '<mark>', '</mark>', ' … ', 22) AS snippet,
                bm25(documents_fts, 0.0, 0.0, 0.0, 6.0, 3.0, 1.0) AS rank
            FROM documents_fts
            JOIN documents d ON d.uri = documents_fts.uri
            WHERE {' AND '.join(clauses)}
            ORDER BY rank ASC
            LIMIT ?
        """
        with self._lock:
            try:
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return []
            results: list[dict[str, Any]] = []
            for r in rows:
                prov = json.loads(r["provenance_json"] or "{}")
                results.append(
                    {
                        "uri": r["uri"],
                        "provider": r["provider"],
                        "external_id": r["external_id"],
                        "scope": r["scope"],
                        "title": r["title"],
                        "folder_path": r["folder_path"],
                        "snippet": r["snippet"] or "",
                        "tags": json.loads(r["tags_json"] or "[]"),
                        "provenance": prov,
                        "updated_at": r["updated_at"],
                        "match_kind": "lexical-fts5",
                        "score": float(-r["rank"]) if r["rank"] is not None else 1.0,
                    }
                )
            return results

    def _recent_items(
        self,
        provider: Optional[str] = None,
        scope: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT uri, provider, external_id, scope, title, folder_path, body, tags_json, provenance_json, updated_at "
                f"FROM documents {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                prov = json.loads(r["provenance_json"] or "{}")
                snippet = (r["body"] or "").strip().replace("\n", " ")[:160]
                out.append(
                    {
                        "uri": r["uri"],
                        "provider": r["provider"],
                        "external_id": r["external_id"],
                        "scope": r["scope"],
                        "title": r["title"],
                        "folder_path": r["folder_path"],
                        "snippet": snippet,
                        "tags": json.loads(r["tags_json"] or "[]"),
                        "provenance": prov,
                        "updated_at": r["updated_at"],
                        "match_kind": "recent",
                        "score": 0.5,
                    }
                )
            return out

    @staticmethod
    def _row_to_doc(row: sqlite3.Row) -> DocumentRecord:
        prov_dict = json.loads(row["provenance_json"] or "{}")
        prov = ProvenanceInfo(
            author=prov_dict.get("author", "thomas"),
            source_type=prov_dict.get("source_type", "markdown"),
            is_agent_summary=bool(prov_dict.get("is_agent_summary", False)),
            source_id=prov_dict.get("source_id"),
            document_id=prov_dict.get("document_id"),
            bank_id=prov_dict.get("bank_id"),
            original_date=prov_dict.get("original_date"),
            updated_at=prov_dict.get("updated_at"),
            related_sources=list(prov_dict.get("related_sources") or []),
            related_pages=list(prov_dict.get("related_pages") or []),
            tags=list(prov_dict.get("tags") or []),
        )
        return DocumentRecord(
            uri=row["uri"],
            provider=row["provider"],
            external_id=row["external_id"],
            title=row["title"],
            body=row["body"],
            raw_markdown=row["raw_markdown"],
            folder_path=row["folder_path"],
            frontmatter=json.loads(row["frontmatter_json"] or "{}"),
            tags=json.loads(row["tags_json"] or "[]"),
            wikilinks=json.loads(row["wikilinks_json"] or "[]"),
            provenance=prov,
            readonly=bool(row["readonly"]),
            content_hash=row["content_hash"],
            updated_at=row["updated_at"],
        )
