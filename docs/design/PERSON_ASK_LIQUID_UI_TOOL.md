# The server asks the person: Liquid UI as a tool

Owner, 2026-10-27 (verbatim):

1. "the clips upload shd happen agentically and is it not part of liquid ui?"
2. "change the android so that remote tools are invoked and hartos so that liquid ui is asked as a tool by server, same for consent screems and other liquid ui screens remotely in in desktop,"
3. "user might also select clips from desktop the mechanism shd be same agnostic of hartos consumer, liqiuid ui shd also fanout"
4. "same for consents via firebase shd fanout in desktop, android, wamp etc"

Register: hartos-70 (99c39a1e) traced this to H-1005, H-1002, H-1024, H-1022,
H-1030, N-0628, N-0629, H-0986, H-1193, H-0747, H-0753, H-0758, H-0362,
H-0308, H-0360, H-1186, M-0104, M-0107, M-0112, M-0138, N-0302, N-0483,
H-0762 and N-0332, plus these four rules.

## What an ask is

An agent, through a tool, puts ONE question in front of its person. The
question is a Liquid UI component (`COMPONENT_TYPES`). It reaches every
surface the person has: the desktop page, the shell, the phone in the app,
and the phone with the app closed. The person answers on any one surface.
The answer comes back to the agent that asked, and every other surface
drops the card.

It is the same mechanism whichever HARTOS consumer renders it: Nunba's
React tree (desktop and web), the HART OS shell, Android, and later iOS.

## What exists, and is reused (read 2026-10-27)

| Need | Existing piece | Where |
|---|---|---|
| Push one card to every surface, one msg_id | `agent_ui_compose` / `push_agent_ui` | `integrations/agent_engine/liquid_ui_service.py` :2162, :11337 |
| A durable ask with an id, plus Firebase for a closed app | `NotificationService.create(..., push=)`, once per msg_id | `integrations/social/services.py`, `realtime.on_notification` (570b6c43a) |
| The phone-push shape and its reply topic | `consent_prompt_push` (request_id = the row id, `topic_reply` = `com.hertzai.pupit.{user}`) | `integrations/social/consent_service.py` |
| Drop the card everywhere once answered | `NotificationService.mark_read`, which calls `realtime.on_notification_read` (`notification.read` on chat.social and SSE) | `realtime.py` :457 |
| One answer door, with the identity taken from the verified caller | `/api/agent/approval` + `approver_from_request` | `hart_intelligence_entry.py` :11907 |
| The phone's files reaching the store | device-link file channel (offer/put/commit, phone-initiated) | `core/peer_link/file_channel.py` |
| The desktop's files reaching the same store | Nunba `/api/chat-sync/files/push` | Nunba `desktop/file_sync.py` |
| An action that stops for a person and resumes | the pause replies and their predicates | `core/agent_tools.py` :142-208 |

## What is missing (the gaps)

- G1: no tool waits for a person's answer, and the asking agent never gets it.
- G2: an answer is approve or deny only, never files or values.
- G3: there is no component that asks for files.
- G4: the phone overlay's reply (`{reply, request_id}` on
  `com.hertzai.pupit.{user}`) has no reader in HARTOS (570b6c43a, the
  10-26 memory).
- G5: only a consent grant is announced to the other surfaces; a deny, and
  any other answer, is not (H-1024 partial).

## Design

### One ask record

`integrations/agent_engine/person_ask.py` (new; the pending-ask concern has
no owner today):

