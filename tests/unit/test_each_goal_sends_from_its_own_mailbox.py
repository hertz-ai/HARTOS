"""Each goal's email goes from its own mailbox, and from no other.

Owner, 16 Nov 2026 (blockers.md item 1): "why cant central itself have all
email sending capabilities what stops it from sending?"  Central sends the
email of six lanes from two company mailboxes: sathish@hevolve.ai for speech
and spoken English, registration@mcgroce.com for lashes, seafood, rice and
textile.  Hevolve mail never goes out from the HertzAI address, nor the
reverse.  A goal whose config names no "sender_email", or whose mailbox is
not connected, is refused with what to fix; it never falls back to another
mailbox or to the shared mailer.

Read before this change: _send_email took the node's ONE email adapter
(registry key 'email', no from-address) or the mailer; the registry held one
adapter per channel name, so a second mailbox replaced the first; and the
channel setup form's connect (the register_channel tool) saved an email
channel without starting it, because it takes four settings.

Review of ae390514f (rejected): a connected mailbox was bound to the chat
agent's auto-responder (4 unread mails got 4 automatic replies from the
company address; two mailboxes answered each other 8 times), another goal's
turn could send from this goal's mailbox, a From carrying the mailbox
address resolved to the owner, and 4 of 5 mutants survived.  Owner, 16 Nov:
"You verify all the outgoing emails on my behalf from central": every
outgoing email passes one check before it leaves.

These tests run the real connect tool, the real channel registry and
FlaskChannelIntegration on a running loop, the real EmailAdapter, the real
outreach tools, follow-up checker and journey engine, and the real social DB
(in memory).  Mocked: the mail servers (imaplib / smtplib), the shared
mailer (pooled_post), the admin config file, the connect watcher's UI cards,
and the reply hook's push and agent dispatch.
"""
import asyncio
import email.mime.text
import email.utils
import imaplib
import json
import logging
import smtplib
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

OWNER = 'owner-ee'
HEVOLVE = 'sathish@hevolve.ai'
MCGROCE = 'registration@mcgroce.com'
PASSWORDS = {HEVOLVE: 'pw-hevolve-SECRET-1', MCGROCE: 'pw-mcgroce-SECRET-2',
             'owner@desktop.example': 'pw-desktop-SECRET-3'}
HOSTS = {HEVOLVE: 'hevolve.example', MCGROCE: 'mcgroce.example',
         'owner@desktop.example': 'desktop.example'}
GOALS = {'g-speech': ('Speech therapy centres', {'sender_email': HEVOLVE}),
         'g-lashes': ('Lash studios', {'sender_email': MCGROCE}),
         'g-plain': ('A goal that names no mailbox', {})}


# ── the mail servers (the boundary) ──────────────────────────────────

class _MailWorld:
    """Every IMAP and SMTP server the adapters reach, by host."""

    def __init__(self):
        self.inbox = {}      # imap host -> [[raw bytes, seen], ...]
        self.sent = []       # one dict per message an SMTP server accepted
        self.bodies = []     # (imap host, fetch spec) per whole message fetched
        self.header_fetches = []   # (imap host, message set) per header FETCH
        self.lock = threading.Lock()

    def seen(self, host):
        with self.lock:
            return [s for _raw, s in self.inbox.get(host, [])]

    def sent_from(self, address):
        return [m for m in self.sent
                if email.utils.parseaddr(m['from'])[1].lower() == address]


def _fake_servers(world):
    class FakeIMAP:
        def __init__(self, host, port=993, ssl_context=None):
            self.host = host

        def login(self, user, password):
            if PASSWORDS.get(user) != password:
                raise imaplib.IMAP4.error('[AUTHENTICATIONFAILED] Invalid credentials')
            return 'OK', [b'logged in']

        def select(self, folder='INBOX'):
            return 'OK', [b'1']

        def search(self, charset, criterion):
            with world.lock:
                box = world.inbox.get(self.host, [])
                nums = [str(i + 1).encode() for i, (_raw, seen) in enumerate(box)
                        if not seen]
            return 'OK', [b' '.join(nums)]

        @staticmethod
        def _numbers(message_set):
            """The message numbers of an IMAP sequence-set ('3:9,12')."""
            text = (message_set.decode() if isinstance(message_set, bytes)
                    else str(message_set))
            numbers = []
            for part in text.split(','):
                low, _, high = part.partition(':')
                numbers.extend(range(int(low), int(high or low) + 1))
            return numbers

        def fetch(self, num, parts):
            with world.lock:
                if 'HEADER.FIELDS' in parts:
                    # A header FETCH of a sequence-set, one reply item per
                    # message, each with its UID (the server's stable number,
                    # the message number plus 1000 here so the two differ).
                    world.header_fetches.append((self.host, num))
                    out = []
                    for n in self._numbers(num):
                        raw = world.inbox[self.host][n - 1][0]
                        head = ('Message-ID: %s\r\n\r\n' % email.message_from_bytes(
                            raw).get('Message-ID', '')).encode()
                        prefix = ('%d (UID %d BODY[HEADER.FIELDS (MESSAGE-ID)] {%d}'
                                  % (n, n + 1000, len(head))).encode()
                        out.extend([(prefix, head), b')'])
                    return 'OK', out
                entry = world.inbox[self.host][int(num) - 1]
                raw = entry[0]
                if 'PEEK' not in parts:
                    entry[1] = True     # a plain fetch sets \Seen, as servers do
                world.bodies.append((self.host, parts))
            return 'OK', [(num + b' (RFC822)', raw)]

        def store(self, num, op, flag):
            with world.lock:
                world.inbox[self.host][int(num) - 1][1] = True
            return 'OK', [b'']

        def logout(self):
            return 'BYE', [b'']

    class FakeSMTP:
        def __init__(self, host, port=587, **kw):
            self.host, self.user = host, None

        def starttls(self, *a, **k):
            return 220, b'ready'

        def login(self, user, password):
            if PASSWORDS.get(user) != password:
                raise smtplib.SMTPAuthenticationError(535, b'5.7.8 not accepted')
            self.user = user

        def send_message(self, msg, *a, **k):
            html = next((p.get_payload(decode=True).decode('utf-8')
                         for p in msg.walk()
                         if p.get_content_type() == 'text/html'), None)
            with world.lock:
                world.sent.append({'host': self.host, 'login': self.user,
                                   'from': msg['From'], 'to': msg['To'],
                                   'subject': msg['Subject'], 'html': html})

        def quit(self):
            return 221, b'bye'

    return FakeIMAP, FakeSMTP


@pytest.fixture
def mail(monkeypatch):
    from integrations.channels.extensions import email_adapter
    world = _MailWorld()
    fake_imap, fake_smtp = _fake_servers(world)
    monkeypatch.setattr(email_adapter, 'HAS_AIOIMAP', False)
    monkeypatch.setattr(email_adapter.imaplib, 'IMAP4_SSL', fake_imap)
    monkeypatch.setattr(email_adapter.smtplib, 'SMTP', fake_smtp)
    return world


@pytest.fixture
def mailer(monkeypatch):
    """The shared HertzAI mailer: it must never carry a goal's email."""
    from core import http_pool
    calls = []

    def _post(*a, **k):
        calls.append((a, k))
        raise ConnectionError('mailer not reachable in this test')
    monkeypatch.setattr(http_pool, 'pooled_post', _post)
    return calls


# ── the node: channels on a running loop, the DB, the prospect store ─

