"""integrations.agent_engine.person_ask -- the server asks the person.

Owner, 2026-10-27: "liquid ui is asked as a tool by server, same for consent
screems and other liquid ui screens", "the mechanism shd be same agnostic of
hartos consumer, liqiuid ui shd also fanout", "same for consents via firebase
shd fanout in desktop, android, wamp etc".  Design and the register rules it
traces to: docs/design/PERSON_ASK_LIQUID_UI_TOOL.md.

An ask is one Liquid UI component put in front of one person, on every
surface they have, answered on any one of them, and its answer handed back to
the agent that asked.  This module keeps no transport of its own; it joins
the ones that exist, in this order:

  * ONE Liquid UI card, through liquid_ui_service.push_agent_ui, under the
    ask's id (agent_ui_compose keeps a card's ``ask_id`` as its msg_id): the
    desktop page (Nunba AgentOverlay), the shell and the person's own topic
    (the phone).  It goes FIRST, because its gates are the human's: the
    Liquid UI kill switch, a halted hive, unsafe content, the allowlist and
    the per-agent rate cap.  A card they refuse means nothing else is sent.
  * ONE Notification row, written by NotificationService.create with a push,
    under the same id (H-1022), so the Firebase leg of
    realtime.on_notification rings the phone once however often the ask is
    fanned out.  It lists as an open question until it is answered (H-1030,
    N-0628).
  * The answer, from whichever surface, goes through answer_ask(): the person
    asked and nobody else, the first answer only.  It is then announced as
    ``ask.answered`` on the person's own topic and SSE (realtime.on_notification,
    the fan-out consent answers use), and every surface drops its copy
    (H-1024); the row is marked read and the card leaves the shell's store.
    Reading the row in the bell is NOT an answer and drops nothing.

The one door that calls answer_ask() is /api/agent/approval with an
``ask_id`` (hart_intelligence_entry, and the shell's own route), always with
the caller's verified Bearer identity (answer_from_request).  No reply topic
is a door: central's router is anonymous, so a payload published there could
answer for anyone (decision 2026-10-27 in the design doc).

Never asked or answered this way: a secret.  A component that asks for one is
refused before anything leaves, and an answer that carries one is refused; a
credential is asked by ai_key_vault.request_credential and lands in the vault,
never in a tool result, a log or the ledger (H-0986).

The asks live in this process.  An ask this process no longer holds (a
restart, or ANSWERED_KEEP_S after its answer) reads ``expired`` to the person
and to the agent, never ``waiting``.
"""
import json
import logging
import threading
import time
import uuid
from typing import Annotated, Any, Dict, Optional, Tuple

logger = logging.getLogger('hevolve_social')

#: The Notification.type of an ask (the column holds 30 characters).
ASK_TYPE = 'agent_ask'
#: Notification.target_type of an ask this module answers.  Consent asks
#: (slice S4) name their own store here, so the answer knows its writer.
ASK_STORE = 'person_ask'
#: The event every surface drops an ask's card on.
ANSWERED_TYPE = 'ask.answered'
#: How long a tool call waits for the person by default, and at most, before
#: it hands back a held ask; the answer is read on a later turn by its id.
DEFAULT_WAIT_S = 20
MAX_WAIT_S = 60
#: How long an answered ask stays readable here, and how long an unanswered
#: one is kept waiting for its answer.
ANSWERED_KEEP_S = 3600
OPEN_KEEP_S = 24 * 3600
#: An answer is a small, shallow object, never a payload.
MAX_ANSWER_BYTES = 16 * 1024
MAX_ANSWER_DEPTH = 4
#: Open asks one person may have at once (a runaway agent cannot ring her
#: phone without end).  The same question asked again is the same ask.
MAX_OPEN_PER_PERSON = 3

#: Keys that type a field, the keys that name one, and the keys whose value is
#: words the person reads (a field's label, a card's title).
_TYPE_KEYS = ('type', 'input_type', 'inputType', 'format', 'kind')
_NAME_KEYS = ('name', 'key', 'field')
_TEXT_KEYS = ('label', 'placeholder', 'title', 'description', 'hint', 'help',
              'prompt', 'question')
#: Beyond the secret key words (security.edge_privacy.is_secret_key), what a
#: person is only ever asked through the vault: a one-time code, a PIN, a
#: card's number or code, a wallet's phrase.  Ask-only: on the wire 'pin' is a
#: GPIO pin, so the egress scrub does not withhold these.
_CREDENTIAL_WORDS = ('otp', 'otp_code', 'one_time_code', 'pin', 'pin_number',
                     'passcode', 'cvv', 'cvc', 'card_number', 'secret_key',
                     'seed_phrase', 'recovery_phrase')
