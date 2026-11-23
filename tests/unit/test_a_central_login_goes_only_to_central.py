"""A stored Hevolve login goes only to a central, and only for a cloud account.

Measured 2026-11-23: a tablet's turn for a central agent (the Cafe agent,
344) reached the desktop with the phone's DEVICE token, and Nunba forwarded
that token to azurekong /chat/custom_gpt, which answered 401; local_assistant
answered in its place.  Owner rule: a central agent runs on central, as the
account, on the account's credits.  The rule for when this node may send
the login it holds for an account (the users row api_token a desktop
sign-in stores) is one: integrations.social.sync_engine.central_login_for,
which recipe_sync_route reuses.  Its origin half, is_central_origin, decides
where Nunba's cloud-agent turn may send any bearer.

The login goes only when the URL's ORIGIN (scheme, host, port) is a known
central's (core.superadmins.ALL_CENTRAL_URLS or HEVOLVE_CENTRAL_URL), the
account is an all-digit cloud id, and this node is not central itself.
Decided from the parsed URL, never its text: a look-alike host, a central
named in the path or in userinfo, another scheme or another port gets ''.
The same central written differently (case, one trailing dot, the default
port written out) gets the login.

Run as one file: python -m pytest <this file> --noconftest -q
"""
import logging
import os
import sys
import tempfile

os.environ['HEVOLVE_DB_PATH'] = ':memory:'
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

import pytest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.superadmins import ALL_CENTRAL_URLS  # noqa: E402
from integrations.social import sync_engine  # noqa: E402
from integrations.social.models import Base, User, db_session, get_engine  # noqa: E402

LOGIN = 'fake-hevolve-login-for-10202'      # a fake token, never a real one
NODE_TOKEN = 'fake-node-own-token'
CLOUD_ID = '10202'
CONFIGURED = 'https://central.example'
GENESIS = 'https://azurekong.hertzai.com'
assert GENESIS in ALL_CENTRAL_URLS, 'the look-alike rows below are built on it'


@pytest.fixture(autouse=True)
def node(monkeypatch):
    """A desktop (flat tier) with the operator's central configured, and the
    account rows a desktop sign-in writes: the cloud id holding the person's
    Hevolve login, and a local account (a uuid) holding the node's own
    token."""
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    monkeypatch.setenv('HEVOLVE_CENTRAL_URL', CONFIGURED)
    monkeypatch.delenv('HEVOLVE_REGIONAL_URL', raising=False)
    engine = get_engine()
    Base.metadata.create_all(engine)
    with db_session() as db:
        db.add(User(id=CLOUD_ID, username='desk', display_name='Desk',
                    email='desk@example.com', user_type='human',
                    api_token=LOGIN))
        db.add(User(username='local', display_name='Local',
                    email='local@example.com', user_type='human',
                    api_token=NODE_TOKEN))
    yield
    Base.metadata.drop_all(engine)


def _local_id():
    with db_session(commit=False) as db:
        return str(db.query(User).filter(
            User.email == 'local@example.com').first().id)


def _gives(url, user_id=CLOUD_ID):
    return sync_engine.central_login_for(user_id, url)


# ── a central gets the login ────────────────────────────────────────────

@pytest.mark.parametrize('url', [
    *(c + '/chat/custom_gpt' for c in ALL_CENTRAL_URLS),
    *(c for c in ALL_CENTRAL_URLS),
    CONFIGURED + '/chat/custom_gpt',
    CONFIGURED,
])
def test_a_central_origin_and_a_cloud_id_get_the_login(url):
    assert sync_engine.is_central_origin(url) is True
    assert _gives(url) == LOGIN