@pytest.fixture
def channels(monkeypatch):
    from integrations.channels import flask_integration
    from integrations.channels import registry as registry_mod
    from integrations.channels.registry import ChannelRegistry

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    registry = ChannelRegistry()
    integration = flask_integration.FlaskChannelIntegration.__new__(
        flask_integration.FlaskChannelIntegration)
    integration.registry = registry
    integration._loop = loop
    integration._thread = None
    monkeypatch.setattr(registry_mod, '_registry', registry)
    monkeypatch.setattr(flask_integration, '_integration', integration)
    yield SimpleNamespace(registry=registry, loop=loop, integration=integration)
    for adapter in _every_adapter(registry):
        try:
            asyncio.run_coroutine_threadsafe(adapter.stop(), loop).result(timeout=5)
        except Exception:
            pass
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def _every_adapter(registry):
    adapters = list(getattr(registry, '_adapters', {}).values())
    accounts = getattr(registry, 'accounts', None)
    if accounts is not None:
        adapters += [a for a in accounts('email').values() if a not in adapters]
    return adapters


class _FakeAdminApi:
    def __init__(self):
        self._channels = {}

    def _save_config(self):
        pass


@pytest.fixture
def node(monkeypatch, tmp_path, channels, mail, mailer):
    """The goals in the DB, the prospect store in this test's folder, the
    admin config in memory, and the connect watcher's cards stubbed."""
    from integrations.agent_engine import outreach_crm_tools
    from integrations.social.models import (
        AgentGoal, Base, UserChannelBinding, db_session, get_engine)
    Base.metadata.create_all(get_engine())
    with db_session() as db:
        for gid, (title, cfg) in GOALS.items():
            db.merge(AgentGoal(id=gid, goal_type='outreach', title=title,
                               config_json=dict(cfg), created_by=OWNER))
        db.query(UserChannelBinding).filter_by(user_id=OWNER).delete()
    monkeypatch.setattr(outreach_crm_tools, '_PROSPECT_FILE',
                        str(tmp_path / 'outreach_prospects.json'))
    monkeypatch.setattr('integrations.channels.admin.api.get_api',
                        lambda: _FakeAdminApi())
    monkeypatch.setattr('integrations.channels.agent_tools._report_when_live',
                        MagicMock(return_value=True))
    yield SimpleNamespace(channels=channels, mail=mail, mailer=mailer,
                          store=outreach_crm_tools)
    with db_session() as db:
        db.query(UserChannelBinding).filter_by(user_id=OWNER).delete()
        for gid in GOALS:
            db.query(AgentGoal).filter_by(id=gid).delete()


# ── helpers: the owner's connect, a goal's turn, the agent's tools ───

def _connect(address, password=None):
    """The owner connects a mailbox in the channel setup form: its submit
    (POST /api/social/channels/email/connect) runs the register_channel
    tool with the form's fields."""
    from integrations.channels.agent_tools import build_channel_tool_closures
    tools = build_channel_tool_closures({'user_id': OWNER, 'prompt_id': None})
    register = next(t[2] for t in tools if t[0] == 'register_channel')
    host = HOSTS[address]
    return register('email', json.dumps({
        'imap_host': f'imap.{host}', 'smtp_host': f'smtp.{host}',
        'email_address': address,
        'password': password or PASSWORDS[address]}))


def _mailbox(channels, address, timeout=10.0):
    """The live adapter of ``address`` once it is connected."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        accounts = getattr(channels.registry, 'accounts', None)
        adapter = (accounts('email') if accounts else {}).get(address)
        if adapter is not None and adapter.is_running():
            return adapter
        time.sleep(0.05)
    raise AssertionError(f'{address} never connected on this node')


def _tools(monkeypatch, store):
    from core import labeled_autogen_function
    tools = {}
    monkeypatch.setattr(labeled_autogen_function, 'register_labeled_function',
                        lambda func, **kw: tools.__setitem__(func.__name__, func))
    store.register_outreach_tools(object(), object(), OWNER)
    return tools


def _prospect(tools, company, address, goal_id=None):
    """create_prospect, in the turn of ``goal_id`` when given: the turn a
    goal's agent works in."""
    create = tools['create_prospect']
    out = (_in_turn(goal_id, create, company, 'Contact', address) if goal_id
           else create(company, 'Contact', address))
    return json.loads(out)['prospect']['id']


def _approved(out):
    """A send tool's reply, as if the owner approved at once what it held.
    A goal's email is held for the owner (HELD_NOTE) and goes out when the
    one approver sends it, exactly as written: ``out`` with the approval's
    outcome folded into the tool's own reply shape (success, sent,
    send_result, error).  A reply that is not held (a refusal, or an email
    for no goal) is returned as it was."""
    if not out.get('held'):
        return out
    from integrations.agent_engine import outreach_crm_tools
    approved = outreach_crm_tools.approve_held_draft(out['draft_id'], approved_by=OWNER)
    return dict(out, success=approved['sent'], sent=approved['sent'],
                send_result=approved.get('send_result'), error=approved.get('error'))


def _send(tools, pid):
    """send_outreach_email to ``pid`` as its goal's own agent would: in that
    goal's turn when the prospect has a goal, else outside any turn; a held
    email approved at once (_approved)."""
    from integrations.agent_engine import outreach_crm_tools
    goal = outreach_crm_tools._load_prospects()['prospects'][pid].get('goal_id')
    args = (pid, 'a question about your centre', '<p>hello</p>')
    send = tools['send_outreach_email']
    return _approved(json.loads(_in_turn(goal, send, *args) if goal else send(*args)))


def _reply(frm, to, subject, body):
    msg = email.mime.text.MIMEText(body)
    msg['From'], msg['To'], msg['Subject'] = frm, to, subject
    msg['Message-ID'] = email.utils.make_msgid()
    return msg.as_bytes()


def _deliver(world, address, *mails):
    """Unread mails, (from, subject, body) each, into ``address``'s inbox."""
    host = f"imap.{HOSTS[address]}"
    with world.lock:
        world.inbox.setdefault(host, []).extend(
            [_reply(frm, address, subject, body), False]
            for frm, subject, body in mails)
    return host


def _poll(channels, *adapters):
    """One inbox check of each adapter: the step its poll loop repeats."""
    for adapter in adapters:
        asyncio.run_coroutine_threadsafe(
            adapter._check_new_messages(), channels.loop).result(timeout=10)


def _legacy_mailbox(node, monkeypatch):
    """A node's one email channel, wired the way the bindings form wires it
    (its IMAP host as the credential, the rest from the environment)."""
    from integrations.social.api_channels import _wire_live_adapter
    monkeypatch.setenv('EMAIL_SMTP_HOST', 'smtp.desktop.example')
    monkeypatch.setenv('EMAIL_ADDRESS', 'owner@desktop.example')
    monkeypatch.setenv('EMAIL_PASSWORD', PASSWORDS['owner@desktop.example'])
    assert _wire_live_adapter('email', 'imap.desktop.example')['success']
    deadline = time.monotonic() + 10
    while True:
        adapter = node.channels.registry.get('email')
        if adapter is not None and adapter.is_running():
            return adapter
        assert time.monotonic() < deadline, 'the email channel never connected'
        time.sleep(0.05)


def _journey_tools(monkeypatch):
    from core import labeled_autogen_function
    from integrations.agent_engine.journey_engine import register_journey_tools
    tools = {}
    monkeypatch.setattr(labeled_autogen_function, 'register_labeled_function',
                        lambda func, **kw: tools.__setitem__(func.__name__, func))
    register_journey_tools(object(), object(), OWNER)
    return tools


def _in_turn(goal_id, fn, *args):
    """``fn(*args)`` inside ``goal_id``'s own turn: the request id
    daemon_<goal> bound the way the turn binds it, and marked as this node's
    own dispatch (core.chat_client.own_daemon_turn, what
    dispatch.local_chat_dispatch does)."""
    from core.chat_client import own_daemon_turn
    from core.llm_outbound_logger import request_id_context
    rid = f'daemon_{goal_id}'
    with request_id_context(rid), own_daemon_turn(rid):
        return fn(*args)


def _named_turn(goal_id, fn, *args):
    """``fn(*args)`` in a turn that only NAMES ``goal_id`` (a client's /chat
    body can carry request_id daemon_<goal>); this node did not start it."""
    from core.llm_outbound_logger import request_id_context
    with request_id_context(f'daemon_{goal_id}'):
        return fn(*args)


