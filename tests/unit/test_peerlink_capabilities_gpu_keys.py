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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import patch  # noqa: E402

import integrations.service_tools.vram_manager as vram  # noqa: E402
from core.peer_link.link import PeerLink  # noqa: E402


def test_gpu_node_advertises_gpu_and_vram():
    """A CUDA node must advertise its GPU name + VRAM (the bug: it didn't)."""
    with patch.object(vram, 'detect_gpu', return_value={
            'name': 'RTX 3070', 'total_gb': 8.0,
            'free_gb': 3.0, 'cuda_available': True}):
        caps = PeerLink._get_local_capabilities()
    assert caps['gpu'] == 'RTX 3070'
    assert caps['vram_mb'] == 8192          # 8.0 GB * 1024
    assert caps['cpu_count'] >= 1


def test_cpu_node_omits_gpu():
    """No CUDA -> no gpu/vram keys (and never crashes)."""
    with patch.object(vram, 'detect_gpu', return_value={
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
