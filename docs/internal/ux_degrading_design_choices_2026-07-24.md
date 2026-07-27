# UX-Degrading Design Choices — HART OS Audit (2026-07-24)

Triggered by the steward's question after the `nightly-0d7c84f` real-HW boot:
*"what are all other such design choices which were wrongly made in HARTOS which
is making the overall system worse user experience?"*

Four parallel read-only audits (polling · parallel-paths · hang/false-healthy ·
offline-first) + the steward's own live observations. Every finding carries
`file:line`, the concrete trigger, the UX symptom, and the fix.

## The meta-pattern: **a cheap proxy substituted for the real signal**

Every finding below is one of four faces of the *same* mistake — trusting a
convenient stand-in instead of the truth:

| Face | Proxy used | Truth it should use |
|------|-----------|--------------------|
| **Poll-for-event** | a wall-clock timer | the event that already exists (SSE / WAMP / D-Bus / systemd notify / udev) |
| **Copy-for-canonical** | a re-authored shell twin | the canonical Nunba microfrontend the shell already floats 40 of |
| **Liveness-for-function** | a bound port / non-null pointer / HTTP 200 / thread-looping | verified output (real throughput, a mapped+scanned-out frame, a banked artifact) |
| **Optimistic-for-real-state** | hardcoded sample data / assume-network | an honest empty / offline / degraded state |

The infrastructure to do it right **already exists everywhere** (SSE bus, WAMP,
`/ready`, `app_catalog`, the iframe-float mechanism, `core.subprocess_safe`).
Almost every item is a localized regression on top of a working-right primitive,
not a missing primitive.

---

## TIER 0 — actively breaks the OS, or is exactly what the steward saw on screen

| # | Finding | `file:line` | Symptom | Fix |
|---|---------|-------------|---------|-----|
| 0.1 | **Mic click freezes the cage floor — STILL LIVE on the default surface.** The GTK4 layer-shell host got the `web-process-terminated → exit(1)` recovery + `GST_PLUGIN_SYSTEM_PATH_1_0`; the cage GTK3 **floor** (`defaultSession="hart-shell"`, the never-fail surface + sway Tier-2 client) never did. | `nixos/modules/hart-liquid-ui.nix:151,158,167,190-208` (no web-process-terminated, no GST path) vs the fixed `hart-layer-shell-host.nix:264,523,638-649` | Click the mic on the default desktop → WebKitWebProcess SIGSEGVs on the missing GStreamer `valve` → surface goes blank, host stays alive so the supervisor's `wait` never returns → **no auto-recovery**. This is the exact incident the steward hit, unfixed on the surface that always runs. | Back-port both GTK4 fixes to the cage floor: connect `web-process-terminated`→`os._exit(1)` and export `GST_PLUGIN_SYSTEM_PATH_1_0`. |
| 0.2 | **Fabricated home data on a brand-new box.** `hartHome.js::samplePayload()` paints "2,140 Spark / 3 agents · 41 tasks" + invented half-done tasks ("Trip to Goa 30%", "Fix STT streaming 45%") + fake hive activity instantly; the live upgrade only *replaces* a row on **non-empty** data, so on a fresh/offline box the fakes persist. | `integrations/agent_engine/static/hartHome.js:84-160,711-738,776` | **This is what the steward saw** — someone else's fake earnings/history on a just-installed machine. | On empty live data render an honest first-run empty state; seed the hero at 0 Spark / "Payout pending", not 2,140 with fake cards. |
| 0.3 | **App Store offers "Install" for preinstalled apps, then fails offline.** The UI uses a hardcoded JS `CATALOG`, not the offline-first `/api/apps/catalog` (which already dedups via `shutil.which` and returns "Open"). Firefox/VLC/GIMP/etc. are **baked in** (`desktop.nix:570 bakeMissing=true`, `585-669`) yet show Install; `list_installed()` uses `nix-env -q` which can't see `environment.systemPackages`. | `static/hartMarketplace.js:16-39,205-246,272`; correct backend unused at `shell_desktop_apis.py:511-516`; `app_installer.py:490,789-825` | Firefox shows Install → click → `flatpak install` → offline fail → "Retry". The steward's `#2`. | Repoint `hartMarketplace.js` at `/api/apps/catalog`; render "Open" for `which`-detected apps; give Flathub-only entries an honest "Needs internet — installs when connected" offline state. |
| 0.4 | **"Disk 100%" reads the read-only squashfs.** `/api/shell/storage` enumerates *all* `psutil.disk_partitions` with no filter and averages `overall_percent`; on the live ISO the always-100%-full squashfs (`/nix/.ro-store`) is counted. | `integrations/agent_engine/shell_system_apis.py:608-636` | The Storage panel alarms at ~100% for no real reason. The steward's disk meter. | Exclude read-only / squashfs / overlay-lower / loop mounts; compute over writable partitions only. (Also fixed for the *installed* image by #1 raw-desktop.) |
| 0.5 | **No voice on a fresh/offline boot — no TTS ships in the image.** Onboarding is designed to *speak* and the orb narrates, but LuxTTS/PocketTTS are first-use downloads and the "always available" espeak-ng floor is only in `hart-accessibility.nix:89`, gated on `screenReader.enable=false` — desktop never enables it. | `luxtts_tool.py:44-50`, `pocket_tts_tool.py:153,206-224`, `hart-accessibility.nix:25,30,89` | The narrated "Light Your HART" ceremony and the orb greeting are **silent** offline; `speak()` no-ops. | Add `espeak-ng` to `desktop.nix` systemPackages unconditionally (tiny closure) as the guaranteed offline floor; bundle one small sherpa/piper voice; same for a bundled STT. |

