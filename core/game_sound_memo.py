"""A game's generated sounds, and the memo that stops them being made twice.

The rules are the owner's, written down in
docs/internal/GAME_SOUND_MEMOIZATION_SPEC.md: an exact key, a declared
ladder, every answer naming what matched, and a correction that belongs to
whoever made it.

This lives on its own so the one matcher serves everyone who needs it --
the agent's tools here, and the node's kids media route -- rather than
each growing a ladder of its own.  Importing it costs nothing.

Two memos, one shape (owner ruling 2026-10-17).  A sound composed once is
replayed for everyone who uses the agent: that is the AGENT's memo, in the
agent's own store.  Which take a person keeps or turns down is a choice,
and a choice belongs to the person: that is their OWN memo, in their own
{user_id}_{prompt_id} store, never inside the agent's.  Only the agent's
creator chooses for everyone (core.agent_tools.is_agent_creator); that is
decided by the callers, which pass the person's memo as ``own``.
"""

# The states a game passes through, and how each is described to the
# composer.  The memo key is (agent, game, state): the same state always
# replays the artifact composed for it the first time, and how each is described to the composer.
# One vocabulary for every game: the app's own game renderer accepts the
# same names over its bridge (DynamicGameRenderer ALLOWED_SOUND_EVENTS), so
# an agent-led sound and a game-led sound are the same sound.
import logging
import time

logger = logging.getLogger(__name__)


GAME_STATES = {
    'bgm': ("{what} — {mood} background music for a children's learning "
            "game, gentle loop, no vocals"),
    'intro': "{what} — a short {mood} fanfare that opens a children's game",
    'correct': "{what} — a bright two-note chime for a correct answer",
    'wrong': "{what} — a soft, kind two-note sound for a wrong answer, never harsh",
    'streak': "{what} — a rising sparkle for a run of correct answers",
    'complete': "{what} — a {mood} little fanfare for finishing a game",
    'starEarned': "{what} — a single shining chime for earning a star",
    'cardFlip': "{what} — a light flip, like a card turning over",
    'matchFound': "{what} — a satisfied click for two things matching",
    'dragStart': "{what} — a soft pick-up sound",
    'dragDrop': "{what} — a soft set-down sound",
    'countdownTick': "{what} — a quiet tick, one second of a countdown",
    'countdownEnd': "{what} — the last beat of a countdown, {mood}",
    'tap': "{what} — a small tap, a child touching the screen",
}


#: How long each state's sound should be, in seconds.  MEASURED
#: 2026-09-22: every state -- including "a bright two-note chime for a
#: correct answer" -- was composed at 60 seconds, because the tool asked
#: for 60 regardless of state.  A cue that outlasts the moment it marks is
#: worse than no cue.  bgm is a loop and may be long; everything that
#: marks an instant is short.  Kept beside GAME_STATES so a new state gets
#: a prompt and a length in the same place.
GAME_STATE_DURATIONS = {
    'bgm': 30,
    'intro': 6,
    'complete': 6,
    'streak': 3,
    'countdownEnd': 3,
    'correct': 2,
    'wrong': 2,
    'starEarned': 2,
    'matchFound': 2,
    'cardFlip': 1,
    'dragStart': 1,
    'dragDrop': 1,
    'countdownTick': 1,
    'tap': 1,
}
assert set(GAME_STATE_DURATIONS) == set(GAME_STATES), (
    'every state needs a length: ' + str(set(GAME_STATES) ^ set(GAME_STATE_DURATIONS)))


#: How long after a submit whose reply timed out this key still counts as
#: composing.  AceStep's own /v1/stats reported avg_job_seconds 907 on a
#: shared GPU; the value is a floor on duplicates, not a promise about
#: completion.  Owned here, beside the matcher that answers "composing",
#: so the agent's tool and the node's route agree (hartos-3a F8).
SUBMIT_COOLDOWN_S = 600


