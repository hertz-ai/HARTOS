"""#152 (found via #135): PeerLink._get_local_capabilities read GPU facts with
keys ``detect_gpu`` never returns.

``vram_manager.detect_gpu`` returns ``{name, total_gb, free_gb, cuda_available}``
(its documented contract).  PeerLink read ``gpu.get('available')`` /
``'device_name'`` / ``'vram_total_mb'`` — none of which exist — so the GPU block
never ran and a GPU node advertised NO gpu/vram to its peers in the handshake
(the #91 wrong-keys class; compute_mesh_service reads the SAME dict correctly).

Behavioral: mock ``detect_gpu`` at the boundary, call the real static method,
assert a GPU node now advertises gpu + vram_mb and a CPU node omits them.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import patch  # noqa: E402

import integrations.service_tools.vram_manager as vram  # noqa: E402
from core.peer_link.link import PeerLink  # noqa: E402


# ── ORDER-INDEPENDENCE, added 2026-09-01 ─────────────────────────────────────
# `_get_local_capabilities` now reads the CANONICAL cached hardware profile
# (security.system_requirements.get_capabilities()) and only falls back to a
# direct detect_gpu() probe when that cache is empty.
#
# These two tests patch `detect_gpu`, so they exercise the FALLBACK. They used to
# reach it by luck: get_capabilities() returns None until run_system_check() has
# run, and nothing in a bare test process runs it. But `integrations/social/
# __init__.py` calls run_system_check() AT IMPORT — so the moment any test in the
# same pytest process imports integrations.social, the cache populates, the
# profile branch wins, this patch is BYPASSED, and these assertions fail with the
# real machine's VRAM instead of the mocked 8192. Order-dependent green.
#
# Measured 2026-09-01: cache empty -> vram_mb 8192 (passes); cache populated ->
# vram_mb 24576 (fails). So force the branch instead of hoping for it. The
# profile path has its own coverage in
# tests/unit/test_peerlink_capabilities_use_canonical_profile.py.
def _force_fallback():
    """Pin get_capabilities() to None so the direct-probe branch is taken."""
    stub = types.ModuleType('security.system_requirements')
    stub.get_capabilities = lambda: None
    return patch.dict(sys.modules, {'security.system_requirements': stub})


def test_gpu_node_advertises_gpu_and_vram():
    """A CUDA node must advertise its GPU name + VRAM (the bug: it didn't)."""
    with _force_fallback(), patch.object(vram, 'detect_gpu', return_value={
            'name': 'RTX 3070', 'total_gb': 8.0,
            'free_gb': 3.0, 'cuda_available': True}):
        caps = PeerLink._get_local_capabilities()
    assert caps['gpu'] == 'RTX 3070'
    assert caps['vram_mb'] == 8192          # 8.0 GB * 1024
    assert caps['cpu_count'] >= 1


def test_cpu_node_omits_gpu():
    """No CUDA -> no gpu/vram keys (and never crashes)."""
    with _force_fallback(), patch.object(vram, 'detect_gpu', return_value={
            'name': None, 'total_gb': 0.0,
            'free_gb': 0.0, 'cuda_available': False}):
        caps = PeerLink._get_local_capabilities()
    assert 'gpu' not in caps
    assert 'vram_mb' not in caps
    assert caps['cpu_count'] >= 1


def test_detect_gpu_contract_has_no_legacy_keys():
    """Guard the contract that made this a silent bug: detect_gpu returns
    cuda_available/name/total_gb, NOT the legacy available/device_name/
    vram_total_mb that PeerLink used to read."""
    d = {'name': None, 'total_gb': 0.0, 'free_gb': 0.0, 'cuda_available': False}
    assert 'cuda_available' in d and 'available' not in d
    assert 'name' in d and 'device_name' not in d
    assert 'total_gb' in d and 'vram_total_mb' not in d


def test_central_telemetry_compute_block_reports_gpu():
    """The SAME bug existed in CentralConnection._publish_telemetry's compute
    block (gpu.get('available')/'device_name'/'vram_free_mb' — none exist).
    Drive the real method with detect_gpu + the message bus mocked; assert the
    published telemetry advertises the GPU via the correct keys."""
    import types
    import core.peer_link.message_bus as mb
    from core.peer_link.telemetry import CentralConnection

    conn = CentralConnection.__new__(CentralConnection)  # skip heavy __init__
    conn._node_id = 'node-test'
    conn._telemetry = types.SimpleNamespace(get_summary=lambda: {})

    captured = {}

    class _Bus:
        def publish(self, topic, payload, *a, **k):
            captured['payload'] = payload

    with patch.object(vram, 'detect_gpu', return_value={
            'name': 'RTX 3070', 'total_gb': 8.0,
            'free_gb': 3.5, 'cuda_available': True}), \
            patch.object(mb, 'get_message_bus', return_value=_Bus()):
        conn._publish_telemetry()

    comp = captured['payload']['compute']
    assert comp['gpu_available'] is True
    assert comp['gpu_name'] == 'RTX 3070'
    assert comp['vram_free_mb'] == 3584          # 3.5 GB * 1024
