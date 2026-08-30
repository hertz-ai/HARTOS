"""Tests for core.peer_link.local_subscribers tracker CRUD.

DeliveryTracker (peer message delivery confirmation) and LongRunningTracker
(task progress) are in-memory accounting for the peer-link event bus. Their core
track/confirm/on_confirmation_message/on_progress logic was uncovered (the 4
files that import this module test peripheral aspects). A bug here silently
mis-counts deliveries or loses task status, so the protocol branches are pinned.

Fresh instances are constructed per test (not the singletons) so there is no
cross-test state; pure in-memory, no network.
"""
from __future__ import annotations

import json

from core.peer_link.local_subscribers import (
    DeliveryTracker, LongRunningTracker, _MAX_PENDING,
)


# ── DeliveryTracker.track / confirm ─────────────────────────────────────────
def test_track_by_request_id():
    t = DeliveryTracker()
    t.track('chat.social', {'request_id': 'r1', 'user_id': 'u1'})
    assert 'r1' in t._pending
    assert t._pending['r1']['user_id'] == 'u1'


def test_track_falls_back_to_msg_id():
    t = DeliveryTracker()
    t.track('chat.social', {'msg_id': 'm9'})
    assert 'm9' in t._pending


def test_track_without_key_is_noop():
    t = DeliveryTracker()
    t.track('chat.social', {'user_id': 'u1'})  # no request_id / msg_id
    assert len(t._pending) == 0


def test_confirm_removes_pending():
    t = DeliveryTracker()
    t.track('chat.social', {'request_id': 'r1'})
    t.confirm('r1')
    assert 'r1' not in t._pending


def test_confirm_unknown_key_is_safe():
    t = DeliveryTracker()
    t.confirm('nope')  # must not raise


def test_track_caps_at_max_pending():
    t = DeliveryTracker()
    for i in range(_MAX_PENDING + 25):
        t.track('chat.social', {'request_id': f'r{i}'})
    assert len(t._pending) <= _MAX_PENDING


# ── DeliveryTracker.on_confirmation_message: the cloud/local protocol ────────
def test_on_confirmation_false_tracks():
    t = DeliveryTracker()
    t.on_confirmation_message('conf', {'request_id': 'r1', 'confirmation': False,
                                       'topic_name': 'chat.social'})
    assert 'r1' in t._pending  # unconfirmed -> tracked


def test_on_confirmation_true_confirms():
    t = DeliveryTracker()
    t.track('chat.social', {'request_id': 'r1'})
    t.on_confirmation_message('conf', {'request_id': 'r1', 'confirmation': True})
    assert 'r1' not in t._pending


def test_on_confirmation_absent_key_confirms_cloud_format():
    t = DeliveryTracker()
    t.track('chat.social', {'request_id': 'r1'})
    # No 'confirmation' key at all -> cloud-format confirmation -> pop.
    t.on_confirmation_message('conf', {'request_id': 'r1'})
    assert 'r1' not in t._pending


def test_on_confirmation_accepts_json_string_payload():
    t = DeliveryTracker()
    t.track('chat.social', {'request_id': 'r1'})
    t.on_confirmation_message('conf', json.dumps({'request_id': 'r1',
                                                  'confirmation': True}))
    assert 'r1' not in t._pending


def test_on_confirmation_bad_string_is_noop():
    t = DeliveryTracker()
    t.on_confirmation_message('conf', '{not json')  # must not raise
    assert len(t._pending) == 0


def test_delivery_get_stats_is_a_dict():
    t = DeliveryTracker()
    t.track('chat.social', {'request_id': 'r1'})
    assert isinstance(t.get_stats(), dict)


# ── LongRunningTracker ──────────────────────────────────────────────────────
def test_on_progress_records_and_reads_back():
    lt = LongRunningTracker()
    lt.on_progress('lr.log', {'request_id': 'q1', 'task_name': 'build',
                              'status': 'RUNNING'})
    st = lt.get_task_status('q1')
    assert st['task_name'] == 'build' and st['status'] == 'RUNNING'
    assert lt.get_stats() == {'tracked_tasks': 1}


def test_on_progress_without_request_id_not_recorded():
    lt = LongRunningTracker()
    lt.on_progress('lr.log', {'task_name': 'x', 'status': 'RUNNING'})
    assert lt.get_stats()['tracked_tasks'] == 0


def test_on_progress_error_status_still_recorded():
    lt = LongRunningTracker()
    lt.on_progress('lr.log', {'request_id': 'q2', 'task_name': 't',
                              'status': 'ERROR'})
    assert lt.get_task_status('q2')['status'] == 'ERROR'


def test_on_progress_accepts_json_string():
    lt = LongRunningTracker()
    lt.on_progress('lr.log', json.dumps({'request_id': 'q3', 'status': 'DONE'}))
    assert lt.get_task_status('q3')['status'] == 'DONE'


def test_on_progress_bad_string_is_noop():
    lt = LongRunningTracker()
    lt.on_progress('lr.log', '{bad json')  # must not raise
    assert lt.get_stats()['tracked_tasks'] == 0


def test_get_task_status_unknown_returns_none():
    assert LongRunningTracker().get_task_status('missing') is None


def test_longrunning_prunes_over_200():
    lt = LongRunningTracker()
    for i in range(210):
        lt.on_progress('lr.log', {'request_id': f'q{i}', 'status': 'RUNNING'})
    assert lt.get_stats()['tracked_tasks'] <= 200
