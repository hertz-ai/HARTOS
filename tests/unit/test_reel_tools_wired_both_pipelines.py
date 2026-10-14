"""Her reel tools reach her agent in BOTH pipelines (plan WS-8b).

integrations/reel_studio/tools.register_reel_studio_tools is a goal-gated
Tier-2 family, wired into the two agent constructors the way the other
families are, in its own ``try`` after the shared Tier-2 one:

* her Reel Studio agent (``reel_studio`` in the turn's goal tags, which only
  her explicit creation hint stamps on a record) gets the schema on the
  Helper AND the Assistant, and execution on the Assistant and the group
  chat's Executor -- the canonical Tier-2 shape WS-9 adopted;
* every other agent gets execution only, so the model is not shown them but
  ``request_tools`` can attach them (test_reel_tools_discoverable.py);
* on the central node nothing is registered (amendment A8 d);
* the tools read the turn's request id from the running pipeline's own
  ``request_id_list``, when they run;
* a broken reel package costs no other family.

The agents are built by the REAL ``create_recipe.create_agents`` and
``reuse_recipe.create_agents_for_user`` with real autogen agents and no LLM or
network (the create-loop harness, tests/unit/test_create_loop_end_to_end.py,
as tests/unit/test_reuse_vlm_learning_files_its_action.py uses it).  The
``seats`` fixture here is the harness the other two WS-8b files import.

    python -m pytest tests/unit/test_reel_tools_wired_both_pipelines.py -q -p no:cacheprovider
"""
import ast
import json
import logging
import os
import sys
from types import SimpleNamespace

import pytest

from tests.unit.test_create_loop_end_to_end import (  # noqa: F401  (fixture)
    ACTIONS, PROMPT_ID, UP, USER_ID, create_env)

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
PIPELINES = ('create', 'reuse')
NEUTRAL_GOAL = 'Summarise three sources on local AI costs'
HER_GOAL = 'Edit my bridal makeup reels and send them to my phone'


def _record(goal_tags=None, goal=NEUTRAL_GOAL):
    record = {
        'personas': [{'name': 'Researcher',
                      'description': 'Collects and summarises sources.'}],
        'goal': goal, 'agent_name': 'E2E Researcher',
        'flows': [{'persona': 'Researcher', 'sub_goal': 'weekly summary',
                   'actions': list(ACTIONS)}],
    }
    if goal_tags is not None:
        record['goal_tags'] = goal_tags
    return record


@pytest.fixture
def seats(create_env, tmp_path, monkeypatch):
    """``build(pipeline, goal_tags=None, goal=...)`` writes the agent record
    and builds its agents with that pipeline's real constructor; returns the
    Helper, Assistant and Executor it built, the pipeline module and its
    ``request_id_list``."""
    env = create_env
    # Her desktop, whatever this box's environment says; a test below makes
    # it the central node.
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    # The reel jobs' once-per-process sweep is WS-9's to test
    # (test_reel_tools.py); building agents here must not run it against this
    # box's data dir.
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path / 'data'))
    from core import platform_paths
    platform_paths.reset_cache()
    from integrations.reel_studio import jobs
    monkeypatch.setattr(jobs, '_SWEPT', True)

    import hartos.reuse_recipe as rr
    monkeypatch.setattr(rr, 'PROMPTS_DIR', str(env.prompts))
    monkeypatch.setattr(rr, 'HAS_SIMPLEMEM', False)
    reuse_caches = ('user_tasks', 'recipes', 'final_recipe', 'request_id_list',
                    'user_agents', 'time_actions', 'user_ledgers')
    for name in reuse_caches:
        cache = getattr(rr, name, None)
        if cache is not None and hasattr(cache, '_loader'):
            monkeypatch.setattr(cache, '_loader', None)
        if cache is not None and hasattr(cache, 'clear'):
            cache.clear()
    if getattr(rr, 'get_production_backend', None) is not None:
        from agent_ledger.backends import InMemoryBackend
        monkeypatch.setattr(rr, 'get_production_backend',
                            lambda *a, **k: InMemoryBackend())
    (env.prompts / f'{PROMPT_ID}_0_recipe.json').write_text(json.dumps({
        'status': 'done', 'actions': [
            {'action_id': i, 'action': text, 'persona': 'Researcher',
             'can_perform_without_user_input': 'yes',
             'recipe': [{'steps': 'authored step %d' % i,
                         'tool_name': 'google_search'}]}
            for i, text in enumerate(ACTIONS, 1)]}), encoding='utf-8')

    def build(pipeline, goal_tags=None, goal=NEUTRAL_GOAL):
        (env.prompts / f'{PROMPT_ID}.json').write_text(
            json.dumps(_record(goal_tags, goal)), encoding='utf-8')
        with env.app.app_context():
            if pipeline == 'create':
                from hartos.helper import Action
                task = Action(list(ACTIONS))
                env.cr.user_tasks[UP] = task
                agents = env.cr.create_agents(USER_ID, task, PROMPT_ID)[6]
                return SimpleNamespace(
                    helper=agents['helper'], assistant=agents['assistant'],
                    executor=agents['executor'], module=env.cr,
                    request_ids=env.cr.request_id_list, built=None)
            built = rr.create_agents_for_user(USER_ID, PROMPT_ID)
        executor = next(a for a in built[2].agents if a.name == 'Executor')
        return SimpleNamespace(helper=built[4], assistant=built[0],
                               executor=executor, module=rr,
                               request_ids=rr.request_id_list, built=built)

    build.env = env
    yield build
    for name in reuse_caches:
        cache = getattr(rr, name, None)
        if cache is not None and hasattr(cache, 'clear'):
            cache.clear()
    platform_paths.reset_cache()


