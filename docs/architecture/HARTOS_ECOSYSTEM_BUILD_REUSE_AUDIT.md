# HART OS — Ecosystem Build + Reuse Audit (cross-repo, cycle-safety)

Deep audit (2026-07-01, 8-agent workflow) of HARTOS + Nunba + siblings to (a) map what
already EXISTS so pending work is reuse-first (never reinvent), and (b) understand how
everything BUILDS so no pending item introduces a build-time cyclic dependency.

## VERDICT: the cross-repo graph is a CLEAN DAG — no build-time cycle exists.
Every candidate loop is held open by a deliberate **runtime seam** (env var, PATH
`command -v` lookup, lazy in-function import, HTTP/WAMP/socket IPC). The job is to HOLD
those seams, not cut an active cycle. **The one active BLOCKER is #135** — a realise-time
FOD-hash failure, not a cycle.

## The 5 governing rules (keep it reuse-first + cycle-free)
1. **FODs, not fetchers.** #135 is the only active blocker: pin `nunbaRev`+`nunbaHash`+
   `npmDepsHash` together (PUBLIC repo, **no token**) so the closure realises. Every model
   download stays a runtime curl (`hart-llm-provision`), never a build FOD.
2. **`hart-app` is a PURE LEAF + star fan-in.** 15+ service wrappers interpolate
   `${cfg.package}`; assets flow OUT, never back in. Any inward edge (hart-app.nix reading
   another module's derivation) instantly creates a real cycle. Build it FIRST.
3. **ONE rust toolchain.** `hart-rust-precedent` (rust_1_88 via `hartRustNixpkgs` 25.05) is
   the single precedent; `hart-comp` and **#168 `hart-os-native` thread the SAME input**,
   with all C buildInputs on `pkgs` 24.11 (libgbm-in-mesa). A 2nd rust/nixpkgs pin re-breaks
   the gbm link. `os_bridge/contract.py` stays the declarative schema the Rust daemon
   implements over socket/D-Bus — **never cross-imported** by `routes.py` at Python load.
4. **Runtime seams stay runtime.** `NUNBA_STATIC_DIR` env; `hart-comp → glass-shell`
   `command -v` PATH lookup; `model_bus → shard_runtime` + all `core → integrations`
   imports LAZY in-function; `WorldModelBridge`'s `sys.modules` guard; siblings
   (hevolveai / Hevolve_Database / mobile) reached only over HTTP/WAMP — **never a nix
   buildInput** (would drag torch/transformers/CUDA into the OS closure).
5. **Route, never reinvent + Gate-6.** Every new module registers INTO `liquid_ui_service`
   (never inlined) AND into `setup_freeze_nunba.py packages[]` + the Flask static route, or
   it 404s / ModuleNotFoundErrors in the frozen Nunba `.exe` (invisible in dev). Inference,
   download, cache, IPC, art all have canonical homes — extend them. The concurrent session
   owns `core/shard_runtime`.

## Build DAG — topological order (build left→right)
```
hart-rust-precedent → hart-comp → [hart-os-native #168] → hevolveai(armored) →
hart-first-boot → llama-server ∥ nunba-static(FODs: pin #135 BEFORE realise) →
hart-app(LEAF, build first among HARTOS derivations) → hart-cli → hart-liquid-ui-wrappers →
hart-layer-shell-host → service-wrappers → hart-llm-provision → hart-hartlog-create →
hart-desktop-closure → iso-desktop
   ∥ nunba-desktop-exe (SEPARATE platform artifact; consumes HARTOS+sibling SOURCE only)
   ∥ hevolve-web / RN / iOS / Hevolve_Database (out-of-closure, independent)
```

## Latent cycles (all broken today by a runtime seam — HOLD each)
| Candidate loop | Held open by | Regresses if… |
|---|---|---|
| hart-liquid-ui → nunba-static | nunba-static is a LEAF (FODs only, ignores `hartSrc`) | you wire the unused `hartSrc` formal into the SPA build |
| HARTOS-closure builds Nunba SPA ↔ Nunba exe bundles HARTOS | **source-only** both ways (neither consumes the other's build OUTPUT) | nunba.nix consumes a BUILT HARTOS, or the exe depends on nunba.nix store output |
| hart-comp → glass shell | runtime `command -v` PATH lookup | swap for `${layerShellHost}/bin/...` store interpolation |
| hart-app ← 15+ wrappers | one-directional star (hart-app reads nothing in-tree) | hart-app.nix embeds a built asset from a consumer |
| os_bridge Python ↔ #168 Rust crate | runtime socket/D-Bus; contract is declarative | `routes.py` imports the crate output at module load |
| model_bus / compute_mesh → shard_runtime | lazy import inside `_handle_shard_frame` | a module-top `import shard_runtime` |
| core → integrations | lazy in-function imports only | any core module top-level-imports integrations (banned layering) |
| WorldModelBridge → hart_intelligence | `sys.modules.get` short-circuit (main-thread only) | a worker forces `from hart_intelligence import` (300s import-lock zombie) |
| nunba-exe ← hevolveai/DB/HARTOS SOURCE | siblings import nothing from hartos/nunba | any sibling `setup.py` imports hartos/nunba (cx_Freeze tracer cycles) |
| OS closure → hevolveai/DB | no edge (runtime-bridged) | someone adds hevolveai as a nix buildInput ("local brain") |

## Reuse map — every pending item extends an EXISTING home (never reinvent)
| # | Reuse this | Reinvention trap |
|---|---|---|
| **135** | `nunba.nix:nunbaHash/npmDepsHash` via `nix-prefetch-github` + `prefetch-npm-deps` | a private-repo token path (repo is PUBLIC); reviving the `:5000` AppImage |
| DEFAULT-MODEL | `hart-llm.nix:modelUrl` (HART_DEFAULT_MODEL_URL) | a new downloader; a model over the single-slot budget |
| **116** | `hart-liquid-ui:NUNBA_STATIC_DIR` + `liquid_ui_service` static-serve | the removed `--server-only :5000` daemon |
| **167** | `native_onboarding.py:accept_btn` + `hart_onboarding.has_hart_name` seal; TTS = Nunba `LightYourHART`/`HARTSpeechPlayer` | a new naming/seal flow |
| **169** | `liquid_ui_service.openPanel` (L3193 registry) + `hartFiles.navigate` history + `hartContextMenu.js` | a parallel router/window-map; duplicate ctx menu |
| **166** | `hartSessionUI.js:#lock-screen` + `hartBootSplash.js` — pre-paint ORDERING only | a new ext-session-lock component |
| **117** | `app_bridge_service.CapabilityRegistry+IntentRouter` + `os_bridge/contract.describe_contract` + `hartOSBridge.js` | a new SDK/manifest schema |
| **118** | `shell_manifest.PANEL_MANIFEST/SYSTEM_PANELS` + `buildStartMenu/openPanel` | a new Settings app / 2nd menu registry |
| **119/157/145** | `shell_system_apis._fsck_cmd/_defrag_cmd/_resize_cmd` (all-FS) + `os_bridge` disk/display + `app_bridge` win/android dispatch + `hart-sandbox.nix` | duplicating fsck/mount/run/open |
| **120** | `hart-notify.nix`(mako) + `shell_desktop_apis` kanshi + `hartDock.js` + `world_model_bridge` | building each gap fresh |
| **121** | `liquid_ui_service.build_home_payload` (one producer, all surfaces) | per-surface hand-authored data |
| **123** | `model_bus_service._route_tts/_route_stt` + `hartHero` click-to-talk | a new whisper/piper pipeline |
| **125** | `core/llama_scheduler` + `resource_governor` + `foreground` + `http_pool` | new cache/thread layers; eager langchain import |
| **143** | `app_poster.resolve_app_poster` + `shell_manifest.bundled_app_logo` + `media_semantic_index.ImageCache` | a 2nd fetcher; editable Magnific SVG (rasterize + credit) |
| **144** | `media_semantic_index.ImageCache` (content-addressed) + `peer_link` P2P | a new CAS; central-as-gatekeeper |
| **142** | `shell_desktop_apis._SOUND_EVENTS/themes` + `hart-audio.nix` (canberra) | a new sound engine |
| **162** | `hartPersonalize.js` Backgrounds (scaffolded) + bundled `lottie.min.js` + wallpaper dirs | a new wallpaper subsystem; ignoring the CPU floor |
| **147** | `core/gpu_tier` + Model-Bus backend routing (MLX = a routed backend) | forking an inference path (concurrent-session rule) |
| **163** | `hart_wm_client.close_window` → compositor `window.close` IPC | a SIGKILL/pkill path bypassing the guardrail |
| **164** | `ipc.rs` events + a NEW `window.ping/unresponsive` (compositor rebuild) | polling processes for hangs |
| **134** | compositor `wayland.rs/udev.rs` libinput seat + onboarding skip button | treating pointer-frozen as a WebView bug |
| **126** | `hart-comp`/cage libinput `tap` + `hart-gpu-offload` | tap-in-JS; flipping the cairo WebView to GPU (breaks d8c1567 baseline) |
| **168** | `os_bridge/contract._PLANNED_DOMAINS` (disk/network/display declared) + `hart-rust-precedent` toolchain | a new IPC/contract; a 2nd rust pin |
| **60** | `ota_push_listener` + `FleetCommandService.verify_command_signature` (Ed25519) | a new signing/replay layer |
| **48/128/130/138/146/148/129/114** | existing nix modules / shell APIs / wizard / venv_paths / first-boot / CI deps / smoketest | bespoke replacements |

## The one cycle-safe global sequence
`135 → DEFAULT-MODEL → 116 → 129 → 136 → 146 → 148 → 48 → 128 → 130 → 138 →`
`{167, 169, 166} shell cluster →`
`{117, 118, 119, 120, 121, 123, 125, 143, 144, 142, 162, 157, 145, 147} runtime app/system cluster →`
`163 → 164 (compositor rebuild) → {134, 126} (compositor + real-HW) →`
`168 (new Rust crate) → 60 → 114 → 149-158 real-HW flash-verify (FINAL).`

**Only new build classes:** #168 (`hart-os-native` buildRustPackage — reuse the one toolchain),
#164/#134/#126 (compositor Rust rebuild). Everything else is runtime Python/JS or a small nix
module wrapping `hart-app`. Nothing introduces a cycle.
