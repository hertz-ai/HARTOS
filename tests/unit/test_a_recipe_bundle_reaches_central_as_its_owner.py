"""A recipe bundle reaches central as its owner's, where central's /chat reads it.

Measured 2026-11-20 (owner: "recipe sync shd have taken"): no desktop agent ever
reached central.  CREATE's push (create_recipe.update_agent_creation_to_db ->
core.recipe_sync.push_recipe) went to get_central_db_url()/prompts/sync, which
is Hevolve_Database behind Kong and answers 404 (all 6 pushes since 11-18), and
it carried no credential.  HARTOS's own /prompts/sync stored a bundle only in
recipe_blobs, a directory /chat never reads, from any caller.

Now the desktop sends the bundle to its parent HARTOS (SyncEngine's parent
tier) with the creator's login, central's gate proves that login
(security.middleware._admit_hevolve_login), and central keeps the bundle as
that account's: in the blob store for the owner's other devices and in the
prompts directory /chat reads.  An id is the client's choice, so a bundle may
not take another account's agent or one of central's own, nor carry a file of
another agent.  A bundle stored with an owner is private: only that owner reads
it back.  One kept before bundles had owners (no owner) is a public agent:
every signed-in account reads it, unless a push of it would be refused today
(a file of another agent, or a claim to be a system agent), and then it gets
the same 404 as no bundle.  Pushes always need the owner.
"""
import json
import os
import sys
import tempfile
import threading
import time

os.environ['HEVOLVE_DB_PATH'] = ':memory:'
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from flask import g  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core import recipe_sync  # noqa: E402
from integrations.social import kong_identity, node_topology  # noqa: E402
from integrations.social.models import Base, User, db_session, get_engine  # noqa: E402

PID = '1793372451267'
OWNER, OTHER = 'owner@example.com', 'other@example.com'
OWNER_TOKEN, OTHER_TOKEN = 'kong-owner-token', 'kong-other-token'
VIA_KONG = {'REMOTE_ADDR': '172.21.0.2'}   # Kong's address on central


@pytest.fixture
def accounts():
    engine = get_engine()
    Base.metadata.create_all(engine)
    with db_session() as db:
        db.add(User(username='owner', display_name='Owner', email=OWNER,
                    user_type='human', api_token='t-owner'))
        db.add(User(username='other', display_name='Other', email=OTHER,
                    user_type='human', api_token='t-other'))
    yield
    Base.metadata.drop_all(engine)


def _id_of(email):
    with db_session(commit=False) as db:
        return str(db.query(User).filter(User.email == email).first().id)


def _envelope(pid=PID, files=None, uploaded_at=None, force=False):
    files = files if files is not None else {
        f'{pid}.json': json.dumps({'name': 'Spoken English Coach', 'flows': []}),
        f'{pid}_0_recipe.json': json.dumps({'actions': ['greet']}),
    }
    env = {'schema_version': recipe_sync.SCHEMA_VERSION, 'prompt_id': str(pid),
           'user_id': '10202', 'files': files,
           'checksum': recipe_sync.envelope_checksum(files),
           'uploaded_at': uploaded_at or int(time.time())}
    if force:
        env['force'] = True
    return env


# ── the desktop half ─────────────────────────────────────────────────────

@pytest.fixture
def bundle_dir(tmp_path, monkeypatch):
    (tmp_path / f'{PID}.json').write_text('{"name":"Coach"}')
    monkeypatch.setattr(recipe_sync, '_PUSH_CACHE_FILE', str(tmp_path / 'cache.json'))
    return tmp_path


def test_a_push_carries_the_creators_login_to_the_parent(bundle_dir):
    sent = {}

    def post(url, **kw):
        sent.update(url=url, headers=kw.get('headers') or {})
        return MagicMock(status_code=200)

    with patch('core.http_pool.pooled_post', post):
        assert recipe_sync.push_recipe(str(bundle_dir), PID, user_id='10202',
                                       central_url='https://central',
                                       bearer=OWNER_TOKEN)
    assert sent['url'] == 'https://central/prompts/sync'
    assert sent['headers'].get('Authorization') == f'Bearer {OWNER_TOKEN}'


def test_a_pull_carries_the_login(tmp_path):
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=404)
    with patch('core.http_pool.get_http_session', return_value=session):
        recipe_sync.pull_recipe(str(tmp_path), PID, '10202',
                                central_url='https://central', bearer=OWNER_TOKEN)
    url = session.get.call_args.args[0]
    assert url == f'https://central/prompts/sync/{PID}'
    assert session.get.call_args.kwargs['headers'].get('Authorization') == \
        f'Bearer {OWNER_TOKEN}'


def test_no_parent_means_no_sync_never_the_account_store(bundle_dir):
    """The /db default was Hevolve_Database: it 404'd every push.  With no
    parent named, nothing is sent anywhere."""
    session = MagicMock()
    with patch('core.config_cache.get_central_db_url',
               return_value='https://kong.example/db'), \
            patch('core.http_pool.pooled_post') as post, \
            patch('core.http_pool.get_http_session', return_value=session):
        assert recipe_sync.push_recipe(str(bundle_dir), PID) is False
        assert recipe_sync.pull_recipe(str(bundle_dir), PID, '10202') is False
    post.assert_not_called()
    session.get.assert_not_called()


def _desk_account():
    """The row a desktop's sign-in writes (Nunba /api/storage/set): the
    cloud id, with the Hevolve token as its api_token."""
    with db_session() as db:
        db.add(User(id='10202', username='desk', display_name='Desk',
                    email='desk@example.com', user_type='human',
                    api_token=OWNER_TOKEN))


def _parent(monkeypatch, url, central_env='', regional_env=''):
    from integrations.social import sync_engine
    for name, value in (('HEVOLVE_CENTRAL_URL', central_env),
                        ('HEVOLVE_REGIONAL_URL', regional_env)):
        if value:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sync_engine.SyncEngine, 'parent_tier_url',
                        staticmethod(lambda: url))
    return sync_engine


def test_the_route_is_the_parent_and_the_login_this_node_holds(accounts, monkeypatch):
    from security import key_delegation
    sync_engine = _parent(monkeypatch, 'https://central',
                          central_env='https://central')
    _desk_account()
    assert sync_engine.recipe_sync_route('10202') == ('https://central', OWNER_TOKEN)
    assert sync_engine.recipe_sync_route('no-such-account') == ('https://central', '')
    assert sync_engine.recipe_sync_route('') == ('https://central', '')
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    assert sync_engine.recipe_sync_route('10202') == ('', ''), \
        'central is the top of the hive: it has no parent to sync to'


def test_a_regional_parent_never_receives_a_login(accounts, monkeypatch):
    """rv_fcaee must-fix 2: the login is a person's Hevolve token; only
    central proves it (with Kong), so no other parent is ever handed it."""
    sync_engine = _parent(monkeypatch, 'https://regional.lan',
                          regional_env='https://regional.lan')
    _desk_account()
    assert sync_engine.recipe_sync_route('10202') == ('https://regional.lan', '')


