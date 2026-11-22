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
server's own refusal of the login counts (EmailAdapter.login_refused), and
the rig's servers refuse a wrong password the way real ones do.  The servers
of an address whose domain names them (gmail.com) are read with no DNS
lookup, since the MX lookup needs dnspython or dig (Windows has neither
unless dnspython is installed).

Review of d072f8ea6, 22 Nov 2026 (rv_d072): smtplib raises
SMTPAuthenticationError for EVERY reply to AUTH but 235 and 503, so an SMTP
454 (try later, too many attempts) or 421 read as a refused password, and an
IMAP NO with [LIMIT] or [ALERT] did too: a right password got a "rejected"
card and used up an entry.  Only a refusal a new password can fix counts
now: SMTP 535 or 534, an IMAP NO with no response code or with an RFC 5530
credential code (AUTHENTICATIONFAILED, AUTHORIZATIONFAILED, EXPIRED); any
other answer is not refused, and the server's own words reach the reply.
The hand-built exceptions are how 454 slipped through, so those cases are
read from real imaplib and smtplib talking to scripted servers on a
loopback socket.  A mail-server lookup that fails, and a provider whose
servers are not known, were silent: one log line names it now, and the
turn says why the setup form is needed.

Real: the outreach send tools, _goal_mailbox, _connect_or_ask,
register_channel, the channel registry and FlaskChannelIntegration on a
running loop, the EmailAdapter, imaplib and smtplib (on loopback servers),
request_credential and ConsentService, the vault (on temp files), the social
DB and the sign-up and goal routes.  Mocked: the mail servers (the rig's
imaplib/smtplib, or scripted servers on 127.0.0.1), the DNS the MX lookup
asks (a dns.resolver module of the test's own, and no dig binary), and the
consent card's delivery (consent_service._emit, the leg that pushes it to
the owner's devices).
"""
import asyncio
import contextlib
import imaplib
import json
import smtplib
import socket
import sys
import threading
import types
from types import SimpleNamespace

import pytest

import tests.unit.test_each_goal_sends_from_its_own_mailbox as rig
from tests.unit.test_each_goal_sends_from_its_own_mailbox import (  # noqa: F401 (fixtures)
    HEVOLVE, MCGROCE, PASSWORDS, channels, mail, mailer, node, web)

SECRET = 'EMAIL_PASSWORD_REGISTRATION_MCGROCE_COM'
IONOS = {'imap_host': 'imap.ionos.com', 'smtp_host': 'smtp.ionos.com'}
#: A goal mailbox at Google, whose servers its domain names (no DNS lookup),
#: and the password the owner entered for it (any value: the loopback servers
#: answer the login the way their script says, whatever the password).
GMAIL_BOX = 'outreach-test@gmail.com'
BOX_PASSWORD = 'pw-gmailbox-SECRET-4'


def _dns(monkeypatch, hosts):
    """The DNS the domain's MX lookup asks (dnspython's dns.resolver, the
    boundary): ``hosts`` maps a domain to its MX host; any other domain does
    not exist (NXDOMAIN).  No dig binary either, so nothing reaches a real
    DNS server."""
    class NXDOMAIN(Exception):
        pass

    def resolve(domain, rdtype, *a, **k):
        if rdtype != 'MX' or domain not in hosts:
            raise NXDOMAIN(f'The DNS query name does not exist: {domain}.')
        return [SimpleNamespace(preference=10, exchange=hosts[domain] + '.')]
    resolver = types.ModuleType('dns.resolver')
    resolver.resolve, resolver.NXDOMAIN = resolve, NXDOMAIN
    package = types.ModuleType('dns')
    package.resolver = resolver
    monkeypatch.setitem(sys.modules, 'dns', package)
    monkeypatch.setitem(sys.modules, 'dns.resolver', resolver)
    _no_dig(monkeypatch)


def _no_dig(monkeypatch):
    """No dig binary on this node (Windows has none)."""
    from integrations.channels import mailing_list

    def run(argv, *a, **k):
        raise FileNotFoundError(2, 'No such file or directory', argv[0])
    monkeypatch.setattr(mailing_list, 'subprocess', SimpleNamespace(run=run))


def _no_resolver(monkeypatch):
    """Neither dnspython nor dig on this node: no MX lookup can run."""
    monkeypatch.setitem(sys.modules, 'dns', None)
    monkeypatch.setitem(sys.modules, 'dns.resolver', None)
    _no_dig(monkeypatch)


@pytest.fixture
def vault(node, monkeypatch, tmp_path):
    """A vault of this test's own, and no start recorded yet."""
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
    monkeypatch.setattr(agent_tools, '_STARTS', {})
    yield
    SecretsManager.reset()
    ai_key_vault.AIKeyVault.reset()


