"""Importing HARTOS must not start the node's background daemons.

MEASURED 2026-09-09.  `hart_intelligence_entry.py:1062` calls `init_social(app)`
from a bare module-level ``try:``, and `init_social` starts SEVEN production
subsystems: peer gossip, UDP LAN auto-discovery, the runtime integrity monitor,
the sync-engine drain, the distributed coding agent, a network registration to
HEVOLVE_REGISTRY_URL, and the node watchdog.  So `import hart_intelligence_entry`
— which the reuse/vlm/recipe unit tests all do transitively — boots a node.

WHAT THAT COST, measured on the same suite with the same flags:

    monitor live        crashed at 89%, no summary, 66 errors
    monitor suppressed  100% complete, 40 failed / 780 passed / 1 error, 219s

The crash is a Windows access violation raised on the integrity monitor's own
thread while it walks the code tree:

    Current thread (most recent call first):
      Garbage-collecting
      pathlib.py ... iterdir
      security/node_integrity.py line 395 in _collect_py_files
      security/runtime_monitor.py line 111 in _stat_sweep
      security/runtime_monitor.py line 156 in _check_loop

Causation, not correlation: neutralising ONLY `start_monitor` (it fired exactly
once) removed the access violation from an otherwise identical run.  That single
crash is why the suite had no completable baseline, which in turn is why every
fix needed its own bespoke A/B to claim "no regression".

THE CONTRACT.  A process that only wants to READ or exercise the code must be
able to import it without joining the hive, opening a UDP socket, registering
with a remote registry, or walking the tree from a background thread.
`should_start_background_services()` is the ONE predicate that decides, it
defaults to True so production behaviour is unchanged, and `init_social` MUST
log loudly when it skips — a node that silently never starts gossip must never
look like a healthy one (the same reasoning as the 2026-09-01 "invisible
branch" comment already in that file).

    python -m pytest tests/unit/test_import_does_not_boot_node.py -q
"""
import ast
import os

import pytest


SOCIAL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'integrations', 'social', '__init__.py')

FLAG = 'HEVOLVE_START_BACKGROUND_SERVICES'
GATE_VAR = '_start_services'

# Every call inside init_social that starts a real background subsystem.
# Keyed on the attribute/function name actually invoked at the call site.
DAEMON_STARTERS = {
    'start_monitor',              # runtime integrity monitor thread
    'start_background_sync',      # sync-engine drain loop
    'init_coding_agent',          # distributed coding daemon
    'start_watchdog',             # node watchdog
    'register_with_registry',     # outbound network registration
}


def _load(path):
    with open(path, encoding='utf-8') as fh:
        return ast.parse(fh.read())


def _init_social(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'init_social':
            return node
    raise AssertionError('init_social not found in integrations/social/__init__.py')


def _parents(root):
    seen = {}
    for node in ast.walk(root):
        for child in ast.iter_child_nodes(node):
            seen[child] = node
    return seen


def _callee(node):
    fn = node.func
    return getattr(fn, 'attr', None) or getattr(fn, 'id', None)


def _starter_calls(fn_node):
    return [n for n in ast.walk(fn_node)
            if isinstance(n, ast.Call) and _callee(n) in DAEMON_STARTERS]


def _guarded_by(node, parents, var):
    """True if any enclosing `if` tests `var`."""
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, ast.If):
            names = {n.id for n in ast.walk(cur.test) if isinstance(n, ast.Name)}
            if var in names:
                return True
        cur = parents.get(cur)
    return False


@pytest.fixture(scope='module')
def social_tree():
    return _load(SOCIAL)


class TestImportDoesNotBootTheNode:

    def test_every_daemon_start_is_gated(self, social_tree):
        """THE DEFECT.  RED before the fix."""
        fn = _init_social(social_tree)
        parents = _parents(fn)
        calls = _starter_calls(fn)
        assert calls, ('the detector found no daemon starters at all — it has '
                       'gone stale against init_social and proves nothing')
        ungated = sorted({_callee(c) for c in calls
                          if not _guarded_by(c, parents, GATE_VAR)})
        assert not ungated, (
            f'these start real background subsystems with no {GATE_VAR} gate: '
            f'{ungated}. Importing hart_intelligence_entry runs init_social, so '
            f'any process that merely imports HARTOS joins the hive and starts '
            f'the integrity monitor whose tree walk crashed the unit suite at 89%.')

    def test_gate_reads_the_canonical_predicate(self, social_tree):
        """One predicate, not a re-spelled env lookup (Gate 2/Gate 4)."""
        fn = _init_social(social_tree)
        src = ast.dump(fn)
        assert 'should_start_background_services' in src, (
            'init_social must consult core.config_cache.'
            'should_start_background_services, not read the env var itself — '
            'a second reader is exactly the parallel path that drifts')

    def test_skip_is_logged_not_silent(self, social_tree):
        """A node that skips its daemons must SAY so.

        The file's own 2026-09-01 comment records what an invisible branch
        cost: 'gossip running fine' and 'gossip never started' looked
        identical for ~24 minutes of a dead retention sweep.
        """
        fn = _init_social(social_tree)
        parents = _parents(fn)
        logs = [n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and _callee(n) in ('warning', 'critical', 'error')
                and _guarded_by(n, parents, GATE_VAR)]
        assert logs, (
            f'no warning/critical is emitted from a branch that tests '
            f'{GATE_VAR}; a silently service-less node is indistinguishable '
            f'from a healthy one')


class TestThePredicateItself:
    """Anti-vacuity: the predicate must actually behave, and must default to
    the production answer so this change cannot alter a real node."""

    def test_defaults_to_true_so_production_is_unchanged(self, monkeypatch):
        from core.config_cache import should_start_background_services
        monkeypatch.delenv(FLAG, raising=False)
        assert should_start_background_services() is True

    @pytest.mark.parametrize('val', ['0', 'false', 'no', 'off', 'FALSE'])
    def test_explicit_falsy_disables(self, monkeypatch, val):
        from core.config_cache import should_start_background_services
        monkeypatch.setenv(FLAG, val)
        assert should_start_background_services() is False

    @pytest.mark.parametrize('val', ['1', 'true', 'yes', 'on'])
    def test_explicit_truthy_enables(self, monkeypatch, val):
        from core.config_cache import should_start_background_services
        monkeypatch.setenv(FLAG, val)
        assert should_start_background_services() is True

    def test_junk_does_not_silently_flip_a_real_node(self, monkeypatch):
        """env_flag's declared semantics: junk falls back to the default.

        This matters more here than for most flags — a typo must never be
        the reason a desktop stops gossiping.
        """
        from core.config_cache import should_start_background_services
        monkeypatch.setenv(FLAG, 'maybe')
        assert should_start_background_services() is True


class TestTheDetectorIsNotVacuous:

    def test_detector_fires_on_an_ungated_starter(self):
        sample = ast.parse(
            'def init_social(app):\n'
            '    start_monitor(m)\n')
        fn = _init_social(sample)
        parents = _parents(fn)
        calls = _starter_calls(fn)
        assert calls and not _guarded_by(calls[0], parents, GATE_VAR)

    def test_detector_accepts_a_gated_starter(self):
        sample = ast.parse(
            'def init_social(app):\n'
            '    _start_services = f()\n'
            '    if _boot_verified and _start_services:\n'
            '        start_monitor(m)\n')
        fn = _init_social(sample)
        parents = _parents(fn)
        calls = _starter_calls(fn)
        assert calls and _guarded_by(calls[0], parents, GATE_VAR)
