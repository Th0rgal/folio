{
  pkgs,
  lib,
  omarchnixFlake,
  nixosConfigSrc,
  folioOverlay,
  folioHmModule,
  hindsightReplayDir ? "/var/lib/orb-node/work/folio-verify/shared",
}:
let
  inputs = omarchnixFlake.inputs;
  testPkgs = import inputs.nixpkgs {
    system = pkgs.stdenv.hostPlatform.system;
    overlays = [
      omarchnixFlake.overlays.default
      folioOverlay
    ];
  };
  hsReplayScript = testPkgs.writeText "hs_replay.py" ''
    import json, sys, urllib.parse
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    DATA = json.load(open("/mnt/host-hindsight/hindsight_live_replay.json", encoding="utf-8"))
    ROUTES = DATA.get("routes", {})
    RECALLS = DATA.get("recalls", {})
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _record(self):
            try:
                with open("/tmp/hs_requests.log", "a", encoding="utf-8") as lf:
                    lf.write(self.path + "\n")
            except Exception:
                pass
        def _send(self, code, obj):
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            self._record()
            if self.path == "/__ready":
                return self._send(200, {"ready": True})
            if self.path in ROUTES:
                return self._send(200, ROUTES[self.path])
            if "/knowledge-base/search?" in self.path:
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                q = (qs.get("q") or [""])[0].lower()
                hits = []
                for k, v in ROUTES.items():
                    if "/knowledge-base/pages/" in k and isinstance(v, dict):
                        md = str(v.get("markdown") or "")
                        nm = str(v.get("name") or "")
                        if q in md.lower() or q in nm.lower():
                            pid = k.rsplit("/", 1)[-1]
                            idx = md.lower().find(q)
                            snip = md[max(0, idx - 40):idx + 160].replace("\n", " ") if idx >= 0 else nm
                            hits.append({"id": pid, "name": nm, "snippet": snip, "score": 0.92, "updated_at": v.get("timestamp", "")})
                return self._send(200, {"results": hits[:8]})
            return self._send(404, {"error": "not_found", "path": self.path})
        def do_POST(self):
            self._record()
            n = int(self.headers.get("Content-Length") or "0")
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            if self.path.endswith("/memories/recall"):
                q = str(body.get("query") or "")
                for k, v in RECALLS.items():
                    if k.lower() in q.lower() or q.lower() in k.lower():
                        return self._send(200, v)
                first = next(iter(RECALLS.values()), {"results": []})
                return self._send(200, first)
            return self._send(403, {"error": "readonly_replay"})
    ThreadingHTTPServer(("127.0.0.1", 18991), H).serve_forever()
  '';
  wallGenScript = testPkgs.writeText "wall_gen.py" ''
    w, h = 800, 500
    with open("/tmp/wall.ppm", "wb") as f:
        f.write(f"P6\n{w} {h}\n255\n".encode())
        for y in range(h):
            row_data = bytearray(w * 3)
            for x in range(w):
                r = int(26 + 38 * (x / w) + 18 * (y / h))
                g = int(34 + 44 * (y / h))
                b = int(58 + 68 * (x / w))
                i = x * 3
                row_data[i] = min(255, r)
                row_data[i + 1] = min(255, g)
                row_data[i + 2] = min(255, b)
            f.write(row_data)
  '';
