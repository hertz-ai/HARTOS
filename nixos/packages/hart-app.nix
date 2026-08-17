{ lib, pkgs, hartSrc }:

# Nix derivation for the HART application
# Builds Python 3.10 environment with all dependencies
# Output: /nix/store/<hash>-hart-app/ with all source + venv

let
  python = pkgs.python310;

  # Python environment with all dependencies
  pythonEnv = python.withPackages (ps: with ps; [
    # Core framework
    flask
    waitress
    requests
    # ── hart_intelligence_entry module-load (column-0, unguarded) imports ──
    # `waitress ... hart_intelligence_entry:app` imports the module, so every
    # top-level import must resolve. bs4 was the FIRST crash on the ISO ("No
    # module named 'bs4'" → waitress exits 0 → Restart never fires → backend
    # dead); pytz / redis / python-dotenv are the next dominoes (all present in
    # this nixpkgs pin).
    #
    # ⚠ CHAT DEGRADED (no longer a boot blocker): the module also imports
    # `langchain_classic` 13x, and langchain_classic 1.x is NOT in this June-2025
    # nixpkgs pin (the split package post-dates it). That import block is now
    # wrapped in try/except in hart_intelligence_entry.py (#99): if it's absent
    # the backend STILL BOOTS and serves social/sync/status/daemon — only the
    # langchain chat path errors at call-time (gated by _LANGCHAIN_OK). So the
    # backend imports cleanly here without langchain. To make CHAT itself work on
    # the ISO, langchain_classic must still be packaged (pip2nix/poetry2nix) or
    # the pin bumped — but that is no longer what gates a successful boot.
    beautifulsoup4
    pytz
    redis
    python-dotenv
    pydantic  # 1.10.x series from nixpkgs 24.11

    # Database
    sqlalchemy

    # Cryptography (Ed25519 identity, signing)
    cryptography

    # systemd integration — REQUIRED by every Type="notify" service
    # (hart-liquid-ui, hart-app-bridge, hart-model-bus). Their ExecStart
    # Python does `import systemd.daemon; systemd.daemon.notify('READY=1')`.
    # Without this the import raises ModuleNotFoundError, the process dies,
    # and systemd kills the unit at TimeoutStartSec — i.e. the LiquidUI server
    # and the app bridge never come up on a fresh ISO boot.
    systemd

    # GObject introspection (`import gi`) — REQUIRED by the LiquidUI glass
    # shell (cage kiosk client). It does `import gi; gi.require_version('Gtk',
    # '3.0'); gi.require_version('WebKit2','4.1')`. Missing pygobject3 crashes
    # the shell window → cage exits → the greeter falls back to GDM (which then
    # leaks the installer 'nixos' user). The GI *typelibs* (Gtk/WebKit2) are
    # put on GI_TYPELIB_PATH by hart-liquid-ui.nix's glassShell wrapper.
    pygobject3

    # LangChain ecosystem
    # langchain  # Pinned version; available in nixpkgs or via pip2nix

    # ML / AI
    numpy
    pillow

    # System metrics — LiquidUI's /api/shell/system/metrics (the CPU/RAM/disk
    # context that IS the adaptive shell's value: "explains WHY the GPU is
    # busy"). Imported lazily + guarded, so its absence only silently degrades
    # the dashboard rather than crashing the service — exactly the kind of
    # quietly-broken feature that hides without this.
    psutil

    # Spoken-form number expansion for TTS (task #33). NOT optional polish:
    # tts_text_normalizer's whole reason for existing is that diffusion-token
    # engines (OmniVoice, F5, CosyVoice, Indic-Parler) cannot pronounce
    # "Rs.200" or "12.5%" — they skip them or emit garbage. Without num2words
    # `_num_to_words()` returns None, so the normalizer expands the SYMBOL but
    # leaves the DIGITS, and the shipped OS says "200 rupees" with the number
    # handed to the engine raw.
    #
    # It was pinned in requirements.txt (`num2words>=0.5.13`) and simply never
    # mirrored here, so the dev box and the OS disagreed silently — the two
    # canonical RulePassTest cases in tests/unit/test_tts_text_normalizer.py
    # have been red for exactly this reason.
    #
    # Second cost, less obvious: with digits left in, `_has_residual_tokens()`
    # is true on essentially every numeric utterance, so the LLM fallback fires
    # and pays a ~2s local-model round trip per reply to do work this package
    # does offline in under a millisecond.
    num2words

    # Caption fetch for the agent's YouTube transcript tool
    # (integrations/browser_research/scripts/youtube.py). Same mirror lesson as
    # num2words directly above, one step worse: this one was in neither list.
    # The import is wrapped in try/except ImportError, so its absence is silent
    # -- every transcript request falls through to the yt-dlp + local-Whisper
    # path, downloading the media and running STT even for videos that publish
    # captions. Nothing is broken, it is just slow and wrong in a way no error
    # ever reports.
    youtube-transcript-api

    # httpx — the LiquidUI shell's Nunba proxy talks to the Nunba daemon over a
    # unix socket, and httpx is the only client here that does UDS transport
    # (requests cannot). liquid_ui_service._create_flask_app() imports it as
    # soon as hart_nunba_socket is set, so preinstalling Nunba made this a hard
    # dependency of the SHELL SERVER: without it the import raised, the unit
    # exited 1 and restart-looped, and the desktop never got a shell. It was in
    # nunba.nix (that package's own python env) but never here.
    httpx

    # Stripe SDK for the commercial API's /upgrade card charges
    # (integrations/ap2/ap2_protocol.py:StripePaymentGateway). requirements.txt
    # says in so many words that it is "baked into the image so production
    # rebuilds don't need a manual pip install" -- it was not, and connect()
    # swallows the ImportError and sets connected = False, so the shipped OS
    # silently served every upgrade through MockPaymentGateway. Revenue path,
    # no error anywhere.
    stripe

    # Utilities
    python-dateutil
    pyyaml
    jinja2
    aiohttp
    websockets

    # LLM-output JSON repair -- helper.py / create_recipe.py / core/agent_tools.py
    # import json_repair to salvage malformed model JSON. Real-HW 2026-07-19:
    # POST /chat 500'd on the node with ModuleNotFoundError 'json_repair' -- and
    # /chat is the ONE agentic pipeline (CREATE/REUSE), so the hive bootstrap was
    # dead on the node. Pure-python, tiny; present in this nixpkgs pin.
    json-repair

    # AutoGen (multi-agent framework)
    # autogen  # May need overlay or fetchPypi
  ]);
