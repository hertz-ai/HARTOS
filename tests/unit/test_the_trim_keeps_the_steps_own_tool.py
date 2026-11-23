"""The per-step schema fit never defers the tool the step was just given.

rv_942a's review of HARTOS 942adc571 (19 Nov 2026), from a probe on real code
(D:/claude_scratch/3a9b530c/rv_942a/probes/fit_protect_probe.py): after
attach_for_names hands a step "crawl4ai: fetch ..." the function crawl4ai_crawl,
REUSE (_attach_named_tools_for_action._fit) and CREATE
(_attach_named_for_create_step) fitted the schema protecting the step's WORDS
("crawl4ai"), so under a tight context crawl4ai_crawl was the first function
deferred, while the completion check still demanded its receipt.  Design agreed
with hartos-43: protect named_function_names(names) and every function the
completion check demands of the action (action_named_tools), neither alone
(rv_942a's population count over 7,381 saved actions: the gate holds 80 the
attach never offers, the attach offers 1 the gate does not hold).

    python -m pytest tests/unit/test_the_trim_keeps_the_steps_own_tool.py -q --noconftest
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import core.agent_tool_menu as menu  # noqa: E402
import core.agent_tools as agent_tools  # noqa: E402
import integrations.service_tools as svc_pkg  # noqa: E402
import integrations.service_tools.registry as reg_mod  # noqa: E402
from hartos import reuse_recipe  # noqa: E402
from hartos.lifecycle_hooks import SEND_TOOL  # noqa: E402
from integrations.service_tools.registry import (  # noqa: E402
    ServiceToolInfo, ServiceToolRegistry)

UP = 'trim_keeps_tool_user_1'


def _info(name, *endpoints):
    return ServiceToolInfo(
        name=name, description=name, base_url='http://127.0.0.1:1',
        endpoints={ep: {'path': '/' + ep, 'method': 'POST', 'description': ep,
                        'params_schema': {}} for ep in endpoints})


def _registry(*infos):
    reg = ServiceToolRegistry.__new__(ServiceToolRegistry)
    reg._tools = {i.name: i for i in infos}
    reg.create_endpoint_function = lambda t, e: (lambda **kw: f'{t}.{e} ran')
    return reg


class _Seat:
    """An autogen seat as the attach and the gate read it."""
    def __init__(self, name, served=()):
        self.name = name
        self._function_map = {n: (lambda **kw: 'ran') for n in served}
        self.llm_config = {'tools': [{'type': 'function', 'function': {'name': n}}
                                     for n in served]}
        self._hart_attached_tools = set()

    def register_for_llm(self, name=None, description=None):
        def _wrap(func):
            self.llm_config['tools'].append({'type': 'function', 'function': {'name': name}})
            return func
        return _wrap

    def register_for_execution(self, name=None):
        def _wrap(func):
            self._function_map[name] = func
            return func
        return _wrap


class _Task:
    def __init__(self, text):
        self._text = text
        self.current_action = 1

    def get_action(self, idx):
        return self._text


# ---- the one selection ---------------------------------------------------

def test_a_one_endpoint_tool_name_stands_for_its_function():
    reg = _registry(_info('crawl4ai', 'crawl'))
    assert menu.named_function_names(['crawl4ai'], reg) == {'crawl4ai', 'crawl4ai_crawl'}


def test_a_several_endpoint_tool_name_stands_for_none_of_them():
    reg = _registry(_info('wan2gp', 'generate', 'status'))
    assert menu.named_function_names(['wan2gp'], reg) == {'wan2gp'}


def test_names_the_registry_does_not_hold_are_kept_as_written():
    reg = _registry(_info('crawl4ai', 'crawl'))
    assert menu.named_function_names(['google_search', ''], reg) == {'google_search'}


def test_an_unreadable_registry_keeps_the_names_and_says_so(monkeypatch):
    said = []
    monkeypatch.setattr(menu.tool_logger, 'warning', lambda m, *a, **k: said.append(str(m)))

    class _Broken:
        @property
        def _tools(self):
            raise RuntimeError('registry lock timed out')

    assert menu.named_function_names(['crawl4ai'], _Broken()) == {'crawl4ai'}
    assert [m for m in said if 'unreadable' in m], said


def test_the_attach_selects_exactly_that_set():
    """attach_for_names attaches what named_function_names names, no more."""
    reg = _registry(_info('crawl4ai', 'crawl'), _info('wan2gp', 'generate', 'status'))
    helper, executor = _Seat('helper'), _Seat('assistant')
    menu.attach_for_names(['crawl4ai', 'wan2gp'], helper, executor, reg, set())
    names = {t['function']['name'] for t in helper.llm_config['tools']}
    assert names == {'crawl4ai_crawl'}, names


# ---- the real trim keeps what is protected --------------------------------

def _entry(name):
    return {'type': 'function', 'function': {
        'name': name, 'description': f'{name} does one thing for the agent.',
        'parameters': {'type': 'object', 'properties': {
            'query': {'type': 'string', 'description': 'what to do'}}}}}


def test_the_real_trim_keeps_the_attached_function_when_it_is_protected():
    """rv_942a's probe: a schema a little over the room. Protecting the words
    drops crawl4ai_crawl; protecting named_function_names keeps it."""
    from core.llm_outbound_logger import _schema_tokens
    core = sorted(menu.MAIN_LEG_CORE_TOOLS)[:10]
    names = ['request_tools'] + core + [f'family_tool_{i}' for i in range(12)] + ['crawl4ai_crawl']
    room = _schema_tokens({'tools': [_entry(n) for n in names]}) - 150
    reg = _registry(_info('crawl4ai', 'crawl'))

    class _A:
        def __init__(self):
            self.llm_config = {'tools': [_entry(n) for n in names]}

    words = _A()
    menu.fit_schema_to_ctx(words, protect={'crawl4ai', SEND_TOOL}, room=room, turn_protect=True)
    assert 'crawl4ai_crawl' not in {t['function']['name'] for t in words.llm_config['tools']}
    kept = _A()
    menu.fit_schema_to_ctx(kept, protect=menu.named_function_names(['crawl4ai'], reg) | {SEND_TOOL},
                           room=room, turn_protect=True)
    assert 'crawl4ai_crawl' in {t['function']['name'] for t in kept.llm_config['tools']}


# ---- REUSE: the protect set _fit hands the fit ----------------------------

@pytest.fixture
def reuse(monkeypatch):
    reg = _registry(_info('crawl4ai', 'crawl'))
    monkeypatch.setattr(reg_mod, 'service_tool_registry', reg)
    monkeypatch.setattr(svc_pkg, 'service_tool_registry', reg)
    seen = []
    monkeypatch.setattr(agent_tools, 'fit_schema_to_ctx',
                        lambda agent, protect=(), **kw: seen.append(set(protect)) or set())
    saved = {k: getattr(reuse_recipe, k).get(UP) for k in ('user_agents', 'user_tasks')}
    yield seen
    for k, v in saved.items():
        store = getattr(reuse_recipe, k)
        if v is None:
            store.pop(UP, None)
        else:
            store[UP] = v


def _reuse_session(text, served=()):
    assistant = _Seat('Assistant', served)
    helper = _Seat('Helper', served)
    reuse_recipe.user_agents[UP] = [assistant, None, None, None, helper]
    reuse_recipe.user_tasks[UP] = _Task(text)


def test_reuse_protects_the_function_the_attach_registered(reuse, monkeypatch):
    monkeypatch.setattr(reuse_recipe, '_reuse_action_tool_names', lambda up, aid: ['crawl4ai'])
    _reuse_session('crawl4ai: fetch and read the top result pages')
    reuse_recipe._attach_named_tools_for_action(UP)
    assert reuse, 'the fit never ran'
    assert all({'crawl4ai', 'crawl4ai_crawl', SEND_TOOL} <= p for p in reuse), reuse


def test_reuse_protects_what_the_completion_check_demands(reuse, monkeypatch):
    """"Use crawl4ai to fetch ..." yields no candidate for the attach, yet the
    gate demands crawl4ai_crawl once it is served (here by an earlier attach)."""
    monkeypatch.setattr(reuse_recipe, '_reuse_action_tool_names', lambda up, aid: [])
    _reuse_session('Use crawl4ai to fetch the top result pages', served=['crawl4ai_crawl'])
    reuse_recipe._attach_named_tools_for_action(UP)
    assert reuse and all('crawl4ai_crawl' in p for p in reuse), reuse


# ---- CREATE: the same set --------------------------------------------------

def test_create_protects_the_same_set(monkeypatch):
    from flask import Flask
    from hartos import create_recipe
    reg = _registry(_info('crawl4ai', 'crawl'))
    monkeypatch.setattr(svc_pkg, 'service_tool_registry', reg)
    seen = []
    monkeypatch.setattr(menu, 'fit_schema_to_ctx',
                        lambda agent, protect=(), **kw: seen.append(set(protect)) or set())
    assistant, helper = _Seat('Assistant'), _Seat('Helper')
    with Flask('trim_test').app_context():
        create_recipe._attach_named_for_create_step(
            {'assistant': assistant, 'helper': helper},
            'crawl4ai: fetch and read the top result pages', UP)
    assert seen and all({'crawl4ai', 'crawl4ai_crawl', SEND_TOOL} <= p for p in seen), seen
