"""F5 privacy complement to d1203f1f.

Commit d1203f1f wired the INBOUND PeerLink 'events' channel ->
receive_from_peer (MessageBus.bootstrap_peerlink_ingress, boot-wired in
local_subscribers.py, covered by test_fleet_command_relay.py::
TestPeerLinkIngressWiring).  That fix made the OUTBOUND broadcast's scope
matter: _route_peerlink fans EVERY bus message out to peers, so once inbound
delivery is live, a regular per-user topic broadcast to a non-SAME_USER (fleet)
peer would be delivered there.

This locks in the trust scoping that closes that gap: per-user topics reach the
user's OWN devices only (SAME_USER), while the signed fleet.command relay
(RELAY_TOPIC) still reaches the whole fleet — multi-hop into NAT'd sub-trees is
its entire purpose.

    python -m pytest tests/unit/test_peerlink_events_relay.py --noconftest -q
"""
import core.peer_link.message_bus as mb


class _Mgr:
    def __init__(self):
        self.calls = []

    def broadcast(self, channel, envelope, trust_filter=None, exclude_peer=''):
        self.calls.append((channel, trust_filter))
        return 0


def test_route_peerlink_regular_topic_is_same_user_scoped(monkeypatch):
    from core.peer_link.link import TrustLevel
    mb.reset_message_bus()
    mgr = _Mgr()
    monkeypatch.setattr('core.peer_link.link_manager.get_link_manager', lambda: mgr)
    mb.get_message_bus()._route_peerlink('chat.response', {'x': 1}, 'm1')
    assert mgr.calls == [('events', TrustLevel.SAME_USER)]   # user's own devices only


def test_route_peerlink_relay_topic_reaches_whole_fleet(monkeypatch):
    mb.reset_message_bus()
    mgr = _Mgr()
    monkeypatch.setattr('core.peer_link.link_manager.get_link_manager', lambda: mgr)
    mb.get_message_bus()._route_peerlink(mb.RELAY_TOPIC, {'cmd': 1}, 'm2')
    assert mgr.calls == [('events', None)]                    # fleet mesh — no trust filter
