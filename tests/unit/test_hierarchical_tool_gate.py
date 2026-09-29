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
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

from core.agent_tools import (attach_for_tags, discover_and_attach,
                              filter_service_tools)
from integrations.agent_engine.goal_manager import get_tool_tags
from integrations.agent_engine.marketing_tools import detect_goal_tags

_ROOT = Path(__file__).resolve().parents[2]


def _called_names(tree):
    """Every called function's name, bare or through a module/object."""
    return [n.func.id if isinstance(n.func, ast.Name) else n.func.attr
            for n in ast.walk(tree) if isinstance(n, ast.Call)
            and isinstance(n.func, (ast.Name, ast.Attribute))]


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

    # ── Lever 2 wires (owner-ratified 2026-09-01: half-VRAM budget makes
    # tag precision the primary overflow fix; stored tags speak the SAME
    # goal-tag vocabulary detection speaks, so downstream is untouched) ──

    def test_coding_row_covers_programmer_workbench(self):
        """Wire 1: a coding agent must reach docs + the desktop — the
        row lacked web/crawling/computer-use entirely.  Tags must speak
        REGISTRY vocabulary (declared ServiceTool tags), not synonyms:
        'web_search' matches nothing, 'web' matches crawl4ai."""
        caps = get_tool_tags('coding')
        self.assertIn('web', caps)
        self.assertIn('crawling', caps)
        self.assertIn('coding', caps)   # existing rows preserved
        self.assertIn('github', caps)
        self.assertNotIn('web_search', caps)  # synonym drift guard
        # The archetype DECLARES computer-use (owner: a coding agent
        # looks at the desktop); today the capability rides the core
        # execute_windows_or_android_command tool on the main Qwen3-VL.
        self.assertIn('computer-use', caps)

    def test_legacy_omniparser_wrapper_does_not_claim_computer_use(self):
        """Owner 2026-09-01: omniparser was replaced by qwen3vl_backend.
        The deprecated wrapper must never resolve for 'computer-use' —
        its base_url localhost:8080 now belongs to llama-server, so an
        agent attaching it would dial the wrong service."""
        from integrations.service_tools.omniparser_tool import OmniParserTool
        info = OmniParserTool.create_tool_info()
        self.assertNotIn('computer-use', info.tags)
        self.assertNotIn('screen', info.tags)
        # behavioral: the row actually UNLOCKS the web tool through the
        # real filter (fake crawl4ai declares ['web','scraping'])
        kept = filter_service_tools(['coding'], _SVC_TOOLS, _SVC_DEFS,
                                    _fake_registry())
        self.assertIn('crawl4ai_crawl', kept)
        self.assertNotIn('pocket_tts_synthesize', kept)

    def test_resolve_goal_tags_is_layered_union(self):
        """Wire 3: stored semantic tags UNION lexical detection — legacy
        records (no stored tags) resolve to exactly today's detection,
        so behavior can only gain tags, never lose."""
        from integrations.agent_engine.marketing_tools import resolve_goal_tags
        # legacy: None/empty stored == pure detection (byte-equal path)
        self.assertEqual(resolve_goal_tags(None, 'fix the bug fix in repo'),
                         detect_goal_tags('fix the bug fix in repo'))
        self.assertEqual(resolve_goal_tags([], 'compose a song about rain'),
                         detect_goal_tags('compose a song about rain'))
        # stored-only: text detects nothing, stored carries the tag
        self.assertEqual(resolve_goal_tags(['coding'], 'a helpful assistant'),
                         ['coding'])
        # union: both sides contribute, deduped + sorted
        got = resolve_goal_tags(['coding'], 'compose a song about rain')
        self.assertIn('coding', got)
        self.assertIn('media', got)
        # junk in stored is dropped, not crashed on
        self.assertEqual(resolve_goal_tags([None, 42, 'coding'], ''), ['coding'])

    def test_reuse_and_create_gates_resolve_stored_tags(self):
        """Wire 3 call sites: both constructors resolve via
        resolve_goal_tags (detection PRESERVED inside it — no lost
        calls); reuse reads goal_tags from the SAME agent-record load
        that already supplies `goal`."""
        reuse = (_ROOT / 'hartos' / 'reuse_recipe.py').read_text(encoding='utf-8')
        create = (_ROOT / 'hartos' / 'create_recipe.py').read_text(encoding='utf-8')
        self.assertIn("config.get('goal_tags')", reuse)
        self.assertIn('resolve_goal_tags(', reuse)
        self.assertIn('resolve_goal_tags(', create)
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
        """Parity + no-parallel-path: sanctioned goal-tag scan sites only.

        Each pipeline resolves goal tags ONCE at construction
        (resolve_goal_tags: stored tags united with detected, Lever 2) and
        runs the per-turn drift scan through ONE shared helper,
        core.agent_tool_menu.attach_for_turn, called once from each turn
        entry (REUSE get_agent_response, CREATE _attach_for_create_turn;
        review of d99b1aa88).  So per file: 1 resolution, 1 gate, 1
        attach_for_turn, and no attach_for_tags of its own (that primitive
        is attach_for_turn's; a direct call is a second per-turn attach).

        Calls are counted by name whether bare (``detect_goal_tags(...)``)
        or through a module (``marketing_tools.detect_goal_tags(...)``):
        counting bare names only let an attribute call slip past (review of
        a4dc8cf3b, F3).  Any count above these means a scan regrew."""
        attach_primitive_owner = _ROOT / 'core' / 'agent_tool_menu.py'
        for fname in ('create_recipe.py', 'reuse_recipe.py'):
            src = (_ROOT / 'hartos' / fname).read_text(encoding='utf-8',
                                                       errors='replace')
            # Count REAL CALLS via AST, not text: a docstring or comment that
            # names a function is not a call site (measured 2026-09-08, the
            # text count scored prose inside _reuse_action_tool_names).
            called = _called_names(ast.parse(src))
            n_scan = sum(name in ('detect_goal_tags', 'resolve_goal_tags')
                         for name in called)
            self.assertEqual(
                n_scan, 1,
                f'{fname}: expected exactly 1 goal-tag resolution call (the '
                f'construction gate); the per-turn scan is attach_for_turn\'s')
            self.assertEqual(
                called.count('filter_service_tools'), 1,
                f'{fname}: expected exactly one Tier-1 gate call')
            self.assertEqual(
                called.count('attach_for_turn'), 1,
                f'{fname}: expected exactly one per-turn attach call')
        # attach_for_tags is called only inside core/agent_tool_menu.py.
        stray = []
        for top in ('hartos', 'integrations', 'core', 'security'):
            for path in (_ROOT / top).rglob('*.py'):
                if path == attach_primitive_owner or '__pycache__' in path.parts:
                    continue
                try:
                    tree = ast.parse(path.read_text(encoding='utf-8',
                                                    errors='replace'))
                except SyntaxError:
                    continue
                if 'attach_for_tags' in _called_names(tree):
                    stray.append(str(path.relative_to(_ROOT)))
        self.assertEqual(stray, [], 'attach_for_tags called outside '
                         'core/agent_tool_menu.py: use attach_for_turn')

    def test_the_scan_counter_sees_attribute_calls(self):
        """Anti-vacuity for the guard above."""
        tree = ast.parse('import m\n'
                         'def f(x):\n'
                         '    m.detect_goal_tags(x)\n'
                         '    detect_goal_tags(x)\n'
                         '    menu.attach_for_tags(1, 2, 3, 4, 5)\n')
        called = _called_names(tree)
        self.assertEqual(called.count('detect_goal_tags'), 2)
        self.assertEqual(called.count('attach_for_tags'), 1)

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
