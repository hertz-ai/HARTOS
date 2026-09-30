"""One name and one derivation for llama-server's ``--ctx-size``, both repos.

WHY A SOURCE GUARD (same rule as the sibling
``test_source_guard_llama_ctx_size_agrees.py``): the thing being protected is a
DRY invariant spread over two repos and five spawn sites.  No behavioural test
can observe "somebody added a second env-var name in the other repo" — Python
imports one module at a time, and Nunba's spawner needs a GPU, a 2.9 GB gguf
and a free port before it emits anything.  So the invariant is asserted against
the declaring text, and the file is named ``test_source_guard_*`` as the rule
requires.  The behavioural half is already covered by
``Nunba/tests/test_ctx_size_model_size_unit.py`` (the tier decision) and
``tests/unit/test_wire_budget_reads_the_live_server.py`` (the reader).

THE FRAGMENTATION IT GUARDS (measured by grep across both repos, 2026-09-22)
───────────────────────────────────────────────────────────────────────────
TWO env-var names for one concept:

  * ``HEVOLVE_LLAMA_CTX_SIZE`` — written by Nunba ``llama/llama_config.py``
    beside the ``--ctx-size`` it hands the server, read by HARTOS
    ``core/llm_outbound_logger._get_budget_per_slot``.  Live and correct.
  * ``HEVOLVE_LLM_CTX_SIZE``  — read by
    ``integrations/service_tools/model_lifecycle.py:1797``.  NOTHING in either
    repo has ever written it, so that spawn always took the literal ``8192``
    default and was disconnected from the real derivation.  A declared
    override that no producer feeds is dead config
    (``memory/feedback_declaration_is_not_a_guard.md``).

THREE independent ``--ctx-size`` derivations:

  * Nunba ``_derive_ctx_size``  — VRAM tiers 16384/8192/4096, capped.  The
    real one; it is the only site that measures the box.
  * ``model_lifecycle``         — the dead env var above, else ``8192``.
  * ``llamacpp_manager``        — its own ladder 10240/8192/4096/2048, else
    ``4096``.
  * ``vision/lightweight_backend`` — the literal ``512`` twice.

Three ladders cannot agree by inspection, and the wire trimmer budgets against
ONE of them.  When the number it budgets against is not the number the server
was launched with, the "zero-tolerance context overflow" guard passes requests
the server then refuses — the exact 78.7 %-overflow shape measured on
2026-08-07 and again on 2026-09-11.

THE RULE THIS ENFORCES
──────────────────────
1. Exactly ONE env-var name for n_ctx in Python code:
   ``core.llama_geometry.CTX_SIZE_ENV`` (= ``HEVOLVE_LLAMA_CTX_SIZE``).
   ``HART_LLM_CTX_SIZE`` survives as the systemd/Nix DEPLOY-scoped spelling —
   expanded by systemd itself in ``hart-llm.service``, never read by Python,
   already pinned to ``LLAMA_CTX_SIZE_DEFAULT`` by the sibling guard.  Letting
   Python read it too is what would make it a second live name, so that is
   what this file forbids.
2. Every ``--ctx-size`` a spawn hands llama-server comes from
   ``core.llama_geometry`` — never a bare integer literal at the call site,
   and never from a file outside the sanctioned spawn set.
"""
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

#: Directory names never worth walking: vendored trees, build output, caches,
#: and HARTOS's ``.review-data`` (holds private keys — never read it).
#:
#: ``.pycharm_plugin`` is TrueFlow's IDE runtime-injector, vendored verbatim
#: into BOTH repos (``runtime_injector/local_llm_server.py``,
#: ``trueflow_mcp_hub.py``, ``trueflow_mcp_server.py`` — each spawns its own
#: llama-server at a hardcoded 4096/16384).  It is NOT first-party code and no
#: HARTOS/Nunba module imports it, which
#: :meth:`VendoredInjectorIsInert.test_no_first_party_module_imports_the_injector`
#: asserts rather than assumes — an exclusion nothing proves is how a zero
#: becomes a false green (memory/feedback_never_assume.md, rule 4).  It is
#: still a real second spawner in the TrueFlow product and is reported as such;
#: it cannot be fixed from these two repos because the file of record lives in
#: ``TrueFlow/src/main/resources/runtime_injector/``.
_PRUNE = {
    '.git', '.venv', 'venv', '__pycache__', 'node_modules', 'python-embed',
    'build', 'dist', 'site-packages', '.review-data', '.mypy_cache',
    '.pytest_cache', '.claude', 'htmlcov', '.tox', '.eggs',
    '.pycharm_plugin',
}

