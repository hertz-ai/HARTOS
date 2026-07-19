"""
Tests for the Paper Explanation goal type — the idle-time agent that reads
AI/BCI research papers and publishes plain-language explanations to the
hevolve.ai research pages through consent-gated GitHub PRs.

Pins the contract that:
  1. The 'paper_explanation' goal type is registered with tool tags that
     expose the canonical URL-fetch tool (Crawl4AITool, tags web/crawling —
     backs data_extraction_from_url) and gh_pr_open (tags github/publish).
  2. The prompt names the canonical tools VERBATIM (the LLM uses the
     prompt as its playbook), carries the idle-only language (MODE_IDLE +
     should_yield_to_user), the grounded-explanation rules (abstract-based,
     never invent findings), and PR-only publishing where the human merge
     is the consent gate.
  3. The bootstrap_paper_explainer seed exists, is disabled by default
     (prompt builder returns None until the operator arms it), and its
     config carries the idle_only + consent flags and the website-repo
     file targets (researchPapers.json / researchExplanations.json).
  4. The agent_daemon honors idle_only via _idle_only_blocked, gated on
     the EXISTING ResourceGovernor MODE_IDLE (no parallel scheduler).

Style mirrors test_seo_goal_type.py (goal-type registration + prompt
content + seed contract).
"""
import os
import sys

# Ensure project root on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')


# ─── Goal Manager Registration Tests ───

class TestPaperExplanationGoalTypeRegistration:
    """Verify 'paper_explanation' is registered in the prompt builder registry."""

    def test_paper_explanation_in_prompt_builders(self):
        from integrations.agent_engine.goal_manager import _prompt_builders
        assert 'paper_explanation' in _prompt_builders

    def test_paper_explanation_in_registered_types(self):
        from integrations.agent_engine.goal_manager import get_registered_types
        assert 'paper_explanation' in get_registered_types()

    def test_get_prompt_builder_returns_callable(self):
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('paper_explanation')
        assert builder is not None
        assert callable(builder)

    def test_tool_tags_expose_crawler_and_gh_pr(self):
        """Tags must intersect BOTH tools' ServiceToolInfo tags:
        Crawl4AITool tags=['web','scraping','markdown','crawling'] (the
        canonical URL-fetch backing data_extraction_from_url), GhPrTool
        tags=['github','publish','pr','blog'] (the PR-only publish path)."""
        from integrations.agent_engine.goal_manager import get_tool_tags
        tags = get_tool_tags('paper_explanation')
        assert 'web' in tags        # matches Crawl4AITool.tags
        assert 'crawling' in tags   # matches Crawl4AITool.tags
        assert 'github' in tags     # matches GhPrTool.tags
        assert 'publish' in tags    # matches GhPrTool.tags


# ─── Prompt Builder Tests ───

def _enabled_config(**overrides):
    cfg = {
        'repo': 'hertz-ai/Hevolve',
        'base_branch': 'main',
        'target_file': 'src/data/researchExplanations.json',
        'papers_source': 'src/data/researchPapers.json',
        'topics': ['ai', 'bci'],
        'source': 'Nature + arXiv',
        'max_per_cycle': 1,
        'enabled': True,
        'idle_only': True,
    }
    cfg.update(overrides)
    return cfg