#: In a sentence these are everyday words ('pass the clips', 'a token of
#: thanks', 'pin it'), so alone they ask for nothing; 'UPI PIN' or 'access
#: token' still does.
_EVERYDAY_WORDS = frozenset({'pass', 'token', 'secret', 'cookie', 'credential',
                             'credentials', 'pin'})
#: Little words a phrase that names a secret does not carry ('a token').
_FUNCTION_WORDS = frozenset({'a', 'an', 'the', 'your', 'my', 'our', 'their',
                             'his', 'her', 'its', 'this', 'that', 'of', 'to',
                             'for', 'and', 'or', 'any', 'some', 'no', 's'})

_lock = threading.Lock()
_asks: Dict[str, '_Ask'] = {}


class _Ask:
    __slots__ = ('ask_id', 'user_id', 'agent_id', 'prompt_id', 'ctype', 'purpose',
                 'max_files', 'same', 'created', 'answered_at', 'answer', 'event')

    def __init__(self, ask_id, user_id, agent_id, prompt_id, ctype, card, same):
        self.ask_id = ask_id
        self.user_id = user_id
        self.agent_id = agent_id
        self.prompt_id = prompt_id
        self.ctype = ctype
        self.purpose = card.get('purpose') if ctype == 'file_request' else None
        self.max_files = card.get('max_files') if ctype == 'file_request' else None
        self.same = same
        self.created = time.time()
        self.answered_at = None
        self.answer = None
        self.event = threading.Event()


def _refused(reason) -> dict:
    return {'ok': False, 'ask_id': None, 'refused': reason, 'card': False}


def _walk(value):
    """Every dict and list in ``value``, without recursion (an agent's
    component can be as deep as it likes)."""
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            yield v
            stack.extend(v.values())
        elif isinstance(v, (list, tuple)):
            stack.extend(v)


def _names_a_secret(name) -> bool:
    """Is ``name`` (a field's or an answer's key, in any spelling) a secret's
    or a credential's?  security.edge_privacy.is_secret_key, extended by the
    words only the vault asks for."""
    from security.edge_privacy import is_secret_key, secret_key_form
    return isinstance(name, str) and is_secret_key(secret_key_form(name),
                                                   _CREDENTIAL_WORDS)


def _words_ask_for_a_secret(text) -> bool:
    """Do the words of a label or a title ask for a secret?  One word that
    names one ('Password', 'Enter the OTP'), or two or three that do the way a
    field name does ('API key', 'access token', 'UPI PIN', 'card number').
    An everyday word alone ('pass', 'token', 'pin') is not enough, nor a
    phrase with a little word in it ('a token')."""
    if not isinstance(text, str):
        return False
    from security.edge_privacy import secret_key_form
    words = secret_key_form(text).split('_')
    for i, word in enumerate(words):
        if word not in _EVERYDAY_WORDS and _names_a_secret(word):
            return True
        for n in (2, 3):
            phrase = words[i:i + n]
            if len(phrase) == n and not _FUNCTION_WORDS.intersection(phrase) \
                    and _names_a_secret('_'.join(phrase)):
                return True
    return False


def _asks_for_a_secret(component, words=()) -> bool:
    """True if ``component`` asks for a secret: a field marked ``secret``,
    typed ``password`` in any spelling, named the way a secret is named, or
    whose label, placeholder or title asks for one; or the ask's own
    ``words`` (its title and body) do."""
    for d in _walk(component):
        if d.get('secret') is True or str(d.get('secret', '')).strip().lower() in (
                'true', '1', 'yes'):
            return True
        if any(str(d.get(k, '')).strip().lower() == 'password' for k in _TYPE_KEYS):
            return True
        if any(_names_a_secret(d.get(k)) for k in _NAME_KEYS):
            return True
        if any(_words_ask_for_a_secret(d.get(k)) for k in _TEXT_KEYS):
            return True
    return any(_words_ask_for_a_secret(w) for w in words)


def _refusal(component, prompt_id, words=()) -> Optional[str]:
    """Why ``component`` cannot be asked, before anything is sent.  Which
    component types exist is the card gate's to say (agent_ui_compose), so a
    type an agent registered at runtime is asked like a builtin."""
    if not isinstance(component, dict) or not component.get('type'):
        return 'not_an_object'
    if _asks_for_a_secret(component, words):
        return 'secrets_go_to_the_vault'
    if component.get('type') == 'file_request':
        from core.user_file_store import INBOUND_PURPOSES
        if component.get('purpose') not in INBOUND_PURPOSES:
            return 'unknown_purpose'
        if prompt_id is None or str(prompt_id).strip() == '':
            return 'no_agent'          # the picks are bound to the agent that asked
    return None