@pytest.fixture
def owner_vault(vault, monkeypatch):
    """vault, and the MX of mcgroce.com at IONOS."""
    _dns(monkeypatch, {'mcgroce.com': 'mx00.ionos.com'})


@pytest.fixture
def cards(vault, monkeypatch):
    """Every consent card the owner is sent."""
    from integrations.social import consent_service
    sent = []
    monkeypatch.setattr(consent_service, '_emit',
                        lambda topic, data, msg_id=None:
                        sent.append((topic, dict(data), msg_id)))
    return sent


@pytest.fixture
def asked(owner_vault, cards):
    """owner_vault, and every consent card the owner is sent."""
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


def _secret(address=MCGROCE):
    """The vault name the owner's password for ``address`` goes by."""
    from integrations.channels.agent_tools import owner_credential_name
    from integrations.channels.metadata import get_channel_metadata
    return owner_credential_name('email', get_channel_metadata('email'), address)


def _entered(value, address=MCGROCE):
    """The owner typed ``value`` on the card of ``address`` (the value store
    the card writes; the vault answers it as the owner's)."""
    from hartos.ai_key_vault import get_ai_key_vault
    get_ai_key_vault().store_credential(_secret(address), value)


def _asks(cards, address=MCGROCE):
    return [data for topic, data, _id in cards
            if topic == 'consent.request'
            and data.get('scope') == 'secret:' + _secret(address)]


# ── the settings that are known without asking ──────────────────────

def test_the_servers_of_a_mailbox_come_from_its_domains_mail_host(monkeypatch):
    from integrations.channels.extensions.email_adapter import provider_servers
    _dns(monkeypatch, {'mcgroce.com': 'mx00.ionos.com', 'hevolve.ai': 'aspmx.l.google.com',
                       'unknown.example': 'mail.unknown.example'})

    assert provider_servers(MCGROCE) == (IONOS, '')
    assert provider_servers('sathish@hevolve.ai') == ({
        'imap_host': 'imap.gmail.com', 'smtp_host': 'smtp.gmail.com'}, '')
    servers, why = provider_servers('a@unknown.example')      # not a known provider
    assert servers is None and 'mail.unknown.example' in why
    servers, why = provider_servers('a@nomx.example')         # no MX
    assert servers is None and 'nomx.example' in why and 'does not exist' in why
    assert provider_servers('')[0] is None


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
    # The claim is never made; the error's own words (a connection
    # "refused") may follow it.
    assert 'refused the login' not in why


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

    assert provider_servers('someone@gmail.com') == (gmail, '')
    assert provider_servers('Someone@GoogleMail.com') == (gmail, '')
    assert provider_servers('someone@zoho.com') == ({
        'imap_host': 'imap.zoho.com', 'smtp_host': 'smtp.zoho.com'}, '')


def test_a_mail_host_a_password_cannot_log_in_to_or_that_is_ambiguous_is_not_guessed(monkeypatch):
    """Microsoft 365 takes no password for IMAP; a custom domain at Zoho logs
    in at imap or imappro depending on its plan, which its MX does not say."""
    from integrations.channels.extensions.email_adapter import provider_servers
    hosts = {'m365.example': 'm365-example.mail.protection.outlook.com',
             'zorg.example': 'mx.zoho.com', 'zin.example': 'mx.zoho.in'}
    _dns(monkeypatch, hosts)

    for address in ('a@m365.example', 'a@zorg.example', 'a@zin.example'):
        servers, why = provider_servers(address)
        assert servers is None, address
        assert hosts[address.split('@')[1]] in why, why


def test_the_first_card_for_a_google_mailbox_says_to_enter_an_app_password(node, web, asked, monkeypatch):
    _dns(monkeypatch, {'hevolve.ai': 'aspmx.l.google.com', 'mcgroce.com': 'mx00.ionos.com'})
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


