"""One place decides whether a GGUF fits the GPU: model_catalog.gguf_fits_gpu.

WHY A SOURCE GUARD, AND WHY ON VOCABULARY RATHER THAN SYMBOLS.  The site
this guard was written for -- ``llamacpp_manager.get_optimal_params`` --
was missed by THREE independent collapses (the ctx-tier migration, which
rewrote the line directly below it; the moe_offload_args rollout, which
reached the other three spawns; the fit-rule unification of 4b16e8edf)
because each enumerated by SYMBOL.  A hand-rolled

    if model_size_gb > 0 and free_vram >= model_size_gb * 1.1:
        ...
    ratio = free_vram / model_size_gb

references no shared symbol, so no grep for ``gguf_fits_gpu`` or
``matches_compute`` could ever have found it.  A guard that asserts "every
fit decision calls gguf_fits_gpu" would be blind the same way: a fifth
hand-rolled rule that imports nothing passes it.  So this file asserts the
invariant on the concept's VOCABULARY -- an arithmetic comparison of a
free-VRAM quantity against a model-size quantity -- and allows it in exactly
one file.  The patterns are derived from the offending lines themselves and
from the copies the two repos have retired (Nunba main.py's
``(free_vram + free_ram) >= vram``, removed in 1bb8f4ac).

Source guards are acceptable only beside behavioural tests
(memory/feedback_no_grep_tests.md).  The behaviour --
``get_optimal_params`` asking the one rule, placing a MoE with
``--cpu-moe``, sizing a partial offload from the file's block count -- is
proven in ``tests/unit/test_get_optimal_params_asks_the_one_fit_rule.py``;
the rule itself in ``test_gguf_fit_is_one_rule.py``.  What only this file
can catch is the NEXT private copy, in a file nothing tests yet.

Modelled on ``test_source_guard_one_ctx_size_authority.py``: one cached scan
per repo, the Nunba half reported as skipped-with-a-reason when absent, and
the instrument proven against the positive case before it is trusted
(memory/feedback_validate_the_measurement_before_the_verdict).
"""
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

#: Directory names never worth walking (same set as the ctx-size guard).
_PRUNE = {
    '.git', '.venv', 'venv', 'venv311', '__pycache__', 'node_modules',
    'python-embed', 'build', 'dist', 'site-packages', '.review-data',
    '.mypy_cache', '.pytest_cache', '.claude', 'htmlcov', '.tox', '.eggs',
    '.pycharm_plugin',
}

#: The ONE file allowed to compare free VRAM with a model size.  Holds
#: gguf_fits_gpu (the rule), llama_gguf_compute_requirements (the sizing
#: it is asked with) and moe_offload_args (the "fits whole?" question for a
#: mixture of experts).  Kept as a literal on purpose: a guard that imports
#: the thing it guards cannot fail when that thing moves.
AUTHORITY = 'integrations/service_tools/model_catalog.py'

# ── the vocabulary ───────────────────────────────────────────────────────────
# A model-size quantity, by the names both repos have used for one.
_SIZE = (r'(?:model_size|model_gb|size_gb|size_mb|weight\w*|whole\w*|'
         r'vram_need\w*|need_vram\w*|vram)\w*')
# A free-VRAM reading.
_FREE = r'free_(?:vram|gpu)\w*'

#: ``free_vram >= model_size_gb * 1.1`` (llamacpp_manager :582, retired),
#: ``free_vram >= vram`` and ``(free_vram + free_ram) >= vram`` (Nunba
#: main.py, retired in 1bb8f4ac): a free-VRAM reading, optionally with free
#: RAM added first, compared against a size quantity.
FREE_VS_SIZE = re.compile(
    r'\(?\s*' + _FREE + r'\s*(?:\+\s*free_ram\w*\s*\)?\s*)?(?:>=|<=|>|<)\s*'
    r'\(?\s*(?:[\w\.\[\]\'"]*\.)?' + _SIZE)

#: ``ratio = free_vram / model_size_gb`` (llamacpp_manager :591, retired):
#: the ratio that sized the layer guess.
FREE_OVER_SIZE = re.compile(
    r'\b' + _FREE + r'\s*/\s*(?:[\w\.]*\.)?' + _SIZE)

