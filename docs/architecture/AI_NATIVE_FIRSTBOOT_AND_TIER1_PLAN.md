# AI-native OS: first-boot LLM, agents, casual chat, Tier-1 100x — grounded plan

> Maps onto `AI_NATIVE_OS_VISION.md`'s proof ledger: the local LLM up on first boot is the
> SUBSTRATE the whole vision rests on (P1 "the LLM is the heart"); the AutoGen/langchain
> agents are P1/P4 (composable agents); cross-OS install is P6 (One Fabric, 🟡
> runtime-gated). Same discipline: a pillar is real only when its PROBE is green AND
> load-bearing — "the agent stack is packaged" must be proven by a behavioural /chat +
> recipe-write round-trip on the booted node, not asserted. Do not relabel a 🟡 as ✅.


Status: 2026-07-11. Synthesis of a 4-agent read-only map (first-boot LLM, casual
chat + AutoGen, Tier-1 animation surface, Nunba native-daemon provisions). The
steward's directive frames it: **"Nunba natively packed for HART OS shd do these"** —
i.e. these capabilities already exist (Nunba + HARTOS); the job is to WIRE/SERVE them
natively, never reimplement. Nothing here reinvents; every item extends a canonical home.

## What already EXISTS (do not rebuild)

| Ask | Exists? | Canonical home |
|---|---|---|
| First-boot model download | ✅ | `nixos/modules/hart-llm.nix:136-229` `hart-llm-provision` (network-online-gated GGUF fetch); llama.cpp binary baked in closure `:18`. Nunba twin: `llama/llama_installer.py:1301 install_on_first_run`, `main.py:1921 /api/llm/auto-setup` |
| LLM launch/supervision | ✅ | `hart-llm.service` (`hart-llm.nix:231-260`); model bus `model_bus_service.py`; watchdog `model_lifecycle.py` (Nunba path) |
| Casual chat (draft-first) | ✅ works on OS | `hart_intelligence_entry.py:8785-8940` via `speculative_dispatcher` (no langchain/autogen needed) |
| Casual chat (full LangChain `get_ans`) | ⛔ not on OS | `hart_intelligence_entry.py:7075` — needs langchain in the env |
| AutoGen CREATE/REUSE agents | ⛔ not on OS | engine = HARTOS `create_recipe.py`/`reuse_recipe.py`; Nunba is a proxy client `routes/hartos_backend_adapter.py:53` |
| Rich animated AI-native UI | ✅ in Nunba | `landing-page/src/components/HART/LightYourHART.js` (18 langs, 13-phase), `VoiceVisualizer.jsx`, `HARTSpeechPlayer.js` (lottie-react, react-spring) |
| GPU-accelerated Tier-1 compositor | ✅ code, ⛔ engaged | `compositor/src/udev.rs:505-547` GLES arm, gated on `/run/hart/gpu-render`; `RepaintScheduler` #137 `main.rs:456-524` |
| Nunba as native OS daemon | 🟡 A+D done, B/C/F written | `NUNBA_NATIVE_DAEMON_PLAN.md`; `nixos/packages/nunba.nix`, `nixos/modules/hart-nunba.nix`, `desktop.nix:113 nunba.enable=false` |

## The real GAPS (what to actually do)

1. **Tier-1 crashes to cage** (BLOCKER for all Tier-1/GPU/animation work). Being
   diagnosed via the bdd29ba3 journald-routing fix → next boot names the hart-comp/sway
   crash. Suspects: `nvidia-drm.modeset=1` no-driver; leftover `simpledrm card0` vs i915.
   Nothing "100x" is testable until Tier-1 boots.

2. **Agent engine not packaged** — `nixos/packages/hart-app.nix` OMITS `langchain` (`:60`)
   and `autogen` (`:81`, source-filtered `:103`), so on the OS the FULL langchain chat +
   ALL AutoGen CREATE/REUSE fail at call-time (`_LANGCHAIN_OK=False`; `ImportError
   pyautogen`). This is a PACKAGING fix on the :6777 backend env, NOT the Nunba daemon
   (Nunba correctly proxies agents to :6777 and drops the libs). Standing TODO at
   `hart-app.nix:29-31`.

