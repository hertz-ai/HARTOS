"""A CREATE step post attaches only what its seats do not hold yet.

WHAT 5ad7c05f8 FIXED (live, research agent, 18 Nov 2026): "Execute Action 2:
crawl4ai: ..." ran with no crawl4ai to call.  CREATE now attaches the tools a
step's own `<tool>: <argument>` text names before it posts the step
(create_recipe._attach_named_for_create_step), and fits both seats' schemas
to the live n_ctx with those tools protected.

WHAT IT COST (rv_66reply, the review that rejected it):
  * S3: a step naming google_search, a tool CREATE's seats always carry,
    re-registered it on the post and logged "-> 1 tools";
  * S4: every post of a step naming any tool, even one that does not exist,
    ran two schema fits, each a /props probe of the local model server (one
    scripted step was posted 12 times);
  * a step naming an unknown tool replaced the seats' protected set, so the
    tool an earlier step was given could be the first one deferred.

THE RULE
  * a named tool both seats already hold is not attached again;
  * a post that attaches nothing fits nothing (no /props probe); the seats
    still protect that step's own tools for its turn, as REUSE records them
    on every post (rv_3local's review of af6a7b2ad), unless the step names
    only tools no seat holds (an unknown name then leaves the set as it was);
  * a post that does attach still gets its fit, its tools protected (the
    5ad7c05f8 fix stays).

Real code: the function lifted from create_recipe.py (importing that module
hangs a bare env), core.agent_tool_menu's attach_for_names and
fit_schema_to_ctx, the real name matcher and the crawl4ai tool's registry
entry.  Only the /props read is counted instead of sent.

    python -m pytest tests/unit/test_a_create_step_post_attaches_only_what_is_new.py -q --noconftest
"""
import ast
import logging
import os
import re
import types

import pytest

import hartos.helper as helper_fun
from tests.unit.module_swap import swap_modules

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
CREATE = os.path.join(ROOT, 'hartos', 'create_recipe.py')
REUSE = os.path.join(ROOT, 'hartos', 'reuse_recipe.py')
CRAWL_STEP = ("crawl4ai: Fetch the top result pages using markdown extraction "
              "to read the content.")
SEARCH_STEP = 'google_search: Collect the three sources on local AI costs'
MAIN = [('google_search', 'search the web'),
        ('send_message_to_user', 'send the person a message'),
        ('get_data_by_key', 'read a saved value'),
        ('save_data_in_memory', 'save a value')]


def _lift(path, names, ns):
    tree = ast.parse(open(path, encoding='utf-8').read())
    nodes = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == set(names), f'{names} missing from {path}'
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, 'exec'), ns)
    return ns


class _Agent:
    """A seat as attach_for_names and fit_schema_to_ctx read it: a dict
    llm_config with a tools block, register_for_llm adding a schema entry,
    register_for_execution adding to _function_map.  Every registration is
    recorded."""

    def __init__(self, name, tools=()):
        self.name = name
        self.llm_config = {'tools': [self._entry(n, d) for n, d in tools]}
        self._function_map = {n: (lambda **kw: None) for n, _d in tools}
        self.registered = []

    @staticmethod
    def _entry(name, desc):
        return {'type': 'function', 'function': {
            'name': name, 'description': desc,
            'parameters': {'type': 'object', 'properties': {
                'query': {'type': 'string', 'description': 'what to ' + desc}},
                'required': ['query']}}}

    def register_for_llm(self, name=None, description=None):
        def _wrap(func):
            self.registered.append(('llm', name))
            self.llm_config['tools'].append(self._entry(name, description or name))
            return func
        return _wrap

    def register_for_execution(self, name=None):
        def _wrap(func):
            self.registered.append(('exec', name))
            self._function_map[name] = func
            return func
        return _wrap

    def names(self):
        return [e['function']['name'] for e in self.llm_config['tools']]


def _pair(core_extra=()):
    """CREATE's main leg: the Helper and the Assistant both carry the core
    set (register_core_tools with executor_proposes), the Helper also
    request_tools; the attach ledger holds service tools only."""
    helper = _Agent('Helper', MAIN + [('request_tools', 'discover tools')])
    assistant = _Agent('Assistant', MAIN)
    assistant._hart_attached_tools = set()
    assistant._hart_core_tools = [(n, d, (lambda **kw: None))
                                  for n, d in MAIN + list(core_extra)]
    return helper, assistant


@pytest.fixture
def probes(monkeypatch):
    import core.llm_outbound_logger as lol
    monkeypatch.delenv('HEVOLVE_LLAMA_CTX_SIZE', raising=False)
    seen = []
    monkeypatch.setattr(lol, '_live_ctx_geometry',
                        lambda: seen.append(1) or (65536, 1))
    return seen


