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
  * the name the turn's agent says it has (core.agent_personality
    .spoken_name), and the app's, Nunba (ASSISTANT_NAME): being called by
    name is a strong cue, not proof ("Nunba is great, right?" said to
    someone else is talk about it), and speech without the name can still
    be for it;
  * the voice window (voiceprint.voice_window), as a hint of who spoke;
  * the words heard;
  * the agent's own open ask for this person (person_ask), with its words;
  * the agent's last line to this person and how long ago it was said;
  * the last turns of this agent's chat with the person, and apart from
    them, under their own label, the last words its mic overheard there.
Where the agent asked, the words are judged by the patient judge (one model
call that waits its turn), and with no verdict they are answered: an answer
to the agent's own question is never dropped for want of a verdict.  With no
question open, a judge that cannot say leaves the words heard (record_heard)
and /chat judges them once more later.  The echo gate (the page drops mic audio while its own TTS plays)
stays where it is.  Stage 2 (after the demo): camera and voice.

The question goes through core.llm_json.ask_model_json -> core.http_pool
.pooled_post, which admits a local main-llama call through the priority
scheduler (core.llama_scheduler) under this turn's own request id (/chat
sets it before it asks here), so it queues exactly like the turn it judges,
never ahead of a real turn and never as background work.

A turn judged not for the agent is still captured (record_heard): the words
are written to the chat history as heard by the open mic (their own role,
chat_messages.HEARD_ROLE), so the whole meeting is on record, and /chat
returns the agent's feedback (addressed false, a one-line reason) with no
reply and no voice.  Heard words are never the person's turn: every reader
that feeds an agent or a device skips them (chat_messages.not_heard).

No keyword list and no pattern on the words: they are handed to the model.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, List, Optional, Tuple

# The role and channel of an utterance the open mic heard that the agent
# judged not for it: on record, never answered, never the person's turn.
from integrations.social.chat_messages import HEARD_CHANNEL, HEARD_ROLE  # noqa: F401

logger = logging.getLogger(__name__)

# How many of this chat's last turns, and apart from them how many of its
# last overheard lines, ride in the question as context.  Not a decision: the
# model decides.
RECENT_TURNS = 6

# The app's name for its AI, told to the model as a name it answers to (owner
# 2026-11-25: "Nunba is like the name for the AI so the wake word shd still be
# there but intelligently while understanding context in a multi user
# environment").  The turn's agent has its own name too, the one it says
# (_agent_name); a turn with no agent of its own is Nunba.  Being called by
# name is a strong cue, not proof, and its absence is not a no.
ASSISTANT_NAME = 'Nunba'

# How each kind of row is shown to the judge: what the agent said, what was
# said to it, and what its mic overheard that was not for it.
_SPOKEN_AS = {'assistant': 'you', 'user': 'said to you',
              HEARD_ROLE: 'heard in the room'}

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


def _open_ask_for(user_id, prompt_id) -> Optional[str]:
    """The words of the turn's agent's newest ask still open for ``user_id``
    (person_ask.open_ask_ids: its own asks of the person's turns, never
    another agent's or background work's; read through person_ask.read_ask),
    '' for an open ask with no words, None when none is open.  The words are
    the card's title and its reason, which the card keeps under
    'description' (person_ask.ask)."""
    from integrations.agent_engine import person_ask
    ids = person_ask.open_ask_ids(user_id, prompt_id)
    if not ids:
        return None
    card, status = person_ask.read_ask(ids[-1], user_id)
    if status != 200:
        return None
    title = str(card.get('title') or '').strip()
    reason = str(card.get('description') or '').strip()
    if title and reason:
        return f"{title}{' ' if title[-1] in '.?!:' else ': '}{reason}"
    return title or reason


