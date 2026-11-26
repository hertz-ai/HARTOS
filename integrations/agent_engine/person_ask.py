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
  * A dismissal, from whichever surface, goes through dismiss_ask(): the
    person asked and nobody else, before any answer.  The ask closes exactly
    as an answered one does (every surface drops its card, the row is marked
    read), and the asking agent reads ``dismissed`` (owner, 2026-11-03:
    "dismissing a request dismisses and answering in one client device clears
    everything").

The one door that calls answer_ask() and dismiss_ask() is
/api/agent/approval with an ``ask_id`` (hart_intelligence_entry, and the
shell's own route; ``"dismiss": true`` dismisses), always with the caller's
verified Bearer identity (answer_from_request).  No reply topic
is a door: central's router is anonymous, so a payload published there could
answer for anyone (decision 2026-10-27 in the design doc).

Never asked or answered this way: a secret.  A component that asks for one is
refused before anything leaves, and an answer that carries one is refused; a
credential is asked by ai_key_vault.request_credential and lands in the vault,
never in a tool result, a log or the ledger (H-0986).

The asks live in this process.  An ask this process no longer holds (a
restart, ANSWERED_KEEP_S after its answer, OPEN_KEEP_S unanswered, or the
CREATE run that asked it finished) reads ``expired`` to the person and to the
agent, never ``waiting``.  One that closes unanswered (its CREATE run
finished, or OPEN_KEEP_S passed) leaves every surface as an answered one does
(_end_unanswered).
"""
import contextvars
import functools
import inspect
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


def answered_msg_id(ask_id) -> str:
    """The msg_id of an ask's drop ('<ask id>.answered'): drop_everywhere
    sends it, and a consent ask asked again under the same row forgets its
    push so its next drop reaches the phone too
    (consent_service._drop_ask)."""
    return f'{ask_id}.answered'


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
#: Open asks something waits on (is_held) one person may have at once OF
#: EACH KIND, her own turns' and background work's (a daemon goal's turn),
#: so at most twice this many in all (a runaway agent cannot ring her phone
#: without end).  An open ask nothing waits on any more stays open for her
#: answer and no longer counts: live 2026-11-25 19:16:46 IST, "3 own asks
#: already open" refused the speech build's next question while nothing
#: waited on the three (design_1125 A, v4 M1).  The same question asked
#: again is the same ask, whatever its kind.  Background asks never take
#: the place of the asks of her own conversations, nor hers theirs (review
#: of ff45f77a4: with three background asks open, the owner's own agent was
#: refused).
MAX_OPEN_PER_PERSON = 3

#: Keys that type a field, and the keys that name one.
_TYPE_KEYS = ('type', 'input_type', 'inputType', 'format', 'kind')
_NAME_KEYS = ('name', 'key', 'field')
#: Beyond the secret key words (security.edge_privacy.is_secret_key), what a
#: person is only ever asked through the vault: a one-time code, a PIN, a
#: card's number or code, a wallet's phrase.  The list is edge_privacy's
#: (CREDENTIAL_KEY_WORDS), read here under the name this module always used,
#: so asking and reading another person's notes answer one list.
from security.edge_privacy import CREDENTIAL_KEY_WORDS as _CREDENTIAL_WORDS  # noqa: E402

_lock = threading.Lock()
_asks: Dict[str, '_Ask'] = {}


class _Ask:
    __slots__ = ('ask_id', 'user_id', 'agent_id', 'prompt_id', 'ctype', 'purpose',
                 'max_files', 'same', 'created', 'answered_at', 'answer', 'event',
                 'dismissed', 'background', 'create_run', 'card', 'asked_for',
                 'asked_in', 'waiters')

    def __init__(self, ask_id, user_id, agent_id, prompt_id, ctype, card, same,
                 background=False, create_run=None, asked_for=None, asked_in=None):
        self.ask_id = ask_id
        # The session that asked: its person (asked_for, the ask_person
        # tool's user) and the turn it asked in (asked_in, the turn's request
        # id).  A step of that session waiting on its question holds the ask
        # (is_held), and the person's reply that step takes closes it
        # (close_asked_in).  None for an ask the framework or a person makes.
        self.asked_for = None if asked_for is None else str(asked_for)
        self.asked_in = str(asked_in) if asked_in else None
        # Tool calls blocked in wait_for_answer on it now: each holds it.
        self.waiters = 0
        # Asked by background work, not by the person's own turn: it counts
        # against its own open-ask limit (MAX_OPEN_PER_PERSON).
        self.background = bool(background)
        # The CREATE build it was asked in, (prompt id, the build's user),
        # else None: closed when a run of that build reports the agent built.
        self.create_run = create_run
        self.user_id = user_id
        self.agent_id = agent_id
        self.prompt_id = prompt_id
        self.ctype = ctype
        self.purpose = card.get('purpose') if ctype == 'file_request' else None
        self.max_files = card.get('max_files') if ctype == 'file_request' else None
        # The card as it went out (ask() adds its ask_id and msg_id to this
        # dict): what read_ask gives a phone whose banner opened the app.
        self.card = card
        self.same = same
        self.created = time.time()
        self.answered_at = None
        self.answer = None
        self.event = threading.Event()
        # Closed by the person without an answer (dismiss_ask): answered_at
        # is set too, so it no longer counts as open anywhere.
        self.dismissed = False


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
    """Is ``name`` (a field's or an answer's key, in any spelling, glued
    into one word too) a secret's or a credential's?
    security.edge_privacy.is_secret_key, extended by the words only the vault
    asks for.  A short credential word glued into a name ('upipin') is not
    read: written 'upi_pin', 'upiPin' or 'UPIPin' it is."""
    from security.edge_privacy import is_secret_key, secret_key_form
    return isinstance(name, str) and is_secret_key(secret_key_form(name),
                                                   _CREDENTIAL_WORDS)


def _asks_for_a_secret(component) -> bool:
    """True if ``component`` asks for a secret by its structure: a field
    marked ``secret``, typed ``password`` in any spelling, or named the way a
    secret is named.

    Its words (a label, a placeholder, a title) are not read: a word list over
    free text refused 36 of 52 honest asks ('the first pass of your reel',
    'Delivery address PIN code') and still let 32 of 47 secret phrasings
    through (review of e6eed01ae), and the owner's ruling of 2026-10-20 for
    reading words without the model is the exact word or the model."""
    for d in _walk(component):
        if _marked_secret(d.get('secret')):
            return True
        if any(str(d.get(k, '')).strip().lower() == 'password' for k in _TYPE_KEYS):
            return True
        if any(_names_a_secret(d.get(k)) for k in _NAME_KEYS):
            return True
    return False


def _marked_secret(marker) -> bool:
    """Whether a field's ``secret`` marks it: truthy the way the desktop page
    reads it (AgentOverlay.jsx draws a password box for a JavaScript-truthy
    ``secret``), so the string 'false' and an empty list mark it too."""
    if marker is None or marker is False or marker == '':
        return False
    if isinstance(marker, (int, float)) and not isinstance(marker, bool):
        return marker == marker and marker != 0          # NaN and 0 are falsy
    return True


def _refusal(component, prompt_id) -> Optional[str]:
    """Why ``component`` cannot be asked, before anything is sent.  Which
    component types exist is the card gate's to say (agent_ui_compose), so a
    type an agent registered at runtime is asked like a builtin.

    A component sent with no "type" is given the one builtin type whose
    required props it all carries ("fields" is a form's, "purpose" a
    file_request's), set on ``component`` itself so every check after this
    one reads it, and it is then asked exactly as if the agent had written
    that type.  The local 4B drops the type and keeps the fields: on
    2026-11-16 the Email Draft Helper's 8 asks were all refused this way and
    it asked again and again.  When no type, or more than one, carries all
    its required props, it is refused not_an_object as before."""
    if isinstance(component, dict) and not component.get('type'):
        from integrations.agent_engine.liquid_ui_service import COMPONENT_TYPES
        fits = [t for t, entry in COMPONENT_TYPES.items()
                if isinstance(entry, dict) and entry.get('required')
                and all(component.get(p) is not None for p in entry['required'])]
        if len(fits) == 1:
            component['type'] = fits[0]
    if not isinstance(component, dict) or not component.get('type'):
        return 'not_an_object'
    if _asks_for_a_secret(component):
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
    purpose (one logo), at most a turn's attachments, for the agent asking.
    Its ``accept`` is the one kind of file the store takes for the purpose
    (video, image or audio), whatever the agent wrote, so every surface
    opens the picker for files the store will keep; for a purpose that
    takes several kinds, an agent's ``accept`` stands only when it names one
    of them (the store refused every pick of a contradicting one: review of
    ec468c821)."""
    from core.chat_attachments import MAX_ATTACHMENTS
    from core.user_file_store import PURPOSES
    kinds = {mime.split('/', 1)[0] for mime in PURPOSES[card['purpose']].types}
    if len(kinds) == 1:
        card['accept'] = next(iter(kinds))
    elif str(card.get('accept') or '').split('/', 1)[0].strip().lower() not in kinds:
        card.pop('accept', None)
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


def ask_push(ask_id: str, user_id: str, ctype: str, title: str, body: str,
             prompt_id=None) -> dict:
    """The Firebase message of an ask.  ``ask_id`` names the ask the phone
    answers through /api/agent/approval; it travels under its own key because
    central's relay replaces a push's request_id (fcm_sync
    _CENTRAL_OWNED_PUSH_KEYS).  No reply topic: none is an answer door.

    It also names the agent that asks (``prompt_id``) and the node that
    holds the ask (fleet_command.self_node_keys: node_id first), the data
    keys of the contract with the phone (23 Nov 2026): a tap on the banner
    of a closed app opens it with these as extras, and the phone reads the
    ask from that node (read_from_request) and shows it in that agent's
    chat when it is open.

    TODO(S4, by 2026-11-30): consent_service.consent_prompt_push and the push
    agent_tools.offer_sound_for_review builds fold onto this one builder.
    """
    from integrations.social.fleet_command import self_node_keys
    title, body = _scrub(title)[:80], _scrub(body)[:240]
    data = {'type': 'ui_ask', 'request_id': ask_id, 'ask_id': ask_id,
            'user_id': str(user_id), 'component_type': ctype,
            'title': title, 'body': body, **self_node_keys()}
    if prompt_id is not None:
        data['prompt_id'] = str(prompt_id)
    return {'title': title, 'body': body, 'data': data}


def _prune(now: float) -> list:
    """Forget the asks kept long enough.  Called under _lock.  An answered or
    dismissed one was closed everywhere when it was; an OPEN one unanswered
    past OPEN_KEEP_S is let go and returned, and the caller closes it on every
    surface (_end_unanswered) once _lock is released: before, it was only
    forgotten here and its card stayed up on every surface."""
    lapsed = []
    for ask_id, a in list(_asks.items()):
        if a.answered_at:
            if now - a.answered_at > ANSWERED_KEEP_S:
                _asks.pop(ask_id, None)
        elif now - a.created > OPEN_KEEP_S:
            _let_go(a, now)
            lapsed.append(a)
    return lapsed


def _let_go(a: '_Ask', now: float) -> None:
    """Called under _lock: the open ask ``a`` closes with no answer.  Nothing
    waits on it any more (its event is set, so a waiting tool call wakes with
    no answer), and it is no longer held, so the agent and a late answer read
    'expired' (ask_state, _expired_or_unknown)."""
    a.answered_at = now
    a.event.set()
    _asks.pop(a.ask_id, None)


def _end_unanswered(asks, why: str, outcome: str = 'expired') -> None:
    """Asks let go unanswered (_let_go) leave every surface as an answered
    one does (_close, saying ``outcome``: 'expired', or 'build_finished'
    for a finished build's), and the hive work held on them goes back to the
    queue.  Never under _lock: _close writes the row and fans out.  Never
    raises."""
    for a in asks:
        try:
            _close(a.ask_id, a.user_id, outcome)
            _hand_back_held_work(a)
            logger.info("person ask %s (%s) of agent %s to user %s closed unanswered: %s",
                        a.ask_id, a.ctype, a.agent_id, a.user_id, why)
        except Exception:
            logger.warning("person ask %s: closing it unanswered (%s) failed",
                           a.ask_id, why, exc_info=True)


#: What create_recipe.recipe answers once the agent is built; its callers in
#: hart_intelligence_entry.chat read the same two.
AGENT_BUILT = ('Agent Created Successfully', 'Agent Already Created Successfully')

#: The CREATE build this code runs in (create_run), as (prompt id, the user
#: the build is for), else None.
_create_run_of: 'contextvars.ContextVar[Optional[Tuple[str, str]]]' = \
    contextvars.ContextVar('person_ask_create_run', default=None)


def _run_key(prompt_id, user_id) -> Optional[Tuple[str, str]]:
    """The key of one CREATE build's asks: its agent and the user the build
    serves (the run's person, who waits for the answer: asked_for), never
    the person a card is shown to (the ask's user_id, or an asked_of field if
    an ask ever names one): a child's build may ask the parent.  None without
    both."""
    parts = tuple('' if p is None else str(p) for p in (prompt_id, user_id))
    return parts if all(parts) else None


def create_run(fn):
    """Decorator for create_recipe.recipe, one CREATE run (one /chat turn) of
    an agent's build for one user.

    An ask made while the run runs is that build's: its tools run inside it,
    the way a camera tool reads the turn's id (core.agent_tools.camera_turn_id).
    A build spans turns (create_recipe 'Using existing session for
    <user>_<prompt>'), so once a run answers that the agent is built
    (AGENT_BUILT), every ask ANY CREATE run of that agent for that user made
    that is still open closes on every surface and the asking agent reads
    'expired' (close_create_run_asks): an ask from turn N of a build that
    finishes at turn N+k was the owner's covering card (live 2026-11-23,
    review of 41b299cbb, whose this-run-only boundary kept it open).  The
    same question asked again in a later run is the same ask and is closed
    with it.  Nothing else is touched: another user's build of the same agent
    (on central, or a phone user's on the owner's desktop; review of
    93937a53c, which closed by agent only), another agent's build, an ask
    made outside a CREATE run (a REUSE turn's above all: it may wait across
    turns).  A run with no prompt id or no user id marks and closes
    nothing."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def _run(*args, **kwargs):
        try:
            bound = sig.bind_partial(*args, **kwargs).arguments
        except TypeError:
            bound = {}
        key = _run_key(bound.get('prompt_id'), bound.get('user_id'))
        token = _create_run_of.set(key)
        try:
            result = fn(*args, **kwargs)
        finally:
            _create_run_of.reset(token)
        if key is not None and isinstance(result, str) and result in AGENT_BUILT:
            close_create_run_asks(*key)
        return result
    return _run


def close_create_run_asks(prompt_id, user_id) -> int:
    """Close every ask a CREATE run of agent ``prompt_id`` for ``user_id``
    made that is still open, in any run of the build: every surface drops
    its card, the bell row is marked read, its saved chat message says it
    closed ('build_finished': an answer after the build ends would land out
    of context, design_1125 A, v4 M2), nothing waits on it, and the agent
    reads 'expired' (ask again if still needed).  Another user's or another
    agent's asks, and asks made outside a CREATE run, are untouched; without
    both ids nothing is closed.  Returns how many.  Never raises: a finished
    build is never failed for its asks."""
    key = _run_key(prompt_id, user_id)
    if key is None:
        return 0
    try:
        now = time.time()
        with _lock:
            ended = [a for a in _asks.values()
                     if a.create_run == key and a.answered_at is None]
            for a in ended:
                _let_go(a, now)
        _end_unanswered(ended, f'the CREATE run of agent {key[0]} for user {key[1]} finished',
                        'build_finished')
        return len(ended)
    except Exception:
        logger.warning("person ask: the open asks of agent %s's CREATE run for user %s "
                       "could not be closed", key[0], key[1], exc_info=True)
        return 0


def _withdraw_card(ask_id: str) -> None:
    try:
        from integrations.agent_engine import liquid_ui_service as lui
        lui.withdraw_agent_ui(ask_id)
    except Exception:
        logger.warning("person ask %s: its card could not be taken back from the "
                       "shell's store", ask_id, exc_info=True)


def _agent_name(agent_id) -> Optional[str]:
    """The name the person knows the asking agent by, or None: the one lookup
    consent cards use (consent_service.agent_display_name).  Never raises."""
    try:
        from integrations.social.consent_service import agent_display_name
        from integrations.social.models import db_session
        with db_session(commit=False) as db:
            return agent_display_name(db, agent_id)
    except Exception:
        logger.debug("person ask: no name for agent %s", agent_id, exc_info=True)
        return None


def _unfilled_blank(body, component) -> Optional[str]:
    """The blank still left in the reason (``body``) or a field's label, or
    None.  What a blank is, is the one rule for every text a person reads
    (core.agent_tools.unfilled_text_placeholder, which the send tools use
    too): the whole text one '<...>' blank, or an identifier joined by '_'
    in <> or [] anywhere in it.  The tool's example writes the reason and the
    question as "<why you ask>" and "<your question>", and the 4B copies the
    example word for word (live 2026-11-07); sent as they are, the card read
    "<why you ask>" over a box labelled "<your question>" (review of
    ab68963ea, MF-1).  "Hi <parent_name>, which day suits you?" was sent as
    an ask while the send tools refused it (review C-1 of 0516f3981).  Words
    that only hold a '<' or a '>' ('Is the parcel <2 kg?') are asked.

    One of the tool example's own blanks with more words around it is a blank
    too, by the same rule: live 2026-11-17 10:55:55 IST a reason that opened
    with "<why you ask>" and went on with the rest of the example reached the
    owner, 23 s after its word-for-word copy was refused (review of the
    rework, MF-A)."""
    from core.agent_tools import unfilled_text_placeholder
    fields = component.get('fields') if isinstance(component, dict) else None
    labels = ([f.get('label') for f in fields if isinstance(f, dict)]
              if isinstance(fields, list) else [])
    for text in [body, *labels]:
        blank = unfilled_text_placeholder(text) if isinstance(text, str) else None
        if blank:
            return blank
    return None


#: The card types whose text is the question itself: a form's field is what
#: the person answers, an approval is a yes or no.  A file ask's purpose says
#: what it asks for ("Send the clips for your reel." is a fair ask).
_QUESTION_TYPES = ('form', 'approval')
#: A question mark in the scripts the cards are written in: ASCII, the
#: full-width one (Chinese, Japanese), the Arabic one, and the Greek (U+037E),
#: Armenian (U+055E) and Ethiopic (U+1367) ones (review of eb6e8053c: a Greek
#: question was refused).  Not ';': U+037E normalises to it, but in English
#: a semicolon asks nothing.  The three are written as code points so no
#: editor or normaliser turns the Greek one into that semicolon.
_QUESTION_MARKS = ('?', '？', '؟', chr(0x037E), chr(0x055E), chr(0x1367))


def _asks_its_question(component, title, body) -> bool:
    """Whether a card an agent wrote says what it asks: for a form or an
    approval, a question mark in its title, its reason (``body``) or a field's
    label.  Measured 2026-11-16 on the desktop (build 75, the local 4B): of
    the 26 cards that reached a person, about half asked nothing; the reason
    was the agent's own step and the one field a noun ("Capture pre-upgrade
    benchmark metrics for performance baseline" over a box labelled
    "Benchmark Capture", "Creating social media post for HART Guardian
    Angels campaign" over "Social Media Post Content").  Owner, the same day:
    "most question does not have detail on what the ask is"."""
    if not isinstance(component, dict) or component.get('type') not in _QUESTION_TYPES:
        return True
    fields = component.get('fields')
    labels = ([f.get('label') for f in fields if isinstance(f, dict)]
              if isinstance(fields, list) else [])
    return any(isinstance(t, str) and any(m in t for m in _QUESTION_MARKS)
               for t in (title, body, *labels))


#: The card keys only this node writes (ask() here): who asks (agent_name,
#: agent_asker, persona) and to whom (to_persona).  An agent writes its
#: component, so what it put under these is dropped here and at the shell's
#: /api/a2ui ingest: a model's 'persona' is a guess.
SAID_BY_THIS_NODE = ('agent_name', 'agent_asker', 'persona', 'to_persona')


def ask(user_id, agent_id, component, *, prompt_id=None, title: str = '',
        body: str = '',
        background: bool = False, question_required: bool = False,
        asked_for=None, asked_in=None) -> dict:
    """Put ``component`` in front of ``user_id`` on every surface they have.

    ``background``: asked by background work (a daemon goal's turn), so it
    counts against the background asks' own open limit, never against the
    asks of the person's own turns (MAX_OPEN_PER_PERSON).

    ``asked_for``: the account whose session asks (the ask_person tool's
    person); its persona with the agent is the one the card's badge names.
    None (a card the framework asks itself, or a person asks) names none.
    ``asked_in``: the request id of the turn that asks.  With ``asked_for``
    and ``prompt_id`` it names the session and turn a waiting step holds the
    ask in (is_held), and the turn whose reply closes it (close_asked_in).

    An ask with a ``prompt_id`` is also the asking agent's chat message,
    saved in the person's chat with that agent (owner 2026-11-23: "while
    the agent is running the question shd be part of chat in all clients";
    design_1125 A): its msg_id is the ask id and its one attachment is
    ``{'kind': 'agent_ask', 'ask_id', 'card'}``.  Saved once, when the ask
    is made; how it ends is written on it (drop_everywhere's ``outcome``).
    An ask with no agent stays a card and a bell row.

    Returns ``{'ok', 'ask_id', 'refused', 'card'}`` (plus ``'reused': True``
    when the same question is already open).  ``refused`` names why nothing
    was sent: 'no_person', 'not_an_object', 'secrets_go_to_the_vault',
    'unknown_purpose', 'no_agent', 'say_why_in_description' (no reason given:
    neither ``body`` nor the component's description), 'unfilled_placeholder'
    (the reason, or a field's label, still holds a blank: the tool example's
    whole '<...>', one of its blanks inside more words, or a fill-in mark
    such as '<parent_name>'),
    'too_many_open_asks', 'not_shown' (the
    Liquid UI gates refused the card: switched off, the hive halted, unsafe
    content, an unknown type, too many pushes; the log says which) or
    'not_filed' (its row could not be written), or 'no_question' (asked
    with ``question_required``: a form or an approval whose title, reason
    and field labels hold no question, see _asks_its_question).
    """
    user_id = str(user_id or '').strip()
    if not user_id:
        return _refused('no_person')
    refused = _refusal(component, prompt_id)
    if refused:
        logger.info("person ask refused (%s) for user %s by agent %s",
                    refused, user_id, agent_id)
        return _refused(refused)
    ctype = component['type']
    agent_id = str(agent_id if agent_id is not None else (prompt_id or ''))
    # The card says who asks and why (owner 2026-11-03: "Your agent asks for
    # files ... not why, for what"): the reason is the agent's own words, so an
    # ask without one is not sent, nor one whose reason or question is still
    # the tool example's blank; with no title of its own, the title names the
    # agent as the person knows it.  The card and the push carry the title and
    # the reason; the bell row carries the title.  A reason or question that
    # still holds a fill-in mark ('<parent_name>') is not sent either.
    body = (body or component.get('description') or '').strip()
    if not body:
        logger.info("person ask refused (say_why_in_description) for user %s by "
                    "agent %s", user_id, agent_id)
        return _refused('say_why_in_description')
    blank = _unfilled_blank(body, component)
    if blank:
        logger.info("person ask refused (unfilled_placeholder %s) for user %s by "
                    "agent %s", blank, user_id, agent_id)
        return _refused('unfilled_placeholder')
    # An agent's card says what it asks (the ask_person tool asks for it; the
    # cards the framework writes itself, the join card, the plan approval and
    # the post destination, are not held to it).
    if question_required and not _asks_its_question(
            component, title or component.get('title'), body):
        # No card text in the log: the reason can carry a child's name
        # (review of eb6e8053c).  The agent gets the text back in its note.
        logger.info("person ask refused (no_question) for user %s by agent %s",
                    user_id, agent_id)
        return _refused('no_question')
    title = (title or component.get('title') or '').strip()
    # Only a card with no title of its own is titled with the agent, and only
    # that card's badge names the agent (below): a card titled by its caller
    # can be a PERSON asking (the join card, _reuse_join_card), and an agent
    # badge above it read as the agent asking (review of bdd6710ce).
    name = asker = None
    if not title:
        name = _agent_name(agent_id)
        # It says an AGENT asks: an agent named like a person ("Vijai") read
        # as another person asking (owner 2026-11-11).
        from integrations.social.consent_service import agent_as_asker
        asker = agent_as_asker(name)
        title = (f'{asker} asks you' if asker
                 else 'Your agent needs your answer')
    card = dict(component)
    card['title'], card['description'] = title, body
    card.pop('msg_id', None)
    for key in SAID_BY_THIS_NODE:
        card.pop(key, None)
    if ctype == 'file_request':
        _file_card(card, prompt_id)
    same = json.dumps([agent_id, card, title, body], sort_keys=True, default=str)
    with _lock:
        lapsed = _prune(time.time())
    _end_unanswered(lapsed, f'unanswered for {OPEN_KEEP_S // 3600} h')
    # Only the open asks of this kind that something waits on fill the cap
    # (MAX_OPEN_PER_PERSON), counted up to it.  Read outside _lock: is_held
    # takes it, and reads the ledger and the waiting goals.
    with _lock:
        kind = [a.ask_id for a in _asks.values()
                if a.user_id == user_id and a.answered_at is None
                and a.background == bool(background) and a.same != same]
    held = 0
    for open_id in kind:
        if held >= MAX_OPEN_PER_PERSON:
            break
        held += is_held(open_id)
    with _lock:
        mine = [a for a in _asks.values() if a.user_id == user_id and a.answered_at is None]
        for a in mine:
            if a.same == same:
                return {'ok': True, 'ask_id': a.ask_id, 'refused': None, 'card': True,
                        'reused': True}
        if held >= MAX_OPEN_PER_PERSON:
            logger.warning("person ask refused for user %s by agent %s: %d %s asks "
                           "already open and waited on", user_id, agent_id, held,
                           'background' if background else 'own')
            return _refused('too_many_open_asks')
        ask_id = uuid.uuid4().hex
        a = _Ask(ask_id, user_id, agent_id,
                 None if prompt_id is None else str(prompt_id), ctype, card, same,
                 background=background, create_run=_create_run_of.get(),
                 asked_for=asked_for, asked_in=asked_in)
        _asks[ask_id] = a
    card['ask_id'] = ask_id
    card['msg_id'] = ask_id
    # Who asks and to whom, for the card's badge (owner 2026-11-25: "where
    # are we showing the persona with agent name to user in chat screen?";
    # design_1125 B).  agent_name and agent_asker are the keys a consent
    # card carries (consent_service._named), from the lookup that titled
    # this one.  persona is the ASKER's: the persona of the account whose
    # session asks (asked_for), never the reader's (review of bdd6710ce).
    # to_persona is the persona of the person the card is addressed to, for
    # a client to compare with its own: this node does not know which
    # persona a device is.  Both from the one reader the pushed message and
    # the /chat reply use (core.flow_progress.persona_of).  Nothing known, no
    # key.  The card only, never the push (ask_push): that leaves this node,
    # and the phone reads the card (read_ask).
    if name:
        card['agent_name'] = name
        if asker:
            card['agent_asker'] = asker
    from core.flow_progress import persona_of
    asking_as = persona_of(asked_for, prompt_id)
    if asking_as:
        card['persona'] = asking_as
    addressed_to = persona_of(user_id, prompt_id)
    if addressed_to:
        card['to_persona'] = addressed_to
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
                push=ask_push(ask_id, user_id, ctype, title, body, prompt_id=prompt_id),
                notification_id=ask_id)
    except Exception:
        # No row, no ask: the card comes back down from every surface it
        # reached (the phone's topic and the page too, not only the shell),
        # and the agent is told, rather than waiting on a question with no
        # record that her answer would find gone.
        with _lock:
            _asks.pop(ask_id, None)
        drop_everywhere(ask_id, user_id)
        logger.warning("person ask for user %s by agent %s NOT filed: its row "
                       "could not be written", user_id, agent_id, exc_info=True)
        return _refused('not_filed')
    if a.answered_at is not None:          # answered before its row existed
        _mark_read(ask_id, user_id)
    _save_in_chat(a, title)
    logger.info("person ask %s (%s) to user %s by agent %s: card, row and push sent",
                ask_id, ctype, user_id, agent_id)
    return {'ok': True, 'ask_id': ask_id, 'refused': None, 'card': True}


#: The kind of the attachment that makes a saved chat message an agent's
#: question: ``{'kind', 'ask_id', 'card'}``, plus ``outcome`` once it ends.
ASK_CHAT_KIND = 'agent_ask'


def _save_in_chat(a: '_Ask', title: str) -> None:
    """Save ask ``a`` as its agent's chat message in its person's chat with
    that agent (ask(); design_1125 A): role 'assistant', the agent's ids,
    msg_id the ask id, the title as its text and the card as it went out in
    its ASK_CHAT_KIND attachment; then chat.new, as for any saved message.
    Every client then shows the question in the chat, live and in the
    chat's history, keyed by the ask id.  An ask with no agent is not saved.
    A failed save is logged: the card and the bell row still ask it."""
    if not a.prompt_id:
        return
    from integrations.social import chat_messages
    card = json.loads(json.dumps(a.card, default=str))
    row = chat_messages.persist(
        a.user_id, 'assistant', title, msg_id=a.ask_id, agent_id=a.prompt_id,
        prompt_id=a.prompt_id, request_id=a.asked_in,
        attachments=[{'kind': ASK_CHAT_KIND, 'ask_id': a.ask_id, 'card': card}])
    if row is None:
        logger.warning("person ask %s: not saved in agent %s's chat; its card and "
                       "bell row still ask it", a.ask_id, a.prompt_id)
        return
    chat_messages.publish_new(row)


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
    """A value under a secret's name: text that is not blank, a number, or a
    dict or list that holds one (an answer is at most MAX_ANSWER_DEPTH deep,
    checked before this)."""
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, dict):
        return any(_has_a_value(x) for x in v.values())
    if isinstance(v, (list, tuple)):
        return any(_has_a_value(x) for x in v)
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


def _write_read(ask_id: str, user_id: str) -> None:
    """Mark the ask's row read: the bell stops listing it, and it is the
    durable record that the ask was answered or dismissed
    (unanswered_on_record).  Raises when it cannot be written."""
    from integrations.social.models import db_session
    from integrations.social.services import NotificationService
    with db_session(commit=True) as db:
        NotificationService.mark_read(db, [ask_id], user_id)


def _mark_read(ask_id: str, user_id: str) -> None:
    try:
        _write_read(ask_id, user_id)
    except Exception:
        logger.warning("person ask %s: its row was not marked read, so the bell "
                       "still lists it", ask_id, exc_info=True)


def _not_recorded(ask_id: str, user_id: str) -> Optional[Tuple[dict, int]]:
    """None once the row records that the ask is closed; else the 503 the
    person is told, and the ask stays open for them to answer again.  An
    answer or a dismissal is taken only after its record: told 200 while
    the write failed ('database is locked'), a denied ask read as never
    answered after a restart and its plan was built (review of dbde21415)."""
    try:
        _write_read(ask_id, user_id)
        return None
    except Exception:
        logger.warning("person ask %s: the answer of user %s could not be recorded, "
                       "so it was not taken and the ask stays open", ask_id, user_id,
                       exc_info=True)
        return {'status': 'error', 'reason': 'not_recorded'}, 503


def drop_everywhere(ask_id: str, user_id: str, outcome: Optional[str] = None) -> None:
    """Every surface is told to drop the ask's card (ANSWERED_TYPE, which is
    the one drop event they listen for) and the shell's store lets it go.

    The phone too: the live legs reach only an app open on this router, so
    the drop also goes as a data-only push naming the request
    (MyFirebaseMessagingService reads ``request_id``), which takes down the
    ask's banner, its card and a consent overlay showing it.  A consent ask
    is dropped the same way by its row id (consent_service), the id its push
    carried.  The drop names the node the ask was answered on
    (fleet_command.self_node_keys), on both legs, so a surface takes down
    only that node's card.

    ``outcome`` says how the ask ended ('answered', 'answered_in_chat',
    'dismissed', 'expired', 'build_finished'): it rides on both legs, and
    the question's saved chat message keeps it (design_1125 A)."""
    said = {'outcome': outcome} if outcome else {}
    try:
        from integrations.social.fleet_command import self_node_keys
        from integrations.social.realtime import on_notification
        node = self_node_keys()
        on_notification(str(user_id), {'type': ANSWERED_TYPE, 'ask_id': ask_id,
                                       'msg_id': answered_msg_id(ask_id), **said, **node},
                        push={'data': {'type': ANSWERED_TYPE,
                                       'request_id': ask_id, 'ask_id': ask_id,
                                       **said, **node}})
    except Exception:
        logger.warning("person ask %s: the drop fan-out failed, so other "
                       "surfaces keep the card", ask_id, exc_info=True)
    _withdraw_card(ask_id)
    if outcome:
        from integrations.social.chat_messages import record_question_outcome
        record_question_outcome(user_id, ask_id, outcome)


def _close(ask_id: str, user_id: str, outcome: Optional[str] = None) -> None:
    """The ask is over (``outcome``, as drop_everywhere takes it): its card
    drops everywhere and the bell stops listing it."""
    drop_everywhere(ask_id, user_id, outcome)
    _mark_read(ask_id, user_id)


def _ask_row(ask_id: str) -> Optional[Tuple[str, bool]]:
    """The ask's row as ``(owner, closed)``, None when it has none, or None
    with a warning when it cannot be read.  ``closed``: marked read or
    dismissed.  The one reader of an ask's row."""
    try:
        from integrations.social.models import Notification, db_session
        with db_session(commit=False) as db:
            row = db.query(Notification).filter_by(id=ask_id, type=ASK_TYPE).first()
            if row is None:
                return None
            return str(row.user_id), bool(row.is_read or row.read_at is not None
                                          or row.dismissed_at is not None)
    except Exception:
        logger.warning("person ask %s: the row could not be read", ask_id, exc_info=True)
        return None


def _owner_of_row(ask_id: str) -> Optional[str]:
    row = _ask_row(ask_id)
    return None if row is None else row[0]


def unanswered_on_record(ask_id, user_id) -> bool:
    """True only when the record proves ``user_id`` never answered nor
    dismissed ask ``ask_id``: its row is hers and was never marked read nor
    dismissed.  An answer or a dismissal is taken only after its row is
    marked read (_not_recorded), so ask_state's 'expired' alone cannot tell
    a denied ask from an unanswered one but the row can (review of
    25d078b8b).  A row read in the bell, a missing row, or one that cannot
    be read is never taken for no answer.  Never raises."""
    ask_id, user_id = str(ask_id or ''), str(user_id or '')
    row = _ask_row(ask_id) if ask_id and user_id else None
    return row is not None and row[0] == user_id and not row[1]


def _expired_or_unknown(ask_id: str, answerer_id) -> Tuple[dict, int]:
    """An ask this process no longer holds (a restart ended the turn that
    waited on it).  Its card may still be on the person's surfaces: their
    answer is told it came too late and the card is dropped everywhere.  To
    anyone else it is simply not found."""
    owner = _owner_of_row(ask_id) if ask_id else None
    if owner is None or not answerer_id or str(answerer_id) != owner:
        return {'status': 'error', 'reason': 'not_found'}, 404
    _close(ask_id, owner, 'expired')
    logger.info("person ask %s: answered after its turn ended; the card is dropped",
                ask_id)
    return {'status': 'error', 'reason': 'expired'}, 410


def answer_ask(ask_id, answer, answerer_id) -> Tuple[dict, int]:
    """The person's answer to ask ``ask_id``, from any surface.

    ``answerer_id`` is the caller's VERIFIED identity, never a body field.
    Returns ``(body, http_status)``: 200 answered; 400 'bad_answer',
    'secrets_go_to_the_vault' or 'files_not_received' (the ask stays open);
    403 'not_yours'; 404 'not_found'; 409 'already_answered' (with the answer
    already given, which stands) or 'dismissed' (she closed it on a surface);
    410 'expired' (the turn that asked ended); 503 'not_recorded' (its row
    could not be written, so the answer was not taken: the ask stays open).
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
            return _already_closed(a)
    problem = _answer_problem(a, answer)
    if problem is None and a.ctype == 'file_request':
        problem = _bind_picks(a, answer['xids'])
    if problem:
        return {'status': 'error', 'reason': problem}, 400
    failed = _not_recorded(ask_id, a.user_id)
    if failed:
        return failed
    with _lock:
        if a.answered_at is not None:
            return _already_closed(a)
        a.answer = dict(answer)
        a.answered_at = time.time()
        a.event.set()
    drop_everywhere(ask_id, a.user_id, 'answered')
    logger.info("person ask %s (%s) answered by user %s", ask_id, a.ctype, a.user_id)
    _hand_back_held_work(a)
    return {'status': 'ok', 'ask_id': ask_id}, 200


def _hand_back_held_work(a: '_Ask') -> None:
    """The hive work held on this ask goes back to the queue now that it is
    answered or dismissed (dispatch.release_holds_answered, by the ask's
    prompt id), not on its goal's next dispatch.  Never raises: the answer
    stands either way."""
    if not a.prompt_id:
        return
    try:
        from integrations.agent_engine.dispatch import release_holds_answered
        release_holds_answered(a.prompt_id)
    except Exception:
        logger.warning("person ask %s: the work held on it was not handed back; "
                       "its goal's next dispatch will", a.ask_id, exc_info=True)


def _already_closed(a: '_Ask') -> Tuple[dict, int]:
    """409 for an ask that is no longer open: answered (the answer stands) or
    dismissed.  Called under _lock."""
    if a.dismissed:
        return {'status': 'error', 'reason': 'dismissed'}, 409
    return {'status': 'error', 'reason': 'already_answered', 'answer': a.answer}, 409


def dismiss_ask(ask_id, dismisser_id) -> Tuple[dict, int]:
    """The person closes ask ``ask_id`` without answering, from any surface.

    The same door and the same rules as answer_ask: ``dismisser_id`` is the
    caller's VERIFIED identity, only the person asked may close it, and only
    while it is open.  It then closes the way an answered ask closes (every
    surface drops its card, the bell row is marked read) and the waiting
    agent wakes to ``dismissed``.  Returns 200; 403 'not_yours'; 404
    'not_found'; 409 'already_answered' or 'dismissed'; 410 'expired' (the
    turn that asked ended: the card is still dropped everywhere); 503
    'not_recorded' (as answer_ask: not taken, the ask stays open)."""
    ask_id = str(ask_id or '')
    with _lock:
        a = _asks.get(ask_id)
    if a is None:
        return _expired_or_unknown(ask_id, dismisser_id)
    if not dismisser_id or str(dismisser_id) != a.user_id:
        logger.warning("person ask %s: a dismissal from %r refused, the ask is for "
                       "user %s", ask_id, dismisser_id, a.user_id)
        return {'status': 'error', 'reason': 'not_yours'}, 403
    with _lock:
        if a.answered_at is not None:
            return _already_closed(a)
    failed = _not_recorded(ask_id, a.user_id)
    if failed:
        return failed
    with _lock:
        if a.answered_at is not None:
            return _already_closed(a)
        a.dismissed = True
        a.answered_at = time.time()
        a.event.set()
    drop_everywhere(ask_id, a.user_id, 'dismissed')
    logger.info("person ask %s (%s) dismissed by user %s", ask_id, a.ctype, a.user_id)
    _hand_back_held_work(a)
    return {'status': 'ok', 'ask_id': ask_id, 'dismissed': True}, 200


def answer_from_request(req, data) -> Tuple[dict, int]:
    """/api/agent/approval's ask branch, for both of its routes (this node's
    and the shell's): the answerer is the caller's Bearer identity.  A
    signed-in person is who their token names (approver_from_request).  A
    phone is this desktop's owner when the owner allowed it and it signed as
    them, else nobody (integrations.social.auth.allowed_device_owner): the
    owner's phone answers the owner's asks, and only asks.  ``"dismiss":
    true`` closes the ask unanswered (dismiss_ask), by the same identity."""
    answerer = _caller(req)
    if (data or {}).get('dismiss') is True:
        return dismiss_ask((data or {}).get('ask_id'), answerer)
    return answer_ask((data or {}).get('ask_id'), (data or {}).get('answer'), answerer)


def _caller(req):
    """Who the approval door's caller is, by the one rule its answer, its
    dismissal and its read use: a signed-in person is who their token names
    (approver_from_request); a phone is this desktop's owner when the owner
    allowed it and it signed as them, else nobody (allowed_device_owner).
    None for nobody."""
    from integrations.commerce.approvals import approver_from_request, bearer_token
    from integrations.social.auth import allowed_device_owner
    return approver_from_request(req) or allowed_device_owner(bearer_token(req))


_GONE = {'status': 'error', 'reason': 'gone'}


def read_ask(ask_id, reader_id) -> Tuple[dict, int]:
    """The card of OPEN ask ``ask_id``, for its own person: what a phone
    reads when the ask's Firebase banner opened the app (owner 2026-11-23:
    "when the app is not open the central or desktop flat node shd be able
    to communicate with user via Firebase"), and shows as the card that
    arrives live.  ``reader_id`` is the caller's VERIFIED identity
    (read_from_request), never a parameter.

    Returns ``(card, 200)``: a copy of the card as it went out, with its
    ask_id and the asking agent's prompt_id.  ``({'reason': 'gone'}, 404)``
    for anything else: an ask answered, dismissed, past OPEN_KEEP_S or no
    longer held (a restart), an unknown id, and another person's ask, so the
    answer says nothing about whose it was (the consent read's rule,
    consent_api._open_ask).  The read changes nothing."""
    ask_id = str(ask_id or '')
    with _lock:
        a = _asks.get(ask_id)
        if (a is None or a.answered_at is not None or not reader_id
                or str(reader_id) != a.user_id
                or time.time() - a.created > OPEN_KEEP_S):
            return dict(_GONE), 404
        card = json.loads(json.dumps(a.card, default=str))
        prompt_id = a.prompt_id
    card['ask_id'] = ask_id
    if prompt_id and not card.get('prompt_id'):
        card['prompt_id'] = prompt_id
    return card, 200


def read_from_request(req) -> Tuple[dict, int]:
    """/api/agent/approval's read of one ask (``GET ?ask_id=``), for both of
    its routes (this node's and the shell's): the reader is the caller, by
    the rule the answer door takes its answerer (_caller)."""
    return read_ask(str(req.args.get('ask_id') or '').strip(), _caller(req))


def wait_for_answer(ask_id, timeout_s) -> Optional[dict]:
    """The answer, if it comes within ``timeout_s`` seconds; else None.
    While it waits, the call holds the ask (is_held)."""
    with _lock:
        a = _asks.get(str(ask_id or ''))
        if a is None:
            return None
        a.waiters += 1
    try:
        a.event.wait(max(0.0, float(timeout_s or 0)))
    finally:
        with _lock:
            a.waiters -= 1
    return dict(a.answer) if a.answer is not None else None


def is_held(ask_id) -> bool:
    """Whether something waits on open ask ``ask_id`` (design_1125 A, v4
    N3): (a) a tool call blocked in wait_for_answer on it; (b) a step of the
    session that asked it waits for the person on the question it asked in
    that turn (hartos.lifecycle_hooks.person_waits_on_ask_from, the hold
    CREATE and REUSE both write); (c) a goal waiting for its person waits on
    it as its plan's card (dispatch.goal_waits_on_card).  The open-ask cap
    reads only this (ask()).  False for an ask not open here."""
    with _lock:
        a = _asks.get(str(ask_id or ''))
        if a is None or a.answered_at is not None:
            return False
        if a.waiters > 0:
            return True
        session = (f'{a.asked_for}_{a.prompt_id}'
                   if a.asked_for and a.prompt_id and a.asked_in else None)
        asked_in = a.asked_in
    from hartos.lifecycle_hooks import person_waits_on_ask_from
    if session and person_waits_on_ask_from(session, asked_in):
        return True
    from integrations.agent_engine.dispatch import goal_waits_on_card
    return goal_waits_on_card(a.ask_id)


def close_asked_in(user_prompt, request_id, reply) -> int:
    """Close, as answered in the chat, every open ask the session
    ``user_prompt`` ('<person>_<prompt id>', the asking session) made in turn
    ``request_id``: the person typed the answer in the chat and the step
    that asked took it as its answer (hartos.lifecycle_hooks.
    record_user_answer, the one answer writer, calls this once it is
    recorded).  The step's judgment closes the ask, whatever the card's
    type (design_1125 A, v4 N2 and its amendment).  Each closes through the
    one drop path with outcome 'answered_in_chat': every surface drops its
    card, the bell row is marked read, its saved chat message says so, a
    tool call waiting on it wakes to the typed words, and a later press is
    told 409 'already_answered'.  Returns how many.  Never raises: the
    answer is recorded either way."""
    session, rid = str(user_prompt or ''), str(request_id or '')
    if not session or not rid:
        return 0
    try:
        now = time.time()
        with _lock:
            ended = [a for a in _asks.values()
                     if a.answered_at is None and a.asked_in == rid and a.asked_for
                     and f'{a.asked_for}_{a.prompt_id}' == session]
            for a in ended:
                a.answer = {'typed_in_chat': str(reply)}
                a.answered_at = now
                a.event.set()
        for a in ended:
            _close(a.ask_id, a.user_id, 'answered_in_chat')
            _hand_back_held_work(a)
            logger.info("person ask %s (%s) answered in the chat by user %s",
                        a.ask_id, a.ctype, a.user_id)
        return len(ended)
    except Exception:
        logger.warning("person ask: the asks of session %s from turn %s were not closed "
                       "after the person answered in the chat", session, rid,
                       exc_info=True)
        return 0


def ask_state(ask_id, user_id) -> dict:
    """``{'status', 'answer'}`` of an ask, as ``user_id``'s agent may read it.

    'open', 'answered' (with the answer), 'dismissed' (she closed it without
    an answer), 'expired' (her ask, no longer held here: a restart, kept past
    ANSWERED_KEEP_S or OPEN_KEEP_S, or closed when the CREATE run that asked
    it finished; ask again) or 'not_found' (no such ask of hers)."""
    ask_id, user_id = str(ask_id or ''), str(user_id or '')
    with _lock:
        a = _asks.get(ask_id)
        if a is not None:
            if a.user_id != user_id:
                return {'status': 'not_found', 'answer': None}
            if a.dismissed:
                return {'status': 'dismissed', 'answer': None}
            if a.answer is not None:
                return {'status': 'answered', 'answer': dict(a.answer)}
            return {'status': 'open', 'answer': None}
    if ask_id and user_id and _owner_of_row(ask_id) == user_id:
        return {'status': 'expired', 'answer': None}
    return {'status': 'not_found', 'answer': None}


def open_ask_ids(user_id, agent_id, background=False) -> list:
    """The ids of ``agent_id``'s own asks of ``user_id`` still open, of one
    kind (background work's, or the person's own turns'), oldest first: what
    an agent refused for too_many_open_asks is told to read instead of
    asking again.  Never another agent's: ask_state lets any agent of the
    person read an id it is given."""
    user_id, agent_id = str(user_id or ''), str(agent_id or '')
    with _lock:
        mine = [a for a in _asks.values()
                if a.user_id == user_id and a.agent_id == agent_id
                and a.answered_at is None and a.background == bool(background)]
    return [a.ask_id for a in sorted(mine, key=lambda a: a.created)]


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


def person_to_ask(user_id, request_id=None) -> Optional[str]:
    """Whom an ask from a turn reaches.  A background turn (request id
    daemon_<goal id>, core.chat_client.daemon_goal_id) asks its goal's
    person; any other turn asks the person behind ``user_id``.  Who the
    person is, is core.event_attribution.person_for's to say: a goal with no
    owner (system seeded), or a system identity, is the node owner's.

    Every daemon turn on a keyless node runs as 'system_daemon'
    (dispatch._internal_auth_headers), so its asks went to that name and
    reached nobody (central 2026-10-31 10:29:25Z).  Owner, relayed
    2026-11-03: daemon goals reach the owner of central (10202) or their
    co-owners."""
    from core.chat_client import daemon_goal_id
    from core.event_attribution import goal_owner_user_id, person_for
    goal_id = daemon_goal_id(request_id)
    if goal_id:
        try:
            from integrations.social.models import AgentGoal, db_session
            with db_session(commit=False) as db:
                goal = db.query(AgentGoal).filter_by(id=goal_id).first()
                if goal is not None:
                    return person_for(goal_owner_user_id(goal), db)
            logger.warning("person ask: goal %s of request %s is not here; the "
                           "turn's own person is asked", goal_id, request_id)
        except Exception:
            logger.warning("person ask: goal %s could not be read; the turn's "
                           "own person is asked", goal_id, exc_info=True)
    return person_for(user_id)


def ask_person_tool(user_id, prompt_id, agent_id=None, request_id=None):
    """The ``ask_person`` tool every agent has: (name, description, func).

    ``request_id`` gives the turn's request id (a callable, read at each
    call): the person asked is person_to_ask's, worked out per call,
    because one agent's session and its tools serve every goal of that
    agent.  A turn that is not the person's own (core.chat_client.
    is_user_turn) asks as background work, under its own open-ask limit.

    A file_request's purpose is one the store keeps (INBOUND_PURPOSES): the
    description names each with what it is for (UploadPurpose.use), and a
    refusal of any other names them again, so the agent's next ask can name
    one (live 2026-11-01: agent 342 made seven file_requests under five
    purposes it made up, 'reel_clips', 'make_reel' and others, and every one
    was refused).  Nothing is guessed for it.

    The hint shows the shape of an ask, with its reason, and holds up no
    agent's purpose: it showed one example, Reel Studio's video request, and
    the speech-therapy agent sent it word for word, twice (live 2026-11-07,
    agent 1794031134533, "choose video" cards with no reason on them).
    Whether this hint changes what the model sends is measured live."""
    from core.agent_tool_menu import ASK_PERSON_SHAPE, ASK_PERSON_TOOL
    from core.chat_client import is_user_turn
    from core.user_file_store import INBOUND_PURPOSES, PURPOSES
    purposes = list(INBOUND_PURPOSES)

    def ask_person(
            component: Annotated[str, ASK_PERSON_SHAPE],
            wait_seconds: Annotated[int, f"seconds to wait now, max {MAX_WAIT_S}"] = DEFAULT_WAIT_S,
            ask_id: Annotated[str, "an earlier ask's id: read its answer"] = '') -> str:
        rid = request_id() if callable(request_id) else request_id
        person = person_to_ask(user_id, rid)
        if ask_id:
            state = ask_state(ask_id, person)
            reply = {'answered': state['status'] == 'answered', 'ask_id': ask_id,
                     'answer': state['answer'],
                     'status': ('waiting_for_person' if state['status'] == 'open'
                                else state['status'])}
            if state['status'] == 'expired':
                reply['note'] = _GONE_NOTE
            elif state['status'] == 'not_found':
                reply['note'] = _NOT_FOUND
            return json.dumps(reply)
        try:
            comp = json.loads(component) if isinstance(component, str) else component
        except ValueError:
            comp = None
        if isinstance(comp, dict) and prompt_id is not None:
            comp.setdefault('prompt_id', str(prompt_id))
        # Not the person's own turn (core.chat_client.is_user_turn, the one
        # rule dispatch uses): its ask is background work's.
        background = not is_user_turn({'request_id': rid})
        asker = agent_id if agent_id is not None else prompt_id
        # asked_for: this session's person, whose persona the badge names
        # (the person asked can be another: a background goal's person).
        # asked_in: this turn, the one a step of this session waits in.
        out = ask(person, asker, comp, prompt_id=prompt_id, background=background,
                  question_required=True, asked_for=user_id, asked_in=rid)
        if not out['ok']:
            # The failed shape stays ('refused', status 'refused'); the note
            # says what was wrong and what to do instead (owner 11-09).
            reply = {'answered': False, 'refused': out['refused'], 'status': 'refused',
                     'note': _refusal_note(out['refused'], comp, person,
                                           '' if asker is None else asker, background)}
            if out['refused'] == 'unknown_purpose':
                reply['purposes'] = purposes
            return json.dumps(reply)
        try:
            wait_s = min(float(wait_seconds or 0), float(MAX_WAIT_S))
        except (TypeError, ValueError):
            wait_s = 0.0
        got = wait_for_answer(out['ask_id'], wait_s)
        if got is not None:
            return json.dumps({'answered': True, 'ask_id': out['ask_id'], 'answer': got,
                               'status': 'answered'})
        # Woken with no answer because the ask is gone (its CREATE run
        # finished while this call waited): said as the read by id says it.
        if ask_state(out['ask_id'], person)['status'] == 'expired':
            return json.dumps({'answered': False, 'ask_id': out['ask_id'],
                               'answer': None, 'status': 'expired', 'note': _GONE_NOTE})
        return json.dumps({
            'answered': False, 'ask_id': out['ask_id'], 'status': 'waiting_for_person',
            'note': ('The question is on their devices and they have not answered '
                     'yet. This step is not done: read the answer later with '
                     '%s(ask_id=...).' % ASK_PERSON_TOOL)})

    return (ASK_PERSON_TOOL,
            ('Ask your person on any of their devices and get their answer. To ask '
             'for files, send a file_request whose purpose is the one that fits: '
             + '; '.join(f'{p} ({PURPOSES[p].use})' if PURPOSES[p].use else p
                         for p in purposes)
             + '. Never for a password or key.'),
            ask_person)


#: The refusal note of an ask with no reason.
_SAY_WHY = ('Not sent: say why you ask in the component\'s "description", in your '
            'own words. The person reads it on the card.')
#: What each refusal tells the model: what was wrong with the call and what
#: to do instead (owner 11-09: "all refusals shd hint llm on what was wrong
#: with tool call").  A bare code made the 4B ask again: 66 of the desktop's
#: 135 failing tool replies were this tool's too_many_open_asks (measured by
#: hartos-16 in llm_outbound.jsonl).  not_an_object, unfilled_placeholder
#: and too_many_open_asks are worded per call, in _refusal_note.
_REFUSAL_NOTES = {
    'no_person': ('Not sent: this turn has no person to ask. Do the step without '
                  'asking, or say in your reply that it needs a person.'),
    'secrets_go_to_the_vault': (
        'Not sent: it asks for a password, key, code or other secret, and a person '
        'is never asked for one this way. Ask for it with request_resource, the '
        'vault\'s credential tool; if request_resource is not in your tools, call '
        'request_tools with need="request_resource" first.'),
    'unknown_purpose': ('Not sent: that "purpose" is not one this node keeps files '
                        'for. Use one of purposes.'),
    'no_agent': ('Not sent: files are kept for the agent that asks, and this turn has '
                 'no agent to keep them for. Do the step without the files, or say in '
                 'your reply that it needs them.'),
    'say_why_in_description': _SAY_WHY,
    'not_shown': ('Not sent: the card was stopped before it reached the person (cards '
                  'switched off, the hive halted, unsafe content, a "type" their '
                  'devices do not show, or too many cards at once). Use a "type" such '
                  'as "form" or "file_request"; if it is stopped again, carry on '
                  'without asking.'),
    'not_filed': ('Not sent: the question could not be saved on this node, so it never '
                  'reached the person. Try once more later; if it fails again, carry '
                  'on without asking.'),
}
#: The note of an ask that is gone ('expired').
_GONE_NOTE = ('This ask is gone (the run that asked it finished, the node restarted, '
              'or it was kept too long). Ask again if you still need it.')

#: An ask_id this person has no question under.
_NOT_FOUND = ('No question of this person has that ask_id. To read an answer, use the '
              'exact ask_id an earlier reply of this tool gave you; to ask a new '
              'question, leave ask_id empty.')


def _refusal_note(refused, comp, person, asker, background) -> str:
    """The note on a refused ask: what was wrong with the call and what to
    do instead, in words the model can act on."""
    from core.agent_tool_menu import ASK_PERSON_SHAPE, ASK_PERSON_TOOL
    if refused == 'not_an_object':
        got = ('it has no "type"' if isinstance(comp, dict)
               else 'what you sent is not JSON' if comp is None
               else 'what you sent is JSON but not an object')
        return (f'Not sent: component must be a JSON object with a "type"; {got}. '
                f'{ASK_PERSON_SHAPE}')
    if refused == 'no_question':
        said = str(comp.get('description') or '').strip()[:160]
        return (f'Not sent: the card asks the person nothing; it reads "{said}". '
                'Write the question itself, as they will read it and ending with "?", '
                'in a field\'s "label" (an approval: in "description"). If this is your '
                'own step\'s work (searching, reading, extracting, generating, checking '
                'a status, judging your own output), do it with your tools instead: '
                'the person cannot see what your tools found.')
    if refused == 'unfilled_placeholder':
        blank = _unfilled_blank(comp.get('description') if isinstance(comp, dict) else '',
                                comp) or 'a <...> blank'
        return (f'Not sent: {blank} is a blank to fill in, not your words, and the '
                'person would read it as it is. Write in its place, in your own words, '
                'why you ask ("description") and what you ask (each field\'s "label": '
                'the question itself, ending with "?"), with the real value where a '
                'name or number goes.')
    if refused == 'too_many_open_asks':
        mine = open_ask_ids(person, asker, background)
        yours = ('Yours are %s: read each answer with %s(ask_id=...), and ask '
                 'something new once one is answered.'
                 % (', '.join(mine), ASK_PERSON_TOOL)) if mine else ''
        if background:
            said = (f'Not sent: this person already has {MAX_OPEN_PER_PERSON} '
                    f'questions from background work open, the most at once. '
                    f'Do not ask again now. ')
            return said + (yours or ("They are other agents' questions: carry on "
                                     'without asking, and ask once one of them is '
                                     'answered.'))
        # The person's own turn: they are in the conversation with this agent
        # now, so its question goes in its own chat message, the step's own
        # send; only the card is refused.  "Carry on without asking" left an
        # ask step nothing to do: live 2026-11-22 14:25 IST the cafe agent's
        # "Hi" turn, three cards of two other agents open, asked four times
        # and replied "I couldn't finish that" after 60.5 s.
        from hartos.lifecycle_hooks import SEND_TOOL
        said = (f'Not sent: this person already has {MAX_OPEN_PER_PERSON} questions '
                f'open, the most at once. Do not ask again now with '
                f'{ASK_PERSON_TOOL}. ')
        chat = (f'This person is talking with you now: ask in your own chat '
                f'message instead, with {SEND_TOOL} and expects_reply true; their '
                f'next message is the answer.')
        return said + (yours or "They are other agents' questions.") + ' ' + chat
    return _REFUSAL_NOTES.get(refused) or (
        f'Not sent ({refused}): change the component before asking again.')
