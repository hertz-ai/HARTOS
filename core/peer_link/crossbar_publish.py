"""Chat-bubble publishers — priority-49 'Thinking' bubble + future siblings.

Sibling modules in this package, one per topic family:
  - ui_commands.py: HARTOS→phone fleet commands
  - realtime.py: social / community events
  - crossbar_publish.py (this): chat-bubble messages on
    com.hertzai.hevolve.chat.{user_id}

All route through publish_async, which itself fans out via
MessageBus.publish (LOCAL + SSE + PEERLINK + CROSSBAR).  The
hartos_backend_adapter._capture_thinking monkey-patch on
publish_async still fires, so per-request thinking buffers keep
working with no adapter change.

The 'preffered_language' typo is preserved on the wire because
the historical schema has carried it for a long time and frontends
may key on the misspelt field name.

Latent bug NOT fixed here:
  Two FULL-schema callers (autogen GroupChat + action-start tap)
  historically pass request_id="123456" as a literal placeholder.
  All their traces collapse into one buffer key in Nunba's
  adapter.  Migrating those callers preserves the placeholder for
  byte-identical output; fixing the propagation is tracked
  separately.
"""
from __future__ import annotations

import itertools
import json
import logging
import threading
import time
import unicodedata
import uuid
from typing import Any, Mapping

from core.constants import (
    CHAT_ACTION_STATUS,
    CHAT_ACTION_THINKING,
    CHAT_BUBBLE_PRIORITY,
)

logger = logging.getLogger(__name__)

# Zoom-box stub — autogen FULL shape ships this so the avatar /
# book-parsing UI sees a stable schema.  All-zero coords mean
# "no zoom"; consumer treats as absent.
_ZOOM_STUB: dict = {
    'top_left':     {'x': 0, 'y': 0},
    'top_right':    {'x': 0, 'y': 0},
    'bottom_right': {'x': 0, 'y': 0},
    'bottom_left':  {'x': 0, 'y': 0},
}


def publish_thinking_trace(
    *,
    text: Any,
    user_id: str,
    request_id: str = '',
    bot_type: str = 'Agent',
    full_schema: bool = False,
    preffered_language: str = 'en-US',
    action: str = CHAT_ACTION_THINKING,
    page_image_url: str = '',
) -> bool:
    """Build a priority-49 'Thinking' bubble + publish to the user's chat topic.

    Args:
        text: Bubble body. Coerced to str via str() if not already a
            string — autogen taps occasionally pass non-str content
            whose default repr was historically what the wire carried.
        user_id: Target user. Empty / None returns False without publishing.
        request_id: Per-request span id used by Nunba's adapter to key
            trace buffers.
        bot_type: 'Agent' for normal pipeline traces, 'ComputeRouter'
            for compute-routing status pushes.
        full_schema: True for the autogen GroupChat / action-start
            taps (adds zoom_bounding_box, page/analogy image URLs,
            preffered_language). False for tool closures and routing
            status pushes.
        preffered_language: Language tag (typo preserved). Only
            emitted when full_schema=True.
        page_image_url: URL of a page image the bubble is quoting —
            e.g. '/uploads/pdf_parse/<stem>/page_7.jpg' from
            integrations/learning/book_tools.py. Only emitted when
            full_schema=True (the non-full envelope has never carried
            the field). Defaults to '' so existing callers produce a
            byte-identical envelope.

    Returns:
        True if publish_async was invoked, False if HARTOS publisher
        is unresolvable or user_id is empty.  Exceptions inside
        publish_async are logged at debug and absorbed.
    """
    if not user_id:
        return False

    text_str = text if isinstance(text, str) else str(text)

    # Per-event dedup id — uuid4 hex.  Each thinking emit gets a unique
    # msg_id so multiple events sharing the same request_id (the typical
    # case — N thinking steps within one chat turn) are NOT collapsed by
    # client dedup (crossbarWorker.processedMessages, realtimeService.
    # _seenIds, Android processedRequestIds).  request_id stays the
    # GROUPING / filtering key; msg_id is the dedup key.
    _msg_id = uuid.uuid4().hex

    if full_schema:
        envelope = {
            'text': [text_str],
            'priority': CHAT_BUBBLE_PRIORITY,
            'action': action,
            'historical_request_id': [],
            'preffered_language': preffered_language,
            'options': [],
            'newoptions': [],
            'bot_type': bot_type,
            # Was an unconditional '' — the field has been in the wire schema
            # and parsed by every client all along (web/desktop
            # Demopage.js:2267 setUploadedImage(parsed.page_image_url);
            # Android CrossbarAnalogyResponse.java:21 @SerializedName), but
            # HARTOS never populated it, so no agent could quote a page image.
            # Default '' keeps every existing caller byte-identical on the wire.
            'page_image_url': page_image_url or '',
            'analogy_image_url': '',
            'request_id': request_id,
            'msg_id': _msg_id,
            'zoom_bounding_box': _ZOOM_STUB,
        }
    else:
        envelope = {
            'text': [text_str],
            'priority': CHAT_BUBBLE_PRIORITY,
            'action': action,
            'bot_type': bot_type,
            'request_id': request_id,
            'msg_id': _msg_id,
            'historical_request_id': [],
            'options': [],
            'newoptions': [],
        }

    try:
        from core.safe_hartos_attr import safe_hartos_attr
        publish_async = safe_hartos_attr('publish_async')
        if publish_async is None:
            logger.debug(
                "publish_thinking_trace: HARTOS publish_async unresolvable")
            return False
        publish_async(
            f'com.hertzai.hevolve.chat.{user_id}',
            json.dumps(envelope),
        )
        return True
    except Exception as e:
        logger.debug(f"publish_thinking_trace failed: {e}")
        return False