class TestBuildPaperExplanationPrompt:
    """Test _build_paper_explanation_prompt output and its config gate."""

    def _build(self, config=None, **overrides):
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('paper_explanation')
        goal = {
            'title': overrides.get('title', 'Test Paper Goal'),
            'description': overrides.get('description', 'Test description'),
            'config': _enabled_config() if config is None else config,
        }
        return builder(goal)

    # — disabled-by-default gate (mirrors the seo/autoresearch config gate) —

    def test_disabled_config_returns_none(self):
        assert self._build(config={'repo': 'hertz-ai/Hevolve',
                                   'enabled': False}) is None

    def test_missing_enabled_flag_returns_none(self):
        """enabled defaults to False — an unconfigured goal never builds."""
        assert self._build(config={'repo': 'hertz-ai/Hevolve'}) is None

    def test_enabled_without_repo_returns_none(self):
        assert self._build(config={'enabled': True, 'repo': ''}) is None

    def test_enabled_with_repo_builds(self):
        prompt = self._build()
        assert prompt is not None
        assert 'hertz-ai/Hevolve' in prompt

    # — canonical tool names, verbatim (the prompt is the playbook) —

    def test_names_url_fetch_tool_verbatim(self):
        assert 'data_extraction_from_url' in self._build()

    def test_names_pr_tool_verbatim(self):
        assert 'gh_pr_open' in self._build()

    # — idle-only instructions (daemon enforces; prompt must state it) —

    def test_idle_only_language(self):
        prompt = self._build()
        assert 'IDLE TIME ONLY' in prompt
        assert 'MODE_IDLE' in prompt
        assert 'should_yield_to_user' in prompt
        assert 'idle_only' in prompt

    # — grounded-explanation rules —

    def test_grounded_never_invent_findings(self):
        prompt = self._build()
        assert 'NEVER invent results' in prompt
        assert 'no fabricated findings' in prompt

    def test_explanation_is_abstract_based(self):
        prompt = self._build()
        assert 'abstract' in prompt
        assert 'based on' in prompt

    def test_explanation_shape_short_paragraphs(self):
        prompt = self._build()
        assert '2-3 short' in prompt
        assert 'blank lines' in prompt

    def test_one_paper_per_cycle(self):
        prompt = self._build()
        assert 'ONE paper' in prompt

    def test_max_per_cycle_honors_config_override(self):
        prompt = self._build(config=_enabled_config(max_per_cycle=2))
        assert 'at most 2 paper(s)' in prompt

    # — PR-only publishing + the human-merge consent gate —

    def test_never_push_directly(self):
        assert 'NEVER push directly' in self._build()

    def test_pr_is_the_only_publish_path(self):
        assert 'pull request' in self._build()

    def test_human_merge_is_consent_gate(self):
        prompt = self._build()
        assert 'consent gate' in prompt
        # Mirrors the seo/marketing goals' consent rule: external
        # publication requires operator approval.
        assert 'Never auto-publish externally without operator approval' in prompt

    # — website-repo file targets (the shapes the Hevolve repo consumes) —

    def test_references_explanations_target_file(self):
        assert 'src/data/researchExplanations.json' in self._build()

    def test_references_papers_source(self):
        assert 'src/data/researchPapers.json' in self._build()

    def test_references_explanations_map_entry_shape(self):
        prompt = self._build()
        assert "'explanations' map" in prompt
        assert '<paper_url>' in prompt

    def test_contains_topics_and_source(self):
        prompt = self._build()
        assert 'ai' in prompt and 'bci' in prompt
        assert 'Nature + arXiv' in prompt

    def test_contains_title_and_description(self):
        prompt = self._build(title='My Paper Goal', description='My Desc')
        assert 'My Paper Goal' in prompt
        assert 'My Desc' in prompt

    # — human-voice guide + paper-specific closing caveat —

    def test_human_voice_no_em_dash_rule(self):
        """The shared voice guide states the hard no-em-dash rule, and the
        returned prompt is itself em-dash-free (the one place the glyph may
        appear is where the rule names the banned character)."""
        prompt = self._build()
        assert 'never use an em dash' in prompt
        assert '—' not in prompt.replace(
            "never use an em dash (the '—' character)", '')

    def test_human_voice_grounds_and_bans_tells(self):
        prompt = self._build()
        assert 'Ground every claim' in prompt
        for tell in ('delve', 'leverage', 'cutting-edge'):
            assert tell in prompt

    def test_explanation_ends_with_varied_caveat(self):
        """Paper explanations must close with a freshly-worded caveat that
        the summary is abstract-based and to read the full paper."""
        prompt = self._build()
        assert 'read the full paper' in prompt
        assert 'freshly-worded caveat' in prompt

    def test_explanation_json_value_is_plain_text_with_blank_lines(self):
        """The researchExplanations.json value is plain text, paragraphs
        joined by \\n\\n (the shape the website consumes)."""
        prompt = self._build()
        assert '\\n\\n' in prompt


# ─── Seed Goal Tests ───