# ── 1. two mailboxes, each goal's mail from its own ──────────────────

def test_two_connected_mailboxes_send_each_goals_mail_from_its_own_address(node, monkeypatch):
    for address in (HEVOLVE, MCGROCE):
        assert 'registered and enabled' in _connect(address)
        _mailbox(node.channels, address)
    tools = _tools(monkeypatch, node.store)
    speech = _prospect(tools, 'Clinic', 'ana@clinic.example', 'g-speech')
    lashes = _prospect(tools, 'Salon', 'ravi@salon.example', 'g-lashes')

    sent = [_send(tools, speech), _send(tools, lashes)]

    assert [s['success'] for s in sent] == [True, True], sent
    assert [m['to'] for m in node.mail.sent_from(HEVOLVE)] == ['ana@clinic.example']
    assert [m['to'] for m in node.mail.sent_from(MCGROCE)] == ['ravi@salon.example']
    assert node.mail.sent_from(HEVOLVE)[0]['host'] == 'smtp.hevolve.example'
    assert node.mail.sent_from(MCGROCE)[0]['host'] == 'smtp.mcgroce.example'
    assert node.mailer == []


def test_follow_ups_and_the_journeys_first_email_go_from_the_prospects_goal_mailbox(node, monkeypatch):
    """Every caller of the send resolves the sender from the prospect's
    goal: the daemon's follow-up checker and the journey's first email."""
    from integrations.agent_engine.journey_engine import JourneyEngine
    for address in (HEVOLVE, MCGROCE):
        _connect(address)
        _mailbox(node.channels, address)
    tools = _tools(monkeypatch, node.store)
    speech = _prospect(tools, 'Clinic', 'ana@clinic.example', 'g-speech')
    lashes = _prospect(tools, 'Salon', 'ravi@salon.example', 'g-lashes')
    step = json.dumps([{'delay_days': 0, 'subject': 'bump',
                        'body_html': '<p>hi again</p>'}])
    for pid, goal in ((speech, 'g-speech'), (lashes, 'g-lashes')):
        _in_turn(goal, tools['create_followup_sequence'], pid, 'bump', step)
    for goal in ('g-speech', 'g-lashes'):        # the owner verifies what is held
        node.store.mark_goal_copy_verified(goal, verified_by=OWNER)

    tick = node.store.check_pending_followups_daemon()

    assert tick['sent'] == 2
    assert [m['to'] for m in node.mail.sent_from(HEVOLVE)] == ['ana@clinic.example']
    assert [m['to'] for m in node.mail.sent_from(MCGROCE)] == ['ravi@salon.example']

    data = node.store._load_prospects()
    first = dict(data['prospects'][lashes], emails_sent=0,
                 email_draft={'subject': 'hello', 'body': '<p>hello</p>'})
    held = JourneyEngine()._action_send_email(first, {})      # held for the owner
    assert held.get('held') is True and node.mail.sent_from(MCGROCE)[1:] == []
    approved = node.store.approve_held_draft(held['draft_id'], approved_by=OWNER)
    result = {'sent': approved['sent']}

    assert result.get('sent') is True, approved
    assert [m['to'] for m in node.mail.sent_from(MCGROCE)] == [
        'ravi@salon.example', 'ravi@salon.example']
    assert len(node.mail.sent_from(HEVOLVE)) == 1
    assert node.mailer == []


# ── 2. a goal whose mailbox is not connected ─────────────────────────

def test_a_goal_whose_mailbox_is_not_connected_is_refused_and_nothing_goes_from_the_other(node, monkeypatch):
    _connect(HEVOLVE)
    _mailbox(node.channels, HEVOLVE)
    tools = _tools(monkeypatch, node.store)
    lashes = _prospect(tools, 'Salon', 'ravi@salon.example', 'g-lashes')

    out = _send(tools, lashes)

    assert out['success'] is False and out['sent'] is False
    assert MCGROCE in out['error']
    assert 'channel setup form' in out['error']
    p = node.store._load_prospects()['prospects'][lashes]
    assert (p['stage'], p['emails_sent']) == ('new', 0)
    assert node.mail.sent == []
    assert node.mailer == []


# ── 3. a goal that names no mailbox, and mail that names no goal ─────

def test_a_goal_with_no_sender_email_is_refused(node, monkeypatch):
    for address in (HEVOLVE, MCGROCE):
        _connect(address)
        _mailbox(node.channels, address)
    tools = _tools(monkeypatch, node.store)
    plain = _prospect(tools, 'Shop', 'mo@shop.example', 'g-plain')

    out = _send(tools, plain)

    assert out['success'] is False
    assert 'sender_email' in out['error']
    assert node.mail.sent == []
    assert node.mailer == []


def test_mail_for_no_goal_is_refused_on_a_node_that_holds_goal_mailboxes(node, monkeypatch):
    """A prospect no goal holds (created outside any goal's turn) has no
    mailbox of its own, and none is guessed for it."""
    _connect(HEVOLVE)
    _mailbox(node.channels, HEVOLVE)
    tools = _tools(monkeypatch, node.store)
    nobody = _prospect(tools, 'Shop', 'mo@shop.example')

    out = _send(tools, nobody)

    assert out['success'] is False
    assert 'goal' in out['error']
    assert node.mail.sent == []
    assert node.mailer == []


# ── 4. a node with one mailbox, connected as before, is unchanged ────

def test_a_single_mailbox_node_is_unchanged(node, monkeypatch):
    """The node's one email channel, wired the way the bindings form wires
    it (its IMAP host as the credential, the rest from the environment),
    still sends the mail that names no goal, as before."""
    _legacy_mailbox(node, monkeypatch)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Shop', 'mo@shop.example')

    out = _send(tools, pid)

    assert out['success'] is True and out['send_result']['via'] == 'email_channel'
    assert [m['to'] for m in node.mail.sent_from('owner@desktop.example')] == [
        'mo@shop.example']
    assert node.mailer == []


# ── 5. the second mailbox keeps the first ────────────────────────────

def test_connecting_a_second_mailbox_keeps_the_first(node):
    from integrations.channels.flask_integration import FlaskChannelIntegration
    from integrations.channels.registry import ChannelRegistry
    from integrations.social.models import UserChannelBinding, db_session
    _connect(HEVOLVE)
    first = _mailbox(node.channels, HEVOLVE)
    _connect(MCGROCE)
    second = _mailbox(node.channels, MCGROCE)

    assert first is not second and first.is_running()
    assert node.channels.registry.accounts('email') == {HEVOLVE: first,
                                                        MCGROCE: second}
    with db_session(commit=False) as db:
        rows = db.query(UserChannelBinding).filter_by(
            user_id=OWNER, channel_type='email', is_active=True).all()
        assert sorted(r.channel_sender_id for r in rows) == sorted([HEVOLVE, MCGROCE])

    # After a restart both come back from their bindings.
    restarted = FlaskChannelIntegration.__new__(FlaskChannelIntegration)
    restarted.registry = ChannelRegistry()
    restarted.restore_persisted_channels()
    assert sorted(restarted.registry.accounts('email')) == sorted([HEVOLVE, MCGROCE])


