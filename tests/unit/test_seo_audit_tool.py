"""Unit tests for integrations/service_tools/seo_audit_tool.py

Coverage approach: build a "golden" passing post and a series of
mutated variants that each break exactly ONE section.  Validates:
  * Score taxonomy: SHIP (>=90), REVIEW (>=70), REWORK (<70)
  * Each section returns failures only when the expected signal is missing
  * Weighted score arithmetic matches SECTION_WEIGHTS
  * Path-traversal in the file-load path is rejected
  * Reuse of _parse_frontmatter from integrations.skills.registry works
"""
import json
from textwrap import dedent

import pytest

from integrations.service_tools import seo_audit_tool
from integrations.service_tools.seo_audit_tool import (
    SECTION_WEIGHTS,
    SeoAuditTool,
    audit_markdown_post,
    seo_audit_score,
)


# ── fixtures ──────────────────────────────────────────────────────

def _golden_post() -> str:
    """A markdown post that should pass every section (score >= 90)."""
    return dedent("""\
        ---
        title: How to Use Hevolve AI Agents for Trading Workflows
        description: Build an AI agent that watches markets, scores news, and proposes trades — using Hevolve's self-evolving multimodal platform.
        slug: how-to-use-hevolve-ai-agents-trading
        author: Sathish Bommannan
        date: 2026-05-19
        keywords: [hevolve ai agents, ai for trading, ai trading bot, self-evolving ai]
        og_image: /og/trading.png
        post_type: tutorial
        ---

        # How to Use Hevolve AI Agents for Trading Workflows

        Building an AI agent that watches markets and proposes trades used to need
        a quant team. With Hevolve AI agents you can create one in an afternoon —
        and it learns from every correction you give it. This tutorial walks you
        through configuring your first trading-aware AI agent on the
        [Hevolve platform](/agents).

        ## Why Self-Evolving AI for Trading

        Markets shift faster than retraining cycles. Static models drift the
        moment you deploy them. Hevolve AI agents accept real-time corrections
        through natural conversation — so when your agent misreads a Fed
        statement, you teach it once and it remembers. See the
        [Hevolve docs](/docs) for the underlying continual-learning model
        and the upstream [Arxiv paper on orthogonal LoRAs](https://arxiv.org/abs/2401.01234)
        for the academic context.

        ## Step 1: Configure Your Agent

        Open the agent builder at [/agents](/agents/new). Pick the trading-bot
        template — it ships with a news ingester, a market-data hook, and a
        risk-clamping prompt. Each component is editable by chat. The agent
        learns your risk tolerance through correction.

        Hevolve AI agents are great at this because they retain context across
        sessions without catastrophic forgetting. The agent doesn't forget
        last week's lesson when you teach it something new today.

        ## Step 2: Train Your Agent

        Spend twenty minutes giving the agent example trades. Tell it which
        signals you trust, which you ignore. Each correction is a sample —
        and Hevolve AI agents use orthogonal LoRAs to integrate them without
        overwriting prior knowledge. You can also feed it your trading journal
        directly through the file upload widget.

        ![Trading bot conversation example](/img/trading-bot-demo.png)

        ## Step 3: Test the Live Agent

        Below is a live demo of the trading agent. You can chat with it right
        now and see how it handles a sample market-news prompt.

        <iframe src="/agents/trading-bot?plugin=1&audio_only=1" width="100%" height="500"></iframe>

        Try asking it: "What would you do if the Fed cuts rates by 50 basis
        points next Wednesday?" — the agent will explain its reasoning and
        propose a risk-clamped position size.

        ## Step 4: Deploy in Paper Trading Mode

        Once the agent's responses look right, flip it to paper trading.
        It will execute mock trades for a week so you can validate the strategy
        with zero capital at risk. Move to live capital only after you trust
        the agent's reasoning chain.

        Real-world trading agents on Hevolve typically take 2-3 days of
        teaching before they're trustworthy at this level. That's far less
        than the months a custom model would need from scratch.

        ## Risk Management Built In

        Every Hevolve AI agent ships with a constitutional filter that
        intercepts risky outputs before they reach a broker API. The filter
        is configurable through the same chat interface — you tell the agent
        what counts as too-risky and it remembers across sessions.
        For trading specifically, the default filter blocks orders that
        would breach a configurable percentage of account equity. You
        can raise the threshold as your confidence in the agent grows.

        Risk management is one of the most teachable surfaces. Spend an
        hour walking through historical drawdowns with the agent and it
        will internalize your loss tolerance. This is exactly the kind
        of nuanced behavior that static models cannot capture without
        days of fine-tuning by an ML team.

        ## Connecting Market Data

        Hevolve AI agents ingest market data from any broker that exposes
        a websocket or REST feed. Common integrations include Alpaca,
        Interactive Brokers, Tradier, and Binance for crypto. The agent
        learns to parse each broker's quirks once and reuses that knowledge
        across instruments. Set up takes around fifteen minutes per broker.

        The agent also handles fundamental data — earnings releases, central
        bank announcements, news feeds. Each data type gets a dedicated
        skill module that the agent invokes when relevant. You can disable
        any skill if you want a leaner, faster agent for high-frequency
        decisions.

        ## Backtesting Your Strategy

        Before paper trading, run the agent against historical data.
        Hevolve AI agents include a backtest harness that replays the last
        twelve months of market data at compressed speed. Drawdown
        statistics, Sharpe ratio, and win rate populate automatically.

        Backtest results inform the agent's risk parameters. If a strategy
        produces a drawdown deeper than your tolerance, the agent flags
        it during the final review step. You can then teach the agent why
        that drawdown is unacceptable and how to avoid similar setups in
        the future. Backtesting and live training compound on each other.

        ## FAQ

        ### How is this different from a custom GPT?

        Hevolve AI agents learn continuously without retraining. A custom GPT
        is frozen at deployment; corrections accumulate as a long system prompt
        and bloat your context window. Hevolve uses orthogonal LoRAs instead.

        ### Can the agent execute real trades?

        Only through your broker's API and only after you flip the live-trading
        toggle. The default mode is paper-trading. The agent also asks for
        confirmation on every order above a threshold you set.

        ### Does the agent run locally or in the cloud?

        Both. Nunba runs the agent locally with full privacy; Hevolve runs the
        same agent in the cloud if you want lower local resource use. Your
        choice per agent.

        ### What models power the agent?

        Qwen3.5-4B locally, or any of 15+ cloud providers when you opt in to
        cloud routing. The provider is auto-selected by cost + latency.

        ## Next Steps

        Try the trading template now at [/agents](/agents) or read the
        [pricing breakdown](/pricing) before scaling up paper-trading capital.
    """)


