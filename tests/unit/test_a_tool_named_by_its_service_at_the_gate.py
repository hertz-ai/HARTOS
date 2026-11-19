"""A step that names a service by its tool name, at the real REUSE gate.

rv_b06b's review of HARTOS b06b29405 (18 Nov 2026) asked for:
  * a gate-level test of the live shape: research agent 1795014914479's
    action 2, "crawl4ai: fetch and read the top result pages ...", whose
    crawl ran and whose step GAVE_UP before b06b29405;
  * the plain-word case (its mutant m7, a bare substring for the tool name,
    survived);
  * no false warning when the text names one of a multi-endpoint tool's
    functions outright ("wan2gp_generate" contains "wan2gp");
  * no silent except when the registry cannot be read;
  * the flat-name rule pinned to the name create_endpoint_function registers
    (its mutant m11, a drifted copy in sole_endpoint_function, survived).

    python -m pytest tests/unit/test_a_tool_named_by_its_service_at_the_gate.py -q --noconftest
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import hartos.lifecycle_hooks as lh  # noqa: E402
import integrations.service_tools.registry as reg_mod  # noqa: E402
from hartos import reuse_recipe  # noqa: E402
from integrations.service_tools.registry import (  # noqa: E402
    ServiceToolInfo, ServiceToolRegistry, sole_endpoint_function)

_UP = 'gate_service_user_1'
_STEP = ("crawl4ai: fetch and read the top result pages from the search to "
         "gather information")
_DISPATCH = {'role': 'user', 'name': 'ChatInstructor',
             'content': f'Perform this action -> Action #2: {_STEP}'}
_CRAWL_CALL = {'role': 'assistant', 'name': 'Helper', 'content': None,
               'tool_calls': [{'id': 'call_crawl_1', 'type': 'function',
                               'function': {'name': 'crawl4ai_crawl',
                                            'arguments': '{"url": "https://example.org"}'}}]}
_FETCHED = '--- Progress ---\nFetched 1181 words: replying when someone says go ahead'
_CRAWL_RESULT = {'role': 'tool', 'name': 'Assistant', 'content': _FETCHED,
                 'tool_responses': [{'tool_call_id': 'call_crawl_1', 'role': 'tool',
                                     'content': _FETCHED}]}
_VERDICT = {'role': 'user', 'name': 'StatusVerifier',
            'content': '{"status": "completed", "action_id": 2}'}


class _Task:
    def __init__(self, text):
        self._text = text
        self.evidence_seen_call_ids = set()

    def get_action(self, idx):
        return self._text


class _Agent:
    def __init__(self, name, tools):
        self.name = name
        self._function_map = {t: (lambda **kw: None) for t in tools}
        self.llm_config = {'tools': [{'function': {'name': t}} for t in tools]}
        self._oai_messages = {}


class _Chat:
    def __init__(self, messages, agents):
        self.messages = messages
        self.agents = agents


def _info(name, *endpoints):
    return ServiceToolInfo(
        name=name, description=name, base_url='http://127.0.0.1:1',
        endpoints={ep: {'path': '/' + ep, 'method': 'POST', 'description': ep,
                        'params_schema': {}} for ep in endpoints})


def _registry(*infos):
    reg = ServiceToolRegistry.__new__(ServiceToolRegistry)
    reg._tools = {i.name: i for i in infos}
    return reg


@pytest.fixture
def warnings_said(monkeypatch):
    said = []
    monkeypatch.setattr(lh.logger, 'warning',
                        lambda msg, *a, **k: said.append(str(msg)))
    return said


@pytest.fixture
def gate(monkeypatch):
    # The live registry's crawl4ai: one endpoint, 'crawl'.
    monkeypatch.setattr(reg_mod, 'service_tool_registry',
                        _registry(_info('crawl4ai', 'crawl')))
    saved = reuse_recipe.user_tasks.get(_UP)
    reuse_recipe.user_tasks[_UP] = _Task(_STEP)
    yield
    if saved is None:
        reuse_recipe.user_tasks.pop(_UP, None)
    else:
        reuse_recipe.user_tasks[_UP] = saved


def _chat(*messages):
    return _Chat(list(messages), [_Agent('Helper', ['crawl4ai_crawl', 'google_search']),
                                  _Agent('Assistant', [])])


def test_the_live_step_completes_on_its_crawl_receipt(gate):
    gc = _chat(_DISPATCH, _CRAWL_CALL, _CRAWL_RESULT, _VERDICT)
    assert reuse_recipe._reuse_fabricated_tools(_UP, 2, gc, gc.agents) == []
    assert reuse_recipe._reuse_completion_evidence(_UP, 2, gc) == {
        'message_index': 2, 'kind': 'tool_receipt'}


def test_the_live_step_without_a_crawl_is_held_to_it(gate):
    gc = _chat(_DISPATCH, _VERDICT)
    assert reuse_recipe._reuse_fabricated_tools(_UP, 2, gc, gc.agents) == [
        'crawl4ai_crawl']


def test_a_plain_word_tool_name_needs_a_call_to_name_it(monkeypatch):
    """'recall' is an English word: prose that uses it names no tool; a call
    written out does (the rule _text_names_tool keeps for plain words)."""
    monkeypatch.setattr(reg_mod, 'service_tool_registry',
                        _registry(_info('recall', 'search')))
    agents = [_Agent('Helper', ['recall_search'])]
    _n, prose = lh.action_named_tools(agents, 'recall the names of the family')
    _n, call = lh.action_named_tools(agents, 'call recall to look the name up')
    assert 'recall_search' not in prose, prose
    assert 'recall_search' in call, call


def test_naming_one_of_several_functions_raises_no_false_warning(
        monkeypatch, warnings_said):
    monkeypatch.setattr(reg_mod, 'service_tool_registry',
                        _registry(_info('wan2gp', 'generate', 'status')))
    agents = [_Agent('Helper', ['wan2gp_generate', 'wan2gp_status'])]
    _n, referenced = lh.action_named_tools(
        agents, 'wan2gp_generate: make the five-second clip')
    assert referenced == ['wan2gp_generate'], referenced
    assert not [w for w in warnings_said if "'wan2gp'" in w], warnings_said


def test_naming_a_several_function_tool_alone_is_still_said(
        monkeypatch, warnings_said):
    monkeypatch.setattr(reg_mod, 'service_tool_registry',
                        _registry(_info('wan2gp', 'generate', 'status')))
    agents = [_Agent('Helper', ['wan2gp_generate', 'wan2gp_status'])]
    _n, referenced = lh.action_named_tools(agents, 'wan2gp: make the clip')
    assert referenced == [], referenced
    assert [w for w in warnings_said if "'wan2gp'" in w], warnings_said


def test_an_unreadable_registry_is_said(monkeypatch, warnings_said):
    class _Broken:
        @property
        def _tools(self):
            raise RuntimeError('registry lock timed out')

    monkeypatch.setattr(reg_mod, 'service_tool_registry', _Broken())
    agents = [_Agent('Helper', ['crawl4ai_crawl'])]
    _n, referenced = lh.action_named_tools(agents, _STEP)
    assert referenced == [], referenced
    assert [w for w in warnings_said if 'registry' in w and 'unreadable' in w], \
        warnings_said


class _SeatAgent:
    """An autogen seat as attach_for_names registers onto it."""
    def __init__(self, name):
        self.name = name
        self.llm_registered = []
        self.exec_registered = []

    def register_for_llm(self, name=None, description=None):
        def _wrap(func):
            self.llm_registered.append(name)
            return func
        return _wrap

    def register_for_execution(self, name=None):
        def _wrap(func):
            self.exec_registered.append(name)
            return func
        return _wrap


def test_the_attach_offers_a_flat_tool_under_its_registered_name():
    """A tool whose one endpoint shares its name (seo_audit_score, gh_pr_open)
    is registered as that name; the attach must offer it under the same name,
    or the model calls a function nobody registered (R14's mutant M8, the
    attach's copy of the rule drifting, survived without this)."""
    from core.agent_tools import attach_for_names
    reg = _registry(_info('ocr', 'ocr'))
    reg.create_endpoint_function = lambda t, e: (lambda **kw: f'{t}.{e} ran')
    helper, executor = _SeatAgent('helper'), _SeatAgent('assistant')
    n = attach_for_names(['ocr'], helper, executor, reg, set())
    assert n == 1, n
    assert helper.llm_registered == ['ocr'], helper.llm_registered
    assert executor.exec_registered == ['ocr'], executor.exec_registered


@pytest.mark.parametrize('info', [_info('crawl4ai', 'crawl'), _info('ocr', 'ocr'),
                                  _info('wan2gp', 'generate', 'status')],
                         ids=['crawl4ai', 'flat', 'several'])
def test_the_name_checked_is_the_name_registered(info):
    reg = _registry(info)
    registered = {ep: reg.create_endpoint_function(info.name, ep).__name__
                  for ep in info.endpoints}
    defined = sorted(d['name'] for d in reg.get_tool_definitions())
    assert defined == sorted(registered.values()), (defined, registered)
    sole = sole_endpoint_function(info.name, info)
    if len(info.endpoints) == 1:
        assert sole == next(iter(registered.values())), (sole, registered)
    else:
        assert sole is None, sole
