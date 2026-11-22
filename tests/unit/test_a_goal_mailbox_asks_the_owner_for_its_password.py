"""A goal of the node's own whose mailbox is not connected asks the owner for
its password, on the consent card that reaches their signed-in devices.

Owner, 21 Nov 2026 (~23:45): "Why hartos is not asking me on android via
central / The registration@mcgroce.com password / Dont ask me unnecessary
questions if a logic or a way to reach me already exists / Fix the
defects".

Read before this change: an email channel takes four settings (IMAP server,
SMTP server, address, password), so chat_connect_blocker answered "several
credentials" and _goal_mailbox only refused with "connect it in the channel
setup form": nothing ever asked the owner.  The goal's config already names
the address, and the domain's MX names the provider's servers, so only the
password is unknown: the owner is asked for it through post_to_channel's
connect (marketing_tools._connect_or_ask, request_credential, the one
credential ask), and the mailbox is connected with all four.

A server that refuses the password is told from one that does not answer
(FlaskChannelIntegration.probe_login's ``refused``): only a refusal asks the
owner for a new password; a slow server asks nothing and counts nothing.

Review of 3f392fe2f, 22 Nov 2026: "refused" was any connect() that returned
False, and the adapter returns False for a server name that does not resolve,
a refused connection or a timeout too, so a right password on a node whose
network was down read "rejected, enter it again" and used up the owner's
entries; the test helper faked a refusal with that same False.  Now only the
server's own refusal of the login counts (EmailAdapter.login_refused: an IMAP
LOGIN answered NO, an SMTP AUTH answered 535), and the rig's servers refuse a
wrong password the way real ones do.  The servers of an address whose domain
names them (gmail.com) are read with no DNS lookup, since the MX lookup needs
dnspython or dig (Windows has neither unless dnspython is installed).

Real: the outreach send tools, _goal_mailbox, _connect_or_ask,
register_channel, the channel registry and FlaskChannelIntegration on a
running loop, the EmailAdapter, request_credential and ConsentService, the
vault (on temp files), the social DB and the sign-up and goal routes.
Mocked: the mail servers (the rig's imaplib/smtplib), the domain's MX lookup
(mailing_list._mx_host), and the consent card's delivery (consent_service
._emit, the leg that pushes it to the owner's devices).
"""
import asyncio
import json
import socket
from types import SimpleNamespace

import pytest

import tests.unit.test_each_goal_sends_from_its_own_mailbox as rig
from tests.unit.test_each_goal_sends_from_its_own_mailbox import (  # noqa: F401 (fixtures)
    HEVOLVE, MCGROCE, PASSWORDS, channels, mail, mailer, node, web)

SECRET = 'EMAIL_PASSWORD_REGISTRATION_MCGROCE_COM'
IONOS = {'imap_host': 'imap.ionos.com', 'smtp_host': 'smtp.ionos.com'}


@pytest.fixture
def owner_vault(node, monkeypatch, tmp_path):
    """A vault of this test's own, the MX of mcgroce.com at IONOS, and no
    start recorded yet."""
    from hartos import ai_key_vault
    from integrations.channels import agent_tools
    from security import secrets_manager
    from security.secrets_manager import SecretsManager

    monkeypatch.setattr(secrets_manager, '_SALT_PATH', str(tmp_path / 'salt'))
    monkeypatch.setattr(secrets_manager, '_VAULT_PATH', str(tmp_path / 'vault'))
    monkeypatch.setenv('HEVOLVE_MASTER_KEY', 'test-only-vault-passphrase')
    monkeypatch.delenv(SECRET, raising=False)
    SecretsManager.reset()
    ai_key_vault.AIKeyVault.reset()
    monkeypatch.setattr(ai_key_vault, '_instance', None)
    monkeypatch.setattr('integrations.channels.mailing_list._mx_host',
                        lambda domain: ('mx00.ionos.com' if domain == 'mcgroce.com'
                                        else None))
    monkeypatch.setattr(agent_tools, '_STARTS', {})
    yield
    SecretsManager.reset()
    ai_key_vault.AIKeyVault.reset()


