"""A daemon goal is distributed only onto a queue another node can claim from.

dispatch._get_distributed_coordinator promises "Returns None when Redis is
unavailable -- caller falls back to local".  api._get_coordinator builds an
in-memory coordinator whenever Redis is absent, and a bundled desktop
(NUNBA_BUNDLED) never tries Redis at all, so the promise was false on every
desktop.  That coordinator lives in this process and dispatch never announces
to peers, so no other node can see what is put in it.  Measured 2026-09-13 on
a desktop: the coding daemon logged 632 "Distributed dispatch" lines for 137
goals in 4 h, and the local worker loop claimed one task in the same window.

    python -m pytest tests/unit/test_dispatch_needs_shared_queue.py -q
"""
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip('agent_ledger', reason='agent_ledger not installed')

import integrations.distributed_agent.api as api  # noqa: E402
from integrations.agent_engine import dispatch  # noqa: E402

_UNSET = object()


@pytest.fixture
def bundled_coordinator(monkeypatch, tmp_path):
    """The coordinator a bundled desktop builds: no Redis attempt, in-memory."""
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    monkeypatch.setenv('HEVOLVE_DB_PATH', str(tmp_path / 'hevolve.db'))
    saved = getattr(api._get_coordinator, '_instance', _UNSET)
    saved_type = api._coordinator_backend_type
    if saved is not _UNSET:
        del api._get_coordinator._instance
    try:
        coordinator = api._get_coordinator()
        assert coordinator is not None
        assert api.get_coordinator_backend_type() == 'inmemory'
        yield coordinator
    finally:
        if hasattr(api._get_coordinator, '_instance'):
            del api._get_coordinator._instance
        if saved is not _UNSET:
            api._get_coordinator._instance = saved
        api._coordinator_backend_type = saved_type


def test_an_in_memory_coordinator_is_not_a_shared_queue(bundled_coordinator):
    assert dispatch._get_distributed_coordinator() is None


def test_a_bundled_desktop_does_not_submit_its_goal_there(bundled_coordinator):
    with patch.object(bundled_coordinator, 'submit_goal') as submit:
        assert dispatch.dispatch_goal_distributed(
            'fix the failing test', 'u1', 'goal-shared-queue', 'coding') is None
    submit.assert_not_called()


def test_a_redis_coordinator_is_still_the_shared_queue():
    coordinator = MagicMock()
    with patch.object(api, '_get_coordinator', return_value=coordinator), \
            patch.object(api, 'get_coordinator_backend_type',
                         return_value='redis'):
        assert dispatch._get_distributed_coordinator() is coordinator