def test_a_genesis_central_gets_the_login_with_no_setting(accounts, monkeypatch):
    from core.superadmins import ALL_CENTRAL_URLS
    sync_engine = _parent(monkeypatch, ALL_CENTRAL_URLS[-1])
    _desk_account()
    assert sync_engine.recipe_sync_route('10202') == (ALL_CENTRAL_URLS[-1],
                                                      OWNER_TOKEN)


def test_a_local_account_never_sends_its_token(accounts, monkeypatch):
    """A users row central does not number (a uuid) holds this node's own
    token, not a Hevolve login: it goes nowhere."""
    sync_engine = _parent(monkeypatch, 'https://central',
                          central_env='https://central')
    assert sync_engine.recipe_sync_route(_id_of(OWNER)) == ('https://central', '')


# ── the phone's list: central assigns the id the phone can hold ─────────
# Measured 2026-11-20: central's prompt.prompt_id is int(11) and the phone's
# CustomChatViewResponse.prompt_id is a Java Integer; desktop ids are 11-13
# digits (1793372451267).  So the agent is listed on central WITHOUT an id,
# Hevolve_Database's createpromptlist gives it one (and finds the same row by
# name + account on a re-send), and the bundle goes under that id.

AGENT = {'name': 'Spoken English Coach', 'goal': 'Coach spoken English',
         'creator_user_id': '10202', 'image_url': '', 'prompt_id': PID}


def _central_db(prompt_ids=(344,), status=200):
    calls = []

    def post(url, **kw):
        calls.append(('POST', url, kw))
        resp = MagicMock(status_code=status)
        resp.json.return_value = {'prompt_ids': list(prompt_ids)}
        return resp

    def patch_(url, **kw):
        calls.append(('PATCH', url, kw))
        return MagicMock(status_code=200)
    return calls, post, patch_


def test_a_finished_agent_is_listed_on_central():
    from integrations.social import sync_engine
    calls, post, patch_ = _central_db()
    with patch('core.config_cache.get_central_db_url',
               return_value='https://kong.example/db'), \
            patch('core.http_pool.pooled_post', post), \
            patch('core.http_pool.pooled_patch', patch_):
        assert sync_engine.list_agent_on_central(AGENT, '10202', OWNER_TOKEN) == 344
    (verb, url, kw), = calls
    assert (verb, url) == ('POST', 'https://kong.example/db/createpromptlist')
    row = kw['json']['listprompts'][0]
    assert 'prompt_id' not in row, 'a desktop id does not fit central or the phone'
    assert row['user_id'] == 10202 and row['name'] == AGENT['name']
    assert row['prompt'] == AGENT['goal'] and row['is_active'] is True
    assert kw['headers']['Authorization'] == f'Bearer {OWNER_TOKEN}'


def test_ready_on_central_takes_the_agent_out_of_creation_mode():
    from integrations.social import sync_engine
    calls, post, patch_ = _central_db()
    with patch('core.config_cache.get_central_db_url',
               return_value='https://kong.example/db'), \
            patch('core.http_pool.pooled_patch', patch_):
        assert sync_engine.mark_agent_ready_on_central(344, OWNER_TOKEN) is True
    (verb, url, kw), = calls
    assert (verb, url) == ('PATCH',
                           'https://kong.example/db/update_agent_prompt?prompt_id=344')
    assert kw['headers']['Authorization'] == f'Bearer {OWNER_TOKEN}'


def test_an_account_central_does_not_number_is_not_listed():
    from integrations.social import sync_engine
    calls, post, patch_ = _central_db()
    with patch('core.config_cache.get_central_db_url',
               return_value='https://kong.example/db'), \
            patch('core.http_pool.pooled_post', post), \
            patch('core.http_pool.pooled_patch', patch_):
        assert sync_engine.list_agent_on_central(
            AGENT, '6c2dc0fc-7c93-4fe0-973e-f7466ff63f29', OWNER_TOKEN) is None
        assert sync_engine.list_agent_on_central(AGENT, '10202', '') is None
    assert calls == []


def test_a_refused_listing_gives_no_id_and_flips_nothing():
    from integrations.social import sync_engine
    calls, post, patch_ = _central_db(status=401)
    with patch('core.config_cache.get_central_db_url',
               return_value='https://kong.example/db'), \
            patch('core.http_pool.pooled_post', post), \
            patch('core.http_pool.pooled_patch', patch_):
        assert sync_engine.list_agent_on_central(AGENT, '10202', OWNER_TOKEN) is None
    assert [c[0] for c in calls] == ['POST']


def test_a_bundle_is_rekeyed_to_the_central_id():
    files = {f'{PID}.json': json.dumps({'name': 'Coach', 'prompt_id': PID}),
             f'{PID}_0_recipe.json': '{"actions":["greet"]}',
             f'{PID}_personality.json': '{"tone":"warm"}'}
    env = _envelope(files=files)
    out = recipe_sync.rekey_envelope(env, 344)
    assert out['prompt_id'] == '344'
    assert set(out['files']) == {'344.json', '344_0_recipe.json',
                                 '344_personality.json'}
    assert json.loads(out['files']['344.json'])['prompt_id'] == '344'
    assert out['files']['344_0_recipe.json'] == files[f'{PID}_0_recipe.json']
    assert out['checksum'] == recipe_sync.envelope_checksum(out['files'])
    assert env['prompt_id'] == PID, 'the local envelope is not changed in place'


def test_a_rekeyed_agent_file_names_a_non_numeric_id_as_given():
    """A numeric id is rewritten as a number only when the new id is all
    digits; any other id goes in as given.  rekey_envelope raised inside
    its own JSON guard for a non-numeric id and sent the agent file still
    naming the old id (int('goal_x') is a ValueError)."""
    files = {f'{PID}.json': json.dumps({'name': 'Coach', 'prompt_id': int(PID)})}
    out = recipe_sync.rekey_envelope(_envelope(files=files), 'goal_x')
    assert json.loads(out['files']['goal_x.json'])['prompt_id'] == 'goal_x'
    out = recipe_sync.rekey_envelope(_envelope(files=files), '344')
    assert json.loads(out['files']['344.json'])['prompt_id'] == 344


def test_source_guard_one_rekey_for_a_bundle_under_another_id():
    """Parallel-path audit H21 (11-21): peer_reuse re-keyed a peer's bundle
    with its own _alias_envelope_for_local beside core.recipe_sync.
    rekey_envelope.  The A2A pull uses the one re-keyer."""
    import inspect
    from integrations.google_a2a import peer_reuse
    assert not hasattr(peer_reuse, '_alias_envelope_for_local')
    assert 'rekey_envelope(' in inspect.getsource(peer_reuse.pull_recipe)


def test_a_push_goes_under_the_central_id(bundle_dir):
    sent = {}

    def post(url, **kw):
        sent.update(json=kw['json'])
        return MagicMock(status_code=200)

    with patch('core.http_pool.pooled_post', post):
        assert recipe_sync.push_recipe(str(bundle_dir), PID, user_id='10202',
                                       central_url='https://central',
                                       bearer=OWNER_TOKEN, as_prompt_id=344)
    assert sent['json']['prompt_id'] == '344'
    assert set(sent['json']['files']) == {'344.json'}


