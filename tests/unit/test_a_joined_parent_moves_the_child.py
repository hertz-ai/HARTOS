"""A parent or therapist who joined a child's chat moves the child to another
of the child's flows, from their own chat.

Owner, 2026-11-04: "The parent and the therapist who joined that agent may
move the child to another flow by telling the agent in their own chat. No new
screen."  Design agreed with hartos-16 (2026-11-07): the child's side's
approval of a join is kept as a chat_join consent grant on the child's account
(a revoke ends the join; the privacy page does not list these rows yet); one
reader of it,
core.persona_registry.joined_chats, serves _resolve_session_for_user,
reuse_recipe.get_role and the move; the move's body is in core
(move_person_to_flow_impl) and reuse_recipe offers a thin tool, only to someone
who joined.

The real ConsentService on an in-memory database, the real core.flow_progress
store on a temp data dir, and for the tool the real create_agents_for_user.

    python -m pytest tests/unit/test_a_joined_parent_moves_the_child.py -q
"""
from __future__ import annotations

import json
import os
import sys

# Before anything imports integrations.social.models (test_consent_service.py).
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from tests.unit.test_create_loop_end_to_end import (  # noqa: E402,F401  (fixture)
    PROMPT_ID, UP, USER_ID, create_env)

CHILD, CHILD2, PARENT, THERAPIST, STRANGER, P, OTHER_P = (
    'u-child', 'u-child2', 'u-parent', 'u-therapist', 'u-stranger', 77, 78)
PERSONAS = ['Child', 'Parent', 'Therapist']
FLOWS = [{'flow_name': 'Name calling', 'persona': 'Child'},
         {'flow_name': 'Flash cards', 'persona': 'Child'},
         {'flow_name': 'Weekly report', 'persona': 'Parent'}]


@pytest.fixture
def pr(monkeypatch, tmp_path):
    """core.persona_registry with empty in-process maps, a fresh consent
    table and a temp flow_progress store."""
    import core.platform_paths as platform_paths
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path))
    import core.persona_registry as pr
    monkeypatch.setattr(pr, 'chat_joinees', {})
    monkeypatch.setattr(pr, 'agents_session', {})
    from integrations.social.models import Base, get_engine
    engine = get_engine()
    Base.metadata.create_all(engine)
    yield pr
    Base.metadata.drop_all(engine)


def _revoke(creator, joiner, prompt_id=P):
    """The child's side revokes the join (ConsentService.revoke_consent, as
    the consent API does): every active chat_join row of that joiner (one
    per approval)."""
    from integrations.social.consent_service import ConsentService
    from integrations.social.models import UserConsent, db_session
    with db_session() as db:
        scopes = [r.scope for r in db.query(UserConsent).filter(
            UserConsent.user_id == creator, UserConsent.consent_type == 'chat_join',
            UserConsent.agent_id == str(prompt_id), UserConsent.revoked_at.is_(None),
            UserConsent.scope.startswith(f'{joiner}#'))]
        for scope in scopes:
            ConsentService.revoke_consent(db, creator, 'chat_join', scope=scope,
                                          agent_id=str(prompt_id))
    assert scopes, 'nothing to revoke'


def _place(user_id):
    from core.flow_progress import current_flow
    return current_flow(user_id, P, FLOWS, 'Child')


def _used_the_agent(*children):
    from core.flow_progress import set_persona
    for child in children:
        set_persona(child, P, 'Child', PERSONAS)


# ── the join link ─────────────────────────────────────────────────────────

def test_an_allowed_join_is_kept_and_read_back(pr):
    assert pr.record_join(CHILD, P, PARENT) is True
    assert pr.joined_chats(PARENT, P) == [CHILD]
    assert pr.joined_chats(STRANGER, P) == []
    assert pr.joined_chats(PARENT, OTHER_P) == []


def test_allowing_the_same_join_twice_keeps_one_grant(pr):
    from integrations.social.models import UserConsent, db_session
    assert pr.record_join(CHILD, P, PARENT) is True
    assert pr.record_join(CHILD, P, PARENT) is True
    assert pr.joined_chats(PARENT, P) == [CHILD]
    with db_session(commit=False) as db:
        active = db.query(UserConsent).filter(
            UserConsent.consent_type == 'chat_join', UserConsent.granted == True,  # noqa: E712
            UserConsent.revoked_at.is_(None)).count()
    assert active == 1