#: An env-var name for n_ctx: ends in ``_CTX_SIZE`` so the Python constant
#: ``LLAMA_CTX_SIZE_DEFAULT`` (which is not an env var) is not swept up.
_ENV_NAME = re.compile(r"""['"]([A-Z][A-Z0-9_]*_CTX_SIZE)['"]""")

#: The ONE name Python may use.  Kept as a literal here on purpose: a guard
#: that imports the value it is guarding cannot fail when the value changes.
CANONICAL_CTX_ENV = 'HEVOLVE_LLAMA_CTX_SIZE'

#: The deploy-scoped spelling, and the ONLY Python files allowed to name it —
#: the sibling guard that pins the systemd unit + Nix module + env template to
#: ``core/constants.py::LLAMA_CTX_SIZE_DEFAULT``.
DEPLOY_CTX_ENV = 'HART_LLM_CTX_SIZE'
DEPLOY_SCOPED_FILES = {
    # The sibling guard, which pins the systemd unit + Nix module + env
    # template to core/constants.py::LLAMA_CTX_SIZE_DEFAULT.
    'tests/unit/test_source_guard_llama_ctx_size_agrees.py',
    # The module that OWNS the vocabulary.  Somewhere has to state "this
    # deploy-scoped spelling exists, here is its scope, and Python must not
    # read it" — a rule nobody writes down is a rule the next contributor
    # re-discovers by breaking it.  That declaration belongs beside the
    # canonical name, and the real risk it guards (a Python READER) is checked
    # separately by test_python_never_reads_the_deploy_scoped_name, which
    # exempts nothing.
    'core/llama_geometry.py',
}

#: Files that may hand ``--ctx-size`` to a llama-server process.  Every one of
#: them must take its value from ``core.llama_geometry``; this set exists so a
#: SIXTH spawner cannot appear without a reviewer noticing.
SANCTIONED_SPAWN_FILES = {
    # Nunba — the chokepoint (owner ruling 2026-09-13, one llama-server).
    'llama/llama_config.py',
    # HARTOS — standalone / Docker / HART OS fallbacks, each adopt-guarded.
    'integrations/service_tools/model_lifecycle.py',
    'integrations/service_tools/llamacpp_manager.py',
    'integrations/vision/lightweight_backend.py',
}

#: ``'--ctx-size', '512'`` / ``"--ctx-size", 4096`` — a number written at the
#: call site is a second derivation by definition.
_LITERAL_CTX = re.compile(r"""--ctx-size['"]\s*,\s*['"]?\d+""")

#: A ``--ctx-size`` that is actually handed to a process: a QUOTED argv token.
#: Prose must not trip this — ``core/constants.py`` and
#: ``core/llm_outbound_logger.py`` both *describe* the flag ("must match the
#: --ctx-size cmdline"), and counting a comment as a spawn would have put two
#: pure-reader modules on the allowlist, which is the opposite of the point.
_SPAWN_CTX = re.compile(r"""['"]--ctx-size['"]""")

#: ``os.environ['X']`` / ``os.environ.get('X')`` / ``os.getenv('X')``.
_ENV_READ = re.compile(
    r"""(?:os\.environ(?:\.get)?\s*[\(\[]|getenv\s*\()\s*['"]([A-Z][A-Z0-9_]*_CTX_SIZE)['"]""")


def _nunba_root():
    """Where the Nunba companion repo lives, or None.

    Order: explicit ``NUNBA_REPO`` → sibling checkout → the directory that
    actually provides ``llama/llama_config.py`` on this interpreter's path.
    """
    env = os.environ.get('NUNBA_REPO')
    if env and os.path.isdir(os.path.join(env, 'llama')):
        return env
    sibling = os.path.join(os.path.dirname(REPO), 'Nunba-HART-Companion')
    if os.path.isdir(os.path.join(sibling, 'llama')):
        return sibling
    for entry in sys.path:
        if entry and os.path.isfile(os.path.join(entry, 'llama', 'llama_config.py')):
            return entry
    return None


#: ``root -> [(relative_posix_path, source)]``.  Both repos are several
#: thousand files; scanning them once per assertion turned a source guard into
#: a multi-minute CI step, and a guard people are tempted to skip is a guard
#: that stops running.  One scan, shared by every test in this file, which also
#: means every assertion judges the SAME snapshot.
_SCAN_CACHE: dict = {}


