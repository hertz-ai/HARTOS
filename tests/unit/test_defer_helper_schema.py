"""Deferring a tool removes its SCHEMA only — execution and recovery survive.

WHAT THIS PRIMITIVE IS FOR, measured live 2026-09-12 on the CREATE walk of
agent 87400889007 (Nunba, the default agent).  The create helper carries 54
tools whose schema alone is 7,191 tokens against an n_ctx of 8,192 (88% of the
window):

    wire-trim: the TOOL SCHEMA alone is 7191 tokens against an n_ctx of 8192
               (54 tool(s)) -- no amount of message trimming can make this fit.
    [TRIM] trim could not reach budget -- messages 1673 tok + schema 7191 tok
               = 8864 tok against n_ctx 8192

and the walk dies at action 5 on a 400 exceed_context_size_error.  Attributing
every wire body by its system prompt: all 4 unfittable calls are the Helper
seat, all 43 fitting calls are Assistant/Executor with 18 tools, zero
crossover.  ``register_dual`` puts the schema on the helper and execution on
the assistant, so the helper accumulates every family while the assistant
keeps the bounded MAIN_LEG_CORE_TOOLS set.

Across 1,568 wire rows (21:31->00:47) ``autogen.create`` made 20 tool calls
over 9 distinct tools and every one is in MAIN_LEG_CORE_TOOLS — none of the
other 36.  At CREATE the helper AUTHORS a recipe; it does not execute.

THE SAFETY CONTRACT, which is the whole reason this is a named primitive
rather than an inline pop().  Deferring must remove ONLY what the model reads:

  1. the schema leaves ``helper.llm_config['tools']`` (the token saving), and
  2. the callable STAYS in the executor's ``_function_map`` (still runnable), and
  3. ``request_tools`` can put the schema back — which works because
     ``discover_and_attach`` now consults the executor's ``_function_map`` as a
     third source (a160020fd, guarded by
     tests/unit/test_request_tools_reaches_executor_only_tools.py).

Without (2) and (3) this would be exclusion, not deferral, and would breach the
owner's 2026-08-31 requirement that the hierarchy be LAZY, not exclusionary.

RED BEFORE GREEN: ``defer_helper_schema`` does not exist against HEAD, so
every test here fails on import.

    python -m pytest tests/unit/test_defer_helper_schema.py --noconftest -q
"""
import pytest

from core.agent_tools import defer_helper_schema


def _llm_config(*names):
    """An autogen-shaped llm_config carrying one function schema per name."""
    return {
        'config_list': [{'model': 'test'}],
        'tools': [
            {'type': 'function',
             'function': {'name': n, 'description': f'{n} does a thing',
                          'parameters': {'type': 'object', 'properties': {}}}}
            for n in names
        ],
    }


class _FakeAgent:
    def __init__(self, *names):
        self.llm_config = _llm_config(*names)


def _names(agent):
    """Tool names on an agent's schema, tolerant of malformed entries.

    Deliberately defensive: test_malformed_tool_entries_do_not_raise injects
    None / function-less / non-dict entries, and a naive
    ``t['function']['name']`` raises on those — failing the assertion helper
    rather than the code under test, which is what happened first run.
    """
    out = []
    for t in (agent.llm_config or {}).get('tools') or []:
        if isinstance(t, dict) and isinstance(t.get('function'), dict):
            name = t['function'].get('name')
            if name:
                out.append(name)
    return out


def test_named_tools_are_removed_from_the_helper_schema():
    """The token saving — this is what makes the 7191-token body fit."""
    helper = _FakeAgent('google_search', 'send_to_channel', 'request_payment')

    defer_helper_schema(helper, {'send_to_channel', 'request_payment'})

    assert _names(helper) == ['google_search']


def test_tools_not_named_are_untouched():
    """Core tools and goal-gated Tier-2 families must survive verbatim."""
    helper = _FakeAgent('google_search', 'get_user_id', 'draft_patent_claims')
    before = [dict(t) for t in helper.llm_config['tools']]

    defer_helper_schema(helper, {'send_to_channel'})

    assert helper.llm_config['tools'] == before


def test_request_tools_is_never_deferred_even_if_named():
    """The lazy escape must survive, or deferral becomes exclusion.

    Owner requirement 2026-08-31: an agent whose set lacks a capability calls
    request_tools instead of denying. If a caller ever passes it — by widening
    a family list, or by deferring "everything not core" — dropping it would
    strand every deferred tool permanently.
    """
    helper = _FakeAgent('request_tools', 'send_to_channel')

    defer_helper_schema(helper, {'request_tools', 'send_to_channel'})

    assert _names(helper) == ['request_tools']


def test_executor_function_map_is_not_touched():
    """Execution survives deferral — the callable stays runnable.

    This is the half that makes the tool recoverable: discover_and_attach
    reads the executor's _function_map (a160020fd).
    """
    helper = _FakeAgent('send_to_channel')

    class _Executor:
        def __init__(self):
            self._function_map = {'send_to_channel': lambda: 'sent'}

    executor = _Executor()
    defer_helper_schema(helper, {'send_to_channel'})

    assert 'send_to_channel' in executor._function_map


def test_empty_or_missing_inputs_are_a_no_op():
    """Degrades quietly: no names, no tools block, or no llm_config at all."""
    helper = _FakeAgent('google_search')
    defer_helper_schema(helper, set())
    assert _names(helper) == ['google_search']

    bare = _FakeAgent()
    bare.llm_config = {}
    defer_helper_schema(bare, {'anything'})        # must not raise

    class _NoConfig:
        pass

    defer_helper_schema(_NoConfig(), {'anything'})  # must not raise


def test_returns_the_names_actually_removed():
    """Callers log what they dropped — a silent prune is unauditable."""
    helper = _FakeAgent('google_search', 'send_to_channel')

    removed = defer_helper_schema(helper, {'send_to_channel', 'not_present'})

    assert removed == {'send_to_channel'}


def test_malformed_tool_entries_do_not_raise():
    """A non-dict or function-less entry must not kill agent construction."""
    helper = _FakeAgent('google_search')
    helper.llm_config['tools'].extend([None, {'type': 'function'}, 'junk'])

    defer_helper_schema(helper, {'send_to_channel'})

    assert 'google_search' in _names(helper)