@pytest.mark.parametrize('url', [
    'https://AzureKong.HertzAI.com/chat/custom_gpt',     # hosts ignore case
    'HTTPS://azurekong.hertzai.com/chat/custom_gpt',     # so do schemes
    'https://azurekong.hertzai.com./chat/custom_gpt',    # one trailing dot
    'https://azurekong.hertzai.com:443/chat/custom_gpt',  # the default port
    'https://azurekong.hertzai.com/chat/custom_gpt?x=1#frag',
])
def test_the_same_central_written_differently_gets_the_login(url):
    """One trailing dot is the DNS root: the same host, so it is treated as
    the same central (documented choice).  Two dots are no host name."""
    assert sync_engine.is_central_origin(url) is True
    assert _gives(url) == LOGIN


# ── anything else gets '' ───────────────────────────────────────────────

@pytest.mark.parametrize('url', [
    'https://example.com/chat/custom_gpt',
    'https://regional.lan',
    'http://localhost:5000/chat',
    '',
    None,
])
def test_a_url_that_is_no_central_gets_nothing(url):
    assert sync_engine.is_central_origin(url) is False
    assert _gives(url) == ''


@pytest.mark.parametrize('url', [
    # the central's host as the front of another domain
    'https://central.example.evil/chat',
    'https://azurekong.hertzai.com.evil/chat',
    'https://azurekong.hertzai.com.evil.com/chat',
    'https://central.hevolve.ai.example.evil/',
    # the central's host at the end of another name
    'https://evilazurekong.hertzai.com/chat',
    'https://evil-central.example/chat',
    # the central named in the path, not the host
    'evil.com/azurekong.hertzai.com',
    'https://evil.com/azurekong.hertzai.com',
    'https://evil.com/https://azurekong.hertzai.com/',
    # userinfo, on either side of the @
    'https://azurekong.hertzai.com@evil.com/',
    'https://evil.com@azurekong.hertzai.com/',
    'https://user:pw@azurekong.hertzai.com/',
    'https://azurekong.hertzai.com:443@evil.com/',
    'https://evil.com\\@azurekong.hertzai.com/',
    'https://evil.com#@azurekong.hertzai.com/',
    # the scheme the central's own URL does not name
    'http://azurekong.hertzai.com/chat/custom_gpt',
    'http://central.example/chat',
    'ftp://azurekong.hertzai.com/',
    # a port the central's URL does not name
    'https://azurekong.hertzai.com:8000/chat',
    'https://azurekong.hertzai.com:80/chat',
    'https://azurekong.hertzai.com:0/chat',
    'https://azurekong.hertzai.com:abc/chat',
    # two trailing dots are no host name
    'https://azurekong.hertzai.com../chat',
    'https:///azurekong.hertzai.com',
])
def test_a_look_alike_gets_nothing(url):
    assert sync_engine.is_central_origin(url) is False
    assert _gives(url) == ''


@pytest.mark.parametrize('url', [
    '  https://azurekong.hertzai.com/chat/custom_gpt  ',
    'https://azurekong.hertzai.com/chat/custom_gpt\n',
    '\x00https://azurekong.hertzai.com/chat/custom_gpt',
    'https://azure\tkong.hertzai.com/chat/custom_gpt',
    'https://azurekong.hertzai.com\r\n.evil.com/chat',
    'https://azurekong.hertzai.com/chat custom_gpt',
    'https://azurekong.hertzai.com\x7f/chat',
    'https://azurekong.hertzai.com /chat',
])
def test_a_url_that_would_need_cleaning_gets_nothing(url):
    """urlsplit drops a tab or newline from inside a host and spaces from
    the front, so the URL it judged is not the text a caller sends: such a
    URL is refused, never cleaned, and the URL approved is the URL called."""
    assert sync_engine.is_central_origin(url) is False
    assert _gives(url) == ''


def test_a_port_the_configured_central_names_is_its_origin(monkeypatch):
    monkeypatch.setenv('HEVOLVE_CENTRAL_URL', 'https://central.example:8443')
    assert _gives('https://central.example:8443/chat') == LOGIN
    assert _gives('https://central.example/chat') == '', \
        'the configured central names 8443; 443 is another server'