def _walk_py(root):
    """``[(relative_posix_path, source)]`` for every .py under ``root``."""
    cached = _SCAN_CACHE.get(root)
    if cached is not None:
        return cached
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE]
        for name in filenames:
            if not name.endswith('.py'):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, '/')
            try:
                with open(full, 'r', encoding='utf-8') as fh:
                    files.append((rel, fh.read()))
            except (OSError, UnicodeDecodeError):
                continue
    _SCAN_CACHE[root] = files
    return files


def _scan_roots():
    """``[(label, root)]`` for every repo this guard can see.

    The Nunba half is reported as skipped-with-a-reason rather than silently
    passing: an absence claim needs to prove the signal had a path to appear
    (``memory/feedback_never_assume.md``).
    """
    roots = [('HARTOS', REPO)]
    nunba = _nunba_root()
    if nunba:
        roots.append(('Nunba', nunba))
    return roots, nunba


class OneEnvVarNameForNCtx(unittest.TestCase):
    """Two spellings of one setting always drift; one of them wins silently."""

    def test_python_names_exactly_one_ctx_size_env_var(self):
        roots, _ = _scan_roots()
        offenders = []
        for label, root in roots:
            for rel, src in _walk_py(root):
                if rel == 'tests/unit/' + os.path.basename(__file__):
                    continue
                for name in set(_ENV_NAME.findall(src)):
                    if name == CANONICAL_CTX_ENV:
                        continue
                    if name == DEPLOY_CTX_ENV and rel in DEPLOY_SCOPED_FILES:
                        continue
                    offenders.append(f'{label}:{rel} -> {name}')
        self.assertFalse(
            sorted(offenders),
            "a SECOND env-var name for llama-server's n_ctx exists in Python.\n"
            "  %s\n"
            "One concept, one name: %s, published by "
            "core.llama_geometry.publish_geometry and read by "
            "core.llama_geometry.ctx_size_from_env.  A name nothing writes is "
            "dead config -- HEVOLVE_LLM_CTX_SIZE was read at "
            "model_lifecycle.py:1797 for months while every spawn took its "
            "8192 literal default."
            % ('\n  '.join(sorted(offenders)), CANONICAL_CTX_ENV))

    def test_python_never_reads_the_deploy_scoped_name(self):
        """HART_LLM_CTX_SIZE belongs to systemd, which expands it itself.

        The unit is ``Type=simple`` with a direct exec, so the variable never
        reaches a Python process.  A Python reader would make it a second LIVE
        name and reopen exactly the drift the sibling guard closed.
        """
        roots, _ = _scan_roots()
        offenders = []
        for label, root in roots:
            for rel, src in _walk_py(root):
                if rel in DEPLOY_SCOPED_FILES or rel.endswith(os.path.basename(__file__)):
                    continue
                for name in set(_ENV_READ.findall(src)):
                    if name == DEPLOY_CTX_ENV:
                        offenders.append(f'{label}:{rel}')
        self.assertFalse(
            sorted(offenders),
            'Python reads %s in %s.  That variable is the systemd/Nix deploy '
            'spelling; Python reads %s.  Two live names for one number is the '
            'fragmentation this guard exists to stop.'
            % (DEPLOY_CTX_ENV, sorted(offenders), CANONICAL_CTX_ENV))


class VendoredInjectorIsInert(unittest.TestCase):
    """Proving the ``.pycharm_plugin`` exclusion, instead of assuming it.

    That tree really does spawn llama-server at a hardcoded ctx.  Excluding it
    is only honest while nothing first-party can reach it; the moment something
    imports it, it becomes a live second spawner on this box and the exclusion
    has to go.
    """

    def test_no_first_party_module_imports_the_injector(self):
        roots, _ = _scan_roots()
        needles = ('runtime_injector', 'pycharm_plugin')
        offenders = []
        for label, root in roots:
            for rel, src in _walk_py(root):
                if rel.endswith(os.path.basename(__file__)):
                    continue
                for line in src.splitlines():
                    stripped = line.lstrip()
                    if not (stripped.startswith('import ')
                            or stripped.startswith('from ')):
                        continue
                    if any(n in stripped for n in needles):
                        offenders.append(f'{label}:{rel}: {stripped[:90]}')
        self.assertFalse(
            sorted(offenders),
            'first-party code imports the vendored TrueFlow injector: %s.  '
            'That tree spawns its own llama-server at a hardcoded --ctx-size, '
            'which is the 2026-09-13 incident (a 4096-ctx CPU 2B adopted as '
            'main, every agent call HTTP 400).  Remove the import, or drop '
            '.pycharm_plugin from _PRUNE and fix the spawner.'
            % sorted(offenders))


