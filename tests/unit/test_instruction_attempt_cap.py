"""An instruction that can never succeed must stop being retried.

DEFECT, measured on the live desktop 2026-09-02: the copilot instruction
queue drained the same 8 benchmark shards every ~50s and every one failed
HTTP 400 — 88 failures in a single day against the same 8 ids.
``fail_instruction`` put a failure straight back to ``QUEUED`` with nothing
counting, so it would have retried for as long as the node ran.

The cap is deliberately NOT a blanket "5 failures and you are out".  A
deferral (a human is using the LLM, or it is saturated) is not the
instruction's fault, and counting those would dead-letter healthy work
purely because someone was typing — worse than the bug being fixed.

Run:
  pytest tests/unit/test_instruction_attempt_cap.py -v
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from integrations.agent_engine.instruction_queue import (  # noqa: E402
    Instruction,
    InstructionStatus,
    InstructionQueue,
    MAX_INSTRUCTION_ATTEMPTS,
)


def _queue(tmp_path):
    q = InstructionQueue.__new__(InstructionQueue)
    import threading
    q._instructions = {}
    q._lock = threading.RLock()
    q._task_map = {}
    q._queue_path = str(tmp_path / 'q.json')
    q._drain_lock_path = str(tmp_path / 'q.lock')
    q._save = lambda: None          # persistence is not under test here
    q._get_ledger = lambda: None
    return q


def _add(q, text='Solve 100 problems from ensemble_mmlu (shard 1/1)'):
    inst = Instruction(user_id='copilot@MSI', text=text)
    q._instructions[inst.id] = inst
    return inst


def test_a_real_failure_returns_the_instruction_to_the_queue(tmp_path):
    q = _queue(tmp_path)
    inst = _add(q)
    q.fail_instruction(inst.id, 'HTTP 400')
    assert inst.status == InstructionStatus.QUEUED
    assert inst.attempts == 1


def test_it_is_dead_lettered_once_the_attempts_are_spent(tmp_path):
    """The 8 shards would otherwise have retried forever."""
    q = _queue(tmp_path)
    inst = _add(q)
    for _ in range(MAX_INSTRUCTION_ATTEMPTS):
        q.fail_instruction(inst.id, 'HTTP 400')
    assert inst.status == InstructionStatus.FAILED
    assert inst.attempts == MAX_INSTRUCTION_ATTEMPTS


def test_a_dead_lettered_instruction_stops_being_pulled(tmp_path):
    q = _queue(tmp_path)
    inst = _add(q)
    for _ in range(MAX_INSTRUCTION_ATTEMPTS):
        q.fail_instruction(inst.id, 'HTTP 400')
    assert inst not in q.get_pending(), \
        'a dead-lettered instruction must not be re-dispatched'


def test_deferrals_never_burn_an_attempt(tmp_path):
    """A human using the LLM must not consume the instruction's allowance."""
    q = _queue(tmp_path)
    inst = _add(q)
    for _ in range(MAX_INSTRUCTION_ATTEMPTS * 3):
        q.fail_instruction(inst.id, 'deferred: user active or LLM busy',
                           transient=True)
    assert inst.attempts == 0
    assert inst.status == InstructionStatus.QUEUED
    assert inst in q.get_pending()


def test_deferrals_mixed_with_real_failures_only_count_the_real_ones(tmp_path):
    q = _queue(tmp_path)
    inst = _add(q)
    q.fail_instruction(inst.id, 'deferred: user active or LLM busy',
                       transient=True)
    q.fail_instruction(inst.id, 'HTTP 400')
    q.fail_instruction(inst.id, 'deferred: user active or LLM busy',
                       transient=True)
    assert inst.attempts == 1
    assert inst.status == InstructionStatus.QUEUED


def test_a_queue_written_before_the_cap_existed_starts_with_a_full_allowance(tmp_path):
    """from_dict on an old file has no 'attempts' key; it must not read as
    already-exhausted and dead-letter on the first failure after an upgrade."""
    old = Instruction(user_id='u', text='t').to_dict()
    old.pop('attempts')
    revived = Instruction.from_dict(old)
    assert revived.attempts == 0


def test_attempts_survive_a_round_trip(tmp_path):
    inst = Instruction(user_id='u', text='t')
    inst.attempts = 3
    assert Instruction.from_dict(inst.to_dict()).attempts == 3
