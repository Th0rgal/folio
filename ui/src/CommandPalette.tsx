import { For, Show, createEffect, createMemo, createSignal } from "solid-js";
import * as Ic from "./icons";
import { rpcInvoke } from "./rpc";
import type { CopilotProfileId, DocumentRecord, LinkCandidate } from "./types";

export interface PaletteCommand {
  id: string;
  label: string;
  hint?: string;
  shortcut?: string;
  run: () => void;
}

export function CommandPalette(props: {
  open: boolean;
  activeDoc: DocumentRecord | null;
  syncBank: string;
  commands: PaletteCommand[];
  onSelectUri: (uri: string) => void;
  onAskCopilot: (profile: CopilotProfileId, prompt?: string) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = createSignal("");
  const [idx, setIdx] = createSignal(0);
  const [notes, setNotes] = createSignal<LinkCandidate[]>([]);
  let inputRef: HTMLInputElement | undefined;

  createEffect(() => {
    if (!props.open) return;
    setQuery("");
    setIdx(0);
    queueMicrotask(() => inputRef?.focus());
    void rpcInvoke<{ items: LinkCandidate[] }>("list_link_candidates", {
      query: "",
      limit: 16,
    })
      .then((r) => setNotes(r.items || []))
      .catch(() => undefined);
  });

  createEffect(() => {
    if (!props.open) return;
    const q = query().trim();
    const timer = window.setTimeout(() => {
      void rpcInvoke<{ items: LinkCandidate[] }>("list_link_candidates", {
        query: q,
        limit: 14,
      })
        .then((r) => setNotes(r.items || []))
        .catch(() => undefined);
    }, 40);
    return () => window.clearTimeout(timer);
  });

  const entries = createMemo(() => {
    const q = query().trim().toLowerCase();
    const matchedCmds = props.commands.filter(
      (c) =>
        !q ||
        c.label.toLowerCase().includes(q) ||
        (c.hint && c.hint.toLowerCase().includes(q)),
    );
    const out: Array<{
      key: string;
      badge: string;
      label: string;
      meta?: string;
      shortcut?: string;
      run: () => void;
    }> = [];

    for (const cmd of matchedCmds) {
      out.push({
        key: `cmd:${cmd.id}`,
        badge: "Action",
        label: cmd.label,
        meta: cmd.hint,
        shortcut: cmd.shortcut,
        run: cmd.run,
      });
    }

    for (const n of notes()) {
      out.push({
        key: `doc:${n.uri}`,
        badge: n.provider === "local-fs" ? "Note" : n.provider === "hindsight" ? "Hindsight" : "Notion",
        label: n.title,
        meta: n.folder_path || n.external_id,
        run: () => props.onSelectUri(n.uri),
      });
    }

    if (q.length >= 3) {
      out.push({
        key: "ask:smart",
        badge: "Copilot",
        label: `Ask Smart Copilot: “${query().trim()}”`,
        shortcut: "builtin/smart",
        run: () => props.onAskCopilot("builtin/smart", query().trim()),
      });
      out.push({
        key: "ask:fast",
        badge: "Copilot",
        label: `Ask Fast Copilot: “${query().trim()}”`,
        shortcut: "builtin/fast",
        run: () => props.onAskCopilot("builtin/fast", query().trim()),
      });
    }

    return out.slice(0, 22);
  });

  createEffect(() => {
    if (idx() >= entries().length) setIdx(0);
  });

  return (
    <Show when={props.open}>
      <div
        class="sd-backdrop sd-backdrop-dim"
        data-testid="command-palette"
        onClick={() => props.onClose()}
      >
        <div class="sd-dialog" onClick={(e) => e.stopPropagation()}>
          <div class="sd-header">
            <div class="sd-searchbar">
              <span class="sd-search-ico">
                <Ic.CommandIcon size={14.5} />
              </span>
              <input
                ref={inputRef}
                type="text"
                class="sd-input"
                placeholder="Type a command, jump to a note, or ask Copilot…"
                value={query()}
                onInput={(e) => {
                  setQuery(e.currentTarget.value);
                  setIdx(0);
                }}
                onKeyDown={(e) => {
                  if (e.isComposing) return;
                  const list = entries();
                  if (e.key === "Escape") {
                    e.preventDefault();
                    props.onClose();
                  } else if (e.key === "ArrowDown") {
                    e.preventDefault();
                    if (list.length > 0) setIdx((i) => (i + 1) % list.length);
                  } else if (e.key === "ArrowUp") {
                    e.preventDefault();
                    if (list.length > 0) setIdx((i) => (i - 1 + list.length) % list.length);
                  } else if (e.key === "Enter") {
                    e.preventDefault();
                    const item = list[idx()];
                    if (item) {
                      props.onClose();
                      item.run();
                    }
                  }
                }}
              />
              <button type="button" class="sd-esc-btn" onClick={() => props.onClose()}>
                ESC
              </button>
            </div>
          </div>

          <div class="search-results">
            <For each={entries()}>
              {(item, i) => (
                <button
                  type="button"
                  class={`search-row palette-row ${i() === idx() ? "selected" : ""}`}
                  onMouseEnter={() => setIdx(i())}
                  onClick={() => {
                    props.onClose();
                    item.run();
                  }}
                >
                  <div class="search-row-top">
                    <span class={`provider-pill ${item.badge === "Action" ? "p-action" : item.badge === "Copilot" ? "p-copilot" : "p-local-fs"}`}>
                      {item.badge}
                    </span>
                    <span class="search-row-title">{item.label}</span>
                    <Show when={item.meta}>
                      <span class="search-row-author">{item.meta}</span>
                    </Show>
                    <Show when={item.shortcut}>
                      <kbd class="palette-kbd">{item.shortcut}</kbd>
                    </Show>
                  </div>
                </button>
              )}
            </For>
          </div>

          <div class="search-foot">
            <span>↑↓ Navigate · ↵ Run · ⌘K Command Palette · Sync bank: {props.syncBank}</span>
          </div>
        </div>
      </div>
    </Show>
  );
}
