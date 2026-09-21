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

### F0 — consent trigger spelling (close my own "read both") — **DONE + VERIFIED LIVE (fc1edfce1)**
Parallel: `require_consent` written at `goal_seeding.py:1745,1863,1892,1948,2009`;
`requires_consent` at `goal_seeding.py:118,514,580`. Sole enforcement
`hive_guardrails.py:1313` (I widened it to read BOTH — deliberate parallel path).
Unrelated same-name keys: plan approval `hart_intelligence_entry.py:10722`;
share-link viewer `api_sharing.py:255,349,372` — **not touched**.

**This entry's own canonical choice was WRONG and was corrected at execution.**
It said keep `requires_consent` ("on the wire"). The audit found the plural has
**zero readers anywhere** — it was never on a wire, it is a producer typo, while
the singular is the gate's long-standing contract. Blast radius therefore points
the other way: singular = 3 seed lines + 3 rows, reader untouched; plural would
have been 5 seed lines + the reader + 5 rows. Per the owner's rule (lowest blast
radius wins) the fold went to **`require_consent`**.
*Lesson for the remaining folds: decide the canonical side from the READER count
found in the audit, not from which spelling looks more like an interface.*

Done: 3 plural producers → singular; `hive_guardrails` reads one key again;
`migrations.py` **v56** re-keys existing `agent_goals.config_json` rows
(value preserved — a deliberate `False` stays `False`; canonical wins if both
present; unparseable rows left untouched and logged).
Tests (`tests/test_goal_consent_gate.py`, 14 pass; +seo/paper seed-shape suites,
98 pass total): producer-side guard (no seed may write the plural), reader-side
guard (exactly one spelling, comment-stripped so prose can still explain it),
and three v56 tests — re-key keeps the gate (fed through the REAL gate),
`False` is preserved, and a canonical row is untouched across two passes.
**Live (bundled node, 2026-09-21):** 3 rows re-keyed 55→56, 0 legacy keys left,
568 goals unchanged, the 5 always-canonical rows untouched; the patched gate in
the embedded 3.12.6 runtime refuses all three real goal configs with a consent
reason. This also closes task **#96** (the three goals were ungated in the field).

Observed, NOT folded (would be scope creep; logged for the inventory): a THIRD
`requires_consent` vocabulary exists at `security/ai_governance.py:665`
(`_score_human_consent`, a governance scoring *context*, not a goal config).
Nothing in the tree builds that context — only its own unit tests do. It is a
registered scorer with no live producer, so it is a dormant-path question for
F10/the framework pass, not a gate.

### F1 — voice: revocation does not take effect — **AUDITED, BLOCKED (peer in-flight is doing this fold)**

**Caller audit complete 2026-09-21** (every reader/writer of the two keys):
- WRITES `nunba_speech_consent`: `LightYourHART.js:929`, `Demopage.js:779`, `:830`
- READS it: `LightYourHART.js:917` (existing-consent check), `:949` (**the gate**,
  `=== 'text_only'`), `Demopage.js:741`, `:754` (**the gate**, `=== 'granted'`)
- WRITES `tts_enabled` (localStorage): `LightYourHART.js:930`, `Demopage.js:780`,
  `:831`, `:877`; READS: `Demopage.js:743` (legacy fallback)
- Canonical side already called: `LightYourHART.js:932/934`
  (`consentApi.grant/revoke voice_speech`, both `.catch(()=>{})`),
  `Demopage.js:786/788`, `:835/837`, privacy page `PrivacySettingsPage.jsx:1001-1010`,
  vocabulary `consentAsks.js:86`
- **NAME COLLISION — do not fold blindly:** `Admin/SettingsPage.js:597-599` uses
  `media.tts_enabled`, a **server-side media config field**, NOT the localStorage
  key. Different concern, same name. Folding them breaks the admin toggle.

