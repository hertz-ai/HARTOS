""""Is this action autonomous?" has ONE rule: core.constants.action_is_autonomous.

(Re-exported by hartos.lifecycle_hooks, where its opposite, autonomy_needs_user,
also lives by re-export.  Both moved to core.constants in review of
cc1393825 so the A2A card reads them without importing hartos.helper.)

The question is asked of the recipe field ``can_perform_without_user_input``
and was answered with a private ``== 'yes'`` at every reader, each with its
own normalisation: a raw subscript compare in REUSE's state_transition and
timer state_transition1 and in CREATE's time_based_execution and timer
state_transition1, a strip().lower() compare in REUSE's session reader
(_reuse_action_is_autonomous), and five raw attribute compares on the A2A
TrainedAgent.  The opposite question already had one rule beside which this
one now lives: lifecycle_hooks.autonomy_needs_user (a leading 'no').

Behaviour is pinned for every value the banked recipes hold.  Census
2026-09-26 of ~/Documents/Nunba/data/prompts (1830 recipe files, action and
flow dicts): 'yes' 2991, missing 191, 'no' 162, None 12,
'no - requires specific dish constraints...' 3.  The review's census of the
same question (1994 'yes', 148 'no', 52 missing, 2 'no - ...') holds the same
four shapes.  For all of them the raw compare and the canonical rule agree,
so no reader changes its answer on data that exists.

The source guard at the bottom fails CI on a new private copy.
"""
import ast
import json
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_MISSING = object()

# (stored value, today's answer).  Every shape in the census above.
CENSUS = [
    ('yes', True),
    ('no', False),
    (_MISSING, False),
    (None, False),
    ('no - requires specific dish constraints, which the user must give', False),
]
_IDS = ['yes', 'no', 'missing', 'None', 'no-with-reason']


def _action(value):
    a = {'action_id': 1, 'action': 'do the thing', 'recipe': []}
    if value is not _MISSING:
        a['can_perform_without_user_input'] = value
    return a


# ── the rule itself ─────────────────────────────────────────────────────

@pytest.mark.parametrize('value,autonomous', CENSUS, ids=_IDS)
def test_the_rule_answers_every_census_value_as_today(value, autonomous):
    from core.constants import action_is_autonomous
    assert action_is_autonomous(
        _action(value).get('can_perform_without_user_input')) is autonomous


@pytest.mark.parametrize('value', [' Yes ', 'YES', 'yes\n'])
def test_the_rule_reads_yes_as_the_session_reader_always_did(value):
    """REUSE's session reader already stripped and lower-cased before its
    compare; the rule keeps that, so no reader got stricter.  (No banked
    recipe holds such a value today; the raw-compare sites would have said
    False for it.)"""
    from hartos.lifecycle_hooks import action_is_autonomous
    assert action_is_autonomous(value) is True


@pytest.mark.parametrize('value,autonomous', CENSUS, ids=_IDS)
def test_autonomous_and_needs_user_never_both_hold(value, autonomous):
    from hartos.lifecycle_hooks import action_is_autonomous, autonomy_needs_user
    v = _action(value).get('can_perform_without_user_input')
    assert not (action_is_autonomous(v) and autonomy_needs_user(v))


# ── REUSE's session reader ──────────────────────────────────────────────

@pytest.mark.parametrize('value,autonomous', CENSUS, ids=_IDS)
def test_reuse_session_reader_answers_every_census_value_as_today(
        monkeypatch, value, autonomous):
    pytest.importorskip('autogen', reason='autogen not installed')
    from hartos import reuse_recipe as rr
    monkeypatch.setattr(rr, 'user_tasks', {'u_1': rr.Action([_action(value)])})
    assert rr._reuse_action_is_autonomous('u_1', 1) is autonomous


# ── the A2A registry (five readers of the flow-level field) ────────────

@pytest.mark.parametrize('value,autonomous', CENSUS, ids=_IDS)
def test_a2a_agent_card_answers_every_census_value_as_today(
        tmp_path, monkeypatch, value, autonomous):
    from integrations.google_a2a.dynamic_agent_registry import (
        DynamicAgentDiscovery)
    recipe = {'persona': 'clerk', 'action': 'file it', 'recipe': [],
              'status': 'done'}
    if value is not _MISSING:
        recipe['can_perform_without_user_input'] = value
    (tmp_path / '71_0_recipe.json').write_text(json.dumps(recipe),
                                               encoding='utf-8')
    disc = DynamicAgentDiscovery(prompts_dir=str(tmp_path))
    assert disc.discover_all_agents() == 1
    agent = disc.get_agent_by_id('71_0')

    assert disc.get_agent_skills(agent)[0]['metadata']['autonomous'] is autonomous
    assert ('Can operate autonomously.'
            in disc.get_agent_description(agent)) is autonomous

    from integrations.google_a2a import register_dynamic_agents as reg
    monkeypatch.setattr(reg, 'get_dynamic_discovery', lambda: disc)
    info = reg.get_registered_agent_info()
    assert (info['autonomous_agents'] == ['71_0']) is autonomous


