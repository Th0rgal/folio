import { For, Show, createEffect, createMemo, createSignal, onCleanup, onMount } from "solid-js";
import { CommandPalette, type PaletteCommand } from "./CommandPalette";
import { EditorPane } from "./EditorPane";
import { EquationPopover } from "./EquationPopover";
import { FindBar } from "./FindBar";
import { KnowledgePanel } from "./KnowledgePanel";
import { MarkdownView, toggleMarkdownTaskAt } from "./MarkdownView";
import { SearchModal } from "./SearchModal";
import { Sidebar } from "./Sidebar";
import * as Ic from "./icons";
import {
  SHORTCUT_LABELS,
  historyNavDelta,
  isCloseSurfaceShortcut,
  isCommandPaletteShortcut,
  isCopilotShortcut,
  isDailyNoteShortcut,
  isFindShortcut,
  isFocusModeShortcut,
  isNewNoteShortcut,
  isRefreshShortcut,
  isSaveShortcut,
  isSearchShortcut,
  isSidebarShortcut,
  isSyncNoteShortcut,
  isToggleSourceShortcut,
  isToggleSplitShortcut,
  sidebarSectionShortcut,
} from "./keyboardShortcuts";
import { applyMathEdit, extractMathSpans } from "./mathParser";
import { detectPlatform, listenDesktopEvent, rpcInvoke } from "./rpc";
import type {
  BootstrapPayload,
  CopilotProfileId,
  DocumentRecord,
  MathSpan,
  SyncSummary,
  ThemeMode,
  TreeNode,
  ViewMode,
} from "./types";

interface HistoryEntry {
  uri: string;
  bank: string;
}

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

const HIDDEN_FRONTMATTER_KEYS = new Set([
  "title",
  "name",
  "tags",
  "id",
  "page_id",
  "source_id",
  "document_id",
  "external_id",
  "bank",
  "bank_id",
  "type",
  "source_type",
  "type_de_source",
  "author",
  "auteur",
  "is_agent_summary",
  "description",
  "summary",
  "abstract",
  "content",
  "body",
  "prompt",
  "instructions",
  "consigne",
  "timestamp",
  "updated_at",
  "created_at",
  "date",
  "date_originale",
  "date_import",
  "original_date",
  "url",
  "uri",
  "links",
  "source_ids",
  "linked_page_ids",
  "related_sources",
  "related_pages",
]);