def schema(agent):
    """The tool names on an agent's LLM schema (what the model is shown)."""
    from core.agent_tool_menu import helper_tool_names
    return helper_tool_names(agent)


def runs(agent):
    """The tool names an agent can execute (autogen's function map)."""
    return set(getattr(agent, 'function_map', None) or {})


def reel_names():
    from integrations.reel_studio.tools import REEL_TOOL_NAMES
    return set(REEL_TOOL_NAMES)


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def logged(seats):
    """Every line the two constructors log: CREATE's agent_logger does not
    propagate, and REUSE logs through the Flask app's logger."""
    handler = _Records()
    loggers = [logging.getLogger('agent_logger'), seats.env.app.logger]
    for lg in loggers:
        lg.addHandler(handler)
    yield handler.lines
    for lg in loggers:
        lg.removeHandler(handler)


# ── her agent, any other agent, the central node ───────────────────────────

@pytest.mark.parametrize('pipeline', PIPELINES)
def test_her_reel_studio_agent_is_shown_the_family_and_runs_it(seats, pipeline):
    built = seats(pipeline, goal_tags=['reel_studio'], goal=HER_GOAL)
    reel = reel_names()
    assert len(reel) == 7
    assert reel <= schema(built.helper), (
        f'{pipeline}: her Reel Studio agent was built without the reel family '
        f'on the Helper\'s schema (missing {sorted(reel - schema(built.helper))})')
    assert reel <= schema(built.assistant), (
        f'{pipeline}: the Assistant carries no reel schema, so a reel_make it '
        f'proposes is free-formed as text autogen never runs')
    assert reel <= runs(built.assistant), pipeline
    assert reel <= runs(built.executor), (
        f'{pipeline}: the Executor cannot run the Assistant\'s own reel_make '
        f'call')
    assert not reel & runs(built.helper), 'the Helper proposes, it never runs'
    assert 'reel_studio' in built.assistant._hart_unlocked_tags


@pytest.mark.parametrize('pipeline', PIPELINES)
def test_any_other_agent_runs_them_but_is_not_shown_them(seats, pipeline):
    built = seats(pipeline)
    reel = reel_names()
    for seat in (built.helper, built.assistant, built.executor):
        assert not reel & schema(seat), (
            f'{pipeline}: an agent that is not her Reel Studio agent was shown '
            f'{sorted(reel & schema(seat))}: the family costs ~900 schema '
            f'tokens per seat and belongs to her agent only')
    assert reel <= runs(built.assistant), (
        f'{pipeline}: not registered for execution, so request_tools has '
        f'nothing to re-attach when this agent is asked for a reel')
    assert reel <= runs(built.executor), pipeline


@pytest.mark.parametrize('pipeline', PIPELINES)
def test_on_central_no_agent_carries_a_reel_tool(seats, monkeypatch, pipeline):
    """Amendment A8 d: on the central node reel_studio is inert.  Central's
    own commerce/marketing goals keep their families and no reel tool sits on
    any seat, not even execution-only for request_tools to re-attach."""
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    built = seats(pipeline, goal_tags=['reel_studio', 'news'], goal=HER_GOAL)
    reel = reel_names()
    for seat in (built.helper, built.assistant, built.executor):
        assert not reel & (schema(seat) | runs(seat)), (pipeline, seat.name)
    assert 'fetch_news_feeds' in schema(built.helper), (
        'central kept its other families')
    assert 'reel_studio' not in built.assistant._hart_unlocked_tags


# ── the turn the tools answer for ─────────────────────────────────────────

@pytest.fixture
def registrations(monkeypatch):
    """Every register_reel_studio_tools call the constructors make, passed on
    to the real one."""
    from integrations.reel_studio import tools
    real = tools.register_reel_studio_tools
    seen = []

    def spy(*args, **kwargs):
        seen.append((args, kwargs))
        return real(*args, **kwargs)
    monkeypatch.setattr(tools, 'register_reel_studio_tools', spy)
    return seen


