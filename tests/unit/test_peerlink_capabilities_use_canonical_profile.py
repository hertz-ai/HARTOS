"""PeerLink advertises the CANONICAL hardware profile — including RAM.

THE DEFECT
──────────
`PeerLink._get_local_capabilities` re-detected hardware itself: `os.cpu_count()`
plus a `detect_gpu()` probe, and **no RAM and no disk at all**. So a peer learned
our core count and our GPU and nothing whatsoever about memory.

Meanwhile the gossip announce, from the SAME machine in the SAME minute, advertised
a full `hardware_summary` — cpu_cores, ram_gb, gpu_vram_gb, disk_free_gb — built
from `security.system_requirements.get_capabilities()`. One node, two answers, and
the smaller one on the link layer.

There was no cost argument for the split: `get_capabilities()` is an O(1) read of a
module-global cache populated once by `run_system_check()`. The expensive
`detect_hardware()` (disk walk, GPU probe, and a NETWORK reachability probe) runs
once at import, not per handshake.

WHAT THESE TESTS PIN
────────────────────
1. The profile is USED when present, and RAM now reaches the wire.
2. The emitted KEY NAMES are unchanged — `cpu_count` / `gpu` / `vram_mb` / `tier`.
   Peers on older code read those, and `link_manager._evict_weakest` reads
   `capabilities.get('gpu')`. Renaming them to the profile's own vocabulary would
   be the '#91 wrong-keys' break the source comment memorialises.
3. `tier` still comes from `key_delegation.get_node_tier()` and NOT from
   `caps.tier`. Two different tiers share that word: key_delegation's is the
   TRUST/topology tier (central|regional|local); system_requirements' is the
   CAPABILITY tier (what the box can run). The handshake means the former, and
   conflating them would be the same wrong-keys class in a new place.
4. The fallback still works before `run_system_check()` has populated the cache.
"""
import os
import sys
import types
import unittest
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from core.peer_link.link import PeerLink  # noqa: E402


class _HW:
    """Stand-in for system_requirements.HardwareProfile."""
    def __init__(self, cpu_cores=16, ram_gb=64.0, gpu_vram_gb=24.0,
                 cuda_available=True, gpu_name='RTX 4090'):
        self.cpu_cores = cpu_cores
        self.ram_gb = ram_gb
        self.gpu_vram_gb = gpu_vram_gb
        self.cuda_available = cuda_available
        self.gpu_name = gpu_name


class _Profile:
    def __init__(self, hw):
        self.hardware = hw
        self.tier = types.SimpleNamespace(value='full')   # the CAPABILITY tier


def _with_profile(profile, node_tier='regional'):
    """Inject a fake system_requirements + key_delegation for one call."""
    sysreq = types.ModuleType('security.system_requirements')
    sysreq.get_capabilities = lambda: profile
    keydel = types.ModuleType('security.key_delegation')
    keydel.get_node_tier = lambda: node_tier
    return patch.dict(sys.modules, {
        'security.system_requirements': sysreq,
        'security.key_delegation': keydel,
    })


class ItUsesTheCanonicalProfile(unittest.TestCase):

    def test_RAM_now_reaches_the_wire(self):
        """The whole point: a peer could never learn our memory before."""
        with _with_profile(_Profile(_HW(ram_gb=64.0))):
            caps = PeerLink._get_local_capabilities()
        self.assertEqual(64.0, caps.get('ram_gb'),
                         "ram_gb is absent from the handshake advert — a peer still "
                         "cannot learn this node's memory, which was the defect")

    def test_cpu_comes_from_the_profile_not_a_re_probe(self):
        with _with_profile(_Profile(_HW(cpu_cores=16))):
            caps = PeerLink._get_local_capabilities()
        self.assertEqual(16, caps['cpu_count'])

    def test_gpu_and_vram_are_taken_from_the_profile(self):
        with _with_profile(_Profile(_HW(gpu_vram_gb=24.0, gpu_name='RTX 4090'))):
            caps = PeerLink._get_local_capabilities()
        self.assertEqual('RTX 4090', caps['gpu'])
        self.assertEqual(24 * 1024, caps['vram_mb'], "GB->MB conversion is wrong")

    def test_a_gpu_less_node_advertises_no_gpu_key_at_all(self):
        """Absent, not 'None' — link_manager does `capabilities.get('gpu')` and a
        truthy placeholder would score a GPU-less peer as GPU-bearing."""
        with _with_profile(_Profile(_HW(cuda_available=False))):
            caps = PeerLink._get_local_capabilities()
        self.assertNotIn('gpu', caps)
        self.assertNotIn('vram_mb', caps)


