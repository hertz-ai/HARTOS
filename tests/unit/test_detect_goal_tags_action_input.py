"""Regression guard for detect_goal_tags accepting non-str inputs.

Root-cause incident: every marketing/outreach/sales goal dispatched
through the autogen path (create_recipe.py:1735) silently lost its
goal-specific tools because ``detect_goal_tags(task)`` was called
with ``task`` being a HARTOS ``helper.Action`` object (the
multi-step task tracker defined at ``helper.py:1363``) — not a
string.  ``prompt.lower()`` raised ``AttributeError: 'Action' object
has no attribute 'lower'``, the surrounding broad ``except
Exception`` swallowed it as DEBUG, and the agent booted without its
``register_marketing_tools`` / ``register_outreach_tools`` /
``register_sales_tools`` step.  Result: agents talked about
marketing without ever calling ``create_campaign``, ``send_email``,
or any other side-effect tool.  Goals "completed" with zero real
work for ~6 weeks before the bug surfaced.

This test fails CI if any future change weakens the input handling
back to a string-only API.
"""
import pytest

from integrations.agent_engine.marketing_tools import detect_goal_tags


# A stand-in for HARTOS's ``helper.Action`` (helper.py:1363) — it
# carries multi-step task state but no .lower() method.  Reproduces
# the historical bug input shape exactly.
class _FakeHARTOSAction:
    def __init__(self, content: str):
        self.actions = content
        self.current_action = 1
        self.fallback = False

    def __str__(self) -> str:
        return self.actions


def test_string_prompt_still_works():
    """Baseline — the historical happy path."""
    assert 'marketing' in detect_goal_tags(
        "Run a marketing campaign with email outreach")
    assert 'coding' in detect_goal_tags(
        "Refactor the github repository codebase")
    assert detect_goal_tags("just chatting") == []


def test_action_object_input_no_attribute_error():
    """The exact bug shape — helper.Action passed where str expected.
    Must NOT raise; must coerce via str() and detect tags."""
    action = _FakeHARTOSAction("plan a marketing campaign on social media")
    tags = detect_goal_tags(action)
    assert 'marketing' in tags, (
        "Coerced str(action) should pass keyword detection. "
        "If this fails the autogen path silently strips marketing tools "
        "from every dispatched goal — exactly the 6-week regression."
    )


def test_none_input_does_not_crash():
    """Defensive — None should produce empty tags, not crash."""
    assert detect_goal_tags(None) == []


def test_int_input_does_not_crash():
    """Defensive — non-str primitive should not raise."""
    assert detect_goal_tags(123) == []


def test_dict_input_does_not_crash():
    """Defensive — autogen sometimes passes dict-like message envelopes;
    str() on a dict returns its repr but does not raise."""
    msg = {'role': 'user', 'content': 'run marketing campaign'}
    # str(dict) gives "{'role': 'user', 'content': 'run marketing campaign'}"
    # which contains 'marketing' as substring → tag is detected
    tags = detect_goal_tags(msg)
    # Must not raise.  Whether it detects depends on dict repr — we
    # just assert no exception + result is a list.
    assert isinstance(tags, list)