**BLOCKED, and not by knowledge:** `LightYourHART.js` (+149) and `Demopage.js`
(+122) are uncommitted-dirty with work that ALREADY implements this fold —
`handleOnboardingSpeechConsent`, `handleSpeechConsentDecision`,
`toggleVoiceSpeechConsent`, a `speechConsent` state, `import { consentApi }`, and
the comment "Sync with canonical HARTOS consent API", with hunks at Demopage
737-774 and 5729-5827 (the exact region). Two agents implementing one fold in one
file is the agent-level parallel path. **Do not edit either file.** Defensive
backup taken (diff + both files) to `scratchpad/canon_wip/peer_wip_backup` —
uncommitted work in this checkout was destroyed once already today. Ownership
query sent; `claude-character-call` is no longer in the peer list, so it may be
orphaned, in which case the owner must decide whether to adopt it (I must not
commit another session's work as mine, nor discard it).

**My acceptance test stands regardless of who writes the code** (rule: a hand-off
is not a closure): revoke `voice_speech` on the privacy page → the SPA stops
speaking **without a reload**. If their change lands without that property, F1 is
still open.

**DO NOT take the tempting fix** (fix-all, independently verified the citation):
having `PrivacySettingsPage` also write `nunba_speech_consent` on revoke **mints a
SECOND writer of the same value and rebuilds the drift one layer up** — the two
agree only until a third surface appears. Hold the framing literally: localStorage
is a PROJECTION of the canonical consent with **exactly one writer**, and the
gates read the projection.

**The copy is currently a promise the code cannot keep** (both of us measured it):
`PrivacySettingsPage.jsx:1000-1007` `VOICE_SPEECH_CARD` says verbatim *"Revoking
mutes all voice output immediately, switching to quiet text-only mode."* while
`grep -c 'nunba_speech_consent\|tts_enabled'` on that file is **0**. Same family
as a tool reporting success it did not achieve, except pre-printed in the UI. If
the full fold cannot land yet, the honest interim is to **weaken the copy to match
the code, never to add a writer** — an over-promising card is worse than an
under-promising one. (Product-copy change ⇒ owner's call, and it must be reverted
when the fold lands, so it is recorded here rather than done silently.)

**Blast-radius insight — the fold may not need the orphaned files at all.** The
two gates (`Demopage.js:754`, `LightYourHART.js:949`) live INSIDE the 271
uncommitted lines, so editing them collides. But the gates only *read*
`localStorage.getItem('nunba_speech_consent')` — so a projection writer placed in
a CLEAN file (e.g. the existing `consent.granted`/`consent.revoked` subscriber
path reached from `App.js`, which is clean) makes those gates correct **with zero
edits to either dirty file**. Residue to be explicit about: the dirty files also
*write* the key today (`LightYourHART.js:929`, `Demopage.js:779/830/877`), so that
step leaves multiple converging writers — authority is correct, exclusivity is
not. Therefore F1 is TWO steps and both must be recorded, not one declared done:
  - **F1a (clean files only):** add the projection subscriber → revocation from any
    surface mutes the SPA. Closes the user-visible harm.
  - **F1b (needs the orphan resolved):** delete the redundant local writes so
    exactly one writer remains. Until F1b lands, F1 is NOT closed.
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

### F2 — copilot: env var overrides a revoked consent — **VERIFIED (66386a45e)**

Caller audit (complete, untruncated): `copilot_enabled()` is the SINGLE gate for
4 readers — the `claude -p` spawn `claude_code_backend.py:103`,
`claude_code_available` `:414`, `mcp_http_bridge.py:260`, `agent_daemon.py:252`
(which additionally reads the consent at `:262/264/268`). One writer:
`set_copilot_enabled` `:355`, called by `consent_service._copilot_switch_from_consent:360`
and by the admin page.
Finding: revocation DOES propagate (the actuator writes the marker) — the hole was
only that the env was read FIRST and returned early, so `HARTOS_COPILOT_ENABLED=1`
outranked the marker and re-enabled all four readers against the human's answer.
Fold: asymmetric override — a non-on pin still force-DISABLES (unchanged), an ON
pin cannot override a present marker. Safe because with no marker the node is
already enabled, so an ON pin was a no-op there by construction. No new store, no
DB read added to the spawn path; the marker remains the derived cache with one
writer (D1/D6).
Tests: 15 passed, incl. a shape guard that fails if the env is ever read before
the marker again. Live-patched + verified in the embedded runtime.
**NOT folded here (deliberate, avoids widening):** the admin page can still flip
the marker without filing a consent row — same shape as F6, handled there.
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

### F3 — `hive_participation` — **RE-SCOPED by its own audit; MERGED INTO F5; file is DIRTY**

Audit 2026-09-21 corrected the plan's own premise — recorded because building on
the wrong target was the risk:
- Readers/writers (complete): `world_model_bridge.py:281` `_has_hive_participation`,
  reads `(user.settings or {}).get('hive_participation', True)` at `:304`, enforced
  at `:1373`. **No writer anywhere in the codebase** — the key is only ever read,
  so it can only be set out-of-band. That alone makes it a poor authority.
- **It is NOT the `compute_contribute` concern.** `:1373` gates `hivemind_query` —
  whether THIS user's query/data goes OUT to the hive. `compute_contribute`
  (`compute_mesh_service.py:730`) gates whether PEERS' work runs ON this device.
  Opposite directions; folding them together would conflate ingress with egress.
- Its own docstring (`:285`) says "Cached alongside cloud consent (same TTL)", and
  it shares `self._consent_cache` with `cloud_data_consent` — **the same file's F5
  store**. So this is the egress concern: it belongs with F5 and the canonical type
  is `cloud_egress`, not `compute_contribute`.
- **BLOCKED:** `world_model_bridge.py` is uncommitted-dirty (another session).
- **OWNER CALL inside it:** default-True means hive queries are opt-OUT. Making
  them opt-in is a product behaviour change (hive queries stop working until
  granted), not a refactor — do not fold that silently.

Fold when unblocked, together with F5 (one file, one cache, one fold): both
`cloud_data_consent` and `hive_participation` become reads of `cloud_egress` via
the canonical service, the 5-minute private cache goes, and the default is the
owner's decision recorded above.

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
- **F6 embodied camera/screen config** — **DONE + VERIFIED LIVE (`2907dae2e`)**.
  Audit found THREE writers of `camera_enabled`/`screen_capture_enabled`, not one:
  `channels/admin/api.py` toggle (:2209-2219) + the config **PUT** (:2185, whole
  schema, missed by a flag-name grep), and `hart_intelligence_entry.py:11196-11201`
  — which turned out to be **already correct**: it is the documented
  `if _recorded is None:` fallback after `record_capability_decision`, and that
  guard was the shape to copy rather than a defect.
  Folded both admin paths onto `record_capability_decision`. Reused, invented
  nothing: `CAPABILITY_CONSENT_TYPES` already maps the endpoint's own feed names
  ('camera'/'screen'), `_admin_auth_gate` already puts `g.user_id` + `g.db` on
  every admin request (so NO env fallback and no `HEVOLVE_OWNER_USER_ID` dead
  end here, unlike the goal gate), and the recorded decision applies the feed
  through the same `_apply_embodied_toggle`.
  Functionality deliberately preserved: 'audio' governs no consent type and is
  still applied directly; a failed consent write logs and falls back to the old
  direct apply. Behaviour change named in the commit: a PUT that switches the
  camera on now actually starts it (it only wrote a flag before), and the PUT
  records only a CHANGED flag so an ordinary settings save stays a no-op.
  Tests: extended `tests/unit/test_capability_consent_canonical.py` (33 pass) —
  real endpoint calls in a Flask request context, not source guards, since
  `admin.api` is importable; incl. an AST divergence guard (no admin function
  may set a feed flag without recording) proven RED against the pre-fold file,
  and an *exactly once* actuator assertion (0 = the toggle stopped working,
  2 = a second path). 224 pass across the 18 consent/admin suites.
  Live: patched into the embedded runtime and driven there — row written, feed
  applied once — against a throwaway DB with the actuator stubbed, because
  verifying a privacy fix must not itself switch a camera on.
  *Lesson: grep the flag NAME and the enclosing schema assignment; the PUT wrote
  the same permission through an object assignment no name-grep would find.*
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

### Progress
- **F2 DONE + VERIFIED** (`66386a45e`) — an env pin can no longer outrank a
  revoked copilot consent; 15 tests incl. a shape guard.
- **F0 DONE + VERIFIED LIVE** (`fc1edfce1`) — one consent-trigger spelling, data
  migrated on the live node, #96 closed. See F0 above for the corrected
  canonical-choice rule.
- **F1 BLOCKED** — 271 orphaned uncommitted lines from a peer session that is
  gone (backed up in `scratchpad/canon_wip/peer_wip_backup/`); needs the owner's
  adopt-or-discard call. Split F1a (projection subscriber, clean file) / F1b
  (delete the redundant local writes).
- **F6 DONE + VERIFIED LIVE** (`2907dae2e`) — the admin camera/screen switch
  records the owner's decision; three writers folded to one entry.
- **F3 merged into F5**, blocked on a dirty `world_model_bridge.py` and carrying
  an owner call (hive queries opt-out → opt-in).
- **NEXT: F11** (`HEVOLVE_HIVE_TRUSTED_PEERS` → the `peer_admission` API that now
  exists), since F5 is still held by another session's edits. Then F8, F9, F7.