class TheWireContractIsUnchanged(unittest.TestCase):
    """Old peers read these names; renaming them is a silent break."""

    def test_the_emitted_key_names_are_the_legacy_ones(self):
        with _with_profile(_Profile(_HW())):
            caps = PeerLink._get_local_capabilities()
        for k in ('cpu_count', 'gpu', 'vram_mb', 'tier'):
            self.assertIn(k, caps, "wire key %r disappeared — peers on older code "
                                   "read it" % k)

    def test_it_did_not_leak_the_profiles_own_vocabulary(self):
        """cpu_cores / gpu_vram_gb are the PROFILE's names, not the wire's."""
        with _with_profile(_Profile(_HW())):
            caps = PeerLink._get_local_capabilities()
        for k in ('cpu_cores', 'gpu_vram_gb', 'hardware_summary'):
            self.assertNotIn(k, caps,
                             "%r leaked onto the wire — that is the #91 wrong-keys "
                             "class: the receiver reads cpu_count/vram_mb" % k)

    def test_evictions_gpu_lookup_still_finds_something(self):
        """link_manager._evict_weakest scores link.capabilities.get('gpu')."""
        with _with_profile(_Profile(_HW(cuda_available=True))):
            caps = PeerLink._get_local_capabilities()
        self.assertTrue(caps.get('gpu'),
                        "a GPU node no longer advertises a truthy 'gpu' — eviction "
                        "scoring goes blind to hardware again")


class TheTwoTiersAreNotConflated(unittest.TestCase):
    """key_delegation tier (central|regional|local) != system_requirements tier
    (what the box can run). They share a word and mean different things."""

    def test_tier_is_the_TRUST_tier_not_the_capability_tier(self):
        prof = _Profile(_HW())
        prof.tier = types.SimpleNamespace(value='full')     # capability tier
        with _with_profile(prof, node_tier='regional'):     # trust tier
            caps = PeerLink._get_local_capabilities()
        self.assertEqual('regional', caps['tier'],
                         "the handshake is advertising the CAPABILITY tier where "
                         "the trust/topology tier belongs")
        self.assertNotEqual('full', caps['tier'])


class ItStillWorksBeforeTheCacheIsPopulated(unittest.TestCase):
    """get_capabilities() returns None until run_system_check() has run (it is
    called from integrations/social/__init__.py at import). A link dialled before
    that must still advertise something true."""

    def test_a_None_profile_falls_back_to_direct_probes(self):
        with _with_profile(None):
            caps = PeerLink._get_local_capabilities()
        self.assertGreaterEqual(caps.get('cpu_count', 0), 1,
                                "the pre-check fallback stopped reporting cpu_count")
        self.assertIn('tier', caps)

    def test_a_missing_system_requirements_module_does_not_raise(self):
        """A trimmed install must not break the handshake."""
        keydel = types.ModuleType('security.key_delegation')
        keydel.get_node_tier = lambda: 'flat'
        with patch.dict(sys.modules, {'security.system_requirements': None,
                                      'security.key_delegation': keydel}):
            caps = PeerLink._get_local_capabilities()
        self.assertIn('cpu_count', caps)


if __name__ == '__main__':
    unittest.main()