---

## TIER 1 — false-healthy: the system reports "fine / done" while broken

| # | Finding | `file:line` | Symptom | Fix |
|---|---------|-------------|---------|-----|
| 1.1 | **`/status` (the documented health check) is hardcoded green.** Top-level `status:running` is seeded unconditionally; bridge fields appended in a swallowing try. | `hart_intelligence_entry.py:10484-10506` | Anything polling `/status` for "is the node working" gets green even when LLM, daemons, and learning are all down. | Roll the verdict up from real sub-checks + non-200 on critical failure, or redirect callers to the honest `/ready` (`:10518-10599`, real `SELECT 1`, 503 on fail). |
| 1.2 | **The node watchdog calls itself healthy from an intent flag and can die silently.** `get_health` returns healthy iff `self._running` (only toggled in start/stop, never vs `thread.is_alive()`); `_check_loop` has no top-level guard. | `security/node_watchdog.py:266-267,289-295` | One exception kills the single monitor thread while the dashboard shows `watchdog: healthy` forever — **the root of the whole recovery tree, itself unmonitored.** | Derive health from `thread.is_alive()`; wrap `_check_all` so one bad pass can't kill the loop; expose+alert on `last_check_completed_at` aging. |
| 1.3 | **`learning_active: True` from a non-null pointer / bare 200** — the product core "healthy with 0 real data." The 50-batch flush buffer + local-model 0-spark trap both report active while nothing reaches HevolveAI. | `integrations/agent_engine/world_model_bridge.py:1710-1722,1732-1744` | `/status`, `/ready`, dashboard show the learning pipeline green while it buffers into the void. | Make `learning_active` reflect real throughput (`experiences_flushed_in_last_Ns > 0`); keep `healthy` for reachability but stop asserting activity from presence. |
| 1.4 | **Heartbeat = "thread looped," not "work progressed."** `while: heartbeat(); try: work() except: sleep_with_heartbeat(backoff)` beats every iteration even if `work()` throws every time. | `security/node_watchdog.py:123-128,162-221` | A daemon livelocked on the same failing goal (flywheel "recipe_requested" stall) reads healthy — no restart, no alert. | Pair liveness with a **progress** counter; freeze-detect on progress stagnation; bound `mark_in_llm_call` with an absolute ceiling. |
| 1.5 | **`ActionState.TERMINATED` maps to ledger `COMPLETED`** — a masked failure the code knowingly ships (contrast the honest `GAVE_UP→FAILED`). | `lifecycle_hooks.py:145,206-231,416-426` | A stalled/force-terminated action shows COMPLETED and **inflates the user's completed/earnings count** — "told done," no artifact produced. | Map TERMINATED to a non-success terminal, or gate the →COMPLETED reconcile on verified artifact existence (recipe/banked output). |
| 1.6 | **Cage floor touches `shell-ready` on `LoadEvent.FINISHED` alone** (no `_mapped`, no `_load_failed` guard — the GTK4 host has both). | `nixos/modules/hart-liquid-ui.nix:186-188` | FINISHED fires even on WebKit's stock error page or an unmapped window → a black/error screen is kept "healthy," **preventing a legitimate paint-watchdog drop** on sway/hart-comp tiers. Incident (d) on the floor. | Require `mapped AND load_finished AND not load_failed` + an `_on_load_failed` guard (mirror `hart-layer-shell-host.nix:495,508,626-672`). |
| 1.7 | **The real paint proof (`first-scanout`) is written but never consumed by the drop decision.** Compositor emits it; supervisor drop logic reads only `$READY`/`$INPUT_ALIVE`. | emit `compositor/src/udev.rs:984`, `main.rs:403-414`; unused in `hart-session-supervisor.nix` | A tier that rendered into its buffer + fired shell-ready but never scans out (lost DRM master, EACCES page-flips) is HEALTHY, never dropped — the "black-but-healthy" hole the #131 beacon was meant to close, still open in the DROP path. | Add a scanout-watchdog rung requiring `first-scanout` on non-floor tiers; fix stale "unbuilt" comments. |
| 1.8 | **Post-paint freeze has no recovery** — `wait "$sesspid"` blocks forever. `web-process-terminated` covers a crash, not a render-loop freeze. | `nixos/modules/hart-session-supervisor.nix:749` | A compositor/shell that freezes *after* first paint (the "dragged the orb and it hung" class) never exits → greetd never relaunches. Compounds 0.1 + 1.2. | Periodic post-paint liveness re-check, or let `node_watchdog` SIGTERM the frozen session so `wait` returns. |
| 1.9 | **Supervisors report `running: True` = PID-exists, not linked/serving.** hevolveai (uptime-only, breaker only on child-exit), Model Bus `/health` hardcoded `{'status':'ok'}`, WhatsApp/LiveKit `running = poll() is None`. | `hevolveai_supervisor.py:802-858`; `model_bus_service.py:1114-1116`; `whatsapp_supervisor.py:254-263`; `livekit_supervisor.py:580-597` | A bound-but-wedged uvicorn / a Model Bus with zero backends / an unlinked WhatsApp gateway all report healthy while requests time out or messages go nowhere. | Post-spawn `/health` probe of the child; verify ≥1 backend + socket bound; probe gateway "linked?" and report `serving` distinct from `process_alive`. |
| 1.10 | **Security integrity monitor: `is_code_healthy()` returns True when the monitor is None** (incl. one that crashed at boot), and a crashed monitor is never registered for restart. | `security/runtime_monitor.py:153-156,172-176`; `integrations/social/__init__.py:538-547` | A node whose tamper monitor failed to start reports "code healthy," has no tamper detection, and no auto-restart. | Return `unknown`/`degraded` (not True) when the monitor never started; register for restart unconditionally; alert on a missed boot snapshot. |

