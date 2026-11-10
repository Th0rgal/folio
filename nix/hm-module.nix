{
  config,
  lib,
  pkgs,
  options,
  ...
}:
let
  cfg = config.programs.folio;
  inherit (lib.generators) mkLuaInline;
  folioExe = "${cfg.package}/bin/folio";
in
{
  options.programs.folio = {
    enable = lib.mkEnableOption "Folio local-first Markdown, LaTeX, and Hindsight desktop client";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.folio-desktop;
      description = "Folio desktop package to install.";
    };

    defaultNotesDir = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Optional default local Markdown workspace path (`FOLIO_DEFAULT_NOTES_DIR`).";
    };

    hindsight = {
      apiUrl = lib.mkOption {
        type = lib.types.str;
        default = "https://memory.thomas.md";
        description = "Hindsight (`spark-memory`) endpoint URL.";
      };

      defaultBank = lib.mkOption {
        type = lib.types.str;
        default = "thomas";
        description = "Default Hindsight memory bank ID.";
      };

      readOnly = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "When true, blocks mutations to remote Hindsight memory (`FOLIO_HINDSIGHT_READONLY=1`).";
      };

      apiKeyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Runtime path to a `0600` secret file (e.g. sops-nix) containing the Hindsight bearer token.";
      };
    };

    hyprland = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Install translucent Hyprland Lua window rule and launch shortcut matching Orb.";
      };

      opacity = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Optional Hyprland window opacity override (`null` matches Orb so CSS surface/sidebar translucency composites cleanly without dimming foreground text).";
      };

      rounding = lib.mkOption {
        type = lib.types.int;
        default = 10;
        description = "Corner rounding matching `starship-subtle` (`theme.ui.rounding`).";
      };

      borderSize = lib.mkOption {
        type = lib.types.int;
        default = 1;
        description = "Hairline border width matching `starship-subtle` (`theme.ui.borderSize`).";
      };

      launchBind = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = "CTRL + SUPER + N";
        description = "Hyprland Lua keybind to launch Folio (defaults to `CTRL + SUPER + N` so `CTRL + SUPER + F` fullscreen stays untouched).";
      };
    };
  };

  config = lib.mkIf cfg.enable (
    lib.mkMerge [
      {
        home.packages = [ cfg.package ];

        home.sessionVariables =
          {
            HINDSIGHT_API_URL = cfg.hindsight.apiUrl;
            HINDSIGHT_BANK_ID = cfg.hindsight.defaultBank;
          }
          // lib.optionalAttrs cfg.hindsight.readOnly {
            FOLIO_HINDSIGHT_READONLY = "1";
          }
          // lib.optionalAttrs (cfg.defaultNotesDir != null) {
            FOLIO_DEFAULT_NOTES_DIR = cfg.defaultNotesDir;
          }
          // lib.optionalAttrs (cfg.hindsight.apiKeyFile != null) {
            HINDSIGHT_API_KEY_FILE = cfg.hindsight.apiKeyFile;
          };
      }

      (lib.optionalAttrs (options ? wayland && options.wayland ? windowManager) (
        lib.mkIf (cfg.hyprland.enable && config.wayland.windowManager.hyprland.enable) {
          wayland.windowManager.hyprland.settings = {
            window_rule = lib.mkAfter [
              (
                {
                  name = "folio-vibrancy";
                  match = {
                    class = "^(folio|\\.folio-unwrapped|folio-desktop)$";
                  };
                  rounding = cfg.hyprland.rounding;
                  border_size = cfg.hyprland.borderSize;
                }
                // lib.optionalAttrs (cfg.hyprland.opacity != null) {
                  opacity = cfg.hyprland.opacity;
                }
              )
            ];

            bind = lib.mkIf (cfg.hyprland.launchBind != null) (
              lib.mkAfter [
                {
                  _args = [
                    cfg.hyprland.launchBind
                    (mkLuaInline ''hl.dsp.exec_cmd("${folioExe}")'')
                  ];
                }
              ]
            );
          };
        }
      ))
    ]
  );
}
