"""The phone's two chat paths, chat/custom_gpt and chat/teachme2: one home.

Android (CustomChatBotAPI, TeachYourselfActivity) and RN (chatApi.customGpt,
its kids calls) post every agent turn to chat/custom_gpt and every Teach
Yourself turn to chat/teachme2, on a base URL their device -> LAN -> cloud
cascade picks.  Two kinds of node answer them: central HARTOS
(hart_intelligence_entry, behind Kong) and a desktop Nunba
(routes/chatbot_routes, which the phone reaches on the LAN).

Each node runs the turn its own way -- central through its /chat, Nunba
through its own /chat, which wraps HARTOS's -- so each passes ``run_turn``.
Everything else is here once: what a body means (turn_text,
settle_request_id), how a topic is taught (tutor_opening, the teach agent),
when a turn counts as answered (turn_outcome), and the reply's shape
(agent_reply_body).  Moved from Nunba routes/chatbot_routes.py on 2026-11-20,
when central began serving these paths itself.

``run_turn(turn) -> (body, status)`` takes a turn in the shape Nunba's /chat
reads ({'text', 'user_id', 'request_id', 'conversation_id', 'draft_first',
'prompt_id'?, ...}) and returns that /chat's reply body and status.
"""
import logging
import os
import re
import threading

from core.session_cache import TTLCache

logger = logging.getLogger(__name__)

#: What the phone shows when a turn produced no answer.  Never the error
#: itself: exception text and backend envelopes are not for the user.
TURN_FAILED_TEXT = "Sorry, I couldn't answer that just now. Please try again."

#: A request id a client may name for its own turn.  The phone mints one per
#: turn so that a reply arriving after its wait (the push, file_list recovery)
#: finds the bubble waiting for it; the web page and the watch send one per
#: turn too.  The id keys the turn's state on the node (HARTOS's per-turn
#: caches, and the user file store's binding of the turn's attachments:
#: core.chat_attachments.bind_inbound), so only this alphabet and length are
#: taken from a client.
CLIENT_REQUEST_ID = re.compile(r'[A-Za-z0-9_-]{8,64}')

#: The goals scopes the Teach Yourself screen sends when the learner picks
#: what to learn.  Other callers post chat/teachme2 as a transport (kids
#: progress reports, catalog requests); those must not start a topic.
TEACH_SCOPES = ('book', 'course', 'topic', 'goals')

#: What a learner sends to go on with the current topic.
TEACH_CONTINUE = ('continue', 'next', 'go on')

#: The goals scopes the kids clients post to chat/teachme2 as a TRANSPORT
#: (iOS and RN's committed kidsLearningApi: getAdaptiveQuestion 'kids_game',
#: reportGameCompletion 'progress_report', syncResults 'batch_sync').  They
#: are not lessons.  Central answers them with an error, and that error is
#: what makes the client keep the result and retry; a 2xx would drop it.
KIDS_TRANSPORT_SCOPES = ('kids_game', 'progress_report', 'batch_sync')

#: The agent that serves Teach Yourself once it is built (owner ruling
#: 2026-09-14: Teach Yourself is a created agent).  54 is the prompt_id of
#: the Nunba chat page's "Personalised Learning" agent (Demopage.js), the one
#: its sidebar launches, so the phone and the desktop talk to the SAME agent.
#: NUNBA_TEACH_AGENT_ID overrides it; an empty value turns it off.
TEACH_AGENT_PROMPT_ID = '54'

#: Each learner's current topic on a node that keeps no other per-user turn
#: state (central).  Android sends the topic (`goals`) on the first turn only,
#: so "continue" needs it kept; bounded, and gone after two idle hours.  Nunba
#: passes its own bounded `sessions` store instead.
TEACH_TOPICS = TTLCache(ttl_seconds=7200, max_size=500, name='phone_teach_topics')
TEACH_TOPICS_LOCK = threading.Lock()


def reply_parts(body):
    """The reply's text as its non-empty parts: a multi-part reply keeps
    every part, and a reply that is Liquid UI alone has none."""
    text = body.get('text') or body.get('response') or ''
    parts = text if isinstance(text, list) else [text]
    return [p for p in parts if isinstance(p, str) and p.strip()]


