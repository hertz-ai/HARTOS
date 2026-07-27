# HART OS - Functional Reality vs Intent

> ## WORK QUEUE = THESE THREE DOCS TOGETHER
> Drive all desktop / OS work from three companion docs, read together:
> - **DESIGN (intent):** `HOME_DESKTOP_DESIGN_CHECKLIST.md` - groups a-k, ~55
>   rules, verbatim steward quotes + message numbers.
> - **MASTER SPEC (workstreams):** `../../HART_OS_FULL_DESKTOP_SPEC.md` - the
>   W1-W11 workstreams + the binding rules.
> - **GAPS / TODO (reality + the queue):** `HARTOS_FUNCTIONAL_GAPS.md` (THIS DOC)
>   - the EXISTS / PARTIAL / STUB / MISSING audit AND the unified "TO BE DONE"
>   work queue below.
>
> When you CLOSE an item: update the gap row here + the W-stream status in the
> master spec + the checklist audit. The single unified queue lives in this doc
> (the "## TO BE DONE" section right below).

> **EXPLICIT list of what is NOT yet there as working functionality.**
>
> Steward 2026-06-29: *"checklist shd find what are not there as actual
> functionality in HARTOS and explicitly list them rather than taking note."*
> This doc therefore lists gaps concretely, per-feature, rather than noting them
> in passing. A route existing is NOT the feature working; a placeholder / sample
> dressed as real is STUB, not EXISTS; a UI with no backend producer is PARTIAL.
>
> **Intent source of truth:** `HOME_DESKTOP_DESIGN_CHECKLIST.md` (groups a-k) +
> `../../HART_OS_FULL_DESKTOP_SPEC.md` (the W1-W11 tracker). Each row below cites
> the checklist item it answers to.
>
> **Update this doc as gaps close.** When an item flips to EXISTS, move it out of
> the gap list at the top, update its row, and adjust the totals line. If
> something is a sample dressed as real, say so.
>
> Status legend: **EXISTS** (works end to end, cited file:line) · **PARTIAL**
> (UI or transport present, the producer / backend half is absent) · **STUB**
> (placeholder / sample / hook with no producer, looks real but is not) ·
> **MISSING** (no working functionality at all).

---

## TO BE DONE - the unified work queue (design + spec + gaps)

> The single backlog. Every unimplemented item the steward asked for, gathered
> from ALL THREE sources and deduplicated: (a) every MISSING / STUB / PARTIAL row
> in this gap doc, (b) every pending workstream W2-W11 in the master spec, (c)
> every checklist item (groups a-k) the audit did NOT mark EXISTS, and (d) newly
> surfaced asks found ONLY by re-scanning the steward's 1797 typed messages -
> tagged `(NEW 2d)` and cited to the exact message number so they are not lost.
>
> Columns: **ID** | **Item** | **Status** | **Sources** (checklist item like d3
> / W-stream / msg `#NNNN` / gapN) | **To close** | **Impact / Effort** (effort:
> `wire` = code exists but unconnected, `build` = real new code).
>
> Status not yet code-verified for groups f / g / h / j / k and W2-W11 is taken
> from the W1 audit + real-HW reports + memory; verify-as-you-close. GLM 5.2 API
> integration (`#1278`) is DONE (`model_registry.py:432-449`) so it is not listed.

### A. HOME-SOUL (agentic Netflix home, earnings, imagery)

| ID | Item | Status | Sources | To close | Impact / Effort |
|---|---|---|---|---|---|
| Q1 | Cards show REAL photos, not gradient tiles | STUB | gap1 / b2,d3 | a producer sets `card.image` (W10 media index / agent-recipe art) | high / wire |
| Q2 | Home is COMPOSED by the local LLM (agentic, not static sample) | MISSING | gap2 / i1, W1 | wire an agent_engine / local-LLM producer to call `compose_home` via the /chat decompose path | high / build |
| Q3 | Earnings hero leads with the REAL money figure, empty on a fresh node | PARTIAL | gap3 / d1, k6, W1-F2 | surface the rupee/earnings figure; drop the 2,140-Spark sample on a 0-balance node | high / wire |
| Q4 | Top-bar small breathing orb (orb-sm) renders + compacts into the bar | STUB | gap4 / c7, e1, W1-F1 | mount the hartHero compact orb into `#top-bar-orb` | med / wire |
| Q5 | W10 semantic media index wired at runtime | MISSING | gap5 / d7, W10 | call `register_media_routes` + `register_idle_indexer` at shell start; card producer queries `/api/media/search` | high / wire |
| Q6 | Desktop app icons as REAL photos, not gradient glyph tiles | PARTIAL | gap10 / d6 | manifest entries set an `image` field | med / wire |
| Q7 | Hero communicates VISUALLY + by VOICE, not text walls | PARTIAL | b8 (`#1799`) / d1, i1 | replace the paragraph hero with an animating number + small graphic + orb TTS | med / build |
| Q8 | Dropped rows stay discoverable (no scroll, still reachable) | PARTIAL | W1-F3 / a2, k3 | a visible "More in Hive / Earn" affordance | low / wire |

### B. NETFLIX-EVERYWHERE (listings on every surface, store, settings, omnibox)

