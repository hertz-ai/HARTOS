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
drops the card. Reading the ask in the bell is not an answer and drops
nothing (review of Nunba 8d2b0c54, F1).

It is the same mechanism whichever HARTOS consumer renders it: Nunba's
React tree (desktop and web), the HART OS shell, Android, and later iOS.

## What exists, and is reused (read 2026-10-27)

| Need | Existing piece | Where |
|---|---|---|
| Push one card to every surface, one msg_id | `agent_ui_compose` / `push_agent_ui` | `integrations/agent_engine/liquid_ui_service.py` :2162, :11337 |
| A durable ask with an id, plus Firebase for a closed app | `NotificationService.create(..., push=)`, once per msg_id | `integrations/social/services.py`, `realtime.on_notification` (570b6c43a) |
| The phone-push shape and its reply topic | `consent_prompt_push` (request_id = the row id, `topic_reply` = `com.hertzai.pupit.{user}`) | `integrations/social/consent_service.py` |
| Drop the card everywhere once answered | `realtime.on_notification` with `{type: 'ask.answered', ask_id}`, the fan-out consent answers use (consent.granted / consent.revoked). NOT `notification.read`: the bell's "Mark all read" and a click on a row send that too | `realtime.py` :425 |
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
  1. Refuses a component that asks for a secret by its structure: a field
     marked `secret`, typed `password` in any spelling (`type`,
     `input_type`, ...), or named the way a secret is named in any spelling
     (`userPassword`, `DBPassword`, `APIToken`, `API-Key`, `password2`:
     `security.edge_privacy.secret_key_form`, then `is_secret_key`, the
     scrub's own rule, extended by the words only the vault asks for: `otp`,
     `pin`, `cvv`, `card_number`, `passcode`, `seed_phrase`, ...). Secrets go
     to the vault (`ai_key_vault.request_credential`, H-0986) and never into
     a tool result, a log or the ledger.

     An ask's words (a label, a placeholder, a title, a body) are not read
     for secrets. A word list over them refused 36 of 52 honest asks ('the
     first pass of your reel', 'Delivery address PIN code', 'Upload your
     boarding pass') and still let 32 of 47 secret phrasings through
     (review of e6eed01ae, measured). The owner's ruling of 2026-10-20 for
     reading words without the model is the exact word or the model. So a
     plain text field named `f1` and labelled 'Password' is NOT caught here;
     the tool's description tells every agent it is never for a password or
     a key.
  2. A `file_request` names a purpose the store has (`INBOUND_PURPOSES`)
     and an agent to give the files to (`prompt_id`). It asks for one file
     of a purpose the store keeps one of (a logo), else at most a turn's
     attachments.
  3. The same question already open is the same ask (no second push). A
     person has at most `MAX_OPEN_PER_PERSON` (3) open asks.
  4. Pushes the card FIRST, through `push_agent_ui`, with `ask_id` on it;
     `agent_ui_compose` keeps a card's `ask_id` as its `msg_id`, so every
     copy on every surface is the ask. The card's gates are the human's and
     come before everything: the Liquid UI kill switch, the hive halt,
     unsafe content, the allowlist (a type an agent registered at runtime
     counts) and the rate cap. A refused card means no row and no push
     (`not_shown`).
  5. Files the ask as ONE `Notification` row (type `agent_ask`, bell title
     "Your agent asks you something") through
     `NotificationService.create(..., push=)`. The row id is the ask_id
     (H-1022), so the Firebase push goes out once (570b6c43a's leg, not a
     second one). The push carries the ask's id as `ask_id` (central's
     relay replaces `request_id`) and no `topic_reply`, because no reply
     topic is an answer door (see the doors section). A row that cannot be
     written takes the card back down from every surface it reached: the
     shell's store, and `ask.answered` for the phone's topic and the page
     (`not_filed`).

  For consent (slice S4), `central_lacks` (#244) stays ahead of all of it.

- `answer_ask(ask_id, answer, answerer_id)`:
  - Only the person asked may answer: the verified caller, never a body
    field.
  - An answer is a small, shallow object (16 KB, 4 levels). One that
    carries a secret (`edge_privacy.redact_secret_leaves` finds one, or a
    key in it names a secret or a credential, in any spelling, with a value)
    is refused; the same name with nothing under it is taken. A plain value
    under an everyday name (a one-time code typed into a field called
    `code`) cannot be told from any other answer, and nothing at ask time
    keeps it out either (above). A `file_request` answer is `{xids: [...]}`: each pick must be
    in the person's own inbox, held for the ask's purpose, and is then
    bound to the asking agent with the ask as its turn
    (`core.chat_attachments.bind_inbound`, the one binding rule); else 400
    `files_not_received` and the ask stays open.
  - The first answer wins; a later one gets 409 and the answer already
    given.
  - The answer is stored and anyone waiting is woken. Then `ask.answered`
    goes to every surface (they drop the card, H-1024), the card leaves the
    shell's store (`withdraw_agent_ui`) and the row is marked read (the
    bell stops listing it).