---

## TIER 2 — polling where an event already exists (latency + potato battery drain)

| # | Finding | `file:line` | Poll | Event source | Fix priority |
|---|---------|-------------|------|--------------|--------------|
| 2.1 | **Shell SSE producer polls its own dict every 2s** — "push" that is a server-side poll; caps the latency of every downstream push-fix. | `liquid_ui_service.py:7721-7737` (writer `:1054`) | 2s | a `threading.Condition`/`queue.Queue` the writer signals | **HIGH — do first** |
| 2.2 | **Boot shell-health wait is a `curl -sf` poll with NO `--max-time`** (duplicated in two modules). | `hart-liquid-ui.nix:81-90,488`, `hart-layer-shell-host.nix:245-255` | 30×1s | systemd socket-activation or `Type=notify`+`sd_notify(READY=1)` | **HIGH** (also a hang: a half-up backend blocks the single curl forever → black first-paint) |
| 2.3 | **Connectivity polled twice** — a 9s server thread re-shelling `nmcli`/`bluetoothctl`/`wpctl` + an 8s browser poll. | `liquid_ui_service.py:452-458`, `static/hartConnectivity.js:498` | 9s + 8s | NetworkManager/BlueZ/UPower/PipeWire D-Bus signals → push over SSE | **HIGH** (worst steady-state CPU/battery drain on the potato) |
| 2.4 | NotificationBell 30s unread poll — the `notification` SSE event is imported but not subscribed. | Nunba `Common/NotificationBell.js:150,157` | 30s | SSE `notification` (`realtimeService.js:218-222`) | MED-HIGH |
| 2.5 | Channel presence 30s poll, one instance per channel. | Nunba `Channels/ChannelPresenceIndicator.js:25` | 30s×N | WAMP `channel.presence.{userId}` (needs a `subscribeChannelPresence` relay) | MED-HIGH |
| 2.6 | Senses (mic/cam) gate polled every 4s for near-static state. | `static/hartSenses.js:200` | 4s | push a senses component over the shell SSE on toggle | MED |
| 2.7 | SmartFS indexer waits for Model Bus via 60×`sleep 5`. | `hart-ai-runtime.nix:604` | 60×5s | `After=hart-model-bus` + bus `Type=notify`/socket-activate | MED |
| 2.8 | Onboarding companion download progress re-POSTs `advance` every 1.2s. | `static/hartOnboarding.js:250` | 1.2s | stream progress over SSE | MED (first-run, bounded) |
| 2.9 | Admin drawer snapshot(2s)/chat-tail(1s); Demopage capability(3s) + setup-progress(1s) polls that duplicate SSE subscriptions in the same files. | Nunba `Admin/AgentOperationsDrawer.jsx:534,541`, `Demopage.js:461-491,4692`, `Agent.js:192` | 1–3s | existing `dashboard.invalidate` / `capability_update` / `setup_progress` SSE | MED (admin/first-run bounded) |
| 2.10 | Kids multiplayer 3s poll runs even when its WAMP+SSE push is live (not gated to fallback). | Nunba `KidsLearning/shared/useMultiplayerSync.js:313` | 3s | gate on `!realtime.connected` | MED-LOW |
| 2.11 | Flash progress(1.2s), WhatsApp QR(3s), engine-ready boot burst(2s), conky settle `sleep 3`, nvidia-smi `while:sleep 30`. | `hartFlash.js:186`, Nunba `GatewayQRDisplay.js:78`, `useLocalEngineReady.js:88`, `hart-conky.nix:69`, `hart-ai-runtime.nix:308,813` | var | SSE / rotation-cadence / `.timer` / `.path` | LOW (rare/first-run/idiom) |