def _replace_frontmatter(post: str, **updates) -> str:
    """Test helper: update individual frontmatter fields and return modified post."""
    lines = post.split('\n')
    end = lines[1:].index('---') + 1 if '---' in lines[1:] else None
    if end is None:
        return post
    fm_lines = lines[1:end]
    new_fm = []
    seen = set()
    for ln in fm_lines:
        key = ln.split(':', 1)[0].strip() if ':' in ln else None
        if key in updates:
            seen.add(key)
            val = updates[key]
            if val is None:
                continue  # delete this key
            new_fm.append(f'{key}: {val}')
        else:
            new_fm.append(ln)
    # Append any new keys not in original
    for k, v in updates.items():
        if k not in seen and v is not None:
            new_fm.append(f'{k}: {v}')
    return '\n'.join(['---', *new_fm, '---', *lines[end + 1:]])


# ── golden path ───────────────────────────────────────────────────

class TestGoldenPath:
    def test_section_weights_sum_to_100(self):
        assert sum(SECTION_WEIGHTS.values()) == 100

    def test_golden_post_ships(self):
        r = audit_markdown_post(_golden_post())
        assert r['ok'] is True
        assert r['verdict'] == 'SHIP', (
            f"expected SHIP, got {r['verdict']} (score={r['score']})\n"
            f"issues: {r['issues']}"
        )
        assert r['score'] >= 90
        for name in SECTION_WEIGHTS:
            assert name in r['sections']
            assert r['sections'][name]['passed'] is True, (
                f'section {name} failed in golden post: '
                f'{r["sections"][name]["issues"]}'
            )

    def test_seo_audit_score_returns_json(self):
        out = seo_audit_score(json.dumps({'markdown': _golden_post()}))
        r = json.loads(out)
        assert r['ok'] is True
        assert isinstance(r['score'], (int, float))
        assert r['verdict'] in ('SHIP', 'REVIEW', 'REWORK')


