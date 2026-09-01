"""Capacity reaches PeerNode.compute_* — from a node's OWN signed announce only.

THE DEFECT
──────────
`_self_info` has always shipped `hardware_summary` (cpu_cores / ram_gb /
gpu_vram_gb / disk_free_gb) INSIDE the signed announce, and its siblings from the
same block — `capability_tier`, `enabled_features` — were persisted. The hardware
sub-dict alone was never mapped, so `PeerNode.compute_*` stayed NULL fleet-wide:
0 of 107 active peers reporting, measured 2026-08-22.

The consequence is wrong data, not absent data. `ComputeDemocracy
.compute_effective_weight` defaults a missing value to 1 GPU / 8 GB, so
`raw = 1 * (8/8) = 1` and EVERY node scores exactly 1.0 — compute democracy has
been a uniform no-op, and `adjusted_reward` scaled every payout identically.

THE SECURITY SHAPE, which these tests pin
─────────────────────────────────────────
Gossip carries two kinds of record. A DIRECT announce is the node speaking for
itself and carries a live signature. A RELAYED record is a third party
republishing a row — `_merge_peer_list` documents that of 72 records from a live
central exchange, exactly ONE carried a signature. Capacity may therefore be set
ONLY by the direct announce: letting hearsay write `compute_*` would let any node
inflate — or zero out — a peer's standing in ComputeDemocracy just by gossiping
about it.

Self-reporting is accepted deliberately, following the precedent the code-hash
gate in the same function already set ("recorded, not fatal"; rejecting
"partitioned the entire network: 69 registered nodes, none federating"). The lie
is bounded by design: weight is log2-scaled and hard-capped, so a 100x claim earns
~3x and then hits the ceiling.
"""
import ast
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

SRC = os.path.join(REPO, 'integrations', 'social', 'peer_discovery.py')


def _source():
    return open(SRC, encoding='utf-8', errors='replace').read()


# ── Why this test does NOT import integrations.social.peer_discovery ──────────
# It cannot, in any practical test budget: importing that package runs
# `integrations/social/__init__.py:481 run_system_check()` at import time, which
# drags in the heavy dependency chain (and a network probe). Measured 2026-08-31:
# the bare `import integrations.social.peer_discovery` did not complete in EIGHT
# MINUTES on this box.
#
# That is not an aside — it is the root cause of the drift this change fixes. A
# module nobody can import in a test is a module nobody unit-tests, which is why
# `_merge_peer` has no behavioural coverage and why `hardware_summary` could ship
# in the signed announce for months without ever being mapped. It is also why the
# neighbouring suites resort to `assert callable(...)`.
#
# `_capacity_from_announce` is a PURE staticmethod over a dict — no I/O, no
# imports, no self. So we lift exactly that function out of the source with the
# AST and exec it standalone. The function under test is the real one, byte for
# byte, and the test runs in milliseconds.
def _load_pure_fn(name):
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            node.decorator_list = []          # drop @staticmethod
            mod = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(mod)
            ns = {}
            exec(compile(mod, SRC, 'exec'), ns)
            return ns[name]
    raise AssertionError('%s not found in %s' % (name, SRC))


class _PD:
    """Stand-in exposing the real, source-extracted function."""
    _capacity_from_announce = staticmethod(_load_pure_fn('_capacity_from_announce'))


def _pd():
    return _PD


ANNOUNCE = {
    'node_id': 'peer-abc',
    'url': 'http://10.0.0.9:6777',
    'hardware_summary': {
        'cpu_cores': 16,
        'ram_gb': 64.0,
        'gpu_vram_gb': 24.0,
        'disk_free_gb': 900.0,
    },
}


