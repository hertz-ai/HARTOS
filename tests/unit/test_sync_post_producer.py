"""#147 (C2): public local posts rise to central via the tier sync — the
producer half of the content CDN both-axes.

federation.sync_to_parent is the VERTICAL counterpart to push_to_followers
(horizontal): the SAME canonical privacy gate (is_public) + the SAME
_outbox_message 'new_post' shape, queued to central via SyncEngine. Only
public/consented content rises; friends/community/private stays local.

Behavioral: real FederationManager.sync_to_parent, mock the gossip + SyncEngine
boundary, assert what gets queued (and what does NOT).
"""
import os
import sys
from unittest.mock import patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.social.federation import federation  # noqa: E402


def test_public_post_queued_to_central_as_new_post():
    # The unified producer matches entities from the SYNC_ENTITIES registry, and
    # the post matcher requires content_type + author_id (that IS what makes a
    # dict a post now). The old three-key fixture matched nothing, so
    # queue_entity returned None before any gate or serialize ran.
    post_dict = {'id': 'p1', 'privacy': 'public', 'title': 'hi',
                 'content_type': 'text', 'author_id': 'u1'}
    with patch('integrations.social.peer_discovery.gossip') as g, \
            patch('integrations.social.sync_engine.SyncEngine.queue') as q:
        g.node_id = 'nodeA'
        g.base_url = 'http://a'
        g.node_name = 'A'
        q.return_value = 'qid-1'
        out = federation.sync_to_parent('DB', post_dict)
    assert out == 'qid-1'
    q.assert_called_once()
    db_arg, tier, op, msg = q.call_args.args
    assert db_arg == 'DB'
    assert tier == 'central'
    assert op == 'sync_post'
    assert msg['type'] == 'new_post'
    assert msg['post'] is post_dict          # the same dict — no re-serialize
    assert msg['origin_node_id'] == 'nodeA'  # built via the shared _outbox_message


def test_non_public_posts_do_not_rise():
    for level in ('private', 'friends'):
        with patch('integrations.social.sync_engine.SyncEngine.queue') as q:
            out = federation.sync_to_parent('DB', {'id': 'x', 'privacy': level})
        assert out is None
        q.assert_not_called()


def test_queue_failure_is_best_effort():
    with patch('integrations.social.peer_discovery.gossip') as g, \
            patch('integrations.social.sync_engine.SyncEngine.queue',
                  side_effect=RuntimeError('queue down')):
        g.node_id = 'n'
        g.base_url = 'u'
        g.node_name = 'N'
        out = federation.sync_to_parent('DB', {'id': 'p', 'privacy': 'public'})
    assert out is None  # swallowed — never blocks the post create