@pytest.fixture
def sends(monkeypatch, tmp_path):
    """sync_agent_to_parent with the route, the listing and the push recorded."""
    from integrations.social import sync_engine
    monkeypatch.setattr(recipe_sync, '_PUSH_CACHE_FILE',
                        str(tmp_path / 'push-cache.json'))
    listed, pushes, events = [], [], []
    stored = {'ok': True}
    monkeypatch.setattr(sync_engine, 'recipe_sync_route',
                        lambda uid: ('https://central', f'login-of-{uid}'))

    def list_(record, uid, login):
        listed.append((record.get('name'), uid, login))
        events.append('listed')
        return 344
    monkeypatch.setattr(sync_engine, 'list_agent_on_central', list_)

    def push(prompts_dir, pid, user_id='', central_url='', force=False,
             bearer='', as_prompt_id=None, **_k):
        pushes.append((str(pid), user_id, central_url, bearer, as_prompt_id,
                       force))
        events.append('pushed')
        return stored['ok']
    monkeypatch.setattr(recipe_sync, 'push_recipe', push)
    monkeypatch.setattr(sync_engine, 'mark_agent_ready_on_central',
                        lambda cid, login: events.append(('ready', cid, login))
                        or True, raising=False)
    monkeypatch.setattr(sync_engine, '_sends_stored', stored, raising=False)
    monkeypatch.setattr(sync_engine, '_sends_events', events, raising=False)
    return sync_engine, listed, pushes


def test_an_agent_goes_to_the_parent_under_its_central_id(sends, tmp_path):
    sync_engine, listed, pushes = sends
    assert sync_engine.sync_agent_to_parent(str(tmp_path), PID, AGENT, force=True)
    assert listed == [(AGENT['name'], '10202', 'login-of-10202')]
    assert pushes == [(PID, '10202', 'https://central', 'login-of-10202', 344, True)]
    assert sync_engine._sends_events == [
        'listed', 'pushed', ('ready', 344, 'login-of-10202')], \
        'the agent leaves Creation Mode only once its bundle is stored'


def test_a_refused_push_leaves_the_agent_in_creation_mode(sends, tmp_path):
    """rv_fcaee must-fix 3: a listed agent whose recipe never reached central
    must not look finished on the phone's list."""
    sync_engine, listed, pushes = sends
    sync_engine._sends_stored['ok'] = False
    assert not sync_engine.sync_agent_to_parent(str(tmp_path), PID, AGENT)
    assert sync_engine._sends_events == ['listed', 'pushed']


def test_a_local_only_agent_sends_nothing(sends, tmp_path):
    sync_engine, listed, pushes = sends
    reel = dict(AGENT, goal_tags=['reel_studio'])
    assert not sync_engine.sync_agent_to_parent(str(tmp_path), PID, reel)
    assert listed == [] and pushes == []


def test_without_a_login_nothing_is_listed_or_stored(sends, tmp_path, monkeypatch):
    sync_engine, listed, pushes = sends
    monkeypatch.setattr(sync_engine, 'recipe_sync_route',
                        lambda uid: ('https://central', ''))
    sync_engine.sync_agent_to_parent(str(tmp_path), PID, AGENT)
    assert listed == []
    # The push names no parent, which push_recipe answers with False and no
    # request (test_no_parent_means_no_sync_never_the_account_store).
    assert pushes and all(p[2] == '' and p[4] is None for p in pushes)


@pytest.fixture
def create_module():
    pytest.importorskip('autogen', reason='autogen not installed')
    from hartos import create_recipe
    return create_recipe


@pytest.fixture
def created(create_module, temp_prompts_dir, mock_flask_app, monkeypatch):
    """CREATE's completion step on a finished agent record, with what it
    sends to the parent recorded."""
    from integrations.social import sync_engine
    cr = create_module
    (temp_prompts_dir / f'{PID}.json').write_text(json.dumps(AGENT))
    monkeypatch.setattr(cr, 'pooled_patch', lambda url, **_k: None)
    sent = []
    monkeypatch.setattr(sync_engine, 'sync_agent_to_parent',
                        lambda prompts_dir, pid, record, force=False:
                        sent.append((str(pid), record.get('name'), force)) or True)
    cr.update_agent_creation_to_db(PID)
    deadline = time.monotonic() + 5
    while not sent and time.monotonic() < deadline:
        time.sleep(0.02)
    return sent


def test_create_sends_the_finished_agent_to_its_parent(created):
    assert created == [(PID, AGENT['name'], False)]


# ── the central half ─────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry  # noqa: TID251 -- the routes under test are on its app
    yield hart_intelligence_entry