def submit_in_flight(record):
    """A submit whose reply timed out, recent enough to still be queued.
    A time that is not a number is no submit, and says so: it raised out
    of a keep (review of c67afc26e, F-2).  The one reading of it, for the
    matcher, a keep and bind_game_sound's cooldown (review of 14c6bbd5f,
    F-C: bind read the field itself, unguarded)."""
    submitted = record.get('submitted_at')
    if not submitted or record.get('url'):
        return False
    try:
        return time.time() - float(submitted) < SUBMIT_COOLDOWN_S
    except (TypeError, ValueError):
        _log_corrupt(('submitted_at', submitted),
                     "a game sound's submitted_at %r is not a time; read as none",
                     submitted)
        return False


def _composing(record):
    """A composition under way at this record, with no take yet: its task,
    or a submit still inside the cooldown.  The one reading of it, for the
    matcher's 'composing' and for what a keep ends."""
    return not record.get('url') and bool(
        record.get('task_id') or submit_in_flight(record))


def game_state_key(state, level=None, variant=None):
    """The memo key for a game's state (spec §3).

    A level may have its own sound, but by default every level of a game
    shares one: a child hears the same cheer on level 1 and level 9, and a
    second run of level 1 hears what the first run did.  A variant is the
    next take after a reviewer rejected one.
    """
    key = str(state)
    if level not in (None, '', 0):
        key = f'{key}@{level}'
    if variant not in (None, '', 0, 1):
        key = f'{key}#{variant}'
    return key


#: The fields of a composition still under way at a key: the composer's
#: task, when it was taken, what it was composed with (``task_take``: its
#: prompt, variant and mood, so the call that finishes it binds it as what
#: it is), and a submit whose reply timed out.  A verdict over it carries
#: them all; an ended job drops them all.
IN_FLIGHT_FIELDS = ('task_id', 'task_since', 'task_take', 'submitted_at')

#: What ``task_take`` names: the only fields a task picked up again lays
#: over the call's own.
TASK_LABELS = ('mood', 'prompt', 'variant')


def task_labels(record):
    """What this record's task was composed with, from ``task_take``: its
    TASK_LABELS only, else {}.  Any other shape reads as none, with a
    warning, never raised: read before the call's own guard, a raise
    wedged the key for good, and extra keys overrode the take's state,
    level and approval (review of 14c6bbd5f, F-A)."""
    take = (record or {}).get('task_take')
    if take is None:
        return {}
    if not isinstance(take, dict):
        _log_corrupt(('task_take', take),
                     "a game sound's task_take %r is not a record; read as none", take)
        return {}
    return {field: take[field] for field in TASK_LABELS if field in take}


def ended_composition(record, reason, task_id=None):
    """``record`` with its composition over and the rest kept: the in-flight
    fields gone, so no lookup joins or resumes it, and the task noted as
    lost with why.  The composer cannot be asked to stop; its take, if it
    finishes, is simply not bound.  The one builder of an ended job, for
    agent_tools' _forget_task and for a keep (record_verdict)."""
    record = record or {}
    ended = {k: v for k, v in record.items() if k not in IN_FLIGHT_FIELDS}
    ended['lost_task'] = {'task_id': task_id or record.get('task_id'),
                          'reason': reason, 'at': time.time()}
    return ended


def submit_note(record, labels):
    """``record`` with the note that a submit is going out now, and what it
    composes (``task_take``), and the note's time, which names it.

    Written at the key BEFORE the submit, so that while the submit waits (up
    to two minutes) the composition is already in the memo.  The key reads
    'composing' (_composing): a second call waits for it rather than queue a
    second job.  A keep ends it the way it ends any job (record_verdict,
    _end_compositions_under), so what a keep ends is decided in one place,
    whenever the keep comes (review of bc7bbf721, F-D1, F-D3, F-D6).  A note
    whose process died lapses with the cooldown (submit_in_flight)."""
    at = time.time()
    return {**(record or {}), 'submitted_at': at, 'task_take': dict(labels or {})}, at


def holds_composition(record, field, value):
    """Whether ``record`` still holds the composition named by ``field`` =
    ``value``: a submit note's ``submitted_at``, or a ``task_id``, with no
    take yet.  A keep ends it (ended_composition drops both), a take landing
    at the key replaces it, a rejection carries it (record_verdict)."""
    record = record or {}
    # ``is not None``, not truth: a clock that starts at 0.0 (a test's, or a
    # box whose clock reset) notes a submit at 0.0, and that note is still
    # this call's.
    return value is not None and record.get(field) == value and not record.get('url')


