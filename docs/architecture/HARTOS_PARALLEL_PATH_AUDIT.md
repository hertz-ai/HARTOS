# HARTOS Parallel-Path Audit (whole-codebase, 2026-07-01)

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