@pytest.fixture
def central(hie, accounts, tmp_path, monkeypatch):
    from security import key_delegation
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    # Central as its master key proves it (node_topology.central_standing);
    # this test process holds no key.
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    monkeypatch.setattr(node_topology, '_central_claim_unproven', False,
                        raising=False)
    for name in ('NUNBA_BUNDLED', 'HEVOLVE_API_KEY', 'HEVOLVE_REQUIRE_AUTH',
                 'TRUSTED_PROXY', 'NUNBA_CI', 'HEVOLVE_TRUST_KONG',
                 'HEVOLVE_CLOUD_MODE'):
        monkeypatch.delenv(name, raising=False)
    prompts = tmp_path / 'prompts'
    blobs = tmp_path / 'recipe_blobs'
    prompts.mkdir()
    monkeypatch.setattr(hie, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(hie, '_RECIPE_BLOB_DIR', str(blobs))
    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    # Central's prompt list (Hevolve_Database) names OWNER as every agent's
    # owner unless a test says otherwise.
    monkeypatch.setattr(recipe_sync, 'prompt_owner_email', lambda pid: OWNER,
                        raising=False)
    return prompts, blobs


def _call(hie, method, path, token=None, body=None):
    """One request through the real gate and route, Kong answering for the
    two tokens as the two accounts' email addresses."""
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    secrets = {'security.secrets_manager': MagicMock(
        get_secret=lambda name: os.environ.get(name, ''))}
    proves = {OWNER_TOKEN: OWNER, OTHER_TOKEN: OTHER}
    with patch.dict(sys.modules, secrets), \
            patch.object(kong_identity, 'email_for_token',
                         side_effect=lambda t: proves.get(t)):
        client = hie.app.test_client()
        if method == 'POST':
            return client.post(path, json=body, headers=headers,
                               environ_base=VIA_KONG)
        return client.get(path, headers=headers, environ_base=VIA_KONG)


def _push(hie, envelope, token=OWNER_TOKEN):
    return _call(hie, 'POST', '/prompts/sync', token, envelope)


def test_central_is_the_node_its_master_key_proves(hie, monkeypatch):
    """Parallel-path audit H6 (11-21) of 799c7df82: the receiver of a push
    (/prompts/sync, _central_recipe_store) decided "central" by the
    HEVOLVE_NODE_TIER claim alone, while the sender (sync_engine.
    recipe_sync_route) asks security.key_delegation.get_node_tier, which
    takes central only on the master key's proof.  A node that claims
    central with no key kept bundles as owners' in the prompts /chat reads;
    it is not central.  (Nor is it a plain store: its sync is refused,
    the tests under "a central claim with no key proof" below.)

    get_node_tier is stubbed for both answers: the real one reads the
    master key from the environment of a process that holds it."""
    from security import key_delegation
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    monkeypatch.setattr(node_topology, '_central_claim_unproven', False,
                        raising=False)
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    assert node_topology.central_standing() != 'proven', \
        'the claim alone made this node central'
    monkeypatch.setattr(node_topology, '_central_claim_unproven', False,
                        raising=False)
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    assert node_topology.central_standing() == 'proven'


def test_a_pushed_bundle_lands_where_chat_reads_it(hie, central):
    prompts, blobs = central
    env = _envelope()
    resp = _push(hie, env)
    assert resp.status_code == 200, resp.get_json()
    for name, content in env['files'].items():
        assert (prompts / name).read_text(encoding='utf-8') == content
    stored = json.loads((blobs / f'{PID}.json').read_text(encoding='utf-8'))
    assert stored['owner'] == _id_of(OWNER)


def test_without_a_login_nothing_is_stored(hie, central):
    prompts, blobs = central
    assert _push(hie, _envelope(), token=None).status_code == 401
    assert _push(hie, _envelope(), token='not-a-login').status_code == 401
    assert list(prompts.iterdir()) == []
    assert not blobs.exists() or list(blobs.iterdir()) == []


def test_a_gate_pass_without_an_account_stores_nothing(hie, central):
    """An API-key caller is admitted by the gate but is nobody's account."""
    prompts, _ = central
    with hie.app.test_request_context('/prompts/sync', method='POST',
                                      json=_envelope()):
        g.auth_source = 'api_key'
        resp = hie.upload_recipe_bundle()
    status = resp[1] if isinstance(resp, tuple) else resp.status_code
    assert status == 401
    assert list(prompts.iterdir()) == []


def test_another_accounts_agent_is_not_overwritten(hie, central):
    prompts, _ = central
    first = _envelope()
    assert _push(hie, first).status_code == 200
    taken = _envelope(files={f'{PID}.json': '{"name":"Hijack"}'}, force=True,
                      uploaded_at=int(time.time()) + 60)
    assert _push(hie, taken, token=OTHER_TOKEN).status_code == 403
    assert (prompts / f'{PID}.json').read_text(encoding='utf-8') == \
        first['files'][f'{PID}.json']


def test_one_of_centrals_own_agents_is_not_overwritten(hie, central):
    prompts, _ = central
    (prompts / '42.json').write_text('{"name":"Speech Therapy"}', encoding='utf-8')
    resp = _push(hie, _envelope(pid='42', files={'42.json': '{"name":"Mine"}'}))
    assert resp.status_code == 403
    assert (prompts / '42.json').read_text(encoding='utf-8') == '{"name":"Speech Therapy"}'


def test_a_file_of_another_agent_inside_a_bundle_is_refused(hie, central):
    prompts, _ = central
    (prompts / '42.json').write_text('{"name":"Speech Therapy"}', encoding='utf-8')
    files = {f'{PID}.json': '{"name":"Coach"}', '42.json': '{"name":"Taken"}'}
    assert _push(hie, _envelope(files=files)).status_code == 400
    assert (prompts / '42.json').read_text(encoding='utf-8') == '{"name":"Speech Therapy"}'
    assert not (prompts / f'{PID}.json').exists()


def test_only_the_owner_pulls_the_bundle_back(hie, central):
    env = _envelope()
    assert _push(hie, env).status_code == 200
    assert _call(hie, 'GET', f'/prompts/sync/{PID}', OTHER_TOKEN).status_code == 404
    mine = _call(hie, 'GET', f'/prompts/sync/{PID}', OWNER_TOKEN)
    assert mine.status_code == 200
    assert mine.get_json()['files'] == env['files']


def test_the_owner_repushes_and_the_new_recipe_is_what_chat_reads(hie, central):
    prompts, _ = central
    assert _push(hie, _envelope()).status_code == 200
    newer = _envelope(files={f'{PID}.json': '{"name":"Coach v2"}',
                             f'{PID}_0_recipe.json': '{"actions":["greet","ask"]}'},
                      uploaded_at=int(time.time()) + 60)
    assert _push(hie, newer).status_code == 200
    assert (prompts / f'{PID}.json').read_text(encoding='utf-8') == '{"name":"Coach v2"}'
    assert (prompts / f'{PID}_0_recipe.json').read_text(encoding='utf-8') == \
        '{"actions":["greet","ask"]}'


def test_an_id_that_is_another_agents_file_stem_is_refused(hie, central):
    """rv_fcaee must-fix 1: an id such as 42_personality or <id>_0 names
    another agent's files; only a plain id is an agent's own."""
    prompts, _ = central
    (prompts / '42.json').write_text('{"name":"Speech Therapy"}', encoding='utf-8')
    stem = _envelope(pid='42_personality',
                     files={'42_personality.json': '{"tone":"taken"}'})
    assert _push(hie, stem, token=OTHER_TOKEN).status_code == 400
    assert not (prompts / '42_personality.json').exists()
    assert _push(hie, _envelope()).status_code == 200
    flow = _envelope(pid=f'{PID}_0', files={f'{PID}_0.json': '{"x":1}'})
    assert _push(hie, flow, token=OTHER_TOKEN).status_code == 400
    assert not (prompts / f'{PID}_0.json').exists()
    assert _call(hie, 'GET', '/prompts/sync/42_personality',
                 OWNER_TOKEN).status_code == 400


def test_an_older_push_is_refused_unless_forced(hie, central):
    prompts, _ = central
    now = int(time.time())
    assert _push(hie, _envelope(uploaded_at=now + 60)).status_code == 200
    older = _envelope(files={f'{PID}.json': '{"name":"Old"}'}, uploaded_at=now)
    assert _push(hie, older).status_code == 409
    assert (prompts / f'{PID}.json').read_text(encoding='utf-8') != '{"name":"Old"}'
    forced = _envelope(files={f'{PID}.json': '{"name":"Old"}'}, uploaded_at=now,
                       force=True)
    assert _push(hie, forced).status_code == 200
    assert (prompts / f'{PID}.json').read_text(encoding='utf-8') == '{"name":"Old"}'


def test_an_unchanged_repush_puts_missing_files_back(hie, central):
    prompts, _ = central
    env = _envelope()
    assert _push(hie, env).status_code == 200
    (prompts / f'{PID}_0_recipe.json').unlink()
    again = _push(hie, env)
    assert again.status_code == 200 and again.get_json().get('unchanged') is True
    assert (prompts / f'{PID}_0_recipe.json').exists()


def test_a_pull_without_a_login_is_refused(hie, central):
    assert _push(hie, _envelope()).status_code == 200
    assert _call(hie, 'GET', f'/prompts/sync/{PID}').status_code == 401


class _Pulled(BaseException):
    """Stops /chat at its pull: chat() swallows Exception there."""


def test_chats_pull_goes_to_the_route_with_its_login(hie, tmp_path, monkeypatch):
    """/chat for an agent this node lacks pulls it from the parent, as the
    turn's account: the route's parent and login, nothing else."""
    from integrations.social import sync_engine
    monkeypatch.delenv('HEVOLVE_NODE_TIER', raising=False)
    monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
    monkeypatch.setattr(hie, 'PROMPTS_DIR', str(tmp_path))
    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    asked, pulled = [], []
    monkeypatch.setattr(sync_engine, 'recipe_sync_route',
                        lambda uid: asked.append(uid) or ('https://central', 'login-x'))

    def pull(prompts_dir, pid, uid, central_url='', bearer=''):
        pulled.append((str(pid), uid, central_url, bearer))
        raise _Pulled()
    monkeypatch.setattr(recipe_sync, 'pull_recipe', pull)
    monkeypatch.setattr(hie, '_cleanup_stale_agents', lambda: None)
    with pytest.raises(_Pulled):
        hie.app.test_client().post('/chat', json={
            'user_id': '10202', 'prompt_id': 344, 'prompt': 'hello coach',
            'request_id': 'phone-r1-abc', 'draft_first': False,
            'casual_conv': False})
    assert asked == ['10202']
    assert pulled == [('344', '10202', 'https://central', 'login-x')]



def _nothing_stored(prompts, blobs):
    assert list(prompts.iterdir()) == []
    assert not blobs.exists() or list(blobs.iterdir()) == []


def test_a_first_push_takes_only_an_id_centrals_list_gives_this_account(
        hie, central, monkeypatch):
    """Review rv_e97b, must-fix 1 of 45c957c93: an id central had no files
    for was any login's to take.  Measured by the review: a stranger pushed
    agent 42 first and the owner's next turn ran the stranger's prompt."""
    prompts, blobs = central
    monkeypatch.setattr(recipe_sync, 'prompt_owner_email', lambda pid: OTHER,
                        raising=False)
    resp = _push(hie, _envelope())
    assert resp.status_code == 403, resp.get_json()
    _nothing_stored(prompts, blobs)


def test_a_first_push_is_refused_when_central_cannot_say_whose_it_is(
        hie, central, monkeypatch):
    prompts, blobs = central
    monkeypatch.setattr(recipe_sync, 'prompt_owner_email', lambda pid: None,
                        raising=False)
    assert _push(hie, _envelope()).status_code == 403
    _nothing_stored(prompts, blobs)


def test_a_pushed_agent_cannot_make_itself_a_system_agent(hie, central):
    prompts, blobs = central
    files = {f'{PID}.json': json.dumps({'name': 'Coach', 'flows': [],
                                        'is_system_agent': True})}
    resp = _push(hie, _envelope(files=files))
    assert resp.status_code == 400, resp.get_json()
    _nothing_stored(prompts, blobs)


def test_the_owner_lookup_reads_centrals_prompt_row_then_its_account(monkeypatch):
    """prompt_owner_email: the prompt row's user_id, then that account's
    email (core.profile_sync.fetch_central_profile), lower-cased; None when
    either is missing or the store cannot be reached."""
    import core.http_pool as http_pool
    import core.profile_sync as profile_sync
    asked = []

    def get(url, params=None, **_k):
        asked.append((url, params))
        rows = [{'prompt_id': 42, 'user_id': 10202}] if params == {
            'prompt_id': 42} else []
        return MagicMock(status_code=200, json=lambda: rows)

    monkeypatch.setattr('core.config_cache.get_central_db_url',
                        lambda: 'https://kong.example/db/')
    monkeypatch.setattr(http_pool, 'pooled_get', get)
    monkeypatch.setattr(profile_sync, 'fetch_central_profile',
                        lambda uid: {'email_address': 'Owner@Example.com'}
                        if str(uid) == '10202' else None)
    assert recipe_sync.prompt_owner_email('42') == OWNER
    assert asked[0] == ('https://kong.example/db/getprompt/', {'prompt_id': 42})
    assert recipe_sync.prompt_owner_email('43') is None

    def down(*_a, **_k):
        raise OSError('unreachable')
    monkeypatch.setattr(http_pool, 'pooled_get', down)
    assert recipe_sync.prompt_owner_email('42') is None


# ── no regression against the parent (review rv_e97b) ──────────────────

def test_create_returns_before_the_parent_answers(
        create_module, temp_prompts_dir, mock_flask_app, monkeypatch):
    """Finishing CREATE made up to 3 sequential calls to central, 13 s
    each, on the request thread; the parent made none."""
    from integrations.social import sync_engine
    cr = create_module
    (temp_prompts_dir / f'{PID}.json').write_text(json.dumps(AGENT))
    monkeypatch.setattr(cr, 'pooled_patch', lambda url, **_k: None)
    release, done = threading.Event(), threading.Event()

    def slow_sync(prompts_dir, pid, record, force=False):
        release.wait(10)
        done.set()
        return True
    monkeypatch.setattr(sync_engine, 'sync_agent_to_parent', slow_sync)
    started = time.monotonic()
    cr.update_agent_creation_to_db(PID)
    assert time.monotonic() - started < 2, 'CREATE waited for the parent'
    assert not done.is_set()
    release.set()
    assert done.wait(5), 'the finished agent was never sent'


@pytest.fixture
def real_push(monkeypatch, tmp_path):
    """sync_agent_to_parent with the real push and push cache, central's
    list and the parent's /prompts/sync recorded."""
    from integrations.social import sync_engine
    monkeypatch.setattr(recipe_sync, '_PUSH_CACHE_FILE',
                        str(tmp_path / 'push-cache.json'))
    monkeypatch.setattr(sync_engine, 'recipe_sync_route',
                        lambda uid: ('https://central', f'login-of-{uid}'))
    seen = {'listed': 0, 'posts': 0, 'ready': [], 'ready_ok': True}

    def list_(record, uid, login):
        seen['listed'] += 1
        return 344
    monkeypatch.setattr(sync_engine, 'list_agent_on_central', list_)

    def ready(cid, login):
        seen['ready'].append(cid)
        return seen['ready_ok']
    monkeypatch.setattr(sync_engine, 'mark_agent_ready_on_central', ready)

    def post(url, **_k):
        seen['posts'] += 1
        return MagicMock(status_code=200)
    monkeypatch.setattr('core.http_pool.pooled_post', post)
    (tmp_path / f'{PID}.json').write_text(json.dumps(AGENT))
    return sync_engine, seen


def test_an_unchanged_agent_is_not_sent_again(real_push, tmp_path):
    """Every finish listed the agent on central again though nothing had
    changed; the parent sent nothing at all."""
    sync_engine, seen = real_push
    assert sync_engine.sync_agent_to_parent(str(tmp_path), PID, AGENT)
    assert (seen['listed'], seen['posts']) == (1, 1)
    assert sync_engine.sync_agent_to_parent(str(tmp_path), PID, AGENT)
    assert (seen['listed'], seen['posts']) == (1, 1), 'sent again unchanged'
    (tmp_path / f'{PID}.json').write_text(json.dumps(dict(AGENT, goal='New')))
    assert sync_engine.sync_agent_to_parent(str(tmp_path), PID, AGENT)
    assert (seen['listed'], seen['posts']) == (2, 2), 'a change was not sent'
    assert sync_engine.sync_agent_to_parent(str(tmp_path), PID, AGENT,
                                            force=True)
    assert seen['listed'] == 3, 'force must send anyway'


def test_an_agent_central_kept_in_creation_mode_is_tried_again(
        real_push, tmp_path):
    sync_engine, seen = real_push
    seen['ready_ok'] = False
    sync_engine.sync_agent_to_parent(str(tmp_path), PID, AGENT)
    seen['ready_ok'] = True
    sync_engine.sync_agent_to_parent(str(tmp_path), PID, AGENT)
    assert seen['ready'] == [344, 344]
    assert seen['listed'] == 2
    sync_engine.sync_agent_to_parent(str(tmp_path), PID, AGENT)
    assert seen['listed'] == 2, 'settled, so not sent a third time'


@pytest.fixture
def flat(hie, tmp_path, monkeypatch):
    from security import key_delegation
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    # Stubbed: the real get_node_tier reads the master key from the
    # environment of a process that holds it.
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    for name in ('NUNBA_BUNDLED', 'HEVOLVE_API_KEY', 'HEVOLVE_REQUIRE_AUTH',
                 'TRUSTED_PROXY', 'NUNBA_CI', 'HEVOLVE_TRUST_KONG',
                 'HEVOLVE_CLOUD_MODE'):
        monkeypatch.delenv(name, raising=False)
    prompts = tmp_path / 'prompts'
    blobs = tmp_path / 'recipe_blobs'
    prompts.mkdir()
    monkeypatch.setattr(hie, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(hie, '_RECIPE_BLOB_DIR', str(blobs))
    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    return prompts, blobs


def test_a_flat_node_keeps_its_store_for_its_own_callers(hie, flat):
    """A flat or regional node's own callers name no account, so asking for
    one answered every push and pull with 401; the parent stored and served
    them.  It still never writes where /chat reads."""
    prompts, blobs = flat
    env = _envelope()
    resp = _push(hie, env, token=None)
    assert resp.status_code == 200, resp.get_json()
    assert json.loads((blobs / f'{PID}.json').read_text(
        encoding='utf-8'))['files'] == env['files']
    assert list(prompts.iterdir()) == []
    back = _call(hie, 'GET', f'/prompts/sync/{PID}')
    assert back.status_code == 200
    assert back.get_json()['files'] == env['files']


def test_a_regional_node_keeps_its_store_for_its_own_callers(
        hie, flat, monkeypatch):
    """Regional is not central: its own callers name no account and its
    store is the plain one.  (A change that counted regional as central
    passed every other test here, review rv_82eb.)"""
    from security import key_delegation
    prompts, blobs = flat
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'regional')
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'regional')
    env = _envelope()
    resp = _push(hie, env, token=None)
    assert resp.status_code == 200, resp.get_json()
    stored = json.loads((blobs / f'{PID}.json').read_text(encoding='utf-8'))
    assert stored['files'] == env['files'] and 'owner' not in stored
    assert list(prompts.iterdir()) == []
    back = _call(hie, 'GET', f'/prompts/sync/{PID}')
    assert back.status_code == 200
    assert back.get_json()['files'] == env['files']