def without_submit_note(record, at):
    """``record`` without the submit note ``at`` when it holds that note and
    nothing came of it, else ``record`` as it is: a submit that left no
    composition must not leave the key reading 'composing'.  An empty result
    means the key held nothing before the note (forget_game_state_record)."""
    record = record or {}
    if not holds_composition(record, 'submitted_at', at) or record.get('task_id'):
        return record
    return {k: v for k, v in record.items() if k not in ('submitted_at', 'task_take')}


def forget_game_state_record(games, game_id, state, level=None):
    """Remove the record at exactly this key: a submit note that left nothing
    behind, at a key that held nothing before it.  For the game's music, the
    copy the first version's shape keeps (set_game_state_sound writes both)
    goes with it only when it is that same record; a take an older memo
    still holds there stays."""
    slot = (games or {}).get(str(game_id)) or {}
    removed = (slot.get('sounds') or {}).pop(game_state_key(state, level), None)
    if (state == 'bgm' and level in (None, '', 0) and removed is not None
            and slot.get('music') == removed):
        slot.pop('music', None)

#: Corrupt values already warned about.  A lookup reads the same record on
#: every turn, so each is a WARNING once and DEBUG after; at DEBUG alone it
#: never reached the desktop log (review of 0c542e278, N-1).
_corrupt_warned = set()


def _log_corrupt(value, msg, *args):
    """Log a corrupt stored value: WARNING the first time, DEBUG after."""
    token = repr(value)[:200]
    if token in _corrupt_warned:
        logger.debug(msg, *args)
        return
    if len(_corrupt_warned) >= 1024:
        _corrupt_warned.clear()
    _corrupt_warned.add(token)
    logger.warning(msg, *args)


def take_variant(record):
    """Which take this record is: 1 for the first, counting up with each
    take composed after a rejection.  A stored value that is not a number
    (hand-edited or corrupt) reads as 1 rather than raising out of a lookup
    (review of ed6576624, F2), and says so (review of 4528e339d, F-3)."""
    value = (record or {}).get('variant') or 1
    try:
        return int(value)
    except (TypeError, ValueError):
        _log_corrupt(('variant', value),
                     "a game sound's variant %r is not a number; read as 1", value)
        return 1


def _named_takes(takes):
    """The takes in a stored ``previous_takes`` that name a take: a dict with
    a url.  Anything else names no take and is skipped, with a log line,
    never raised (review of ed6576624, F2; 4528e339d, F-3)."""
    takes = list(takes or ())
    named = [take for take in takes if isinstance(take, dict) and take.get('url')]
    if len(named) < len(takes):
        skipped = [take for take in takes if take not in named]
        _log_corrupt(('takes', skipped),
                     '%d past take(s) with no url skipped in a game sound record: %r',
                     len(skipped), skipped[:3])
    return named


def turned_down_takes(record):
    """Every take this record holds as turned down, oldest first: the ones
    it carries in ``previous_takes``, then the one just rejected.  An entry
    with no url names no take and is skipped.

    The one builder of that list, and the one source of a take's history:
    every write that replaces a record carries it forward, both setters
    alike (set_game_state_sound_at, set_game_state_sound)."""
    record = record or {}
    takes = [dict(take) for take in _named_takes(record.get('previous_takes'))]
    if record.get('rejected_url'):
        takes.append({'url': record['rejected_url'],
                      'variant': take_variant(record),
                      'rejected_reason': record.get('rejected_reason') or '',
                      'rejected_at': record.get('rejected_at')})
    return takes


def _turned_down(sounds, keys):
    """Every take turned down at these keys of one memo: the one just
    rejected, and the earlier ones each next take carries forward in
    ``previous_takes``.  Reading only the last rejection brought the
    first back: reject the agent's take U, reject your own W, and U played
    again (review of c48e90601, F1)."""
    urls = set()
    for key in keys:
        urls.update(take.get('url') for take in turned_down_takes(sounds.get(key)))
    urls.discard(None)
    return urls


