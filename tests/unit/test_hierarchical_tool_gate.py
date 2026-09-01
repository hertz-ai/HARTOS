"""Tier-1 hierarchical tool gate: need-to-know service-tool loading.

Owner decision 2026-08-31: option 1, hierarchically selected.  The design
already existed half-wired — detect_goal_tags ("category-based tool
loading"), register_goal_type(tool_tags=[ServiceToolRegistry tags]) and
get_tool_tags (tested-but-dead, #666) — while the attach loops in
create/reuse_recipe registered EVERY registry tool unconditionally.
Measured cost of the ungated loop: 50 rendered defs = 5,820 of the
6,144-token slot, so a one-message conversation overflowed (12 context-
exceeded rejections in one boot).

    python -m pytest tests/unit/test_hierarchical_tool_gate.py --noconftest -q
"""
from pathlib import Path
from types import SimpleNamespace
import unittest

from core.agent_tools import (attach_for_tags, discover_and_attach,
                              filter_service_tools)
from integrations.agent_engine.goal_manager import get_tool_tags
from integrations.agent_engine.marketing_tools import detect_goal_tags

_ROOT = Path(__file__).resolve().parents[2]


def _fake_registry():
    tools = {
        'crawl4ai': SimpleNamespace(tags=['web', 'scraping']),
        'pocket_tts': SimpleNamespace(tags=['tts', 'speech']),
    }
    return SimpleNamespace(_tools=tools)


_SVC_TOOLS = {'crawl4ai_crawl': lambda: None, 'pocket_tts_synthesize': lambda: None}
_SVC_DEFS = [
    {'name': 'crawl4ai_crawl', 'service_tool': 'crawl4ai'},
    {'name': 'pocket_tts_synthesize', 'service_tool': 'pocket_tts'},
]


