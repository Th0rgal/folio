{
  lib,
  stdenv,
  nodejs,
  pnpm,
  pnpmConfigHook,
  fetchPnpmDeps,
  python3,
  pkg-config,
  gtk3,
  webkitgtk_4_1 ? null,
  wrapGAppsHook3 ? null,
  makeWrapper,
  librsvg,
  libsecret ? null,
  xdg-utils ? null,
  cacert,
  apple-sdk_15 ? null,
  swift ? null,
  folioSrc,
  pnpmDepsHash ? "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
}:
let
  version = "0.1.0";

  pnpmDeps = fetchPnpmDeps {
    pname = "folio-ui";
    inherit version;
    src = "${folioSrc}/ui";
    fetcherVersion = 3;
    hash = pnpmDepsHash;
  };

  folio-ui = stdenv.mkDerivation {
    pname = "folio-ui";
    inherit version pnpmDeps;
    src = "${folioSrc}/ui";

    nativeBuildInputs = [
      nodejs
      pnpm
      pnpmConfigHook
    ];

    buildPhase = ''
      runHook preBuild
      pnpm test
      pnpm build
      runHook postBuild
    '';

    installPhase = ''
      runHook preInstall
      mkdir -p "$out"
      cp -r dist/. "$out/"
      runHook postInstall
    '';
  };

  folio-locald = stdenv.mkDerivation {
    pname = "folio-locald";
    inherit version;
    src = "${folioSrc}/daemon";

    nativeBuildInputs = [ makeWrapper ];

    doCheck = true;
    checkPhase = ''
      runHook preCheck
      PYTHONPATH=. ${python3}/bin/python3 -m unittest discover -s tests -v
      runHook postCheck
    '';

    installPhase = ''
      runHook preInstall
      mkdir -p "$out/lib/folio" "$out/bin"
      cp -r folio_daemon "$out/lib/folio/"
      cat > "$out/bin/.folio-locald-unwrapped" <<EOF
      #!${python3}/bin/python3
      import sys
      sys.path.insert(0, "$out/lib/folio")
      from folio_daemon.server import main
      raise SystemExit(main())
      EOF
      chmod 0755 "$out/bin/.folio-locald-unwrapped"
      makeWrapper "$out/bin/.folio-locald-unwrapped" "$out/bin/folio-locald" \
        --prefix PATH : "${
          lib.makeBinPath (
            lib.optionals (libsecret != null) [ libsecret ]
          )
        }" \
        --set-default SSL_CERT_FILE "${cacert}/etc/ssl/certs/ca-bundle.crt" \
        --set-default NIX_SSL_CERT_FILE "${cacert}/etc/ssl/certs/ca-bundle.crt"
      runHook postInstall
    '';
  };