def agent_reply_body(turn_body, request_id, dynamic_data=None):
    """The reply to a chat turn: ONE shape for every agent and every path.

    It is the body /chat returns (text, the agent and routing fields,
    dynamic_layout, ...) plus two things:

      * `dynamic_data`, the flexible map.  Any agent puts its own keys here
        (a Teach Yourself topic and its "continue" option, a custom agent's
        name and avatar), merged over whatever the turn itself returned.
        Liquid UI binds dynamic_layout's nodes to it: the same layout + data
        pair HARTOS's core.peer_link.ui_commands.ui_overlay_show sends, and
        the key RN/iOS LiquidOverlay already read (res.dynamic_data).  A
        client that does not know a key ignores it.
      * what a phone's native chat screen needs to show any reply.  Android
        parses this body as Revision_Response_Message, which has no `text`
        field: the bubble is drawn from answerList, a message without a
        priority is dropped (AbstractChatActivity.addNewMessage), answer
        chips come from optionLists (the map's `options`) and the Teach
        Yourself mode from `teachme`.  Derived the same way for every agent.
    """
    body = dict(turn_body or {})
    # RN's CustomBotChatScreen shows `message` before `text`, so nothing but
    # the reply may sit there; central's reply carries no `message` at all.
    body.pop('message', None)
    parts = reply_parts(body)
    data = dict(body.get('dynamic_data') or {})
    data.update(dynamic_data or {})
    body.update({
        # a string, as central sends; every part stays in answerList
        'text': '\n'.join(parts),
        # never [] -- AbstractChatActivity reads answerList.get(0)
        'answerList': parts or [''],
        'priority': 99,
        'optionLists': [str(o) for o in (data.get('options') or [])],
        'request_id': str(request_id),
        'dynamic_data': data,
    })
    if 'teachme' in data:
        body['teachme'] = bool(data['teachme'])
    return body


def turn_text(data):
    """The message of a chat/custom_gpt or chat/teachme2 body.

    `text` first: every caller puts the message there -- Android as a
    one-item list (translated to English; `raw_text` keeps the original),
    RN's custom-bot screen as a string, and RN's kids calls their full
    instruction, with a short label in `raw_text` -- so `raw_text` never wins
    over it.  `message` is RN's alias.
    """
    for key in ('text', 'message', 'raw_text'):
        value = data.get(key)
        if isinstance(value, list):
            value = value[0] if value else ''
        if isinstance(value, str) and value.strip():
            return value
    return ''


def settle_request_id(sent, fresh, route):
    """The id a turn whose body names ``sent`` runs under: ``sent`` when a
    client may name the turn, else ``fresh``.

    A client may name a turn with a well-formed id (CLIENT_REQUEST_ID) that
    does not carry HARTOS's background tag (core.chat_client.DAEMON_PREFIX,
    its one spelling): HARTOS reads a ``daemon_<goal>`` turn as the agent
    daemon's own work (dispatch.is_genuine_user_request), so it would keep
    that goal's lane, not mark the person active, and book escalations to
    the goal (create_recipe, daemon_goal_id).  An id that is not taken is
    logged by its shape, never by its value.
    """
    if sent in (None, '') or (sent == 0 and not isinstance(sent, bool)):
        return fresh                      # no id: the turn gets one
    rid = (str(sent) if isinstance(sent, (str, int))
           and not isinstance(sent, bool) else None)
    if not (rid and CLIENT_REQUEST_ID.fullmatch(rid)):
        why = 'is not a request id'
    else:
        try:
            # Read when asked, never copied at import: the tag has one
            # spelling, core.chat_client's.
            from core.chat_client import DAEMON_PREFIX
        except Exception:
            # Fail closed: without HARTOS's tag a person's turn cannot be told
            # from the daemon's, so no client's id is taken, well formed or not.
            logger.warning("%s: core.chat_client is unavailable, so a person's "
                           "turn cannot be told from the daemon's; the turn "
                           "runs as %s, not under the id the body names",
                           route, fresh, exc_info=True)
            return fresh
        if not rid.startswith(DAEMON_PREFIX):
            return rid
        why = "carries HARTOS's background tag"
    # Shape only, never the value: it is whatever the client sent.
    logger.info("%s: the body's request_id (%s, length %s) %s; the turn runs "
                "as %s", route, type(sent).__name__, len(rid) if rid else '-',
                why, fresh)
    return fresh


def turn_outcome(body, status):
    """(body, status) of a turn as its /chat answered it.

    A turn that failed, or answered with neither text, nor a Liquid UI
    payload (dynamic_layout / dynamic_data: a reply can be the overlay
    alone), nor `media` (a reel can be the whole answer), comes back as an
    empty body with a non-2xx status; why stays in the log, never in the
    reply.
    """
    body = body if isinstance(body, dict) else {}
    if status >= 400:
        return {}, status
    if not (reply_parts(body) or body.get('dynamic_layout')
            or body.get('dynamic_data') or body.get('media')):
        return {}, 502
    return body, status


def custom_gpt_turn(data, request_id, run_turn):
    """(reply body, status) of a chat/custom_gpt turn: a turn with a custom
    agent, run by ``run_turn`` and answered in the one agent reply shape.

    draft_first is off because these screens show only this synchronous
    reply, and a draft's standby ("let me check...") would otherwise be the
    whole answer.  A Reel Studio turn carries attachments and goal_tags,
    which go on the turn as sent; the node's /chat bounds them by HARTOS's
    contracts (turn_attachments, turn_goal_tags; core.chat_attachments), and
    the `media` HARTOS answers with is in the reply as HARTOS sent it.
    """
    text = turn_text(data)
    if not text.strip():
        return agent_reply_body(
            {'text': "Request cannot be blank, Please enter something as request.."},
            request_id), 400
    prompt_id = data.get('prompt_id') or data.get('bot_id')
    turn = {
        'text': text,
        'user_id': data.get('user_id', 'guest'),
        'request_id': request_id,
        'conversation_id': (data.get('conversation_id')
                            or f'custom_{prompt_id or "default"}'),
        'create_agent': bool(data.get('create_agent')),
        'teacher_avatar_id': data.get('teacher_avatar_id'),
        'draft_first': False,
    }
    if prompt_id not in (None, '', 0, '0'):
        turn['prompt_id'] = prompt_id
    for key in ('preferred_lang', 'language', 'media_mode',
                'attachments', 'goal_tags'):
        if data.get(key):
            turn[key] = data[key]
    body, status = run_turn(turn)
    if not body:
        return agent_reply_body({'text': TURN_FAILED_TEXT}, request_id), status
    return agent_reply_body(body, request_id), 200


