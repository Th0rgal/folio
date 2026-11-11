import { For, Show, createMemo, createSignal, onCleanup } from "solid-js";
import * as Ic from "./icons";
import { hasPrimaryModifier } from "./keyboardShortcuts";
import { rpcInvoke } from "./rpc";
import type { LinkCandidate } from "./types";

interface SlashItem {
  id: string;
  label: string;
  hint: string;
  insert?: string;
  cursorOffset?: number;
  action?: "ask_copilot";
}

const SLASH_COMMANDS: SlashItem[] = [
  {
    id: "math",
    label: "/math",
    hint: "Display KaTeX equation block",
    insert: "$$\n\n$$",
    cursorOffset: 3,
  },
  {
    id: "task",
    label: "/task",
    hint: "Interactive checklist item",
    insert: "- [ ] ",
  },
  {
    id: "callout",
    label: "/callout",
    hint: "Obsidian-style callout card",
    insert: "> [!tip] Key Insight\n> ",
  },
  {
    id: "lean",
    label: "/lean",
    hint: "Lean 4 theorem / specification block",
    insert: "```lean\n\n```",
    cursorOffset: 8,
  },
  {
    id: "mermaid",
    label: "/mermaid",
    hint: "Fluid zoomable Mermaid flowchart",
    insert: '```mermaid\nflowchart LR\n  A["Input"] --> B["Output"]\n```',
  },
  {
    id: "table",
    label: "/table",
    hint: "3-column Markdown table",
    insert: "| Column 1 | Column 2 | Column 3 |\n| :--- | :--- | ---: |\n|  |  |  |",
    cursorOffset: 62,
  },
  {
    id: "ask",
    label: "/ask",
    hint: "Ask Orb Knowledge Copilot on this note",
    action: "ask_copilot",
  },
];