def test_the_scheme_rule_is_the_configured_centrals_own(monkeypatch):
    """The same scheme rule the central check always had: the central's URL
    names its scheme, and only that scheme is the central."""
    monkeypatch.setenv('HEVOLVE_CENTRAL_URL', 'http://central.lan')
    assert _gives('http://central.lan/chat') == LOGIN
    assert _gives('https://central.lan/chat') == ''


# ── only a cloud account, only from a node that is not central ──────────

@pytest.mark.parametrize('who', ['uuid', '', None, ' 10202', '10202 ', '-10202',
                                 '10202.0', 'no-such-account', '99999'])
def test_an_account_central_does_not_number_gets_nothing(who):
    user_id = _local_id() if who == 'uuid' else who
    assert _gives(GENESIS + '/chat/custom_gpt', user_id) == ''


def test_digits_of_another_script_are_no_cloud_id():
    """str.isdigit is True for Arabic-Indic digits; central numbers its
    accounts in ASCII.  The row exists, so it is the rule that refuses."""
    other = '١٠٢٠٢'
    with db_session() as db:
        db.add(User(id=other, username='odd', display_name='Odd',
                    email='odd@example.com', user_type='human',
                    api_token='fake-login-of-odd-digits'))
    assert other.isdigit(), 'the row this test needs: digits str.isdigit takes'
    assert _gives(GENESIS + '/chat/custom_gpt', other) == ''


def test_a_numeric_id_is_read_as_its_text():
    assert _gives(GENESIS + '/chat/custom_gpt', 10202) == LOGIN


def test_a_central_node_sends_no_stored_login(monkeypatch):
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    assert _gives(GENESIS + '/chat/custom_gpt') == ''


