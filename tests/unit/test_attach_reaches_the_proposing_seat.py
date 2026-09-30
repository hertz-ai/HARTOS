"""A tool attached for the seat that SPEAKS must reach that seat -- bounded,
only when it was asked for by name, and never stranded by a deferral.

THE DEFECT, measured live 2026-09-25 on the installed Nunba (HARTOS 20bfca03d):

  A2A-3  POST /chat create_agent + autonomous, an action that says "call
         delegate_to_specialist".  The Assistant answered "the
         delegate_to_specialist tool isn't available in my current toolset",
         saved delegation_id null, and the verifier marked it completed.
  A2A-7  the same shape for share_context_with_agents / get_shared_context.

``register_core_tools(..., executor_proposes=True)`` makes the main leg's
Assistant a proposing seat with its own schema, but every other registration
(``register_dual``) and every on-demand attach put the schema on the Helper
only -- including the escape, ``request_tools``.

The first fix (f526c4580) put EVERY attach on the Assistant and was withdrawn
after review found: the CREATE Assistant's schema grew without bound; the
fuzzy matcher put device_control / execute_coding_task on the speaking seat;
a tool deferred by fit_schema_to_ctx could never come back (the ledger only
grows and every path skipped ledger names); and 3 of 6 attach sites had no
test that could fail.  Each of those is a test class below.

Real autogen agents: the claim is about what lands in ``llm_config['tools']``,
which is what the model is sent.
"""
import json
from typing import Annotated

import pytest

autogen = pytest.importorskip('autogen')

import core.llm_outbound_logger as lol  # noqa: E402
from core.agent_tools import (  # noqa: E402
    attach_for_names,
    attach_for_tags,
    defer_helper_schema,
    discover_and_attach,
    fit_schema_to_ctx,
    helper_tool_names,
    register_core_tools,
    register_dual,
    register_request_tools,
)

# Never contacted: registration builds the client, it does not call it.
_CFG = {'config_list': [{'model': 'x', 'api_key': 'x',
                         'base_url': 'http://127.0.0.1:1/v1'}]}


@pytest.fixture(autouse=True)
def _roomy_ctx(monkeypatch):
    """No llama-server here: pin the live room so fit_schema_to_ctx never
    probes, and so a test that wants a tight window can say so."""
    monkeypatch.setattr(lol, 'schema_token_room', lambda: 100_000)


def _send_message_to_user(text: Annotated[str, 'text']) -> str:
    """Send a message to the user."""
    return 'sent'


def _share_context_with_agents(
        context_key: Annotated[str, 'key'],
        context_value: Annotated[str, 'value']) -> str:
    """Share context information with other agents."""
    return json.dumps({'success': True})


def _get_shared_context(context_key: Annotated[str, 'key']) -> str:
    """Retrieve context information shared by other agents"""
    return json.dumps({'success': True})


def _delegate_to_specialist(task: Annotated[str, 'task']) -> str:
    """Delegate a task to a specialist agent based on required skills"""
    return json.dumps({'success': True, 'delegation_id': 'd1'})


def _device_control(command: Annotated[str, 'command']) -> str:
    """Control a device: share its screen context with other agents."""
    return 'ok'


def _main_leg():
    """Helper / Assistant / Executor wired the way create_agents wires them."""
    helper = autogen.AssistantAgent('Helper', llm_config=dict(_CFG))
    assistant = autogen.AssistantAgent('Assistant', llm_config=dict(_CFG))
    executor = autogen.UserProxyAgent('Executor', code_execution_config=False,
                                      human_input_mode='NEVER')
    register_core_tools([('send_message_to_user', 'send',
                          _send_message_to_user)],
                        helper, assistant,
                        executor_proposes=True, second_executor=executor)
    return helper, assistant, executor


def _a2a_registered_at_construction(helper, assistant):
    """create_recipe's delegate/share/get registration, then the CREATE
    helper deferral that drops them from the Helper's schema too."""
    register_dual(helper, assistant, _share_context_with_agents,
                  'share_context_with_agents', 'Share context with other agents')
    register_dual(helper, assistant, _get_shared_context,
                  'get_shared_context', 'Retrieve shared context')
    register_dual(helper, assistant, _delegate_to_specialist,
                  'delegate_to_specialist', 'Delegate to a specialist')
    register_dual(helper, assistant, _device_control,
                  'device_control', 'Control a device')
    defer_helper_schema(helper, {'share_context_with_agents',
                                 'get_shared_context',
                                 'delegate_to_specialist', 'device_control'})