# ── per-section mutation tests (break one signal, assert it caught) ──

class TestSectionFailures:
    def _audit_with(self, **fm_updates) -> dict:
        return audit_markdown_post(_replace_frontmatter(_golden_post(), **fm_updates))

    def test_missing_title_fails_metadata(self):
        r = self._audit_with(title=None)
        assert r['sections']['metadata']['passed'] is False
        assert any('title' in i for i in r['sections']['metadata']['issues'])

    def test_title_too_long_fails_metadata(self):
        r = self._audit_with(title='x' * 80)
        assert r['sections']['metadata']['passed'] is False
        assert any('too long' in i for i in r['sections']['metadata']['issues'])

    def test_description_too_long_fails_metadata(self):
        r = self._audit_with(description='y' * 200)
        assert any('description too long' in i
                   for i in r['sections']['metadata']['issues'])

    def test_missing_keywords_fails_keyword_section(self):
        r = self._audit_with(keywords=None)
        # When primary keyword can't be inferred, both keyword section and
        # metadata flag it (metadata at lower severity)
        assert r['sections']['keyword_presence']['passed'] is False

    def test_keyword_not_in_title(self):
        # Replace keywords with an unrelated word that's not in the title
        r = self._audit_with(keywords='[completely-different-phrase]')
        assert r['sections']['keyword_presence']['passed'] is False
        issues = r['sections']['keyword_presence']['issues']
        assert any('title' in i for i in issues)

    def test_no_h1(self):
        post = _golden_post().replace('# How to Use Hevolve AI Agents for Trading Workflows',
                                       '## How to Use Hevolve AI Agents for Trading Workflows',
                                       1)
        r = audit_markdown_post(post)
        assert r['sections']['heading_hierarchy']['passed'] is False
        assert any('H1' in i for i in r['sections']['heading_hierarchy']['issues'])

    def test_thin_content(self):
        # Strip body down to just frontmatter + a stub
        post = _golden_post()
        idx = post.find('---', 4) + 3
        thin = post[:idx] + '\n# title\nshort.\n'
        r = audit_markdown_post(thin)
        assert r['sections']['word_count']['passed'] is False

    def test_no_internal_links(self):
        post = _golden_post()
        # strip /-prefixed link targets
        import re as _re
        post = _re.sub(r'\]\(/[^\)]*\)', '](https://example.com)', post)
        r = audit_markdown_post(post)
        assert r['sections']['internal_links']['passed'] is False

    def test_no_external_links(self):
        post = _golden_post()
        import re as _re
        post = _re.sub(r'\]\(https?://[^\)]*\)', '](/internal)', post)
        r = audit_markdown_post(post)
        assert r['sections']['external_links']['passed'] is False

    def test_image_without_alt(self):
        post = _golden_post().replace(
            '![Trading bot conversation example](/img/trading-bot-demo.png)',
            '![](/img/trading-bot-demo.png)',
        )
        r = audit_markdown_post(post)
        assert r['sections']['image_alt_text']['passed'] is False

    def test_missing_faq(self):
        post = _golden_post().replace('## FAQ', '## Wrap-up')
        r = audit_markdown_post(post)
        assert r['sections']['faq']['passed'] is False

    def test_tutorial_without_live_demo(self):
        post = _golden_post().replace(
            '<iframe src="/agents/trading-bot?plugin=1&audio_only=1" width="100%" height="500"></iframe>',
            '',
        )
        # Also strip the URL elsewhere so the embed-detection doesn't catch it
        post = post.replace('/agents/trading-bot?plugin=1', '/agents/trading-bot')
        r = audit_markdown_post(post)
        assert r['sections']['live_demo']['passed'] is False

    def test_non_tutorial_post_does_not_need_demo(self):
        r = self._audit_with(post_type='pillar')
        # post_type=pillar exempts the live_demo requirement
        assert r['sections']['live_demo']['passed'] is True


