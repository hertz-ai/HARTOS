# Nunba as a native HART OS daemon (full Python + React, socket-based, HARTOS-excluded)

## Status (2026-07-09)

| Part | What | State |
|------|------|-------|
| **A** | Nunba `main.py` binds `unix:$HART_NUNBA_SOCKET` (native-daemon mode) | ✅ **DONE + committed** — Nunba `cb849ba9`. Reused main.py's existing headless Hypercorn/Waitress block (no `--server-only`, no app.py GUI change). Zero desktop regression (unset → host:port unchanged). |
| **D** | LiquidUI reverse-proxies unclaimed paths → the socket (same-origin, graceful static floor) | ✅ **DONE + committed** — HARTOS `8c1be533`. Traced compatible with the concurrent session's `tests/unit/test_liquid_ui_nunba_serving.py` (socket-unset → `elif nunba_dir` reproduces the old two-route structure; both-unset → 404 floor-lock holds). |
| **B** | `nunba.nix` builds full Nunba (Python + React) minus HARTOS/GUI/ML | ✅ **WRITTEN** — HARTOS `2b3fc629`. Extended in place: React sub-build + curated python310 env (hart-app.nix pattern) + Nunba Python copy + one-artifact symlinks + `passthru.python`; `nunbaRev` = Nunba HEAD `cb849ba9`; FODs seeded `fakeHash`. **CI** still: pin FODs + walk the import-domino boot loop. |
| **C** | `hart-nunba.nix` hardened socket daemon | ✅ **WRITTEN** — HARTOS `2b3fc629`. Options-only → hardened daemon (mirrors hart-backend.nix): `main.py` direct, `HART_NUNBA_SOCKET`+`HARTOS_BACKEND_URL`, `/run/hart` RW, no `PrivateNetwork`. |
| **E** | Retire `hartOnboarding.js` + `native_onboarding.py` | ⏸ **DEFERRED** — only after the daemon is proven serving Nunba's `LightYourHART` on real HW. Retiring the fallback before the replacement is verified = onboarding regression. |
| **F** | CI: pin `nunbaHash`/`npmDepsHash`, build `.#packages.nunba`, flip `hart.nunba.enable` | 🟡 **WIRED** — HARTOS `2b3fc629` exposes `packages.nunba` + the LiquidUI `HART_NUNBA_SOCKET` wiring + the desktop single-flip marker. **CI** still runs the build + hash-pin + flip (no Nix on the authoring box). |

**What the authoring box cannot do.** This box has **no Nix, no Node, no Docker, no WSL distro** — so it cannot *realise* the Nix build, compute the FOD hashes, or run the webpack/pytest. Everything else is done here: all edits (A–D + wiring + F) are written into the real files, Nunba's import surface was static-analyzed to seed the curated python env, and the eval-level zero-regression is verified by inspection (the `mkIf` gates keep the fakeHash build unforced while `hart.nunba.enable=false`). The Python env is still a **starting** curated set (hart-app.nix's proven pattern, NOT `requirements.txt`; langchain/autogen/torch/chromadb omitted, guarded by try/except); the remaining **import-domino discovery** ("bs4 → pytz → redis …") needs a real `nix build`+boot to finalize — that is the CI step, with pinning the two FOD hashes and flipping `hart.nunba.enable`. B/C were edited **in place** (not a parallel file) under the steward's explicit "finish end to end" direction, building on the concurrent session's landed `0a338951` (its React-static build + serving test both preserved).

## Nix-free import-domino walk (2026-07-10, de-risking the CI build)

A static transitive module-load import walk of Nunba's `main.py` (following its
first-party packages; `scratchpad/import_domino_walk.py`) — the nix-free way to advance
the same work with no build:
- ✅ **The drop-ML decision HOLDS.** ZERO ML/TTS/LLM packages (torch, transformers,
  langchain, autogen, chromadb, faiss, onnxruntime, piper…) are imported *unguarded* at
  module load — all lazy. Only `pyautogui` (GUI) appears and it is *guarded*. The daemon
  therefore boots without the dropped stack; those imports only fire on code paths HART OS
  serves server-side.