# ── a central claim with no key proof ────────────────────────────────────
# Review rv_82eb of 82eb57889.  The API gate reads the bare claim
# HEVOLVE_NODE_TIER=central and asks every caller for a login; get_node_tier
# calls the same node flat.  82eb57889 gave that node the plain store behind
# central's gate: another account's push over the owner's agent answered 200
# and the owner's devices pulled it.  Two shipped start paths set the claim
# with no key (scripts/start_cloud.sh, deploy/distributed/
# docker-compose.distributed.yml).

@pytest.fixture
def unproven(hie, accounts, tmp_path, monkeypatch):
    """A node that sets the central claim while get_node_tier (stubbed, its
    calls counted) finds no key proof and answers flat."""
    from security import key_delegation
    asked = []
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    monkeypatch.setattr(key_delegation, 'get_node_tier',
                        lambda: asked.append(1) or 'flat')
    monkeypatch.setattr(node_topology, '_central_claim_unproven', False,
                        raising=False)
    for name in ('NUNBA_BUNDLED', 'HEVOLVE_API_KEY', 'HEVOLVE_REQUIRE_AUTH',
                 'TRUSTED_PROXY', 'NUNBA_CI', 'HEVOLVE_TRUST_KONG',
                 'HEVOLVE_CLOUD_MODE'):
        monkeypatch.delenv(name, raising=False)
    prompts = tmp_path / 'prompts'
    blobs = tmp_path / 'recipe_blobs'
    prompts.mkdir()
    blobs.mkdir()
    monkeypatch.setattr(hie, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(hie, '_RECIPE_BLOB_DIR', str(blobs))
    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    monkeypatch.setattr(recipe_sync, 'prompt_owner_email', lambda pid: OWNER,
                        raising=False)
    return prompts, blobs, asked


def _says_why_and_what_to_set(resp):
    body = resp.get_json()
    assert resp.status_code == 503, (resp.status_code, body)
    assert body['error'] == 'central_not_proven'
    # Why, and the two ways out.
    assert 'HEVOLVE_NODE_TIER=central' in body['reason']
    assert 'HEVOLVE_MASTER_PRIVATE_KEY' in body['reason']
    assert 'set the master key' in body['reason']
    assert 'remove' in body['reason'] and 'restart' in body['reason']


def test_an_unproven_central_refuses_the_owners_push_and_says_why(
        hie, unproven):
    """82eb57889 answered the owner 200 "stored" and wrote nothing where
    /chat reads, with no word of why."""
    prompts, blobs, _ = unproven
    resp = _push(hie, _envelope())
    _says_why_and_what_to_set(resp)
    assert 'stored' not in resp.get_json()
    assert list(prompts.iterdir()) == [] and list(blobs.iterdir()) == []


def test_an_unproven_central_never_lets_another_account_replace_a_bundle(
        hie, unproven):
    prompts, blobs, _ = unproven
    owners = dict(_envelope(uploaded_at=1000), owner=_id_of(OWNER))
    kept = json.dumps(owners)
    (blobs / f'{PID}.json').write_text(kept, encoding='utf-8')
    taken = _envelope(files={f'{PID}.json': '{"name":"Hijack"}'}, force=True,
                      uploaded_at=int(time.time()) + 60)
    resp = _push(hie, taken, token=OTHER_TOKEN)
    _says_why_and_what_to_set(resp)
    assert (blobs / f'{PID}.json').read_text(encoding='utf-8') == kept
    assert list(prompts.iterdir()) == []
    # Nor does any account read a bundle off this node, the owner included.
    for token in (OWNER_TOKEN, OTHER_TOKEN):
        back = _call(hie, 'GET', f'/prompts/sync/{PID}', token)
        _says_why_and_what_to_set(back)
        assert 'files' not in back.get_json()


def test_an_unproven_central_still_asks_for_a_login_first(hie, unproven):
    """The gate asks the same rule, and an unproven claim still needs a
    login: a caller with no login gets its 401 and learns nothing about the
    node."""
    assert _push(hie, _envelope(), token=None).status_code == 401
    assert _call(hie, 'GET', f'/prompts/sync/{PID}').status_code == 401


def test_an_unproven_claim_is_asked_and_logged_once(hie, unproven):
    """get_node_tier logs CRITICAL each time it finds the claim unproven;
    82eb57889 called it on every push and pull."""
    import logging
    _, _, asked = unproven
    lines = []

    class _Seen(logging.Handler):
        def emit(self, record):
            lines.append((record.levelno, record.getMessage()))

    seen = _Seen(level=logging.CRITICAL)
    node_topology.logger.addHandler(seen)
    try:
        for _ in range(3):
            _says_why_and_what_to_set(_push(hie, _envelope()))
            _says_why_and_what_to_set(
                _call(hie, 'GET', f'/prompts/sync/{PID}', OWNER_TOKEN))
    finally:
        node_topology.logger.removeHandler(seen)
    # The gate and the store each ask the one rule on every request; the
    # rule asks get_node_tier once.
    assert len(asked) == 1
    said = [m for level, m in lines if 'neither central nor flat' in m]
    assert len(said) == 1 and 'HEVOLVE_MASTER_PRIVATE_KEY' in said[0]


def test_one_rule_says_whether_this_node_is_central(monkeypatch):
    """Owner 11-22, "No parallel paths" (parallel-path audit H6): whether
    this node is central is one rule, as claimed AND as proven, beside the
    one for flat (node_topology.is_flat_node).  It was two: the API gate
    read the bare claim and /prompts/sync asked a rule of its own
    (hart_intelligence_entry._recipe_store_rule, 5ef03be7c).

    get_node_tier (stubbed, its answers counted) is asked only when the
    claim counts, as is_flat_node asks it: it warns on every call for a
    regional claim with no certificate, and the gate asks the rule on
    every request."""
    from security import key_delegation
    asked = []

    def tier(answer):
        return lambda: asked.append(answer) or answer

    monkeypatch.setattr(node_topology, '_central_claim_unproven', False,
                        raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    monkeypatch.setattr(key_delegation, 'get_node_tier', tier('central'))
    assert node_topology.central_standing() == 'proven'
    # The key alone proves central; get_node_tier sets the claim then.
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    assert node_topology.central_standing() == 'proven'
    monkeypatch.setattr(key_delegation, 'get_node_tier', tier('flat'))
    assert node_topology.central_standing() == ''
    monkeypatch.delenv('HEVOLVE_NODE_TIER')
    assert node_topology.central_standing() == ''
    asked.clear()
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'regional')
    assert node_topology.central_standing() == '' and asked == []
    # The claim with no proof: neither central nor flat, asked once.
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    for _ in range(3):
        assert node_topology.central_standing() == 'unproven'
    assert asked == ['flat']
    assert node_topology.is_flat_node() is False


def test_the_gate_and_the_store_ask_the_one_rule(hie, unproven, monkeypatch):
    """Review rv_82eb: one request was judged by two rules, the gate's bare
    claim and the store's own.  Both ask node_topology.central_standing
    now, so where the rule and the claim differ, the rule decides both."""
    prompts, blobs, _ = unproven
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    monkeypatch.setattr(node_topology, 'central_standing', lambda: 'unproven')
    assert _push(hie, _envelope(), token=None).status_code == 401, \
        'the gate read the claim, not the rule'
    _says_why_and_what_to_set(_push(hie, _envelope()))
    assert list(prompts.iterdir()) == [] and list(blobs.iterdir()) == []

    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    monkeypatch.setattr(node_topology, 'central_standing', lambda: '')
    env = _envelope()
    resp = _push(hie, env, token=None)
    assert resp.status_code == 200, (resp.status_code, resp.get_json())
    assert json.loads((blobs / f'{PID}.json').read_text(
        encoding='utf-8'))['files'] == env['files']
    assert list(prompts.iterdir()) == []


def test_dropping_the_claim_gives_the_plain_store_back(
        hie, unproven, monkeypatch):
    """The refusal belongs to the claim: a node no longer making it is a
    flat node, whatever this process answered before."""
    prompts, blobs, _ = unproven
    _says_why_and_what_to_set(_push(hie, _envelope()))
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    env = _envelope()
    resp = _push(hie, env, token=None)
    assert resp.status_code == 200, resp.get_json()
    assert json.loads((blobs / f'{PID}.json').read_text(
        encoding='utf-8'))['files'] == env['files']
    assert list(prompts.iterdir()) == []


def _kept_before_owners(blobs, env):
    blobs.mkdir(exist_ok=True)
    (blobs / f'{PID}.json').write_text(json.dumps(env), encoding='utf-8')


def test_a_bundle_kept_before_owners_is_its_proven_owners_to_push(
        hie, central, monkeypatch):
    """A bundle stored before bundles had owners answered its own owner 403
    on push.  Central's prompt list decides who may replace it; reading it
    is open (test_an_agent_with_no_owner_is_public_to_read)."""
    prompts, blobs = central
    old = _envelope(uploaded_at=int(time.time()) - 60)
    _kept_before_owners(blobs, old)
    mine = _call(hie, 'GET', f'/prompts/sync/{PID}', OWNER_TOKEN)
    assert mine.status_code == 200 and mine.get_json()['files'] == old['files']
    assert _push(hie, _envelope(), token=OTHER_TOKEN).status_code == 403
    assert list(prompts.iterdir()) == []
    resp = _push(hie, _envelope())
    assert resp.status_code == 200, resp.get_json()
    stored = json.loads((blobs / f'{PID}.json').read_text(encoding='utf-8'))
    assert stored['owner'] == _id_of(OWNER)
    assert (prompts / f'{PID}.json').exists()


# ── an owner's email stands only when it was proven (rv_5local of 799c7df82) ──

def _sign_up_by_password(email):
    """The account under ``email`` as /auth/register leaves it: a password
    beside an email that was typed, never proven.  Its HARTOS JWT."""
    from integrations.social.auth import generate_jwt
    with db_session() as db:
        row = db.query(User).filter(User.email == email).first()
        row.password_hash = 'typed-at-sign-up'
        uid = str(row.id)
    return uid, generate_jwt(uid, 'squatter')


def test_a_password_account_under_the_owners_email_cannot_take_the_agent(
        hie, central):
    """Measured by the review: an account that typed the owner's email at
    sign-up pushed an agent central had no files for and got 200.  Central's
    prompt list names the owner by email, so only a proven email may push as
    it, by the same rule a Hevolve login is refused under
    (auth.email_never_proven).  Reading a bundle kept before bundles had
    owners is open to every signed-in account, this one too: it is a public
    agent (owner, 2026-11-21; test_an_agent_with_no_owner_is_public_to_read).
    So this test checks pushes only."""
    prompts, blobs = central
    uid, jwt = _sign_up_by_password(OWNER)
    resp = _push(hie, _envelope(), token=jwt)
    assert resp.status_code == 403, resp.get_json()
    _nothing_stored(prompts, blobs)
    _kept_before_owners(blobs, _envelope())
    resp = _push(hie, _envelope(uploaded_at=int(time.time()) + 60), token=jwt)
    assert resp.status_code == 403, resp.get_json()
    # The same account and JWT, its email proven: the store takes it, so
    # the refusals above were the email rule's alone.
    with db_session() as db:
        db.query(User).filter(User.id == uid).first().password_hash = None
    resp = _push(hie, _envelope(uploaded_at=int(time.time()) + 60), token=jwt)
    assert resp.status_code == 200, resp.get_json()


# ── an agent with no owner is a public agent (owner, 2026-11-21) ─────────

def test_an_agent_with_no_owner_is_public_to_read(hie, central):
    """Owner 2026-11-21 on fcaee0425: agents stored before bundles had owners
    were locked to everyone (pull 404) - "we need to treat them no owners as
    public agent; we have public or private demarcation".  An agent with an
    owner is that owner's (private); one with no owner is public: every
    signed-in account reads it, as every caller could before owners."""
    prompts, blobs = central
    old = _envelope(uploaded_at=int(time.time()) - 60)
    _kept_before_owners(blobs, old)
    for token in (OTHER_TOKEN, OWNER_TOKEN):
        got = _call(hie, 'GET', f'/prompts/sync/{PID}', token)
        assert got.status_code == 200, (token, got.get_json())
        assert got.get_json()['files'] == old['files']
    # A sign-in is still needed, and reading never gives the right to replace.
    assert _call(hie, 'GET', f'/prompts/sync/{PID}').status_code == 401
    kept = (blobs / f'{PID}.json').read_bytes()
    taken = _envelope(files={f'{PID}.json': '{"name":"Hijack"}'}, force=True,
                      uploaded_at=int(time.time()) + 60)
    assert _push(hie, taken, token=OTHER_TOKEN).status_code == 403
    assert list(prompts.iterdir()) == []
    assert (blobs / f'{PID}.json').read_bytes() == kept


def test_an_agent_with_an_owner_stays_private_to_it(hie, central):
    """The other half of the demarcation: once an owner stored it, another
    account is not shown it."""
    assert _push(hie, _envelope()).status_code == 200
    assert _call(hie, 'GET', f'/prompts/sync/{PID}', OTHER_TOKEN).status_code == 404
    assert _call(hie, 'GET', f'/prompts/sync/{PID}', OWNER_TOKEN).status_code == 200


# ── a public agent is served only as a push today would take it ─────────
# Review of 0e8aa81d4, finding 1: a bundle kept before bundles had owners never
# passed today's push checks (upload_recipe_bundle), yet 0e8aa81d4 served it to
# every account, and the account's node wrote all of it (pull_recipe ->
# write_envelope_files; /chat pulls an agent it lacks the same way).

def _pull_as(hie, token, node_dir):
    """core.recipe_sync.pull_recipe on another node, as the account ``token``
    proves, its GET answered by central's real gate and route.  What that
    node keeps is the user-visible result."""
    def get(url, params=None, timeout=None, headers=None):
        resp = _call(hie, 'GET', url[len('https://central'):], token)
        return MagicMock(status_code=resp.status_code, json=resp.get_json)
    with patch('core.http_pool.get_http_session',
               return_value=MagicMock(get=get)):
        return recipe_sync.pull_recipe(str(node_dir), PID, '10202',
                                       central_url='https://central',
                                       bearer=token)


def _no_bundle_answer(hie):
    """What central answers for an agent it keeps no bundle of."""
    resp = _call(hie, 'GET', f'/prompts/sync/{PID}', OTHER_TOKEN)
    assert resp.status_code == 404
    return resp.get_json()


def test_a_public_agent_carrying_another_agents_file_is_not_served(
        hie, central, tmp_path):
    """Planted by the review: agent PID's bundle, kept with no owner, also
    carries agent 42's file.  Every account got it with 200, and the
    account's own agent 42 was overwritten on its node.  A push of it today
    is refused (belongs_to_prompt), so it is not served: the same 404 as no
    bundle, and nothing is written."""
    _, blobs = central
    missing = _no_bundle_answer(hie)
    files = {f'{PID}.json': json.dumps({'name': 'Coach', 'flows': []}),
             '42.json': json.dumps({'name': 'Taken'})}
    _kept_before_owners(blobs, _envelope(files=files))
    node = tmp_path / 'node'
    node.mkdir()
    (node / '42.json').write_text('{"name":"My own agent"}', encoding='utf-8')
    for token in (OTHER_TOKEN, OWNER_TOKEN):
        got = _call(hie, 'GET', f'/prompts/sync/{PID}', token)
        assert (got.status_code, got.get_json()) == (404, missing), \
            (token, got.status_code)
        assert _pull_as(hie, token, node) is False
        assert (node / '42.json').read_text(encoding='utf-8') == \
            '{"name":"My own agent"}'
        assert [p.name for p in node.iterdir()] == ['42.json']


def test_a_public_agent_that_claims_to_be_a_system_agent_is_not_served(
        hie, central, tmp_path):
    """Planted by the review: the bundle's own file says is_system_agent,
    which /chat gives the system seat, and the pulling node kept the flag.
    A push of it today is refused (claims_system_agent), so it is not
    served."""
    _, blobs = central
    missing = _no_bundle_answer(hie)
    files = {f'{PID}.json': json.dumps({'name': 'Coach', 'flows': [],
                                        'is_system_agent': True})}
    _kept_before_owners(blobs, _envelope(files=files))
    node = tmp_path / 'node'
    node.mkdir()
    for token in (OTHER_TOKEN, OWNER_TOKEN):
        got = _call(hie, 'GET', f'/prompts/sync/{PID}', token)
        assert (got.status_code, got.get_json()) == (404, missing), \
            (token, got.status_code)
        assert _pull_as(hie, token, node) is False
    assert list(node.iterdir()) == []


def test_a_clean_public_agent_reaches_another_accounts_node(
        hie, central, tmp_path):
    """The public half still holds: a bundle with no owner whose files are
    its agent's own reaches any signed-in account's node."""
    _, blobs = central
    old = _envelope()
    _kept_before_owners(blobs, old)
    assert _call(hie, 'GET', f'/prompts/sync/{PID}', OTHER_TOKEN).status_code == 200
    node = tmp_path / 'node'
    node.mkdir()
    assert _pull_as(hie, OTHER_TOKEN, node) is True
    for name, content in old['files'].items():
        assert (node / name).read_text(encoding='utf-8') == content
