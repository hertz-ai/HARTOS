"""A test run must never read or write the shared agent_data database.

`integrations/social/models.py` resolves DB_PATH ONCE at import and caches it at
module level, so in a single pytest process the FIRST suite to import `models`
decides the database for every suite after it. Suites disagreed about pinning
HEVOLVE_DB_PATH, and when an unpinned one imported first the whole run fell
through to `agent_data/hevolve_database.db` -- a real file holding tens of MB of
development state. The run mutated it, and the NEXT run started on those
leftovers.

The cost was not hypothetical. Two runs of byte-identical code gave
"253 passed, 1 error" and "3 failed, 247 passed, 7 errors"; on that basis a
regression AND its supposed fix were both credited to code that was present in
both arms. With the database fresh, the same suites give 254 passed and no errors
at all, including the one failure that had been written off as pre-existing.

The fix is in models.py, not in each suite: under pytest with nothing configured,
DB_PATH resolves to a per-process temp file. Pinning it per suite could never
cover the whole problem anyway, because several suites import `models` lazily
inside a fixture, long after any module-top pin would have run.

Narrower claim than it looks: this closes ONE carrier of cross-suite leakage.
Process-global state surviving a suite boundary has other carriers (module
singletons, caches keyed at import -- a peer's sweep flakes on an unreset
SessionGuard counter), so green here does not mean the tree is hermetic.

Cost note, learned the hard way: an earlier version of this file spawned FIVE
children, one per scenario, each paying a full HARTOS import. That is ~40s idle
and over 180s when anything else is running, so three of six tests died on
`subprocess.TimeoutExpired` inside a larger sweep while passing alone -- this
file's own flavour of the defect it guards. Now: TWO children, and each one
re-imports only `models` between scenarios, which is cheap once its dependencies
are cached.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Anything that would pre-empt the branch under test. Cleared in the child so the
# developer's own environment cannot decide the answer.
_CLEARED = ('HEVOLVE_DB_PATH', 'SOCIAL_DB_PATH', 'HEVOLVE_DB_URL', 'DATABASE_URL',
            'NUNBA_BUNDLED', 'HEVOLVE_CLOUD_MODE', 'DOCKER_CONTAINER',
            'PYTEST_CURRENT_TEST')

# Resolve in a CHILD, because the caching is the whole point: re-importing models
# into this process would corrupt the live run.
_PROBE = textwrap.dedent('''
    import importlib, json, os, sys
    sys.path.insert(0, {root!r})
    {pytest_import}

    CLEARED = {cleared!r}

    def resolve(env):
        for key in CLEARED:
            os.environ.pop(key, None)
        os.environ.update(env)
        # Drop ONLY models, so its (expensive) dependencies stay cached and the
        # module-level DB_PATH block runs again under the new environment.
        sys.modules.pop('integrations.social.models', None)
        mod = importlib.import_module('integrations.social.models')
        return {{'db_path': str(mod.DB_PATH or ''), 'db_url': str(mod.DB_URL or '')}}

    out = [resolve(env) for env in {scenarios!r}]
    print('__RESULT__' + json.dumps(out))
''')


def _resolve(scenarios, with_pytest):
    """DB_PATH/DB_URL for each scenario, resolved in one child process."""
    env = {k: v for k, v in os.environ.items() if k not in _CLEARED}
    code = _PROBE.format(
        root=REPO_ROOT,
        cleared=_CLEARED,
        scenarios=scenarios,
        pytest_import='import pytest' if with_pytest else '',
    )
    result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                            text=True, cwd=REPO_ROOT, env=env, timeout=900)
    marker = [ln for ln in result.stdout.splitlines() if ln.startswith('__RESULT__')]
    if not marker:
        pytest.fail('probe produced no result (exit %s)\nstdout tail:\n%s\n'
                    'stderr tail:\n%s'
                    % (result.returncode, result.stdout[-1500:], result.stderr[-1500:]))
    return json.loads(marker[-1][len('__RESULT__'):])


def test_this_process_is_not_using_the_shared_agent_data_database():
    """The live invariant, asserted against the process actually running.

    If a suite imported models before the guard took effect, this run is reading
    and writing the shared development database, and no comparison across runs
    means anything.
    """
    from integrations.social import models

    db_path = str(getattr(models, 'DB_PATH', None) or '').replace('\\', '/')
    assert 'agent_data' not in db_path, (
        'models.DB_PATH resolved to %r, so this test run is reading and writing '
        'the shared agent_data database.' % (db_path,))


def test_db_path_resolution_under_pytest():
    """Four scenarios in one child: default, uniqueness, and both pins."""
    pinned = os.path.join(os.path.sep, 'tmp', 'pinned_probe.db')
    first, second, explicit, memory = _resolve(
        [{}, {}, {'HEVOLVE_DB_PATH': pinned}, {'HEVOLVE_DB_PATH': ':memory:'}],
        with_pytest=True)

    # 1. Unconfigured under pytest must not be the shared file.
    assert 'agent_data' not in first['db_path'].replace('\\', '/'), (
        'an unconfigured test process resolved to %r' % first['db_path'])
    assert first['db_path'], 'expected a real path, got an empty DB_PATH'
    # File-backed on purpose: ':memory:' would change the pooling every suite
    # runs under (see the StaticPool statement-cache note in models.py).
    assert first['db_url'].startswith('sqlite:///'), (
        'expected a file-backed sqlite URL, got %r' % first['db_url'])

    # 2. Fresh each time, so run N+1 cannot start on run N's leftovers.
    assert first['db_path'] != second['db_path'], (
        'two unconfigured resolutions gave the same database %r, which is the '
        'exact contamination this guards' % first['db_path'])

    # 3. The guard is a DEFAULT: an explicit pin still wins.
    assert explicit['db_path'] == pinned, (
        'explicit HEVOLVE_DB_PATH=%r was overridden, resolved to %r'
        % (pinned, explicit['db_path']))

    # 4. The ten-odd suites pinning ':memory:' must keep getting ':memory:'.
    assert memory['db_path'] == ':memory:', (
        "a ':memory:' pin resolved to %r instead" % memory['db_path'])
    assert memory['db_url'] == 'sqlite://', (
        'expected the in-memory URL, got %r' % memory['db_url'])


def test_production_still_resolves_to_the_shared_database():
    """The regression risk, asserted directly: NOT under pytest, nothing changes.

    Without pytest in sys.modules the old fallback must still apply, or this
    guard would have quietly repointed a real node's database at a temp file.
    """
    resolved, = _resolve([{}], with_pytest=False)

    db_path = resolved['db_path'].replace('\\', '/')
    assert db_path.endswith('agent_data/hevolve_database.db'), (
        'a process with no pytest resolved to %r; the production fallback moved'
        % db_path)
