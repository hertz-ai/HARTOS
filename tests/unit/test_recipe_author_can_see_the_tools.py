"""The agent that AUTHORS recipes must be shown the tool names it may use.

THE DEFECT, root-caused and live-confirmed 2026-09-11 (D70).

create_recipe pins every recipe-creation request to the StatusVerifier:

    create_recipe.py:2727
        if is_recipe_creation_request(messages[-1].get("content")):
            current_app.logger.info("[RECIPE-ROUTE] recipe-creation request "
                                    "-> StatusVerifier (deterministic pin...)")
            return verify

That pin is deliberate and correct (it fixed a real 2026-06-07 defect where the
LLM speaker-selector handed recipe requests to the Assistant, which echoed the
prompt).  LIVE EVIDENCE it is the active path: 15 `[RECIPE-ROUTE]` lines across
the gui_app.log rotation set, including 01:28:26-01:30:50 inside the two CREATE
walks measured this session.

But the StatusVerifier is the ONE agent in the group with zero tool visibility:

  * every registration in create_agents targets the pair ``helper, assistant``
    -- register_core_tools, register_memory_graph_tools, register_channel_tools,
    register_media_tools, and ~20 register_dual(...) calls.  ``verify`` is
    passed to none of them, so its llm_config carries no tools[] schema.
  * its system_message names no tool and says "Do not perform actions
    yourself -- only report status".
  * the authoring request itself carries no tool list: the prompt is
    RECIPE_CREATE_PROMPT_PREFIX ("Focus on the current task at hand and create
    a detailed recipe") plus a JSON template whose tool_name placeholder reads
    verbatim:

        "If this step uses a tool, put the EXACT name of one of the tools
         provided to you in this request. Do not invent a name. If no provided
         tool fits, leave this empty string."

So the author is ordered to copy an exact name out of a list it was never
given.  Only two responses are available to it, and BOTH were observed live:

  walk 1 (agent 89088690384, "Disk Watch Dan") -- INVENT:
      action 2 tool_name = 'MemoryStore'      (a class, never a tool)
      action 3 tool_name = 'MemoryRetriever'  (does not exist anywhere)
    Cost: an unregistered name is never demanded by the fabrication gate
    (which filters its demands to REGISTERED names), so the action advanced
    having executed nothing and the agent told the user
      "The available free space on your primary system drive is 145.6 GB"
    against a real 9.7 GB -- wrong by ~15x, with an invented timestamp,
    returned as success=True.

  walk 2 (agent 89090102140) -- LEAVE EMPTY:
      every tool_name = '' and the steps authored `df -h`, a Linux command,
      on Windows.  Nothing could bind, so nothing could run.

POPULATION.  core.agent_tools.main_leg_tool_menu's own docstring records the
scale of exactly this failure across the banked corpus: of 1,034 steps in 127
saved flow recipes only 241 (23.3%) name a tool the runtime serves, and 51.4%
of the identifier-shaped names are unserved -- dominated by near-misses of real
capabilities (retrieve_memory / memory_query / MemoryService for
get_chat_history, web_search for google_search).  That is the signature of an
author guessing at names rather than copying them.

THE FIX THIS GUARD PINS: show the StatusVerifier the same canonical menu the
Assistant and Executor legs already get, derived from
``core.agent_tools.main_leg_tool_menu`` -- ONE source, so the advertised set can
never drift from the set the leg registers.  That helper is a pure name-join
(no closures, no Flask context), so it is safe in this constructor.

WHAT THIS GUARD DOES NOT CLAIM: that the model then picks the BEST tool, or
that recipes become correct.  It claims the author can no longer be ordered to
copy a name from a list it was not shown.
"""

import io
import os
import re
import unittest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Overridable so the guard can be pointed at a revision lacking the fix and
# proven to FAIL there (memory/feedback_vacuous_guards.md).
_SRC = os.environ.get('HARTOS_CREATE_SRC') or os.path.join(
    _HARTOS, 'hartos', 'create_recipe.py')

_VERIFIER = 'instantiate_status_verifier_agent'
_MENU = 'main_leg_tool_menu'

# The two capabilities the live walks needed and could not name.
_NEEDED_BY_THE_LIVE_WALKS = ('save_data_in_memory',
                             'execute_windows_or_android_command')


def _src():
    return io.open(_SRC, encoding='utf-8', errors='replace').read()