def test_lifecycle_hooks_reexports_the_same_two_rules():
    """One definition, two import paths: callers that import from
    hartos.lifecycle_hooks get the core.constants objects, not copies."""
    import core.constants as cc
    import hartos.lifecycle_hooks as lh
    assert lh.action_is_autonomous is cc.action_is_autonomous
    assert lh.autonomy_needs_user is cc.autonomy_needs_user


# ── CREATE should_continue_autonomously (review of cc1393825) ──────────

def _lift_should_continue(user_tasks):
    """create_recipe cannot be imported in a bare pytest env (its import
    waits on live services), so the function is lifted by name with ast and
    exec'd with its collaborators injected, as the create tests do."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from core.constants import autonomy_needs_user
    path = os.path.join(_ROOT, 'hartos', 'create_recipe.py')
    src = open(path, encoding='utf-8').read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef)
              and n.name == 'should_continue_autonomously')
    ns = {'user_tasks': user_tasks, 'autonomy_needs_user': autonomy_needs_user,
          'TaskStatus': SimpleNamespace(IN_PROGRESS='in_progress'),
          'current_app': SimpleNamespace(logger=MagicMock())}
    exec(ast.get_source_segment(src, fn), ns)
    return ns['should_continue_autonomously']


def _ledger_with_next(context, blocked_reason=None):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    task = SimpleNamespace(context=context, blocked_reason=blocked_reason,
                           description='next', task_id='action_2')
    ledger = MagicMock()
    ledger.get_next_executable_task.return_value = task
    return {'u_1': SimpleNamespace(ledger=ledger)}


# This reader asks the OTHER question: does the value say the action needs
# the user?  Only a leading 'no' does.  A missing value never blocks a
# continuation (it did not before, and must not start to).  The string 'no'
# used to be truthy here, so it did NOT block; that was the defect.
_CONTINUE = [
    ('yes', True),
    ('no', False),
    (_MISSING, True),
    (None, True),
    ('no - requires specific dish constraints, which the user must give', False),
]


@pytest.mark.parametrize('value,continues', _CONTINUE, ids=_IDS)
def test_create_continuation_asks_the_needs_user_rule(value, continues):
    ctx = {} if value is _MISSING else {'can_perform_without_user_input': value}
    should_continue = _lift_should_continue(_ledger_with_next(ctx))
    assert should_continue('u_1') is continues


def test_create_continuation_still_stops_on_input_required():
    should_continue = _lift_should_continue(
        _ledger_with_next({'can_perform_without_user_input': 'yes'},
                          blocked_reason='input_required'))
    assert should_continue('u_1') is False


# ── the A2A card reads the rule without importing the pipeline ──────────

def test_a2a_card_reads_autonomy_without_importing_hartos_helper():
    """Review of cc1393825: TrainedAgent.is_autonomous imported
    hartos.lifecycle_hooks, which pulls hartos.helper (autogen, langchain):
    7.35 s cold on the first card read.  Measured in a fresh interpreter."""
    import subprocess
    code = (
        "import sys, time\n"
        "from integrations.google_a2a.dynamic_agent_registry import TrainedAgent\n"
        "a = TrainedAgent('71_0', 71, 0, 'p', 'a', [], 'done', 'yes', '', {}, 'f')\n"
        "t = time.perf_counter(); v = a.is_autonomous\n"
        "dt = time.perf_counter() - t\n"
        "print(v, 'hartos.helper' in sys.modules,\n"
        "      'hartos.lifecycle_hooks' in sys.modules, round(dt, 3))\n")
    out = subprocess.run([sys.executable, '-c', code], cwd=_ROOT,
                         capture_output=True, text=True, timeout=300)
    last = out.stdout.strip().splitlines()[-1].split()
    assert last[:3] == ['True', 'False', 'False'], (out.stdout, out.stderr)
    assert float(last[3]) < 0.5, last


# ── source guard: no private copy of either answer ─────────────────────

_VOCAB = ('can_perform_without_user_input', 'autonom')
_SKIP_DIRS = {'venv', '.venv', 'venv311', 'node_modules', '.git', 'tests',
              'build', 'dist', '__pycache__', '.claude', 'python-embed',
              'site-packages', '.cache'}
# The two rules themselves, and nothing else.
_OWNERS = {('core/constants.py', 'action_is_autonomous'),
           ('core/constants.py', 'autonomy_needs_user')}
_FIELD = 'can_perform_without_user_input'


def _is_verdict_word(node):
    """A 'yes' / 'no' literal (any case; a 'no - reason' too)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        v = node.value.strip().lower()
        return v in ('yes', 'y') or v.startswith('no')
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_is_verdict_word(e) for e in node.elts)
    return False