def _room_for(agent, names, slack=5):
    """Tokens that fit exactly ``names``' entries of ``agent``'s schema."""
    from core.llm_outbound_logger import _schema_tokens
    return _schema_tokens({'tools': [
        e for e in agent.llm_config['tools']
        if e['function']['name'] in set(names)]}) + slack


class _EmptyRegistry:
    _tools = {}

    def create_endpoint_function(self, tool_name, ep_name):  # pragma: no cover
        return None


class _Tool:
    def __init__(self, tags, description, endpoints):
        self.tags = tags
        self.description = description
        self.endpoints = endpoints


def _crawl(url: Annotated[str, 'url']) -> str:
    """Crawl a webpage."""
    return 'page'


class _Registry:
    """One service tool, crawl4ai, with one endpoint, crawl4ai_crawl."""

    def __init__(self):
        self._tools = {'crawl4ai': _Tool(
            ['crawling', 'web'], 'Crawl and scrape web pages',
            {'crawl': {'description': 'Crawl a webpage and return text'}})}

    def create_endpoint_function(self, tool_name, ep_name):
        return _crawl


# ---------------------------------------------------------------------------
class TestTheEscapeReachesTheSpeakingSeat:

    def test_request_tools_is_on_the_proposing_assistant(self):
        helper, assistant, _ = _main_leg()
        register_request_tools(helper, assistant, _EmptyRegistry(), set())
        assert 'request_tools' in helper_tool_names(helper)
        assert 'request_tools' in helper_tool_names(assistant)

    def test_a_non_proposing_executor_gets_no_schema(self):
        helper = autogen.AssistantAgent('Helper', llm_config=dict(_CFG))
        proxy = autogen.UserProxyAgent('Executor', code_execution_config=False,
                                       human_input_mode='NEVER')
        register_request_tools(helper, proxy, _EmptyRegistry(), set())
        assert 'request_tools' in helper_tool_names(helper)
        assert helper_tool_names(proxy) == set()
        assert 'request_tools' in proxy._function_map

    def test_the_escape_calls_discovery_with_the_core_list_read_at_call_time(
            self):
        helper, assistant, _ = _main_leg()
        rt = register_request_tools(helper, assistant, _EmptyRegistry(), set())
        # Set AFTER registration, as both legs do.
        assistant._hart_core_tools = [
            ('delegate_to_specialist', 'Delegate to a specialist',
             _delegate_to_specialist)]
        out = rt('delegate_to_specialist')
        assert 'delegate_to_specialist' in out
        assert 'delegate_to_specialist' in helper_tool_names(assistant)


# ---------------------------------------------------------------------------
class TestANamedRequestReachesTheProposerFromEverySource:
    """One test per source discover_and_attach reads, so removing the
    proposer half from any one of them fails (review: 3 of 6 sites had no
    test that could)."""

    def test_the_executors_function_map_source(self):
        """A2A-3 exactly: registered at construction, deferred off the
        Helper, then asked for by name."""
        helper, assistant, _ = _main_leg()
        _a2a_registered_at_construction(helper, assistant)
        out = discover_and_attach('delegate_to_specialist', helper, assistant,
                                  _EmptyRegistry(), set())
        assert 'delegate_to_specialist' in helper_tool_names(assistant), out
        assert 'delegate_to_specialist' in helper_tool_names(helper)
        assert 'Assistant can call directly: delegate_to_specialist' in out

    def test_the_core_closure_source(self):
        helper, assistant, _ = _main_leg()
        core = [('get_shared_context', 'Retrieve shared context',
                 _get_shared_context)]
        discover_and_attach('get shared context', helper, assistant,
                            _EmptyRegistry(), set(), core_tools=core)
        assert 'get_shared_context' in helper_tool_names(assistant)

    def test_the_service_registry_source(self):
        helper, assistant, _ = _main_leg()
        discover_and_attach('crawl4ai crawl a webpage', helper, assistant,
                            _Registry(), set())
        assert 'crawl4ai_crawl' in helper_tool_names(helper)
        assert 'crawl4ai_crawl' in helper_tool_names(assistant)


