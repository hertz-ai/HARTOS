"""integrations.agent_engine.person_ask -- the server asks the person.

Owner, 2026-10-27: "liquid ui is asked as a tool by server, same for consent
screems and other liquid ui screens", "the mechanism shd be same agnostic of
hartos consumer, liqiuid ui shd also fanout", "same for consents via firebase
shd fanout in desktop, android, wamp etc".  Design and the register rules it
traces to: docs/design/PERSON_ASK_LIQUID_UI_TOOL.md.

An ask is one Liquid UI component put in front of one person, on every
surface they have, answered on any one of them, and its answer handed back to
the agent that asked.  This module keeps no transport of its own; it joins
the ones that exist:

  * ONE Notification row, written by NotificationService.create with a push,
    is the ask's record and its id (H-1022: the row id is the msg_id, so the
    Firebase leg of realtime.on_notification rings the phone once however
    often the ask is fanned out).  It lists as an open question until it is
    answered (H-1030, N-0628).
  * ONE Liquid UI card, through liquid_ui_service.push_agent_ui, under the
    same id: the desktop page (Nunba AgentOverlay), the shell and the
    person's own topic (the phone).
  * The answer, from whichever surface, goes through answer_ask(): the person
    asked and nobody else, the first answer only.  It marks the row read,
    which is NotificationService.mark_read's notification.read fan-out: every
    other surface drops its copy (H-1024).

The doors that call answer_ask() are the transports' own: /api/agent/approval
with an ``ask_id`` (hart_intelligence_entry), for the desktop page and the
shell.  The phone's reply on com.hertzai.pupit.<user> is the second door
(slice S1b in the design doc).

Never asked this way: a secret.  A component that carries one is refused
before anything leaves; a credential is asked by ai_key_vault.request_credential
and lands in the vault, never in a tool result, a log or the ledger (H-0986).
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
#: The longest a tool call waits for the person before it hands back a held
#: ask; the answer is read on a later turn with the ask's id.
MAX_WAIT_S = 120
#: How long an answered ask stays readable here, and how long an unanswered
#: one is kept waiting for its answer.
ANSWERED_KEEP_S = 3600
OPEN_KEEP_S = 24 * 3600
#: An answer is a small structured object, never a payload.
MAX_ANSWER_BYTES = 16 * 1024

_lock = threading.Lock()
_asks: Dict[str, '_Ask'] = {}


class _Ask:
    __slots__ = ('ask_id', 'user_id', 'agent_id', 'prompt_id', 'ctype',
                 'created', 'answered_at', 'answer', 'event')

    def __init__(self, ask_id, user_id, agent_id, prompt_id, ctype):
        self.ask_id = ask_id
        self.user_id = user_id
        self.agent_id = agent_id
        self.prompt_id = prompt_id
        self.ctype = ctype
        self.created = time.time()
        self.answered_at = None
        self.answer = None
        self.event = threading.Event()


def _carries_a_secret(value) -> bool:
    """True if any part of a component asks for a secret: a field marked
    ``secret`` or typed ``password``, however deeply it is nested."""
    if isinstance(value, dict):
        if value.get('secret') is True or str(value.get('type', '')).lower() == 'password':
            return True
        return any(_carries_a_secret(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_carries_a_secret(v) for v in value)
    return False


def _refusal(component) -> Optional[str]:
    if not isinstance(component, dict):
        return 'not_an_object'
    from integrations.agent_engine import liquid_ui_service as lui
    if component.get('type') not in lui.COMPONENT_TYPES:
        return 'unknown_type'
    if _carries_a_secret(component):
        return 'secrets_go_to_the_vault'
    return None


def _scrub(text: str) -> str:
    """A push leaves the person's devices (Google, or central's relay), so
    its words are scrubbed as consent_prompt_push scrubs its own."""
    try:
        from security.dlp_engine import get_dlp_engine
        return get_dlp_engine().redact(text)
    except Exception:
        logger.warning("person ask: DLP scrub unavailable; the push says only "
                       "that an agent asks", exc_info=True)
        return 'An agent asks you something.'


def ask_push(ask_id: str, user_id: str, ctype: str, title: str, body: str) -> dict:
    """The Firebase message of an ask, in the shape the phone's ask overlay
    reads (consent_prompt_push's: the id a reply answers and the topic it is
    published on)."""
    title, body = _scrub(title)[:80], _scrub(body)[:240]
    return {'title': title, 'body': body,
            'data': {'type': 'ui_ask', 'request_id': ask_id, 'user_id': str(user_id),
                     'topic_reply': f'com.hertzai.pupit.{user_id}',
                     'component_type': ctype, 'title': title, 'body': body}}


def _prune(now: float) -> None:
    for ask_id, a in list(_asks.items()):
        if (a.answered_at and now - a.answered_at > ANSWERED_KEEP_S) or \
                (not a.answered_at and now - a.created > OPEN_KEEP_S):
            _asks.pop(ask_id, None)


def ask(user_id, agent_id, component, *, prompt_id=None, title: str = '',
        body: str = '') -> dict:
    """Put ``component`` in front of ``user_id`` on every surface they have.

    Returns ``{'ok', 'ask_id', 'refused', 'card'}``.  ``refused`` names why
    nothing was sent: 'no_person', 'not_an_object', 'unknown_type' or
    'secrets_go_to_the_vault'.  ``card`` False means the row and the push
    went out but the Liquid UI card was not accepted (its gates: the kill
    switch, a halted hive, unsafe content); the person still has the ask.
    """
    user_id = str(user_id or '').strip()
    if not user_id:
        return {'ok': False, 'ask_id': None, 'refused': 'no_person', 'card': False}
    refused = _refusal(component)
    if refused:
        logger.info("person ask refused (%s) for user %s by agent %s",
                    refused, user_id, agent_id)
        return {'ok': False, 'ask_id': None, 'refused': refused, 'card': False}
    ctype = component['type']
    agent_id = str(agent_id if agent_id is not None else (prompt_id or ''))
    title = (title or component.get('title') or 'Your agent needs your answer').strip()
    body = (body or component.get('description') or '').strip()
    ask_id = uuid.uuid4().hex
    with _lock:
        _prune(time.time())
        _asks[ask_id] = _Ask(ask_id, user_id, agent_id,
                             None if prompt_id is None else str(prompt_id), ctype)
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
        # No row, no ask: nothing was sent, and the agent is told so rather
        # than waiting on a question nobody can see.
        with _lock:
            _asks.pop(ask_id, None)
        logger.warning("person ask for user %s by agent %s NOT filed: its row "
                       "could not be written", user_id, agent_id, exc_info=True)
        return {'ok': False, 'ask_id': None, 'refused': 'not_filed', 'card': False}
    from integrations.agent_engine import liquid_ui_service as lui
    card = dict(component)
    card['ask_id'] = ask_id
    card['msg_id'] = ask_id
    try:
        shown = bool(lui.push_agent_ui(agent_id, card, user_id=user_id))
    except Exception:
        logger.warning("person ask %s: the card was not pushed", ask_id, exc_info=True)
        shown = False
    logger.info("person ask %s (%s) to user %s by agent %s: row + push filed, card %s",
                ask_id, ctype, user_id, agent_id, 'shown' if shown else 'NOT shown')
    return {'ok': True, 'ask_id': ask_id, 'refused': None, 'card': shown}


def _answer_problem(a: '_Ask', answer) -> Optional[str]:
    if not isinstance(answer, dict):
        return 'bad_answer'
    try:
        if len(json.dumps(answer)) > MAX_ANSWER_BYTES:
            return 'bad_answer'
    except (TypeError, ValueError):
        return 'bad_answer'
    if _carries_a_secret(answer):
        return 'bad_answer'
    if a.ctype == 'file_request':
        from core.user_file_store import is_xid
        xids = answer.get('xids')
        if not isinstance(xids, list) or not all(isinstance(x, str) and is_xid(x)
                                                 for x in xids):
            return 'bad_answer'
    return None


def _mark_answered(ask_id: str, user_id: str) -> None:
    """The row is read: realtime.on_notification_read tells every other
    surface to drop the card."""
    try:
        from integrations.social.models import db_session
        from integrations.social.services import NotificationService
        with db_session(commit=True) as db:
            NotificationService.mark_read(db, [ask_id], user_id)
    except Exception:
        logger.warning("person ask %s: answered, but the row was not marked read, "
                       "so other surfaces keep the card", ask_id, exc_info=True)


def _expired_or_unknown(ask_id: str, answerer_id) -> Tuple[dict, int]:
    """An ask this process no longer holds (a restart ended the turn that
    waited on it).  Its row may still be open on the person's surfaces: their
    answer is told it came too late and the card is dropped everywhere.  To
    anyone else it is simply not found."""
    try:
        from integrations.social.models import Notification, db_session
        with db_session(commit=False) as db:
            row = db.query(Notification).filter_by(id=ask_id, type=ASK_TYPE).first()
            owner = row.user_id if row is not None else None
    except Exception:
        logger.warning("person ask %s: the row could not be read", ask_id, exc_info=True)
        owner = None
    if owner is None or not answerer_id or str(answerer_id) != str(owner):
        return {'status': 'error', 'reason': 'not_found'}, 404
    _mark_answered(ask_id, str(owner))
    logger.info("person ask %s: answered after its turn ended; the card is dropped",
                ask_id)
    return {'status': 'error', 'reason': 'expired'}, 410


def answer_ask(ask_id, answer, answerer_id) -> Tuple[dict, int]:
    """The person's answer to ask ``ask_id``, from any surface.

    ``answerer_id`` is the caller's VERIFIED identity, never a body field.
    Returns ``(body, http_status)``: 200 answered; 400 'bad_answer'; 403
    'not_yours'; 404 'not_found'; 409 'already_answered' (with the answer
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
    problem = _answer_problem(a, answer)
    if problem:
        return {'status': 'error', 'reason': problem}, 400
    with _lock:
        if a.answered_at is not None:
            return {'status': 'error', 'reason': 'already_answered',
                    'answer': a.answer}, 409
        a.answer = dict(answer)
        a.answered_at = time.time()
        a.event.set()
    _mark_answered(ask_id, a.user_id)
    logger.info("person ask %s (%s) answered by user %s", ask_id, a.ctype, a.user_id)
    return {'status': 'ok', 'ask_id': ask_id}, 200


def wait_for_answer(ask_id, timeout_s) -> Optional[dict]:
    """The answer, if it comes within ``timeout_s`` seconds; else None."""
    with _lock:
        a = _asks.get(str(ask_id or ''))
    if a is None:
        return None
    a.event.wait(max(0.0, float(timeout_s or 0)))
    return dict(a.answer) if a.answer is not None else None


def answer_of(ask_id) -> Optional[dict]:
    """The answer once given (None while the ask is open, or unknown here)."""
    with _lock:
        a = _asks.get(str(ask_id or ''))
        return dict(a.answer) if a is not None and a.answer is not None else None


def ask_person_tool(user_id, prompt_id, agent_id=None):
    """The ``ask_person`` tool every agent has: (name, description, func)."""

    def ask_person(
            component: Annotated[str, 'Liquid UI component JSON, e.g. {"type":"file_request",'
                                      '"purpose":"reel_source","accept":"video"}'],
            wait_seconds: Annotated[int, f"seconds to wait now, max {MAX_WAIT_S}"] = 60,
            ask_id: Annotated[str, "an earlier ask's id: read its answer"] = '') -> str:
        if ask_id:
            got = answer_of(ask_id)
            return json.dumps({'answered': got is not None, 'ask_id': ask_id,
                               'answer': got,
                               'status': 'answered' if got is not None
                               else 'waiting_for_person'})
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
            'note': ('The question is on all of their devices and they have not '
                     'answered yet. This step is not done: read the answer later '
                     'with ask_person(ask_id=...).')})

    return ('ask_person',
            ('Ask your person on any of their devices and get their answer, e.g. a '
             'file_request for clips. Never for a password or key.'),
            ask_person)