in
if stdenv.hostPlatform.isLinux then
  stdenv.mkDerivation {
    pname = "folio-desktop";
    inherit version;
    src = "${folioSrc}/linux_shell";

    nativeBuildInputs = [
      pkg-config
      wrapGAppsHook3
      makeWrapper
      librsvg
    ];

    buildInputs = [
      gtk3
      webkitgtk_4_1
    ];

    dontWrapGApps = true;

    buildPhase = ''
      runHook preBuild
      make HAS_WEBKIT=1 folio
      runHook postBuild
    '';

    installPhase = ''
      runHook preInstall
      mkdir -p \
        "$out/bin" \
        "$out/share/folio" \
        "$out/share/applications" \
        "$out/share/pixmaps" \
        "$out/share/icons/hicolor/scalable/apps" \
        "$out/share/icons/hicolor/256x256/apps" \
        "$out/share/icons/hicolor/512x512/apps"

      install -m 0755 folio "$out/bin/.folio-unwrapped"
      ln -s "${folio-locald}/bin/folio-locald" "$out/bin/folio-locald"
      cp -r "${folio-ui}/." "$out/share/folio/"

      install -m 0644 "${folioSrc}/assets/folio.svg" "$out/share/icons/hicolor/scalable/apps/folio.svg"
      install -m 0644 "${folioSrc}/assets/folio.svg" "$out/share/pixmaps/folio.svg"
      rsvg-convert -w 256 -h 256 "${folioSrc}/assets/folio.svg" -o "$out/share/icons/hicolor/256x256/apps/folio.png"
      rsvg-convert -w 512 -h 512 "${folioSrc}/assets/folio.svg" -o "$out/share/icons/hicolor/512x512/apps/folio.png"
      install -m 0644 "$out/share/icons/hicolor/256x256/apps/folio.png" "$out/share/pixmaps/folio.png"

      substitute "${folioSrc}/assets/folio.desktop" "$out/share/applications/folio.desktop" \
        --replace-fail "@out@" "$out"

      makeWrapper "$out/bin/.folio-unwrapped" "$out/bin/folio" \
        "''${gappsWrapperArgs[@]}" \
        --prefix PATH : "/run/current-system/sw/bin:${
          lib.makeBinPath (
            [
              folio-locald
              python3
            ]
            ++ lib.optionals (libsecret != null) [ libsecret ]
            ++ lib.optionals (xdg-utils != null) [ xdg-utils ]
          )
        }" \
        --set FOLIO_LOCALD_BIN "${folio-locald}/bin/folio-locald" \
        --set FOLIO_UI_ROOT "$out/share/folio" \
        --set-default GTK_USE_PORTAL "1" \
        --set-default SSL_CERT_FILE "${cacert}/etc/ssl/certs/ca-bundle.crt" \
        --set-default NIX_SSL_CERT_FILE "${cacert}/etc/ssl/certs/ca-bundle.crt" \
        --run ${lib.escapeShellArg ''
          case " $* " in
            *" --self-test "*) ;;
            *)
              if [ -z "''${FOLIO_LOCALD_PORT:-}" ]; then
                export FOLIO_LOCALD_PORT=$(${python3}/bin/python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
              fi
              ;;
          esac
        ''}
      runHook postInstall
    '';

    passthru = {
      inherit folio-ui folio-locald;
    };

    meta = {
      description = "Folio local-first Markdown, offline KaTeX, and Hindsight desktop client";
      mainProgram = "folio";
      platforms = lib.platforms.linux ++ lib.platforms.darwin;
    };
  }
else
  stdenv.mkDerivation {
    pname = "folio-desktop";
    inherit version;
    src = "${folioSrc}/macos_shell";

    nativeBuildInputs =
      [ makeWrapper ]
      ++ lib.optionals (swift != null) [ swift ];

    buildInputs = lib.optionals (apple-sdk_15 != null) [ apple-sdk_15 ];

    buildPhase = ''
      runHook preBuild
      swiftc -O main.swift -o folio-macos
      runHook postBuild
    '';

    installPhase = ''
      runHook preInstall
      mkdir -p "$out/bin" "$out/Applications/Folio.app/Contents/MacOS" "$out/Applications/Folio.app/Contents/Resources/ui"
      install -m 0755 folio-macos "$out/Applications/Folio.app/Contents/MacOS/Folio"
      cp -r "${folio-ui}/." "$out/Applications/Folio.app/Contents/Resources/ui/"
      ln -s "${folio-locald}/bin/folio-locald" "$out/bin/folio-locald"
      makeWrapper "$out/Applications/Folio.app/Contents/MacOS/Folio" "$out/bin/folio" \
        --set FOLIO_LOCALD_BIN "${folio-locald}/bin/folio-locald" \
        --set FOLIO_UI_ROOT "$out/Applications/Folio.app/Contents/Resources/ui" \
        --set-default SSL_CERT_FILE "${cacert}/etc/ssl/certs/ca-bundle.crt"
      runHook postInstall
    '';

    passthru = {
      inherit folio-ui folio-locald;
    };

    meta = {
      description = "Folio local-first Markdown, offline KaTeX, and Hindsight desktop client";
      mainProgram = "folio";
      platforms = lib.platforms.darwin;
    };
  }
