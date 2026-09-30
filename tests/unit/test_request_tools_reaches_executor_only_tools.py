"""``request_tools`` must be able to re-arm a tool the helper can no longer SEE.

THE CONSTRAINT THIS PINS, measured live 2026-09-12 during the CREATE walk of
agent 87400889007 (Nunba, the default agent).

The create helper carries 54 tools whose schema alone is 7,191 tokens against
an n_ctx of 8,192 — 88% of the window — so the wire trimmer reports:

    wire-trim: the TOOL SCHEMA alone is 7191 tokens against an n_ctx of 8192
               (54 tool(s)) -- no amount of message trimming can make this fit.
    [TRIM] trim could not reach budget -- messages 1673 tok + schema 7191 tok
               = 8864 tok against n_ctx 8192

and the walk dies at action 5 with a 400 exceed_context_size_error.  Measured
across 1,568 wire rows (21:31 -> 00:47), ``autogen.create`` made 20 tool calls
over 9 distinct tools and EVERY one is in the 18-name MAIN_LEG_CORE_TOOLS set:
zero of the other 36 was ever called.  So narrowing the helper's schema is the
fix.

WHY THIS TEST EXISTS AND MUST COME FIRST.  Narrowing is only safe if the
dropped tools stay REACHABLE, because the owner's requirement (2026-08-31) is
that the hierarchy be "LAZY, not exclusionary — an agent whose current set
lacks a capability calls request_tools instead of denying".  Today
``discover_and_attach`` searches exactly two sources: the service registry
(``registry._tools``) and ``core_tools``.  The families that make the create
helper overflow — channel (11), memory-graph (5), coding (4), AP2 payments (3),
media (3) — are in NEITHER.  Dropping them without this fix would make them
permanently unreachable at CREATE, which is a REGRESSION wearing a fix's
clothes, not a narrowing.

``register_dual`` already puts the callable on the executor
(``register_for_execution`` -> ``_function_map``) and only the SCHEMA on the
helper, so the executor is a live third source that costs nothing to consult.
``reuse_recipe.py:5000-5008`` already reads ``_function_map`` for the sibling
"can this agent serve the call" question, so this is the established accessor,
not a new one.

RED BEFORE GREEN: against HEAD ``discover_and_attach`` never looks at the
executor, so the executor-only tool is not re-attached and test 1 fails.

    python -m pytest tests/unit/test_request_tools_reaches_executor_only_tools.py --noconftest -q
"""
import pytest

from core.agent_tools import discover_and_attach


class _FakeAgent:
    """Minimal stand-in for an autogen ConversableAgent pair member.

    Records what register_for_llm / register_for_execution were handed, which
    is exactly the helper=schema / executor=execution split register_dual
    relies on.
    """

    def __init__(self):
        self.llm_registered = {}      # name -> description  (the SCHEMA half)
        self._function_map = {}       # name -> callable     (the EXEC half)

    def register_for_llm(self, name=None, description=None):
        def _wrap(func):
            self.llm_registered[name] = description
            return func
        return _wrap

    def register_for_execution(self, name=None):
        def _wrap(func):
            self._function_map[name] = func
            return func
        return _wrap


class _EmptyRegistry:
    """A service registry holding nothing — isolates the executor source."""
    _tools = {}


def _send_to_channel(text: str) -> str:
    """Send a message to a connected channel such as Discord or Slack."""
    return 'sent'


def test_executor_only_tool_is_reattached_to_the_helper():
    """The measured case: schema stripped from helper, callable still on executor.

    This is the state the create-helper narrowing creates. request_tools must
    bring the schema back, or the tool is gone for good.
    """
    helper, executor = _FakeAgent(), _FakeAgent()
    executor._function_map['send_to_channel'] = _send_to_channel

    discover_and_attach('send a message to a channel', helper, executor,
                        _EmptyRegistry(), set())

    assert 'send_to_channel' in helper.llm_registered, (
        "discover_and_attach did not re-arm a tool that is executable on the "
        "executor but has no schema on the helper. Narrowing the create "
        "helper would make channel/memory/coding/AP2/media tools permanently "
        "unreachable, breaking the owner's LAZY-not-exclusionary requirement.")


def test_already_attached_names_are_not_reattached():
    """Idempotent across turns — same contract the other two sources honour."""
    helper, executor = _FakeAgent(), _FakeAgent()
    executor._function_map['send_to_channel'] = _send_to_channel

    discover_and_attach('send a message to a channel', helper, executor,
                        _EmptyRegistry(), {'send_to_channel'})

    assert 'send_to_channel' not in helper.llm_registered, (
        'a name already in attached_names must be skipped, or every turn '
        're-registers the same tool and the schema grows without bound')


def test_non_matching_executor_tool_is_left_alone():
    """The selector still SELECTS — this is what stops it re-attaching all 36.

    Without this the "fix" would hand the helper everything on the executor,
    i.e. exactly the 54-tool body the narrowing exists to prevent.
    """
    helper, executor = _FakeAgent(), _FakeAgent()
    executor._function_map['send_to_channel'] = _send_to_channel

    discover_and_attach('generate a picture of a cat', helper, executor,
                        _EmptyRegistry(), set())

    assert 'send_to_channel' not in helper.llm_registered, (
        'an unrelated capability request attached a channel tool — the '
        'keyword/stem selector is not being applied to the executor source')


def test_no_executor_function_map_is_a_no_op():
    """An executor without _function_map must not raise.

    The time and visual legs pass agents built by other factories; a missing
    attribute has to degrade to today's behaviour, not kill the turn.
    """
    helper = _FakeAgent()

    class _Bare:
        pass

    # Must not raise.
    discover_and_attach('send a message to a channel', helper, _Bare(),
                        _EmptyRegistry(), set())
    assert helper.llm_registered == {}


def test_core_tools_source_still_works():
    """No regression to the D25/#788 core-closure source added 2026-09-09."""
    helper, executor = _FakeAgent(), _FakeAgent()

    def _crawl(url: str) -> str:
        return 'ok'

    discover_and_attach('crawl a webpage', helper, executor, _EmptyRegistry(),
                        set(), core_tools=[('crawl4ai_crawl',
                                            'Crawl a webpage and return text',
                                            _crawl)])

    assert 'crawl4ai_crawl' in helper.llm_registered