def open_question(user_id, prompt_id) -> Optional[str]:
    """The question the turn's agent still waits on from ``user_id``: its
    newest open card (_open_ask_for), else the question its running step
    sent (lifecycle_hooks.conversation_open_question: a send that asked,
    "Say ball", opens no card), '' for one without words, None when neither.
    The one reader for the judge's context and for /chat's door, which keeps
    a final heard while a turn runs only when the agent asked (hartos-91's
    review of #303).  A reader that fails is no evidence, never a crash."""
    try:
        card = _open_ask_for(user_id, prompt_id)
    except Exception:  # noqa: BLE001
        logger.debug('open mic: open ask not read', exc_info=True)
        card = None
    if card is not None or not user_id or prompt_id in (None, ''):
        return card
    try:
        from hartos.lifecycle_hooks import conversation_open_question
        return conversation_open_question(f'{user_id}_{prompt_id}')
    except Exception:  # noqa: BLE001
        # Not quiet: without it a child's answer to "Say ball" is heard only
        # (review rv_303 m5).
        logger.warning("open mic: the conversation's open question not read",
                       exc_info=True)
        return None


def _agent_name(prompt_id) -> Optional[str]:
    """The name the turn's agent says it has, or None for a turn with no
    agent of its own (no prompts/<prompt_id>.json).  The one name rule
    (core.agent_personality.spoken_name) on the inputs the agent's prompt
    reads (hartos.agent_identity.build_identity_prompt): its saved
    personality, else its config's, with its config's name and its id."""
    from core.cache_loaders import load_agent_config
    config = load_agent_config(prompt_id) if prompt_id not in (None, '') else None
    if not isinstance(config, dict):
        return None
    from core.agent_personality import load_personality, spoken_name
    aid = str(prompt_id)   # load_agent_config refused an id unsafe as a file name
    who = load_personality(aid)
    if who is None:
        personality = config.get('personality')
        who = dict(personality) if isinstance(personality, dict) else {}
        who.setdefault('agent_name', config.get('agent_name', ''))
    return spoken_name(who, config.get('name', ''), aid)


def _epoch(created) -> Optional[float]:
    if created is None:
        return None
    if created.tzinfo is None:
        # Stored by func.now() on SQLite: UTC without a zone.
        import calendar
        return calendar.timegm(created.timetuple()) + created.microsecond / 1e6
    return created.timestamp()


def _recent_turns(user_id, prompt_id) -> List[Tuple[str, str, Optional[float]]]:
    """[(role, text, epoch seconds)] of the turn's agent's chat with
    ``user_id`` (its rows carry the agent's prompt_id; a turn with no agent,
    the rows with none), oldest first: its last RECENT_TURNS turns, and
    apart from them its last RECENT_TURNS overheard lines under HEARD_ROLE,
    so room talk never pushes the agent's own last line out (integrations
    .social.chat_messages' model accessor, the one that serves the chat
    feed)."""
    from integrations.social.chat_messages import _models, heard, not_heard
    get_db, ConversationEntry = _models()
    db = get_db()
    try:
        chat = db.query(ConversationEntry).filter(
            ConversationEntry.user_id == str(user_id),
            ConversationEntry.prompt_id == str(prompt_id)
            if prompt_id not in (None, '') else ConversationEntry.prompt_id.is_(None))
        rows = []
        for kind in (not_heard, heard):
            rows += (chat.filter(kind(ConversationEntry))
                     .order_by(ConversationEntry.id.desc())
                     .limit(RECENT_TURNS).all())
        return [(HEARD_ROLE if r.channel_type == HEARD_CHANNEL else str(r.role or ''),
                 str(r.content or ''), _epoch(r.created_at))
                for r in sorted(rows, key=lambda r: r.id)]
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def _voice_hint(user_id) -> str:
    """Which voice said what on this person's device lately (integrations
    .audio.voiceprint.voice_window, the window the REUSE turn already reads),
    or ''.  A hint for the model, never a gate."""
    from integrations.audio.voiceprint import voice_window
    return voice_window(str(user_id))