- ⚠️ **Fixed a real boot blocker (`3635ef77`):** Nunba's `models/catalog.py` +
  `models/orchestrator.py` do an unguarded `import integrations.service_tools.model_catalog`
  (→ `integrations/__init__` → `core/__init__`). Nunba has **no** `core/`/`integrations/` of
  its own — those are **HARTOS** packages (why desktop cx_Freeze bundles HARTOS). The
  HARTOS-excluded daemon would crash on import. Fix = `PYTHONPATH=${config.hart.package}` on
  the service → resolves to the **native** HARTOS tree (the same one the backend runs), no
  copy. Delegated path is dependency-light (`model_catalog` is stdlib-only; `core/__init__`
  pulls requests/httpx, already in the env). One name overlap (`desktop/` exists in both
  repos) — Nunba's wins via `sys.path[0]` (WorkingDirectory).
- **Still a CI domino walk:** the walk is static + Windows-side, so it proves the *big*
  dominoes but not every transitive dep of `core/__init__`'s util chain. The final pythonEnv
  completeness is confirmed by the first real `nix build`+boot (CI).

## Context

The steward's directive (2026-07-09): **"all existing Nunba + HARTOS functionalities should
be natively wired to the HART OS,"** and specifically — package the **exact Nunba desktop
build** (Python *and* React) as a **native daemon inside HART OS**, using HART OS's **native
HARTOS backend** (no re-bundled second copy), with **no runtime download** and **without
occupying a host TCP port**.

Today HART OS bundles **only** the Nunba React dist (`nixos/packages/nunba.nix` →
`$out/lib/nunba/static`, served by LiquidUI when `NUNBA_STATIC_DIR` is set). That path:
- **Loses all of Nunba's Python** — `app.py`, `main.py`, `wamp_router.py`, and the whole
  `desktop/*.py` service layer (`chat_sync`, `memory_sync`, `file_sync`,
  `media_classification`, `guest_identity`, `ai_key_vault`, `chat_settings`, …). There is **no
  HARTOS parallel** for these — they simply vanish.
- Leaves the OS **reimplementing** Nunba UI it can't serve (the two onboarding frontends), and
  every `route` panel shows "url not working" when `NUNBA_STATIC_DIR` is unset
  (`nunba.enable=false` because `nunba.nix` pins `lib.fakeHash`).

Goal: retire the reimplementations by **running the full Nunba as a native, socket-based OS
daemon** wired to the native backend — one HARTOS, all of Nunba's Python, no host port, no
CORS, no download.

## Architecture

```
glass shell (WebView, same-origin :6800) ──> LiquidUIService (:6800)
    ├─ /  ,  /shell/static/*                         OS shell (render_desktop_shell) — UNCHANGED
    └─ Nunba routes (/social, /agents, /api/nunba/*) ──reverse-proxy──> unix:/run/hart/nunba.sock
                                                                          Nunba daemon (full Python + React)
                                                                            └─ HARTOS_BACKEND_URL → native :6777
```

- **No host TCP port**: the Nunba daemon binds `unix:/run/hart/nunba.sock` (Hypercorn/Waitress
  bind unix sockets natively). WAMP realtime stays SSE-primary via LiquidUI; Nunba's `:8088`
  WAMP router is proxied over the socket or left off (SSE is the OS-mode path).
- **Same-origin**: LiquidUI reverse-proxies the Nunba sub-paths to the socket, so `NUNBA_BASE`
  stays `''` and the existing panel-iframe seam (`liquid_ui_service.py:3435`, the 2xx-verify at
  `:3469`) works unchanged — no CORS regression.
- **One HARTOS**: the daemon runs Nunba-**minus**-HARTOS; all backend calls proxy to the native
  backend via `HARTOS_BACKEND_URL` (`hartos_backend_adapter.py:53`).

## Changes

