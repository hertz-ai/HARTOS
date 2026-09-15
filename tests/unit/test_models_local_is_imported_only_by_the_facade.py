"""integrations.social._models_local is imported by integrations.social.models
and by nothing else.

_models_local declares every social table on the SAME declarative Base the
facade uses (line 25: ``from integrations.social.models import Base``).  It
is the fallback for when hevolve-database (sql.models) is not installed.
On an install that HAS sql.models, executing it a second time registers a
second User, Post, Comment, ... on that Base, and from then on every
relationship resolved by bare class name fails:

    sqlalchemy.exc.InvalidRequestError: Multiple classes found for path
    "Post" in the registry of this declarative base.

Live 2026-09-15 12:21:32 (Nunba, HARTOS bbf9653d3): hartos/helper.py
get_time_based_history imported ConversationEntry from _models_local, its
own query failed with that error one line later, and every later write on
the social DB failed the same way for the life of the process, including
the owner's seven clicks on the consent card (POST /api/social/consent 500,
12:30-12:50).  core/user_memory_migration.py carried the same import.

The facade exports ConversationEntry on both branches; import it from
there.  This test names every offending import site so the next one is
caught before it ships.

    python -m pytest tests/unit/test_models_local_is_imported_only_by_the_facade.py --noconftest -q
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FACADE = ROOT / 'integrations' / 'social' / 'models.py'
FALLBACK = 'integrations.social._models_local'
# Runtime code only.  A test that imports the fallback runs on the fallback
# branch of the developer venv, where the module is already the active one.
RUNTIME_DIRS = ('core', 'hartos', 'integrations', 'security', 'routes')
RUNTIME_FILES = ('hart_intelligence_entry.py',)


def _imports_fallback(path: Path):
    try:
        tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
    except SyntaxError:
        return []
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ''
            if mod == FALLBACK or (node.level and mod == '_models_local'):
                hits.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == FALLBACK for a in node.names):
                hits.append(node.lineno)
    return hits


def test_only_the_facade_imports_the_fallback_models():
    files = [ROOT / f for f in RUNTIME_FILES if (ROOT / f).is_file()]
    for d in RUNTIME_DIRS:
        files += sorted((ROOT / d).rglob('*.py')) if (ROOT / d).is_dir() else []
    offenders = []
    for f in files:
        if f.resolve() == FACADE.resolve() or f.name == '_models_local.py':
            continue
        for ln in _imports_fallback(f):
            offenders.append(f'{f.relative_to(ROOT).as_posix()}:{ln}')
    assert not offenders, (
        "import ConversationEntry (or any model) from integrations.social.models, "
        "never from _models_local; on an install with sql.models the fallback "
        f"re-registers every table on the shared Base: {offenders}")


def test_the_facade_exports_conversation_entry_on_both_branches():
    import re
    src = FACADE.read_text(encoding='utf-8')
    for opener in ('from sql.models import (', 'from integrations.social._models_local import ('):
        block = re.search(re.escape(opener) + r'(.*?)\)', src, re.S)
        assert block, f'facade import block missing: {opener}'
        assert 'ConversationEntry' in block.group(1), f'{opener} must export ConversationEntry'
