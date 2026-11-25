"""Was speech the always-on mic heard meant for the agent?  One decision for
an open-mic turn, called once by /chat (hart_intelligence_entry.chat) before
the turn is dispatched, answered or spoken.

Owner 2026-11-24: "this wake word kind of filter we put to avoid chatter
detection should be automatically understood from multiple modality
context"; 2026-11-25: "mic is on by default is the design", "it shd be
intelligent enough to demarcate rather than stats based", and "the client
cant have all intelligence, the agent shd decide and give a feedback to ui
even if sent, so that entire meeting is caotured but only the demo works
intelligently".

So the page sends every final its always-on mic hears, marked ``open_mic``,
and gates nothing.  Here the turn's own model judges EVERY such final: no
word count, no time window and no length rule decides.  What this module
knows goes to the model as context, so it can tell speech meant for the
agent (an answer to its question, a request or reply to it) from room talk
(people talking to each other, a presenter explaining to an audience, TV):
  * the words heard;
  * the agent's open ask for this person (person_ask), with its question;
  * the agent's last line to this person and how long ago it was said;
  * the last few turns of this chat (their conversation rows).
A model that cannot answer never drops the person's words: the turn is
answered.  The echo gate (the page drops mic audio while its own TTS plays)
stays where it is.  Stage 2 (after the demo): camera and voice.

The question goes through core.llm_json.ask_model_json -> core.http_pool
.pooled_post, which admits a local main-llama call through the priority
scheduler (core.llama_scheduler) under this turn's own request id, so it
queues exactly like the turn it judges, never ahead of a real turn.

A turn judged not for the agent is still captured (record_heard): the
person's words are written to the chat history as heard by the open mic, so
the whole meeting is on record, and /chat returns the agent's feedback
(addressed false, a one-line reason) with no reply and no voice.

No keyword list and no pattern on the words: they are handed to the model.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# How many of this chat's last turns ride in the question as context.  Not a
# decision: the model decides.
RECENT_TURNS = 6

# The chat-history channel of an utterance the open mic heard that the agent
# judged not for it: on record, never answered.
HEARD_CHANNEL = 'open_mic_heard'

# The rule the agent already follows when it may or may not be addressed
# (integrations/social/mention_service: "Reply if appropriate; otherwise stay
# silent."), the same words here.
STAY_SILENT_LINE = 'Reply if appropriate; otherwise stay silent.'

_JUDGE_SCHEMA = {
    'type': 'object',
    'properties': {'for_the_agent': {'type': 'boolean'},
                   'reason': {'type': 'string'}},
    'required': ['for_the_agent', 'reason'],
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


def _epoch(created) -> Optional[float]:
    if created is None:
        return None
    if created.tzinfo is None:
        # Stored by func.now() on SQLite: UTC without a zone.
        import calendar
        return calendar.timegm(created.timetuple()) + created.microsecond / 1e6
    return created.timestamp()


def _recent_turns(user_id) -> List[Tuple[str, str, Optional[float]]]:
    """[(role, text, epoch seconds)] of the last RECENT_TURNS rows of
    ``user_id``'s conversation, oldest first (integrations.social
    .chat_messages' model accessor, the one that serves the chat feed)."""
    from integrations.social.chat_messages import _models
    get_db, ConversationEntry = _models()
    db = get_db()
    try:
        rows = (db.query(ConversationEntry)
                .filter(ConversationEntry.user_id == str(user_id))
                .order_by(ConversationEntry.id.desc())
                .limit(RECENT_TURNS)
                .all())
        return [(str(r.role or ''), str(r.content or ''), _epoch(r.created_at))
                for r in reversed(rows)]
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def _context_lines(context: dict) -> List[str]:
    lines = []
    question = context.get('open_question')
    if question is not None:
        lines.append(f'You asked this person a question and are waiting for '
                     f'the reply: "{question}".' if question else
                     'You asked this person something and are waiting for the reply.')
    else:
        lines.append('You have no open question to this person.')
    last_line, age = context.get('last_agent_line'), context.get('last_agent_line_age_s')
    if last_line:
        when = f', {age} seconds ago' if age is not None else ''
        lines.append(f'Your last words to this person{when}: "{last_line}".')
    turns = context.get('recent_turns') or []
    if turns:
        lines.append('The last turns of this chat, oldest first:')
        for role, text in turns:
            lines.append(f'  {"you" if role == "assistant" else "heard"}: {text}')
    return lines


