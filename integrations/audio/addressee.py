"""Was speech the always-on mic heard meant for the agent?  One decision for
an open-mic turn, called once by /chat (hart_intelligence_entry.chat) before
the turn is dispatched, written to history or spoken.

Owner 2026-11-24: "this wake word kind of filter we put to avoid chatter
detection should be automatically understood from multiple modality
context"; 2026-11-25: "mic is on by default is the design".  The page sends
every final its always-on mic hears, marked ``open_mic``; nothing in a client
gates on a word.

Stage 1 (this module), the cheap signals, none of which vetoes alone:
  * an open ask: the agent is waiting for this person's reply (person_ask);
  * an engaged conversation: the agent spoke to this person in the last
    ENGAGED_WINDOW_S seconds (their conversation rows);
  * the length of the speech: long enough to plausibly be a request.
Very short speech with no open ask and no engaged conversation is chatter.
When the signals do not settle it, the turn's own model is asked one short
yes or no question carrying the words and the context.  A model that cannot
answer never drops the person's words: the turn is answered.  The echo gate
(the page drops mic audio while its own TTS plays) stays where it is.
Stage 2 (after the demo): camera and voice weighting.

No keyword list and no pattern on the words: the words are only counted
(str.split) and handed to the model.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

# An agent turn this recent means the person is in a conversation with it.
ENGAGED_WINDOW_S = 60
# Speech this long plausibly is a request, whoever it was for.
REQUEST_MIN_WORDS = 6
# Speech this short, with no open ask and no engaged conversation, is chatter.
CHATTER_MAX_WORDS = 2

# The rule the agent already follows when it may or may not be addressed
# (integrations/social/mention_service: "Reply if appropriate; otherwise stay
# silent."), the same words here.
STAY_SILENT_LINE = 'Reply if appropriate; otherwise stay silent.'

_JUDGE_SCHEMA = {
    'type': 'object',
    'properties': {'for_the_agent': {'type': 'boolean'}},
    'required': ['for_the_agent'],
    'additionalProperties': False,
}


def _open_ask_for(user_id) -> Optional[str]:
    """The question of an ask still open for ``user_id`` (person_ask's held
    asks, any agent's), '' when one is open with no text, else None."""
    from integrations.agent_engine import person_ask
    uid = str(user_id or '')
    with person_ask._lock:
        open_asks = [a for a in person_ask._asks.values()
                     if a.user_id == uid and a.answered_at is None]
    if not open_asks:
        return None
    newest = max(open_asks, key=lambda a: a.created)
    card = newest.card if isinstance(newest.card, dict) else {}
    return str(card.get('title') or card.get('body') or '')


def _last_agent_turn(user_id) -> Optional[Tuple[float, str]]:
    """(epoch seconds, text) of the newest agent line in ``user_id``'s
    conversation rows, else None (integrations.social.chat_messages' model
    accessor, the one that serves the chat feed)."""
    from integrations.social.chat_messages import _models
    get_db, ConversationEntry = _models()
    db = get_db()
    try:
        row = (db.query(ConversationEntry)
               .filter(ConversationEntry.user_id == str(user_id),
                       ConversationEntry.role == 'assistant')
               .order_by(ConversationEntry.id.desc())
               .first())
        if row is None or row.created_at is None:
            return None
        created = row.created_at
        if created.tzinfo is None:
            # Stored by func.now() on SQLite: UTC without a zone.
            import calendar
            at = calendar.timegm(created.timetuple()) + created.microsecond / 1e6
        else:
            at = created.timestamp()
        return at, str(row.content or '')
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def _ask_the_turns_model(words: str, open_question: Optional[str],
                         last_line: Optional[str]) -> Optional[bool]:
    """The turn's own model (core.llm_json.ask_model_json: the node's model,
    one stateless question, nothing posted to any chat) on whether ``words``
    were meant for the agent.  None when it cannot say."""
    from core.llm_json import ask_model_json
    context = []
    if open_question:
        context.append(f'Your open question to them: {open_question}')
    if last_line:
        context.append(f'Your last words to them: {last_line}')
    content = (
        'You are an assistant whose microphone is always on, so it also hears '
        'people talking to each other or to someone else. '
        + (' '.join(context) + ' ' if context else '')
        + f'It just heard: "{words}". '
        'Were these words meant for you? ' + STAY_SILENT_LINE
        + ' Answer for_the_agent true to reply, false to stay silent.')
    reply = ask_model_json(content, _JUDGE_SCHEMA, 'open_mic_addressee',
                           max_tokens=20, timeout=15.0, once=True)
    verdict = (reply or {}).get('for_the_agent')
    return verdict if isinstance(verdict, bool) else None


_UNSET = object()


def decide_open_mic_turn(user_id, prompt_id, text, *, open_ask=_UNSET,
                         last_agent_turn_at=_UNSET,
                         judge: Optional[Callable] = None,
                         now: Optional[float] = None) -> Tuple[bool, str]:
    """(respond, signals) for a final the always-on mic heard unprompted.

    ``open_ask``: the open question's text ('' for an ask without text) or
    None; ``last_agent_turn_at``: (epoch seconds, text) of the agent's last
    line to this person or None.  Left out, each is read here; a reader that
    fails is no evidence, never a crash.  ``judge(words, open_question,
    last_line)`` returns True, False or None; left out, the turn's own model.
    ``signals`` is the audit form, never the words."""
    now = time.time() if now is None else now
    words = str(text or '').strip()
    n_words = len(words.split())

    if open_ask is _UNSET:
        try:
            open_ask = _open_ask_for(user_id)
        except Exception:  # noqa: BLE001
            logger.debug('open mic: open ask not read', exc_info=True)
            open_ask = None
    if last_agent_turn_at is _UNSET:
        try:
            last_agent_turn_at = _last_agent_turn(user_id)
        except Exception:  # noqa: BLE001
            logger.debug('open mic: last agent turn not read', exc_info=True)
            last_agent_turn_at = None

    ask_open = open_ask is not None
    last_at, last_line = (last_agent_turn_at if last_agent_turn_at else (None, None))
    engaged = last_at is not None and 0 <= now - last_at <= ENGAGED_WINDOW_S

    def signals(verdict):
        return (f'ask_open={int(ask_open)},engaged={int(engaged)},'
                f'words={n_words},judge={verdict}')

    if ask_open or engaged or n_words >= REQUEST_MIN_WORDS:
        return True, signals('none')
    if n_words <= CHATTER_MAX_WORDS:
        return False, signals('none')
    try:
        verdict = (judge or _ask_the_turns_model)(words, open_ask or None, last_line)
    except Exception:  # noqa: BLE001
        logger.warning('open mic: the model could not judge the words; answered',
                       exc_info=True)
        verdict = None
    if verdict is None:
        return True, signals('unknown')
    return bool(verdict), signals('yes' if verdict else 'no')