# ── only a refusal a new password can fix asks for one (rv_d072) ──────

# The rig's `mail` fixture replaces IMAP4_SSL and SMTP; these are the real ones.
_REAL_IMAP4 = imaplib.IMAP4
_REAL_SMTP = smtplib.SMTP


class _LoopbackServer:
    """One scripted TCP server on 127.0.0.1: ``script(conn, lines)`` serves
    each client.  Real imaplib and smtplib talk to it, so a reply is parsed
    and raised exactly as from a mail server; only TLS is left out."""

    def __init__(self, script):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.script = script
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            self.script(conn, conn.makefile('rb'))
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.sock.close()


def _imap_script(login_reply):
    """An IMAP server that answers LOGIN with ``login_reply`` after the tag."""
    def script(conn, lines):
        conn.sendall(b'* OK [CAPABILITY IMAP4rev1 AUTH=PLAIN] ready\r\n')
        for line in iter(lines.readline, b''):
            tag, _sp, rest = line.decode('latin-1').partition(' ')
            command = rest.split(' ', 1)[0].strip().upper()
            if command == 'CAPABILITY':
                conn.sendall(b'* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n'
                             + tag.encode() + b' OK done\r\n')
            elif command == 'LOGIN':
                conn.sendall(tag.encode() + b' ' + login_reply + b'\r\n')
            elif command == 'LOGOUT':
                conn.sendall(b'* BYE bye\r\n' + tag.encode() + b' OK bye\r\n')
                return
            else:
                conn.sendall(tag.encode() + b' OK done\r\n')
    return script


def _smtp_script(auth_reply, mechanisms=b'PLAIN LOGIN', close_after_auth=False):
    """An SMTP server that answers AUTH with ``auth_reply`` (and hangs up
    after it when ``close_after_auth``)."""
    def script(conn, lines):
        conn.sendall(b'220 fake ESMTP\r\n')
        for line in iter(lines.readline, b''):
            command = line.decode('latin-1').strip().split(' ', 1)[0].upper()
            if command in ('EHLO', 'HELO'):
                conn.sendall(b'250-fake\r\n250 AUTH ' + mechanisms + b'\r\n')
            elif command == 'AUTH':
                conn.sendall(auth_reply + b'\r\n')
                if close_after_auth:
                    return
            elif command == 'QUIT':
                conn.sendall(b'221 bye\r\n')
                return
            else:
                conn.sendall(b'250 ok\r\n')
    return script


@contextlib.contextmanager
def _mail_servers(monkeypatch, imap_reply=b'OK logged in',
                  smtp=(b'235 2.7.0 Accepted',)):
    """The adapter's IMAP and SMTP servers, whatever host it names, are
    loopback servers answering the login with ``imap_reply`` / ``smtp``
    (_smtp_script's arguments)."""
    from integrations.channels.extensions import email_adapter
    imap_server = _LoopbackServer(_imap_script(imap_reply))
    smtp_server = _LoopbackServer(_smtp_script(*smtp))

    class IMAP(_REAL_IMAP4):
        def __init__(self, host, port=993, ssl_context=None):
            super().__init__('127.0.0.1', imap_server.port, timeout=5)

    class SMTP(_REAL_SMTP):
        def __init__(self, host, port=587, *a, **k):
            super().__init__('127.0.0.1', smtp_server.port, timeout=5)

        def starttls(self, *a, **k):          # only TLS is left out
            return 220, b'ready'
    monkeypatch.setattr(email_adapter.imaplib, 'IMAP4_SSL', IMAP)
    monkeypatch.setattr(email_adapter.smtplib, 'SMTP', SMTP)
    try:
        yield
    finally:
        imap_server.close()
        smtp_server.close()