export function EditorPane(props: {
  value: string;
  readonly: boolean;
  onChange: (nextValue: string, immediate?: boolean) => void;
  onSelectionChange?: (selectedText: string) => void;
  onAskCopilot?: (selectedText: string) => void;
  onScrollRatio?: (ratio: number) => void;
}) {
  let textareaRef: HTMLTextAreaElement | undefined;
  let composing = false;
  let linkTimer: number | undefined;

  const [wikilinkState, setWikilinkState] = createSignal<{
    open: boolean;
    startPos: number;
    query: string;
    items: LinkCandidate[];
    idx: number;
  }>({ open: false, startPos: 0, query: "", items: [], idx: 0 });

  const [slashState, setSlashState] = createSignal<{
    open: boolean;
    startPos: number;
    query: string;
    idx: number;
  }>({ open: false, startPos: 0, query: "", idx: 0 });

  onCleanup(() => {
    if (linkTimer) window.clearTimeout(linkTimer);
  });

  const filteredSlash = createMemo(() => {
    const q = slashState().query.toLowerCase();
    if (!q) return SLASH_COMMANDS;
    return SLASH_COMMANDS.filter(
      (c) => c.id.startsWith(q) || c.label.slice(1).includes(q) || c.hint.toLowerCase().includes(q),
    );
  });

  const wordStats = createMemo(() => {
    const text = props.value.trim();
    if (!text) return { words: 0, lines: 1 };
    const words = text.split(/\s+/).length;
    const lines = props.value.split("\n").length;
    return { words, lines };
  });

  const replaceRange = (start: number, end: number, inserted: string, selStartOffset?: number, selEndOffset?: number) => {
    const ta = textareaRef;
    if (!ta || props.readonly) return;
    const cur = ta.value;
    const next = cur.slice(0, start) + inserted + cur.slice(end);
    props.onChange(next);
    const nextCursorStart = start + (selStartOffset ?? inserted.length);
    const nextCursorEnd = start + (selEndOffset ?? nextCursorStart - start);
    queueMicrotask(() => {
      if (!textareaRef) return;
      textareaRef.focus();
      textareaRef.setSelectionRange(nextCursorStart, nextCursorEnd);
      emitSelection();
    });
  };

  const wrapSelection = (before: string, after: string, placeholder = "") => {
    const ta = textareaRef;
    if (!ta || props.readonly) return;
    const s = ta.selectionStart ?? 0;
    const e = ta.selectionEnd ?? s;
    const selected = ta.value.slice(s, e) || placeholder;
    const inserted = `${before}${selected}${after}`;
    replaceRange(s, e, inserted, before.length, before.length + selected.length);
  };

  const emitSelection = () => {
    const ta = textareaRef;
    if (!ta || !props.onSelectionChange) return;
    const s = ta.selectionStart ?? 0;
    const e = ta.selectionEnd ?? s;
    props.onSelectionChange(e > s ? ta.value.slice(s, e) : "");
  };

  const detectTriggers = () => {
    const ta = textareaRef;
    if (!ta || props.readonly) return;
    const pos = ta.selectionStart ?? 0;
    if (pos !== (ta.selectionEnd ?? pos)) {
      setWikilinkState((s) => (s.open ? { ...s, open: false } : s));
      setSlashState((s) => (s.open ? { ...s, open: false } : s));
      return;
    }

    const before = ta.value.slice(Math.max(0, pos - 64), pos);
    const wlMatch = /\[\[([^[\]\n]{0,48})$/.exec(before);
    if (wlMatch) {
      const q = wlMatch[1];
      const triggerStart = pos - wlMatch[0].length;
      setSlashState((s) => (s.open ? { ...s, open: false } : s));
      if (linkTimer) window.clearTimeout(linkTimer);
      linkTimer = window.setTimeout(() => {
        void rpcInvoke<{ items: LinkCandidate[] }>("list_link_candidates", {
          query: q,
          limit: 12,
        })
          .then((res) => {
            setWikilinkState({
              open: true,
              startPos: triggerStart,
              query: q,
              items: res.items || [],
              idx: 0,
            });
          })
          .catch(() => undefined);
      }, 45);
      return;
    }
    if (wikilinkState().open) {
      setWikilinkState((s) => ({ ...s, open: false }));
    }

    const lineStart = ta.value.lastIndexOf("\n", pos - 1) + 1;
    const linePrefix = ta.value.slice(lineStart, pos);
    const slMatch = /^(?:\s*)\/([a-zA-Z]{0,14})$/.exec(linePrefix);
    if (slMatch) {
      const slashPos = pos - slMatch[1].length - 1;
      setSlashState({
        open: true,
        startPos: slashPos,
        query: slMatch[1],
        idx: 0,
      });
      return;
    }
    if (slashState().open) {
      setSlashState((s) => ({ ...s, open: false }));
    }
  };

  const commitWikilink = (cand: LinkCandidate) => {
    const ta = textareaRef;
    if (!ta) return;
    const st = wikilinkState();
    const pos = ta.selectionStart ?? st.startPos;
    const target =
      cand.provider === "hindsight" && cand.external_id.startsWith("kp-")
        ? `${cand.external_id}|${cand.title}`
        : cand.title;
    const afterChar = ta.value.slice(pos, pos + 2) === "]]" ? pos + 2 : pos;
    setWikilinkState((s) => ({ ...s, open: false }));
    replaceRange(st.startPos, afterChar, `[[${target}]]`);
  };

  const commitSlash = (item: SlashItem) => {
    const ta = textareaRef;
    if (!ta) return;
    const st = slashState();
    const pos = ta.selectionStart ?? st.startPos;
    setSlashState((s) => ({ ...s, open: false }));
    if (item.action === "ask_copilot") {
      replaceRange(st.startPos, pos, "");
      props.onAskCopilot?.("");
      return;
    }
    const ins = item.insert || "";
    replaceRange(st.startPos, pos, ins, item.cursorOffset ?? ins.length);
  };

  const handleKeyDown = (e: KeyboardEvent) => {
    if (e.isComposing || props.readonly) return;
    const ta = textareaRef;
    if (!ta) return;

    // 1. Active `[[` Wikilink menu navigation
    if (wikilinkState().open && wikilinkState().items.length > 0) {
      const list = wikilinkState().items;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setWikilinkState((s) => ({ ...s, idx: (s.idx + 1) % list.length }));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setWikilinkState((s) => ({ ...s, idx: (s.idx - 1 + list.length) % list.length }));
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        const picked = list[wikilinkState().idx];
        if (picked) commitWikilink(picked);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        setWikilinkState((s) => ({ ...s, open: false }));
        return;
      }
    }

    // 2. Active `/` Slash command menu navigation
    if (slashState().open && filteredSlash().length > 0) {
      const list = filteredSlash();
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashState((s) => ({ ...s, idx: (s.idx + 1) % list.length }));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashState((s) => ({ ...s, idx: (s.idx - 1 + list.length) % list.length }));
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        const picked = list[slashState().idx];
        if (picked) commitSlash(picked);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        setSlashState((s) => ({ ...s, open: false }));
        return;
      }
    }

    // 3. Cmd+I italic & Cmd+Shift+M inline math
    if (hasPrimaryModifier(e) && !e.altKey) {
      if (!e.shiftKey && (e.code === "KeyI" || e.key.toLowerCase() === "i")) {
        e.preventDefault();
        wrapSelection("*", "*", "emphasis");
        return;
      }
      if (e.shiftKey && (e.code === "KeyM" || e.key.toLowerCase() === "m")) {
        e.preventDefault();
        wrapSelection("$", "$", "x");
        return;
      }
    }

    const selStart = ta.selectionStart ?? 0;
    const selEnd = ta.selectionEnd ?? selStart;
    const hasSelection = selEnd > selStart;

    // 4. Smart delimiter wrapping when text is selected
    if (hasSelection && !e.metaKey && !e.ctrlKey && !e.altKey) {
      const pairs: Record<string, [string, string]> = {
        "*": ["*", "*"],
        "`": ["`", "`"],
        $: ["$", "$"],
        "[": ["[", "]"],
        "(": ["(", ")"],
      };
      if (pairs[e.key]) {
        e.preventDefault();
        const [l, r] = pairs[e.key];
        wrapSelection(l, r);
        return;
      }
    }

    // 5. Tab / Shift+Tab 2-space indent & outdent
    if (e.key === "Tab" && !e.metaKey && !e.ctrlKey && !e.altKey) {
      e.preventDefault();
      const val = ta.value;
      const lineStart = val.lastIndexOf("\n", selStart - 1) + 1;
      const block = val.slice(lineStart, selEnd);
      if (!hasSelection && !e.shiftKey) {
        replaceRange(selStart, selEnd, "  ");
        return;
      }
      const lines = block.split("\n");
      const transformed = lines.map((ln) =>
        e.shiftKey ? ln.replace(/^ {1,2}|\t/, "") : `  ${ln}`,
      );
      const joined = transformed.join("\n");
      replaceRange(lineStart, selEnd, joined, 0, joined.length);
      return;
    }

    // 6. Smart Enter list / checklist / blockquote continuation
    if (e.key === "Enter" && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey && !hasSelection) {
      const val = ta.value;
      const lineStart = val.lastIndexOf("\n", selStart - 1) + 1;
      const curLine = val.slice(lineStart, selStart);

      const taskMatch = /^([ \t]*[-*+]\s+)\[[ xX]\]\s*(.*)$/.exec(curLine);
      if (taskMatch) {
        e.preventDefault();
        if (!taskMatch[2].trim()) {
          replaceRange(lineStart, selStart, "");
        } else {
          replaceRange(selStart, selEnd, `\n${taskMatch[1]}[ ] `);
        }
        return;
      }

      const bulletMatch = /^([ \t]*[-*+]\s+)(.*)$/.exec(curLine);
      if (bulletMatch) {
        e.preventDefault();
        if (!bulletMatch[2].trim()) {
          replaceRange(lineStart, selStart, "");
        } else {
          replaceRange(selStart, selEnd, `\n${bulletMatch[1]}`);
        }
        return;
      }

      const numMatch = /^([ \t]*)(\d+)\.\s+(.*)$/.exec(curLine);
      if (numMatch) {
        e.preventDefault();
        if (!numMatch[3].trim()) {
          replaceRange(lineStart, selStart, "");
        } else {
          replaceRange(selStart, selEnd, `\n${numMatch[1]}${Number(numMatch[2]) + 1}. `);
        }
        return;
      }

      const quoteMatch = /^([ \t]*>\s?)(.*)$/.exec(curLine);
      if (quoteMatch && !quoteMatch[2].startsWith("[!")) {
        e.preventDefault();
        if (!quoteMatch[2].trim()) {
          replaceRange(lineStart, selStart, "");
        } else {
          replaceRange(selStart, selEnd, `\n${quoteMatch[1]}`);
        }
      }
    }
  };

  return (
    <section class="source-pane" onClick={(e) => e.stopPropagation()}>
      <div class="pane-subbar editor-toolbar">
        <Show
          when={!props.readonly}
          fallback={
            <>
              <span>Markdown + LaTeX</span>
              <span>Read-only remote document</span>
            </>
          }
        >
          <div class="editor-fmt-group">
            <button
              type="button"
              class="fmt-btn"
              title="Section heading"
              onClick={() => wrapSelection("## ", "", "Section")}
            >
              H2
            </button>
            <button
              type="button"
              class="fmt-btn fmt-bold"
              title="Bold"
              onClick={() => wrapSelection("**", "**", "bold")}
            >
              B
            </button>
            <button
              type="button"
              class="fmt-btn fmt-italic"
              title="Italic (⌘I)"
              onClick={() => wrapSelection("*", "*", "italic")}
            >
              I
            </button>
            <button
              type="button"
              class="fmt-btn mono"
              title="Inline code"
              onClick={() => wrapSelection("`", "`", "code")}
            >
              {"<>"}
            </button>
            <span class="fmt-sep" />
            <button
              type="button"
              class="fmt-btn mono"
              title="Inline KaTeX ($...$)"
              onClick={() => wrapSelection("$", "$", "\\theta")}
            >
              $x$
            </button>
            <button
              type="button"
              class="fmt-btn mono"
              title="Display KaTeX block ($$...$$)"
              onClick={() => wrapSelection("\n$$\n", "\n$$\n", "f(x) = \\int_0^1 g(t)\\,\\mathrm{d}t")}
            >
              $$
            </button>
            <button
              type="button"
              class="fmt-btn mono"
              title="Insert [[Wikilink]] or [[kp-...]]"
              onClick={() => {
                wrapSelection("[[", "]]", "");
                queueMicrotask(detectTriggers);
              }}
            >
              [[ ]]
            </button>
            <button
              type="button"
              class="fmt-btn"
              title="Checklist task"
              onClick={() => wrapSelection("- [ ] ", "", "Task")}
            >
              ☑
            </button>
            <Show when={props.onAskCopilot}>
              <button
                type="button"
                class="fmt-btn fmt-copilot"
                title="Ask Orb Copilot about selection or note (⌘J)"
                onClick={() => {
                  const ta = textareaRef;
                  const sel =
                    ta && (ta.selectionEnd ?? 0) > (ta.selectionStart ?? 0)
                      ? ta.value.slice(ta.selectionStart, ta.selectionEnd)
                      : "";
                  props.onAskCopilot?.(sel);
                }}
              >
                <Ic.SparklesIcon size={12} />
                <span>Ask</span>
              </button>
            </Show>
          </div>
          <span class="editor-stats">
            {wordStats().words}w · {wordStats().lines}L · <kbd>/</kbd> commands
          </span>
        </Show>
      </div>

      <div class="editor-body-wrap">
        <textarea
          ref={textareaRef}
          class="markdown-editor"
          data-testid="markdown-source-editor"
          spellcheck={false}
          readOnly={props.readonly}
          value={props.value}
          onKeyDown={handleKeyDown}
          onSelect={emitSelection}
          onKeyUp={emitSelection}
          onScroll={(e) => {
            if (!props.onScrollRatio) return;
            const el = e.currentTarget;
            const max = el.scrollHeight - el.clientHeight;
            props.onScrollRatio(max > 4 ? el.scrollTop / max : 0);
          }}
          onCompositionStart={() => {
            composing = true;
          }}
          onCompositionEnd={(e) => {
            composing = false;
            props.onChange(e.currentTarget.value);
            detectTriggers();
          }}
          onInput={(e) => {
            if (e.isComposing || composing) {
              props.onChange(e.currentTarget.value, false);
              return;
            }
            props.onChange(e.currentTarget.value);
            detectTriggers();
          }}
        />

        {/* Live `[[` Wikilink & Hindsight KP Autocomplete Popover */}
        <Show when={wikilinkState().open && wikilinkState().items.length > 0}>
          <div class="editor-suggest-popover" data-testid="wikilink-popover">
            <div class="editor-suggest-head">Link Note or Hindsight Page (↵ / Tab)</div>
            <For each={wikilinkState().items}>
              {(cand, idx) => (
                <button
                  type="button"
                  class={`editor-suggest-row ${idx() === wikilinkState().idx ? "selected" : ""}`}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    commitWikilink(cand);
                  }}
                >
                  <span class={`provider-pill p-${cand.provider} ${cand.is_agent_summary ? "agent" : "human"}`}>
                    {cand.provider === "local-fs" ? "Local" : cand.provider === "hindsight" ? "KP" : "Notion"}
                  </span>
                  <span class="editor-suggest-title">{cand.title}</span>
                  <Show when={cand.folder_path}>
                    <span class="editor-suggest-meta">{cand.folder_path}</span>
                  </Show>
                </button>
              )}
            </For>
          </div>
        </Show>

        {/* Live `/` Slash Command Popover */}
        <Show when={slashState().open && filteredSlash().length > 0}>
          <div class="editor-suggest-popover" data-testid="slash-popover">
            <div class="editor-suggest-head">Insert Block (↵ / Tab)</div>
            <For each={filteredSlash()}>
              {(cmd, idx) => (
                <button
                  type="button"
                  class={`editor-suggest-row ${idx() === slashState().idx ? "selected" : ""}`}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    commitSlash(cmd);
                  }}
                >
                  <code class="slash-cmd-pill">{cmd.label}</code>
                  <span class="editor-suggest-title">{cmd.hint}</span>
                </button>
              )}
            </For>
          </div>
        </Show>
      </div>
    </section>
  );
}