def test_a_revoked_join_ends_it_even_while_this_process_remembers_it(pr):
    pr.record_join(CHILD, P, PARENT)
    pr.chat_joinees[PARENT] = {P: CHILD}
    _revoke(CHILD, PARENT)
    assert pr.joined_chats(PARENT, P) == []


def test_a_row_stamped_revoked_never_counts_even_if_it_still_says_granted(pr):
    """revoke_consent writes both granted=False and revoked_at; a row with
    only the stamp (an older writer, a hand edit) is still revoked, as every
    other consent reader treats it."""
    from datetime import datetime
    from integrations.social.models import UserConsent, db_session
    pr.record_join(CHILD, P, PARENT)
    with db_session() as db:
        for row in db.query(UserConsent).filter(UserConsent.user_id == CHILD,
                                                UserConsent.consent_type == 'chat_join'):
            row.revoked_at = datetime.utcnow()
            assert row.granted is True
    assert pr.joined_chats(PARENT, P) == []


def test_a_join_allowed_again_after_a_revoke_counts_again(pr):
    """hartos-16: consent rows are append-only and unique per scope, so with
    one row per joiner a second approval raised and the join was lost."""
    pr.record_join(CHILD, P, PARENT)
    _revoke(CHILD, PARENT)
    assert pr.joined_chats(PARENT, P) == []
    assert pr.record_join(CHILD, P, PARENT) is True
    assert pr.joined_chats(PARENT, P) == [CHILD]


def test_a_join_says_whether_it_was_kept(pr, monkeypatch):
    from integrations.social.consent_service import ConsentService

    def broken(*_a, **_k):
        raise RuntimeError('disk full')
    monkeypatch.setattr(ConsentService, 'grant_consent', staticmethod(broken))
    # The store is there but the grant was not written: not kept.
    assert pr.record_join(CHILD, P, PARENT) is False
    # No consent store on this node (central): process-only, as before.
    monkeypatch.setattr(pr, '_kept_joins', broken)
    assert pr.record_join(CHILD, P, PARENT) is None


def test_unreadable_grants_fall_back_to_this_process_joins(pr, monkeypatch):
    from integrations.social.consent_service import ConsentService

    def unreadable(*_a, **_k):
        raise RuntimeError('no consent table on this node')
    monkeypatch.setattr(ConsentService, 'granted_by', staticmethod(unreadable))
    pr.chat_joinees[PARENT] = {P: CHILD}
    assert pr.joined_chats(PARENT, P) == [CHILD]


def test_a_joiners_messages_reach_the_childs_session_through_the_grant(pr):
    entries = [{'agentInstanceID': f'com.hertzai.hevolve.chat.{P}.{CHILD}',
                'user_id': CHILD, 'role': 'Child', 'deviceID': 'something'}]
    pr.agents_session[f'{CHILD}_{P}'] = entries
    assert pr._resolve_session_for_user(PARENT, P) == ([], f'{PARENT}_{P}')
    pr.record_join(CHILD, P, PARENT)
    assert pr._resolve_session_for_user(PARENT, P) == (entries, f'{CHILD}_{P}')
    _revoke(CHILD, PARENT)
    assert pr._resolve_session_for_user(PARENT, P) == ([], f'{PARENT}_{P}')


# ── the move ──────────────────────────────────────────────────────────────

def test_a_joined_parent_moves_the_child_to_another_of_their_flows(pr):
    from core.flow_progress import read
    _used_the_agent(CHILD)
    pr.record_join(CHILD, P, PARENT)
    out = pr.move_person_to_flow_impl(PARENT, P, FLOWS, PERSONAS, 'flash cards')
    assert 'Flash cards' in out
    assert _place(CHILD) == 1
    assert read(CHILD, P)['places']['child']['by'] == PARENT


def test_someone_who_has_not_joined_moves_no_one(pr):
    _used_the_agent(CHILD)
    out = pr.move_person_to_flow_impl(STRANGER, P, FLOWS, PERSONAS, 'Flash cards')
    assert 'not joined' in out
    assert _place(CHILD) is None


