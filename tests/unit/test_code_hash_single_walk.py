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
