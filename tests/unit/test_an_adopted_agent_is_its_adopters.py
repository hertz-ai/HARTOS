"""An agent the desktop's owner adopted is the owner's: listed and sent as his.

Owner, 2026-11-23, asked whether agents built on his desktop before sign-in
should become his: "yes adopt but consolidate similar functionality
agents".  Measured the same day, the identities that built them are not
guests but local accounts from the live proofs, so he approved adopting
exactly ONE agent: 1794540972213 "Tamil Speech Practice Agent", built by
the local account 1c1a4ec4 ("Dr. Kavitha"), to be his (10202) Wednesday
speech agent.

The adoption is a field, ``adopted_by``, on the agent's record, written by
integrations.social.sync_engine.adopt_agent; creator_user_id is kept, so
who built it stays on file, and removing the field undoes it.  One rule,
core.prompt_files.agent_account, says whose an agent is: the adopter, else
the builder.  It is read by the re-send of finished agents
(resend_unsettled_agents), by the send itself (sync_agent_to_parent, as
the adopter with the adopter's login) and by the agent list (/prompts).

The adoption refuses, and says why, when nobody is signed in, when the
agent is another Hevolve account's, when another account adopted it, or
when a phone on this desktop is allowed as the account that built it
(#306: such a phone acts here as its own account).  Central is never
called: the list, push and ready calls are recorded.
"""
import json
import os
import sys
import tempfile
from datetime import datetime

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

import pytest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core import prompt_files, recipe_sync  # noqa: E402
from integrations.social import sync_engine  # noqa: E402
from integrations.social.models import (  # noqa: E402
    Base, UserConsent, db_session, get_engine)

OWNER = '10202'
KAVITHA = '1c1a4ec4-7a6e-4382-be52-1196b005ed03'   # the local account that built it
PID = '1794540972213'


def _record(creator=KAVITHA, **extra):
    record = {'status': 'completed', 'name': 'Tamil Speech Practice Agent',
              'goal': 'Help <child_name> respond to their name in Tamil',
              'creator_user_id': creator, 'user_id': creator,
              'prompt_id': int(PID),
              'personas': [{'name': 'Child'}, {'name': 'Parent'},
                           {'name': 'Therapist'}],
              'flows': [{'flow_name': 'Name calling response',
                         'actions': ['call the name']}]}
    record.update(extra)
    return record


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """This desktop: its prompts dir with the agent and its flow recipe,
    the owner signed in, and the social DB."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    prompts = tmp_path / 'prompts'
    prompts.mkdir()
    (prompts / f'{PID}.json').write_text(json.dumps(_record(), indent=2),
                                         encoding='utf-8')
    (prompts / f'{PID}_0_recipe.json').write_text(
        json.dumps({'status': 'completed', 'actions': []}), encoding='utf-8')
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    monkeypatch.setattr(recipe_sync, '_PUSH_CACHE_FILE',
                        str(tmp_path / 'push-cache.json'))
    yield prompts
    Base.metadata.drop_all(engine)


def _on_disk(prompts, pid=PID):
    return json.loads((prompts / f'{pid}.json').read_text(encoding='utf-8'))


def _write(prompts, record, pid=PID):
    (prompts / f'{pid}.json').write_text(json.dumps(record), encoding='utf-8')


# ── the one rule ────────────────────────────────────────────────────────

def test_an_adopted_agent_is_its_adopters_and_keeps_its_builder():
    assert prompt_files.agent_account(_record(adopted_by=OWNER)) == OWNER
    assert prompt_files.agent_account(_record()) == KAVITHA
    assert prompt_files.agent_account({}) == ''
    assert prompt_files.agent_account(None) == ''


# ── the adoption ────────────────────────────────────────────────────────

def test_the_owner_adopts_the_agent_and_its_builder_stays_on_file(desk):
    before = _on_disk(desk)
    assert sync_engine.adopt_agent(str(desk), PID) == ''
    after = _on_disk(desk)
    assert after['adopted_by'] == OWNER
    assert after['creator_user_id'] == KAVITHA
    assert {k: v for k, v in after.items() if k != 'adopted_by'} == before, \
        'the adoption adds one field and changes nothing else'


def test_adopting_it_again_changes_nothing(desk):
    assert sync_engine.adopt_agent(str(desk), PID) == ''
    once = (desk / f'{PID}.json').read_bytes()
    assert sync_engine.adopt_agent(str(desk), PID) == ''
    assert (desk / f'{PID}.json').read_bytes() == once


def test_the_owners_own_agent_needs_no_adoption(desk):
    _write(desk, _record(creator=OWNER))
    assert sync_engine.adopt_agent(str(desk), PID) == ''
    assert 'adopted_by' not in _on_disk(desk)


def _refused(desk, why_words):
    before = (desk / f'{PID}.json').read_bytes() if (
        desk / f'{PID}.json').exists() else None
    why = sync_engine.adopt_agent(str(desk), PID)
    assert why, 'the adoption must be refused'
    for word in why_words:
        assert word in why, why
    if before is not None:
        assert (desk / f'{PID}.json').read_bytes() == before, \
            'a refused adoption writes nothing'
    return why


def test_nobody_signed_in_is_refused_and_says_so(desk, monkeypatch):
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID')
    _refused(desk, ['nobody is signed in', PID])


def test_an_agent_with_no_record_is_refused(desk):
    (desk / f'{PID}.json').unlink()
    _refused(desk, ['no record', PID])


def test_another_hevolve_accounts_agent_is_not_adopted(desk):
    _write(desk, _record(creator='10077'))
    _refused(desk, ['10077', PID])


def test_an_agent_another_account_adopted_is_not_taken(desk):
    _write(desk, _record(adopted_by='40021'))
    _refused(desk, ['40021', PID])


def test_a_phone_allowed_as_its_builder_keeps_it(desk):
    """#306: a phone the owner allowed on this desktop acts as the account
    it was allowed as, its own; that account's agents are not taken."""
    with db_session() as db:
        db.add(UserConsent(id='c1', user_id=OWNER, agent_id=None,
                           consent_type='device_access', scope='device:ab12',
                           granted=True, granted_at=datetime.utcnow(),
                           paired_user_id=KAVITHA))
    _refused(desk, ['phone', KAVITHA, PID])


