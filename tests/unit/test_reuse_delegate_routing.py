"""should_delegate_route_to_helper — the `delegate` routing gap.

Found across 2026-08-11/08-18/08-19: the Assistant sets is_casual/delegate
on every response (see reuse_recipe._extract_conversational_reply), but
state_transition never read `delegate`, so a substantive request classified
'local' or 'hive' had nowhere to go and spun to the turn deadline before
apologising -- even though the model's real answer sat in group_chat.messages
the whole time. Confirmed live on Discord 2026-08-12: "i need a help in
coding" was classified delegate='local' and the user waited ~238s for a
generic failure.

2026-08-24: state_transition now routes delegate in ('local', 'hive') to
Helper (which has the registered tools) instead of leaving it unroutable.
'hive' has no peer/federation dispatch wired yet, so it is routed through
the same local Helper path for now -- a deliberate interim scoping choice.

should_delegate_route_to_helper is a pure predicate (no autogen agent
objects) extracted specifically so it can be unit-tested directly --
state_transition itself is a closure inside create_agents_for_user that
needs a live autogen/LLM setup to construct, same reasoning as
lifecycle_hooks.is_recipe_creation_request for create_recipe's
state_transition (see test_recipe_speaker_routing.py).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

pytest.importorskip('autogen', reason='autogen not installed')

from reuse_recipe import should_delegate_route_to_helper


class TestShouldDelegateRouteToHelper:
    def test_local_delegate_from_assistant_routes_to_helper(self):
        last_json = {"reply": "sure", "is_casual": True, "delegate": "local"}
        assert should_delegate_route_to_helper(last_json, "assistant") is True

    def test_hive_delegate_routes_to_helper_too_interim(self):
        # Deliberate interim scoping: hive has no peer dispatch yet.
        last_json = {"reply": "sure", "is_casual": True, "delegate": "hive"}
        assert should_delegate_route_to_helper(last_json, "assistant") is True

    def test_none_delegate_does_not_route(self):
        last_json = {"reply": "hi", "is_casual": True, "delegate": "none"}
        assert should_delegate_route_to_helper(last_json, "assistant") is False

    def test_missing_delegate_key_does_not_route(self):
        last_json = {"reply": "hi", "is_casual": True}
        assert should_delegate_route_to_helper(last_json, "assistant") is False

    def test_case_and_whitespace_insensitive(self):
        last_json = {"delegate": "  Local  "}
        assert should_delegate_route_to_helper(last_json, "assistant") is True

    def test_does_not_bounce_immediately_back_to_helper(self):
        # Helper just spoke -- don't re-select Helper before Assistant gets a
        # turn to synthesize, or the graph could stall bouncing on itself.
        # "Helper" (capitalized) matches the agent's real registered name
        # (reuse_recipe.py's AssistantAgent(name="Helper", ...)) -- a
        # lowercase "helper" here would silently pin the case-mismatch bug
        # that let this guard never fire (found live 2026-08-27: an
        # infinite Helper<->Helper bounce on every delegate='local' turn).
        last_json = {"delegate": "local"}
        assert should_delegate_route_to_helper(last_json, "Helper") is False

    def test_does_not_override_executor_or_chatinstructor_turns(self):
        last_json = {"delegate": "local"}
        assert should_delegate_route_to_helper(last_json, "Executor") is False
        assert should_delegate_route_to_helper(last_json, "ChatInstructor") is False

    def test_non_dict_json_is_safe(self):
        assert should_delegate_route_to_helper(None, "assistant") is False
        assert should_delegate_route_to_helper("not a dict", "assistant") is False

    def test_completed_status_is_handled_elsewhere_not_by_this_predicate(self):
        # state_transition checks status=='completed' BEFORE calling this
        # predicate and returns chat_instructor directly -- a message that
        # happens to carry both keys should still evaluate on delegate alone
        # here, since routing that case is state_transition's job, not this
        # predicate's.
        last_json = {"status": "completed", "delegate": "local"}
        assert should_delegate_route_to_helper(last_json, "assistant") is True
