"""
Tests for the SEO Web Publisher goal type — the wire that connects news
ingests to hevolve.ai through consent-gated GitHub PRs (Phase 1 of the
distributed platform plan).

Pins the contract that:
  1. The 'seo' goal type is registered with tool tags that expose the
     previously-dormant service-tool pair (seo_audit_score / gh_pr_open).
  2. The prompt names the canonical tools VERBATIM (the LLM uses the
     prompt as its playbook — near-miss names would call nothing) and
     carries both hard gates: audit score >= 90 before publishing, and
     PR-only publishing where the human merge is the consent gate.
  3. The bootstrap_seo_publisher seed exists, is disabled by default
     (prompt builder returns None until the operator arms it), and its
     config carries the publish gate + consent flags.

Style mirrors test_news_tools.py (goal-type registration + prompt
content) and test_bootstrap_provision_tts_venvs.py (seed contract).
"""
import os
import sys

# Ensure project root on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')


# ─── Goal Manager Registration Tests ───

class TestSeoGoalTypeRegistration:
    """Verify 'seo' goal type is registered in the prompt builder registry."""

    def test_seo_in_prompt_builders(self):
        from integrations.agent_engine.goal_manager import _prompt_builders
        assert 'seo' in _prompt_builders

    def test_seo_in_registered_types(self):
        from integrations.agent_engine.goal_manager import get_registered_types
        assert 'seo' in get_registered_types()

    def test_get_prompt_builder_returns_callable(self):
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('seo')
        assert builder is not None
        assert callable(builder)

    def test_tool_tags_expose_seo_audit_and_gh_pr(self):
        """Tags must intersect BOTH dormant tools' ServiceToolInfo tags:
        SeoAuditTool tags=['seo','blog','audit','gate'], GhPrTool
        tags=['github','publish','pr','blog'] — plus 'news' so the agent
        can read/clear the publish_web flag via the news tools."""
        from integrations.agent_engine.goal_manager import get_tool_tags
        tags = get_tool_tags('seo')
        assert 'seo' in tags       # matches SeoAuditTool.tags
        assert 'github' in tags    # matches GhPrTool.tags
        assert 'publish' in tags   # matches GhPrTool.tags
        assert 'news' in tags      # publish_web flag tools live in news_tools


# ─── Prompt Builder Tests ───

def _enabled_config(**overrides):
    cfg = {
        'repo': 'hertz-ai/Hevolve',
        'base_branch': 'main',
        'min_seo_score': 90,
        'enabled': True,
    }
    cfg.update(overrides)
    return cfg


class TestBuildSeoPrompt:
    """Test _build_seo_prompt output and its disabled-by-default gate."""

    def _build(self, config=None, **overrides):
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('seo')
        goal = {
            'title': overrides.get('title', 'Test SEO Goal'),
            'description': overrides.get('description', 'Test description'),
            'config': _enabled_config() if config is None else config,
        }
        return builder(goal)

    # — disabled-by-default gate (mirrors the autoresearch config gate) —

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

    def test_names_audit_tool_verbatim(self):
        assert 'seo_audit_score' in self._build()

    def test_names_pr_tool_verbatim(self):
        assert 'gh_pr_open' in self._build()

    def test_names_publish_web_flag_tools(self):
        prompt = self._build()
        assert 'publish_web' in prompt
        assert 'list_news_for_web' in prompt
        assert 'mark_news_for_web' in prompt

    # — the audit gate —

    def test_contains_score_gate(self):
        prompt = self._build()
        assert '>= 90' in prompt
        assert 'SHIP' in prompt

    def test_score_gate_honors_config_override(self):
        prompt = self._build(config=_enabled_config(min_seo_score=95))
        assert '>= 95' in prompt

    # — PR-only publishing + the human-merge consent gate —

    def test_never_push_directly(self):
        assert 'NEVER push directly' in self._build()

    def test_pr_is_the_only_publish_path(self):
        assert 'pull request' in self._build()

    def test_human_merge_is_consent_gate(self):
        prompt = self._build()
        assert 'consent gate' in prompt
        # Mirrors the marketing goals' consent rule verbatim in spirit:
        # external publication requires operator approval.
        assert 'Never auto-publish externally without operator approval' in prompt

    # — website-repo formatting contract (registry + mirrors) —

    def test_references_news_registry_pattern(self):
        assert 'src/pages/News/newsData.js' in self._build()

    def test_references_mirror_files(self):
        prompt = self._build()
        assert 'public/sitemap.xml' in prompt
        assert 'scripts/prerender.js' in prompt
        assert 'scripts/verify-mirrors.js' in prompt

    # — provenance / etiquette —

    def test_aggregator_etiquette(self):
        prompt = self._build()
        assert 'attribution' in prompt
        assert 'no full-article republication' in prompt

    def test_contains_title_and_description(self):
        prompt = self._build(title='My SEO Goal', description='My Desc')
        assert 'My SEO Goal' in prompt
        assert 'My Desc' in prompt

    # — human-voice guide: the anti-AI-detection rules the prompt carries —

    def test_human_voice_no_em_dash_rule(self):
        """The shared voice guide must state the hard no-em-dash rule, and
        the returned prompt itself must be free of em dashes (a published
        page that reads like AI does not rank)."""
        prompt = self._build()
        assert 'never use an em dash' in prompt
        assert '—' not in prompt.replace(
            "never use an em dash (the '—' character)", '')

    def test_human_voice_grounds_and_bans_tells(self):
        prompt = self._build()
        assert 'Ground every claim' in prompt
        # a representative sample of the banned AI tells
        for tell in ('delve', 'leverage', 'cutting-edge', 'game-changer'):
            assert tell in prompt


