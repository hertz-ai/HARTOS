"""Runtime integrity monitor — boot-baseline mode + tiered checking.

FT: the 10-minute tamper check finally works on bundled desktops: with no
    signed manifest (central-only by policy) the monitor arms against a
    boot baseline derived from the snapshot it already takes, and a
    one-letter edit to any tracked .py is detected on the next full verify.
NFT: performance. Steady-state cycles are stat-only sweeps (metadata, no
     file reads); the expensive byte-walk runs only when metadata moved or
     on the slow _full_every schedule.  And force_walk exists because the
     bundle pins HEVOLVE_CODE_HASH_PRECOMPUTED to a constant — without it
     the comparison could never move.
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from security import node_integrity as ni  # noqa: E402
from security.runtime_monitor import RuntimeIntegrityMonitor  # noqa: E402


@pytest.fixture
def tree(tmp_path):
    """A tiny code root with two tracked files."""
    (tmp_path / 'pkg').mkdir()
    (tmp_path / 'pkg' / 'mod.py').write_text('VALUE = 1\n', encoding='utf-8')
    (tmp_path / 'main.py').write_text('print("hello")\n', encoding='utf-8')
    return tmp_path


class TestForceWalk:

    def test_precomputed_env_wins_by_default(self, tree, monkeypatch):
        monkeypatch.setenv('HEVOLVE_CODE_HASH_PRECOMPUTED', 'a' * 64)
        assert ni.compute_code_hash(str(tree)) == 'a' * 64

    def test_force_walk_hashes_the_actual_bytes(self, tree, monkeypatch):
        """The bundle's constant must not blind a forced walk."""
        monkeypatch.setenv('HEVOLVE_CODE_HASH_PRECOMPUTED', 'a' * 64)
        walked = ni.compute_code_hash(str(tree), force_walk=True)
        assert walked != 'a' * 64
        assert len(walked) == 64

    def test_force_walk_moves_on_a_one_letter_edit(self, tree):
        before = ni.compute_code_hash(str(tree), force_walk=True)
        p = tree / 'pkg' / 'mod.py'
        p.write_text(p.read_text(encoding='utf-8') + '#', encoding='utf-8')
        after = ni.compute_code_hash(str(tree), force_walk=True)
        assert before != after

    def test_force_walk_does_not_write_the_cache(self, tree):
        ni.compute_code_hash(str(tree), force_walk=True)
        assert not (tree / 'agent_data' / 'code_hash_cache.json').exists()

    def test_manifest_fold_matches_the_walk(self, tree):
        """Format lock: baseline mode derives its expected hash by folding
        the boot snapshot — if the fold ever drifted from the walk, every
        baseline node would false-positive as tampered."""
        walked = ni.compute_code_hash(str(tree), force_walk=True)
        folded = ni.manifest_to_code_hash(ni.compute_file_manifest(str(tree)))
        assert folded == walked