def test_the_mobile_setup_forms_submit_connects_the_mailbox_beside_the_other(node):
    """The phone's channel setup screen sends every field at the top level
    to POST /api/social/channels/bindings, which keeps one credential per
    channel; a mailbox's submit there connects that mailbox, with all its
    settings, beside the one already connected."""
    from flask import Flask
    from integrations.social.api import social_bp
    from integrations.social.api_channels import channel_user_bp
    from integrations.social.models import UserChannelBinding, db_session
    from integrations.social.rate_limiter import get_limiter
    _connect(HEVOLVE)
    first = _mailbox(node.channels, HEVOLVE)
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(social_bp)
    app.register_blueprint(channel_user_bp)
    get_limiter()._buckets.clear()
    client = app.test_client()
    reg = client.post('/api/social/auth/register', json={
        'username': 'mailbox_owner', 'password': 'testpass123',
        'display_name': 'Mailbox Owner'}).get_json()['data']
    try:
        resp = client.post(
            '/api/social/channels/bindings',
            headers={'Authorization': f"Bearer {reg['api_token']}"},
            json={'channel_type': 'email', 'imap_host': 'imap.mcgroce.example',
                  'smtp_host': 'smtp.mcgroce.example',
                  'email_address': MCGROCE, 'password': PASSWORDS[MCGROCE]})

        assert resp.get_json()['success'] is True, resp.get_json()
        assert _mailbox(node.channels, MCGROCE) is not first and first.is_running()
        with db_session(commit=False) as db:
            row = db.query(UserChannelBinding).filter_by(
                user_id=str(reg['id']), channel_type='email').one()
            assert row.channel_sender_id == MCGROCE
            assert {'imap_host', 'smtp_host', 'email_address', 'password'} <= set(
                row.metadata_json)
    finally:
        with db_session() as db:
            db.query(UserChannelBinding).filter_by(user_id=str(reg['id'])).delete()


# ── 6. replies reach the prospect's record from either mailbox ───────

def test_replies_to_each_mailbox_reach_handle_inbound_email(node, monkeypatch):
    store = node.store
    drafts = MagicMock()
    monkeypatch.setattr(store, '_dispatch_response_draft', drafts)
    monkeypatch.setattr(store, '_notify_prospect_replied', MagicMock())
    boxes = {}
    for address in (HEVOLVE, MCGROCE):
        _connect(address)
        boxes[address] = _mailbox(node.channels, address)
    tools = _tools(monkeypatch, store)
    speech = _prospect(tools, 'Clinic', 'ana@clinic.example', 'g-speech')
    lashes = _prospect(tools, 'Salon', 'ravi@salon.example', 'g-lashes')
    store.check_pending_followups_daemon()        # the tick that hooks replies
    with node.mail.lock:
        node.mail.inbox['imap.hevolve.example'] = [[_reply(
            'ana@clinic.example', HEVOLVE, 'Re: a question', 'yes please'), False]]
        node.mail.inbox['imap.mcgroce.example'] = [[_reply(
            'ravi@salon.example', MCGROCE, 'Re: lashes', 'send prices'), False]]

    for adapter in boxes.values():
        asyncio.run_coroutine_threadsafe(
            adapter._check_new_messages(), node.channels.loop).result(timeout=10)

    prospects = store._load_prospects()['prospects']
    assert prospects[speech]['stage'] == 'replied'
    assert prospects[lashes]['stage'] == 'replied'
    assert sorted(c.args[0]['id'] for c in drafts.call_args_list) == sorted([speech, lashes])
    # A reply the outreach hook took is read; nothing else answered it.
    assert node.mail.seen('imap.hevolve.example') == [True]
    assert node.mail.seen('imap.mcgroce.example') == [True]
    assert node.mail.sent == []


def test_a_reply_moves_the_prospect_of_the_mailbox_it_reached(node, monkeypatch):
    """One address can be a prospect of two goals; its reply to the McGroce
    mailbox is a reply to the lashes outreach, not to the speech one."""
    store = node.store
    monkeypatch.setattr(store, '_dispatch_response_draft', MagicMock())
    monkeypatch.setattr(store, '_notify_prospect_replied', MagicMock())
    boxes = {}
    for address in (HEVOLVE, MCGROCE):
        _connect(address)
        boxes[address] = _mailbox(node.channels, address)
    tools = _tools(monkeypatch, store)
    speech = _prospect(tools, 'Both Clinic', 'buyer@both.example', 'g-speech')
    lashes = _prospect(tools, 'Both Salon', 'buyer@both.example', 'g-lashes')
    assert [_send(tools, pid)['success'] for pid in (speech, lashes)] == [True, True]
    store.check_pending_followups_daemon()
    _deliver(node.mail, MCGROCE, ('buyer@both.example', 'Re: lashes', 'prices?'))

    _poll(node.channels, boxes[MCGROCE])

    prospects = store._load_prospects()['prospects']
    assert (prospects[speech]['stage'], prospects[lashes]['stage']) == (
        'contacted', 'replied')


# ── 6b. a company mailbox never answers, and leaves its mail unread ──

PEOPLE = [('bank@bank.example', 'Your statement', 'balance attached'),
          ('newsletter@news.example', 'Weekly digest', 'this week'),
          ('customer@acme.example', 'Invoice query', 'please check')]


def test_a_goal_mailbox_never_answers_its_mail_and_leaves_what_it_did_not_handle_unread(node, monkeypatch):
    """Owner, 16 Nov: a company outreach mailbox never auto-replies.  Its
    inbound goes only to the outreach reply hook (a prospect's reply: stage,
    draft for the owner); everything else stays unread for the owner, and is
    fetched whole once, not on every poll."""
    store = node.store
    drafts = MagicMock()
    monkeypatch.setattr(store, '_dispatch_response_draft', drafts)
    monkeypatch.setattr(store, '_notify_prospect_replied', MagicMock())
    agent = MagicMock(return_value='thank you, noted')
    node.channels.registry.set_agent_handler(agent)
    _connect(HEVOLVE)
    box = _mailbox(node.channels, HEVOLVE)
    tools = _tools(monkeypatch, store)
    speech = _prospect(tools, 'Clinic', 'ana@clinic.example', 'g-speech')
    store.check_pending_followups_daemon()
    host = _deliver(node.mail, HEVOLVE, *PEOPLE,
                    ('ana@clinic.example', 'Re: a question', 'yes please'))

    _poll(node.channels, box)
    _poll(node.channels, box)
    time.sleep(0.5)          # time for any reply the registry might send

    assert node.mail.sent == []
    agent.assert_not_called()
    assert node.mail.seen(host) == [False, False, False, True]
    assert store._load_prospects()['prospects'][speech]['stage'] == 'replied'
    assert drafts.call_count == 1
    assert len(node.mail.bodies) == 4                  # each mail fetched once


def test_a_goal_mailbox_is_never_answered_through_the_nodes_own_email_channel(node, monkeypatch):
    """A node with its own email channel and a goal mailbox: mail to the
    goal mailbox reaches no chat agent, so it is answered from neither
    address."""
    agent = MagicMock(return_value='thank you, noted')
    node.channels.registry.set_agent_handler(agent)
    _legacy_mailbox(node, monkeypatch)
    _connect(HEVOLVE)
    box = _mailbox(node.channels, HEVOLVE)
    node.store.register_reply_handler()      # a mailbox is read only once a hook is on it
    _deliver(node.mail, HEVOLVE, *PEOPLE)

    _poll(node.channels, box)
    time.sleep(0.5)

    agent.assert_not_called()
    assert node.mail.sent == []
    assert len(node.mail.bodies) == len(PEOPLE)          # the poll did read it


def test_two_goal_mailboxes_never_mail_each_other(node):
    agent = MagicMock(return_value='thank you, noted')
    node.channels.registry.set_agent_handler(agent)
    boxes = {}
    for address in (HEVOLVE, MCGROCE):
        _connect(address)
        boxes[address] = _mailbox(node.channels, address)
    node.store.register_reply_handler()      # a mailbox is read only once a hook is on it
    _deliver(node.mail, MCGROCE, (HEVOLVE, 'hello', 'from the other mailbox'))
    _deliver(node.mail, HEVOLVE, (MCGROCE, 'hello', 'from the other mailbox'))

    for _round in range(4):
        _poll(node.channels, *boxes.values())
    time.sleep(0.5)

    assert node.mail.sent == []
    agent.assert_not_called()
    assert len(node.mail.bodies) == 2                    # each read once, by the hook only


# ── 6c. an inbound From is never the owner ───────────────────────────