### A. `Nunba` repo — a headless, socket-bound entrypoint (small)
`app.py`'s `main()` starts a Flask server thread (`start_flask`, :5000) then **blocks on
`webview.start()`** (`app.py:8262-8285`). Add a **headless server-only path** (~15 lines) that:
- binds the server to `unix:/run/hart/nunba.sock` instead of `0.0.0.0:5000` (Hypercorn
  `bind = ["unix:..."]`), starts `wamp_router.start_wamp_router(...)`, and **blocks by joining
  the server thread** — never creates a pywebview window.
- Gate it on a `--server-only`/`HART_NUNBA_HEADLESS=1` flag (the old removed AppImage used
  `--server-only`; restore the flag name).

### B. `nixos/packages/nunba.nix` — build the FULL Nunba minus HARTOS ⛔ CI/coordination
The current file is a **React-only** `buildNpmPackage` (`nunba-static`, `$out/lib/nunba/static`).
Extend it to ALSO carry Nunba's Python + a headless launcher, keeping the existing React
derivation as a sub-build. **`pythonEnv` follows hart-app.nix's CURATED-minimal pattern (NOT
`requirements.txt`)** — the heavy/absent deps (langchain, autogen, torch, transformers, chromadb,
piper/vibevoice, onnxruntime, faiss) are OMITTED and Nunba's imports of them must be
try/except-guarded, discovered by the **import-domino build loop** (mirror hart-app.nix's own
"bs4 → pytz → redis" history). GUI deps (`pywebview`, `pystray`, `pyautogui`, `win10toast`,
`pywin32`, `rumps`, `pyobjc*`) are dropped (main.py is the headless path). **Why no ML/TTS:**
HART OS owns the unified server-managed model+VRAM stack natively — `model_catalog.py`
(`ModelCatalog`, single source of truth for ALL model types incl. TTS/STT/VLM), `vram_manager.py`,
`model_onboarding.py`; *which* model/voice runs is orchestrated server-side, so the daemon calls
HART OS's model/TTS through the backend (`hartos_speech.py`'s `edge_tts` is a build-time
onboarding-audio utility, not runtime TTS).

