import { For, Show, createEffect, createMemo, createSignal, onCleanup, onMount } from "solid-js";
import { EquationPopover } from "./EquationPopover";
import { MarkdownView } from "./MarkdownView";
import { SearchModal } from "./SearchModal";
import { Sidebar } from "./Sidebar";
import * as Ic from "./icons";
import { applyMathEdit, extractMathSpans } from "./mathParser";
import { detectPlatform, listenDesktopEvent, rpcInvoke } from "./rpc";
import type {
  BootstrapPayload,
  DocumentRecord,
  MathSpan,
  ThemeMode,
  TreeNode,
  ViewMode,
} from "./types";

function parseInitialParams(): {
  openUri: string | null;
  searchQuery: string | null;
  openEqIndex: number | null;
  modeOverride: ViewMode | null;
  themeOverride: ThemeMode | null;
} {
  if (typeof window === "undefined") {
    return {
      openUri: null,
      searchQuery: null,
      openEqIndex: null,
      modeOverride: null,
      themeOverride: null,
    };
  }
  const params = new URLSearchParams(window.location.search);
  const eqRaw = params.get("openEq");
  const modeRaw = params.get("view");
  const themeRaw = params.get("theme");
  return {
    openUri: params.get("open"),
    searchQuery: params.get("search"),
    openEqIndex: eqRaw !== null && !Number.isNaN(Number(eqRaw)) ? Number(eqRaw) : null,
    modeOverride:
      modeRaw === "preview" || modeRaw === "split" || modeRaw === "source" ? modeRaw : null,
    themeOverride: themeRaw === "dark" || themeRaw === "light" ? themeRaw : null,
  };
}

function firstDocumentUri(nodes: TreeNode[]): string | null {
  for (const n of nodes) {
    if (n.uri) return n.uri;
    const childHit = firstDocumentUri(n.children || []);
    if (childHit) return childHit;
  }
  return null;
}

