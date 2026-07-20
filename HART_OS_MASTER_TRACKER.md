# HART OS — Master Request Tracker

Living tracker of **everything the steward has asked for**, with honest status.
Updated by Claude each session. Newest concerns at top of each section.

**Status legend**
- ✅ **DONE+VERIFIED** — shipped and confirmed working
- 🟢 **DONE, NEEDS REFLASH** — committed + tested in CI, but NOT in the nightly you're booted on (`89279df`). A reflash makes it real.
- 🔄 **IN PROGRESS** — actively being built
- ⏳ **PENDING** — agreed, not started
- 📋 **QUEUED** — captured as a direction, sequenced behind the foundation

> ## 🔑 THE ONE ACTION THAT UNBLOCKS THE MOST
> You're booted on **`nightly-89279df`**. The newer nightlies fix the GPU hang, the
> taps, and the slowness — but they're **not flashed yet**. The stick is still in
> Windows, so a **reflash to the newest nightly** turns a big chunk of 🟢 into ✅.
> Latest published: **`nightly-c4323ed`** (the GPU gate). GSK-Vulkan (`70e0a116`) +
> smoke-test (`10538ca5`) are newer commits whose nightlies may still be building.

---

## 1. Boot, display & input

| # | Request | Status | Where / note |
|---|---------|--------|--------------|
| 1.1 | Tier-1/Tier-2 must paint, not black-loop | ✅ | DRM-master fix `52a6e26e`; cage floor renders the full shell (your photo) |
| 1.2 | Touchpad taps register | 🟢→⚠️ | **sway/hart-comp** tap-to-click fixed `e1bf4af3`. **BUT you're on CAGE, which the fix does NOT cover** → see 1.6 |
| 1.3 | gtk4-layer-shell drop-to-cage | 🟢 | LD_PRELOAD fix `e1bf4af3` (needs reflash) |
| 1.4 | **Tier-2 sway HUNG → cage** (06-25 journal) | 🟢(unflashed) | TWO real-HW root causes nailed from the journal: (a) GSK=**vulkan** regressed paint (surface-lost on Tier-1 pixman, no-paint on Tier-2) → forced **cairo** `d032cc5`; (b) ~26s GTK stall on the missing portal → start xdg-desktop-portal + `XDG_CURRENT_DESKTOP=sway` `d032cc5`. In `dd841b65` build |
| 1.5 | GPU render lever | 🟢 | `c4323ed`/`70e0a116` superseded by 1.4's **cairo** decision — Vulkan can't present to a software compositor; cairo is the proven paint path. GPU accel returns via `preferHardwareGL` once compositor GPU-buffer-sharing is real-HW proven |
| 1.6 | **Taps STILL dead in cage** (NEW) | ⏳ | cage has no config file; the sway tap fix doesn't reach it. Reaching sway (1.4) gives taps for free |
| 1.7 | Portal timeout in boot log | 🟢(unflashed) | FIXED `d032cc5` — start xdg-desktop-portal (gtk backend) in the sway host + set XDG_CURRENT_DESKTOP so GTK's startup Settings query resolves in ms instead of the 25s D-Bus activation stall |
| 1.8 | **nouveau MMIO FAULT [PRIVRING]** on the 940MX (NEW, 06-25 journal) | 🟢(unflashed) | `d032cc5` blacklist nouveau + `nouveau.modeset=0` — the Maxwell dGPU the open driver can't drive faulted + dragged out boot; Intel iGPU (healthy) drives display. dGPU returns opt-in via proprietary driver for AI compute |
| 1.9 | **Backend unreachable on offline USB** ("couldn't connect / Reconnecting") | 🟢(unflashed) | LIVE-OS #1 ROOT CAUSE: transitive network-online.target stall delayed :6777 ~90-120s. `acc4dd1b` drops it from hart.target + hart-first-boot so the brain binds offline. In `dd841b65` |
| 1.10 | **hart-comp XWayland missing** (06-25 journal) | 🟢(unflashed) | `dd84a24d` adds pkgs.xwayland to the hart-comp session PATH + guarantees XDG_RUNTIME_DIR. A STEP toward the Rust moat (not the finish — moat still skeleton); no Rust rebuild |

## 2. Performance

| # | Request | Status | Where / note |
|---|---------|--------|--------------|
| 2.1 | Assistant on GPU not CPU (llama.cpp) | 🟢 | Vulkan GPU build + `-ngl` gating `10585ff9`/`51e1de38` (needs reflash) |
| 2.2 | Throttle CPU so llama doesn't starve OS | 🟢 | CPUWeight 50, Nice 10, threads=nproc-1 `51e1de38` |
| 2.3 | **Cage is slow vs Windows** (NEW) | ⏳→🟢 | Cage IS the software floor (GTK3/WebKit, no GPU) — slow by design. The fix is **getting off cage onto a GPU tier** = exactly 1.5. Reflash → Tier-2 + GPU = snappy |
| 2.4 | langchain/autogen import slowness | ✅ | lazy-imports `4da0eec` + `552-553` |

