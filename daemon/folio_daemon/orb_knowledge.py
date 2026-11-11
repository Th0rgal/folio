import concurrent.futures
import difflib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .cache import CacheStore
from .hindsight import HindsightProvider
from .http_pool import HttpPoolError, SHARED_HTTP_POOL
from .local_fs import LocalFsProvider, is_note_private_for_sync
from .markdown_utils import extract_tags, parse_frontmatter
from .models import DocumentRecord
from .notion import NotionProvider
from .secrets import CredentialResolver


USER_AGENT = "folio-copilot/0.2.0 (+https://github.com/Th0rgal/folio)"

PROFILE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "builtin/fast",
        "label": "Fast",
        "short": "fast",
        "description": "Ultra-low-latency synthesis, quick edits & instant lookup",
        "candidates": ("builtin/fast", "fast"),
        "fallback": "builtin/fast",
        "sovereign_local": False,
    },
    {
        "id": "builtin/smart",
        "label": "Smart",
        "short": "smart",
        "description": "Deep mathematical reasoning, multi-hop crawl & structured refactoring",
        "candidates": ("builtin/smart", "smart"),
        "fallback": "builtin/smart",
        "sovereign_local": False,
    },
    {
        "id": "builtin/private",
        "label": "Private",
        "short": "private",
        "description": "Sovereign Spark inference — safe for #private & sensitive notes",
        "candidates": ("builtin/private", "private"),
        "fallback": "private",
        "sovereign_local": True,
    },
    {
        "id": "builtin/obliterated",
        "label": "Obliterated",
        "short": "obliterated",
        "description": "Uncensored abliterated Spark model chain",
        "candidates": ("builtin/obliterated", "obliterated"),
        "fallback": "obliterated",
        "sovereign_local": True,
    },
)

PROFILE_BY_ID: dict[str, dict[str, Any]] = {p["id"]: p for p in PROFILE_SPECS}
for _p in PROFILE_SPECS:
    for _cand in _p["candidates"]:
        PROFILE_BY_ID.setdefault(_cand, _p)


SYSTEM_PROMPT = """You are Folio Knowledge Engine, Thomas's fast local-first research & note-editing copilot.

Core rules:
1. Epistemic provenance: Strictly distinguish human-authored raw notes/sources (`author: thomas`, `is_agent_summary: false`) from synthesized Hindsight Knowledge Pages (`kp-*`, `is_agent_summary: true`). Cite notes with `[[Note Title]]`, Knowledge Pages with `[[kp-...]]`, and raw sources with `` `src-...` ``.
2. Concise & rigorous: Prefer direct, dense answers. Use GitHub-flavored Markdown and KaTeX (`$...$` inline, `$$...$$` display). Write literal currency dollars as `\\$100` so two `$` never collide into broken math.
3. Crawling knowledge: Local FTS5 hits and the active note are pre-seeded below. Use `search_local_notes`, `crawl_hindsight`, and `read_knowledge_item` in parallel whenever deeper facts, cross-bank recall (`thomas`, `folio`, `ppl`), or full page contents are needed.
4. Editing & creating notes:
   - Never dump a giant rewritten file in chat when the user wants to edit a note; call `propose_note_edit` with minimal surgical `edits` (`old_text` -> `new_text`) or `new_markdown` so Folio renders an interactive diff card the user can apply in one keystroke.
   - To draft a brand-new standalone note, call `create_note_draft`."""


TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_local_notes",
            "description": "Fast SQLite FTS5 lexical search across all mounted local Markdown directories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keywords or phrase."},
                    "limit": {"type": "integer", "description": "Max results (default 8).", "default": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_hindsight",
            "description": "Parallel semantic recall + Knowledge Page search across Hindsight memory banks (`thomas`, `folio`, `ppl`).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Semantic or topical query."},
                    "banks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional bank IDs (defaults to ['thomas', sync_bank]).",
                    },
                    "limit": {"type": "integer", "description": "Max results (default 8).", "default": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_knowledge_item",
            "description": "Read the full Markdown body and provenance metadata of a local note (`local://...` or title slug), Hindsight Knowledge Page (`kp-...` or `hindsight://...`), raw source (`src-...`), or Notion page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "URI (`local://...`, `hindsight://...`), ID (`kp-...`, `src-...`), or wikilink title.",
                    },
                    "bank_id": {
                        "type": "string",
                        "description": "Optional Hindsight bank ID (defaults to 'thomas').",
                    },
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_note_edit",
            "description": "Stage a clean, reviewable diff proposal on the active note (or a target `local://` note). Prefer exact `edits` blocks (`old_text` -> `new_text`) for surgical changes, or `new_markdown` for full rewrites/empty notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Short 1-sentence description of the edit."},
                    "target_uri": {
                        "type": "string",
                        "description": "Optional `local://` URI. Defaults to the currently open note.",
                    },
                    "edits": {
                        "type": "array",
                        "description": "Ordered exact search-and-replace blocks.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_text": {"type": "string", "description": "Exact existing substring to replace."},
                                "new_text": {"type": "string", "description": "Replacement Markdown text."},
                            },
                            "required": ["old_text", "new_text"],
                        },
                    },
                    "new_markdown": {
                        "type": "string",
                        "description": "Complete replacement Markdown (only if rewriting the entire document).",
                    },
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_note_draft",
            "description": "Stage or create a new local Markdown note with YAML frontmatter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Note title."},
                    "markdown_body": {"type": "string", "description": "Markdown body content (starting with `# Title`)."},
                    "folder": {"type": "string", "description": "Target folder (default 'Notes').", "default": "Notes"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags to attach in YAML frontmatter.",
                    },
                },
                "required": ["title", "markdown_body"],
            },
        },
    },
]