def _file_card(card: dict, prompt_id) -> None:
    """A file_request asks for as many files as the store keeps of its
    purpose (one logo), at most a turn's attachments, for the agent asking."""
    from core.chat_attachments import MAX_ATTACHMENTS
    from core.user_file_store import PURPOSES
    if PURPOSES[card['purpose']].newest_only:
        card['max_files'] = 1
    else:
        try:
            n = int(card.get('max_files') or MAX_ATTACHMENTS)
        except (TypeError, ValueError):
            n = MAX_ATTACHMENTS
        card['max_files'] = max(1, min(n, MAX_ATTACHMENTS))
    card['prompt_id'] = str(prompt_id)


def _scrub(text: str) -> str:
    """A push leaves this node (for Google, or central's relay), so its words
    pass the one egress scrub (secrets, then PII), as consent_prompt_push's
    and goal_manager's pushes do."""
    try:
        from security.edge_privacy import scrub_text
        return scrub_text(text)
    except Exception:
        logger.warning("person ask: egress scrub unavailable; the push says only "
                       "that an agent asks", exc_info=True)
        return 'An agent asks you something.'


def ask_push(ask_id: str, user_id: str, ctype: str, title: str, body: str) -> dict:
    """The Firebase message of an ask.  ``ask_id`` names the ask the phone
    answers through /api/agent/approval; it travels under its own key because
    central's relay replaces a push's request_id (fcm_sync
    _CENTRAL_OWNED_PUSH_KEYS).  No reply topic: none is an answer door.

    TODO(S4, by 2026-11-30): consent_service.consent_prompt_push and the push
    agent_tools.offer_sound_for_review builds fold onto this one builder.
    """
    title, body = _scrub(title)[:80], _scrub(body)[:240]
    return {'title': title, 'body': body,
            'data': {'type': 'ui_ask', 'request_id': ask_id, 'ask_id': ask_id,
                     'user_id': str(user_id), 'component_type': ctype,
                     'title': title, 'body': body}}


def _prune(now: float) -> None:
    for ask_id, a in list(_asks.items()):
        if (a.answered_at and now - a.answered_at > ANSWERED_KEEP_S) or \
                (not a.answered_at and now - a.created > OPEN_KEEP_S):
            _asks.pop(ask_id, None)


def _withdraw_card(ask_id: str) -> None:
    try:
        from integrations.agent_engine import liquid_ui_service as lui
        lui.withdraw_agent_ui(ask_id)
    except Exception:
        logger.warning("person ask %s: its card could not be taken back from the "
                       "shell's store", ask_id, exc_info=True)


