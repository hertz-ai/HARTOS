"""The fallback ORM models never declare what the canonical ones lack.

integrations.social.models imports the ORM models from the canonical
``sql.models`` (the hevolve-database sibling repo) and falls back to
``integrations.social._models_local`` only when that package cannot be
imported.  This venv has no ``sql`` package, so every HARTOS test runs on the
fallback, while the installed desktop pip-installs the canonical one.  A
column or mapped attribute added to the fallback alone is therefore green
here and an AttributeError in production: ``user_consents.reopened_at``
(HARTOS v58, 84c650a5d) lived only in the fallback from 2026-09-27 to
2026-10-18, so every ``ConsentService.declined`` raised on the desktop and
the PeerLink device verifier refused every phone ("type object 'UserConsent'
has no attribute 'reopened_at'", measured live 2026-10-18; fixed in
Hevolve_Database c5be877).

Each side is read in its own child interpreter, through the facade, the way
production resolves it: the fallback with the sibling off sys.path and the
``sql`` modules blocked (so the guard reads the fallback even where
hevolve-database is pip-installed), the canonical with the sibling on
sys.path.  The facade picks its branch once per process, at import, so the
two readings cannot share one; and this process imports neither side, so
nothing leaks into the tests that run after this file (no ``sql`` modules
in sys.modules, no HEVOLVE_DB_PATH in the environment).

Compared per exported facade name, since that is what HARTOS code imports:
the table name, every fallback column (name, type, nullability) and every
fallback mapped attribute (column and relationship keys, so a column mapped
under another attribute name is caught: that is the exact AttributeError
above).  A column only the canonical model has is allowed: code written
against it fails loudly on the fallback, which every test runs.

Skipped when the sibling repo is not checked out beside HARTOS.
HARTOS_TEST_HEVOLVE_DB_ROOT points the guard at another checkout (a
git-archive of one commit, to run it against that commit) and must then be
one.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

HARTOS_ROOT = pathlib.Path(__file__).resolve().parents[2]
_ROOT_ENV = 'HARTOS_TEST_HEVOLVE_DB_ROOT'
#: The canonical repo: beside HARTOS, or wherever HARTOS_TEST_HEVOLVE_DB_ROOT
#: points.
HEVOLVE_DB_ROOT = pathlib.Path(os.environ.get(_ROOT_ENV)
                               or HARTOS_ROOT.parent / 'Hevolve_Database')

#: Runs in a child interpreter.  argv: the HARTOS root, the Hevolve_Database
#: root, the side ('fallback' | 'canonical').  Prints one JSON line: the file
#: the facade's models came from and, per exported facade name that is a
#: mapped class, its table, columns and mapped attribute keys.
_CHILD = r'''
import inspect, json, sys
hartos, hdb, side = sys.argv[1:4]
sys.path.insert(0, hartos)
if side == 'canonical':
    sys.path.insert(1, hdb)
else:
    # The fallback branch even where hevolve-database is pip-installed: a
    # None entry makes the facade's `from sql.database import Base` and
    # `from sql.models import ...` raise ImportError, its fallback trigger.
    for name in ('sql', 'sql.database', 'sql.models'):
        sys.modules[name] = None
import integrations.social.models as m
from sqlalchemy import inspect as sa_inspect
names = {}
for name in dir(m):
    obj = getattr(m, name)
    if not (isinstance(obj, type) and getattr(obj, '__table__', None) is not None
            and isinstance(getattr(obj, '__tablename__', None), str)):
        continue
    names[name] = {
        'table': obj.__tablename__,
        'cols': {c.name: {'type': repr(c.type), 'nullable': bool(c.nullable)}
                 for c in obj.__table__.columns},
        'attrs': sorted(sa_inspect(obj).attrs.keys()),
    }
print('__MODELS__' + json.dumps({
    'file': inspect.getfile(m.UserConsent).replace('\\', '/'), 'names': names}))
'''

_RESOLVES_TO = {'fallback': '/integrations/social/_models_local.py',
                'canonical': '/sql/models.py'}


def _models_as(side):
    """{facade name: {table, cols, attrs}} of the models the facade resolves
    to on one side, read in a child interpreter (module docstring)."""
    env = dict(os.environ, HEVOLVE_DB_PATH=':memory:')
    run = subprocess.run(
        [sys.executable, '-I', '-c', _CHILD, str(HARTOS_ROOT),
         str(HEVOLVE_DB_ROOT), side],
        capture_output=True, text=True, timeout=600, env=env,
        cwd=str(HARTOS_ROOT))
    assert run.returncode == 0, run.stderr[-2000:]
    lines = [ln for ln in run.stdout.splitlines() if ln.startswith('__MODELS__')]
    assert lines, run.stdout[-2000:]
    doc = json.loads(lines[-1][len('__MODELS__'):])
    assert doc['file'].endswith(_RESOLVES_TO[side]), (
        'the %s child resolved integrations.social.models to %s'
        % (side, doc['file']))
    return doc['names']


@pytest.fixture(scope='module')
def sides():
    """(fallback, canonical): each side's models, read once for the module."""
    if not (HEVOLVE_DB_ROOT / 'sql' / 'models.py').is_file():
        if os.environ.get(_ROOT_ENV):
            pytest.fail('%s=%s is not a Hevolve_Database checkout (no '
                        'sql/models.py there)' % (_ROOT_ENV, HEVOLVE_DB_ROOT))
        pytest.skip('sibling repo Hevolve_Database not checked out at %s'
                    % HEVOLVE_DB_ROOT)
    return _models_as('fallback'), _models_as('canonical')


def test_every_fallback_column_and_attribute_is_on_the_canonical_model(sides):
    fallback, canonical = sides
    shared = sorted(set(fallback) & set(canonical))
    assert len(shared) >= 80, 'the two model sets barely overlap: %s' % shared
    drift = {}
    for name in shared:
        f, c = fallback[name], canonical[name]
        found = {}
        if f['table'] != c['table']:
            found['table (fallback, canonical)'] = [f['table'], c['table']]
        only_here = sorted(set(f['cols']) - set(c['cols']))
        if only_here:
            found['columns only the fallback declares'] = only_here
        changed = {col: [f['cols'][col], c['cols'][col]]
                   for col in sorted(set(f['cols']) & set(c['cols']))
                   if f['cols'][col] != c['cols'][col]}
        if changed:
            found['columns declared differently (fallback, canonical)'] = changed
        only_here = sorted(set(f['attrs']) - set(c['attrs']))
        if only_here:
            found['mapped attributes only the fallback has'] = only_here
        if found:
            drift[name] = found
    assert not drift, (
        'the installed desktop runs the canonical model and raises on these; '
        'add them to Hevolve_Database sql/models.py with a migration '
        '(precedent c5be877 reopened_at): %s' % json.dumps(drift, indent=1))


def test_the_consent_column_v58_reads_is_on_both_models(sides):
    """The exact attribute behind the 2026-10-18 refusals, by name on each
    side, so the failure says what broke even if the general check is
    widened: the mapped attribute ``UserConsent.reopened_at`` over the
    column ``user_consents.reopened_at``."""
    for side, models in zip(('fallback', 'canonical'), sides):
        consent = models['UserConsent']
        assert consent['table'] == 'user_consents', (
            '%s: UserConsent maps %s' % (side, consent['table']))
        assert 'reopened_at' in consent['cols'], (
            '%s: user_consents has no reopened_at column' % side)
        assert 'reopened_at' in consent['attrs'], (
            '%s: UserConsent.reopened_at is not a mapped attribute, the '
            'AttributeError of 2026-10-18' % side)