def test_a_login_that_cannot_be_read_is_not_sent_and_not_logged(
        monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    class Broken:
        def __enter__(self):
            raise RuntimeError('db down')

        def __exit__(self, *a):
            return False

    from integrations.social import models
    monkeypatch.setattr(models, 'db_session', lambda **kw: Broken())
    assert _gives(GENESIS + '/chat/custom_gpt') == ''
    assert 'db down' in caplog.text


def test_no_log_line_ever_carries_the_login(caplog):
    caplog.set_level(logging.DEBUG)
    for url in (GENESIS, 'https://evil.com@azurekong.hertzai.com/', ''):
        for who in (CLOUD_ID, _local_id(), ''):
            sync_engine.central_login_for(who, url)
    sync_engine.recipe_sync_route(CLOUD_ID)
    assert LOGIN not in caplog.text and NODE_TOKEN not in caplog.text


# ── recipe_sync_route: one rule, its callers' cases unchanged ───────────

def _parent(monkeypatch, url):
    monkeypatch.setattr(sync_engine.SyncEngine, 'parent_tier_url',
                        staticmethod(lambda: url))


def test_recipe_sync_route_takes_the_login_from_the_one_rule(monkeypatch):
    asked = []

    def rule(user_id, url):
        asked.append((user_id, url))
        return 'from-the-rule'

    _parent(monkeypatch, CONFIGURED)
    monkeypatch.setattr(sync_engine, 'central_login_for', rule)
    assert sync_engine.recipe_sync_route(CLOUD_ID) == (CONFIGURED,
                                                       'from-the-rule')
    assert asked == [(CLOUD_ID, CONFIGURED)]


def test_recipe_sync_route_cases_are_unchanged(monkeypatch):
    from security import key_delegation
    _parent(monkeypatch, CONFIGURED)
    assert sync_engine.recipe_sync_route(CLOUD_ID) == (CONFIGURED, LOGIN)
    assert sync_engine.recipe_sync_route('no-such-account') == (CONFIGURED, '')
    assert sync_engine.recipe_sync_route('') == (CONFIGURED, '')
    assert sync_engine.recipe_sync_route(_local_id()) == (CONFIGURED, '')
    _parent(monkeypatch, ALL_CENTRAL_URLS[-1])
    monkeypatch.delenv('HEVOLVE_CENTRAL_URL', raising=False)
    assert sync_engine.recipe_sync_route(CLOUD_ID) == (ALL_CENTRAL_URLS[-1],
                                                       LOGIN)
    _parent(monkeypatch, 'https://regional.lan')
    assert sync_engine.recipe_sync_route(CLOUD_ID) == ('https://regional.lan', '')
    _parent(monkeypatch, '')
    assert sync_engine.recipe_sync_route(CLOUD_ID) == ('', '')
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    _parent(monkeypatch, CONFIGURED)
    assert sync_engine.recipe_sync_route(CLOUD_ID) == ('', '')


# ── the agent list on central's DB: the same rule for its own URL ───────
# sync_agent_to_parent lists the agent on central's DB (get_central_db_url,
# which HEVOLVE_CENTRAL_DB_URL overrides as given) and takes it out of
# Creation Mode there, both with the creator's login.  That URL is not the
# parent's, so the login goes to it only by central_login_for of that URL.

PID = '1793372451267'
AGENT = {'name': 'Spoken English Coach', 'goal': 'Coach spoken English',
         'creator_user_id': CLOUD_ID}


@pytest.fixture
def agent_sync(monkeypatch, tmp_path):
    """sync_agent_to_parent on a desktop whose parent is the configured
    central, with every request it makes recorded (never sent)."""
    import json
    from unittest.mock import MagicMock
    from core import http_pool, recipe_sync
    _parent(monkeypatch, CONFIGURED)
    monkeypatch.setattr(recipe_sync, '_PUSH_CACHE_FILE',
                        str(tmp_path / 'push-cache.json'))
    (tmp_path / f'{PID}.json').write_text(json.dumps(AGENT))
    sent, pushes = [], []

    def post(url, **kw):
        sent.append(('POST', url, dict(kw.get('headers') or {})))
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'prompt_ids': [344]}
        return resp

    def patch_(url, **kw):
        sent.append(('PATCH', url, dict(kw.get('headers') or {})))
        return MagicMock(status_code=200)

    def push(prompts_dir, pid, user_id='', central_url='', bearer='',
             as_prompt_id=None, **_k):
        pushes.append({'central_url': central_url, 'as_prompt_id': as_prompt_id})
        return bool(central_url)
    monkeypatch.setattr(http_pool, 'pooled_post', post)
    monkeypatch.setattr(http_pool, 'pooled_patch', patch_)
    monkeypatch.setattr(recipe_sync, 'push_recipe', push)

    def run(db_url):
        monkeypatch.setenv('HEVOLVE_CENTRAL_DB_URL', db_url)
        sync_engine.sync_agent_to_parent(str(tmp_path), PID, AGENT, force=True)
        return sent, pushes
    return run


def test_a_central_db_url_gets_the_login_for_the_agent_list(agent_sync):
    sent, pushes = agent_sync(GENESIS + '/db')
    assert [(verb, url) for verb, url, _h in sent] == [
        ('POST', GENESIS + '/db/createpromptlist'),
        ('PATCH', GENESIS + '/db/update_agent_prompt?prompt_id=344')]
    assert all(h.get('Authorization') == f'Bearer {LOGIN}' for _v, _u, h in sent)
    assert pushes == [{'central_url': CONFIGURED, 'as_prompt_id': 344}]


@pytest.mark.parametrize('db_url', [
    'https://kong.example/db',
    'https://azurekong.hertzai.com.evil/db',
    'https://azurekong.hertzai.com@evil.com/db',
    'http://azurekong.hertzai.com/db',
    'https://azurekong.hertzai.com:8443/db',
    'https://azure\tkong.hertzai.com/db',
    'https://my-private-cloud:8443/db',     # the documented override, alone
])
def test_a_db_url_that_is_no_central_never_gets_the_login(agent_sync, db_url):
    sent, pushes = agent_sync(db_url)
    assert all(LOGIN not in str(h) for _v, _u, h in sent), \
        f'the login went to {db_url!r}'
    assert sent == [], 'the agent was listed without a login central proves'
    assert pushes == [{'central_url': '', 'as_prompt_id': None}]