class OneDerivationForNCtx(unittest.TestCase):
    """Every ``--ctx-size`` comes from core.llama_geometry, from a known file."""

    def test_no_spawn_site_hardcodes_a_context_size(self):
        roots, _ = _scan_roots()
        offenders = []
        for label, root in roots:
            for rel, src in _walk_py(root):
                if rel.startswith('tests/') or '/tests/' in rel:
                    continue
                if _LITERAL_CTX.search(src):
                    offenders.append(f'{label}:{rel}')
        self.assertFalse(
            sorted(offenders),
            'a literal integer is handed to --ctx-size in %s.  A number '
            'written at the call site IS a second derivation: it cannot see '
            'the box, and the wire trimmer budgets against the ONE number '
            'core.llama_geometry produced.  Call derive_ctx_size() / '
            'ctx_for_role() instead.' % sorted(offenders))

    def test_only_sanctioned_files_spawn_with_a_context_size(self):
        roots, nunba = _scan_roots()
        offenders = []
        for label, root in roots:
            for rel, src in _walk_py(root):
                if rel.startswith('tests/') or '/tests/' in rel:
                    continue
                if not _SPAWN_CTX.search(src):
                    continue
                if rel in SANCTIONED_SPAWN_FILES:
                    continue
                offenders.append(f'{label}:{rel}')
        self.assertFalse(
            sorted(offenders),
            'a NEW llama-server spawn site appeared in %s.  Owner ruling '
            '2026-09-13 (memory/feedback_one_llama_server_single_chokepoint.md): '
            'one llama-server, one chokepoint.  If this site is genuinely a '
            'separate model class, add it to SANCTIONED_SPAWN_FILES *and* make '
            'it take its ctx from core.llama_geometry.ctx_for_role().'
            % sorted(offenders))

    def test_every_sanctioned_file_consults_the_one_authority(self):
        """Being on the allowlist is not a licence to derive your own number."""
        roots, _ = _scan_roots()
        seen = {}
        for label, root in roots:
            for rel, src in _walk_py(root):
                if rel in SANCTIONED_SPAWN_FILES:
                    seen[rel] = src
        missing = [rel for rel, src in seen.items()
                   if 'llama_geometry' not in src]
        self.assertFalse(
            sorted(missing),
            '%s spawns llama-server with a --ctx-size it did not get from '
            'core.llama_geometry.  Import derive_ctx_size / ctx_for_role / '
            'ctx_size_from_env there.' % sorted(missing))

    def test_the_sanctioned_set_holds_every_spawner_that_exists(self):
        """The allowlist is a census, not a wish: no entry may be stale.

        An allowlist that names a file which no longer spawns is how a
        reviewer's attention gets spent on the wrong four lines.
        """
        roots, nunba = _scan_roots()
        if nunba is None:
            self.skipTest('Nunba repo absent — the census cannot be completed')
        found = set()
        for _label, root in roots:
            for rel, src in _walk_py(root):
                if _SPAWN_CTX.search(src) and not rel.startswith('tests/'):
                    found.add(rel)
        self.assertEqual(
            SANCTIONED_SPAWN_FILES, found,
            'SANCTIONED_SPAWN_FILES no longer matches the spawners on disk.\n'
            '  only in the allowlist (stale): %s\n'
            '  only on disk (unreviewed):     %s'
            % (sorted(SANCTIONED_SPAWN_FILES - found), sorted(found - SANCTIONED_SPAWN_FILES)))

    def test_the_guard_can_actually_see_the_nunba_repo(self):
        """A zero is a hypothesis until the signal has a path to appear.

        Without Nunba on disk the four checks above scan HARTOS only and pass
        vacuously, so say that out loud instead of reporting a green.
        """
        _, nunba = _scan_roots()
        if nunba is None:
            self.skipTest(
                'Nunba companion repo not found (set NUNBA_REPO, or check out '
                'Nunba-HART-Companion beside HARTOS) -- the cross-repo half of '
                'this guard did NOT run')
        self.assertTrue(
            os.path.isfile(os.path.join(nunba, 'llama', 'llama_config.py')),
            'NUNBA_REPO resolved to %s but the spawner is not there' % nunba)


if __name__ == '__main__':
    unittest.main()
