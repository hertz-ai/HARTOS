{ lib, pkgs
, hartSrc ? null   # Accepted only for call-site compatibility: hart-liquid-ui.nix
                   # and hart-nunba.nix both `callPackage` this file with
                   # `{ inherit hartSrc; }`, and callPackage passes the override
                   # through verbatim, so the formal MUST exist or eval fails with
                   # "called with unexpected argument 'hartSrc'". The Nunba daemon
                   # is HARTOS-EXCLUDED — it fetches ONLY the Nunba repo, never the
                   # HART tree — so hartSrc is intentionally unused here.
, nunbaRev ? "cb849ba96a6d103be3eb1c25f09d14a7324d3165"  # Nunba HEAD carrying the
                   # HART_NUNBA_SOCKET bind (main.py, commit cb849ba9). nunbaHash +
                   # npmDepsHash are now BOTH pinned for THIS rev (CI hash-pin R3,
                   # 2026-07-12); if the rev is bumped, re-pin all three in ONE commit
                   # (npm ci fails the lock-vs-deps integrity check if the rev and the
                   # lock drift apart).
, nunbaHash ? "sha256-vEVEuk8kTSKqc5zlCTj9MBjxP9/XLZp63zxeF0K91U4="  # pinned 2026-07-12 for nunbaRev cb849ba9 (CI hash-pin, R3)
, npmDepsHash ? "sha256-QYp9XbZ+2q2mCZDiGS9kObEOPQXDZbE1s882st7+oqA="  # pinned 2026-07-12 for nunbaRev cb849ba9 (CI hash-pin round 2, R3); prefetch-npm-deps landing-page/package-lock.json
, backendUrl ? "http://127.0.0.1:6777"
}:

# ═══════════════════════════════════════════════════════════════
# Nunba — the FULL desktop companion (Python + React) as a NATIVE
# HART OS daemon.  HARTOS-EXCLUDED, GUI-less, ML/TTS/LLM-less.
# ═══════════════════════════════════════════════════════════════
#
# Steward directive (2026-07-09): "all existing Nunba + HARTOS functionalities
# should be natively wired to the HART OS" — package the EXACT Nunba desktop
# build (Python AND React) as an in-process OS daemon on a UNIX SOCKET (no host
# TCP port), calling the native HARTOS backend (:6777), with no runtime download.
#
# This derivation REPLACES the earlier React-only `nunba-static` stub.  That stub
# built ONLY `landing-page/build` and served it via LiquidUI's NUNBA_STATIC_DIR —
# which LOST all of Nunba's Python (app.py/main.py/wamp_router.py + the whole
# desktop/* service layer: chat_sync, memory_sync, file_sync, media_classification,
# guest_identity, ai_key_vault, chat_settings, …), for which there is NO HARTOS
# parallel.  Now the FULL Nunba runs as `hart-nunba.service` (hart-nunba.nix)
# binding `unix:/run/hart/nunba.sock`, and LiquidUI reverse-proxies it same-origin.
#
# What is DROPPED and WHY (Gate 4 — one authoritative owner per concern):
#   • GUI:  pywebview, pyautogui, pyperclip, cx_Freeze — main.py IS the headless
#           server; app.py (the pywebview window) is never launched in daemon mode.
#   • ML / TTS / LLM:  torch, torchaudio, transformers, sentence-transformers,
#           tokenizers, safetensors, huggingface_hub, accelerate, sentencepiece,
#           chromadb, faiss-cpu, opencv-python, scikit-learn, tiktoken, onnxruntime,
#           piper-tts, soundfile, langchain*, langgraph, autogen-agentchat — because
#           HART OS owns the UNIFIED, SERVER-MANAGED model+VRAM stack NATIVELY:
#           integrations/service_tools/model_catalog.py (ModelCatalog — single source
#           of truth for ALL model types incl. TTS/STT/VLM, admin-CRUD via
#           POST /api/admin/models), vram_manager.py, model_onboarding.py.  *Which*
#           model/voice runs is orchestrated server-side; the daemon calls HART OS's
#           model/TTS services through the backend, so it must NOT carry a second
#           model stack.  (hartos_speech.py's edge_tts is a BUILD-time onboarding-
#           audio utility, not the runtime TTS.)
#
# HARTOS EXCLUSION (no COPY) is automatic: this fetches ONLY hertz-ai/Nunba, so the
# four cx_Freeze sibling-HARTOS bundling mechanisms (setup_freeze_nunba.py's
# _sibling_editable_deps pip installs, find_hevolve_modules() include_files,
# _hartos_packages + agent_ledger dir copies, python-embed re-install) NEVER run —
# there is no HART tree in the sandbox to copy.  BUT Nunba's own code imports HARTOS
# packages directly (models/catalog.py → `import integrations.service_tools.
# model_catalog`, → core/*), so the DAEMON reaches the ONE NATIVE HARTOS via
# PYTHONPATH=${config.hart.package} (set in hart-nunba.nix) — the same tree the
# backend runs, never a second copy.  Backend calls also go over :6777.  "One HARTOS."