#: case -> (the server that answers, its answer to the login, refused?).
#: SMTP's answer: (_smtp_script's auth reply, mechanisms, hangs up after it).
_REPLIES = {
    'smtp-535': ('smtp', (b'535 5.7.8 Username and Password not accepted', b'PLAIN LOGIN', False), True),
    'smtp-534': ('smtp', (b'534 5.7.9 Application-specific password required', b'PLAIN LOGIN', False), True),
    'smtp-454': ('smtp', (b'454 4.7.0 Temporary authentication failure', b'PLAIN LOGIN', False), False),
    'smtp-454-plain-only': ('smtp', (b'454 4.7.0 Too many login attempts, try later', b'PLAIN', False), False),
    'smtp-421': ('smtp', (b'421 4.3.2 System not accepting network messages', b'PLAIN', False), False),
    'smtp-421-closing': ('smtp', (b'421 4.7.0 Service not available, closing', b'PLAIN', True), False),
    'imap-no-plain': ('imap', b'NO LOGIN failed.', True),
    'imap-authenticationfailed': ('imap', b'NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)', True),
    'imap-authorizationfailed': ('imap', b'NO [AUTHORIZATIONFAILED] No such authorization-ID', True),
    'imap-expired': ('imap', b"NO [EXPIRED] That password isn't valid any more", True),
    'imap-limit': ('imap', b'NO [LIMIT] Too many simultaneous connections', False),
    'imap-contactadmin': ('imap', b'NO [CONTACTADMIN] Account locked, contact your administrator', False),
    'imap-privacyrequired': ('imap', b'NO [PRIVACYREQUIRED] Connection is not private', False),
    'imap-unavailable': ('imap', b'NO [UNAVAILABLE] Temporary System Problem.  Try again later', False),
    'imap-alert-imap-off': ('imap', b'NO [ALERT] Your account is not enabled for IMAP use.', False),
    'imap-alert-web-login': ('imap', b'NO [ALERT] Please log in via your web browser', False),
    'imap-bad': ('imap', b'BAD Command syntax error', False),
}


def _servers(case):
    """_mail_servers' arguments for ``case``."""
    side, answer, _refused = _REPLIES[case]
    return {'imap_reply': answer} if side == 'imap' else {'smtp': answer}


def _server_said(case):
    """The server's own words in ``case``'s answer: what the owner is told
    (a run of spaces or line breaks reads as one space in a reply)."""
    side, answer, _refused = _REPLIES[case]
    words = answer[0].decode() if side == 'smtp' else answer.decode().split(' ', 1)[1]
    return ' '.join(words.split())


def _socket_probe(monkeypatch, case):
    from integrations.channels.flask_integration import get_channel_integration
    with _mail_servers(monkeypatch, **_servers(case)):
        return get_channel_integration().probe_login(
            'email', IONOS['imap_host'], smtp_host=IONOS['smtp_host'],
            email_address=MCGROCE, password=PASSWORDS[MCGROCE], wait_s=10)


@pytest.mark.parametrize('case', [
    'smtp-535', 'smtp-534', 'smtp-454', 'smtp-454-plain-only', 'smtp-421',
    'smtp-421-closing'])
def test_an_smtp_auth_reply_refuses_the_password_only_when_it_is_535_or_534(channels, mail, monkeypatch, inline_executor, case):
    """smtplib raises SMTPAuthenticationError for every reply to AUTH but
    235 and 503, so a 454 (try later, too many attempts) or a 421 read as a
    refused password (rv_d072, measured).  Only 535 and 534 refuse it; any
    reply's own words reach the reply."""
    ok, why, refused = _socket_probe(monkeypatch, case)

    assert (ok, refused) == (False, _REPLIES[case][2])
    assert ('refused the login' in why) is refused
    assert _server_said(case) in why
    assert PASSWORDS[MCGROCE] not in why


@pytest.mark.parametrize('case', [
    'imap-no-plain', 'imap-authenticationfailed', 'imap-authorizationfailed',
    'imap-expired', 'imap-limit', 'imap-contactadmin', 'imap-privacyrequired',
    'imap-unavailable', 'imap-alert-imap-off', 'imap-alert-web-login',
    'imap-bad'])
def test_an_imap_no_refuses_the_password_only_with_no_code_or_a_credential_code(channels, mail, monkeypatch, inline_executor, case):
    """RFC 5530: a NO to LOGIN with no response code (RFC 3501's "user name
    or password rejected") or with AUTHENTICATIONFAILED, AUTHORIZATIONFAILED
    or EXPIRED refuses the credentials.  Any other code (LIMIT, CONTACTADMIN,
    PRIVACYREQUIRED, UNAVAILABLE, ...) is something a new password cannot
    fix, and so is [ALERT]: it carries no cause a client can read, only words
    for the person ("not enabled for IMAP", "log in via your web browser"),
    and those words reach the reply."""
    ok, why, refused = _socket_probe(monkeypatch, case)

    assert (ok, refused) == (False, _REPLIES[case][2])
    assert ('refused the login' in why) is refused
    assert _server_said(case) in why
    assert PASSWORDS[MCGROCE] not in why