class TestBootBaselineMode:

    def test_arms_without_a_manifest(self, tree):
        mon = RuntimeIntegrityMonitor(None, code_root=str(tree))
        assert mon._baseline_mode is True
        # __init__ must stay cheap — the walk happens on the monitor's own
        # thread (it wedged hartos-bootstrap for minutes when it ran on the
        # caller's).  Before preparation there is no expected hash yet.
        assert mon._expected_hash == ''
        mon._prepare_baseline()
        assert len(mon._expected_hash) == 64

    def test_baseline_ignores_the_bundle_constant(self, tree, monkeypatch):
        """The bundle sets HEVOLVE_CODE_HASH_PRECOMPUTED=sha256(exe|mtime);
        the baseline must come from real bytes, not that constant."""
        monkeypatch.setenv('HEVOLVE_CODE_HASH_PRECOMPUTED', 'a' * 64)
        mon = RuntimeIntegrityMonitor(None, code_root=str(tree))
        mon._prepare_baseline()
        assert mon._expected_hash != 'a' * 64
        assert mon._expected_hash == ni.compute_code_hash(
            str(tree), force_walk=True)

    def test_untouched_tree_is_healthy(self, tree):
        mon = RuntimeIntegrityMonitor(None, code_root=str(tree))
        mon._check_loop_once_for_test()
        assert mon.is_healthy is True

    def test_one_letter_edit_is_detected(self, tree):
        mon = RuntimeIntegrityMonitor(None, code_root=str(tree))
        # Baseline BEFORE the edit — prepared lazily at check time it would
        # absorb the tampered state as "boot".
        mon._prepare_baseline()
        p = tree / 'main.py'
        p.write_text(p.read_text(encoding='utf-8').replace('hello', 'hellp'),
                     encoding='utf-8')
        mon._check_loop_once_for_test()
        assert mon.is_healthy is False

    def test_manifest_mode_is_unchanged(self, tree):
        mon = RuntimeIntegrityMonitor({'code_hash': 'b' * 64},
                                      code_root=str(tree))
        assert mon._baseline_mode is False
        assert mon._expected_hash == 'b' * 64


class TestStatSweepTier:

    def test_quiet_tree_reports_no_change(self, tree):
        mon = RuntimeIntegrityMonitor(None, code_root=str(tree))
        mon._prepare_baseline()
        assert mon._stat_sweep() == mon._stat_baseline

    def test_edit_moves_the_sweep(self, tree):
        mon = RuntimeIntegrityMonitor(None, code_root=str(tree))
        mon._prepare_baseline()
        p = tree / 'pkg' / 'mod.py'
        p.write_text(p.read_text(encoding='utf-8') + '# changed\n',
                     encoding='utf-8')
        assert mon._stat_sweep() != mon._stat_baseline

    def test_new_file_moves_the_sweep(self, tree):
        mon = RuntimeIntegrityMonitor(None, code_root=str(tree))
        mon._prepare_baseline()
        (tree / 'injected.py').write_text('EVIL = True\n', encoding='utf-8')
        assert mon._stat_sweep() != mon._stat_baseline

    def test_full_every_is_env_tunable(self, tree, monkeypatch):
        monkeypatch.setenv('HEVOLVE_TAMPER_FULL_EVERY', '4')
        mon = RuntimeIntegrityMonitor(None, code_root=str(tree))
        assert mon._full_every == 4


class TestDevnullSurvivesUnicode:
    """io_guard's discard streams must accept ALL of Unicode.

    Regression for the 2026-08-23 production failure: a bare
    open(os.devnull, 'w') is cp1252 on Windows and encodes BEFORE
    discarding, so printing '\u2192' through the silenced stdout raised
    UnicodeEncodeError and killed the whole agent turn (the final action
    of a six-action creation flow failed on every retry).
    """

    def test_silenced_stdout_swallows_arrows(self, monkeypatch):
        import io as _io
        import sys as _sys
        from core.io_guard import silence_stdio
        closed = _io.StringIO()
        closed.close()
        monkeypatch.setattr(_sys, 'stdout', closed)
        monkeypatch.setattr(_sys, 'stderr', closed)
        silence_stdio()
        try:
            print('recipe_received \u2192 terminated')
            print('unicode: \u2713 \u00e9 \U0001f41d')
        finally:
            pass  # monkeypatch restores the real streams

    def test_safe_iostream_rearm_is_unicode_safe(self, monkeypatch):
        import os as _os
        import sys as _sys
        from core.io_guard import _SafeIOStream
        # stdout closed underneath — print() raises, stream re-arms
        broken = open(_os.devnull, 'w', encoding='utf-8')
        broken.close()
        monkeypatch.setattr(_sys, 'stdout', broken)
        s = _SafeIOStream()
        s.print('first \u2192 drops and re-arms')
        # the re-armed stream must then take arrows without raising
        s.print('second \u2192 must not raise')