def test_a_revoked_join_moves_no_one(pr):
    _used_the_agent(CHILD)
    pr.record_join(CHILD, P, PARENT)
    _revoke(CHILD, PARENT)
    out = pr.move_person_to_flow_impl(PARENT, P, FLOWS, PERSONAS, 'Flash cards')
    assert 'not joined' in out
    assert _place(CHILD) is None


def test_a_flow_of_another_persona_is_refused_and_the_childs_flows_are_listed(pr):
    _used_the_agent(CHILD)
    pr.record_join(CHILD, P, PARENT)
    out = pr.move_person_to_flow_impl(PARENT, P, FLOWS, PERSONAS, 'Weekly report')
    listed = out.split('their flows are:')[1]
    assert 'Name calling' in listed and 'Flash cards' in listed
    assert 'Weekly report' not in listed
    assert _place(CHILD) is None


def test_a_child_who_never_used_the_agent_is_not_moved(pr):
    from core.flow_progress import read
    pr.record_join(CHILD, P, PARENT)
    out = pr.move_person_to_flow_impl(PARENT, P, FLOWS, PERSONAS, 'Flash cards')
    assert 'has not used this agent' in out
    assert not read(CHILD, P).get('places')


def test_with_two_children_joined_the_joiner_is_asked_whose_flow(pr):
    _used_the_agent(CHILD, CHILD2)
    pr.record_join(CHILD, P, THERAPIST)
    pr.record_join(CHILD2, P, THERAPIST)
    out = pr.move_person_to_flow_impl(THERAPIST, P, FLOWS, PERSONAS, 'Flash cards')
    assert 'more than one' in out
    assert _place(CHILD) is None and _place(CHILD2) is None
    pr.move_person_to_flow_impl(THERAPIST, P, FLOWS, PERSONAS, 'Flash cards', person=CHILD2)
    assert _place(CHILD2) == 1
    assert _place(CHILD) is None


def test_naming_a_child_whose_chat_was_not_joined_is_refused(pr):
    _used_the_agent(CHILD, CHILD2)
    pr.record_join(CHILD, P, PARENT)
    out = pr.move_person_to_flow_impl(PARENT, P, FLOWS, PERSONAS, 'Flash cards', person=CHILD2)
    assert f"not joined {CHILD2}'s chat" in out
    assert _place(CHILD2) is None and _place(CHILD) is None


def test_a_childs_own_account_is_not_its_own_joiner(pr):
    _used_the_agent(CHILD)
    pr.record_join(CHILD, P, CHILD)
    out = pr.move_person_to_flow_impl(CHILD, P, FLOWS, PERSONAS, 'Flash cards')
    assert 'not joined' in out
    assert _place(CHILD) is None


# ── wiring in reuse_recipe ────────────────────────────────────────────────

@pytest.fixture
def rr(pr, monkeypatch, tmp_path):
    """The real reuse_recipe join and get_role, its in-process maps empty."""
    from flask import Flask
    from hartos import reuse_recipe as rr
    for name in ('agents_session', 'agents_roles', 'chat_joinees', 'temp_users'):
        monkeypatch.setattr(rr, name, {}, raising=False)
    prompt = tmp_path / f'{P}.json'
    prompt.write_text(json.dumps({'personas': [{'name': n} for n in PERSONAS],
                                  'flows': FLOWS}), encoding='utf-8')
    monkeypatch.setattr(rr.helper_fun, 'safe_prompt_path', lambda *_a, **_k: str(prompt))
    with Flask('move-test').app_context():
        yield rr


def test_an_allowed_join_keeps_the_childs_grant(rr, pr):
    rr.agents_session[f'{CHILD}_{P}'] = [{'agentInstanceID': 'x', 'user_id': CHILD,
                                          'role': 'Child', 'deviceID': 'something'}]
    assert rr._reuse_join_chat(PARENT, P, 'Parent', CHILD) == 'terminate'
    # Not from this process's map (pr.chat_joinees is empty): from the grant.
    assert pr.joined_chats(PARENT, P) == [CHILD]


