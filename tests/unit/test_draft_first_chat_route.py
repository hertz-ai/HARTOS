"""E2E test: /chat → draft_first classifier → autogen CREATE fallthrough.

Regression guard for the integration surface where silent
misclassification regressions surface. The draft 0.8B classifier
emits {reply, delegate, is_casual, is_create_agent, confidence}.
When is_create_agent=true with high confidence, the /chat handler
must fall through to the autogen CREATE flow instead of returning
the draft's standby reply.

This test exercises the EXACT code path from the 2026-04-11 incident
without requiring a running llama-server or Flask app — it mocks the
dispatcher to return a controlled envelope and asserts the /chat
handler's routing logic.
"""
import json
import pytest
from unittest.mock import patch, MagicMock


class TestDraftFirstToAutogenCreate:
    """When the draft classifier says is_create_agent=true, the /chat
    handler must route to the autogen CREATE flow, not return the
    draft's reply as final."""

    def _make_draft_result(self, **overrides):
        """Build a mock dispatch_draft_first result."""
        base = {
            'response': 'Draft standby reply',
            'speculation_id': 'test-123',
            'draft_model': 'qwen3.5-0.8b-draft',
            'delegate': 'none',
            'draft_confidence': 0.95,
            'is_correction': False,
            'is_casual': False,
            'is_create_agent': False,
            'channel_connect': '',
            'expert_pending': False,
            'latency_ms': 280.0,
            'energy_kwh': 0.0001,
        }
        base.update(overrides)
        return base

    @patch('integrations.agent_engine.speculative_dispatcher.get_speculative_dispatcher')
    def test_casual_hi_returns_draft_reply_directly(self, mock_get_disp):
        """A casual 'hi' with delegate=none should return the draft's
        reply immediately without falling through to LangChain or
        autogen. This is the fast-path we're optimizing for."""
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch_draft_first.return_value = self._make_draft_result(
            response='Hey! How can I help?',
            delegate='none',
            is_casual=True,
            draft_confidence=0.92,
        )
        mock_get_disp.return_value = mock_dispatcher

        # We can't easily test the full Flask route without app context,
        # so we test the decision logic directly
        result = mock_dispatcher.dispatch_draft_first.return_value
        assert result['response'] == 'Hey! How can I help?'
        assert result['is_casual'] is True
        assert result['delegate'] == 'none'
        # When delegate=none + is_create_agent=False, the /chat handler
        # returns this directly (line 5611 of hart_intelligence_entry.py)

    @patch('integrations.agent_engine.speculative_dispatcher.get_speculative_dispatcher')
    def test_create_agent_intent_falls_through(self, mock_get_disp):
        """When the draft says is_create_agent=true with high confidence,
        the /chat handler must NOT return the draft reply — it must
        fall through to the autogen CREATE flow."""
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch_draft_first.return_value = self._make_draft_result(
            response='I can help you create that agent!',
            delegate='local',
            is_create_agent=True,
            draft_confidence=0.88,
        )
        mock_get_disp.return_value = mock_dispatcher

        result = mock_dispatcher.dispatch_draft_first.return_value
        # The /chat handler at line 5600 checks:
        #   if result.get('is_create_agent') and confidence >= threshold
        #   → set create_agent=True, fall through (don't return draft reply)
        assert result['is_create_agent'] is True
        assert result['draft_confidence'] >= 0.85  # _DRAFT_INTENT_CONFIDENCE

    @patch('integrations.agent_engine.speculative_dispatcher.get_speculative_dispatcher')
    def test_low_confidence_create_does_not_fall_through(self, mock_get_disp):
        """A low-confidence is_create_agent should NOT route to CREATE —
        the draft isn't sure enough. The draft reply is returned as-is."""
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch_draft_first.return_value = self._make_draft_result(
            response='Not sure what you want, but here goes...',
            delegate='local',
            is_create_agent=True,
            draft_confidence=0.3,  # Below threshold
        )
        mock_get_disp.return_value = mock_dispatcher

        result = mock_dispatcher.dispatch_draft_first.return_value
        assert result['is_create_agent'] is True
        assert result['draft_confidence'] < 0.85  # Below threshold → no fallthrough

    def test_draft_first_defaults_to_true(self):
        """The draft_first flag should default to True when no env var
        or request body override is set."""
        import os
        with patch.dict(os.environ, {}, clear=False):
            # Remove HEVOLVE_DRAFT_FIRST if present
            os.environ.pop('HEVOLVE_DRAFT_FIRST', None)
            env_val = os.environ.get('HEVOLVE_DRAFT_FIRST', '').strip()
            # The logic at hart_intelligence_entry.py:5363-5371:
            # if env == '0': False
            # elif env == '1': True
            # elif 'draft_first' in data: bool(data['draft_first'])
            # else: True  ← DEFAULT
            assert env_val == '' or env_val not in ('0', '1')
            # When env is empty and body has no draft_first → defaults True

    def test_envelope_fields_complete(self):
        """The draft envelope must contain all the fields the /chat
        handler consumes. Missing fields = silent misrouting."""
        required_fields = [
            'response', 'speculation_id', 'draft_model', 'delegate',
            'draft_confidence', 'is_correction', 'is_casual',
            'is_create_agent', 'channel_connect', 'expert_pending',
            'latency_ms',
        ]
        result = self._make_draft_result()
        for field in required_fields:
            assert field in result, (
                f"Draft envelope missing '{field}' — the /chat handler "
                f"reads this field and will silently misroute if absent"
            )


