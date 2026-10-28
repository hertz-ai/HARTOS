# Break the Ice, end to end on two phones against the local Nunba desktop

The acceptance checklist for priority board outcome 1 (P1): consent, match,
draft, edit, explicit send, recipient receives, reply, on Android and iOS,
both phones talking to one local Nunba desktop (HARTOS in-process, flat
topology). Central is not part of this run.

Written 2026-10-26 before the run. Every verdict below is **not run** until
the two-phone run fills it in. Labels follow `USER_JOURNEYS_LIVE_2026-10-13.md`:
**measured** (an HTTP response, a DB row read back, or a screen seen on the
device), **read** (code), **not checked**. Evidence levels follow the
operating plan: unit / integration / simulator / physical device /
cross-device / deployed / end-to-end. This repository is public, so the
record names no private addresses, hosts or accounts.

## Where the steps were already written

- Backend steps 1 to 3 and 5: Nunba `tests/journey/PRODUCT_MAP.md` J200
  (discoverable, 18+ claim, TTL), J203 (BLE sighting), J204 (mutual like
  creates the match), J205 (one-sided like never leaks), J207 (draft), J208
  (central refuses a draft without cloud consent; central only), J209
  (approve), J210 (decline).
- What J209 does not cover and this branch adds: approve posts the approved
  text into a DM and returns its `conversation_id`
  (`integrations/social/encounter_api.py:860` `_send_icebreaker_as_dm`,
  `:944` `icebreaker_approve`), and GPS matches can break the ice too
  (`icebreaker_service.py:173` `find_icebreaker_match`, `kind='proximity'`).
- Steps 4, 6 and 7 (edit saved, recipient receives, reply arrives) were
  written nowhere before this file.

## Build under test (fill at run time)

