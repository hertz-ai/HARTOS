"""Two callers that miss the code-hash cache together walk the tree once.

Measured 2026-10-21 on a dev boot (thread dump): two boot threads,
``hartos-bootstrap`` via start_relay_hub and ``platform-init`` via telemetry,
both missed the cache and ran compute_code_hash's full walk side by side,
hashing every file twice while init_social waited behind them.

The second caller now waits for the first and reads the cache it wrote.

Run:
  pytest tests/unit/test_code_hash_single_walk.py -v --noconftest
"""
import threading
import time
from pathlib import Path

from security import node_integrity as ni


def _tree(tmp_path, n=5):
    for i in range(n):
        (tmp_path / f'mod{i}.py').write_text(f'x = {i}\n', encoding='utf-8')
    return n


def test_concurrent_cache_misses_walk_once(tmp_path, monkeypatch):
    monkeypatch.delenv('HEVOLVE_CODE_HASH_PRECOMPUTED', raising=False)
    n = _tree(tmp_path)
    hashed = []
    real_hash = ni._hash_file

    def _slow_hash(path):
        hashed.append(path)
        time.sleep(0.1)          # a real tree takes long enough to overlap
        return real_hash(path)

    monkeypatch.setattr(ni, '_hash_file', _slow_hash)

    results = []
    threads = [threading.Thread(
        target=lambda: results.append(ni.compute_code_hash(str(tmp_path))))
        for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert len(results) == 2 and results[0] == results[1]
    assert len(hashed) == n, (
        f"{len(hashed)} file hashes for {n} files: both callers walked the tree")


def test_a_forced_walk_still_hashes_the_bytes(tmp_path, monkeypatch):
    """The integrity monitor's force_walk must never be served from cache."""
    monkeypatch.delenv('HEVOLVE_CODE_HASH_PRECOMPUTED', raising=False)
    n = _tree(tmp_path)
    ni.compute_code_hash(str(tmp_path))          # warms the cache
    hashed = []
    real_hash = ni._hash_file
    monkeypatch.setattr(ni, '_hash_file',
                        lambda p: hashed.append(p) or real_hash(p))

    ni.compute_code_hash(str(tmp_path), force_walk=True)
    assert len(hashed) == n


def test_the_hash_is_unchanged_by_the_lock(tmp_path, monkeypatch):
    monkeypatch.delenv('HEVOLVE_CODE_HASH_PRECOMPUTED', raising=False)
    _tree(tmp_path)
    forced = ni.compute_code_hash(str(tmp_path), force_walk=True)
    assert ni.compute_code_hash(str(tmp_path)) == forced


def _slow_counting_hash(monkeypatch, hashed, delay=0.1, gate=None):
    real_hash = ni._hash_file

    def _hash(path):
        hashed.append(path)
        if gate is not None:
            gate(path)
        time.sleep(delay)
        return real_hash(path)

    monkeypatch.setattr(ni, '_hash_file', _hash)


def test_callers_reuse_the_walk_when_the_cache_cannot_be_written(
        tmp_path, monkeypatch):
    """Review of #142 (20 Nov, measured): on a root whose cache cannot be
    written (read-only, no HEVOLVE_CODE_HASH_PRECOMPUTED) three callers
    still hashed the tree three times, one after another, because each
    waiter found no cache when its turn came."""
    monkeypatch.delenv('HEVOLVE_CODE_HASH_PRECOMPUTED', raising=False)
    monkeypatch.setattr(ni, '_save_code_hash_cache', lambda root, h: None)
    n = _tree(tmp_path)
    hashed = []
    _slow_counting_hash(monkeypatch, hashed)

    results = []
    threads = [threading.Thread(
        target=lambda: results.append(ni.compute_code_hash(str(tmp_path))))
        for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert len(results) == 3 and len(set(results)) == 1
    assert len(hashed) == n, f"{len(hashed)} file hashes for {n} files"


def test_a_changed_file_is_walked_again_after_an_unsaved_walk(
        tmp_path, monkeypatch):
    """The in-memory result is stale as soon as a file is newer than it."""
    monkeypatch.delenv('HEVOLVE_CODE_HASH_PRECOMPUTED', raising=False)
    monkeypatch.setattr(ni, '_save_code_hash_cache', lambda root, h: None)
    _tree(tmp_path)
    first = ni.compute_code_hash(str(tmp_path))
    time.sleep(0.05)
    (tmp_path / 'mod0.py').write_text('x = "changed"\n', encoding='utf-8')
    assert ni.compute_code_hash(str(tmp_path)) != first


def test_two_roots_do_not_wait_for_each_other(tmp_path, monkeypatch):
    """Review of #142 (nit, measured 0.62 s -> 1.05 s): one lock covered every
    root, so a walk of one root held up a caller of another."""
    monkeypatch.delenv('HEVOLVE_CODE_HASH_PRECOMPUTED', raising=False)
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    _tree(a)
    _tree(b)
    release = threading.Event()

    def _hold_root_a(path):
        if a in Path(path).parents:
            release.wait(10)

    _slow_counting_hash(monkeypatch, [], delay=0, gate=_hold_root_a)
    walker = threading.Thread(target=lambda: ni.compute_code_hash(str(a)))
    walker.start()
    time.sleep(0.3)                       # root a's walk is under way
    t0 = time.monotonic()
    try:
        ni.compute_code_hash(str(b))
        elapsed = time.monotonic() - t0
    finally:
        release.set()
        walker.join(30)
    assert elapsed < 5, f"root b waited {elapsed:.1f}s for root a's walk"