@pytest.fixture
def asked(owner_vault, monkeypatch):
    """owner_vault, and every consent card the owner is sent."""
    from integrations.social import consent_service
    cards = []
    monkeypatch.setattr(consent_service, '_emit',
                        lambda topic, data, msg_id=None:
                        cards.append((topic, dict(data), msg_id)))
    return cards


@pytest.fixture
def inline_executor(monkeypatch):
    """The adapter's blocking IMAP and SMTP calls run on the calling thread,
    so an exception they raise reaches the adapter's own except as raised.
    Measured in this test venv (review of 3f392fe2f, and the parent-tree run
    of these tests): an exception raised inside run_in_executor does not come
    back as itself, so a refused login and an unreachable server both left
    the adapter by the same broken path.  The adapter's _connect_imap and
    _test_smtp run unchanged; only where their blocking part runs changes."""
    import asyncio as real
    from integrations.channels.extensions import email_adapter

    class _Loop:
        def __init__(self, loop):
            self._loop = loop

        def run_in_executor(self, executor, fn, *args):
            done = self._loop.create_future()
            try:
                done.set_result(fn(*args))
            except BaseException as e:   # noqa: BLE001 (handed to the awaiter)
                done.set_exception(e)
            return done
    shim = SimpleNamespace(**{k: getattr(real, k) for k in dir(real)
                              if not k.startswith('__')})
    shim.get_event_loop = lambda: _Loop(real.get_event_loop())
    monkeypatch.setattr(email_adapter, 'asyncio', shim)


def _owner(web, monkeypatch):
    owner = web.sign_up('theowner')
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', owner['id'])
    return owner


def _entered(value):
    """The owner typed ``value`` on the card (the value store the card
    writes; the vault answers it as the owner's)."""
    from hartos.ai_key_vault import get_ai_key_vault
    get_ai_key_vault().store_credential(SECRET, value)


def _asks(cards):
    return [data for topic, data, _id in cards
            if topic == 'consent.request' and data.get('scope') == 'secret:' + SECRET]


# ── the settings that are known without asking ──────────────────────

def test_the_servers_of_a_mailbox_come_from_its_domains_mail_host(monkeypatch):
    from integrations.channels.extensions.email_adapter import provider_servers
    hosts = {'mcgroce.com': 'mx00.ionos.com.', 'hevolve.ai': 'aspmx.l.google.com',
             'unknown.example': 'mail.unknown.example'}
    monkeypatch.setattr('integrations.channels.mailing_list._mx_host',
                        lambda domain: hosts.get(domain))

    assert provider_servers(MCGROCE) == IONOS
    assert provider_servers('sathish@hevolve.ai') == {
        'imap_host': 'imap.gmail.com', 'smtp_host': 'smtp.gmail.com'}
    assert provider_servers('a@unknown.example') is None      # not a known provider
    assert provider_servers('a@nomx.example') is None         # no MX
    assert provider_servers('') is None


def test_with_its_address_and_servers_known_a_mailbox_needs_only_its_password():
    from integrations.channels.agent_tools import (
        chat_connect_blocker, owner_credential_name, owner_setting)
    from integrations.channels.metadata import get_channel_metadata
    meta = get_channel_metadata('email')
    known = {'email_address': MCGROCE, **IONOS}

    assert chat_connect_blocker('email', meta, known) is None
    assert chat_connect_blocker('email', meta)[0] is not None   # nothing known
    assert owner_setting('email', meta, MCGROCE) == 'password'
    assert owner_credential_name('email', meta, MCGROCE) == SECRET


# ── the owner is asked, once, on their devices ───────────────────────

def test_the_owners_goal_asks_the_owner_for_its_mailboxs_password(node, web, asked, monkeypatch):
    goal = web.goal(_owner(web, monkeypatch), 'Rice buyers', MCGROCE)

    out = rig._send_in(goal, rig._tools(monkeypatch, node.store), to='buyer@mill.example')

    said = json.dumps(out)
    assert out['success'] is False and out.get('sent') in (False, None)
    assert 'HARTOS asked the owner for the password of ' + MCGROCE in said
    assert 'setup form' not in said
    assert node.mail.sent == [] and node.mailer == []
    [card] = _asks(asked)
    assert card['consent_type'] == 'credential'
    assert MCGROCE in card['reason']                  # the card names the mailbox
    assert PASSWORDS[MCGROCE] not in json.dumps(asked)