def _keeping_turned_down(replaced, record):
    """``record``, with every take ``replaced`` held as turned down carried
    into its ``previous_takes``.

    A write at a key replaces the record there, and the takes a person
    turned down live only in that record.  A take finishing over a verdict
    given while it composed kept only the takes turned down before the
    composition began, so the take turned down meanwhile played to them
    again (measured 2026-10-18 at f572fd775)."""
    carried = _named_takes(record.get('previous_takes'))
    known = {take['url'] for take in carried}
    known.update((record.get('url'), record.get('rejected_url')))
    extra = []
    for take in turned_down_takes(replaced):
        if take['url'] not in known:
            known.add(take['url'])
            extra.append(take)
    if not extra:
        return record
    return {**record, 'previous_takes': carried + extra}


def _record_at(games, game_id, key):
    """The record at exactly this memo key, or {}."""
    slot = (games or {}).get(str(game_id), {})
    return (slot.get('sounds') or {}).get(key) or {}


def game_state_match(games, game_id, state, level=None, *, own=None,
                     own_only=False):
    """The sound memoized for a game's state, and what matched (spec §4).

    ``games`` is the agent's memo; ``own`` is one person's memo (same
    shape, from their own store), or None for nobody's.  Tries, in order
    and only exactly: this person's own take, this level's sound, the
    game's sound.  An agent take this person turned down is skipped for
    them alone, so they stop hearing it while everyone else still does.
    Returns (record, matched, key) where matched is 'mine', 'level',
    'game', 'composing' or 'miss' -- a caller reports it, so a hit is
    never confused with a near miss.

    ``own_only`` looks at this person's own memo alone.  Correcting a
    sound has to: with the ladder, a person asking to replace what they
    are hearing would be handed the agent's approved piece back and could
    never change anything (caught by its test, 2026-09-21).
    """
    slot = (games or {}).get(str(game_id), {})
    agent_sounds = {} if own_only else (slot.get('sounds') or {})
    mine = ((own or {}).get(str(game_id), {}).get('sounds') or {})

    level_key = game_state_key(state, level)
    game_key = game_state_key(state)
    turned_down = _turned_down(mine, (level_key, game_key))

    for source, sounds, keys in (
        ('mine', mine, (level_key, game_key)),
        ('level', agent_sounds, (level_key,) if level_key != game_key else ()),
        ('game', agent_sounds, (game_key,)),
    ):
        for key in keys:
            record = sounds.get(key) or {}
            if record.get('url') and (source == 'mine'
                                      or record['url'] not in turned_down):
                return record, source, key

    # the shape written before states existed: a game's music
    if (not own_only and state == 'bgm'
            and isinstance(slot.get('music'), dict) and slot['music'].get('url')
            and slot['music']['url'] not in turned_down):
        return slot['music'], 'game', game_key

    # a composition already under way for the exact key is itself the memo,
    # so a second request joins it instead of starting another
    for sounds in (mine, agent_sounds):
        record = sounds.get(level_key) or {}
        # A task id, OR a submit whose reply timed out and may be queued:
        # the route used to see the second as a miss and queue a duplicate
        # job for bgm, the very thing the cooldown exists to stop (F8).
        if _composing(record):
            return record, 'composing', level_key
    return {}, 'miss', level_key


def game_state_sound(games, game_id, state, level=None, *, own=None,
                     own_only=False):
    """What a game plays for this state, and what matched -- the common ask.

    game_state_match answers the same question and also names the KEY it
    matched, which a verdict needs and a lookup does not.
    """
    record, source, _key = game_state_match(
        games, game_id, state, level, own=own, own_only=own_only)
    return record, source


