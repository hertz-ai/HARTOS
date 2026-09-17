from unittest.mock import patch

from agent_ledger import SmartLedger, TaskStatus
from agent_ledger.backends import JSONBackend
from integrations.vlm import activity_stream


def test_activity_persists_before_existing_notification_fanout(tmp_path):
    """One task is reduced through phases; notification is only a projection."""
    ledger = SmartLedger(
        '42', 'guest_42_test',
        backend=JSONBackend(storage_dir=str(tmp_path)),
    )

    with patch.object(activity_stream, '_ledger_for', return_value=ledger), \
         patch.object(activity_stream, 'resolve_steering_agent_id', return_value='goal-42'), \
         patch('integrations.social.realtime.on_notification') as notify:
        started = activity_stream.record_activity(
            user_id='guest', prompt_id='42', run_id='run1', iteration=1,
            action='left_click', phase='executing', agent_id='42',
            audit_ref={'activity_id': 'run1:1'}, caption='Open Settings (left_click)',
        )
        assert started['task_id'] in ledger.tasks
        task = ledger.get_task(started['task_id'])
        assert task.context['kind'] == 'computer_use'
        assert task.context['audit_ref']['activity_id'] == 'run1:1'
        assert task.context['caption'] == 'Open Settings (left_click)'
        assert task.status == TaskStatus.IN_PROGRESS
        notify.assert_called_once_with('guest', started)

        completed = activity_stream.record_activity(
            user_id='guest', prompt_id='42', run_id='run1', iteration=1,
            action='left_click', phase='completed', agent_id='42',
            audit_ref={'activity_id': 'run1:1'}, caption='Open Settings (left_click)',
        )

    assert completed['task_id'] == started['task_id']
    assert completed['msg_id'] != started['msg_id']
    assert completed['caption'] == 'Open Settings (left_click)'
    assert ledger.get_task(started['task_id']).status == TaskStatus.COMPLETED
