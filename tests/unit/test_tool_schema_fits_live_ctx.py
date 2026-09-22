"""A tool set that cannot fit the LIVE n_ctx must never be offered.

THE MEASURED FAILURE (2026-09-22, this box, installed build).

``~/Documents/Nunba/logs/llm_outbound.jsonl`` + its ``.old`` rotation, 1,184
records.  Every HTTP 400 is ``source=autogen.reuse``; there is not one 400 on
any other source::

    (autogen.reuse, 200): 972      (autogen.create, 200): 125
    (autogen.reuse, 400):  60      (autogen.reuse,   500):  20
    (dispatcher.draft, 200):  5    (autogen.reuse, ReadError): 2

The discriminator is the SEAT, and the seat is visible in the tool count.  A
200 body carries exactly the 23 names of ``MAIN_LEG_CORE_TOOLS`` — the set
``register_core_tools(main_leg_core_tools(...), helper, assistant,
executor_proposes=True)`` puts on the ASSISTANT (reuse_recipe.py:2494).  A 400
body carries 50-65: those same 23 plus every family ``register_dual`` gives the
HELPER and only the helper — AP2 payments, A2A delegation, outreach CRM, memory
graph, model lifecycle, service tools, ``request_tools``, and the named-attach
extras.  Diffed on two adjacent live records::

    10:21:05  200  23 tools  system prompt starts 'CURRENT DATE: today is ...'
                             == reuse_recipe.py:1477 agent_prompt  (Assistant)
    10:20:38  400  60 tools  system prompt starts 'You are Helper Agent. ...'
                             == reuse_recipe.py:1588/2342            (Helper)
    only on the 400: authorize_payment, process_payment, request_payment,
      delegate_to_specialist, share_context_with_agents, get_shared_context,
      bulk_import_prospects, create_prospect, create_followup_sequence,
      move_prospect_stage, send_outreach_email, check_pending_followups,
      list_sent_emails, crawl4ai_crawl, gh_pr_open, recall_memory, remember,
      backtrace_memory, get_memory_context, get_boot_decision, get_gpu_tier,
      get_pipeline_status, get_system_health, get_tier_thresholds,
      get_tts_status, list_running_models, record_lifecycle_event,
      explain_decision, list_decisions, consult_expert, create_new_agent,
      create_scheduled_jobs, execute_windows_or_android_command,
      get_user_camera_inp_by_mins, register_visual_watcher,
      send_message_to_roles, request_tools

AND THE TOOL COUNT ALONE IS NOT THE RULE.  The SAME 50-74-tool helper bodies
returned 200 earlier the same morning and 400 later, with no code change
between them — the llama-server was restarted::

    >=50-tool bodies, by hour   04:xx 200x1   05:xx 200x23 500x2
                                06:xx 200x6   07:xx 200x20
                                08:xx 200x2  400x21
                                09:xx        400x25
                                10:xx        400x16

    logs/llama_server_8080.log (started 08:04):
      srv load_model: initializing, n_slots = 1, n_ctx_slot = 4096

The tool set is fixed at agent construction; the context moved under it.  That
is the whole defect: NOTHING in the selection path reads the live n_ctx, so the
same set is offered whatever the server is running.  The server's own words,
which the JSONL never stored::

    srv send_error: task id = 213, error: request (6249 tokens) exceeds the
                    available context size (4096 tokens), try increasing it

and the wire layer already knew, and could only complain::

    llm_outbound - ERROR - wire-trim: the TOOL SCHEMA alone is 6849 tokens
      against an n_ctx of 4096 (60 tool(s)) - no amount of message trimming
      can make this fit. Prune the tool list for this agent; the request will
      be rejected as over-length.

THE BOUND THIS GUARD PINS.  Prompt-side only, because that is what llama
rejects: ``schema_tokens <= n_ctx - min_message_budget``.  Checked against BOTH
measured populations at n_ctx 4096 (room = 4096 - 1024 = 3072):

    23 tools = 2489 tok  ->  fits    (and 516 such bodies did return 200)
    50 tools = 5782 tok  ->  pruned  (20 such bodies returned 400)
    60 tools = 7712 tok  ->  pruned  (16 such bodies returned 400)

Deliberately NOT ``- max_tokens``: reserving the 2048 generation budget as well
would put the room at 4096-2048-2816-1024 = -1792 and prune the 23-tool set
that demonstrably works.  Generation overrun is a different failure with a
different owner (the wire trim already reserves for it); this is the 400.

RED BEFORE GREEN: ``fit_schema_to_ctx`` does not exist against HEAD, so every
test in the first class fails on import, and the source guard fails because
``_attach_named_tools_for_action`` never mentions it.

    python -m pytest tests/unit/test_tool_schema_fits_live_ctx.py --noconftest -q
"""
import io
import os
import re
import sys
import unittest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if _HARTOS not in sys.path:
    sys.path.insert(0, _HARTOS)

from core.agent_tools import MAIN_LEG_CORE_TOOLS, fit_schema_to_ctx  # noqa: E402