def ask(user_id, agent_id, component, *, prompt_id=None, title: str = '',
        body: str = '') -> dict:
    """Put ``component`` in front of ``user_id`` on every surface they have.

    Returns ``{'ok', 'ask_id', 'refused', 'card'}`` (plus ``'reused': True``
    when the same question is already open).  ``refused`` names why nothing
    was sent: 'no_person', 'not_an_object', 'secrets_go_to_the_vault',
    'unknown_purpose', 'no_agent', 'too_many_open_asks', 'not_shown' (the
    Liquid UI gates refused the card: switched off, the hive halted, unsafe
    content, an unknown type, too many pushes; the log says which) or
    'not_filed' (its row could not be written).
    """
    user_id = str(user_id or '').strip()
    if not user_id:
        return _refused('no_person')
    refused = _refusal(component, prompt_id, (title, body))
    if refused:
        logger.info("person ask refused (%s) for user %s by agent %s",
                    refused, user_id, agent_id)
        return _refused(refused)
    ctype = component['type']
    agent_id = str(agent_id if agent_id is not None else (prompt_id or ''))
    title = (title or component.get('title') or 'Your agent needs your answer').strip()
    body = (body or component.get('description') or '').strip()
    card = dict(component)
    card.pop('msg_id', None)
    if ctype == 'file_request':
        _file_card(card, prompt_id)
    same = json.dumps([agent_id, card, title, body], sort_keys=True, default=str)
    with _lock:
        _prune(time.time())
        mine = [a for a in _asks.values() if a.user_id == user_id and a.answered_at is None]
        for a in mine:
            if a.same == same:
                return {'ok': True, 'ask_id': a.ask_id, 'refused': None, 'card': True,
                        'reused': True}
        if len(mine) >= MAX_OPEN_PER_PERSON:
            logger.warning("person ask refused for user %s by agent %s: %d asks "
                           "already open", user_id, agent_id, len(mine))
            return _refused('too_many_open_asks')
        ask_id = uuid.uuid4().hex
        a = _Ask(ask_id, user_id, agent_id,
                 None if prompt_id is None else str(prompt_id), ctype, card, same)
        _asks[ask_id] = a
    card['ask_id'] = ask_id
    card['msg_id'] = ask_id
    from integrations.agent_engine import liquid_ui_service as lui
    try:
        shown = bool(lui.push_agent_ui(agent_id, card, user_id=user_id))
    except Exception:
        logger.warning("person ask %s: the card was not pushed", ask_id, exc_info=True)
        shown = False
    if not shown:
        with _lock:
            _asks.pop(ask_id, None)
        logger.warning("person ask for user %s by agent %s NOT sent: Liquid UI "
                       "refused its %s card", user_id, agent_id, ctype)
        return _refused('not_shown')
    from integrations.social.models import db_session
    from integrations.social.services import NotificationService
    try:
        with db_session(commit=True) as db:
            NotificationService.create(
                db, user_id, ASK_TYPE, target_type=ASK_STORE,
                target_id=(agent_id or None) and agent_id[:64], message=title,
                push=ask_push(ask_id, user_id, ctype, title, body),
                notification_id=ask_id)
    except Exception:
        # No row, no ask: the card comes back down from every surface it
        # reached (the phone's topic and the page too, not only the shell),
        # and the agent is told, rather than waiting on a question with no
        # record that her answer would find gone.
        with _lock:
            _asks.pop(ask_id, None)
        _drop_everywhere(ask_id, user_id)
        logger.warning("person ask for user %s by agent %s NOT filed: its row "
                       "could not be written", user_id, agent_id, exc_info=True)
        return _refused('not_filed')
    if a.answered_at is not None:          # answered before its row existed
        _mark_read(ask_id, user_id)
    logger.info("person ask %s (%s) to user %s by agent %s: card, row and push sent",
                ask_id, ctype, user_id, agent_id)
    return {'ok': True, 'ask_id': ask_id, 'refused': None, 'card': True}


def _too_deep(value) -> bool:
    stack = [(value, 1)]
    while stack:
        v, depth = stack.pop()
        if isinstance(v, (dict, list, tuple)):
            if depth > MAX_ANSWER_DEPTH:
                return True
            items = v.values() if isinstance(v, dict) else v
            stack.extend((c, depth + 1) for c in items)
    return False


def _answer_problem(a: '_Ask', answer) -> Optional[str]:
    if not isinstance(answer, dict) or _too_deep(answer):
        return 'bad_answer'
    try:
        if len(json.dumps(answer)) > MAX_ANSWER_BYTES:
            return 'bad_answer'
    except (TypeError, ValueError):
        return 'bad_answer'
    if a.ctype == 'file_request':
        from core.user_file_store import is_xid
        xids = answer.get('xids')
        if set(answer) != {'xids'} or not isinstance(xids, list) or not xids \
                or len(xids) > (a.max_files or 1) or len(set(xids)) != len(xids) \
                or not all(isinstance(x, str) and is_xid(x) for x in xids):
            return 'bad_answer'
        return None
    try:
        from security.edge_privacy import redact_secret_leaves
        _copy, found = redact_secret_leaves(answer)
    except Exception:
        logger.warning("person ask %s: the answer could not be checked for "
                       "secrets, so it is refused", a.ask_id, exc_info=True)
        return 'bad_answer'
    if found or any(_names_a_secret(k) and _has_a_value(v)
                    for d in _walk(answer) for k, v in d.items()):
        return 'secrets_go_to_the_vault'
    return None


def _has_a_value(v) -> bool:
    if isinstance(v, str):
        return bool(v.strip())
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _bind_picks(a: '_Ask', xids) -> Optional[str]:
    """Her picks become the asking agent's files, bound to the ask as their
    turn (core.chat_attachments.bind_inbound, the one binding rule): only
    files in her own inbox, held for the ask's purpose."""
    from core.chat_attachments import bind_inbound
    bound = bind_inbound(a.user_id, a.prompt_id, a.ask_id,
                         [{'file_id': x, 'purpose': a.purpose} for x in xids])
    if set(xids) - {f.get('xid') for f in bound}:
        logger.warning("person ask %s: %d of %d picks are not held for user %s as %s",
                       a.ask_id, len(set(xids) - {f.get('xid') for f in bound}),
                       len(xids), a.user_id, a.purpose)
        return 'files_not_received'
    return None


