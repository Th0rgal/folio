{
  description = "Folio — Local-first Markdown, offline KaTeX, and Hindsight desktop client (GTK3/WebKitGTK on Wayland + AppKit/WKWebView on macOS)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;

      pnpmDepsHash = "sha256-ddCy5/Fci/Ldx8EuqnTz86BevFnOn1wCkS33ffZCWTI=";

      folioOverlay = final: prev: {
        folio-desktop = final.callPackage ./nix/package.nix {
          folioSrc = self;
          inherit pnpmDepsHash;
        };
        folio = final.folio-desktop;
        folio-locald = final.folio-desktop.passthru.folio-locald;
        folio-ui = final.folio-desktop.passthru.folio-ui;
      };
    in
    {
      overlays = {
        folio = folioOverlay;
        default = folioOverlay;
      };

      nixosModules = {
        folio = import ./nix/nixos-module.nix;
        default = self.nixosModules.folio;
      };

      homeManagerModules = {
        folio = import ./nix/hm-module.nix;
        default = self.homeManagerModules.folio;
      };

      packages = forAllSystems (
        system:
        let
          pkgs = import nixpkgs {
            inherit system;
            overlays = [ folioOverlay ];
          };
        in
        {
          folio = pkgs.folio-desktop;
          folio-desktop = pkgs.folio-desktop;
          folio-locald = pkgs.folio-locald;
          folio-ui = pkgs.folio-ui;
          default = pkgs.folio-desktop;
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs {
            inherit system;
            overlays = [ folioOverlay ];
          };
          isLinux = pkgs.stdenv.hostPlatform.isLinux;
        in
        {
          default = pkgs.mkShell.override { stdenv = pkgs.clangStdenv; } {
            packages =
              [
                pkgs.nodejs
                pkgs.pnpm_10
                pkgs.python3
                pkgs.pkg-config
              ]
              ++ nixpkgs.lib.optionals isLinux [
                pkgs.gtk3
                pkgs.webkitgtk_4_1
                pkgs.librsvg
                pkgs.libsecret
                pkgs.xdg-utils
              ];

            shellHook = ''
              export CC=clang
            ''
            + nixpkgs.lib.optionalString isLinux ''
              if [ -d /run/opengl-driver ]; then
                export XDG_DATA_DIRS="/run/opengl-driver/share''${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}"
                export LD_LIBRARY_PATH="/run/opengl-driver/lib''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
                export LIBGL_DRIVERS_PATH="/run/opengl-driver/lib/dri''${LIBGL_DRIVERS_PATH:+:$LIBGL_DRIVERS_PATH}"
                export GBM_BACKENDS_PATH="/run/opengl-driver/lib/gbm''${GBM_BACKENDS_PATH:+:$GBM_BACKENDS_PATH}"
                [ -d /run/opengl-driver/share/glvnd/egl_vendor.d ] && export __EGL_VENDOR_LIBRARY_DIRS="/run/opengl-driver/share/glvnd/egl_vendor.d''${__EGL_VENDOR_LIBRARY_DIRS:+:$__EGL_VENDOR_LIBRARY_DIRS}"
              fi
            '';
          };
        }
      );

      checks = forAllSystems (
        system:
        let
          pkgs = import nixpkgs {
            inherit system;
            overlays = [ folioOverlay ];
          };
        in
        {
          folio-locald = pkgs.folio-locald;
          folio-ui = pkgs.folio-ui;
        }
        // nixpkgs.lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
          folio-self-test =
            pkgs.runCommand "folio-self-test"
              {
                nativeBuildInputs = [ pkgs.folio-desktop ];
              }
              ''
                export HOME="$TMPDIR/home"
                mkdir -p "$HOME"
                folio --self-test
                folio-locald --self-test
                touch "$out"
              '';
        }
      );
    };
}