#: ``preset.size_mb <= budget_mb`` / ``size_mb > diag['compute_budget_mb']``
#: / ``size_mb > available_mb``: the same comparison with the operands
#: swapped and in megabytes.  Nunba's llama_config.py and
#: desktop/ai_installer.py still decide fit this way -- see NUNBA_KNOWN_OPEN.
SIZE_VS_BUDGET = re.compile(
    r'\b(?:model_)?size_(?:mb|gb)\s*(?:>=|<=|>|<)\s*'
    r'[\w\[\]\'"\.]*?(?:budget|available|free)\w*')

#: ``model_size_gb * 1.1``: a size scaled by a literal overhead factor,
#: which is a private restatement of llama_gguf_compute_requirements.
SIZE_TIMES_FACTOR = re.compile(
    r'\b(?:model_size\w*|model_gb|size_gb|weights?_gb|whole\w*)\s*\*\s*\d+\.\d+')

PATTERNS = {
    'free-VRAM compared with a model size': FREE_VS_SIZE,
    'free VRAM divided by a model size': FREE_OVER_SIZE,
    'model size compared with a VRAM budget': SIZE_VS_BUDGET,
    'model size times a literal overhead factor': SIZE_TIMES_FACTOR,
}

#: The retired lines, verbatim.  The instrument is proven against these
#: before any verdict is trusted; putting any of them back must go red.
RETIRED_LINES = (
    # HARTOS llamacpp_manager.get_optimal_params, until 2026-09-23
    "if model_size_gb > 0 and free_vram >= model_size_gb * 1.1:",
    "ratio = free_vram / model_size_gb",
    # Nunba main.py _gguf_install_files._fits_compute, until 1bb8f4ac
    "if compute_state.get('gpu_available') and free_vram >= vram:",
    "if (free_vram + free_ram) >= vram:",
    # Nunba llama_config.py, still live (NUNBA_KNOWN_OPEN)
    "if preset.size_mb <= budget_mb and preset.size_mb > best_size:",
    "if diag['run_mode'] == 'gpu' and preset.size_mb > diag['compute_budget_mb']:",
)

#: Lines that look adjacent but are NOT fit rules, and must stay silent:
#: the ctx-geometry headroom subtraction, sidecar VRAM budgets, an
#: inference-headroom check, a log-rotation size cap, "is there a GPU".
INNOCENT_LINES = (
    "remaining = float(free_gib) - float(model_gib)",
    "if vram_manager.get_free_vram() < budget[1]:",
    "if free_gb >= needed_gb:",
    "if os.path.getsize(path) <= max_bytes:",
    "if free_vram_gb > 0.5 and not gpu_occupied:",
    "if gpu_available and free_vram > 0.5:",
)

#: Nunba files KNOWN to still carry a hand-rolled fit rule (the size_mb
#: budget family in auto_setup / diagnose / the installer's preset pick).
#: A census, not an allowlist: the test below fails in BOTH directions --
#: a new Nunba file with a rule cannot appear unnoticed, and a fixed file
#: must be removed from here so the entry cannot go stale.  Collapsing
#: these onto gguf_fits_gpu is Nunba's change to make, reported as open.
NUNBA_KNOWN_OPEN = {
    'desktop/ai_installer.py',
    'llama/llama_config.py',
}


def _nunba_root():
    """Where the Nunba companion repo lives, or None."""
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


_SCAN_CACHE: dict = {}


def _walk_py(root):
    """``[(relative_posix_path, source)]`` for every non-test .py under root."""
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
            if rel.startswith('tests/') or '/tests/' in rel:
                continue
            try:
                with open(full, 'r', encoding='utf-8') as fh:
                    files.append((rel, fh.read()))
            except (OSError, UnicodeDecodeError):
                continue
    _SCAN_CACHE[root] = files
    return files


def matches(line):
    """The pattern names a single line trips, [] for an innocent line."""
    return [name for name, pat in PATTERNS.items() if pat.search(line)]


def offending_lines(src):
    """``[(lineno, line, [pattern names])]`` -- comments excluded, because a
    comment that DESCRIBES the retired rule is how the fix explains itself."""
    hits = []
    for i, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        names = matches(line)
        if names:
            hits.append((i, stripped, names))
    return hits


def _report(label, rel, hits):
    return '\n'.join(f'  {label}:{rel}:{n} [{", ".join(names)}] {line[:100]}'
                     for n, line, names in hits)