@pytest.mark.parametrize('pipeline', PIPELINES)
@pytest.mark.parametrize('goal_tags, shown', [(['reel_studio'], True), (None, False)])
def test_the_tools_answer_for_the_turn_this_pipeline_is_running(
        seats, registrations, pipeline, goal_tags, shown):
    import hartos.create_recipe as cr
    import hartos.reuse_recipe as rr
    built = seats(pipeline, goal_tags=goal_tags, goal=HER_GOAL if shown else NEUTRAL_GOAL)
    assert len(registrations) == 1, f'{pipeline}: {len(registrations)} registrations'
    args, kwargs = registrations[0]
    assert args[0] is built.helper and args[1] is built.assistant
    assert str(args[2]) == str(USER_ID)
    assert str(kwargs['prompt_id']) == str(PROMPT_ID)
    assert kwargs['with_schema'] is shown
    assert kwargs['executor'] is built.executor
    other = rr.request_id_list if built.module is cr else cr.request_id_list
    getter = kwargs['request_id_getter']
    built.request_ids[UP] = 'turn-1'
    other[UP] = 'the-other-pipelines-turn'
    assert getter() == 'turn-1', (
        f'{pipeline}: the tools must read the turn from this pipeline\'s own '
        f'request_id_list')
    built.request_ids[UP] = 'turn-2'
    assert getter() == 'turn-2', 'read when the tool runs, not when it was built'


# ── a broken reel package ─────────────────────────────────────────────────

@pytest.mark.parametrize('pipeline', PIPELINES)
def test_a_broken_reel_package_costs_no_other_family(seats, logged, monkeypatch,
                                                     pipeline):
    """The reel branch has its own try, after the shared Tier-2 one: an
    import failure there is logged and leaves every other family as it was
    (here the news family of a news + reel_studio goal)."""
    reel = {'reel_make', 'reel_trending_songs', 'reel_check_caption',
            'reel_score_hooks', 'reel_beat_sheet', 'reel_skill', 'reel_voice'}
    monkeypatch.setitem(sys.modules, 'integrations.reel_studio.tools', None)
    built = seats(pipeline, goal_tags=['news', 'reel_studio'], goal=HER_GOAL)
    assert 'fetch_news_feeds' in schema(built.helper), (
        f'{pipeline}: the news family was lost to the reel import failure')
    assert 'fetch_news_feeds' in runs(built.assistant)
    for seat in (built.helper, built.assistant, built.executor):
        assert not reel & (schema(seat) | runs(seat)), seat.name
    assert any('Reel Studio tool loading FAILED' in line for line in logged), (
        f'{pipeline}: the failure was not logged at warning: an agent built '
        f'without her reel tools talks about reels without making one')


# ── test_source_guard: one reel branch, the same in both constructors ─────

_CONSTRUCTORS = {'create_recipe.py': 'create_agents',
                 'reuse_recipe.py': 'create_agents_for_user'}
_REEL_CALL = 'register_reel_studio_tools'


def _callee(call):
    f = call.func
    return f.id if isinstance(f, ast.Name) else getattr(f, 'attr', None)


def _calls(node):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call) and _callee(n)]


def _family_loaders(nodes):
    """Calls to a Tier-2 family loader (register_<family>_tools) in ``nodes``."""
    return [c for n in nodes for c in _calls(n)
            if c.func and _callee(c).startswith('register_')
            and _callee(c).endswith('_tools')]


def _gates_on_goal_tags(nodes):
    """True when ``nodes`` hold a ``<tag> in goal_tags`` test."""
    return any(isinstance(c, ast.Compare)
               and any(isinstance(op, ast.In) for op in c.ops)
               and any(isinstance(x, ast.Name) and x.id == 'goal_tags'
                       for x in c.comparators)
               for n in nodes for c in ast.walk(n))


