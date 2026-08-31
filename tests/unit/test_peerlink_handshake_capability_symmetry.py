"""The PeerLink handshake must exchange capabilities in BOTH directions.

THE DEFECT
──────────
`_complete_handshake` (the ACCEPTING side) has always done

    self.capabilities = hello_data.get('capabilities', {})

but `_perform_handshake` (the DIALING side) never put `capabilities` in its `hello`.
Only the `hello_ack` carried them. So the exchange was one-way: the dialer learned
the acceptor's cpu/gpu/tier, and **the acceptor recorded `{}` for every peer**.

That is not cosmetic. `link_manager._evict_weakest` scores links with
`link.capabilities.get('gpu')` (+10 for a GPU peer), so on the accept side every
peer looked GPU-less and eviction was effectively blind to hardware.

WHY IT MATTERS *NOW*, with no listener yet
──────────────────────────────────────────
Blast radius is nil today — nothing serves `ws://…/peer_link`, so no link is ever
accepted. That is exactly why this is the moment to fix it. Bootstrap hook #5 adds
the inbound listener; the day it lands, this ships as silent data loss on every
inbound link, and it would be found the way the last one was — in production, weeks
later.

THE PRECEDENT, in this same file
────────────────────────────────
`user_id_proof` was read by `_complete_handshake` and written by nobody. Every peer
requesting SAME_USER was demoted to PEER, and because `message_bus._route_peerlink`
scopes non-relay topics to SAME_USER, "multi-device sync (and the skill broadcast
riding it) had no recipients on any node." Fixed 2026-08-13 (ce8be281, 6098a66f).
Same class, same file, second instance.

These tests read the real source with the AST — they do not import `core.peer_link`
(which pulls the security stack) and they do not open a socket.
"""
import ast
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

LINK = os.path.join(REPO, 'core', 'peer_link', 'link.py')


def _fn(name):
    tree = ast.parse(open(LINK, encoding='utf-8', errors='replace').read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    raise AssertionError('%s not found in link.py' % name)


def _dict_keys_assigned_to(src, varname):
    """Keys of the dict literal assigned to `varname` in this function source."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == varname:
                    return {k.value for k in node.value.keys
                            if isinstance(k, ast.Constant)}
    return set()


class BothDirectionsAdvertiseCapabilities(unittest.TestCase):

    def test_the_DIALER_sends_capabilities_in_hello(self):
        keys = _dict_keys_assigned_to(_fn('_perform_handshake'), 'hello')
        self.assertTrue(keys, "could not find the `hello` dict literal")
        self.assertIn(
            'capabilities', keys,
            "_perform_handshake's hello has no `capabilities` key, but "
            "_complete_handshake reads hello_data['capabilities'] — so every "
            "ACCEPTED peer records {}. This is the user_id_proof bug class again: a "
            "field read by the receiver that no sender writes.")

    def test_the_ACCEPTOR_still_sends_capabilities_in_the_ack(self):
        """The half that always worked must keep working."""
        keys = _dict_keys_assigned_to(_fn('_complete_handshake'), 'ack')
        self.assertIn('capabilities', keys,
                      "the hello_ack lost its capabilities — the dialer now learns "
                      "nothing about the peer it dialed")

    def test_both_sides_use_the_SAME_capability_source(self):
        """One producer, so the two directions cannot describe the machine
        differently."""
        for fn in ('_perform_handshake', '_complete_handshake'):
            self.assertIn(
                '_get_local_capabilities()', _fn(fn),
                "%s does not build its capabilities from _get_local_capabilities — "
                "a second producer would let the two handshake directions disagree "
                "about the same node" % fn)


class TheReceiverStillReadsWhatIsNowSent(unittest.TestCase):
    """Pins the other half of the contract: if someone 'cleans up' the read, the
    write becomes dead weight and the next reader deletes it."""

    def test_complete_handshake_reads_capabilities_from_hello(self):
        self.assertIn("hello_data.get('capabilities'", _fn('_complete_handshake'),
                      "the accepting side no longer reads capabilities out of the "
                      "hello — the field just added to the dialer is now dead")

    def test_perform_handshake_reads_capabilities_from_the_ack(self):
        self.assertIn("resp.get('capabilities'", _fn('_perform_handshake'),
                      "the dialing side no longer reads capabilities out of the ack")


class TheConsumerThatWasBlind(unittest.TestCase):
    """Why the asymmetry had teeth: eviction scoring reads link.capabilities."""

    def test_eviction_still_scores_on_gpu_capability(self):
        mgr = os.path.join(REPO, 'core', 'peer_link', 'link_manager.py')
        src = open(mgr, encoding='utf-8', errors='replace').read()
        self.assertIn(
            "link.capabilities.get('gpu')", src,
            "link_manager no longer scores eviction on GPU capability — if that "
            "moved, re-check whether the handshake symmetry still has a consumer")


if __name__ == '__main__':
    unittest.main()