- `ask(user_id, agent_id, component, *, prompt_id=None, title, body)`:
  1. Refuses a component whose type is not in `COMPONENT_TYPES`.
  2. Refuses a component carrying a `secret` field. Secrets go to the vault
     (`ai_key_vault.request_credential`, H-0986) and never into a tool
     result, a log or the ledger.
  3. Files the ask as ONE `Notification` row (type `agent_ask`) through
     `NotificationService.create(..., push=)`. The row id is the ask_id
     (H-1022), so the Firebase push goes out once (570b6c43a's leg, not a
     second one). The push is in `consent_prompt_push`'s shape:
     `request_id` = ask_id, `topic_reply` = `com.hertzai.pupit.{user}`.
  4. Pushes the component through `push_agent_ui` with `msg_id` =
     `ask:<ask_id>` and `ask_id` on the card.

  Gates that already exist stay ahead of all of this: the hive halt, the
  Liquid UI kill switch and allowlist, and, for consent, `central_lacks`
  (#244).

- `answer(ask_id, answer, answerer_id)`:
  - Only the person asked may answer: the verified caller, never a body
    field.
  - The first answer wins; a later one gets 409 and the answer already
    given.
  - The answer is stored, the row is marked read (that is the dismiss
    fan-out, H-1024), and anyone waiting is woken.
- `wait(ask_id, timeout_s)`: blocks for at most `timeout_s`.
- `answer_of(ask_id)`: the answer once given, so a later turn can read it.

### One resolver, two transport doors (the mobile adapter's own shape)

- HTTP: `/api/agent/approval` with `{ask_id, answer}`. This is the desktop
  page and the shell. When the body carries `ask_id`, the route calls
  `person_ask.answer`. Every existing branch keeps its shape and its
  identity rule: commerce, `game_sound:` (#240) and capability consent.
- WAMP: the reader of `com.hertzai.pupit.{user}` replies `{reply,
  request_id, answer}` (G4). It is the phone's door, for an app that is
  open or one woken by Firebase, on the LAN or off it, and for whichever
  node asked: each node resolves only the ask_ids it holds.

Both doors call `person_ask.answer`. There is no second resolver.

### The tool

`ask_person(component, wait_s)` is available to every agent, with no
per-agent code. It files the ask and waits up to `wait_s`. If the answer
comes in time, it is the tool's result. If not, the tool returns a held
reply. Like a help pause, a held reply is neither work done nor a failure,
and the agent reads the answer with `answer_of` on its next turn.

### `file_request` (new `COMPONENT_TYPES` entry)

`{title, purpose (reel_source|logo|song|chat_attachment), accept
(video|image|audio), max_files, prompt_id}`.

Each surface uploads the person's picks to `core.user_file_store` with that
purpose and prompt_id: the phone over its file channel, the desktop page
through file_sync. It then answers `{xids: [...]}`. The agent's existing
`get_user_uploaded_file` reads those files, because they are bound to the
agent like any upload.

### Consent (slice S4, with hartos-f3)

`request_consent` files its ask through `person_ask.ask`, with the
UserConsent row id as the ask_id. `record_capability_decision` stays the
ONE writer, and `central_lacks` stays ahead of everything.

## Open owner decision

- H-1193 (owner 09-30: "why is it not behind a consent which naturally gets
  asked to user?"). Phone pushes should be gated by the person's consent,
  not by an env var. 570b6c43a's Firebase leg is gated by
  `should_start_background_services`, and every ask inherits that gate.
  hartos-70 is raising it with the owner. This design does not change the
  gate.

## Slices, each test-first and live-proven

- S1 HARTOS: `person_ask`, the HTTP door, `file_request`, the tool. Unit
  tests on the real NotificationService, an in-memory social DB and a
  recorded push leg. Live: an ask on the installed desktop shows on the
  desktop page and on the phone (Firebase or WAMP), and an answer from one
  of them resolves the tool and drops the card on the other.
- S2 Nunba AgentOverlay: renders `file_request` (a file input, the
  file_sync push, then the answer), answers any card that carries
  `ask_id`, and drops the card on `notification.read`.
- S3 Android: renders the ask in the app (approval and `file_request`): the
  system picker, then DesktopFiles upload, then the reply on
  `com.hertzai.pupit.{user}`. A Firebase ask opens the app on the ask.
- S4 consent folded onto the ask (with hartos-f3).
- S5 the journey: agent 342's step 2 asks for clips, the person picks them
  on the Pad or the desktop, and step 2 moves on.