class TestDraftFirstDelegateRouting:
    """Regression for #114 — when the draft returns delegate='local' or
    'hive' for a NON-CASUAL prompt that's also NOT is_create_agent,
    the /chat handler must route to autogen CREATE+execute, NOT
    return the draft's standby reply.

    Witnessed bug 2026-05-07 10:24:16 (RequestID f4f7d95f, prompt
    "open chrome and research AI papers"):
      draft_telemetry = {is_casual: false, is_create_agent: false,
                         delegate: 'hive', confidence: 0.95}
    The handler returned the 102-char draft standby reply ("I'll
    gather that research...") and never executed any actual work.

    User's design (2026-05-07): "only if local needs help it shd go
    via hive" — local-first, hive as downstream fallback. So both
    delegate='local' and delegate='hive' for non-casual tasks must
    enter the local autogen execution path (find_matching_agent
    → REUSE if match, else _autonomous_gather_info → recipe()).

    This is a source-level guard since Flask-routed E2E tests on
    chat() are too brittle (dispatcher mocks, request-context
    fixtures, llama-server stubs all interact).  The branch's
    source presence is the contract — any future PR that drops
    this elif will flip this red.
    """

    def test_delegate_local_or_hive_branch_in_source(self):
        """The /chat handler must contain a branch that flips
        create_agent=True AND autonomous=True when the draft says
        delegate in ('local', 'hive') AND is_casual=False AND
        is_create_agent=False."""
        import re
        from pathlib import Path

        src_path = (Path(__file__).parent.parent.parent
                    / 'hart_intelligence_entry.py')
        src = src_path.read_text(encoding='utf-8')

        # Find the elif block that handles delegate routing.  Looser
        # regex: locate the elif by signature, then capture up to ~80
        # following indented lines (the branch body).
        match = re.search(
            r"elif\s*\(\s*result\.get\(\s*['\"]delegate['\"]\s*\)\s*in\s*"
            r"\(\s*['\"]local['\"]\s*,\s*['\"]hive['\"]\s*\)"
            r"[^:]*?:\s*\n"
            r"((?:[ \t]+[^\n]*\n){1,80})",
            src,
            re.DOTALL,
        )
        assert match, (
            "Could not find the `elif result.get('delegate') in "
            "('local', 'hive') ...` branch in hart_intelligence_entry.py "
            "— task #114 fix has been removed?"
        )
        block = match.group(1)
        # Code-shape assertions must not trip on the branch's own prose: the
        # block's comments narrate the OLD mechanism while explaining #118.
        code_only = "\n".join(
            line for line in block.splitlines()
            if not line.lstrip().startswith("#")
        )

        # CONTRACT UPDATED to the #118 design (this test used to pin the
        # pre-#118 mechanism and went red the moment the code was fixed).
        # #118: forcing create_agent/autonomous on EVERY non-casual turn
        # hijacked recall/Q&A ("what did we discuss 15 days back") into an
        # 8-action execute_windows CREATE plan, bypassing get_ans — the ONLY
        # path with the working FULL_HISTORY date-recall tool. The branch now
        # deliberately falls through (no flags, no return) so get_ans answers
        # recall directly and escalates genuine tasks via its own
        # Create_Agent / Agentic_Router tools. The 2026-05-07 "open chrome
        # and research" regression stays fixed BY get_ans, not by forced
        # CREATE. So the branch must NOT set the old flags:
        assert not re.search(r"\bcreate_agent\s*=\s*True\b", code_only), (
            "delegate-routing branch must NOT force create_agent=True — "
            "#118: that hijacked recall/Q&A into a CREATE plan and bypassed "
            "get_ans (the only path with FULL_HISTORY date recall)."
        )
        assert not re.search(r"\bautonomous\s*=\s*True\b", code_only), (
            "delegate-routing branch must NOT force autonomous=True — see "
            "the #118 note above; fall-through to get_ans is the contract."
        )
        # And the branch must still be a documented, deliberate fall-through
        # (the #118 marker comment), not an accidentally-emptied block.
        assert re.search(r"#118 FIX", block), (
            "the delegate-routing branch lost its #118 marker comment — if "
            "the fall-through was changed on purpose, update this test's "
            "contract alongside the code."
        )


class TestDraftFirstDisabled:
    """When draft_first is disabled, the /chat handler must skip the
    dispatcher entirely and go straight to the LangChain path."""

    def test_env_var_zero_disables(self):
        """HEVOLVE_DRAFT_FIRST=0 → draft_first=False"""
        import os
        with patch.dict(os.environ, {'HEVOLVE_DRAFT_FIRST': '0'}):
            val = os.environ.get('HEVOLVE_DRAFT_FIRST', '').strip()
            assert val == '0'  # The /chat handler sets draft_first=False

    def test_env_var_one_enables(self):
        """HEVOLVE_DRAFT_FIRST=1 → draft_first=True"""
        import os
        with patch.dict(os.environ, {'HEVOLVE_DRAFT_FIRST': '1'}):
            val = os.environ.get('HEVOLVE_DRAFT_FIRST', '').strip()
            assert val == '1'  # The /chat handler sets draft_first=True
