"""A game's generated sounds, and the memo that stops them being made twice.

The rules are the owner's, written down in
docs/internal/GAME_SOUND_MEMOIZATION_SPEC.md: an exact key, a declared
ladder, every answer naming what matched, and a correction that belongs to
whoever made it.

This lives on its own so the one matcher serves everyone who needs it --
the agent's tools here, and the node's kids media route -- rather than
each growing a ladder of its own.  Importing it costs nothing.
"""

# The states a game passes through, and how each is described to the
# composer.  The memo key is (agent, game, state): the same state always
# replays the artifact composed for it the first time, and how each is described to the composer.
# One vocabulary for every game: the app's own game renderer accepts the
# same names over its bridge (DynamicGameRenderer ALLOWED_SOUND_EVENTS), so
# an agent-led sound and a game-led sound are the same sound.
import time


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


def game_state_match(games, game_id, state, level=None, user_id=None,
                     own_only=False):
    """The sound memoized for a game's state, and what matched (spec §4).

    Tries, in order and only exactly: this user's own correction, this
    level's sound, the game's sound.  Returns (record, matched) where
    matched is 'mine', 'level', 'game', 'composing' or 'miss' — a caller
    reports it, so a hit is never confused with a near miss.

    ``own_only`` looks at this person's own memo alone.  Correcting a
    sound has to: with the ladder, a person asking to replace what they
    are hearing would be handed the agent's approved piece back and could
    never change anything (caught by its test, 2026-09-21).
    """
    slot = (games or {}).get(str(game_id), {})
    agent_sounds = {} if own_only else (slot.get('sounds') or {})
    mine = (slot.get('mine') or {}).get(str(user_id), {}) if user_id else {}

    level_key = game_state_key(state, level)
    game_key = game_state_key(state)

    for source, sounds, keys in (
        ('mine', mine, (level_key, game_key)),
        ('level', agent_sounds, (level_key,) if level_key != game_key else ()),
        ('game', agent_sounds, (game_key,)),
    ):
        for key in keys:
            record = sounds.get(key) or {}
            if record.get('url'):
                return record, source, key

    # the shape written before states existed: a game's music
    if (not own_only and state == 'bgm'
            and isinstance(slot.get('music'), dict) and slot['music'].get('url')):
        return slot['music'], 'game', game_key

    # a composition already under way for the exact key is itself the memo,
    # so a second request joins it instead of starting another
    for sounds in (mine, agent_sounds):
        record = sounds.get(level_key) or {}
        if record.get('task_id'):
            return record, 'composing', level_key
    return {}, 'miss', level_key


def game_state_sound(games, game_id, state, level=None, user_id=None,
                     own_only=False):
    """What a game plays for this state, and what matched -- the common ask.

    game_state_match answers the same question and also names the KEY it
    matched, which a verdict needs and a lookup does not.
    """
    record, source, _key = game_state_match(
        games, game_id, state, level, user_id, own_only)
    return record, source


def set_game_state_sound_at(games, game_id, key, record, user_id=None):
    """Write a record at an EXACT key -- the key a lookup MATCHED.

    A verdict belongs to the memo it was given (spec 6.1), which is not
    always the key that was asked for: the ladder falls back from
    'correct@3' to 'correct', so a reviewer rejecting while playing level 3
    was writing a rejection at 'correct@3' while 'correct' -- the take
    actually sounding -- kept its url and went on playing. Composing under
    the key asked for (spec 4) is a different rule for a different path: a
    miss that composes.
    """
    slot = games.setdefault(str(game_id), {})
    if user_id in (None, ''):
        slot.setdefault('sounds', {})[key] = record
        if key == 'bgm' and isinstance(slot.get('music'), dict):
            slot['music'] = record
    else:
        slot.setdefault('mine', {}).setdefault(str(user_id), {})[key] = record
    return record


def set_game_state_sound(games, game_id, state, record, level=None, user_id=None):
    """Memoize a sound under the key that was ASKED for (spec §4).

    Not under the key a fallback would have used: the next identical
    request has to be an exact hit.
    """
    slot = games.setdefault(str(game_id), {})
    if user_id in (None, ''):
        slot.setdefault('sounds', {})[game_state_key(state, level)] = record
        if state == 'bgm' and level in (None, '', 0):
            # the shape the first version wrote, kept so older memos read back
            slot['music'] = record
    else:
        slot.setdefault('mine', {}).setdefault(str(user_id), {})[
            game_state_key(state, level)] = record
    return record


def record_verdict(games, game_id, state, approved, reason='',
                   level=None, user_id=None):
    """Mark the memo a reviewer just judged, and say which one it was.

    ONE implementation, deliberately: the agent's approve_game_sound tool
    calls it, and so does the endpoint the reviewer's card posts to. Before
    this, the card's answer reached an endpoint with no mapping for a game
    sound and returned applied=False, so approved_at stayed null forever and
    REUSE could not tell an approved sound from an unreviewed one. A second
    copy of this logic over there would have been the parallel path the
    owner's rule exists to stop.

    The verdict lands on the memo the ladder MATCHED, not the key asked for
    (spec 6.1), except for a person correcting their own copy, who always
    writes in their own space (spec 6.2).

    Returns (record, matched, key). An empty record means there was nothing
    bound to judge.
    """
    found, matched, matched_key = game_state_match(
        games, game_id, state, level, user_id, own_only=bool(user_id))
    record = dict(found)
    if not record.get('url'):
        return {}, matched, matched_key
    write_key = game_state_key(state, level) if user_id else matched_key
    if approved:
        record['approved_at'] = time.time()
    else:
        # kept, not deleted: the audio moves aside so a reviewer can go
        # back to it, but it must leave 'url' or the ladder goes on
        # serving the take they just turned down
        record['rejected_at'] = time.time()
        record['rejected_reason'] = (reason or '').strip()
        record['rejected_url'] = record.pop('url', None)
    set_game_state_sound_at(games, game_id, write_key, record, user_id)
    return record, matched, write_key


def rejected_take(games, game_id, state, level=None, user_id=None):
    """The take a reviewer turned down for this exact key, if any.

    Kept so the next composition can answer the reason they gave, and so a
    reviewer can go back to it (spec §6.1).
    """
    slot = (games or {}).get(str(game_id), {})
    sounds = ((slot.get('mine') or {}).get(str(user_id), {}) if user_id
              else (slot.get('sounds') or {}))
    record = sounds.get(game_state_key(state, level)) or {}
    return record if record.get('rejected_at') else {}
