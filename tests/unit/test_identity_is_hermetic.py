"""A test run never reads or writes the owner's real data root.

The identity (node_id.json, keys) was the first casualty; the coding
benchmark DB was the second (see the bottom of this file).

On 2026-09-23 an uncommitted identity change ran under test and replaced the
owner's desktop node_id.json (46329c87, the id central had verified) with a
fresh id. Importing integrations.social.peer_discovery builds GossipProtocol at
module level, and its __init__ loads-or-creates node_id.json under the real
data root; the Ed25519 key dir resolved there too.

These tests stand in a decoy home for the real one, so a failure writes into
tmp_path, never into ~/Documents/Nunba.
"""
import importlib
import os
import subprocess
import sys

import pytest

import core.platform_paths as pp

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_OVERRIDES = ('NUNBA_DATA_DIR', 'HARTOS_DATA_DIR', 'HEVOLVE_KEY_DIR',
              'HEVOLVE_DB_PATH', 'XDG_DATA_HOME')


@pytest.fixture
def decoy_home(tmp_path, monkeypatch):
    """Point the platform default data root at tmp_path, with no overrides."""
    home = tmp_path / 'home'
    home.mkdir()
    for var in ('HOME', 'USERPROFILE'):
        monkeypatch.setenv(var, str(home))
    for var in _OVERRIDES:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(pp, '_cached_data_dir', None)
    monkeypatch.setattr(pp, '_pytest_data_dir', None)
    real_root = pp._platform_default_data_dir()
    assert real_root.startswith(str(home)), real_root
    return real_root


def test_node_id_is_not_written_under_the_real_root(decoy_home):
    from integrations.social import peer_discovery

    node_id = peer_discovery._load_or_create_node_id()

    assert node_id
    assert not os.path.exists(os.path.join(decoy_home, 'node_id.json'))


def test_key_dir_is_not_the_real_root(decoy_home):
    import security.node_integrity as ni

    key_dir = os.path.abspath(ni._resolve_key_dir())

    assert not key_dir.startswith(os.path.abspath(decoy_home)), key_dir


def test_a_data_dir_the_test_chose_is_used_as_given(decoy_home, tmp_path, monkeypatch):
    mine = tmp_path / 'mine'
    monkeypatch.setenv('NUNBA_DATA_DIR', str(mine))
    monkeypatch.setattr(pp, '_cached_data_dir', None)
    from integrations.social import peer_discovery
    import security.node_integrity as ni

    key_dir = ni._resolve_key_dir()
    monkeypatch.setattr(ni, '_KEY_DIR', key_dir)
    monkeypatch.setattr(ni, '_private_key', None)
    monkeypatch.setattr(ni, '_public_key', None)

    node_id = peer_discovery._load_or_create_node_id()

    assert os.path.abspath(key_dir) == str(mine / 'agent_data')
    assert (mine / 'agent_data' / 'node_identity.json').read_text().count(node_id) == 1


def test_the_same_temp_root_holds_for_the_whole_process(decoy_home):
    first = pp.get_identity_data_dir()

    assert first == pp.get_identity_data_dir()
    assert not first.startswith(os.path.abspath(decoy_home))


def test_no_shipped_module_imports_pytest():
    # get_identity_data_dir keys off "pytest is imported". A shipped module
    # that imported it would put a real node on a temp identity.
    import re
    pattern = re.compile(r'^\s*(import|from)\s+_?pytest\b', re.M)
    offenders = []
    for top in ('core', 'integrations', 'security'):
        for dirpath, dirnames, filenames in os.walk(os.path.join(_REPO, top)):
            dirnames[:] = [d for d in dirnames if d not in ('tests', 'test')]
            for name in filenames:
                if not name.endswith('.py') or name.startswith('test_') \
                        or name.endswith('_test.py') or name == 'conftest.py':
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding='utf-8', errors='replace') as fh:
                    if pattern.search(fh.read()):
                        offenders.append(os.path.relpath(path, _REPO))

    assert offenders == []


def test_outside_pytest_the_real_root_is_used(decoy_home):
    # A real node must keep its identity where it always was.
    code = ('import sys, core.platform_paths as pp; '
            'assert "pytest" not in sys.modules; '
            'print(pp.get_identity_data_dir() == pp._platform_default_data_dir())')
    env = {k: v for k, v in os.environ.items() if k not in _OVERRIDES}
    out = subprocess.run([sys.executable, '-c', code], cwd=_REPO, env=env,
                         capture_output=True, text=True, timeout=60)

    assert out.stdout.strip() == 'True', out.stderr


# ── The whole data root, not only the identity ──────────────────────────────
# a4ea04651 moved coding_benchmarks.db to get_agent_data_dir(). The identity
# guard covered get_identity_data_dir only, so test_autoresearch's
# test_record_benchmark_no_crash_on_failure wrote
# ('autoresearch', 'aider_native_backend', 'score', ..., 0.0, 0) into the
# owner's live DB on 2026-09-27 (three rows by 18:09). get_best_tool learns a
# 0% success rate from five of those, and export_learning_delta ships them to
# hive peers.