| ID | Item | Status | Sources | To close | Impact / Effort |
|---|---|---|---|---|---|
| Q9 | Netflix listings on agents / recipes / communities / settings / file-explorer | MISSING | gap6 / d4, W7 | render these surfaces with `hh-row`/`hh-card` instead of (or over) the Nunba iframe | high / build |
| Q10 | App Store as Netflix image rows, not a glyph grid | PARTIAL | gap8 / d4, W7 | reuse `hh-row`/`hh-card` with photos, text-over-art, hover-expand | med / build |
| Q11 | Unified Settings hub (one page, all customization) | MISSING | gap7 / e6, W4, `#118` | one hub grouping SYSTEM_PANELS + Nunba settings iframes by category | high / build |
| Q12 | Omnibox 3-way routing (deterministic apps/files / semantic media / ask) | PARTIAL | gap9 / e2, e3 | add deterministic file+registry search + semantic media routing to `acSend` | high / build |
| Q13 | App Store Install button does nothing + bundled/preinstalled apps not visible | STUB / bug | `#1666`, `#1685` / d4, W7 | wire Install to the app_ops install verb; list preinstalled apps `(NEW 2d)` | high / wire |
| Q14 | Install / registry / uninstall as a DESIGNED Netflix surface with animated progress | MISSING | d4 (memory) / W7 | minimised views animate install progress | med / build |

### C. ORB + VOICE

| ID | Item | Status | Sources | To close | Impact / Effort |
|---|---|---|---|---|---|
| Q15 | Voice-viz look restored + orb skins switchable | MISSING / regression | c3 (`#1791`,`#1790`) / W1-F4 | restore the pre-`1c4546bd` viz in hartHero/voiceOrbViz; visualiser default + character toggle | high / build |
| Q16 | Orb float / compact / minimise / disappear / reappear over windows; merge-demerge bg; attach-detach chat; autohide | PARTIAL | c4 (`#1723`,`#632`,`#640`) | port the VUI float + taskbar-style autohide to the shell orb | med / build |
| Q17 | Orb hard rules: no outer ring, transparency control, NO mic inside, breathing, brand un-clipped | PARTIAL | c1,c2,c5 (`#1721`,`#1672`) | verify on real HW; remove any mic-in-orb | med / wire |
| Q18 | Realtime voice recognition + realtime responses through the orb / agent_engine | PARTIAL / MISSING | c6, W9 (`#1723`) | wire the realtime STT -> LLM -> TTS loop | high / build |
| Q19 | ASR streaming backend (Nemotron 3.5 ASR 0.6B via parakeet.cpp + whisper, accuracy-selected) | MISSING | `#1265` / W9 | add parakeet.cpp + nemotron path alongside whisper `(NEW 2d)` | med / build |

### D. AGENTS + ECONOMY (real money, embodied, AI-composed apps)

| ID | Item | Status | Sources | To close | Impact / Effort |
|---|---|---|---|---|---|
| Q20 | Peer-compute earning WRITER (per-inference `compute:%`) | MISSING | gap3 / d1.1 (`#1798`) | ship the hive-work earning writer feeding ResonanceTransaction | high / build |
| Q21 | Payout rail (Spark -> real money) | MISSING | gap3 / d1.1 | a real payout path; retire hardcoded `payout_pending=true` | high / build |
| Q22 | Robot-installable: auto-query the robot API surface + autonomous learning | PARTIAL | `#1687`/memory / W6 | the embodied robot-API probe (twin of hart-compat-smoketest) `(NEW 2d)` | med / build |
| Q23 | Reachy Mini + every embodied AI SDK integration tested | UNKNOWN / PARTIAL | `#1406` / W6 | integration tests for the embodied SDKs `(NEW 2d)` | med / build |
| Q24 | `.hartapp` AI-composed self-organising native apps loader | PARTIAL | `#1687`/memory / W3, j1 | AI composes/compiles the app on top of `hart_sdk/app_builder.py` `(NEW 2d)` | med / build |

### E. SYSTEM + SOUNDS (parity-or-better micro-detail, system mgmt, power)

| ID | Item | Status | Sources | To close | Impact / Effort |
|---|---|---|---|---|---|
| Q25 | OS micro-detail SOUND layer (USB connect/disconnect chime, notif sound, error/alert tone, volume + battery-low feedback, haptics) | MISSING | k8 (`#1800`) / b5 | udev -> hart-notify, a cohesive on-brand HART sound set `(NEW 2d)` | high / build |
| Q26 | System-management depth in Settings (devices / accessories / disk / paging / env / DPI / font), typed-native not Flask+subprocess | PARTIAL | j3, W5 (`#1751`) / memory typed-native bridge | one Settings API; native ops (logind/udisks/NM) result-checked | high / build |
| Q27 | Customization-as-API; the API is the SDK; enhance every Nix default | PARTIAL | j1, Rule15 (`#1752`) / W3 | expose each customization as a typed API consumed via the SDK | med / build |
| Q28 | App-registerable context-menu insertions / Start-menu entries / Settings panels (freedesktop + Shell-integration) | PARTIAL | j2, W3 (`#1752`) | the SDK registration surface | med / build |
| Q29 | Windows/macOS feature-parity apps (recycle bin, startup manager, event viewer, file explorer FULL parity) | PARTIAL | j4 (`#248`) | build the missing parity apps | med / build |
| Q30 | freedesktop XDG scan so installed apps auto-appear (.desktop/MIME/autostart + file associations) | PARTIAL | W3-A | hart-app-bridge runtime scan | med / build |
| Q31 | Android-style notification SHADE UI (daemon exists, shade UI absent) | MISSING | W6 (`#1751`) | build the shade UI over hart-notify | med / build |
| Q32 | Floating "disable all AI" button with PROOF its sensory signals are shut | PARTIAL / UNKNOWN | f8 (`#1292`) | minimalist AI-native kill with real-signal proof | med / build |
| Q33 | Right-click context menus canonical (retire the duplicate `#ctx-menu`) | PARTIAL | W8, f3 (`#1368`) | `hartContextMenu.js` fully replaces `#ctx-menu` | low / wire |
| Q34 | Power ops native + boot-to-UEFI / advanced reboot (restart/shutdown/UEFI silently fail today) | STUB | e5 power footer, `#1578`,`#1780` / memory `#133` | logind D-Bus power ops, result-checked `(NEW 2d)` | high / build |
| Q35 | Display power: screen-timeout control + a display Settings GUI | MISSING | `#1589` / W5 | a display/power Settings panel `(NEW 2d)` | med / build |
| Q36 | Multi-monitor / multi-screen + edge-docking + DPI + font-size responsive | PARTIAL | W6, a3, j3 (`#1751`) | the multi-screen / DPI lever | med / build |

