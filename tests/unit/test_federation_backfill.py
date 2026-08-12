"""Backfill on follow.

A follow only subscribes to FUTURE posts: record_follow registers us and the
peer pushes from then on. Nothing historical arrives. Because
_auto_federate_peer follows every peer it accepts on discovery, a freshly
installed node follows hundreds of instances and still shows an empty feed
until somebody happens to post. That empty feed is the first thing a new user
sees, so follow_instance now pulls the peer's recent public posts.
"""
import types

import pytest

from integrations.social.federation import FederationManager


def _post(pid, title='t'):
    return {'id': pid, 'title': title, 'content': 'c',
            'content_type': 'text', 'author': {'username': 'nunba'}}


def _fake_response(payload):
    r = types.SimpleNamespace()
    r.json = lambda: payload
    return r


def _manager(monkeypatch, payload=None, boom=False):
    fm = FederationManager()
    import integrations.social.federation as fed

    def _get(url, **kwargs):
        if boom:
            raise RuntimeError('peer unreachable')
        return _fake_response(payload)

    monkeypatch.setattr(fed, 'pooled_get', _get)
    return fm


def test_backfill_stores_the_peers_recent_posts(monkeypatch):
    fm = _manager(monkeypatch, {'data': [_post('a'), _post('b')]})
    seen = []
    fm.receive_inbox = lambda db, p: (seen.append(p) or 'id')
    assert fm.backfill_from_peer(None, 'peer1', 'https://peer.example') == 2
    assert [p['post']['id'] for p in seen] == ['a', 'b']


def test_backfill_envelope_matches_what_receive_inbox_expects(monkeypatch):
    """receive_inbox dedups on origin_node_id + post id and ignores any
    type that is not new_post, so the envelope has to carry all three."""
    fm = _manager(monkeypatch, {'data': [_post('a')]})
    seen = []
    fm.receive_inbox = lambda db, p: (seen.append(p) or 'id')
    fm.backfill_from_peer(None, 'peer1', 'https://peer.example')
    env = seen[0]
    assert env['type'] == 'new_post'
    assert env['origin_node_id'] == 'peer1'
    assert env['origin_url'] == 'https://peer.example'
    assert env['post']['id'] == 'a'


def test_backfill_counts_only_newly_stored_posts(monkeypatch):
    """receive_inbox returns None for a duplicate; those must not be counted."""
    fm = _manager(monkeypatch, {'data': [_post('a'), _post('b'), _post('c')]})
    fm.receive_inbox = lambda db, p: 'id' if p['post']['id'] != 'b' else None
    assert fm.backfill_from_peer(None, 'peer1', 'https://peer.example') == 2


def test_backfill_survives_an_unreachable_peer(monkeypatch):
    """Best-effort by contract: a peer that is down must never break the
    follow that triggered the backfill."""
    fm = _manager(monkeypatch, boom=True)
    fm.receive_inbox = lambda db, p: 'id'
    assert fm.backfill_from_peer(None, 'peer1', 'https://down.example') == 0


def test_backfill_skips_malformed_posts_without_abandoning_the_page(monkeypatch):
    fm = _manager(monkeypatch, {'data': [{'no_id': True}, _post('b'), 'nonsense']})
    seen = []
    fm.receive_inbox = lambda db, p: (seen.append(p) or 'id')
    assert fm.backfill_from_peer(None, 'peer1', 'https://peer.example') == 1
    assert seen[0]['post']['id'] == 'b'


def test_backfill_tolerates_an_empty_or_odd_payload(monkeypatch):
    for payload in ({'data': []}, {}, {'data': None}):
        fm = _manager(monkeypatch, payload)
        fm.receive_inbox = lambda db, p: 'id'
        assert fm.backfill_from_peer(None, 'peer1', 'https://peer.example') == 0


def test_one_bad_post_does_not_stop_the_rest(monkeypatch):
    """A single post that blows up inside receive_inbox must not cost us the
    remainder of the page."""
    fm = _manager(monkeypatch, {'data': [_post('a'), _post('b'), _post('c')]})

    def _inbox(db, p):
        if p['post']['id'] == 'a':
            raise ValueError('bad row')
        return 'id'

    fm.receive_inbox = _inbox
    assert fm.backfill_from_peer(None, 'peer1', 'https://peer.example') == 2
