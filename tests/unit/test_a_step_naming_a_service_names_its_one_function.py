"""A step that names a service tool by its tool name names that tool's one
function, for the completion check as for the attach.

WHAT WENT WRONG (live, source Nunba on the local 4B, 18 Nov 2026 22:25-22:26
IST, research agent 1795014914479, a test account): action 2 read
"crawl4ai: fetch and read the top result pages from the search to gather
information".  The attach (core.agent_tool_menu.attach_for_names) already
counts a one-endpoint tool as named by its tool name, so it attached
crawl4ai_crawl, and the crawl ran (HTTP 200, 1181 words).  The completion
check's rule for "this step names that tool"
(hartos.lifecycle_hooks.action_named_tools) matched function names only, so
it saw no tool named: the fabrication gate passed with nothing to check, the
receipt search never ran, and the step GAVE_UP with "no canonical receipt in
its dispatch window".  49 actions in 27 agents' configs on that desktop name
crawl4ai this way.

THE RULE (integrations.service_tools.registry.sole_endpoint_function, read by
both): a tool with exactly one endpoint is named by its tool name; a tool
with several is not, and a named tool whose function the group does not
serve requires nothing.  Neither of those two is silent.

    python -m pytest tests/unit/test_a_step_naming_a_service_names_its_one_function.py -q --noconftest
"""
import logging

import pytest

import hartos.lifecycle_hooks as lh
import integrations.service_tools.registry as reg_mod
from core.agent_tools import attach_for_names
from integrations.service_tools.crawl4ai_tool import Crawl4AITool
from integrations.service_tools.registry import ServiceToolInfo, ServiceToolRegistry

LIVE_STEP = ("crawl4ai: fetch and read the top result pages from the search "
             "to gather information")


class _Agent:
    """An agent as action_named_tools reads it: the functions it serves."""

    def __init__(self, *served):
        self._function_map = {name: (lambda **kw: 'ran') for name in served}
        self.llm_config = {'tools': []}


class _AttachAgent:
    def __init__(self):
        self.llm_registered = []

    def register_for_llm(self, name=None, description=None):
        def _wrap(func):
            self.llm_registered.append(name)
            return func
        return _wrap

    def register_for_execution(self, name=None):
        return lambda func: func


def _two_endpoint_tool():
    return ServiceToolInfo(
        name='pocket_tts', description='speech', base_url='native://x',
        endpoints={
            'speak': {'path': '/speak', 'method': 'POST', 'description': 'speak',
                      'params_schema': {}},
            'voices': {'path': '/voices', 'method': 'GET', 'description': 'list',
                       'params_schema': {}},
        })


def _registry(*infos):
    reg = ServiceToolRegistry.__new__(ServiceToolRegistry)
    reg._tools = {i.name: i for i in infos}
    reg.create_endpoint_function = lambda t, e: (lambda **kw: f'{t}.{e} ran')
    return reg


@pytest.fixture
def registry(monkeypatch):
    reg = _registry(Crawl4AITool.create_tool_info(), _two_endpoint_tool())
    monkeypatch.setattr(reg_mod, 'service_tool_registry', reg)
    return reg


def test_the_live_step_names_the_crawler_it_was_given(registry):
    _names, referenced = lh.action_named_tools(
        [_Agent('google_search', 'crawl4ai_crawl')], LIVE_STEP)
    assert 'crawl4ai_crawl' in referenced, referenced


def test_the_function_name_still_names_it_once(registry):
    _names, referenced = lh.action_named_tools(
        [_Agent('crawl4ai_crawl')], 'crawl4ai_crawl the first result page')
    assert referenced.count('crawl4ai_crawl') == 1, referenced


def test_a_step_naming_no_service_tool_names_none(registry):
    _names, referenced = lh.action_named_tools(
        [_Agent('crawl4ai_crawl')], 'write a short summary for the person')
    assert referenced == [], referenced


def test_a_tool_with_several_endpoints_names_none_and_says_so(registry, caplog):
    with caplog.at_level(logging.WARNING, logger=lh.logger.name):
        _names, referenced = lh.action_named_tools(
            [_Agent('pocket_tts_speak', 'pocket_tts_voices')],
            'pocket_tts: read the story aloud')
    assert referenced == [], referenced
    assert any("'pocket_tts'" in r.getMessage() and '2 endpoints' in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


def test_a_named_tool_not_served_here_requires_nothing_and_says_so(registry, caplog):
    with caplog.at_level(logging.WARNING, logger=lh.logger.name):
        _names, referenced = lh.action_named_tools([_Agent('google_search')],
                                                   LIVE_STEP)
    assert 'crawl4ai_crawl' not in referenced, referenced
    assert any("'crawl4ai_crawl' is not served here" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


def test_the_attach_and_the_check_read_one_rule(registry):
    """What the attach hands a step for a tool name is what the check counts."""
    from integrations.service_tools.registry import sole_endpoint_function
    for tool_name, tool in registry._tools.items():
        helper = _AttachAgent()
        attach_for_names([tool_name], helper, _AttachAgent(), registry, set())
        served = [fn for fn in (sole_endpoint_function(tool_name, tool),) if fn]
        _names, referenced = lh.action_named_tools(
            [_Agent(*served)], f'{tool_name}: do the step')
        assert sorted(helper.llm_registered) == sorted(referenced), (
            tool_name, helper.llm_registered, referenced)


def test_sole_endpoint_function_follows_the_flat_name_rule():
    from integrations.service_tools.registry import sole_endpoint_function
    assert sole_endpoint_function(
        'crawl4ai', Crawl4AITool.create_tool_info()) == 'crawl4ai_crawl'
    same = ServiceToolInfo(name='ocr', description='d', base_url='native://x',
                           endpoints={'ocr': {'path': '/', 'method': 'POST'}})
    assert sole_endpoint_function('ocr', same) == 'ocr'
    assert sole_endpoint_function('pocket_tts', _two_endpoint_tool()) is None
    assert sole_endpoint_function('none', object()) is None