in
pkgs.stdenv.mkDerivation {
  pname = "hart-app";
  version = "1.0.0";

  src = lib.cleanSourceWith {
    src = hartSrc;
    filter = path: type:
      let
        baseName = baseNameOf path;
        relPath = lib.removePrefix (toString hartSrc + "/") (toString path);
        # The latency-budget DATA lives under docs/architecture (a reviewed design
        # artifact, versioned beside LATENCY_HARNESS.md) but it is ALSO an input to
        # the checkPhase build gate below. Excluding the whole docs/ tree would
        # leave that gate unable to read its budgets and would fail EVERY ISO
        # build, so keep the directory chain plus the file itself. Everything else
        # under docs/ stays excluded.
        keepForBuildGate =
          relPath == "docs"
          || relPath == "docs/architecture"
          || relPath == "docs/architecture/latency_budgets.json";
      in
      # Exclude dev artifacts, tests, build outputs
      if keepForBuildGate then true else
      !(
        baseName == ".git" ||
        baseName == "__pycache__" ||
        baseName == ".idea" ||
        baseName == ".pycharm_plugin" ||
        baseName == "venv310" ||
        baseName == "autogen-0.2.37" ||
        baseName == "docs" ||
        baseName == "tests" ||
        baseName == "nixos" ||
        baseName == ".env" ||
        lib.hasSuffix ".pyc" baseName ||
        lib.hasSuffix ".egg-info" baseName ||
        lib.hasSuffix ".dist-info" baseName ||
        (lib.hasPrefix "agent_data/" relPath && lib.hasSuffix ".db" baseName)
      );
  };

  buildInputs = [ pythonEnv ];

  # ── BUILD GATE: latency-budget coverage ────────────────────────────────────
  # Steward asked the decisive question (2026-07-20): "fails in tests or at
  # compile or build time?" A pytest-only guard was too weak HERE, because
  # `publish-nightly` needs only [build-iso, build-installers] and `build-iso`
  # does NOT need `gate-checks` -- proven on run 29725400559, where gate-checks
  # was cancelled while iso-desktop shipped. So a failing coverage test could not
  # stop a nightly; only `tag-and-sign` is gated on the full suite.
  # Running the SAME checker (scripts/check_latency_budgets.py -- one
  # implementation, also called by tests/unit/test_latency_budget_coverage.py)
  # in checkPhase makes the ISO itself unbuildable when a component ships with no
  # input-to-photon budget, or when a continuous-interaction budget is raised
  # above one frame to hide an easing layer. Pure-python, no network, no extra
  # closure input: it only reads the repo + renders the served shell.
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    echo "=== latency-budget coverage (build gate) ==="
    ${pythonEnv}/bin/python scripts/check_latency_budgets.py
    runHook postCheck
  '';

  installPhase = ''
    mkdir -p $out
    cp -r . $out/

    # Theme presets MUST ship (real-HW 2026-07-20 'fully bluish, no aura'): the
    # source filter above drops the whole nixos/ tree (build infra), but the
    # RUNTIME theme JSONs (ThemeService._THEME_DIR) live under
    # nixos/assets/conky-themes -- so on the node get_preset('aura') found
    # nothing, get_active_theme fell to the inline hart-default (no wallpaper
    # key), and the shell painted the legacy BLUISH navy gradient instead of the
    # aura cosmic field. Re-install the theme dir at the exact path the code
    # resolves ($out/nixos/assets/conky-themes); hartSrc is the unfiltered repo.
    mkdir -p $out/nixos/assets
    cp -r ${hartSrc}/nixos/assets/conky-themes $out/nixos/assets/conky-themes

    # Make the Python environment accessible
    mkdir -p $out/bin
    ln -s ${pythonEnv}/bin/python $out/bin/python
    ln -s ${pythonEnv}/bin/python3 $out/bin/python3

    # Create agent_data directory structure
    mkdir -p $out/agent_data
  '';

  # Expose the Python env for service modules to reference
  passthru = {
    python = pythonEnv;
    inherit pythonEnv;
  };

  meta = {
    description = "HART OS — Crowdsourced Agentic Intelligence Platform";
    homepage = "https://hevolve.ai";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
  };
}
