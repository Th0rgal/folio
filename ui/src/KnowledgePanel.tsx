import { For, Show, createEffect, createSignal, onCleanup, onMount } from "solid-js";
import { MarkdownView } from "./MarkdownView";
import * as Ic from "./icons";
import { listenDesktopEvent, rpcInvoke } from "./rpc";
import type {
  CopilotProfile,
  CopilotProfileId,
  CopilotProposal,
  CopilotScope,
  CopilotStatus,
  CopilotTrace,
  DocumentRecord,
  ThemeMode,
  TreeNode,
} from "./types";

interface ChatTurn {
  id: string;
  role: "user" | "assistant";
  content: string;
  profile: CopilotProfileId;
  resolvedModel?: string;
  privacyClamped?: boolean;
  streaming?: boolean;
  elapsedMs?: number;
  error?: string | null;
  traces: CopilotTrace[];
  proposals: CopilotProposal[];
}

const DEFAULT_PROFILES: CopilotProfile[] = [
  {
    id: "builtin/fast",
    label: "Fast",
    short: "fast",
    description: "Ultra-low-latency synthesis, quick edits & instant lookup",
    resolved_model: "builtin/fast",
    sovereign_local: false,
  },
  {
    id: "builtin/smart",
    label: "Smart",
    short: "smart",
    description: "Deep mathematical reasoning, multi-hop crawl & refactoring",
    resolved_model: "builtin/smart",
    sovereign_local: false,
  },
  {
    id: "builtin/private",
    label: "Private",
    short: "private",
    description: "Sovereign Spark inference — safe for #private & sensitive notes",
    resolved_model: "private",
    sovereign_local: true,
  },
  {
    id: "builtin/obliterated",
    label: "Obliterated",
    short: "obliterated",
    description: "Uncensored abliterated Spark model chain",
    resolved_model: "obliterated",
    sovereign_local: true,
  },
];

const QUICK_PROMPTS: Array<{ label: string; prompt: string; scope?: CopilotScope }> = [
  {
    label: "Related Hindsight",
    prompt: "Crawl my Hindsight banks (`thomas` and `folio`) and local notes for prior pages, raw sources, or related derivations connected to this note.",
    scope: "vault",
  },
  {
    label: "Polish & tighten",
    prompt: "Propose a surgical edit to tighten the prose, structure, and formatting of this note while preserving every technical claim and equation.",
    scope: "note",
  },
  {
    label: "Verify math / specs",
    prompt: "Audit the mathematical equations, boundary conditions, and formal claims in this note for subtle errors or missing assumptions.",
    scope: "note",
  },
  {
    label: "Synthesize draft",
    prompt: "Crawl local notes and Hindsight on this topic and stage a clean new note draft linking key `[[kp-...]]` pages and raw `src-...` sources.",
    scope: "all",
  },
];

function DiffLines(props: { diff: string }) {
  const lines = () =>
    props.diff
      .split("\n")
      .filter((ln) => !ln.startsWith("--- ") && !ln.startsWith("+++ "))
      .slice(0, 60);

  return (
    <pre class="cop-diff-pre">
      <For each={lines()}>
        {(ln) => {
          const cls = ln.startsWith("+")
            ? "diff-add"
            : ln.startsWith("-")
              ? "diff-del"
              : ln.startsWith("@@")
                ? "diff-hunk"
                : "";
          return <div class={`cop-diff-line ${cls}`}>{ln || " "}</div>;
        }}
      </For>
    </pre>
  );
}