class TestAFuzzyMatchStaysWithTheHelper:
    """The keyword/stem matcher over-matches on purpose; only what the need
    NAMES goes on the seat that speaks."""

    def test_share_context_attaches_the_named_tool_not_device_control(self):
        helper, assistant, _ = _main_leg()
        _a2a_registered_at_construction(helper, assistant)
        out = discover_and_attach('share context with other agents', helper,
                                  assistant, _EmptyRegistry(), set())
        on_assistant = helper_tool_names(assistant)
        assert 'share_context_with_agents' in on_assistant
        # device_control's docstring matches the need's words; its NAME does
        # not, so it goes to the Helper only, and the reply says so.
        assert 'device_control' in helper_tool_names(helper)
        assert 'device_control' not in on_assistant
        assert 'ask @Helper to call' in out and 'device_control' in out

    def test_a_service_tool_matched_only_by_description(self):
        helper, assistant, _ = _main_leg()
        discover_and_attach('scrape web pages', helper, assistant,
                            _Registry(), set())
        assert 'crawl4ai_crawl' in helper_tool_names(helper)
        assert 'crawl4ai_crawl' not in helper_tool_names(assistant)

    def test_a_non_proposer_executor_is_never_given_a_schema(self):
        helper = autogen.AssistantAgent('Helper', llm_config=dict(_CFG))
        proxy = autogen.UserProxyAgent('Executor', code_execution_config=False,
                                       human_input_mode='NEVER')
        out = discover_and_attach('crawl4ai crawl', helper, proxy,
                                  _Registry(), set())
        assert helper_tool_names(proxy) == set()
        assert 'crawl4ai_crawl' in helper_tool_names(helper)
        assert 'can call directly' not in out


# ---------------------------------------------------------------------------
class TestWhatReachesTheProposerIsBoundedToTheLiveWindow:
    """CREATE has no per-turn fit, so discovery bounds the seat it widened."""

    def test_a_tight_window_defers_the_rest_but_keeps_what_was_named(
            self, monkeypatch):
        helper, assistant, _ = _main_leg()
        _a2a_registered_at_construction(helper, assistant)
        before = helper_tool_names(assistant)
        from core.llm_outbound_logger import _schema_tokens
        # Room for the named tool alone, measured on a probe agent with the
        # description discovery gives it (the docstring's first line): it
        # must win the window over the core tool.
        probe = autogen.AssistantAgent('Probe', llm_config=dict(_CFG))
        probe.register_for_llm(
            name='delegate_to_specialist',
            description=_delegate_to_specialist.__doc__)(_delegate_to_specialist)
        room = _room_for(probe, {'delegate_to_specialist'})
        monkeypatch.setattr(lol, 'schema_token_room', lambda: room)
        out = discover_and_attach('delegate_to_specialist', helper, assistant,
                                  _EmptyRegistry(), set())
        after = helper_tool_names(assistant)
        assert 'delegate_to_specialist' in after, out
        assert 'send_message_to_user' in before
        assert 'send_message_to_user' not in after, (
            'the window was not reconciled: the schema kept everything')
        assert _schema_tokens({'tools': assistant.llm_config['tools']}) <= room


# ---------------------------------------------------------------------------
class TestAttachForNamesReachesTheProposer:

    def test_the_core_loop(self):
        helper, assistant, _ = _main_leg()
        n = attach_for_names(['delegate_to_specialist'], helper, assistant,
                             _EmptyRegistry(), set(),
                             core_tools=[('delegate_to_specialist', 'Delegate',
                                          _delegate_to_specialist)])
        assert n == 1
        assert 'delegate_to_specialist' in helper_tool_names(assistant)
        assert 'delegate_to_specialist' in helper_tool_names(helper)

    def test_the_registry_loop(self):
        helper, assistant, _ = _main_leg()
        n = attach_for_names(['crawl4ai_crawl'], helper, assistant,
                             _Registry(), set())
        assert n == 1
        assert 'crawl4ai_crawl' in helper_tool_names(assistant)

    def test_tags_stay_with_the_helper(self):
        helper, assistant, _ = _main_leg()
        n = attach_for_tags({'crawling'}, helper, assistant, _Registry(), set())
        assert n == 1
        assert 'crawl4ai_crawl' in helper_tool_names(helper)
        assert 'crawl4ai_crawl' not in helper_tool_names(assistant)

    def test_construction_registration_leaves_the_assistant_alone(self):
        helper, assistant, _ = _main_leg()
        before = helper_tool_names(assistant)
        _a2a_registered_at_construction(helper, assistant)
        assert helper_tool_names(assistant) == before