def test_a_default_benchmark_tracker_never_writes_under_the_real_root(decoy_home):
    from integrations.coding_agent.benchmark_tracker import BenchmarkTracker

    tracker = BenchmarkTracker()
    tracker.record('autoresearch', 'aider_native_backend', 0.0, False,
                   model_name='score', user_id='hermetic_probe')

    real = os.path.normcase(os.path.abspath(decoy_home))
    assert not os.path.normcase(os.path.abspath(tracker._db_path)).startswith(real)
    assert os.path.isfile(tracker._db_path)
    written = [os.path.join(d, f) for d, _, fs in os.walk(decoy_home) for f in fs]
    assert written == []
    assert tracker.get_summary()['total_benchmarks'] == 1


def test_every_data_root_resolver_is_off_the_real_root(decoy_home):
    real = os.path.normcase(os.path.abspath(decoy_home))
    for resolve in (pp.get_data_dir, pp.get_db_dir, pp.get_agent_data_dir,
                    pp.get_db_path, pp.get_prompts_dir, pp.get_uploads_dir,
                    pp.get_memory_graph_dir, pp.get_simplemem_dir,
                    pp.get_identity_data_dir, pp.get_log_dir):
        got = os.path.normcase(os.path.abspath(resolve()))
        assert not got.startswith(real), (resolve.__name__, got)


def test_identity_and_data_share_one_root(decoy_home):
    # One guard: the identity root IS the data root, swapped or not.
    assert pp.get_identity_data_dir() == pp.get_data_dir()


def test_outside_pytest_the_data_root_is_the_real_one(decoy_home):
    code = ('import sys, core.platform_paths as pp; '
            'assert "pytest" not in sys.modules; '
            'print(pp.get_data_dir() == pp._platform_default_data_dir())')
    env = {k: v for k, v in os.environ.items() if k not in _OVERRIDES}
    out = subprocess.run([sys.executable, '-c', code], cwd=_REPO, env=env,
                         capture_output=True, text=True, timeout=60)

    assert out.stdout.strip() == 'True', out.stderr


# ── No silent data loss: a frozen build is never swapped, a swap is loud ─────
# The installed Nunba ships pytest in lib/.  If anything there imported it, a
# swap would put a person's DB, recipes and memories in a dir deleted at exit.

def test_a_frozen_process_with_pytest_imported_keeps_the_real_root(
        decoy_home, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)

    assert pp.get_data_dir() == decoy_home
    assert pp.get_agent_data_dir().startswith(decoy_home)
    # Under test the frozen answer is not cached: it would outlive the patch.
    assert pp._cached_data_dir is None
    monkeypatch.setattr(sys, 'frozen', False)
    assert not pp.get_data_dir().startswith(decoy_home)


def test_a_swap_logs_a_warning_naming_both_paths(decoy_home, caplog):
    with caplog.at_level('WARNING', logger='hevolve.platform'):
        swapped = pp.get_data_dir()

    warned = [r.getMessage() for r in caplog.records
              if r.name == 'hevolve.platform' and r.levelname == 'WARNING']
    assert len(warned) == 1, warned
    assert decoy_home in warned[0] and swapped in warned[0]


def test_a_dir_the_test_chose_is_not_a_swap_and_logs_nothing(
        decoy_home, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path / 'mine'))
    with caplog.at_level('WARNING', logger='hevolve.platform'):
        assert pp.get_data_dir() == str(tmp_path / 'mine')
    assert [r for r in caplog.records if r.name == 'hevolve.platform'] == []


def test_the_macos_log_root_is_off_the_real_one(decoy_home, monkeypatch):
    # macOS logs live in ~/Library/Logs/Nunba, outside the data root.
    monkeypatch.delenv('NUNBA_LOG_DIR', raising=False)
    monkeypatch.setattr(pp, '_IS_MACOS', True)
    monkeypatch.setattr(pp, '_IS_WINDOWS', False)
    monkeypatch.setattr(pp, '_IS_LINUX', False)
    real_logs = os.path.expanduser('~/Library/Logs/Nunba')
    home = os.path.expanduser('~')
    assert decoy_home.startswith(home) and real_logs.startswith(home)  # the decoy

    got = pp.get_log_dir()

    assert not os.path.normcase(got).startswith(os.path.normcase(real_logs)), got


# ── One test predicate, and no module builds the data root itself ────────────