def publish_chat_stage(stage: str, *, user_id: str, request_id: str = '', text: str = None) -> bool:
    """Emit a chat-hot-path PROGRESS milestone (#508) on the chat topic with
    action='Status' — NOT 'Thinking'.

    These are canned pipeline stages ("Loading your context…", "Recalling…",
    "Preparing tools…"), not the model's reasoning.  Routing them through
    action='Thinking' made the frontend (Demopage: priority===49 &&
    action==='Thinking') append them to thinkingSteps, so they rendered as the
    "Thought process" Step 1..N — drowning out the actual LLM thoughts.  With
    action='Status' the priority-49 'Thinking' container is reserved for real
    autogen reasoning; a status consumer can drive the "analysing…" indicator
    from these instead.  When `text` is provided, use it directly (per-tool
    calls); otherwise look up CHAT_STAGE_TEXTS[stage]."""
    if not user_id:
        return False
    if text is None:
        from core.constants import CHAT_STAGE_TEXTS
        text = CHAT_STAGE_TEXTS.get(stage)
    if not text:
        logger.warning("publish_chat_stage: unknown stage %r", stage)
        return False
    return publish_thinking_trace(
        text=text, user_id=str(user_id), request_id=str(request_id or ''),
        bot_type='Agent', full_schema=False, action=CHAT_ACTION_STATUS,
    )


# What an agent sent the person DURING a chat turn, in order, by the turn's
# request id, until the turn's reply collects it (turn_sends).
#
# The reply's speech is made once, at the end of the turn, from the reply's
# text; a message sent earlier in the same turn was shown and never spoken.
# Live, 22 Nov 2026: a rhyme agent sent the rhyme, then a line of praise;
# only the praise was spoken.  A mid-turn send carries the turn's request id
# with '-intermediate-<n>' after it (send_request_id, which CREATE's and
# REUSE's send_message_to_user1 both use).  Nothing else is recorded here, so a notice
# published outside a turn (Reel Studio's) is never held, nor a send with no
# turn id in front (two such turns would hear each other's).  Kept for a
# bounded number of turns: a background turn's sends are never collected.
_TURN_SEND_MARK = '-intermediate'
_TURN_SENDS_KEPT = 256
# The most a turn's speech holds when it says the turn's sends with its
# reply: a little over two minutes of speech.  The one TTS worker
# (hart_intelligence_entry._tts_executor) speaks one clip at a time, so a
# turn that sent without end would hold the next turn's voice back.  Over
# it, the OLDEST whole messages are left out; a message is never cut, so a
# rhyme is never stopped mid-line.  The reply alone is spoken as before
# whatever its length.
_TURN_SPEECH_MAX_CHARS = 2000
_turn_sends: 'dict[str, list]' = {}
_turn_sends_lock = threading.Lock()