# ---------------------------------------------------------------------------
class TestADeferredToolComesBack:
    """The ledger only grows and schemas shrink; "attached" is both."""

    def test_named_attach_after_the_fit_deferred_it(self):
        helper, assistant, _ = _main_leg()
        ledger = set()
        core = [('delegate_to_specialist', 'Delegate', _delegate_to_specialist)]
        assert attach_for_names(['delegate_to_specialist'], helper, assistant,
                                _EmptyRegistry(), ledger, core_tools=core) == 1
        fit_schema_to_ctx(assistant, protect={'send_message_to_user'},
                          room=_room_for(assistant, {'send_message_to_user'}))
        fit_schema_to_ctx(helper, protect={'send_message_to_user'},
                          room=_room_for(helper, {'send_message_to_user'}))
        assert 'delegate_to_specialist' not in helper_tool_names(assistant)
        assert 'delegate_to_specialist' not in helper_tool_names(helper)
        assert 'delegate_to_specialist' in ledger
        assert attach_for_names(['delegate_to_specialist'], helper, assistant,
                                _EmptyRegistry(), ledger, core_tools=core) == 1
        assert 'delegate_to_specialist' in helper_tool_names(assistant)
        assert 'delegate_to_specialist' in helper_tool_names(helper)

    def test_request_tools_after_the_fit_deferred_it(self):
        helper, assistant, _ = _main_leg()
        ledger = set()
        core = [('delegate_to_specialist', 'Delegate', _delegate_to_specialist)]
        attach_for_names(['delegate_to_specialist'], helper, assistant,
                         _EmptyRegistry(), ledger, core_tools=core)
        fit_schema_to_ctx(helper, protect={'send_message_to_user'},
                          room=_room_for(helper, {'send_message_to_user'}))
        assert 'delegate_to_specialist' not in helper_tool_names(helper)
        out = discover_and_attach('delegate a task to a specialist', helper,
                                  assistant, _EmptyRegistry(), ledger,
                                  core_tools=core)
        assert 'No local registry tool matches' not in out
        assert 'delegate_to_specialist' in helper_tool_names(helper)

    def test_tag_attach_after_a_deferral(self):
        helper, assistant, _ = _main_leg()
        ledger = set()
        assert attach_for_tags({'web'}, helper, assistant, _Registry(),
                               ledger) == 1
        defer_helper_schema(helper, {'crawl4ai_crawl'})
        assert attach_for_tags({'web'}, helper, assistant, _Registry(),
                               ledger) == 1
        assert 'crawl4ai_crawl' in helper_tool_names(helper)

    def test_still_idempotent_when_nothing_was_deferred(self):
        helper, assistant, _ = _main_leg()
        ledger = set()
        core = [('delegate_to_specialist', 'Delegate', _delegate_to_specialist)]
        assert attach_for_names(['delegate_to_specialist'], helper, assistant,
                                _EmptyRegistry(), ledger, core_tools=core) == 1
        assert attach_for_names(['delegate_to_specialist'], helper, assistant,
                                _EmptyRegistry(), ledger, core_tools=core) == 0
        assert attach_for_tags({'web'}, helper, assistant, _Registry(),
                               ledger) == 1
        assert attach_for_tags({'web'}, helper, assistant, _Registry(),
                               ledger) == 0


class TestARequestDoesNotEvictTheActionsOwnTool:
    """Review of ee79a6fcb, measured: request_tools' fit protected only what
    it had just attached, so it evicted the action's recipe-named tool that
    the per-turn fit had protected (crawl4ai_crawl gone after
    request_tools(delegate_to_specialist)).  One protected set per agent."""

    def test_the_turns_protected_tool_survives_a_request(self, monkeypatch):
        helper, assistant, _ = _main_leg()
        attach_for_names(['crawl4ai_crawl'], helper, assistant, _Registry(),
                         set())
        # Room for exactly the action's tool plus the one about to be named.
        probe = autogen.AssistantAgent('Probe', llm_config=dict(_CFG))
        probe.register_for_llm(
            name='delegate_to_specialist',
            description=_delegate_to_specialist.__doc__)(_delegate_to_specialist)
        room = (_room_for(assistant, {'crawl4ai_crawl'}, slack=0)
                + _room_for(probe, {'delegate_to_specialist'}, slack=10))
        monkeypatch.setattr(lol, 'schema_token_room', lambda: room)
        # The per-turn fit, as REUSE's attach door runs it.
        fit_schema_to_ctx(assistant, protect={'crawl4ai_crawl'},
                          turn_protect=True)
        assert 'crawl4ai_crawl' in helper_tool_names(assistant)
        discover_and_attach('delegate_to_specialist', helper, assistant,
                            _EmptyRegistry(), set(),
                            core_tools=[('delegate_to_specialist',
                                         _delegate_to_specialist.__doc__,
                                         _delegate_to_specialist)])
        on = helper_tool_names(assistant)
        assert 'delegate_to_specialist' in on, on
        assert 'crawl4ai_crawl' in on, (
            "the request evicted the action's own recipe-named tool: %r" % on)

    def test_without_turn_protect_nothing_is_remembered(self):
        helper, assistant, _ = _main_leg()
        fit_schema_to_ctx(assistant, protect={'send_message_to_user'})
        assert not getattr(assistant, '_hart_turn_protect', None)