export function App() {
  const initParams = parseInitialParams();
  const [boot, setBoot] = createSignal<BootstrapPayload | null>(null);
  const [localTree, setLocalTree] = createSignal<TreeNode[]>([]);
  const [hindsightTree, setHindsightTree] = createSignal<TreeNode[]>([]);
  const [notionTree, setNotionTree] = createSignal<TreeNode[]>([]);
  const [activeBank, setActiveBank] = createSignal("thomas");
  const [doc, setDoc] = createSignal<DocumentRecord | null>(null);
  const [draftMarkdown, setDraftMarkdown] = createSignal("");
  const [dirty, setDirty] = createSignal(false);
  const [saving, setSaving] = createSignal(false);
  const [viewMode, setViewMode] = createSignal<ViewMode>(initParams.modeOverride || "preview");
  const [sidebarOpen, setSidebarOpen] = createSignal(true);
  const [sbWidth, setSbWidth] = createSignal(248);
  const [themeMode, setThemeMode] = createSignal<ThemeMode>(initParams.themeOverride || "dark");
  const [macros, setMacros] = createSignal<Record<string, string>>({
    "\\R": "\\mathbb{R}",
    "\\E": "\\mathbb{E}",
    "\\Prob": "\\mathbb{P}",
  });

  const [selectedMath, setSelectedMath] = createSignal<MathSpan | null>(null);
  const [searchOpen, setSearchOpen] = createSignal(Boolean(initParams.searchQuery !== null));
  const [mountModalOpen, setMountModalOpen] = createSignal(false);
  const [mountPathInput, setMountPathInput] = createSignal("");
  const [retainToast, setRetainToast] = createSignal<string | null>(null);
  const [errorBanner, setErrorBanner] = createSignal<string | null>(null);

  let saveTimer: number | undefined;
  let composingInEditor = false;

  const startSidebarResize = (e: PointerEvent) => {
    e.preventDefault();
    document.body.classList.add("resizing");
    const move = (ev: PointerEvent) => setSbWidth(Math.min(420, Math.max(188, ev.clientX)));
    const up = () => {
      document.body.classList.remove("resizing");
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const applyThemeToDom = (mode: ThemeMode) => {
    setThemeMode(mode);
    document.documentElement.dataset.theme = mode;
    void rpcInvoke("set_window_theme", { theme: mode }).catch(() => undefined);
  };

  const openDocument = async (uriOrId: string, bankOverride?: string) => {
    try {
      setErrorBanner(null);
      setSelectedMath(null);
      const loaded = await rpcInvoke<DocumentRecord>("open_document", {
        uri: uriOrId,
        bank_id: bankOverride || activeBank(),
      });
      setDoc(loaded);
      setDraftMarkdown(loaded.raw_markdown);
      setDirty(false);

      if (initParams.openEqIndex !== null) {
        const spans = extractMathSpans(loaded.raw_markdown);
        const targetSpan = spans[initParams.openEqIndex] || spans[0];
        if (targetSpan) {
          setSelectedMath(targetSpan);
        }
        initParams.openEqIndex = null;
      }
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    }
  };

  const refreshHindsightTree = async (bankId: string) => {
    try {
      const tree = await rpcInvoke<TreeNode[]>("list_tree", {
        provider: "hindsight",
        scope: bankId,
      });
      setHindsightTree(tree);
    } catch {
      setHindsightTree([]);
    }
  };

  const flushSave = async (uri: string, raw: string) => {
    setSaving(true);
    try {
      const updated = await rpcInvoke<DocumentRecord>("save_local_document", {
        uri,
        raw_markdown: raw,
      });
      setDoc(updated);
      setDirty(false);
      const freshLocal = await rpcInvoke<TreeNode[]>("list_tree", { provider: "local-fs" });
      setLocalTree(freshLocal);
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const scheduleAutosave = (nextRaw: string) => {
    setDraftMarkdown(nextRaw);
    const current = doc();
    if (!current || current.readonly || current.provider !== "local-fs") return;
    setDirty(true);
    if (saveTimer) window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(() => {
      if (!composingInEditor) {
        void flushSave(current.uri, nextRaw);
      }
    }, 420);
  };

  onMount(() => {
    document.documentElement.dataset.platform = detectPlatform();

    const mql = window.matchMedia?.("(prefers-color-scheme: light)");
    if (!initParams.themeOverride && mql) {
      applyThemeToDom(mql.matches ? "light" : "dark");
    } else {
      applyThemeToDom(themeMode());
    }
    const onMqlChange = (ev: MediaQueryListEvent) => {
      applyThemeToDom(ev.matches ? "light" : "dark");
    };
    mql?.addEventListener?.("change", onMqlChange);

    let unlistenTheme: (() => void) | undefined;
    let unlistenWorkspace: (() => void) | undefined;

    void listenDesktopEvent("theme:changed", (payload) => {
      const mode = (payload as { mode?: string } | null)?.mode;
      if (mode === "light" || mode === "dark") {
        applyThemeToDom(mode);
      }
    }).then((fn) => {
      unlistenTheme = fn;
    });

    void listenDesktopEvent("workspace:changed", () => {
      void rpcInvoke<TreeNode[]>("list_tree", { provider: "local-fs" }).then(setLocalTree);
    }).then((fn) => {
      unlistenWorkspace = fn;
    });

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.isComposing) return;
      const primary = e.metaKey || e.ctrlKey;
      if (!primary) return;

      const k = e.key.toLowerCase();
      if (k === "k" && !e.shiftKey && !e.altKey) {
        e.preventDefault();
        setSearchOpen((v) => !v);
        return;
      }
      if ((e.key === "/" || e.code === "Slash") && !e.altKey) {
        e.preventDefault();
        setViewMode((prev) =>
          prev === "preview" ? "split" : prev === "split" ? "source" : "preview",
        );
        return;
      }
      if (k === "\\" && !e.altKey) {
        e.preventDefault();
        setSidebarOpen((v) => !v);
        return;
      }
      if (k === "n" && !e.shiftKey && !e.altKey) {
        e.preventDefault();
        void handleCreateNote();
        return;
      }
      if (k === "s" && !e.shiftKey && !e.altKey) {
        const cur = doc();
        if (cur && !cur.readonly && cur.provider === "local-fs") {
          e.preventDefault();
          void flushSave(cur.uri, draftMarkdown());
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);

    void (async () => {
      try {
        const data = await rpcInvoke<BootstrapPayload>("get_bootstrap", {});
        setBoot(data);
        setLocalTree(data.local_tree || []);
        setActiveBank(data.active_bank || "thomas");
        if (data.macros) setMacros(data.macros);
        if (
          !initParams.themeOverride &&
          (data.omarchy_mode === "light" || data.omarchy_mode === "dark")
        ) {
          applyThemeToDom(data.omarchy_mode);
        }

        void refreshHindsightTree(data.active_bank || "thomas");
        if (data.providers.notion?.connected) {
          void rpcInvoke<TreeNode[]>("list_tree", { provider: "notion" }).then(setNotionTree);
        }

        const initialTarget = initParams.openUri || firstDocumentUri(data.local_tree || []);
        if (initialTarget) {
          await openDocument(initialTarget, data.active_bank || "thomas");
        }
      } catch (err) {
        setErrorBanner(err instanceof Error ? err.message : String(err));
      }
    })();

    onCleanup(() => {
      mql?.removeEventListener?.("change", onMqlChange);
      window.removeEventListener("keydown", onKeyDown);
      unlistenTheme?.();
      unlistenWorkspace?.();
      if (saveTimer) window.clearTimeout(saveTimer);
    });
  });

  createEffect(() => {
    const currentSpan = selectedMath();
    if (!currentSpan) return;
    const spans = extractMathSpans(draftMarkdown());
    const updatedSpan = spans[currentSpan.index];
    if (
      updatedSpan &&
      updatedSpan.tex === currentSpan.tex &&
      (updatedSpan.rawStart !== currentSpan.rawStart ||
        updatedSpan.rawEnd !== currentSpan.rawEnd ||
        updatedSpan.delimiter !== currentSpan.delimiter)
    ) {
      setSelectedMath(updatedSpan);
    }
  });

  const handleCreateNote = async () => {
    try {
      const created = await rpcInvoke<DocumentRecord>("create_local_document", {
        title: "Untitled Research Note",
        folder: "Notes",
      });
      const freshTree = await rpcInvoke<TreeNode[]>("list_tree", { provider: "local-fs" });
      setLocalTree(freshTree);
      setDoc(created);
      setDraftMarkdown(created.raw_markdown);
      setDirty(false);
      setViewMode("split");
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    }
  };

  const handleApplyEquation = (span: MathSpan, nextTex: string) => {
    const updatedRaw = applyMathEdit(draftMarkdown(), span, nextTex);
    scheduleAutosave(updatedRaw);
    const refreshedSpans = extractMathSpans(updatedRaw);
    setSelectedMath(refreshedSpans[span.index] || null);
  };

  const handleSaveMacros = async (nextMacros: Record<string, string>) => {
    setMacros(nextMacros);
    await rpcInvoke("save_latex_macros", { macros: nextMacros }).catch(() => undefined);
  };

  const handleRetainToHindsight = async () => {
    const current = doc();
    if (!current) return;
    try {
      await rpcInvoke("hindsight_save_source", {
        content: draftMarkdown(),
        bank_id: activeBank(),
        author: "thomas",
        source_type: "markdown",
        tags: current.tags,
        is_agent_summary: false,
      });
      setRetainToast(`Saved raw source to Hindsight (${activeBank()})`);
      window.setTimeout(() => setRetainToast(null), 2600);
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    }
  };

  const frontmatterEntries = createMemo(() => {
    const fm = doc()?.frontmatter || {};
    return Object.entries(fm).filter(([k]) => k !== "title" && k !== "tags");
  });

  return (
    <div
      class={`app folio-shell ${sidebarOpen() ? "" : "sb-hidden"}`}
      style={{ "--sb-w": `${sbWidth()}px` }}
      data-testid="folio-shell"
      onClick={() => setSelectedMath(null)}
    >
      <button
        type="button"
        class="sidebar-backdrop"
        aria-label="Close sidebar"
        tabIndex={-1}
        onClick={() => setSidebarOpen(false)}
      />

      <Sidebar
        localTree={localTree()}
        hindsightTree={hindsightTree()}
        notionTree={notionTree()}
        activeUri={doc()?.uri ?? null}
        activeBank={activeBank()}
        banks={boot()?.banks || []}
        hindsightStatus={boot()?.providers.hindsight}
        notionStatus={boot()?.providers.notion}
        themeMode={themeMode()}
        onToggleTheme={() => applyThemeToDom(themeMode() === "dark" ? "light" : "dark")}
        onStartResize={startSidebarResize}
        onSelectUri={(u) => void openDocument(u)}
        onChangeBank={(b) => {
          setActiveBank(b);
          void refreshHindsightTree(b);
        }}
        onNewNote={() => void handleCreateNote()}
        onOpenSearch={() => setSearchOpen(true)}
        onOpenMountDialog={() => setMountModalOpen(true)}
        onSyncNotion={() => {
          void rpcInvoke<{ tree: TreeNode[] }>("notion_sync", { limit: 20 }).then((r) =>
            setNotionTree(r.tree || []),
          );
        }}
      />

      {/* Unified Orb Titlebar */}
      <header class="titlebar">
        <div class="tb-left">
          <button
            type="button"
            class="icon-btn"
            aria-label="Toggle sidebar"
            aria-expanded={sidebarOpen()}
            aria-controls="folio-sidebar"
            title="Toggle sidebar (⌘\\)"
            onClick={() => setSidebarOpen((v) => !v)}
          >
            <Ic.SidebarIcon />
          </button>
          <button
            type="button"
            class="icon-btn"
            aria-label="Search & Recall"
            title="Search & Recall (⌘K)"
            onClick={() => setSearchOpen(true)}
          >
            <Ic.SearchIcon />
          </button>
        </div>

        <div class="tb-title">
          <Show when={doc()} fallback={<span class="crumb-folder">Folio</span>}>
            {(d) => (
              <div class="breadcrumbs">
                <span class="crumb-provider">
                  {d().provider === "local-fs"
                    ? "Local"
                    : d().provider === "hindsight"
                      ? `Hindsight · ${d().provenance.bank_id || activeBank()}`
                      : "Notion"}
                </span>
                <span class="crumb-sep">/</span>
                <Show when={d().folder_path}>
                  <span class="crumb-folder">{d().folder_path}</span>
                  <span class="crumb-sep">/</span>
                </Show>
                <span class="crumb-title">{d().title}</span>
                <Show when={dirty() || saving()}>
                  <span class="save-indicator">{saving() ? "Saving…" : "Edited"}</span>
                </Show>
              </div>
            )}
          </Show>
        </div>

        <div class="tb-tools">
          <div class="cr-tabs" role="tablist" aria-label="View Mode">
            <button
              type="button"
              class={viewMode() === "preview" ? "on" : ""}
              onClick={() => setViewMode("preview")}
            >
              Preview
            </button>
            <button
              type="button"
              class={viewMode() === "split" ? "on" : ""}
              onClick={() => setViewMode("split")}
            >
              Split
            </button>
            <button
              type="button"
              class={viewMode() === "source" ? "on" : ""}
              onClick={() => setViewMode("source")}
            >
              Source
            </button>
          </div>

          <Show
            when={
              doc()?.provider === "local-fs" &&
              boot()?.providers.hindsight?.connected &&
              !boot()?.providers.hindsight?.readonly
            }
          >
            <button
              type="button"
              class="dlg-button dlg-button-primary tb-save-btn"
              title="Explicitly archive raw source to Hindsight (raw-first)"
              onClick={() => void handleRetainToHindsight()}
            >
              <Ic.ArchiveUpIcon size={13} />
              <span>Save to Hindsight</span>
            </button>
          </Show>

          <button
            type="button"
            class="icon-btn"
            aria-label="Toggle appearance"
            title="Toggle Dark / Light appearance"
            onClick={() => applyThemeToDom(themeMode() === "dark" ? "light" : "dark")}
          >
            {themeMode() === "dark" ? <Ic.MoonIcon size={15} /> : <Ic.SunIcon size={15} />}
          </button>
        </div>
      </header>

      <main class="main workspace-pane">
        <Show when={errorBanner()}>
          {(msg) => (
            <div class="error-strip" role="alert">
              <span>{msg()}</span>
              <button type="button" class="sb-mini-btn" onClick={() => setErrorBanner(null)}>
                Dismiss
              </button>
            </div>
          )}
        </Show>

        <Show when={retainToast()}>
          {(msg) => <div class="toast-banner">{msg()}</div>}
        </Show>

        <Show
          when={doc()}
          fallback={
            <div class="empty-stage">
              Select or create a Markdown note (<kbd>⌘N</kbd>) or recall Hindsight (<kbd>⌘K</kbd>).
            </div>
          }
        >
          {(d) => (
            <div class={`editor-stage mode-${viewMode()}`}>
              {/* Left Source Editor Pane (in Split or Source mode) */}
              <Show when={viewMode() === "split" || viewMode() === "source"}>
                <section class="source-pane" onClick={(e) => e.stopPropagation()}>
                  <div class="pane-subbar">
                    <span>Markdown + LaTeX</span>
                    <span>{d().readonly ? "Read-only" : "UTF-8 · Atomic save"}</span>
                  </div>
                  <textarea
                    class="markdown-editor"
                    data-testid="markdown-source-editor"
                    spellcheck={false}
                    readOnly={d().readonly}
                    value={draftMarkdown()}
                    onCompositionStart={() => {
                      composingInEditor = true;
                    }}
                    onCompositionEnd={(e) => {
                      composingInEditor = false;
                      scheduleAutosave(e.currentTarget.value);
                    }}
                    onInput={(e) => {
                      if (e.isComposing || composingInEditor) {
                        setDraftMarkdown(e.currentTarget.value);
                        return;
                      }
                      scheduleAutosave(e.currentTarget.value);
                    }}
                  />
                </section>
              </Show>

              {/* Rendered Document & Provenance Pane */}
              <Show when={viewMode() === "preview" || viewMode() === "split"}>
                <section class="reader-pane">
                  <div class="reader-column">
                    {/* Quiet Orb-style Provenance & Frontmatter Line */}
                    <div class="provenance-bar" data-testid="provenance-bar">
                      <span
                        class={`prov-badge ${
                          d().provenance.is_agent_summary ? "agent-summary" : "human-authored"
                        }`}
                      >
                        <span class="prov-dot" />
                        {d().provenance.is_agent_summary
                          ? "Agent summary · Knowledge Page"
                          : `Human · ${d().provenance.author || "thomas"}`}
                      </span>

                      <span class="prov-meta-pill">
                        {d().provenance.source_type || d().provider}
                      </span>

                      <Show when={d().external_id && d().provider === "hindsight"}>
                        <span class="prov-id-pill">{d().external_id}</span>
                      </Show>

                      <For each={d().tags}>
                        {(tag) => <span class="tag-pill">#{tag}</span>}
                      </For>

                      <For each={frontmatterEntries()}>
                        {([k, v]) => (
                          <span class="fm-pill">
                            <span class="fm-key">{k}:</span> {String(v)}
                          </span>
                        )}
                      </For>
                    </div>

                    {/* Linked Hindsight Raw Sources / Related Pages strip */}
                    <Show
                      when={
                        d().provenance.related_sources.length > 0 ||
                        d().provenance.related_pages.length > 0
                      }
                    >
                      <div class="grounding-strip">
                        <span class="grounding-label">Grounded in</span>
                        <div class="grounding-chips">
                          <For each={d().provenance.related_sources.slice(0, 8)}>
                            {(srcId) => (
                              <button
                                type="button"
                                class="ref-chip src"
                                onClick={() => void openDocument(srcId)}
                              >
                                <span class="ref-dot" />
                                {srcId.slice(0, 16)}
                              </button>
                            )}
                          </For>
                          <For each={d().provenance.related_pages.slice(0, 6)}>
                            {(kpId) => (
                              <button
                                type="button"
                                class="ref-chip kp"
                                onClick={() => void openDocument(kpId)}
                              >
                                <span class="ref-dot" />
                                {kpId.slice(0, 16)}
                              </button>
                            )}
                          </For>
                        </div>
                      </div>
                    </Show>

                    {/* Inline Equation Editor / Inspector Card */}
                    <Show when={selectedMath()}>
                      {(span) => (
                        <EquationPopover
                          span={span()}
                          readonly={d().readonly}
                          macros={macros()}
                          onApply={handleApplyEquation}
                          onSaveMacros={(m) => void handleSaveMacros(m)}
                          onClose={() => setSelectedMath(null)}
                        />
                      )}
                    </Show>

                    {/* Rich Markdown + Offline KaTeX + Mermaid + Prism */}
                    <MarkdownView
                      rawMarkdown={draftMarkdown()}
                      macros={macros()}
                      themeMode={themeMode()}
                      activeMathIndex={selectedMath()?.index ?? null}
                      onSelectMath={(span) => setSelectedMath(span)}
                      onOpenWikilink={(target) => void openDocument(target)}
                      onExternalUrl={(url) =>
                        void rpcInvoke("open_external_url", { url }).catch(() => undefined)
                      }
                    />

                    {/* Backlinks Footer */}
                    <Show when={(d().backlinks || []).length > 0}>
                      <footer class="backlinks-panel">
                        <div class="backlinks-head">
                          Backlinks · {(d().backlinks || []).length}
                        </div>
                        <div class="backlinks-grid">
                          <For each={d().backlinks || []}>
                            {(bl) => (
                              <button
                                type="button"
                                class="backlink-card"
                                onClick={() => void openDocument(bl.uri)}
                              >
                                <span class="bl-provider">{bl.provider}</span>
                                <span class="bl-title">{bl.title}</span>
                              </button>
                            )}
                          </For>
                        </div>
                      </footer>
                    </Show>
                  </div>
                </section>
              </Show>
            </div>
          )}
        </Show>
      </main>

      {/* Cmd+K Hybrid Search & Hindsight Recall Modal */}
      <SearchModal
        open={searchOpen()}
        initialQuery={initParams.searchQuery || ""}
        activeBank={activeBank()}
        onSelectUri={(u) => void openDocument(u)}
        onClose={() => setSearchOpen(false)}
      />

      {/* Mount Local Folder Dialog (Orb Dialog contract) */}
      <Show when={mountModalOpen()}>
        <div class="dlg-back dlg-back-dim" onClick={() => setMountModalOpen(false)}>
          <div class="dlg" onClick={(e) => e.stopPropagation()}>
            <div class="dlg-head">
              <h3>Mount local folder</h3>
              <button
                type="button"
                class="dlg-close"
                aria-label="Close"
                onClick={() => setMountModalOpen(false)}
              >
                <Ic.CloseIcon />
              </button>
            </div>
            <p class="dlg-description">
              Index any local Markdown directory into SQLite FTS5 without moving your files.
            </p>
            <div class="dlg-body">
              <input
                type="text"
                class="s-input mono"
                placeholder="/home/thomas/Notes"
                value={mountPathInput()}
                onInput={(e) => setMountPathInput(e.currentTarget.value)}
              />
            </div>
            <div class="dlg-foot">
              <button
                type="button"
                class="dlg-button dlg-button-secondary"
                onClick={() => setMountModalOpen(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                class="dlg-button dlg-button-primary"
                onClick={() => {
                  const p = mountPathInput().trim();
                  if (!p) return;
                  void rpcInvoke<{ tree: TreeNode[] }>("mount_directory", { path: p })
                    .then((res) => {
                      setLocalTree(res.tree || []);
                      setMountPathInput("");
                      setMountModalOpen(false);
                    })
                    .catch((err) =>
                      setErrorBanner(err instanceof Error ? err.message : String(err)),
                    );
                }}
              >
                Mount folder
              </button>
            </div>
          </div>
        </div>
      </Show>
    </div>
  );
}