def _ask_the_turns_model(words: str, context: dict) -> Optional[Tuple[bool, str]]:
    """The turn's own model (core.llm_json.ask_model_json: the node's model,
    one stateless question, nothing posted to any chat) on whether ``words``
    were meant for the agent: (verdict, one-line reason), or None when it
    cannot say."""
    from core.llm_json import ask_model_json
    content = '\n'.join(
        ['Your microphone is always on, so it hears everything said in the '
         'room, not only what is said to you.  Decide whether the speech '
         'below is meant for you or is room talk.',
         'Meant for you: an answer to your question, or a request, reply or '
         'remark addressed to you.',
         'Room talk: people talking to each other, a presenter explaining '
         'something to an audience, a TV or video playing.']
        + _context_lines(context)
        + [f'The speech just heard: "{words}"',
           STAY_SILENT_LINE + '  Answer for_the_agent true if it is meant for '
           'you, false if it is room talk, and give the reason in one short line.'])
    reply = ask_model_json(content, _JUDGE_SCHEMA, 'open_mic_addressee',
                           max_tokens=60, timeout=15.0, once=True)
    verdict = (reply or {}).get('for_the_agent')
    if not isinstance(verdict, bool):
        return None
    return verdict, str((reply or {}).get('reason') or '').strip()[:200]


_UNSET = object()


def decide_open_mic_turn(user_id, prompt_id, text, *, open_ask=_UNSET,
                         recent_turns=_UNSET,
                         judge: Optional[Callable] = None,
                         now: Optional[float] = None) -> Tuple[bool, str, str]:
    """(respond, signals, reason) for a final the always-on mic heard.

    The model judges every final; nothing here decides by number.
    ``open_ask``: the open question's text ('' for an ask without text) or
    None; ``recent_turns``: [(role, text, epoch seconds)] oldest first.  Left
    out, each is read here; a reader that fails is no evidence, never a
    crash.  ``judge(words, context)`` returns (verdict, reason), a bare
    verdict, or None; left out, the turn's own model.  ``signals`` is the
    audit form, never the words."""
    now = time.time() if now is None else now
    words = str(text or '').strip()

    if open_ask is _UNSET:
        try:
            open_ask = _open_ask_for(user_id)
        except Exception:  # noqa: BLE001
            logger.debug('open mic: open ask not read', exc_info=True)
            open_ask = None
    if recent_turns is _UNSET:
        try:
            recent_turns = _recent_turns(user_id)
        except Exception:  # noqa: BLE001
            logger.debug('open mic: recent turns not read', exc_info=True)
            recent_turns = None
    recent_turns = list(recent_turns or [])

    last_line, last_age = None, None
    for role, line, at in reversed(recent_turns):
        if role == 'assistant':
            last_line = line
            last_age = int(now - at) if at is not None and now >= at else None
            break

    context = {
        'open_question': open_ask,
        'last_agent_line': last_line,
        'last_agent_line_age_s': last_age,
        'recent_turns': [(role, line) for role, line, _ in recent_turns],
    }

    def signals(verdict):
        return (f'ask_open={int(open_ask is not None)},'
                f'last_agent_s={"none" if last_age is None else last_age},'
                f'turns={len(recent_turns)},judge={verdict}')

    try:
        answer = (judge or _ask_the_turns_model)(words, context)
    except Exception:  # noqa: BLE001
        logger.warning('open mic: the model could not judge the words; answered',
                       exc_info=True)
        answer = None
    if isinstance(answer, tuple):
        verdict, reason = answer[0], str(answer[1] if len(answer) > 1 else '')
    else:
        verdict, reason = answer, ''
    if not isinstance(verdict, bool):
        return True, signals('unknown'), ''
    return verdict, signals('yes' if verdict else 'no'), reason


def record_heard(user_id, words, *, prompt_id=None, request_id=None) -> Optional[dict]:
    """Write an utterance the agent judged not for it to the chat history as
    the person's speech, heard by the open mic (HEARD_CHANNEL), through the
    one chat-history writer (integrations.social.chat_messages.persist).  No
    agent reply is written.  The row dict, or None when it was not written."""
    from integrations.social.chat_messages import persist
    return persist(str(user_id), 'user', str(words or ''),
                   prompt_id=str(prompt_id) if prompt_id is not None else None,
                   request_id=request_id, channel_type=HEARD_CHANNEL)