```nix
# nixos/packages/nunba.nix — full daemon package (skeleton; pin FODs + walk dominoes in CI)
{ lib, pkgs, hartSrc ? null
, nunbaRev ? "72780cd43fd274057251e5e594f7d949a29e2237"   # == Nunba HEAD (cb849ba9's parent line)
, nunbaHash ? lib.fakeHash          # nix-prefetch-github hertz-ai Nunba --rev ${nunbaRev}
, npmDepsHash ? lib.fakeHash        # prefetch-npm-deps landing-page/package-lock.json
, backendUrl ? "http://127.0.0.1:6777"
}:
let
  python = pkgs.python310;
  nunbaSrc = pkgs.fetchFromGitHub { owner = "hertz-ai"; repo = "Nunba"; rev = nunbaRev; hash = nunbaHash; };

  # React dist — the EXISTING buildNpmPackage, unchanged (kept as a sub-build).
  nunbaStatic = pkgs.buildNpmPackage {
    pname = "nunba-static"; version = "1.0.0"; src = nunbaSrc;
    sourceRoot = "${nunbaSrc.name}/landing-page";
    inherit npmDepsHash;
    npm_config_omit = "optional"; npmInstallFlags = [ "--omit=optional" ];
    nativeBuildInputs = [ pkgs.python3 ];
    PUBLIC_URL = "/"; REACT_APP_API_BASE_URL = backendUrl;
    DISABLE_ESLINT_PLUGIN = "true"; ESLINT_NO_DEV_ERRORS = "true"; CI = "false";
    CYPRESS_INSTALL_BINARY = "0"; CYPRESS_SKIP_BINARY_INSTALL = "1";
    GENERATE_SOURCEMAP = "false"; NODE_OPTIONS = "--max-old-space-size=4096";
    dontFixup = true;
    installPhase = "runHook preInstall; mkdir -p $out; cp -r build/. $out/; runHook postInstall";
  };

  # CURATED minimal boot set (extend via the domino loop — do NOT paste requirements.txt).
  # Start from hart-app.nix's proven set; add Nunba-boot deps as `nix build` surfaces them.
  pythonEnv = python.withPackages (ps: with ps; [
    flask waitress requests httpx            # server + the reverse-proxy client contract
    hypercorn h11 h2 wsproto                 # ASGI primary (main.py:5860)
    beautifulsoup4 pytz redis python-dotenv pydantic sqlalchemy cryptography
    psutil python-dateutil pyyaml jinja2 aiohttp websockets pillow numpy
    # + Nunba-specific boot imports discovered by the build loop (autobahn?, txaio?, …)
  ]);
in
pkgs.stdenv.mkDerivation {
  pname = "nunba"; version = "1.0.0"; src = nunbaSrc;
  # EXCLUDE HARTOS: copy ONLY Nunba's own tree — never run setup_freeze_nunba.py's four
  # sibling-HARTOS mechanisms (_sibling_editable_deps pip installs, find_hevolve_modules()
  # include_files, _hartos_packages + agent_ledger dir copies, python-embed re-install).
  installPhase = ''
    runHook preInstall
    mkdir -p $out/lib/nunba $out/bin
    cp -r landing-page/../. $out/lib/nunba/   # Nunba repo root (app.py/main.py/wamp_router.py/routes/desktop/api/…)
    rm -rf $out/lib/nunba/.git $out/lib/nunba/landing-page/node_modules
    ln -sfn ${nunbaStatic} $out/lib/nunba/static      # React dist at the byte-for-byte contract path
    cat > $out/bin/nunba <<EOF
    #!${pkgs.runtimeShell}
    exec ${pythonEnv}/bin/python $out/lib/nunba/main.py "\$@"
    EOF
    chmod +x $out/bin/nunba
    runHook postInstall
  '';
  dontFixup = true;
  passthru = { python = pythonEnv; inherit pythonEnv nunbaStatic; };
  meta = with lib; { description = "Nunba — full HART OS native daemon (Python + React, HARTOS-excluded)";
                     license = licenses.asl20; platforms = platforms.linux; };
}
```
**Verify (CI):** `nix build .#packages.<sys>.nunba`; then `ls $out/lib/nunba` shows Nunba's Python
+ `static` symlink + `$out/bin/nunba`; `grep -rL` confirms **no** `core/ integrations/ security/
hart_intelligence*` in the closure (HARTOS excluded). Walk the import dominoes: run
`$out/bin/nunba` with `HART_NUNBA_SOCKET=/tmp/n.sock`, read the first `ModuleNotFoundError`, add
that nixpkgs pkg (or guard the import in Nunba), repeat until it binds the socket.

### C. `nixos/modules/hart-nunba.nix` — a hardened system daemon ⛔ CI/coordination
The current file is **options-only** (documents the removed AppImage). Replace its `config` block
(keep the `options.hart.nunba.*`) with a real socket daemon mirroring hart-backend.nix. **Runs
`main.py` directly** (it IS the headless server; no `--server-only`, no app.py).

