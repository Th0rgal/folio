import { For, Show, createEffect, createSignal, onCleanup } from "solid-js";
import * as Ic from "./icons";
import { rpcInvoke } from "./rpc";
import type { SearchHit } from "./types";

export function SearchModal(props: {
  open: boolean;
  initialQuery?: string;
  activeBank: string;
  onSelectUri: (uri: string) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = createSignal(props.initialQuery || "");
  const [results, setResults] = createSignal<SearchHit[]>([]);
  const [selectedIndex, setSelectedIndex] = createSignal(0);
  const [loading, setLoading] = createSignal(false);
  const [includeHindsight, setIncludeHindsight] = createSignal(true);
  let inputRef: HTMLInputElement | undefined;
  const stripFtsMarks = (s: string): string => s.replace(/<\/?mark>/g, "");

  const runSearch = async (q: string, withHs: boolean) => {
    setLoading(true);
    try {
      const resp = await rpcInvoke<{ results: SearchHit[] }>("unified_search", {
        query: q,
        bank_id: props.activeBank,
        include_hindsight: withHs,
        include_notion: true,
        limit: 20,
      });
      setResults(resp.results || []);
      setSelectedIndex(0);
    } catch {
      setResults([]);
    } finally {
      setLoading(false);
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
    void runSearch(query(), includeHindsight());
  });

  createEffect(() => {
    if (!props.open) return;
    const q = query();
    const hs = includeHindsight();
    const timer = window.setTimeout(() => {
      void runSearch(q, hs);
    }, 110);
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
                placeholder={`Search local Markdown + Hindsight (${props.activeBank})…`}
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
                Hindsight · {props.activeBank}
              </button>
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
                      return hit.is_agent_summary ? "Hindsight · KP" : "Hindsight · Raw";
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
                        <div class="search-row-snippet">{stripFtsMarks(hit.snippet)}</div>
                      </Show>
                    </button>
                  );
                }}
              </For>
            </Show>
          </div>

          <div class="search-foot">
            <span>↑↓ Navigate · ↵ Open · ⌘K Unified Search & Recall</span>
            <Show when={loading()}>
              <span class="search-status-pulse">Syncing…</span>
            </Show>
          </div>
        </div>
      </div>
    </Show>
  );
}
