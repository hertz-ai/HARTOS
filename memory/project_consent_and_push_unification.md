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

**Live on CENTRAL too, verified 2026-09-21 10:09Z** (image `langchain_gpt:baca7523`,
i.e. the tag IS the commit): `schema_version 56`, **0 legacy plural rows**, 241
goals. v56 ran itself at boot on production data.

**F0b — DONE + VERIFIED (`a39b1d9d6`). Owner answered 2026-09-21.**
Asked whether to give central an owner; measured first and found the wall:
**central has no human account at all** — 45 rows, 36 guests + 2 service
accounts (`hevolve_system`, `nunba`), no admin role, nothing matching the owner.
(Also confirmed that IS the live DB: no `HEVOLVE_DB_URL` is set, so central boots
the SQLite fallback and logs CRITICAL about it every time — its own separate
misconfiguration, filed nowhere yet.)
**Owner's decision: run consent-requiring goals on the DESKTOP only.** A node
with no declared owner does not run work that needs someone asked. So the gate
now PARKS such a goal via `_park_goal_for_no_owner` — `status='paused'` +
`config.pause_reason` naming both ways out — instead of refusing silently every
tick. Written through `GoalManager.update_goal`/`update_goal_status` (no second
writer), with a NEW dict for the MutableDict trap.
Desktop is unchanged, which is the property that matters: Nunba's boot exports
`HEVOLVE_OWNER_USER_ID`, so the gate still takes the ask path there. A test pins
that a goal whose owner CAN be asked is never parked — parking it would be the
self-inflicted outage.
Live: embedded 3.12, owner env unset, real AgentGoal row -> active becomes paused
with a readable reason, config intact, second tick a no-op. Against a throwaway
DB on purpose: central's 8 are already paused for other reasons and the helper
leaves an already-paused goal alone by design, so this stays latent there until
one is un-paused, which is the correct outcome.

**F0b — original finding (kept for the record):** All 8 consent-gated
goals there carry **`owner_id = NULL`** (bootstrap seeding calls
`GoalManager.create_goal(..., created_by='system_bootstrap')` and never passes an
owner), and central sets no `HEVOLVE_OWNER_USER_ID`. So
`agent_daemon` passes `user_id=goal.owner_id` = None, the env fallback is empty,
and the gate returns *"consent-flagged goal dispatched without user context"* and
files **nothing** — blocked, with nobody asked, every tick. Exactly the
BLOCK-without-ASK failure the owner named.
**Latent, not active:** all 8 are `paused`, last dispatched Aug 2-30, so nothing
is being stopped today and F0 broke nothing. It bites the moment the owner
un-pauses any of them.
Candidate fix (uses existing machinery, no new path): when the gate cannot name a
human, **pause the goal with a `pause_reason`** instead of refusing silently on
every tick — the four existing pause paths already write one, so the dead end
becomes visible state a human can act on rather than a log line nobody reads.
Needs the owner's answer to "who is the human for a bootstrap goal on a shared
cloud node?" before the ASK half can work at all.

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

### F4 — the mic shares the goal gate's consent — **OWNER DECISION 2026-09-21; do not implement**
Owner asked 2026-09-21 with the three options below costed; answer was **file it, do
not change behaviour**. So F4 stays open by decision, not by ignorance: the finding
and its measurements are recorded, nothing is folded, and the next session must NOT
"just add microphone_capture" without revisiting the grant-migration question.
The "HELD" premise was wrong. fix-all-log-observed-issues confirms it has **no**
`consent_service.py` edits and never had any in this session — it only READ the
file — and `git status`/`git diff` on that path are both empty. So F4 is
unclaimed and doable now.
*Lesson (the second misattribution of the night, both mine): a dirty file, or a
peer who once discussed a file, is not evidence that a session owns work in it.
Ask, or check authorship, before holding a fold for someone — holding on a false
attribution costs exactly as much as a real block.*
**PREMISE CORRECTED 2026-09-21 — the fold is bigger and needs an owner call.**
"The mic has no consent type" is WRONG. The mic IS gated, on a SHARED generic
type, which is worse than ungated because it conflates two capabilities:

    whisper_tool._mic_learning_consented:
        check_consent(db, user, 'data_access', scope='*')     -> ingest mic PCM
    hive_guardrails.py:1407-1413:
        consent_type = cfg.get('consent_type', 'data_access')
        check_consent(db, user_id, consent_type, scope='*')   -> run a goal