def test_a_phone_no_longer_allowed_does_not_hold_it(desk):
    with db_session() as db:
        db.add(UserConsent(id='c2', user_id=OWNER, agent_id=None,
                           consent_type='device_access', scope='device:cd34',
                           granted=True, granted_at=datetime.utcnow(),
                           revoked_at=datetime.utcnow(),
                           paired_user_id=KAVITHA))
    assert sync_engine.adopt_agent(str(desk), PID) == ''


# ── sent and listed as the adopter's ────────────────────────────────────

def test_the_re_send_takes_it_for_its_adopter_only(desk, monkeypatch):
    sent = []
    monkeypatch.setattr(sync_engine, 'sync_agent_to_parent',
                        lambda d, pid, record, force=False: sent.append(
                            (pid, force)) or True)
    assert sync_engine.adopt_agent(str(desk), PID) == ''
    sync_engine.resend_unsettled_agents(str(desk), KAVITHA)
    assert sent == [], "the builder's pass must not send the owner's agent"
    sync_engine.resend_unsettled_agents(str(desk), OWNER)
    assert sent == [(PID, True)]


def test_it_is_listed_and_pushed_as_the_adopter(desk, monkeypatch):
    calls = {}
    monkeypatch.setattr(sync_engine, 'recipe_sync_route',
                        lambda uid: ('https://central', f'login-of-{uid}'))
    monkeypatch.setattr(sync_engine, 'central_login_for',
                        lambda uid, url: f'login-of-{uid}')

    def list_(record, uid, login):
        calls['listed'] = (uid, login)
        return 347
    monkeypatch.setattr(sync_engine, 'list_agent_on_central', list_)

    def push(prompts_dir, pid, user_id='', central_url='', force=False,
             bearer='', as_prompt_id=None):
        calls['pushed'] = (str(pid), user_id, bearer, as_prompt_id)
        return True
    monkeypatch.setattr(recipe_sync, 'push_recipe', push)
    monkeypatch.setattr(sync_engine, 'mark_agent_ready_on_central',
                        lambda cid, login: calls.setdefault('ready', (cid, login)))
    assert sync_engine.adopt_agent(str(desk), PID) == ''
    assert sync_engine.sync_agent_to_parent(str(desk), PID, _on_disk(desk),
                                            force=True)
    assert calls['listed'] == (OWNER, f'login-of-{OWNER}')
    assert calls['pushed'] == (PID, OWNER, f'login-of-{OWNER}', 347)
    assert calls['ready'] == (347, f'login-of-{OWNER}')


@pytest.fixture
def hie(desk, monkeypatch):
    import hart_intelligence_entry  # noqa: TID251 -- /prompts is on its app
    monkeypatch.setattr(hart_intelligence_entry, 'PROMPTS_DIR', str(desk))
    monkeypatch.setattr(hart_intelligence_entry, 'CENTRAL_DB_URL', '')
    return hart_intelligence_entry


