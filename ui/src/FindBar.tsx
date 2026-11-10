import { Show, createEffect, createSignal, onCleanup, onMount } from "solid-js";
import {
  CaseSensitiveIcon,
  CloseIcon,
  FindNextIcon,
  FindPrevIcon,
  SearchIcon,
  WholeWordIcon,
} from "./icons";
import { hasPrimaryModifier, isFindShortcut } from "./keyboardShortcuts";
import type { ViewMode } from "./types";

interface ReaderMatch {
  kind: "reader";
  range: Range;
}

interface EditorMatch {
  kind: "editor";
  start: number;
  end: number;
}

type DocMatch = ReaderMatch | EditorMatch;

function escapeRegExp(raw: string): string {
  return raw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function buildPattern(query: string, caseSensitive: boolean, wholeWord: boolean): RegExp | null {
  if (!query) return null;
  const escaped = escapeRegExp(query);
  const body = wholeWord ? `\\b${escaped}\\b` : escaped;
  try {
    return new RegExp(body, caseSensitive ? "g" : "gi");
  } catch {
    return null;
  }
}

function collectReaderRanges(root: HTMLElement, re: RegExp): ReaderMatch[] {
  const out: ReaderMatch[] = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement;
      if (!parent) return NodeFilter.FILTER_REJECT;
      if (parent.closest(".equation-popover, .katex-mathml, script, style")) {
        return NodeFilter.FILTER_REJECT;
      }
      return node.nodeValue ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
    },
  });

  let current = walker.nextNode();
  while (current && out.length < 1500) {
    const text = current.nodeValue || "";
    re.lastIndex = 0;
    for (const m of text.matchAll(re)) {
      if (!m[0]) continue;
      const start = m.index ?? 0;
      const r = document.createRange();
      r.setStart(current, start);
      r.setEnd(current, start + m[0].length);
      out.push({ kind: "reader", range: r });
      if (out.length >= 1500) break;
    }
    current = walker.nextNode();
  }
  return out;
}

function collectEditorMatches(text: string, re: RegExp): EditorMatch[] {
  const out: EditorMatch[] = [];
  re.lastIndex = 0;
  for (const m of text.matchAll(re)) {
    if (!m[0]) continue;
    const start = m.index ?? 0;
    out.push({ kind: "editor", start, end: start + m[0].length });
    if (out.length >= 1500) break;
  }
  return out;
}

