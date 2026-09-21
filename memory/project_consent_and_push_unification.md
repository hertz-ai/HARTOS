# Consent + push unification — execution plan

Owner directive 2026-09-21: close these **minimalistically (least blast radius)**,
**wholly** (never half-done — half done is worse), **without regression**, and
**without introducing another parallel path**. Do a **caller audit before folding
each mechanism**. Do not start another broad audit — the inventory below is the
audit. Own and execute end to end.

Status legend: `TODO` / `AUDITED` / `LANDED` / `VERIFIED` (live) / `HELD (who)`.
One fold in flight at a time. Update this file as each fold moves.

---

## THE SEVEN RULES (apply to every fold — this is the part that prevents regress)

1. **Caller audit FIRST.** Enumerate every reader and every writer of the parallel
   store with `file:line`, into that fold's section below, before editing anything.
   Scoped greps only (`git ls-files | grep`, then per-file) — broad greps time out.
2. **One canonical store.** After the fold the parallel store is either **deleted**
   or a **derived cache with exactly one writer**. Never two sources of truth.
3. **"Read both" is never an end state.** It is allowed only as a temporary step
   *inside* a fold that finishes with the migration in the same commit series.
   (I violated this with `require_consent`/`requires_consent` — F0 closes it.)
4. **Two tests per fold:** one pinning the old broken behaviour as fixed, one
   asserting the parallel path *cannot* diverge again (source-guard or a
   round-trip through the canonical store).
5. **Live-patch + verify in the embedded runtime** (`python-embed/python.exe`
   against the install). HARTOS `.py` → `python-embed/Lib/site-packages`
   (sys.path 6, beats `lib/` at 7). Nunba modules ship as `.pyc` in `lib/` —
   recompile with the embedded interpreter (magic `cb0d0d0a`). Back up first to
   `scratchpad/canon_wip/live_patch_backup`. Integrity check is OFF in local dev.
6. **One fold per commit.** Local commits only while pushes are TLS-blocked.
   Stage own paths only; shared checkout — `git revert`, **never** `reset`.
