"""set_flags_to_enter_reuse_mode's inverted review_agents flag.

Found 2026-08-24 testing a brand-new Slack channel binding against
prompt_id=8888 (HARTOS's shared default agent): every single message,
including the very first one from a user who had never touched this agent
before, got permanently trapped in "resuming in-progress creation" --
chat()'s own log line for it.

Root cause: this function (named set_flags_to_enter_review_mode when this
bug was first found -- independently renamed to set_flags_to_enter_reuse_mode
by a same-day fix on origin/main, merged 2026-08-24 evening; see its
docstring for both incidents) has exactly one call site, the "all flows
complete -> REUSE" branch in chat(), and used to unconditionally set
review_agents[_ak] = True there -- backwards from its own
create_agent = False return value. chat()'s "resuming in-progress
creation" check reads that same review_agents key a few lines later in
the SAME request and stomped the correct False back to True, so the bug
poisoned its own request's outcome, not just later ones.

conversation_agent[_ak] is asserted True, not False -- main's same-day fix
(2026-08-23, live incident #684) found that False there sent every first
turn on a completed agent into Phase-2 recipe(), short-circuiting on the
raw "Agent Already Created Successfully" stub instead of executing the
message. Both fixes landed together; review_agents=False is the one this
test exists to guard.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

pytest.importorskip('autogen', reason='autogen not installed')


class TestSetFlagsToEnterReuseModeClearsReviewAgents:
    def test_returns_false_clears_review_sets_conversation(self):
        from hart_intelligence_entry import (
            app, set_flags_to_enter_reuse_mode, review_agents, conversation_agent,
        )
        user_id, prompt_id = 'test-user-review-mode-fix', 8888
        ak = f'{user_id}_{prompt_id}'
        # Simulate the poisoned state a prior (buggy) call would have left.
        review_agents[ak] = True
        conversation_agent[ak] = False
        try:
            with app.app_context():
                create_agent = set_flags_to_enter_reuse_mode(0, user_id, prompt_id)
            assert create_agent is False
            assert review_agents[ak] is False
            assert conversation_agent[ak] is True
        finally:
            review_agents.pop(ak, None)
            conversation_agent.pop(ak, None)