def _shipped_sources():
    """Git-tracked shipped modules: core, hartos, integrations, security and
    the repo-root entry points.  core/platform_paths.py is the one resolver."""
    # A walk, not `git ls-files`: an export has no .git, and an empty list
    # would pass vacuously (a mutant proved it).  Root scratch files are
    # named _*.py and are not shipped.
    found = [n for n in os.listdir(_REPO)
             if n.endswith('.py') and not n.startswith('_')]
    for top in ('core', 'hartos', 'integrations', 'security'):
        for dirpath, dirnames, filenames in os.walk(os.path.join(_REPO, top)):
            dirnames[:] = [d for d in dirnames
                           if d not in ('tests', 'test', '__pycache__')]
            found += [os.path.relpath(os.path.join(dirpath, n), _REPO).replace(os.sep, '/')
                      for n in filenames if n.endswith('.py')]
    found = [rel for rel in found if rel != 'core/platform_paths.py']
    assert len(found) > 500, len(found)   # the scan saw the tree
    return found


def _docstring_ids(tree):
    import ast
    ids = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.ClassDef)) and n.body \
                and isinstance(n.body[0], ast.Expr) \
                and isinstance(n.body[0].value, ast.Constant):
            ids.add(id(n.body[0].value))
    return ids


def _builds_the_data_root(tree):
    """Line numbers where a module spells ~/Documents/Nunba itself: a
    join('Documents', 'Nunba', ...), a Path / 'Documents' / 'Nunba' chain, or
    a path string holding Documents/Nunba (one with no whitespace: prose that
    tells a person or a model where a file lives is not a path in use)."""
    import ast
    docs = _docstring_ids(tree)
    lines = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            vals = [a.value if isinstance(a, ast.Constant) else None for a in n.args]
            if any(a == 'Documents' and b == 'Nunba' for a, b in zip(vals, vals[1:])):
                lines.append(n.lineno)
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div) \
                and isinstance(n.right, ast.Constant) and n.right.value == 'Nunba' \
                and isinstance(n.left, ast.BinOp) \
                and isinstance(n.left.right, ast.Constant) \
                and n.left.right.value == 'Documents':
            lines.append(n.lineno)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) \
                and id(n) not in docs and not any(c.isspace() for c in n.value) \
                and 'Documents/Nunba' in n.value.replace(chr(92), '/'):
            lines.append(n.lineno)
    return lines


def _tests_for_pytest(tree):
    """Line numbers of a `'pytest' in <...>` / `not in` test: the question
    platform_paths.under_test answers, asked again."""
    import ast
    lines = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Compare) and isinstance(n.left, ast.Constant) \
                and n.left.value == 'pytest' \
                and any(isinstance(op, (ast.In, ast.NotIn)) for op in n.ops):
            lines.append(n.lineno)
    return lines


def _offenders(check):
    import ast
    found = []
    for rel in _shipped_sources():
        with open(os.path.join(_REPO, rel), encoding='utf-8', errors='replace') as fh:
            try:
                tree = ast.parse(fh.read())
            except SyntaxError:
                continue
        found += ['%s:%d' % (rel, ln) for ln in check(tree)]
    return found


def test_source_guard_no_shipped_module_builds_the_data_root_itself():
    # Such a path skips get_data_dir, so it skips the test guard: this is how
    # test_persist_language could write the owner's hart_language.json.
    assert _offenders(_builds_the_data_root) == []


def test_source_guard_under_test_is_the_only_pytest_check():
    assert _offenders(_tests_for_pytest) == []


def test_the_source_guards_can_fail():
    import ast
    built = ast.parse(
        "import os\nfrom pathlib import Path\n"
        "a = os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data')\n"
        "b = Path.home() / 'Documents' / 'Nunba' / 'logs'\n"
        "c = os.path.expanduser('~/Documents/Nunba/data')\n"
        "def f():\n    '''Reads ~/Documents/Nunba/logs (a docstring is fine).'''\n"
        "d = 'prose: a venv at ~/Documents/Nunba/data/venvs/x/ is fine'\n")
    assert _builds_the_data_root(built) == [3, 4, 5]
    asked = ast.parse("import sys\nx = 'pytest' in sys.modules\n"
                      "y = 'pytest' not in sys.modules\n")
    assert _tests_for_pytest(asked) == [2, 3]


def test_the_social_db_under_test_is_a_temp_file():
    # integrations/social/models.py asks under_test() (not its own
    # "'pytest' in sys.modules") before falling back to the repo agent_data DB.
    code = ('import pytest, os\n'
            'import integrations.social.models as m\n'
            'print(os.path.abspath(m.DB_PATH))\n')
    env = {k: v for k, v in os.environ.items()
           if k not in _OVERRIDES + ('NUNBA_BUNDLED', 'HEVOLVE_DB_URL',
                                     'DATABASE_URL', 'DOCKER_CONTAINER')}
    out = subprocess.run([sys.executable, '-c', code], cwd=_REPO, env=env,
                         capture_output=True, text=True, timeout=120)
    path = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ''

    assert 'hartos_test_' in path, (path, out.stderr[-2000:])
    assert not path.startswith(os.path.join(_REPO, 'agent_data'))