7. **A held file is not a finished fold.** If a fold needs a file another session
   holds, coordinate and track to LANDED+TESTED+VERIFIED myself (task #119).
   "Sent" ≠ "landed". Never mark done on a hand-off.

Also binding: **a gate needs BLOCK + ASK + RECOVERY** — never land a gate whose
ask cannot reach a human who can answer.

---

## DESIGN — every fold is an instance of an EXISTING pattern. Reuse, never invent.

The reason twenty parallel stores exist is that each was built as a new
mechanism. So no fold below is allowed to add a mechanism: each one maps onto an
artifact that already ships. Named here so a future pass reuses the same thing.

**D1. Parallel store → derived cache, via the actuator pattern that already exists.**
`consent_service._copilot_switch_from_consent` (`:348-362`) and
`_embodied_feed_from_consent` (`:370-400`) are already "a consent change flips a
downstream store", called from `grant_consent` (`:625`) and
`announce_revocation` (`:797`). **Every Tier-3 fold is another `_x_from_consent`
actuator** plus deleting the parallel *write*. No new sync machinery: the hook
points already exist. What is missing today is only the reverse-read discipline —
the parallel store must stop being *authoritative*, which is a deletion, not an
addition.

**D2. A gate that asks → reuse `check_or_request` (`consent_service.py:502-519`).**
It is the canonical check-then-file-the-ask shape, with the stable `msg_id`
re-emit that makes a polling gate idempotent. Never hand-roll check-then-ask
(that is how `_pending_contacts` and `DeviceRoutingService.request_consent`
happened). Pair with the BLOCK+ASK+RECOVERY rule: the requester must be a human
the gate can name — pass the real owner (`agent_daemon` now passes
`goal.owner_id`, commit `61736aad7`).

**D3. An approval surface → reuse `record_capability_decision` (`:521-572`).**
It is already "the one approval-surface writer" and already re-asserts the
downstream feed. F6's admin toggle routes through it rather than writing
`embodied_ai_config.json` directly.

**D4. A new permission → a new *type*, never a new store.** Add to
`CONSENT_TYPES` (`:32-124`) + `CAPABILITY_CONSENT_TYPES`/`consent_type_for_action`
(`:138-157`) + `CONSENT_ASKS` (`consentAsks.js:61-120`). `_validate_consent_type`
(`:277-281`) then rejects anything unlisted, which is the guardrail that keeps
F4's mic ask inside the framework.

**D5. One name with two vocabularies → the resolver pattern already proven twice.**
`core/wamp_url.py` (canonical reader for `WAMP_URL`, whose docstring is the worked
example) and `core/intelligence_preference.IntelligencePreference.coerce` (`str`
enum, canonical values + legacy aliases, unknown → a known default). **F0 is
exactly this**: one canonical key, legacy spelling accepted only by the coercer,
readers stop branching. Rule: `memory/feedback_one_name_two_vocabularies.md`.

**D6. A cache in front of a slow read → reuse the shape landed in `9f5ab1ca`.**
`LlamaConfig.cached_intelligence_preference()` + atomic `_save_config`
(temp + `os.replace`): read once per process, **exactly one writer invalidates**,
owned beside the field and not in the caller. **F1 is this pattern applied to
localStorage**: the key becomes a projection with one writer (the consent event),
not a second source of truth.

**D7. A schema change → reuse `integrations/social/migrations.py`.** Versioned
`SCHEMA_VERSION` steps, `ALTER TABLE … ADD COLUMN` wrapped in try/except with
`_is_already_exists_error`. For F0's existing-row data migration and for any
future `expires_at`. Do not hand-write DDL elsewhere.

**D8. Telling a human something → one entry, many transports.**
Entry: `NotificationService.create` (`services.py:1137-1158`) → `realtime.on_notification`
(`realtime.py:424-442`) → `MessageBus.publish` fan-out (`message_bus.py:224-333`)
with `TOPIC_MAP` (`:69-119`) and `broadcast_sse_safe` (`events.py:554`), sharing
one `msg_id`. FCM (`core/fcm_sync.py`), FleetCommand + `RELAY_TOPIC`
(`message_bus.py:63-64`), desktop toast, TTS and SMTP are **transports selected by
that entry**, not entry points of their own. Tier 4 is re-parenting, not rewriting.

**D9. A card / an N-option question → reuse A2UI.** `agent_ui_update`
(`liquid_ui_service.py:1244`) with the `COMPONENT_TYPES` registry (`:626-735`);
the `notification` type's `actions` prop already renders one button per action in
`AgentOverlay.jsx:55-115` (`kind: navigate|external` wired). This is how an open
question with options reaches the user **without a new framework** — and F18
either honours or deletes `approval.options`, which is declared but ignored.

**D10. A consent change must reach every surface → reuse `_emit` + the SPA contract.**
`_emit` (`:222-274`) already fans out on EventBus + `on_notification`;
`CONSENT_ANSWER_TYPES` (`consentAsks.js:209`) and `answerCoversAsk` (`:223-233`)
are the SPA side. Any new consent event (e.g. a future `consent.expired`) must go
on this same fan-out **and** gain an `answerCoversAsk` reader, or surfaces diverge
silently — the failure fix-all predicted for a TTL.

**Artifacts NOT to reuse as consent** (they are correctly separate, folding them
would be the mistake): `pre_trust_contract.can_join_hive` (machine attestation,
deliberately human-free), `core/ai_sensing` (a hard kill switch that must work
with the DB down — keep as a second *layer*, not a second *record*),
`tool_allowlist`, `domain_allowlist`, `claw_native` permissions,
`engagement_guardrails` (advisory by charter).

---

## TIER 1 — a withdrawn permission still acts. Highest harm, smallest blast. DO FIRST.

### F0 — consent trigger spelling (close my own "read both") — TODO
Parallel: `require_consent` written at `goal_seeding.py:1745,1863,1892,1948,2009`;
`requires_consent` at `goal_seeding.py:118,514,580`. Sole enforcement
`hive_guardrails.py:1313` (I widened it to read BOTH — deliberate parallel path).
Unrelated same-name keys: plan approval `hart_intelligence_entry.py:10722`;
share-link viewer `api_sharing.py:255,349,372` — **do not touch those**.
Canonical: keep `requires_consent` (on the wire = least blast per owner's rule).
Audit: the 8 producers, the 1 reader, and **existing goal rows** whose
`config_json` already holds the singular (a data migration, or the reader keeps a
deprecation shim with a removal test).
Fold: migrate the 5 singular producers → plural; migrate existing rows; reader
reads ONLY plural. Acceptance: a goal seeded either way is gated; no row loses
its gate. Blast: `goal_seeding.py` + `hive_guardrails.py` + a data migration.

### F1 — voice: revocation does not take effect — TODO ← **START HERE**
Parallel (3 stores): `UserConsent('voice_speech')` vs `nunba_speech_consent`
localStorage vs `tts_enabled` localStorage.
Known sites: `Nunba/…/HART/LightYourHART.js:917-937` (writes both, `.catch(()=>{})`
swallows failure), `:949` (**the gate** reads localStorage only);
`Nunba/landing-page/src/pages/Demopage.js:739-758,774-792,824-840`, `:814-818`
(browser `SpeechSynthesisUtterance` fallback = N19);
`PrivacySettingsPage.jsx:1001` (revoke — does NOT touch localStorage);
`services/consentApi`.
Harm: revoking `voice_speech` on the privacy page leaves the SPA speaking.
Fold (minimal, no new store): localStorage becomes a **projection** of the
canonical consent, not a source. One writer: a subscriber to the existing
`consent.granted` / `consent.revoked` events (`CONSENT_ANSWER_TYPES`) plus a
read-through on app load. The gate keeps reading localStorage (autoplay needs a
synchronous answer) but can no longer disagree. Collapse `tts_enabled` into the
same projection or delete it.
Acceptance: revoke on the privacy page → speech stops **without a reload**; grant
→ resumes. Test: revocation event clears the key; the gate is false after it.
Blast: SPA only, no backend change, no new framework.

### F2 — copilot: env var overrides a revoked consent — TODO
Parallel (3 stores): `UserConsent('copilot_access')`; `hartos-copilot.off` marker
(`claude_code_backend.py:337-386`); `HARTOS_COPILOT_ENABLED` (`:347`, logs
"pinned by").
Readers bypassing consent: `claude_code_backend.py:103` (the `claude -p` spawn),
`:414` (`claude_code_available`), `mcp_http_bridge.py:259-260`. Consent-reading
site: `agent_daemon.py:250-268`. One-way sync:
`consent_service._copilot_switch_from_consent:348-362`.
Harm: a **revoked** row can still run the copilot; a granted row can sit beside a
disabled one.
Fold: one predicate both the spawn and the daemon consult; the marker stays as
the fast/offline cache with consent as authority; the env pin may force OFF but
**must not force ON against a revoked row**.
Acceptance: revoke → spawn refuses even with `HARTOS_COPILOT_ENABLED=1`; grant →
runs. Blast: `claude_code_backend.py` + 1 bridge read + a test.

### F3 — `hive_participation` default True inverts the opt-in — TODO
Parallel: `User.settings['hive_participation']` (default **True**)
`world_model_bridge.py:281-311`, enforced `:1373`. Canonical
`compute_contribute` is fail-closed opt-in.
Fold: read the canonical consent; treat the setting as a legacy override only
when no row exists; drop the default-True. Acceptance: no consent → no
contribution. Blast: one file, 2 sites.

---

## TIER 2 — a concern with no canonical store at all

### F4 — the mic has no consent type — HELD (fix-all holds `consent_service.py`)
`core/ai_sensing.py:27-102` in-process kill switch is the only gate;
`consent_service.py:136-137` says an ask "belongs here" if built.
Fold: add `microphone_capture` to `CONSENT_TYPES` + `CAPABILITY_CONSENT_TYPES` +
`consentAsks.js`; the audio path (`whisper_tool.py:1595`) checks it; `ai_sensing`
stays as the hard cut (legitimate second layer — a kill switch that works with the
DB down is NOT a parallel consent store, but it must not be the *only* record).
Track to landed per #119.

---

## TIER 3 — duplicate stores, no sync, silent divergence

- **F5 cloud egress** — `User.settings['cloud_data_consent']`
  `world_model_bridge.py:248-278` (5-min cache, no sync) vs canonical
  `cloud_egress` (`sync_engine.py:880-1000`). Same file already uses the canonical
  service at `:670-673` — a migration that stopped halfway. Blast: one file.
- **F6 embodied camera/screen config** — admin toggle writes
  `embodied_ai_config.json` with **no row** (`admin/api.py:2195-2226`); consent→config
  is one-way (`consent_service.py:370-400`, which documents the desync at `:558-563`).
  Fold: route the admin toggle through `record_capability_decision`.
- **F7 NodeComputeConfig + `HEVOLVE_*`** (`compute_config.py:16-89`, env > DB >
  defaults). **Distinguish permission from capacity**: fold only the permission-ish
  fields (`allow_metered_for_hive`, `accept_*`) onto consent; leave capacity
  numbers (`max_hive_gpu_pct`, `offered_gpu_hours_per_day`) as config. Do not
  over-fold.
- **F8 ShareEvent(event_type='consent')** — `api_sharing.py:357,384-400`; a third
  consent store. Fold onto `UserConsent` with a share scope.
- **F9 two ask paths that write no row** — `_pending_contacts` in-memory dict
  (`Nunba/routes/chatbot_routes.py:4238-4330`, lost on restart) and
  `DeviceRoutingService.request_consent` (`device_routing_service.py:165-261`,
  name-collides with the canonical one). Fold: both file the ask via
  `ConsentService.request_consent`; FCM + FleetCommand stay as **transports**, not
  stores. Keep the FCM leg — it exists because FleetCommand only reaches the RN app
  when open (`:196-201`).
- **F10 `intelligence_preference`** — OWNER DESIGN CALL. The field I canonicalised
  is itself a parallel permission store for hive participation
  (`llama/llama_config.py:491-509`, gate `Nunba/main.py:601-609`); its own docstring
  frames it as consent. Decide: is joining the relay a *consent* (then the store is
  `compute_contribute`/a new `hive_relay` type and `intelligence_preference` keeps
  only routing) or a *preference*? Do not fold until answered.
- **F11 `HEVOLVE_HIVE_TRUSTED_PEERS`** — `hive_expert_discovery.py:22-23,545,564-566`;
  its own comment says it is a stopgap "until that API ships". The API now exists
  (`hartos_bootstrap.py:135-173`, `peer_admission`). Fold onto it.

**Legitimately NOT consent stores — do not fold:** `pre_trust_contract.can_join_hive`
(machine attestation, deliberately human-free), `tool_allowlist`,
`browser_research/domain_allowlist`, `claw_native` permissions,
`engagement_guardrails` (advisory), and the plan-approval / share-link
`requires_consent` name collisions.

---

## TIER 4 — push: 6 frameworks → 1 entry + declared transports

Canonical entry = `NotificationService.create` (`services.py:1137-1158`) →
`realtime.on_notification` (`realtime.py:424-442`) → WAMP + SSE, one shared
`msg_id`. **The others are TRANSPORTS, not frameworks.** Fold = every user-facing
message *originates* at the canonical entry and picks a transport; nothing else is
an entry point.

- **F12** legacy `publish_async` bypass (`hart_intelligence_entry.py:3028-3029`,
  `:11145-11160`) — the migration already tracked in
  `memory/project_publish_aop_migration.md`. Own it; do not start a rival doc.
- **F13** FCM three paths → one `send_push` (`core/fcm_sync.py:325-365`,
  `local_subscribers.py:165-183`, `device_routing_service.py`). Note the
  self-contradiction to resolve: `local_subscribers.py:40-45` says FCM is
  cloud-only, `:165-173` then sends FCM. Also fix the hardcoded generic body.
- **F14** four desktop-toast emitters → one (`hart-notify.nix:92-124`,
  `shell_os_apis.py:384`, `tray_handler.py:136-146`, `indicator_window.py:108`).
- **F15** three SMTP senders → one (`email_campaign.py:643`,
  `email_adapter.py:264`, `mailing_list.py:286` — the last is a *prober*, keep it
  separate but say so).
- **F16** `localStorage` as a push channel (`App.js:56-61`
  `agent_proactive_message`) → an event.
- **F17** two divergent public-topic allowlists — SSE `events.py:127-138` vs WAMP
  `realtime.py:43`; `events.py:113-118` admits the divergence is
  unmaintained-by-construction. Fold to one list, two projections.
- **F18** `approval.options` is **dead schema** (`liquid_ui_service.py:633`
  declares it; `AgentOverlay.jsx:269-305` and the desktop shell both ignore it).
  Either honour it or delete it — dead schema invites the next parallel path.

---

## The owner-facing ask surface (for floating open questions with options)

The consent interface is **strictly binary** — no options column, no `options`
param, `CONSENT_ANSWER_TYPES` frozen at two (`consentAsks.js:209`). The A2UI
**`notification` card already renders arbitrary N labelled actions**
(`AgentOverlay.jsx:55-115`, `actions.map`, `kind: navigate|external` wired) and
rides the same `agent_ui_update` → SSE → AgentOverlay channel as consent asks, so
an N-option question needs **no new framework**. Tradeoff to state when using it:
it is not a `UserConsent` row, so no persistence, no revoke, no privacy-page
entry, no `answerCoversAsk` cross-device dismissal. Use it for *questions*, never
as a substitute for a consent.

---

## Execution order

F1 → F2 → F3 → F0 → F5 → F6 → F11 → F8 → F9 → F7 → F13 → F12 → F17 → F14 → F15 → F16 → F18 → F4 (when unheld) → F10 (after the owner's design call).

Rationale: Tier 1 first (a withdrawn permission that still acts is live harm),
smallest blast within a tier, held/owner-gated last. F0 early because it is my own
deliberate parallel path and leaving it contradicts rule 3.