class TheInstrumentIsProvenFirst(unittest.TestCase):
    """A zero is a hypothesis until the signal has a path to appear."""

    def test_every_retired_rule_trips_a_pattern(self):
        missed = [line for line in RETIRED_LINES if not matches(line)]
        self.assertFalse(
            missed,
            'the vocabulary no longer recognises a rule this guard exists '
            'to keep out:\n  ' + '\n  '.join(missed))

    def test_the_innocent_lines_stay_silent(self):
        tripped = [(line, matches(line)) for line in INNOCENT_LINES
                   if matches(line)]
        self.assertFalse(
            tripped,
            'the vocabulary over-reaches into lines that are not fit '
            'rules:\n  ' + '\n  '.join(f'{m} <- {l}' for l, m in tripped))

    def test_the_authority_file_contains_the_rule(self):
        """The one allowed file must actually trip the scan -- otherwise
        the allowlist is decorative and the scan sees nothing."""
        src = dict(_walk_py(REPO))[AUTHORITY]
        hits = offending_lines(src)
        self.assertTrue(
            hits, f'{AUTHORITY} no longer compares free VRAM with a model '
                  'size at all; either the rule moved (update AUTHORITY) or '
                  'the patterns went blind')
        self.assertTrue(
            any('def gguf_fits_gpu' in line for line in src.splitlines()),
            'gguf_fits_gpu is not defined in the authority file')


class OneGgufFitAuthorityInHartos(unittest.TestCase):
    """No HARTOS file but model_catalog.py compares free VRAM with a size."""

    def test_no_other_file_carries_a_fit_comparison(self):
        offenders = []
        for rel, src in _walk_py(REPO):
            if rel == AUTHORITY:
                continue
            hits = offending_lines(src)
            if hits:
                offenders.append(_report('HARTOS', rel, hits))
        self.assertFalse(
            offenders,
            'a hand-rolled "does this model fit the GPU" rule exists outside '
            f'{AUTHORITY}:\n' + '\n'.join(offenders) + '\n'
            'One rule: model_catalog.gguf_fits_gpu, asked at the knowledge '
            'level you have (a banked residency, else the file size through '
            'llama_gguf_compute_requirements).  llamacpp_manager.'
            'get_optimal_params carried a private `free_vram >= size * 1.1` '
            'for months and refused --cpu-moe to a MoE the same card had '
            'already served; see test_get_optimal_params_asks_the_one_fit_'
            'rule.py for the shape a caller takes.')

    def test_the_spawn_asks_the_authority_and_the_placement_helper(self):
        """Being clean of a private rule is necessary, not sufficient: the
        spawn must positively consult both shared answers."""
        src = dict(_walk_py(REPO))['integrations/service_tools/llamacpp_manager.py']
        self.assertIn('gguf_fits_gpu(', src)
        self.assertIn('moe_offload_args(', src)


class TheNunbaCensus(unittest.TestCase):
    """Nunba's remaining hand-rolled rules are counted, in both directions."""

    def test_nunba_files_with_a_fit_rule_are_exactly_the_census(self):
        nunba = _nunba_root()
        if nunba is None:
            self.skipTest(
                'Nunba companion repo not found (set NUNBA_REPO, or check out '
                'Nunba-HART-Companion beside HARTOS) -- the cross-repo half of '
                'this guard did NOT run')
        found = {}
        for rel, src in _walk_py(nunba):
            hits = offending_lines(src)
            if hits:
                found[rel] = hits
        new = sorted(set(found) - NUNBA_KNOWN_OPEN)
        fixed = sorted(NUNBA_KNOWN_OPEN - set(found))
        self.assertFalse(
            new,
            'a NEW hand-rolled fit rule appeared in Nunba:\n'
            + '\n'.join(_report('Nunba', rel, found[rel]) for rel in new)
            + '\nAsk model_catalog.gguf_fits_gpu (importable from '
              'models.catalog) instead, as main.py does since 1bb8f4ac.')
        self.assertFalse(
            fixed,
            f'{fixed} no longer carries a hand-rolled fit rule -- remove it '
            'from NUNBA_KNOWN_OPEN so the census stays a census.')

    def test_the_guard_can_actually_see_the_nunba_repo(self):
        nunba = _nunba_root()
        if nunba is None:
            self.skipTest('Nunba companion repo not found -- the cross-repo '
                          'half of this guard did NOT run')
        self.assertTrue(
            os.path.isfile(os.path.join(nunba, 'llama', 'llama_config.py')),
            f'NUNBA_REPO resolved to {nunba} but the spawner is not there')


if __name__ == '__main__':
    unittest.main()
