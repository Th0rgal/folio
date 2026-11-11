import hashlib
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .cache import CacheStore
from .fs_watch import NativeDirectoryWatcher
from .markdown_utils import (
    extract_hindsight_refs,
    extract_tags,
    extract_title,
    extract_wikilinks,
    parse_frontmatter,
    serialize_frontmatter,
    sha256_text,
)
from .models import DocumentRecord, ProvenanceInfo, TreeNode


IGNORED_DIRS: frozenset[str] = frozenset(
    {".git", ".direnv", ".lake", "node_modules", "dist", "target", "__pycache__", ".venv"}
)

PRIVATE_SYNC_TAGS: frozenset[str] = frozenset({"private", "nosync", "no-sync", "secret"})


def is_note_private_for_sync(doc: DocumentRecord) -> bool:
    fm = doc.frontmatter or {}
    if fm.get("sync") is False or str(fm.get("sync") or "").lower() in ("false", "no", "never", "off"):
        return True
    for t in doc.tags:
        bare = str(t).lstrip("#@").strip().lower()
        if bare in PRIVATE_SYNC_TAGS:
            return True
    return False


class LocalFsProvider:
    provider_id = "local-fs"

    def __init__(
        self,
        cache: CacheStore,
        default_dir: Path,
        on_change: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        self.cache = cache
        self.default_dir = default_dir
        self._on_change = on_change
        self._mtimes: dict[str, int] = {}
        self._watcher: Optional[NativeDirectoryWatcher] = None

        self._ensure_default_mount()
        self.scan_all()

    def _ensure_default_mount(self) -> None:
        mounts = self.cache.list_mounts()
        env_dirs = [p.strip() for p in os.environ.get("FOLIO_NOTES_DIRS", "").split(os.pathsep) if p.strip()]
        resolved_default = self.default_dir.expanduser().resolve()
        if len(mounts) == 1:
            only_root = Path(mounts[0]["root_path"]).resolve()
            if only_root == (resolved_default / "Folio").resolve() and resolved_default.name != "Folio":
                self.unmount_directory(mounts[0]["mount_id"])
                mounts = []
        if not mounts:
            self.default_dir.mkdir(parents=True, exist_ok=True)
            self._seed_starter_notes_if_empty(self.default_dir)
            self.mount_directory(self.default_dir, label=self.default_dir.name or "Notes")
        for extra in env_dirs:
            p = Path(extra).expanduser()
            if p.is_dir():
                self.mount_directory(p, label=p.name)

    def _seed_starter_notes_if_empty(self, root: Path) -> None:
        existing_md = list(root.rglob("*.md"))
        if existing_md:
            return
        (root / "Research").mkdir(parents=True, exist_ok=True)
        (root / "Notes").mkdir(parents=True, exist_ok=True)

        showcase = """---
title: "Bayesian Calibration & Spec-Driven Verification"
author: "thomas"
tags: [research, formal-specs, maxwell, latex]
updated: "2026-11-10"
---

# Bayesian Calibration & Spec-Driven Verification

Local-first working note linking [[Maxwell Optimization Loop]] and Hindsight methodology page [[kp-47c739bec662419d8d05f283de607b9e]].

> [!tip] Epistemic Invariant
> Separate **human-authored commitments** (`author: thomas`) from **agent syntheses** (`is_agent_summary: true`), and keep inline currency such as $50–$100 or escaped \\$100 strictly outside math delimiters while `env = "$HOME"` stays opaque.

## 1. Posterior Evidence Update & KL-Regularized Objective

Given prior belief $\\pi_0(\\theta)$ over specification candidates $\\theta \\in \\Theta$ and formal verifier likelihood $\\mathcal{L}(y \\mid \\theta)$, the tempered posterior satisfies:

$$
p_\\beta(\\theta \\mid y) = \\frac{\\pi_0(\\theta)\\,\\exp\\!\\bigl(-\\beta\\,\\mathcal{E}(\\theta; y)\\bigr)}{\\int_{\\Theta} \\pi_0(\\vartheta)\\,\\exp\\!\\bigl(-\\beta\\,\\mathcal{E}(\\vartheta; y)\\bigr)\\,\\mathrm{d}\\vartheta}, \\qquad \\mathrm{KL}\\bigl(q \\,\\|\\, p_\\beta\\bigr) \\le \\varepsilon_{\\text{gate}}
$$

```lean
theorem spec_gate_sound (s : SpecState) (h_inv : ValidInvariant s) :
    evalGate s = .accepted -> PreservesSafety s := by
  intro h_acc
  exact safety_of_accepted h_inv h_acc
```

For discrete shard budgets $B = \\sum_{i=1}^{K} b_i$ with inline bound $\\mathbb{P}(\\text{unsound}) \\le 2^{-128}$ and LaTeX bracketed display math:

\\[
\\mathcal{J}^*(b_1, \\dots, b_K) = \\max_{b \\in \\Delta_K(B)} \\sum_{k=1}^{K} w_k \\left(1 - \\prod_{t=1}^{b_k} \\bigl(1 - \\rho_{k,t}\\bigr)\\right) - \\lambda \\sum_{k=1}^{K} c_k b_k
\\]

## 2. Benchmark & Gate Summary

| Stage | Target | Latency | Soundness Gate | Cost Cap |
| :--- | :--- | ---: | :---: | ---: |
| Spec Layer 1 | Core quotient | `14 ms` | `0 sorryAx` | $12 |
| Spec Layer 2 | Two-sided CVE subspec | `48 ms` | Counterexample-checked | $25 |
| Native Runner | `lean-native` SIMD | `3.2 ms` | Bit-exact equivalence | $100 |

## 3. Verification Checklist

- [x] Mount local Markdown directories with instant SQLite FTS5 indexing
- [x] Cross-reference live Hindsight `thomas` Knowledge Pages (`kp-*`)
- [ ] Promote stabilized theorem checklist via raw-first `save_source`
"""
        maxwell_note = """---
title: "Maxwell Optimization Loop"
author: "thomas"
tags: [maxwell, optimization, turbo-lean]
updated: "2026-11-10"
---

# Maxwell Optimization Loop

Companion architecture note referenced by [[Bayesian Calibration & Spec-Driven Verification]] and grounded in Hindsight page [[kp-18069005e3c748649d5eeec34b2b343a]].

## Speedup Law under Amdahl + Speculative Pruning

With serial specification fraction $f_s \\in (0, 1)$ and $N$ parallel `boat` / `boost` shards:

$$
S(N) = \\frac{1}{f_s + \\dfrac{1 - f_s}{N} + \\kappa_{\\text{sync}} \\log_2 N}
$$

```mermaid
flowchart LR
  Spec["Formal Spec (0 sorryAx)"] --> Shard["Parallel Proof Shards"]
  Shard --> Check["Kernel Check + Diff Gate"]
  Check --> Retain["Hindsight Raw-First Archive"]
```
"""
        (root / "Research" / "bayesian-calibration-and-specs.md").write_text(showcase, encoding="utf-8")
        (root / "Notes" / "maxwell-optimization-loop.md").write_text(maxwell_note, encoding="utf-8")

    def _watch_roots(self) -> list[Path]:
        return [Path(m["root_path"]) for m in self.cache.list_mounts() if Path(m["root_path"]).exists()]

    def _on_fs_event(self) -> None:
        changed = self.scan_all()
        if changed and self._on_change:
            self._on_change({"provider": self.provider_id, "changed_uris": changed})

    def start_watcher(self, interval_sec: float = 2.0) -> None:
        if self._watcher is not None:
            return
        self._watcher = NativeDirectoryWatcher(
            self._watch_roots,
            self._on_fs_event,
            ignored_dirs=IGNORED_DIRS,
            recursive=True,
            fallback_interval_sec=interval_sec,
            name="folio-fs-watcher",
        )
        self._watcher.start()

    def stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def status(self) -> dict[str, Any]:
        mounts = self.cache.list_mounts()
        docs = self.cache.list_documents(provider=self.provider_id)
        return {
            "provider": self.provider_id,
            "provider_id": self.provider_id,
            "connected": True,
            "mounts": mounts,
            "document_count": len(docs),
            "folders": self.list_folders(),
        }

    def list_folders(self, mount_id: Optional[str] = None) -> list[str]:
        mounts = self.cache.list_mounts()
        target_mounts = [m for m in mounts if not mount_id or m["mount_id"] == mount_id]
        found: set[str] = set()
        for m in target_mounts:
            root = Path(m["root_path"])
            if not root.is_dir():
                continue
            for dirpath, dirnames, _ in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith("."))
                rel = Path(dirpath).relative_to(root).as_posix()
                if rel != ".":
                    found.add(rel)
        return sorted(found)

    def mount_directory(self, directory: Path, label: Optional[str] = None) -> dict[str, Any]:
        resolved = directory.expanduser().resolve()
        if not resolved.exists():
            resolved.mkdir(parents=True, exist_ok=True)
        if not resolved.is_dir():
            raise ValueError(f"Not a directory: {resolved}")
        mount_id = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:10]
        now_iso = datetime.now(timezone.utc).isoformat()
        mount_label = (label or resolved.name or "Workspace").strip()
        self.cache.add_mount(mount_id, mount_label, str(resolved), now_iso)
        self._scan_mount(mount_id, resolved)
        if self._watcher:
            self._watcher.notify_roots_changed()
        return {"mount_id": mount_id, "label": mount_label, "root_path": str(resolved)}

    def unmount_directory(self, mount_id: str) -> dict[str, Any]:
        self.cache.remove_mount(mount_id)
        stale_keys = [k for k in self._mtimes if k.startswith(f"local://{mount_id}/")]
        for k in stale_keys:
            self._mtimes.pop(k, None)
        if self._watcher:
            self._watcher.notify_roots_changed()
        return {"removed": mount_id}

    def resolve_filesystem_path(self, uri: str) -> Optional[Path]:
        if not uri.startswith("local://"):
            return None
        rest = uri[len("local://") :]
        mount_id, _, rel = rest.partition("/")
        if not mount_id or not rel:
            return None
        mount = next((m for m in self.cache.list_mounts() if m["mount_id"] == mount_id), None)
        if not mount:
            return None
        root = Path(mount["root_path"]).resolve()
        target = (root / rel).resolve()
        if not (target == root or str(target).startswith(str(root) + os.sep)):
            return None
        return target

    def open_filesystem_path(self, raw_path: str) -> Optional[DocumentRecord]:
        target = Path(raw_path).expanduser().resolve()
        if target.is_dir():
            self.mount_directory(target, label=target.name)
            return None
        if not target.is_file():
            return None
        for m in self.cache.list_mounts():
            root = Path(m["root_path"]).resolve()
            if str(target).startswith(str(root) + os.sep):
                rel = target.relative_to(root).as_posix()
                uri = f"local://{m['mount_id']}/{rel}"
                doc = self._read_markdown_file(m["mount_id"], root, target, target.stat().st_mtime)
                if doc:
                    self.cache.upsert_document(doc, scope=m["mount_id"])
                return doc
        # External .md file: mount its parent directory so wikilinks & saves work naturally
        mounted = self.mount_directory(target.parent, label=target.parent.name or "External")
        mid = mounted["mount_id"]
        root = Path(mounted["root_path"]).resolve()
        rel = target.relative_to(root).as_posix()
        doc = self._read_markdown_file(mid, root, target, target.stat().st_mtime)
        if doc:
            self.cache.upsert_document(doc, scope=mid)
        return doc

    def scan_all(self) -> list[str]:
        changed: list[str] = []
        for m in self.cache.list_mounts():
            root = Path(m["root_path"])
            if root.is_dir():
                changed.extend(self._scan_mount(m["mount_id"], root))
        return changed

    def _scan_mount(self, mount_id: str, root: Path) -> list[str]:
        changed: list[str] = []
        seen_uris: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith("."))
            for fname in sorted(filenames):
                if not fname.lower().endswith((".md", ".markdown")):
                    continue
                if fname.startswith("."):
                    continue
                fpath = Path(dirpath) / fname
                try:
                    st = fpath.stat()
                except OSError:
                    continue
                rel = fpath.relative_to(root).as_posix()
                uri = f"local://{mount_id}/{rel}"
                seen_uris.add(uri)
                mtime_ns = st.st_mtime_ns
                if self._mtimes.get(uri) == mtime_ns:
                    continue
                self._mtimes[uri] = mtime_ns
                doc = self._read_markdown_file(mount_id, root, fpath, st.st_mtime)
                if doc and self.cache.upsert_document(doc, scope=mount_id):
                    changed.append(uri)

        existing = self.cache.list_documents(provider=self.provider_id, scope=mount_id)
        for doc in existing:
            if doc.uri not in seen_uris:
                self.cache.delete_document(doc.uri)
                self._mtimes.pop(doc.uri, None)
                changed.append(doc.uri)
        return changed

    def _read_markdown_file(
        self, mount_id: str, root: Path, fpath: Path, mtime: float
    ) -> Optional[DocumentRecord]:
        try:
            raw = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        rel = fpath.relative_to(root).as_posix()
        uri = f"local://{mount_id}/{rel}"
        fm, body = parse_frontmatter(raw)
        fallback_title = fpath.stem.replace("-", " ").replace("_", " ").strip() or fpath.stem
        title = extract_title(fm, body, fallback_title)
        wikilinks = extract_wikilinks(body)
        tags = extract_tags(fm, body)
        src_refs, kp_refs = extract_hindsight_refs(raw)
        for kp in kp_refs:
            if kp not in wikilinks:
                wikilinks.append(kp)

        author = str(fm.get("author") or fm.get("auteur") or "thomas")
        is_agent = bool(fm.get("is_agent_summary", False)) or author.lower().startswith("agent:")
        updated_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        folder = str(Path(rel).parent.as_posix())
        if folder == ".":
            folder = ""

        prov = ProvenanceInfo(
            author=author,
            source_type=str(fm.get("type") or "local_markdown"),
            is_agent_summary=is_agent,
            source_id=str(fm["source_id"]) if "source_id" in fm else None,
            document_id=str(fm.get("id") or fm.get("document_id") or rel),
            original_date=str(fm["date"]) if "date" in fm else None,
            updated_at=updated_iso,
            related_sources=src_refs,
            related_pages=kp_refs,
            tags=tags,
        )
        return DocumentRecord(
            uri=uri,
            provider=self.provider_id,
            external_id=rel,
            title=title,
            body=body,
            raw_markdown=raw,
            folder_path=folder,
            frontmatter=fm,
            tags=tags,
            wikilinks=wikilinks,
            provenance=prov,
            readonly=False,
            content_hash=sha256_text(raw),
            updated_at=updated_iso,
        )

    @classmethod
    def _annotate_folder_counts(cls, node: TreeNode) -> int:
        if node.kind != "folder":
            return 1
        total = 0
        for ch in node.children:
            total += cls._annotate_folder_counts(ch)
        node.badge = str(total) if total > 0 else None
        return total

    def list_tree(self, scope: Optional[str] = None) -> list[TreeNode]:
        mounts = self.cache.list_mounts()
        sync_states = self.cache.list_sync_states()
        filtered_mounts = [m for m in mounts if not scope or m["mount_id"] == scope]
        roots: list[TreeNode] = []
        for m in filtered_mounts:
            mid = m["mount_id"]
            root_path = Path(m["root_path"])
            docs = self.cache.list_documents(provider=self.provider_id, scope=mid)
            mount_node = TreeNode(
                id=f"mount:{mid}",
                name=m["label"],
                kind="folder",
                provider=self.provider_id,
                badge=str(len(docs)),
            )
            folder_map: dict[str, TreeNode] = {"": mount_node}
            if root_path.is_dir():
                for dirpath, dirnames, _ in os.walk(root_path):
                    dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith("."))
                    rel_dir = Path(dirpath).relative_to(root_path).as_posix()
                    if rel_dir != ".":
                        self._ensure_folder_chain(folder_map, mount_node, mid, rel_dir)

            for doc in docs:
                parent = self._ensure_folder_chain(folder_map, mount_node, mid, doc.folder_path)
                st = sync_states.get(doc.uri)
                badge: Optional[str] = None
                if is_note_private_for_sync(doc):
                    badge = "LOCAL"
                elif st:
                    badge = "SYNC" if st.get("synced_hash") == doc.content_hash else "MOD"
                parent.children.append(
                    TreeNode(
                        id=doc.uri,
                        name=doc.title,
                        kind="page",
                        uri=doc.uri,
                        provider=self.provider_id,
                        badge=badge,
                        is_agent_summary=doc.provenance.is_agent_summary,
                    )
                )
            self._annotate_folder_counts(mount_node)
            if len(filtered_mounts) == 1:
                return mount_node.children
            roots.append(mount_node)
        return roots

    def _ensure_folder_chain(
        self,
        folder_map: dict[str, TreeNode],
        root_node: TreeNode,
        mount_id: str,
        folder_path: str,
    ) -> TreeNode:
        if not folder_path:
            return root_node
        parts = [p for p in folder_path.split("/") if p]
        accum = ""
        current = root_node
        for part in parts:
            accum = f"{accum}/{part}" if accum else part
            if accum not in folder_map:
                node = TreeNode(
                    id=f"folder:{mount_id}:{accum}",
                    name=part,
                    kind="folder",
                    provider=self.provider_id,
                )
                current.children.append(node)
                folder_map[accum] = node
            current = folder_map[accum]
        return current

    def get_document(self, uri_or_id: str, scope: Optional[str] = None) -> Optional[DocumentRecord]:
        if uri_or_id.startswith("local://"):
            doc = self.cache.get_document(uri_or_id)
            if doc:
                return doc
        return self.cache.find_by_slug_or_id(uri_or_id)

    @staticmethod
    def _sanitize_rel_folder(folder: str) -> str:
        parts = [p.strip() for p in folder.replace("\\", "/").split("/") if p.strip()]
        for p in parts:
            if p in (".", "..") or p.startswith("."):
                raise ValueError(f"Invalid folder segment: {p!r}")
        return "/".join(parts)

    @staticmethod
    def _slugify_title(title: str) -> str:
        slug = "".join(
            ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in title.strip().lower()
        )
        slug = re.sub(r"-{2,}", "-", slug).strip("-")
        return slug[:72] or f"note-{int(time.time())}"

    def create_folder(self, folder: str, mount_id: Optional[str] = None) -> dict[str, Any]:
        clean_folder = self._sanitize_rel_folder(folder)
        if not clean_folder:
            raise ValueError("Folder name cannot be empty")
        mounts = self.cache.list_mounts()
        if not mounts:
            raise RuntimeError("No local workspace directory mounted")
        chosen = next((m for m in mounts if m["mount_id"] == mount_id), mounts[0])
        root = Path(chosen["root_path"]).resolve()
        target = (root / clean_folder).resolve()
        if not str(target).startswith(str(root) + os.sep):
            raise ValueError("Path traversal outside workspace mount is forbidden")
        target.mkdir(parents=True, exist_ok=True)
        if self._watcher:
            self._watcher.notify_roots_changed()
        return {"mount_id": chosen["mount_id"], "folder": clean_folder, "path": str(target)}

    def save_document(self, uri: str, raw_markdown: str) -> DocumentRecord:
        if not uri.startswith("local://"):
            raise ValueError("Only local:// URIs can be saved directly to disk")
        rest = uri[len("local://") :]
        mount_id, _, rel = rest.partition("/")
        if not mount_id or not rel:
            raise ValueError(f"Invalid local URI: {uri}")
        mount = next((m for m in self.cache.list_mounts() if m["mount_id"] == mount_id), None)
        if not mount:
            raise ValueError(f"Unknown mount: {mount_id}")
        root = Path(mount["root_path"]).resolve()
        target = (root / rel).resolve()
        if not (target == root or str(target).startswith(str(root) + os.sep)):
            raise ValueError("Path traversal outside workspace mount is forbidden")
        target.parent.mkdir(parents=True, exist_ok=True)

        # Crash-safe atomic write via same-directory temp file + fsync + replace
        fd, tmp_path = tempfile.mkstemp(prefix=".folio-", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(raw_markdown)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        st = target.stat()
        self._mtimes[uri] = st.st_mtime_ns
        doc = self._read_markdown_file(mount_id, root, target, st.st_mtime)
        if not doc:
            raise RuntimeError("Failed to reload saved Markdown file")
        self.cache.upsert_document(doc, scope=mount_id)
        return doc

    def create_document(
        self,
        title: str,
        folder: str = "",
        mount_id: Optional[str] = None,
        initial_body: Optional[str] = None,
        tags: Optional[list[str]] = None,
        extra_frontmatter: Optional[dict[str, Any]] = None,
    ) -> DocumentRecord:
        mounts = self.cache.list_mounts()
        if not mounts:
            raise RuntimeError("No local workspace directory mounted")
        chosen = next((m for m in mounts if m["mount_id"] == mount_id), mounts[0])
        mid = chosen["mount_id"]
        clean_folder = self._sanitize_rel_folder(folder) if folder else ""
        slug = self._slugify_title(title)
        rel = f"{clean_folder}/{slug}.md" if clean_folder else f"{slug}.md"
        uri = f"local://{mid}/{rel}"
        root = Path(chosen["root_path"]).resolve()
        if (root / rel).exists():
            rel = f"{rel[:-3]}-{int(time.time()) % 10000}.md"
            uri = f"local://{mid}/{rel}"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        clean_title = title.strip() or "Untitled Note"
        fm: dict[str, Any] = {
            "title": clean_title,
            "author": "thomas",
            "date": today,
            "tags": list(tags or []),
            **(extra_frontmatter or {}),
        }
        body_str = initial_body if initial_body is not None else f"# {clean_title}\n\n"
        raw = serialize_frontmatter(fm, body_str)
        doc = self.save_document(uri, raw)
        if self._watcher:
            self._watcher.notify_roots_changed()
        return doc

    def open_or_create_daily_note(self, date_str: Optional[str] = None) -> DocumentRecord:
        mounts = self.cache.list_mounts()
        if not mounts:
            raise RuntimeError("No local workspace directory mounted")
        chosen = mounts[0]
        mid = chosen["mount_id"]
        root = Path(chosen["root_path"]).resolve()
        day = (date_str or datetime.now().strftime("%Y-%m-%d")).strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            raise ValueError(f"Invalid date format: {day!r}")
        rel = f"Journal/{day}.md"
        uri = f"local://{mid}/{rel}"
        target = root / rel
        if target.is_file():
            doc = self._read_markdown_file(mid, root, target, target.stat().st_mtime)
            if doc:
                self.cache.upsert_document(doc, scope=mid)
                return doc
        body = (
            f"# {day}\n\n"
            "## Focus & Working Notes\n\n"
            "- \n\n"
            "## Open Questions & Links\n\n"
            "- \n"
        )
        fm: dict[str, Any] = {
            "title": f"{day} · Journal",
            "author": "thomas",
            "date": day,
            "tags": ["journal"],
        }
        doc = self.save_document(uri, serialize_frontmatter(fm, body))
        if self._watcher:
            self._watcher.notify_roots_changed()
        return doc

    def rename_or_move_document(
        self,
        uri: str,
        *,
        new_title: Optional[str] = None,
        new_folder: Optional[str] = None,
        raw_markdown_override: Optional[str] = None,
    ) -> DocumentRecord:
        if not uri.startswith("local://"):
            raise ValueError("Only local:// URIs can be renamed or moved")
        rest = uri[len("local://") :]
        mount_id, _, rel = rest.partition("/")
        mount = next((m for m in self.cache.list_mounts() if m["mount_id"] == mount_id), None)
        if not mount:
            raise ValueError(f"Unknown mount: {mount_id}")
        root = Path(mount["root_path"]).resolve()
        old_path = (root / rel).resolve()
        if not str(old_path).startswith(str(root) + os.sep) or not old_path.is_file():
            raise FileNotFoundError(f"Local note not found: {uri}")

        current_raw = (
            raw_markdown_override
            if raw_markdown_override is not None
            else old_path.read_text(encoding="utf-8", errors="replace")
        )
        fm, body = parse_frontmatter(current_raw)
        old_title = extract_title(fm, body, old_path.stem)

        target_title = (new_title.strip() if new_title is not None else old_title) or old_title
        if new_title is not None and target_title != old_title:
            fm["title"] = target_title
            # Update leading `# Old Title` heading if present
            lines = body.splitlines()
            for idx, ln in enumerate(lines):
                if not ln.strip():
                    continue
                m = re.match(r"^#\s+(.+)$", ln.strip())
                if m:
                    lines[idx] = f"# {target_title}"
                break
            body = "\n".join(lines) + ("\n" if body.endswith("\n") else "")
            current_raw = serialize_frontmatter(fm, body)

        old_folder = Path(rel).parent.as_posix()
        if old_folder == ".":
            old_folder = ""
        target_folder = (
            self._sanitize_rel_folder(new_folder) if new_folder is not None else old_folder
        )

        new_slug = (
            self._slugify_title(target_title)
            if new_title is not None
            else old_path.stem
        )
        new_rel = f"{target_folder}/{new_slug}.md" if target_folder else f"{new_slug}.md"
        new_uri = f"local://{mount_id}/{new_rel}"
        new_path = (root / new_rel).resolve()
        if not str(new_path).startswith(str(root) + os.sep):
            raise ValueError("Forbidden target path")

        if new_path != old_path and new_path.exists():
            new_rel = f"{new_rel[:-3]}-{int(time.time()) % 10000}.md"
            new_uri = f"local://{mount_id}/{new_rel}"

        saved = self.save_document(new_uri, current_raw)
        if new_uri != uri:
            if old_path.is_file():
                try:
                    old_path.unlink()
                except OSError:
                    pass
            self.cache.delete_document(uri)
            self._mtimes.pop(uri, None)
            self.cache.move_sync_uri(uri, new_uri)
            if self._watcher:
                self._watcher.notify_roots_changed()
        return saved

    def fork_remote_to_local(
        self,
        source_doc: DocumentRecord,
        folder: str = "Notes",
    ) -> DocumentRecord:
        extra_fm: dict[str, Any] = {
            "forked_from": source_doc.uri,
        }
        body_prefix = ""
        if source_doc.provider == "hindsight":
            if source_doc.external_id.startswith("kp-"):
                body_prefix = f"> Grounded in Hindsight Knowledge Page [[{source_doc.external_id}]].\n\n"
            elif source_doc.external_id.startswith("src-"):
                body_prefix = f"> Grounded in Hindsight Raw Source `{source_doc.external_id}`.\n\n"
            else:
                body_prefix = f"> Imported from Hindsight (`{source_doc.uri}`).\n\n"
        elif source_doc.provider == "notion":
            body_prefix = f"> Imported from Notion (`{source_doc.title}`).\n\n"

        clean_body = source_doc.body.strip()
        if not clean_body.startswith("# "):
            clean_body = f"# {source_doc.title}\n\n{body_prefix}{clean_body}\n"
        else:
            first_nl = clean_body.find("\n")
            if first_nl >= 0:
                clean_body = (
                    clean_body[: first_nl + 1]
                    + "\n"
                    + body_prefix
                    + clean_body[first_nl + 1 :].lstrip("\n")
                    + "\n"
                )
        return self.create_document(
            title=source_doc.title,
            folder=folder,
            initial_body=clean_body,
            tags=source_doc.tags,
            extra_frontmatter=extra_fm,
        )

    def delete_local_document(self, uri: str) -> dict[str, Any]:
        if not uri.startswith("local://"):
            raise ValueError("Only local:// URIs can be deleted")
        rest = uri[len("local://") :]
        mount_id, _, rel = rest.partition("/")
        mount = next((m for m in self.cache.list_mounts() if m["mount_id"] == mount_id), None)
        if not mount:
            raise ValueError(f"Unknown mount: {mount_id}")
        root = Path(mount["root_path"]).resolve()
        target = (root / rel).resolve()
        if not str(target).startswith(str(root) + os.sep):
            raise ValueError("Forbidden path")
        if target.is_file():
            target.unlink()
        self.cache.delete_document(uri)
        self.cache.delete_sync_state(uri)
        self._mtimes.pop(uri, None)
        return {"deleted": uri}

    def search(self, query: str, scope: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        return self.cache.search_fts(query, provider=self.provider_id, scope=scope, limit=limit)