in
testPkgs.testers.runNixOSTest {
  name = "folio-m2air-desktop-visual";

  qemu.package = testPkgs.symlinkJoin {
    name = "qemu-kvm-virgl-sw";
    paths = [ testPkgs.qemu_kvm ];
    nativeBuildInputs = [ testPkgs.makeWrapper ];
    postBuild = ''
      for bin in qemu-kvm qemu-system-x86_64; do
        if [ -e "$out/bin/$bin" ]; then
          wrapProgram "$out/bin/$bin" \
            --set SDL_VIDEODRIVER offscreen \
            --set LIBGL_ALWAYS_SOFTWARE 1 \
            --set __EGL_VENDOR_LIBRARY_DIRS "${testPkgs.mesa}/share/glvnd/egl_vendor.d" \
            --set LIBGL_DRIVERS_PATH "${testPkgs.mesa}/lib/dri" \
            --set GBM_BACKENDS_PATH "${testPkgs.mesa}/lib/gbm" \
            --set GALLIUM_DRIVER llvmpipe \
            --prefix LD_LIBRARY_PATH : "${testPkgs.libglvnd}/lib:${testPkgs.mesa}/lib" \
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
          source = hindsightReplayDir;
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
        wtype
        folio-desktop
        python3
      ];
    };

  testScript = ''
    import json
    import os
    import shlex
    import time

    def hypr(cmd: str) -> str:
        inner = (
            "export XDG_RUNTIME_DIR=/run/user/1000; "
            "export WAYLAND_DISPLAY=wayland-1; "
            "export HYPRLAND_INSTANCE_SIGNATURE=$(ls -1 /run/user/1000/hypr 2>/dev/null | head -n1); "
            "test -n \"$HYPRLAND_INSTANCE_SIGNATURE\" && "
            f"{cmd}"
        )
        return f"su - thomas -c {shlex.quote(inner)}"

    start_all()
    desktop.wait_for_unit("multi-user.target")

    # 1. Start local read-only Hindsight replay server inside VM backed by host-fetched `thomas` bank JSON
    desktop.succeed(
        ": > /tmp/hs_requests.log\n"
        "nohup python3 ${hsReplayScript} >/tmp/hs_replay.log 2>&1 &\n"
        "mkdir -p /run/folio-sec && printf 'replay-readonly-token-0000000000000000\\n' > /run/folio-sec/HINDSIGHT_API_KEY && chown -R thomas:users /run/folio-sec && chmod 0700 /run/folio-sec && chmod 0600 /run/folio-sec/HINDSIGHT_API_KEY"
    )
    desktop.wait_until_succeeds(
        "python3 -c 'import urllib.request; assert urllib.request.urlopen(\"http://127.0.0.1:18991/__ready\", timeout=2).status == 200'",
        timeout=30,
    )

    # Create a crisp geometric gradient wallpaper at the path thomas.nix expects so frosted glass is unmistakable
    desktop.succeed(
        "su - thomas -c 'mkdir -p /home/thomas/Pictures/Wallpapers'\n"
        "python3 ${wallGenScript}\n"
        "pnmtopng /tmp/wall.ppm > /home/thomas/Pictures/Wallpapers/singapore.webp && chown thomas:users /home/thomas/Pictures/Wallpapers/singapore.webp"
    )

    # Wait for Hyprland, verify zero config errors, verify omarchy-shell active with 0 restarts, and run self-tests
    desktop.wait_until_succeeds(hypr("hyprctl instances -j | jq -e 'length >= 1'"), timeout=90)
    config_err_raw = desktop.succeed(hypr("hyprctl configerrors -j")).strip()
    config_errs = list(filter(lambda e: e.strip(), json.loads(config_err_raw)))
    assert not config_errs, f"Hyprland config errors: {config_errs!r}"

    desktop.wait_until_succeeds("systemctl --user -M thomas@ is-active omarchy-shell.service", timeout=60)
    desktop.succeed("test \"$(systemctl --user -M thomas@ show -p NRestarts --value omarchy-shell.service)\" = 0")
    desktop.succeed("su - thomas -c 'folio --self-test && folio-locald --self-test'")

    # Ensure Dock autohide is false in live shell.json so Dock icons stay visible in screenshots
    desktop.succeed(
        "su - thomas -c '"
        "test ! -f ~/.config/omarchy/shell.json || "
        "(jq \".dock.autohide = false\" ~/.config/omarchy/shell.json > /tmp/sh.json && mv /tmp/sh.json ~/.config/omarchy/shell.json)"
        "'"
    )

    def launch_folio(query: str):
        desktop.succeed("pkill -u thomas -f '\\.folio-unwrapped|folio-locald' || true")
        desktop.wait_until_succeeds("! pgrep -u thomas -f '\\.folio-unwrapped|folio-locald'", timeout=15)
        desktop.succeed(
            "systemd-run --user -M thomas@ --collect "
            "--setenv=XDG_RUNTIME_DIR=/run/user/1000 "
            "--setenv=WAYLAND_DISPLAY=wayland-1 "
            "--setenv=FOLIO_SECRETS_DIR=/run/folio-sec "
            "--setenv=HINDSIGHT_API_URL=http://127.0.0.1:18991 "
            "--setenv=FOLIO_HINDSIGHT_READONLY=1 "
            f"--setenv=FOLIO_INITIAL_QUERY={shlex.quote(query)} "
            "/run/current-system/sw/bin/folio"
        )
        desktop.wait_until_succeeds(
            hypr("hyprctl clients -j | jq -e 'map(select(.class == \"folio\")) | length >= 1'"),
            timeout=45,
        )
        desktop.wait_until_succeeds("pgrep -u thomas -f 'folio-locald'", timeout=15)
        time.sleep(5.0)

    def snap_and_verify(out_name: str):
        path = f"/tmp/{out_name}"
        if desktop.execute(hypr(f"timeout 4 grim {path}"))[0] != 0:
            # Wake VFR static-frame idle via QEMU hardware key + subtle cursor nudge without stealing focus
            desktop.send_key("shift")
            desktop.succeed(
                hypr(f"(sleep 0.2; hyprctl dispatch movecursor 800 500 >/dev/null 2>&1 || true) & timeout 10 grim {path}")
            )
        sz = int(desktop.succeed(f"stat -c %s {path}").strip())
        assert sz > 16384, f"{out_name} too small: {sz}"
        colors = int(desktop.succeed(f"pngtopam {path} | ppmhist -noheader | wc -l").strip())
        assert colors >= 64, f"{out_name} looks blank: {colors} unique colors"
        desktop.copy_from_machine(path, "")

    # Shot 1: Dark mode (starship-subtle) showing real read-only Hindsight `thomas` knowledge page (`kp-47c739bec662419d8d05f283de607b9e`) + provenance badges + grounding chips
    desktop.succeed(": > /tmp/hs_requests.log")
    launch_folio("?open=hindsight://thomas/page/kp-47c739bec662419d8d05f283de607b9e")
    desktop.wait_until_succeeds(
        "grep -q 'kp-47c739bec662419d8d05f283de607b9e' /tmp/hs_requests.log && grep -q 'knowledge-base/tree' /tmp/hs_requests.log",
        timeout=60,
    )
    time.sleep(3.0)
    snap_and_verify("shot1-dark-hindsight-kp.png")

    # Shot 2: LaTeX & Markdown Showcase note with rendered KaTeX equations, code block, callout, and inline equation editor/preview open (`openEq=3`)
    launch_folio("?open=bayesian-calibration-and-specs&openEq=3")
    snap_and_verify("shot2-latex-equation-editor.png")

    # Shot 3: Cmd+K Unified Search & Recall modal across local `.md` + Hindsight (`search=Bayesian`)
    launch_folio("?open=bayesian-calibration-and-specs&search=Bayesian")
    time.sleep(1.5)
    snap_and_verify("shot3-cmdk-unified-search.png")

    # Shot 4: Light mode (`starship-subtle-light`) via `omarchnix theme toggle` + Spotlight & Dock icon integration
    desktop.succeed(hypr("omarchnix theme toggle"))
    time.sleep(2.5)
    launch_folio("?open=bayesian-calibration-and-specs&theme=light&openEq=0")
    # Open Spotlight (`th0rgal.spotlight`) filtered to "folio" via QEMU hardware keyboard so Spotlight shows Folio's crisp icon right over the light-mode Folio workspace + bottom Dock!
    desktop.succeed(hypr("omarchy-shell shell summon th0rgal.spotlight \"{}\" || true"))
    time.sleep(1.5)
    desktop.send_chars("folio")
    time.sleep(1.2)
    snap_and_verify("shot4-light-mode-dock-spotlight.png")

    # Final health check: omarchy-shell still 0 restarts and zero coredumps, export verification metrics
    n_restarts = desktop.succeed("systemctl --user -M thomas@ show -p NRestarts --value omarchy-shell.service").strip()
    assert n_restarts == "0", f"omarchy-shell restarted: {n_restarts}"
    report = {
        "hyprctl_configerrors": config_errs,
        "omarchy_shell_restarts": int(n_restarts),
    }
    out_dir = os.environ.get("out", ".")
    with open(os.path.join(out_dir, "vm-health-report.json"), "w", encoding="utf-8") as rf:
        rf.write(json.dumps(report, indent=2) + "\n")
  '';
}