def _reel_branch_problems(src, constructor):
    """What is wrong with ``constructor``'s reel branch, read from the AST:
    [] when it is one register_reel_studio_tools call, alone in its own try
    after the shared goal-gated Tier-2 try, that catches and does not
    re-raise, passing this pipeline's turn and its executor."""
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef) and n.name == constructor), None)
    if fn is None:
        return [f'{constructor} not found']
    calls = [c for c in _calls(fn) if _callee(c) == _REEL_CALL]
    if len(calls) != 1:
        return [f'{len(calls)} {_REEL_CALL} calls, expected 1']
    call = calls[0]
    tries = [t for t in ast.walk(fn) if isinstance(t, ast.Try)
             and any(call in list(ast.walk(s)) for s in t.body)]
    if not tries:
        return ['the reel registration is not inside a try']
    own = min(tries, key=lambda t: t.end_lineno - t.lineno)
    problems = []
    others = [c for c in _family_loaders(own.body) if _callee(c) != _REEL_CALL]
    if others:
        problems.append('the reel try also loads %s: one failure costs both'
                        % sorted({_callee(c) for c in others}))
    shared = [t for t in ast.walk(fn) if isinstance(t, ast.Try) and t is not own
              and _gates_on_goal_tags(t.body)
              and [c for c in _family_loaders(t.body) if _callee(c) != _REEL_CALL]]
    if len(shared) != 1:
        problems.append(f'{len(shared)} shared goal-gated Tier-2 tries, expected 1')
    elif not shared[0].end_lineno < own.lineno:
        problems.append('the reel try does not come after the shared Tier-2 try')
    for h in own.handlers:
        if not (isinstance(h.type, ast.Name) and h.type.id == 'Exception'):
            problems.append('the reel try does not catch Exception')
        if any(isinstance(n, ast.Raise) for n in ast.walk(h)):
            problems.append('the reel try re-raises')
    kw = {k.arg: k.value for k in call.keywords}
    if [getattr(a, 'id', None) for a in call.args] != ['helper', 'assistant', 'user_id']:
        problems.append('not called with (helper, assistant, user_id)')
    getter = kw.get('request_id_getter')
    body = getter.body if isinstance(getter, ast.Lambda) else None
    if not (isinstance(body, ast.Call) and _callee(body) == 'get'
            and getattr(body.func.value, 'id', None) == 'request_id_list'
            and [getattr(a, 'id', None) for a in body.args] == ['user_prompt']):
        problems.append('request_id_getter does not read '
                        'request_id_list.get(user_prompt) when called')
    if not _gates_on_goal_tags([kw['with_schema']] if 'with_schema' in kw else []):
        problems.append('with_schema is not a goal_tags test')
    if getattr(kw.get('executor'), 'id', None) != 'executor':
        problems.append('the group chat executor is not passed')
    if getattr(kw.get('prompt_id'), 'id', None) != 'prompt_id':
        problems.append('prompt_id is not passed')
    return problems


def _constructor_src(fname):
    with open(os.path.join(_ROOT, 'hartos', fname), encoding='utf-8') as fh:
        return fh.read()


def test_source_guard_reel_branch_parity():
    """Both constructors carry the same reel branch, in its own try after
    the shared Tier-2 one.  A source guard on top of the behavioural tests
    above, for the parity they cannot see: CREATE and REUSE drifting apart is
    how news and finance went missing from one pipeline before."""
    found = {}
    for fname, constructor in _CONSTRUCTORS.items():
        src = _constructor_src(fname)
        assert _reel_branch_problems(src, constructor) == [], fname
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == constructor)
        found[fname] = ast.dump(next(c for c in _calls(fn)
                                     if _callee(c) == _REEL_CALL))
    assert found['create_recipe.py'] == found['reuse_recipe.py'], (
        'the two constructors register the reel family differently')


def test_the_branch_guard_flags_a_reel_call_inside_the_shared_try():
    """Anti-vacuity for the guard above."""
    shared_only = (
        'def create_agents():\n'
        '    try:\n'
        "        if 'news' in goal_tags:\n"
        '            register_news_tools(helper, assistant, user_id)\n'
        '        register_reel_studio_tools(\n'
        '            helper, assistant, user_id, prompt_id=prompt_id,\n'
        '            request_id_getter=lambda: request_id_list.get(user_prompt),\n'
        '            with_schema=TAG in goal_tags, executor=executor)\n'
        '    except Exception as e:\n'
        '        log(e)\n')
    assert any('also loads' in p
               for p in _reel_branch_problems(shared_only, 'create_agents'))
    stale_turn = (
        'def create_agents():\n'
        '    try:\n'
        "        if 'news' in goal_tags:\n"
        '            register_news_tools(helper, assistant, user_id)\n'
        '    except Exception as e:\n'
        '        log(e)\n'
        '    rid = request_id_list.get(user_prompt)\n'
        '    try:\n'
        '        register_reel_studio_tools(\n'
        '            helper, assistant, user_id, prompt_id=prompt_id,\n'
        '            request_id_getter=lambda: rid,\n'
        '            with_schema=TAG in goal_tags, executor=executor)\n'
        '    except Exception as e:\n'
        '        log(e)\n')
    assert any('request_id_getter' in p
               for p in _reel_branch_problems(stale_turn, 'create_agents'))
    good = stale_turn.replace('lambda: rid', 'lambda: request_id_list.get(user_prompt)')
    assert _reel_branch_problems(good, 'create_agents') == []