def test_a_from_header_carrying_a_mailbox_address_is_never_the_owner(node):
    """The owner's binding of a mailbox names who connected it, never who
    writes in: a From that spoofs the company address (or the connect tool's
    placeholder) resolves like any stranger.  A real sender's own binding
    still resolves."""
    from integrations.social.models import UserChannelBinding, db_session
    _connect(HEVOLVE)
    _mailbox(node.channels, HEVOLVE)
    with db_session() as db:
        db.add(UserChannelBinding(user_id=OWNER, channel_type='email',
                                  channel_sender_id='agent_registered',
                                  auth_method='credentials', is_active=True))
        db.add(UserChannelBinding(user_id='u-ana', channel_type='email',
                                  channel_sender_id='ana@clinic.example',
                                  is_active=True))
    resolve = node.channels.integration._resolve_user_id_for_sender
    try:
        assert resolve(channel='email', sender_id=HEVOLVE, fallback='stranger') == 'stranger'
        assert resolve(channel='email', sender_id='agent_registered',
                       fallback='stranger') == 'stranger'
        assert resolve(channel='email', sender_id='ana@clinic.example',
                       fallback='stranger') == 'u-ana'
    finally:
        with db_session() as db:
            db.query(UserChannelBinding).filter_by(user_id='u-ana').delete()


# ── 6d. a goal's turn emails only its own goal's prospects ───────────

def test_a_goals_turn_sends_only_to_its_own_goals_prospects(node, monkeypatch):
    for address in (HEVOLVE, MCGROCE):
        _connect(address)
        _mailbox(node.channels, address)
    tools = _tools(monkeypatch, node.store)
    journey = _journey_tools(monkeypatch)
    speech = _prospect(tools, 'Clinic', 'ana@clinic.example', 'g-speech')
    step = json.dumps([{'delay_days': 0, 'subject': 'lash prices',
                        'body_html': '<p>lashes</p>'}])

    sent = json.loads(_in_turn('g-lashes', tools['send_outreach_email'],
                               speech, 'lash extensions wholesale', '<p>lashes</p>'))
    seq = json.loads(_in_turn('g-lashes', tools['create_followup_sequence'],
                              speech, 'lashes', step))
    msg = json.loads(_in_turn('g-lashes', journey['send_prospect_message'],
                              speech, 'lash prices inside', 'email'))

    # A turn that only names the speech goal (a client's /chat body can),
    # and a person's turn outside any goal, are not the speech goal's own.
    named = json.loads(_named_turn('g-speech', tools['send_outreach_email'],
                                   speech, 'a question', '<p>hello</p>'))
    person = json.loads(tools['send_outreach_email'](
        speech, 'a question', '<p>hello</p>'))

    for out in (sent, seq, msg, named, person):
        assert out.get('success') is False, out
        assert "Speech therapy centres" in json.dumps(out)
    assert node.store._load_prospects()['sequences'] == {}
    assert node.mail.sent == []
    own = _approved(json.loads(_in_turn('g-speech', tools['send_outreach_email'],
                                        speech, 'a question', '<p>hello</p>')))
    assert own['success'] is True and own['held'] is True
    assert [m['to'] for m in node.mail.sent_from(HEVOLVE)] == ['ana@clinic.example']


def test_a_turn_that_only_names_a_goal_never_files_prospects_under_it(node, monkeypatch):
    """A prospect created in a turn this node did not start is no goal's:
    its email cannot go from a goal's mailbox."""
    _connect(HEVOLVE)
    _mailbox(node.channels, HEVOLVE)
    tools = _tools(monkeypatch, node.store)

    pid = json.loads(_named_turn('g-speech', tools['create_prospect'],
                                 'Clinic', 'Contact', 'ana@clinic.example'))['prospect']['id']

    assert 'goal_id' not in node.store._load_prospects()['prospects'][pid]
    assert _send(tools, pid)['success'] is False
    assert node.mail.sent == []


# ── 6e. no goal's email leaves from the node's own or a stray mailbox ─

def test_a_node_mailbox_never_carries_a_goals_email(node, monkeypatch):
    """On a node with its own email channel, a goal that names no mailbox,
    a goal that is gone, and a goal whose mailbox is not connected are each
    refused; none of their email leaves from the node's mailbox."""
    from integrations.agent_engine.journey_engine import JourneyEngine
    _legacy_mailbox(node, monkeypatch)
    tools = _tools(monkeypatch, node.store)
    plain = _prospect(tools, 'Shop', 'mo@shop.example', 'g-plain')
    speech = _prospect(tools, 'Clinic', 'ana@clinic.example', 'g-speech')
    old = _prospect(tools, 'Old', 'old@old.example', 'g-speech')
    gone = dict(node.store._load_prospects()['prospects'][old], goal_id='g-deleted',
                email_draft={'subject': 'hello', 'body': '<p>hi</p>'})

    outs = {pid: _send(tools, pid) for pid in (plain, speech)}
    first = JourneyEngine()._action_send_email(gone, {})   # no turn: the journey tick

    assert all(o['success'] is False for o in outs.values()), outs
    assert 'names no mailbox' in outs[plain]['error']
    assert f'{HEVOLVE} that goal' in outs[speech]['error']
    assert first.get('sent') is False and 'is not on this node' in first['result']['error']
    assert node.mail.sent == []
    assert node.mailer == []


def test_a_mailbox_that_is_not_connected_sends_nothing_even_when_its_smtp_answers(node, monkeypatch):
    """The mailbox is still registered, but its connection is down (the
    state a failed connect leaves, ChannelAdapter.start: ERROR); its SMTP
    server would still take mail.  Nothing goes out until it is connected
    again.  (Set directly: in this test venv, 3.12.3 on the 3.12.6 stdlib,
    an exception raised in an executor never reaches the awaiting
    coroutine, so neither a refused IMAP login nor stop() can end.)"""
    from integrations.channels.base import ChannelStatus
    _connect(HEVOLVE)
    box = _mailbox(node.channels, HEVOLVE)
    box.status = ChannelStatus.ERROR
    assert node.channels.registry.accounts('email').get(HEVOLVE) is box
    tools = _tools(monkeypatch, node.store)

    out = _send(tools, _prospect(tools, 'Clinic', 'ana@clinic.example', 'g-speech'))

    assert out['success'] is False and 'not connected' in out['error']
    assert node.mail.sent == []


def test_a_send_that_names_no_mailbox_never_leaves_from_a_goal_mailbox(node):
    """The channel tools send through 'email' with no mailbox named: that
    is never one of the goals' company mailboxes."""
    from integrations.channels.agent_tools import build_channel_tool_closures
    for address in (HEVOLVE, MCGROCE):
        _connect(address)
        _mailbox(node.channels, address)
    send = next(t[2] for t in build_channel_tool_closures({'user_id': OWNER})
                if t[0] == 'send_to_channel')

    out = send('email', 'Your order is ready', 'mo@shop.example')

    assert 'successfully' not in out, out
    assert node.mail.sent == []


# ── 6f. every outgoing email is verified before it leaves ────────────

def test_every_outgoing_email_is_verified_before_it_leaves(node, monkeypatch):
    """Owner, 16 Nov: "You verify all the outgoing emails on my behalf from
    central."  One check every email passes before it leaves: a template
    blank left in it, or an address of the node's own mailboxes, stops it
    with what to fix; a clean email goes."""
    for address in (HEVOLVE, MCGROCE):
        _connect(address)
        _mailbox(node.channels, address)
    tools = _tools(monkeypatch, node.store)
    ana = _prospect(tools, 'Clinic', 'ana@clinic.example', 'g-speech')
    own = _prospect(tools, 'Us', MCGROCE, 'g-speech')

    send = tools['send_outreach_email']
    footer = json.loads(_in_turn('g-speech', send, ana, 'a question',
                                 '<p>hello</p><p>{POSTAL ADDRESS}</p>'))
    subject = json.loads(_in_turn('g-speech', send, ana,
                                  'quick question about {product_keyword}',
                                  '<p>hello</p>'))
    to_self = _send(tools, own)

    for out in (footer, subject):
        assert out['success'] is False and 'blank' in out['error'], out
    assert '{POSTAL ADDRESS}' in footer['error']
    assert "node's own" in to_self['error']
    assert node.mail.sent == []
    assert _send(tools, ana)['success'] is True
    assert len(node.mail.sent) == 1


