"""`request_tools` must be able to attach a CORE tool, not just a service one.

OWNER REQUIREMENT (2026-09-09): "agents even without a named tool should
identify the need for a tool at RUNTIME, for autonomous agent creation AND
reuse."  That runtime path is `request_tools` -> `discover_and_attach`.

THE DEFECT.  `attach_for_names` was given a `core_tools=` source when D25/#788
found it searched only `registry._tools` (the SERVICE registry).  Its two
siblings were not.  So the NAMED path can reach a core closure and the
DISCOVERY path -- the one an agent uses when its recipe names nothing -- still
cannot.  Measured live 2026-09-09 on agent 33323830039: the model called
`request_tools` at 17:55:35 because it could not see
execute_windows_or_android_command, and discovery attached nothing, because
that tool is a core closure and the registry holds 13 service names.

Registry contents on this deployment (payments x3, seo_audit_score,
gh_pr_open, crawl4ai, crawl4ai_crawl, pocket_tts x3, acestep x3) -- of the
tool names recipes actually use, exactly one (crawl4ai) is among them.

NOT EXTENDED, deliberately: `attach_for_tags` selects on `tool.tags`, and core
closures are (name, description, func) triples carrying no tags.  Giving it
core access would mean inventing a second tag taxonomy for the core set --
a parallel path, which is the thing being removed, not added.  Tag-driven
attach stays registry-only until core closures carry real tags.

    python -m pytest tests/unit/test_runtime_discovery_reaches_core_tools.py --noconftest -q
"""
from core.agent_tools import MAIN_LEG_CORE_TOOLS, discover_and_attach


class _FakeAgent:
    def __init__(self, name):
        self.name = name
        self.llm_registered = []

    def register_for_llm(self, name=None, description=None):
        def _wrap(func):
            self.llm_registered.append(name)
            return func
        return _wrap

    def register_for_execution(self, name=None):
        def _wrap(func):
            return func
        return _wrap


class _EmptyRegistry:
    """The live registry as it is for core names: it does not hold them."""
    _tools = {}

    def create_endpoint_function(self, tool_name, ep_name):  # pragma: no cover
        return None


def _core_closures():
    return [
        ('execute_windows_or_android_command',
         'Processes user-defined commands on a personal Windows or Android system.',
         lambda **kw: 'ok'),
        ('get_text_from_image', 'extract text from an image by ocr', lambda **kw: 'ok'),
    ]


class TestThePrecondition:

    def test_the_tool_is_not_always_on(self):
        """If it were in the main-leg set it would never need discovering."""
        assert 'execute_windows_or_android_command' not in MAIN_LEG_CORE_TOOLS, (
            "adding this to the always-on set would be a SECURITY POSTURE change "
            "(arbitrary OS commands for every agent), not a bug fix")


class TestRuntimeDiscoveryReachesCore:
    """RED before the fix: request_tools cannot surface a core tool."""

    def test_discovery_attaches_a_core_tool_by_capability_words(self):
        helper, executor = _FakeAgent('helper'), _FakeAgent('assistant')
        attached = set()

        out = discover_and_attach(
            'run a command on my windows computer',
            helper, executor, _EmptyRegistry(), attached,
            core_tools=_core_closures(),
        )

        assert 'execute_windows_or_android_command' in attached, (
            "request_tools -> discover_and_attach found nothing for a plain "
            "capability request. The agent asked for the capability at RUNTIME, "
            "which is the designed behaviour, and discovery could not reach the "
            "core closure that provides it. Got: %r" % out)
        assert 'execute_windows_or_android_command' in helper.llm_registered

    def test_it_is_idempotent(self):
        helper, executor = _FakeAgent('helper'), _FakeAgent('assistant')
        attached = set()
        args = ('run a command on my windows computer', helper, executor,
                _EmptyRegistry(), attached)
        discover_and_attach(*args, core_tools=_core_closures())
        before = len(helper.llm_registered)
        discover_and_attach(*args, core_tools=_core_closures())
        assert len(helper.llm_registered) == before, (
            "a second identical request re-registered the same tool")

    def test_unrelated_need_does_not_over_attach(self):
        """The matcher must still discriminate, or every request attaches all."""
        helper, executor = _FakeAgent('helper'), _FakeAgent('assistant')
        attached = set()
        discover_and_attach('compose a haiku about autumn', helper, executor,
                            _EmptyRegistry(), attached,
                            core_tools=_core_closures())
        assert 'execute_windows_or_android_command' not in attached, (
            "an unrelated capability request attached the OS-command tool")

    def test_omitting_core_tools_is_a_no_op(self):
        """Existing callers that pass nothing must behave exactly as before."""
        helper, executor = _FakeAgent('helper'), _FakeAgent('assistant')
        attached = set()
        discover_and_attach('run a command on my windows computer',
                            helper, executor, _EmptyRegistry(), attached)
        assert attached == set()
