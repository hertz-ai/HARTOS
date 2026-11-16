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
        self.lock = threading.Lock()

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

        def fetch(self, num, parts):
            with world.lock:
                raw = world.inbox[self.host][int(num) - 1][0]
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
            with world.lock:
                world.sent.append({'host': self.host, 'login': self.user,
                                   'from': msg['From'], 'to': msg['To'],
                                   'subject': msg['Subject']})

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
    """create_prospect, in the turn of ``goal_id`` (daemon_<goal id>) when
    given: the turn a goal's agent works in."""
    from core.llm_outbound_logger import request_id_context
    if goal_id:
        with request_id_context(f'daemon_{goal_id}'):
            out = json.loads(tools['create_prospect'](company, 'Contact', address))
    else:
        out = json.loads(tools['create_prospect'](company, 'Contact', address))
    return out['prospect']['id']


def _send(tools, pid):
    return json.loads(tools['send_outreach_email'](
        pid, 'a question about your centre', '<p>hello</p>'))


def _reply(frm, to, subject, body):
    msg = email.mime.text.MIMEText(body)
    msg['From'], msg['To'], msg['Subject'] = frm, to, subject
    msg['Message-ID'] = email.utils.make_msgid()
    return msg.as_bytes()


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
    for pid in (speech, lashes):
        tools['create_followup_sequence'](pid, 'bump', step)

    tick = node.store.check_pending_followups_daemon()

    assert tick['sent'] == 2
    assert [m['to'] for m in node.mail.sent_from(HEVOLVE)] == ['ana@clinic.example']
    assert [m['to'] for m in node.mail.sent_from(MCGROCE)] == ['ravi@salon.example']

    data = node.store._load_prospects()
    first = dict(data['prospects'][lashes], emails_sent=0,
                 email_draft={'subject': 'hello', 'body': '<p>hello</p>'})
    result = JourneyEngine()._action_send_email(first, {})

    assert result.get('sent') is True, result
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
    from integrations.social.api_channels import _wire_live_adapter
    monkeypatch.setenv('EMAIL_SMTP_HOST', 'smtp.desktop.example')
    monkeypatch.setenv('EMAIL_ADDRESS', 'owner@desktop.example')
    monkeypatch.setenv('EMAIL_PASSWORD', PASSWORDS['owner@desktop.example'])
    assert _wire_live_adapter('email', 'imap.desktop.example')['success']
    deadline = time.monotonic() + 10
    while not (node.channels.registry.get('email') or SimpleNamespace(
            is_running=lambda: False)).is_running():
        assert time.monotonic() < deadline, 'the email channel never connected'
        time.sleep(0.05)
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


def test_an_inbound_email_is_answered_from_the_mailbox_it_reached(node):
    """The channel's agent reply goes out from the mailbox the email came
    to, never from the node's other mailbox."""
    boxes = {}
    for address in (HEVOLVE, MCGROCE):
        _connect(address)
        boxes[address] = _mailbox(node.channels, address)
    node.channels.registry.set_agent_handler(lambda message: 'thank you, noted')
    with node.mail.lock:
        node.mail.inbox['imap.mcgroce.example'] = [[_reply(
            'ravi@salon.example', MCGROCE, 'lash prices', 'what do they cost'), False]]

    asyncio.run_coroutine_threadsafe(
        boxes[MCGROCE]._check_new_messages(), node.channels.loop).result(timeout=10)
    deadline = time.monotonic() + 10
    while not node.mail.sent and time.monotonic() < deadline:
        time.sleep(0.05)

    assert [m['to'] for m in node.mail.sent_from(MCGROCE)] == ['ravi@salon.example']
    assert node.mail.sent_from(HEVOLVE) == []


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