def _entry(name, n_props=2):
    """A tool schema entry the size the live ones are.

    CALIBRATED, not guessed — the two live blocks and this fixture against
    them, so the arithmetic these tests assert is the arithmetic production
    does rather than a fixture's own:

        live helper, 60 tools : 7,712 tok    fixture n_props=2 : 7,697 (-0.2%)
        live MAIN_LEG_CORE, 23: 2,489 tok    fixture n_props=1 : 2,405 (-3.4%)

    The helper's extra families carry fatter schemas than the core closures,
    which is why the two take different widths.
    """
    props = {
        f'arg{i}': {'type': 'string',
                    'description': f'the {i}th argument of {name}, which the '
                                   f'model must supply as plain text'}
        for i in range(n_props)
    }
    return {'type': 'function',
            'function': {'name': name,
                         'description': f'{name} performs the {name} operation '
                                        f'on behalf of the calling agent',
                         'parameters': {'type': 'object', 'properties': props,
                                        'required': [f'arg{i}'
                                                     for i in range(n_props // 2)]}}}


class _FakeAgent:
    """Minimal autogen-shaped agent: llm_config['tools'] + the remove API."""

    def __init__(self, names, n_props=2):
        self.llm_config = {'config_list': [{'model': 'test'}],
                           'tools': [_entry(n, n_props) for n in names]}
        self.removed = []

    def update_tool_signature(self, name, is_remove=False):
        if not is_remove:
            raise AssertionError('fit must only ever REMOVE')
        self.removed.append(name)
        self.llm_config['tools'] = [
            e for e in self.llm_config['tools']
            if (e.get('function') or {}).get('name') != name]


def _names(agent):
    return [(e.get('function') or {}).get('name')
            for e in agent.llm_config['tools']]


def _schema_tokens(agent):
    from core.llm_outbound_logger import _schema_tokens as st
    return st({'tools': agent.llm_config['tools']})


def _pin_ctx(monkey_target, n_ctx):
    """Force the LIVE geometry reader to report ``n_ctx`` per slot."""
    import core.llm_outbound_logger as wire
    monkey_target.append((wire, '_get_budget_per_slot',
                          wire._get_budget_per_slot))
    wire._get_budget_per_slot = lambda: n_ctx


class ToolSchemaFitsLiveCtx(unittest.TestCase):

    def setUp(self):
        self._restore = []

    def tearDown(self):
        for mod, attr, old in reversed(self._restore):
            setattr(mod, attr, old)

    # ── the 400 population ────────────────────────────────────────────

    def test_a_sixty_tool_helper_is_pruned_to_fit_a_4096_slot(self):
        """The measured 400: 60 tools, 7,712 schema tokens, n_ctx 4,096."""
        _pin_ctx(self._restore, 4096)
        helper = _FakeAgent([f'tool_{i:02d}' for i in range(60)])
        before = _schema_tokens(helper)
        self.assertGreater(before, 4096,
                           'fixture must reproduce an unfittable block')
        dropped = fit_schema_to_ctx(helper)
        self.assertTrue(dropped, 'an unfittable set must be pruned')
        after = _schema_tokens(helper)
        self.assertLessEqual(
            after, 4096 - 1024,
            f'the kept schema ({after} tok) must fit n_ctx 4096 minus the '
            f'message floor (1024) — otherwise llama-server answers '
            f'"request (N tokens) exceeds the available context size"')

    def test_the_twenty_three_tool_set_that_works_is_left_alone(self):
        """MAIN_LEG_CORE_TOOLS at 2,489 tok fits 4,096 — 516 such bodies
        returned 200 and must not be pruned by a fix aimed at the 400s."""
        _pin_ctx(self._restore, 4096)
        assistant = _FakeAgent(sorted(MAIN_LEG_CORE_TOOLS), n_props=1)
        self.assertEqual(len(assistant.llm_config['tools']), 23,
                         'MAIN_LEG_CORE_TOOLS is the 23-name set the 200 '
                         'bodies carry')
        dropped = fit_schema_to_ctx(assistant)
        self.assertEqual(dropped, set(),
                         f'the working set must survive untouched; dropped '
                         f'{sorted(dropped)}')

    def test_a_larger_ctx_keeps_everything(self):
        """Before 08:04 the same 60-tool bodies returned 200 on a bigger
        slot.  The bound is the LIVE n_ctx, not a hard tool cap."""
        _pin_ctx(self._restore, 12288)
        helper = _FakeAgent([f'tool_{i:02d}' for i in range(60)])
        dropped = fit_schema_to_ctx(helper)
        self.assertEqual(dropped, set(),
                         'a set that fits the live window must not be pruned')

    # ── what survives the prune ───────────────────────────────────────

    def test_request_tools_is_never_dropped(self):
        """The escape hatch that makes every deferral recoverable.  Dropping
        it converts deferral into exclusion for the whole set."""
        _pin_ctx(self._restore, 4096)
        helper = _FakeAgent(['request_tools']
                            + [f'tool_{i:02d}' for i in range(60)])
        fit_schema_to_ctx(helper)
        self.assertIn('request_tools', _names(helper))

    def test_the_actions_own_named_tools_are_kept_first(self):
        """``attach_for_names`` attached them because the action's recipe
        names them; pruning must not undo the one authoritative selector."""
        _pin_ctx(self._restore, 4096)
        helper = _FakeAgent([f'tool_{i:02d}' for i in range(60)]
                            + ['execute_windows_or_android_command'])
        fit_schema_to_ctx(
            helper, protect={'execute_windows_or_android_command'})
        self.assertIn('execute_windows_or_android_command', _names(helper))

    def test_core_tools_outrank_the_unconditional_families(self):
        """Between two tools that both fit, keep the one the leg is built
        around.  ``google_search`` is core; ``tool_00`` is filler."""
        _pin_ctx(self._restore, 4096)
        helper = _FakeAgent([f'tool_{i:02d}' for i in range(60)]
                            + ['google_search'])
        fit_schema_to_ctx(helper)
        self.assertIn('google_search', _names(helper))

    # ── it may never be the reason a turn dies ────────────────────────

    def test_a_bare_agent_is_a_no_op(self):
        _pin_ctx(self._restore, 4096)

        class _Bare:
            pass

        self.assertEqual(fit_schema_to_ctx(_Bare()), set())
        self.assertEqual(fit_schema_to_ctx(None), set())

    def test_an_unreadable_geometry_never_raises(self):
        import core.llm_outbound_logger as wire

        def _boom():
            raise RuntimeError('llama-server is down')

        self._restore.append((wire, '_get_budget_per_slot',
                              wire._get_budget_per_slot))
        wire._get_budget_per_slot = _boom
        helper = _FakeAgent([f'tool_{i:02d}' for i in range(60)])
        self.assertEqual(fit_schema_to_ctx(helper), set(),
                         'an unreadable server must leave the set alone, not '
                         'strip the agent')

    def test_it_is_idempotent(self):
        _pin_ctx(self._restore, 4096)
        helper = _FakeAgent([f'tool_{i:02d}' for i in range(60)])
        fit_schema_to_ctx(helper)
        kept = _names(helper)
        self.assertEqual(fit_schema_to_ctx(helper), set())
        self.assertEqual(_names(helper), kept)


# ── the reuse leg must actually call it ───────────────────────────────
#
# Source-level, and overridable via HARTOS_REUSE_SRC, for the reason
# tests/unit/test_named_attach_runs_for_every_action.py documents: a guard
# that has only ever been run against the fixed file has not been shown to be
# able to fail.  reuse_recipe imports flask's current_app at module scope, so
# an import-level test would need the whole app context to say one thing about
# one function.

_SRC = os.environ.get('HARTOS_REUSE_SRC') or os.path.join(
    _HARTOS, 'hartos', 'reuse_recipe.py')


def _src():
    return io.open(_SRC, encoding='utf-8', errors='replace').read()


def _func(name, src):
    m = re.search(r'^def %s\(.*\n(?:(?:[ \t].*)?\n)*' % re.escape(name),
                  src, re.M)
    return m.group(0) if m else ''


class ReuseLegReconcilesItsToolsWithTheServer(unittest.TestCase):

    def test_the_one_per_turn_attach_door_also_fits_the_set_to_the_ctx(self):
        """``_attach_named_tools_for_action`` is the ONE door every reuse
        turn and every advance goes through (its own docstring: "One home,
        two callers ... the ONE door every advance goes through").  The set
        it just grew has to be reconciled with the server there, or it is
        offered unbounded."""
        body = _func('_attach_named_tools_for_action', _src())
        self.assertTrue(body, '_attach_named_tools_for_action not found')
        self.assertIn(
            'fit_schema_to_ctx', body,
            'the per-turn attach grows the helper\'s schema and nothing '
            'downstream bounds it: measured 2026-09-22, 60 of 60 HTTP 400s '
            'were helper-seat bodies of 50-65 tools against n_ctx 4096')

    def test_the_fit_runs_after_every_attach_in_get_agent_response(self):
        """The tag attach must not add tools AFTER the budget was settled.

        ``get_agent_response`` ran ``_attach_named_tools_for_action`` and then
        ``attach_for_tags``; with the fit inside the former, the latter could
        push the set back over for that turn's dispatch.  One door means the
        last thing to touch the set is the thing that bounds it.
        """
        body = _func('get_agent_response', _src())
        self.assertTrue(body, 'get_agent_response not found')
        named = body.find('_attach_named_tools_for_action(')
        tags = body.find('attach_for_tags(')
        self.assertNotEqual(named, -1, 'named attach call not found')
        self.assertNotEqual(tags, -1, 'tag attach call not found')
        self.assertLess(
            tags, named,
            'attach_for_tags must run BEFORE the named attach, because the '
            'named attach is where the set is reconciled with the live n_ctx '
            '— anything attached after it is offered unbounded')


if __name__ == '__main__':
    unittest.main()