def test_asking_again_files_no_second_card(node, web, asked, monkeypatch):
    owner = _owner(web, monkeypatch)
    goal = web.goal(owner, 'Rice buyers', MCGROCE)
    tools = rig._tools(monkeypatch, node.store)

    rig._send_in(goal, tools, to='buyer@mill.example')
    rig._send_in(goal, tools, to='other@mill.example')

    from integrations.social.models import UserConsent, db_session
    with db_session(commit=False) as db:
        rows = db.query(UserConsent).filter_by(
            user_id=owner['id'], consent_type='credential',
            scope='secret:' + SECRET).all()
    assert len(rows) == 1 and rows[0].granted_at is None


def test_once_the_owner_enters_it_the_mailbox_connects_and_the_goal_sends_from_it(node, web, asked, monkeypatch):
    goal = web.goal(_owner(web, monkeypatch), 'Rice buyers', MCGROCE)
    tools = rig._tools(monkeypatch, node.store)
    _entered(PASSWORDS[MCGROCE])

    first = rig._send_in(goal, tools, to='buyer@mill.example')
    assert first['success'] is False
    assert 'is connecting now with the password the owner entered' in json.dumps(first)
    adapter = rig._mailbox(node.channels, MCGROCE)
    assert adapter.email_config.imap_host == 'imap.ionos.com'
    assert adapter.email_config.smtp_host == 'smtp.ionos.com'

    out = rig._send_in(goal, tools, to='buyer@mill.example')

    assert out['success'] is True, out
    assert [m['to'] for m in node.mail.sent_from(MCGROCE)] == ['buyer@mill.example']
    assert node.mail.sent[0]['host'] == 'smtp.ionos.com'
    assert _asks(asked) == []                         # nothing was asked


def test_a_password_the_server_refuses_asks_the_owner_for_another(node, web, asked, monkeypatch, inline_executor):
    from integrations.channels.agent_tools import START_FAILED, start_verdict
    goal = web.goal(_owner(web, monkeypatch), 'Rice buyers', MCGROCE)
    _entered('a-mistyped-password')

    out = rig._send_in(goal, rig._tools(monkeypatch, node.store), to='buyer@mill.example')

    said = json.dumps(out)
    assert out['success'] is False
    assert 'refused the password the owner entered' in said
    assert 'asked the owner for the password of ' + MCGROCE + ' again' in said
    assert start_verdict('email', MCGROCE)['state'] == START_FAILED
    assert len(_asks(asked)) == 1                     # asked again
    assert node.channels.registry.accounts('email') == {}
    assert node.mail.sent == [] and node.mailer == []


def test_a_server_that_does_not_answer_asks_nothing_and_counts_nothing(node, web, asked, monkeypatch):
    from integrations.channels import flask_integration
    from integrations.channels.agent_tools import START_FAILED, start_verdict
    from integrations.channels.extensions import email_adapter

    async def never_answers(self):
        await asyncio.sleep(60)
        return True
    monkeypatch.setattr(email_adapter.EmailAdapter, '_connect_imap', never_answers)
    monkeypatch.setattr(flask_integration, 'ACCOUNT_LOGIN_WAIT_S', 1)
    goal = web.goal(_owner(web, monkeypatch), 'Rice buyers', MCGROCE)
    _entered(PASSWORDS[MCGROCE])

    out = rig._send_in(goal, rig._tools(monkeypatch, node.store), to='buyer@mill.example')

    said = json.dumps(out)
    assert out['success'] is False
    assert 'did not answer within 1 seconds' in said
    assert 'the owner was not asked again' in said
    assert (start_verdict('email', MCGROCE) or {}).get('state') != START_FAILED
    assert _asks(asked) == []
    assert node.mail.sent == [] and node.mailer == []


# ── who is never asked for ───────────────────────────────────────────