```nix
# hart-nunba.nix config block (mirror hart-backend.nix hardening; add to the existing options)
config = lib.mkIf (config.hart.enable && config.hart.nunba.enable) {
  systemd.services.hart-nunba = {
    description = "Nunba native daemon (full Python + React, unix socket)";
    after = [ "hart-backend.service" ];
    partOf = [ "hart.target" ]; wantedBy = [ "hart.target" ];
    environment = {
      HART_NUNBA_SOCKET = "/run/hart/nunba.sock";                 # inbound: unix socket, NO host port
      HARTOS_BACKEND_URL = "http://127.0.0.1:${toString config.hart.ports.backend}";  # outbound → native 6777
      # NUNBA_BUNDLED intentionally UNSET → hartos_backend_adapter takes the explicit-URL HTTP path
      PYTHONDONTWRITEBYTECODE = "1"; PYTHONUNBUFFERED = "1";
    };
    serviceConfig = {
      Type = "simple"; User = "hart"; Group = "hart";
      WorkingDirectory = "${nunbaPkg}/lib/nunba";
      ExecStart = "${nunbaPkg.python}/bin/python ${nunbaPkg}/lib/nunba/main.py";
      RuntimeDirectory = "hart"; RuntimeDirectoryMode = "0750";   # creates+owns /run/hart for the socket
      Restart = "on-failure"; RestartSec = 5; TimeoutStartSec = 45;
      # ── hardening (copy hart-backend.nix) ──
      NoNewPrivileges = true; ProtectSystem = "strict"; ProtectHome = true;
      ReadWritePaths = [ config.hart.dataDir config.hart.logDir ];
      PrivateTmp = true; ProtectKernelTunables = true; ProtectKernelModules = true;
      RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];   # AF_UNIX for the socket; AF_INET for → 6777
      SystemCallFilter = [ "@system-service" ]; LockPersonality = true;
      RestrictRealtime = true; RestrictSUIDSGID = true;
      MemoryMax = if config.hart.variant == "edge" then "512M" else "2G";
      StandardOutput = "journal"; StandardError = "journal"; SyslogIdentifier = "hart-nunba";
    };
  };
};
# NOTE: `nunbaPkg = pkgs.callPackage ../packages/nunba.nix { inherit hartSrc; };` at the module let-binding.
# DO NOT set PrivateNetwork=true — the daemon must reach 127.0.0.1:6777 (native HARTOS) on the host loopback.
```
Then wire LiquidUI to set the socket env (`hart-liquid-ui.nix`: `HART_NUNBA_SOCKET =
"/run/hart/nunba.sock"` alongside the existing `NUNBA_STATIC_DIR`, so the proxy is preferred and
the dist stays the floor), and keep `hart.nunba.enable = false` until `nix build .#packages.nunba`
is green — flipping it on before the closure builds would fail the ISO.

**Two binding gates (steward: "no parallel paths and zero regression"):**
1. **No parallel path.** EXTEND the existing `nunba.nix`/`hart-nunba.nix` in place — never a second
   `nunba-daemon.nix` or a forked module. And `NUNBA_STATIC_DIR` MUST point at the daemon package's
   OWN `${nunbaPkg}/lib/nunba/static` (i.e. the same `nunbaStatic` store path the daemon serves), so
   there is exactly ONE React artifact served two ways (socket primary, static floor) — it cannot
   drift, because both are the same `/nix/store` path. The floor is graceful degradation of the same
   build, not a second UI source.
2. **Zero regression.** `hart.nunba.enable` stays `false` until the closure builds green; A+D are
   additive (activate only when `HART_NUNBA_SOCKET` is set), so the current React-static nightly is
   byte-for-byte unchanged until the daemon is proven. `main.py`'s host:port path and LiquidUI's
   `elif nunba_dir` static path are untouched when the socket is unset.

### D. `integrations/agent_engine/liquid_ui_service.py` — reverse-proxy ✅ DONE (`8c1be533`)
Replaced the `NUNBA_STATIC_DIR`-gated static block with a socket-first reverse-proxy: when
`HART_NUNBA_SOCKET` is set, a last-place `@app.route('/<path:path>')` streams (httpx UDS +
`iter_raw`, `read=None` for SSE) from `unix:/run/hart/nunba.sock`; on socket failure it falls back
to `_serve_nunba_static` (the old `NUNBA_STATIC_DIR` dist) — never a 404. Same origin, `NUNBA_BASE`
stays `''`. Explicit routes (`/`, `/shell/static/*`, `/api/*`, `/cors/test`, `/health`,
`/favicon.ico`) still win by rule specificity. **Verified compatible** with the concurrent
session's `tests/unit/test_liquid_ui_nunba_serving.py` (socket unset → the `elif nunba_dir` branch
reproduces the old two-route structure; both env unset → the 404 floor-lock holds).

### E. Retire the frontend reimplementations (backend stays) ⏸ DEFERRED until real-HW proof
- **Retire** `integrations/agent_engine/static/hartOnboarding.js` (web overlay) and
  `integrations/agent_engine/native_onboarding.py` (GTK4) — both are thin re-draws of the same
  FSM that Nunba's `LightYourHART.js` renders richly. Remove the overlay `<script>`/DOM
  (`liquid_ui_service.py:2601,2701`) and drop `native_onboarding.py` from `hart-onboarding.nix`.