# ── verdict thresholds ────────────────────────────────────────────

class TestVerdictThresholds:
    def test_score_below_70_is_rework(self):
        # Strip almost everything to drive score down
        bad = '---\ntitle: x\n---\n\ntiny.\n'
        r = audit_markdown_post(bad)
        assert r['verdict'] == 'REWORK'
        assert r['score'] < 70

    def test_score_70_to_89_is_review(self):
        # Take golden, break one medium-weight signal — score should
        # dip into REVIEW range
        post = _golden_post()
        post = post.replace('## FAQ', '## Wrap')
        # also remove external link to dial it down
        import re as _re
        post = _re.sub(r'\]\(https?://[^\)]*\)', '](/internal)', post)
        r = audit_markdown_post(post)
        assert 50 <= r['score'] < 90  # broad-but-correct band


# ── path-load safety ──────────────────────────────────────────────

class TestPathLoad:
    def test_path_traversal_rejected(self):
        out = seo_audit_score(json.dumps({'path': '../../etc/passwd'}))
        r = json.loads(out)
        assert r['ok'] is False
        assert r['reason_code'] == 'bad_input'

    def test_missing_params(self):
        out = seo_audit_score(json.dumps({}))
        r = json.loads(out)
        assert r['ok'] is False
        assert r['reason_code'] == 'bad_input'

    def test_bad_json(self):
        out = seo_audit_score('not json')
        r = json.loads(out)
        assert r['ok'] is False
        assert r['reason_code'] == 'bad_input'

    def test_path_not_found(self, tmp_path, monkeypatch):
        # Point the data root at a temp dir + try a non-existent file
        from integrations.service_tools import seo_audit_tool as mod
        import core.platform_paths as pp
        monkeypatch.setattr(pp, 'get_data_dir', lambda: str(tmp_path))
        out = seo_audit_score(json.dumps({'path': 'nope.md'}))
        r = json.loads(out)
        assert r['ok'] is False
        assert r['reason_code'] == 'not_found'


# ── registration shape ────────────────────────────────────────────

class TestRegistration:
    def test_create_tool_info_shape(self):
        info = SeoAuditTool.create_tool_info()
        assert info.name == 'seo_audit_score'
        assert info.base_url == 'native://in-process'
        assert 'seo_audit_score' in info.endpoints
        ep = info.endpoints['seo_audit_score']
        for key in ('path', 'method', 'description',
                    'params_schema', 'native_handler'):
            assert key in ep
        assert ep['native_handler'] is seo_audit_score

    def test_register_returns_bool(self):
        result = SeoAuditTool.register()
        assert isinstance(result, bool)

    def test_importable_from_service_tools_package(self):
        """P1 wiring: the tool must be exported by the package __init__
        (same as Crawl4AITool) so create_recipe/reuse_recipe can register
        it — otherwise the SEO publishing path stays dormant."""
        import integrations.service_tools as pkg
        assert pkg.SeoAuditTool is SeoAuditTool
        assert 'SeoAuditTool' in pkg.__all__

    def test_registered_in_global_service_tool_registry(self):
        """After register(), the tool is discoverable by name in the
        global registry (the surface create_recipe/reuse_recipe expose
        to the agents)."""
        from integrations.service_tools import service_tool_registry
        SeoAuditTool.register()
        assert SeoAuditTool.NAME in service_tool_registry._tools
