# Folio

**Folio** is a fast, zero-Electron native desktop client for **local Markdown workspaces**, **offline KaTeX mathematics**, **Hindsight (`spark-memory`) knowledge hierarchies**, and **Orb-powered Knowledge Crawling & Note Editing** — built on the exact **Orb** architecture (`GTK4 + WebKitGTK 6.0` on Linux/Wayland, `AppKit + WKWebView` on macOS, and a hardened loopback `folio-locald` daemon).

---

## Highlights

- **Zero Electron / Zero Cloud Lock-in**: Native system WebView (`webkitgtk-6.0` + `gtk4` custom `folio://localhost/` scheme on Linux, `WKWebView` on macOS) + SolidJS UI + local SQLite WAL + FTS5 cache + Linux `inotify` directory watcher.
- **First-Class Note Writing & Offline LaTeX (`KaTeX`)**:
  - Bundled `.woff2` KaTeX fonts (zero network/CDN requests), interactive `- [ ]` task checkboxes, relative local image assets (`/__folio_asset`), and Orb-style zoomable/pannable Mermaid diagrams.
  - Upgraded Markdown + LaTeX editor with formatting/math toolbar, live `[[wikilink]]` / `[[kp-...]]` autocomplete, `/` slash commands, `Tab`/`Shift+Tab` indent, smart list continuation, and Daily Journal notes (`⌘⇧D` $\rightarrow$ `Journal/YYYY-MM-DD.md`).
  - Click any rendered equation to open the **inline TeX editor & live preview** with custom per-workspace macro definitions (`\R`, `\E`, `\norm`, etc.), `Copy TeX`, and lossless delimiter-preserving write-back.
- **Dedicated Hindsight Sync Bank (`folio`) + Multi-Source Workspace**:
  1. **`local-fs`**: Local `.md` directories (`~/Notes` + mounted folders via native GTK4 folder picker) with YAML frontmatter, `[[wikilinks]]`, backlinks, `SYNC`/`MOD`/`LOCAL` status badges, and atomic writes.
  2. **Dedicated `folio` Hindsight Sync (`⌘⇧S`)**: 1-click and batch content-addressed (`SHA-256`) sync of local Markdown notes into a dedicated `folio` Hindsight bank (`FOLIO_SYNC_BANK=folio`), `#private` tag guard (never leaves local disk), 1-click `Fork to Local` for remote pages, and explicit raw-first promotion to `thomas` (`save_source`).
  3. **`hindsight` & `notion`**: Recursive `kp-*` page trees across all banks, human-authored vs. agent-synthesized provenance badges, clickable `kp-*` / `src-*` grounding chips, two-phase progressive `<3ms` FTS5 + parallel Hindsight recall (`⌘⇧F`), and optional Notion API v1 sync.
- **Orb Knowledge Crawler & Surgical Note Copilot (`⌘J`)**:
  - Lightweight, bounded-4-hop SSE copilot connected directly to Orb Router (`~/.orb/connection.json` / `https://orbrouter.thomas.md/v1`) without heavy coding-agent overhead.
  - Instant switching across **`builtin/fast`**, **`builtin/smart`**, **`builtin/private`** (Sovereign Spark), and **`builtin/obliterated`** (Uncensored Spark) — with automatic clamping of `#private` notes to `builtin/private`.
  - Pre-seeded `<3ms` SQLite FTS5 context + parallel Hindsight crawling + staged unified-diff note edit & draft proposals with 1-click **Apply** and **Undo**.
- **Seamless `m2air` / `omarchnix` Integration**:
  - Orb-matched RGBA frosted-glass chrome, `GtkGestureZoom` 2-finger touchpad pinch, single-instance UNIX socket file/URI forwarding (`folio note.md`), `Cmd+K` Command Palette, `Cmd+W` surface close, and Apple Silicon M2 AGX Vulkan/DMA-BUF acceleration.

---

