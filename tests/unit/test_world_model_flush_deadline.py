"""Sparse verified experience and failed replay must use the same queue."""
import threading
from unittest.mock import patch, Mock

import pytest
from integrations.agent_engine.world_model_bridge import WorldModelBridge


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setenv('HEVOLVEAI_API_URL', '')
    monkeypatch.setenv('HEVOLVE_WM_FLUSH_BATCH', '50')
    monkeypatch.setenv('HEVOLVE_WM_FLUSH_MAX_WAIT', '3600')
    with patch.object(WorldModelBridge, '_init_in_process'), \
         patch.object(WorldModelBridge, '_start_crawl_integrity_watcher'):
        b = WorldModelBridge()
    yield b
    b._shutdown_flush()


def test_sparse_sample_reaches_provider_without_another_interaction(bridge):
    bridge._in_process = True
    bridge._provider = Mock()
    bridge._queue_training_experience({'prompt': 'receipt', 'response': 'verified'})
    timer = bridge._flush_timer
    assert timer is not None
    timer.cancel()
    bridge._flush_due(timer)
    bridge._flush_executor.shutdown(wait=True)
    assert not bridge._experience_queue
    bridge._provider.create_chat_completion.assert_called_once()
    assert bridge._stats['total_flushed'] == 1
    assert bridge.check_health()['learning_active'] is True


def test_cancelled_timer_cannot_steal_new_timer_queue(bridge):
    bridge._queue_training_experience({'prompt': 'first'})
    old = bridge._flush_timer
    old.cancel()
    with bridge._lock:
        bridge._flush_timer = None
        bridge._schedule_flush_locked()
    current = bridge._flush_timer
    with patch.object(bridge._flush_executor, 'submit') as submit:
        bridge._flush_due(old)
        submit.assert_not_called()
    assert bridge._flush_timer is current
    assert len(bridge._experience_queue) == 1


def test_full_batch_cancels_partial_deadline_without_duplicate_delivery(bridge):
    bridge._flush_batch_size = 2
    with patch.object(bridge._flush_executor, 'submit') as submit:
        bridge._queue_training_experience({'prompt': 'a'})
        timer = bridge._flush_timer
        bridge._queue_training_experience({'prompt': 'b'})
        bridge._flush_due(timer)
        assert submit.call_count == 1
        assert len(submit.call_args.args[1]) == 2
    assert bridge._flush_timer is None


def test_disabled_transport_requeues_and_has_a_retry_deadline(bridge):
    sample = {'prompt': 'a'}
    bridge._flush_to_world_model([sample])
    assert list(bridge._experience_queue) == [sample]
    assert bridge._flush_timer is not None
    assert bridge._stats['total_flushed'] == 0


def test_shutdown_cancels_pending_callback(bridge):
    bridge._queue_training_experience({'prompt': 'a'})
    timer = bridge._flush_timer
    bridge._shutdown_flush()
    with patch.object(bridge._flush_executor, 'submit') as submit:
        bridge._flush_due(timer)
        submit.assert_not_called()
    assert bridge._flush_timer is None


def test_inprocess_failure_retries_only_unacknowledged_samples(bridge):
    bridge._in_process = True
    bridge._provider = Mock()
    bridge._provider.create_chat_completion.side_effect = [None, OSError('provider offline')]
    batch = [{'prompt': name, 'response': 'verified'} for name in ('first', 'second', 'third')]
    bridge._flush_to_world_model(batch)
    assert bridge._stats['total_flushed'] == 1
    assert list(bridge._experience_queue) == batch[1:]
    assert bridge._flush_timer is not None
    timer = bridge._flush_timer
    timer.cancel()
    bridge._provider.create_chat_completion.side_effect = None
    bridge._flush_due(timer)
    bridge._flush_executor.shutdown(wait=True)
    assert bridge._stats['total_flushed'] == 3
    assert not bridge._experience_queue
    assert bridge._provider.create_chat_completion.call_count == 4