Identical type AND scope. So a user who approves a consent-gated GOAL (whose type
DEFAULTS to `data_access`) thereby also permits raw mic PCM16 to be ingested into
HevolveAI's world model for continual learning, and a user who answers the mic ask
thereby satisfies every default-typed goal gate. The whisper docstring intends the
second direction on purpose ("a user who has granted the speech-therapy or spoken
English agent its microphone consent is not asked a second time"); the FIRST
direction — a goal's data-access approval silently enabling the microphone — reads
as unintended, and it is the privacy-relevant one.

This blocks the fold on a decision I must not take alone, because BOTH options cost
something and the owner has reserved exactly this kind of call ("one flag per
concept; consents have scope+TTL", and "never discard an existing grant without
knowing its scope+TTL"):
  (a) switch the mic to `microphone_capture` and re-ask everyone. Honest and
      separates the concepts, but it revokes de-facto permission that users
      currently hold and interrupts live voice agents (speech therapy, spoken
      English) mid-flow. The original F4 text assumed this without noticing it
      discards grants.
  (b) accept `microphone_capture` OR a pre-existing `data_access` grant during a
      migration window. No interruption, but it keeps the conflation alive for as
      long as the window lasts, and needs a TTL to ever end.
Whichever is chosen, `ai_sensing` stays as the hard cut (a kill switch that works
with the DB down is a legitimate second layer, not a parallel store) but must not
be the only record.

Original notes below, kept because the rest of the fold still applies:
`core/ai_sensing.py:27-102` in-process kill switch (`_state = {'mic', 'camera',
'screen'}`, `allowed(sensor)`); `consent_service.py:136-137` says an ask "belongs
here" if built. Note `screen_capture` IS already in CONSENT_TYPES and
`CAPABILITY_CONSENT_TYPES` maps camera/screen aliases, so the registry pattern to
copy for the mic is established.
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
- **F7 NodeComputeConfig + `HEVOLVE_*`** — **defect 1 DONE + PUSHED (`a1789b6c2`),
  defect 2 OPEN (needs an owner call).** Defect 1: the env pin may now only
  RESTRICT `allow_metered_for_hive`, never grant, via `_ENV_MAY_ONLY_RESTRICT`.
  Red-first proved the guard discriminates (env=true fix-off -> True, fix-on ->
  False, env=false -> still restricts); 22 tests pass and the 4 new ones were
  verified BY NAME. The pre-existing `test_env_allow_metered_override` asserted the
  GRANT works, i.e. it pinned the defect, and is replaced.
  Audit detail below stands.
  *Path in this plan was wrong: it is `integrations/agent_engine/compute_config.py`,
  not `integrations/social/`. Third premise error caught by checking first.*

  `_DEFAULTS` (`:16-24`), precedence **env > DB > defaults** (`:54-77`), split by
  kind as the fold intends:
  | field | kind |
  |---|---|
  | `allow_metered_for_hive` (False) | **PERMISSION** — spends the user's metered link |
  | `accept_thought_experiments` (**True**) | **PERMISSION** — accepts hive work |
  | `accept_frontier_training` (False) | **PERMISSION** |
  | `max_hive_gpu_pct` (50), `offered_gpu_hours_per_day` (0.0), `metered_daily_limit_usd` | capacity — LEAVE as config |
  | `compute_policy`, `hive_compute_policy`, `auto_settle`, `min_settlement_spark` | policy/financial — out of scope |

  **Two real defects found, one of them F2's exact shape:**
  1. `HEVOLVE_ALLOW_METERED_HIVE` (`:76`) overrides `allow_metered_for_hive`, and
     env outranks DB — so **an env pin can GRANT a permission the user declined**,
     which is precisely what F2 fixed for the copilot (`66386a45e`). Same remedy
     applies verbatim: the override may only ever RESTRICT, never grant. Copy F2's
     asymmetric predicate and its test shape.
  2. `accept_thought_experiments` **defaults to True**, so a node accepts hive
     thought-experiment work with no record that anyone agreed. Permission by
     default, and nothing to revoke.

  **Do defect 1 first, alone.** It is a strict narrowing with an accepted precedent,
  so it carries no self-DoS risk. Defect 2 flips a default that other nodes'
  work depends on, so consent-gating it could stop hive work fleet-wide — that needs
  BLOCK + ASK + RECOVERY designed before it lands, and probably an owner call on
  whether existing accepting nodes are grandfathered. Do NOT bundle them.

  Callers to audit before either (the fields are read in 5 non-test files):
  `hart_intelligence_entry.py`, `compute_config.py`,
  `integrations/coding_agent/tool_backends.py`, `integrations/social/api_tracker.py`,
  `integrations/social/_models_local.py`; tests in `tests/unit/test_compute_config.py`.
- **F8 ShareEvent(event_type='consent')** — **CLOSED, NOT FOLDED (`afe51600a`)**.
  Determination: this is **not** the same concern as `UserConsent` and folding it
  would be over-folding. `UserConsent` is "I permit software to do X to me",
  keyed UNIQUE(user_id, agent_id, consent_type, scope) with revoke + a
  privacy-page entry. This is a **viewer acknowledging a notice before seeing
  someone else's content**: different subject (viewer, not owner), different
  object (one link, not a capability), unbounded per-link values, and it doubles
  as the sharer's audit trail. Folding it would list every view acknowledgement
  on the owner's privacy page and bury the actual camera/screen/copilot grants.
  F0's own exclusion list already flagged this file as a name collision — F8 and
  that exclusion contradicted each other; the exclusion was right.
  **But the audit found two real defects in the same code, so it was not a
  no-op:** (1) `resolve_share_token` hid only the OG card for a private link and
  still returned `redirect_url` + `resource_id`, on an `@optional_auth` route,
  while `/consent` is `@require_auth` — an anonymous caller could read the target
  and skip consent entirely (same shape as F11: a gate that guards nothing);
  (2) `requires_consent` was `link.is_private` flat, so an already-consented
  viewer was re-asked every visit and banked a duplicate ShareEvent.
  Fixed by withholding the LOCATOR until consent (`resource_type` stays — the
  consent screen's copy renders it and it is a category, not an address) and by
  extracting the one `_viewer_has_consented` predicate that `/check-consent`
  already had and this path did not consult.
  10 tests (`tests/unit/test_private_share_link_gate.py`), red against the
  pre-fix file by construction; 143 pass across the four suites touching the
  module; live-verified in the embedded runtime through the real anonymous
  `@optional_auth` branch.
  *Lesson: "a third store for the same concern" can be a third store for a
  DIFFERENT concern wearing the same word. Check subject/object/lifecycle before
  folding — and when the answer is "do not fold", still audit the code, because
  the reason it looked like a consent store is that it gates something.*
- **F9 — DONE + PUSHED 2026-09-21** (`077b27332`). Zero regression EVIDENCED, not
  asserted: hermetic A/B of `a34e6489f^` vs this change, fresh pinned
  `HEVOLVE_DB_PATH` per arm, 254 passed / 0 failed / 0 errors in both arms.

  **What it turned out to be, bigger than the audit said.** Implementing half A
  hit `IntegrityError` and exposed a defect in the SHARED writer:
  `request_consent` files a pending row on the very
  `UNIQUE(user_id, agent_id, consent_type, scope)` key `grant_consent` then
  inserts on, and SQL treats only NULL as distinct — so **a per-agent consent
  could be ASKED and then never GRANTED**. The documented workaround (revoke
  first) cannot help: revoking UPDATES the row rather than freeing the key. That
  is *why* `record_capability_decision` grants BLANKET — it sidesteps the wall
  instead of hitting it.
  Fix: `grant_consent` promotes a never-granted pending row (sets granted +
  granted_at, clears revoked_at) **only when `agent_id` is not NULL**.

  **My first version regressed a real invariant and a test caught it.**
  Promoting unconditionally broke `test_grant_after_pending_request_appends_row`
  — an orchestrator-reviewed semantic (`acd11f55`): with agent_id NULL the
  pending row must REMAIN as audit history and a new granted row is appended.
  Narrowing to non-NULL agent_id (where appending is impossible anyway) leaves
  that invariant and its test untouched. *Lesson: when widening a shared writer,
  scope the new branch to exactly the case the old one cannot serve.*

  **Two things the tests corrected, not the reverse:**
  - deny → accept DOES record (a denied ask never had a granted_at, so promotion
    applies). I had assumed it could not.
  - the real remaining limitation is narrower: **accept → revoke → accept**
    cannot be recorded, because promoting a row that HAS a granted_at would
    rewrite history. Pinned as current behaviour in
    `Nunba/tests/test_agent_contact_consent_record.py` so fixing it fails loudly
    rather than drifting. Closing it needs a schema call (a partial unique index
    that ignores revoked rows, or an explicit re-ask row).

  **Half B**: `DeviceRoutingService.request_consent` now files the canonical row
  via `ConsentService.request_consent` before its three transport legs (FCM
  overlay, FleetCommand, notification). NOT renamed on purpose: it has no
  production callers, only tests, so the hazard was that the two same-named
  functions DISAGREED (one recorded, one did not). Making them agree removes the
  hazard; renaming would have moved it.

  **Deliberately left, with its blocker named**: `_pending_contacts` stays as a
  per-process cache of the ask PAYLOAD, documented as not-the-record. The payload
  has no durable home — `Notification` has one `message` column and no metadata
  field, `UserConsent` has no `reason` column — so surviving a restart needs a
  v57 `notifications.payload_json` (or a table). Also NOT done: suppressing the
  re-ask when a grant exists, because the live e2e
  (`landing-page/cypress/e2e/agent-consent-e2e-live.cy.js`) re-runs the same
  agent+user and then answers the request_id it got back; short-circuiting would
  404 its respond call. That contract also pins 404-before-400 ordering and a
  REPLAYABLE accept — all three now asserted in the new pytest so they are
  checkable without a browser.

  **THIRD self-correction — RETRACTED IN FULL, and the retraction is the lesson.**
  What stood here first: the sweep went 3 failed / 247 passed / 7 errors, "HEAD
  measured 253 passed / 1 error", therefore the regression was mine, root-caused
  to autoflush, fixed by `no_autoflush`. **That whole attribution was wrong.**

  How it fell apart. I diffed the tree I had called "HEAD, none of my changes"
  against my own version. The only difference was the wrapper itself:
  ```
  -            _pending = db.query(UserConsent).filter(
  +            with db.no_autoflush:
  +                _pending = db.query(UserConsent).filter(
  ```
  The peer sweep commit `a34e6489f` (16:52) had already committed my in-flight
  `consent_service.py`, so the "baseline at HEAD" I checked out at 17:10
  **already contained my promotion branch**. 253/1 and 3F/247P/7E were the SAME
  CODE. There was no measured regression and no measured fix.

  **The real source of the variance: THE CONSENT SWEEP IS NOT HERMETIC.**
  `integrations/social/models.py` reads `HEVOLVE_DB_PATH` once at import and
  caches `DB_PATH`. Several suites assign `':memory:'` directly, while
  `test_consent_api.py` and `test_goal_consent_gate.py` set nothing — so whichever
  suite imports `models` first decides the DB for the whole process. When a file
  path wins, the run writes `agent_data/hevolve_database.db`, a real 44MB file
  (modified 17:31 during this work), and the next run starts on that state. Run
  order, not code, produced the difference.
  → Filed as **F19** below. It also means the pass/fail counts used while closing
  F0 and F6 came out of the same non-hermetic sweep; both folds have independent
  LIVE verification so their verdicts stand, but their sweep numbers support
  nothing.

  `no_autoflush` **stays, on principle, not on a measurement**: adding a read to
  a write path does change flush timing, and guarding it costs nothing. The
  in-code comment and commit `077b27332` both carry this retraction, so the false
  claim cannot be re-derived from either.

  **Replacement evidence (the real A/B, hermetic).** `a34e6489f^` (pre-F9) vs my
  version, 19 suites, a FRESH pinned `HEVOLVE_DB_PATH` per arm:
  `scratchpad/f9_hermetic_ab.sh`, results in `f9_AB_baseline.txt` /
  `f9_AB_mine.txt`. Zero-regression is claimable only off that pair.

  *Rules earned, all about evidence rather than SQLAlchemy:*
  - *Before comparing two trees, PROVE THEY DIFFER — `git diff` them. "I checked
    out HEAD" is not proof in a checkout other sessions commit into.*
  - *A suite is a control only when its state is pinned. Where a module caches a
    DB path at import, import ORDER is an input to the test, so a pass/fail count
    across runs measures order, not code.*
  - *I wrote "Measured" into a commit message on the strength of a comparison I
    had not verified. That is the green-signal failure I keep a rule about,
    committed by the one keeping the rule.*
  - *Peers `git stash`/`stash pop` this shared checkout (seen at 17:44, reflog
    "reset: moving to HEAD"). A file-swap A/B here can be silently reverted
    mid-run: print the discriminating grep BEFORE AND AFTER each arm.*

- **F9 original audit** — **PREMISE VERIFIED 2026-09-21, both
  files CLEAN, ready to implement. This one IS the same concern as `UserConsent`
  (unlike F8): subject = the human, object = a named agent, revocable, belongs on
  the privacy page.**

  **Half A — `_pending_contacts`** (`Nunba/routes/chatbot_routes.py:4242` decl,
  written `:4306`, read `:4347,4353`, expired `:4375-4377`). A module-level dict.
  `agent_contact_request` stores the ask and pushes an `agent_contact_request`
  notification; `agent_contact_respond` accepts/denies. Four measured defects:
  1. **lost on restart** — the pushed notification card survives but the dict does
     not, so tapping Accept later returns `{'error': 'Unknown or expired request'}`
     404 on a card the user can still see;
  2. **an accept is never recorded**, so the same agent re-asks next time — the
     "pestered forever" failure `record_capability_decision`'s own docstring names;
  3. **a deny is equally unrecorded** (`contact['status'] = action` on a dict about
     to be GC'd), so a refused agent can re-ask immediately;
  4. the 1h cleanup runs ONLY inside `respond`, so an ask nobody answers never
     expires at all.
  Owned agents (`creator_user_id == target_user_id`) deliver directly with
  `requires_consent: False` — leave that branch alone, it is correct.

  **Half B — `DeviceRoutingService.request_consent`**
  (`integrations/social/device_routing_service.py:165`). Does three real things —
  `NotificationService.create('agent_consent_request')`, an FCM `consent_prompt`
  (the native over-other-apps overlay, the ONLY leg that reaches a user away from
  the agent's machine), and a FleetCommand — and writes **no consent row**.
  **Name collision to fix deliberately:** its signature is
  `(db, user_id, action, agent_id, description, timeout_s)` while the canonical
  `ConsentService.request_consent(db, user_id, consent_type, scope)` DOES file a
  row. Same name, different contract, one records and one does not — the same trap
  shape as F11's wrapper-vs-inner-dict. Rename or delegate, do not leave both.

  Fold: both file the ask through `ConsentService.request_consent`; FCM +
  FleetCommand + the notification stay **transports**, never stores. Keep the FCM
  leg (`:194-204` explains why: FleetCommand only reaches the RN app when open).
  Blast: 1 Nunba file + 1 HARTOS file. Acceptance: an accept survives a restart
  and is not re-asked; a deny is remembered; the phone overlay still fires.
- **F10 `intelligence_preference`** — OWNER DESIGN CALL. The field I canonicalised
  is itself a parallel permission store for hive participation
  (`llama/llama_config.py:491-509`, gate `Nunba/main.py:601-609`); its own docstring
  frames it as consent. Decide: is joining the relay a *consent* (then the store is
  `compute_contribute`/a new `hive_relay` type and `intelligence_preference` keeps
  only routing) or a *preference*? Do not fold until answered.
- **F11 `HEVOLVE_HIVE_TRUSTED_PEERS`** — **DONE + VERIFIED LIVE (`4ad2cea9e`)**.
  **This entry's premise was wrong twice over.** `peer_admission`
  (`hartos_bootstrap.py:135-173`) is the PeerLink device-link consent, a
  different concern entirely — not this gate's API. And the audit found the gate
  was not "a stopgap with a working alternative": it had THREE mechanisms for one
  decision and **all three were dead**, so the path was inert for its whole life.
  (1) the import was `security.key_delegation.verify_peer_attestation`, which
  that module does not define → ImportError on every advert; (2) the fallback env
  allowlist is unset in the field → `peer_id in set()` → no peer ever trusted;
  (3) the producer sent `trust_signature: ''`, unacceptable to any verifier.
  Real API: `security.origin_attestation.verify_peer_attestation`, already called
  correctly by `federated_aggregator.py:863`. Folded onto exactly that — same
  function, same `origin_attestation` payload key, same `(ok, reason)` contract —
  and the advertiser publishes `get_attestation_for_federation()`'s output.
  **Contract trap, cost me a false negative:** the producer returns
  `{'valid','attestation'}`, the verifier takes the INNER dict, and passing the
  wrapper fails "Origin fingerprint mismatch" *on a genuine node* — reads exactly
  like a rejected peer. Unwrap now lives in one helper (`_origin_attestation`),
  with a test pinning that the wrapper is refused.
  Live (embedded 3.12, env lever unset): real attestation → expert backend
  registers (1); forged or absent → 0. 98 tests pass across both suites, **none
  skipped**; guard red against all three deleted mechanisms pre-fold.
  *Lesson: a fold's premise can be wrong in the plan. Before folding onto an
  "existing API", check it exists under that NAME with that SIGNATURE and that
  the producer half emits what it consumes — and prove the target path works
  before deleting the operator lever, or the fold just swaps one always-deny for
  another and removes the only way out.*
  *Also: a test that sets the env var itself will pass while the path is dead.*

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
  `:11145-11160`) — **PREMISE PARTLY WRONG, checked 2026-09-22.**
  `memory/project_publish_aop_migration.md` **DOES NOT EXIST** — `memory/` holds
  only this plan, `tool_name_unification_plan.md` and `tool_naming_audit.md`. So
  there is nothing to "own", and F12 needs its own audit from scratch. The
  instruction "do not start a rival doc" was predicated on a doc that isn't there;
  it should not stop the next session from writing the first one.
  `publish_async` itself DOES exist and has real callers
  (`core/persona_registry.py:211`, `core/peer_link/crossbar_publish.py:143-148`,
  `core/peer_link/message_bus.py:135`), several resolving it indirectly through
  `safe_hartos_attr('publish_async')` — which is the interesting part, because an
  attr-resolved callable is invisible to a name grep of call sites.
  ### F12 AUDIT 2026-09-22 — **DO NOT FOLD THE CALL SITES YET. There is a live
  feature riding on this exact function, and folding it silently breaks it.**

  **Three definitions, and they are NOT rival copies** (my own first reading, and
  it was wrong):
  | site | what it is |
  |---|---|
  | `hart_intelligence_entry.py:2408` | **the canonical one** |
  | `hartos/reuse_recipe.py:391` | thin delegator via `safe_hartos_attr('publish_async')` |
  | `hartos/create_recipe.py:141` | same, and the docstring gives the REASON: a worker eager-importing `hart_intelligence` deadlocks against the canonical loader's import lock |
  So the duplication is deliberate and load-bearing. Deleting either delegator
  reintroduces an import deadlock in workers.

  **THE HAZARD, measured in both repos.** Nunba monkey-patches `publish_async`
  **in place, in all three modules**, to call `_capture_thinking(message)`:
  `Nunba-HART-Companion/routes/hartos_backend_adapter.py:94` says so outright
  ("We monkey-patch publish_async in all 3 modules so thinking traces are..."),
  with `_capture_thinking` at `:107`, the canonical patch at `:222-230` and the
  per-module loop at `:235-243`. That buffer is what the `/chat` HTTP response
  embeds as its thinking trace.
  → **Folding these call sites onto the bus / a canonical push entry silently
  strips thinking traces from every `/chat` response.** Nothing errors; a feature
  just goes quiet. Exactly the failure shape the rest of this plan keeps finding.
  → PREREQUISITE, and it is cross-repo: migrate Nunba's interceptor from an
  in-place monkey-patch to a bus subscriber on `chat.response` FIRST, ship it, and
  only then fold HARTOS's call sites. Same ordering constraint as F11 and the WAMP
  ACL: the consumer moves before the producer.

  **DOUBLE-CAPTURE: mechanically REAL, currently LATENT. Measured, not inferred.**
  The chain exists, every link read from source:
  1. Nunba patches the delegators too, not just the canonical
     (`hartos_backend_adapter.py:235-243`, "all 3 modules").
  2. A delegator's body resolves the target at CALL time via
     `safe_hartos_attr('publish_async')`, and `safe_hartos_attr.py:156` is a plain
     `getattr(mod, name, None)` on `sys.modules['hart_intelligence']` — so it hands
     back the **patched** canonical, not the original.
  3. `_capture_thinking` (`:107`) appends with **no dedupe**.
  So one call through a delegator runs `_capture_thinking` twice on the same
  message.
  **But it does not fire today.** The only delegator call site is
  `create_recipe.py:573` (`send_message_to_user1`, and only under `_bundled`), and
  its `chat_payload` (`:555-566`) carries NO `priority` and NO `action` key, while
  `_capture_thinking` records only when `priority == 49 and action == 'Thinking'`.
  The payload is skipped, so nothing duplicates.
  → **Latent trap for whoever touches this next**: add `priority: 49` /
  `action: 'Thinking'` to a delegator-published payload, or route an existing
  Thinking bubble through a delegator, and traces silently double. Either dedupe in
  `_capture_thinking` (cheap, by request_id + identity) or have the delegators call
  the ORIGINAL canonical rather than the patched attribute. Worth doing as part of
  the interceptor migration, not before it.
  *Also recorded from that call site's own comment: a previous
  `from core.message_bus import publish_async` raised ModuleNotFoundError on EVERY
  call, so bundled mode silently dropped every intermediate chat message. Third
  silent-drop in this one function's history — this path has a habit of it.*

  **Where the missing doc reference came from**: the canonical docstring itself
  names `memory/project_publish_aop_migration.md` as "the right long-term shape".
  So the plan's author copied a pointer out of a docstring, and the doc was never
  written. The shape was identified and then lost — which is the thing to fix by
  writing it, not by treating the reference as authoritative.

  `hart_intelligence_entry.py:3029` is the described bypass, inside
  `_push_workflow_flowchart`:
  ```python
  from core.peer_link.message_bus import chat_topic_for
  publish_async(chat_topic_for(user_id), json.dumps(crossbar_message))
  except Exception:
      logging.getLogger(__name__).exception(
          "_push_workflow_flowchart: swallowed Exception")
  ```
  Note the `except Exception` that SWALLOWS: a push that never arrives is visible
  only in a log nobody reads, which is the same shape as the rest of tonight's
  findings. Worth folding the delivery AND the silence together.
  Still to audit next session: where `publish_async` is DEFINED, the second cited
  site `:11145-11160`, and whether `message_bus.py:245` monkey-patching
  `hart_intelligence.publish_async` is a third path that any fold must not break.

  *Caution recorded, and then corrected: my first check used a bash `grep` that
  hit the 120s timeout, and its partial output showed no matches. I began to read
  that silence as "the function does not exist" — wrong twice over, because the
  search had not finished AND the function is real. The task completed later and
  returned the matches above. A search that was cut off is not a negative result,
  and the fast Grep tool answers this tree in a second where bash grep does not.*

**FOUR of this plan's premises have now been wrong** (F11's API, F4's "no consent
type", F7's path, F12's doc). The plan was written from recollection, not from
reading the tree, so roughly half its specific file references do not survive
checking. **Verify before executing remains the highest-value rule here**, and a
fold that "looks trivial" in the plan text is exactly the one to check first.
- **F13** FCM paths → one — **AUDITED 2026-09-22. One instruction in it would be a
  PRIVACY REGRESSION; do not execute that part without the owner.**

  Names, for accuracy: the canonical pair is `send_fcm_push(user_id, ...)`
  (`core/fcm_sync.py:325`) and `send_fcm_push_to_node(node_id, ...)` (`:346`), not
  `send_push`. Those two are different TARGETS (a user vs a node), so per F12's
  lesson check they are genuinely duplicated before collapsing them — two callers
  needing two addressing modes is not the same as two rival implementations.

  **The "self-contradiction to resolve" is already resolved, knowingly.**
  `local_subscribers.py:165-173` says in so many words: "this is the FCM send the
  class docstring deferred as 'cloud-only'", and explains why a LOCAL send is
  right (a personal message unconfirmed after TTL means a backgrounded phone never
  acked; push via the locally-cached token, no crossbar, no cloud round-trip, no-op
  without a token so it is safe on every node). What remains is one STALE DOCSTRING
  LINE at `:43` ("no FCM — that's cloud-only"), which is now false. That is a
  comment fix, not a defect.

  **"Also fix the hardcoded generic body" — DO NOT, not as written.**
  `body='You have a new notification'` is DELIBERATE, and the code says why:
  "Generic body — the content renders in-app on open", and
  `build_fcm_v1_message` (`:242`) defaults `privacy_tier_skipped=True`, stamping
  `privacy_notice` so the user is TOLD their message used the central relay. So an
  FCM push is treated as a privacy-tier escalation. Putting message content in that
  body would send it through Google's infrastructure and onto a lock screen —
  weakening privacy, not fixing a bug. Same shape as F4: the plan text asks for
  something that discards a protection the code deliberately holds.
  → OWNER CALL if a richer body is wanted. Middle grounds that do not leak content:
  carry the SENDER's name only; or gate previews behind an explicit opt-in consent
  (which is what this whole plan is building the machinery for). Either way the
  `privacy_tier_skipped` notice must keep firing.

  *Fifth premise issue in this plan, and the second where following the text
  literally would have removed a protection. The pattern is now clear enough to
  state: this plan's DIAGNOSES are good, its PRESCRIPTIONS are not trustworthy
  without reading what the code already decided and why.*
- **F14** four desktop-toast emitters → one (`hart-notify.nix:92-124`,
  `shell_os_apis.py:384`, `tray_handler.py:136-146`, `indicator_window.py:108`).
- **F15** SMTP senders → one — **PREMISE FULLY ACCURATE 2026-09-22, all three
  files AND line numbers correct. Prescription is also right, including its
  caveat.** One refinement from reading them:
  | site | what it is |
  |---|---|
  | `integrations/channels/email_campaign.py:643` | real sender — `smtplib.SMTP(SMTP_HOST, SMTP_PORT)`, `srv.sendmail(...)` at `:655` |
  | `integrations/channels/extensions/email_adapter.py:264` | real sender — `aiosmtplib.SMTP(`, with a SYNC `smtplib.SMTP` fallback at `:282` |
  | `integrations/channels/mailing_list.py:286` | **NOT a sender** — catch-all detection prober: `ehlo` / `mail` / `rcpt` and never `sendmail` |
  So it is **two senders plus a prober**, not three senders. The adapter's
  async+sync pair is one sender with two transports (same F12 lesson: variants for a
  real reason are not duplication), so the fold is 2 -> 1 with the prober documented
  as deliberately separate. The plan already said to keep the prober separate "but
  say so", which is exactly right.
  Care warranted regardless: this path sends to the 77,369-address list, and
  `mailing_list.py` carries measured knowledge (`PROBE_LIARS`, provider behaviour at
  RCPT TO) that must not be lost in a refactor.
- **F16** `localStorage` as a push channel → an event. **PREMISE ACCURATE**
  (2026-09-22), lines are `hevolve/src/App.js:46-48`, not 56-61:
  `localStorage.setItem('agent_proactive_message', JSON.stringify(data))` with the
  comment "Store in localStorage so Agent component picks it up". That is exactly
  the described defect — localStorage used as an inter-component event bus. Safe to
  execute; the only caution is that `active_agent_id` is set alongside it at `:47`
  and `:64`, so the fold must keep whatever reads THAT working.
- **F17** two public-topic allowlists — **AUDITED 2026-09-22. DO NOT "fold to one
  list". The divergence is DELIBERATE and folding it is a SECURITY REGRESSION.**

  Correct locations (the plan's path was wrong): SSE allowlist is
  `core/platform/events.py:127-138` (`_SSE_GLOBAL_PREFIXES`), NOT
  `integrations/social/events.py` (that file is ICS calendar parsing). WAMP side is
  `integrations/social/realtime.py:43` (`_PUBLIC_TOPIC_PREFIXES`) as stated.

  **The plan misread the comment.** It says `:113-118` "admits the divergence is
  unmaintained-by-construction". What `:116-118` actually says:
  > "(The two lists **intentionally differ** elsewhere: WAMP also lists
  > per-conversation chat.social/dm. which are authorized per-subscriber, NOT
  > SSE-global.)"
  The comment aligns the two lists for the INFRA subset (`system.`, `model.`,
  `catalog.`) and documents that they differ on purpose everywhere else.

  **Why folding them leaks.** WAMP authorizes PER-SUBSCRIBER, so it can safely list
  `chat.social.` and `dm.`. SSE broadcasts GLOBALLY with no per-user scoping —
  `_is_sse_global` returns "safe to broadcast without a user_id". Merge the lists
  and per-conversation chat becomes an SSE-global broadcast to every connected
  client, which is exactly the leak this guard was added to stop (`:98`: without it
  "an emit_event that forgot to include user_id leaks the payload (e.g. a personal
  pair-code card) to every connected client").
  → If anything is done here it is to make the SHARED INFRA SUBSET single-sourced
  (`system.`/`model.`/`catalog.`/`resource.`/`app.`), leaving each transport's
  transport-specific entries alone. Not "one list, two projections".

  **A real open item the code already identifies correctly** (`:120-126`):
  agent/goal/memory-scoped topics (`agent.action.completed` ×4882,
  `action_state.changed`, `inference.completed`, `memory.item_added`) are
  DELIBERATELY excluded, because a global SSE broadcast would leak cross-user
  activity metadata on a multi-tenant node. The comment names the right fix — the
  PUBLISHER stamps the owning user_id so the event routes per-user — and says it is
  "tracked separately, NOT bypassed by whitelisting here." That is worth its own
  fold, and it is the opposite of widening a list.

  *SIXTH premise correction, and the THIRD prescription that would have reduced
  safety if followed literally (F4 discards live grants, F13 leaks message content
  to lock screens, F17 leaks per-conversation chat to every SSE client). The
  through-line: this plan's author read COMMENTS and inferred intent, where the
  comments were in fact recording a deliberate decision. Read the code AND the
  reason before folding anything it calls duplicate.*
- **F18** `approval.options` is **dead schema** — **PREMISE VERIFIED 2026-09-22,
  end to end. The cleanest fold left in this tier.**
  Path correction: `integrations/agent_engine/liquid_ui_service.py`, not
  `integrations/social/`.
  - DECLARED `:633` — `'approval': {'props': [..., 'options']}`
  - POPULATED `:1593` — `agent_request_approval()` sets
    `'options': ['Approve', 'Deny', 'Ask me later']` and pushes via `agent_ui_update`
  - IGNORED — `Nunba/landing-page/src/components/AgentOverlay/AgentOverlay.jsx`:
    `ApprovalOverlay` (`:270`, dispatched `:761`) renders the card and never reads
    `data.options`. Only `data.title` and its own buttons.
  So the field is produced with real values on every approval request and no
  consumer looks at it. *Near-miss worth recording: `liquid_ui_service.py:4048` has
  `const options = opts.options || []` and I briefly took it as a consumer — it is
  `dsSelect`, a generic select primitive, unrelated to `approval.options`. Checking
  the enclosing function is what separated a consumer from a coincidence.*
  Prescription ("honour it or delete it") is SAFE and well posed — the first in
  this tier that needs no warning. Honouring it means the agent controls the button
  labels; deleting it means the overlay owns them. A design preference, not a risk.

  **DONE + PUSHED 2026-09-22 — HONOURED, and my own audit above was wrong twice.**
  The caller audit before the fold found what the audit had missed, and it changed
  the answer from "delete" to "honour":
  - **Not one consumer, three.** `AgentOverlay.jsx:270` (Nunba), the desktop shell's
    own JS renderer at `liquid_ui_service.py:7265`, and a server-side HTML fallback at
    `:7402` that draws **no buttons at all** (so labels do not apply there).
  - **Not one producer, two.** `core/agent_tools.py:904` sets
    `'options': ['Keep it', 'Compose another']`. Those are not the three defaults, so
    the field was never dead — it was a real affordance being dropped. The game-sound
    card read *"Have a listen: keep it, or say what is wrong and I will compose
    another"* above buttons saying **Approve** and **Deny**.
  - **The schema was already tested.** `tests/unit/test_bind_game_sound.py:309` pins
    `len(component['options']) == 2`. It proved the dict was built, not that anyone
    could read it — the same shape as the `media` defect its own neighbours record.
  Contract chosen: `options` labels the three decisions **positionally**
  `[approve, deny, defer]`; a missing or non-string entry keeps that button's
  default, so the button SET never shrinks. Two reasons the set must not shrink:
  an approval card is exempt from the overlay auto-dismiss (`:7328`), so dropping the
  third button leaves it unclosable; and `/api/agent/approval` (`:7694`) validates
  `approve|deny|later` and nothing else, so `options` can only ever carry labels.
  Both existing producers already satisfy this reading, so no producer changed.
  Escaping: labels are `_esc`'d at the point of use, because the prop pre-escape at
  the top of `renderAgentOverlay` (`:7063`) walks **string** props only and a LIST
  prop's entries arrive raw.
  **Correction to `1c1881098`'s commit message, made minutes later:** it calls the
  neighbouring list renderer (`:7258-7260`, which interpolates `item.label` into the
  `<li>` body unescaped) "an injection sink". That overstates it. `_a2ui_has_xss`
  (`:107-115`) recurses into **lists** as well as dicts, so script-bearing entries are
  REJECTED server-side before any push — the comment at `:97` says reject-not-escape
  is deliberate, to avoid double-escaping legitimate content. So the unescaped `<li>`
  is a defence-in-depth weakness behind a denylist regex, not a live hole, and my
  `_esc` on the option labels is belt-and-braces rather than the only guard. Worth
  hardening because a denylist has gaps by construction (`:100` lists six tags; a
  `<link>`, `<base>` or `<form action>` carries no `on\w+=`), but it is not the
  emergency the commit message implies. Filed separately, not folded in here.
  Evidence: `tests/unit/test_shell_custom_render.mjs` drives the REAL renderer on a
  DOM shim — 25/25 with 12 new assertions; red-first confirmed by restoring one
  hardcoded label (3 fail). `test_flow_05_events_and_sinks.py` 9/9,
  `test_shell_custom_render.py` 1/1. The Python AST is identical to HEAD once string
  constants are normalised, so no Python path moved. HARTOS `1c1881098`.
  *Lesson for the remaining folds: "declared but nobody reads it" is a claim about
  EVERY reader and EVERY writer. I had checked one of each. Grep the component type,
  not just the prop name.*

**PUSH TIER AUDIT COMPLETE — all seven folds checked against the code.**
With the consent tier already audited, **every fold in this plan now has a verified
or corrected premise.** Nothing here should be executed off the plan text alone
again. Premise accuracy across the tier:
| fold | premise | prescription |
|---|---|---|
| F12 | code right, doc reference phantom | **blocked** — needs Nunba's interceptor migrated first or `/chat` loses thinking traces |
| F13 | paths/names off; "contradiction" already annotated | **half unsafe** — "fix the generic body" leaks content to lock screens |
| F14 | **fully accurate** (4 files, 2 repos) | cross-repo fold, no trap found |
| F16 | accurate (line drift 46-48) | safe |
| F17 | path wrong; comment says the OPPOSITE of the plan | **unsafe** — folding the allowlists leaks per-conversation chat to every SSE client |
| F18 | **fully accurate**, verified end to end | safe, well posed |
| F15 | **fully accurate** (3/3 files + lines) | safe, and its caveat is right |

**Verdict for whoever picks this up.** Four of seven premises were accurate (F14,
F15, F16 with line drift, F18) and three were wrong in ways that matter. Three
prescriptions would cause harm executed literally (F12 strips `/chat` thinking
traces, F13 puts message content on lock screens, F17 leaks per-conversation chat to
every SSE client), and two of those are the plan MISREADING a comment that recorded
a deliberate decision. **Execute F18 or F16 first** — both safe, both self-contained.
F15 next (2 senders -> 1, prober left alone). F14 is safe but cross-repo. F12, F13,
F17 need the owner or a prerequisite, and must not be done as written.

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
- **F11 DONE + VERIFIED LIVE** (`4ad2cea9e`) — peer trust is one signed
  attestation; the path went from inert to actually registering a peer's expert
  model. Plan premise was wrong; see F11 above.
- **F3 merged into F5**, blocked on a dirty `world_model_bridge.py` and carrying
  an owner call (hive queries opt-out → opt-in).
- **F8 CLOSED, NOT FOLDED** (`afe51600a`) — different concern, same word; but the
  audit found a real bypass (a private link published its target to anonymous
  callers) and a duplicate-consent bug, both fixed. See F8 above.
- **F9 DONE + PUSHED** (`077b27332`) — per-agent consents were askable but not
  grantable. Hermetic A/B (`a34e6489f^` vs mine, fresh pinned DB per arm):
  **254 passed / 0 failed / 0 errors in BOTH arms**, so zero regression is
  evidenced rather than asserted. Landed on the remote via the peer sweep before
  I pushed, which is its own lesson about this checkout.
- **F19 DONE + PUSHED** (`828562872`), differently from how it was specified.
  Neither option (a) nor (b): instead `models.py` resolves DB_PATH to a
  per-process temp FILE when running under pytest with nothing configured. That
  covers what per-suite pins could not, because several suites import `models`
  lazily inside a fixture, long after a module-top pin would have run. A temp
  file rather than `':memory:'` keeps the file-backed NullPool semantics every
  suite already runs under. Guarded by `tests/unit/test_consent_sweep_is_hermetic.py`,
  which also asserts the regression risk directly: a process WITHOUT pytest still
  resolves to `agent_data`, so no real node's database was repointed.
- **NEXT after F9: F7**, then F4 (unheld). F5 still held by another session's edits.

### F19 — the consent test sweep cannot serve as a control — **OPEN, filed 2026-09-21**

Not a consent fold; a defect in the instrument every fold here is measured with,
found by being burned by it (see F9's third self-correction).

**Defect.** `integrations/social/models.py` reads `HEVOLVE_DB_PATH` once at import
and caches `DB_PATH` at module level. Suites disagree about who sets it:
several assign `':memory:'` directly, `tests/unit/test_consent_api.py` and
`tests/test_goal_consent_gate.py` set nothing. In a single pytest process the
FIRST importer of `models` therefore decides the database for every suite after
it, and when a file path wins the run mutates `agent_data/hevolve_database.db` —
a real 44MB checked-in-adjacent file — so the next run starts on the previous
run's state.

**Consequence.** Two runs of identical code gave 253 passed / 1 error and
3 failed / 247 passed / 7 errors. Any before/after comparison on this sweep is
uninterpretable, which is exactly how I came to attribute a regression, and then
a fix, to code that was byte-identical in both arms.

**Fix (small, no parallel path).** Do NOT add a second env var or a new fixture
layer. Either (a) make `models.DB_PATH` a function/property read per call so the
env var is honoured whenever it changes, or (b) have the two unpinned suites use
the same `':memory:'` pin the others already use, and add one test asserting no
suite writes `agent_data/hevolve_database.db`. (b) is the smaller blast radius and
closes the cross-contamination; (a) is the real fix for the import-order
sensitivity and should follow it.

**Interim rule, in force now.** Any regression claim in this area runs
`scratchpad/f9_hermetic_ab.sh`: fresh pinned `HEVOLVE_DB_PATH` per arm, the
discriminating grep printed before AND after each arm (peers stash/pop this
shared checkout), and both arms' raw output kept.

**The general class, wider than this instance.** `fix-all-log-observed-issues`
re-measured its own sweep against this finding and its attribution survived a
fresh DB, because its flake has a DIFFERENT carrier: a process-wide
`SessionGuard` 100-action cap that is never reset across suites (an in-process
singleton). Same family — **process-global state surviving a suite boundary** —
different instance. So fixing `DB_PATH` does not make the test tree hermetic; it
fixes one carrier. Others to expect: module-level singletons, caches keyed at
import, and anything read once into a module global from the environment.
*Corollary for attribution: a same-order, same-file-set A/B with only the code
varying still controls for the ordering effect, which is why their conclusion
stands and mine did not — mine had no code difference between the arms at all.*

**Verify each remaining fold's premise before executing it** — F11 taught that
the plan can be wrong about which API exists. Check name, signature, and that the
producer emits what the consumer reads.