## Screenshots (`omarchnix` + Hyprland Wayland)

| Hindsight Knowledge Page (`starship-subtle` Dark) | Live Inline LaTeX (`KaTeX`) Equation Editor |
| :---: | :---: |
| ![Dark mode Hindsight KP](docs/screenshots/shot1-dark-hindsight-kp.png) | ![Inline LaTeX Equation Editor](docs/screenshots/shot2-latex-equation-editor.png) |
| **`Cmd+K` Unified Hybrid Search & Recall** | **Light Mode (`starship-subtle-light`) + Dock & Spotlight** |
| ![Cmd+K Unified Search](docs/screenshots/shot3-cmdk-unified-search.png) | ![Light Mode Dock & Spotlight](docs/screenshots/shot4-light-mode-dock-spotlight.png) |

---

## Repository Layout

- `linux_shell/` — Native GTK4 + WebKitGTK 6.0 Wayland shell (`main.c`, `Makefile`)
- `macos_shell/` — Native AppKit + WKWebView macOS shell (`main.swift`)
- `daemon/` — Hardened loopback `folio-locald` daemon (`SQLite WAL + FTS5`, `inotify` watcher, HTTP/1.1 keep-alive pool, `local-fs`, `hindsight`, `orb_knowledge`, `notion`, OS keyring / Orb `0600` secret resolution)
- `ui/` — SolidJS + TypeScript + Vite frontend (offline KaTeX, zoomable Mermaid, Prism + Lean 4 + Nix, upgraded Markdown+LaTeX editor, `Cmd+K` Command Palette, `Cmd+Shift+F` progressive recall, `Cmd+J` Orb Knowledge Copilot)
- `assets/` — Scalable SVG icon + Freedesktop `.desktop` entry (`StartupWMClass=folio`)
- `nix/` — Nix package derivation, Home Manager / NixOS modules, and QEMU/KVM VirGL Wayland visual test

---

## Building & Running with Nix

```bash
# Build Folio desktop binary
nix build .#folio

# Run self-tests (Linux shell + loopback daemon)
./result/bin/folio --self-test
./result/bin/folio-locald --self-test

# Run full flake checks (daemon unit tests + UI math parser tests + package build)
nix flake check
```

---

## Declarative `omarchnix` & `nixos-config` Integration (`m2air`)

### 1. Add `folio` flake input (`omarchnix/flake.nix` or `nixos-config/flake.nix`)

```nix
inputs.folio = {
  url = "github:Th0rgal/folio";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

### 2. Overlay & System Package (`hosts/m2air/default.nix`)

Add `folio.overlays.default` to `nixpkgs.overlays` and `folio-desktop` (or `folio`) to `environment.systemPackages`:

```nix
environment.systemPackages = with pkgs; [
  orb-desktop
  folio-desktop
];
```

### 3. Hyprland Lua Window Rule & Keybind (`omarchnix/home/hyprland.nix` or `home/thomas.nix`)

Import `folio.homeManagerModules.default` and enable `programs.folio`:

```nix
programs.folio = {
  enable = true;
  hindsight = {
    apiUrl = "https://memory.thomas.md";
    defaultBank = "thomas";
  };
  hyprland = {
    enable = true;
    # Note: CTRL + SUPER + F is fullscreen in omarchnix, so Folio defaults to CTRL + SUPER + N (configurable)
    launchBind = "CTRL + SUPER + N";
  };
};
```

### Credentials

Folio never stores tokens in SQLite or Nix store paths. Provide credentials at runtime via:
- `libsecret` / `gnome-keyring` (`secret-tool store --label="Folio Hindsight" service folio account hindsight_api_key`)
- macOS Keychain (`security add-generic-password -s folio -a hindsight_api_key -w ...`)
- `sops-nix` `0600` secret files via `HINDSIGHT_API_KEY_FILE` / `SPARK_MEMORY_API_TOKEN_FILE` / `NOTION_API_KEY_FILE`.
