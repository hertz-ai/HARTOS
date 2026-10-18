# A game's sounds: generated once, matched exactly, correctable

Status: **draft for the owner's approval**. Written 2026-09-21 after the
owner stopped the implementation to settle the design. Nothing in
"Open decisions" is built until it is answered.

## 1. What this is for

An agent plays a kids game with a child and sounds it: the music under
the game, a cheer for a correct answer, a kind note for a wrong one, a
fanfare at the end. Those sounds are **generated**, not shipped, and the
goal is:

- the first time a game reaches a given state, the sound for that state is
  composed and **remembered** (the owner's word: *prewalk is the
  memoization step*);
- every later time that same state is reached — later in the run, in a
  later run, at another level, or by someone else reusing the agent — the
  **same artifact** is replayed, never composed again;
- when a sound is wrong, a person can **correct** it.  The agent's
  creator's correction is what everyone hears afterwards; anyone else's
  is what they alone hear (§5, owner 2026-10-17);
- a hit is a hit and a miss is a miss, with **no fuzzy matching** and no
  silent near-misses.

## 2. Vocabulary

| Term | Meaning |
|---|---|
| **state** | A moment a game can reach that can be sounded: `bgm`, `intro`, `correct`, `wrong`, `streak`, `complete`, `starEarned`, `cardFlip`, `matchFound`, `dragStart`, `dragDrop`, `countdownTick`, `countdownEnd`, `tap`. Same names the app's game renderer already accepts over its bridge, so an agent-led sound and a game-led sound are one sound. |
| **artifact** | One generated audio file, addressed by URL, with the prompt that made it. |
| **memo** | The record that binds a key to an artifact. |
| **prewalk** | Filling the memo on first use. Not a batch job. |
| **scope** | Which key a memo belongs to (§4). |

## 3. The key

```
(owner_user_id, prompt_id, game_id, state, level?, variant?)
```

- **owner_user_id** — as built (§5), not a field of the key but the
  store the memo lives in: the agent's own store for what everyone hears,
  a person's own `{user_id}_{prompt_id}` store for their choices.  Two
  users reusing the same agent share its approved sounds (§6) but
  corrections are theirs alone.
- **prompt_id** — the agent. Sounds belong to the agent, not to the app,
  so reuse carries them.
- **game_id** — the game's own id as the app knows it (`config.id`, or a
  brief's `brief_id` for a generated game).
- **state** — from the table above. Anything else is rejected, not guessed.
- **level** — optional. Absent means the sound serves every level.
- **variant** — optional, reserved for a deliberate second take
  (§6 correction). Absent for the first.

The key is built by one function on each side and never by string
concatenation at a call site.

## 4. Scope and matching (the accuracy rule)

Lookup tries the **declared scopes in order** and stops at the first
exact match:

1. `game_id + state + level` — this level's own sound.
2. `game_id + state` — the game's sound for that state, shared by levels.
3. `template + state` — the sound for a family of games (e.g. every
   `multiple-choice` game), when the agent bound one.
4. miss.

Rules that make hits and misses exact:

- **Only exact keys match.** No prefix matching, no nearest-mood, no
  "close enough". A level asks for its own key and falls back only
  through the ladder above.
- **Every answer names what matched**: `matched: "mine" | "level" |
  "game" | "composing" | "miss"` ("mine" is the person's own memo, §5;
  "template" joins them if §4.3 is built). The caller logs it; tests
  assert it.
- **A miss composes exactly one artifact** and memoizes it **under the
  key that was requested** (not under the fallback it would have used),
  so the second identical request is a level‑1 hit.
- **A path never walked is simply a miss.** No pre-generation of states a
  game may never reach; no empty records that later read as hits.
- **A composition in flight is itself a memo** (`status: composing`, with
  the task id). A second request for the same key joins it rather than
  starting a second composition.

## 5. Where the memo lives