def _note_turn_send(request_id, text: str) -> None:
    turn, mark, _ = str(request_id or '').partition(_TURN_SEND_MARK)
    if not (turn and mark) or not text.strip():
        return
    with _turn_sends_lock:
        _turn_sends.setdefault(turn, []).append(text)
        while len(_turn_sends) > _TURN_SENDS_KEPT:
            _turn_sends.pop(next(iter(_turn_sends)))


def turn_sends(request_id) -> list:
    """The messages published to the person during the turn ``request_id``,
    oldest first; the record is emptied (a turn's reply is made once)."""
    with _turn_sends_lock:
        return _turn_sends.pop(str(request_id or ''), [])


# Numbered from the process's start in milliseconds, so a turn id that
# repeats across a restart (CREATE's '<user>_<prompt>' fallback) never
# repeats a send id the phone already holds a clip for (rv_voice delta b).
_send_numbers = itertools.count(int(time.time() * 1000))


def send_request_id(session_turn) -> str:
    """The id of one message an agent sends the person during a turn:
    '<turn>-intermediate-<n>', with an <n> no other send has.

    <turn> is the person's turn this thread is serving: the turn whose voice
    the /chat handler kept on the thread (turn_voice; autogen runs a tool on
    the thread that called it).  Else ``session_turn``, the newest turn the
    caller recorded for the session (its request_id_list), as before: a
    thread no person's turn started (the build-end job review, a scheduled
    run) and a turn that is not spoken (text mode, background) file their
    sends where they always did.  Two turns of one agent run at once when
    the person answers while a turn is still running (live 2026-11-27: seven
    typed answers from the phone); filed under the session's newest turn, an
    older turn's messages were collected and spoken by a turn they were not
    part of, and their own turn's end found none (#183).  A thread's own
    turn is never newer than the turn it serves.

    The number: CREATE's sends all carried '<turn>-intermediate', and the
    phone keeps one clip per id (AbstractChatActivity, requestId +
    videoRequestId), so a CREATE turn's second spoken send was never heard,
    and the turn's sends merged into one row there (rv_voice F3)."""
    turn = ''
    try:
        from hartos.threadlocal import thread_local_data
        voice = thread_local_data.get_turn_voice()
        turn = str((voice or {}).get('turn') or '')
    except Exception:
        logger.debug('send_request_id: no turn on this thread', exc_info=True)
    if not turn or _TURN_SEND_MARK in turn:
        turn = str(session_turn or '')
    return f'{turn}{_TURN_SEND_MARK}-{next(_send_numbers)}'


# What was already SPOKEN of a turn, one entry per send that got its own
# clip when it was sent (_speak_send), by the turn's request id.  The turn's
# reply leaves these out (turn_speech_text): a spoken send cannot be
# unspoken, so saying it again at the turn's end says it twice.
#
# Owner, 2026-11-24 17:46 IST, on a tablet that heard a turn's question only
# when the turn ended: "regardless of cause a text reaching the tablet shd
# also get the audio delivered as and when it was fanned out isnt?"  And
# 2026-11-27, after a demo turn from the phone ran 20 minutes with its
# questions shown and never heard: "only final reply is ever spoke wtf we
# have send user to message as a tool, why are they not spoken".  The phone
# plays only clips the server makes, and the desktop page stopped speaking
# another device's turns on 2026-11-24 (Nunba 5c8ee27d, the owner's "Only
# the device you used"), so a mid-turn send of a phone turn was spoken by no
# device until the turn ended.
_turn_spoken: 'dict[str, list]' = {}
# Turns whose sends went over the speech bound: their later sends are left
# to the turn's end too, so the turn is heard in order (_send_voice).
_turn_cut: 'dict[str, bool]' = {}