class HierarchicalToolGate(unittest.TestCase):

    def test_goal_unlocks_only_matching_capability_tags(self):
        kept = filter_service_tools(['marketing'], _SVC_TOOLS, _SVC_DEFS,
                                    _fake_registry())
        self.assertIn('crawl4ai_crawl', kept, "'marketing' unlocks web/scraping")
        self.assertNotIn('pocket_tts_synthesize', kept,
                         "'marketing' must not drag TTS defs into the prompt")

    def test_no_goal_tags_means_no_service_tools(self):
        """Need-to-know default: a general conversation carries only the
        always-on core closures, zero registry defs."""
        self.assertEqual(filter_service_tools([], _SVC_TOOLS, _SVC_DEFS,
                                              _fake_registry()), {})
        self.assertEqual(filter_service_tools(['no_such_tag'], _SVC_TOOLS,
                                              _SVC_DEFS, _fake_registry()), {})

    def test_media_goal_unlocks_tts(self):
        kept = filter_service_tools(['media'], _SVC_TOOLS, _SVC_DEFS,
                                    _fake_registry())
        self.assertIn('pocket_tts_synthesize', kept)

    def test_capability_rows_seeded(self):
        """get_tool_tags is no longer dead — the detectable vocabulary maps
        to registry capability tags (goal_manager._CAPABILITY_TAGS)."""
        self.assertIn('web', get_tool_tags('marketing'))
        self.assertIn('tts', get_tool_tags('media'))
        self.assertEqual(get_tool_tags('never_registered'), [])

    def test_detect_media_vocabulary(self):
        self.assertIn('media', detect_goal_tags('compose a song about rain'))
        self.assertNotIn('media', detect_goal_tags('summarize this text file'))

    def test_discover_attaches_gated_out_tool(self):
        """Never-say-unavailable: a need matching a registry tool attaches
        it onto the live agents mid-conversation."""
        calls = []

        class _Agent:
            def register_for_llm(self, name=None, description=None):
                calls.append(('llm', name))
                return lambda f: f

            def register_for_execution(self, name=None):
                calls.append(('exec', name))
                return lambda f: f

        reg = _fake_registry()
        reg._tools['pocket_tts'].endpoints = {
            'synthesize': {'description': 'Text to speech synthesis'}}
        reg._tools['crawl4ai'].endpoints = {
            'crawl': {'description': 'Crawl a webpage to markdown'}}
        reg._tools['pocket_tts'].description = 'offline speech synthesis'
        reg._tools['crawl4ai'].description = 'web crawler'
        reg.create_endpoint_function = lambda t, e: (lambda **kw: 'ok')
        attached = set()
        out = discover_and_attach('text to speech please', _Agent(), _Agent(),
                                  reg, attached)
        self.assertIn('pocket_tts_synthesize', attached)
        self.assertIn('Attached', out)
        self.assertNotIn('crawl4ai_crawl', attached,
                         'unrelated tools must not attach')

    def test_discover_no_match_offers_routes_not_denial(self):
        reg = _fake_registry()
        for t in reg._tools.values():
            t.endpoints = {}
        out = discover_and_attach('quantum teleportation', object(), object(),
                                  reg, set())
        self.assertNotIn('impossible', out.split('Do not tell')[0])
        for route in ('install', 'peer', 'consent'):
            self.assertIn(route, out)

    def test_single_detection_and_gate_in_both_constructors(self):
        """Parity + no-parallel-path: sanctioned detection sites only.
        create: 1 (construction).  reuse: 2 (construction + the per-turn
        hook in get_agent_response that attaches families when the
        conversation drifts — deterministic, zero extra LLM calls).
        Any count above these means a detection regrew somewhere."""
        expected_detect = {'create_recipe.py': 1, 'reuse_recipe.py': 2}
        for fname, n_expected in expected_detect.items():
            src = (_ROOT / 'hartos' / fname).read_text(encoding='utf-8',
                                                       errors='replace')
            # count CODE lines only — reuse_recipe:2691 names the function
            # inside a #510 history comment
            code = [ln for ln in src.splitlines()
                    if not ln.lstrip().startswith('#')]
            n_detect = sum('detect_goal_tags(' in ln for ln in code)
            n_filter = sum('filter_service_tools(' in ln for ln in code)
            self.assertEqual(
                n_detect, n_expected,
                f'{fname}: expected exactly {n_expected} detect_goal_tags '
                f'call(s) (construction gate; reuse also has the per-turn '
                f'attach hook)')
            self.assertEqual(
                n_filter, 1,
                f'{fname}: expected exactly one Tier-1 gate call')

    def test_attach_for_tags_attaches_matching_family(self):
        """Per-turn drift: capability tags attach the matching family via
        the same primitives, skip non-matching and already-attached."""
        calls = []

        class _Agent:
            def register_for_llm(self, name=None, description=None):
                calls.append(('llm', name))
                return lambda f: f

            def register_for_execution(self, name=None):
                calls.append(('exec', name))
                return lambda f: f

        reg = _fake_registry()
        reg._tools['pocket_tts'].endpoints = {
            'synthesize': {'description': 'Text to speech synthesis'}}
        reg._tools['crawl4ai'].endpoints = {
            'crawl': {'description': 'Crawl a webpage to markdown'}}
        reg.create_endpoint_function = lambda t, e: (lambda **kw: 'ok')
        attached = set()
        n = attach_for_tags({'tts', 'speech'}, _Agent(), _Agent(), reg,
                            attached)
        self.assertEqual(n, 1)
        self.assertIn('pocket_tts_synthesize', attached)
        self.assertNotIn('crawl4ai_crawl', attached)
        # idempotent across turns: second call attaches nothing
        self.assertEqual(
            attach_for_tags({'tts'}, _Agent(), _Agent(), reg, attached), 0)

    def test_attach_for_tags_empty_tags_noop(self):
        self.assertEqual(
            attach_for_tags(set(), object(), object(), _fake_registry(),
                            set()), 0)

    def test_discover_matches_morphological_variant(self):
        """'scrape' is NOT a substring of 'scraping' — the 4-char stem rule
        must catch inflected forms (was a proven miss pre-fix)."""

        class _Agent:
            def register_for_llm(self, name=None, description=None):
                return lambda f: f

            def register_for_execution(self, name=None):
                return lambda f: f

        reg = _fake_registry()
        reg._tools['crawl4ai'].endpoints = {
            'crawl': {'description': 'Crawl a URL to markdown'}}
        reg._tools['pocket_tts'].endpoints = {
            'synthesize': {'description': 'Text to speech synthesis'}}
        reg.create_endpoint_function = lambda t, e: (lambda **kw: 'ok')
        attached = set()
        discover_and_attach('scrape the site', _Agent(), _Agent(), reg,
                            attached)
        self.assertIn('crawl4ai_crawl', attached)
        self.assertNotIn('pocket_tts_synthesize', attached)

    def test_discover_stopwords_do_not_overattach(self):
        """'the' is a substring of 'synthesis' — stopwords must not match."""
        reg = _fake_registry()
        reg._tools['pocket_tts'].endpoints = {
            'synthesize': {'description': 'Text to speech synthesis'}}
        reg._tools['crawl4ai'].endpoints = {}
        attached = set()
        out = discover_and_attach('please get the thing for me', object(),
                                  object(), reg, attached)
        self.assertEqual(attached, set())
        self.assertIn('No local registry tool matches', out)

    def test_registry_umbrella_has_no_orphans(self):
        """Exhaustiveness where it is enumerable: every statically seeded
        registry tool must be reachable through >=1 goal tag's capability
        set — a tool added with out-of-umbrella tags goes red here instead
        of being silently unreachable by the gate."""
        from integrations.service_tools import (
            service_tool_registry, Crawl4AITool, AceStepTool,
            SeoAuditTool, GhPrTool)
        from integrations.agent_engine.goal_manager import _tool_tags
        Crawl4AITool.register()
        AceStepTool.register()
        SeoAuditTool.register()
        GhPrTool.register()
        cap_by_goal = {g: set(get_tool_tags(g)) for g in _tool_tags}
        for name, tool in service_tool_registry._tools.items():
            tags = set(tool.tags or [])
            reachable = [g for g, caps in cap_by_goal.items() if tags & caps]
            self.assertTrue(
                reachable,
                f"registry tool '{name}' (tags={sorted(tags)}) is an ORPHAN: "
                f"no goal tag unlocks it — add a capability tag to a "
                f"goal_manager row or fix the tool's tags")

    def test_intent_prompts_mention_request_tools(self):
        """The model can only reach the discovery layer it knows about:
        both the Assistant delegation list and the Helper system message
        must name request_tools (owner 2026-08-31: 'intent shd know')."""
        src = (_ROOT / 'hartos' / 'reuse_recipe.py').read_text(
            encoding='utf-8', errors='replace')
        self.assertIn("ask @Helper to call the 'request_tools' tool", src)
        self.assertIn("FIRST call the 'request_tools' tool", src)


if __name__ == '__main__':
    unittest.main()
