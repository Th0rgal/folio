import { For, Show, createSignal } from "solid-js";
import type { ProviderStatus, TreeNode } from "./types";

function TreeBranch(props: {
  node: TreeNode;
  depth: number;
  activeUri: string | null;
  onSelectUri: (uri: string) => void;
}) {
  const [expanded, setExpanded] = createSignal(props.depth < 2);
  const isFolder = () => props.node.kind === "folder";
  const isActive = () => Boolean(props.node.uri && props.node.uri === props.activeUri);

  return (
    <div class="tree-branch">
      <button
        type="button"
        class={`tree-row ${isActive() ? "active" : ""} ${isFolder() ? "is-folder" : ""}`}
        style={{ "padding-left": `${10 + props.depth * 13}px` }}
        onClick={() => {
          if (isFolder()) {
            setExpanded((v) => !v);
          } else if (props.node.uri) {
            props.onSelectUri(props.node.uri);
          }
        }}
      >
        <span class="tree-chevron">
          {isFolder() ? (expanded() ? "▾" : "▸") : props.node.is_agent_summary ? "◇" : "•"}
        </span>
        <span class="tree-label">{props.node.name}</span>
        <Show when={props.node.badge}>
          {(badge) => (
            <span class={`tree-badge ${props.node.is_agent_summary ? "agent" : ""}`}>
              {badge()}
            </span>
          )}
        </Show>
      </button>
      <Show when={isFolder() && expanded() && props.node.children.length > 0}>
        <div class="tree-children">
          <For each={props.node.children}>
            {(child) => (
              <TreeBranch
                node={child}
                depth={props.depth + 1}
                activeUri={props.activeUri}
                onSelectUri={props.onSelectUri}
              />
            )}
          </For>
        </div>
      </Show>
    </div>
  );
}

export function Sidebar(props: {
  localTree: TreeNode[];
  hindsightTree: TreeNode[];
  notionTree: TreeNode[];
  activeUri: string | null;
  activeBank: string;
  banks: Array<{ bank_id: string; name?: string }>;
  hindsightStatus: ProviderStatus | undefined;
  notionStatus: ProviderStatus | undefined;
  onSelectUri: (uri: string) => void;
  onChangeBank: (bankId: string) => void;
  onNewNote: () => void;
  onOpenSearch: () => void;
  onOpenMountDialog: () => void;
  onSyncNotion: () => void;
}) {
  return (
    <aside class="sidebar" data-testid="sidebar">
      <div class="sidebar-top">
        <div class="brand-row">
          <div class="brand-mark">
            <span class="brand-glyph">ƒ</span>
            <span class="brand-name">Folio</span>
          </div>
          <button
            type="button"
            class="new-note-btn"
            title="New Local Markdown Note (⌘N)"
            onClick={props.onNewNote}
          >
            + Note
          </button>
        </div>

        <button
          type="button"
          class="sidebar-search-trigger"
          data-testid="sidebar-search-btn"
          onClick={props.onOpenSearch}
        >
          <span>Search & Recall…</span>
          <kbd>⌘K</kbd>
        </button>
      </div>

      <div class="sidebar-scroll">
        {/* Local Workspace Section */}
        <section class="sb-section">
          <div class="sb-section-head">
            <span class="sb-section-title">Local Markdown</span>
            <button
              type="button"
              class="sb-mini-btn"
              title="Mount local folder"
              onClick={props.onOpenMountDialog}
            >
              + Folder
            </button>
          </div>
          <div class="sb-tree">
            <For each={props.localTree}>
              {(node) => (
                <TreeBranch
                  node={node}
                  depth={0}
                  activeUri={props.activeUri}
                  onSelectUri={props.onSelectUri}
                />
              )}
            </For>
          </div>
        </section>

        {/* Hindsight Memory Bank Section */}
        <section class="sb-section">
          <div class="sb-section-head">
            <div class="sb-title-with-dot">
              <span
                class={`status-dot ${props.hindsightStatus?.connected ? "online" : "offline"}`}
              />
              <span class="sb-section-title">Hindsight Memory</span>
            </div>
            <Show when={props.hindsightStatus?.readonly}>
              <span class="ro-chip" title="Read-only guard enabled">RO</span>
            </Show>
          </div>

          <div class="bank-selector-row">
            <select
              class="bank-select"
              value={props.activeBank}
              onChange={(e) => props.onChangeBank(e.currentTarget.value)}
            >
              <For
                each={
                  props.banks.length > 0
                    ? props.banks
                    : [{ bank_id: props.activeBank, name: props.activeBank }]
                }
              >
                {(b) => <option value={b.bank_id}>bank: {b.bank_id}</option>}
              </For>
            </select>
          </div>

          <div class="sb-tree" data-testid="hindsight-tree">
            <Show
              when={props.hindsightTree.length > 0}
              fallback={
                <div class="sb-empty-hint">
                  {props.hindsightStatus?.connected
                    ? "Loading Knowledge Pages…"
                    : "Offline cache · connect key in Settings"}
                </div>
              }
            >
              <For each={props.hindsightTree}>
                {(node) => (
                  <TreeBranch
                    node={node}
                    depth={0}
                    activeUri={props.activeUri}
                    onSelectUri={props.onSelectUri}
                  />
                )}
              </For>
            </Show>
          </div>
        </section>

        {/* Optional Notion Adapter Section */}
        <Show when={props.notionStatus?.connected || props.notionTree.length > 0}>
          <section class="sb-section">
            <div class="sb-section-head">
              <div class="sb-title-with-dot">
                <span class="status-dot online" />
                <span class="sb-section-title">Notion Mirror</span>
              </div>
              <button type="button" class="sb-mini-btn" onClick={props.onSyncNotion}>
                Sync
              </button>
            </div>
            <div class="sb-tree">
              <For each={props.notionTree}>
                {(node) => (
                  <TreeBranch
                    node={node}
                    depth={0}
                    activeUri={props.activeUri}
                    onSelectUri={props.onSelectUri}
                  />
                )}
              </For>
            </div>
          </section>
        </Show>
      </div>

      <div class="sidebar-foot">
        <span class="foot-stat">SQLite FTS5 · KaTeX Offline</span>
      </div>
    </aside>
  );
}
