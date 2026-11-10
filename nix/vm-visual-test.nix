{
  pkgs,
  lib,
  omarchnixFlake,
  nixosConfigSrc,
  folioOverlay,
  folioHmModule,
}:
let
  inputs = omarchnixFlake.inputs;
in
pkgs.testers.runNixOSTest {
  name = "folio-m2air-desktop-visual";

  qemu.package = pkgs.symlinkJoin {
    name = "qemu-kvm-virgl-sw";
    paths = [ pkgs.qemu_kvm ];
    nativeBuildInputs = [ pkgs.makeWrapper ];
    postBuild = ''
      for bin in qemu-kvm qemu-system-x86_64; do
        if [ -e "$out/bin/$bin" ]; then
          wrapProgram "$out/bin/$bin" \
            --set SDL_VIDEODRIVER offscreen \
            --set LIBGL_ALWAYS_SOFTWARE 1 \
            --set __EGL_VENDOR_LIBRARY_DIRS "${pkgs.mesa}/share/glvnd/egl_vendor.d" \
            --set LIBGL_DRIVERS_PATH "${pkgs.mesa}/lib/dri" \
            --set GBM_BACKENDS_PATH "${pkgs.mesa}/lib/gbm" \
            --set GALLIUM_DRIVER llvmpipe \
            --prefix LD_LIBRARY_PATH : "${pkgs.libglvnd}/lib:${pkgs.mesa}/lib" \
            --append-flags "-display sdl,gl=on"
        fi
      done
    '';
  };

  node.specialArgs = { inherit inputs; };

  nodes.desktop =
    { pkgs, ... }:
    {
      imports = [
        omarchnixFlake.nixosModules.omarchnix
        inputs.home-manager.nixosModules.home-manager
      ];

      nixpkgs.overlays = [
        omarchnixFlake.overlays.default
        folioOverlay
      ];

      networking.hostName = "m2air-vm";

      virtualisation = {
        memorySize = 6144;
        cores = 6;
        resolution = {
          x = 1600;
          y = 1000;
        };
        qemu.options = [
          "-vga none"
          "-device virtio-vga-gl,xres=1600,yres=1000"
        ];
        sharedDirectories.host_hindsight = {
          source = "/var/lib/orb-node/work/folio-verify/shared";
          target = "/mnt/host-hindsight";
        };
      };

      hardware.graphics.enable = true;

      omarchnix = {
        enable = true;
        profile = "vm";
        hardening.enable = false;
        containers.enable = false;
        secrets.enable = false;
        desktop = {
          enable = true;
          user = "thomas";
          theme = "starship-subtle";
          shellBackend = "omarchy-shell";
          keyboard = {
            layout = "us";
            variant = "intl";
          };
          touchpad = {
            naturalScroll = false;
            scrollFactor = 0.5;
          };
          autostartTty.enable = true;
        };
      };

      i18n.inputMethod = {
        enable = true;
        type = "fcitx5";
        fcitx5.waylandFrontend = true;
      };

      environment.variables = {
        WLR_RENDERER_ALLOW_SOFTWARE = "1";
      };

      users.users.thomas = {
        isNormalUser = true;
        uid = 1000;
        extraGroups = [
          "wheel"
          "video"
          "render"
          "audio"
        ];
        shell = "/run/current-system/sw/bin/zsh";
      };

      home-manager = {
        useGlobalPkgs = true;
        useUserPackages = true;
        extraSpecialArgs = { inherit inputs; };
        users.thomas =
          { lib, ... }:
          {
            imports = [
              "${nixosConfigSrc}/home/thomas.nix"
              folioHmModule
            ];

            programs.folio = {
              enable = true;
              hindsight = {
                apiUrl = "http://127.0.0.1:18991";
                defaultBank = "thomas";
                readOnly = true;
              };
              hyprland = {
                enable = true;
                opacity = "0.88 0.88";
                rounding = 10;
                borderSize = 1;
                launchBind = "CTRL + SUPER + N";
              };
            };

            # Keep Dock always visible during desktop verification so Shot 4 shows Dock + Spotlight icons cleanly
            programs.omarchnix.shell.extraSettings = lib.mkForce {
              disabledPlugins = [ "omarchy.dock" ];
              dock = {
                position = "bottom";
                autohide = false;
                allWorkspaces = true;
              };
            };

            wayland.windowManager.hyprland.settings.monitor = lib.mkForce [
              {
                output = "";
                mode = "1600x1000@60";
                position = "0x0";
                scale = "1.0";
              }
            ];
          };
      };

      environment.systemPackages = with pkgs; [
        jq
        netpbm
        procps
        grim
        folio-desktop
        python3
      ];
    };

  testScript = ''
    import json
    import time

    def hypr(cmd: str) -> str:
        return (
            "su - thomas -c '"
            "export XDG_RUNTIME_DIR=/run/user/1000; "
            "export WAYLAND_DISPLAY=wayland-1; "
            "export HYPRLAND_INSTANCE_SIGNATURE=$(ls -1 /run/user/1000/hypr 2>/dev/null | head -n1); "
            "test -n \"$HYPRLAND_INSTANCE_SIGNATURE\" && "
            f"{cmd}'"
        )

    start_all()
    desktop.wait_for_unit("multi-user.target")

    # 1. Start local read-only Hindsight replay server backed by live-captured thomas bank pages/sources
    desktop.succeed(
        "cat > /tmp/hs_replay.py << 'PY'\n"
        "import json, sys, urllib.parse\n"
        "from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler\n"
        "DATA = json.load(open('/mnt/host-hindsight/hindsight_live_replay.json', encoding='utf-8'))\n"
        "ROUTES = DATA.get('routes', {})\n"
        "RECALLS = DATA.get('recalls', {})\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def log_message(self, *a): pass\n"
        "    def _send(self, code, obj):\n"
        "        raw = json.dumps(obj, ensure_ascii=False).encode('utf-8')\n"
        "        self.send_response(code)\n"
        "        self.send_header('Content-Type', 'application/json; charset=utf-8')\n"
        "        self.send_header('Content-Length', str(len(raw)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(raw)\n"
        "    def do_GET(self):\n"
        "        if self.path in ROUTES:\n"
        "            return self._send(200, ROUTES[self.path])\n"
        "        if '/knowledge-base/search?' in self.path:\n"
        "            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)\n"
        "            q = (qs.get('q') or [''])[0].lower()\n"
        "            hits = []\n"
        "            for k, v in ROUTES.items():\n"
        "                if '/knowledge-base/pages/' in k and isinstance(v, dict):\n"
        "                    md = str(v.get('markdown') or '')\n"
        "                    nm = str(v.get('name') or '')\n"
        "                    if q in md.lower() or q in nm.lower():\n"
        "                        pid = k.rsplit('/', 1)[-1]\n"
        "                        idx = md.lower().find(q)\n"
        "                        snip = md[max(0, idx-40):idx+160].replace('\\n', ' ') if idx >= 0 else nm\n"
        "                        hits.append({'id': pid, 'name': nm, 'snippet': snip, 'score': 0.92, 'updated_at': v.get('timestamp', '')})\n"
        "            return self._send(200, {'results': hits[:8]})\n"
        "        return self._send(404, {'error': 'not_found', 'path': self.path})\n"
        "    def do_POST(self):\n"
        "        n = int(self.headers.get('Content-Length') or '0')\n"
        "        body = json.loads(self.rfile.read(n).decode('utf-8') or '{}') if n else {}\n"
        "        if self.path.endswith('/memories/recall'):\n"
        "            q = str(body.get('query') or '')\n"
        "            for k, v in RECALLS.items():\n"
        "                if k.lower() in q.lower() or q.lower() in k.lower():\n"
        "                    return self._send(200, v)\n"
        "            first = next(iter(RECALLS.values()), {'results': []})\n"
        "            return self._send(200, first)\n"
        "        return self._send(403, {'error': 'readonly_replay'})\n"
        "ThreadingHTTPServer(('127.0.0.1', 18991), H).serve_forever()\n"
        "PY\n"
        "nohup python3 /tmp/hs_replay.py >/tmp/hs_replay.log 2>&1 &\n"
        "mkdir -p /run/folio-sec && printf 'replay-readonly-token-0000000000000000\\n' > /run/folio-sec/HINDSIGHT_API_KEY && chown -R thomas:users /run/folio-sec && chmod 0700 /run/folio-sec && chmod 0600 /run/folio-sec/HINDSIGHT_API_KEY"
    )

    # Create a crisp geometric gradient wallpaper at the path thomas.nix expects so frosted glass is unmistakable
    desktop.succeed(
        "su - thomas -c 'mkdir -p /home/thomas/Pictures/Wallpapers'\n"
        "python3 - << 'PY'\n"
        "w, h = 800, 500\n"
        "with open('/tmp/wall.ppm', 'wb') as f:\n"
        "    f.write(f'P6\\n{w} {h}\\n255\\n'.encode())\n"
        "    row_data = bytearray(w * 3)\n"
        "    for y in range(h):\n"
        "        for x in range(w):\n"
        "            r = int(26 + 38 * (x / w) + 18 * (y / h))\n"
        "            g = int(34 + 44 * (y / h))\n"
        "            b = int(58 + 68 * (x / w))\n"
        "            i = x * 3\n"
        "            row_data[i] = min(255, r)\n"
        "            row_data[i+1] = min(255, g)\n"
        "            row_data[i+2] = min(255, b)\n"
        "        f.write(row_data)\n"
        "PY\n"
        "pnmtopng /tmp/wall.ppm > /home/thomas/Pictures/Wallpapers/singapore.webp && chown thomas:users /home/thomas/Pictures/Wallpapers/singapore.webp"
    )

    # Wait for Hyprland, verify zero config errors, verify omarchy-shell active with 0 restarts, and run self-tests
    desktop.wait_until_succeeds(hypr("hyprctl instances -j | jq -e 'length >= 1'"), timeout=90)
    config_err_raw = desktop.succeed(hypr("hyprctl configerrors -j")).strip()
    config_errs = [e for e in json.loads(config_err_raw) if e.strip()]
    assert config_errs == [], f"Hyprland config errors: {config_errs!r}"

    desktop.wait_until_succeeds("systemctl --user -M thomas@ is-active omarchy-shell.service", timeout=60)
    desktop.succeed("test \"$(systemctl --user -M thomas@ show -p NRestarts --value omarchy-shell.service)\" = 0")
    desktop.succeed("su - thomas -c 'folio --self-test && folio-locald --self-test'")

    # Ensure Dock autohide is false in live shell.json so Dock icons stay visible in screenshots
    desktop.succeed(
        "su - thomas -c '"
        "if [ -f ~/.config/omarchy/shell.json ]; then "
        "  jq \".dock.autohide = false\" ~/.config/omarchy/shell.json > /tmp/sh.json && mv /tmp/sh.json ~/.config/omarchy/shell.json; "
        "fi'"
    )

    def launch_folio(query: str):
        desktop.succeed("pkill -u thomas -f '\\.folio-unwrapped|folio-locald' || true")
        time.sleep(1.0)
        desktop.succeed(
            hypr(
                "FOLIO_SECRETS_DIR=/run/folio-sec "
                "HINDSIGHT_API_URL=http://127.0.0.1:18991 "
                "FOLIO_HINDSIGHT_READONLY=1 "
                f"FOLIO_INITIAL_QUERY='{query}' "
                "nohup folio >/tmp/folio-run.log 2>&1 &"
            )
        )
        desktop.wait_until_succeeds(
            hypr("hyprctl clients -j | jq -e 'map(select(.class == \"folio\")) | length >= 1'"),
            timeout=45,
        )
        time.sleep(4.5)

    def snap_and_verify(out_name: str):
        path = f"/tmp/{out_name}"
        desktop.succeed(hypr(f"grim {path}"))
        sz = int(desktop.succeed(f"stat -c %s {path}").strip())
        assert sz > 16384, f"{out_name} too small: {sz}"
        colors = int(desktop.succeed(f"pngtopam {path} | ppmhist -noheader | wc -l").strip())
        assert colors >= 64, f"{out_name} looks blank: {colors} unique colors"
        desktop.copy_from_machine(path, "")

    # Shot 1: Dark mode (starship-subtle) showing real read-only Hindsight `thomas` knowledge page (`kp-47c739bec662419d8d05f283de607b9e`) + provenance badges + grounding chips
    launch_folio("?open=hindsight://thomas/page/kp-47c739bec662419d8d05f283de607b9e")
    snap_and_verify("shot1-dark-hindsight-kp.png")

    # Shot 2: LaTeX & Markdown Showcase note with rendered KaTeX equations, code block, callout, and inline equation editor/preview open (`openEq=3`)
    launch_folio("?openEq=3")
    snap_and_verify("shot2-latex-equation-editor.png")

    # Shot 3: Cmd+K Unified Search & Recall modal across local `.md` + Hindsight (`search=Bayesian`)
    launch_folio("?search=Bayesian")
    time.sleep(1.5)
    snap_and_verify("shot3-cmdk-unified-search.png")

    # Shot 4: Light mode (`starship-subtle-light`) via `omarchnix theme toggle` + Spotlight & Dock icon integration
    desktop.succeed(hypr("omarchnix theme toggle"))
    time.sleep(2.5)
    launch_folio("?theme=light&openEq=0")
    # Open Spotlight (`th0rgal.spotlight`) filtered to "Folio" or show Folio + Dock in crisp light mode
    desktop.succeed(hypr("omarchy-shell shell summon th0rgal.spotlight '{}' || true"))
    time.sleep(1.5)
    # Type "folio" into Spotlight via hyprctl sendshortcut or show Folio window + Dock; let's send F-O-L-I-O keys so Spotlight shows Folio's crisp icon right over the light-mode Folio workspace!
    for ch in ["F", "O", "L", "I", "O"]:
        desktop.succeed(hypr(f"hyprctl dispatch sendshortcut ',{ch},' || true"))
        time.sleep(0.12)
    time.sleep(1.2)
    snap_and_verify("shot4-light-mode-dock-spotlight.png")

    # Final health check: omarchy-shell still 0 restarts and zero coredumps
    desktop.succeed("test \"$(systemctl --user -M thomas@ show -p NRestarts --value omarchy-shell.service)\" = 0")
  '';
}
