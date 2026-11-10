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

      pnpmDepsHash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";

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
