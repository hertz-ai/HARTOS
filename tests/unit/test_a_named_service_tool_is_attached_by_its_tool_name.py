"""A step that names a service tool by its tool name gets that tool.

MEASURED LIVE on the desktop 4B, 2026-11-16 (build 75, agent Quick Research
Brief 1794818852965, parent test account):

  * the approved plan's steps 2 and 3 read "crawl4ai: fetch the first relevant
    result URL to extract clean markdown content";
  * every round of those steps logged
        Tier-1 named attach: action 2 names ['google_search', 'crawl4ai'] -> 0 tools
    (14:30:15 IST on the REUSE turn; the same on CREATE);
  * so no page-reading tool was ever offered (the 50 tools on the wire held
    none), the model read stored search results instead, CREATE asked the
    person for help at step 2 and REUSE recorded GAVE_UP at step 2
    ("no canonical receipt in its dispatch window"), and the person got a
    narration of the failure instead of the three facts.

Cause: attach_for_names matches a service tool's ENDPOINT function name,
``<tool>_<endpoint>`` unless the endpoint shares the tool's name.  The
in-process crawler registers tool ``crawl4ai`` with the one endpoint
``crawl``, so its function is ``crawl4ai_crawl`` and a step naming
``crawl4ai`` resolved nothing.  A tool with exactly one endpoint is named
unambiguously by its tool name; a tool with several still needs the endpoint.

    python -m pytest tests/unit/test_a_named_service_tool_is_attached_by_its_tool_name.py -q --noconftest
"""
from core.agent_tools import attach_for_names
from integrations.service_tools.crawl4ai_tool import Crawl4AITool
from integrations.service_tools.registry import ServiceToolInfo, ServiceToolRegistry


class _FakeAgent:
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


def _registry(*infos):
    reg = ServiceToolRegistry.__new__(ServiceToolRegistry)
    reg._tools = {i.name: i for i in infos}
    reg.create_endpoint_function = lambda t, e: (lambda **kw: f'{t}.{e} ran')
    return reg


def _two_endpoint_tool():
    return ServiceToolInfo(
        name='pocket_tts', description='speech', base_url='native://x',
        endpoints={
            'speak': {'path': '/speak', 'method': 'POST', 'description': 'speak',
                      'params_schema': {}},
            'voices': {'path': '/voices', 'method': 'GET', 'description': 'list',
                       'params_schema': {}},
        })


def test_a_step_naming_crawl4ai_gets_the_crawler():
    helper, executor = _FakeAgent('helper'), _FakeAgent('assistant')
    n = attach_for_names(['google_search', 'crawl4ai'], helper, executor,
                         _registry(Crawl4AITool.create_tool_info()), set())
    assert n == 1, f'a step naming crawl4ai attached {n} tools (live: 0)'
    assert helper.llm_registered == ['crawl4ai_crawl'], helper.llm_registered
    assert executor.exec_registered == ['crawl4ai_crawl'], executor.exec_registered


def test_the_endpoint_name_still_works():
    helper, executor = _FakeAgent('helper'), _FakeAgent('assistant')
    n = attach_for_names(['crawl4ai_crawl'], helper, executor,
                         _registry(Crawl4AITool.create_tool_info()), set())
    assert n == 1 and helper.llm_registered == ['crawl4ai_crawl']


def test_a_tool_with_several_endpoints_is_not_attached_by_its_tool_name():
    """Naming a multi-endpoint tool does not hand over all its endpoints."""
    helper, executor = _FakeAgent('helper'), _FakeAgent('assistant')
    n = attach_for_names(['pocket_tts'], helper, executor,
                         _registry(_two_endpoint_tool()), set())
    assert n == 0 and helper.llm_registered == []