let
  python = pkgs.python310;

  nunbaSrc = pkgs.fetchFromGitHub {
    owner = "hertz-ai";
    repo = "Nunba";
    rev = nunbaRev;
    hash = nunbaHash;
  };

  # ── React dist — the SAME buildNpmPackage as before, kept as a sub-build ──
  # Output is the CRA build/ tree at $out (root), symlinked into the daemon
  # package below at BOTH the path main.py serves it from AND the legacy
  # NUNBA_STATIC_DIR floor path — one /nix/store artifact, served two ways, so
  # the floor CANNOT drift from what the daemon serves (steward: no parallel paths).
  nunbaStatic = pkgs.buildNpmPackage {
    pname = "nunba-static";
    version = "1.0.0";
    src = nunbaSrc;
    sourceRoot = "${nunbaSrc.name}/landing-page";

    inherit npmDepsHash;

    # We must INSTALL optional deps (so autobahn's optional `when` — needed by its
    # build-time `require('when/monitor/console')` — lands in node_modules; R3
    # round-6 proved --omit=optional dropped it), but must NOT run the `canvas`
    # node-gyp C build (no cairo/pango toolchain in the sandbox). `--ignore-scripts`
    # gives both: every dep's FILES are extracted from the prefetched cache (pure-JS
    # `when` included), while NO install/build script runs (canvas's node-gyp is
    # never invoked — canvas is not imported at build; react-pdf renders without it).
    # --legacy-peer-deps: react-konva declares `konva >=2.6` as an UNMET peer (not a
    # resolved lock entry — only a peerDependencies spec), so npm 7+ tries to
    # AUTO-FETCH konva during `npm ci` → offline cache miss `ENOTCACHED` (R3 round-3).
    # Skipping peer auto-install (npm-6 behaviour) is the canonical buildNpmPackage
    # fix; neither flag changes npmDepsHash (prefetched from package-lock.json alone).
    npmFlags = [ "--legacy-peer-deps" ];
    npmInstallFlags = [ "--legacy-peer-deps" "--ignore-scripts" ];
    # buildNpmPackage runs a SEPARATE `npm rebuild` after `npm ci` that re-invokes
    # node-gyp on native modules — so install-time --ignore-scripts is not enough:
    # round-8 failed with `gyp ERR! ... node-gyp rebuild` on `canvas`. Ignore scripts
    # in the rebuild too; canvas ends up installed-but-unbuilt (never imported at
    # build — react-pdf guards it), while pure-JS deps (autobahn's `when`) are intact.
    npmRebuildFlags = [ "--ignore-scripts" ];
    nativeBuildInputs = [ pkgs.python3 ];

    PUBLIC_URL = "/";                     # root-absolute assets (/static/...) — the
                                          # no-basename <BrowserRouter> contract.
    REACT_APP_API_BASE_URL = backendUrl;  # bake the API base onto the HART backend.
    DISABLE_ESLINT_PLUGIN = "true";
    ESLINT_NO_DEV_ERRORS = "true";
    CI = "false";                         # CRA: CI=true makes warnings fatal.
    CYPRESS_INSTALL_BINARY = "0";
    CYPRESS_SKIP_BINARY_INSTALL = "1";
    GENERATE_SOURCEMAP = "false";
    NODE_OPTIONS = "--max-old-space-size=4096";

    # (autobahn's optional `when` now installs via --ignore-scripts above — round-7
    # proved a post-ci `npm install when --offline` fails ENOTCACHED because `npm
    # install` does a REGISTRY METADATA lookup that the npm-ci offline cache lacks;
    # `npm ci` installs straight from the lock, so including optional deps is the fix.)

    dontFixup = true;
    installPhase = ''
      runHook preInstall
      mkdir -p $out
      cp -r build/. $out/
      runHook postInstall
    '';
  };

  # ── Python env — CURATED minimal boot set (NOT requirements.txt) ──
  # Follows the PROVEN hart-app.nix pattern: a hand-picked set of high-confidence
  # nixpkgs packages that lets main.py IMPORT and bind the socket; the heavy/absent
  # deps (torch, transformers, chromadb, langchain, autogen, …) are OMITTED (see the
  # DROP list above) and their imports must be try/except-guarded in Nunba — exactly
  # like HARTOS guards langchain (hart-app.nix's #99).  This is a STARTING set: CI
  # walks the import dominoes on the first real `nix build` (hart-app.nix's own
  # history: "bs4 was the FIRST crash → pytz → redis → python-dotenv …"), adding a
  # nixpkgs pkg or guarding the import until the daemon boots.  Deliberately
  # CONSERVATIVE — a missing pkg is a fast CI domino; a WRONG nixpkgs attr fails eval.
  pythonEnv = python.withPackages (ps: with ps; [
    # ── server + the reverse-proxy contract (main.py:5806+ boot path) ──
    flask flask-cors werkzeug waitress
    hypercorn h11 h2 wsproto      # ASGI primary (main.py:5860); h2/wsproto = HTTP2/WS
    requests httpx certifi urllib3
    # ── boot-critical (module-load / start_background_services) ──
    beautifulsoup4 pytz redis python-dotenv pydantic sqlalchemy alembic greenlet
    cryptography pyjwt cachetools apscheduler
    psutil python-dateutil pyyaml jinja2 aiohttp websockets pillow numpy regex tqdm
    packaging
    # ── systemd Type=notify (if the service ever notifies) + gi (NOT needed headless) ──
    systemd
    # NOTE (CI dominoes to resolve on first build): pydantic in the 24.11 pin is the
    # 1.10 series — Nunba pins pydantic 2.x; if a v2-only import crashes, bump the pin
    # or add python310Packages.pydantic2. autobahn/crossbarhttp3 (WAMP) are LAZY —
    # main.py only imports them inside _wamp_is_needed()/publish helpers, and OS mode
    # is SSE-primary, so they are NOT boot-critical (left out on purpose).
  ]);
