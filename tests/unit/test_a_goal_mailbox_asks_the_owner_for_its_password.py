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

import pytest

import tests.unit.test_each_goal_sends_from_its_own_mailbox as rig
from tests.unit.test_each_goal_sends_from_its_own_mailbox import (  # noqa: F401 (fixtures)
    MCGROCE, PASSWORDS, channels, mail, mailer, node, web)

SECRET = 'EMAIL_PASSWORD_REGISTRATION_MCGROCE_COM'
IONOS = {'imap_host': 'imap.ionos.com', 'smtp_host': 'smtp.ionos.com'}


@pytest.fixture
def asked(node, monkeypatch, tmp_path):
    """A vault of this test's own, the MX of mcgroce.com at IONOS, no start
    recorded yet, and every consent card the owner is sent."""
    from hartos import ai_key_vault
    from integrations.channels import agent_tools
    from integrations.social import consent_service
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
    cards = []
    monkeypatch.setattr(consent_service, '_emit',
                        lambda topic, data, msg_id=None:
                        cards.append((topic, dict(data), msg_id)))
    yield cards
    SecretsManager.reset()
    ai_key_vault.AIKeyVault.reset()


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


def _refuse_wrong_passwords(monkeypatch):
    """The IMAP server answers a wrong password with "refused" (the adapter's
    connect returns False), as a real server's AUTHENTICATIONFAILED does."""
    from integrations.channels.extensions import email_adapter

    async def login(self):
        return self.email_config.imap_password == PASSWORDS[MCGROCE]
    monkeypatch.setattr(email_adapter.EmailAdapter, '_connect_imap', login)


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


def test_a_password_the_server_refuses_asks_the_owner_for_another(node, web, asked, monkeypatch):
    from integrations.channels.agent_tools import START_FAILED, start_verdict
    _refuse_wrong_passwords(monkeypatch)
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


def test_a_strangers_refused_connect_never_marks_the_owners_password_failed(node, web, asked, monkeypatch):
    """Another account's connect of the same mailbox with a wrong password is
    refused by the server; the owner's own password is still tried."""
    from integrations.channels.agent_tools import START_FAILED, start_verdict
    _refuse_wrong_passwords(monkeypatch)
    owner = _owner(web, monkeypatch)
    _entered(PASSWORDS[MCGROCE])
    web.connect(web.sign_up('stranger'), MCGROCE, password='a-guess')
    assert (start_verdict('email', MCGROCE) or {}).get('state') != START_FAILED

    goal = web.goal(owner, 'Rice buyers', MCGROCE)
    first = rig._send_in(goal, rig._tools(monkeypatch, node.store), to='buyer@mill.example')

    assert 'is connecting now' in json.dumps(first)
    rig._mailbox(node.channels, MCGROCE)
    assert _asks(asked) == []