def turn_voice(body, request_id):
    """How the turn ``request_id`` whose /chat body is ``body`` is spoken:
    None when it is not spoken at all, else ``{'turn', 'avatar_id',
    'media_mode', 'language'}``.

    The ONE voicing decision for a turn: _chat_reply speaks the reply by it,
    and the /chat handler keeps it on the thread (thread_local_data
    .set_turn_voice) for the agent's sends during the turn (_speak_send), so
    a send and the reply are never voiced by two rules (hartos-17, build note
    3 on the 2026-11-24 ruling).

    Not spoken: a turn whose person chose text (media_mode 'text', #731),
    and a background turn (a non-empty id that is not a genuine person's,
    dispatch.is_genuine_user_request): its audio would go to the agent's own
    user id, which no client holds.  An empty id keeps speaking, as callers
    that predate request ids always did.  The avatar is the one the reply is
    spoken as (core.teacher_avatar.avatar_id_from); the language is the
    body's, else the person's persisted one, else English, the chat entry's
    own order."""
    body = body if isinstance(body, Mapping) else {}
    media_mode = body.get('media_mode')
    if media_mode == 'text':
        return None
    if request_id:
        try:
            from integrations.agent_engine.dispatch import (
                is_genuine_user_request)
            if not is_genuine_user_request(request_id):
                return None
        except Exception:
            logger.debug('turn_voice: background-turn gate skipped',
                         exc_info=True)
    avatar_id = None
    try:
        from core.teacher_avatar import avatar_id_from
        avatar_id = avatar_id_from(body.get('teacher_avatar_id'))
    except Exception:
        logger.debug('turn_voice: avatar id unread', exc_info=True)
    language = body.get('preferred_lang') or body.get('language')
    if not language:
        try:
            from core.user_lang import get_preferred_lang
            language = get_preferred_lang() or 'en'
        except Exception:
            language = 'en'
    return {'turn': str(request_id or ''), 'avatar_id': avatar_id,
            'media_mode': media_mode, 'language': language}


def _send_voice(request_id, text: str):
    """The voice a mid-turn send of ``request_id`` is spoken in when it is
    sent (_speak_send), or None when it is not: no send mark, nothing to
    say, or not the turn this thread is serving (see _speak_send)."""
    turn, mark, _ = str(request_id or '').partition(_TURN_SEND_MARK)
    if not (turn and mark) or not text.strip():
        return None
    try:
        from hartos.threadlocal import thread_local_data
        voice = thread_local_data.get_turn_voice()
    except Exception:
        logger.debug('_send_voice: no turn voice on this thread', exc_info=True)
        return None
    if not voice or voice.get('turn') != turn:
        return None
    from core.safe_hartos_attr import safe_hartos_attr
    if safe_hartos_attr('_tts_synthesize_and_publish') is None:
        return None             # no speech here (HARTOS not loaded)
    # The turn's one speech bound (see _speak_send), decided here, before
    # the message is published marked 'voiced': a client told a clip is
    # coming waits for it.  Once one send is over it, no later send of the
    # turn is voiced either: a shorter one that still fitted would be heard
    # before the one left to the turn's end (rv_voice N1).
    with _turn_sends_lock:
        if turn in _turn_cut:
            return None
        said = sum(len(t) for t in _turn_spoken.get(turn, ()))
        if said + len(text) > _TURN_SPEECH_MAX_CHARS:
            _turn_cut[turn] = True
            while len(_turn_cut) > _TURN_SENDS_KEPT:
                _turn_cut.pop(next(iter(_turn_cut)))
            logger.info("_send_voice: turn %s has spoken %d chars; this send "
                        "and the turn's later ones are left to its end",
                        turn, said)
            return None
    return voice


