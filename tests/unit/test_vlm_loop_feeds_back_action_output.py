"""The VLM loop must show the model what its own action RETURNED.

RED BEFORE THE FIX.  Measured live 2026-09-09 03:47:19-39 (agent 33323830039,
single action `Get-Content ...\\tts_chatterbox_turbo.err -Head 50`):

    iter 1/3  Action: shell   -> ran, output captured into extracted_responses
    iter 2/3  Action: type    value='Get-Content C:\\...\\logs\\tt'
    iter 3/3  Action: type    value='Get-Content C:\\...\\logs\\tt'
    VLM loop finished: exit_reason=max_iterations, status=incomplete

The loop re-TYPED the command it had already executed, because the only
feedback the next iteration received was the task, a one-line "Previous
action: <action> - <reasoning>", and a SCREENSHOT.  A deterministic action
(shell / read_file_and_understand / list_folders_and_files) writes to stdout
and changes nothing on screen, so "check the screenshot" cannot be answered
for it.  status=incomplete then becomes TOOL_FAILURE_RESULTS, which the
reuse fabrication gate correctly reports as unrun and the StatusVerifier
correctly reports as 'error' -- so the user is told the tool failed for work
that had already succeeded.

This drives the REAL loop (no source scanning -- a contract guarded only by
string-matching the source is how the requires_breakdown gap survived, see
reuse_recipe._reuse_group_terminate) with a fake backend, and asserts that
iteration 2's prompt carries iteration 1's stdout.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

SENTINEL = "CHATTERBOX_TRACEBACK_SENTINEL_9f3a"


class _FakeBackend:
    """Minimal stand-in for Qwen3VLBackend, recording every prompt it sees."""

    def __init__(self):
        self.prompts = []

    def route_task(self, _instruction):
        # Same classification the live run took, which caps the budget at 3.
        return 'single_shot'

    def try_taskbar_pre_check(self, *_a, **_k):
        return None

    def detect_grounding_bias(self, *_a, **_k):
        return None

    def _call_api(self, messages):
        text = ''
        for part in (messages[0].get('content') or []):
            if isinstance(part, dict) and part.get('type') == 'text':
                text = part.get('text', '')
        self.prompts.append(text)
        if len(self.prompts) == 1:
            # Iteration 1: run the deterministic shell action.
            return ('{"Reasoning": "read the error log", '
                    '"Next Action": "shell", '
                    '"command": "Get-Content tts_chatterbox_turbo.err", '
                    '"Status": "IN_PROGRESS"}')
        # Iteration 2+: park on a no-op so the loop ends predictably.
        return ('{"Reasoning": "done", "Next Action": "None", '
                '"Status": "DONE"}')


@pytest.fixture
def loop_with_fake(monkeypatch):
    lct = pytest.importorskip('integrations.vlm.local_computer_tool')
    from integrations.vlm import local_loop, qwen3vl_backend

    backend = _FakeBackend()
    monkeypatch.setattr(qwen3vl_backend, 'get_qwen3vl_backend',
                        lambda: backend, raising=False)
    monkeypatch.setattr(lct, 'take_screenshot',
                        lambda _tier: 'ZmFrZQ==', raising=False)
    # The action really "runs" and returns stdout on the documented key.
    monkeypatch.setattr(
        lct, 'execute_action',
        lambda payload, tier, **kw: {'output': SENTINEL, 'status': 'ok'},
        raising=False)
    monkeypatch.setenv('HEVOLVE_VLM_UNIFIED', '1')
    return local_loop, backend


def test_second_iteration_sees_the_first_actions_output(loop_with_fake):
    """The defect, stated as the property that was missing."""
    local_loop, backend = loop_with_fake

    local_loop.run_local_agentic_loop(
        {'instruction_to_vlm_agent': 'read the chatterbox error log',
         'enhanced_instruction': 'read the chatterbox error log',
         'user_id': 'u-test', 'prompt_id': 'p-test',
         'max_ETA_in_seconds': 30},
        tier='inprocess',
    )

    assert len(backend.prompts) >= 2, (
        "loop did not reach a second iteration; cannot test feedback "
        f"(prompts={len(backend.prompts)})")
    assert SENTINEL in backend.prompts[1], (
        "iteration 2's prompt does NOT contain the output iteration 1's "
        "action returned — the model is being asked to judge a headless "
        "command from a screenshot, which is what made it re-type the "
        "command until max_iterations.\n"
        f"prompt was:\n{backend.prompts[1][:1200]}")


def test_first_iteration_has_no_stale_feedback(loop_with_fake):
    """Anti-vacuity: the sentinel must not be present before any action ran,
    or the assertion above would pass for the wrong reason."""
    local_loop, backend = loop_with_fake

    local_loop.run_local_agentic_loop(
        {'instruction_to_vlm_agent': 'read the chatterbox error log',
         'enhanced_instruction': 'read the chatterbox error log',
         'user_id': 'u-test2', 'prompt_id': 'p-test2',
         'max_ETA_in_seconds': 30},
        tier='inprocess',
    )

    assert backend.prompts, "loop never called the model"
    assert SENTINEL not in backend.prompts[0], (
        "iteration 1 already carried an action result — the feedback block "
        "is firing without an action having run")
