"""CREATE must not bank a step naming a tool that does not exist.

THE LIVE FAILURE, end to end, 2026-09-11 on the installed build. Agent
89088690384 "Disk Watch Dan" was CREATED live through the real /chat flow
(06:34-06:39) and REUSE-walked (06:41-06:44). The banked recipe:

    id=1  "Query the operating system for the current free disk space"  tool_name = ''
    id=2  "Store the reported value as a timestamped memory entry"      tool_name = 'MemoryStore'
    id=3  "Return the current free space value to the user"             tool_name = 'MemoryRetriever'

MemoryStore and MemoryRetriever are not tools. Evidence independent of any
registry parse: across the ENTIRE gui_app.log, `"name": "MemoryStore"` and
`"name": "MemoryRetriever"` each occur ZERO times -- if either had ever been
offered to a model it would appear in a tools schema. MemoryStore exists only
as a CLASS (integrations/channels/memory/memory_store.py); MemoryRetriever
does not exist anywhere.

WHAT IT COST, measured at the user-visible surface:
    agent replied: "The available free space on your primary system drive is
                    145.6 GB, as measured on September 11, 2026, at 12:00:00 UTC."
    reality      : Get-PSDrive C -> 9.7 GB free.   Wrong by ~15x.
    the walk ran 06:41-06:44 IST = ~01:11 UTC, so the timestamp is invented too.
    response     : success = True.

THE CHAIN. An invented name is worse than a wrong one. The fabrication gate
filters its demands to REGISTERED names (reuse_recipe.py:4950), so a name that
is not registered is never "referenced", the gate makes no demand, and the
action reads exactly like a prose action -- it can advance having executed
nothing. An invented tool_name silently converts a tool action into an
unchecked one. Meanwhile a non-empty tool_name sets
agent_to_perform_this_action = 'Helper', routing the step to the tool executor
that has no such tool.

THE INSTRUCTION ALREADY EXISTS AND IS NOT ENOUGH. create_recipe.py:5446 tells
the author verbatim: "If this step uses a tool, put the EXACT name of one of
the tools provided to you in this request. Do not invent a name. If no provided
tool fits, leave this empty string." It invented two anyway. The producer never
VALIDATES the answer against the list it just offered.

THE FIX THIS GUARD PINS: at the moment CREATE banks the recipe, check each
authored tool_name against the tools the agents actually carry -- the same
llm_config['tools'] + _hart_core_tools derivation reuse already uses, i.e. by
construction the set the model was shown -- and blank anything else so the step
routes honestly to Assistant/Executor instead of to Helper with a phantom tool.
Blanking, not dropping: the step TEXT still says what to do, so at REUSE the
model can still pick a real tool for it.

WHAT THIS GUARD DOES NOT CLAIM: that the agent then produces a correct figure,
or that the model stops inventing. It claims a name the system cannot call is
not written into a recipe as though it could.
"""

import io
import os
import re
import unittest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Overridable so the guard can be pointed at a revision that lacks the fix and
# proven to fail there (memory/feedback_vacuous_guards.md).
_SRC = os.environ.get('HARTOS_CREATE_SRC') or os.path.join(
    _HARTOS, 'hartos', 'create_recipe.py')

_HELPER = '_drop_unregistered_tool_names'

# Verbatim from the live recipe.
_LIVE_STEPS = [
    {'steps': 'Query the operating system for the current free disk space '
              'on the primary system drive in gigabytes (GB).',
     'tool_name': '', 'generalized_functions': ''},
    {'steps': 'Store the reported value as a timestamped memory entry.',
     'tool_name': 'MemoryStore', 'generalized_functions': ''},
    {'steps': 'Return the current free space value to the user.',
     'tool_name': 'MemoryRetriever', 'generalized_functions': ''},
]


def _src():
    return io.open(_SRC, encoding='utf-8', errors='replace').read()