in
pkgs.stdenv.mkDerivation {
  pname = "nunba";
  version = "1.0.0";
  src = nunbaSrc;

  # Pure copy — no compile. The React dist is the separate nunbaStatic derivation.
  dontConfigure = true;
  dontBuild = true;
  dontFixup = true;

  installPhase = ''
    runHook preInstall
    mkdir -p $out/lib/nunba

    # Copy ONLY Nunba's own tree (HARTOS is not in the sandbox → auto-excluded).
    cp -r ./. $out/lib/nunba/
    chmod -R u+w $out/lib/nunba
    rm -rf $out/lib/nunba/.git \
           $out/lib/nunba/landing-page/node_modules \
           $out/lib/nunba/landing-page/build

    # ONE React artifact (nunbaStatic), reachable at BOTH:
    #   • where main.py serves the SPA:  APP_DIR/landing-page/build  (main.py:390), and
    #   • the legacy NUNBA_STATIC_DIR floor path: $out/lib/nunba/static.
    # Both are symlinks to the SAME /nix/store path — cannot drift.
    mkdir -p $out/lib/nunba/landing-page
    ln -sfn ${nunbaStatic} $out/lib/nunba/landing-page/build
    ln -sfn ${nunbaStatic} $out/lib/nunba/static

    runHook postInstall
  '';
  # No $out/bin launcher: main.py IS the headless server (app.py's pywebview never
  # runs in daemon mode). hart-nunba.nix's ExecStart calls
  # `${passthru.python}/bin/python ${out}/lib/nunba/main.py` directly — the same
  # pattern hart-backend.nix uses for hart_intelligence_entry (no launcher shim).

  # Expose the Python env for the systemd unit (mirror hart-app.nix passthru.python).
  passthru = {
    python = pythonEnv;
    inherit pythonEnv nunbaStatic;
  };

  meta = with lib; {
    description = "Nunba — full HART OS native daemon (Python + React, HARTOS/GUI/ML-excluded)";
    longDescription = ''
      The complete Nunba desktop companion (app.py's Flask app via main.py's
      headless server + the whole desktop/* Python service layer + the React SPA),
      packaged as a native HART OS daemon that binds a unix socket (no host TCP
      port) and calls the native HARTOS backend on :6777. HARTOS is EXCLUDED (no
      second copy); the GUI and the model/TTS stack are dropped (HART OS owns the
      server-managed model+VRAM stack natively). LiquidUIService reverse-proxies
      the socket same-origin inside the glass shell.
    '';
    homepage = "https://hevolve.ai";
    license = licenses.asl20;
    platforms = platforms.linux;
  };
}