def test_get_role_finds_a_joiners_role_through_the_grant(rr, pr):
    rr.agents_session[f'{CHILD}_{P}'] = [
        {'agentInstanceID': 'x', 'user_id': CHILD, 'role': 'Child', 'deviceID': 'something'},
        {'agentInstanceID': 'y', 'user_id': THERAPIST, 'role': 'Therapist', 'deviceID': 'something'}]
    assert rr.get_role(THERAPIST, P) == 'user'
    pr.record_join(CHILD, P, THERAPIST)
    assert rr.get_role(THERAPIST, P) == 'Therapist'


def _child_chat(rr):
    rr.agents_session[f'{CHILD}_{P}'] = [{'agentInstanceID': 'x', 'user_id': CHILD,
                                          'role': 'Child', 'deviceID': 'something'}]


def _approved_join_waiting(monkeypatch):
    """PARENT's join to CHILD's chat waits, and the child's side said yes."""
    from core.flow_progress import set_pending_join
    from integrations.agent_engine import person_ask
    import core.event_attribution as event_attribution
    monkeypatch.setattr(event_attribution, 'person_for', lambda uid, *_a, **_k: str(uid))
    monkeypatch.setattr(person_ask, 'ask_state', lambda *_a, **_k: {
        'status': 'answered', 'answer': {'decision': 'approve'}})
    set_pending_join(PARENT, P, CHILD, 'Parent', 'ask-1')


def test_a_join_that_cannot_be_kept_is_not_made_and_the_joiner_is_told(rr, monkeypatch):
    """hartos-16: the store reads fine but the grant was not written, so no
    reader would ever see the join; it must not be made half-way."""
    _child_chat(rr)
    _approved_join_waiting(monkeypatch)
    monkeypatch.setattr(rr, 'record_join', lambda *_a: False)
    out = rr._reuse_pending_join_turn(PARENT, P)
    assert 'could not be saved' in str(out)
    assert [m['user_id'] for m in rr.agents_session[f'{CHILD}_{P}']] == [CHILD]
    assert not rr.chat_joinees.get(PARENT)
    assert rr.get_role(PARENT, P) == 'user'


@pytest.mark.parametrize('word', ['yes', 'allow', 'approved'])
def test_only_the_cards_approve_lets_someone_join(rr, monkeypatch, word):
    """An approval card answers approve / deny / later; no other word is a
    yes (claude-autostart-6d, review of 2eb963781)."""
    from integrations.agent_engine import person_ask
    _child_chat(rr)
    _approved_join_waiting(monkeypatch)
    monkeypatch.setattr(person_ask, 'ask_state', lambda *_a, **_k: {
        'status': 'answered', 'answer': {'decision': word}})
    assert 'did not allow you' in str(rr._reuse_pending_join_turn(PARENT, P))
    assert not rr.chat_joinees.get(PARENT)


def test_a_flows_persona_is_read_from_that_flow(rr):
    """The run-start stamp and the finished-flow record both ask
    _reuse_flow_persona which persona a flow belongs to."""
    assert rr._reuse_flow_persona(P, 0) == 'Child'
    assert rr._reuse_flow_persona(P, 2) == 'Parent'
    assert rr._reuse_flow_persona(P, 7) is None


def test_a_join_on_a_node_without_a_consent_store_lasts_this_process(rr, monkeypatch):
    _child_chat(rr)
    _approved_join_waiting(monkeypatch)
    monkeypatch.setattr(rr, 'record_join', lambda *_a: None)
    assert rr._reuse_pending_join_turn(PARENT, P) is None
    assert rr.chat_joinees[PARENT] == {P: CHILD}


def test_an_allowed_join_to_a_chat_that_never_started_says_so(rr, monkeypatch):
    """The reply used to be dropped and the turn went on as if joined."""
    _approved_join_waiting(monkeypatch)
    out = rr._reuse_pending_join_turn(PARENT, P)
    assert 'start this chat first' in str(out)
    assert not rr.chat_joinees.get(PARENT)