@pytest.mark.parametrize('case', [
    'smtp-454', 'smtp-421', 'imap-limit', 'imap-alert-imap-off'])
def test_a_right_password_the_server_cannot_take_now_files_no_card(node, web, vault, cards, monkeypatch, inline_executor, case):
    """The owner's password and a server that answers the login with what a
    new password cannot fix: no "rejected" card, no failed start (no entry
    used up), and the turn carries the server's words so the owner learns
    what to do (try later, enable IMAP)."""
    from integrations.channels.agent_tools import START_FAILED, start_verdict
    from tests.unit.test_a_phone_credential_answer_is_stored_before_its_grant import (
        _all_records)
    records = _all_records(monkeypatch)
    goal = web.goal(_owner(web, monkeypatch), 'Rice buyers', GMAIL_BOX)
    _entered(BOX_PASSWORD, GMAIL_BOX)

    with _mail_servers(monkeypatch, **_servers(case)):
        out = rig._send_in(goal, rig._tools(monkeypatch, node.store), to='buyer@mill.example')

    said = json.dumps(out)
    assert out['success'] is False
    assert 'refused the password' not in said
    assert 'the owner was not asked again' in said
    assert _server_said(case) in said
    assert (start_verdict('email', GMAIL_BOX) or {}).get('state') != START_FAILED
    assert _asks(cards, GMAIL_BOX) == []
    assert BOX_PASSWORD not in json.dumps([out, cards])
    assert records.text and sum(BOX_PASSWORD in t for t in records.text) == 0


@pytest.mark.parametrize('case', [
    'imap-no-plain', 'imap-authenticationfailed', 'smtp-535', 'smtp-534'])
def test_a_password_the_server_refuses_files_exactly_one_card(node, web, vault, cards, monkeypatch, inline_executor, case):
    """A refusal a new password can fix still asks the owner again, once."""
    from integrations.channels.agent_tools import START_FAILED, start_verdict
    from tests.unit.test_a_phone_credential_answer_is_stored_before_its_grant import (
        _all_records)
    records = _all_records(monkeypatch)
    goal = web.goal(_owner(web, monkeypatch), 'Rice buyers', GMAIL_BOX)
    _entered(BOX_PASSWORD, GMAIL_BOX)

    with _mail_servers(monkeypatch, **_servers(case)):
        out = rig._send_in(goal, rig._tools(monkeypatch, node.store), to='buyer@mill.example')

    said = json.dumps(out)
    assert out['success'] is False
    assert 'refused the password the owner entered' in said
    assert start_verdict('email', GMAIL_BOX)['state'] == START_FAILED
    assert len(_asks(cards, GMAIL_BOX)) == 1
    assert BOX_PASSWORD not in json.dumps([out, cards])
    assert records.text and sum(BOX_PASSWORD in t for t in records.text) == 0


@pytest.mark.parametrize('case', ['imap-authenticationfailed', 'imap-limit'])
def test_only_a_refusal_says_to_check_the_password_and_the_advice_has_one_wording(node, vault, monkeypatch, inline_executor, case):
    """A login the server could not complete said "Check the address, the
    password (Gmail ... App Password) and both server names" too, and the App
    Password advice was written twice (rv_d072).  A refusal says to check the
    address and the password, with the provider's advice from the one place
    the password card reads it (password_hint); anything else names no
    password."""
    from integrations.channels.agent_tools import build_channel_tool_closures
    from integrations.channels.extensions.email_adapter import password_hint
    register = next(t[2] for t in build_channel_tool_closures(
        {'user_id': rig.OWNER, 'prompt_id': None}) if t[0] == 'register_channel')
    hint = password_hint({'imap_host': 'imap.gmail.com'})

    with _mail_servers(monkeypatch, **_servers(case)):
        said = register('email', json.dumps({
            'imap_host': 'imap.gmail.com', 'smtp_host': 'smtp.gmail.com',
            'email_address': GMAIL_BOX, 'password': BOX_PASSWORD}))

    assert hint and 'was not connected' in said and BOX_PASSWORD not in said
    if _REPLIES[case][2]:
        assert 'Check the address and the password' in said and hint in said
    else:
        assert 'password' not in said.lower()
        assert 'both server names' in said