def _speak_send(request_id, text: str, user_id, voice=None) -> bool:
    """Speak one message the agent just sent the person, as it is sent, under
    the send's own id ('<turn>-intermediate-<n>'), so a client plays it only
    on the device that started that turn (Nunba ttsGuards.isTurnVoicedHere,
    Android ChatBubble.playsVoiceOf: both cut the id at the send mark).

    Spoken only for the turn this thread is serving, by that turn's voicing
    decision (turn_voice, kept on the thread by the /chat handler): a send
    with no turn in front, another turn's send, a text-mode turn and a
    background turn are not spoken here, and the turn's reply says them as
    before (turn_speech_text).  The text is not rewritten by the model before
    it is spoken (llm_rewrite=False): that rewrite is one more call on the
    one local model the turn itself is waiting for (hartos-17, cost note).

    A turn's speech is bounded once: the sends spoken as they are sent and
    what its end says share _TURN_SPEECH_MAX_CHARS.  A send that would take
    the turn past it is not spoken here (_send_voice says no, before it is
    published); the turn's end says what still fits (turn_speech_text).  A
    looping flow sends without end, and the one TTS worker would speak every
    send before the next turn's voice (rv_voice F4).

    On a video turn a send is spoken as audio: the face is the reply's.  A
    live face per send started one talking-face stream on the GPU for each,
    and the page, which plays one face at a time, cut send N with send N+1
    (rv_voice F7).
    True when a clip was asked for."""
    voice = voice or _send_voice(request_id, text)
    if not voice:
        return False
    turn = voice['turn']
    from core.safe_hartos_attr import safe_hartos_attr
    speak = safe_hartos_attr('_tts_synthesize_and_publish')
    if speak is None:
        return False
    media_mode = voice.get('media_mode')
    if media_mode == 'video':
        media_mode = 'audio'
    try:
        speak(text, user_id, request_id, language=voice.get('language'),
              avatar_id=voice.get('avatar_id'),
              media_mode=media_mode, llm_rewrite=False)
    except Exception as e:
        logger.warning("_speak_send: the send of turn %s is not spoken: %s",
                       turn, e)
        return False
    with _turn_sends_lock:
        _turn_spoken.setdefault(turn, []).append(text)
        while len(_turn_spoken) > _TURN_SENDS_KEPT:
            _turn_spoken.pop(next(iter(_turn_spoken)))
    return True


def turn_spoken(request_id) -> list:
    """The sends of the turn ``request_id`` that were spoken as they were
    sent, oldest first; the record is emptied (a turn's reply is made
    once)."""
    with _turn_sends_lock:
        _turn_cut.pop(str(request_id or ''), None)
        return _turn_spoken.pop(str(request_id or ''), [])


def turn_speech_text(request_id, reply) -> str:
    """What is spoken for the turn ``request_id`` whose reply is ``reply``:
    every message the person was sent during it, in the order they were
    shown, then the reply, each said once.  The turn's record is emptied
    whatever is spoken, so a silent turn and an empty reply leave nothing
    behind.  A turn that sent nothing, and an empty reply, are spoken
    exactly as before: the reply.

    The reply alone used to be spoken, so a turn that sent a rhyme and then
    a line of praise spoke the praise only (live walk, 22 Nov 2026).  The
    sends are the ones the publisher delivered to the person (turn_sends):
    text written FOR them, never the group's control text.

    Said once, by WHOLE words (case and the marks around a word aside):
    a message takes the place of every earlier one whose words it holds,
    where the later one was shown, so "Great!", a rhyme, then the reply
    "Great! You sang it all." says the rhyme and then the reply, a reply
    that repeats two sends says each once, and the same words sent again
    are said once, where they were said last.  A message is never left out
    because an earlier one holds its words: after "Say 'ba'." the reply
    "Ba!" is the child's cue to repeat, and is said (review of a6dbff622).
    "Ok" after "Look, a book!" is said too: "ok" is not a word of it.  Over
    _TURN_SPEECH_MAX_CHARS, the oldest whole messages are left out; the
    reply, the newest, never is, and a reply longer than that alone is
    spoken alone, as before.

    A send that was spoken when it was sent (_speak_send, owner 2026-11-24)
    is not said again here, and a reply whose words are EXACTLY the NEWEST
    send's, when that send was spoken (case and the marks around words
    aside), is not said at all: a held turn's reply IS its newest send.  A
    reply equal to an OLDER spoken send is said: "Say 'ba'!", "Good try!",
    then the reply "Say ba." is the cue again, after other speech (rv_voice
    F2).  A reply that only holds a spoken send's words with more is said
    ("Ba!" after "Say 'ba'.", hartos-17 build note 2).  What was spoken as
    it was sent counts against _TURN_SPEECH_MAX_CHARS here (rv_voice F4)."""
    sent = turn_sends(request_id)
    spoken = turn_spoken(request_id)
    if not (sent or spoken) or not reply:
        return reply
    reply = reply if isinstance(reply, str) else str(reply)
    newest_spoken = (spoken[-1] if spoken and sent and sent[-1] == spoken[-1]
                     else None)
    budget = max(0, _TURN_SPEECH_MAX_CHARS - sum(len(t) for t in spoken))
    for text in spoken:
        if text in sent:
            sent.remove(text)       # one spoken send takes out one shown one

    def _words(text):
        # ' w1 w2 ' with a space around each word, so a run of whole words
        # is found by `in`.  A word keeps its letters and the marks inside
        # it (a Tamil vowel sign is part of its word); the punctuation and
        # symbols around it (emoji too) go.
        words = []
        for token in str(text or '').split():
            i, j = 0, len(token)
            while i < j and unicodedata.category(token[i])[0] in 'PS':
                i += 1
            while j > i and unicodedata.category(token[j - 1])[0] in 'PS':
                j -= 1
            if i < j:
                words.append(token[i:j].casefold())
        return f" {' '.join(words)} " if words else ''

    if (newest_spoken is not None and _words(reply)
            and _words(newest_spoken) == _words(reply)):
        reply = ''          # already heard, word for word, when it was sent
    if not reply:
        sent = [text for text in sent if _words(text)]
        if not sent:
            return ''

    kept = []           # [words, text], in the order they are said
    for text in list(sent) + [reply]:
        said = _words(text)
        if said:
            kept = [k for k in kept if k[0] not in said] + [[said, text]]

    def _length(entries):
        return sum(len(e[1]) for e in entries) + 2 * max(0, len(entries) - 1)

    while kept and _length(kept) > budget:
        kept.pop(0)
    return '\n\n'.join(e[1] for e in kept) if kept else reply