@pytest.fixture
def attach():
    from integrations.service_tools.crawl4ai_tool import Crawl4AITool
    from integrations.service_tools.registry import ServiceToolRegistry
    real = _lift(REUSE, {'_tool_name_candidates'},
                 {'re': re, '_TOOL_IDENT_RE': helper_fun.TOOL_IDENT_RE})
    reuse = types.ModuleType('hartos.reuse_recipe')
    reuse._tool_name_candidates = real['_tool_name_candidates']
    reg = ServiceToolRegistry.__new__(ServiceToolRegistry)
    reg._tools = {'crawl4ai': Crawl4AITool.create_tool_info()}
    reg.create_endpoint_function = lambda t, e: (lambda **kw: f'{t}.{e} ran')
    svc = types.ModuleType('integrations.service_tools')
    svc.service_tool_registry = reg
    ns = _lift(CREATE, {'_attach_named_for_create_step'},
               {'current_app': types.SimpleNamespace(
                   logger=logging.getLogger('create_step_attach_test'))})
    fn = ns['_attach_named_for_create_step']

    def _post(helper, assistant, step):
        with swap_modules({'hartos.reuse_recipe': reuse,
                           'integrations.service_tools': svc}):
            return fn({'helper': helper, 'assistant': assistant}, step,
                      'u_1795008854159')
    return _post


def test_the_named_service_tool_is_attached_and_protected(attach, probes):
    helper, assistant = _pair()
    assert attach(helper, assistant, CRAWL_STEP) == 1
    assert 'crawl4ai_crawl' in helper.names()
    assert 'crawl4ai_crawl' in assistant.names()
    assert 'crawl4ai_crawl' in assistant._function_map
    for seat in (helper, assistant):
        assert {'crawl4ai_crawl', 'send_message_to_user'} <= seat._hart_turn_protect
    assert len(probes) == 2, probes          # one fit per seat


def test_a_step_naming_an_always_on_tool_registers_and_fits_nothing(attach, probes):
    helper, assistant = _pair()
    assert attach(helper, assistant, SEARCH_STEP) == 0
    assert helper.registered == [] and assistant.registered == []
    assert helper.names().count('google_search') == 1
    assert probes == []


def test_the_same_step_posted_again_attaches_and_fits_nothing(attach, probes):
    helper, assistant = _pair()
    attach(helper, assistant, CRAWL_STEP)
    regs = (list(helper.registered), list(assistant.registered))
    probes.clear()
    for _ in range(3):
        assert attach(helper, assistant, CRAWL_STEP) == 0
    assert (helper.registered, assistant.registered) == regs
    assert probes == []


def test_a_step_naming_an_unknown_tool_keeps_the_earlier_steps_protection(attach, probes):
    helper, assistant = _pair()
    attach(helper, assistant, CRAWL_STEP)
    kept = (set(helper._hart_turn_protect), set(assistant._hart_turn_protect))
    probes.clear()
    assert attach(helper, assistant, 'frobnicate_tool: do the thing') == 0
    assert probes == []
    assert (helper._hart_turn_protect, assistant._hart_turn_protect) == kept


def test_a_step_naming_an_always_on_tool_protects_it_with_no_probe(attach, probes):
    """Nothing to attach, so no fit and no probe; the seats protect this
    step's own tool for its turn, as REUSE records each step's on every post."""
    helper, assistant = _pair()
    attach(helper, assistant, CRAWL_STEP)
    probes.clear()
    assert attach(helper, assistant, SEARCH_STEP) == 0
    for seat in (helper, assistant):
        assert seat._hart_turn_protect == {'google_search', 'send_message_to_user'}
    assert probes == []


CODE_STEP = 'execute_coding_task: summarise the pages into the report'


def test_a_step_named_again_protects_its_own_tool_not_the_last_steps(attach, probes):
    """rv_3local's review of af6a7b2ad: crawl4ai, then execute_coding_task,
    then crawl4ai again.  The third post attaches nothing new, yet its own
    crawl4ai_crawl is what a later fit (request_tools', which passes no turn
    protect) must keep when the room holds every tool but one; the coding
    tool is the one to go.  af6a7b2ad returned before recording the third
    step's tools and the fit dropped crawl4ai_crawl from both seats (the
    ee79a6fcb failure)."""
    from core.agent_tool_menu import fit_schema_to_ctx
    from core.llm_outbound_logger import _schema_tokens
    helper, assistant = _pair(core_extra=[('execute_coding_task', 'run code')])
    attach(helper, assistant, CRAWL_STEP)
    attach(helper, assistant, CODE_STEP)
    probes.clear()
    assert attach(helper, assistant, CRAWL_STEP) == 0
    assert probes == []
    for seat in (helper, assistant):
        assert 'crawl4ai_crawl' in seat._hart_turn_protect, seat._hart_turn_protect
        total = _schema_tokens({'tools': seat.llm_config['tools']})
        deferred = fit_schema_to_ctx(seat, protect={'delegate_to_specialist'},
                                     room=total - 5)
        assert 'crawl4ai_crawl' in seat.names(), (seat.name, deferred)
        assert 'execute_coding_task' in deferred, (seat.name, deferred)


def test_a_core_tool_the_seats_lack_is_still_attached(attach, probes):
    helper, assistant = _pair(core_extra=[('execute_coding_task', 'run code')])
    assert attach(helper, assistant, 'execute_coding_task: read the error log') == 1
    assert 'execute_coding_task' in helper.names()
    assert 'execute_coding_task' in assistant._function_map
    assert len(probes) == 2


def test_a_tool_the_helper_offers_but_the_assistant_cannot_run_is_attached(attach, probes):
    helper, assistant = _pair()
    del assistant._function_map['google_search']
    assert attach(helper, assistant, SEARCH_STEP) == 1
    assert 'google_search' in assistant._function_map
