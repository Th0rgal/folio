import { For, Show, createEffect, createMemo, createSignal, onCleanup } from "solid-js";
import { Portal } from "solid-js/web";
import * as Ic from "./icons";
import type { ProviderStatus, ThemeMode, TreeNode } from "./types";

function BankPicker(props: {
  activeBank: string;
  banks: Array<{ bank_id: string; name?: string }>;
  onChangeBank: (bankId: string) => void;
}) {
  const [open, setOpen] = createSignal(false);
  const [pos, setPos] = createSignal({ top: 0, left: 0 });
  const [hoverId, setHoverId] = createSignal<string | null>(null);

  let triggerRef: HTMLButtonElement | undefined;
  let popoverRef: HTMLDivElement | undefined;

  const options = createMemo(() => {
    const list =
      props.banks.length > 0
        ? props.banks
        : [{ bank_id: props.activeBank, name: props.activeBank }];
    if (!list.some((b) => b.bank_id === props.activeBank)) {
      return [{ bank_id: props.activeBank, name: props.activeBank }, ...list];
    }
    return list;
  });

  const updatePosition = () => {
    if (!triggerRef) return;
    const rect = triggerRef.getBoundingClientRect();
    const width = 204;
    const left = Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8));
    const top = Math.min(rect.bottom + 4, window.innerHeight - 180);
    setPos({ top, left });
  };

  const toggle = () => {
    if (open()) {
      setOpen(false);
      return;
    }
    updatePosition();
    setHoverId(props.activeBank);
    setOpen(true);
    requestAnimationFrame(() => {
      const selected = popoverRef?.querySelector<HTMLButtonElement>('[aria-selected="true"]');
      (selected ?? popoverRef?.querySelector<HTMLButtonElement>("button.picker-row"))?.focus({
        preventScroll: true,
      });
    });
  };

  createEffect(() => {
    if (!open()) return;
    const onPointerDown = (ev: PointerEvent) => {
      const target = ev.target as Node | null;
      if (!target) return;
      if (triggerRef?.contains(target) || popoverRef?.contains(target)) return;
      setOpen(false);
    };
    const onKeyDown = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") {
        ev.preventDefault();
        ev.stopPropagation();
        setOpen(false);
        triggerRef?.focus({ preventScroll: true });
      }
    };
    const onResize = () => updatePosition();
    window.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("resize", onResize);
    onCleanup(() => {
      window.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("resize", onResize);
    });
  });

  const stepFocus = (delta: number) => {
    const list = options();
    if (!list.length) return;
    const curIdx = Math.max(
      0,
      list.findIndex((b) => b.bank_id === (hoverId() ?? props.activeBank)),
    );
    const nextIdx = (curIdx + delta + list.length) % list.length;
    const next = list[nextIdx];
    if (!next) return;
    setHoverId(next.bank_id);
    popoverRef
      ?.querySelectorAll<HTMLButtonElement>("button.picker-row")
      ?.[nextIdx]?.focus({ preventScroll: true });
  };

  return (
    <div class="bank-picker-wrap">
      <button
        ref={triggerRef}
        type="button"
        class={`bank-picker-btn ${open() ? "open" : ""}`}
        data-testid="bank-picker-btn"
        aria-label="Hindsight memory bank"
        aria-haspopup="listbox"
        aria-expanded={open()}
        title={`Memory bank: ${props.activeBank}`}
        onClick={toggle}
      >
        <span class="bank-picker-label">{props.activeBank}</span>
        <Ic.ChevronDown size={11} class="bank-picker-chevron" />
      </button>

      <Show when={open()}>
        <Portal>
          <div
            ref={popoverRef}
            class="overlay-popover picker bank-popover"
            role="listbox"
            aria-label="Hindsight memory bank"
            data-testid="bank-picker-popover"
            style={{ top: `${pos().top}px`, left: `${pos().left}px` }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                e.stopPropagation();
                stepFocus(1);
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                e.stopPropagation();
                stepFocus(-1);
              }
            }}
          >
            <div class="picker-group">Memory bank</div>
            <div class="picker-list">
              <For each={options()}>
                {(b) => {
                  const selected = () => b.bank_id === props.activeBank;
                  const active = () => (hoverId() ?? props.activeBank) === b.bank_id;
                  return (
                    <button
                      type="button"
                      role="option"
                      class="picker-row"
                      aria-selected={selected()}
                      data-active={active()}
                      onPointerMove={() => setHoverId(b.bank_id)}
                      onFocus={() => setHoverId(b.bank_id)}
                      onClick={() => {
                        setOpen(false);
                        if (b.bank_id !== props.activeBank) {
                          props.onChangeBank(b.bank_id);
                        }
                        triggerRef?.focus({ preventScroll: true });
                      }}
                    >
                      <span class="picker-row-copy">
                        <span class="bank-option-id">{b.bank_id}</span>
                        <Show when={b.name && b.name !== b.bank_id}>
                          <small>{b.name}</small>
                        </Show>
                      </span>
                      <Show when={selected()}>
                        <span class="picker-check" aria-hidden="true">
                          <Ic.CheckIcon size={13} />
                        </span>
                      </Show>
                    </button>
                  );
                }}
              </For>
            </div>
          </div>
        </Portal>
      </Show>
    </div>
  );
}

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

          <div class="group" data-section="local">
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

            <BankPicker
              activeBank={props.activeBank}
              banks={props.banks}
              onChangeBank={props.onChangeBank}
            />
          </div>

          <div class="group" data-section="hindsight" data-testid="hindsight-tree">
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