## 3. UI / Shell

| # | Request | Status | Where / note |
|---|---------|--------|--------------|
| 3.1 | Orb reacts to thinking + mic | 🟢 | `89279df` (you're booted on this — should work) |
| 3.2 | **Idle voice-orb animation** | 🟢→❓ | Reactivity logic fixed `89279df`; the idle *breathing* runs via requestAnimationFrame but is likely **choppy on the cage software floor** — smoothness needs the GPU tier (1.5). Honest: logic ✅, smoothness pending GPU |
| 3.3 | Deterministic-first app search | 🟢 | `89279df` — app names launch instantly, no "Thinking…" |
| 3.4 | **Float the ONE Nunba UI into every shell (microfrontend), don't recreate per tier** (NEW) | 📋 | Direction captured. Needs: confirm whether `liquid_ui_service` (server-rendered shell) and the Nunba React app (`landing-page/`) are duplicate UIs, then unify — float the React app as a microfrontend hosted by cage/sway/hart-comp, instead of two shell implementations |
| 3.5 | Onboarding "New password" overlaps search hint | 🟢 | FIXED `b19c9501` — opaque base under the tint (WebKitGTK backdrop-filter unreliable) |
| 3.6 | **App Store: dead buttons / can't install / no bundled-apps list** (NEW) | 🟢 | Route-drop is ALREADY in your boot (verified — backend works). Net-new `b19c9501`: "Installed (N)" list + honest install feedback. ⚠️ If buttons are STILL fully unresponsive after reflash → deeper WebView-focus (LIVE-OS #2), will chase |
| 3.7 | **Group eye + mic at bottom (sensory), leave orb alone** (NEW) | 🟢 | FIXED `b19c9501` — `#hart-senses-mic` beside the eye; central orb byte-identical/untouched. → SUPERSEDED by 3.9: steward says remove the orb mic ENTIRELY (the orb IS the voice control) |
| 3.8 | **Remove em dashes from the OS's visible text** (NEW) | 🟢 | FIXED `39063b74` — 14 em dashes → hyphen in the shell render-text + static-JS UI strings; comments/docstrings/logs left (not visual); verified zero in the served /shell |
| | **── UX OVERHAUL (ultracode workflow `wcheodrri`, MAX effort) ──** | 🔄 | 3-vision Behance design panel → synthesis → split implement (CSS/structure ‖ JS behavior) → adversarial review → fix. I verify served-page + tests, then commit. Covers 3.9–3.16 |
| 3.9 | **Orb must have NO mic** — the orb IS the voice control ("what's the point of a mic within the orb?") | 🔄 | SUPERSEDES 3.7: remove `#hart-hero-mic` entirely; orb click = toggle voice |
| 3.10 | **Sensory inputs = floating, DRAGGABLE, grid-snapping widget** (not rigid like cage) | 🔄 | eye+mic (+ room for more) movable, position persisted; web-layer drag → works on cage now, real in Tier-1 |
| 3.11 | **Contextual/deterministic visibility** — elements appear/disappear by what's needed now | 🔄 | a real visibility engine keyed on live state |
| 3.12 | **Active-state lighting** — mic lights when used, eye lights when AI sees, orb lights when listening/thinking | 🔄 | deterministic, on real state |
| 3.13 | **Desktop icons** rearrangeable / sortable / draggable / **snap-to-grid**, layout persisted | 🔄 | |
| 3.14 | **Workspace pager redesign** — the naive "1 2 3 4" → a designed premium pager | 🔄 | |
| 3.15 | **Pager is also BROKEN** — clicking 2/3/4 selects but the desktop never switches (NEW) | 🔄 | functional fix folded into the pager work + behavior-review; I confirm switching works before commit |
| 3.16 | **Behance-level visual overhaul** — no naive elements, cohesive tokens, depth + motion | 🔄 | the umbrella above |
| 3.17 | **Appearance "couldn't reach the hive backend"** on double-click (NEW) | 🟢(local) | RESOLVED. The copy LIED: the panel always had 8 LOCAL themes + 8 gradient wallpapers (offline-safe); only the Images section fetches /api/shell/wallpaper/collection = the user's LOCAL Pictures, NOT the hive. Overhaul renders the local themes; `9b037af0` (local) fixes the misleading copy (Images→"Photo wallpapers unavailable"; generic→neutral "Reconnecting"). No real hive dependency |
| 3.18 | **Everything-on by default (privacy-first)** (NEW) | 🟢(local) | `b0fbddac` (local) desktop.nix: portal/screenshot-record + a11y/cups/dns/email/firewall/ime/devtools ON; Hive-egress (federation/public-exposure/contributing-compute/marketing) stays OPT-IN + consent. Memory: hartos_privacy_first_defaults |
| 3.19 | **nightlight + dlna = preferences, not default-on** (NEW) | 🟢(local) | `edbeb6c6` (local) — reverted from 3.18 (steward catch); available but user-activated (screen tint / LAN media broadcast). DNS + firewall stay ON (privacy-POSITIVE) |
| 3.20 | **Screenshot + screen-record now functional** (NEW) | 🟢(local) | `b0fbddac` enables hart.portal → grim/wf-recorder installed + fail-closed on the screen kill-switch. Was built but OFF (portal was opt-in). The app-to-app ScreenCast portal stays VM-pending |
| 3.21 | **OS connectivity tray — wifi/bluetooth/battery/volume + quick settings** (NEW, "nothing an OS should have") | 🟢(unflashed) | `dd841b65` (ultracode workflow) — top-bar indicator cluster (live glyphs) + glass quick-settings popover (toggle wifi/bt, join a network, volume slider, battery). Backend probes ALREADY existed (shell_system_apis) + degrade cleanly; the gap was the tray. New `hartConnectivity.js`. Works on EVERY tier incl. cage |
| 3.22 | **Sensory-pod drag rubber-bands a selection** (NEW, 06-25) | 🟢(unflashed) | `edc4a9b3` — preventDefault + user-select:none during the drag |
| 3.23 | **App Store install: no progress bar** (NEW, 06-25) | 🟢(unflashed) | `edc4a9b3` — honest indeterminate sweeping bar (backend flatpak install is blocking with no progress stream; a real % needs a bg-job refactor, queued) |

## 4. Cross-OS apps & native app format

| # | Request | Status | Where / note |
|---|---------|--------|--------------|
| 4.1 | Cross-OS runtime smoke-test (prove Wine/Android/macOS actually run) | 🟢 | `10538ca5` — honest per-runtime verdict to `/run/hart/compat-status` |
| 4.2 | Finish Wine / Android (Waydroid) / macOS (Darling) compat + registry | ⏳ | Runtimes wired (better than old audit); finish the stubs + unified registry |
| 4.3 | **Native `.hartapp` apps** — AI composes+compiles loosely-structured apps, self-organizing, recipe-banked | 📋 | Queued behind the daemon foundation (§5). Design captured in `hartos_universal_ai_native_os_vision_2026-06-24` memory |

## 5. Foundation — all Nunba capabilities as native daemons (the "first step")

| # | Request | Status | Where / note |
|---|---------|--------|--------------|
| 5.0 | Audit Nunba caps vs HART daemons | ✅ | ~60-65% already daemons (brain, agent, LLM, vision, shell, compositor, OTA). **Model Bus** (`com.hart.ModelBus`) already serves LLM/vision/TTS/STT to any app |
| 5.1 | Notifications daemon | ⏳ | No mako/dunst + D-Bus bridge → AI-composed apps can't notify natively |
| 5.2 | Screen-capture portal | ⏳ | `hart-portal.nix` does settings+lock only; wire screencopy → portal, gate on B3 kill-switch. (Also fixes 1.7) |
| 5.3 | Tray / status indicator | ⏳ | glass shell has no interactive tray |
| 5.4 | Standalone WAMP router daemon | ⏳ | `wamp_router :8088` in-process → mobile fan-out dies with the brain proc |
| 5.5 | Watchdog daemon | ⏳ | promote `security/node_watchdog.py` to a systemd unit |
| 5.6 | TTS as a unit | 🟢(soft) | already SERVED via Model Bus (D-Bus TextToSpeech); own unit optional |

## 6. Robot / embodied (the same machine, one substrate up)

| # | Request | Status | Where / note |
|---|---------|--------|--------------|
| 6.1 | **HARTOS installable on any robot, auto-query the robot API surface, learn autonomously** (NEW) | 📋 | Robot-API probe = embodied twin of the cross-OS smoke-test; registers ROS/SDK caps into the Model Bus + `WorldModelBridge`/`RobotAction`; learns via Recipe Pattern. Embodied seed exists (`ModelType.EMBODIED`) |

## 7. The unifying frame
All of the above is **ONE self-extending AI-native OS**: lands on any substrate (PC /
phone / runtime / robot) → probes its capability surface → exposes it via the **Model
Bus** → AI composes apps + behaviors + learns by recipe. Full write-up:
`memory/hartos_universal_ai_native_os_vision_2026-06-24.md`.

---

## Immediate next actions (recommended order)
1. **Reflash the newest nightly** (stick is in Windows) → tests 1.4/1.5/2.1/2.3 at once.
2. **Cage tap-to-click** (1.6) — so the floor is usable even when it's the floor.
3. **Microfrontend UI unification** (3.4) — one floated UI, audit duplication first.
4. **Daemon foundation** (§5) — notifications → capture/portal → tray → WAMP → watchdog.
5. **Robot-API probe** (6.1) and **`.hartapp`** (4.3) — on top of the foundation.
