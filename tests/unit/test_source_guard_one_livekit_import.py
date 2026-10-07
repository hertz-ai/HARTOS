"""Each LiveKit package is imported in ONE module of HARTOS.

WHY A SOURCE GUARD: the invariant is "no second import site", which no
behavioural test can see -- a second ``from livekit import rtc`` behaves
exactly like the first until the day the import fails, and then the new
site swallows the error the one import site keeps and names.  That is the
collapse of HARTOS b1d46f57c: agent_voice_bridge carried its own copy of the
rtc import (its ``livekit_rtc`` never used), which kept nothing of the
failure.  Re-adding it passed all 206 tests around it (review of b1d46f57c,
mutant M1).  The behavioural half is tests/unit/
test_livekit_import_failure_is_named.py.

THE RULE
  * ``livekit.rtc`` (the realtime SDK): only integrations/social/
    _livekit_room.py, which keeps the import's error
    (LIVEKIT_RTC_IMPORT_ERROR).  The publisher, subscriber and bridge take
    ``livekit_rtc`` / ``HAS_LIVEKIT_RTC`` from it.
  * ``livekit.api`` (token signing, egress): only integrations/social/
    livekit_service.py, which keeps its error the same way.

Checked by AST, so prose and docstrings that name the packages do not count.
Tests are out of scope: they stand fakes in for livekit on purpose.
"""
import ast
import os

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Directory names never walked: vendored trees, build output, caches, the
#: private review data, and the tests themselves.
_PRUNE = {
    '.git', '.venv', 'venv', 'venv311', '__pycache__', 'node_modules',
    'python-embed', 'build', 'dist', 'site-packages', '.review-data',
    '.mypy_cache', '.pytest_cache', '.claude', 'htmlcov', '.tox', '.eggs',
    '.pycharm_plugin', 'tests',
}

#: The one module each LiveKit package may be imported in.
OWNERS = {
    'rtc': 'integrations/social/_livekit_room.py',
    'api': 'integrations/social/livekit_service.py',
}


def _livekit_packages(tree):
    """The LiveKit packages (rtc, api, or '' for bare ``livekit``) a module
    imports, with the line of each import."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split('.')
                if parts[0] == 'livekit':
                    found.append((parts[1] if len(parts) > 1 else '', node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            parts = node.module.split('.')
            if parts[0] != 'livekit':
                continue
            if len(parts) > 1:
                found.append((parts[1], node.lineno))
            else:
                found.extend((alias.name, node.lineno) for alias in node.names)
    return found


def _imports_by_file():
    out = {}
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in _PRUNE]
        for name in files:
            if not name.endswith('.py'):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, REPO).replace(os.sep, '/')
            try:
                with open(path, encoding='utf-8', errors='replace') as f:
                    tree = ast.parse(f.read(), filename=rel)
            except SyntaxError:
                continue
            found = _livekit_packages(tree)
            if found:
                out[rel] = found
    return out


def test_each_livekit_package_is_imported_in_its_one_module():
    strays = []
    for rel, found in sorted(_imports_by_file().items()):
        for package, line in found:
            if OWNERS.get(package) != rel:
                strays.append(f'{rel}:{line} imports livekit'
                              f'{"." + package if package else ""}')
    assert strays == [], (
        'LiveKit is imported outside its one module per package (OWNERS); '
        'import livekit_rtc / HAS_LIVEKIT_RTC from _livekit_room, or use '
        'LiveKitService, so a failed import is kept and named once:\n  '
        + '\n  '.join(strays))


def test_the_owners_do_import_them():
    """The guard would pass vacuously if the owners moved: pin that they are
    where the rule says."""
    found = _imports_by_file()
    for package, owner in OWNERS.items():
        assert package in {p for p, _ in found.get(owner, [])}, \
            f'{owner} no longer imports livekit.{package}; update OWNERS'