class TheMappingIsExactAndHonest(unittest.TestCase):

    def test_cpu_and_ram_map_straight_through(self):
        out = _pd()._capacity_from_announce(ANNOUNCE)
        self.assertEqual(16, out['compute_cpu_cores'])
        self.assertEqual(64.0, out['compute_ram_gb'])

    def test_vram_becomes_gpu_PRESENCE_never_a_fabricated_count(self):
        """hardware_summary carries VRAM; PeerNode has a device COUNT and no VRAM
        column. Mapping one onto the other is the '#91 wrong-keys' error. Presence
        under-reports a multi-GPU node, which is honest; a count invented from a
        capacity number is not."""
        out = _pd()._capacity_from_announce(ANNOUNCE)
        self.assertEqual(1, out['compute_gpu_count'],
                         "24 GB VRAM should register as GPU PRESENT (1), not as 24")
        self.assertNotIn(24.0, out.values(), "VRAM leaked into a count field")

    def test_a_gpu_less_node_reports_zero_not_missing(self):
        a = dict(ANNOUNCE, hardware_summary=dict(ANNOUNCE['hardware_summary'],
                                                 gpu_vram_gb=0))
        self.assertEqual(0, _pd()._capacity_from_announce(a)['compute_gpu_count'])

    def test_an_announce_with_no_hardware_yields_NOTHING(self):
        """Empty, not zeros: callers apply this unconditionally, and returning
        zeros would clobber a previously-known capacity with a wrong value."""
        self.assertEqual({}, _pd()._capacity_from_announce({'node_id': 'x'}))
        self.assertEqual({}, _pd()._capacity_from_announce({}))
        self.assertEqual({}, _pd()._capacity_from_announce(None))

    def test_a_malformed_hardware_block_is_ignored_not_crashed(self):
        for bad in ('not-a-dict', [], 0):
            self.assertEqual({}, _pd()._capacity_from_announce(
                {'hardware_summary': bad}), "malformed block %r" % (bad,))

    def test_partial_hardware_sets_only_what_was_sent(self):
        out = _pd()._capacity_from_announce(
            {'hardware_summary': {'cpu_cores': 4}})
        self.assertEqual({'compute_cpu_cores': 4}, out,
                         "a field the peer did not send must not be invented")


class ItActuallyMovesTheDemocracyWeight(unittest.TestCase):
    """The reason the whole change exists: with compute_* NULL, every node scores
    exactly 1.0 and the 5% cap is measured against a uniform fiction."""

    def _weight(self, peer_dict):
        from security.hive_guardrails import ComputeDemocracy
        return ComputeDemocracy.compute_effective_weight(peer_dict)

    def test_todays_null_state_gives_every_node_the_same_weight(self):
        self.assertEqual(self._weight({}), self._weight({'compute_ram_gb': None}))

    def test_real_capacity_now_differentiates(self):
        big = self._weight(_pd()._capacity_from_announce(ANNOUNCE))
        nul = self._weight({})
        self.assertGreater(big, nul,
                           "a 16-core/64GB/GPU node still weighs the same as an "
                           "unknown one — the mapping is not reaching the guardrail")

    def test_an_inflated_claim_is_BOUNDED_not_trusted(self):
        """Self-reporting is safe because the guardrail is log-scaled and capped.
        A liar claiming 1000 GPUs must not get 1000x influence."""
        from security.hive_guardrails import VALUES
        liar = self._weight({'compute_gpu_count': 1000, 'compute_ram_gb': 4096})
        self.assertLessEqual(liar, VALUES.MAX_INFLUENCE_WEIGHT,
                             "an inflated claim exceeded the influence cap")
        honest = self._weight(_pd()._capacity_from_announce(ANNOUNCE))
        self.assertLess(liar / max(honest, 0.001), 4.0,
                        "a 1000x hardware claim bought more than ~4x the weight of "
                        "an honest node — the log scaling is not doing its job")


class HearsayMayNotSetCapacity(unittest.TestCase):
    """A relayed record is a third party republishing a row. If it could write
    compute_*, any node could inflate or zero a peer's standing by gossiping."""

    def _merge_peer_src(self):
        """The real _merge_peer body, lifted by AST (see the note at the top of
        this file for why we cannot import the module)."""
        tree = ast.parse(_source())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_merge_peer':
                return ast.unparse(node)
        self.fail('_merge_peer not found')

    def test_the_create_branch_gates_capacity_on_not_relayed(self):
        src = self._merge_peer_src()
        self.assertIn('if relayed else self._capacity_from_announce', src,
                      "the new-peer branch no longer gates capacity on `relayed` — "
                      "a first sighting via someone else's peer list could seed "
                      "ComputeDemocracy with hearsay")

    def test_the_update_branch_sets_capacity_inside_the_not_relayed_block(self):
        src = self._merge_peer_src()
        i_gate = src.find('if not relayed:')
        i_cap = src.find('_capacity_from_announce(peer_data).items()')
        self.assertNotEqual(-1, i_gate, 'the `if not relayed` gate is gone')
        self.assertNotEqual(-1, i_cap, 'the update branch no longer writes capacity')
        self.assertLess(i_gate, i_cap,
                        "capacity is written OUTSIDE the `if not relayed` gate in "
                        "the update branch — relayed hearsay can now overwrite a "
                        "peer's proven capacity")


if __name__ == '__main__':
    unittest.main()