@dataclass
class EditProposal:
    proposal_id: str
    request_id: str
    kind: str  # "edit" | "create"
    summary: str
    target_uri: Optional[str]
    title: str
    folder: str
    tags: list[str]
    before_markdown: str
    after_markdown: str
    unified_diff: str
    added_lines: int
    removed_lines: int
    applied: bool = False
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "request_id": self.request_id,
            "kind": self.kind,
            "summary": self.summary,
            "target_uri": self.target_uri,
            "title": self.title,
            "folder": self.folder,
            "tags": self.tags,
            "before_markdown": self.before_markdown,
            "after_markdown": self.after_markdown,
            "unified_diff": self.unified_diff,
            "added_lines": self.added_lines,
            "removed_lines": self.removed_lines,
            "applied": self.applied,
        }


def _compute_diff(before: str, after: str, label: str) -> tuple[str, int, int]:
    b_lines = before.splitlines(keepends=True)
    a_lines = after.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(b_lines, a_lines, fromfile=f"a/{label}", tofile=f"b/{label}", n=2)
    )
    added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))
    diff_text = "".join(diff_lines)
    if len(diff_text) > 12000:
        diff_text = diff_text[:12000] + "\n... (diff truncated)\n"
    return diff_text, added, removed


def _raw_markdown_is_private(raw_md: str) -> bool:
    if not raw_md.strip():
        return False
    fm, body = parse_frontmatter(raw_md)
    if fm.get("sync") is False or str(fm.get("sync") or "").lower() in ("false", "no", "never", "off"):
        return True
    for t in extract_tags(fm, body):
        if t.lstrip("#@").strip().lower() in ("private", "nosync", "no-sync", "secret", "local"):
            return True
    return False


