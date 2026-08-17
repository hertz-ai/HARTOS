# HARTOS Parallel-Path Audit (whole-codebase, 2026-07-01)

> ## ⚠ RE-VERIFIED 2026-08-17 — **9 of the 10 HIGH findings are FIXED**
>
> This audit was 6 weeks old and still listed nearly everything as open. A stale
> audit is worse than no audit: it sends the next reader to re-fix work that is
> already done, and it hides the one thing that is genuinely still broken.
>
> Every HIGH item below was re-checked against `HEAD` mechanically. Result:
>
> | # | finding | status 2026-08-17 | the check that proves it |
> |---|---|---|---|
> | 1 | theme-apply dual handler, palette dead | **FIXED** | `/api/social/theme/apply` no longer exists in `integrations/` |
> | 2 | raw `/api/shell/launch` bypasses `launch_app` | **FIXED** | route delegates to `get_app_bridge().launch_app`, checks `ok`, returns a real 500 (the #133 anti-pattern is named in the code) |
> | 3 | shell-auth 4 copies, `0.0.0.0` trusted | **FIXED** | canonical `integrations/agent_engine/shell_auth.py`; **0** hits for a trusted `'0.0.0.0'` across `shell_*.py` |
> | 4 | consent written by two drifted writers | **FIXED** | `consent_api.py` carries 8 refs to `ConsentService` |
> | 5 | secret-redaction lists 3×, live key leak | **FIXED** | `audit_log.py` delegates to `secret_redactor` |
> | 6 | Ed25519 canonical-JSON copied 9× | **FIXED** | one `canonical_payload()` in `node_integrity.py`, used across 7 security files |
> | 7 | `get_node_identity()` name collision | **FIXED** | `hart_onboarding.py` no longer defines it |
> | 8 | **TaskStatus transition map duplicated** | **STILL OPEN** | `STATE_MAP` defined **twice** in `lifecycle_hooks.py` (`:134` and `:1598`), each with its own consumer (`:157`, `:1618`). The audit called the second a dead copy; it is not obviously dead. |
> | 9 | power/session route UNAUTHENTICATED | **FIXED** | the session route now carries `@_require_shell_auth`; the code comments the drift it closed |
> | 10 | 2 inner-chat payload builders + 2 LLM wrappers | **FIXED** | one consolidated builder in `speculative_dispatcher.py` |
>
> Most of these were closed by `06dc5528` (the canonical-shell-auth / canonical-
> redactor / canonical-payload sweep) and its follow-ups. **#8 is the only HIGH
> survivor and is therefore the top of the queue.**
>
> **Two HIGH-class findings this audit did not have, both verified since:**
>
> - **No canonical `/chat` client.** 9 of 11 callers omitted `request_id`, so
>   `dispatch.is_genuine_user_request` classified real people — D-Bus users, the
>   CLI, the desktop intent bar, and every Discord/Telegram/WhatsApp user — as
>   background daemon work: never preempted for, run on the *closable* client,
>   abortable mid-flight. Fixed by `core/chat_client.py` (`ff7f536b`); the
>   remaining callers are a shrink-only ratchet in
>   `tests/unit/test_source_guard_chat_callers_use_canonical_client.py`. That
>   guard found **4 callers a manual grep had missed**, so the real population was
>   ~15, not 11 — which is why this class needs a mechanical detector, not eyes.
> - **`app_installer.py:1831-1835` fails OPEN.** If importing `shell_os_apis`
>   fails, `_require_shell_auth` becomes an identity decorator and the app-INSTALL
>   routes lose authentication entirely. Documented as a deliberate non-shell-node
>   fallback, so it is a product decision — but fail-open inverts the safe default
>   on a privileged surface. Cheap fix: import from `shell_auth` directly (no shell
>   dependencies), making the cited failure mode unreachable.
>
> **Method note for whoever refreshes this next:** re-verify before reading. The
> standing rule (`feedback_check_review_queue_regularly`) exists because *one of
> eight* items was already fixed last time. Here it was *nine of ten*.


5-agent codebase-wide audit for parallel paths (2+ implementations of ONE concept that
drift). Triggered by the steward catching reengineering (voice `voice_turn` vs the
existing `handle_voice_input`). **~45 genuine duplicates found; ~10 HIGH.** Line numbers
verified by the auditors.

**Key framing:** the RECENT session introduced FEW net-new parallel paths (voice already
reverted; #117 apps.launch is the *better* path but coexists with the old one; the #161
palette is *shadowed* by an OLDER dup). The SEVERE findings are **long-standing debt**
(security/crypto, auth, consent, dispatch) that this audit surfaced.

**Calibration suspects — all FIXED:** the 4× non-Latin frozenset (→ `core.constants`), the
3× thread-dump (→ `core.diag`), the 2× `_ensure_mcp_token`, the multi-writer
`hart_language.json` (→ `core.user_lang`). The federation sync `if/elif` ladder is gone
(→ `SyncEngine.queue_entity`); `oauth2.py` no longer exists. Navigation (`openPanel`+
`HartNavCore`, #169), `buildStartMenu` (#118), `showToast`, `whisper_transcribe` (STT),
`pooled_post`/`_patched_send` dual-transport, `message_bus`, fleet-command signing — all
CLEAN.

---
## MECHANICAL BASELINE (2026-08-17) — exhaustive, not eyeballed

The 2026-07-01 pass was 5 agents reading code. Reading misses things at this scale
by construction, so this baseline is an **AST walk over every file** — 837 source +
916 test, 0 unparseable. A number here is a fact; re-run the detector to refute it.

| detector | count | honest reading |
|---|---|---|
| public name DEFINED in >1 module | **122** | some are legitimate *interfaces* (every channel adapter defines `search`/`post`/`timeline` — that is polymorphism, not drift). The real finding is the cluster below. |
| public def referenced nowhere | 817 | **INFLATED — do not quote.** `social/api.py` has 154 route decorators against 160 defs, so ~96% are Flask/click handlers referenced by decorator, not by name. A trustworthy count needs decorator-awareness. |
| SOURCE modules > 3000 lines | **9** | 11 if you count `test_nixos_configs.py` and `test_agent_engine.py`, whose size mirrors the sprawl they cover |
| `except: pass` | **1533 across 450 files** | against an explicit house rule banning silent swallows |

### THE WORST THING IN THE REPO: the recipe pipeline is one concept in four files

`hart_intelligence_entry.py` (12,305) + `create_recipe.py` (6,096) +
`reuse_recipe.py` (3,968) + `helper.py` (3,210) = **25,579 lines** carrying the
CREATE/REUSE pipeline, and they share copy-pasted helpers by the same name:

| helper | defined in |
|---|---|
| `get_frame` | **all four** |
| `parse_date` | **all four** |
| `get_llm_config` | create_recipe, reuse_recipe, helper |
| `save_conversation_db` | create_recipe, reuse_recipe, helper |
| `publish_async` | create_recipe, reuse_recipe, hart_intelligence_entry |
| `get_action_user_details` | create_recipe, reuse_recipe, hart_intelligence_entry |
| `subscribe_and_return` | create_recipe, reuse_recipe, crossbar_server |

This is not a style problem. It is the **Recipe Pattern** — the product's central
claim — implemented four times, so every fix must be made four times or the paths
drift. It is also why `hart_intelligence_entry.py` is 12k lines and why the
langchain import chain costs 169s: there is no seam to import across.

### The same finding through each lens

| lens | what it sees |
|---|---|
| **Architect** | no module boundary around the core domain; four files that must all change together are, by definition, one module badly split |
| **Developer** | a one-line fix is a four-line fix in four files, and the compiler cannot tell you when you missed one |
| **Tester** | 3,578-line `test_agent_engine.py` mirrors the sprawl; you cannot unit-test `get_frame` because there are four of it |
| **Performance** | 169s meta-import (`perf_gil_autogen_ssl_context`); no seam means everything imports everything |
| **Product / Owner** | every feature touching CREATE or REUSE costs ~4× and carries a drift risk that shows up as "works in create, broken in reuse" |
| **Business / Investor** | the differentiator (learn-once, replay-cheap) is the least maintainable code in the tree; onboarding a contributor to it is a multi-week ramp |
| **Marketer / Sales** | "REUSE is 90% cheaper" is true and demonstrable — but a REUSE-only bug class (e.g. the `KeyError 'actions'` crash) reads to a customer as the flagship feature being unreliable |
| **Tester (again)** | 1,533 `except: pass` mean failures are invisible; the flywheel can be broken for weeks and every dashboard stays green — which is exactly what happened |

---

## REFACTOR GRAPH — dependency-ordered, zero regression

Each phase is gated: **no phase starts until the previous phase's gate is green.**
Nothing here is a rewrite; every step moves existing code behind an existing seam.

```
P0  BASELINE (do first, blocks everything)
    ├─ pin the detector as a CI check (counts may only go DOWN)
    └─ characterisation tests around CREATE + REUSE at the HTTP boundary
       GATE: a recipe banks and replays identically before and after any move
                    │
P1  EXTRACT THE SHARED HELPERS  ← the 7 verbatim duplicates above
    ├─ one home each in core/ (integrations→core allowed; core→integrations BANNED)
    ├─ the four files import it; delete the copies
    └─ source guard: the name may be defined ONCE
       GATE: P0 characterisation still green; import time measured, not assumed
                    │
P2  SILENCE → SIGNAL                     (parallel with P1, no shared files)
    ├─ 1,533 `except: pass` — triage: log, or delete the try, or narrow the except
    └─ start with the 12 worst files; ratchet the count down in CI
       GATE: no NEW bare swallow can merge
                    │
P3  ONE /chat CLIENT (started — ff7f536b)
    └─ migrate the 14 remaining callers off the shrink-only allowlist
       GATE: allowlist empty; a user turn preempts a daemon under flywheel load
                    │
P4  SPLIT hart_intelligence_entry.py (12k)  ← only now, once P1 gave it seams
    └─ routes / pipeline / transport, by existing boundaries, not new ones
       GATE: route table identical before and after (diff the URL map)
                    │
P5  HIGH-8: one TaskStatus transition map
    └─ the last surviving HIGH from the 2026-07-01 audit
```

**Why this order.** P1 before P4: splitting a 12k file whose helpers are duplicated
just moves the duplication. P0 before P1: without characterisation tests, "zero
regression" is a hope. P2 alongside P1: different files, no merge contention, and
it is the cheapest way to make the other phases *observable* — you cannot verify a
refactor whose failures are swallowed.

**What NOT to do**, per the standing rules: no new abstraction where one exists
(`core.file_cache.atomic_json_write`, `core.platform_paths`, `core.constants`,
`core.subprocess_safe.run_probe`, `core.chat_client` all already exist and are
under-used); no rewrite of the Recipe Pattern itself; no second registry.

---

## RECENT (this session / ~2 weeks) — what we actually introduced or exposed
- **Voice** — `model_bus.voice_turn` duplicated `handle_voice_input`. **REVERTED (b93d355c).**
- **App-launch** — #117 added the typed `os_bridge.apps.launch` (BETTER); it coexists with
  the long-standing raw `POST /api/shell/launch` (`liquid_ui_service.py:6191`, direct
  `Popen(gtk-launch)`, fire-and-forget, weaker id validator, no audit) → see HIGH-2.
- **Theme/palette (#161)** — `api_theme.apply_theme` (ThemeService + palette overlay) is
  **shadowed dead** by the older `social_bp.theme_apply` on the SAME URL → see HIGH-1.
That's it — the rest below is pre-existing.

---
## HIGH (fix first — security / correctness / live bugs)
1. **Theme-apply: two handlers on `/api/social/theme/apply`; the palette picker is DEAD.**
   `social/api.py:3910 theme_apply` (registered first → wins, per-user DB, drops
   `secondary_accent`/`custom`) shadows `social/api_theme.py:39 apply_theme` (ThemeService
   + #161 palette + GTK apply). → collapse to the ThemeService-backed handler. *(RECENT bug.)*
2. **App-launch: raw `/api/shell/launch` bypasses the result-checked `app_bridge.launch_app`.**
   3 surfaces: `liquid_ui_service.py:6191` (raw Popen) vs `app_bridge_service.py:604`
   (canonical, multi-subsystem, result-checked) vs `os_bridge/apps.py:34` (typed). Masks
   failure like the #133 anti-pattern; frontend `launchApp('xdg-open',path)` silently drops
   the path. → `/api/shell/launch` delegates to `launch_app`.
3. **Shell auth decorator: 4 copies, DIVERGENT allowlists.** `_require_shell_auth`
   (shell_os_apis:98, allows `0.0.0.0`) vs `_require_system_auth` (shell_system_apis:413,
   no `0.0.0.0`) vs `_require_desktop_auth` (shell_desktop_apis:424). `app_installer:1778`
   correctly imports the one. → the other two import it; delete copies. *(authz gate drift.)*
4. **Consent grant/revoke: two writers drifted.** `ConsentService.grant_consent`
   (consent_service.py:143 — row + immutable audit + WAMP/SSE emit + type-validation +
   `public_exposure`→agent up-sync) vs the UI's actual path `consent_api.py:118` (inline
   `UserConsent` row ONLY). UI consent leaves no audit trail + no up-sync. → `consent_api`
   calls `ConsentService`. *(compliance/security.)*
5. **Secret-redaction pattern lists duplicated 3×, already drifting → LIVE key leak.**
   `audit_log.py:12` (Google key `AIzaSy…{33}`) ≠ `secret_redactor.py:79` (`AIza…{35}`) ≠
   `dlp_engine.py:25`. → import `secret_redactor` patterns everywhere.
6. **Ed25519 signature trust boundary copy-pasted.** canonical-JSON payload builder 9×
   (5 different exclude-field names) + verify primitive inline in 6 security modules
   (master_key, key_delegation, origin_attestation, pre_trust_contract, native_hive_loader)
   vs `node_integrity.verify_signature`. → one `canonical_payload()` + all call
   `verify_signature`. *(any drift = signatures silently fail network-wide.)*
7. **`get_node_identity()` name collision** — crypto identity (`node_integrity.py:286`) vs
   onboarding identity (`hart_onboarding.py:539`), both return a dict with `tier`. → rename
   the onboarding one.
8. **TaskStatus transitions duplicated** — `agent_ledger/core.py:623` inline vs
   `graph.py:247 TaskStateMachine.TRANSITIONS` (a test is the only thing keeping them
   aligned); + `lifecycle_hooks.py:134 STATE_MAP` live vs `:1432` dead copy. → one map.
9. **Power routes: 3 surfaces + the session route is UNAUTHENTICATED.**
   `/api/shell/session/<action>` (liquid_ui_service:6207, **no auth**, verb `restart`) vs
   `/api/shell/power/action` (shell_os_apis:959, authed, `reboot`) vs `os_bridge.power
   .invoke_power` (typed). Method map unified (#165); routes/verbs/auth/firmware-two-step
   still 3×. → thin adapters over `invoke_power` + auth the session route. *(security smell.)*
10. **AI dispatch: 2 verbatim inner-chat payload builders** — `speculative_dispatcher.py:1815
    _dispatch_to_model` vs `:1663 _dispatch_expert_langchain` (payload char-identical). +
    **2 LangChain local-llama wrappers** `ChatQwen3VL` (hie:574) vs `CustomGPT` (hie:5441),
    both live, already drifted (refusal-check on one only). → one payload builder + one LLM class.

---
## MED (breadth / acknowledged drift)
- **Atomic-JSON-write reimplemented ~18×** vs `core.file_cache.atomic_json_write` (some
  copies lack fsync/tmp-cleanup — `create_recipe.py:5744`). 
- **`~/Documents/Nunba/data` path inlined 48× / 38 files** vs `core.platform_paths` (one
  fallback already drifted, missing `data/`).
- **`HEVOLVE_NODE_TIER` read raw 12×** bypassing `key_delegation.get_node_tier()`'s
  master-key auto-promotion → central-gating drift.
- **TTS orchestration split** — `TTSRouter.synthesize` (canonical, used by model_bus/
  /api/voice/speak) vs the chat-reply path `hie:8112 _tts_synthesize_and_publish`→
  Nunba `tts_engine`; + duplicate TTS engine *selection* (`TTSRouter.select_engines` has an
  inline "remove me, use catalog.select_best" TODO); + model_bus `_try_luxtts/_try_pocket_tts`
  duplicate `TTSRouter._call_*`.
- **Local-llama transport bypass** — `speculative_dispatcher.py:1890` raw `requests.post`
  to the draft port, bypassing `pooled_post`/`llama_scheduler` → reintroduces the #162
  "user can't preempt" bug for the draft path. → route through `pooled_post`.
- **Notifications span 3 stores** (agent_components SSE / `_notification_queue` memory /
  DB `NotificationService`) — visibility depends on which created it. → one NotificationService.
- **Context menu = 3 renderers** (inline `#ctx-menu`/`ctxItem`, `HartCtxMenu`, hartFiles
  `showCtx`) with 3 item contracts (#122). → HartCtxMenu.
- **Audio volume 2 paths** (`/audio/*` pactl per-sink vs `/volume` wpctl `_vol_run`).
- `_run` bounded-subprocess dup (system==desktop byte-identical) + inline `subprocess.run`
  sites vs `core.subprocess_safe.run_bounded`; `_run_async_bounded` vs logind `_bounded`.
- Terminal-status defined 3× and disagree (`is_terminal_state` vs `_TERMINAL_TASK_STATUSES`
  vs `_TERMINAL_LEDGER_STATUSES`). Multiple SmartLedger registries (multi-writer of
  `ledger_*.json`). `MASTER_PUBLIC_KEY_HEX` literal 3×. master-key-holder check copy-paste.
  `verify_guardrail_integrity` name collision (2 different functions). recipe-path builder
  `safe_prompt_path` bypassed by inline f-strings (skips traversal guard) + no canonical
  parser. 4th non-Latin set (`agent_personality._NON_LATIN_SCRIPTS`, missing ur/fa/km).
  port-probe reimplemented 6×. display control split 2 modules. connectivity prober
  re-implements wifi/bt/battery probes (in-code TODO). 3 VLM image-describe paths.

## LOW
JS micro-utils (`esc/getJSON/postJSON/sig`) redefined 6+ modules · audit-log wrapper 3× ·
open-with 3× · wallpaper client-apply 2 flows · `profile_sync.py` clone of `fcm_sync.py` ·
canary broadcast hand-rolls the gossip POST · `on_notification` double-SSE · `datetime
.utcnow().isoformat()` inlined 228× (no `now_iso()`) · env-bool parsing inlined ~66×.

---
## Recommended fix order (by risk × blast-radius)
**Security first:** #5 (redaction leak) → #6 (signature substrate) → #3 (auth allowlists) →
#4 (consent audit) → #9 (unauthenticated power route). **Live bugs:** #1 (palette dead) →
#2 (launch masks failure). **Correctness:** #7 (identity collision) → #8/#10 (dup maps/
dispatch). **Breadth (mechanical, high count):** atomic-write, data-dir, node-tier.
Each fix = collapse onto the named canonical home + a behavioural test; no new abstractions.