def tutor_opening(goals, learner_text):
    """The first turn of a topic: what to teach, and how much of it.

    Deliberately short.  Measured 2026-09-13, an opening carrying HARTOS's
    full tutor persona ran past the 120 s LLM timeout on a desktop node,
    while the same model answered a scoped "first step, then one check
    question" turn in about 45 s.
    """
    name = goals.get('name') or goals.get('book_name') or 'this topic'
    book, pages = goals.get('book_name') or '', goals.get('pages') or []
    where = f' (pages {pages[0]}-{pages[-1]} of "{book}")' if pages and book else ''
    about = f" -- {goals['text']}" if goals.get('text') else ''
    ask = learner_text or f'Teach me {name}.'
    return (f'{ask}\nYou are my patient tutor. Teach {name}{where}{about}: '
            f'the first step only, simply, then one quick check question.')


def teach_agent_prompt_id():
    """The Teach Yourself agent's prompt_id when it is built, else None.

    "Built" is HARTOS's own question (core.flow_recipe_optimizer.recipe_exists:
    the flow-0 recipe is in the prompts dir /chat reads).  Until it is, the
    turn names no agent: /chat given the id of an agent that has a config and
    no recipe starts BUILDING it (measured 2026-10-05 on id 54: "I need your
    input to finish building this agent"), which is not what a learner's turn
    should do.
    """
    pid = os.environ.get('NUNBA_TEACH_AGENT_ID', TEACH_AGENT_PROMPT_ID).strip()
    if not pid:
        return None
    try:
        from core.flow_recipe_optimizer import recipe_exists
        return pid if recipe_exists(pid, 0) else None
    except Exception:
        logger.warning('Teach Yourself agent %s: cannot tell whether it is '
                       'built; serving the turn without it', pid, exc_info=True)
        return None


def teachme2_turn(data, request_id, run_turn,
                  topics=TEACH_TOPICS, topics_lock=TEACH_TOPICS_LOCK):
    """(reply body, status) of a chat/teachme2 turn: Teach Yourself.

    Android's TeachYourselfActivity posts every turn here.  A newly picked
    topic is framed as a short tutoring request (tutor_opening).  Android
    sends the topic (`goals`) on the first turn only, so it is kept on the
    learner's entry in ``topics`` (keyed by the body's user_id, which the
    host has already set to the verified account where there is one) and
    "continue" keeps teaching it.  The topic and its "continue" option travel
    in the reply's dynamic_data, like any agent's own fields.

    TRANSITIONAL (owner ruling 2026-09-14): Teach Yourself is a created
    agent.  The turn names it (teach_agent_prompt_id) once its recipe is
    built; until then it runs without an agent id.
    """
    user_id = str(data.get('user_id', 'guest'))
    text = turn_text(data)
    goals = data.get('goals')
    if isinstance(goals, dict) and goals.get('scope') in KIDS_TRANSPORT_SCOPES:
        return agent_reply_body({'text': 'This request is not served on this node.'},
                                request_id), 501
    with topics_lock:
        entry = topics.get(user_id) or {}
        if isinstance(goals, dict) and goals.get('scope') in TEACH_SCOPES:
            entry['teach_topic'] = goals
            text = tutor_opening(goals, text)
        topic = entry.get('teach_topic')
        # Assigned, not setdefault: a bounded store enforces its cap in
        # __setitem__, which dict.setdefault never calls.
        topics[user_id] = entry
    if topic and text.strip().lower() in TEACH_CONTINUE:
        name = topic.get('name') or topic.get('book_name') or 'the topic'
        text = (f'Continue teaching {name}: the next step, '
                f'then one quick check question.')
    teach_data = ({'topic': topic.get('name') or topic.get('book_name') or '',
                   'options': ['continue'], 'teachme': True} if topic else {})
    if not text.strip():
        return agent_reply_body({'text': "Please enter something as response.."},
                                request_id, dynamic_data=teach_data), 400
    turn = {
        'text': text,
        'user_id': user_id,
        'request_id': request_id,
        'conversation_id': f'teachme_{user_id}',
        'teacher_avatar_id': data.get('teacher_avatar_id'),
        'draft_first': False,
    }
    agent = teach_agent_prompt_id()
    if agent:
        turn['prompt_id'] = agent
    body, status = run_turn(turn)
    if not body:
        return agent_reply_body({'text': TURN_FAILED_TEXT}, request_id,
                                dynamic_data=teach_data), status
    return agent_reply_body(body, request_id, dynamic_data=teach_data), 200