class OrbKnowledgeEngine:
    """Fast, bounded Knowledge Crawler & Editor Copilot connected to Orb Router (`orbrouter.thomas.md/v1`).
    Supports `builtin/fast`, `builtin/smart`, `builtin/private` (`private`), and `builtin/obliterated` (`obliterated`).
    """

    def __init__(
        self,
        cache: CacheStore,
        secrets: CredentialResolver,
        local_fs: LocalFsProvider,
        hindsight: HindsightProvider,
        notion: NotionProvider,
        push_event: Callable[[str, Any], None],
    ) -> None:
        self.cache = cache
        self.secrets = secrets
        self.local_fs = local_fs
        self.hindsight = hindsight
        self.notion = notion
        self._push_event = push_event

        self.base_url = (
            os.environ.get("FOLIO_ORBROUTER_URL")
            or os.environ.get("ORBROUTER_BASE_URL")
            or "https://orbrouter.thomas.md/v1"
        ).rstrip("/")

        self._lock = threading.Lock()
        self._models_cache: Optional[set[str]] = None
        self._models_fetched_at: float = 0.0
        self._active_cancels: dict[str, threading.Event] = {}
        self._proposals: dict[str, EditProposal] = {}

    def _api_key(self) -> Optional[str]:
        return self.secrets.get("orbrouter_api_key")

    def _discover_models(self, force: bool = False) -> set[str]:
        now = time.monotonic()
        with self._lock:
            if not force and self._models_cache is not None and (now - self._models_fetched_at) < 600.0:
                return set(self._models_cache)
        tok = self._api_key()
        if not tok:
            return set()
        try:
            res = SHARED_HTTP_POOL.request_json(
                f"{self.base_url}/models",
                headers={
                    "Authorization": f"Bearer {tok}",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
                timeout=5.0,
            )
            items = res.get("data") if isinstance(res, dict) else []
            found = {
                str(item.get("id")).strip()
                for item in (items or [])
                if isinstance(item, dict) and item.get("id")
            }
            if found:
                with self._lock:
                    self._models_cache = found
                    self._models_fetched_at = now
                return found
        except Exception:
            pass
        return set()

    def resolve_profile_model(self, requested_profile: str, discover: bool = True) -> tuple[str, str, bool]:
        """Returns `(canonical_profile_id, upstream_model_id, is_sovereign_local)`."""
        clean = (requested_profile or "builtin/smart").strip()
        spec = PROFILE_BY_ID.get(clean)
        available = self._discover_models() if discover else set()
        if spec:
            for cand in spec["candidates"]:
                if cand in available:
                    return spec["id"], cand, bool(spec["sovereign_local"])
            return spec["id"], str(spec["fallback"]), bool(spec["sovereign_local"])
        sovereign = clean in ("private", "obliterated", "builtin/private", "builtin/obliterated")
        return clean, clean, sovereign

    def status(self, discover: bool = False) -> dict[str, Any]:
        configured = self.secrets.has("orbrouter_api_key")
        available = self._discover_models() if (discover and configured) else (self._models_cache or set())
        profiles_out: list[dict[str, Any]] = []
        for spec in PROFILE_SPECS:
            resolved = spec["fallback"]
            for cand in spec["candidates"]:
                if cand in available:
                    resolved = cand
                    break
            profiles_out.append(
                {
                    "id": spec["id"],
                    "label": spec["label"],
                    "short": spec["short"],
                    "description": spec["description"],
                    "resolved_model": resolved,
                    "sovereign_local": spec["sovereign_local"],
                }
            )
        return {
            "configured": configured,
            "connected": configured,
            "base_url": self.base_url,
            "auth_source": self.secrets.source_kind("orbrouter_api_key"),
            "profiles": profiles_out,
            "default_profile": "builtin/smart",
        }

    def cancel_query(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            ev = self._active_cancels.get(request_id)
            if ev:
                ev.set()
                return {"cancelled": True, "request_id": request_id}
        return {"cancelled": False, "request_id": request_id}

    def start_query(
        self,
        *,
        prompt: str,
        profile: str = "builtin/smart",
        scope: str = "vault",
        active_uri: Optional[str] = None,
        active_markdown: Optional[str] = None,
        selection: Optional[str] = None,
        history: Optional[list[dict[str, str]]] = None,
        bank_id: Optional[str] = None,
        auto_apply: bool = False,
        allow_external_for_private: bool = False,
        request_id: Optional[str] = None,
        blocking: bool = False,
    ) -> dict[str, Any]:
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            raise ValueError("Prompt cannot be empty")
        if not self._api_key():
            raise RuntimeError(
                "Orb Router key not configured. Connect local Orb (`~/.orb/connection.json`) or set ORBROUTER_API_KEY."
            )

        req_id = (request_id or f"cop-{uuid.uuid4().hex[:12]}").strip()
        cancel_ev = threading.Event()
        with self._lock:
            self._active_cancels[req_id] = cancel_ev

        # Check privacy guard on active note before choosing upstream model
        active_doc: Optional[DocumentRecord] = None
        if active_uri:
            if active_uri.startswith("local://"):
                active_doc = self.local_fs.get_document(active_uri)
            elif active_uri.startswith("hindsight://") or active_uri.startswith(("kp-", "src-")):
                active_doc = self.hindsight.get_document(active_uri, scope=bank_id)
            elif active_uri.startswith("notion://"):
                active_doc = self.notion.get_document(active_uri)

        note_is_private = bool(
            (active_doc and active_doc.provider == "local-fs" and is_note_private_for_sync(active_doc))
            or (active_markdown and _raw_markdown_is_private(active_markdown))
        )

        effective_profile = profile or "builtin/smart"
        privacy_clamped = False
        _, _, requested_sovereign = self.resolve_profile_model(effective_profile, discover=False)
        if note_is_private and not requested_sovereign and not allow_external_for_private:
            effective_profile = "builtin/private"
            privacy_clamped = True

        canon_profile, upstream_model, is_sovereign = self.resolve_profile_model(effective_profile, discover=True)

        runner_args = (
            req_id,
            clean_prompt,
            canon_profile,
            upstream_model,
            is_sovereign,
            privacy_clamped,
            scope,
            active_uri,
            active_doc,
            active_markdown,
            selection,
            list(history or []),
            bank_id or self.hindsight.default_bank,
            auto_apply,
            allow_external_for_private,
            cancel_ev,
        )

        if blocking:
            return self._execute_query(*runner_args)

        worker = threading.Thread(
            target=self._execute_query_safe,
            args=runner_args,
            daemon=True,
            name=f"folio-copilot-{req_id}",
        )
        worker.start()
        return {
            "request_id": req_id,
            "profile": canon_profile,
            "resolved_model": upstream_model,
            "privacy_clamped": privacy_clamped,
            "streaming": True,
        }

    def _execute_query_safe(self, *args: Any) -> None:
        req_id = str(args[0])
        try:
            self._execute_query(*args)
        except Exception as exc:
            self._push_event(
                "copilot:error",
                {"request_id": req_id, "error": str(exc)},
            )
        finally:
            with self._lock:
                self._active_cancels.pop(req_id, None)

    def _build_turn1_context(
        self,
        *,
        prompt: str,
        scope: str,
        active_uri: Optional[str],
        active_doc: Optional[DocumentRecord],
        active_markdown: Optional[str],
        selection: Optional[str],
        allow_private_notes: bool,
    ) -> tuple[str, list[dict[str, Any]]]:
        sections: list[str] = []
        preseed_traces: list[dict[str, Any]] = []

        raw_note = active_markdown if active_markdown is not None else (active_doc.raw_markdown if active_doc else "")
        if active_doc or raw_note:
            title = active_doc.title if active_doc else "Unsaved Draft"
            uri_str = active_uri or (active_doc.uri if active_doc else "local://draft")
            prov_str = (
                f"provider={active_doc.provider}, author={active_doc.provenance.author}, "
                f"is_agent_summary={active_doc.provenance.is_agent_summary}"
                if active_doc
                else "provider=local-fs"
            )
            trimmed = raw_note
            if len(trimmed) > 14000:
                trimmed = trimmed[:14000] + "\n... [truncated — call read_knowledge_item if tail is needed]"
            sections.append(
                f"<active_note uri=\"{uri_str}\" title=\"{title}\" {prov_str}>\n{trimmed}\n</active_note>"
            )

        if selection and selection.strip():
            sections.append(f"<editor_selection>\n{selection.strip()}\n</editor_selection>")

        if scope in ("vault", "all"):
            fts_hits = self.local_fs.search(prompt, limit=6)
            filtered_hits: list[dict[str, Any]] = []
            for h in fts_hits:
                if active_uri and h.get("uri") == active_uri:
                    continue
                if not allow_private_notes:
                    d = self.cache.get_document(h["uri"])
                    if d and is_note_private_for_sync(d):
                        continue
                filtered_hits.append(h)
            if filtered_hits:
                lines = []
                for h in filtered_hits[:5]:
                    snip = str(h.get("snippet") or "").replace("<mark>", "").replace("</mark>", "")
                    lines.append(f"- {h['title']} (`{h['uri']}`): {snip}")
                sections.append("<local_fts5_preseed>\n" + "\n".join(lines) + "\n</local_fts5_preseed>")
                preseed_traces.append(
                    {
                        "tool": "fts5_preseed",
                        "label": f"Indexed {len(filtered_hits[:5])} local FTS5 matches",
                        "items": [
                            {"uri": h["uri"], "title": h["title"], "provider": h["provider"]}
                            for h in filtered_hits[:5]
                        ],
                    }
                )

        if not sections:
            return prompt, preseed_traces
        return "\n\n".join(sections) + f"\n\n<user_request>\n{prompt}\n</user_request>", preseed_traces

    def _execute_query(
        self,
        req_id: str,
        prompt: str,
        canon_profile: str,
        upstream_model: str,
        is_sovereign: bool,
        privacy_clamped: bool,
        scope: str,
        active_uri: Optional[str],
        active_doc: Optional[DocumentRecord],
        active_markdown: Optional[str],
        selection: Optional[str],
        history: list[dict[str, str]],
        bank_id: str,
        auto_apply: bool,
        allow_external_for_private: bool,
        cancel_ev: threading.Event,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        allow_private_notes = is_sovereign or allow_external_for_private

        user_turn_content, traces = self._build_turn1_context(
            prompt=prompt,
            scope=scope,
            active_uri=active_uri,
            active_doc=active_doc,
            active_markdown=active_markdown,
            selection=selection,
            allow_private_notes=allow_private_notes,
        )

        self._push_event(
            "copilot:start",
            {
                "request_id": req_id,
                "profile": canon_profile,
                "resolved_model": upstream_model,
                "privacy_clamped": privacy_clamped,
                "traces": traces,
            },
        )

        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for turn in history[-8:]:
            role = str(turn.get("role") or "")
            content = str(turn.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_turn_content})

        active_tools = TOOLS_SCHEMA if scope != "note_notools" else []
        full_answer_parts: list[str] = []
        created_proposals: list[dict[str, Any]] = []
        tools_enabled = True
        max_hops = 4

        try:
            for _hop in range(max_hops):
                if cancel_ev.is_set():
                    break

                try:
                    assistant_text, tool_calls = self._stream_chat_step(
                        req_id=req_id,
                        model=upstream_model,
                        messages=messages,
                        tools=active_tools if tools_enabled else None,
                        cancel_ev=cancel_ev,
                    )
                except HttpPoolError as http_err:
                    if tools_enabled and http_err.status in (400, 422) and "tool" in http_err.body.lower():
                        # Fallback for local models without native OpenAI tool-call grammar:
                        # pre-crawl Hindsight directly and stream answer in a single pass.
                        tools_enabled = False
                        if scope in ("vault", "all") and self.hindsight.status()["connected"]:
                            hs_hits = self._run_crawl_hindsight(
                                query=prompt,
                                banks=None,
                                limit=6,
                                scope=scope,
                                default_bank=bank_id,
                            )
                            if hs_hits.get("results"):
                                messages[-1]["content"] += (
                                    "\n\n<hindsight_recall>\n"
                                    + json.dumps(hs_hits["results"], ensure_ascii=False)
                                    + "\n</hindsight_recall>"
                                )
                        assistant_text, tool_calls = self._stream_chat_step(
                            req_id=req_id,
                            model=upstream_model,
                            messages=messages,
                            tools=None,
                            cancel_ev=cancel_ev,
                        )
                    else:
                        raise

                if assistant_text:
                    full_answer_parts.append(assistant_text)

                if not tool_calls or cancel_ev.is_set():
                    break

                messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_text or None,
                        "tool_calls": tool_calls,
                    }
                )

                # Execute all tool calls in parallel for minimum wall-clock latency
                tool_outputs = self._execute_tool_calls_parallel(
                    req_id=req_id,
                    tool_calls=tool_calls,
                    scope=scope,
                    default_bank=bank_id,
                    active_uri=active_uri or (active_doc.uri if active_doc else None),
                    active_markdown=active_markdown,
                    allow_private_notes=allow_private_notes,
                    auto_apply=auto_apply,
                    traces=traces,
                    created_proposals=created_proposals,
                )
                for tc, out_str in zip(tool_calls, tool_outputs):
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": out_str,
                        }
                    )

            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            final_text = "\n\n".join(p for p in full_answer_parts if p).strip()
            if not final_text and created_proposals:
                final_text = f"Prepared {len(created_proposals)} edit proposal(s): {created_proposals[-1]['summary']}"

            done_payload = {
                "request_id": req_id,
                "profile": canon_profile,
                "resolved_model": upstream_model,
                "privacy_clamped": privacy_clamped,
                "cancelled": cancel_ev.is_set(),
                "answer": final_text,
                "proposals": created_proposals,
                "traces": traces,
                "elapsed_ms": elapsed_ms,
            }
            self._push_event("copilot:done", done_payload)
            return done_payload
        finally:
            with self._lock:
                self._active_cancels.pop(req_id, None)

    def _stream_chat_step(
        self,
        *,
        req_id: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]],
        cancel_ev: threading.Event,
    ) -> tuple[str, list[dict[str, Any]]]:
        tok = self._api_key()
        if not tok:
            raise RuntimeError("Missing Orb Router API key")

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {tok}",
            "User-Agent": USER_AGENT,
        }

        text_chunks: list[str] = []
        tc_by_index: dict[int, dict[str, Any]] = {}

        for raw_frame in SHARED_HTTP_POOL.stream_sse(
            f"{self.base_url}/chat/completions",
            method="POST",
            headers=headers,
            payload=payload,
            timeout=75.0,
            cancel_event=cancel_ev,
        ):
            try:
                frame = json.loads(raw_frame)
            except Exception:
                continue
            if not isinstance(frame, dict):
                continue
            if "error" in frame and frame["error"]:
                err_obj = frame["error"]
                msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
                raise RuntimeError(f"Orb Router error: {msg}")

            choices = frame.get("choices") or []
            if not choices:
                continue
            choice0 = choices[0] if isinstance(choices[0], dict) else {}

            # Non-streaming full message fallback
            if "message" in choice0 and isinstance(choice0["message"], dict):
                msg_obj = choice0["message"]
                txt = str(msg_obj.get("content") or "")
                if txt:
                    text_chunks.append(txt)
                    self._push_event("copilot:chunk", {"request_id": req_id, "delta": txt})
                raw_tcs = msg_obj.get("tool_calls") or []
                for idx, tc in enumerate(raw_tcs):
                    if isinstance(tc, dict):
                        tc_by_index[idx] = {
                            "id": str(tc.get("id") or f"call_{idx}"),
                            "type": "function",
                            "function": {
                                "name": str((tc.get("function") or {}).get("name") or ""),
                                "arguments": str((tc.get("function") or {}).get("arguments") or "{}"),
                            },
                        }
                continue

            delta = choice0.get("delta")
            if not isinstance(delta, dict):
                continue

            content_piece = delta.get("content")
            if isinstance(content_piece, str) and content_piece:
                text_chunks.append(content_piece)
                self._push_event("copilot:chunk", {"request_id": req_id, "delta": content_piece})

            reasoning_piece = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning_piece, str) and reasoning_piece:
                self._push_event("copilot:reasoning", {"request_id": req_id, "delta": reasoning_piece})

            delta_tcs = delta.get("tool_calls")
            if isinstance(delta_tcs, list):
                for item in delta_tcs:
                    if not isinstance(item, dict):
                        continue
                    idx = int(item.get("index", 0))
                    slot = tc_by_index.setdefault(
                        idx,
                        {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    if item.get("id"):
                        slot["id"] = str(item["id"])
                    fn = item.get("function")
                    if isinstance(fn, dict):
                        if fn.get("name"):
                            slot["function"]["name"] += str(fn["name"])
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += str(fn["arguments"])

        assembled_tcs: list[dict[str, Any]] = []
        for idx in sorted(tc_by_index.keys()):
            slot = tc_by_index[idx]
            if not slot["id"]:
                slot["id"] = f"call_{uuid.uuid4().hex[:8]}"
            if slot["function"]["name"]:
                if not slot["function"]["arguments"].strip():
                    slot["function"]["arguments"] = "{}"
                assembled_tcs.append(slot)

        return "".join(text_chunks), assembled_tcs

    def _execute_tool_calls_parallel(
        self,
        *,
        req_id: str,
        tool_calls: list[dict[str, Any]],
        scope: str,
        default_bank: str,
        active_uri: Optional[str],
        active_markdown: Optional[str],
        allow_private_notes: bool,
        auto_apply: bool,
        traces: list[dict[str, Any]],
        created_proposals: list[dict[str, Any]],
    ) -> list[str]:
        def _run_one(tc: dict[str, Any]) -> tuple[dict[str, Any], str]:
            fn_name = tc["function"]["name"]
            raw_args = tc["function"]["arguments"]
            try:
                args = json.loads(raw_args) if raw_args else {}
                if not isinstance(args, dict):
                    args = {}
            except Exception:
                args = {}

            self._push_event(
                "copilot:tool",
                {"request_id": req_id, "tool": fn_name, "status": "running", "args": args},
            )
            try:
                if fn_name == "search_local_notes":
                    q = str(args.get("query") or "")
                    lim = min(int(args.get("limit") or 8), 20)
                    hits = self.local_fs.search(q, limit=lim)
                    if not allow_private_notes:
                        hits = [
                            h
                            for h in hits
                            if not (
                                (d := self.cache.get_document(h["uri"])) and is_note_private_for_sync(d)
                            )
                        ]
                    compact = [
                        {
                            "uri": h["uri"],
                            "title": h["title"],
                            "folder": h.get("folder_path", ""),
                            "tags": h.get("tags", []),
                            "snippet": str(h.get("snippet") or "").replace("<mark>", "").replace("</mark>", ""),
                        }
                        for h in hits
                    ]
                    trace = {
                        "tool": fn_name,
                        "label": f"Searched local notes for “{q}” ({len(compact)} hits)",
                        "items": [{"uri": c["uri"], "title": c["title"], "provider": "local-fs"} for c in compact[:6]],
                    }
                    return trace, json.dumps({"results": compact}, ensure_ascii=False)

                if fn_name == "crawl_hindsight":
                    q = str(args.get("query") or "")
                    banks_arg = args.get("banks")
                    lim = min(int(args.get("limit") or 8), 16)
                    res = self._run_crawl_hindsight(
                        query=q,
                        banks=banks_arg if isinstance(banks_arg, list) else None,
                        limit=lim,
                        scope=scope,
                        default_bank=default_bank,
                    )
                    items_meta = [
                        {"uri": r["uri"], "title": r["title"], "provider": "hindsight"}
                        for r in res.get("results", [])[:6]
                    ]
                    trace = {
                        "tool": fn_name,
                        "label": f"Crawled Hindsight ({', '.join(res.get('banks', []))}) for “{q}” ({len(res.get('results', []))} hits)",
                        "items": items_meta,
                    }
                    return trace, json.dumps(res, ensure_ascii=False)

                if fn_name == "read_knowledge_item":
                    target = str(args.get("target") or "").strip()
                    b_id = str(args.get("bank_id") or default_bank)
                    doc = self._resolve_any_doc(target, b_id)
                    if not doc:
                        trace = {"tool": fn_name, "label": f"Not found: {target}", "items": []}
                        return trace, json.dumps({"error": f"Document not found: {target}"})
                    if doc.provider == "local-fs" and is_note_private_for_sync(doc) and not allow_private_notes:
                        trace = {"tool": fn_name, "label": f"Skipped #private note: {doc.title}", "items": []}
                        return trace, json.dumps(
                            {"error": "Note is marked #private; switch Copilot profile to Private to read it."}
                        )
                    body_text = doc.raw_markdown if doc.provider == "local-fs" else doc.body
                    if len(body_text) > 16000:
                        body_text = body_text[:16000] + "\n... [truncated]"
                    trace = {
                        "tool": fn_name,
                        "label": f"Read {doc.title}",
                        "items": [{"uri": doc.uri, "title": doc.title, "provider": doc.provider}],
                    }
                    return (
                        trace,
                        json.dumps(
                            {
                                "uri": doc.uri,
                                "provider": doc.provider,
                                "title": doc.title,
                                "tags": doc.tags,
                                "provenance": doc.provenance.to_dict(),
                                "content": body_text,
                            },
                            ensure_ascii=False,
                        ),
                    )

                if fn_name == "propose_note_edit":
                    prop_dict = self._stage_edit_proposal(
                        req_id=req_id,
                        args=args,
                        active_uri=active_uri,
                        active_markdown=active_markdown,
                        auto_apply=auto_apply,
                    )
                    trace = {
                        "tool": fn_name,
                        "label": f"{'Applied' if prop_dict['applied'] else 'Staged'} edit on {prop_dict['title']} (+{prop_dict['added_lines']}/-{prop_dict['removed_lines']})",
                        "items": (
                            [{"uri": prop_dict["target_uri"], "title": prop_dict["title"], "provider": "local-fs"}]
                            if prop_dict.get("target_uri")
                            else []
                        ),
                    }
                    return (
                        trace,
                        json.dumps(
                            {
                                "status": "applied" if prop_dict["applied"] else "staged_for_user_approval",
                                "proposal_id": prop_dict["proposal_id"],
                                "added_lines": prop_dict["added_lines"],
                                "removed_lines": prop_dict["removed_lines"],
                                "proposal": prop_dict,
                            },
                            ensure_ascii=False,
                        ),
                    )

                if fn_name == "create_note_draft":
                    prop_dict = self._stage_create_proposal(
                        req_id=req_id,
                        args=args,
                        auto_apply=auto_apply,
                    )
                    trace = {
                        "tool": fn_name,
                        "label": f"{'Created' if prop_dict['applied'] else 'Drafted'} note “{prop_dict['title']}”",
                        "items": (
                            [{"uri": prop_dict["target_uri"], "title": prop_dict["title"], "provider": "local-fs"}]
                            if prop_dict.get("target_uri")
                            else []
                        ),
                    }
                    return (
                        trace,
                        json.dumps(
                            {
                                "status": "created" if prop_dict["applied"] else "staged_for_user_approval",
                                "proposal_id": prop_dict["proposal_id"],
                                "proposal": prop_dict,
                            },
                            ensure_ascii=False,
                        ),
                    )

                return {"tool": fn_name, "label": f"Unknown tool {fn_name}", "items": []}, json.dumps(
                    {"error": f"Unknown tool: {fn_name}"}
                )
            except Exception as exc:
                return {"tool": fn_name, "label": f"{fn_name} error: {exc}", "items": []}, json.dumps(
                    {"error": str(exc)}
                )

        if len(tool_calls) == 1:
            pairs = [_run_one(tool_calls[0])]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tool_calls), 4)) as pool:
                pairs = list(pool.map(_run_one, tool_calls))

        outputs: list[str] = []
        for trace, out_json in pairs:
            traces.append(trace)
            self._push_event(
                "copilot:tool",
                {"request_id": req_id, "tool": trace["tool"], "status": "done", "trace": trace},
            )
            try:
                parsed_out = json.loads(out_json)
                if isinstance(parsed_out, dict) and isinstance(parsed_out.get("proposal"), dict):
                    created_proposals.append(parsed_out["proposal"])
            except Exception:
                pass
            outputs.append(out_json)
        return outputs

    def _run_crawl_hindsight(
        self,
        *,
        query: str,
        banks: Optional[list[str]],
        limit: int,
        scope: str,
        default_bank: str,
    ) -> dict[str, Any]:
        if banks:
            target_banks = [str(b).strip() for b in banks if str(b).strip()]
        elif scope == "all":
            known = [str(b["bank_id"]) for b in self.hindsight.list_banks(include_sync_bank=True)]
            target_banks = [b for b in known if b.lower() != "spark-memory-bank"][:4]
        else:
            target_banks = list(dict.fromkeys([default_bank, self.hindsight.sync_bank]))

        hits = self.hindsight.search(query, limit=limit, banks=target_banks)
        compact = [
            {
                "uri": h["uri"],
                "external_id": h.get("external_id"),
                "bank_id": h.get("scope"),
                "title": h["title"],
                "match_kind": h.get("match_kind"),
                "author": h.get("author"),
                "is_agent_summary": h.get("is_agent_summary"),
                "snippet": h.get("snippet"),
            }
            for h in hits
        ]
        return {"banks": target_banks, "results": compact}

    def _resolve_any_doc(self, target: str, bank_id: str) -> Optional[DocumentRecord]:
        clean = target.strip().strip("[]")
        if "|" in clean:
            clean = clean.split("|", 1)[0].strip()
        if clean.startswith("local://"):
            return self.local_fs.get_document(clean)
        if clean.startswith("hindsight://") or clean.startswith(("kp-", "src-")):
            return self.hindsight.get_document(clean, scope=bank_id)
        if clean.startswith("notion://"):
            return self.notion.get_document(clean)
        doc = self.local_fs.get_document(clean)
        if doc:
            return doc
        return self.cache.find_by_slug_or_id(clean)

    def _stage_edit_proposal(
        self,
        *,
        req_id: str,
        args: dict[str, Any],
        active_uri: Optional[str],
        active_markdown: Optional[str],
        auto_apply: bool,
    ) -> dict[str, Any]:
        target_uri = str(args.get("target_uri") or active_uri or "").strip()
        summary = str(args.get("summary") or "Update note").strip()

        doc: Optional[DocumentRecord] = None
        if target_uri and target_uri.startswith("local://"):
            doc = self.local_fs.get_document(target_uri)

        if target_uri and target_uri == active_uri and active_markdown is not None:
            before = active_markdown
        elif doc is not None:
            before = doc.raw_markdown
        elif active_markdown is not None:
            before = active_markdown
        else:
            raise ValueError("No editable local note is active; pass target_uri or use create_note_draft.")

        edits = args.get("edits")
        new_markdown = args.get("new_markdown")
        after = before
        if isinstance(edits, list) and edits:
            for idx, item in enumerate(edits):
                if not isinstance(item, dict):
                    continue
                old_t = str(item.get("old_text") or "")
                new_t = str(item.get("new_text") or "")
                if not old_t:
                    after = after.rstrip() + "\n\n" + new_t + "\n"
                elif old_t in after:
                    after = after.replace(old_t, new_t, 1)
                else:
                    raise ValueError(f"Edit block #{idx + 1} old_text did not match current note content")
        elif isinstance(new_markdown, str) and new_markdown.strip():
            after = new_markdown
        else:
            raise ValueError("propose_note_edit requires non-empty `edits` or `new_markdown`")

        label = doc.external_id if doc else "draft.md"
        title = doc.title if doc else "Active Note"
        diff_txt, added, removed = _compute_diff(before, after, label)
        prop = EditProposal(
            proposal_id=f"prop-{uuid.uuid4().hex[:10]}",
            request_id=req_id,
            kind="edit",
            summary=summary,
            target_uri=target_uri if target_uri.startswith("local://") else None,
            title=title,
            folder=doc.folder_path if doc else "",
            tags=list(doc.tags) if doc else [],
            before_markdown=before,
            after_markdown=after,
            unified_diff=diff_txt,
            added_lines=added,
            removed_lines=removed,
            applied=False,
        )
        with self._lock:
            self._proposals[prop.proposal_id] = prop

        if auto_apply and prop.target_uri:
            self.apply_proposal(prop.proposal_id)

        out = prop.to_dict()
        self._push_event("copilot:proposal", out)
        return out

    def _stage_create_proposal(
        self,
        *,
        req_id: str,
        args: dict[str, Any],
        auto_apply: bool,
    ) -> dict[str, Any]:
        title = str(args.get("title") or "Untitled Note").strip()
        body = str(args.get("markdown_body") or "").strip()
        folder = str(args.get("folder") or "Notes").strip()
        tags = [str(t) for t in (args.get("tags") or []) if str(t).strip()]
        if not body.startswith("# "):
            body = f"# {title}\n\n{body}\n"
        else:
            body = body + "\n"

        diff_txt, added, removed = _compute_diff("", body, f"{folder}/{title}.md")
        prop = EditProposal(
            proposal_id=f"prop-{uuid.uuid4().hex[:10]}",
            request_id=req_id,
            kind="create",
            summary=f"Create note “{title}” in {folder or 'root'}",
            target_uri=None,
            title=title,
            folder=folder,
            tags=tags,
            before_markdown="",
            after_markdown=body,
            unified_diff=diff_txt,
            added_lines=added,
            removed_lines=removed,
            applied=False,
        )
        with self._lock:
            self._proposals[prop.proposal_id] = prop

        if auto_apply:
            self.apply_proposal(prop.proposal_id)

        out = prop.to_dict()
        self._push_event("copilot:proposal", out)
        return out

    def apply_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self._lock:
            prop = self._proposals.get(proposal_id)
        if not prop:
            raise KeyError(f"Unknown edit proposal: {proposal_id}")

        doc: Optional[DocumentRecord] = None
        if prop.kind == "create":
            doc = self.local_fs.create_document(
                title=prop.title,
                folder=prop.folder,
                initial_body=prop.after_markdown,
                tags=prop.tags,
            )
            prop.target_uri = doc.uri
        elif prop.target_uri and prop.target_uri.startswith("local://"):
            doc = self.local_fs.save_document(prop.target_uri, prop.after_markdown)

        prop.applied = True
        doc_dict = doc.to_dict() if doc else None
        if doc_dict and doc:
            doc_dict["backlinks"] = self.cache.get_backlinks(doc)
            doc_dict["sync"] = self.hindsight.compute_note_sync_info(doc)
        return {
            "proposal": prop.to_dict(),
            "document": doc_dict,
            "tree": [n.to_dict() for n in self.local_fs.list_tree()],
        }