def set_game_state_sound_at(games, game_id, key, record):
    """Write a record at an EXACT key -- the key a lookup MATCHED.

    ``games`` is whichever memo the write belongs to, the agent's or one
    person's own; the caller chooses.  A verdict belongs to the memo it
    was given (spec 6.1), which is not always the key that was asked for:
    the ladder falls back from 'correct@3' to 'correct', so a reviewer
    rejecting while playing level 3 was writing a rejection at
    'correct@3' while 'correct' -- the take actually sounding -- kept its
    url and went on playing. Composing under the key asked for (spec 4)
    is a different rule for a different path: a miss that composes.

    Every take the replaced record held as turned down is kept
    (_keeping_turned_down).  Returns the record as stored.
    """
    record = _keeping_turned_down(_record_at(games, game_id, key), record)
    slot = games.setdefault(str(game_id), {})
    slot.setdefault('sounds', {})[key] = record
    if key == 'bgm' and isinstance(slot.get('music'), dict):
        slot['music'] = record
    return record


def set_game_state_sound(games, game_id, state, record, level=None):
    """Memoize a sound under the key that was ASKED for (spec §4).

    Not under the key a fallback would have used: the next identical
    request has to be an exact hit.  Like set_game_state_sound_at, it keeps
    every take the replaced record held as turned down, and returns the
    record as stored.
    """
    key = game_state_key(state, level)
    record = _keeping_turned_down(_record_at(games, game_id, key), record)
    slot = games.setdefault(str(game_id), {})
    slot.setdefault('sounds', {})[key] = record
    if state == 'bgm' and level in (None, '', 0):
        # the shape the first version wrote, kept so older memos read back
        slot['music'] = record
    return record


#: What a reviewer's card names a game sound by.  agent_tools builds it with
#: game_sound_action and the approval endpoint reads it with
#: parse_game_sound_action -- one producer, one reader.
GAME_SOUND_ACTION = 'game_sound:'


def game_sound_action(game_id, state):
    """The action a reviewer's card carries for this game's state."""
    return f'{GAME_SOUND_ACTION}{game_id}:{state}'


def parse_game_sound_action(action):
    """(game_id, state) from a card's action, CASE KEPT, or None.

    States are camelCase ('starEarned', 'countdownTick') and memo keys are
    exact.  The approval endpoint lowercased the action before parsing it,
    so a verdict on 7 of the 14 states matched nothing and never landed
    (hartos-3a F5, CONFIRMED).  Only the prefix is compared without case.
    """
    text = str(action or '').strip()
    if not text.lower().startswith(GAME_SOUND_ACTION):
        return None
    parts = text.split(':')
    game_id = parts[1] if len(parts) > 1 else ''
    state = parts[2] if len(parts) > 2 and parts[2] else 'bgm'
    return game_id, state


def record_verdict(games, game_id, state, approved, reason='',
                   level=None, *, own=None):
    """Mark the memo a reviewer just judged, and say which one it was.

    ONE implementation, deliberately: the agent's approve_game_sound tool
    calls it, and so does the endpoint the reviewer's card posts to. Before
    this, the card's answer reached an endpoint with no mapping for a game
    sound and returned applied=False, so approved_at stayed null forever and
    REUSE could not tell an approved sound from an unreviewed one. A second
    copy of this logic over there would have been the parallel path the
    owner's rule exists to stop.

    The verdict lands on the take the ladder MATCHED, at the key it
    matched, not the key asked for (spec 6.1).  With ``own`` (a person's
    memo) it is that person's verdict on the take THEY hear, their own or
    the agent's, and it is written in their memo alone (spec 6.2): the
    agent's take stays as it is for everyone else.

    Returns (record, matched, key). An empty record means there was nothing
    bound to judge.
    """
    found, matched, matched_key = game_state_match(
        games, game_id, state, level, own=own)
    record = dict(found)
    if not record.get('url'):
        return {}, matched, matched_key
    write_key = matched_key
    if approved:
        record['approved_at'] = time.time()
    else:
        # kept, not deleted: the audio moves aside so a reviewer can go
        # back to it, but it must leave 'url' or the ladder goes on
        # serving the take they just turned down
        record['rejected_at'] = time.time()
        record['rejected_reason'] = (reason or '').strip()
        record['rejected_url'] = record.pop('url', None)
    target = own if own is not None else games
    # A person's verdict on the agent's take lands at the key their own
    # take may still be composing at.  The verdict does not end that
    # composition: without its task the next call could not find the job
    # and queued a second one (measured 2026-10-18 at f572fd775).
    # Only a rejection keeps it: a take that was KEPT ends the person's own
    # composition, and carried onto the kept take the job read 'composing'
    # again once that take was turned down, then resumed a take composed to
    # answer an older complaint (review of ed6576624, F1).
    composing = _record_at(target, game_id, write_key)
    in_flight = {field: composing[field] for field in IN_FLIGHT_FIELDS
                 if field in composing and not composing.get('url')}
    if not record.get('url'):
        record.update(in_flight)
    elif _composing(composing):
        # The composer cannot be asked to stop; its take, if it finishes,
        # is not written over this one (agent_tools bind_game_sound).  A
        # submit past its cooldown is no composition (review of 0c542e278,
        # N-3).
        logger.info('a take kept at %s/%s ends the composition there (%s)',
                    game_id, write_key,
                    in_flight.get('task_id') or 'a submit still queued')
    record = set_game_state_sound_at(target, game_id, write_key, record)
    if approved and write_key == game_state_key(state):
        _end_compositions_under(target, game_id, write_key)
    return record, matched, write_key


