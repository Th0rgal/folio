/**
 * Orb-matched keyboard shortcut registry & matchers for Folio.
 *
 * On macOS/iOS, `metaKey` (⌘) without `ctrlKey` is the primary app modifier.
 * On Linux (`m2air` + Omarchnix), Folio's GTK3 shell bridges physical `Super` (`Cmd`)
 * to `metaKey` while Hyprland uses `Ctrl + Super` (`ctrlKey && metaKey`) for WM actions,
 * so `event.metaKey !== event.ctrlKey` accepts both `Cmd+<key>` and `Ctrl+<key>` while
 * never firing during `Ctrl+Cmd+<key>` window-manager chords.
 */

export interface KeyEventLike {
  key: string;
  code?: string;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
  isComposing?: boolean;
  defaultPrevented?: boolean;
}

export const SHORTCUT_LABELS = {
  newNote: "⌘N",
  search: "⌘⇧F",
  find: "⌘F",
  sidebar: "⌘B",
  focusMode: "⌘\\",
  toggleSource: "⌘/",
  toggleSplit: "⌘⇧/",
  navBack: "⌘[",
  navForward: "⌘]",
  save: "⌘S",
  refresh: "⌘R",
  localSection: "⌘1",
  hindsightSection: "⌘2",
} as const;

export function isMacKeyboardPlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const platform =
    (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform ??
    navigator.platform ??
    navigator.userAgent ??
    "";
  return /mac|iphone|ipad|ipod/i.test(platform);
}

/** Command on macOS/iOS, Command or Control elsewhere (never both simultaneously). */
export function hasPrimaryModifier(event: KeyEventLike, macOnly = isMacKeyboardPlatform()): boolean {
  return macOnly ? event.metaKey && !event.ctrlKey : event.metaKey !== event.ctrlKey;
}

/** Command-Shift-F (`⌘⇧F`) / Ctrl-Shift-F: Global Unified Search & Recall (Orb contract). */
export function isSearchShortcut(event: KeyEventLike, macOnly?: boolean): boolean {
  if (event.isComposing || event.defaultPrevented || event.altKey || !event.shiftKey) return false;
  const keyMatches = event.code === "KeyF" || event.key.toLowerCase() === "f";
  return keyMatches && hasPrimaryModifier(event, macOnly);
}

/** Command-F (`⌘F`) / Ctrl-F: Local Find in active note (or Global Search when sidebar has focus). */
export function isFindShortcut(event: KeyEventLike, macOnly?: boolean): boolean {
  if (event.isComposing || event.defaultPrevented || event.altKey || event.shiftKey) return false;
  const keyMatches = event.code === "KeyF" || event.key.toLowerCase() === "f";
  return keyMatches && hasPrimaryModifier(event, macOnly);
}

/** Command-B (`⌘B`) / Ctrl-B: Toggle sidebar (Orb contract). */
export function isSidebarShortcut(event: KeyEventLike, macOnly?: boolean): boolean {
  if (event.isComposing || event.defaultPrevented || event.altKey || event.shiftKey) return false;
  const keyMatches = event.code === "KeyB" || event.key.toLowerCase() === "b";
  return keyMatches && hasPrimaryModifier(event, macOnly);
}

/** Command-\ (`⌘\`) / Ctrl-\: Focus / expand active pane (Orb contract). */
export function isFocusModeShortcut(event: KeyEventLike, macOnly?: boolean): boolean {
  if (event.isComposing || event.defaultPrevented || event.altKey || event.shiftKey) return false;
  const keyMatches = event.code === "Backslash" || event.key === "\\";
  return keyMatches && hasPrimaryModifier(event, macOnly);
}

/** Command-/ (`⌘/`) / Ctrl-/: Toggle Markdown Source vs Preview (Orb contract). */
export function isToggleSourceShortcut(event: KeyEventLike, macOnly?: boolean): boolean {
  if (event.isComposing || event.defaultPrevented || event.altKey || event.shiftKey) return false;
  const keyMatches = event.code === "Slash" || event.key === "/";
  return keyMatches && hasPrimaryModifier(event, macOnly);
}

/** Command-Shift-/ (`⌘⇧/`) / Ctrl-Shift-/: Toggle Split editor/preview view. */
export function isToggleSplitShortcut(event: KeyEventLike, macOnly?: boolean): boolean {
  if (event.isComposing || event.defaultPrevented || event.altKey || !event.shiftKey) return false;
  const keyMatches = event.code === "Slash" || event.key === "?" || event.key === "/";
  return keyMatches && hasPrimaryModifier(event, macOnly);
}

/** Command-N (`⌘N`) / Ctrl-N: New note. */
export function isNewNoteShortcut(event: KeyEventLike, macOnly?: boolean): boolean {
  if (event.isComposing || event.defaultPrevented || event.altKey || event.shiftKey) return false;
  const keyMatches = event.code === "KeyN" || event.key.toLowerCase() === "n";
  return keyMatches && hasPrimaryModifier(event, macOnly);
}

/** Command-S (`⌘S`) / Ctrl-S: Save active local note. */
export function isSaveShortcut(event: KeyEventLike, macOnly?: boolean): boolean {
  if (event.isComposing || event.defaultPrevented || event.altKey || event.shiftKey) return false;
  const keyMatches = event.code === "KeyS" || event.key.toLowerCase() === "s";
  return keyMatches && hasPrimaryModifier(event, macOnly);
}

/** Command-R (`⌘R`) / Ctrl-R: Refresh trees & active document without reloading WebKit. */
export function isRefreshShortcut(event: KeyEventLike, macOnly?: boolean): boolean {
  if (event.isComposing || event.defaultPrevented || event.altKey || event.shiftKey) return false;
  const keyMatches = event.code === "KeyR" || event.key.toLowerCase() === "r";
  return keyMatches && hasPrimaryModifier(event, macOnly);
}

/** Command-[ (`⌘[`) and Command-] (`⌘]`): Navigate document history Back / Forward (Orb contract). */
export function historyNavDelta(event: KeyEventLike, macOnly?: boolean): -1 | 1 | 0 {
  if (event.isComposing || event.defaultPrevented || event.altKey || event.shiftKey) return 0;
  if (!hasPrimaryModifier(event, macOnly)) return 0;
  if (event.code === "BracketLeft" || event.key === "[") return -1;
  if (event.code === "BracketRight" || event.key === "]") return 1;
  return 0;
}

/** Command-1 (`⌘1`) / Command-2 (`⌘2`): Jump sidebar focus to Local notes / Hindsight. */
export function sidebarSectionShortcut(
  event: KeyEventLike,
  macOnly?: boolean,
): "local" | "hindsight" | null {
  if (event.isComposing || event.defaultPrevented || event.altKey || event.shiftKey) return null;
  if (!hasPrimaryModifier(event, macOnly)) return null;
  if (event.code === "Digit1" || event.key === "1") return "local";
  if (event.code === "Digit2" || event.key === "2") return "hindsight";
  return null;
}