function formatTagLabel(raw: string): string | null {
  const cleaned = raw.replace(/\s+/g, " ").trim();
  if (!cleaned || cleaned.length > 40) return null;
  if (cleaned.startsWith("@")) {
    return cleaned;
  }
  const bare = cleaned.replace(/^#+/, "").trim();
  if (!bare) return null;
  return bare.startsWith("@") ? bare : `#${bare}`;
}

function formatShortExternalId(id: string): string {
  const m = /^(kp|src|doc)-([0-9a-f]{8})[0-9a-f]{6,}$/i.exec(id);
  if (m) return `${m[1]}-${m[2]}…`;
  return id.length > 20 ? `${id.slice(0, 16)}…` : id;
}

function formatShortDate(raw?: string | null): string | null {
  if (!raw) return null;
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(raw.trim());
  return m ? m[1] : null;
}

function readStoredSidebarOpen(): boolean {
  try {
    return window.localStorage?.getItem("folio:sidebar-open") !== "0";
  } catch {
    return true;
  }
}

function readStoredSectionOpen(id: "local" | "hindsight" | "notion"): boolean {
  try {
    return window.localStorage?.getItem(`folio:sec:${id}`) !== "0";
  } catch {
    return true;
  }
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
  const [bankTrees, setBankTrees] = createSignal<Record<string, TreeNode[] | undefined>>({});
  const [loadingBanks, setLoadingBanks] = createSignal<Record<string, boolean>>({});
  const [notionTree, setNotionTree] = createSignal<TreeNode[]>([]);
  const [notionSyncing, setNotionSyncing] = createSignal(false);
  const [sectionOpen, setSectionOpen] = createSignal({
    local: readStoredSectionOpen("local"),
    hindsight: readStoredSectionOpen("hindsight"),
    notion: readStoredSectionOpen("notion"),
  });
  const [activeBank, setActiveBank] = createSignal("thomas");
  const [syncBank, setSyncBank] = createSignal("folio");
  const [syncSummary, setSyncSummary] = createSignal<SyncSummary | null>(null);
  const [syncingNote, setSyncingNote] = createSignal(false);
  const [syncMenuOpen, setSyncMenuOpen] = createSignal(false);

  const [doc, setDoc] = createSignal<DocumentRecord | null>(null);
  const [draftMarkdown, setDraftMarkdown] = createSignal("");
  const [editorSelection, setEditorSelection] = createSignal("");
  const [dirty, setDirty] = createSignal(false);
  const [saving, setSaving] = createSignal(false);
  const [viewMode, setViewMode] = createSignal<ViewMode>(initParams.modeOverride || "preview");
  const [sidebarOpen, setSidebarOpen] = createSignal(readStoredSidebarOpen());
  const [sbWidth, setSbWidth] = createSignal(248);
  const [copilotOpen, setCopilotOpen] = createSignal(false);
  const [copilotWidth, setCopilotWidth] = createSignal(392);
  const [requestedCopilotProfile, setRequestedCopilotProfile] =
    createSignal<CopilotProfileId | null>(null);

  const [themeMode, setThemeMode] = createSignal<ThemeMode>(initParams.themeOverride || "dark");
  const [history, setHistory] = createSignal<HistoryEntry[]>([]);
  const [hIdx, setHIdx] = createSignal(-1);
  const [macros, setMacros] = createSignal<Record<string, string>>({
    "\\R": "\\mathbb{R}",
    "\\E": "\\mathbb{E}",
    "\\Prob": "\\mathbb{P}",
  });

  const [selectedMath, setSelectedMath] = createSignal<MathSpan | null>(null);
  const [searchOpen, setSearchOpen] = createSignal(Boolean(initParams.searchQuery !== null));
  const [paletteOpen, setPaletteOpen] = createSignal(false);
  const [mountModalOpen, setMountModalOpen] = createSignal(false);
  const [mountPathInput, setMountPathInput] = createSignal("");
  const [folderModalOpen, setFolderModalOpen] = createSignal(false);
  const [newFolderInput, setNewFolderInput] = createSignal("");
  const [renameModalOpen, setRenameModalOpen] = createSignal(false);
  const [renameTitleInput, setRenameTitleInput] = createSignal("");
  const [renameFolderInput, setRenameFolderInput] = createSignal("");
  const [retainToast, setRetainToast] = createSignal<string | null>(null);
  const [errorBanner, setErrorBanner] = createSignal<string | null>(null);

  let saveTimer: number | undefined;
  let readerPaneRef: HTMLElement | undefined;
  let preFocusSnapshot: { sidebar: boolean; copilot: boolean; mode: ViewMode } | null = null;

  const showToast = (msg: string) => {
    setRetainToast(msg);
    window.setTimeout(() => {
      if (retainToast() === msg) setRetainToast(null);
    }, 2600);
  };

  const hasAnyModal = () =>
    searchOpen() ||
    paletteOpen() ||
    mountModalOpen() ||
    folderModalOpen() ||
    renameModalOpen();

  const setSidebarVisible = (next: boolean) => {
    if (!next && document.getElementById("folio-sidebar")?.contains(document.activeElement)) {
      (document.activeElement as HTMLElement | null)?.blur?.();
    }
    setSidebarOpen(next);
    try {
      window.localStorage?.setItem("folio:sidebar-open", next ? "1" : "0");
    } catch {
      // ignore storage errors
    }
  };

  const toggleSidebar = () => {
    preFocusSnapshot = null;
    setSidebarVisible(!sidebarOpen());
  };

  const setProviderSectionOpen = (id: "local" | "hindsight" | "notion", next: boolean) => {
    setSectionOpen((prev) => ({ ...prev, [id]: next }));
    try {
      window.localStorage?.setItem(`folio:sec:${id}`, next ? "1" : "0");
    } catch {
      // ignore storage errors
    }
  };

  const toggleProviderSection = (id: "local" | "hindsight" | "notion") => {
    setProviderSectionOpen(id, !sectionOpen()[id]);
  };

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

  const startCopilotResize = (e: PointerEvent) => {
    e.preventDefault();
    document.body.classList.add("resizing");
    const move = (ev: PointerEvent) => {
      const rightWidth = window.innerWidth - ev.clientX;
      setCopilotWidth(Math.min(620, Math.max(300, rightWidth)));
    };
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

  const pushHistory = (entry: HistoryEntry) => {
    const curList = history();
    const curPos = hIdx();
    const active = curPos >= 0 ? curList[curPos] : undefined;
    if (active && active.uri === entry.uri && active.bank === entry.bank) return;
    const trimmed = curPos >= 0 ? curList.slice(0, curPos + 1) : [];
    trimmed.push(entry);
    setHistory(trimmed);
    setHIdx(trimmed.length - 1);
  };

  const openDocument = async (
    uriOrId: string,
    bankOverride?: string,
    recordHistory = true,
  ) => {
    try {
      setErrorBanner(null);
      setSelectedMath(null);
      setSyncMenuOpen(false);
      const bank = bankOverride || activeBank();
      if (bankOverride && bankOverride !== activeBank()) {
        setActiveBank(bankOverride);
      }
      const loaded = await rpcInvoke<DocumentRecord>("open_document", {
        uri: uriOrId,
        bank_id: bank,
      });
      if (loaded.provider === "hindsight" && loaded.provenance.bank_id) {
        setActiveBank(loaded.provenance.bank_id);
      }
      setDoc(loaded);
      setDraftMarkdown(loaded.raw_markdown);
      setDirty(false);
      if (recordHistory) {
        pushHistory({ uri: loaded.uri, bank });
      }

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

  const navHistory = (delta: -1 | 1) => {
    const nextPos = hIdx() + delta;
    const list = history();
    if (nextPos < 0 || nextPos >= list.length) return;
    const entry = list[nextPos];
    setHIdx(nextPos);
    if (entry.bank !== activeBank()) {
      setActiveBank(entry.bank);
      void ensureBankTreeLoaded(entry.bank);
    }
    void openDocument(entry.uri, entry.bank, false);
  };

  const refreshBankTree = async (bankId: string) => {
    setLoadingBanks((prev) => ({ ...prev, [bankId]: true }));
    try {
      const tree = await rpcInvoke<TreeNode[]>("list_tree", {
        provider: "hindsight",
        scope: bankId,
      });
      setBankTrees((prev) => ({ ...prev, [bankId]: tree }));
    } catch {
      setBankTrees((prev) => ({ ...prev, [bankId]: [] }));
    } finally {
      setLoadingBanks((prev) => ({ ...prev, [bankId]: false }));
    }
  };

  const ensureBankTreeLoaded = async (bankId: string) => {
    if (bankTrees()[bankId] !== undefined || loadingBanks()[bankId]) return;
    await refreshBankTree(bankId);
  };

  const refreshSyncSummary = async () => {
    try {
      const sum = await rpcInvoke<SyncSummary>("hindsight_sync_status");
      setSyncSummary(sum);
    } catch {
      // ignore
    }
  };

  const handleSyncNotion = async () => {
    setNotionSyncing(true);
    try {
      const res = await rpcInvoke<{ tree: TreeNode[] }>("notion_sync", { limit: 24 });
      setNotionTree(res.tree || []);
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setNotionSyncing(false);
    }
  };

  const handleRefreshAll = async () => {
    void rpcInvoke<TreeNode[]>("list_tree", { provider: "local-fs" })
      .then(setLocalTree)
      .catch(() => undefined);
    void refreshSyncSummary();
    const loadedBankIds = Object.keys(bankTrees());
    for (const bId of loadedBankIds.length ? loadedBankIds : [activeBank()]) {
      void refreshBankTree(bId);
    }
    if (boot()?.providers.notion?.connected) {
      void rpcInvoke<TreeNode[]>("list_tree", { provider: "notion" })
        .then(setNotionTree)
        .catch(() => undefined);
    }
    const cur = doc();
    if (cur && !dirty() && cur.provider !== "local-fs") {
      await openDocument(cur.uri, activeBank(), false);
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
      void refreshSyncSummary();
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const scheduleAutosave = (nextRaw: string, triggerSave = true) => {
    setDraftMarkdown(nextRaw);
    const current = doc();
    if (!current || current.readonly || current.provider !== "local-fs") return;
    setDirty(true);
    if (!triggerSave) return;
    if (saveTimer) window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(() => {
      void flushSave(current.uri, nextRaw);
    }, 360);
  };

  const toggleFocusMode = () => {
    if (!sidebarOpen() && !copilotOpen() && preFocusSnapshot) {
      setSidebarVisible(preFocusSnapshot.sidebar);
      setCopilotOpen(preFocusSnapshot.copilot);
      setViewMode(preFocusSnapshot.mode);
      preFocusSnapshot = null;
      return;
    }
    preFocusSnapshot = { sidebar: sidebarOpen(), copilot: copilotOpen(), mode: viewMode() };
    setSidebarVisible(false);
    setCopilotOpen(false);
    setViewMode("preview");
  };

  const handleCreateNote = async (folderOverride?: string) => {
    try {
      const currentFolder = folderOverride ?? (doc()?.provider === "local-fs" ? doc()?.folder_path || "Notes" : "Notes");
      const created = await rpcInvoke<DocumentRecord & { tree?: TreeNode[] }>("create_local_document", {
        title: "Untitled Research Note",
        folder: currentFolder,
      });
      if (created.tree) setLocalTree(created.tree);
      setDoc(created);
      setDraftMarkdown(created.raw_markdown);
      setDirty(false);
      pushHistory({ uri: created.uri, bank: activeBank() });
      setViewMode("split");
      void refreshSyncSummary();
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    }
  };

  const handleOpenDailyNote = async () => {
    try {
      const res = await rpcInvoke<{ document: DocumentRecord; tree: TreeNode[] }>(
        "open_daily_note",
        {},
      );
      setLocalTree(res.tree || []);
      setDoc(res.document);
      setDraftMarkdown(res.document.raw_markdown);
      setDirty(false);
      pushHistory({ uri: res.document.uri, bank: activeBank() });
      if (viewMode() === "preview") setViewMode("split");
      void refreshSyncSummary();
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    }
  };

  const handleOpenRenameModal = () => {
    const cur = doc();
    if (!cur || cur.provider !== "local-fs") return;
    setRenameTitleInput(cur.title);
    setRenameFolderInput(cur.folder_path || "");
    setRenameModalOpen(true);
  };

  const handleSubmitRename = async () => {
    const cur = doc();
    if (!cur || cur.provider !== "local-fs") return;
    try {
      const res = await rpcInvoke<{ document: DocumentRecord; tree: TreeNode[] }>(
        "rename_local_document",
        {
          uri: cur.uri,
          new_title: renameTitleInput().trim() || cur.title,
          new_folder: renameFolderInput().trim(),
          raw_markdown: dirty() ? draftMarkdown() : undefined,
        },
      );
      setLocalTree(res.tree || []);
      setDoc(res.document);
      setDraftMarkdown(res.document.raw_markdown);
      setDirty(false);
      setRenameModalOpen(false);
      pushHistory({ uri: res.document.uri, bank: activeBank() });
      showToast(`Renamed to “${res.document.title}”`);
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    }
  };

  const handleDeleteCurrentNote = async () => {
    const cur = doc();
    if (!cur || cur.provider !== "local-fs") return;
    if (!window.confirm(`Delete local note “${cur.title}”?`)) return;
    try {
      const res = await rpcInvoke<{ tree: TreeNode[]; sync_summary?: SyncSummary }>(
        "delete_local_document",
        { uri: cur.uri },
      );
      setLocalTree(res.tree || []);
      if (res.sync_summary) setSyncSummary(res.sync_summary);
      const nextUri = firstDocumentUri(res.tree || []);
      if (nextUri) {
        await openDocument(nextUri);
      } else {
        setDoc(null);
        setDraftMarkdown("");
      }
      showToast("Deleted local note");
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    }
  };

  const handleForkRemoteToLocal = async () => {
    const cur = doc();
    if (!cur) return;
    try {
      const res = await rpcInvoke<{ document: DocumentRecord; tree: TreeNode[] }>("fork_to_local", {
        uri: cur.uri,
        bank_id: activeBank(),
        folder: "Notes",
      });
      setLocalTree(res.tree || []);
      setDoc(res.document);
      setDraftMarkdown(res.document.raw_markdown);
      setDirty(false);
      setViewMode("split");
      pushHistory({ uri: res.document.uri, bank: activeBank() });
      showToast(`Forked to local note (${res.document.external_id})`);
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    }
  };

  const handleRevealInFiles = async () => {
    const cur = doc();
    if (!cur || cur.provider !== "local-fs") return;
    try {
      await rpcInvoke("reveal_in_file_manager", { uri: cur.uri });
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    }
  };

  const handleSyncActiveNote = async (promoteRawSource = false) => {
    const cur = doc();
    if (!cur || cur.provider !== "local-fs" || syncingNote()) return;
    setSyncingNote(true);
    setSyncMenuOpen(false);
    try {
      if (saveTimer) window.clearTimeout(saveTimer);
      const res = await rpcInvoke<{
        bank_id: string;
        status: string;
        document: DocumentRecord;
        sync_summary: SyncSummary;
        local_tree: TreeNode[];
      }>("hindsight_sync_note", {
        uri: cur.uri,
        raw_markdown: draftMarkdown(),
        promote_raw_source: promoteRawSource,
      });
      setDoc(res.document);
      setDirty(false);
      setLocalTree(res.local_tree || []);
      setSyncSummary(res.sync_summary);
      void refreshBankTree(res.bank_id);
      showToast(
        promoteRawSource
          ? "Promoted raw source to Hindsight (thomas)"
          : `Synced note to Hindsight bank (${res.bank_id})`,
      );
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setSyncingNote(false);
    }
  };

  const handleSyncBatchNotes = async () => {
    if (syncingNote()) return;
    setSyncingNote(true);
    setSyncMenuOpen(false);
    try {
      const res = await rpcInvoke<{
        bank_id: string;
        synced_count: number;
        summary: SyncSummary;
        local_tree: TreeNode[];
      }>("hindsight_sync_batch", { bank_id: syncBank() });
      setLocalTree(res.local_tree || []);
      setSyncSummary(res.summary);
      void refreshBankTree(res.bank_id);
      const cur = doc();
      if (cur && cur.provider === "local-fs") {
        await openDocument(cur.uri, activeBank(), false);
      }
      showToast(`Synced ${res.synced_count} note(s) to Hindsight (${res.bank_id})`);
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setSyncingNote(false);
    }
  };

  const handleBrowseMountFolder = async () => {
    try {
      const res = await rpcInvoke<{ path?: string | null }>("pick_directory_native");
      if (res?.path) {
        setMountPathInput(res.path);
      }
    } catch {
      // Fallback to manual path input when running in pure browser mode
    }
  };

  const paletteCommands = createMemo<PaletteCommand[]>(() => [
    {
      id: "daily",
      label: "Open Today's Daily Journal Note",
      hint: "Journal/YYYY-MM-DD.md",
      shortcut: SHORTCUT_LABELS.dailyNote,
      run: () => void handleOpenDailyNote(),
    },
    {
      id: "new-note",
      label: "Create New Research Note",
      shortcut: SHORTCUT_LABELS.newNote,
      run: () => void handleCreateNote(),
    },
    {
      id: "copilot",
      label: "Toggle Knowledge Copilot Drawer",
      hint: "Fast · Smart · Private · Obliterated",
      shortcut: SHORTCUT_LABELS.copilot,
      run: () => setCopilotOpen((v) => !v),
    },
    {
      id: "sync-note",
      label: `Sync Active Note to Hindsight (${syncBank()})`,
      shortcut: SHORTCUT_LABELS.syncNote,
      run: () => void handleSyncActiveNote(false),
    },
    {
      id: "sync-batch",
      label: `Sync All Changed Notes to Hindsight (${syncBank()})`,
      hint: `${syncSummary()?.pending_sync ?? 0} pending`,
      run: () => void handleSyncBatchNotes(),
    },
    {
      id: "promote-thomas",
      label: "Promote Active Note to thomas Raw-First Archive (save_source)",
      run: () => void handleSyncActiveNote(true),
    },
    {
      id: "rename-note",
      label: "Rename or Move Active Note…",
      run: () => handleOpenRenameModal(),
    },
    {
      id: "fork-local",
      label: "Fork Active Remote Page to Local Markdown Note",
      run: () => void handleForkRemoteToLocal(),
    },
    {
      id: "reveal-files",
      label: "Reveal Active Note in File Manager",
      run: () => void handleRevealInFiles(),
    },
    {
      id: "new-folder",
      label: "Create New Local Subfolder…",
      run: () => setFolderModalOpen(true),
    },
    {
      id: "mount-folder",
      label: "Mount Local Markdown Directory…",
      run: () => setMountModalOpen(true),
    },
    {
      id: "view-split",
      label: "Switch to Split Editor + Preview",
      shortcut: SHORTCUT_LABELS.toggleSplit,
      run: () => setViewMode("split"),
    },
    {
      id: "theme-toggle",
      label: "Toggle Dark / Light Appearance",
      run: () => applyThemeToDom(themeMode() === "dark" ? "light" : "dark"),
    },
  ]);

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
    let unlistenOpenTarget: (() => void) | undefined;

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
      void refreshSyncSummary();
    }).then((fn) => {
      unlistenWorkspace = fn;
    });

    void listenDesktopEvent("app:open-target", (payload) => {
      const target = (payload as { target?: string } | null)?.target;
      if (!target) return;
      void rpcInvoke<{ document: DocumentRecord | null; tree: TreeNode[] }>(
        "open_filesystem_path",
        { target, bank_id: activeBank() },
      )
        .then((res) => {
          if (res.tree) setLocalTree(res.tree);
          if (res.document) {
            setDoc(res.document);
            setDraftMarkdown(res.document.raw_markdown);
            setDirty(false);
            pushHistory({ uri: res.document.uri, bank: activeBank() });
          }
        })
        .catch((err) => setErrorBanner(err instanceof Error ? err.message : String(err)));
    }).then((fn) => {
      unlistenOpenTarget = fn;
    });

    const onSelectionChange = () => {
      const active = document.activeElement;
      if (!(active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement)) return;
      if ((active.selectionStart ?? 0) !== (active.selectionEnd ?? 0)) return;
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.anchorNode || active.contains(sel.anchorNode)) return;
      active.blur();
    };
    document.addEventListener("selectionchange", onSelectionChange);

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.isComposing) return;

      // ⌘W closes active modal / popover / Copilot drawer without killing the window
      if (isCloseSurfaceShortcut(e)) {
        e.preventDefault();
        if (paletteOpen()) {
          setPaletteOpen(false);
          return;
        }
        if (searchOpen()) {
          setSearchOpen(false);
          return;
        }
        if (mountModalOpen() || folderModalOpen() || renameModalOpen() || syncMenuOpen()) {
          setMountModalOpen(false);
          setFolderModalOpen(false);
          setRenameModalOpen(false);
          setSyncMenuOpen(false);
          return;
        }
        if (selectedMath()) {
          setSelectedMath(null);
          return;
        }
        if (copilotOpen()) {
          setCopilotOpen(false);
          return;
        }
        return;
      }

      // ⌘K Command Palette
      if (isCommandPaletteShortcut(e)) {
        e.preventDefault();
        setSearchOpen(false);
        setPaletteOpen((v) => !v);
        return;
      }

      // ⌘J Knowledge Copilot drawer
      if (isCopilotShortcut(e) && !hasAnyModal()) {
        e.preventDefault();
        setCopilotOpen((v) => !v);
        return;
      }

      // ⌘B Sidebar toggle
      if (isSidebarShortcut(e) && !hasAnyModal()) {
        e.preventDefault();
        e.stopPropagation();
        toggleSidebar();
        return;
      }

      const sidebarFocused =
        isFindShortcut(e) &&
        Boolean(document.getElementById("folio-sidebar")?.contains(document.activeElement));

      // ⌘⇧F Global Search & Recall
      if (isSearchShortcut(e) || sidebarFocused) {
        e.preventDefault();
        setPaletteOpen(false);
        if (searchOpen()) {
          const el = document.querySelector<HTMLInputElement>('[data-testid="search-input"]');
          el?.focus({ preventScroll: true });
          el?.select();
          return;
        }
        setSearchOpen(true);
        return;
      }

      if (isRefreshShortcut(e)) {
        e.preventDefault();
        void handleRefreshAll();
        return;
      }

      if (hasAnyModal()) {
        if (e.key === "Escape") {
          e.preventDefault();
          setMountModalOpen(false);
          setFolderModalOpen(false);
          setRenameModalOpen(false);
          setPaletteOpen(false);
        }
        return;
      }

      if (e.key === "Escape") {
        if (syncMenuOpen()) {
          e.preventDefault();
          setSyncMenuOpen(false);
          return;
        }
        if (sidebarOpen() && window.matchMedia?.("(max-width: 720px)").matches) {
          e.preventDefault();
          setSidebarVisible(false);
          return;
        }
        if (selectedMath()) {
          e.preventDefault();
          setSelectedMath(null);
          return;
        }
      }

      if (isDailyNoteShortcut(e)) {
        e.preventDefault();
        void handleOpenDailyNote();
        return;
      }

      if (isSyncNoteShortcut(e)) {
        const cur = doc();
        if (cur && cur.provider === "local-fs") {
          e.preventDefault();
          void handleSyncActiveNote(false);
        }
        return;
      }

      if (isToggleSourceShortcut(e)) {
        e.preventDefault();
        setViewMode((prev) => (prev === "source" ? "preview" : "source"));
        return;
      }

      if (isToggleSplitShortcut(e)) {
        e.preventDefault();
        setViewMode((prev) => (prev === "split" ? "preview" : "split"));
        return;
      }

      if (isFocusModeShortcut(e)) {
        e.preventDefault();
        toggleFocusMode();
        return;
      }

      if (isNewNoteShortcut(e)) {
        e.preventDefault();
        void handleCreateNote();
        return;
      }

      if (isSaveShortcut(e)) {
        const cur = doc();
        if (cur && !cur.readonly && cur.provider === "local-fs") {
          e.preventDefault();
          void flushSave(cur.uri, draftMarkdown());
        }
        return;
      }

      const delta = historyNavDelta(e);
      if (delta !== 0) {
        e.preventDefault();
        navHistory(delta);
        return;
      }

      const sectionTarget = sidebarSectionShortcut(e);
      if (sectionTarget) {
        e.preventDefault();
        setSidebarVisible(true);
        setProviderSectionOpen(sectionTarget, true);
        requestAnimationFrame(() => {
          const region = document.querySelector<HTMLElement>(
            `[data-section="${sectionTarget}"] button.row:not(:disabled)`,
          );
          region?.focus();
        });
      }
    };
    window.addEventListener("keydown", onKeyDown, true);

    void (async () => {
      try {
        const data = await rpcInvoke<BootstrapPayload>("get_bootstrap", {});
        setBoot(data);
        setLocalTree(data.local_tree || []);
        const defaultBank = data.active_bank || "thomas";
        setActiveBank(defaultBank);
        if (data.sync_bank) setSyncBank(data.sync_bank);
        if (data.providers.hindsight?.sync_summary) {
          setSyncSummary(data.providers.hindsight.sync_summary);
        }
        if (data.macros) setMacros(data.macros);
        if (
          !initParams.themeOverride &&
          (data.omarchy_mode === "light" || data.omarchy_mode === "dark")
        ) {
          applyThemeToDom(data.omarchy_mode);
        }

        void refreshBankTree(defaultBank).then(() => {
          for (const b of data.banks || []) {
            if (b.bank_id && b.bank_id !== defaultBank) {
              void ensureBankTreeLoaded(b.bank_id);
            }
          }
        });

        if (data.providers.notion?.connected) {
          setNotionSyncing(true);
          void rpcInvoke<TreeNode[]>("list_tree", { provider: "notion" })
            .then(setNotionTree)
            .catch(() => undefined)
            .finally(() => setNotionSyncing(false));
        }

        if (initParams.openUri) {
          if (
            initParams.openUri.startsWith("/") ||
            initParams.openUri.startsWith("file://") ||
            initParams.openUri.startsWith("folio://")
          ) {
            const openedFs = await rpcInvoke<{ document: DocumentRecord | null; tree: TreeNode[] }>(
              "open_filesystem_path",
              { target: initParams.openUri, bank_id: defaultBank },
            );
            if (openedFs.tree) setLocalTree(openedFs.tree);
            if (openedFs.document) {
              setDoc(openedFs.document);
              setDraftMarkdown(openedFs.document.raw_markdown);
              pushHistory({ uri: openedFs.document.uri, bank: defaultBank });
            }
          } else {
            await openDocument(initParams.openUri, defaultBank);
          }
        } else {
          const initialTarget = firstDocumentUri(data.local_tree || []);
          if (initialTarget) {
            await openDocument(initialTarget, defaultBank);
          }
        }
      } catch (err) {
        setErrorBanner(err instanceof Error ? err.message : String(err));
      }
    })();

    onCleanup(() => {
      mql?.removeEventListener?.("change", onMqlChange);
      document.removeEventListener("selectionchange", onSelectionChange);
      window.removeEventListener("keydown", onKeyDown, true);
      unlistenTheme?.();
      unlistenWorkspace?.();
      unlistenOpenTarget?.();
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

  const cleanTags = createMemo(() => {
    const raw = doc()?.tags || [];
    const seen = new Set<string>();
    const out: string[] = [];
    for (const item of raw) {
      const formatted = formatTagLabel(String(item));
      if (!formatted) continue;
      const key = formatted.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(formatted);
    }
    return out;
  });

  const frontmatterEntries = createMemo(() => {
    const fm = doc()?.frontmatter || {};
    const out: Array<[string, string]> = [];
    for (const [k, v] of Object.entries(fm)) {
      if (HIDDEN_FRONTMATTER_KEYS.has(k.trim().toLowerCase())) continue;
      if (v === null || v === undefined || typeof v === "boolean" || typeof v === "object") {
        continue;
      }
      const text = String(v).replace(/\s+/g, " ").trim();
      if (!text || text.length > 42) continue;
      out.push([k.trim(), text]);
    }
    return out;
  });

  const syncStateLabel = createMemo(() => {
    const cur = doc();
    if (!cur || cur.provider !== "local-fs") return null;
    if (dirty()) return { text: `Modified · ${syncBank()}`, tone: "modified" };
    const st = cur.sync?.state || "unsynced";
    if (st === "private") return { text: "Local (#private)", tone: "private" };
    if (st === "synced") return { text: `Synced · ${cur.sync?.target_bank || syncBank()}`, tone: "synced" };
    if (st === "modified") return { text: `Modified · ${cur.sync?.target_bank || syncBank()}`, tone: "modified" };
    return { text: `Sync · ${syncBank()}`, tone: "unsynced" };
  });

  return (
    <div
      class={`app folio-shell ${sidebarOpen() ? "" : "sb-hidden"}`}
      style={{ "--sb-w": `${sbWidth()}px` }}
      data-testid="folio-shell"
      onClick={() => {
        setSelectedMath(null);
        setSyncMenuOpen(false);
      }}
    >
      <button
        type="button"
        class="sidebar-backdrop"
        aria-label="Close sidebar"
        tabIndex={-1}
        onClick={() => setSidebarVisible(false)}
      />

      <Sidebar
        localTree={localTree()}
        bankTrees={bankTrees()}
        loadingBanks={loadingBanks()}
        notionTree={notionTree()}
        notionSyncing={notionSyncing()}
        activeUri={doc()?.uri ?? null}
        activeBank={activeBank()}
        defaultBank={boot()?.active_bank || "thomas"}
        syncBank={syncBank()}
        banks={boot()?.banks || []}
        hindsightStatus={boot()?.providers.hindsight}
        notionStatus={boot()?.providers.notion}
        copilotOpen={copilotOpen()}
        sectionOpen={sectionOpen()}
        onToggleSection={toggleProviderSection}
        themeMode={themeMode()}
        onToggleTheme={() => applyThemeToDom(themeMode() === "dark" ? "light" : "dark")}
        onStartResize={startSidebarResize}
        onSelectUri={(u, bankId) => void openDocument(u, bankId)}
        onEnsureBankLoaded={(bankId) => void ensureBankTreeLoaded(bankId)}
        onNewNote={() => void handleCreateNote()}
        onOpenDailyNote={() => void handleOpenDailyNote()}
        onOpenSearch={() => setSearchOpen(true)}
        onToggleCopilot={() => setCopilotOpen((v) => !v)}
        onNewFolder={() => setFolderModalOpen(true)}
        onOpenMountDialog={() => setMountModalOpen(true)}
        onSyncNotion={() => void handleSyncNotion()}
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
            title={`Toggle sidebar (${SHORTCUT_LABELS.sidebar})`}
            onClick={toggleSidebar}
          >
            <Ic.SidebarIcon />
          </button>
          <button
            type="button"
            class="icon-btn"
            aria-label="Command Palette"
            title={`Command Palette (${SHORTCUT_LABELS.commandPalette})`}
            onClick={() => setPaletteOpen(true)}
          >
            <Ic.CommandIcon />
          </button>
          <button
            type="button"
            class="icon-btn"
            aria-label="Search & Recall"
            title={`Search & Recall (${SHORTCUT_LABELS.search})`}
            onClick={() => setSearchOpen(true)}
          >
            <Ic.SearchIcon />
          </button>
          <span class="tb-gap" />
          <button
            type="button"
            class="icon-btn"
            aria-label="Back"
            title={`Back (${SHORTCUT_LABELS.navBack})`}
            disabled={hIdx() <= 0}
            onClick={() => navHistory(-1)}
          >
            <Ic.ArrowLeft />
          </button>
          <button
            type="button"
            class="icon-btn"
            aria-label="Forward"
            title={`Forward (${SHORTCUT_LABELS.navForward})`}
            disabled={hIdx() < 0 || hIdx() >= history().length - 1}
            onClick={() => navHistory(1)}
          >
            <Ic.ArrowRight />
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
                <Show
                  when={d().provider === "local-fs"}
                  fallback={<span class="crumb-title">{d().title}</span>}
                >
                  <button
                    type="button"
                    class="crumb-title crumb-title-btn"
                    title="Click to rename or move note"
                    onClick={(e) => {
                      e.stopPropagation();
                      handleOpenRenameModal();
                    }}
                  >
                    {d().title}
                  </button>
                </Show>
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
              title={`Preview (${SHORTCUT_LABELS.toggleSource})`}
              onClick={() => setViewMode("preview")}
            >
              Preview
            </button>
            <button
              type="button"
              class={viewMode() === "split" ? "on" : ""}
              title={`Split (${SHORTCUT_LABELS.toggleSplit})`}
              onClick={() => setViewMode("split")}
            >
              Split
            </button>
            <button
              type="button"
              class={viewMode() === "source" ? "on" : ""}
              title={`Source (${SHORTCUT_LABELS.toggleSource})`}
              onClick={() => setViewMode("source")}
            >
              Source
            </button>
          </div>

          {/* Remote page -> 1-click Fork to Local */}
          <Show when={doc() && doc()!.provider !== "local-fs"}>
            <button
              type="button"
              class="dlg-button dlg-button-secondary tb-save-btn"
              title="Fork this page into an editable local Markdown note"
              onClick={() => void handleForkRemoteToLocal()}
            >
              <Ic.ForkIcon size={12.5} />
              <span>Fork to Local</span>
            </button>
          </Show>

          {/* Local note -> Dedicated Hindsight `folio` Bank Sync Split Button */}
          <Show
            when={
              doc()?.provider === "local-fs" &&
              boot()?.providers.hindsight?.connected &&
              !boot()?.providers.hindsight?.readonly
            }
          >
            <div class="sync-split-wrap" onClick={(e) => e.stopPropagation()}>
              <button
                type="button"
                class={`sync-pill sync-${syncStateLabel()?.tone || "unsynced"}`}
                disabled={syncingNote() || syncStateLabel()?.tone === "private"}
                title={`Sync note to dedicated Hindsight '${syncBank()}' bank (${SHORTCUT_LABELS.syncNote})`}
                onClick={() => void handleSyncActiveNote(false)}
              >
                <Ic.SyncCloudIcon size={13} />
                <span>{syncingNote() ? "Syncing…" : syncStateLabel()?.text}</span>
              </button>
              <button
                type="button"
                class="sync-caret-btn"
                aria-label="Hindsight sync options"
                onClick={() => setSyncMenuOpen((v) => !v)}
              >
                <Ic.ChevronDown size={10.5} />
              </button>

              <Show when={syncMenuOpen()}>
                <div class="sync-popover" data-testid="sync-popover">
                  <div class="sync-pop-head">
                    Dedicated Hindsight Sync Bank · <strong>{syncBank()}</strong>
                  </div>
                  <button
                    type="button"
                    class="sync-pop-item"
                    disabled={syncStateLabel()?.tone === "private"}
                    onClick={() => void handleSyncActiveNote(false)}
                  >
                    <Ic.SyncCloudIcon size={13.5} />
                    <span>Sync Note to `{syncBank()}`</span>
                    <kbd>⌘⇧S</kbd>
                  </button>
                  <button
                    type="button"
                    class="sync-pop-item"
                    onClick={() => void handleSyncBatchNotes()}
                  >
                    <Ic.RefreshIcon size={13} />
                    <span>
                      Sync All Changed Notes ({syncSummary()?.pending_sync ?? 0} pending)
                    </span>
                  </button>
                  <div class="sync-pop-sep" />
                  <button
                    type="button"
                    class="sync-pop-item"
                    disabled={syncStateLabel()?.tone === "private"}
                    onClick={() => void handleSyncActiveNote(true)}
                  >
                    <Ic.ArchiveUpIcon size={13.5} />
                    <span>Promote to `thomas` Raw Archive</span>
                  </button>
                  <div class="sync-pop-sep" />
                  <button
                    type="button"
                    class="sync-pop-item"
                    onClick={() => {
                      setSyncMenuOpen(false);
                      handleOpenRenameModal();
                    }}
                  >
                    <Ic.EditPenIcon size={13} />
                    <span>Rename / Move Note…</span>
                  </button>
                  <button
                    type="button"
                    class="sync-pop-item"
                    onClick={() => {
                      setSyncMenuOpen(false);
                      void handleRevealInFiles();
                    }}
                  >
                    <Ic.FolderOpenIcon size={13} />
                    <span>Reveal in File Manager</span>
                  </button>
                  <button
                    type="button"
                    class="sync-pop-item danger"
                    onClick={() => {
                      setSyncMenuOpen(false);
                      void handleDeleteCurrentNote();
                    }}
                  >
                    <Ic.TrashIcon size={13} />
                    <span>Delete Local Note</span>
                  </button>
                </div>
              </Show>
            </div>
          </Show>

          <button
            type="button"
            class={`icon-btn ${copilotOpen() ? "is-active" : ""}`}
            aria-label="Toggle Knowledge Copilot"
            title={`Toggle Orb Knowledge Copilot (${SHORTCUT_LABELS.copilot})`}
            onClick={() => setCopilotOpen((v) => !v)}
          >
            <Ic.SparklesIcon size={15} />
          </button>

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
        <div class="workspace-row">
          <div class="workspace-center">
            <FindBar
              activeDocKey={doc()?.uri ?? null}
              viewMode={viewMode()}
              hasModal={hasAnyModal()}
            />

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
                  Select or create a Markdown note (<kbd>{SHORTCUT_LABELS.newNote}</kbd>), open
                  today's journal (<kbd>{SHORTCUT_LABELS.dailyNote}</kbd>), or summon Copilot (
                  <kbd>{SHORTCUT_LABELS.copilot}</kbd>).
                </div>
              }
            >
              {(d) => (
                <div class={`editor-stage mode-${viewMode()}`}>
                  <Show when={viewMode() === "split" || viewMode() === "source"}>
                    <EditorPane
                      value={draftMarkdown()}
                      readonly={d().readonly}
                      onChange={(next, immediate = true) => scheduleAutosave(next, immediate)}
                      onSelectionChange={setEditorSelection}
                      onAskCopilot={(sel) => {
                        if (sel) setEditorSelection(sel);
                        setCopilotOpen(true);
                      }}
                      onScrollRatio={(ratio) => {
                        if (viewMode() !== "split" || !readerPaneRef) return;
                        const max = readerPaneRef.scrollHeight - readerPaneRef.clientHeight;
                        if (max > 4) {
                          readerPaneRef.scrollTop = ratio * max;
                        }
                      }}
                    />
                  </Show>

                  <Show when={viewMode() === "preview" || viewMode() === "split"}>
                    <section ref={readerPaneRef} class="reader-pane">
                      <div class="reader-column">
                        <div class="provenance-bar" data-testid="provenance-bar">
                          <span
                            class={`prov-badge ${
                              d().provenance.is_agent_summary ? "agent-summary" : "human-authored"
                            }`}
                          >
                            <span class="prov-dot" />
                            {d().provenance.is_agent_summary
                              ? "Agent summary · Knowledge Page"
                              : d().provider === "notion"
                                ? `Notion · ${d().provenance.author || "Workspace"}`
                                : `Human · ${d().provenance.author || "thomas"}`}
                          </span>

                          <Show
                            when={
                              !d().provenance.is_agent_summary ||
                              (d().provenance.source_type &&
                                !/^knowledge[_-]page$/i.test(d().provenance.source_type || ""))
                            }
                          >
                            <span class="prov-meta-pill">
                              {d().provenance.source_type || d().provider}
                            </span>
                          </Show>

                          <Show when={d().external_id && d().provider === "hindsight"}>
                            <span class="prov-id-pill" title={d().external_id || undefined}>
                              {formatShortExternalId(d().external_id || "")}
                            </span>
                          </Show>

                          <Show when={formatShortDate(d().provenance.original_date || d().updated_at)}>
                            {(shortDate) => (
                              <span
                                class="prov-meta-pill"
                                title={d().provenance.original_date || d().updated_at || undefined}
                              >
                                {shortDate()}
                              </span>
                            )}
                          </Show>

                          <For each={cleanTags()}>
                            {(tag) => (
                              <span class="tag-pill" title={tag}>
                                {tag}
                              </span>
                            )}
                          </For>

                          <For each={frontmatterEntries()}>
                            {([k, v]) => (
                              <span class="fm-pill" title={`${k}: ${v}`}>
                                <span class="fm-key">{k}:</span>
                                <span class="fm-val">{v}</span>
                              </span>
                            )}
                          </For>
                        </div>

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

                        <MarkdownView
                          rawMarkdown={draftMarkdown()}
                          noteUri={d().uri}
                          macros={macros()}
                          themeMode={themeMode()}
                          activeMathIndex={selectedMath()?.index ?? null}
                          onSelectMath={(span) => setSelectedMath(span)}
                          onOpenWikilink={(target) => void openDocument(target)}
                          onExternalUrl={(url) =>
                            void rpcInvoke("open_external_url", { url }).catch(() => undefined)
                          }
                          onToggleTask={
                            !d().readonly && d().provider === "local-fs"
                              ? (ordinal) => {
                                  const next = toggleMarkdownTaskAt(draftMarkdown(), ordinal);
                                  scheduleAutosave(next);
                                }
                              : undefined
                          }
                        />

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
          </div>

          {/* Right-hand Orb Knowledge Crawler & Editor Copilot Drawer (⌘J) */}
          <KnowledgePanel
            open={copilotOpen()}
            width={copilotWidth()}
            status={boot()?.providers.copilot}
            activeDoc={doc()}
            draftMarkdown={draftMarkdown()}
            selection={editorSelection()}
            activeBank={activeBank()}
            macros={macros()}
            themeMode={themeMode()}
            requestedProfile={requestedCopilotProfile()}
            onClearSelection={() => setEditorSelection("")}
            onStartResize={startCopilotResize}
            onClose={() => setCopilotOpen(false)}
            onOpenUri={(u) => void openDocument(u)}
            onProposalApplied={(updatedDoc, updatedTree) => {
              setLocalTree(updatedTree || []);
              if (updatedDoc) {
                setDoc(updatedDoc);
                setDraftMarkdown(updatedDoc.raw_markdown);
                setDirty(false);
                pushHistory({ uri: updatedDoc.uri, bank: activeBank() });
              }
              void refreshSyncSummary();
              showToast("Applied Copilot note edit");
            }}
            onRestoreMarkdown={(uri, md) => {
              if (doc()?.uri === uri) {
                setDraftMarkdown(md);
              }
              void flushSave(uri, md).then(() => showToast("Restored previous note version"));
            }}
          />
        </div>
      </main>

      {/* ⌘K Command Palette */}
      <CommandPalette
        open={paletteOpen()}
        activeDoc={doc()}
        syncBank={syncBank()}
        commands={paletteCommands()}
        onSelectUri={(u) => void openDocument(u)}
        onAskCopilot={(prof) => {
          setRequestedCopilotProfile(prof);
          setCopilotOpen(true);
        }}
        onClose={() => setPaletteOpen(false)}
      />

      {/* ⌘⇧F Hybrid Search & Hindsight Recall Modal */}
      <SearchModal
        open={searchOpen()}
        initialQuery={initParams.searchQuery || ""}
        activeBank={activeBank()}
        syncBank={syncBank()}
        banks={boot()?.banks || []}
        onSelectUri={(u) => void openDocument(u)}
        onClose={() => setSearchOpen(false)}
      />

      {/* Rename / Move Local Note Dialog */}
      <Show when={renameModalOpen()}>
        <div class="dlg-back dlg-back-dim" onClick={() => setRenameModalOpen(false)}>
          <div class="dlg" onClick={(e) => e.stopPropagation()}>
            <div class="dlg-head">
              <h3>Rename or move note</h3>
              <button type="button" class="dlg-close" onClick={() => setRenameModalOpen(false)}>
                <Ic.CloseIcon />
              </button>
            </div>
            <div class="dlg-body dlg-stack">
              <label class="dlg-label">
                <span>Title</span>
                <input
                  type="text"
                  class="s-input"
                  value={renameTitleInput()}
                  onInput={(e) => setRenameTitleInput(e.currentTarget.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      void handleSubmitRename();
                    }
                  }}
                />
              </label>
              <label class="dlg-label">
                <span>Folder (relative to workspace root)</span>
                <input
                  type="text"
                  class="s-input mono"
                  placeholder="Notes"
                  value={renameFolderInput()}
                  onInput={(e) => setRenameFolderInput(e.currentTarget.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      void handleSubmitRename();
                    }
                  }}
                />
              </label>
            </div>
            <div class="dlg-foot">
              <button
                type="button"
                class="dlg-button dlg-button-secondary"
                onClick={() => setRenameModalOpen(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                class="dlg-button dlg-button-primary"
                onClick={() => void handleSubmitRename()}
              >
                Save changes
              </button>
            </div>
          </div>
        </div>
      </Show>

      {/* New Local Subfolder Dialog */}
      <Show when={folderModalOpen()}>
        <div class="dlg-back dlg-back-dim" onClick={() => setFolderModalOpen(false)}>
          <div class="dlg" onClick={(e) => e.stopPropagation()}>
            <div class="dlg-head">
              <h3>New subfolder</h3>
              <button type="button" class="dlg-close" onClick={() => setFolderModalOpen(false)}>
                <Ic.CloseIcon />
              </button>
            </div>
            <p class="dlg-description">Create a nested directory inside your local notes vault.</p>
            <div class="dlg-body">
              <input
                type="text"
                class="s-input mono"
                placeholder="Research/Theorems"
                value={newFolderInput()}
                onInput={(e) => setNewFolderInput(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && newFolderInput().trim()) {
                    e.preventDefault();
                    void rpcInvoke<{ tree: TreeNode[] }>("create_local_folder", {
                      folder: newFolderInput().trim(),
                    })
                      .then((res) => {
                        setLocalTree(res.tree || []);
                        setNewFolderInput("");
                        setFolderModalOpen(false);
                      })
                      .catch((err) =>
                        setErrorBanner(err instanceof Error ? err.message : String(err)),
                      );
                  }
                }}
              />
            </div>
            <div class="dlg-foot">
              <button
                type="button"
                class="dlg-button dlg-button-secondary"
                onClick={() => setFolderModalOpen(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                class="dlg-button dlg-button-primary"
                onClick={() => {
                  const f = newFolderInput().trim();
                  if (!f) return;
                  void rpcInvoke<{ tree: TreeNode[] }>("create_local_folder", { folder: f })
                    .then((res) => {
                      setLocalTree(res.tree || []);
                      setNewFolderInput("");
                      setFolderModalOpen(false);
                    })
                    .catch((err) =>
                      setErrorBanner(err instanceof Error ? err.message : String(err)),
                    );
                }}
              >
                Create folder
              </button>
            </div>
          </div>
        </div>
      </Show>

      {/* Mount Local Folder Dialog (with native GTK4 / XDG portal Browse button) */}
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
            <div class="dlg-body dlg-input-row">
              <input
                type="text"
                class="s-input mono"
                placeholder="/home/thomas/Notes"
                value={mountPathInput()}
                onInput={(e) => setMountPathInput(e.currentTarget.value)}
              />
              <button
                type="button"
                class="dlg-button dlg-button-secondary"
                onClick={() => void handleBrowseMountFolder()}
              >
                Browse…
              </button>
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
