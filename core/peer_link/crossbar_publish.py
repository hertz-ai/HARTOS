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

import json
import logging
import threading
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
# with '-intermediate' after it: REUSE's send_message_to_user1 adds '-<n>'
# to that, CREATE's adds nothing (a first run's sends were missed while the
# mark was '-intermediate-').  Nothing else is recorded here, so a notice
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


def whole_words(text) -> str:
    """``text`` as ' w1 w2 ': its whole words, casefolded, a space around
    each, so a run of whole words is found by `in` and two texts with the
    same words compare equal.  A word keeps its letters and the marks inside
    it (a Tamil vowel sign is part of its word); the punctuation and symbols
    around it (emoji too) go.  '' when there is no word.  The one reader of
    "the same words": turn_speech_text says each message once by it, and the
    send tool refuses an answered question asked again word for word by it
    (lifecycle_hooks.answered_question_asked_again)."""
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
    spoken alone, as before."""
    sent = turn_sends(request_id)
    if not sent or not reply:
        return reply
    reply = reply if isinstance(reply, str) else str(reply)

    kept = []           # [words, text], in the order they are said
    for text in list(sent) + [reply]:
        said = whole_words(text)
        if said:
            kept = [k for k in kept if k[0] not in said] + [[said, text]]

    def _length(entries):
        return sum(len(e[1]) for e in entries) + 2 * max(0, len(entries) - 1)

    while kept and _length(kept) > _TURN_SPEECH_MAX_CHARS:
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
        return True
    except Exception as e:
        logger.warning(
            "publish_agent_message: publish for user %s failed: %s", user_id, e)
        return False