def _mark_read(ask_id: str, user_id: str) -> None:
    try:
        from integrations.social.models import db_session
        from integrations.social.services import NotificationService
        with db_session(commit=True) as db:
            NotificationService.mark_read(db, [ask_id], user_id)
    except Exception:
        logger.warning("person ask %s: its row was not marked read, so the bell "
                       "still lists it", ask_id, exc_info=True)


def _drop_everywhere(ask_id: str, user_id: str) -> None:
    """Every surface is told to drop the ask's card (ANSWERED_TYPE, which is
    the one drop event they listen for) and the shell's store lets it go."""
    try:
        from integrations.social.realtime import on_notification
        on_notification(str(user_id), {'type': ANSWERED_TYPE, 'ask_id': ask_id,
                                       'msg_id': f'{ask_id}.answered'})
    except Exception:
        logger.warning("person ask %s: the drop fan-out failed, so other "
                       "surfaces keep the card", ask_id, exc_info=True)
    _withdraw_card(ask_id)


def _close(ask_id: str, user_id: str) -> None:
    """The ask is over: its card drops everywhere and the bell stops
    listing it."""
    _drop_everywhere(ask_id, user_id)
    _mark_read(ask_id, user_id)


def _owner_of_row(ask_id: str) -> Optional[str]:
    try:
        from integrations.social.models import Notification, db_session
        with db_session(commit=False) as db:
            row = db.query(Notification).filter_by(id=ask_id, type=ASK_TYPE).first()
            return None if row is None else str(row.user_id)
    except Exception:
        logger.warning("person ask %s: the row could not be read", ask_id, exc_info=True)
        return None


def _expired_or_unknown(ask_id: str, answerer_id) -> Tuple[dict, int]:
    """An ask this process no longer holds (a restart ended the turn that
    waited on it).  Its card may still be on the person's surfaces: their
    answer is told it came too late and the card is dropped everywhere.  To
    anyone else it is simply not found."""
    owner = _owner_of_row(ask_id) if ask_id else None
    if owner is None or not answerer_id or str(answerer_id) != owner:
        return {'status': 'error', 'reason': 'not_found'}, 404
    _close(ask_id, owner)
    logger.info("person ask %s: answered after its turn ended; the card is dropped",
                ask_id)
    return {'status': 'error', 'reason': 'expired'}, 410


def answer_ask(ask_id, answer, answerer_id) -> Tuple[dict, int]:
    """The person's answer to ask ``ask_id``, from any surface.

    ``answerer_id`` is the caller's VERIFIED identity, never a body field.
    Returns ``(body, http_status)``: 200 answered; 400 'bad_answer',
    'secrets_go_to_the_vault' or 'files_not_received' (the ask stays open);
    403 'not_yours'; 404 'not_found'; 409 'already_answered' (with the answer
    already given, which stands); 410 'expired' (the turn that asked ended).
    """
    ask_id = str(ask_id or '')
    with _lock:
        a = _asks.get(ask_id)
    if a is None:
        return _expired_or_unknown(ask_id, answerer_id)
    if not answerer_id or str(answerer_id) != a.user_id:
        logger.warning("person ask %s: an answer from %r refused, the ask is for "
                       "user %s", ask_id, answerer_id, a.user_id)
        return {'status': 'error', 'reason': 'not_yours'}, 403
    with _lock:
        if a.answered_at is not None:
            return {'status': 'error', 'reason': 'already_answered',
                    'answer': a.answer}, 409
    problem = _answer_problem(a, answer)
    if problem is None and a.ctype == 'file_request':
        problem = _bind_picks(a, answer['xids'])
    if problem:
        return {'status': 'error', 'reason': problem}, 400
    with _lock:
        if a.answered_at is not None:
            return {'status': 'error', 'reason': 'already_answered',
                    'answer': a.answer}, 409
        a.answer = dict(answer)
        a.answered_at = time.time()
        a.event.set()
    _close(ask_id, a.user_id)
    logger.info("person ask %s (%s) answered by user %s", ask_id, a.ctype, a.user_id)
    return {'status': 'ok', 'ask_id': ask_id}, 200


