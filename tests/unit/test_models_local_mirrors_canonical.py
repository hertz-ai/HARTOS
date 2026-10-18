"""The fallback ORM models never declare a column the canonical ones lack.

integrations.social.models imports the ORM models from the canonical
``sql.models`` (the hevolve-database sibling repo) and falls back to
``integrations.social._models_local`` only when that package is absent.
This venv has no ``sql`` package, so every HARTOS test runs on the fallback,
while the installed desktop pip-installs the canonical one.  A column added
to the fallback alone is therefore green here and an AttributeError in
production: ``user_consents.reopened_at`` (HARTOS v58, 84c650a5d) lived
only in the fallback from 2026-09-27 to 2026-10-18, so every
``ConsentService.declined`` raised on the desktop and the PeerLink device
verifier refused every phone ("type object 'UserConsent' has no attribute
'reopened_at'", measured live 2026-10-18; fixed in Hevolve_Database c5be877).

The two model modules share one declarative Base and cannot be loaded in
one process (test_config_json_mutation.py), so the fallback is loaded in a
child interpreter with the sibling repo off sys.path (it is then what
``integrations.social.models`` resolves to, the way central runs) and the
canonical models in this one.  Skipped when the sibling repo is not checked
out beside HARTOS (test_consent_fanout_p3._needs).  Tables only the
fallback defines (the facade's own site_pages, page_layouts, pdf_files)
are not compared; a column only the canonical model has is allowed: code
written against it fails loudly on the fallback, which every test runs.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

HARTOS_ROOT = pathlib.Path(__file__).resolve().parents[2]
#: The canonical repo: beside HARTOS, or wherever HARTOS_TEST_HEVOLVE_DB_ROOT
#: points (a checkout of another commit, to run this guard against it).
HEVOLVE_DB_ROOT = pathlib.Path(os.environ.get('HARTOS_TEST_HEVOLVE_DB_ROOT')
                               or HARTOS_ROOT.parent / 'Hevolve_Database')

_CHILD = r'''
import inspect, json, os, sys
sys.path.insert(0, %r)
os.environ['HEVOLVE_DB_PATH'] = ':memory:'
import integrations.social.models as m
tables = {}
for name in dir(m):
    obj = getattr(m, name)
    table = getattr(obj, '__table__', None)
    if isinstance(obj, type) and table is not None \
            and isinstance(getattr(obj, '__tablename__', None), str):
        tables[obj.__tablename__] = sorted(c.name for c in table.columns)
print(json.dumps({'file': inspect.getfile(m.User), 'tables': tables}))
'''


def _columns_of(models_module):
    out = {}
    for name in dir(models_module):
        obj = getattr(models_module, name)
        table = getattr(obj, '__table__', None)
        if isinstance(obj, type) and table is not None \
                and isinstance(getattr(obj, '__tablename__', None), str):
            out[obj.__tablename__] = {c.name for c in table.columns}
    return out


def _fallback_columns():
    """{tablename: {column}} of the models integrations.social.models resolves
    to with no canonical package on sys.path: the fallback, as central runs."""
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    env['HEVOLVE_DB_PATH'] = ':memory:'
    run = subprocess.run([sys.executable, '-c', _CHILD % str(HARTOS_ROOT)],
                         capture_output=True, text=True, timeout=600, env=env,
                         cwd=str(HARTOS_ROOT))
    assert run.returncode == 0, run.stderr[-2000:]
    doc = json.loads(run.stdout.strip().splitlines()[-1])
    assert doc['file'].replace('\\', '/').endswith('/_models_local.py'), \
        'the child did not resolve to the fallback: %s' % doc['file']
    return {t: set(cols) for t, cols in doc['tables'].items()}


@pytest.fixture(scope='module')
def canonical():
    if not (HEVOLVE_DB_ROOT / 'sql' / 'models.py').is_file():
        pytest.skip('sibling repo Hevolve_Database not checked out beside HARTOS')
    sys.path.insert(0, str(HEVOLVE_DB_ROOT))
    os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
    try:
        import sql.models as canonical_models
    finally:
        sys.path.remove(str(HEVOLVE_DB_ROOT))
    return _columns_of(canonical_models)


def test_no_fallback_column_is_missing_from_the_canonical_model(canonical):
    fallback = _fallback_columns()
    shared = sorted(set(fallback) & set(canonical))
    assert len(shared) >= 80, 'the two model sets barely overlap: %s' % shared
    drift = {t: sorted(fallback[t] - canonical[t]) for t in shared
             if fallback[t] - canonical[t]}
    assert not drift, (
        'columns only the fallback declares; the installed desktop runs the '
        'canonical model and raises on them -- add them to Hevolve_Database '
        'sql/models.py with a migration (precedent c5be877 reopened_at): %r'
        % drift)


def test_the_consent_column_v58_reads_is_on_both_models(canonical):
    """The exact column behind the 2026-10-18 refusals, by name, so the
    failure message says what broke even if the general check is widened."""
    fallback = _fallback_columns()
    assert 'reopened_at' in fallback['user_consents']
    assert 'reopened_at' in canonical['user_consents']