def test_a_strangers_goal_asks_the_owner_nothing(node, web, asked, monkeypatch):
    _owner(web, monkeypatch)
    goal = web.goal(web.sign_up('stranger'), 'Spring watch sale', MCGROCE)

    out = rig._send_in(goal, rig._tools(monkeypatch, node.store))

    assert out['success'] is False
    assert 'setup form' in json.dumps(out)
    assert _asks(asked) == [] and node.mail.sent == []


def test_a_goal_with_no_creator_on_this_node_asks_nothing(node, asked, monkeypatch):
    """The rig's goals are made by 'owner-ee', an id with no account here:
    not this node's own, so nothing is asked."""
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', rig.OWNER)
    tools = rig._tools(monkeypatch, node.store)
    pid = rig._prospect(tools, 'A lash studio', 'studio@lash.example', 'g-lashes')

    out = rig._send(tools, pid)

    assert out['success'] is False
    assert 'setup form' in json.dumps(out)
    assert _asks(asked) == [] and node.mail.sent == []


def test_a_strangers_refused_connect_never_marks_the_owners_password_failed(node, web, asked, monkeypatch, inline_executor):
    """Another account's connect of the same mailbox with a wrong password is
    refused by the server; the owner's own password is still tried."""
    from integrations.channels.agent_tools import START_FAILED, start_verdict
    owner = _owner(web, monkeypatch)
    _entered(PASSWORDS[MCGROCE])
    web.connect(web.sign_up('stranger'), MCGROCE, password='a-guess')
    assert (start_verdict('email', MCGROCE) or {}).get('state') != START_FAILED

    goal = web.goal(owner, 'Rice buyers', MCGROCE)
    first = rig._send_in(goal, rig._tools(monkeypatch, node.store), to='buyer@mill.example')

    assert 'is connecting now' in json.dumps(first)
    rig._mailbox(node.channels, MCGROCE)
    assert _asks(asked) == []


# ── a refusal is the server's no to the login, nothing else ───────────

def _probe(monkeypatch, imap=None, smtp=None):
    """probe_login of the mcgroce mailbox with its right password, on the
    rig's servers, with the IMAP or SMTP server replaced by ``imap`` /
    ``smtp`` (a class standing in for imaplib.IMAP4_SSL / smtplib.SMTP)."""
    from integrations.channels.extensions import email_adapter
    from integrations.channels.flask_integration import get_channel_integration
    if imap is not None:
        monkeypatch.setattr(email_adapter.imaplib, 'IMAP4_SSL', imap)
    if smtp is not None:
        monkeypatch.setattr(email_adapter.smtplib, 'SMTP', smtp)
    # The email channel's "token" is its IMAP server (the setup form's first
    # field); the password is a setting like the others.
    return get_channel_integration().probe_login(
        'email', IONOS['imap_host'], smtp_host=IONOS['smtp_host'],
        email_address=MCGROCE, password=PASSWORDS[MCGROCE], wait_s=10)


def _server_raising(error, at_login=True):
    """A mail server (IMAP or SMTP) that raises ``error`` when it is reached
    (``at_login`` False: the name does not resolve, the connection is
    refused, it times out) or when the login is tried."""
    class Server:
        def __init__(self, *a, **k):
            if not at_login:
                raise error

        def starttls(self, *a, **k):
            return 220, b'ready'

        def login(self, user, password):
            raise error

        def logout(self):
            return 'BYE', [b'']

        def quit(self):
            return 221, b'bye'
    return Server


