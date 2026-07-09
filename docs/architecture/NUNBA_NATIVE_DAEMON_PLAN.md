# Nunba as a native HART OS daemon (full Python + React, socket-based, HARTOS-excluded)

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

### B. `nixos/packages/nunba.nix` — build the FULL Nunba minus HARTOS
Mirror `nixos/packages/hart-app.nix` (`pythonEnv = python310.withPackages(...)` + copied source
+ `passthru.python`) and the removed `6b46061e:nixos/packages/nunba.nix` (its direct precedent):
- `pythonEnv` from Nunba's `requirements.txt` **minus** the GUI drop-list (`pywebview`, `pystray`,
  `pyautogui`, `win10toast`, `pywin32`, `rumps`, `pyobjc*`) **and the entire ML/TTS/LLM stack**
  (`torch`, `transformers`, `sentence_transformers`, `chromadb`, `faiss`, `piper`/`vibevoice`,
  `onnxruntime`, `llama`).
- **Why drop TTS/models too (corrected 2026-07-09, steward):** HART OS owns a **unified,
  server-managed model + VRAM stack natively** — `integrations/service_tools/model_catalog.py`
  (`ModelCatalog`, the single source of truth for **all** model types incl. **TTS**/STT/VLM,
  JSON-backed, admin-CRUD via `POST /api/admin/models`), `vram_manager.py` (GPU tracking +
  gpu/cpu_offload/cpu_only strategy), and `model_onboarding.py` (name → VRAM-pick quant →
  download → start server → register). *Which* model/voice runs is orchestrated **server-side**;
  the device runs it locally. So the Nunba daemon must NOT carry its own torch/piper/transformers
  — it calls HART OS's native model/TTS services through the backend. (`hartos_speech.py`'s
  `edge_tts` is a pre-synth build utility for onboarding audio, not the runtime TTS.) This keeps
  the daemon lean and one authoritative model manager.
- Keep building the React dist (existing `buildNpmPackage` → `landing-page/build`).
- **Exclude HARTOS**: do NOT run the four sibling-HARTOS bundling mechanisms
  (`setup_freeze_nunba.py`: `_sibling_editable_deps` pip installs; `find_hevolve_modules()`
  include_files; `_hartos_packages` + `agent_ledger` dir copies; the python-embed re-install).
  The Nix build copies only Nunba's own `app.py/main.py/wamp_router.py/routes/desktop/api/…`.
- `passthru.python = pythonEnv`; output `$out/bin/nunba` (headless launcher) + `$out/lib/nunba`
  (Python) + `$out/lib/nunba/static` (React), keeping the static path contract for fallback.

### C. `nixos/modules/hart-nunba.nix` — a hardened system daemon
Mirror `nixos/modules/hart-backend.nix` (system service, `@hart.target`, full hardening block):
- `ExecStart = "${nunbaPkg.python}/bin/python ${nunbaPkg}/lib/nunba/app.py --server-only"`,
  `WorkingDirectory = "${nunbaPkg}/lib/nunba"`.
- Environment: `HARTOS_BACKEND_URL = "http://127.0.0.1:${toString cfg.ports.backend}"` (native
  6777), `HART_NUNBA_SOCKET = /run/hart/nunba.sock`, `NUNBA_BUNDLED` **unset** (so the adapter
  takes the explicit-URL Tier-2 HTTP path to native HARTOS), `PYTHONDONTWRITEBYTECODE=1`.
- `RuntimeDirectory=hart` (for the socket), `ReadWritePaths`, `User/Group=hart`, `after =
  [ "hart-backend.service" ]`, `wantedBy = [ "hart.target" ]`.

### D. `integrations/agent_engine/liquid_ui_service.py` — reverse-proxy, retire static-serve
- Replace the `NUNBA_STATIC_DIR`-gated static block (`:5987-5999`) with a **reverse-proxy** of
  the Nunba SPA + API sub-paths to `unix:/run/hart/nunba.sock` (via `httpx`/`requests-unixsocket`
  — a small `@app.route('/<path:path>')` last-place handler streaming from the socket). Same
  origin, so `NUNBA_BASE` stays `''` and route panels are unchanged.
- Keep `/`, `/shell/static/*`, `/api/*`, `/cors/test` exactly as they are (LiquidUI wins by rule
  specificity).

### E. Retire the frontend reimplementations (backend stays)
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
- `nunba.nix` + `hart-nunba.nix` are the **concurrent inference session's files** — coordinate
  before editing; this change is additive to their sharded-inference work but shares the files.
- No Node/Nix on the current box → the build + hash pinning must run in **CI** (or a machine with
  the toolchain + private-repo access). This plan is authored to be executed there.
- The Nunba headless entrypoint (A) is a change in the **Nunba repo** — a cross-repo commit.