# ─── Seed Goal Tests ───

class TestSeoSeedGoal:
    """Verify the bootstrap_seo_publisher seed entry contract."""

    def _get_goal(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        return next(g for g in SEED_BOOTSTRAP_GOALS
                    if g['slug'] == 'bootstrap_seo_publisher')

    def test_seed_goal_exists(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert 'bootstrap_seo_publisher' in slugs

    def test_slug_unique_in_seed_list(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert slugs.count('bootstrap_seo_publisher') == 1

    def test_goal_type_is_seo(self):
        assert self._get_goal()['goal_type'] == 'seo'

    def test_goal_type_is_registered(self):
        """A seed with an unregistered goal_type is silently skipped by
        seed_bootstrap_goals (create_goal rejects it), breaking the
        count == len(SEED_BOOTSTRAP_GOALS) invariant."""
        from integrations.agent_engine.goal_manager import get_registered_types
        assert self._get_goal()['goal_type'] in get_registered_types()

    def test_config_contract(self):
        cfg = self._get_goal()['config']
        assert cfg['repo'] == 'hertz-ai/Hevolve'
        assert cfg['min_seo_score'] == 90
        assert cfg['requires_consent'] is True

    def test_disabled_by_default(self):
        """Seeded dormant: enabled=False, and the prompt builder must
        decline the seed config as-is (daemon skips dispatch) until a
        human arms it."""
        from integrations.agent_engine.goal_manager import get_prompt_builder
        goal = self._get_goal()
        assert goal['config'].get('enabled') is False
        builder = get_prompt_builder('seo')
        prompt = builder({
            'title': goal['title'],
            'description': goal['description'],
            'config': goal['config'],
        })
        assert prompt is None

    def test_description_names_canonical_tools_verbatim(self):
        """The LLM uses the description as its playbook — the tool
        identifiers must be exact (same rationale as
        test_bootstrap_provision_tts_venvs)."""
        desc = self._get_goal()['description']
        for tool_name in ('seo_audit_score', 'gh_pr_open',
                          'mark_news_for_web', 'list_news_for_web'):
            assert tool_name in desc, f'description must name {tool_name!r}'
        assert 'publish_web' in desc

    def test_description_carries_pr_only_consent_language(self):
        desc = self._get_goal()['description']
        assert 'NEVER push directly' in desc
        assert 'consent gate' in desc
        assert 'never auto-publish externally' in desc.lower()

    def test_description_references_website_repo_pattern(self):
        desc = self._get_goal()['description']
        assert 'src/pages/News/newsData.js' in desc
        assert 'public/sitemap.xml' in desc
        assert 'scripts/prerender.js' in desc
        assert 'scripts/verify-mirrors.js' in desc

    def test_spark_budget_positive(self):
        assert self._get_goal()['spark_budget'] > 0


# ─── Service-tool pair wiring (registry surface) ───

class TestSeoServiceToolsWired:
    """The previously-dormant pair must be exported by the package
    __init__ AND land in the global registry after register() — the
    exact surface create_recipe/reuse_recipe expose to agents."""

    def test_both_tools_in_package_all(self):
        import integrations.service_tools as pkg
        assert 'SeoAuditTool' in pkg.__all__
        assert 'GhPrTool' in pkg.__all__

    def test_both_tools_register_into_global_registry(self):
        from integrations.service_tools import (
            service_tool_registry, SeoAuditTool, GhPrTool)
        SeoAuditTool.register()
        GhPrTool.register()
        assert 'seo_audit_score' in service_tool_registry._tools
        assert 'gh_pr_open' in service_tool_registry._tools

    def test_registered_functions_are_callable_by_agents(self):
        """get_all_tool_functions is what create_recipe/reuse_recipe hand
        to the agents — both native handlers must survive the trip."""
        from integrations.service_tools import (
            service_tool_registry, SeoAuditTool, GhPrTool)
        SeoAuditTool.register()
        GhPrTool.register()
        funcs = service_tool_registry.get_all_tool_functions()
        assert 'seo_audit_score' in funcs
        assert 'gh_pr_open' in funcs
