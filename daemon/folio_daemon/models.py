from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass
class ProvenanceInfo:
    author: str = "thomas"
    source_type: str = "markdown"
    is_agent_summary: bool = False
    source_id: Optional[str] = None
    document_id: Optional[str] = None
    bank_id: Optional[str] = None
    original_date: Optional[str] = None
    updated_at: Optional[str] = None
    related_sources: list[str] = field(default_factory=list)
    related_pages: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "author": self.author,
            "source_type": self.source_type,
            "is_agent_summary": self.is_agent_summary,
            "source_id": self.source_id,
            "document_id": self.document_id,
            "bank_id": self.bank_id,
            "original_date": self.original_date,
            "updated_at": self.updated_at,
            "related_sources": list(self.related_sources),
            "related_pages": list(self.related_pages),
            "tags": list(self.tags),
        }


@dataclass
class DocumentRecord:
    uri: str
    provider: str
    external_id: str
    title: str
    body: str
    raw_markdown: str
    folder_path: str = ""
    frontmatter: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    wikilinks: list[str] = field(default_factory=list)
    provenance: ProvenanceInfo = field(default_factory=ProvenanceInfo)
    readonly: bool = False
    content_hash: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "provider": self.provider,
            "external_id": self.external_id,
            "title": self.title,
            "body": self.body,
            "raw_markdown": self.raw_markdown,
            "folder_path": self.folder_path,
            "frontmatter": self.frontmatter,
            "tags": list(self.tags),
            "wikilinks": list(self.wikilinks),
            "provenance": self.provenance.to_dict(),
            "readonly": self.readonly,
            "content_hash": self.content_hash,
            "updated_at": self.updated_at,
        }


@dataclass
class TreeNode:
    id: str
    name: str
    kind: str  # "folder" | "page" | "source"
    uri: Optional[str] = None
    provider: str = "local-fs"
    badge: Optional[str] = None
    is_agent_summary: bool = False
    children: list["TreeNode"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "uri": self.uri,
            "provider": self.provider,
            "badge": self.badge,
            "is_agent_summary": self.is_agent_summary,
            "children": [c.to_dict() for c in self.children],
        }


class SourceProvider(Protocol):
    provider_id: str

    def status(self) -> dict[str, Any]: ...
    def list_tree(self, scope: Optional[str] = None) -> list[TreeNode]: ...
    def get_document(self, uri_or_id: str, scope: Optional[str] = None) -> Optional[DocumentRecord]: ...
    def search(self, query: str, scope: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]: ...
