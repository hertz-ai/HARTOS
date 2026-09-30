"""The tool menu a prompt SHOWS must be the tool set the leg REGISTERS.

THE LIVE FAILURE THIS ENCODES (2026-09-10, agent 28160128202 and the whole corpus):

create_recipe.py has TWO legs, and they register DIFFERENT sets:

    create_agents      :1116  register_core_tools(main_leg_core_tools(core_tools), ...)
                              -> the FILTERED 18 in MAIN_LEG_CORE_TOOLS
    create_time_agents :3546  register_core_tools(core_tools_time, ...)
                              -> the FULL 37 build_core_tool_closures list

Every prose menu in the file was one hand-copy of the same 16 names, shown on BOTH
legs, and it had drifted from each:

    ON THE MAIN LEG, REGISTERED BUT NEVER ADVERTISED:
        get_chat_history, search_visual_history, txt2img, img2txt
    ON THE MAIN LEG, ADVERTISED BUT NOT REGISTERED:
        text_2_image, get_text_from_image   (real closures -- but filtered out of
            MAIN_LEG_CORE_TOOLS in favour of txt2img / img2txt)
        create_scheduled_jobs               (deliberately absent; the factory twin
            is a create-flow stub -- see the note on MAIN_LEG_CORE_TOOLS)

So the authoring model was told three names this leg cannot call and never told about
get_chat_history.  Measured over all 127 saved flow recipes / 1,034 steps: only 241
steps (23.3%) name a tool the runtime serves, and 51.4% of identifier-shaped names are
unserved -- dominated by near-misses of exactly the capabilities the menu omits
(retrieve_memory / memory_query / search_chat_history / MemoryService for
get_chat_history; web_search for google_search).  The hallucinations are the shape of
the hole.

The registry ground truth used for those numbers is the wire itself: 71 distinct
tools[].function.name values in logs/llm_outbound.jsonl.

This suite pins the ONE invariant that stops the drift: every prose menu is DERIVED
from the list its own leg registers, never hand-listed.
"""

import io
import os
import re
import sys
import unittest

import core.agent_tools  # noqa: F401  (ensure cached)

at = sys.modules['core.agent_tools']
_CREATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(at.__file__))),
    'hartos', 'create_recipe.py')

# Names the MAIN leg's prompts advertised that the MAIN leg does not register.
# They are real closures (build_core_tool_closures builds all four), but
# main_leg_core_tools() keeps only the txt2img / img2txt spelling, so on this
# leg the other two resolve to "Function not found".  create_scheduled_jobs is
# the third: filtered off deliberately (create-flow stub twin).
DEAD_NAMES = ('text_2_image', 'get_text_from_image', 'create_scheduled_jobs')


class TestMenuIsDerivedFromRegistration(unittest.TestCase):

    def test_menu_advertises_every_registered_core_tool(self):
        menu = at.main_leg_tool_menu()
        missing = sorted(n for n in at.MAIN_LEG_CORE_TOOLS if n not in menu)
        self.assertEqual(
            missing, [],
            'the leg registers these but the menu does not advertise them: %s'
            % missing)

    def test_get_chat_history_is_advertised(self):
        """The specific omission the corpus hallucinated four aliases for."""
        self.assertIn('get_chat_history', at.main_leg_tool_menu())

    def test_menu_never_advertises_a_dead_name(self):
        menu = at.main_leg_tool_menu()
        for dead in DEAD_NAMES:
            self.assertNotIn(
                dead, menu,
                '%r is not registered on the main leg; it is filtered out of '
                'MAIN_LEG_CORE_TOOLS' % dead)

    def test_extras_are_included(self):
        menu = at.main_leg_tool_menu(('execute_windows_or_android_command',))
        self.assertIn('execute_windows_or_android_command', menu)

    def test_unfiltered_leg_menu_names_what_that_leg_registers(self):
        """create_time_agents registers the closure list UNFILTERED."""
        tools = [('text_2_image', 'd', None), ('get_chat_history', 'd', None)]
        menu = at.registered_tool_menu(tools)
        self.assertEqual(menu, 'get_chat_history, text_2_image')

    def test_extras_do_not_mutate_the_canonical_set(self):
        before = set(at.MAIN_LEG_CORE_TOOLS)
        at.main_leg_tool_menu(('some_extra_tool',))
        self.assertEqual(set(at.MAIN_LEG_CORE_TOOLS), before)

    def test_menu_is_a_readable_comma_list(self):
        menu = at.main_leg_tool_menu()
        self.assertNotIn('{', menu)
        self.assertNotIn("'", menu)
        self.assertGreaterEqual(len(menu.split(',')), len(at.MAIN_LEG_CORE_TOOLS))


class TestCreateRecipeNoLongerHandListsTools(unittest.TestCase):
    """Drift guard: the hand-copy is what went stale in the first place."""

    def _src(self):
        return io.open(_CREATE, encoding='utf-8', errors='replace').read()

    def test_the_dead_names_are_gone_from_create_recipe(self):
        src = self._src()
        for dead in DEAD_NAMES:
            self.assertNotIn(
                "%s," % dead, src,
                'create_recipe.py still hand-lists %r in a prose menu -- this is '
                'what taught the model a name its leg cannot call' % dead)

    def _derived_locals(self, src):
        return set(re.findall(
            r'(\w+)\s*=\s*(?:main_leg_tool_menu|registered_tool_menu)\(', src))

    def test_no_line_hand_lists_a_tool_menu(self):
        """A line naming 5+ registered tools is a hand-copied MENU.

        Phrase-matching the headings was too brittle -- "Tools Helper Agent can
        use:" is also a bare heading with the names on the NEXT line.  What
        separates a menu from prose is the COUNT, and the two populations do not
        overlap: measured on the pre-fix file, the five menu lines named 16, 17,
        16, 16 and 16 names each, while the only other lines naming tools at all
        named 3 (:3385, :3395 -- real usage guidance about save_data_in_memory /
        get_data_by_key / get_saved_metadata, which this leg does register).
        5 sits in the empty gap between them.
        """
        known = set(at.MAIN_LEG_CORE_TOOLS) | set(DEAD_NAMES) | {
            'get_user_details', 'data_extraction_from_url', 'get_data_from_memory'}
        word = re.compile('[^A-Za-z0-9_]+')
        offenders = []
        for i, ln in enumerate(self._src().splitlines()):
            hits = set(word.split(ln)) & known
            if len(hits) >= 5:
                offenders.append((i + 1, len(hits)))
        self.assertEqual(
            offenders, [],
            'these lines hand-list a tool menu instead of interpolating a '
            'derived one (line, name count): %s' % offenders)

    def test_the_prose_menus_interpolate_a_derived_local(self):
        src = self._src()
        derived = self._derived_locals(src)
        self.assertTrue(derived, 'no menu is derived from a helper at all')
        lines = src.splitlines()
        sites = [i + 1 for i, ln in enumerate(lines)
                 if (set(re.findall(r'[{+]\s*(\w+)', ln)) & derived
                     and not re.match(r'\s*_\w*tool_menu\s*=', ln))]
        self.assertGreaterEqual(
            len(sites), 5,
            'expected the 5 known prose-menu sites to interpolate one of %s; '
            'found %d: %s' % (sorted(derived), len(sites), sites))


if __name__ == '__main__':
    unittest.main()
