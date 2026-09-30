"""The main leg's Assistant must carry the tool SCHEMA, not execution alone.

Measured live 2026-09-06 on agent 89555447799 (llm_outbound.jsonl + .old,
1,449 bodies).  ``reuse_recipe.py`` registered the 18 MAIN_LEG_CORE_TOOLS with
``register_core_tools(..., helper, assistant)``, which is
``helper.register_for_llm`` + ``assistant.register_for_execution`` — so the
Assistant held EXECUTION ONLY and never received a tool schema.

When autogen's speaker selection made the Assistant the speaker, its outbound
body carried no ``tools[]`` at all — and the recipes assign the work to exactly
that agent (``'agent_to_perform_this_action': 'Assistant'``).  Consequences
measured in the same window:

  * autogen.reuse carried a tools[] block in 11 of 1,182 bodies.  587 of the
    tools-less ones are the StatusVerifier (correctly tool-less); ~591 are
    EXECUTION-persona bodies that should have had tools and did not.
  * 26x "The requested tool 'google_search' is not available"
  * 2,657+ "Error: Function <X> not found" — send_message_to_user x1052 (the
    path that returns the agent's result to the user), get_user_camera_inp
    x676, and request_tools x101 (the never-say-unavailable escape hatch,
    itself unreachable).
  * One body at 08:43:17 carried SEVEN identical `role=assistant
    name=Assistant` messages sharing ONE tool_call id, none answered, after
    which StatusVerifier emitted {"status":"completed"}.  Action recorded done,
    tool never ran.

This is the same defect already fixed narrowly for the Tier-2 news and revenue
families (news_tools.py:421-442, revenue_tools.py:224-225), whose comment reads
"Deliberately dual here ... do not 'simplify' it back".  Per review follow-up
#755 item 2 the pattern belongs in ONE place; this pins it for the core set.

    python -m pytest tests/unit/test_main_leg_assistant_carries_schema.py -q
"""
import os
import re

from core.agent_tools import MAIN_LEG_CORE_TOOLS, register_core_tools


class _RecordingAgent:
    """Minimal stand-in for an autogen agent's two registration surfaces."""

    def __init__(self, name):
        self.name = name
        self.llm = []          # names registered for_llm  (SCHEMA)
        self.exec_ = []        # names registered for_execution

    def register_for_llm(self, name=None, description=None):
        def _wrap(func):
            self.llm.append(name)
            return func
        return _wrap

    def register_for_execution(self, name=None):
        def _wrap(func):
            self.exec_.append(name)
            return func
        return _wrap


def _tools():
    return [('google_search', 'search the web', lambda text: 'ok'),
            ('send_message_to_user', 'deliver the result', lambda msg: 'ok')]


class TestRegistrationContract:

    def test_default_is_unchanged(self):
        """:2112 and :2209 must keep helper=schema / executor=execution."""
        helper, executor = _RecordingAgent('h'), _RecordingAgent('e')
        register_core_tools(_tools(), helper, executor)
        assert helper.llm == ['google_search', 'send_message_to_user']
        assert executor.exec_ == ['google_search', 'send_message_to_user']
        assert executor.llm == [], (
            'the time/visual legs must NOT gain a schema on their executor')

    def test_executor_proposes_gives_the_assistant_the_schema(self):
        """The fix: the agent the recipe names to act can SEE the tools."""
        helper, assistant = _RecordingAgent('h'), _RecordingAgent('a')
        register_core_tools(_tools(), helper, assistant,
                            executor_proposes=True)
        assert assistant.llm == ['google_search', 'send_message_to_user'], (
            'the Assistant must carry the tool SCHEMA — without it autogen '
            'sends its body with no tools[] and the model is told the tool '
            'does not exist')
        # still executes, and the Helper still proposes
        assert assistant.exec_ == ['google_search', 'send_message_to_user']
        assert helper.llm == ['google_search', 'send_message_to_user']

    def test_second_executor_unstrands_the_assistants_own_calls(self):
        """Repeat-speaker rule: the proposer cannot run its own call.

        Same reason news_tools.py takes an ``executor=`` — an Assistant that
        both proposes AND is the only executor strands its own tool_call with
        no role=tool answer.
        """
        helper, assistant = _RecordingAgent('h'), _RecordingAgent('a')
        second = _RecordingAgent('executor')
        register_core_tools(_tools(), helper, assistant,
                            executor_proposes=True, second_executor=second)
        assert second.exec_ == ['google_search', 'send_message_to_user']
        assert second.llm == [], 'the code executor is not a proposer'


class TestReuseCallSiteUsesIt:
    """The contract is worthless if the main leg does not opt in."""

    def _main_leg_call(self):
        src_path = os.path.join(os.path.dirname(__file__), '..', '..',
                                'hartos', 'reuse_recipe.py')
        with open(src_path, encoding='utf-8') as fh:
            src = fh.read()
        m = re.search(
            r'register_core_tools\(\s*main_leg_core_tools\(core_tools\)[^)]*\)',
            src)
        assert m, 'the main-leg register_core_tools call was not found'
        return m.group(0)

    def test_main_leg_opts_the_assistant_in(self):
        call = self._main_leg_call()
        assert 'executor_proposes=True' in call, (
            'reuse_recipe.py must register the main leg with '
            'executor_proposes=True, or the Assistant goes back to carrying '
            'zero tools and every action it is assigned fails with '
            '"Function <X> not found"')

    def test_main_leg_passes_a_distinct_executor(self):
        call = self._main_leg_call()
        assert 'second_executor=' in call, (
            "the Assistant's own structured tool_calls strand without a "
            'distinct executor (autogen will not let it speak twice in a row)')

    def test_the_two_worst_offenders_are_in_the_core_set(self):
        """Guards the population this fix is meant to unblock."""
        assert 'google_search' in MAIN_LEG_CORE_TOOLS
        assert 'send_message_to_user' in MAIN_LEG_CORE_TOOLS