### F. INTERACTION (taps, drag, click-semantics, pager, sensory cluster)

| ID | Item | Status | Sources | To close | Impact / Effort |
|---|---|---|---|---|---|
| Q37 | Taps REGISTER on real HW (the #1 recurring dead-husk bug) | bug / PARTIAL | f1 (`#1771`,`#1721`,`#1365`) / boot | real-HW input proof on Tier-1 | high / build |
| Q38 | Keyboard / typing focus reflects input on Tier-1 (ctrl+alt+f2 hung) | bug | `#1550`,`#1781` / f1 | keyboard focus in the compositor `(NEW 2d)` | high / build |
| Q39 | Correct click semantics: touch single-tap opens; desktop single=select, double=open | PARTIAL | f2 (`#1466`,`#1467`) | per-surface click semantics | med / wire |
| Q40 | Everything draggable / movable / rearrangeable; snap-to-grid; sortable (Android-widget style, transparent bg) | MISSING | f4 (`#1397`,`#1675`) | drag/snap for shell elements | med / build |
| Q41 | Movable / dockable multi-window + multi-app concurrency + snap-zones + multi-tap context switch without losing focus | PARTIAL | f5 (`#1721`,`#1751`) | retained; verify + close gaps | med / wire |
| Q42 | Full customizability (add-to-desktop, pin-to-taskbar, wallpaper video/image/solid, theme transparent/translucent, resolution, extended displays, system font, taskbar dock direction, VLC + icon/font gallery) | PARTIAL | f6 (`#1403`,`#249`,`#1293`) | the customization surface | high / build |
| Q43 | The "1 2 3 4" pager redesigned (looks naive) + clicking a workspace actually switches | MISSING / bug | f7 (`#1675`,`#1682`) | a real workspace-switcher design element | med / build |
| Q44 | Sensory cluster: eye + mic grouped at bottom, orb-viz alone at center | PARTIAL | g1 (`#1668`,`#1400`) | group all sensory icons | med / wire |
| Q45 | Sensory = floating draggable WIDGET, contextual light-up (mic lights when used, eye when AI sees) | MISSING / PARTIAL | g2 (`#1673`,`#1675`) | contextual deterministic show/hide + draggable | med / build |
| Q46 | Clustered sensory panel retained for embodied installs | PARTIAL | g3 (`#1751`) | keep across installs | low / wire |

### G. NUNBA-PARITY + ONBOARDING + IDENTITY

| ID | Item | Status | Sources | To close | Impact / Effort |
|---|---|---|---|---|---|
| Q47 | Nunba bundled native + every page a NAMED microfrontend (Start menu + omnibox) | PARTIAL | W2, e7 (`#1723`,`#1769`) | native dist done (AppImage removed); wire all pages as named microfrontends | high / build |
| Q48 | ALL Nunba companion capabilities as native daemons (~60-65%: notifications / tray / capture / WAMP-standalone / watchdog) | PARTIAL | `#1663`,`#1769`/memory / W2 | close the daemon-parity gaps | high / build |
| Q49 | "Light your HART" first-boot onboarding (language + preference wizard, reuse hartOnboarding.js; AI setup wizard part of it) | PARTIAL / MISSING | h1 (`#1365`,`#1721`,`#1729`) | a phase-driven first-boot wizard | high / build |
| Q50 | Initial-password setup + login auth fix (admin / "not listed" auth failed) | bug | `#1588`,`#1590` / h | a working set-password + login `(NEW 2d)` | high / build |
| Q51 | P2P password sync + MFA across all Nunba devices (Bluetooth / authenticator-style accept, no separate login) | PARTIAL | `#1500` / `core/profile_sync.py` exists | extend profile_sync to password + MFA accept `(NEW 2d)` | med / build |

### H. PERF + BOOT / HW

| ID | Item | Status | Sources | To close | Impact / Effort |
|---|---|---|---|---|---|
| Q52 | GPU-accelerated compositor (software-render today) | MISSING | i4, W6 (`#1789`) | a GLES hardware renderer for hart-comp + the GTK4 GSK-GL lever | high / build |
| Q53 | 100x optimization, MEASURED (budgets chat 1.5s / draft 300ms / cache <1ms, 60fps, ZERO hangs) | PARTIAL | i3, W11 (`#1753`) | measure before/after; never regress a budget | high / build |
| Q54 | Hover-expand 60fps real-HW proof (GPU-gated; render-proven only so far) | PARTIAL | d5, W1 / b4, b5 | prove on real HW | med / wire |
| Q55 | Throttle llama.cpp CPU so it does not starve the OS (CPU fallback) | MISSING / PARTIAL | `#1649` / W11 | a CPU-allocation cap for llama.cpp `(NEW 2d)` | med / build |
| Q56 | Nunba auto-installs the correct llama.cpp GPU/CPU build per hardware | PARTIAL | `#1639`,`#1747` / W11 | a HW-aware llama.cpp bootstrap `(NEW 2d)` | med / build |
| Q57 | Tier-1 HART-comp paints on real HW (the moat; software scanout proven, real-HW paint pending) | PARTIAL | `#1642`,`#1592` / W6, i4 | real-HW DRM paint proof | high / build |
| Q58 | Dual-boot GUI install wizard + partitioning (live-OS vs dual-boot) | MISSING | `#1386`,`#1518`,`#1389` / boot | a GUI installer with dual partition `(NEW 2d)` | med / build |
| Q59 | Boot-disk default selection / auto-boot the selected OS on restart | MISSING | `#1593`,`#1594` / boot | programmatic default-boot selection `(NEW 2d)` | low / build |
| Q60 | WiFi / network available on the OS + a network Settings panel | MISSING | `#1272` / W5 | NetworkManager wiring + Settings `(NEW 2d)` | high / build |
| Q61 | Backend autostarts on boot | PARTIAL / bug | `#1508`,`#1554` / boot | reliable backend autostart `(NEW 2d)` | high / wire |
| Q62 | HARTLOG log partition readable from the stick | PARTIAL | `#1599`/memory / boot | carve free space so the journal persists | med / build |
| Q63 | Responsive for ALL device dimensions (multi-screen, big-monitor DPI, touch) staying a fixed canvas | PARTIAL | a3, W1-a3 (`#1751`,`#1463`) | full responsive + DPI | med / build |

### I. INFRA / CI (keeps everything shippable)

| ID | Item | Status | Sources | To close | Impact / Effort |
|---|---|---|---|---|---|
| Q64 | Fix the chronic non-desktop CI failures (Nix Build Matrix, E2E, Lint) | MISSING | `#1269`,`#1270` / Rule6 | drive them green, zero regression `(NEW 2d)` | high / build |
| Q65 | Compositor Rust rebuild exceeds the 6h CI limit on every input/compositor change | MISSING | `#1777`/memory / infra | Rust build caching so nightlies complete `(NEW 2d)` | high / build |
| Q66 | Deploy-docs: download FIRST in Quick links, minimal text, intuitive | MISSING | `#1274`,`#1631` / k3 | reorder the deploy-docs quick links `(NEW 2d)` | low / wire |

### J. PRE-DESKTOP PLATFORM ASKS (recovered from the 1797 messages, in NO doc - all `(NEW 2d)`)

> These are actionable steward asks from the platform layer (channels, sync,
> mobile push/consent, meeting bridge, search, TTS/STT, agent model, memory,
> economy federation, CI) that predate the desktop program and were tracked in
> NEITHER the three docs NOR a MEMORY topic file. Kept here so the queue is the
> single backlog. Some overlap the desktop rows (noted); close once.

| ID | Item | Status | Sources | To close | Impact / Effort |
|---|---|---|---|---|---|
| Q67 | Omni-channel bridge: auto-associate browser-logged-in channels; ingest posts/meets/events into Nunba; cross-post out; join Discord/etc meets as a Nunba user via bridge | PARTIAL | `#193`,`#309`,`#314` | finish the inbound ingest + outbound cross-post + meet-join bridge | high / build |
| Q68 | Unified sync hub: colocate ALL social-entity sync, P2P-first then central fallback (assets from peer first, central when peer gone), consent-gate, idempotent | PARTIAL | `#1121`,`#785`,`#787` | one colocated sync path, peer-then-central | high / build |
| Q69 | Tagged-public local content (posts/communities/agents) flows to central + async regularly | PARTIAL | `#785` | wire the public-tagged async flow to central | med / build |
| Q70 | Truecaller-style mobile system overlay for an incoming agent interaction / consent (over-other-apps) | MISSING / STUB | `#1105`,`#1103` | a mobile over-other-apps incoming overlay | high / build |
| Q71 | public_exposure consent toggle on mobile RN clients (web/desktop have it) | PARTIAL | `#1103` | add the toggle to the RN clients | med / wire |
| Q72 | FCM central -> local PUSH trust flow over WAMP (central pushes its token, node does not pull) | PARTIAL / STUB | `#1117`,`#1118` | implement the central-push-over-WAMP trust flow | med / build |
| Q73 | Meeting reply-audio + video/audio sensor-track ingest bridge (#64): livekit-rtc frame I/O, `_consume_video_track` / `_maybe_ingest_audio_sensor` (lost/reverted) | STUB / PARTIAL | `#1069`,`#777`,`#781` | restore the audio publish + video/audio ingest wiring | med / build |
| Q74 | Keyless, zero-rate-limit web search with citations (SearXNG/meta or DDGS tier-1, crawl fallback, open-meteo weather), fix GOOGLE_CSE failure | PARTIAL | `#602`,`#603`,`#604`,`#607` | bundle a keyless search + crawl fallback | med / build |
| Q75 | TTS: run the existing WAV-based verifier BEFORE selecting the F5 model (uplift autoheal regressed it) | STUB / PARTIAL | `#618`,`#616` | reuse the built WAV TTS verifier in the selection path | med / wire |
| Q76 | Streaming STT/ASR + speaker diarization + voice-activity/noise gate (no all-language text when no speech) | PARTIAL | `#637`,`#636`,`#4078` | fix faster-whisper streaming + diarization + VAD (pairs with Q19) | high / build |
| Q77 | Autonomous capability-upgrade actuator + setup cards: Tier-1 global llama.cpp upgrade via `/api/llm/upgrade` (no restart), Tier-2 per-agent capability re-hydrate cards | PARTIAL | `#669`,`#677`,`#701` | the self-heal upgrade actuator (Claude Code cannot run on users' machines) | high / build |
| Q78 | Polymorphic agents: an agent can morph into / talk to other agents; agent-id-less cards | UNKNOWN / PARTIAL | `#684`,`#683`,`#685` | the polymorphic agent model | med / build |
| Q79 | History-aware chat + natural-time date recall (retrieve AROUND a date, attach timestamps; shared casual memory across langchain+autogen, agent memory isolated) | PARTIAL | `#546`,`#349`,`#548`,`#561` | un-hide the date window + broader memory wiring | high / build |
| Q80 | Casual/factual queries mis-routed into the autogen CREATE pipeline instead of a langchain tool-call; gather_info should plan with the full tool catalog | PARTIAL | `#558`,`#590`,`#596` | route simple queries to tool-call; plan with all tools | high / build |
| Q81 | Programmatic build recovery: python-embed corruption auto-repair (NULL-byte .py, missing METADATA Name header) in `_autorepair` | STUB | `#698`,`#699` | make `_autorepair` reinstall siblings + drop dist-info | med / build |
| Q82 | Real-LLM-in-the-loop E2E tests (A2A, A2P, channel onboarding, consent, LiquidUI) without daemon flooding | MISSING / PARTIAL | `#1150`,`#1148`,`#1160` | real-LLM E2E suite; no grep tests | high / build |
| Q83 | CI release-gating gaps (#163): publish-nightly `needs: test`, pre-merge PR gate, stop ignoring test_social_models / VLM | PARTIAL | `#936` | tighten the release gates | med / wire |
| Q84 | Collective earning federation: every box HARTOS runs earns collectively for the user; gossip earning deltas via federated_aggregator | MISSING / PARTIAL | `#732`,`#737` | wire the L3 earning aggregation across nodes (pairs with Q20) | high / build |
| Q85 | Auto-evolve should also use the hive to auto-evolve | UNKNOWN | `#1159` | route the auto-evolve loop through the hive | med / build |
| Q86 | Common parent channel class to wire metrics + dashboard for ALL channels (not per-adapter) | STUB / UNKNOWN | `#156`,`#154` | wire metrics/dashboard from one parent channel class | low / wire |
| Q87 | Open-source remote-desktop (TeamViewer/RustDesk-equivalent) bundled in HART OS | PARTIAL | `#539`,`#2288` | bundle an OSS remote-desktop (folds into Q29/Q42 baked-in apps) | low / build |

> Also left UNANSWERED by the steward (flag, not yet a task): the parallel-path
> architecture decisions from the repo audit `#435` (delete vs keep the
> cloud-provider media stack; collapse the 5 agent registries; restructure JWT
> secret discovery). Resolve direction before queuing.

### Unified-queue totals

**87 items queued** across 10 areas: A home-soul 8 - B netflix-everywhere 6 - C
orb+voice 5 - D agents+economy 5 - E system+sounds 12 - F interaction 10 - G
nunba-parity 5 - H perf+boot/HW 12 - I infra/CI 3 - J pre-desktop platform 21.
By source: **10** from this gap doc's MISSING/STUB/PARTIAL rows; **W2-W11** all
represented (W1 partial); the **checklist groups f / g / h / j / k** (not
previously folded into this doc) now fully tracked; and **41 newly surfaced
`(NEW 2d)` asks** recovered from the 1797 messages that were in NO doc before (20
desktop + 21 platform). Status rollup (best estimate, verify-as-you-close):
MISSING ~31 - PARTIAL ~43 - STUB ~7 - bug ~4 - unknown ~2.

---

## MISSING + STUB - the explicit gap list

These are the items that do NOT work as the steward intended, grouped and sorted
by user impact (most-visible / most-promised first). Each is concrete and
actionable. PARTIAL items whose missing half is the user-facing one are flagged
at the end of this section so nothing hides.

### Home / desktop visual soul (highest impact - the "Netflix, agentic, real-money" promise)

1. **Cards never show a real photo - every card is a gradient placeholder.**
   *(STUB · b2 / d3 "image-rich, lots of images, text-over-art photos")*
   The renderer fully supports real photos (`hartHome.js:194` lazy `<img>` +
   IntersectionObserver, `:160` `gradientArt` fallback), but NO card payload
   anywhere sets an `image` field: `samplePayload()` (`:84`) has none, and the
   live fetchers `fetchAgents` (`:674`) / `fetchRecipes` (`:704`) / `fetchEarnings`
   (`:603`) never populate it. Result: 100% of cards paint `gradientArt()` (a
   brand-gradient tile), never a photo. The image-rich intent is gradient tiles
   in practice.
   **To close:** have a producer set `card.image` (from the W10 media index, see
   gap 5, or from agent/recipe artwork).

2. **The home is never composed by the local LLM - it is static sample + direct
   fetches, not agentic.** *(MISSING · i1 "the LLM is the heart that paints the
   surface")* The A2UI transport is complete end to end (`compose_home`
   `liquid_ui_service.py:641` -> `agent_ui_update` `:468` -> SSE `:4813` ->
   `HartHome.compose` `hartHome.js:811` -> `render` `:583`), but the ONLY callers
   of `compose_home` are the external `POST /api/home/compose` route (`:5311`) and
   two unit tests (`tests/unit/test_home_compose_feed.py`). No autonomous agent /
   local-LLM path ever composes a home. In practice the surface is the hardcoded
   `samplePayload()` upgraded by hardcoded endpoint fetches - transport with zero
   producer. The "agent decides which rows / content / formats" soul is absent.
   **To close:** wire a local-LLM / agent_engine producer that calls
   `compose_home` (it can ride the existing /chat decompose path, no new
   transport).

3. **"Real money earned via the hive working together" is economically a stub.**
   *(PARTIAL READ, STUB ECONOMICS · d1 / d1.1)* The hero DOES read a real ledger
   (`fetchEarnings` `hartHome.js:603` -> `/api/social/resonance/wallet` then
   `/api/compute/earnings/<uid>` -> `api_compute_earnings.list_earnings:107`
   reads real `ResonanceTransaction` rows). But: (a) the ONLY earning WRITER is
   `api_cost_recovery` (metered-API settle), NOT peer-compute hive work - the
   per-inference `compute:%` writer does not exist yet (explicit "when future
   writers ship" comment at `api_compute_earnings.py:99-104`); (b) `payout_pending`
   is hardcoded `true` (`hartHome.js:631`) and there is NO payout rail; (c) on a
   fresh node `total_spark_in_window=0` so the hero falls back to the ~2,140-Spark
   sample skeleton. d1.1 "real money earned via the hive nodes working together"
   therefore reads a real ledger while the peer-compute earning source and the
   payout rail are both absent - the headline money is sample-or-cost-recovery,
   not hive earnings.
   **To close:** ship the peer-compute earning writer + a payout rail; until then
   the hero shows an empty state on a real node.

4. **The top-bar small orb (orb-sm) never renders - it is a static button.**
   *(STUB · c7 "the hero orb compacts into an always-accessible top-bar orb")*
   `#top-bar-orb` (`liquid_ui_service.py:2014`) is an empty `<button>` whose only
   behaviour is `onclick=toggleVoice()`. `hartHero.js` (which owns the breathing
   voice-viz orb and its compact / dock states) has NO reference to `top-bar-orb`
   or `orb-sm` (grep: zero hits), so the actual orb never mounts in it and never
   compacts into it. It is a CSS-styled placeholder dot, not the always-accessible
   small breathing orb.
   **To close:** mount the hero orb's compact instance into `#top-bar-orb` from
   `hartHero.js`.

5. **The W10 semantic media index is fully coded but COMPLETELY UNWIRED.**
   *(MISSING · d7 / W10 "dynamic image cache + semantic media index feeds the
   cards")* `media_semantic_index.py` has the whole module (MediaSemanticIndex
   `:391`, ImageCache `:768`, `register_idle_indexer:730`, `register_media_routes:937`)
   with unit tests, but at runtime: `register_media_routes` is never called outside
   tests (so `/api/media/search` and `/api/media/image` are NOT mounted),
   `register_idle_indexer` is never started (so the caption catalog is never
   populated), and `hartHome.js` never calls `/api/media/search` or
   `/api/media/image` (grep of `static/`: zero hits). The module header itself
   says "shell wiring into liquid_ui_service is a follow-up" (`:940-942`). The
   semantic-index -> card-image pipeline does not exist at runtime.
   **To close:** call `register_media_routes(app)` + `register_idle_indexer()` at
   shell startup and have the card producer query `/api/media/search`.

### Netflix-everywhere + Settings (the "every surface" promise)

6. **Netflix listings exist ONLY on Home - every other surface is a plain Nunba
   iframe.** *(MISSING · d4 / W7 "Netflix listings EVERYWHERE")* Agents, recipes,
   communities, settings sub-pages and file-explorer all open as iframe panels to
   plain Nunba SPA routes (`shell_manifest.py:61` agents_browse -> `/agents`, `:68`
   communities -> `/social/communities`, etc.; `renderRoutePanel`
   `liquid_ui_service.py:2835` builds an `<iframe src=NUNBA_BASE+route>`). The
   Netflix card system (`hh-row` / `hh-card`) exists ONLY in `hartHome.js` (grep:
   `hh-card` nowhere else). Install / registry / uninstall is also a glyph grid,
   not a designed Netflix surface. W7's "listings everywhere" is unimplemented for
   every non-home surface.
   **To close:** render these surfaces with the `hh-row`/`hh-card` system instead
   of (or layered over) the Nunba iframe.

7. **No unified Settings page hub - customization is scattered across ~40
   individual panels.** *(MISSING · e6 / W4 "the hub for all customization")*
   There is no consolidated Windows/macOS-style Settings page. `shell_manifest.py`
   has no settings-index entry; `SYSTEM_PANELS` are individual panels reachable
   only via the Start menu, plus separate Nunba appearance / privacy / backup
   iframes (`admin_settings:209` is a Nunba `/admin/settings` iframe). A grep for
   a settings hub in `liquid_ui_service.py` returns none. No single surface
   organizes devices / display / personalization / system. (Task #118 still
   pending.)
   **To close:** build one Settings hub that groups the existing SYSTEM_PANELS +
   Nunba settings iframes into categories.

### PARTIAL items where the missing half is the user-facing one

8. **App Store is a glyph-card grid, not a Netflix image listing.**
   *(PARTIAL · d4 / W7)* `hartMarketplace.appCard` (`hartMarketplace.js:187`)
   builds a vertical `.hart-app-card` with a Material glyph icon (`app.i`) + an
   Install button, by category. It does NOT reuse the `hh-row`/`hh-card` system:
   real photos, text-over-art and hover-expand are all absent. Functional store,
   wrong design - "App Store as image-card category rows" is a glyph grid.

9. **Omnibox 3-way routing is ask-only - no deterministic file/registry search,
   no semantic media search.** *(PARTIAL · e2 / e3)* The omnibox pill focuses the
   hero command bar (`liquid_ui_service.py:2009` -> `HartHome.ask` ->
   `#hart-hero-input`; Super+K bound at `hartHome.js:844`) and the ask path works
   via `/api/agent/ask` -> brain. But `acSend` (`liquid_ui_service.py:4452-4496`)
   only has a theme fast-path, an `open <app>` fast-path, then default ask. There
   is NO deterministic file/registry search and NO semantic media (image/video by
   caption) routing from the omnibox. Typing "terminal" lists the Terminal app
   only in the separate Start-menu `filterStart`, NOT in the omnibox pill (e3).

10. **Desktop app icons are colored gradient tiles, never real photos.**
    *(PARTIAL · d6)* `hartDesktop.js` renders icons as cards: a real photo plate
    IF the manifest supplies `def.image` (`:86`), else `renderGlyphTile` (`:164`)
    a brand-gradient art tile + colored glyph + label. But NO `shell_manifest.py`
    entry sets an `image` field (grep: zero hits), so the photo plate never
    appears. Icons are colorful (meets the de-monochrome half of d6) but the
    "real image-rich representations / colorful like macOS-Windows photos" half is
    absent - the hook exists with no producer.

> Cross-cutting note: gaps 1, 3, 5, 8, 10 all share one root - **no producer ever
> supplies real imagery or real per-node economic data.** The renderers, ledger
> read, and media-index code are built; what is missing everywhere is the agent /
> indexer / writer that FEEDS them. Closing gap 2 (the LLM composer) + gap 5 (the
> media index wiring) unblocks most of the visual gaps at once.

---

## Totals

**HOME / DESKTOP UI area: 14 features audited - EXISTS 4 · PARTIAL 4 · STUB 2 ·
MISSING 4.** I.e. 10 of 14 intended features are not yet fully working
functionality (4 partial, 2 stub, 4 missing).

---

## HOME / DESKTOP UI - full per-feature table

Every intended feature in this area, with status, where (file:line), and
evidence (for EXISTS) or the exact gap (for PARTIAL / STUB / MISSING).

| Feature (checklist item) | Status | Where | Evidence / exact gap |
|---|---|---|---|
| Agentic home compose TRANSPORT (agent_ui_update -> SSE -> HartHome.compose -> render) | **EXISTS** | `liquid_ui_service.py:641` compose_home, `:658` home_compose component, `:468` agent_ui_update, `:182` A2UI allowlist 'home_compose', `:4813` SSE consumer, `:5305` /api/home/compose; `hartHome.js:811` compose, `:583` render | The governed A2UI channel is wired end to end: compose_home builds a `{hero,rows}` 'home_compose' component, agent_ui_update gates it (kill-switch / rate-cap / XSS), the SSE branch hands `ev.payload` to `HartHome.compose` -> `render()`. Works as a renderer. |
| Home is actually COMPOSED by the local LLM (i1 - LLM is the heart) | **MISSING** | `liquid_ui_service.py:641` compose_home (only callers: `/api/home/compose` route `:5311` + `tests/unit/test_home_compose_feed.py`); `hartHome.js:84` samplePayload, `:750` refresh | No autonomous agent / local-LLM ever calls compose_home. Only producers are the external POST route + unit tests. The home is hardcoded `samplePayload()` upgraded by direct endpoint fetches, NOT an LLM composition. Transport present, zero producer. |
| Earnings hero wired to the REAL earnings ledger (d1 / d1.1 real money via the hive) | **PARTIAL** | `hartHome.js:603` fetchEarnings (-> `/api/social/resonance/wallet` then `/api/compute/earnings/<uid>`), `:631` payout_pending hardcoded true; `integrations/social/api_compute_earnings.py:107` list_earnings (reads ResonanceTransaction), `:99-104` source comment | Reads a REAL ledger (ResonanceTransaction rows). But the ONLY earning writer is `api_cost_recovery` (metered-API settle), NOT peer-compute hive work; the `compute:%` per-inference writer does not exist yet. `payout_pending` hardcoded true, no payout rail. Fresh node `total_spark_in_window=0` -> falls back to the ~2,140-Spark sample. Real READ, stub economics. |
| Image-rich cards with REAL photos (b2 / d3) | **STUB** | `hartHome.js:194` card.image lazy `<img>`, `:160` gradientArt fallback, `:84` samplePayload (no image fields), `:674` fetchAgents / `:704` fetchRecipes (never set image) | Renderer supports real photos (data-src + IntersectionObserver), but NO payload sets an `image` field anywhere - samplePayload has none, live fetchers never populate it. Every card paints `gradientArt()` (a brand-gradient tile), never a photo. Image-rich intent = gradient placeholders in practice. |
| Dynamic image cache + semantic media index feeds the cards (W10 / d7) | **MISSING** | `media_semantic_index.py:391` MediaSemanticIndex, `:768` ImageCache, `:730` register_idle_indexer, `:937` register_media_routes (NO non-test caller); `:940-942` header "shell wiring is a follow-up" | Whole module coded + unit-tested but COMPLETELY UNWIRED: register_media_routes never called outside tests (`/api/media/search`, `/api/media/image` not mounted), register_idle_indexer never started (catalog never populated), `hartHome.js` never calls either endpoint (grep static/: 0 hits). The pipeline does not exist at runtime. |
| Netflix listings on the App Store (W7 / d4) | **PARTIAL** | `hartMarketplace.js:187` appCard (`.hart-app-card` vertical card, glyph `app.i`, Install button), `:16` CATALOG, `:40` CATS; loaded via `liquid_ui_service.py:3914` loadAppStorePanel | App Store is a card GRID by category but uses Material glyph icons + Install buttons, NOT Netflix image-card horizontal rows with real photos / text-over-art / hover-expand. Does not reuse hh-row/hh-card (those exist only in hartHome.js). Functional store, wrong design. |
| Netflix listings on agents / recipes / communities / settings / file-explorer (W7 / d4 "everywhere") | **MISSING** | `shell_manifest.py:61` agents_browse, `:68` communities, recipes etc. (route -> Nunba iframe); `liquid_ui_service.py:2835` renderRoutePanel (iframe to `NUNBA_BASE`+route), `:2219` NUNBA_BASE='' | These surfaces open as iframe panels to plain Nunba SPA routes, NOT image-card category rows with hover-expand. The Netflix card system (hh-row/hh-card) exists ONLY on Home. Install/registry/uninstall is a glyph grid too. "Listings everywhere" unimplemented for every non-home surface. |
| Top bar (e1: brand \| nav tabs \| omnibox \| orb-sm \| avatar) | **EXISTS** | `liquid_ui_service.py:1996-2029` (.top-bar: start-btn brand, nav Home/Agents/Apps/Hive/Earn, `#agent-status`, omnibox pill `:2009`, `#top-bar-orb` `:2014`, `#top-bar-avatar` `:2015`, tray); `hartHome.js:778` NAV_MAP, `:794` navTo | Single restructured top bar present: brand, five active-state nav tabs wired to HartHomeNav/openPanel, "Ask or search anything" omnibox pill, an orb-sm slot, avatar, tray. Matches the e1 layout. (orb-sm content + omnibox routing are separate gaps.) |
| Top-bar small breathing orb (orb-sm, c7) | **STUB** | `liquid_ui_service.py:2014` `<button id=top-bar-orb onclick=toggleVoice>`; `hartHero.js` (NO reference to top-bar-orb or orb-sm) | `#top-bar-orb` is a static empty `<button>` that only calls toggleVoice(). hartHero.js (owner of the breathing voice-viz orb + compact/dock states) never references it, so the orb never renders in it and never compacts into it. A CSS-styled placeholder dot, not the always-accessible small breathing orb. |
| Omnibox 3-way routing (e2: deterministic apps/files \| semantic media \| ask) | **PARTIAL** | `liquid_ui_service.py:2009` omnibox pill -> HartHome.ask; `hartHome.js:328` ask (focuses `#hart-hero-input`), `:844` Super+K; acSend `liquid_ui_service.py:4452-4496` | Omnibox focuses the hero command bar (reuse, no fork) + Super+K bound - the 'ask' path works via `/api/agent/ask` -> brain. But 3-way routing NOT implemented: acSend has only a theme fast-path, an `open <app>` fast-path, then default ask. NO deterministic file/registry search, NO semantic media routing. e3 "type terminal lists the Terminal app" works only in Start-menu filterStart, not the omnibox. |
| Windows-style Start menu (e5 / W4) | **EXISTS** | `liquid_ui_service.py:2641` buildStartMenu (groups MANIFEST + SYSTEM_PANELS), `:2677` filterStart search, `:2686` startSearchEnter (launch first hit), `:2115-2125` markup + power footer (lock/sleep/restart/UEFI/shutdown) | A real Start menu opens from the brand button: grouped app grid, live search filter, Enter launches first hit, pinned/power footer. Functional. Gap vs W7: it is a Material-glyph grid not an image-card listing - but as a Windows-style Start menu (e5) it is implemented and works. |
| Unified Settings page hub (e6 / W4 "the hub for all customization") | **MISSING** | `shell_manifest.py` (no settings-index entry; SYSTEM_PANELS are individual; `:209` admin_settings is a Nunba `/admin/settings` iframe); `liquid_ui_service.py` (no settings hub) | No consolidated Windows/macOS-style Settings page. Customization is scattered across ~40 individual SYSTEM_PANELS reachable only via the Start menu, plus separate Nunba appearance/privacy/backup iframes. No single surface organizing devices/display/personalization/system. Task #118 pending. |
| Desktop app icons as image-rich cards paired with name (d6) | **PARTIAL** | `hartDesktop.js:86` def.image -> `<img>` .di-image plate, `:164` renderGlyphTile (gradient art tile + glyph), `:353` makeIcon; `shell_manifest.py` (manifest entries carry only glyph `icon`, NO `image`); `:774` with_icon_colors | Icons render as cards: a real photo plate IF the manifest supplies `def.image`, else a colored brand-gradient art tile + glyph + label. But NO manifest entry sets `image`, so photo icons never appear - icons are gradient art tiles with glyphs. Colorful (de-monochrome half of d6) but the "real image-rich / colorful like macOS-Windows photos" half is absent. Hook exists, no producer. |
| Panels open / float / maximize (windowing) | **EXISTS** | `liquid_ui_service.py:2694` openPanel (floating glass panel, cascade, potato cap), `:2738` titlebar min/max/close, `:2739` dblclick toggleMax, `:2835` renderRoutePanel (Nunba iframe), `:2994` loadSystemPanel (native), `:3914` loadAppStorePanel | Panels open as draggable / resizable floating glass windows with minimize/maximize (dblclick + control); route panels load Nunba via iframe, system panels render natively. Works. (The CONTENT of route panels being plain Nunba iframes rather than Netflix listings is the separate W7 gap above.) |

---

## Notes for the next auditor

- This audit covers the **HOME / DESKTOP UI** area (checklist groups a / b / c /
  d / e / i and the W1 / W7 / W10 workstreams that touch it). Other checklist
  groups - (c) the orb voice-viz / switchable skins regression `[#1791]`, (f)
  interaction / drag / context-menu, (g) the sensory cluster, (h) "Light your
  HART" onboarding, (j) customization-as-API + system management, (k8) the OS
  micro-detail sound layer - are NOT yet folded into this doc. Add them as
  further areas (one `## AREA` block + table each) using the same status
  discipline. The W-series tracker in `../../HART_OS_FULL_DESKTOP_SPEC.md` lists
  the still-pending workstreams (W2-W11, harness tasks #116-125).
- Verification method for this pass: every EXISTS row was confirmed by reading
  the cited file:line; every MISSING/STUB row was confirmed by a no-producer
  grep (e.g. `compose_home`, `register_media_routes`, `top-bar-orb`, the
  manifest `image` field - all returned only the route/definition + tests, never
  a real caller). "A route exists" was never accepted as "it works".
