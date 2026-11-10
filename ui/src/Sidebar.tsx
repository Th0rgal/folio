import { For, type JSX, Show, createEffect, createMemo, createSignal } from "solid-js";
import * as Ic from "./icons";
import type { ProviderStatus, ThemeMode, TreeNode } from "./types";

function readBoolStorage(key: string, fallback: boolean): boolean {
  try {
    const raw = window.localStorage?.getItem(key);
    if (raw === "1") return true;
    if (raw === "0") return false;
  } catch {
    // ignore storage errors
  }
  return fallback;
}

function writeBoolStorage(key: string, value: boolean): void {
  try {
    window.localStorage?.setItem(key, value ? "1" : "0");
  } catch {
    // ignore storage errors
  }
}

function countTreeLeaves(nodes: TreeNode[]): number {
  let total = 0;
  for (const n of nodes) {
    if (n.uri) total += 1;
    if (n.children?.length) total += countTreeLeaves(n.children);
  }
  return total;
}

function TreeBranch(props: {
  node: TreeNode;
  depth: number;
  defaultExpanded?: boolean;
  activeUri: string | null;
  onSelectUri: (uri: string) => void;
}) {
  const storageKey = () => `folio:tree:${props.node.id}`;
  const hasChildren = () => (props.node.children?.length ?? 0) > 0;
  const isFolder = () => props.node.kind === "folder" || hasChildren();
  const [expanded, setExpanded] = createSignal(
    readBoolStorage(storageKey(), props.defaultExpanded ?? props.depth < 1),
  );
  const isActive = () => Boolean(props.node.uri && props.node.uri === props.activeUri);

  const toggleExpand = () => {
    const next = !expanded();
    setExpanded(next);
    writeBoolStorage(storageKey(), next);
  };

  return (
    <div class="tree-branch">
      <button
        type="button"
        class={`row tree-row ${isActive() ? "active" : ""} ${isFolder() ? "project" : "file"}`}
        style={{ "padding-left": `${16 + props.depth * 13}px` }}
        onClick={() => {
          if (props.node.uri && hasChildren()) {
            if (!expanded()) {
              setExpanded(true);
              writeBoolStorage(storageKey(), true);
            }
            props.onSelectUri(props.node.uri);
          } else if (isFolder()) {
            toggleExpand();
          } else if (props.node.uri) {
            props.onSelectUri(props.node.uri);
          }
        }}
      >
        <span
          class="row-ico"
          onClick={(e) => {
            if (props.node.uri && hasChildren()) {
              e.stopPropagation();
              toggleExpand();
            }
          }}
        >
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
        <span class="row-label" title={props.node.name}>
          {props.node.name}
        </span>
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
                defaultExpanded={false}
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

function BankSubcategory(props: {
  bank: { bank_id: string; name?: string };
  isDefaultBank: boolean;
  isActiveBank: boolean;
  nodes: TreeNode[] | undefined;
  loading: boolean;
  activeUri: string | null;
  onEnsureLoaded: (bankId: string) => void;
  onSelectBankUri: (uri: string, bankId: string) => void;
}) {
  const storageKey = () => `folio:bank-open:${props.bank.bank_id}`;
  const [expanded, setExpanded] = createSignal(
    readBoolStorage(storageKey(), props.isDefaultBank),
  );

  createEffect(() => {
    if (expanded() && props.nodes === undefined && !props.loading) {
      props.onEnsureLoaded(props.bank.bank_id);
    }
  });

  // Auto-expand bank subcategory if an active document inside it is opened (e.g. via Search)
  createEffect(() => {
    const uri = props.activeUri;
    if (uri && uri.startsWith(`hindsight://${props.bank.bank_id}/`) && !expanded()) {
      setExpanded(true);
      writeBoolStorage(storageKey(), true);
    }
  });

  const badgeCount = createMemo(() => {
    const list = props.nodes;
    if (!list) return null;
    const leaves = countTreeLeaves(list);
    return leaves > 0 ? String(leaves) : String(list.length);
  });

  const toggle = () => {
    const next = !expanded();
    setExpanded(next);
    writeBoolStorage(storageKey(), next);
    if (next && props.nodes === undefined) {
      props.onEnsureLoaded(props.bank.bank_id);
    }
  };

  return (
    <div class="tree-branch bank-branch" data-bank-id={props.bank.bank_id}>
      <button
        type="button"
        class={`row tree-row project bank-row ${props.isActiveBank && !expanded() ? "active" : ""}`}
        style={{ "padding-left": "16px" }}
        title={
          props.bank.name && props.bank.name !== props.bank.bank_id
            ? `${props.bank.bank_id} — ${props.bank.name}`
            : props.bank.bank_id
        }
        aria-expanded={expanded()}
        onClick={toggle}
      >
        <span class="row-ico">{expanded() ? <Ic.ChevronDown /> : <Ic.ChevronRight />}</span>
        <span class="row-label">{props.bank.bank_id}</span>
        <Show when={props.loading && !props.nodes}>
          <span class="inbox-sb-badge">…</span>
        </Show>
        <Show when={badgeCount()}>
          {(cnt) => <span class="inbox-sb-badge">{cnt()}</span>}
        </Show>
      </button>

      <Show when={expanded()}>
        <div class="tree-children">
          <Show
            when={(props.nodes?.length ?? 0) > 0}
            fallback={
              <div class="row note" style={{ "padding-left": "26px" }}>
                {props.loading ? "Loading bank…" : "No knowledge pages yet"}
              </div>
            }
          >
            <For each={props.nodes}>
              {(node) => (
                <TreeBranch
                  node={node}
                  depth={1}
                  defaultExpanded={false}
                  activeUri={props.activeUri}
                  onSelectUri={(uri) => props.onSelectBankUri(uri, props.bank.bank_id)}
                />
              )}
            </For>
          </Show>
        </div>
      </Show>
    </div>
  );
}

function ProviderSection(props: {
  id: "local" | "hindsight" | "notion";
  title: string;
  icon: JSX.Element;
  open: boolean;
  onToggle: () => void;
  statusDot?: "on" | "offline" | null;
  readonlyBadge?: boolean;
  action?: JSX.Element;
  testId?: string;
  children: JSX.Element;
}) {
  return (
    <section class="provider-section" data-provider={props.id}>
      <div class="section section-row provider-head">
        <button
          type="button"
          class="provider-toggle"
          aria-expanded={props.open}
          onClick={props.onToggle}
        >
          <span class="provider-chevron">
            {props.open ? <Ic.ChevronDown size={11.5} /> : <Ic.ChevronRight size={11.5} />}
          </span>
          <span class="provider-ico">{props.icon}</span>
          <span class="provider-title">{props.title}</span>
          <Show when={props.statusDot}>
            {(state) => <span class={`m-dot ${state()}`} />}
          </Show>
          <Show when={props.readonlyBadge}>
            <span class="ro-chip" title="Read-only guard enabled">
              ro
            </span>
          </Show>
        </button>

        <Show when={props.action}>
          <div class="provider-actions">{props.action}</div>
        </Show>
      </div>

      <Show when={props.open}>
        <div class="group" data-section={props.id} data-testid={props.testId}>
          {props.children}
        </div>
      </Show>
    </section>
  );
}

export function Sidebar(props: {
  localTree: TreeNode[];
  bankTrees: Record<string, TreeNode[] | undefined>;
  loadingBanks: Record<string, boolean>;
  notionTree: TreeNode[];
  notionSyncing: boolean;
  activeUri: string | null;
  activeBank: string;
  defaultBank: string;
  banks: Array<{ bank_id: string; name?: string }>;
  hindsightStatus: ProviderStatus | undefined;
  notionStatus: ProviderStatus | undefined;
  sectionOpen: { local: boolean; hindsight: boolean; notion: boolean };
  onToggleSection: (id: "local" | "hindsight" | "notion") => void;
  themeMode: ThemeMode;
  onToggleTheme: () => void;
  onStartResize: (ev: PointerEvent) => void;
  onSelectUri: (uri: string, bankId?: string) => void;
  onEnsureBankLoaded: (bankId: string) => void;
  onNewNote: () => void;
  onOpenSearch: () => void;
  onOpenMountDialog: () => void;
  onSyncNotion: () => void;
}) {
  const bankList = createMemo(() =>
    props.banks.length > 0
      ? props.banks
      : [{ bank_id: props.defaultBank, name: props.defaultBank }],
  );

  return (
    <aside id="folio-sidebar" class="sidebar" data-testid="sidebar">
      <div class="sb-top" />

      <nav
        class="sb-scroll"
        data-sidebar-nav
        onContextMenu={(e) => e.preventDefault()}
        onKeyDown={(event) => {
          if (
            event.defaultPrevented ||
            !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key) ||
            (event.target as HTMLElement).matches("input,textarea,select,[contenteditable]")
          ) {
            return;
          }
          const rows = Array.from(
            event.currentTarget.querySelectorAll<HTMLButtonElement>("button.row:not(:disabled)"),
          ).filter((row) => row.getClientRects().length > 0);
          if (!rows.length) return;
          event.preventDefault();
          const current = rows.indexOf(document.activeElement as HTMLButtonElement);
          const next =
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? rows.length - 1
                : (current + (event.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length;
          rows[next].focus();
        }}
      >
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
            <kbd>⌘⇧F</kbd>
          </button>

          {/* 1. Local Notes — expandable/hidable with icon + folder subcategories */}
          <ProviderSection
            id="local"
            title="Local Notes"
            icon={<Ic.LocalNotesIcon size={15.5} />}
            open={props.sectionOpen.local}
            onToggle={() => props.onToggleSection("local")}
            testId="local-tree"
            action={
              <button
                type="button"
                class="section-add"
                title="Mount local folder"
                aria-label="Mount local folder"
                onClick={props.onOpenMountDialog}
              >
                <Ic.PlusIcon size={14} />
              </button>
            }
          >
            <For each={props.localTree}>
              {(node, idx) => (
                <TreeBranch
                  node={node}
                  depth={0}
                  defaultExpanded={idx() === 0}
                  activeUri={props.activeUri}
                  onSelectUri={(u) => props.onSelectUri(u)}
                />
              )}
            </For>
          </ProviderSection>

          {/* 2. Hindsight — expandable/hidable with icon + Memory Bank folder subcategories */}
          <ProviderSection
            id="hindsight"
            title="Hindsight"
            icon={<Ic.HindsightIcon size={15.5} />}
            open={props.sectionOpen.hindsight}
            onToggle={() => props.onToggleSection("hindsight")}
            statusDot={props.hindsightStatus?.connected ? "on" : "offline"}
            readonlyBadge={Boolean(props.hindsightStatus?.readonly)}
            testId="hindsight-tree"
          >
            <Show
              when={props.hindsightStatus?.connected || bankList().length > 0}
              fallback={<div class="row note">Offline cache · connect key in keyring</div>}
            >
              <For each={bankList()}>
                {(bank) => (
                  <BankSubcategory
                    bank={bank}
                    isDefaultBank={bank.bank_id === props.defaultBank}
                    isActiveBank={bank.bank_id === props.activeBank}
                    nodes={props.bankTrees[bank.bank_id]}
                    loading={Boolean(props.loadingBanks[bank.bank_id])}
                    activeUri={props.activeUri}
                    onEnsureLoaded={props.onEnsureBankLoaded}
                    onSelectBankUri={(uri, bankId) => props.onSelectUri(uri, bankId)}
                  />
                )}
              </For>
            </Show>
          </ProviderSection>

          {/* 3. Notion — expandable/hidable with icon + Workspace folder subcategories */}
          <ProviderSection
            id="notion"
            title="Notion"
            icon={<Ic.NotionIcon size={15.5} />}
            open={props.sectionOpen.notion}
            onToggle={() => props.onToggleSection("notion")}
            statusDot={props.notionStatus?.connected ? "on" : "offline"}
            testId="notion-tree"
            action={
              <Show when={props.notionStatus?.connected}>
                <button
                  type="button"
                  class={`section-add ${props.notionSyncing ? "spinning" : ""}`}
                  title="Sync Notion workspaces"
                  aria-label="Sync Notion workspaces"
                  disabled={props.notionSyncing}
                  onClick={props.onSyncNotion}
                >
                  <Ic.RefreshIcon size={13.5} />
                </button>
              </Show>
            }
          >
            <Show
              when={props.notionTree.length > 0}
              fallback={
                <div class="row note">
                  {props.notionSyncing
                    ? "Syncing Notion workspaces…"
                    : props.notionStatus?.connected
                      ? "Click Sync to index Notion pages"
                      : "Connect NOTION_API_KEY in keyring"}
                </div>
              }
            >
              <For each={props.notionTree}>
                {(node, idx) => (
                  <TreeBranch
                    node={node}
                    depth={0}
                    defaultExpanded={idx() === 0}
                    activeUri={props.activeUri}
                    onSelectUri={(u) => props.onSelectUri(u)}
                  />
                )}
              </For>
            </Show>
          </ProviderSection>
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