def publish_agent_message(
    *,
    text: Any,
    user_id: str,
    request_id: str,
    prompt_id: Any,
    inp: Any = '',
    media: Any = None,
    skip_crossbar: bool = False,
) -> bool:
    """Deliver an agent's message to the user on the local chat topic.

    The one on-device path for send_message_to_user1 in BOTH create_recipe
    and reuse_recipe.  REUSE had no such path and POSTed every message to
    the cloud host aws_rasa:9890, which a desktop cannot reach (measured in
    the Nunba gui_app.log 2026-09-26: WinError 10061, the agent's question
    never arrived).  The envelope is the one CREATE has published since
    2026-06-09 and chat-stream subscribers parse (text, request_id,
    prompt_id, bot_type, options, page_image_url).

    ``persona``: the persona the person this message is for has with this
    agent, so their screen names it beside the agent's name (owner
    2026-11-25; design_1125 B), from core.flow_progress.persona_of, the one
    reader the question card and the /chat reply use too.  No persona
    placed, no such key: never a guess.

    ``media``: reply media items (Reel Studio's reel, plan 4.2(f)) the
    message carries under ``media``; non-object entries are dropped.  A
    message still needs text: media alone is not sent.  Without media the
    envelope is exactly the one above.  With media, the first ``image``
    item's url also fills ``page_image_url`` -- the same slot a book page
    has always used, which is where the owner wants an agent's picture
    (ruling 2026-10-25; see the note at the assignment).

    ``skip_crossbar``: keep the message on this node's own legs -- LOCAL,
    SSE and the user's own devices over PeerLink, which is how the phone
    receives agent messages (EVENTS) -- and off central's crossbar (decision
    U4(d): a reel push names her clients).  It is publish_async's own flag
    (the one publisher, amendment A9 e), passed only when set: every other
    message reaches publish_async, and any wrapper around it, exactly as
    before.  Nunba's thinking-trace wrapper (routes/hartos_backend_adapter
    ._capturing_thinking) forwards it when set.  A wrapper that does not
    (an older Nunba's, which takes (topic, message, timeout)) raises, and
    the push is reported undelivered rather than sent to the crossbar.

    ``voiced`` (set only when true): this message's own speech clip follows
    under the same request_id (_speak_send), so a client that speaks a
    pushed text itself (Nunba's chat page) leaves it to the clip and the
    person hears it once.

    Returns True only when the publisher was resolved and returned without
    raising; False for empty text or user, an unresolvable publisher, or a
    raising one.  Callers report that result instead of claiming delivery.
    """
    text_str = text if isinstance(text, str) else str(text or '')
    # An elided-text pointer a model copied into its message is never shown
    # to the user (owner ruling 2026-09-27; core.llm_outbound_logger).
    from core.llm_outbound_logger import strip_elided_pointers
    text_str = strip_elided_pointers(text_str)
    if not user_id or not text_str:
        return False
    payload = {
        'text': [text_str],
        'request_id': request_id,
        'prompt_id': prompt_id,
        'bot_type': 'Custom GPT',
        'options': [],
        'newoptions': [],
        'page_image_url': '',
        'analogy_image_url': '',
        'probe': False,
        'inp': inp,
    }
    from core.flow_progress import persona_of
    persona = persona_of(user_id, prompt_id)
    if persona:
        payload['persona'] = persona
    voice = _send_voice(request_id, text_str)
    if voice:
        payload['voiced'] = True
    items = [dict(m) for m in media if isinstance(m, Mapping)] \
        if isinstance(media, (list, tuple)) else []
    if items:
        payload['media'] = items
        # An agent's picture rides page_image_url, the SAME slot a book page
        # has always used.  Owner ruling 2026-10-25, relayed verbatim: "we
        # show page image url and analogy images", "it shd be the same exact
        # way we used t show earlier", "what filter per agent when did we
        # write one agent specific contract".
        #
        # WARNING TO THE NEXT READER: 0b632c59f removed this and argues the
        # case well -- a book-page MODE, a double render, an unloadable
        # phone card. It was OVERRULED, so do not re-derive it from that
        # commit alone.
        #
        # Its Android premise is NOT refuted, only unmeasured: two
        # independent readings say Glide treats a leading "/" as a local
        # file, so a relative url may genuinely fail on the phone. Measure
        # it on the Pad before relying either way.
        #
        # This is also why no per-agent contract branch exists: the reel
        # contract describes a reel, and filtering every agent's reply
        # through it was the mistake, not the missing branch.
        payload['page_image_url'] = next(
            (it['url'] for it in items
             if it.get('kind') == 'image' and isinstance(it.get('url'), str)), '')
        # Which items the slot could NOT take, so a kind is never dropped
        # from the record in silence.  It says only what THIS function
        # knows -- the slot took one image, the rest ride `media` -- and
        # NOTHING about what a client renders, which the publisher cannot
        # know: Reel Studio publishes every reel through here
        # (reel_studio/pipeline.py:569) and the phone does render those.
        # info, not warning: it is a fact about the envelope.
        # str() on every kind, because this sort MUST NOT RAISE: it sits
        # outside the try below, so a set mixing 'video' with a non-string
        # kind threw TypeError and lost the whole message for a log line.
        # The item itself is unaffected either way -- it still rides
        # `media` as sent -- so this protects the publish and nothing else.
        rides_media_only = sorted(
            {str(it.get('kind')) for it in items
             if it.get('kind') is not None and it.get('kind') != 'image'})
        if rides_media_only:
            # Says what the slot ACTUALLY did, in both branches. A reel's
            # items are videos and the slot stays empty, so a fixed
            # "carries the first image" would be false on every reel.
            logger.info(
                "publish_agent_message: page_image_url %s; %s item(s) ride "
                "`media` (user %s)",
                'carries the first image' if payload['page_image_url']
                else 'is empty',
                ', '.join(rides_media_only), user_id)
    try:
        from core.peer_link.message_bus import chat_topic_for
        from core.safe_hartos_attr import safe_hartos_attr
        publish_async = safe_hartos_attr('publish_async')
        if publish_async is None:
            logger.warning(
                "publish_agent_message: HARTOS publish_async unresolvable; "
                "message for user %s not delivered", user_id)
            return False
        flags = {'skip_crossbar': True} if skip_crossbar else {}
        publish_async(chat_topic_for(user_id), payload, **flags)
        _note_turn_send(request_id, text_str)
        # Spoken now, after its text went out: the phone plays a clip only
        # once it holds a text row under the clip's exact request id
        # (Android AbstractChatActivity.waitForTextResponse, per cointinue
        # 2026-11-27), and the clip carries this send's own id.
        if voice:
            _speak_send(request_id, text_str, user_id, voice=voice)
        return True
    except Exception as e:
        logger.warning(
            "publish_agent_message: publish for user %s failed: %s", user_id, e)
        return False