@pytest.fixture
def build(create_env, tmp_path, monkeypatch):
    """create_agents_for_user with real autogen agents and no LLM, as
    tests/unit/test_reel_tools_wired_both_pipelines.py builds it."""
    env = create_env
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path / 'data'))
    from core import platform_paths
    platform_paths.reset_cache()
    from integrations.reel_studio import jobs
    monkeypatch.setattr(jobs, '_SWEPT', True)
    import hartos.reuse_recipe as rr
    monkeypatch.setattr(rr, 'PROMPTS_DIR', str(env.prompts))
    monkeypatch.setattr(rr, 'HAS_SIMPLEMEM', False)
    caches = ('user_tasks', 'recipes', 'final_recipe', 'request_id_list',
              'user_agents', 'time_actions', 'user_ledgers')
    for name in caches:
        cache = getattr(rr, name, None)
        if cache is not None and hasattr(cache, '_loader'):
            monkeypatch.setattr(cache, '_loader', None)
        if cache is not None and hasattr(cache, 'clear'):
            cache.clear()
    if getattr(rr, 'get_production_backend', None) is not None:
        from agent_ledger.backends import InMemoryBackend
        monkeypatch.setattr(rr, 'get_production_backend', lambda *a, **k: InMemoryBackend())
    (env.prompts / f'{PROMPT_ID}_0_recipe.json').write_text(json.dumps({
        'status': 'done', 'actions': [
            {'action_id': 1, 'action': 'say hello', 'persona': 'Researcher',
             'can_perform_without_user_input': 'yes',
             'recipe': [{'steps': 'say hello', 'tool_name': 'google_search'}]}]}),
        encoding='utf-8')

    def make(joined):
        monkeypatch.setattr(rr, 'joined_chats', lambda *_a, **_k: list(joined))
        with env.app.app_context():
            built = rr.create_agents_for_user(USER_ID, PROMPT_ID)
        return built[0], built[4], env, built
    yield make
    for name in caches:
        cache = getattr(rr, name, None)
        if cache is not None and hasattr(cache, 'clear'):
            cache.clear()
    platform_paths.reset_cache()


def test_the_move_tool_is_offered_only_to_someone_who_joined(build):
    from core.agent_tool_menu import helper_tool_names
    _assistant, helper, _env, _built = build([])
    assert 'move_person_to_flow' not in helper_tool_names(helper)
    _assistant, helper, _env, _built = build([CHILD])
    assert 'move_person_to_flow' in helper_tool_names(helper)


def test_the_move_tool_is_kept_when_the_schema_is_trimmed(build, monkeypatch):
    """hartos-16: no saved step names the tool, so the fit to the model's
    context deferred it first and the parent's words never reached it."""
    from types import SimpleNamespace
    import core.agent_tools as agent_tools
    import hartos.reuse_recipe as rr
    for joined, kept in (([], False), ([CHILD], True)):
        _assistant, _helper, env, built = build(joined)
        protected = []
        monkeypatch.setattr(agent_tools, 'fit_schema_to_ctx',
                            lambda _agent, protect=(), **_k: protected.append(set(protect)))
        rr.user_agents[UP] = built
        rr.user_tasks[UP] = SimpleNamespace(current_action=None)
        with env.app.app_context():
            rr._attach_named_tools_for_action(UP)
        assert len(protected) == 2, protected                      # Helper and Assistant
        assert all(('move_person_to_flow' in p) is kept for p in protected)


def test_the_move_tool_hands_the_agents_flows_and_the_joiner_to_the_move(build, monkeypatch):
    import hartos.reuse_recipe as rr
    assistant, _helper, env, _built = build([CHILD])
    seen = []
    monkeypatch.setattr(rr, 'move_person_to_flow_impl',
                        lambda *a: seen.append(a) or 'moved')
    with env.app.app_context():
        out = assistant.function_map['move_person_to_flow'](flow_name='weekly summary')
    assert out == 'moved'
    joiner, prompt_id, flows, personas, flow_name, person = seen[0]
    assert (joiner, prompt_id, flow_name, person) == (USER_ID, PROMPT_ID, 'weekly summary', None)
    record = json.loads((env.prompts / f'{PROMPT_ID}.json').read_text(encoding='utf-8'))
    assert flows == record['flows']
    assert personas == [p['name'] for p in record['personas']]