def _context_lines(context: dict) -> List[str]:
    lines = []
    name = context.get('assistant_name') or ASSISTANT_NAME
    if name == ASSISTANT_NAME:
        called, its_name = f'Your name is {name}.', f'"{name}, ..."'
    else:
        called = (f'Your name is {name}, and you are the AI of the '
                  f'{ASSISTANT_NAME} app, so "{ASSISTANT_NAME}" is a name for '
                  f'you too.')
        its_name = f'"{name}, ..." or "{ASSISTANT_NAME}, ..."'
    lines.append(
        f'{called}  Being called by name ({its_name}) is a '
        f'strong sign the speech is for you, but not proof: in a room of '
        f'several people, "{name} is great, right?" said to someone else is '
        f'talk about you, not to you.  Speech without your name can still be '
        f'for you: an answer to your open question, or a follow-up in an '
        f'exchange you are having.  Work out who is talking to whom from the '
        f'words and the context.')
    hint = context.get('voice_hint')
    if hint:
        lines.append('A hint, not proof, of which voice said what lately:')
        lines.append(str(hint))
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
    turns = [(_SPOKEN_AS[role], text) for role, text in context.get('recent_turns') or []
             if role in _SPOKEN_AS]
    if turns:
        lines.append('The last lines of your chat with this person, oldest '
                     'first ("said to you": said to you; "heard in the room": '
                     'your microphone overheard it and it was not for you):')
        for label, text in turns:
            lines.append(f'  {label}: {text}')
    return lines


def _ask_the_turns_model(words: str, context: dict,
                         patient: bool = False) -> Optional[Tuple[bool, str]]:
    """The turn's own model (core.llm_json.ask_model_json: the node's model,
    one stateless question, nothing posted to any chat) on whether ``words``
    were meant for the agent: (verdict, one-line reason), or None when it
    cannot say.  ``patient``: asked as any model call is (ask_model_json's
    own wait and resend), for the one later verdict on a final whose first
    judge could not say (patient_judge)."""
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
    reply = (ask_model_json(content, _JUDGE_SCHEMA, 'open_mic_addressee',
                            max_tokens=60) if patient else
             ask_model_json(content, _JUDGE_SCHEMA, 'open_mic_addressee',
                            max_tokens=60, timeout=15.0, once=True))
    verdict = (reply or {}).get('for_the_agent')
    if not isinstance(verdict, bool):
        if reply is not None:
            # A failed call is named by ask_model_json (its timeout or HTTP
            # error); a reply with no verdict is named here, once per final.
            logger.warning('open mic: the judge answered with no verdict')
        return None
    return verdict, str((reply or {}).get('reason') or '').strip()[:200]


def patient_judge(words: str, context: dict) -> Optional[Tuple[bool, str]]:
    """The judge that waits as any model call does: the only judge of words
    heard while the agent's question is open (decide_open_mic_turn), and the
    one later verdict on a final whose first judge could not say
    (hart_intelligence_entry._judge_heard_words_again)."""
    return _ask_the_turns_model(words, context, patient=True)


_UNSET = object()


