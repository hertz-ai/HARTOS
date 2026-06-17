"""#154 (found in the week review): the federation outbox is the EGRESS side of
the content-sync privacy guarantee.

`/api/social/federation/outbox` serves recent local posts for peers to pull
(the C4 CDN-fallback path pulls exactly this).  It previously filtered only on
`is_deleted`, so friends/community/private posts leaked to ANY peer — defeating
the write-side gate (federation.sync_to_parent / push_to_followers, #47) that
only lets PUBLIC posts leave the node.

Behavioural: mount the real discovery blueprint, feed a mixed-privacy post set
through a fake DB, and assert the route returns ONLY public posts via the real
`privacy.is_public` gate (NULL/''/'public' == public; friends/community/private
excluded).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import patch, MagicMock  # noqa: E402


class _FakePost:
    def __init__(self, pid, privacy):
        self.id = pid
        self.privacy = privacy
        self.is_deleted = False
        self.created_at = pid

    def to_dict(self):
        return {'id': self.id, 'privacy': self.privacy}


class _Chain:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return _Chain(self._rows)

    def close(self):
        pass


def _client(rows):
    from flask import Flask
    import integrations.social.models as models
    import integrations.social.peer_discovery as pd
    from integrations.social.discovery import discovery_bp

    app = Flask(__name__)
    app.register_blueprint(discovery_bp)

    fake_gossip = MagicMock(node_id='n1', base_url='http://n1', node_name='N1')
    cm_db = patch.object(models, 'get_db', return_value=_FakeDB(rows))
    cm_gossip = patch.object(pd, 'gossip', fake_gossip)
    return app, cm_db, cm_gossip


def test_outbox_serves_only_public_posts():
    rows = [
        _FakePost('pub', 'public'),
        _FakePost('friends', 'friends'),
        _FakePost('private', 'private'),
        _FakePost('null', None),        # NULL privacy == public (matches write-side)
        _FakePost('empty', ''),         # '' == public
        _FakePost('community', 'community'),
    ]
    app, cm_db, cm_gossip = _client(rows)
    with cm_db, cm_gossip:
        resp = app.test_client().get('/api/social/federation/outbox?limit=20')
    assert resp.status_code == 200
    served = {p['id'] for p in resp.get_json()['posts']}
    assert served == {'pub', 'null', 'empty'}              # public-only
    assert not ({'friends', 'private', 'community'} & served)  # no leak


def test_outbox_respects_limit_after_privacy_filter():
    rows = [_FakePost('p%d' % i, 'public') for i in range(50)]
    app, cm_db, cm_gossip = _client(rows)
    with cm_db, cm_gossip:
        resp = app.test_client().get('/api/social/federation/outbox?limit=5')
    assert len(resp.get_json()['posts']) == 5