*Justified (NOT findings, so they're not re-flagged): continuously-varying telemetry sampling (`hartSessionUI.js:157` metrics, `hartVisibility.js` local DOM), bounded startup readiness backoffs for external servers with no readiness signal (`llamacpp_manager`, `runtime_manager`), the world-model 10s HTTP fallback across the subprocess boundary, and the idle-compute 30s DB scan (deliberately throttled).*

---

## TIER 3 — parallel paths: canonical Nunba ↔ re-authored shell twin (drift the user feels)

The shell **already floats ~40 Nunba routes as same-origin iframes** (`shell_manifest.py:52-330`, loader `liquid_ui_service.py:3878-3891`). The items below are the always-present chrome that was re-authored in vanilla instead of floated, plus two backend forks.

| # | Finding | Canonical | Divergent shell copy | Symptom / Fix |
|---|---------|-----------|----------------------|---------------|
| 3.1 | **Onboarding — forked at THREE layers.** | `LightYourHART.js` (particles, Web-Audio score, 3-tier pre-synth, audio-synced pacing, 39 langs, `/api/hart/advance`) | FE `static/hartOnboarding.js` (no particles/score; `typeLines` on a fixed **1800ms timer** `:162` while `speak()` is fire-and-forget `:161` → text/voice desync); markup `liquid_ui_service.py:3106-3111` (static `.hob-orb` sphere, not the reactive canvas); BE `hart_onboarding.py`+`onboarding_routes.py` (3-lang `CONVERSATION_SCRIPT`, `/api/onboarding/*`; the "#167 accept-name dead no-op" bug at `onboarding_routes.py:64`) | Signature first-run is a dumb sphere with desynced narration + few languages. **Fix = task #4:** flip `nunba.enable=true`, float `LightYourHART` fullscreen via the existing iframe primitive, retire the three copies. |
| 3.2 | **Theme/Personalize — two disjoint stores.** | `ThemeSettingsPage.jsx`+`themePresets.js`+`ThemeContext.js` → `/api/social/theme/*` (per-user DB, `--nunba-*` vars, 8 presets, 6 roles) | `static/hartPersonalize.js`+`applyPreset` (`liquid_ui_service.py:5759`, comment: *"Mirrors paintPalette / Nunba injectCSSVars"*)+`theme_service.py` → `/api/appearance/*` (disk, `--hart-*` vars, 9 presets, 3 roles) | Changing theme in the shell panel **does not sync** to floated Nunba surfaces (only `hart-default` id overlaps, and it's teal in shell vs violet in Nunba). Fix: one appearance authority emitting both `--hart-*`+`--nunba-*` aliases from one store. |
| 3.3 | **Voice orb — identical math, drifted palette, + a duplicated ORB SELECTOR** (steward-flagged). | `VoiceVisualizer.jsx` (violet `108,99,255`) consumed by onboarding + the Nunba companion window; skin pref `hart_orb_skin` in `localStorage` | `static/voiceOrbViz.js` — byte-identical geometry (PTS=180, same harmonics/rings), drifted to **teal** `[0,230,195]` + 5 extra `STYLES` (`:39-78`); style pref `orb_style` in `HartSession` | The signature orb is a **different color and has a different style-selector per surface**, and a chosen style in one place doesn't carry to the other. Fix: one framework-agnostic render core + one brand palette + one orb-skin store; React wraps it, shell calls it. |
| 3.4 | **Floating assistant chat — drifted transcript, dead capability pills.** | `NunbaChatPill/Panel/Provider` (persisted history, streaming, real identity) | markup `liquid_ui_service.py:3036-3052`, `AC_CAPS` `:5561-5573`, `acSend` `:5660` → `/api/agent/ask` `:6828` | Identity hardcoded `hart_desktop_user`; history DOM-only (forgets on reload); no streaming; the 10 capability pills send `capability` the handler **drops** → dead no-ops; two assistant personas can show at once. Fix: host the canonical `NunbaChatPanel` transcript; wire or delete `AC_CAPS`. |
| 3.5 | **TTS — two engine backends, three HTTP contracts.** | `TTSRouter.synthesize` (`tts_router.py:1208`, sherpa, normalized) via `/api/voice/speak` | `tts/tts_engine.py::synthesize_text` (Chatterbox/IndicParler/CosyVoice, no normalization) via the chat-reply path + `/tts/synthesize` | The same text is spoken in a **different voice/quality** depending on whether it came from a chat turn vs a read-aloud/onboarding tap. Fix: one synth entry; the others delegate/proxy. |
| 3.6 | **LLM dispatch — draft path bypasses the priority scheduler.** | `_pooled_post_with_refusal_check`→`pooled_post`→`llama_scheduler` (`hart_intelligence_entry.py:5498`) | `speculative_dispatcher.py:1890` raw `requests.post` (bypass admitted `:1877`), + a byte-identical payload built twice (`:1751,:1837`) | The draft dispatch **cannot be preempted by a foreground chat turn** — sidesteps the #162 fix, reintroducing preempt-jank. Fix: route all three transports through the pooled+scheduled primitive; extract `_build_inner_chat_payload`. |
| 3.7 | **Non-Latin script constants — one residual drifted copy.** | `core/constants.py:164 NON_LATIN_SCRIPT_LANGS` (36 codes) | `core/agent_personality.py:450 _NON_LATIN_SCRIPTS` (20 keys, missing fa/ur/my/as/km/lo/…) | Persian/Urdu/Burmese get the draft-skip but not the "reply in native script" directive → Latin transliteration TTS mangles. Fix: key the map off the canonical set + a subset guard. |

**FALSE POSITIVES — name collisions, do NOT merge:** `hartMarketplace.js` (OS App Store, `/api/apps/*`) ≠ `MarketplacePage.jsx` (agent-hire); `hartCredits.js` (art-license attribution) ≠ `Credits.js` (paid wallet); `hartConnectivity.js` (wifi/bt tray) ≠ `Channels/*` (messaging bindings).

---

## Offline / first-boot (subsumed by the tiers above, listed for completeness)

- All AI models (TinyLlama LLM, TTS, STT, vision) are first-boot **downloads** → chat/voice/vision dead offline with only a terse "Could not process" (`liquid_ui_service.py:1374-1389`), no honest "finishing setup — connect once" banner. Fix: bundle one small GGUF + one TTS voice in the image; add the honest state. (The *infra* degrades correctly — `hart-llm-provision` exits 0, gates stay closed — it's the user-facing story that's thin.)
- ClamAV daemon starts with no signature DB offline (honest in `hart-security status`, invisible in the shell). LOW.

**Patterns already DONE RIGHT (templates):** `hartConnectivity.js` honest "unknown/not-detected" glyphs; onboarding companion Retry/Skip + deterministic local name fallback; `hart-flathub-init`/`hart-waydroid-init` now event-driven + mark-on-success only (this session); `hart-first-boot.nix` fully offline; `/ready` + `core/health_probe.py` real checks; `core/subprocess_safe.run_bounded`; the shell frontend's `HartTimeoutSignal` on every fetch.

---

## Fix priority (highest leverage first)

1. **0.1 mic-freeze on the cage floor** — back-port `web-process-terminated`+GST to the default surface. It's the literal cited incident, still live.
2. **2.1 shell SSE → real event-driven** (Condition/Queue) — unblocks the latency of every push-fix.
3. **1.1–1.5 stop the health lies** — `/status` rolls up real checks; watchdog health from `thread.is_alive()`+progress; `learning_active`/`COMPLETED`/`ok` from verified output not presence.
4. **0.2 / 0.3 / 0.4 first-impression truth** — honest empty home; App Store → `/api/apps/catalog` with "Open"; disk meter excludes squashfs.
5. **2.2/2.3 kill the boot + connectivity polls** — socket-activate the boot wait (also fixes a hang); D-Bus-signal the connectivity cache.
6. **3.1–3.3 float the chrome** — onboarding (task #4), theme store, orb core+palette+selector.
7. **0.5 bundle an offline voice** (espeak-ng floor) so onboarding speaks with no network.

Each becomes a tracked task; this doc is the durable record.
