import { For, type JSX, Show, createEffect, createSignal, onCleanup } from "solid-js";
import * as Ic from "./icons";
import { rpcInvoke } from "./rpc";
import type { SearchHit } from "./types";

function renderSafeMarks(snippet: string): JSX.Element[] {
  const parts = snippet.split(/(<mark>.*?<\/mark>)/g);
  return parts.map((seg) => {
    if (seg.startsWith("<mark>") && seg.endsWith("</mark>")) {
      return <mark class="fts-mark">{seg.slice(6, -7)}</mark>;
    }
    return <>{seg.replace(/<\/?mark>/g, "")}</>;
  });
}

export function SearchModal(props: {
  open: boolean;
  initialQuery?: string;
  activeBank: string;
  syncBank?: string;
  banks?: Array<{ bank_id: string }>;
  onSelectUri: (uri: string) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = createSignal(props.initialQuery || "");
  const [results, setResults] = createSignal<SearchHit[]>([]);
  const [selectedIndex, setSelectedIndex] = createSignal(0);
  const [loading, setLoading] = createSignal(false);
  const [includeHindsight, setIncludeHindsight] = createSignal(true);
  const [allBanks, setAllBanks] = createSignal(false);
  let inputRef: HTMLInputElement | undefined;
  let searchEpoch = 0;

  const targetBanks = () => {
    if (allBanks() && props.banks?.length) {
      return props.banks
        .map((b) => b.bank_id)
        .filter((id) => id && id.toLowerCase() !== "spark-memory-bank");
    }
    const sBank = props.syncBank || "folio";
    return Array.from(new Set([props.activeBank, sBank]));
  };

  const runProgressiveSearch = async (q: string, withHs: boolean) => {
    const myEpoch = ++searchEpoch;
    setLoading(true);
    try {
      // Phase 1: Instant (<3ms) local SQLite FTS5 + cached Hindsight/Notion hits
      const localResp = await rpcInvoke<{ results: SearchHit[] }>("unified_search", {
        query: q,
        bank_id: props.activeBank,
        include_hindsight: withHs,
        include_notion: true,
        local_only: true,
        banks: targetBanks(),
        limit: 24,
      });
      if (myEpoch !== searchEpoch) return;
      setResults(localResp.results || []);
      setSelectedIndex(0);

      // Phase 2: Live parallel Hindsight semantic + KB recall if query is non-empty
      if (!withHs || !q.trim()) {
        setLoading(false);
        return;
      }
      const fullResp = await rpcInvoke<{ results: SearchHit[] }>("unified_search", {
        query: q,
        bank_id: props.activeBank,
        include_hindsight: true,
        include_notion: true,
        local_only: false,
        banks: targetBanks(),
        limit: 24,
      });
      if (myEpoch !== searchEpoch) return;
      setResults(fullResp.results || []);
    } catch {
      if (myEpoch === searchEpoch) setResults([]);
    } finally {
      if (myEpoch === searchEpoch) setLoading(false);
    }
  };

  createEffect(() => {
    if (!props.open) return;
    if (props.initialQuery !== undefined && props.initialQuery !== "") {
      setQuery(props.initialQuery);
    }
    queueMicrotask(() => {
      inputRef?.focus();
      inputRef?.select();
    });
    void runProgressiveSearch(query(), includeHindsight());
  });

  createEffect(() => {
    if (!props.open) return;
    const q = query();
    const hs = includeHindsight();
    const _all = allBanks();
    void _all;
    const timer = window.setTimeout(() => {
      void runProgressiveSearch(q, hs);
    }, 55);
    onCleanup(() => window.clearTimeout(timer));
  });

  return (
    <Show when={props.open}>
      <div
        class="sd-backdrop sd-backdrop-dim"
        data-testid="search-modal"
        onClick={() => props.onClose()}
      >
        <div class="sd-dialog" onClick={(e) => e.stopPropagation()}>
          <div class="sd-header">
            <div class="sd-searchbar">
              <span class="sd-search-ico">
                <Ic.SearchIcon size={15} />
              </span>
              <input
                ref={inputRef}
                type="text"
                class="sd-input"
                data-testid="search-input"
                placeholder={`Search local Markdown + Hindsight (${
                  allBanks() ? "all banks" : targetBanks().join(" + ")
                })…`}
                value={query()}
                onInput={(e) => setQuery(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.isComposing) return;
                  const list = results();
                  if (e.key === "Escape") {
                    e.preventDefault();
                    props.onClose();
                  } else if (e.key === "ArrowDown") {
                    e.preventDefault();
                    if (list.length > 0) setSelectedIndex((i) => (i + 1) % list.length);
                  } else if (e.key === "ArrowUp") {
                    e.preventDefault();
                    if (list.length > 0)
                      setSelectedIndex((i) => (i - 1 + list.length) % list.length);
                  } else if (e.key === "Enter") {
                    e.preventDefault();
                    const hit = list[selectedIndex()];
                    if (hit) {
                      props.onSelectUri(hit.uri);
                      props.onClose();
                    }
                  }
                }}
              />
              <button
                type="button"
                class={`sd-tab ${includeHindsight() ? "is-active" : ""}`}
                onClick={() => setIncludeHindsight((v) => !v)}
                title="Toggle live Hindsight semantic recall"
              >
                Hindsight
              </button>
              <Show when={includeHindsight()}>
                <button
                  type="button"
                  class={`sd-tab ${allBanks() ? "is-active" : ""}`}
                  onClick={() => setAllBanks((v) => !v)}
                  title="Recall across every Hindsight memory bank"
                >
                  All Banks
                </button>
              </Show>
              <button type="button" class="sd-esc-btn" onClick={() => props.onClose()}>
                ESC
              </button>
            </div>
          </div>

          <div class="search-results" data-testid="search-results">
            <Show
              when={results().length > 0}
              fallback={
                <div class="search-empty">
                  {loading()
                    ? "Searching SQLite FTS5 & Hindsight…"
                    : "No matching notes or Hindsight Knowledge Pages."}
                </div>
              }
            >
              <For each={results()}>
                {(hit, idx) => {
                  const providerLabel = () => {
                    if (hit.provider === "local-fs") return "Local";
                    if (hit.provider === "hindsight") {
                      const bankSuffix = hit.scope ? ` · ${hit.scope}` : "";
                      return hit.is_agent_summary
                        ? `KP${bankSuffix}`
                        : `Raw${bankSuffix}`;
                    }
                    return "Notion";
                  };
                  return (
                    <button
                      type="button"
                      class={`search-row ${idx() === selectedIndex() ? "selected" : ""}`}
                      onMouseEnter={() => setSelectedIndex(idx())}
                      onClick={() => {
                        props.onSelectUri(hit.uri);
                        props.onClose();
                      }}
                    >
                      <div class="search-row-top">
                        <span
                          class={`provider-pill p-${hit.provider} ${hit.is_agent_summary ? "agent" : "human"}`}
                        >
                          {providerLabel()}
                        </span>
                        <span class="search-row-title">{hit.title}</span>
                        <span class="search-row-author">
                          {hit.is_agent_summary
                            ? "Agent summary"
                            : `Human · ${hit.author || "thomas"}`}
                        </span>
                      </div>
                      <Show when={hit.snippet}>
                        <div class="search-row-snippet">{renderSafeMarks(hit.snippet)}</div>
                      </Show>
                    </button>
                  );
                }}
              </For>
            </Show>
          </div>

          <div class="search-foot">
            <span>↑↓ Navigate · ↵ Open · ⌘⇧F Progressive FTS5 + Hindsight Recall</span>
            <Show when={loading()}>
              <span class="search-status-pulse">Recalling Hindsight…</span>
            </Show>
          </div>
        </div>
      </div>
    </Show>
  );
}
