"""A completed hive task is reported to the HUMAN who submitted the goal.

``_notify_goal_contribution`` receives the claiming worker's node id as
``agent_id``.  Before d793bd6ee it notified that node id as if it were a
user.  The goal's requester is stamped into the task context by
``dispatch_goal_distributed`` (``context['user_id']``) and inherited by every
child through ``submit_goal``; that field, and only that field, names the
notification target.
"""
from unittest.mock import MagicMock, patch

from tests.unit.test_coordinator_dedup import _coordinator, _ledger


def _goal_with_child(context):
    led = _ledger()
    coord = _coordinator(led)
    coord.submit_goal('objective', [{'task_id': 'g1_task_0', 'description': 'd'}],
                      context, goal_id='g1')
    return coord


def _notify(coord):
    created = MagicMock()
    db = MagicMock()
    with patch('integrations.social.services.NotificationService.create', created), \
         patch('integrations.social.models.get_db', return_value=db):
        coord._notify_goal_contribution('g1_task_0', agent_id='node-abc',
                                        task_description='d')
    return created


def test_the_goal_owner_is_notified_not_the_worker_node():
    created = _notify(_goal_with_child({'user_id': 'human-7'}))
    created.assert_called_once()
    assert created.call_args.args[1] == 'human-7'
    assert created.call_args.args[2] == 'goal_contribution'


def test_a_goal_owned_by_a_machine_author_creates_no_notification():
    from core.constants import MACHINE_GOAL_AUTHORS
    author = sorted(MACHINE_GOAL_AUTHORS)[0]
    created = _notify(_goal_with_child({'user_id': author}))
    created.assert_not_called()


def test_a_goal_with_no_requester_creates_no_notification():
    created = _notify(_goal_with_child({}))
    created.assert_not_called()
