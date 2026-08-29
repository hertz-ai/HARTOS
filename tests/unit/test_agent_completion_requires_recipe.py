"""#718 — "Agent Created Successfully" must mean the agent is actually reusable.

MEASURED 2026-08-29 on the live corpus (Documents\\Nunba\\data\\prompts):

    742 top-level agent configs
    612 with status='completed'
     81 of those have a _0_recipe.json   (13.2%)  -> reusable
    531 do NOT                            (86.8%) -> present as done, never reusable

Every one of the 531 hits hart_intelligence_entry.py:9876-9906 on every turn —
"has a config but no flow-0 recipe — routing this reuse turn into creation to
finish it autonomously" — and re-enters creation forever.

WHY: `safe_action_boundary_check` is a GUARD (contract: `(should_continue,
response_or_none)`), but two of its branches return the terminal string
'Agent Created Successfully' without the flow recipe ever being written:

    :5782  if current_flow >= total_flows:      # bare index comparison
    :5832  else:  # "All flows completed"

Neither calls `_save_flow_recipe`. The single caller at :4169 does
`if not should_continue: return early_response`, so that string becomes the
/chat reply verbatim — the guard is acting as the authority on completion.

POSITIVE IDENTIFICATION: the marker at :5831, "All flows completed - agent
creation ready", appears exactly twice (gui_app.log.1, server.log.1) — the same
two rotated files that contain "Agent Created Successfully" — while every other
producer's marker ('[ALL-FLOWS-DONE]', 'Completed from here', 'Completed from
here3', '[FLOW-RECIPE-SAVED]') is zero across all log files.

The reuse gate (hart_intelligence_entry.py:9250) asks exactly one question:
does `{prompt_id}_0_recipe.json` exist?  Completion must answer the same
question, or the two disagree forever.
"""
import os

import create_recipe


def test_helper_exists_and_is_the_gate_predicate():
    """Completion and the reuse gate must ask the SAME question.

    The gate is `os.path.exists(PROMPTS_DIR/{prompt_id}_0_recipe.json)`.
    """
    assert hasattr(create_recipe, '_agent_build_is_complete'), (
        'completion must be decidable from the same artifact the reuse gate reads')


def test_missing_recipe_is_not_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(create_recipe, 'PROMPTS_DIR', str(tmp_path))
    # A config exists (agent was "created") but no flow-0 recipe — the exact
    # shape of all 531 unreusable agents.
    (tmp_path / '9999000001.json').write_text('{"status": "completed"}')
    assert create_recipe._agent_build_is_complete('9999000001') is False, (
        'an agent with no _0_recipe.json can never be reused and must not '
        'be reported as created')


def test_present_recipe_is_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(create_recipe, 'PROMPTS_DIR', str(tmp_path))
    (tmp_path / '9999000002.json').write_text('{"status": "completed"}')
    (tmp_path / '9999000002_0_recipe.json').write_text('{"actions": []}')
    assert create_recipe._agent_build_is_complete('9999000002') is True


def test_matches_the_reuse_gate_exactly(tmp_path, monkeypatch):
    """Same predicate as hart_intelligence_entry.py:9250, by construction."""
    monkeypatch.setattr(create_recipe, 'PROMPTS_DIR', str(tmp_path))
    pid = '9999000003'
    gate = lambda: os.path.exists(  # noqa: E731 - mirrors hie:9250 verbatim
        os.path.join(str(tmp_path), f'{pid}_0_recipe.json'))
    assert create_recipe._agent_build_is_complete(pid) == gate()
    (tmp_path / f'{pid}_0_recipe.json').write_text('{}')
    assert create_recipe._agent_build_is_complete(pid) == gate()


def test_bad_prompt_id_does_not_raise(tmp_path, monkeypatch):
    """A guard must not become a new crash site."""
    monkeypatch.setattr(create_recipe, 'PROMPTS_DIR', str(tmp_path))
    for bad in (None, '', 0):
        assert create_recipe._agent_build_is_complete(bad) is False