- **KEEP** `hart_onboarding.py` (identity seal engine) + `onboarding_routes.py` (the 4
  `/api/onboarding/*` endpoints, mounted at `liquid_ui_service.py:7107-7111`) + the "My HART
  Setup" panel — Nunba's UI drives these unchanged.

### F. CI — pin the hashes, build the daemon package
- Pin `nunbaHash` (`nix-prefetch-github hertz-ai Nunba --rev <rev>`) + `npmDepsHash`
  (`prefetch-npm-deps landing-page/package-lock.json`) in the same commit that bumps `nunbaRev`.
- Expose `packages.nunba` in `nixos/flake.nix` so a dedicated CI job builds + caches the heavy
  Python+webpack closure once (as `nunba.nix:51-54` already recommends for `nunba-static`).
- Set `hart.nunba.enable = true` on the desktop variant.

## Reuse (do NOT reinvent)
- Service template: `nixos/modules/hart-backend.nix`. Daemon-ExecStart shape: `hart-agent.nix`.
- Python packaging: `nixos/packages/hart-app.nix` (`withPackages` + `passthru.python`) and the
  removed `6b46061e:nixos/packages/nunba.nix` (Nunba's own precedent — recover from git).
- Backend seam: `routes/hartos_backend_adapter.py:53` (`HARTOS_BACKEND_URL`) + `core.port_registry
  .get_local_backend_url()`.
- Sibling fetch: existing `fetchFromGitHub` in `nunba.nix`/`hart-openclaw.nix`.

## Verification
1. `nix build .#packages.<sys>.nunba` succeeds (hashes pinned) → `$out/bin/nunba` +
   `$out/lib/nunba` (Python) + `.../static` (React), and `nm`/`grep` confirms **no** `core/`,
   `integrations/`, `security/`, `hart_intelligence*` in the closure (HARTOS excluded).
2. nixosTest (mirror `hart-backend`'s): boot desktop VM → `hart-nunba.service` active →
   `curl --unix-socket /run/hart/nunba.sock http://x/cors/test` → 200; the daemon reaches native
   HARTOS (`HARTOS_BACKEND_URL`), not a bundled copy.
3. Real-HW: flash a nightly → a route panel (e.g. `/social`) loads via the reverse-proxy (no
   "url not working"), onboarding renders Nunba's `LightYourHART` (not `hartOnboarding.js`), and
   `pgrep` shows exactly ONE HARTOS backend process.

## Risks / coordination
- **CONFIRMED: `nunba.nix` + `hart-nunba.nix` are the concurrent sharded-inference session's
  files** — `0a338951` ("bulk landing of the sharded-model inference session's in-flight tree")
  rewrote `nunba.nix` (±199 lines), `hart-nunba.nix` (±77), and added
  `tests/unit/test_liquid_ui_nunba_serving.py`. B/C MUST therefore be applied in coordination
  with that session (not blind-overwritten). The A+D seams touch only MY files (Nunba `main.py`, HARTOS
  `liquid_ui_service.py`) and are verified compatible with their serving test — no collision.
- **Import-domino discovery needs a real Nix build+boot** (hart-app.nix's curated-set history
  proves it) → B's `pythonEnv` cannot be finalized on a box without Nix. The skeleton above is the
  starting point; CI walks the dominoes.
- No Node/Nix on the current box → the build + hash pinning must run in **CI** (or a machine with
  the toolchain + private-repo access). This plan is authored to be executed there.
- **Safe-by-default staging:** A+D are additive (activate only when `HART_NUNBA_SOCKET` is set) and
  are committed now. `hart.nunba.enable` STAYS `false` until `nix build .#packages.nunba` is green,
  so the current working nightly (React-static floor) is never broken by an in-progress daemon
  closure.
- The Nunba socket-bind (A) is a change in the **Nunba repo** — cross-repo commit `cb849ba9`.