def _func(name, src):
    """One top-level function's source, bounded by the next TOP-LEVEL stmt."""
    m = re.search(r'^def %s\(.*\n(?:(?:[ \t].*)?\n)*' % re.escape(name),
                  src, re.M)
    return m.group(0) if m else ''


class TestTheAuthorIsShownTheTools(unittest.TestCase):
    """RED until the recipe author can see a tool name."""

    def setUp(self):
        self.src = _src()
        self.body = _func(_VERIFIER, self.src)
        if not self.body:
            self.fail('%s not found in %s -- re-point this guard'
                      % (_VERIFIER, _SRC))

    def test_the_verifier_is_given_the_canonical_tool_menu(self):
        self.assertIn(
            _MENU, self.body,
            "The StatusVerifier authors EVERY recipe (deterministic pin at "
            "the is_recipe_creation_request branch, 15 live firings) but is "
            "shown no tool names, while the authoring prompt orders it to "
            "'put the EXACT name of one of the tools provided to you in this "
            "request. Do not invent a name.' Live result: 'MemoryStore' and "
            "'MemoryRetriever' invented (-> a 145.6 GB answer against a real "
            "9.7 GB), then every tool_name left empty. Give it the same "
            "%s(...) menu the Assistant and Executor legs already carry."
            % _MENU)

    def test_the_menu_is_derived_not_hand_listed(self):
        """A hand-typed list is the drift this helper exists to prevent.

        main_leg_tool_menu's docstring records the measured cost of a prose
        menu drifting from the registered set: three advertised names the leg
        could not call, and get_chat_history never advertised at all.
        """
        if _MENU not in self.body:
            self.skipTest('covered by test_the_verifier_is_given_the_'
                          'canonical_tool_menu')
        # A literal run of comma-separated registered-looking names in the
        # system message would mean someone pasted the list instead.
        pasted = re.search(
            r"'(?:save_data_in_memory|google_search|send_message_to_user)'"
            r"\s*,\s*'", self.body)
        self.assertIsNone(
            pasted,
            'the verifier system message hand-lists tool names; derive them '
            'from %s so the advertised set cannot drift from the registered '
            'set' % _MENU)

    def test_the_menu_names_what_the_two_failed_walks_needed(self):
        """The menu must actually contain the capabilities that were missed.

        Behavioural, against the REAL helper -- a guard that only checks the
        call site would pass even if the menu were empty.
        """
        try:
            from core.agent_tools import main_leg_tool_menu
        except Exception as err:                       # pragma: no cover
            self.skipTest('core.agent_tools not importable here: %r' % err)
        menu = main_leg_tool_menu(('execute_windows_or_android_command',))
        for name in _NEEDED_BY_THE_LIVE_WALKS:
            self.assertIn(
                name, menu,
                '%r is absent from the menu the author will be shown. Walk 1 '
                'invented MemoryStore/MemoryRetriever in place of the memory '
                'tools and walk 2 authored `df -h` because no OS-command tool '
                'was nameable.' % name)

    def test_the_extra_matches_what_this_leg_actually_registers(self):
        """execute_windows_or_android_command is registered via register_dual.

        It is NOT in MAIN_LEG_CORE_TOOLS, so it reaches the menu only as the
        ``extra`` argument -- exactly as the Assistant and Executor legs pass
        it. Advertising it without registering it, or registering without
        advertising, are the two halves of the drift this pins.
        """
        if _MENU not in self.body:
            self.skipTest('covered by the first test')
        self.assertIn(
            'execute_windows_or_android_command', self.body,
            'the verifier menu omits execute_windows_or_android_command, '
            'which create_agents DOES register on this leg via register_dual '
            '-- the author would still be unable to name the OS-command tool, '
            'which is precisely what walk 2 needed for `df -h`')

    def test_every_authoring_leg_derives_from_the_one_helper(self):
        """Assistant, Executor and StatusVerifier must all use the same source.

        The verifier was left behind once; this fails if it happens again to
        any of the three.
        """
        for fn in ('instantiate_assistant_agent', 'instantiate_executor_agent',
                   _VERIFIER):
            body = _func(fn, self.src)
            self.assertTrue(body, '%s not found -- re-point this guard' % fn)
            self.assertIn(
                _MENU, body,
                '%s does not derive its tool menu from %s; that is how the '
                'StatusVerifier ended up authoring recipes blind.'
                % (fn, _MENU))


if __name__ == '__main__':
    unittest.main()
