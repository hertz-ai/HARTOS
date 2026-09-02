"""A hive task is paid for work, not for text that reported no error.

DEFECT, measured on .69 2026-09-02.  ``validate_result``'s checks are:
1 result structure, 2 files-in-scope, 3 tests, 4 no error reported, 5 DLP
clean.  For a task with no ``files_scope`` and ``requires_tests`` false only
checks 1, 4 and 5 apply — and 4 and 5 are BOTH satisfied by a result that
contains nothing at all.  2/3 = 0.67, which clears ``on_task_result``'s 0.4
gate, so the task went to VALIDATED and Spark was distributed.

That is not theoretical.  The in-backend executor
(``ClaudeHiveSession._dispatch_to_pipeline``) asks the LLM for a diff and
PARSES it, but nothing on that path writes a file.  A task "create
copilot_proof.txt" was reported completed at quality 0.50 with no file on
disk.  ``_compute_quality_score`` starts at "0.5 base score for completion",
so the session paid itself for having a status.

The gate added in front of the scoring asks what the task was FOR:
  code-producing types  a diff or changed file, and APPLIED — a proposal is
                        not a completion
  everything else       (BENCHMARK, MODEL_ONBOARD) the answer is the
                        deliverable, so output or test output is evidence

Run:
  pytest tests/unit/test_hive_result_evidence_gate.py -v
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from integrations.coding_agent.hive_task_protocol import (  # noqa: E402
    HiveTask,
    HiveTaskType,
    validate_result,
)


def _task(task_type, **kw):
    """A task with the shape that exposed the defect: no files_scope and no
    required tests, so only the structure / no-error / DLP checks apply."""
    return HiveTask(
        task_id='t-0000000000',
        task_type=task_type,
        title='t',
        description='d',
        instructions='i',
        requires_tests=False,
        **kw
    )


# ── the fabrication ────────────────────────────────────────────────────

def test_an_empty_result_earns_nothing():
    """The exact shape that was paid: no changes, no output, no error."""
    score = validate_result(_task(HiveTaskType.CODE_WRITE.value), {})
    assert score == 0.0, \
        'a result carrying no evidence of work must not clear the 0.4 gate'


def test_a_parsed_but_unapplied_diff_is_not_a_completion():
    """The in-backend executor's actual output: a diff it never applied."""
    result = {
        'files_changed': ['copilot_proof.txt'],
        'diff': '--- a/copilot_proof.txt\n+++ b/copilot_proof.txt\n@@\n+hi',
        'changes_applied': False,
        'error': None,
    }
    assert validate_result(_task(HiveTaskType.CODE_WRITE.value), result) == 0.0


def test_no_error_and_clean_dlp_alone_do_not_pay():
    """Checks 4 and 5 passing is what made an empty result score 0.67."""
    result = {'error': None, 'test_output': None}
    assert validate_result(_task(HiveTaskType.BUG_FIX.value), result) == 0.0


# ── work that really happened still scores ─────────────────────────────

def test_an_applied_change_scores():
    result = {
        'files_changed': ['a.py'],
        'diff': '--- a/a.py\n+++ b/a.py\n@@\n+x = 1',
        'changes_applied': True,
        'error': None,
    }
    assert validate_result(_task(HiveTaskType.CODE_WRITE.value), result) > 0.4


def test_an_external_executor_without_the_flag_is_not_penalised():
    """The claude-code daemon really does write files and never had to carry
    `changes_applied`; an absent flag must not be read as 'not applied'."""
    result = {
        'files_changed': ['a.py'],
        'diff': '--- a/a.py\n+++ b/a.py\n@@\n+x = 1',
        'error': None,
    }
    assert validate_result(_task(HiveTaskType.CODE_WRITE.value), result) > 0.4


# ── a benchmark's deliverable is not a diff ────────────────────────────

def test_a_benchmark_answer_is_evidence():
    """'Solve 100 problems from ensemble_mmlu' produces no diff. Before the
    gate its answer was discarded by _dispatch_to_pipeline entirely, so the
    result carried nothing and validated on 'no error' alone."""
    result = {'output': 'Solved 100/100. Accuracy 0.83.', 'error': None}
    assert validate_result(_task(HiveTaskType.BENCHMARK.value), result) > 0.0


def test_a_benchmark_with_no_answer_earns_nothing():
    result = {'error': None}
    assert validate_result(_task(HiveTaskType.BENCHMARK.value), result) == 0.0


def test_an_errored_result_still_earns_nothing():
    """Unchanged behaviour, pinned so the new gate cannot resurrect it."""
    result = {'output': 'partial', 'error': 'pipeline exploded'}
    score = validate_result(_task(HiveTaskType.BENCHMARK.value), result)
    assert score < 0.4