3. **`hart-gpu-scheduler` boot timeout** — `hart-ai-runtime.nix:253-321`: `Type=notify` +
   `User=hart` + `systemd-notify --ready` from a NON-main child under default
   `NotifyAccess=main` → READY ignored → 90s timeout → `Restart=on-failure` loop (matches
   the journal). Fix = `NotifyAccess="all"` (or in-process notify / `Type=simple`).
   ⚠️ hart-ai-runtime.nix may be the concurrent inference session's territory — COORDINATE
   before editing.

4. **First-boot LLM bring-up gaps**: (a) no same-boot re-trigger — a late model download
   doesn't `systemctl start hart-llm.service` (legacy `hart-first-boot.sh:175` did); (b) no
   user-facing first-connection network/model wizard (today headless `network-online.target`);
   (c) daemon-mode first-boot model-download trigger under-specified (Nunba's trigger is
   GUI-bound `app.py:2557`; the daemon runs `main.py`; HARTOS `model_onboarding.py` should
   drive it server-side — confirm in the CI boot-loop).

5. **Nunba daemon not built/enabled** — B/C/F blocker is pure CI: pin `nunbaHash` +
   `npmDepsHash` for `nunbaRev cb849ba9` in ONE commit (`nunba.nix:9-15`), green
   `nix build .#packages.x86_64-linux.nunba` by walking the import-domino loop, flip
   `desktop.nix:113 nunba.enable=true`. Then it SERVES LightYourHART + the rich UI natively.
   E (retire hartOnboarding.js/native_onboarding.py forks) deferred until real-HW proof.

## Sequenced plan (dependency order)

**P0 — Tier-1 boots (the gate).** Diagnose the hart-comp/sway crash from the bdd29ba3
journal (task #173) → fix the real root cause → re-flash → verify Tier-1 comes up. Until
this, GPU compositor + animations are untestable.

**P1 — Agents + chat actually run on the OS (packaging, parallel to P0).** Add
`langchain_classic` + `autogen` to `hart-app.nix` pythonEnv (the :6777 backend). Behavioural
verify on the built node: env import, /chat draft-first (works today), full get_ans langchain,
AutoGen CREATE (recipe file written), REUSE. Probe plan in the casual-chat map.

**P2 — First-boot LLM up + wizard.** Fix `hart-gpu-scheduler` NotifyAccess (coordinate);
add the same-boot re-trigger (provisioner completes → start hart-llm.service + re-probe bus);
surface a first-connection network+model-download wizard via Nunba's LightYourHART (served by
the daemon) wired to HARTOS `model_onboarding.py`. Reuse `hart-llm-provision` + Nunba
`/api/llm/auto-setup`; no parallel downloader.

**P3 — Nunba daemon serves the rich UI (CI).** Finish B/C/F (pin hashes → green build → flip
enable) so LightYourHART + VoiceVisualizer + the animated shell are the native surface; retire
the HARTOS onboarding fork (E) once proven on real HW.

**P4 — Tier-1 100x (AFTER P0).** Engage the GPU compositor (GLES already coded); the allowed
animation set, checklist-bound (`HOME_DESKTOP_DESIGN_CHECKLIST.md`): agentic Liquid-UI
micro-anims + orb speech (b8/i1), icon-hover + living wallpaper (d5/k8), app-open/switch
crossfades (b5), tighter idle-RAF (i3). FORBIDDEN: solid orb ring (c1), mic-in-orb (c5),
page-scroll (a1/a2), mono palette (b1), em dashes (b6), and NEVER flip the cairo WebView shell
to GPU (the 500ms-lag trap, `hart-layer-shell-host.nix:292-296`).

## Discipline
Every P-item = a tracked task + verification (behavioural test + real-HW where observable),
zero regression, zero parallel path. Respect the concurrent inference session (route to
model_bus/hart-llm/llama-server, never reimplement). Tier-1 UI/animation is BINDING on the
design checklist.