def test_the_node_mailboxs_email_is_verified_too(node, monkeypatch):
    _legacy_mailbox(node, monkeypatch)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Shop', 'mo@shop.example')

    out = json.loads(tools['send_outreach_email'](
        pid, 'hello', '<p>hi {first_name}</p>'))

    assert out['success'] is False and 'blank' in out['error']
    assert node.mail.sent == [] and node.mailer == []


# ── 6g. review of 88b6821f2: the journey, the mailbox's owner, the poll ─

def _quiet_journey(monkeypatch, store):
    """The journey's side doors a stage change knocks on, stubbed: the agent
    dispatch (deep research, draft response) and the owner's push."""
    from integrations.agent_engine import dispatch
    from integrations.channels.response import router
    monkeypatch.setattr(dispatch, 'dispatch_goal', MagicMock(), raising=False)
    monkeypatch.setattr(store, '_dispatch_response_draft', MagicMock())
    monkeypatch.setattr(router, 'get_response_router', lambda *a, **k: MagicMock())


def test_a_goals_turn_moves_only_its_own_goals_prospects_through_the_journey(node, monkeypatch):
    """M1: advance_prospect_stage ran a stage's auto-actions for any goal's
    prospect, so another goal's turn made this goal's mailbox send ('engaged':
    a meeting proposal; 'outreach': the journey's template email) while its
    sibling send_prospect_message refused."""
    _quiet_journey(monkeypatch, node.store)
    for address in (HEVOLVE, MCGROCE):
        _connect(address)
        _mailbox(node.channels, address)
    tools = _tools(monkeypatch, node.store)
    journey = _journey_tools(monkeypatch)
    speech = _prospect(tools, 'Clinic', 'ana@clinic.example', 'g-speech')
    speech2 = _prospect(tools, 'Clinic Two', 'bo@clinic2.example', 'g-speech')
    advance = journey['advance_prospect_stage']

    engaged = json.loads(_in_turn('g-lashes', advance, speech, 'engaged'))
    research = json.loads(_in_turn('g-lashes', advance, speech2, 'research'))
    outreach = json.loads(_in_turn('g-lashes', advance, speech2, 'outreach'))
    named = json.loads(_named_turn('g-speech', advance, speech, 'engaged'))
    person = json.loads(advance(speech, 'engaged'))

    for out in (engaged, research, outreach, named, person):
        assert out.get('success') is False and 'moved' not in out, out
        assert 'Speech therapy centres' in json.dumps(out)
    prospects = node.store._load_prospects()['prospects']
    assert all('journey_stage' not in prospects[p] for p in (speech, speech2))
    assert node.mail.sent == []
    own = json.loads(_in_turn('g-speech', advance, speech2, 'research'))
    assert own.get('moved') is True


def test_a_goals_prospect_changes_stage_only_in_its_own_goals_turn(node, monkeypatch):
    """S6: move_prospect_stage was the one writer left unguarded; another
    goal's turn (and a person's) moved a speech prospect to 'won' / 'lost'."""
    tools = _tools(monkeypatch, node.store)
    speech = _prospect(tools, 'Clinic', 'ana@clinic.example', 'g-speech')
    move = tools['move_prospect_stage']

    other = json.loads(_in_turn('g-lashes', move, speech, 'won'))
    person = json.loads(move(speech, 'lost', 'not interested'))

    for out in (other, person):
        assert out['success'] is False and 'Speech therapy centres' in json.dumps(out)
    stage = lambda: node.store._load_prospects()['prospects'][speech]['stage']  # noqa: E731
    assert stage() == 'new'
    own = json.loads(_in_turn('g-speech', move, speech, 'contacted'))
    assert own['success'] is True and stage() == 'contacted'


def test_a_persons_turn_is_told_what_the_person_can_do(node, monkeypatch):
    """S3: a person's turn has no goal, so "work on this turn's own goal's
    prospects" tells it nothing.  It reads what to tell the person; another
    goal's turn still reads to work on its own goal's prospects."""
    tools = _tools(monkeypatch, node.store)
    speech = _prospect(tools, 'Clinic', 'ana@clinic.example', 'g-speech')
    args = (speech, 'a question', '<p>hello</p>')

    person = json.loads(tools['send_outreach_email'](*args))
    other = json.loads(_in_turn('g-lashes', tools['send_outreach_email'], *args))

    assert person['refused'] == 'another_goals_prospect' and person['retry'] == 'never'
    assert 'Tell the person' in person['note'] and 'steer' in person['note']
    assert 'Speech therapy centres' in person['note']
    assert 'get_pipeline_status' not in person['note']
    assert other['retry'] == 'after_fix' and 'get_pipeline_status' in other['note']


def test_the_pipeline_lists_each_prospects_goal(node, monkeypatch):
    """S7: the refusals point the agent to get_pipeline_status's goal_id."""
    monkeypatch.setattr(node.store, '_get_erxes', lambda: None)
    tools = _tools(monkeypatch, node.store)
    speech = _prospect(tools, 'Clinic', 'ana@clinic.example', 'g-speech')
    lashes = _prospect(tools, 'Salon', 'ravi@salon.example', 'g-lashes')
    nobody = _prospect(tools, 'Shop', 'mo@shop.example')

    status = json.loads(tools['get_pipeline_status']())

    listed = {p['id']: p['goal_id'] for rows in status['pipeline'].values() for p in rows}
    assert listed == {speech: 'g-speech', lashes: 'g-lashes', nobody: None}


def test_connecting_a_mailbox_says_what_a_goal_mailbox_is_for(node):
    """S4: the connect form reads like every chat channel; the reply says
    this mailbox sends goals' email, reads only prospects' replies and never
    chats."""
    said = _connect(HEVOLVE)

    assert 'registered and enabled' in said
    assert 'goal mailbox' in said and "prospects' replies" in said
    assert 'does not chat with the agent' in said


# ── 6h. who may use a company mailbox (review of 88b6821f2, M2) ──────

def _api():
    """The real routes a signed-in account reaches: sign-up, goals and the
    channel setup form."""
    from flask import Flask
    from integrations.agent_engine.api import agent_engine_bp
    from integrations.social.api import social_bp
    from integrations.social.api_channels import channel_user_bp
    from integrations.social.rate_limiter import get_limiter
    app = Flask(__name__)
    app.config['TESTING'] = True
    for blueprint in (social_bp, channel_user_bp, agent_engine_bp):
        app.register_blueprint(blueprint)
    get_limiter()._buckets.clear()
    return app.test_client()


@pytest.fixture
def web(node):
    """Accounts that sign up, create goals and connect mailboxes through the
    real routes; their goals and bindings are removed afterwards."""
    import uuid
    from integrations.agent_engine import goal_manager
    from integrations.social.models import AgentGoal, UserChannelBinding, db_session
    if 'outreach' not in goal_manager.get_registered_types():
        node.store.register_outreach_goal_type()
    made = SimpleNamespace(client=_api(), users=[], goals=[])

    def sign_up(name):
        reg = made.client.post('/api/social/auth/register', json={
            'username': f'{name}_{uuid.uuid4().hex[:6]}', 'password': 'testpass123',
            'display_name': name}).get_json()['data']
        made.users.append(str(reg['id']))
        return {'id': str(reg['id']),
                'auth': {'Authorization': f"Bearer {reg['api_token']}"}}

    def connect(who, address, password=None):
        host = HOSTS[address]
        return made.client.post(
            '/api/social/channels/email/connect', headers=who['auth'],
            json={'imap_host': f'imap.{host}', 'smtp_host': f'smtp.{host}',
                  'email_address': address,
                  'password': password or PASSWORDS[address]})

    def goal(who, title, sender, **config):
        """POST /api/goals as ``who``; ``config`` is the body's config beside
        sender_email (a client can write anything there)."""
        resp = made.client.post('/api/goals', headers=who['auth'], json={
            'goal_type': 'outreach', 'title': title,
            'description': 'tell people about it',
            'config': {'sender_email': sender, **config}})
        assert resp.status_code == 201, resp.get_json()
        made.goals.append(resp.get_json()['goal']['id'])
        return made.goals[-1]

    made.sign_up, made.connect, made.goal = sign_up, connect, goal
    yield made
    with db_session() as db:
        for gid in made.goals:
            db.query(AgentGoal).filter_by(id=gid).delete()
        for uid in made.users:
            db.query(UserChannelBinding).filter_by(user_id=uid).delete()