**Owner ruling 2026-10-17** (built, task #240): *"background music
selection can be altered by anyone for my agent ... the unique key is
userid_promptid"*, and *"background music created once gets reused ...
should not be conflated with"* it.  So there are two memos with one shape:

- **The agent's memo** (below): every composed take.  A take is composed
  once and replayed for everyone; an empty slot is filled by whoever's
  turn reaches it first.
- **A person's own memo**: `agent_data[person_store_id("{user_id}_{prompt_id}")]['games']`
  (`core.agent_tools.person_store_id` / `person_store_for`: the session
  key itself when it is safe as a file name and lower case, else a
  `p<digest>` of it), the same shape.  It holds their choices: every take
  they turned down (the last one and, in `previous_takes`, the ones
  before), a take they kept, a take of their own.
  Their lookup tries it first and skips any agent take they turned down.

Only the agent's creator (`core.agent_tools.is_agent_creator`, from
`prompts/<id>.json`'s `creator_user_id`) chooses for everyone, in any
mode, because the creator's review ("Evaluation Mode") runs through reuse.
Anyone else's approval, rejection or replacement is written in their own
memo only, by the agent's tools and by the review card
(`/api/agent/approval`, which uses the caller's verified identity and
answers 401 without one).  The card is answered outside any chat turn,
so it writes the store files directly and then every recipe-pipeline
cache drops its copy (`core.cache_loaders.forget_cached_store`); a live
session would otherwise keep serving the old store and write it back over
the answer.  The per-person `'mine'` sub-slot that used to live INSIDE the
agent's memo is gone; measured 2026-10-17, no stored agent file on the
desktop held one.

The agent's memo, in the agent's own saved data, beside everything else
the agent keeps (persisted per `prompt_id`):

```
agent_data[prompt_id]['games'][game_id]['sounds'][key] = {
    url, prompt, state, level?, variant?,
    composed_at, approved_at?, rejected_at?, rejected_reason?,
    task_id?            # while composing
}
```

Why there and not a cache: a cache may evict; a memo may not. The app's
media cache and the node's media cache stay caches in front of it, and
losing either costs a download, never a regeneration.

## 6. Correction

A generated sound can be wrong: too loud, too sad, wrong instrument,
wrong for the age. Three moments, three behaviours:

1. **In CREATE review (Evaluation Mode).** The reviewer hears it and
   says so in chat. `approve_game_sound(game_id, state, approved=false,
   reason)` marks the memo rejected with the reason, and the next
   composition for that key **includes the reason in the prompt** and
   stores the result as the next `variant`. The rejected artifact is kept
   (not deleted) so a reviewer can go back to it.  A verdict lands on the
   key the ladder matched, so a rejection given while a level played is
   the game's; a level composed afterwards answers it too
   (`core.game_sound_memo.rejected_take` reads the level's key, then the
   game's).
2. **In REUSE, by anyone but the agent's creator.** Their correction must
   not silently change what everyone else hears. It writes a memo **in
   their own store** (§5), at the key of the take they were hearing, which
   their lookups match before the agent's. The agent's approved sound is
   untouched.  (The creator's own review runs through REUSE too, and their
   word is the agent's, as in 1.)
3. **Never automatically.** Nothing rewrites a memo because a model
   thought it could do better.

A turned-down take stays turned down.  Every write at a key keeps the
takes the record it replaces held as turned down
(`core.game_sound_memo.set_game_state_sound_at` / `set_game_state_sound`,
built by `turned_down_takes`).  So a person's own take finishing over a
verdict they gave while it composed still skips the take they turned
down.  And a rejection written at a key where the person's own take is
still composing keeps that composition's task (`IN_FLIGHT_FIELDS`), so
the next call finds the job instead of queueing a second one (both
measured 2026-10-18 at f572fd775).  A take they KEEP ends their own
composition there: the kept record holds a url and no task.  Nothing the
composing call holds is written over a take that landed while it
composed, whether kept on a card or filled by another turn: not the
task or the submit note its submit returns (the submit can wait two
minutes), not the take it finishes.  The call asks this before each of
those writes with the same lookup it began with, so a take kept at the
game's key also ends a composition for one level; the job it ends is
cleared, and the composer, which cannot be asked to stop, simply has
its take left unbound.  The same take, bound first by another call
polling the same task, is left as it is.  The setters are the one
source of a take's history; the composing tool builds none of its own.

`approved_at` is what REUSE prefers; an unapproved memo still plays (a
game is not held hostage to review) but is reported as unapproved so a
surface can mark it.

## 7. Who composes

One capability: `generate_media(output_modality='audio_music')`, the tool
CREATE and REUSE agents already hold. The node's kids media route
delegates to it (landed, §9). Nothing else calls an engine directly.

## 8. The flow, end to end

```
game reaches state S in game G at level L, under agent A
        │
        ├─ app: memo lookup (§4) via the node, naming A, G, S, L
        │        hit  → URL → local cache → play
        │        miss → node composes once (§7), memoizes under the asked key,
        │               answers `composing` until ready; the game stays silent
        │               rather than blocking, and plays it next time it is reached
        │
        └─ agent guidance: the agent names a state in its reply's
           `dynamic_data` (the map the Liquid overlay already reads), the app
           resolves that state through the SAME memo, so an agent-led sound and
           the game's own sound are the same artifact.
```

## 9. What is already built (2026-09-21)

Committed and tested:

| Where | Commit | What |
|---|---|---|
| Nunba | `cab0f300` | The kids media route composes through the agents' capability instead of calling engines itself. 53 tests. |
| RN | `dcbd528a3` | One resolver for a game's music; the shell plays it. The old lookup could never hit. 5 tests. |
| HARTOS | `b5927ebd2` | `bind_game_sound` / `get_game_sound`: compose once, memoize against the agent. 6 tests. |
| Nunba | `001027bd` | The route answers from the agent's memo when the app names the agent and the game. 5 tests. |
| RN | `fc1814016` | The app names the agent and the game, and caches per game. 2 tests. |
| HARTOS | `6aa80a1f8` | `approve_game_sound`: the reviewer's word decides what reuse plays. 3 tests. |

Uncommitted, written before the spec and **to be reconciled with it**:

- HARTOS `core/agent_tools.py` — a state vocabulary (`GAME_STATES`) and
  state-keyed accessors, with `state` on the three tools. Matches §2–§3
  except: no `owner_user_id`, no `variant`, no `template` scope, and the
  level fallback was half-written (the owner stopped it mid-edit).
- RN `shared/agentGameSound.js` + `LiquidOverlay.js` — the agent naming a
  state in `dynamic_data` and the overlay playing it (§8, second branch),
  plus the game's context on the outgoing turn. Matches §8; needs §4's
  `matched` reporting.

## 10. What is missing against this spec

1. ~~`owner_user_id` in the key, and the user-scoped correction of §6.2.~~
   Built 2026-10-17 as the two memos of §5.
2. `variant`, and composing with the rejection reason (§6.1).
3. The `template` scope (§4.3) — and whether it is wanted at all.
4. `matched` in every answer, and the logging and tests that make §4
   checkable.
5. The node route taking `state` and `level` (it takes only the game today).
6. The app resolving a state, not only the background music.
7. A live proof on a device: first play composes, second play is a hit,
   reuse by another user hears the approved sound.

## 11. Open decisions for the owner

- **Template scope (§4.3)**: worth it, or do sounds belong to a game only?
  It is the difference between one composition per game family and one per
  game.
- **Silence while composing**: a first play is silent for that state. Is
  that acceptable, or should the app fall back to its bundled sound for
  that state until the artifact arrives? (The bundled sounds exist today.)
- ~~**Correction in reuse (§6.2)**: user-scoped override, or should a
  reused agent be fixed as approved and corrections go back to the author?~~
  Answered 2026-10-17: user-scoped, under `{user_id}_{prompt_id}`; only
  the creator changes the agent's (§5).
- **How many states are worth composing.** All fourteen per game is a lot
  of generation. A shortlist (`bgm`, `correct`, `wrong`, `complete`)
  covers most of what a child hears.