export function FindBar(props: {
  activeDocKey: string | null;
  viewMode: ViewMode;
  hasModal: boolean;
}) {
  const [open, setOpen] = createSignal(false);
  const [query, setQuery] = createSignal("");
  const [sensitive, setSensitive] = createSignal(false);
  const [whole, setWhole] = createSignal(false);
  const [matches, setMatches] = createSignal<DocMatch[]>([]);
  const [index, setIndex] = createSignal(0);

  let inputEl: HTMLInputElement | undefined;
  let previousFocus: HTMLElement | null = null;

  const highlightRegistry = () =>
    (globalThis.CSS as unknown as { highlights?: Map<string, unknown> } | undefined)?.highlights;

  const clearHighlights = () => {
    highlightRegistry()?.delete("folio-find");
    highlightRegistry()?.delete("folio-find-current");
  };

  const paintHighlights = (list: DocMatch[], activeIdx: number) => {
    clearHighlights();
    const HighlightCtor = (window as unknown as { Highlight?: new (...r: Range[]) => unknown })
      .Highlight;
    const reg = highlightRegistry();
    if (!HighlightCtor || !reg) return;
    const readerRanges = list
      .filter((m): m is ReaderMatch => m.kind === "reader")
      .map((m) => m.range);
    if (!readerRanges.length) return;
    reg.set("folio-find", new HighlightCtor(...readerRanges));
    const current = list[activeIdx];
    if (current?.kind === "reader") {
      reg.set("folio-find-current", new HighlightCtor(current.range));
    }
  };

  const revealMatch = (m: DocMatch | undefined, focusEditor = false) => {
    if (!m) return;
    if (m.kind === "reader") {
      const container = document.querySelector<HTMLElement>(".reader-pane");
      const rect = m.range.getBoundingClientRect();
      if (container && (rect.width || rect.height)) {
        const box = container.getBoundingClientRect();
        if (rect.top < box.top + 56 || rect.bottom > box.bottom - 32) {
          container.scrollTop = Math.max(
            0,
            container.scrollTop + (rect.top - (box.top + box.height * 0.35)),
          );
        }
      }
    } else {
      const editor = document.querySelector<HTMLTextAreaElement>(".markdown-editor");
      if (editor) {
        if (focusEditor) editor.focus({ preventScroll: true });
        editor.setSelectionRange(m.start, m.end);
      }
    }
  };

  const recompute = (preserveIndex = false) => {
    if (!open()) {
      clearHighlights();
      return;
    }
    const re = buildPattern(query(), sensitive(), whole());
    if (!re) {
      setMatches([]);
      setIndex(0);
      clearHighlights();
      return;
    }

    let found: DocMatch[] = [];
    const readerRoot = document.querySelector<HTMLElement>(".reader-pane .markdown-body");
    const editorEl = document.querySelector<HTMLTextAreaElement>(".markdown-editor");

    if (props.viewMode !== "source" && readerRoot) {
      found = collectReaderRanges(readerRoot, re);
    } else if (editorEl) {
      found = collectEditorMatches(editorEl.value, re);
    }

    const nextIdx =
      preserveIndex && found.length > 0 ? Math.min(index(), found.length - 1) : 0;
    setMatches(found);
    setIndex(nextIdx);
    paintHighlights(found, nextIdx);
    revealMatch(found[nextIdx], false);
  };

  const step = (delta: 1 | -1) => {
    const list = matches();
    if (!list.length) return;
    const next = (index() + delta + list.length) % list.length;
    setIndex(next);
    paintHighlights(list, next);
    revealMatch(list[next], false);
  };

  const close = () => {
    setOpen(false);
    clearHighlights();
    if (previousFocus && previousFocus.isConnected) {
      previousFocus.focus({ preventScroll: true });
    }
    previousFocus = null;
  };

  const openFind = () => {
    if (!props.activeDocKey) return;
    if (!open()) {
      previousFocus = document.activeElement as HTMLElement | null;
      const sel = window.getSelection?.()?.toString().trim();
      if (sel && sel.length <= 160 && !/[\r\n]/.test(sel)) {
        setQuery(sel);
      }
      setOpen(true);
    }
    queueMicrotask(() => {
      recompute(false);
      inputEl?.focus({ preventScroll: true });
      inputEl?.select();
    });
  };

  createEffect(() => {
    void props.activeDocKey;
    void props.viewMode;
    void query();
    void sensitive();
    void whole();
    if (open()) {
      queueMicrotask(() => recompute(false));
    }
  });

  onMount(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.isComposing || props.hasModal) return;

      if (isFindShortcut(e)) {
        if (document.getElementById("folio-sidebar")?.contains(document.activeElement)) {
          return;
        }
        if (!props.activeDocKey) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        openFind();
        return;
      }

      if (!open()) return;

      if (e.key === "Escape") {
        e.preventDefault();
        e.stopImmediatePropagation();
        close();
        return;
      }

      if ((hasPrimaryModifier(e) && !e.altKey && e.key.toLowerCase() === "g") || e.key === "F3") {
        e.preventDefault();
        e.stopImmediatePropagation();
        step(e.shiftKey ? -1 : 1);
        return;
      }

      const optionShortcut = !e.ctrlKey && !e.shiftKey && e.altKey;
      const lower = e.key.toLowerCase();
      if (optionShortcut && (e.code === "KeyC" || lower === "c")) {
        e.preventDefault();
        e.stopImmediatePropagation();
        setSensitive((v) => !v);
        return;
      }
      if (optionShortcut && (e.code === "KeyW" || lower === "w")) {
        e.preventDefault();
        e.stopImmediatePropagation();
        setWhole((v) => !v);
      }
    };

    window.addEventListener("keydown", onKeyDown, true);
    onCleanup(() => {
      window.removeEventListener("keydown", onKeyDown, true);
      clearHighlights();
    });
  });

  const countText = () => {
    if (!query()) return "";
    return matches().length > 0 ? `${index() + 1} / ${matches().length}` : "No results";
  };

  return (
    <Show when={open()}>
      <div
        class="find-bar"
        data-testid="find-bar"
        role="search"
        aria-label="Find in note"
        onClick={(e) => e.stopPropagation()}
      >
        <div class="find-field">
          <SearchIcon size={13} class="find-icon" />
          <input
            ref={inputEl}
            type="text"
            autocapitalize="off"
            autocorrect="off"
            spellcheck={false}
            autocomplete="off"
            aria-label="Find in note"
            placeholder="Find"
            value={query()}
            onInput={(e) => setQuery(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (!e.isComposing && e.key === "Enter") {
                e.preventDefault();
                step(e.shiftKey ? -1 : 1);
              }
            }}
          />
          <button
            type="button"
            class="find-toggle"
            title="Match Case (⌥⌘C)"
            aria-label="Match case"
            aria-pressed={sensitive()}
            onClick={() => {
              setSensitive((v) => !v);
              inputEl?.focus({ preventScroll: true });
            }}
          >
            <CaseSensitiveIcon size={15} />
          </button>
          <button
            type="button"
            class="find-toggle"
            title="Match Whole Word (⌥⌘W)"
            aria-label="Whole word"
            aria-pressed={whole()}
            onClick={() => {
              setWhole((v) => !v);
              inputEl?.focus({ preventScroll: true });
            }}
          >
            <WholeWordIcon size={15} />
          </button>
        </div>

        <span
          class="find-count"
          classList={{ "is-empty": Boolean(query() && !matches().length) }}
          aria-live="polite"
        >
          {countText()}
        </span>

        <div class="find-actions">
          <button
            type="button"
            class="find-nav"
            title="Previous Match (⇧↵ / ⌘⇧G)"
            aria-label="Previous match"
            disabled={!matches().length}
            onClick={() => step(-1)}
          >
            <FindPrevIcon size={14} />
          </button>
          <button
            type="button"
            class="find-nav"
            title="Next Match (↵ / ⌘G)"
            aria-label="Next match"
            disabled={!matches().length}
            onClick={() => step(1)}
          >
            <FindNextIcon size={14} />
          </button>
          <button
            type="button"
            class="find-close"
            title="Close (Esc)"
            aria-label="Close search"
            onClick={close}
          >
            <CloseIcon size={14} />
          </button>
        </div>
      </div>
    </Show>
  );
}