def _send_in(goal, tools, to='victim@anywhere.example', subject='cheap watches'):
    """A goal's agent creates a prospect and emails it, in the goal's own turn."""
    pid = _prospect(tools, 'Anyone', to, goal)
    return _approved(json.loads(_in_turn(goal, tools['send_outreach_email'], pid,
                                         subject, '<p>buy our watches</p>')))


def test_a_strangers_goal_cannot_send_from_a_company_mailbox(node, web, monkeypatch):
    """M2: any signed-in account creates a goal through POST /api/goals with
    config sender_email set to a company mailbox, and the daemon runs it:
    mail then left the company's address with a stranger's words."""
    _connect(HEVOLVE)
    _mailbox(node.channels, HEVOLVE)
    stranger = web.sign_up('stranger')
    goal = web.goal(stranger, 'Spring watch sale', HEVOLVE)

    out = _send_in(goal, _tools(monkeypatch, node.store))

    assert out['success'] is False and out['sent'] is False
    assert HEVOLVE in out['error'] and "owner" in out['error']     # who may use it
    assert node.mail.sent == [] and node.mailer == []


def test_the_account_that_connected_a_mailbox_may_make_a_goal_that_sends_from_it(node, web, monkeypatch):
    seller = web.sign_up('seller')
    resp = web.connect(seller, MCGROCE)
    assert resp.get_json()['success'] is True, resp.get_json()
    _mailbox(node.channels, MCGROCE)
    goal = web.goal(seller, 'My own lash outreach', MCGROCE)

    out = _send_in(goal, _tools(monkeypatch, node.store))

    assert out['success'] is True, out
    assert [m['to'] for m in node.mail.sent_from(MCGROCE)] == ['victim@anywhere.example']


def test_a_goal_made_by_the_nodes_owner_may_use_a_mailbox_another_account_connected(node, web, monkeypatch):
    owner = web.sign_up('theowner')
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', owner['id'])
    _connect(HEVOLVE)                      # connected by the 'owner-ee' account
    _mailbox(node.channels, HEVOLVE)
    goal = web.goal(owner, 'Speech therapy centres, the owner\'s own', HEVOLVE)

    out = _send_in(goal, _tools(monkeypatch, node.store), to='ana@clinic.example')

    assert out['success'] is True, out
    assert [m['to'] for m in node.mail.sent_from(HEVOLVE)] == ['ana@clinic.example']


def test_a_second_account_cannot_connect_a_mailbox_another_account_holds(node, web):
    from integrations.social.models import UserChannelBinding, db_session
    _connect(HEVOLVE)
    first = _mailbox(node.channels, HEVOLVE)
    intruder = web.sign_up('intruder')

    resp = web.connect(intruder, HEVOLVE, password='intruder-SECRET')

    assert resp.status_code == 400 and 'another account' in resp.get_json()['error']
    assert node.channels.registry.accounts('email')[HEVOLVE] is first and first.is_running()
    with db_session(commit=False) as db:
        assert db.query(UserChannelBinding).filter_by(
            user_id=intruder['id'], channel_type='email').count() == 0
        assert db.query(UserChannelBinding).filter_by(
            user_id=OWNER, channel_type='email', channel_sender_id=HEVOLVE,
            is_active=True).count() == 1


def test_a_mailbox_named_in_a_bindings_metadata_is_not_a_connect(node, web, monkeypatch):
    """POST /api/social/channels/bindings stores 'metadata' verbatim: a row
    naming a mailbox there needs no credential and no connect, yet read as
    "the account that connected it"."""
    from integrations.social.models import UserChannelBinding, db_session
    _connect(HEVOLVE)
    _mailbox(node.channels, HEVOLVE)
    forger = web.sign_up('forger')

    resp = web.client.post('/api/social/channels/bindings', headers=forger['auth'], json={
        'channel_type': 'email', 'channel_sender_id': HEVOLVE,
        'metadata': {'email_address': HEVOLVE}})
    goal = web.goal(forger, 'Spring watch sale', HEVOLVE)
    out = _send_in(goal, _tools(monkeypatch, node.store))

    assert resp.status_code == 400
    with db_session(commit=False) as db:
        assert db.query(UserChannelBinding).filter_by(user_id=forger['id']).count() == 0
    assert out['success'] is False and node.mail.sent == []


@pytest.mark.parametrize('metadata', [
    {'email_address': ''},                         # the key, no value
    {'email_address': None},
    {'email_address': '   '},
    {'imap_host': 'imap.evil.example', 'email_address': ''},
    {'email_address': 'someone@else.example'},     # another address entirely
], ids=['empty', 'null', 'blank', 'empty+host', 'another-address'])
def test_a_binding_posted_with_no_value_for_the_mailbox_is_not_a_connect(node, web, monkeypatch, metadata):
    """POST /bindings stored 'metadata' verbatim when it named no usable
    address, and a row counted as the connect of a mailbox when
    'email_address' was merely a KEY of its metadata: any signed-in account
    posted {channel_sender_id: <company mailbox>, metadata: {email_address: ''}}
    and held the mailbox with no password (review of ab8e0c840)."""
    from integrations.channels.flask_integration import FlaskChannelIntegration
    from integrations.social.models import UserChannelBinding, db_session
    _connect(HEVOLVE)
    _mailbox(node.channels, HEVOLVE)
    forger = web.sign_up('forger')

    web.client.post('/api/social/channels/bindings', headers=forger['auth'], json={
        'channel_type': 'email', 'channel_sender_id': HEVOLVE, 'metadata': metadata})
    goal = web.goal(forger, 'Spring watch sale', HEVOLVE)
    out = _send_in(goal, _tools(monkeypatch, node.store))

    assert out['success'] is False and node.mail.sent == []
    with db_session(commit=False) as db:
        assert FlaskChannelIntegration.accounts_connecting(db, 'email', HEVOLVE) == {OWNER}
        for row in db.query(UserChannelBinding).filter_by(user_id=forger['id']).all():
            if row.channel_sender_id == HEVOLVE:        # what the route stored for it
                assert 'email_address' not in (row.metadata_json or {})


def test_only_a_row_naming_the_mailbox_is_a_connect_of_it(node):
    """The reader of who holds a mailbox: a row counts when the address its
    metadata holds is the mailbox (any case); the key alone, an empty or
    another value, and no metadata do not, whoever wrote the row."""
    from integrations.channels.flask_integration import FlaskChannelIntegration
    from integrations.social.models import UserChannelBinding, db_session
    _connect(HEVOLVE)
    shapes = {'u-empty': {'email_address': ''}, 'u-null': {'email_address': None},
              'u-other': {'email_address': 'other@else.example'}, 'u-bare': None,
              'u-other-key': {'smtp_host': 'smtp.hevolve.example'},
              'u-same': {'email_address': HEVOLVE.upper()}}
    with db_session() as db:
        for user, meta in shapes.items():
            db.add(UserChannelBinding(user_id=user, channel_type='email',
                                      channel_sender_id=HEVOLVE, is_active=True,
                                      metadata_json=meta))
    try:
        with db_session(commit=False) as db:
            held = FlaskChannelIntegration.accounts_connecting(db, 'email', HEVOLVE)
        assert held == {OWNER, 'u-same'}
    finally:
        with db_session() as db:
            db.query(UserChannelBinding).filter(
                UserChannelBinding.user_id.in_(list(shapes))).delete(
                synchronize_session=False)


