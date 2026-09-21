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


def _sounds_of(games, game_id, user_id=None):
    """Where a game's sounds are kept: the agent's, or one user's own.

    A person correcting a sound while reusing an agent must not change
    what everybody else hears (spec §6.2), so their correction is kept
    beside the agent's, under their id.
    """
    slot = (games if games is not None else {}).setdefault(str(game_id), {}) \
        if hasattr(games, 'setdefault') else (games or {}).get(str(game_id), {})
    if user_id in (None, ''):
        return slot.setdefault('sounds', {}) if hasattr(slot, 'setdefault') else (slot.get('sounds') or {})
    if hasattr(slot, 'setdefault'):
        return slot.setdefault('mine', {}).setdefault(str(user_id), {})
    return (slot.get('mine') or {}).get(str(user_id), {})


def game_state_sound(games, game_id, state, level=None, user_id=None,
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
                return record, source

    # the shape written before states existed: a game's music
    if (not own_only and state == 'bgm'
            and isinstance(slot.get('music'), dict) and slot['music'].get('url')):
        return slot['music'], 'game'

    # a composition already under way for the exact key is itself the memo,
    # so a second request joins it instead of starting another
    for sounds in (mine, agent_sounds):
        record = sounds.get(level_key) or {}
        if record.get('task_id'):
            return record, 'composing'
    return {}, 'miss'


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
