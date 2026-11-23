"""A step-naming warning names the session and action, and says what to write.

rv_942a's review of HARTOS 942adc571 (19 Nov 2026), should-fixes S3 and S4:
  * S3: the [FAB-GUARD] warnings named no session and no action, and one
    server.log holds every agent's lines, so a line could not be traced; the
    several-endpoint warning did not list the tool's functions, so the step's
    author did not learn what to write; the registry-unreadable warning was
    said once per gate call (5 calls, 5 lines).
  * S4: the skip that stops a false warning when a step names one of a
    several-endpoint tool's functions outright ("wan2gp_generate" contains
    "wan2gp") also hid a TRUE warning: "wan2gp: make the clip, then
    wan2gp_status" names the tool on its own for the clip, and that part is
    not held.  The skip now holds only when every mention of the tool sits
    inside a function name the step already requires.

    python -m pytest tests/unit/test_a_service_tool_warning_names_its_step.py -q --noconftest
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import hartos.lifecycle_hooks as lh  # noqa: E402
import integrations.service_tools.registry as reg_mod  # noqa: E402
from hartos.threadlocal import thread_local_data  # noqa: E402
from integrations.service_tools.registry import (  # noqa: E402
    ServiceToolInfo, ServiceToolRegistry)


class _Agent:
    def __init__(self, name, tools):
        self.name = name
        self._function_map = {t: (lambda **kw: None) for t in tools}
        self.llm_config = {'tools': [{'function': {'name': t}} for t in tools]}
        self._oai_messages = {}


def _info(name, *endpoints):
    return ServiceToolInfo(
        name=name, description=name, base_url='http://127.0.0.1:1',
        endpoints={ep: {'path': '/' + ep, 'method': 'POST', 'description': ep,
                        'params_schema': {}} for ep in endpoints})


def _registry(*infos):
    reg = ServiceToolRegistry.__new__(ServiceToolRegistry)
    reg._tools = {i.name: i for i in infos}
    return reg


class _Broken:
    @property
    def _tools(self):
        raise RuntimeError('registry lock timed out')


@pytest.fixture
def said(monkeypatch):
    out = []
    monkeypatch.setattr(lh.logger, 'warning', lambda m, *a, **k: out.append(str(m)))
    monkeypatch.setattr(lh, '_REGISTRY_UNREADABLE_SAID', [False])
    return out


@pytest.fixture
def wan2gp(monkeypatch):
    monkeypatch.setattr(reg_mod, 'service_tool_registry',
                        _registry(_info('wan2gp', 'generate', 'status')))
    return [_Agent('Helper', ['wan2gp_generate', 'wan2gp_status'])]


def _about(said, tool):
    return [w for w in said if repr(tool) in w]


# ---- S3: which session and action ----------------------------------------

def test_the_warning_names_the_session_and_action_it_was_given(said, wan2gp):
    lh.action_named_tools(wan2gp, 'wan2gp: make the clip',
                          where='session 10077_55 action 3')
    lines = _about(said, 'wan2gp')
    assert lines and all('session 10077_55 action 3' in w for w in lines), said


def test_the_completion_gates_reading_names_its_session_and_action(
        monkeypatch, said, wan2gp):
    class _Ledger:
        tasks = {'action_2': type('T', (), {'description': 'wan2gp: make the clip'})()}

    monkeypatch.setattr(lh, 'get_registered_ledger', lambda up: _Ledger())
    monkeypatch.setattr(lh, 'get_registered_groupchat',
                        lambda up: type('G', (), {'agents': wan2gp})())
    lh._tools_this_action_names('10077_55', 2)
    lines = _about(said, 'wan2gp')
    assert lines and all('session 10077_55 action 2' in w for w in lines), said


def test_without_a_session_the_warning_names_the_turn(said, wan2gp):
    thread_local_data.set_request_id('req-7731')
    thread_local_data.set_user_id('user-42')
    lh.action_named_tools(wan2gp, 'wan2gp: make the clip')
    lines = _about(said, 'wan2gp')
    assert lines and all("'req-7731'" in w and "'user-42'" in w for w in lines), said


def test_an_unserved_function_warning_names_its_session_too(monkeypatch, said):
    monkeypatch.setattr(reg_mod, 'service_tool_registry',
                        _registry(_info('crawl4ai', 'crawl')))
    lh.action_named_tools([_Agent('Helper', ['google_search'])],
                          'crawl4ai: fetch the first result',
                          where='session 10077_55 action 4')
    lines = _about(said, 'crawl4ai')
    assert lines and all('session 10077_55 action 4' in w for w in lines), said


# ---- S3: what to write ---------------------------------------------------

def test_the_several_endpoint_warning_lists_the_functions(said, wan2gp):
    lh.action_named_tools(wan2gp, 'wan2gp: make the clip', where='s a')
    lines = _about(said, 'wan2gp')
    assert lines and 'wan2gp_generate' in lines[0] and 'wan2gp_status' in lines[0], said


# ---- S3: the registry warning once per failure ---------------------------

def test_an_unreadable_registry_is_said_once_per_failure(monkeypatch, said):
    agents = [_Agent('Helper', ['crawl4ai_crawl'])]
    monkeypatch.setattr(reg_mod, 'service_tool_registry', _Broken())
    for _ in range(5):
        lh.action_named_tools(agents, 'crawl4ai: fetch the page')
    assert len([w for w in said if 'unreadable' in w]) == 1, said
    monkeypatch.setattr(reg_mod, 'service_tool_registry',
                        _registry(_info('crawl4ai', 'crawl')))
    lh.action_named_tools(agents, 'crawl4ai: fetch the page')
    monkeypatch.setattr(reg_mod, 'service_tool_registry', _Broken())
    lh.action_named_tools(agents, 'crawl4ai: fetch the page')
    assert len([w for w in said if 'unreadable' in w]) == 2, said


# ---- S4: the skip holds only when every mention is inside a function -----

def test_a_tool_named_on_its_own_beside_one_of_its_functions_is_said(said, wan2gp):
    _n, referenced = lh.action_named_tools(
        wan2gp, 'wan2gp: make the clip, then wan2gp_status', where='s b')
    assert referenced == ['wan2gp_status'], referenced
    assert _about(said, 'wan2gp'), said


def test_a_tool_named_only_inside_its_functions_is_not_said(said, wan2gp):
    _n, referenced = lh.action_named_tools(
        wan2gp, 'run wan2gp_generate, then check wan2gp_status', where='s c')
    assert sorted(referenced) == ['wan2gp_generate', 'wan2gp_status'], referenced
    assert not _about(said, 'wan2gp'), said