def _end_compositions_under(games, game_id, game_key):
    """End each level's composition that a take kept at the game's key
    covers: the ladder answers that take at every level without one of its
    own.  Ended only while a call was polling it, a level's job read
    'composing' again once the kept take was turned down, and the next call
    bound a take made for the older complaint (review of 5feeee2b8, F1)."""
    sounds = ((games or {}).get(str(game_id)) or {}).get('sounds') or {}
    for key in [k for k in sounds if str(k).startswith(f'{game_key}@')]:
        record = sounds.get(key)
        if isinstance(record, dict) and _composing(record):
            logger.info('a take kept at %s/%s ends the composition at %s (%s)',
                        game_id, game_key, key,
                        record.get('task_id') or 'a submit still queued')
            set_game_state_sound_at(games, game_id, key, ended_composition(
                record, f'a take was kept at {game_key}'))


def game_state_record(games, game_id, state, level=None):
    """The record at EXACTLY this key in this memo, or {} -- no ladder.

    The one reader of the storage shape for every caller that needs the
    key's own record rather than what the game would play: a take a reviewer
    turned down (rejected_take), a submit still in flight (bind_game_sound's
    cooldown), and the note that submit writes, which must land ON
    TOP of whichever of those is already there.  Before this each of them
    carried its own copy of the three lines below, and the copy in
    agent_tools was the fourth reader of a shape this module owns.
    """
    return _record_at(games, game_id, game_state_key(state, level))


def _latest_turned_down(record):
    """The take this record holds as most lately turned down: its own
    rejection, else, for a record with no take of its own (a job, or one
    that ended), the last of the takes it carries as turned down."""
    if record.get('rejected_at'):
        return record
    if record.get('url'):
        return {}
    takes = turned_down_takes(record)
    return takes[-1] if takes else {}


def _turned_down_when(take):
    """When ``take`` was turned down, as a number; 0 when unknown."""
    try:
        return float(take.get('rejected_at') or 0)
    except (TypeError, ValueError):
        return 0.0


def rejected_take(games, game_id, state, level=None):
    """The complaint the next take at this key answers, if any: the newer
    of the level's and the game's (the level's on a tie), each the take
    its key holds as most lately turned down.

    Kept so the next composition can answer the reason they gave, and so a
    reviewer can go back to it (spec §6.1).  A verdict lands at the key the
    ladder matched, so a rejection given while a level played is the
    game's; read at the level's key alone, the level was then composed as
    take 1 without the reason (review of 0c542e278).  A job written at a
    key replaces its rejection, carrying it as turned down: read at the top
    of the record alone, a take composed after that job was lost or failed
    asked for what was just turned down (review of 14c6bbd5f, F-B).
    """
    found = {}
    for key in dict.fromkeys((game_state_key(state, level), game_state_key(state))):
        take = _latest_turned_down(_record_at(games, game_id, key))
        if take and (not found or _turned_down_when(take) > _turned_down_when(found)):
            found = take
    return found
