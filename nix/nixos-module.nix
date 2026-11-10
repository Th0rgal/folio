{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.programs.folio;
in
{
  options.programs.folio = {
    enable = lib.mkEnableOption "Folio desktop client system integration";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.folio-desktop;
      description = "Folio package to install in `environment.systemPackages`.";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ cfg.package ];
  };
}
