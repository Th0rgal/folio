import { describe, expect, it } from "vitest";
import {
  type KeyEventLike,
  hasPrimaryModifier,
  historyNavDelta,
  isFindShortcut,
  isFocusModeShortcut,
  isNewNoteShortcut,
  isRefreshShortcut,
  isSaveShortcut,
  isSearchShortcut,
  isSidebarShortcut,
  isToggleSourceShortcut,
  isToggleSplitShortcut,
  sidebarSectionShortcut,
} from "./keyboardShortcuts";

function ev(overrides: Partial<KeyEventLike>): KeyEventLike {
  return {
    key: "",
    code: "",
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    isComposing: false,
    defaultPrevented: false,
    ...overrides,
  };
}

describe("Orb-matched keyboard shortcuts", () => {
  it("uses Cmd+Shift+F (and Ctrl+Shift+F on Linux) for global search instead of Cmd+K", () => {
    expect(isSearchShortcut(ev({ key: "F", code: "KeyF", metaKey: true, shiftKey: true }), false)).toBe(true);
    expect(isSearchShortcut(ev({ key: "F", code: "KeyF", ctrlKey: true, shiftKey: true }), false)).toBe(true);
    expect(isSearchShortcut(ev({ key: "k", code: "KeyK", metaKey: true }), false)).toBe(false);
    expect(isSearchShortcut(ev({ key: "f", code: "KeyF", metaKey: true, shiftKey: false }), false)).toBe(false);
  });

  it("rejects Hyprland WM Ctrl+Cmd chords (metaKey && ctrlKey)", () => {
    expect(
      hasPrimaryModifier(ev({ key: "F", code: "KeyF", metaKey: true, ctrlKey: true, shiftKey: true }), false),
    ).toBe(false);
    expect(
      isSearchShortcut(ev({ key: "F", code: "KeyF", metaKey: true, ctrlKey: true, shiftKey: true }), false),
    ).toBe(false);
  });

  it("matches Cmd+F local find, Cmd+B sidebar, Cmd+\\ focus mode, Cmd+/ source, Cmd+Shift+/ split", () => {
    expect(isFindShortcut(ev({ key: "f", code: "KeyF", metaKey: true }), false)).toBe(true);
    expect(isSidebarShortcut(ev({ key: "b", code: "KeyB", metaKey: true }), false)).toBe(true);
    expect(
      isSidebarShortcut(
        ev({
          key: "b",
          code: "KeyB",
          metaKey: false,
          getModifierState: (mod) => mod === "Super",
        }),
        false,
      ),
    ).toBe(true);
    expect(isFocusModeShortcut(ev({ key: "\\", code: "Backslash", metaKey: true }), false)).toBe(true);
    expect(isToggleSourceShortcut(ev({ key: "/", code: "Slash", metaKey: true }), false)).toBe(true);
    expect(isToggleSplitShortcut(ev({ key: "?", code: "Slash", metaKey: true, shiftKey: true }), false)).toBe(true);
  });

  it("matches Cmd+N, Cmd+S, Cmd+R, Cmd+[/], and Cmd+1/2 navigation", () => {
    expect(isNewNoteShortcut(ev({ key: "n", code: "KeyN", metaKey: true }), false)).toBe(true);
    expect(isSaveShortcut(ev({ key: "s", code: "KeyS", metaKey: true }), false)).toBe(true);
    expect(isRefreshShortcut(ev({ key: "r", code: "KeyR", metaKey: true }), false)).toBe(true);
    expect(historyNavDelta(ev({ key: "[", code: "BracketLeft", metaKey: true }), false)).toBe(-1);
    expect(historyNavDelta(ev({ key: "]", code: "BracketRight", metaKey: true }), false)).toBe(1);
    expect(sidebarSectionShortcut(ev({ key: "1", code: "Digit1", metaKey: true }), false)).toBe("local");
    expect(sidebarSectionShortcut(ev({ key: "2", code: "Digit2", metaKey: true }), false)).toBe("hindsight");
    expect(sidebarSectionShortcut(ev({ key: "3", code: "Digit3", metaKey: true }), false)).toBe("notion");
  });
});
