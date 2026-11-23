"""A service-tool warning is not said falsely, and a new failure is not lost.

The review of HARTOS a4b5da5c2 (20 Nov 2026), leads (2) and (3), both
confirmed against a4b5da5c2 and its parent:
  * (3) a step that spells a function out with a space ("wan2gp generate the
    clip") is held to wan2gp_generate (_text_names_tool reads the spaced
    form), yet a4b5da5c2's skip took out only the underscore spelling, found
    the tool still named, and said the false several-endpoint warning again.
    Its parent said none.
  * (2) a second, different registry failure with no good read in between was
    never said: A, A, B gave one line, where the parent said each failure.

    python -m pytest tests/unit/test_a_service_tool_warning_is_neither_false_nor_lost.py -q --noconftest
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import hartos.lifecycle_hooks as lh  # noqa: E402
import integrations.service_tools.registry as reg_mod  # noqa: E402
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


def _broken(error):
    """A registry whose read fails with ``error``."""
    class _Broken:
        @property
        def _tools(self):
            raise error
    return _Broken()


@pytest.fixture
def said(monkeypatch):
    out = []
    monkeypatch.setattr(lh.logger, 'warning', lambda m, *a, **k: out.append(str(m)))
    # raising=False: a4b5da5c2's parent has no such state, and these tests
    # must run there too to show what it said.
    monkeypatch.setattr(lh, '_REGISTRY_UNREADABLE_SAID', [None], raising=False)
    return out


@pytest.fixture
def wan2gp(monkeypatch):
    monkeypatch.setattr(reg_mod, 'service_tool_registry',
                        _registry(_info('wan2gp', 'generate', 'status')))
    return [_Agent('Helper', ['wan2gp_generate', 'wan2gp_status'])]


def _about(said, tool):
    return [w for w in said if repr(tool) in w]


def _unreadable(said):
    return [w for w in said if 'unreadable' in w]


# ---- lead (3): a function spelled out with a space -------------------------

def test_a_function_spelled_with_a_space_raises_no_false_warning(said, wan2gp):
    _n, referenced = lh.action_named_tools(
        wan2gp, 'wan2gp generate the five-second clip')
    assert referenced == ['wan2gp_generate'], referenced
    assert not _about(said, 'wan2gp'), said


def test_both_spellings_of_its_functions_raise_no_false_warning(said, wan2gp):
    _n, referenced = lh.action_named_tools(
        wan2gp, 'wan2gp generate the clip, then call wan2gp_status')
    assert sorted(referenced) == ['wan2gp_generate', 'wan2gp_status'], referenced
    assert not _about(said, 'wan2gp'), said


def test_a_function_name_inside_a_longer_word_raises_no_false_warning(
        said, wan2gp):
    """_text_names_tool reads an identifier wherever it stands, so the step is
    held to wan2gp_generate; its mention is taken out the same way."""
    _n, referenced = lh.action_named_tools(
        wan2gp, 'wan2gp_generate_hd the clip')
    assert referenced == ['wan2gp_generate'], referenced
    assert not _about(said, 'wan2gp'), said


def test_a_tool_named_alone_beside_a_spelled_out_function_is_said(said, wan2gp):
    """a4b5da5c2's S4 warning stays: the clip is asked of 'wan2gp' alone."""
    _n, referenced = lh.action_named_tools(
        wan2gp, 'wan2gp: make the clip, then wan2gp status')
    assert referenced == ['wan2gp_status'], referenced
    lines = _about(said, 'wan2gp')
    assert lines and 'wan2gp_generate' in lines[0], said


def test_a_spelled_out_function_not_served_here_is_still_said(said, monkeypatch):
    """Only a function the step is held to is taken out of its words."""
    monkeypatch.setattr(reg_mod, 'service_tool_registry',
                        _registry(_info('wan2gp', 'generate', 'status')))
    _n, referenced = lh.action_named_tools(
        [_Agent('Helper', ['wan2gp_status'])], 'wan2gp generate the clip')
    assert referenced == [], referenced
    assert _about(said, 'wan2gp'), said


# ---- lead (2): each different registry failure is said ---------------------

_A = RuntimeError('registry lock timed out')
_B = OSError('service_tools.json could not be read')


def test_a_different_registry_failure_is_said(monkeypatch, said):
    agents = [_Agent('Helper', ['crawl4ai_crawl'])]
    monkeypatch.setattr(reg_mod, 'service_tool_registry', _broken(_A))
    lh.action_named_tools(agents, 'crawl4ai: fetch the page')
    monkeypatch.setattr(reg_mod, 'service_tool_registry', _broken(_B))
    lh.action_named_tools(agents, 'crawl4ai: fetch the page')
    lines = _unreadable(said)
    assert [repr(_A) in w for w in lines] == [True, False], said
    assert [repr(_B) in w for w in lines] == [False, True], said


def test_a_failure_is_said_once_and_a_new_one_once_more(monkeypatch, said):
    agents = [_Agent('Helper', ['crawl4ai_crawl'])]
    monkeypatch.setattr(reg_mod, 'service_tool_registry', _broken(_A))
    lh.action_named_tools(agents, 'crawl4ai: fetch the page')
    lh.action_named_tools(agents, 'crawl4ai: fetch the page')
    assert len(_unreadable(said)) == 1, said
    monkeypatch.setattr(reg_mod, 'service_tool_registry', _broken(_B))
    lh.action_named_tools(agents, 'crawl4ai: fetch the page')
    lh.action_named_tools(agents, 'crawl4ai: fetch the page')
    lines = _unreadable(said)
    assert len(lines) == 2, said
    assert repr(_A) in lines[0] and repr(_B) in lines[1], said