def _func(name, src):
    """One top-level function's source, bounded by the next TOP-LEVEL statement."""
    m = re.search(r'^def %s\(.*\n(?:(?:[ \t].*)?\n)*' % re.escape(name), src, re.M)
    return m.group(0) if m else ''


class _Agent(object):
    """Minimal stand-in carrying tools the way a real autogen agent does."""

    def __init__(self, names=(), core=()):
        self.llm_config = {
            'tools': [{'type': 'function', 'function': {'name': n}}
                      for n in names]
        }
        if core:
            self._hart_core_tools = [(n, 'desc', None) for n in core]


def _load_helper():
    """exec the REAL helper against a stub logger.

    Imported behaviour, not a reimplementation: a local copy would prove
    nothing about what the pipeline banks.
    """
    body = _func(_HELPER, _src())
    if not body:
        return None
    ns = {}
    logged = []

    class _L(object):
        def warning(self, msg, *a, **k):
            logged.append(('warning', str(msg)))

        def info(self, msg, *a, **k):
            logged.append(('info', str(msg)))

        def debug(self, msg, *a, **k):
            logged.append(('debug', str(msg)))

    class _App(object):
        logger = _L()

    ns['current_app'] = _App()
    ns['tool_logger'] = _L()
    exec(compile(body, '<helper>', 'exec'), ns)
    return ns[_HELPER], logged


class TestTheHelperExistsAndBlanksPhantoms(unittest.TestCase):
    """RED until CREATE validates what it banks."""

    def setUp(self):
        loaded = _load_helper()
        if loaded is None:
            self.skipTest('helper absent -- TestItIsCalledAtBothSaveSites '
                          'is the assertion that fails for that')
        self.fn, self.logged = loaded

    def test_the_live_phantoms_are_blanked(self):
        agents = [_Agent(names=('save_data_in_memory', 'send_message_to_user'))]
        out = self.fn([dict(s) for s in _LIVE_STEPS], agents)
        names = [s.get('tool_name') for s in out]
        self.assertEqual(
            names, ['', '', ''],
            "MemoryStore/MemoryRetriever survived. They are not callable "
            "(zero occurrences in any tool schema in the whole log) and a "
            "non-empty tool_name routes the step to 'Helper' with a phantom "
            "tool, while the fabrication gate cannot demand an unregistered "
            "name -- so the action passes unchecked. Live 2026-09-11 that "
            "produced '145.6 GB' against a real 9.7 GB.")

    def test_a_real_registered_name_is_preserved(self):
        """A guard that blanks everything is worse than the bug."""
        agents = [_Agent(names=('save_data_in_memory',))]
        steps = [{'steps': 'Store it.', 'tool_name': 'save_data_in_memory'}]
        out = self.fn(steps, agents)
        self.assertEqual(
            out[0]['tool_name'], 'save_data_in_memory',
            'a REGISTERED tool name was blanked; that would strip real tools '
            'out of every recipe CREATE banks')

    def test_core_tools_count_as_registered(self):
        """_hart_core_tools is the other half of the offered set.

        reuse_recipe's own reader takes both llm_config['tools'] AND
        _hart_core_tools; taking only the first would blank every core tool
        (execute_windows_or_android_command, google_search, ...), which is 204+
        steps in the banked population.
        """
        agents = [_Agent(names=(), core=('execute_windows_or_android_command',))]
        steps = [{'steps': 'Run it.',
                  'tool_name': 'execute_windows_or_android_command'}]
        out = self.fn(steps, agents)
        self.assertEqual(out[0]['tool_name'],
                         'execute_windows_or_android_command',
                         'a CORE tool was blanked -- the reader is only '
                         "looking at llm_config['tools']")

    def test_the_tool_colon_argument_authoring_form_survives(self):
        """`<tool>: <argument>` in tool_name is a REAL name, not a phantom.

        The authoring model routinely writes the tool AND its argument into the
        one field -- reuse_recipe's _tool_name_candidates exists precisely for
        that. Measured in the banked population: 204 steps name
        execute_windows_or_android_command and 6 of them carry a trailing
        argument. Blanking those on a raw-string mismatch would strip real
        tools out of recipes that work today -- a regression far worse than
        the phantom this guard is for.
        """
        agents = [_Agent(core=('execute_windows_or_android_command',))]
        steps = [{'steps': 'Click it.',
                  'tool_name': "execute_windows_or_android_command: click the "
                               "'Search' button"}]
        out = self.fn(steps, agents)
        self.assertTrue(
            out[0]['tool_name'],
            'the <tool>: <argument> form was blanked; that is a REGISTERED '
            'tool with its argument appended, and 204 banked steps use this '
            'tool')

    def test_empty_stays_empty_and_shape_is_preserved(self):
        agents = [_Agent(names=('save_data_in_memory',))]
        steps = [{'steps': 'Think about it.', 'tool_name': '',
                  'generalized_functions': 'x = 1'}]
        out = self.fn(steps, agents)
        self.assertEqual(out[0]['tool_name'], '')
        self.assertEqual(out[0]['generalized_functions'], 'x = 1',
                         'the helper altered a field that is not its business')

    def test_a_blanking_is_logged_loudly_enough_to_measure(self):
        """gui_app.log captures ZERO debug lines (0 of 45,599 measured)."""
        agents = [_Agent(names=('save_data_in_memory',))]
        self.fn([dict(s) for s in _LIVE_STEPS], agents)
        levels = {lvl for lvl, _ in self.logged}
        self.assertTrue(
            levels & {'warning', 'error'},
            'a phantom tool name must be countable in production; debug is '
            'invisible there. Levels emitted: %r' % (sorted(levels),))

    def test_it_never_raises(self):
        """It runs while a recipe is being banked; it must not cost the save."""
        for junk_steps in (None, [], [None], [{}], 'nonsense', [{'tool_name': 5}]):
            for junk_agents in (None, [], [object()]):
                try:
                    self.fn(junk_steps, junk_agents)
                except Exception as err:
                    self.fail('raised on steps=%r agents=%r: %r'
                              % (junk_steps, junk_agents, err))