def answer_from_request(req, data) -> Tuple[dict, int]:
    """/api/agent/approval's ask branch, for both of its routes (this node's
    and the shell's): the answerer is the caller's Bearer identity."""
    from integrations.commerce.approvals import approver_from_request
    return answer_ask((data or {}).get('ask_id'), (data or {}).get('answer'),
                      approver_from_request(req))


def wait_for_answer(ask_id, timeout_s) -> Optional[dict]:
    """The answer, if it comes within ``timeout_s`` seconds; else None."""
    with _lock:
        a = _asks.get(str(ask_id or ''))
    if a is None:
        return None
    a.event.wait(max(0.0, float(timeout_s or 0)))
    return dict(a.answer) if a.answer is not None else None


def ask_state(ask_id, user_id) -> dict:
    """``{'status', 'answer'}`` of an ask, as ``user_id``'s agent may read it.

    'open', 'answered' (with the answer), 'expired' (her ask, no longer held
    here: a restart, or kept past ANSWERED_KEEP_S; ask again) or 'not_found'
    (no such ask of hers)."""
    ask_id, user_id = str(ask_id or ''), str(user_id or '')
    with _lock:
        a = _asks.get(ask_id)
        if a is not None:
            if a.user_id != user_id:
                return {'status': 'not_found', 'answer': None}
            if a.answer is not None:
                return {'status': 'answered', 'answer': dict(a.answer)}
            return {'status': 'open', 'answer': None}
    if ask_id and user_id and _owner_of_row(ask_id) == user_id:
        return {'status': 'expired', 'answer': None}
    return {'status': 'not_found', 'answer': None}


def open_file_request(ask_id, user_id) -> Optional[dict]:
    """``{'purpose', 'prompt_id', 'max_files'}`` of ``user_id``'s OPEN
    file_request ``ask_id``, else None.  The door that takes the picked files
    (Nunba's /api/chat-sync/files/push) asks here: a file comes in for an
    ask only while it is open, for its person, under the purpose it names."""
    with _lock:
        a = _asks.get(str(ask_id or ''))
        if a is None or a.ctype != 'file_request' or a.answered_at is not None \
                or a.user_id != str(user_id or ''):
            return None
        return {'purpose': a.purpose, 'prompt_id': a.prompt_id, 'max_files': a.max_files}


def ask_person_tool(user_id, prompt_id, agent_id=None):
    """The ``ask_person`` tool every agent has: (name, description, func)."""

    def ask_person(
            component: Annotated[str, 'Liquid UI component JSON, e.g. {"type":"file_request",'
                                      '"purpose":"reel_source","accept":"video"}'],
            wait_seconds: Annotated[int, f"seconds to wait now, max {MAX_WAIT_S}"] = DEFAULT_WAIT_S,
            ask_id: Annotated[str, "an earlier ask's id: read its answer"] = '') -> str:
        if ask_id:
            state = ask_state(ask_id, user_id)
            reply = {'answered': state['status'] == 'answered', 'ask_id': ask_id,
                     'answer': state['answer'],
                     'status': ('waiting_for_person' if state['status'] == 'open'
                                else state['status'])}
            if state['status'] == 'expired':
                reply['note'] = ('This ask is gone (the node restarted, or its answer '
                                 'was kept too long). Ask again if you still need it.')
            return json.dumps(reply)
        try:
            comp = json.loads(component) if isinstance(component, str) else component
        except ValueError:
            comp = None
        if isinstance(comp, dict) and prompt_id is not None:
            comp.setdefault('prompt_id', str(prompt_id))
        out = ask(user_id, agent_id if agent_id is not None else prompt_id, comp,
                  prompt_id=prompt_id)
        if not out['ok']:
            return json.dumps({'answered': False, 'refused': out['refused'],
                               'status': 'refused'})
        try:
            wait_s = min(float(wait_seconds or 0), float(MAX_WAIT_S))
        except (TypeError, ValueError):
            wait_s = 0.0
        got = wait_for_answer(out['ask_id'], wait_s)
        if got is not None:
            return json.dumps({'answered': True, 'ask_id': out['ask_id'], 'answer': got,
                               'status': 'answered'})
        return json.dumps({
            'answered': False, 'ask_id': out['ask_id'], 'status': 'waiting_for_person',
            'note': ('The question is on their devices and they have not answered '
                     'yet. This step is not done: read the answer later with '
                     'ask_person(ask_id=...).')})

    return ('ask_person',
            ('Ask your person on any of their devices and get their answer, e.g. a '
             'file_request for clips. Never for a password or key.'),
            ask_person)