def _listed(hie, user_id):
    resp = hie.app.test_client().get(f'/prompts?user_id={user_id}')
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return {str(p['prompt_id']): p for p in resp.get_json()}


def test_the_agent_list_shows_it_to_its_adopter_not_its_builder(hie, desk):
    assert sync_engine.adopt_agent(str(desk), PID) == ''
    mine = _listed(hie, OWNER)
    assert PID in mine, 'the desktop list must show the adopted agent'
    assert mine[PID]['user_id'] == OWNER
    assert PID not in _listed(hie, KAVITHA)


# ── its creator's rights are the adopter's ──────────────────────────────
# Review of ce1316fb4 (Medium): core.agent_tools.is_agent_creator still read
# creator_user_id, a second ownership rule beside agent_account, so for an
# adopted agent the owner's "save for everyone" memory, his game-sound
# choices and a resumed build's restore went by the old builder.

@pytest.fixture
def building(desk, tmp_path, monkeypatch):
    """The memory tools of one person building this agent, over
    core.cache_loaders' real files: the agent's record is read from this
    desktop's prompts dir, its stores live in a temporary folder."""
    from core import cache_loaders
    from core.agent_tools import build_core_tool_closures
    from core.session_cache import TTLCache
    stores = tmp_path / 'agent_data'
    stores.mkdir()
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(desk))
    monkeypatch.setattr(cache_loaders, '_agent_config_cache', {})
    monkeypatch.setattr(cache_loaders, 'AGENT_DATA_DIR', str(stores))

    class DiskHelper:
        def save_agent_data_to_file(self, store_id, store):
            return cache_loaders.save_agent_data(store_id,
                                                 store.get(store_id, {}))

        def load_agent_data_from_file(self, store_id, store):
            data = cache_loaders.load_agent_data(store_id)
            store[store_id] = data if data is not None else {}
            return data is not None

    def tools(user_id):
        ctx = {
            'building_agent': True, 'user_id': user_id, 'prompt_id': PID,
            'agent_data': TTLCache(ttl_seconds=7200, max_size=50,
                                   name='adopted_agent_data',
                                   loader=cache_loaders.load_agent_data),
            'helper_fun': DiskHelper(), 'user_prompt': f'{user_id}_{PID}',
            'request_id_list': {}, 'recent_file_id': None, 'scheduler': None,
            'send_message_to_user1': lambda *a, **k: None,
            'retrieve_json': json.loads,
            'strip_json_values': lambda d: d,
            'save_conversation_db': lambda *a, **k: None,
        }
        return {name: fn for name, _d, fn in build_core_tool_closures(ctx)}
    return tools


def _shared_store():
    from core import cache_loaders
    return cache_loaders.load_agent_data(PID)


def test_the_adopters_save_for_everyone_lands_in_the_agents_store(building,
                                                                  desk):
    from core.agent_tools import is_agent_creator
    assert sync_engine.adopt_agent(str(desk), PID) == ''
    out = building(OWNER)['save_data_in_memory'](
        'greeting_template', 'Vanakkam {child_name}', for_everyone=True)
    assert 'for everyone' in out, out
    assert _shared_store() == {'greeting_template': 'Vanakkam {child_name}'}
    assert is_agent_creator(OWNER, PID)


def test_its_old_builder_no_longer_saves_for_everyone(building, desk):
    from core.agent_tools import is_agent_creator
    assert sync_engine.adopt_agent(str(desk), PID) == ''
    out = building(KAVITHA)['save_data_in_memory'](
        'greeting_template', 'Hello', for_everyone=True)
    assert 'for this person only' in out, out
    assert _shared_store() is None
    assert not is_agent_creator(KAVITHA, PID)


def test_an_agent_nobody_adopted_is_still_its_builders(building, desk):
    """Guard: an agent with no adoption keeps the 2026-10-08 rule, its
    builder (creator_user_id) and nobody else."""
    from core.agent_tools import is_agent_creator
    assert 'for this person only' in building(OWNER)['save_data_in_memory'](
        'greeting_template', 'Hi', for_everyone=True)
    assert _shared_store() is None
    assert 'for everyone' in building(KAVITHA)['save_data_in_memory'](
        'greeting_template', 'Vanakkam', for_everyone=True)
    assert _shared_store() == {'greeting_template': 'Vanakkam'}
    assert is_agent_creator(KAVITHA, PID) and not is_agent_creator(OWNER, PID)