| Part | Branch | SHA at writing | SHA at run |
|---|---|---|---|
| HARTOS (in Nunba desktop) | `encounters-chat` (PR #147) | 17d86d490 | 016074c39 (2026-10-28) |
| Nunba desktop | `fix/freeze-validate-diagnostics` (PR #50) | f4c32fb | 7f5dd97 (2026-10-28) |
| Android app | `feat/break-the-ice` | fbe362bb | native fbe362bb, JS e788dc7c via Metro (2026-10-28) |
| iOS app | `v1_macos` (PR #2) | ed709a7 | ed709a7 (installed build) |

Devices: one physical Android phone, one physical iPhone, same network as the
desktop. Accounts: two real accounts, one per phone. Server flag
`conversations` must be on (approve answers 503 without it, unit
`test_gps_icebreaker_needs_conversations_flag`).

## Known constraint before the run

A phone signs in to the local desktop with a signed device token, admitted
only when the desktop owner approves that phone, and the phone's user must be
the desktop owner (`integrations/social/auth.py`, unit
`tests/unit/test_social_device_auth.py`). Two different people on one desktop
therefore needs a second-account route that does not exist yet. Do not work
around it with a borrowed or forged identity; if it blocks step 6, record
step 6 as BLOCKED with this reason.

## The checklist

| # | Step | Setup | Expected | Code and existing test | Evidence level reached | Device / SHA | Label | Verdict |
|---|---|---|---|---|---|---|---|---|
| 1a | Phone admitted by the desktop | Phone: Privacy, Connect to the desktop address | First call answers 403 `consent_pending` with "Allow this phone in the desktop privacy settings."; owner approves on desktop Privacy; next call 200 | `auth.py` require_auth device branch; `test_social_device_auth.py`, `test_device_access_gate.py` | physical device (Android); iPhone user-reported | Android + iPhone; desktop 7f5dd97 + HARTOS 016074c39 | measured | WORKS (partial). Each phone filed one `device_access` ask (DB rows read 10-26); the owner clicked Allow on desktop Privacy, granted rows read back 10-28; the Android phone then held open connections to the desktop's port and Encounters loaded with no pending notice (screen seen). The 403 `consent_pending` body itself was not captured. iPhone: owner reports Encounters open and scanning; its connection was not tied to the device by measurement |
| 1b | Discoverable consent | Encounters: tick 18+, turn Discoverable and Location on | `POST /api/social/encounter/discoverable` 200, then `GET` returns `enabled=true`, remaining TTL > 0; without the 18+ tick 403 | `encounter_api.py:389` set_discoverable; J200, `test_encounter_api.py`, `test_encounter_privacy.py` | physical device | Android + iPhone, same builds as 1a | measured | WORKS (partial). `discoverable_prefs` row read back: `enabled=1`, `age_claim_18=1`, expires 4 h after enabling; `location_pings` arrive every 30 s; both phones show Scanning. Not measured: the 403 without the 18+ tick |
| 2a | Match by GPS (Nearby) | Both phones discoverable, near each other, both reveal | `/api/social/encounters/proximity-matches` lists the pair; after both reveal, status `matched`; card shows "Break the ice" | `api_gamification.py:1489` location-ping, `:1530` proximity-matches, `:1543` reveal | read only | | measured | BLOCKED 2026-10-28: both phones are signed in as the desktop owner, so one `discoverable_prefs` row and one user's pings; 0 sightings, 0 matches. Needs the second-account route (see Known constraint) |
| 2b | Match by BLE | Both phones discoverable with BLE on, mutual like | `POST /encounter/swipe` returns `match_id` to both; one-sided like returns `match_id:null` | `encounter_api.py:554` sighting, `:631` swipe; J203, J204, J205 | unit | | read | not run |
| 2c | The old "BLE match not found" on a GPS match | Tap Break the ice on a GPS match | Draft opens; no `BLE match ... not found` (main looked up BLE only; this branch looks up the `kind` the card sends) | `icebreaker_service.py:173`, `encounter_api.py:817` `_icebreaker_kind`; `test_gps_match_gets_a_draft`, `test_pending_gps_match_has_no_icebreaker` | unit | | read | not run |
| 3 | Draft generated and shown | Tap Break the ice | `POST /encounter/icebreaker/draft {match_id, kind}` 200 with `draft` and `alt_drafts`; sheet shows the draft in an editable field; a server error shows as an error, never as a draft | `encounter_api.py:901`; J207; app sheet `IcebreakerDraftSheet.js` (Android `components/CommunityView/components/Encounters/`, iOS `js/shared/components/CommunityView/components/Encounters/`) | unit | | read | not run |
| 4 | Edit saved | Change the draft text in the sheet | The edited text, not the AI draft, is what Send posts (`approveIcebreaker(match.id, editedText, kind)`) | Android `IcebreakerDraftSheet.js:217`; sheet tests in both apps | unit | | read | not run |
| 5 | Explicit send | Leave the sheet open without tapping Send, then tap Send | Nothing is sent before the tap; on tap `POST /encounter/icebreaker/approve` 200 with `conversation_id`; a second send answers 409; DirectChat opens with the opener | `encounter_api.py:944`; J209; `test_gps_icebreaker_becomes_first_dm_message`, `test_gps_icebreaker_only_once`, `test_ble_icebreaker_also_lands_in_dm` | unit | | read | not run |
| 5b | Decline sends nothing | Open the sheet, Decline with a reason | 200; no DM created | `encounter_api.py:1026`; J210; `test_gps_decline_sends_nothing` | unit | | read | not run |
| 6 | Recipient receives | Second phone, other account, same desktop | The opener appears as the first message of the same conversation (`GET /api/social/conversations/<id>/messages`) and in the recipient's chat list | `api_conversations.py:106`; no test covers the second device | none | | not checked | not run |
| 7 | Reply arrives at the sender | Recipient replies in DirectChat | `POST /api/social/conversations/<id>/messages` 200; the sender sees the reply in the same chat | `api_conversations.py:163`; no test covers this | none | | not checked | not run |

## What passing means

All rows WORKS at physical-device level on both phones, with step 6 and 7
cross-device, recorded with the four SHAs above. Then a 30 to 90 second demo
(match, Break the ice, edit, Send, the other phone receives, reply), SHA and
date in the last frame, filed in the demo catalog, and an encounter row added
to `docs/VERIFICATION.md`. Anything short of that is reported at the level
measured, with the blocker and its owner.
