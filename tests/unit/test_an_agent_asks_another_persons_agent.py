"""send_message_to_roles: one person's agent asks another person's agent.

Owner, 2026-11-12, rule 1: in a joined chat "the parent's agent asks the
child's agent"; the child's agent "answers from its own memory" and each
person's store stays sealed.

The tool used to publish the message to a topic nothing on a desktop listens
to and answer 'Message sent Successfully'.  Now the asked agent answers from
its saved notes about that person, by one call to the node's own model, and
the answer comes back as the tool's result.  Nothing reaches the other
person, their step, their ledger or their memory.

Who may be asked: owner, 2026-11-23T12:12:55Z, asked "When a parent or
therapist joins the child's chat, can the child's agent ask them, and their
agent, for what it needs?", chose "Yes, joining allows it": joining the chat
lets every member's agent ask the others, and the join card says so.  This
replaces the 11-22 default (494c91a08: a joiner asks about the chat's owner
only), which these tests pinned until then.  So every standing member, the
chat's owner or a joiner, is both an asker and someone asked about; joiner to
joiner too.  Never asked about: a join that was revoked, a join still waiting
for the owner's side, and the asker itself.  One resolver answers who that
is (core.persona_registry._others_in_the_chat).

The real persona registry, reuse_recipe.get_role, consent store (in-memory
database), flow_progress, agent-data files, ledger and step states; only the
HTTP post to the model, the audit-trail writer and the outbound-log writer
are recorders.

    python -m pytest tests/unit/test_an_agent_asks_another_persons_agent.py -q
"""
from __future__ import annotations

import copy
import json
import os
import sys
from types import SimpleNamespace

# Before anything imports integrations.social.models (test_consent_service.py).
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

CHILD, PARENT, THERAPIST, STRANGER, P = 'u-child', 'u-parent', 'u-therapist', 'u-stranger', 77
KEY = f'{CHILD}_{P}'
PERSONAS = ['Child', 'Parent', 'Therapist']
FLOWS = [{'flow_name': 'Name calling', 'persona': 'Child'},
         {'flow_name': 'Weekly report', 'persona': 'Parent'}]
CONFIG = {'name': 'Speech Coach', 'goal': 'Help Arjun answer to his name.',
          'personas': [{'name': 'Child', 'description': 'Arjun, four years old'},
                       {'name': 'Parent', 'description': "Arjun's mother"},
                       {'name': 'Therapist', 'description': 'his speech therapist'}],
          'flows': FLOWS}
ANSWER = 'He answered to his name three times today.'
NOTHING_KEPT = ('Nothing was sent to Child, and nothing in your message is kept '
                'for their next turn.')
QUESTION = 'How did Arjun do today?'


