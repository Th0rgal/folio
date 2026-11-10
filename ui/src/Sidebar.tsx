import { For, Show, createSignal } from "solid-js";
import * as Ic from "./icons";
import type { ProviderStatus, ThemeMode, TreeNode } from "./types";

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
        class={`row tree-row ${isActive() ? "active" : ""} ${isFolder() ? "project" : "file"}`}
        style={{ "padding-left": `${6 + props.depth * 12}px` }}
        onClick={() => {
          if (isFolder()) {
            setExpanded((v) => !v);
          } else if (props.node.uri) {
            props.onSelectUri(props.node.uri);
          }
        }}
      >
        <span class="row-ico">
          <Show
            when={isFolder()}
            fallback={
              props.node.is_agent_summary ? (
                <Ic.KnowledgeIcon size={13} />
              ) : (
                <Ic.DocIcon size={14} />
              )
            }
          >
            {expanded() ? <Ic.ChevronDown /> : <Ic.ChevronRight />}
          </Show>
        </span>
        <span class="row-label">{props.node.name}</span>
        <Show when={props.node.badge}>
          {(badge) => (
            <span class={`inbox-sb-badge ${props.node.is_agent_summary ? "agent" : ""}`}>
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
  themeMode: ThemeMode;
  onToggleTheme: () => void;
  onStartResize: (ev: PointerEvent) => void;
  onSelectUri: (uri: string) => void;
  onChangeBank: (bankId: string) => void;
  onNewNote: () => void;
  onOpenSearch: () => void;
  onOpenMountDialog: () => void;
  onSyncNotion: () => void;
}) {
  return (
    <aside id="folio-sidebar" class="sidebar" data-testid="sidebar">
      <div class="sb-top" />

      <nav class="sb-scroll" onContextMenu={(e) => e.preventDefault()}>
        <div class="sb-main-nav">
          <button type="button" class="row" onClick={props.onNewNote}>
            <span class="row-ico">
              <Ic.NewNoteIcon />
            </span>
            <span class="row-label">New Note</span>
            <kbd>⌘N</kbd>
          </button>

          <button
            type="button"
            class="row"
            data-testid="sidebar-search-btn"
            onClick={props.onOpenSearch}
          >
            <span class="row-ico">
              <Ic.SearchIcon />
            </span>
            <span class="row-label">Search & Recall</span>
            <kbd>⌘K</kbd>
          </button>

          {/* Local Workspace Section */}
          <div class="section section-row">
            <span>Local notes</span>
            <button
              type="button"
              class="section-add"
              title="Mount local folder"
              aria-label="Mount local folder"
              onClick={props.onOpenMountDialog}
            >
              <Ic.PlusIcon size={13} />
            </button>
          </div>

          <div class="group">
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

          {/* Hindsight Memory Bank Section */}
          <div class="section section-row">
            <span class="sb-title-with-dot">
              <span
                class={`m-dot ${props.hindsightStatus?.connected ? "on" : "offline"}`}
              />
              <span>Hindsight</span>
              <Show when={props.hindsightStatus?.readonly}>
                <span class="ro-chip" title="Read-only guard enabled">
                  ro
                </span>
              </Show>
            </span>

            <select
              class="bank-select-inline"
              aria-label="Hindsight memory bank"
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
                {(b) => <option value={b.bank_id}>{b.bank_id}</option>}
              </For>
            </select>
          </div>

          <div class="group" data-testid="hindsight-tree">
            <Show
              when={props.hindsightTree.length > 0}
              fallback={
                <div class="row note">
                  {props.hindsightStatus?.connected
                    ? "Loading knowledge pages…"
                    : "Offline cache · connect key in keyring"}
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

          {/* Optional Notion Adapter Section */}
          <Show when={props.notionStatus?.connected || props.notionTree.length > 0}>
            <div class="section section-row">
              <span class="sb-title-with-dot">
                <span class="m-dot on" />
                <span>Notion</span>
              </span>
              <button type="button" class="sb-mini-btn" onClick={props.onSyncNotion}>
                Sync
              </button>
            </div>
            <div class="group">
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
          </Show>
        </div>
      </nav>

      <div class="sb-foot">
        <button
          type="button"
          class="gear-btn"
          title="Toggle appearance"
          onClick={props.onToggleTheme}
        >
          {props.themeMode === "dark" ? <Ic.MoonIcon size={14} /> : <Ic.SunIcon size={14} />}
          <span>{props.themeMode === "dark" ? "Dark" : "Light"}</span>
        </button>
        <span class="sb-foot-meta">FTS5 · KaTeX</span>
      </div>

      <div class="sb-resize" onPointerDown={props.onStartResize} />
    </aside>
  );
}