@pytest.mark.parametrize('case', ['imap-authenticationfailed', 'imap-limit'])
def test_an_aioimaplib_no_to_login_is_read_by_the_same_rule(monkeypatch, case):
    """aioimaplib answers LOGIN with the server's response instead of
    raising, so a refused login looked connected.  Not taken today (a goal
    mailbox leaves its mail unread and reads through imaplib, and the package
    is not installed here), but the rule is the same one."""
    from integrations.channels.extensions import email_adapter
    result, _sp, text = _REPLIES[case][1].partition(b' ')

    class FakeIMAP:
        def __init__(self, host, port=993):
            pass

        async def wait_hello_from_server(self):
            return None

        async def login(self, user, password):
            return SimpleNamespace(result=result.decode(), lines=[text])

        async def logout(self):
            return None

    class FakeSMTP:
        def __init__(self, **kwargs):
            pass

        async def connect(self):
            return None

        async def login(self, user, password):
            return None

        async def quit(self):
            return None
    monkeypatch.setattr(email_adapter, 'HAS_AIOIMAP', True)
    monkeypatch.setattr(email_adapter, 'HAS_AIOSMTP', True)
    monkeypatch.setattr(email_adapter, 'aioimaplib',
                        SimpleNamespace(IMAP4_SSL=FakeIMAP, IMAP4=FakeIMAP), raising=False)
    monkeypatch.setattr(email_adapter, 'aiosmtplib', SimpleNamespace(SMTP=FakeSMTP),
                        raising=False)
    adapter = email_adapter.create_email_adapter(
        imap_host='imap.ionos.com', smtp_host='smtp.ionos.com',
        email_address=MCGROCE, password=PASSWORDS[MCGROCE], mark_as_read=True)

    connected = asyncio.run(adapter.connect())

    assert connected is False
    assert adapter.login_refused is _REPLIES[case][2]


# ── a mail-server lookup that fails says so (rv_d072) ──────────────────

def test_a_mail_server_lookup_that_cannot_run_is_logged_and_the_turn_says_why(node, web, vault, cards, monkeypatch):
    """No dnspython and no dig (Windows without dnspython): the lookup of a
    custom domain's mail servers cannot run.  It was silent, and the turn
    said only "Connect ... in the channel setup form" (rv_d072)."""
    from tests.unit.test_a_phone_credential_answer_is_stored_before_its_grant import (
        _all_records)
    _no_resolver(monkeypatch)
    records = _all_records(monkeypatch)
    goal = web.goal(_owner(web, monkeypatch), 'Rice buyers', MCGROCE)

    out = rig._send_in(goal, rig._tools(monkeypatch, node.store), to='buyer@mill.example')

    said = json.dumps(out)
    assert out['success'] is False and 'setup form' in said
    assert 'dnspython' in said and 'dig' in said              # why the form
    [line] = [t for t in records.text if 'mcgroce.com' in t and 'dnspython' in t]
    assert MCGROCE not in line and '***@mcgroce.com' in line   # local part masked
    assert _asks(cards) == []


def test_a_mail_host_whose_provider_is_not_known_is_logged_and_the_turn_says_why(node, web, vault, cards, monkeypatch):
    """A domain whose mail host is not a provider whose servers are known
    (Microsoft 365 here: it takes no password for IMAP) needs the setup
    form, and the turn says that is why."""
    from tests.unit.test_a_phone_credential_answer_is_stored_before_its_grant import (
        _all_records)
    m365 = 'mcgroce-com.mail.protection.outlook.com'
    _dns(monkeypatch, {'mcgroce.com': m365})
    records = _all_records(monkeypatch)
    goal = web.goal(_owner(web, monkeypatch), 'Rice buyers', MCGROCE)

    out = rig._send_in(goal, rig._tools(monkeypatch, node.store), to='buyer@mill.example')

    said = json.dumps(out)
    assert out['success'] is False and 'setup form' in said
    assert m365 in said and 'not one of the providers' in said
    [line] = [t for t in records.text if m365 in t]
    assert MCGROCE not in line and '***@mcgroce.com' in line
    assert _asks(cards) == []


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