class TestItIsCalledAtBothSaveSites(unittest.TestCase):
    """Both save blocks, BEFORE the role assignment that reads tool_name.

    create_recipe.py banks the recipe in two near-identical places: the normal
    path and the 'Late save' twin. A validator on only one of them leaves the
    other free to bank phantoms.
    """

    def setUp(self):
        self.src = _src()

    def _save_blocks(self):
        # The role-assignment loop is the marker: it is the consumer of
        # tool_name, and the validation has to run before it.
        return re.findall(
            r"for i in json_obj\['recipe'\]:\s*\n\s*if 'tool_name' in i",
            self.src)

    def test_both_save_blocks_still_exist(self):
        self.assertEqual(
            len(self._save_blocks()), 2,
            'expected exactly 2 role-assignment loops (normal save + late '
            'save); the file changed shape, re-point this guard')

    def test_the_validator_runs_before_each_role_assignment(self):
        for m in re.finditer(
                r"for i in json_obj\['recipe'\]:\s*\n\s*if 'tool_name' in i",
                self.src):
            head = self.src[:m.start()]
            tail = head[-1200:]
            self.assertIn(
                _HELPER, tail,
                'a role-assignment loop at offset %d has no %s call in the '
                '1200 chars before it. A non-empty tool_name sets '
                "agent_to_perform_this_action='Helper', so validation must "
                'happen BEFORE that decision, at BOTH save sites.'
                % (m.start(), _HELPER))

    def test_the_helper_is_defined_once(self):
        self.assertEqual(
            len(re.findall(r'^def %s\(' % _HELPER, self.src, re.M)), 1,
            'the validator must have ONE definition; two copies drift')


if __name__ == '__main__':
    unittest.main()