def _refusals():
    import imaplib
    import smtplib
    return {
        'imap-authenticationfailed': dict(imap=_server_raising(imaplib.IMAP4.error(
            b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'))),
        'imap-login-failed': dict(imap=_server_raising(imaplib.IMAP4.error(
            b'LOGIN failed.'))),
        'smtp-535': dict(smtp=_server_raising(smtplib.SMTPAuthenticationError(
            535, b'5.7.8 Username and Password not accepted'))),
    }


def _unreachable():
    import imaplib
    return {
        'dns': dict(imap=_server_raising(
            socket.gaierror(11001, 'getaddrinfo failed'), at_login=False)),
        'connection-refused': dict(imap=_server_raising(
            ConnectionRefusedError(10061, 'refused'), at_login=False)),
        'timeout': dict(imap=_server_raising(TimeoutError('timed out'),
                                             at_login=False)),
        'imap-unavailable': dict(imap=_server_raising(imaplib.IMAP4.error(
            b'[UNAVAILABLE] Temporary System Problem.  Try again later'))),
        'imap-abort': dict(imap=_server_raising(imaplib.IMAP4.abort(
            'socket error: EOF'))),
        'smtp-dns': dict(smtp=_server_raising(
            socket.gaierror(11001, 'getaddrinfo failed'), at_login=False)),
    }


@pytest.mark.parametrize('case', [
    'imap-authenticationfailed', 'imap-login-failed', 'smtp-535'])
def test_a_login_the_server_refuses_is_refused(channels, mail, monkeypatch, inline_executor, case):
    ok, why, refused = _probe(monkeypatch, **_refusals()[case])

    assert (ok, refused) == (False, True)
    assert 'refused the login' in why


@pytest.mark.parametrize('case', [
    'dns', 'connection-refused', 'timeout', 'imap-unavailable', 'imap-abort',
    'smtp-dns'])
def test_a_server_that_cannot_be_reached_is_not_a_refused_password(channels, mail, monkeypatch, inline_executor, case):
    ok, why, refused = _probe(monkeypatch, **_unreachable()[case])

    assert (ok, refused) == (False, False)
    assert 'refused' not in why


def test_a_right_password_on_a_node_that_cannot_resolve_its_server_is_not_asked_again(node, web, asked, monkeypatch, inline_executor):
    """The owner's right password, a mail server that does not resolve (DNS
    down, an offline node): no "rejected" card, no failed start, and the
    tool says the password was not refused."""
    from integrations.channels.agent_tools import START_FAILED, start_verdict
    from integrations.channels.extensions import email_adapter
    from tests.unit.test_a_phone_credential_answer_is_stored_before_its_grant import (
        _all_records)
    monkeypatch.setattr(email_adapter.imaplib, 'IMAP4_SSL', _server_raising(
        socket.gaierror(11001, 'getaddrinfo failed'), at_login=False))
    goal = web.goal(_owner(web, monkeypatch), 'Rice buyers', MCGROCE)
    _entered(PASSWORDS[MCGROCE])
    records = _all_records(monkeypatch)

    out = rig._send_in(goal, rig._tools(monkeypatch, node.store), to='buyer@mill.example')

    said = json.dumps(out)
    assert out['success'] is False
    assert 'refused the password' not in said
    assert 'the owner was not asked again' in said
    assert (start_verdict('email', MCGROCE) or {}).get('state') != START_FAILED
    assert _asks(asked) == []                      # no "rejected" card
    assert node.mail.sent == [] and node.mailer == []
    assert records.text and sum(PASSWORDS[MCGROCE] in t for t in records.text) == 0


# ── which servers are known without asking ────────────────────────────

def test_an_address_whose_domain_names_its_servers_needs_no_dns_lookup():
    """Unmocked: the address's own domain names the servers, so this holds
    on a node with no MX resolver (no dnspython, no dig) and no network."""
    from integrations.channels.extensions.email_adapter import provider_servers
    gmail = {'imap_host': 'imap.gmail.com', 'smtp_host': 'smtp.gmail.com'}

    assert provider_servers('someone@gmail.com') == gmail
    assert provider_servers('Someone@GoogleMail.com') == gmail
    assert provider_servers('someone@zoho.com') == {
        'imap_host': 'imap.zoho.com', 'smtp_host': 'smtp.zoho.com'}


def test_a_mail_host_a_password_cannot_log_in_to_or_that_is_ambiguous_is_not_guessed(monkeypatch):
    """Microsoft 365 takes no password for IMAP; a custom domain at Zoho logs
    in at imap or imappro depending on its plan, which its MX does not say."""
    from integrations.channels.extensions.email_adapter import provider_servers
    hosts = {'m365.example': 'm365-example.mail.protection.outlook.com',
             'zorg.example': 'mx.zoho.com', 'zin.example': 'mx.zoho.in'}
    monkeypatch.setattr('integrations.channels.mailing_list._mx_host',
                        lambda domain: hosts.get(domain))

    for address in ('a@m365.example', 'a@zorg.example', 'a@zin.example'):
        assert provider_servers(address) is None, address


def test_the_first_card_for_a_google_mailbox_says_to_enter_an_app_password(node, web, asked, monkeypatch):
    monkeypatch.setattr('integrations.channels.mailing_list._mx_host',
                        lambda domain: ('aspmx.l.google.com' if domain == 'hevolve.ai'
                                        else 'mx00.ionos.com'))
    owner = _owner(web, monkeypatch)
    tools = rig._tools(monkeypatch, node.store)

    rig._send_in(web.goal(owner, 'Speech centres', HEVOLVE), tools, to='a@clinic.example')
    rig._send_in(web.goal(owner, 'Rice buyers', MCGROCE), tools, to='b@mill.example')

    cards = {d['scope']: d['reason'] for t, d, _m in asked
             if t == 'consent.request' and d.get('consent_type') == 'credential'}
    # Every email card's label already reads "Password / App Password"
    # (the channel's setup field); the hint is what tells a Google mailbox
    # which one, and only a Google mailbox.
    hint = 'not the password you sign in with'
    google = [r for s, r in cards.items() if s != 'secret:' + SECRET]
    assert len(google) == 1 and hint in google[0]
    assert hint not in cards['secret:' + SECRET]     # IONOS


# ── the ask reaches the owner's Nunba clients ─────────────────────────

class _Now:
    """The push leg runs on the caller's thread here."""

    def __init__(self, target=None, args=(), **kw):
        self._run = lambda: target(*args)

    def start(self):
        self._run()


def test_the_mailbox_ask_leaves_for_the_desktop_and_the_phone_on_the_credential_card(node, web, owner_vault, monkeypatch):
    """Owner ruling 2026-11-22: "the sending mailbox should be asked via nunba
    clients."  Read where each leg leaves this node: the person's WAMP topic
    and SSE (the desktop's AgentOverlay shows a consent.request with
    consent_type 'credential' as the password card) and the Firebase push
    (the phone's overlay reads consent_prompt, request_id and
    consent_type)."""
    from core import fcm_sync
    from core.platform import events
    from integrations.social import realtime
    wamp, sse, push = [], [], []
    monkeypatch.setattr(realtime, 'publish_event',
                        lambda topic, data, user_id='': wamp.append(
                            (topic, dict(data), user_id)))
    monkeypatch.setattr(events, 'broadcast_sse_safe',
                        lambda kind, data, user_id=None: sse.append(
                            (kind, dict(data), user_id)) or True)
    monkeypatch.setattr(fcm_sync, 'send_fcm_push',
                        lambda uid, title, body, data=None, **k: push.append(
                            (uid, title, body, dict(data or {}))) or True)
    monkeypatch.setattr(realtime, '_pushes_allowed', lambda: True)
    monkeypatch.setattr(realtime, 'threading', SimpleNamespace(Thread=_Now))
    owner = _owner(web, monkeypatch)

    out = rig._send_in(web.goal(owner, 'Rice buyers', MCGROCE),
                       rig._tools(monkeypatch, node.store), to='buyer@mill.example')

    assert 'HARTOS asked the owner for the password of ' + MCGROCE in json.dumps(out)
    scope = 'secret:' + SECRET

    def card(d):
        return (d.get('type') == 'consent.request' and d.get('scope') == scope
                and d.get('consent_type') == 'credential')
    [desktop_wamp] = [d for topic, d, uid in wamp
                      if topic == 'chat.social' and uid == owner['id'] and card(d)]
    [desktop_sse] = [d for kind, d, uid in sse
                     if kind == 'notification' and uid == owner['id'] and card(d)]
    row = desktop_sse['msg_id'].split(':', 1)[1]
    assert desktop_sse['msg_id'] == desktop_wamp['msg_id'] == 'consent.request:' + row
    assert MCGROCE in desktop_sse['reason']
    [(uid, title, body, data)] = [p for p in push
                                  if p[3].get('type') == 'consent_prompt']
    assert uid == owner['id'] and title and body
    assert data['request_id'] == row and data['consent_type'] == 'credential'
    assert PASSWORDS[MCGROCE] not in json.dumps([wamp, sse, push])
