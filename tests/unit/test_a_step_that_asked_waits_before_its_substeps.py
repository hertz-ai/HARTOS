"""A step that has asked the person a question waits for the answer, even
when the model has split that step into sub-steps of its own.

MEASURED on the owner's desktop, 2026-11-16, Nunba build 75 on the local 4B,
the Tamil speech agent (1794540972213), a test account new to it.  ONE turn
(msg_1794824159377_sim, 15:46:32-15:51:38 IST) sent send_message_to_user with
expects_reply=True eight times ("What is your child's name?" twice, then
"Thank you for sharing your child's name...", ...), with two [SUBTASK-HOLD]
steers between them ("Once the child's name is received, respond in
Tamil...") and no [REUSE-WAIT].  The person never got to answer; the next
turns saved the child's sound "Aaa!" as her name, and an earlier account got
a made-up name saved.

Cause: _advance_or_steer's sub-step hold steers the next unrun sub-step and
keeps the turn looping without asking whether this step's question is already
out.  The one helper that records an ask and takes the canonical wait
(_reuse_note_ask_and_wait, "at a hold or a turn exit") runs only at turn
exit, which a held step never reaches.  The fix asks that helper first; when
it holds, the turn ends and the sub-steps wait for the reply.

    python -m pytest tests/unit/test_a_step_that_asked_waits_before_its_substeps.py -q
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

UP = 'u-parent_1794540972213'


class _Sub:
    def __init__(self, description):
        self.description = description


class _Instructor:
    def __init__(self):
        self.posted = []

    def initiate_chat(self, recipient=None, message=None, **_kw):
        self.posted.append(message)


class _Manager:
    def __init__(self):
        self.groupchat = object()


@pytest.fixture
def rr(monkeypatch):
    from hartos import reuse_recipe as rr
    monkeypatch.setattr(rr, '_reuse_complete_pending_subtask', lambda *_a, **_k: False)
    monkeypatch.setattr(rr, 'get_pending_subtasks', lambda *_a, **_k: [
        _Sub("Once the child's name is received, respond in Tamil")])
    monkeypatch.setattr(rr, '_narrow_assistant_to_current_action', lambda *_a, **_k: None)

    def _no_advance(*_a, **_k):
        raise AssertionError('a step with unrun sub-steps must not advance')
    monkeypatch.setattr(rr, '_advance_reuse_action', _no_advance)
    with Flask('asked-waits-test').app_context():
        yield rr


def _held(rr, monkeypatch, question_out):
    seen = []

    def _note(user_prompt, action_id, group_chat, returned_text=None):
        seen.append((user_prompt, action_id, group_chat))
        return question_out
    monkeypatch.setattr(rr, '_reuse_note_ask_and_wait', _note)
    return seen


def test_a_step_whose_question_is_out_ends_the_turn_instead_of_steering(rr, monkeypatch):
    seen = _held(rr, monkeypatch, question_out=True)
    manager, instructor, latch = _Manager(), _Instructor(), {1}
    keep_looping = rr._advance_or_steer(UP, 1, 'reuse-w1', '1794540972213',
                                        manager, instructor, advanced_latch=latch)
    assert keep_looping is False
    assert instructor.posted == []
    assert seen == [(UP, 1, manager.groupchat)]
    assert 1 not in latch  # the next turn, with the reply, verifies the step again


def test_a_step_with_no_question_out_still_steers_its_next_substep(rr, monkeypatch):
    """The 2026-09-10 rule this hold exists for (a401c14a0) is kept."""
    _held(rr, monkeypatch, question_out=False)
    instructor = _Instructor()
    keep_looping = rr._advance_or_steer(UP, 1, 'reuse-w1', '1794540972213',
                                        _Manager(), instructor, advanced_latch=set())
    assert keep_looping is True
    assert len(instructor.posted) == 1
    assert "Once the child's name is received" in instructor.posted[0]


def test_a_step_with_no_unrun_substeps_never_asks_the_wait(rr, monkeypatch):
    """The wait is asked only on the hold; a step whose sub-steps are done
    advances exactly as before."""
    seen = _held(rr, monkeypatch, question_out=True)
    monkeypatch.setattr(rr, 'get_pending_subtasks', lambda *_a, **_k: [])
    moved = []
    monkeypatch.setattr(rr, '_advance_reuse_action',
                        lambda *_a, **_k: (moved.append(1) or (None, True)))
    monkeypatch.setattr(rr, '_reuse_fab_steer_message', lambda *_a, **_k: None, raising=False)
    try:
        rr._advance_or_steer(UP, 1, 'reuse-w1', '1794540972213',
                             _Manager(), _Instructor(), advanced_latch=set())
    except Exception:
        pass  # what follows the advance is not this test's business
    assert moved == [1]
    assert seen == []