- `wait_for_answer(ask_id, timeout_s)`: blocks for at most `timeout_s`.
- `ask_state(ask_id, user_id)`: `open`, `answered` (with the answer),
  `expired` (her ask, no longer held here: a restart, or an hour after its
  answer; ask again) or `not_found` (not hers). Never "waiting" for an ask
  that is gone.
- `open_file_request(ask_id, user_id)`: the purpose of her OPEN file ask,
  for the door that takes the files (Nunba's `/api/chat-sync/files/push`
  takes a file for an ask only while it is open, for her, under its
  purpose).

### One resolver, one authenticated door

Every surface answers through `/api/agent/approval` with `{ask_id,
answer}` and its own Bearer, and the route calls `person_ask.answer_ask`
with the VERIFIED caller (`approver_from_request`), through
`person_ask.answer_from_request`: the backend's route and the shell's own
route (:6800, whose `_postApproval` sends the ask's id) take that branch
first. Every existing branch
keeps its shape and its identity rule: commerce, `game_sound:` (#240) and
capability consent.

- The desktop page and the shell answer over HTTP on the node itself.
- The phone answers through its one client. Off the LAN, the call rides
  the device link: a mobile-adapter alias row (`aliases.py`) maps it to the
  desktop's route, and the adapter carries the phone's bearer, checked
  against the link's user. An ask the desktop does not hold answers 404,
  and the phone's DesktopRouteInterceptor retries on the cloud, so central's
  own asks reach central.

NOT a door, by decision (2026-10-27, security): the reply topic
`com.hertzai.pupit.{user}`. Central's router is anonymous (realm1, measured
2026-10-06), so anyone can publish `{reply, request_id}` there. And the
ask's id is not a secret: it travels on the person's social topic on the
same router. A reader that turned those replies into answers or consent
grants would accept forged ones. The phone overlay's existing pupit reply
(G4) moves to the door above, and is not read.

This supersedes the June steward spec's "a HARTOS subscriber maps the reply
to a ConsentService grant or decline by the row id"
(docs/STEWARD_INSTRUCTION_LOG.md:20327-20328), for that reason. Do not
build that subscriber. An anonymous-realm topic may carry an ask OUT but
never an answer or a grant IN. The answerer is always the caller's verified
identity, never a body field or a topic payload, which is the rule the
game_sound branch already follows. The id in an answer only names WHICH ask
is answered, never WHO answers (hartos-f3, consent lane, agreed
2026-10-27). Pinned by tests: a reply published on the topic answers
nothing, and an answer whose Bearer is not the asked person is refused and
leaves the ask open.

Not checked (outside this lane, noted by hartos-70): whether central's
chatbot_pipeline (confirmation.py, on central's host) acts on pupit
replies. If it does, it carries the same anonymous-router exposure.
consent_prompt_push still names `topic_reply` (consent_service.py:313).
That field goes in S4, in the same change that moves the phone's
ConsentOverlayService to the door, so the overlay never publishes replies
that nobody reads.

### The tool

`ask_person(component, wait_seconds, ask_id)` is available to every agent,
with no per-agent code. It files the ask and waits up to `wait_seconds`
(20 by default, 60 at most). If the answer comes in time, it is the tool's
result. If not, the tool returns a held reply. Like a help pause, a held
reply is neither work done nor a failure, and the agent reads the answer
with `ask_person(ask_id=...)` on its next turn (`ask_state`).

Not done yet: an answer that comes after the turn ended does not start a
new turn by itself; the agent reads it when it next runs.

### `file_request` (new `COMPONENT_TYPES` entry)

`{title, purpose (reel_source|logo|song_audio|chat_attachment), accept
(video|image|audio), max_files, prompt_id}`.

Each surface uploads the person's picks to `core.user_file_store` for the
ask: the desktop page through file_sync, naming the ask's id (the door
takes the purpose from the open ask, not from the client), the phone over
its file channel. It then answers `{xids: [...]}`, and answer_ask binds
them to the agent that asked (above).

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
  file_sync push with the ask's id, then the answer), answers any card that
  carries `ask_id`, and drops the card on `ask.answered` (never on
  `notification.read`). An open ask's card is not pushed out by newer
  cards.
- S3 Android: renders the ask in the app (approval and `file_request`): the
  system picker, then DesktopFiles upload, then the answer through
  `/api/agent/approval` with its Bearer (the device-link alias row, else
  direct). A Firebase ask opens the app on the ask.
- S4 consent folded onto the ask (with hartos-f3).
- S5 the journey: agent 342's step 2 asks for clips, the person picks them
  on the Pad or the desktop, and step 2 moves on.
