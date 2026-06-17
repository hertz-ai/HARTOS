"""#146 (C1): a hierarchically-synced post lands via the canonical federation
inbox — the durable central/regional "origin" backup for the CDN model.

The vertical (child→parent) content sync reuses the SAME FederatedPost
persistence + dedup that the horizontal (peer→follower) federation inbox uses
(federation.receive_inbox) — content has ONE durable store, not a parallel
table. receive_sync_batch's 'sync_post' op was previously a no-op log stub.

Behavioral: real SyncEngine.receive_sync_batch, mock the federation singleton
boundary, assert the synced payload is forwarded to receive_inbox + the batch
stays resilient if the receiver raises.
"""
import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.social.sync_engine import SyncEngine  # noqa: E402


def _fed_module(receive_return='fed-1'):
    fed_singleton = MagicMock()
    fed_singleton.receive_inbox = MagicMock(return_value=receive_return)
    fed_mod = MagicMock()
    fed_mod.federation = fed_singleton
    return fed_mod, fed_singleton


def test_sync_post_lands_via_federation_inbox():
    payload = {'type': 'new_post', 'origin_node_id': 'nodeA',
               'post': {'id': 'p1', 'title': 'hi', 'content': 'world'}}
    items = [{'operation_type': 'sync_post', 'payload': payload, 'id': 'i1'}]
    fed_mod, fed = _fed_module('fed-123')
    with patch.dict(sys.modules, {'integrations.social.federation': fed_mod}):
        result = SyncEngine.receive_sync_batch(None, items)
    fed.receive_inbox.assert_called_once_with(None, payload)
    assert 'i1' in result['processed']


def test_sync_post_receiver_failure_does_not_break_batch():
    # receive_inbox raising must NOT crash the batch — _handle_sync_post is
    # best-effort (swallows) so one bad post never wedges the whole drain.
    payload = {'type': 'new_post', 'origin_node_id': 'nodeA', 'post': {'id': 'p2'}}
    items = [{'operation_type': 'sync_post', 'payload': payload, 'id': 'i2'}]
    fed_mod, fed = _fed_module()
    fed.receive_inbox.side_effect = RuntimeError('db down')
    with patch.dict(sys.modules, {'integrations.social.federation': fed_mod}):
        result = SyncEngine.receive_sync_batch(None, items)
    assert 'i2' in result['processed']
    assert result['errors'] == []
