"""MeshPeer staleness must be clock-jump-proof (#6 false-healthy / #24 RTC jump).

A dead mesh peer must NOT read as alive after a backward wall-clock jump (which
would make the mesh route work to a gone peer), and the exported `last_seen` must
stay wall-clock for cross-node/display consumers — only the internal liveness math
is monotonic.
"""
import time
from unittest.mock import patch

from integrations.agent_engine.compute_mesh_service import MeshPeer


def _peer():
    return MeshPeer('peer-1', '10.0.0.2:6780', 'pubkey0123456789abcdef')


class TestMeshPeerStaleness:
    def test_fresh_peer_not_stale(self):
        p = _peer()
        p.last_seen_mono = time.monotonic()
        assert p.is_stale(max_age=300) is False

    def test_old_peer_is_stale(self):
        p = _peer()
        p.last_seen_mono = time.monotonic() - 400
        assert p.is_stale(max_age=300) is True

    def test_export_last_seen_stays_wall_clock(self):
        # to_dict export stays wall-clock (epoch) so cross-node/display consumers
        # are unaffected; only the internal liveness/age math is monotonic.
        d = _peer().to_dict()
        assert d['last_seen'] > 1_000_000_000  # epoch seconds, not a monotonic value
        assert isinstance(d['age_seconds'], int)

    @patch('integrations.agent_engine.compute_mesh_service.time.time')
    def test_backward_wall_jump_does_not_revive_dead_peer(self, mock_time):
        # Genuinely dead in monotonic terms (last seen 400s ago, 300s max_age).
        p = _peer()
        p.last_seen_mono = time.monotonic() - 400
        # Simulate the RTC backward jump: wall clock leaps to a tiny value that
        # would flip the OLD wall-based check (now - last_seen < max_age) to
        # not-stale — a dead peer reading alive. The monotonic check is immune.
        mock_time.return_value = 1000.0
        assert p.is_stale(max_age=300) is True