export function KnowledgePanel(props: {
  open: boolean;
  width: number;
  status: CopilotStatus | undefined;
  activeDoc: DocumentRecord | null;
  draftMarkdown: string;
  selection: string;
  activeBank: string;
  macros: Record<string, string>;
  themeMode: ThemeMode;
  requestedProfile?: CopilotProfileId | null;
  onClearSelection: () => void;
  onStartResize: (ev: PointerEvent) => void;
  onClose: () => void;
  onOpenUri: (uri: string) => void;
  onProposalApplied: (doc: DocumentRecord | null, tree: TreeNode[]) => void;
  onRestoreMarkdown: (uri: string, markdown: string) => void;
}) {
  const [profile, setProfile] = createSignal<CopilotProfileId>("builtin/smart");
  const [scope, setScope] = createSignal<CopilotScope>("vault");
  const [input, setInput] = createSignal("");
  const [turns, setTurns] = createSignal<ChatTurn[]>([]);
  const [activeReqId, setActiveReqId] = createSignal<string | null>(null);
  const [applyingId, setApplyingId] = createSignal<string | null>(null);

  let inputRef: HTMLTextAreaElement | undefined;
  let scrollRef: HTMLDivElement | undefined;

  const profiles = () => props.status?.profiles || DEFAULT_PROFILES;

  createEffect(() => {
    if (props.requestedProfile) {
      setProfile(props.requestedProfile);
    }
  });

  createEffect(() => {
    if (props.open) {
      queueMicrotask(() => inputRef?.focus());
    }
  });

  const scrollToBottom = () => {
    queueMicrotask(() => {
      if (scrollRef) {
        scrollRef.scrollTop = scrollRef.scrollHeight;
      }
    });
  };

  onMount(() => {
    const cleanups: Array<() => void> = [];

    const bind = (evName: string, fn: (p: any) => void) => {
      void listenDesktopEvent(evName, fn).then((unsub) => cleanups.push(unsub));
    };

    bind("copilot:start", (p) => {
      if (!p?.request_id) return;
      setTurns((prev) =>
        prev.map((t) =>
          t.id === p.request_id
            ? {
                ...t,
                profile: (p.profile as CopilotProfileId) || t.profile,
                resolvedModel: p.resolved_model,
                privacyClamped: Boolean(p.privacy_clamped),
                traces: Array.isArray(p.traces) ? p.traces : t.traces,
              }
            : t,
        ),
      );
      scrollToBottom();
    });

    bind("copilot:chunk", (p) => {
      if (!p?.request_id || typeof p.delta !== "string") return;
      setTurns((prev) =>
        prev.map((t) => (t.id === p.request_id ? { ...t, content: t.content + p.delta } : t)),
      );
      scrollToBottom();
    });

    bind("copilot:tool", (p) => {
      if (!p?.request_id || p.status !== "done" || !p.trace) return;
      setTurns((prev) =>
        prev.map((t) =>
          t.id === p.request_id ? { ...t, traces: [...t.traces, p.trace as CopilotTrace] } : t,
        ),
      );
      scrollToBottom();
    });

    bind("copilot:proposal", (p) => {
      if (!p?.request_id || !p?.proposal_id) return;
      const prop = p as CopilotProposal;
      setTurns((prev) =>
        prev.map((t) => {
          if (t.id !== prop.request_id) return t;
          const exists = t.proposals.some((x) => x.proposal_id === prop.proposal_id);
          return {
            ...t,
            proposals: exists
              ? t.proposals.map((x) => (x.proposal_id === prop.proposal_id ? prop : x))
              : [...t.proposals, prop],
          };
        }),
      );
      scrollToBottom();
    });

    bind("copilot:done", (p) => {
      if (!p?.request_id) return;
      if (activeReqId() === p.request_id) setActiveReqId(null);
      setTurns((prev) =>
        prev.map((t) =>
          t.id === p.request_id
            ? {
                ...t,
                content: t.content || String(p.answer || ""),
                streaming: false,
                elapsedMs: p.elapsed_ms,
                resolvedModel: p.resolved_model || t.resolvedModel,
                privacyClamped: Boolean(p.privacy_clamped ?? t.privacyClamped),
                traces: Array.isArray(p.traces) ? p.traces : t.traces,
                proposals: Array.isArray(p.proposals) ? p.proposals : t.proposals,
              }
            : t,
        ),
      );
      scrollToBottom();
    });

    bind("copilot:error", (p) => {
      if (!p?.request_id) return;
      if (activeReqId() === p.request_id) setActiveReqId(null);
      setTurns((prev) =>
        prev.map((t) =>
          t.id === p.request_id
            ? { ...t, streaming: false, error: String(p.error || "Copilot request failed") }
            : t,
        ),
      );
    });

    onCleanup(() => {
      for (const fn of cleanups) fn();
    });
  });

  const submitQuery = async (overridePrompt?: string, overrideScope?: CopilotScope) => {
    const q = (overridePrompt ?? input()).trim();
    if (!q || activeReqId()) return;
    if (!overridePrompt) setInput("");
    const effectiveScope = overrideScope ?? scope();
    if (overrideScope) setScope(overrideScope);

    const reqId = `cop-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
    const priorHistory = turns()
      .filter((t) => !t.error && t.content.trim().length > 0)
      .slice(-6)
      .map((t) => ({ role: t.role, content: t.content }));

    const userTurn: ChatTurn = {
      id: `${reqId}-u`,
      role: "user",
      content: q,
      profile: profile(),
      traces: [],
      proposals: [],
    };
    const assistantTurn: ChatTurn = {
      id: reqId,
      role: "assistant",
      content: "",
      profile: profile(),
      streaming: true,
      traces: [],
      proposals: [],
    };
    setTurns((prev) => [...prev, userTurn, assistantTurn]);
    setActiveReqId(reqId);
    scrollToBottom();

    try {
      await rpcInvoke("copilot_query", {
        request_id: reqId,
        prompt: q,
        profile: profile(),
        scope: effectiveScope,
        active_uri: props.activeDoc?.uri ?? null,
        active_markdown: props.draftMarkdown,
        selection: props.selection || null,
        history: priorHistory,
        bank_id: props.activeBank,
      });
    } catch (err) {
      setActiveReqId(null);
      setTurns((prev) =>
        prev.map((t) =>
          t.id === reqId
            ? { ...t, streaming: false, error: err instanceof Error ? err.message : String(err) }
            : t,
        ),
      );
    }
  };

  const handleStop = () => {
    const reqId = activeReqId();
    if (!reqId) return;
    setActiveReqId(null);
    void rpcInvoke("copilot_cancel", { request_id: reqId }).catch(() => undefined);
  };

  const handleApplyProposal = async (prop: CopilotProposal) => {
    setApplyingId(prop.proposal_id);
    try {
      const res = await rpcInvoke<{
        proposal: CopilotProposal;
        document: DocumentRecord | null;
        tree: TreeNode[];
      }>("copilot_apply_proposal", { proposal_id: prop.proposal_id });

      setTurns((prev) =>
        prev.map((t) => ({
          ...t,
          proposals: t.proposals.map((p) =>
            p.proposal_id === prop.proposal_id ? res.proposal : p,
          ),
        })),
      );
      props.onProposalApplied(res.document, res.tree);
    } catch (err) {
      setTurns((prev) =>
        prev.map((t) =>
          t.id === prop.request_id
            ? { ...t, error: err instanceof Error ? err.message : String(err) }
            : t,
        ),
      );
    } finally {
      setApplyingId(null);
    }
  };

  const isNotePrivate = () => props.activeDoc?.sync?.state === "private";

  return (
    <Show when={props.open}>
      <aside
        class="copilot-panel"
        style={{ width: `${props.width}px` }}
        data-testid="copilot-panel"
        onClick={(e) => e.stopPropagation()}
      >
        <div class="cop-resize" onPointerDown={props.onStartResize} />

        {/* Header + 4 Instant Model Chain Pills */}
        <div class="cop-head">
          <div class="cop-title-row">
            <span class="cop-brand">
              <Ic.SparklesIcon size={13.5} />
              <span>Knowledge Engine</span>
            </span>
            <div class="cop-head-actions">
              <Show when={turns().length > 0}>
                <button
                  type="button"
                  class="cop-clear-btn"
                  title="Clear thread"
                  onClick={() => setTurns([])}
                >
                  Clear
                </button>
              </Show>
              <button
                type="button"
                class="icon-btn"
                title="Close Knowledge Copilot (⌘J)"
                onClick={props.onClose}
              >
                <Ic.CloseIcon size={13} />
              </button>
            </div>
          </div>

          <div class="cop-profile-bar" role="tablist" aria-label="Orb Model Profile">
            <For each={profiles()}>
              {(p) => (
                <button
                  type="button"
                  class={`cop-profile-pill ${profile() === p.id ? "active" : ""} ${
                    p.sovereign_local ? "sovereign" : ""
                  }`}
                  title={`${p.label} (${p.resolved_model}) — ${p.description}`}
                  onClick={() => setProfile(p.id)}
                >
                  <Show when={p.id === "builtin/private"}>
                    <Ic.LockIcon size={10.5} />
                  </Show>
                  <Show when={p.id === "builtin/fast"}>
                    <Ic.BoltIcon size={10.5} />
                  </Show>
                  <span>{p.label}</span>
                </button>
              )}
            </For>
          </div>

          <div class="cop-scope-row">
            <div class="cop-scope-pills">
              <button
                type="button"
                class={`cop-scope-btn ${scope() === "note" ? "on" : ""}`}
                onClick={() => setScope("note")}
              >
                Active Note
              </button>
              <button
                type="button"
                class={`cop-scope-btn ${scope() === "vault" ? "on" : ""}`}
                onClick={() => setScope("vault")}
              >
                Vault + folio
              </button>
              <button
                type="button"
                class={`cop-scope-btn ${scope() === "all" ? "on" : ""}`}
                onClick={() => setScope("all")}
              >
                All Banks
              </button>
            </div>

            <Show when={isNotePrivate()}>
              <span class="cop-priv-guard" title="#private note auto-routes to Sovereign Spark">
                <Ic.LockIcon size={10} /> #private
              </span>
            </Show>
          </div>
        </div>

        {/* Conversation & Crawl Stream */}
        <div ref={scrollRef} class="cop-stream">
          <Show
            when={turns().length > 0}
            fallback={
              <div class="cop-empty">
                <div class="cop-empty-title">
                  Fast Knowledge Crawler & Surgical Note Editor
                </div>
                <p class="cop-empty-sub">
                  Connected directly to Orb Router (<code class="inline-code">fast</code> ·{" "}
                  <code class="inline-code">smart</code> · <code class="inline-code">private</code> ·{" "}
                  <code class="inline-code">obliterated</code>) with sub-3ms SQLite FTS5 pre-seeding
                  and parallel Hindsight recall.
                </p>
                <div class="cop-quick-grid">
                  <For each={QUICK_PROMPTS}>
                    {(qp) => (
                      <button
                        type="button"
                        class="cop-quick-chip"
                        onClick={() => void submitQuery(qp.prompt, qp.scope)}
                      >
                        {qp.label}
                      </button>
                    )}
                  </For>
                </div>
              </div>
            }
          >
            <For each={turns()}>
              {(turn) => (
                <div class={`cop-turn cop-turn-${turn.role}`}>
                  <Show
                    when={turn.role === "assistant"}
                    fallback={<div class="cop-user-bubble">{turn.content}</div>}
                  >
                    <div class="cop-assistant-meta">
                      <span class="cop-model-tag">{turn.resolvedModel || turn.profile}</span>
                      <Show when={turn.privacyClamped}>
                        <span class="cop-priv-clamped">Clamped to Private</span>
                      </Show>
                      <Show when={turn.elapsedMs}>
                        <span class="cop-latency">{(turn.elapsedMs! / 1000).toFixed(1)}s</span>
                      </Show>
                    </div>

                    {/* Crawl Trace Chips */}
                    <Show when={turn.traces.length > 0}>
                      <div class="cop-traces">
                        <For each={turn.traces}>
                          {(tr) => (
                            <div class="cop-trace-card">
                              <div class="cop-trace-label">{tr.label}</div>
                              <Show when={tr.items.length > 0}>
                                <div class="cop-trace-links">
                                  <For each={tr.items}>
                                    {(it) => (
                                      <button
                                        type="button"
                                        class="cop-cite-pill"
                                        onClick={() => props.onOpenUri(it.uri)}
                                      >
                                        {it.title}
                                      </button>
                                    )}
                                  </For>
                                </div>
                              </Show>
                            </div>
                          )}
                        </For>
                      </div>
                    </Show>

                    <Show when={turn.content}>
                      <div class="cop-md-wrap">
                        <MarkdownView
                          rawMarkdown={turn.content}
                          noteUri={props.activeDoc?.uri}
                          macros={props.macros}
                          themeMode={props.themeMode}
                          activeMathIndex={null}
                          onSelectMath={() => undefined}
                          onOpenWikilink={(target) => props.onOpenUri(target)}
                          onExternalUrl={(url) =>
                            void rpcInvoke("open_external_url", { url }).catch(() => undefined)
                          }
                        />
                      </div>
                    </Show>

                    <Show when={turn.streaming && !turn.content}>
                      <div class="cop-thinking">Crawling & synthesizing…</div>
                    </Show>

                    <Show when={turn.error}>
                      {(errMsg) => <div class="cop-error">{errMsg()}</div>}
                    </Show>

                    {/* Staged Diff Proposals */}
                    <For each={turn.proposals}>
                      {(prop) => (
                        <div class={`cop-proposal-card ${prop.applied ? "is-applied" : ""}`}>
                          <div class="cop-prop-head">
                            <span class="cop-prop-title">
                              {prop.kind === "create" ? "New Note Draft" : "Note Edit"} ·{" "}
                              {prop.summary}
                            </span>
                            <span class="cop-prop-stats mono">
                              <span class="stat-add">+{prop.added_lines}</span>{" "}
                              <span class="stat-del">−{prop.removed_lines}</span>
                            </span>
                          </div>

                          <DiffLines diff={prop.unified_diff} />

                          <div class="cop-prop-foot">
                            <Show
                              when={!prop.applied}
                              fallback={
                                <>
                                  <span class="cop-applied-badge">
                                    <Ic.CheckIcon size={12} /> Applied
                                  </span>
                                  <Show when={prop.kind === "edit" && prop.target_uri}>
                                    <button
                                      type="button"
                                      class="dlg-button dlg-button-secondary cop-btn-sm"
                                      onClick={() =>
                                        props.onRestoreMarkdown(
                                          prop.target_uri!,
                                          prop.before_markdown,
                                        )
                                      }
                                    >
                                      Undo
                                    </button>
                                  </Show>
                                </>
                              }
                            >
                              <button
                                type="button"
                                class="dlg-button dlg-button-primary cop-btn-sm"
                                disabled={applyingId() === prop.proposal_id}
                                onClick={() => void handleApplyProposal(prop)}
                              >
                                <Ic.CheckIcon size={12} />
                                <span>
                                  {prop.kind === "create" ? "Create & Open Note" : "Apply Edit"}
                                </span>
                              </button>
                            </Show>
                          </div>
                        </div>
                      )}
                    </For>
                  </Show>
                </div>
              )}
            </For>
          </Show>
        </div>

        {/* Composer */}
        <div class="cop-composer">
          <Show when={props.selection.trim().length > 0}>
            <div class="cop-selection-chip">
              <span>Selection · {props.selection.trim().length} chars</span>
              <button type="button" onClick={props.onClearSelection} aria-label="Clear selection">
                ×
              </button>
            </div>
          </Show>

          <div class="cop-input-box">
            <textarea
              ref={inputRef}
              class="cop-textarea"
              rows={2}
              placeholder="Ask across vault + Hindsight, or ask to edit this note…"
              value={input()}
              onInput={(e) => setInput(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.isComposing) return;
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void submitQuery();
                } else if (e.key === "Escape") {
                  if (activeReqId()) {
                    e.preventDefault();
                    handleStop();
                  } else {
                    props.onClose();
                  }
                }
              }}
            />
            <Show
              when={!activeReqId()}
              fallback={
                <button
                  type="button"
                  class="cop-send-btn is-stop"
                  title="Stop generation (Esc)"
                  onClick={handleStop}
                >
                  <Ic.StopIcon />
                </button>
              }
            >
              <button
                type="button"
                class="cop-send-btn"
                disabled={!input().trim()}
                title="Send (↵)"
                onClick={() => void submitQuery()}
              >
                <Ic.SendIcon />
              </button>
            </Show>
          </div>
        </div>
      </aside>
    </Show>
  );
}