class TestPaperExplainerSeedGoal:
    """Verify the bootstrap_paper_explainer seed entry contract."""

    def _get_goal(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        return next(g for g in SEED_BOOTSTRAP_GOALS
                    if g['slug'] == 'bootstrap_paper_explainer')

    def test_seed_goal_exists(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert 'bootstrap_paper_explainer' in slugs

    def test_slug_unique_in_seed_list(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert slugs.count('bootstrap_paper_explainer') == 1

    def test_goal_type_is_paper_explanation(self):
        assert self._get_goal()['goal_type'] == 'paper_explanation'

    def test_goal_type_is_registered(self):
        """A seed with an unregistered goal_type is silently skipped by
        seed_bootstrap_goals (create_goal rejects it), breaking the
        count == len(SEED_BOOTSTRAP_GOALS) invariant."""
        from integrations.agent_engine.goal_manager import get_registered_types
        assert self._get_goal()['goal_type'] in get_registered_types()

    def test_config_contract(self):
        cfg = self._get_goal()['config']
        assert cfg['repo'] == 'hertz-ai/Hevolve'
        assert cfg['target_file'] == 'src/data/researchExplanations.json'
        assert cfg['papers_source'] == 'src/data/researchPapers.json'
        assert cfg['topics'] == ['ai', 'bci']
        assert cfg['source'] == 'Nature + arXiv'
        assert cfg['max_per_cycle'] == 1
        assert cfg['requires_consent'] is True
        assert cfg['idle_only'] is True

    def test_disabled_by_default(self):
        """Seeded dormant: enabled=False, and the prompt builder must
        decline the seed config as-is (daemon skips dispatch) until a
        human arms it."""
        from integrations.agent_engine.goal_manager import get_prompt_builder
        goal = self._get_goal()
        assert goal['config'].get('enabled') is False
        builder = get_prompt_builder('paper_explanation')
        prompt = builder({
            'title': goal['title'],
            'description': goal['description'],
            'config': goal['config'],
        })
        assert prompt is None

    def test_description_names_canonical_tools_verbatim(self):
        """The LLM uses the description as its playbook — the tool
        identifiers must be exact (same rationale as test_seo_goal_type)."""
        desc = self._get_goal()['description']
        for tool_name in ('data_extraction_from_url', 'gh_pr_open'):
            assert tool_name in desc, f'description must name {tool_name!r}'

    def test_description_carries_idle_only_language(self):
        desc = self._get_goal()['description']
        assert 'IDLE time only' in desc

    def test_description_carries_pr_only_consent_language(self):
        desc = self._get_goal()['description']
        assert 'NEVER push directly' in desc
        assert 'consent gate' in desc
        assert 'never auto-publish externally' in desc.lower()

    def test_description_carries_grounded_language(self):
        desc = self._get_goal()['description']
        assert 'NEVER invent results' in desc
        assert 'abstract' in desc

    def test_description_references_website_repo_shapes(self):
        desc = self._get_goal()['description']
        assert 'src/data/researchPapers.json' in desc
        assert 'src/data/researchExplanations.json' in desc
        assert 'research:pull' in desc

    def test_spark_budget_positive(self):
        assert self._get_goal()['spark_budget'] > 0


# ─── Idle gating (agent_daemon honors idle_only via ResourceGovernor) ───

class TestIdleOnlyGating:
    """_idle_only_blocked must gate on the EXISTING ResourceGovernor
    MODE_IDLE — the one canonical idle detector, no parallel scheduler."""

    def _governor(self):
        from core.resource_governor import get_governor
        return get_governor()

    def test_not_flagged_never_blocked(self):
        from integrations.agent_engine.agent_daemon import _idle_only_blocked
        assert _idle_only_blocked({}) is False
        assert _idle_only_blocked(None) is False
        assert _idle_only_blocked({'idle_only': False}) is False

    def test_blocked_while_governor_active(self):
        from integrations.agent_engine.agent_daemon import _idle_only_blocked
        from core.resource_governor import MODE_ACTIVE
        gov = self._governor()
        prev = gov._mode
        try:
            gov._mode = MODE_ACTIVE
            assert _idle_only_blocked({'idle_only': True}) is True
        finally:
            gov._mode = prev

    def test_blocked_while_governor_sleep(self):
        from integrations.agent_engine.agent_daemon import _idle_only_blocked
        from core.resource_governor import MODE_SLEEP
        gov = self._governor()
        prev = gov._mode
        try:
            gov._mode = MODE_SLEEP
            assert _idle_only_blocked({'idle_only': True}) is True
        finally:
            gov._mode = prev

    def test_allowed_while_governor_idle(self):
        from integrations.agent_engine.agent_daemon import _idle_only_blocked
        from core.resource_governor import MODE_IDLE
        gov = self._governor()
        prev = gov._mode
        try:
            gov._mode = MODE_IDLE
            assert _idle_only_blocked({'idle_only': True}) is False
        finally:
            gov._mode = prev

    def test_fails_closed_when_governor_errors(self):
        """idle_only means PROVEN idle — an unreadable governor blocks."""
        from unittest.mock import patch
        from integrations.agent_engine.agent_daemon import _idle_only_blocked
        with patch('core.resource_governor.get_governor',
                   side_effect=RuntimeError('governor down')):
            assert _idle_only_blocked({'idle_only': True}) is True

    def test_seed_goal_is_idle_gated(self):
        """The bootstrap seed's own config flows through the daemon gate."""
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        from integrations.agent_engine.agent_daemon import _idle_only_blocked
        from core.resource_governor import MODE_ACTIVE, MODE_IDLE
        cfg = next(g for g in SEED_BOOTSTRAP_GOALS
                   if g['slug'] == 'bootstrap_paper_explainer')['config']
        gov = self._governor()
        prev = gov._mode
        try:
            gov._mode = MODE_ACTIVE
            assert _idle_only_blocked(cfg) is True
            gov._mode = MODE_IDLE
            assert _idle_only_blocked(cfg) is False
        finally:
            gov._mode = prev