class _Reply:
    status_code = 200

    def __init__(self, answer):
        self._answer = answer

    def json(self):
        return {'choices': [{'message': {'content': json.dumps({'answer': self._answer})}}]}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Two modules, one set of in-process maps (as in the running node), a
    fresh consent table, and every store under a temp data dir."""
    from hartos import reuse_recipe as rr
    import core.cache_loaders as cl
    import core.http_pool as http_pool
    import core.llm_outbound_logger as outbound
    import core.persona_registry as pr
    import core.platform_paths as platform_paths
    import security.audit_trail as trail
    from hartos.threadlocal import thread_local_data
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path))
    sessions, joins = {}, {}
    for module in (pr, rr):
        monkeypatch.setattr(module, 'agents_session', sessions)
        monkeypatch.setattr(module, 'chat_joinees', joins)
    for name in ('user_agents', 'user_tasks', 'user_ledgers', 'request_id_list'):
        monkeypatch.setattr(rr, name, {})
    agent_dir, prompts = tmp_path / 'agent_data', tmp_path / 'prompts'
    agent_dir.mkdir()
    prompts.mkdir()
    prompt = prompts / f'{P}.json'
    prompt.write_text(json.dumps(CONFIG), encoding='utf-8')
    monkeypatch.setattr(cl, 'AGENT_DATA_DIR', str(agent_dir))
    monkeypatch.setattr(cl, 'PROMPTS_DIR', str(prompts))
    cl._agent_config_cache.pop(str(P), None)
    monkeypatch.setattr(rr.helper_fun, 'safe_prompt_path', lambda *_a, **_k: str(prompt))
    seen = SimpleNamespace(posts=[], audit=[], outbound=[], published=[], sends=[],
                           answer=ANSWER, post_error=None)

    def post(url, timeout=None, **kwargs):
        seen.posts.append(SimpleNamespace(url=url, timeout=timeout, body=kwargs.get('json'),
                                          resend=kwargs.get('resend')))
        if seen.post_error is not None:
            raise seen.post_error
        return _Reply(seen.answer)

    monkeypatch.setattr(http_pool, 'pooled_post', post)
    monkeypatch.setattr(trail, 'record', lambda kind, **f: seen.audit.append((kind, f)))
    monkeypatch.setattr(outbound, 'log_outbound',
                        lambda body, **f: seen.outbound.append((body, f)))
    monkeypatch.setattr(rr, 'send_message_to_user1', lambda *a, **k: seen.sends.append(a))
    monkeypatch.setattr(rr, 'publish_async', lambda *a, **k: seen.published.append(a))
    from integrations.social.models import Base, get_engine
    engine = get_engine()
    Base.metadata.create_all(engine)
    saved = thread_local_data.snapshot()
    thread_local_data.set_request_id('req-parent-1')
    thread_local_data.clear_model_config_override()
    with Flask('role-ask-test').app_context():
        yield SimpleNamespace(pr=pr, rr=rr, seen=seen, sessions=sessions, joins=joins,
                              tmp=tmp_path, agent_dir=agent_dir, tl=thread_local_data)
    thread_local_data.set_request_id(None)
    thread_local_data.clear_model_config_override()
    thread_local_data.adopt(saved)
    Base.metadata.drop_all(engine)
    cl._agent_config_cache.pop(str(P), None)


def uses(*people):
    """Each (user, persona) picked their persona at first use, and it is kept."""
    from core.flow_progress import set_persona
    for user, persona in people:
        assert set_persona(user, P, persona, PERSONAS) == persona


def childs_chat(env, *joiners):
    """The child's chat, and each (joiner, persona) let in by the child's side
    as reuse_recipe._reuse_join_chat leaves it: a kept grant and a member."""
    env.sessions[KEY] = [{'agentInstanceID': f'com.hertzai.hevolve.chat.{P}.{CHILD}',
                          'user_id': CHILD, 'role': 'Child', 'deviceID': 'something'}]
    for joiner, persona in joiners:
        assert env.pr.record_join(CHILD, P, joiner) is True
        env.sessions[KEY].append(
            {'agentInstanceID': f'com.hertzai.hevolve.chat.{P}.{joiner}',
             'user_id': joiner, 'role': persona, 'deviceID': 'something'})


def saved(env, user, notes):
    """What the agent saved about ``user`` (save_data_in_memory's file)."""
    (env.agent_dir / f'{user}_{P}_agent_data.json').write_text(
        json.dumps({'data': notes}), encoding='utf-8')


def revoke(creator, joiner):
    from integrations.social.consent_service import ConsentService
    from integrations.social.models import UserConsent, db_session
    with db_session() as db:
        scopes = [r.scope for r in db.query(UserConsent).filter(
            UserConsent.user_id == creator, UserConsent.consent_type == 'chat_join',
            UserConsent.agent_id == str(P), UserConsent.revoked_at.is_(None),
            UserConsent.scope.startswith(f'{joiner}#'))]
        for scope in scopes:
            ConsentService.revoke_consent(db, creator, 'chat_join', scope=scope,
                                          agent_id=str(P))
    assert scopes, 'nothing to revoke'


def ask(env, caller, role, message=QUESTION):
    return env.pr._send_message_to_roles_impl(
        caller, P, role, message,
        publish_fn=lambda *a, **k: env.seen.published.append(a))


def a_joined_parent(env, notes=None):
    uses((CHILD, 'Child'), (PARENT, 'Parent'))
    childs_chat(env, (PARENT, 'Parent'))
    saved(env, CHILD, {'name_response': 'answered 3 of 4 calls'} if notes is None else notes)


def prompt_of(env):
    assert len(env.seen.posts) == 1
    return env.seen.posts[0].body['messages'][0]['content']


def finishes_the_step(reply):
    from core.constants import tool_reply_failed
    from core.tool_refusal import refusal_has_hint, refusal_lets_step_finish
    assert tool_reply_failed(reply) and refusal_has_hint(reply), reply
    return refusal_lets_step_finish(reply, 'send_message_to_roles')


# ── the answer ────────────────────────────────────────────────────────────

def test_the_parents_agent_gets_the_childs_agents_answer(env):
    a_joined_parent(env)
    out = ask(env, PARENT, 'Child')
    assert out == (f"Answered from Speech Coach's notes about Child (user {CHILD}): "
                   f"{ANSWER}\n{NOTHING_KEPT}")
    said = prompt_of(env)
    assert 'answered 3 of 4 calls' in said and QUESTION in said
    # The asked person's own persona, and who asks, by their kept picks.
    assert 'One of the people you work with is Child (Arjun, four years old).' in said
    assert 'Parent, asks you about Child' in said


def test_a_parent_with_a_chat_of_their_own_still_reaches_the_child(env):
    """The old lookup read the caller's own chat first and never the joined
    one, so a parent who had one of their own found no Child."""
    a_joined_parent(env)
    env.sessions[f'{PARENT}_{P}'] = [{'agentInstanceID': 'x', 'user_id': PARENT,
                                      'role': 'Parent', 'deviceID': 'something'}]
    assert ask(env, PARENT, 'Child').startswith("Answered from Speech Coach's notes about Child")


def test_the_child_is_reached_after_this_process_forgot_their_chat(env):
    """The member lists live two hours; the grant and the kept picks do not
    end with them."""
    a_joined_parent(env)
    env.sessions.clear()
    assert ask(env, PARENT, 'Child').startswith("Answered from Speech Coach's notes about Child")


FATHER = 'u-father'
NAME_QUESTION = 'What does Arjun call his mother?'


def test_the_childs_agent_gets_the_joined_parents_agents_answer(env):
    """Owner 11-23: the child's agent asks the parent's agent.  RED on HEAD:
    refused 'not the chat joined' (the joiners of the asker's own chat were
    `held`, persona_registry._others_in_the_chat)."""
    a_joined_parent(env)
    saved(env, PARENT, {'family_members': ['Amma', 'Appa']})
    out = ask(env, CHILD, 'Parent', NAME_QUESTION)
    assert out == (f"Answered from Speech Coach's notes about Parent (user {PARENT}): "
                   f"{ANSWER}\nNothing was sent to Parent, and nothing in your message "
                   f"is kept for their next turn.")
    said = prompt_of(env)
    assert 'Amma' in said and NAME_QUESTION in said
    assert "One of the people you work with is Parent (Arjun's mother)." in said
    assert 'Child, asks you about Parent' in said


def test_the_parent_is_reached_after_this_process_forgot_the_childs_chat(env):
    """The member list (agents_session) lives two hours and is gone after a
    restart; who joined the child's chat is read from the chat_join grants on
    the child's account.  RED on HEAD."""
    a_joined_parent(env)
    saved(env, PARENT, {'family_members': ['Amma', 'Appa']})
    env.sessions.clear()
    assert ask(env, CHILD, 'Parent', NAME_QUESTION).startswith(
        f"Answered from Speech Coach's notes about Parent (user {PARENT})")


def test_a_joiner_asks_another_joiner(env):
    """'every member's agent ask the others': joiner to joiner too.  RED on
    HEAD (refused 'not the chat joined')."""
    uses((CHILD, 'Child'), (PARENT, 'Parent'), (THERAPIST, 'Therapist'))
    childs_chat(env, (PARENT, 'Parent'), (THERAPIST, 'Therapist'))
    saved(env, CHILD, {'name_response': 'answered 3 of 4 calls'})
    saved(env, THERAPIST, {'plan': 'two sessions a week'})
    out = ask(env, PARENT, 'Therapist', 'What is the plan?')
    assert out.startswith(
        f"Answered from Speech Coach's notes about Therapist (user {THERAPIST}): ")
    assert 'two sessions a week' in prompt_of(env)
    # The person whose chat they both joined is still theirs to ask about.
    assert ask(env, PARENT, 'Child').startswith('Answered from')


def test_a_joiner_reaches_the_other_joiner_after_this_process_forgot_the_chat(env):
    """RED on HEAD."""
    uses((CHILD, 'Child'), (PARENT, 'Parent'), (THERAPIST, 'Therapist'))
    childs_chat(env, (PARENT, 'Parent'), (THERAPIST, 'Therapist'))
    saved(env, THERAPIST, {'plan': 'two sessions a week'})
    saved(env, PARENT, {'family_members': ['Amma']})
    env.sessions.clear()
    assert ask(env, THERAPIST, 'Parent').startswith(
        f"Answered from Speech Coach's notes about Parent (user {PARENT})")
    assert ask(env, PARENT, 'Therapist').startswith(
        f"Answered from Speech Coach's notes about Therapist (user {THERAPIST})")


def test_the_childs_ask_of_the_parent_is_on_the_trail_for_both_sides(env):
    """RED on HEAD: one line, 'refused: not the chat joined'."""
    a_joined_parent(env)
    saved(env, PARENT, {'family_members': ['Amma']})
    ask(env, CHILD, 'Parent')
    assert [(f['user_id'], f['outcome']) for _k, f in env.seen.audit] == [
        (CHILD, 'answered'), (CHILD, 'answered'), (PARENT, 'answered'), (PARENT, 'answered')]
    for _kind, f in env.seen.audit:
        assert (f['from_user_id'], f['from_role'], f['to_user_id'], f['to_role']) == (
            CHILD, 'Child', PARENT, 'Parent')


# Who is never asked about: a revoked join, a join waiting for the owner's
# side, the asker itself, and a role two members share (named, never picked).

def test_a_revoked_member_is_not_asked_about_and_the_standing_one_is(env):
    """A member who left (their join revoked) is refused by name with the
    members who can be asked; the standing member is answered.  RED on HEAD
    (the child reached no member at all)."""
    uses((CHILD, 'Child'), (PARENT, 'Parent'), (THERAPIST, 'Therapist'))
    childs_chat(env, (PARENT, 'Parent'), (THERAPIST, 'Therapist'))
    saved(env, PARENT, {'family_members': ['Amma']})
    saved(env, THERAPIST, {'plan': 'two sessions a week'})
    revoke(CHILD, THERAPIST)
    out = ask(env, CHILD, 'Therapist')
    assert "No persona with role='Therapist'" in out and 'Parent' in out
    assert env.seen.posts == []
    assert ask(env, CHILD, 'Parent').startswith(
        f"Answered from Speech Coach's notes about Parent (user {PARENT})")
    assert 'two sessions' not in prompt_of(env)
    # Nor does the revoked member ask: their join no longer stands.
    env.seen.posts.clear()
    assert 'No persona' in ask(env, THERAPIST, 'Parent')
    assert env.seen.posts == []


def test_a_revoked_member_is_not_asked_about_after_this_process_forgot_the_chat(env):
    """The grant rows are the reader once the member list is gone: a revoked
    row reaches nobody, and the refusal is about the role.  RED on HEAD: the
    child's own chat, forgotten by this process, read as "No personas
    registered for session ... Run gather-requirements first" (the 'no
    session' branch reads the two-hour member list outside the resolver)."""
    a_joined_parent(env)
    saved(env, PARENT, {'family_members': ['Amma']})
    revoke(CHILD, PARENT)
    env.sessions.clear()
    out = ask(env, CHILD, 'Parent')
    assert "No persona with role='Parent'" in out and 'Amma' not in out
    assert env.seen.posts == []


def test_a_join_still_waiting_for_the_childs_side_is_not_asked_about(env):
    """Asked to join, not allowed yet: no grant, so not a member.  Guard:
    passes on HEAD and must stay green."""
    from core.flow_progress import set_pending_join
    uses((CHILD, 'Child'), (PARENT, 'Parent'))
    childs_chat(env)
    saved(env, PARENT, {'family_members': ['Amma']})
    set_pending_join(PARENT, P, CHILD, 'Parent', 'ask-pending-1')
    out = ask(env, CHILD, 'Parent')
    assert "No persona with role='Parent'" in out and 'Amma' not in out
    assert env.seen.posts == []
    # Nor does the one still waiting ask: they are not a member yet.
    assert 'Answered from' not in ask(env, PARENT, 'Child')
    assert env.seen.posts == []


def test_the_childs_agent_asking_about_the_child_is_the_own_role_refusal(env):
    """The asker itself is never another member.  Guard: passes on HEAD."""
    a_joined_parent(env)
    out = ask(env, CHILD, 'Child')
    assert "role='Child' is the person you are serving" in out
    assert finishes_the_step(out) and env.seen.posts == []


def test_a_role_two_members_share_is_refused_naming_both(env):
    """Both parents joined as Parent: which one is meant is the model's to
    say, never the first in a list.  RED on HEAD (refused, naming nobody)."""
    uses((CHILD, 'Child'), (PARENT, 'Parent'), (FATHER, 'Parent'))
    childs_chat(env, (PARENT, 'Parent'), (FATHER, 'Parent'))
    saved(env, PARENT, {'family_members': ['Amma']})
    saved(env, FATHER, {'family_members': ['Appa']})
    out = ask(env, CHILD, 'Parent')
    assert "role='Parent'" in out and PARENT in out and FATHER in out
    assert env.seen.posts == []


def test_nobody_joined_says_so_and_points_at_asking_the_person_served(env):
    """With no member to ask, the reply says so in as many words and names
    the existing way to ask the person in front of the agent (ask_person),
    so the model can choose it.  No new path.  RED on HEAD (the hint names
    no tool)."""
    uses((CHILD, 'Child'))
    childs_chat(env)
    saved(env, CHILD, {'name_response': 'answered'})
    out = ask(env, CHILD, 'Parent')
    assert "No persona with role='Parent' in session: no other person has joined" in out
    assert 'ask_person' in out
    assert finishes_the_step(out) and env.seen.posts == []


def test_a_join_kept_before_the_owners_word_covers_the_new_asks_unasked(env, monkeypatch):
    """Existing joins get the new scope (owner's choice + the 11-22 pilot
    ruling): no card is shown again and no grant written.  RED on HEAD."""
    a_joined_parent(env)
    saved(env, PARENT, {'family_members': ['Amma']})
    import integrations.agent_engine.person_ask as person_ask
    asked = []
    monkeypatch.setattr(person_ask, 'ask', lambda *a, **k: asked.append(a) or {
        'ok': False, 'refused': 'test'})
    for _ in range(3):
        assert ask(env, CHILD, 'Parent').startswith('Answered from')
    assert asked == []
    from integrations.social.models import UserConsent, db_session
    with db_session(commit=False) as db:
        assert db.query(UserConsent).filter(
            UserConsent.consent_type == 'chat_join').count() == 1


def test_an_agent_with_no_notes_says_so_and_asks_no_model(env):
    uses((CHILD, 'Child'), (PARENT, 'Parent'))
    childs_chat(env, (PARENT, 'Parent'))
    out = ask(env, PARENT, 'Child')
    assert out == (f"Speech Coach has no notes about Child (user {CHILD}) yet, so there is "
                   f"nothing to answer from. {NOTHING_KEPT}")
    assert env.seen.posts == []


def test_a_steer_is_told_that_nothing_is_kept(env):
    a_joined_parent(env)
    out = ask(env, PARENT, 'Child', 'Go slower with him tomorrow.')
    assert out.endswith(NOTHING_KEPT)


def test_the_persons_own_notes_come_before_what_the_agent_keeps_for_everyone(env):
    a_joined_parent(env, {'zz_today': 'turned at the second call'})
    (env.agent_dir / f'{P}_agent_data.json').write_text(
        json.dumps({'data': {'aa_templates': 'x' * 9000}}), encoding='utf-8')
    ask(env, PARENT, 'Child')
    assert 'turned at the second call' in prompt_of(env)
    assert 'the rest of the notes is not shown' in prompt_of(env)


# ── nothing reaches the other person ──────────────────────────────────────

def _a_child_mid_drill(env):
    """The child's session held on an ask-and-save step: a real ledger task
    blocked for their reply, its step state, pointer and group log."""
    from agent_ledger import create_ledger_from_actions
    from hartos.helper import Action
    from hartos import lifecycle_hooks as lh
    actions = [{'action_id': 1, 'action': 'Ask the child their name with '
                'send_message_to_user, then save_data_in_memory key name_response',
                'can_perform_without_user_input': 'no'},
               {'action_id': 2, 'action': 'Praise them', 'can_perform_without_user_input': 'yes'}]
    ledger = create_ledger_from_actions(user_id=CHILD, prompt_id=P, actions=actions, flow_id=0)
    lh.clear_action_states(KEY)
    lh.register_ledger_for_session(KEY, ledger)
    task = Action(actions)
    task.ledger = ledger
    env.rr.user_tasks[KEY] = task
    env.rr.user_ledgers[KEY] = ledger
    env.rr.request_id_list[KEY] = 'req-child-9'
    assert lh.safe_set_state(KEY, 1, lh.ActionState.ASSIGNED, 'test')
    assert lh.safe_set_state(KEY, 1, lh.ActionState.IN_PROGRESS, 'test')
    assert lh.record_ask_delivered(KEY, 1, 'req-child-9', 2, 0, via='send',
                                   question='What is your name?')
    assert lh.block_for_user_input(KEY, 1)
    log = SimpleNamespace(messages=[
        {'role': 'user', 'name': 'User', 'content': 'hello'},
        {'role': 'assistant', 'name': 'Assistant', 'content': 'What is your name?'}])
    env.rr.user_agents[KEY] = (None, None, log, None)
    assert lh.person_waits_on(KEY, 1, 0)
    return ledger, log


def _the_childs_side(env, ledger, log):
    from hartos import lifecycle_hooks as lh
    files = {}
    for folder, _dirs, names in os.walk(env.tmp):
        for name in names:
            path = os.path.join(folder, name)
            with open(path, 'rb') as f:
                files[os.path.relpath(path, env.tmp)] = f.read()
    return {
        'step states': copy.deepcopy(lh.action_states.get(KEY)),
        'pointer': env.rr.user_tasks[KEY].current_action,
        'ledger tasks': {k: json.dumps(t.to_dict(), sort_keys=True, default=str)
                         for k, t in ledger.tasks.items()},
        'files': files,
        'group log': copy.deepcopy(log.messages),
        'request': dict(env.rr.request_id_list),
        'waits for the child': lh.person_waits_on(KEY, 1, 0),
        'open ask': copy.deepcopy(lh.ledger_open_ask(KEY, 1, 0)),
        'answers': lh.recorded_answers(KEY, 1, 0),
        'held for a background turn': env.rr._reuse_held_step_gate(KEY, 'daemon_goal-1'),
        'held step': env.rr._reuse_held_action(KEY),
        'sends': list(env.seen.sends), 'published': list(env.seen.published),
    }


def test_an_ask_leaves_the_childs_step_ledger_memory_and_chat_as_they_were(env):
    a_joined_parent(env)
    ledger, log = _a_child_mid_drill(env)
    before = _the_childs_side(env, ledger, log)
    assert before['waits for the child'] is True and before['held step'] == 1
    assert before['answers'] == [] and before['open ask']['question'] == 'What is your name?'
    out = ask(env, PARENT, 'Child', 'His name is Arjun, tell him well done.')
    assert out.startswith("Answered from Speech Coach's notes about Child")
    after = _the_childs_side(env, ledger, log)
    for part in before:
        assert after[part] == before[part], f'the ask changed the child\'s {part}'
    # The parent's words and the reply are in no list a later turn of the
    # child's reads as the child's answer.
    assert all('Arjun, tell him' not in str(m.get('content')) and ANSWER not in str(m.get('content'))
               for m in log.messages)
    assert env.seen.sends == [] and env.seen.published == []


def test_the_message_is_published_to_no_topic(env):
    a_joined_parent(env)
    assert ask(env, PARENT, 'Child').startswith('Answered from')
    assert env.seen.published == []


# ── who may be asked about ────────────────────────────────────────────────

def test_a_join_turned_off_ends_the_asking_with_a_hint(env):
    a_joined_parent(env)
    env.joins[PARENT] = {P: CHILD}       # this process saw the join
    assert ask(env, PARENT, 'Child').startswith('Answered from')
    revoke(CHILD, PARENT)
    env.seen.posts.clear()
    out = ask(env, PARENT, 'Child')
    assert "No persona with role='Child' in session: their join to this chat was turned off" in out
    assert 'only once the join is allowed again' in out
    assert finishes_the_step(out) and env.seen.posts == []


def test_a_standing_join_is_never_asked_for_again(env, monkeypatch):
    a_joined_parent(env)
    import integrations.agent_engine.person_ask as person_ask
    asked = []
    monkeypatch.setattr(person_ask, 'ask_person', lambda *a, **k: asked.append(a),
                        raising=False)
    for _ in range(3):
        assert ask(env, PARENT, 'Child').startswith('Answered from')
    assert asked == []
    from integrations.social.models import UserConsent, db_session
    with db_session(commit=False) as db:
        assert db.query(UserConsent).filter(
            UserConsent.consent_type == 'chat_join').count() == 1


def test_a_wrong_role_is_refused_with_the_roles_that_can_be_asked(env):
    uses((CHILD, 'Child'), (PARENT, 'Parent'), (THERAPIST, 'Therapist'))
    childs_chat(env, (PARENT, 'Parent'), (THERAPIST, 'Therapist'))
    out = ask(env, PARENT, 'Teacher')
    assert "No persona with role='Teacher' in session." in out
    # Every other standing member, the other joiner too (owner 11-23).  RED
    # on HEAD: 'Child.' alone.
    assert 'Set role to one of the roles in this chat: Child, Therapist.' in out
    assert not finishes_the_step(out)        # a call that can be corrected
    assert env.seen.posts == []


def test_the_persons_own_role_is_refused(env):
    a_joined_parent(env)
    saved(env, PARENT, {'family_members': ['Amma']})
    out = ask(env, PARENT, 'Parent')
    assert "role='Parent' is the person you are serving" in out
    assert 'send_message_to_user' in out and 'or set role to one of: Child' in out
    assert finishes_the_step(out) and env.seen.posts == []


def test_a_role_a_list_files_under_the_callers_own_id_is_not_another_person(env):
    """A creator's member list holds every persona under the creator's id
    (register_persona_for_session): asking one of them is asking oneself."""
    uses((CHILD, 'Child'))
    env.pr.register_persona_for_session(CHILD, P, PERSONAS)
    saved(env, CHILD, {'name_response': 'answered'})
    out = ask(env, CHILD, 'Parent')
    assert "No persona with role='Parent' in session: no other person has joined" in out
    assert finishes_the_step(out) and env.seen.posts == []


def test_a_person_is_asked_about_by_their_own_role_not_the_ones_a_list_gives_them(env):
    """The child's kept pick is Child; the member list a CREATE left files
    every persona under the child's id, and that does not make the child's
    notes the therapist's."""
    uses((CHILD, 'Child'), (PARENT, 'Parent'))
    env.pr.register_persona_for_session(CHILD, P, ['Therapist', 'Parent', 'Child'])
    assert env.pr.record_join(CHILD, P, PARENT) is True
    saved(env, CHILD, {'name_response': 'answered 3 of 4 calls'})
    out = ask(env, PARENT, 'Therapist')
    assert "No persona with role='Therapist' in session." in out
    assert 'Set role to one of the roles in this chat: Child.' in out
    assert env.seen.posts == []
    assert ask(env, PARENT, 'Child').startswith('Answered from')


def test_a_person_with_nothing_on_this_computer_is_refused(env):
    """The parent joined a chat whose owner has nothing of this agent's
    here, so their role reads as the default; asked by it, there are still
    no notes on this computer to answer from."""
    uses((PARENT, 'Parent'))
    assert env.pr.record_join(CHILD, P, PARENT) is True
    out = ask(env, PARENT, 'user')
    assert f'user (user {CHILD}) is not on this computer' in out
    assert finishes_the_step(out) and env.seen.posts == []


def test_a_joined_chat_whose_owner_is_on_another_computer_is_refused(env):
    uses((PARENT, 'Parent'))
    assert env.pr.record_join(CHILD, P, PARENT) is True
    out = ask(env, PARENT, 'Child')
    assert (f"No persona with role='Child' in session on this computer: user {CHILD}, "
            f"whose chat with this agent is shared, is not on this computer") in out
    assert finishes_the_step(out) and env.seen.posts == []


# ── secrets stay in the other person's store ──────────────────────────────

SECRET_KEY = 'AKIAIOSFODNN7EXAMPLE'
# What the reviewer's probe of c53452437 found in the prompt unchanged, and
# the same credentials saved as a number or a list: key -> (saved value, the
# part that must not leave the person's store).
CREDENTIALS = {
    'wifi_password': ('Qw9!wifiAAA1', 'Qw9!wifiAAA1'),
    'login': ({'apiToken': 'zzTOKENzz9911'}, 'zzTOKENzz9911'),
    'note': (f'cloud key {SECRET_KEY} for backups', SECRET_KEY),
    'note_colon': ('tablet password: Zx8!colonBBB2', 'Zx8!colonBBB2'),
    'note_free': ('his tablet password is Vb7!freeCCC3', 'Vb7!freeCCC3'),
    'note_pin': ('Amma said the UPI PIN is 5567 today', '5567'),
    'note_otp': ('one-time code 880021 came by SMS', '880021'),
    'upi_pin': ('4821', '4821'),
    'atmPin': (7719, '7719'),
    'otp': ('771203', '771203'),
    'passcode': ('Xk9#mP2$qLz', 'Xk9#mP2$qLz'),
    'cvv': ('913', '913'),
    'card_number': ('4111 2222 3333 4444', '2222 3333'),
    'seed_phrase': ('abandon ability able about above absent absorb abstract',
                    'absorb abstract'),
    'recovery_phrase': (['zebra', 'quartz', 'violin'], 'quartz'),
    'parent_login': ('amma@example.com / Nm6!loginDDD4', 'Nm6!loginDDD4'),
    'note_login': ('the tablet login is arjun4 and Kp2!noteEEE5', 'Kp2!noteEEE5'),
}


def test_a_saved_secret_or_credential_reaches_neither_the_model_nor_the_reply(env):
    notes = {'name_response': 'answered 3 of 4 calls',
             'practice_note': 'spinning the pineapple card, then a compass game'}
    notes.update({key: value for key, (value, _part) in CREDENTIALS.items()})
    a_joined_parent(env, notes)
    out = ask(env, PARENT, 'Child')
    said = prompt_of(env)
    for key, (_value, part) in CREDENTIALS.items():
        assert part not in said, f'{key} reached the model'
        assert part not in out, f'{key} reached the reply'
    # What is no credential still reaches the answer, words that only look
    # like one ('spinning', 'pineapple', 'compass') included.
    assert 'answered 3 of 4 calls' in said and 'spinning the pineapple card' in said
    assert out.startswith('Answered from')


@pytest.mark.parametrize('key', sorted(CREDENTIALS))
def test_each_credential_alone_stays_in_the_persons_store(env, key):
    value, part = CREDENTIALS[key]
    a_joined_parent(env, {'name_response': 'answered 3 of 4 calls', key: value})
    out = ask(env, PARENT, 'Child')
    assert part not in prompt_of(env) and part not in out


def test_reading_notes_and_asking_a_person_answer_one_credential_list(env):
    """One list: what person_ask refuses to ask for is what is withheld from
    another person's agent."""
    import integrations.agent_engine.person_ask as person_ask
    import security.edge_privacy as edge_privacy
    assert person_ask._CREDENTIAL_WORDS is edge_privacy.CREDENTIAL_KEY_WORDS
    for word in edge_privacy.CREDENTIAL_KEY_WORDS:
        assert person_ask._names_a_secret(word)
        copy, n = edge_privacy.redact_secret_leaves({word: 'Zq7#value'}, credentials=True)
        assert (copy, n) == ({word: '[SECRET_REDACTED]'}, 1)


# rv_c534's review of 494c91a08: a speech lesson's notes, withheld whole
# because they hold the word 'pin' or 'login', so the asking agent answered
# "not in my notes" (6 of these 10 were withheld).
LESSON_NOTES = {
    'minimal_pairs': 'pin/bin, pat/bat: 3 of 4 right',
    'game_today': 'Played pin the tail on the donkey and said "pin" clearly',
    'word_list': ['pin', 'pan', 'pen'],
    'next_session': 'practise /p/ in pin, pot, pig',
    'tablet_note': 'could not login to the tablet, used paper cards',
    'craft': 'made a safety pin badge',
    'pass_ball': 'passed the ball and said pass',
    'spinning_top': 'spinning top game, named colours',
    'pin_drill_score': 7,
    'favourite_words': {'pin': 'loves it', 'cat': 'ok'},
}


def test_a_lessons_notes_are_read_whole():
    import security.edge_privacy as edge_privacy
    assert edge_privacy.redact_secret_leaves(LESSON_NOTES, credentials=True) == (
        LESSON_NOTES, 0)


def test_a_lessons_notes_reach_the_asking_agent(env):
    a_joined_parent(env, dict(LESSON_NOTES))
    out = ask(env, PARENT, 'Child')
    said = prompt_of(env)
    assert 'SECRET_REDACTED' not in said, said
    for text in ('pin/bin, pat/bat: 3 of 4 right', 'practise /p/ in pin, pot, pig',
                 'pin the tail on the donkey', 'could not login to the tablet',
                 'made a safety pin badge', 'loves it'):
        assert text in said, text
    assert out.startswith('Answered from')


@pytest.mark.parametrize('text', [
    'pin', 'ask amma for the pin', 'forgot the password.', 'the seed phrase',
    'he said pin, pan and pen: 3 of 3', '/p/ is the sound in pin words',
    'login/logout practice on the tablet', 'pin-up board with his drawings',
    'said otp for top, a swap of sounds'])
def test_a_credential_word_with_no_value_is_no_credential(text):
    import security.edge_privacy as edge_privacy
    assert edge_privacy.redact_secret_leaves({'note': text}, credentials=True) == (
        {'note': text}, 0)


@pytest.mark.parametrize('text', [
    'PIN 4821', 'pin is 4821', 'password is X', 'otp: 123456', 'pin=4821',
    'PIN-4821', 'pin - 4821', 'the pin for the tablet is 4821',
    'pin number is 48 21', 'pin is four eight two one', 'cvv 913',
    'card number 4111 2222 3333 4444', 'one time code was 880021',
    '4821 is his tablet PIN', 'Vb7!x is the wifi password',
    'wifi password sunshine', 'pwd=hunter2', 'passcode Xk9#mP2$qLz',
    'seed phrase abandon ability able', 'recovery phrase: zebra quartz violin',
    'login: amma / secret', 'login amma@example.com',
    'the login for the tablet is amma/Nm6', 'the tablet login is arjun'])
def test_a_credential_word_with_its_value_is_withheld_whole(text):
    import security.edge_privacy as edge_privacy
    assert edge_privacy.redact_secret_leaves({'note': [text]}, credentials=True) == (
        {'note': ['[SECRET_REDACTED]']}, 1)


@pytest.mark.parametrize('notes, kept', [
    ({'favourite_words': {'pin': 'loves it', 'cat': 'ok'}}, True),
    ({'pin': ['loves', 'it']}, True),
    ({'words': {'otp': {'said': 'top'}}}, True),
    ({'pin': 4821}, False), ({'pin': '48 21'}, False), ({'PIN': 4821.0}, False),
    ({'favourite_words': {'pin': '4821'}}, False),
    ({'pin': 'four eight two one'}, False), ({'pin': ['4', '8', '2', '1']}, False),
    ({'cvv': {'back': 913}}, False), ({'otp': '771203'}, False),
    # A longer name is a field named for a credential, and a bare word that is
    # no number word has no value shape to go by: withheld whatever they hold.
    ({'upi_pin': 'ask amma'}, False), ({'atmPin': 'ask amma'}, False),
    ({'pin_number': 'ask amma'}, False), ({'otp_code': 'by sms'}, False),
    ({'login': 'ask amma'}, False), ({'passcode': 'sunshine'}, False),
    ({'seed_phrase': ['zebra', 'quartz']}, False)])
def test_a_bare_number_word_key_is_read_by_its_value(notes, kept):
    """'pin' as a key is a credential field and an everyday entry of a words
    map.  Its credential is a number, so the value decides; every longer name
    and every other credential word stays withheld whole."""
    import security.edge_privacy as edge_privacy
    copy, n = edge_privacy.redact_secret_leaves(notes, credentials=True)
    assert (copy == notes and n == 0) is kept, (copy, n)
    assert ('[SECRET_REDACTED]' in json.dumps(copy)) is not kept, copy


def test_the_value_shapes_are_read_off_the_one_credential_list():
    import security.edge_privacy as edge_privacy
    for word in (edge_privacy._NUMBER_CREDENTIAL_WORDS
                 + edge_privacy._ACTION_CREDENTIAL_WORDS):
        assert word in edge_privacy.CREDENTIAL_KEY_WORDS, word
    assert not set(edge_privacy._NUMBER_CREDENTIAL_WORDS) & set(
        edge_privacy._ACTION_CREDENTIAL_WORDS)


def test_notes_that_cannot_be_cleared_of_secrets_are_not_read(env, monkeypatch):
    a_joined_parent(env, {'wifi_password': 'Tr0ub4dor&3'})
    import security.edge_privacy as edge_privacy

    def broken(_data):
        raise ImportError('no redactor')
    monkeypatch.setattr(edge_privacy, 'redact_secret_leaves', broken)
    out = ask(env, PARENT, 'Child')
    assert "notes about Child could not be cleared of secrets, so they were not read" in out
    assert finishes_the_step(out) and env.seen.posts == []


# ── the one model call ────────────────────────────────────────────────────

def test_the_call_goes_to_the_nodes_own_model_whatever_the_askers_turn_runs_on(env):
    from core.autogen_config import llm_http_target
    a_joined_parent(env)
    env.tl.set_model_config_override([{'model': 'expert-model', 'api_key': 'k2',
                                       'base_url': 'https://expert.example/v1'}])
    assert ask(env, PARENT, 'Child').startswith('Answered from')
    url, _headers, model = llm_http_target()
    call = env.seen.posts[0]
    assert (call.url, call.body['model']) == (url, model)
    assert call.body['model'] != 'expert-model' and 'expert.example' not in call.url
    assert call.timeout == 30.0 and call.resend is False     # sent once
    assert 'tools' not in call.body
    # Logged through the outbound log's own writer: one line, this call.
    assert [(body is call.body, fields.get('source'), fields.get('response_status'))
            for body, fields in env.seen.outbound] == [(True, 'llm_json.role_memory_answer', 200)]


NO_REPLY = ("Child's agent did not answer: the model gave no reply within 30 s of "
            "being asked, and it was asked once")


def test_a_model_that_does_not_answer_in_time_is_a_refusal(env):
    import requests
    a_joined_parent(env)
    env.seen.post_error = requests.exceptions.ReadTimeout('30 s')
    out = ask(env, PARENT, 'Child')
    assert NO_REPLY in out
    assert NOTHING_KEPT in out and finishes_the_step(out)
    assert len(env.seen.posts) == 1
    assert [fields.get('response_error') for _b, fields in env.seen.outbound] == ['ReadTimeout']


def test_an_empty_answer_is_a_refusal_not_an_answer(env):
    a_joined_parent(env)
    env.seen.answer = '   '
    out = ask(env, PARENT, 'Child')
    assert NO_REPLY in out and finishes_the_step(out)


def test_a_refusing_provider_is_not_called(env, monkeypatch):
    a_joined_parent(env)
    import integrations.agent_engine.dispatch as dispatch
    monkeypatch.setattr(dispatch, 'local_dispatch_provider_breaker_open',
                        lambda *a, **k: 'api.example')
    out = ask(env, PARENT, 'Child')
    assert "this computer's model provider is refusing calls" in out
    assert finishes_the_step(out) and env.seen.posts == []


# ── the audit trail ───────────────────────────────────────────────────────

def _lines(env):
    return [(f['user_id'], f['agent_role'], f['part'], f['direction'], f.get('text'),
             f['outcome']) for kind, f in env.seen.audit if kind == 'message']


def test_the_ask_and_the_answer_are_on_the_trail_for_both_sides(env):
    a_joined_parent(env)
    ask(env, PARENT, 'Child')
    assert _lines(env) == [
        (PARENT, 'Parent', 'ask', 'out', QUESTION, 'answered'),
        (PARENT, 'Parent', 'answer', 'in', ANSWER, 'answered'),
        (CHILD, 'Child', 'ask', 'in', QUESTION, 'answered'),
        (CHILD, 'Child', 'answer', 'out', ANSWER, 'answered')]
    for _kind, f in env.seen.audit:
        assert (f['agent_id'], f['request_id'], f['via'], f['actor']) == (
            P, 'req-parent-1', 'send_message_to_roles', 'agent')
        assert (f['from_user_id'], f['from_role'], f['to_user_id'], f['to_role']) == (
            PARENT, 'Parent', CHILD, 'Child')


def test_a_refused_ask_is_on_the_trail_with_its_reason(env):
    a_joined_parent(env)
    ask(env, PARENT, 'Teacher')
    assert _lines(env) == [(PARENT, 'Parent', 'ask', 'out', QUESTION, 'refused: no such role')]
    env.seen.audit.clear()
    import requests
    env.seen.post_error = requests.exceptions.ReadTimeout('30 s')
    ask(env, PARENT, 'Child')
    assert _lines(env) == [(PARENT, 'Parent', 'ask', 'out', QUESTION, 'refused: no answer'),
                           (CHILD, 'Child', 'ask', 'in', QUESTION, 'refused: no answer')]


# ── what the join card says ───────────────────────────────────────────────

def test_the_join_card_says_the_members_agents_ask_each_other(env):
    """The scope the owner chose is the one the card states: once joined,
    the agents of everyone in this chat can ask each other (and so this
    agent can ask the joiner and their agent too).  RED on HEAD: the card
    names one direction only."""
    said = env.rr._reuse_join_card(PARENT, P, 'Parent')['description']
    assert 'ask each other' in said, said
    assert 'move your chat to another of its flows' in said and 'for this agent only' in said