def test_a_private_cloud_db_gets_the_login_when_it_is_the_configured_central(
        agent_sync, monkeypatch):
    """docs/getting-started/configuration.md: the private-cloud DB override
    lists agents with the login once HEVOLVE_CENTRAL_URL names its origin."""
    private = 'https://my-private-cloud:8443'
    monkeypatch.setenv('HEVOLVE_CENTRAL_URL', private)
    _parent(monkeypatch, private)
    sent, pushes = agent_sync(private + '/db')
    assert [url for _v, url, _h in sent] == [
        private + '/db/createpromptlist',
        private + '/db/update_agent_prompt?prompt_id=344']
    assert all(h.get('Authorization') == f'Bearer {LOGIN}' for _v, _u, h in sent)


# ── why a URL is not a central's, in one word, never the URL ────────────

@pytest.mark.parametrize('url, word', [
    (GENESIS + '/chat/custom_gpt', ''),
    ('https://AzureKong.HertzAI.com:443/chat', ''),
    (CONFIGURED + '/x', ''),
    ('https://evil.com/chat', 'host'),
    ('https://azurekong.hertzai.com.evil/chat', 'host'),
    ('https://evil.com/azurekong.hertzai.com', 'host'),
    ('https://azurekong.hertzai.com../chat', 'host'),
    ('https:///azurekong.hertzai.com', 'host'),
    ('https://[azurekong/chat', 'host'),
    ('', 'host'),
    ('http://azurekong.hertzai.com/chat', 'scheme'),
    ('http://central.example/chat', 'scheme'),
    ('ftp://azurekong.hertzai.com/', 'scheme'),
    ('evil.com/azurekong.hertzai.com', 'scheme'),
    ('https://azurekong.hertzai.com:8443/chat', 'port'),
    ('https://azurekong.hertzai.com:80/chat', 'port'),
    ('https://azurekong.hertzai.com:abc/chat', 'port'),
    ('https://azure\tkong.hertzai.com/chat', 'characters'),
    (' https://azurekong.hertzai.com/chat', 'characters'),
    ('https://azurekong.hertzai.com/chat\n', 'characters'),
    ('https://azurekong.hertzai.com@evil.com/', 'userinfo'),
    ('https://evil.com@azurekong.hertzai.com/', 'userinfo'),
])
def test_the_mismatch_names_what_is_not_a_centrals(url, word):
    assert sync_engine.central_origin_mismatch(url) == word
    assert sync_engine.is_central_origin(url) is (word == '')


def test_whitespace_in_the_central_address_is_said_in_the_log(
        monkeypatch, caplog):
    """A HEVOLVE_CENTRAL_URL pasted with a newline sends no login, and the
    log says why (it used to show only 'login held=False'); never the URL,
    never the login."""
    caplog.set_level(logging.DEBUG)
    pasted = CONFIGURED + '\n'
    monkeypatch.setenv('HEVOLVE_CENTRAL_URL', pasted)
    _parent(monkeypatch, pasted)
    assert sync_engine.recipe_sync_route(CLOUD_ID) == (pasted, '')
    line = next(r.getMessage() for r in caplog.records
                if 'whitespace or a control character' in r.getMessage())
    assert 'HEVOLVE_CENTRAL_URL' in line and CLOUD_ID in line
    assert 'central.example' not in caplog.text and LOGIN not in caplog.text


def test_a_parent_that_is_just_no_central_logs_nothing(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _parent(monkeypatch, 'https://regional.lan')
    assert sync_engine.recipe_sync_route(CLOUD_ID) == ('https://regional.lan', '')
    assert 'whitespace' not in caplog.text