def decide_open_mic_turn(user_id, prompt_id, text, *, open_ask=_UNSET,
                         recent_turns=_UNSET, voice_hint=_UNSET,
                         agent_name=_UNSET,
                         judge: Optional[Callable] = None,
                         now: Optional[float] = None,
                         heard_already: bool = False) -> Tuple[bool, str, str]:
    """(respond, signals, reason) for a final the always-on mic heard on
    the chat of agent ``prompt_id`` (None: a turn with no agent of its own).

    The model judges every final; nothing here decides by number.
    ``open_ask``: the open question's text ('' for an ask without text) or
    None; ``recent_turns``: [(role, text, epoch seconds)] oldest first, role
    HEARD_ROLE for overheard lines; ``agent_name``: the name the agent says
    it has, None for Nunba.  Left out, each is read here for this agent; a
    reader that fails is no evidence, never a crash.  ``judge(words,
    context)`` returns (verdict, reason), a bare verdict, or None; left out,
    the turn's own model.  ``signals`` is the audit form, never the words."""
    now = time.time() if now is None else now
    words = str(text or '').strip()

    if open_ask is _UNSET:
        open_ask = open_question(user_id, prompt_id)
    if recent_turns is _UNSET:
        try:
            recent_turns = _recent_turns(user_id, prompt_id)
        except Exception:  # noqa: BLE001
            logger.debug('open mic: recent turns not read', exc_info=True)
            recent_turns = None
    recent_turns = list(recent_turns or [])
    if heard_already:
        # A later verdict on words already recorded heard (record_heard):
        # their own row would tell the model these very words were judged
        # not for it, and bias it to no (review rv_303 MUST-3).
        for i in range(len(recent_turns) - 1, -1, -1):
            if (recent_turns[i][0] == HEARD_ROLE
                    and str(recent_turns[i][1] or '').strip() == words):
                del recent_turns[i]
                break
    if voice_hint is _UNSET:
        try:
            voice_hint = _voice_hint(user_id)
        except Exception:  # noqa: BLE001
            logger.debug('open mic: voice window not read', exc_info=True)
            voice_hint = None
    if agent_name is _UNSET:
        try:
            agent_name = _agent_name(prompt_id)
        except Exception:  # noqa: BLE001
            logger.warning('open mic: the name of agent %s not read; judged as %s',
                           prompt_id, ASSISTANT_NAME, exc_info=True)
            agent_name = None

    last_line, last_age = None, None
    for role, line, at in reversed(recent_turns):
        if role == 'assistant':
            last_line = line
            last_age = int(now - at) if at is not None and now >= at else None
            break

    context = {
        'assistant_name': agent_name or ASSISTANT_NAME,
        'voice_hint': voice_hint or None,
        'open_question': open_ask,
        'last_agent_line': last_line,
        'last_agent_line_age_s': last_age,
        'recent_turns': [(role, line) for role, line, _ in recent_turns],
    }

    def signals(verdict):
        return (f'ask_open={int(open_ask is not None)},'
                f'last_agent_s={"none" if last_age is None else last_age},'
                f'turns={len(recent_turns)},judge={verdict}')

    # Where the agent asked (an open card, or its running step's question),
    # the words are judged by the patient judge, once: the 15 s one-try judge
    # fails exactly when the one-slot model is busy, and an answer to the
    # agent's own question is worth the wait.  Live 2026-11-27 23:38 IST: the
    # TV's "ご視聴ありがとうございました" reached the speech agent unjudged while
    # it waited on "Say ball" and was praised as "ball" (249 of 273 unjudged
    # finals in 24 h had a question open; review rv_gaps A3).
    if judge is None:
        judge = patient_judge if open_ask is not None else _ask_the_turns_model
    try:
        answer = judge(words, context)
    except Exception:  # noqa: BLE001
        logger.warning('open mic: the model could not judge the words',
                       exc_info=True)
        answer = None
    if isinstance(answer, tuple):
        verdict, reason = answer[0], str(answer[1] if len(answer) > 1 else '')
    else:
        verdict, reason = answer, ''
    if not isinstance(verdict, bool):
        # No verdict is no evidence (hartos-91's review of #303; owner: models
        # judge, not defaults).  Answered as addressed, 2026-11-27 23:00 to
        # 11-28 00:42 IST, 18 of 18 finals whose judge timed out on the busy
        # one-slot 4B were TV, room talk and decode loops, each a turn that
        # kept the model busy, so the next judge failed too.  Where the agent
        # asked, the patient judge above had its full wait and still could not
        # say, so the words reach the agent: an answer to its question is
        # never dropped for want of a verdict.  Otherwise /chat records them
        # heard and judges them once more later (_judge_heard_words_again).
        return (open_ask is not None, signals('unknown'),
                'the model could not judge the words')
    return verdict, signals('yes' if verdict else 'no'), reason


def record_heard(user_id, words, *, prompt_id=None, request_id=None) -> Optional[dict]:
    """Write an utterance the agent judged not for it to the chat history,
    word for word, as heard by the open mic (HEARD_ROLE on HEARD_CHANNEL),
    through the one chat-history writer (integrations.social.chat_messages
    .persist).  Never the person's turn: no reader that feeds an agent or a
    device reads it unless it asks for heard speech, and it is not published
    to the person's devices.  No agent reply is written.  The row dict, or
    None when it was not written."""
    from integrations.social.chat_messages import persist
    return persist(str(user_id), HEARD_ROLE, str(words or ''),
                   prompt_id=str(prompt_id) if prompt_id is not None else None,
                   request_id=request_id, channel_type=HEARD_CHANNEL)