def _autonomous_default(node):
    """A .get() default that answers 'autonomous' when the value is absent."""
    if not isinstance(node, ast.Constant):
        return False
    v = node.value
    if isinstance(v, str):
        return v.strip().lower() in ('yes', 'y', 'true')
    return bool(v)


def _names_the_field(expr, assigned):
    src = ast.unparse(expr)
    if any(v in src for v in _VOCAB):
        return True
    # One hop of indirection: `v = a['can_perform...']; v == 'yes'`.
    return any(isinstance(n, ast.Name) and any(
        v in assigned.get(n.id, '') for v in _VOCAB) for n in ast.walk(expr))


def _names_the_field_itself(expr, assigned):
    """Like _names_the_field, but only for the field name (one hop through
    a local), never the 'autonom' vocabulary of the rule's own names."""
    if _FIELD in ast.unparse(expr):
        return True
    return any(isinstance(n, ast.Name) and _FIELD in assigned.get(n.id, '')
               for n in ast.walk(expr))


def _reads_field_with_get(node):
    """`<x>.get('can_perform_without_user_input', ...)`."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'get' and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == _FIELD)


def private_autonomy_checks(src, filename='<src>'):
    """Every private answer to "autonomous?" / "needs the user?" in *src*,
    as (line, func).  Three shapes:
      * a compare of the field with a yes/no word (==, !=, in, ...);
      * .startswith() / .endswith() called on the field;
      * .get(<the field>, <a default that means autonomous>)."""
    tree = ast.parse(src, filename)
    hits = []

    def visit(node, func, assigned):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            func = getattr(node, 'name', '<lambda>')
            assigned = dict(assigned)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    assigned[t.id] = ast.unparse(node.value)
        if isinstance(node, ast.Compare):
            operands = [node.left] + list(node.comparators)
            if any(_is_verdict_word(o) for o in operands) and any(
                    _names_the_field(o, assigned)
                    for o in operands if not _is_verdict_word(o)):
                hits.append((node.lineno, func))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if (attr in ('startswith', 'endswith')
                    and _names_the_field(node.func.value, assigned)):
                hits.append((node.lineno, func))
            if _reads_field_with_get(node):
                defaults = list(node.args[1:2]) + [
                    k.value for k in node.keywords if k.arg == 'default']
                if any(_autonomous_default(d) for d in defaults):
                    hits.append((node.lineno, func))
        # getattr(x, <the field>, <autonomous default>) (review of 924b8e9dc).
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'getattr' and len(node.args) >= 3
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == _FIELD
                and _autonomous_default(node.args[2])):
            hits.append((node.lineno, func))
        # `v if v is not None else 'yes'` / `'yes' if v is None else v` on
        # the field: the autonomous default as a conditional expression
        # (review of 924b8e9dc).  Matched on the FIELD itself, not the
        # 'autonom' vocabulary, so a verdict read from the rule
        # (`'yes' if action_is_autonomous(a) else 'no'`) is left alone.
        if isinstance(node, ast.IfExp):
            branches = (node.body, node.orelse)
            if (any(_autonomous_default(b) for b in branches)
                    and _names_the_field_itself(node.test, assigned)):
                hits.append((node.lineno, func))
        # `.get(field) or 'yes'`: the same autonomous default, spelled with
        # `or` (review of d8fe536b2, F2).
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            if (any(_reads_field_with_get(v) for v in node.values)
                    and any(_autonomous_default(v) for v in node.values)):
                hits.append((node.lineno, func))
        for child in ast.iter_child_nodes(node):
            visit(child, func, assigned)

    visit(tree, '<module>', {})
    return hits


def _repo_checks():
    found = []
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith('.py'):
                continue
            path = os.path.join(dirpath, fn)
            try:
                src = open(path, encoding='utf-8').read()
            except (OSError, UnicodeDecodeError):
                continue
            if not any(v in src for v in _VOCAB):
                continue
            rel = os.path.relpath(path, _ROOT).replace(os.sep, '/')
            try:
                for line, func in private_autonomy_checks(src, rel):
                    if (rel, func) not in _OWNERS:
                        found.append(f'{rel}:{line} in {func}')
            except SyntaxError:
                continue
    return found


def test_source_guard_is_autonomous_has_one_rule():
    offenders = _repo_checks()
    assert offenders == [], (
        "a private answer to can_perform_without_user_input; ask "
        "core.constants.action_is_autonomous (may it run alone?) or "
        "autonomy_needs_user (must it stop for the user?) instead:\n  "
        + '\n  '.join(offenders))


@pytest.mark.parametrize('snippet', [
    "def f(a):\n    return a['can_perform_without_user_input'] == 'yes'\n",
    "def f(a):\n    return a.get('can_perform_without_user_input') == 'Yes'\n",
    "def f(agent):\n    return agent.can_perform_without_user_input == \"yes\"\n",
    "def f(a):\n    v = a.get('can_perform_without_user_input')\n"
    "    return v in ('yes', 'true')\n",
    "def f(p, i):\n    return _reuse_action_autonomy(p, i) != 'yes'\n",
    # the three shapes the review probed past the first guard
    "def f(t):\n    return t.context.get('can_perform_without_user_input', True)\n",
    "def f(a):\n    return a['can_perform_without_user_input'] != 'no'\n",
    "def f(a):\n    return a['can_perform_without_user_input'].startswith('y')\n",
    "def f(a):\n    v = str(a.get('can_perform_without_user_input')).lower()\n"
    "    return v.startswith('no')\n",
    "def f(a):\n    return a.get('can_perform_without_user_input', 'yes')\n",
    # review of d8fe536b2, F2
    "def f(a):\n    return a.get('can_perform_without_user_input') or 'yes'\n",
    "def f(a):\n    return a.get('can_perform_without_user_input', default=True)\n",
    # review of 924b8e9dc: an attribute read with an autonomous default, and
    # the default spelled as a conditional expression
    "def f(a):\n    return getattr(a, 'can_perform_without_user_input', 'yes')\n",
    "def f(a):\n    v = a.get('can_perform_without_user_input')\n"
    "    return v if v is not None else 'yes'\n",
    "def f(a):\n    v = a['can_perform_without_user_input']\n"
    "    return 'yes' if v is None else v\n",
], ids=['eq-yes-subscript', 'eq-Yes-get', 'eq-yes-attr', 'in-via-variable',
        'ne-yes-reader', 'get-default-True', 'ne-no', 'startswith-y',
        'startswith-no-via-variable', 'get-default-yes', 'get-or-yes',
        'get-default-kw-True', 'getattr-default-yes', 'ifexp-else-yes',
        'ifexp-yes-if-none'])
def test_source_guard_sees_every_shape_of_a_copy(snippet):
    """Anti-vacuity: the guard above can fail, once per shape."""
    hits = private_autonomy_checks(snippet)
    assert hits and {fn for _, fn in hits} == {'f'}, hits


@pytest.mark.parametrize('src', [
    "def f(parts, v):\n    ntp = v == 'yes'\n    return parts[0] == 'yes' and ntp\n",
    # loading the field with a NOT-autonomous default is data, not a verdict
    "def f(d):\n    return d.get('can_perform_without_user_input', 'no')\n",
    "def f(d):\n    return d.get('can_perform_without_user_input')\n",
    "def f(s):\n    return s.startswith('no')\n",
    "def f(d):\n    return (d.get('can_perform_without_user_input') or '').strip()\n",
    "def f(d):\n    return d.get('can_perform_without_user_input', default='no')\n",
    "def f(a):\n    return getattr(a, 'can_perform_without_user_input', 'no')\n",
    "def f(a):\n    v = a.get('can_perform_without_user_input')\n"
    "    return v if v is not None else 'no'\n",
    "def f(a):\n    return 'yes' if action_is_autonomous(a) else 'no'\n",
], ids=['unrelated-yes', 'get-default-no', 'get-no-default', 'unrelated-startswith',
        'get-or-empty', 'get-default-kw-no', 'getattr-default-no',
        'ifexp-else-no', 'ifexp-unrelated-autonomous'])
def test_source_guard_leaves_unrelated_code_alone(src):
    assert private_autonomy_checks(src) == []