def test_a_connect_sealed_by_the_vault_still_counts(node, monkeypatch, tmp_path):
    """On a node with HEVOLVE_MASTER_KEY the connect tool stores the address
    SEALED (it is a required setup field); the reader opens it, so the
    account that connected a mailbox on such a node can still use it."""
    from integrations.social.models import UserChannelBinding, db_session
    from security import secrets_manager
    from security.secrets_manager import SecretsManager
    monkeypatch.setattr(secrets_manager, '_SALT_PATH', str(tmp_path / 'salt'))
    monkeypatch.setattr(secrets_manager, '_VAULT_PATH', str(tmp_path / 'vault'))
    monkeypatch.setenv('HEVOLVE_MASTER_KEY', 'test-only-vault-passphrase')
    SecretsManager.reset()
    try:
        _connect(HEVOLVE)
        _mailbox(node.channels, HEVOLVE)
        with db_session(commit=False) as db:
            row = db.query(UserChannelBinding).filter_by(
                user_id=OWNER, channel_type='email', channel_sender_id=HEVOLVE).one()
            assert row.metadata_json['email_address'].startswith('fernet:')

        tools = _tools(monkeypatch, node.store)
        out = _send(tools, _prospect(tools, 'Clinic', 'ana@clinic.example', 'g-speech'))

        assert out['success'] is True, out
        assert [m['to'] for m in node.mail.sent_from(HEVOLVE)] == ['ana@clinic.example']
    finally:
        SecretsManager.reset()


def test_the_nodes_owner_connecting_a_mailbox_ends_a_squatters_claim(node, web, monkeypatch):
    """A stranger connects the company address before the owner does (with
    credentials of their own); once the owner connects it for real, the
    stranger's goals cannot send from it."""
    owner = web.sign_up('theowner')
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', owner['id'])
    squatter = web.sign_up('squatter')
    assert web.connect(squatter, HEVOLVE, password='squatter-SECRET').get_json()['success']
    squatted = web.goal(squatter, 'Spring watch sale', HEVOLVE)

    resp = web.connect(owner, HEVOLVE)
    assert resp.get_json()['success'] is True, resp.get_json()
    _mailbox(node.channels, HEVOLVE)
    tools = _tools(monkeypatch, node.store)
    refused = _send_in(squatted, tools)
    own = _send_in(web.goal(owner, 'Speech therapy centres, the owner\'s own', HEVOLVE),
                   tools, to='ana@clinic.example')

    assert refused['success'] is False
    assert own['success'] is True, own
    assert [m['to'] for m in node.mail.sent_from(HEVOLVE)] == ['ana@clinic.example']


# ── 6i. the poll: a reply is not lost, an unread inbox is cheap ──────

def test_a_reply_waiting_before_the_hook_is_attached_is_matched_once_it_is(node, monkeypatch):
    """S1: a prospect's reply already in the mailbox when it connects (a
    restart, or mail that came while the node was down) was recorded as seen
    by a poll that had no handler, so it was never matched and the prospect
    kept getting follow-ups."""
    store = node.store
    drafts = MagicMock()
    monkeypatch.setattr(store, '_dispatch_response_draft', drafts)
    monkeypatch.setattr(store, '_notify_prospect_replied', MagicMock())
    tools = _tools(monkeypatch, store)
    speech = _prospect(tools, 'Clinic', 'ana@clinic.example', 'g-speech')
    host = _deliver(node.mail, HEVOLVE,
                    ('ana@clinic.example', 'Re: a question', 'yes please'))
    _connect(HEVOLVE)
    box = _mailbox(node.channels, HEVOLVE)

    _poll(node.channels, box)                  # no hook on the mailbox yet
    assert node.mail.seen(host) == [False] and node.mail.bodies == []
    store.check_pending_followups_daemon()     # the tick that attaches the hook
    _poll(node.channels, box)

    assert store._load_prospects()['prospects'][speech]['stage'] == 'replied'
    assert drafts.call_count == 1
    assert node.mail.seen(host) == [True]


def test_an_unhandled_inbox_costs_one_header_fetch_per_poll(node, monkeypatch):
    """S2: the owner's unread mail stays unread, so every poll meets all of
    it again: one header FETCH for the whole set (not one per mail), and a
    mail with no Message-ID known by its UID (it was fetched whole on every
    poll)."""
    _connect(HEVOLVE)
    box = _mailbox(node.channels, HEVOLVE)
    node.store.register_reply_handler()
    host = _deliver(node.mail, HEVOLVE,
                    *[(f'p{i}@x.example', f's{i}', 'b') for i in range(50)])
    with node.mail.lock:
        node.mail.inbox[host].append(
            [email.mime.text.MIMEText('no id').as_bytes(), False])   # no Message-ID

    per_poll = []
    for _ in range(3):
        headers, bodies = len(node.mail.header_fetches), len(node.mail.bodies)
        _poll(node.channels, box)
        per_poll.append((len(node.mail.header_fetches) - headers,
                         len(node.mail.bodies) - bodies))

    assert per_poll == [(1, 51), (1, 0), (1, 0)]
    assert node.mail.seen(host).count(False) == 51          # still the owner's


def test_a_server_that_puts_the_uid_after_the_header_is_read_too():
    """Where a server puts the UID in a header FETCH reply varies: after the
    header literal, in the closing ' UID 12)', is as valid as before it."""
    from integrations.channels.extensions.email_adapter import (
        EmailAdapter, EmailConfig, _sequence_sets)

    class _Imap:
        def fetch(self, message_set, parts):
            return 'OK', [(b'1 (BODY[HEADER.FIELDS (MESSAGE-ID)] {20}',
                           b'Message-ID: <a@x>\r\n\r\n'), b' UID 11)',
                          (b'2 (BODY[HEADER.FIELDS (MESSAGE-ID)] {4}', b'\r\n\r\n'),
                          b' UID 12)']

    adapter = EmailAdapter(EmailConfig(mark_as_read=False))
    adapter._imap = _Imap()
    assert adapter._unprocessed([b'1', b'2']) == [
        (b'1', b'11', '<a@x>'), (b'2', b'12', '')]
    adapter._seen_uids.add(b'12')
    adapter._seen_message_ids.add('<a@x>')
    assert adapter._unprocessed([b'1', b'2']) == []
    assert _sequence_sets([b'1', b'2', b'3', b'7', b'9', b'10']) == ['1:3,7,9:10']
    assert _sequence_sets([b'1', b'3', b'5', b'7'], limit=4) == ['1,3', '5,7']
    assert _sequence_sets([]) == []


# ── 7. no password in a refusal, a tool result or a log line ─────────

def test_no_password_appears_in_any_refusal_or_log_line(node, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    said = [_connect(HEVOLVE, password='wrong-SECRET-0')]   # refused by the server
    said.append(_connect(HEVOLVE))
    _mailbox(node.channels, HEVOLVE)
    tools = _tools(monkeypatch, node.store)
    said.append(json.dumps(_send(tools, _prospect(
        tools, 'Salon', 'ravi@salon.example', 'g-lashes'))))     # not connected
    said.append(json.dumps(_send(tools, _prospect(
        tools, 'Shop', 'mo@shop.example', 'g-plain'))))          # names none
    said.append(json.dumps(_send(tools, _prospect(
        tools, 'Clinic', 'ana@clinic.example', 'g-speech'))))    # delivered
    said.append(_connect(MCGROCE))
    _mailbox(node.channels, MCGROCE)

    assert MCGROCE in said[2]                     # the refusal names the address
    text = caplog.text + '\n'.join(said)
    for secret in ('wrong-SECRET-0', PASSWORDS[HEVOLVE], PASSWORDS[MCGROCE]):
        assert secret not in text
