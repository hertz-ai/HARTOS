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
| 1.4 | **Tier-2 sway HUNG → cage (your boot log)** | 🔄 | ROOT CAUSE FOUND: `libEGL: not allowed to force software rendering when API selects a hardware device` — on your Intel iGPU `LIBGL_ALWAYS_SOFTWARE` is *refused*, GL goes hardware + hangs. **Today's GPU fixes target exactly this** (1.5) |
| 1.5 | GPU render lever | 🟢 | **gpu-gate** `c4323ed` (stop forcing software when GPU proven) + **GSK-Vulkan** `70e0a116` (no GL context to hang). Both un-flashed — **reflash to test if Tier-2 paints** |
| 1.6 | **Taps STILL dead in cage** (NEW) | ⏳ | cage has no config file; the sway tap fix doesn't reach it. Fix = system libinput tap default for the cage floor. Also: reaching sway (1.5) gives taps for free |
| 1.7 | Portal timeout in boot log | ⏳ | `org.freedesktop.portal.Desktop: Timeout` — the missing portal daemon (see 5.x capture/portal gap) delays GTK startup |

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
| 3.7 | **Group eye + mic at bottom (sensory), leave orb alone** (NEW) | 🟢 | FIXED `b19c9501` — `#hart-senses-mic` beside the eye; central orb byte-identical/untouched |

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
