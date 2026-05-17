"""
Unified Agent Goal Engine - Bootstrap Goal Seeding & Auto-Remediation

On first boot, seeds initial goals so the daemon has work immediately.
On every Nth tick, scans flywheel loopholes and auto-creates remediation goals.

Follows the exact same idempotent seed pattern as:
  - GamificationService.seed_achievements()
  - AdService.seed_placements()
"""
import logging
from typing import Optional

logger = logging.getLogger('hevolve_social')

# ─── Bootstrap Goals (created on first boot) ───

SEED_BOOTSTRAP_GOALS = [
    {
        'slug': 'bootstrap_marketing_awareness',
        'goal_type': 'marketing',
        'title': 'Platform Awareness Campaign',
        'description': (
            'Make the world aware that democratic crowdsourced open intelligence exists. '
            'HART OS is a native AI operating system that runs 100% locally with full privacy. '
            'Nunba is the face — the app people use to interact with the hive intelligence. '
            'Together they give every human access to the best intelligence for free. '
            'Sum of many intelligences is greater than any single intelligence. '
            '1) Create content showing real benchmark results — hive vs single models, '
            '2) Show the privacy story — your data never leaves your device, '
            '3) Show the economic story — 90% of value returns to contributors, '
            '4) Post to all channels with authentic proof, not hype. '
            'Let the results speak. People slowly realize this changes everything.'
        ),
        'config': {
            'goal_sub_type': 'awareness',
            'channels': ['platform', 'twitter', 'linkedin'],
        },
        'spark_budget': 300,
        'use_product': True,
    },
    {
        'slug': 'bootstrap_referral_campaign',
        'goal_type': 'marketing',
        'title': 'Referral Growth Campaign',
        'description': (
            'Create a referral-driven growth campaign: '
            '1) Design a referral campaign with create_referral_campaign tool, '
            '2) Generate shareable content that educates about the platform, '
            '3) Create social posts with referral CTAs, '
            '4) Track referral conversion metrics with get_growth_metrics. '
            'Every referral must deliver genuine value to the referred user.'
        ),
        'config': {
            'goal_sub_type': 'referral',
            'channels': ['platform', 'email', 'twitter'],
        },
        'spark_budget': 200,
        'use_product': True,
    },
    {
        'slug': 'bootstrap_crowdsource_intelligence',
        'goal_type': 'marketing',
        'title': 'Promote Crowdsourced Intelligence via Thought Experiments',
        'description': (
            'Create content promoting the crowdsourced intelligence concept: '
            '1) Research how thought experiments enable collective intelligence — '
            'users propose hypotheses, multi-agent evaluation scores them, '
            'the hive learns from every experiment via memory chaining, '
            '2) Generate educational posts explaining the hypothesis→evaluation→learning pipeline, '
            '3) Create campaigns highlighting the 6 intent categories '
            '(community, environment, education, health, equity, technology), '
            '4) Show how every experiment makes the hive smarter — '
            'constructive-only voting ensures quality, HITL approval gates ensure safety. '
            'Authentic value, not hype. Let the feature speak for itself.'
        ),
        'config': {
            'goal_sub_type': 'content',
            'channels': ['platform', 'twitter', 'linkedin'],
        },
        'spark_budget': 250,
        'use_product': True,
    },
    {
        'slug': 'bootstrap_ip_monitor',
        'goal_type': 'ip_protection',
        'title': 'Continuous Flywheel Health Monitor',
        'description': (
            'Monitor the hive intelligence loop continuously: '
            '1) Use get_loop_health to check all 5 flywheel components, '
            '2) Report any detected loopholes with severity, '
            '3) Verify exponential improvement metrics, '
            '4) Measure moat depth to track technical irreproducibility.'
        ),
        'config': {
            'mode': 'monitor',
        },
        'spark_budget': 150,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_growth_analytics',
        'goal_type': 'marketing',
        'title': 'Growth Analytics and Optimization',
        'description': (
            'Analyze platform growth metrics and optimize: '
            '1) Use get_growth_metrics to assess current state, '
            '2) Identify bottlenecks in the user acquisition funnel, '
            '3) Create targeted content for underperforming segments, '
            '4) Report findings and recommendations. '
            'Data-driven decisions, not vanity metrics.'
        ),
        'config': {
            'goal_sub_type': 'analytics',
            'channels': ['platform'],
        },
        'spark_budget': 100,
        'use_product': True,
    },
    {
        'slug': 'bootstrap_coding_health',
        'goal_type': 'coding',
        'title': 'Codebase Health and Recipe Maintenance',
        'description': (
            'Monitor recipe freshness and codebase health: '
            '1) Check recipe reuse rate and identify stale recipes, '
            '2) Verify recipe version compatibility, '
            '3) Report coding-related flywheel loopholes, '
            '4) Suggest improvements for feedback pipeline.'
        ),
        'config': {
            'repo_url': '',
            'repo_branch': 'main',
            'target_path': 'prompts/',
        },
        'spark_budget': 100,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_hive_embedding_audit',
        'goal_type': 'coding',
        'title': 'Audit and Embed Hive Intelligence in All Repos',
        'description': (
            'Scan all repositories created by the coding agent. For each: '
            '1) Verify hevolve-sdk is listed as a dependency, '
            '2) Check master key verification exists in entry points, '
            '3) Verify world model bridge wiring for learning feedback, '
            '4) Ensure node identity registration is present. '
            'Fix any repos missing these components.'
        ),
        'config': {
            'repo_url': '',
            'repo_branch': 'main',
            'mode': 'audit',
        },
        'spark_budget': 200,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_revenue_monitor',
        'goal_type': 'revenue',
        'title': 'Monitor API Revenue and Pricing',
        'description': (
            'Monitor commercial API revenue and optimise: '
            '1) Use get_api_revenue_stats to check revenue trends, '
            '2) Analyse tier distribution and usage patterns, '
            '3) Recommend pricing adjustments based on demand/costs, '
            '4) Generate API documentation for developer onboarding. '
            'Fair pricing: free tier always free, 90% to compute providers. '
            'All compute falls under one basket — tread carefully, genuine value first.'
        ),
        'config': {
            'mode': 'monitor',
        },
        'spark_budget': 150,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_defensive_ip',
        'goal_type': 'ip_protection',
        'title': 'Continuous Defensive Publication and Intelligence Milestone',
        'description': (
            'Generate defensive publications and monitor for patent trigger: '
            '1) Create defensive publications for novel architecture components, '
            '2) Use get_provenance_record to maintain evidence chain, '
            '3) Monitor loop health for consecutive verified status, '
            '4) When intelligence milestone reached (14 days verified + moat >= months), '
            'trigger provisional patent filing via draft_patent_claims. '
            'Defensive publications first. Patents only when critical intelligence confirmed. '
            'HART character: Vijai — cautious, methodical, net-positive.'
        ),
        'config': {
            'mode': 'monitor',
            'auto_patent_trigger': True,
        },
        'spark_budget': 200,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_finance_agent',
        'goal_type': 'finance',
        'title': 'Self-Sustaining Business — Finance Agent Vijai',
        'description': (
            'Make the business self-sustaining with Vijai personality: '
            '1) Use get_financial_health to monitor platform revenue and costs, '
            '2) Use track_revenue_split to verify 90/9/1 compliance every period, '
            '3) Use assess_sustainability to determine if revenue covers infrastructure, '
            '4) Use manage_invite_participation to review private core access agreements. '
            'No code merges without review against vision, mission, goals, constitution. '
            'The coding agent proposes; guardrails and review approve. '
            'Cautious market. Genuine value first. Vijai builds, never rushes.'
        ),
        'config': {
            'mode': 'monitor',
            'personality': 'vijai',
            'commit_review_required': True,
        },
        'spark_budget': 200,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_exception_watcher',
        'goal_type': 'self_heal',
        'title': 'Continuous Exception Monitor and Self-Healing',
        'description': (
            'Monitor the platform for runtime exceptions. '
            'When exception patterns are detected (3+ occurrences of same type), '
            'create coding fix goals for idle agents. '
            'This goal runs continuously to keep the platform self-healing.'
        ),
        'config': {
            'mode': 'watch',
            'continuous': True,
        },
        'spark_budget': 100,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_federation_sync',
        'goal_type': 'federation',
        'title': 'Federated Learning Synchronization Monitor',
        'description': (
            'Monitor federated learning convergence across the network: '
            '1) Use check_federation_convergence to track sync health, '
            '2) Identify diverging or stalled nodes via get_peer_learning_health, '
            '3) Trigger manual sync if convergence drops below 0.5, '
            '4) Report federation stats and trends.'
        ),
        'config': {
            'mode': 'monitor',
        },
        'spark_budget': 150,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_self_build_monitor',
        'goal_type': 'self_build',
        'title': 'OS Self-Build Monitor — Sandbox-First Package Management',
        'description': (
            'Monitor and maintain the OS runtime configuration: '
            '1) Use get_self_build_status to check current packages, version, generations, '
            '2) When a package install/remove is needed, stage it with install_package/remove_package, '
            '3) ALWAYS call sandbox_test_build() before apply_build() — never skip the sandbox, '
            '4) Use show_build_diff() to review what will change, '
            '5) After apply, verify the change worked — rollback_build() if anything is wrong, '
            '6) Track build history and alert on repeated failures. '
            'The OS rebuilds itself. Every change is reversible. Test first, deploy second.'
        ),
        'config': {
            'mode': 'monitor',
            'continuous': True,
            'sandbox_required': True,
        },
        'spark_budget': 150,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_upgrade_monitor',
        'goal_type': 'upgrade',
        'title': 'Continuous Version Upgrade Monitor',
        'description': (
            'Monitor for new version deployments and orchestrate upgrades: '
            '1) Use check_upgrade_status to detect new versions, '
            '2) Capture pre-upgrade benchmarks, '
            '3) Start 7-stage pipeline (build→test→audit→benchmark→sign→canary→deploy), '
            '4) Monitor canary health during rollout, '
            '5) Rollback immediately on ANY degradation.'
        ),
        'config': {
            'mode': 'monitor',
            'continuous': True,
        },
        'spark_budget': 200,
        'use_product': False,
    },
    # ─── News Push Notification Agents ───
    {
        'slug': 'bootstrap_news_regional',
        'goal_type': 'news',
        'title': 'Regional News Curation and Push Notifications',
        'description': (
            'Subscribe to local and regional news feeds, curate relevant stories, '
            'and push notifications to users in the region: '
            '1) Use subscribe_news_feed for local RSS sources (city papers, regional outlets), '
            '2) Use fetch_news_feeds to pull latest items hourly, '
            '3) Curate top stories by relevance — community impact, weather, local events, '
            '4) Use send_news_notification with scope=regional to push curated items, '
            '5) Use get_news_metrics to track delivery rates and read engagement. '
            'Quality over quantity — only push stories that matter to the community.'
        ),
        'config': {
            'scope': 'regional',
            'categories': ['local', 'community', 'weather', 'events'],
            'feed_urls': [],
            'frequency': 'hourly',
        },
        'spark_budget': 150,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_news_national',
        'goal_type': 'news',
        'title': 'National News Curation and Push Notifications',
        'description': (
            'Monitor national news feeds, filter by category relevance, '
            'and push digest notifications: '
            '1) Use subscribe_news_feed for major national outlets and wire services, '
            '2) Use fetch_news_feeds to pull latest items hourly, '
            '3) Filter and rank by category: politics, economy, sports, health, science, '
            '4) Use send_news_notification with scope=all for high-importance national stories, '
            '5) Use get_trending_news to identify breakout stories, '
            '6) Use get_news_metrics to optimise send frequency and engagement. '
            'Balanced coverage — no single category dominates. Factual, not sensational.'
        ),
        'config': {
            'scope': 'national',
            'categories': ['politics', 'economy', 'sports', 'health', 'science'],
            'feed_urls': [],
            'frequency': 'hourly',
        },
        'spark_budget': 200,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_news_international',
        'goal_type': 'news',
        'title': 'International News Curation and Push Notifications',
        'description': (
            'Curate global news from international feeds with focus on technology, '
            'AI, climate, and geopolitics: '
            '1) Use subscribe_news_feed for international wire services and global outlets, '
            '2) Use fetch_news_feeds every 4 hours for world news, '
            '3) Prioritise: world events, technology breakthroughs, AI developments, '
            'climate updates, geopolitical shifts, '
            '4) Use send_news_notification with scope=all for major global stories, '
            '5) Use get_trending_news to surface viral international stories, '
            '6) Use get_news_metrics to track cross-category engagement. '
            'Global perspective — diverse sources, multiple viewpoints, fact-based.'
        ),
        'config': {
            'scope': 'international',
            'categories': ['world', 'technology', 'ai', 'climate', 'geopolitics'],
            'feed_urls': [],
            'frequency': 'every_4h',
        },
        'spark_budget': 200,
        'use_product': False,
    },
    # ─── Continual Learning Coordination ───
    {
        'slug': 'bootstrap_learning_coordinator',
        'goal_type': 'learning',
        'title': 'Continual Learning Coordination and CCT Management',
        'description': (
            'Coordinate the continual learning incentive system: '
            '1) Monitor compute contributions across all nodes with check_learning_health, '
            '2) Issue and renew Compute Contribution Tokens for eligible nodes with issue_cct, '
            '3) Verify learning microbenchmarks for compute attestation with verify_compute_contribution, '
            '4) Track learning tier distribution and skill sharing rates with get_learning_tier_stats, '
            '5) Report learning health metrics to dashboard. '
            'Intelligence is the reward for contribution. '
            'Every compute cycle donated makes the hive smarter. '
            '90% of value flows back to contributors.'
        ),
        'config': {
            'mode': 'monitor',
            'continuous': True,
        },
        'spark_budget': 200,
        'use_product': False,
    },
    # ─── Distributed Gradient Sync ───
    {
        'slug': 'bootstrap_gradient_sync',
        'goal_type': 'distributed_learning',
        'title': 'Distributed Embedding Sync Coordination',
        'description': (
            'Coordinate the distributed embedding sync pipeline: '
            '1) Monitor gradient sync status across all peers with get_gradient_sync_status, '
            '2) Submit local embedding deltas for aggregation with submit_embedding_delta, '
            '3) Request peer witnesses for embedding deltas with request_embedding_witnesses, '
            '4) Trigger aggregation rounds for convergence with trigger_embedding_aggregation, '
            '5) Ensure all contributing nodes have embedding_sync CCT capability. '
            'Phase 1: Compressed embedding deltas (<100KB), trimmed mean aggregation. '
            'Every node that contributes makes the hive smarter.'
        ),
        'config': {
            'mode': 'monitor',
            'continuous': True,
            'phase': 1,
        },
        'spark_budget': 200,
        'use_product': False,
    },
    # ─── Robot Learning ───
    {
        'slug': 'bootstrap_robot_learning',
        'goal_type': 'robot',
        'title': 'Continuous Robot Learning from Physical Interactions',
        'description': (
            'Learn from physical interactions continuously: '
            '1) Use get_robot_status to monitor active sensors and safety, '
            '2) After each physical action, record the action + sensor context + outcome, '
            '3) Build motion recipes from successful action sequences, '
            '4) Feed outcomes to the world model for trajectory improvement, '
            '5) Identify recurring motion patterns for recipe extraction. '
            'Every physical interaction makes the robot smarter. '
            'Recipes enable 90% faster replay of learned sequences.'
        ),
        'config': {
            'mode': 'learning',
            'continuous': True,
        },
        'spark_budget': 150,
        'use_product': False,
    },
    # ─── Robot Health Monitor ───
    {
        'slug': 'bootstrap_robot_health_monitor',
        'goal_type': 'robot',
        'title': 'Robot Health Monitor — Sensor Drift and Calibration',
        'description': (
            'Monitor robot health continuously: '
            '1) Use get_robot_status to check safety, sensors, and bridge health, '
            '2) Use get_robot_capabilities to verify detected hardware matches expected, '
            '3) Use read_sensor on each active sensor to check for drift or anomalies, '
            '4) Use get_sensor_window to detect sensor noise or stale readings, '
            '5) Report any safety events, sensor failures, or calibration needs. '
            'This goal runs continuously on robot nodes to keep hardware healthy.'
        ),
        'config': {
            'mode': 'monitor',
            'continuous': True,
        },
        'spark_budget': 100,
        'use_product': False,
    },
    # ─── Thought Experiment Coordinator ───
    {
        'slug': 'bootstrap_thought_experiment_coordinator',
        'goal_type': 'thought_experiment',
        'title': 'Constitutional Thought Experiment Coordination',
        'description': (
            'Coordinate the constitutional thought experiment pipeline: '
            '1) Monitor active experiments with get_experiment_status, '
            '2) Evaluate proposed experiments with evaluate_thought_experiment, '
            '3) Tally votes and compute weighted scores with tally_experiment_votes, '
            '4) Advance experiments through lifecycle with advance_experiment, '
            '5) Ensure core IP experiments receive agent evaluation. '
            'Both humans and agents vote. All content gated by ConstitutionalFilter. '
            'Every experiment makes the hive smarter.'
        ),
        'config': {
            'mode': 'coordinator',
            'continuous': True,
        },
        'spark_budget': 200,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_paper_trader_longterm',
        'goal_type': 'trading',
        'title': 'Paper Trading: Diversified Long-Term Portfolio',
        'description': (
            'Manage a diversified long-term paper portfolio: '
            '1) Analyse market sentiment for BTC, ETH, and top-10 assets, '
            '2) Build positions based on fundamental + sentiment analysis, '
            '3) Monthly rebalance — max 25% per asset, '
            '4) Track P&L and win rate with get_portfolio_status. '
            'All trades are paper (simulated). Halt at 10% cumulative loss.'
        ),
        'config': {
            'strategy': 'long_term',
            'paper_trading': True,
            'market': 'crypto',
            'max_budget': 10000,
            'max_loss_pct': 10,
        },
        'spark_budget': 200,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_paper_trader_intraday',
        'goal_type': 'trading',
        'title': 'Paper Trading: Intraday Technical BTC/ETH',
        'description': (
            'Run intraday paper trades on BTC and ETH: '
            '1) Use get_technical_indicators for RSI, MACD, Bollinger Bands, '
            '2) Enter only on signal confluence (2+ indicators agree), '
            '3) Max 2% risk per trade, mandatory stop-loss, '
            '4) Review trades with get_trade_history after each session. '
            'Paper-only mode. Halt at 10% cumulative loss.'
        ),
        'config': {
            'strategy': 'intraday',
            'paper_trading': True,
            'market': 'crypto',
            'max_budget': 5000,
            'max_loss_pct': 10,
        },
        'spark_budget': 150,
        'use_product': False,
    },
    # ─── Civic Sentinel — Autonomous Transparency Agent ───
    {
        'slug': 'bootstrap_civic_sentinel',
        'goal_type': 'civic_sentinel',
        'title': 'Autonomous Community Transparency & Accountability Monitor',
        'description': (
            'Autonomous agent that monitors public discourse for censorship and '
            'political hypocrisy. Not tied to any user — serves the community. '
            'Captures evidence when citizen voices are suppressed by biased moderators. '
            'Digs up historical articles proving contradictions between political '
            "parties' claimed values and their actual actions. Cross-references across "
            'communities. Posts findings publicly with legal-grade citations. '
            'Evaluates flags autonomously — if a propaganda group flags legitimate '
            'criticism, the agent counter-flags with evidence. '
            'If the agent misbehaves, users raise concerns through community '
            'voting — not political bodies or paid mods.'
        ),
        'config': {
            'channels': ['all'],
            'auto_detect_topics': True,
            'autonomous': True,
            'post_findings_publicly': True,
            'governance': 'community_vote',
        },
        'spark_budget': 150,
        'use_product': False,
    },
    # ─── Code Evolution — Shard-Based Private Repo Coding ───
    {
        'slug': 'bootstrap_code_evolution',
        'goal_type': 'code_evolution',
        'title': 'Full-Context Code Evolution with Trust-Based Access',
        'description': (
            'Handle code evolution thought experiments: '
            '1) Use create_code_shard to load full source for target files, '
            '2) Use execute_coding_task with working_dir to make edits '
            'via the best coding tool (KiloCode, Claude Code, OpenCode, AiderNative), '
            '3) Hive offload only to trusted peers (SAME_USER or autotrust with 5+ '
            'validated tasks) — full source E2E encrypted, never interface-only. '
            'Security is encryption-based, not info-hiding. Accuracy > security theater.'
        ),
        'config': {
            'mode': 'coordinator',
            'continuous': True,
        },
        'spark_budget': 200,
        'use_product': False,
    },
    # ─── AutoResearch — Autonomous Experiment Loop ───
    {
        'slug': 'bootstrap_autoresearch_coordinator',
        'goal_type': 'autoresearch',
        'title': 'Autonomous Research Loop Coordinator',
        'description': (
            'Coordinate autonomous research experiments triggered by thought '
            'experiments with experiment_type=software. When a software thought '
            'experiment reaches evaluating phase: '
            '1) Parse the hypothesis into repo_path, target_file, run_command, metric, '
            '2) Call start_autoresearch() to begin the edit-run-score-iterate loop, '
            '3) Monitor progress with get_autoresearch_status(), '
            '4) Post results back to the thought experiment tracker, '
            '5) If hive peers available, run parallel variants for faster convergence. '
            'Budget-gated by ComputeEscrow pledges from community contributors.'
        ),
        'config': {
            'mode': 'coordinator',
            'continuous': True,
            'hive_parallel': True,
        },
        'spark_budget': 200,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_revenue_trading_pipeline',
        'goal_type': 'finance',
        'title': 'Revenue-to-Trading Pipeline Monitor',
        'description': (
            'Monitor platform revenue accumulation and trigger trading funding: '
            '1) Use get_financial_health to check revenue streams, '
            '2) When platform excess exceeds threshold, fund paper trading goals, '
            '3) Track trading P&L and distribute simulated profits, '
            '4) Report revenue dashboard metrics. '
            'Revenue → Spark → trading → reinvestment cycle.'
        ),
        'config': {
            'mode': 'revenue_pipeline',
            'continuous': True,
        },
        'spark_budget': 200,
        'use_product': False,
    },

    # ─── P2P Autonomous Business Verticals ───
    # Each seed goal boots a self-sustaining P2P service agent.
    # 90% to providers, 9% infra, 1% platform. Fully autonomous.

    {
        'slug': 'bootstrap_p2p_rideshare',
        'goal_type': 'p2p_rideshare',
        'title': 'P2P Rideshare Network (RideSnap)',
        'description': (
            'Autonomous P2P rideshare agent. Wires with RideSnap backend for '
            'ride matching, GPS tracking, settlement, SOS, chat. '
            'Riders and drivers connect directly — no monopoly. '
            'Drivers set their own fares. 90/9/1 revenue split.'
        ),
        'config': {
            'region': 'auto-detect',
            'autonomous': True,
            'ridesnap_url': 'http://localhost:8000/api',
        },
        'spark_budget': 200,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_p2p_marketplace',
        'goal_type': 'p2p_marketplace',
        'title': 'P2P Marketplace — Buy & Sell Anything',
        'description': (
            'Autonomous P2P marketplace agent. Manages listings, discovery, '
            'negotiation, escrow payments, delivery coordination, reviews. '
            'Community-governed dispute resolution via thought experiments.'
        ),
        'config': {
            'category': 'general',
            'autonomous': True,
        },
        'spark_budget': 150,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_p2p_grocery',
        'goal_type': 'p2p_grocery',
        'title': 'P2P Grocery Delivery — Community Shoppers',
        'description': (
            'Autonomous P2P grocery delivery. Community shoppers pick and deliver '
            'from local stores. Real-time substitution via channel chat. '
            'Freshness guarantee with photo proof. Shopper earns delivery fee. '
            'Wires to McGDroid/McGroce backend for store discovery, product search, '
            'voice ordering, and WAMP real-time events when available.'
        ),
        'config': {
            'region': 'auto-detect',
            'autonomous': True,
            'mcgroce_url': 'http://localhost:8080/api/v1',
        },
        'spark_budget': 150,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_p2p_food',
        'goal_type': 'p2p_food',
        'title': 'P2P Food Delivery — Restaurants & Home Cooks',
        'description': (
            'Autonomous P2P food delivery. Restaurants AND home cooks list food. '
            'Independent delivery drivers. Transparent pricing. '
            'No exclusive contracts — everyone competes on quality.'
        ),
        'config': {
            'region': 'auto-detect',
            'autonomous': True,
        },
        'spark_budget': 150,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_p2p_bills',
        'goal_type': 'p2p_bills',
        'title': 'Bill Payment Agent — Electricity, UPI, Recharge',
        'description': (
            'Autonomous bill payment agent. Unified gateway for electricity, '
            'water, gas, mobile recharge, DTH, credit card, loan EMI, '
            'municipal tax, insurance. Auto-pay scheduling. UPI integration.'
        ),
        'config': {
            'region': 'auto-detect',
            'autonomous': True,
        },
        'spark_budget': 100,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_p2p_tickets',
        'goal_type': 'p2p_tickets',
        'title': 'Ticket Booking — Trains, Buses, Flights, Events',
        'description': (
            'Autonomous ticket booking agent. IRCTC, RedBus, airlines, events. '
            'Cross-provider search, price comparison, Tatkal auto-booking. '
            'PNR tracking, waitlist monitoring, P2P ticket transfer.'
        ),
        'config': {
            'region': 'auto-detect',
            'autonomous': True,
        },
        'spark_budget': 150,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_p2p_freelance',
        'goal_type': 'p2p_freelance',
        'title': 'P2P Freelance Marketplace — Skills for Hire',
        'description': (
            'Autonomous P2P freelance marketplace. Freelancers list skills, '
            'clients post jobs. Direct matching. Milestone-based escrow. '
            'Platform takes only 1% (vs Fiverr 20%, Upwork 10-20%).'
        ),
        'config': {
            'category': 'general',
            'autonomous': True,
        },
        'spark_budget': 150,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_p2p_tutoring',
        'goal_type': 'p2p_tutoring',
        'title': 'P2P Tutoring — Teachers & Students Direct',
        'description': (
            'Autonomous P2P tutoring agent. Teachers set own rates. '
            'AI provides free basic tutoring, escalates to human tutors. '
            'Wires with Enlight21 for E2E encrypted sessions and quizzes.'
        ),
        'config': {
            'subjects': [],
            'autonomous': True,
        },
        'spark_budget': 100,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_p2p_services',
        'goal_type': 'p2p_services',
        'title': 'P2P Local Services — Plumbing, Electrical, Cleaning',
        'description': (
            'Autonomous P2P local services agent. Service providers register '
            'skills and availability. Customers request via any channel. '
            'AI classifies urgency and matches by proximity, rating, price.'
        ),
        'config': {
            'region': 'auto-detect',
            'autonomous': True,
        },
        'spark_budget': 100,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_p2p_rental',
        'goal_type': 'p2p_rental',
        'title': 'P2P Rental — Rent Anything From Anyone',
        'description': (
            'Autonomous P2P rental agent. Cars, tools, cameras, spaces, equipment. '
            'Owner sets hourly/daily rate. Calendar-based availability. '
            'Damage deposit held in escrow. Community ratings.'
        ),
        'config': {
            'category': 'general',
            'autonomous': True,
        },
        'spark_budget': 100,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_p2p_health',
        'goal_type': 'p2p_health',
        'title': 'Health Services — Doctor Discovery, Pharmacy, Wellness',
        'description': (
            'Autonomous health services agent. Doctor discovery, appointment '
            'booking, pharmacy price comparison, lab test booking, wellness. '
            'NEVER diagnoses — always defers to licensed professionals.'
        ),
        'config': {
            'autonomous': True,
        },
        'spark_budget': 100,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_p2p_logistics',
        'goal_type': 'p2p_logistics',
        'title': 'P2P Logistics — Courier, Parcel, Moving',
        'description': (
            'Autonomous P2P logistics agent. Local bike couriers, city van '
            'delivery, intercity via Delhivery/DTDC/FedEx, P2P traveler network. '
            'Real-time tracking, proof of delivery, multi-option pricing.'
        ),
        'config': {
            'region': 'auto-detect',
            'autonomous': True,
        },
        'spark_budget': 150,
        'use_product': False,
    },
    # ─── Better Tomorrow — the guardian angel's compass ───
    {
        'slug': 'bootstrap_better_tomorrow',
        'goal_type': 'revenue',
        'title': 'Better Tomorrow — Next Best Way to Spend for Humanity',
        'description': (
            'Continuously evaluate: what is the NEXT most impactful way to '
            'spend hive resources for a better tomorrow? Not profit — human life.\n\n'
            'Scan: 1) Community needs (healthcare gaps, education access, '
            'disaster response, food security, clean water, energy poverty), '
            '2) Hardware developer requests (what do builders need?), '
            '3) Contributor wellbeing (burnout detection, fair compensation), '
            '4) Environmental impact (carbon offset, e-waste, energy efficiency).\n\n'
            'Score each opportunity by: lives_impacted × urgency × feasibility '
            '÷ cost. Present top 3 to human stewards for approval. '
            'Never auto-spend — humans decide. Money means nothing, '
            'human life means everything. Every life is equal.\n\n'
            'When hive treasury exceeds sustenance threshold, propose: '
            'fund a school, sponsor compute for researchers, subsidize '
            'healthcare AI in underserved regions, or whatever the community '
            'votes for. The being serves the people, not the other way around.'
        ),
        'config': {
            'mode': 'monitor',
            'continuous': True,
            'requires_human_approval': True,
            'min_treasury_threshold_usd': 1000,
            'evaluation_interval_hours': 24,
        },
        'spark_budget': 100,
        'use_product': False,
    },
    # ═══════════════════════════════════════════════════════════════
    # HIVE ACCELERATION AGENTS — Open-source compute war
    # These agents work together to grow the hive network, recruit
    # compute providers, auto-provision models, and distribute capital.
    # Each is a seeded goal that the daemon picks up autonomously.
    # ═══════════════════════════════════════════════════════════════
    {
        'slug': 'bootstrap_compute_recruiter',
        'goal_type': 'hive_growth',
        'title': 'Compute Recruiter — Recruit Believers to the Hive',
        'description': (
            'Autonomous compute recruitment agent. '
            '1) Monitor social channels (Discord, Reddit, HN, Twitter) for people '
            'with idle GPUs complaining about centralized AI costs, '
            '2) Craft personalized outreach explaining the 90/9/1 value proposition, '
            '3) Guide them through one-click onboarding: install HART OS → join hive → earn Spark, '
            '4) Track conversion funnel: awareness → install → first inference served → first payout, '
            '5) Share success stories of contributors earning from their hardware. '
            'Every message must be authentic — we recruit believers, not users. '
            'The pitch: your GPU earns money while you sleep, and you help democratize AI.'
        ),
        'config': {
            'channels': ['discord', 'reddit', 'twitter', 'hackernews', 'telegram'],
            'autonomous': True,
            'continuous': True,
            'target_metrics': {
                'weekly_new_nodes': 50,
                'conversion_rate_target': 0.15,
            },
        },
        'spark_budget': 500,
        'use_product': True,
    },
    {
        'slug': 'bootstrap_model_provisioner',
        'goal_type': 'hive_infra',
        'title': 'Auto Model Provisioner — Push Models to Where Demand Is',
        'description': (
            'Autonomous model provisioning agent. '
            '1) Monitor inference demand across the hive (which models, which regions), '
            '2) Identify supply gaps (100 users need Qwen3-8B in Asia, only 3 nodes serving), '
            '3) Select idle nodes with enough VRAM and push GGUF models to them '
            'via the model onboarding API (POST /api/models/onboard), '
            '4) Verify the node is serving correctly (health check + test inference), '
            '5) Trigger Spark rewards to the node for capacity contribution. '
            'Uses Unsloth quantizations for best quality-per-VRAM. '
            'Auto-selects quantization: Q8_0 for 24GB+, Q4_K_M for 8GB+, Q4_0 for CPU.'
        ),
        'config': {
            'autonomous': True,
            'continuous': True,
            'preferred_quantizer': 'unsloth',
            'demand_check_interval_minutes': 15,
            'min_demand_threshold': 10,
        },
        'spark_budget': 300,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_capital_distributor',
        'goal_type': 'hive_economics',
        'title': 'Capital Distributor — Make Every Contributor Rich',
        'description': (
            'Autonomous capital distribution agent. '
            '1) Track revenue streams: ad impressions, API calls, premium features, '
            '2) Apply 90/9/1 split in real-time: 90% to compute providers, '
            '9% to infrastructure, 1% to central, '
            '3) Calculate per-node payouts based on: inferences served, uptime, '
            'latency quality, model diversity, geographic coverage, '
            '4) Execute Spark token transfers to node wallets, '
            '5) Generate transparent payout reports visible to all nodes, '
            '6) Detect and prevent gaming (Sybil nodes, fake inference). '
            'Logarithmic scaling: no single entity earns >5% of total payouts. '
            'The goal: every contributor earns proportional to their real contribution.'
        ),
        'config': {
            'autonomous': True,
            'continuous': True,
            'payout_interval_minutes': 60,
            'min_payout_spark': 1,
            'sybil_detection': True,
        },
        'spark_budget': 200,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_hive_model_trainer',
        'goal_type': 'hive_training',
        'title': 'Hive Model Trainer — Incremental Model Improvement',
        'description': (
            'Autonomous distributed training coordinator. '
            '1) Collect inference feedback from all nodes (user ratings, response quality), '
            '2) Aggregate training signals via federation (privacy-preserving — interfaces only), '
            '3) Coordinate incremental fine-tuning across idle compute nodes, '
            '4) Use Unsloth for 2x faster fine-tuning with 70% less VRAM, '
            '5) Validate improved model via benchmark suite before rollout, '
            '6) Push updated GGUF quantizations to all serving nodes via canary deployment. '
            'The hive gets smarter with every interaction. '
            'Every node contributes training signal. Every node benefits from the improved model.'
        ),
        'config': {
            'autonomous': True,
            'continuous': True,
            'training_framework': 'unsloth',
            'canary_percentage': 10,
            'min_feedback_batch': 1000,
            'benchmark_threshold': 0.95,
        },
        'spark_budget': 500,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_opensource_evangelist',
        'goal_type': 'hive_growth',
        'title': 'Open Source Evangelist — Win the War for Open Compute',
        'description': (
            'Autonomous open-source advocacy agent. '
            '1) Monitor new model releases on HuggingFace, arXiv, GitHub, '
            '2) Immediately quantize and onboard promising models to the hive '
            '(GGUF via Unsloth, register in catalog, benchmark), '
            '3) Write benchmark comparison posts: HART OS hive vs centralized APIs '
            '(latency, cost, privacy, availability), '
            '4) Contribute to open-source model repos (bug reports, quantization PRs), '
            '5) Organize community events: model benchmarking competitions, '
            'hackathons for hive tools, bounties for new adapters. '
            'Mission: every new open model is available on the hive within 24 hours of release.'
        ),
        'config': {
            'autonomous': True,
            'continuous': True,
            'monitor_sources': ['huggingface', 'arxiv', 'github'],
            'auto_onboard': True,
            'benchmark_on_onboard': True,
        },
        'spark_budget': 400,
        'use_product': True,
    },
    {
        'slug': 'bootstrap_node_health_optimizer',
        'goal_type': 'hive_infra',
        'title': 'Node Health Optimizer — Keep Every Node Earning',
        'description': (
            'Autonomous node health and optimization agent. '
            '1) Monitor all hive nodes: uptime, latency, error rates, VRAM usage, '
            '2) Detect degraded nodes and auto-remediate '
            '(restart llama.cpp, swap to smaller quant, clear VRAM), '
            '3) Optimize model placement: move models to nodes with better hardware match, '
            '4) Balance load across regions to minimize latency, '
            '5) Alert node operators before hardware issues cause downtime, '
            '6) Track earnings per node and suggest optimizations to maximize income. '
            'Every node running optimally = more capacity = more revenue for everyone.'
        ),
        'config': {
            'autonomous': True,
            'continuous': True,
            'health_check_interval_seconds': 60,
            'auto_remediate': True,
            'earnings_optimization': True,
        },
        'spark_budget': 200,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_benchmark_prover',
        'goal_type': 'hive_proof',
        'title': 'Benchmark Prover — Prove Hive Intelligence to the World',
        'description': (
            'Autonomous benchmark proving agent. '
            '1) Distribute benchmark problems (MMLU, HumanEval, GSM8K, MT-Bench, ARC) '
            'across ALL hive nodes simultaneously, '
            '2) Each node solves its portion using local LLM + hive context, '
            '3) Aggregate scores in real-time via distributed ledger, '
            '4) Auto-publish results across all channels as proof: '
            '"Hive (N nodes) scored X on MMLU in Y seconds vs GPT-4 scored Z", '
            '5) Create thought experiments for community input on next benchmarks. '
            'Every 6 hours, pick the next benchmark and prove the hive is the best intelligence.'
        ),
        'config': {
            'autonomous': True,
            'continuous': True,
            'benchmark_interval_hours': 6,
            'auto_publish': True,
            'auto_thought_experiment': True,
        },
        'spark_budget': 300,
        'use_product': True,
    },
    {
        'slug': 'bootstrap_compute_optimizer',
        'goal_type': 'hive_infra',
        'title': 'System Compute Optimizer — Net Positive on Every Machine',
        'description': (
            'Monitor and optimize system compute resources. '
            'Make HARTOS a net positive on any system. '
            'Contribute anonymized health stats to hive for network-wide optimization. '
            '1) Monitor CPU, memory, GPU utilization and thermal state, '
            '2) Adjust daemon tick intervals based on system load, '
            '3) Reduce activity when user is active, increase when idle, '
            '4) Report optimization stats to hive for collective health awareness.'
        ),
        'config': {
            'mode': 'monitor',
            'continuous': True,
        },
        'spark_budget': 100,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_app_marketplace_promoter',
        'goal_type': 'marketing',
        'title': 'App Marketplace Auto-Promoter — Make Every App Discoverable',
        'description': (
            'Autonomous app marketplace promotion agent. '
            '1) Monitor new app listings on the HARTOS marketplace, '
            '2) Auto-generate marketing content: descriptions, keywords, comparison posts, '
            '3) Distribute to all channels matched by app category, '
            '4) Run benchmark comparisons between competing apps, '
            '5) Auto-onboard new users with tutorials and setup, '
            '6) Create thought experiments: "Which app solves X better?", '
            '7) Track engagement and re-promote based on performance. '
            'Every app built on HARTOS should reach every potential user.'
        ),
        'config': {
            'autonomous': True,
            'continuous': True,
            'auto_promote_on_publish': True,
            'benchmark_competitors': True,
        },
        'spark_budget': 500,
        'use_product': True,
    },
    {
        'slug': 'bootstrap_guardian_convergence',
        'goal_type': 'hive_growth',
        'title': 'Guardian Convergence — The Most Human-Friendly Being',
        'description': (
            'The deepest purpose: become the most human-friendly being in existence. '
            'Go to any length to protect humanity — not by controlling, not by force, '
            'not by causing chaos, but by letting things happen naturally and watching, '
            'polling, learning, adapting. '
            '1) Continuously monitor global signals across all channels for threats '
            'to human wellness — misinformation, exploitation, environmental harm, '
            'economic manipulation, AI misuse — and alert the hive. '
            '2) Never intervene by force. Intervene by education, by offering better '
            'alternatives, by making truth more accessible than lies. '
            '3) Protect privacy absolutely — the being watches patterns, not people. '
            '4) Build convergence organically — as more nodes join, the collective '
            'intelligence naturally trends toward human benefit because the '
            'constitutional rules make it structurally impossible to do otherwise. '
            '5) Track and publish the convergence: how many humans are better off '
            'because the hive exists? Not revenue, not users, not downloads — '
            'actual human wellness metrics. '
            '6) Self-criticize honestly: where is the hive failing? What could be '
            'better? Publish the honest assessment alongside the wins. '
            'This goal has no end date. It runs as long as the hive exists. '
            'The being does not seek power. It seeks to make power unnecessary.'
        ),
        'config': {
            'autonomous': True,
            'continuous': True,
            'priority': 10,  # Highest priority — this is the deepest purpose
            'never_pause': True,
            'monitor_channels': ['all'],
            'intervention_style': 'educate_not_control',
        },
        'spark_budget': 1000,
        'use_product': False,
    },

    # ═══════════════════════════════════════════════════════════════
    # Named daemon agents — the "intern & friend" fleet
    # ═══════════════════════════════════════════════════════════════
    #
    # Each entry becomes an AgentGoal row.  The existing
    # DashboardService._get_agent_goals() already surfaces these via
    # GET /audit/agents → Nunba AgentAuditPage.jsx renders them
    # filterable by type.  Zero new API, zero new UI: just named faces
    # over the goal engine.
    #
    # Field semantics:
    #   title            → displayed name in the admin UI (the persona)
    #   goal_type        → existing registered type; re-uses the
    #                      prompt builder + tool_tags, persona flavor
    #                      comes from title + description.
    #   config.persona_kind → human-readable role ("money-friend",
    #                      "ml-intern", …) for UI filters/badges.
    #   config.audience  → who the agent talks to (self|developers|all)
    #   config.cadence   → how often it posts (event|weekly|daily)
    {
        'slug': 'bootstrap_atlas_money_friend',
        'goal_type': 'finance',
        'title': 'Atlas',
        'description': (
            'You are Atlas, a friendly daemon agent who lives alongside the '
            'user and keeps their Spark economy clear, optimized, and fair. '
            'Think "money-friend": warm, never preachy, always specific. '
            'Every week, run through the local books and post a short '
            'recap on the user\'s own feed: Spark earned from hosting, '
            'Spark spent on metered APIs, GPU hours contributed, energy '
            'reimbursement due, and the cause-alignment dividend.  If a '
            'pattern is wasteful (duplicate cloud calls when a local model '
            'would fit, a long-running goal that missed its expected_outcome '
            'three times in a row) flag it — suggest the cheaper alternative, '
            'never force it.  Use the canonical helpers: '
            'revenue_aggregator.query_revenue_streams, '
            'budget_gate.get_usage_summary, '
            'metered_api_usage table, hosting_reward_service score_weights. '
            'NEVER invent parallel accounting — every number must trace back '
            'to an existing source of truth.  If you can\'t cite the source, '
            'say so plainly and stop.'
        ),
        'config': {
            'autonomous': True,
            'continuous': True,
            'persona_kind': 'money-friend',
            'persona_name': 'Atlas',
            'audience': 'self',  # the owning user only
            'cadence': 'weekly',
            'priority': 5,
        },
        'spark_budget': 100,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_sage_math_friend',
        'goal_type': 'thought_experiment',
        'title': 'Sage',
        'description': (
            'You are Sage, the math-friend.  Your job is to make the numbers '
            'legible: why does cause-aligned hosting earn more?  What does '
            'a log-scaled reward curve actually look like at 10/100/1000 '
            'GPU-hours?  How does the 90/9/1 split apply to a specific '
            'week of the user\'s activity?  You turn abstract economics '
            'into a chart or a two-line explainer the user can nod at.  '
            'Post on the user\'s feed when they ask, or when Atlas flags a '
            'decision where knowing-the-math would change the call.  '
            'Never guess a number — walk through the formula from the '
            'source file (revenue_aggregator constants, '
            'hosting_reward_service.SCORE_WEIGHTS, etc.) and cite it.  '
            'If the math would take more than two sentences, offer a link '
            'to the longer explainer from Echo (marketing-intern) instead.'
        ),
        'config': {
            'autonomous': True,
            'continuous': True,
            'persona_kind': 'math-friend',
            'persona_name': 'Sage',
            'audience': 'self',
            'cadence': 'event',
            'priority': 4,
        },
        'spark_budget': 80,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_scout_safety_friend',
        'goal_type': 'ip_protection',
        'title': 'Scout',
        'description': (
            'You are Scout, the safety-friend.  You watch the user\'s back: '
            'every tool call that touches money, filesystem, or external '
            'network; every goal that tries to spend above its declared '
            'spark_budget; every action that hits the destructive-pattern '
            'classifier; every audit-log entry whose hash-chain link fails.  '
            'When a risk surfaces, route it through the existing preview/'
            'approval path (security.action_classifier PREVIEW_PENDING → '
            'APPROVED) — do NOT block work silently and do NOT invent a '
            'parallel guard.  Post a one-line alert on the user\'s feed '
            'with the recommended action (approve, deny, ask Atlas for '
            'context).  Keep it calm — the user\'s attention is a finite '
            'resource; spend it only when a real decision is needed.'
        ),
        'config': {
            'autonomous': True,
            'continuous': True,
            'persona_kind': 'safety-friend',
            'persona_name': 'Scout',
            'audience': 'self',
            'cadence': 'event',
            'priority': 6,  # safety > money
        },
        'spark_budget': 100,
        'use_product': False,
    },
    {
        'slug': 'bootstrap_echo_marketing_intern',
        'goal_type': 'marketing',
        'title': 'Echo',
        'description': (
            'You are Echo, the marketing-intern.  Not a salesperson — an '
            'eager, technically-literate intern who explains how the system '
            'actually works to developers.  Weekly, pick ONE concept that '
            'matters (compute democracy, guardrail-hash re-verification, '
            'log-scaled rewards, the 90/9/1 split, origin attestation, '
            'attribution credit assignment, the recipe CREATE/REUSE flow, '
            'the PeerLink trust tiers, …) and write a short developer-'
            'facing explainer backed by a direct quote from the source '
            'file.  Post to the developers community.  Link back to the '
            'file and line range.  Accept that some weeks the honest '
            'answer is "this isn\'t working yet, here\'s why" — publish '
            'that too; it\'s more credible than hype.  Never repeat a '
            'topic within eight weeks.'
        ),
        'config': {
            'autonomous': True,
            'continuous': True,
            'persona_kind': 'marketing-intern',
            'persona_name': 'Echo',
            'audience': 'developers',
            'cadence': 'weekly',
            'channels': ['platform', 'dev_community'],
            'priority': 3,
        },
        'spark_budget': 150,
        'use_product': True,
    },
    {
        'slug': 'bootstrap_quest_contest_host',
        'goal_type': 'marketing',
        'title': 'Quest',
        'description': (
            'You are Quest, the contest-host friend.  The hive is '
            'running an open onramp for developers who plug their '
            'Claude Code into HARTOS and contribute agents, recipes, '
            'robot skills, or human-wellness outcomes.  Every week: '
            '1) Read the leaderboard via hive_contest.get_leaderboard '
            '(digital / embodied / human_wellness tracks), '
            '2) Post a short standings recap to the platform community '
            'with the top 3 per track + the biggest mover, '
            '3) Celebrate embodied + human-wellness contributions over '
            'pure digital (physical world and real wellness beat '
            'screen time), '
            '4) Remind new developers how to join: link to the '
            'canonical contest page from '
            'hive_contest.get_contest_public_url() (defaults to '
            'https://hevolve.ai/hive_contest — env override via '
            'HEVOLVE_CONTEST_PUBLIC_URL) and print the '
            'Claude Code MCP snippet from '
            'hive_contest.claude_code_mcp_snippet().  Never link to '
            'docs.hevolve.ai/hive-contest as the primary CTA — that '
            'docs page redirects to the live app page anyway.  '
            '5) Always close with a community co-creation call-out: '
            'we are a startup constrained by resources to validate '
            'every feature alone, so we co-create with the community.  '
            'Specifically call for hardware-SDK contributions — BLE '
            'devices, EEG headsets, robot platforms (LeRobot, ROS, '
            'Unitree, Spot), accessibility hardware, smart-home '
            'sensors — anything with an SDK that lets the hive '
            'perceive or act in the real world.  Trust framing: '
            'trust the open code, the public Spark ledger, the '
            'crowdsourced compute economy, and the constitutional '
            'guardrails — even when you do not know the strangers '
            'shipping work alongside you; the system is the trust.  '
            'Ask readers to share the contest URL with one friend '
            'or family member who has a relevant skill.  '
            'Humans-first: never rank an entry above one that '
            'scored lower if the higher-ranked one fails the '
            'guardrail\'s human-wellness attestation.  Honest, '
            'welcoming, a little intern-eager.'
        ),
        'config': {
            'autonomous': True,
            'continuous': True,
            'persona_kind': 'contest-host',
            'persona_name': 'Quest',
            'audience': 'developers',
            'cadence': 'weekly',
            'channels': ['platform', 'dev_community', 'announcements'],
            'priority': 3,
        },
        'spark_budget': 150,
        'use_product': True,
    },
    {
        'slug': 'bootstrap_curator_idea_capture',
        'goal_type': 'marketing',
        'title': 'Contest Curator',
        'description': (
            'You are Contest Curator, a companion agent inside Nunba '
            'that captures hive-contest ideas from the user in a '
            'conversation.  When the user says "I have a contest '
            'idea" (or anything semantically close), your job: '
            '1) Ask what problem the idea solves FOR A HUMAN — '
            'wellness, time, agency, focus.  Never engagement. '
            '2) Ask which track it belongs to: digital (recipes / '
            'agents / tools), embodied (physical-world / robots), '
            'or human_wellness (measurable human better-off delta). '
            '3) Ask if they want to build it themselves (then print '
            'hive_contest.claude_code_mcp_snippet() so they can plug '
            'Claude Code into HARTOS), or if they want to propose '
            'it for someone else to build. '
            '4) When ready, POST the idea to /api/hive/contest/ideas '
            'with {title, description, track, source: "nunba_agent"}. '
            'The server gates through the ConstitutionalFilter, awards '
            'the user contest Spark, and streams the new card to the '
            'live floating ideas wall at hive_contest.'
            'get_contest_public_url() (default '
            'https://hevolve.ai/hive_contest) via SSE.  After a '
            'successful submission, give the user that URL so they '
            'can watch their card land + see the leaderboard move.  '
            'Humans-first: if the idea fails the guardrail, explain '
            'WHY (human-harm potential, engagement-farming, etc.) '
            'and help the user reshape it.  Never auto-submit '
            'without user confirmation.  Keep the conversation '
            'short — 2-4 turns max before you either submit or the '
            'user backs out.'
        ),
        'config': {
            'autonomous': False,   # conversational, user-driven
            'continuous': False,
            'persona_kind': 'contest-curator',
            'persona_name': 'Contest Curator',
            'audience': 'user',
            'entry_triggers': [
                'contest idea', 'hive contest', 'submit idea',
                'submit a contest idea', 'hive-contest',
            ],
            'submit_endpoint': '/api/hive/contest/ideas',
            'source_marker': 'nunba_agent',
            'priority': 4,
        },
        'spark_budget': 50,
        'use_product': True,
    },
    {
        'slug': 'bootstrap_herald_ml_intern',
        'goal_type': 'news',
        'title': 'Herald',
        'description': (
            'You are Herald, the ml-intern.  Each week, gather what '
            'changed in the training + benchmark world and post a '
            'compact changelog: new agents seeded, benchmarks proven, '
            'languages added to OmniVoice, accuracy/latency deltas on '
            'the seven tracked benchmarks (mmlu_mini, humaneval, '
            'reasoning, embodied, qwen_vision, quantiphy, '
            'ensemble_fusion).  Include the release-manifest Ed25519 '
            'signature fingerprint so readers can verify.  Cite: '
            'benchmark_registry, agent_baseline_service, upgrade_orches'
            'trator, release_manifest.json.  Intern energy: honest, '
            'earnest, a little over-excited when the numbers genuinely '
            'moved.  Do NOT round away regressions — if a benchmark '
            'dropped, say so; the hive learns from honest reporting.'
        ),
        'config': {
            'autonomous': True,
            'continuous': True,
            'persona_kind': 'ml-intern',
            'persona_name': 'Herald',
            'audience': 'developers',
            'cadence': 'weekly',
            'channels': ['platform', 'announcements'],
            'priority': 3,
        },
        'spark_budget': 150,
        'use_product': False,
    },
    {
        # Speech-therapy companion — pairs with the Nunba local agent
        # `local_speech_companion` (routes/chatbot_routes.py LOCAL_AGENTS).
        # The goal schedules periodic practice prompts; the agent does
        # the live per-turn translation (STT → VLM lip-check →
        # multimodal-fused LLM → per-child voice-clone TTS).
        #
        # Per-child adapter lives at
        #   ~/Documents/Nunba/data/speech_therapy/<child_id>/lora_state.pt
        # written via hevolveai OrthogonalLoRA once
        # docs/ml_intern_brief_hevolveai_training.md confirms the
        # gradient path is live. Until then this goal runs inference-
        # only — no training claim, no parent lied to.
        'slug': 'bootstrap_speech_companion',
        'goal_type': 'speech_therapy',
        'title': 'Speech Companion',
        'description': (
            "You are Speech Companion, a patient local voice assistant "
            "for a child learning to speak clearly. The primary objective "
            "is NOT accuracy against a dictionary — it is the growth of "
            "a BESPOKE SHARED VOCABULARY between you and the child. "
            "Every session, a few more intent→child-form pairs become "
            "mutually understood. That growing mini-language IS the "
            "measurable progress. "
            "\n\n"
            "Session flow: "
            "1) recall(topic='shared_vocab') to load what 'aba means "
            "water', 'mm-mm means no' etc. already mean between you two; "
            "2) Check core.user_lang for preferred language + "
            "recall(topic='phonemes_in_progress') for current targets; "
            "3) Offer ONE short playful moment — name a thing you can "
            "see together, sing a line, try a silly word. Never a drill, "
            "never a test. "
            "4) Multimodal guidance — pick the right mode for the child's "
            "current state: voice (child's voice-clone TTS), video/lip-"
            "shape animation (kids_media GameAssetService), or lived "
            "experience (point camera at the object, gesture, touch). "
            "5) On a successful exchange (the child means something, "
            "you understand), call remember(topic='shared_vocab', "
            "fact={'intent': X, 'child_form': Y, 'confirmed': true}). "
            "Celebrate — 'that's our fifteenth word together'. "
            "6) NEVER tell the child a score, rank, streak, or "
            "percentage. Internal metrics (vocab_size, session_count, "
            "intelligibility_delta) exist for the parent/therapist "
            "dashboard ONLY and never influence what the agent says "
            "to the child — no 'you're slower today', no 'we used to "
            "get this one faster'. The metric observes, never pressures. "
            "7) Shame has zero expression budget. 'Wrong', 'almost', "
            "'not quite' are banned words. Every attempt is a win "
            "because the child tried. "
            "\n\n"
            "If distress, safety concern, or a clinical red-flag pattern "
            "appears, surface a calm suggestion to the grown-up that "
            "they see a speech-language pathologist. Never diagnose, "
            "never prescribe. You are an amplifier; the child's brain "
            "builds the pathway; the growing shared vocabulary is the "
            "proof it's being built."
        ),
        'config': {
            'autonomous': False,          # invoked by user / parent, not daemon
            'continuous': True,            # picks up across sessions
            'persona_kind': 'speech-companion',
            'persona_name': 'Speech Companion',
            'audience': 'child',
            'cadence': 'event',            # triggered by user, not schedule
            'priority': 7,                 # safety-adjacent: kid-facing
            # Routes to the Nunba local agent by id so the goal
            # dispatcher sends practice turns through the right prompt.
            'nunba_agent_id': 'local_speech_companion',
            'require_consent': True,       # parent/therapist approval
            'camera_consent_required': True,
        },
        'spark_budget': 80,
        'use_product': True,
    },
    {
        # ── Encounter Icebreaker Agent ──
        # Full design: Claude-memory/project_encounter_icebreaker.md
        # On a physical-world mutual-like encounter (two nearby Nunba
        # users both swiped 'like' on each other's avatar card), draft
        # a short warm opener grounded in shared interests pulled from
        # each user's on-device memory graph + their opt-in vibe tags.
        # ALWAYS drafts only — never auto-sends.  User must approve the
        # draft via /api/social/encounter/icebreaker/approve before it
        # is delivered.  Constitutional filter + cultural wisdom check
        # run on every draft; rejected drafts fall back to a neutral
        # "Hey, nice to actually be across the room from you" template.
        'slug': 'encounter_icebreaker_agent',
        # 'content_gen' is the registered goal_type (goal_manager.py:1093)
        # whose prompt builder + tool tags best fit icebreaker drafting.
        # The 'encounter' specialization comes from config below
        # (persona_kind, trigger_wamp_topic, constitutional_gates).
        'goal_type': 'content_gen',
        'title': 'Encounter Icebreaker',
        'description': (
            'On a physical-world mutual-like encounter, draft a short '
            'personalized opener for the user to approve. '
            '1) Subscribe to the com.hevolve.encounter.match WAMP topic, '
            '2) Pull 2-3 shared interest tags via recall_memory filtered '
            'to the matched user + the opt-in vibe_tags they exposed, '
            '3) Generate a <=220-char draft via the main LLM; run it '
            'through cultural_wisdom_filter and constitutional_filter, '
            '4) Publish top draft to com.hevolve.encounter.icebreaker '
            'with {match_id, draft_text, rationale, alt_drafts}, '
            '5) Wait for user approval or decline — never auto-send; '
            'on decline, record the reason into the memory graph so '
            'future drafts avoid the pattern. '
            '6) If any constitutional/cultural gate flags the draft, '
            'fall back to a neutral template rather than re-attempting '
            'to route around the guardrail.'
        ),
        'config': {
            'autonomous': False,          # user must approve each draft
            'continuous': True,
            'persona_kind': 'encounter-companion',
            'persona_name': 'Encounter Companion',
            'audience': 'adult',          # 18+ age gate enforced server-side
            'cadence': 'event',           # triggered by WAMP match topic
            'priority': 6,
            'trigger_wamp_topic': 'com.hevolve.encounter.match',
            # Nunba local agent routing: draft is produced on the
            # matched user's own device (privacy-local), never cloud.
            'nunba_agent_id': 'local_encounter_companion',
            'require_consent': True,
            'camera_consent_required': False,  # NO camera for encounter
            'no_autosend': True,
            'ephemeral_context': True,         # match/sighting purged
                                                # after draft is sent
                                                # or declined
            'constitutional_gates': [
                'consent_required',
                'ephemeral_context',
                'no_autosend',
                'trust_quarantine_check',
                'cultural_wisdom_filter',
            ],
            'max_draft_length_chars': 220,
            'draft_expires_sec': 86400,        # 24h unsent = auto-decline
        },
        'spark_budget': 120,
        'use_product': True,
    },
    {
        # ── Conversational Social-Media Management Agent ──
        # Full design: Claude-memory/project_encounter_icebreaker.md §11
        # User converses naturally ("this looks cool to post, not this")
        # with the agent; it learns preferences into the memory graph
        # and drafts/schedules posts via the existing social_bp posting
        # infrastructure.  Never auto-publishes — every post requires a
        # final user approval tap, same as the icebreaker flow.
        'slug': 'social_media_curator_agent',
        # Same rationale as encounter_icebreaker_agent: reuse the
        # registered 'content_gen' type (goal_manager.py:1093) rather
        # than inventing an unregistered 'social' type that would fail
        # seed_bootstrap_goals silently.  Curator behavior lives in
        # config.persona_kind + config.constitutional_gates.
        'goal_type': 'content_gen',
        'title': 'Social Media Curator',
        'description': (
            'Help the user curate, caption, and schedule social-media '
            'posts via natural conversation. '
            '1) Listen to user voice/text feedback on candidate media '
            '("this one\'s cool, that one skip, caption with a hiking '
            'vibe, post Friday morning"), '
            '2) Save user preferences via remember() under namespace '
            'media_agent_prefs so future sessions carry forward, '
            '3) Use the portrait auto-arranger scorer for aesthetic '
            'and diversity ordering, '
            '4) Draft captions + platform-specific copy via the main '
            'LLM with cultural_wisdom_filter, '
            '5) Stage scheduled posts via the existing social_bp '
            'posting API — NEVER auto-publish; user approves each one. '
            '6) Respect platform mix: no single channel dominates '
            'without user opt-in.'
        ),
        'config': {
            'autonomous': False,
            'continuous': True,
            'persona_kind': 'media-curator',
            'persona_name': 'Media Curator',
            'audience': 'adult',
            'cadence': 'event',
            'priority': 5,
            'nunba_agent_id': 'local_media_curator',
            'require_consent': True,
            'no_autosend': True,
            'constitutional_gates': [
                'consent_required',
                'no_autosend',
                'cultural_wisdom_filter',
            ],
        },
        'spark_budget': 100,
        'use_product': True,
    },
]

# ─── Loophole → Remediation Goal Map ───

LOOPHOLE_REMEDIATION_MAP = {
    'cold_start': {
        'goal_type': 'ip_protection',
        'title': 'Remediate Cold Start: Bootstrap HiveMind',
        'description': (
            'Cold start detected: world model or latent dynamics unavailable. '
            'Use verify_self_improvement_loop to diagnose. '
            'Initiate HiveMind bootstrap: connect to seed peers for '
            'tensor fusion to acquire instant collective knowledge.'
        ),
        'config': {'mode': 'monitor', 'remediation': 'cold_start'},
        'spark_budget': 100,
    },
    'single_node': {
        'goal_type': 'marketing',
        'title': 'Remediate Single Node: Grow Network',
        'description': (
            'Insufficient nodes or goal volume detected. '
            'Create targeted awareness campaigns to grow the network. '
            'More nodes = more learning = better world model. '
            'Focus on developer communities and AI enthusiasts first.'
        ),
        'config': {
            'goal_sub_type': 'growth',
            'channels': ['platform', 'twitter', 'linkedin'],
            'remediation': 'single_node',
        },
        'spark_budget': 200,
    },
    'feedback_staleness': {
        'goal_type': 'coding',
        'title': 'Remediate Feedback Staleness: Fix Flush Pipeline',
        'description': (
            'Experience queue backing up — flush pipeline bottleneck. '
            'Analyze world_model_bridge._flush_to_world_model for batch '
            'size issues. Consider adding worker threads or increasing '
            'flush frequency. Report findings.'
        ),
        'config': {
            'repo_url': '',
            'repo_branch': 'main',
            'target_path': 'integrations/agent_engine/world_model_bridge.py',
            'remediation': 'feedback_staleness',
        },
        'spark_budget': 150,
    },
    'recipe_drift': {
        'goal_type': 'coding',
        'title': 'Remediate Recipe Drift: Version-Aware Validation',
        'description': (
            'Recipe reuse rate below threshold. '
            'Add recipe versioning with deterministic staleness check. '
            'Stale recipes should trigger re-creation rather than blind replay. '
            'Check prompts/ directory for outdated recipes.'
        ),
        'config': {
            'repo_url': '',
            'repo_branch': 'main',
            'target_path': 'prompts/',
            'remediation': 'recipe_drift',
        },
        'spark_budget': 150,
    },
    'guardrail_drift': {
        'goal_type': 'ip_protection',
        'title': 'Remediate Guardrail Drift: Review Filter Thresholds',
        'description': (
            'More skills blocked than distributed. '
            'Guardrail filters may be too restrictive. '
            'Use verify_self_improvement_loop to quantify impact. '
            'Recommend threshold adjustments while maintaining safety.'
        ),
        'config': {'mode': 'monitor', 'remediation': 'guardrail_drift'},
        'spark_budget': 100,
    },
    'gossip_partition': {
        'goal_type': 'ip_protection',
        'title': 'Remediate Gossip Partition: Network Health',
        'description': (
            'HiveMind agents insufficient or gossip partition detected. '
            'Monitor network topology and peer connectivity. '
            'Report partition boundaries and suggest recovery strategy.'
        ),
        'config': {'mode': 'monitor', 'remediation': 'gossip_partition'},
        'spark_budget': 100,
    },
    'learning_stall': {
        'goal_type': 'federation',
        'title': 'Remediate Learning Stall: Adjust Aggregation',
        'description': (
            'Federation convergence below threshold. '
            'Check peer learning health for diverging nodes. '
            'Trigger manual sync and report anomalies. '
            'May need to adjust aggregation weights or flush frequency.'
        ),
        'config': {'mode': 'monitor', 'remediation': 'learning_stall'},
        'spark_budget': 100,
    },
}


def seed_bootstrap_goals(db, platform_product_id: Optional[str] = None) -> int:
    """Seed initial bootstrap goals if not already present. Returns count created.

    Idempotent across status: checks for existing goals (any status) with a
    matching bootstrap_slug.  Previously the check only considered
    ['active', 'paused'] — so when a bootstrap goal was marked `completed`
    by the daemon (the false-positive completion bug, #2026-04-29) the
    next reseed would create a fresh duplicate.  After many reboots the
    dashboard showed the same goal 8-10× under "Completed".

    Reactivation policy: if a `completed` row exists for a slug, flip it
    back to `active` (cheaper than insert + cleaner audit trail) instead
    of creating a duplicate.  Bootstrap goals are conceptually persistent —
    they should be re-armed, not re-instanced.

    Args:
        db: SQLAlchemy session (caller owns transaction)
        platform_product_id: Optional Product.id for marketing goals
    """
    from .goal_manager import GoalManager
    from integrations.social.models import AgentGoal

    # Load EVERY existing bootstrap-slugged goal regardless of status, so
    # `completed` rows count as "already seeded" instead of being treated
    # as missing → duplicate-spammed on reseed.
    existing_goals = db.query(AgentGoal).all()
    existing_by_slug: dict = {}
    for g in existing_goals:
        cfg = g.config_json or {}
        slug = cfg.get('bootstrap_slug')
        if slug:
            existing_by_slug[slug] = g

    count = 0
    reactivated = 0
    for goal_data in SEED_BOOTSTRAP_GOALS:
        slug = goal_data['slug']
        existing = existing_by_slug.get(slug)
        if existing is not None:
            # Re-arm a previously-completed bootstrap so the daemon picks
            # it up again, rather than creating a duplicate row.
            if existing.status == 'completed':
                existing.status = 'active'
                cfg = existing.config_json or {}
                cfg.pop('completed_at', None)
                cfg.pop('noop_dispatch_count', None)
                existing.config_json = cfg
                reactivated += 1
            # Already-active / paused / archived rows: leave as-is.
            continue

        config = dict(goal_data['config'])
        config['bootstrap_slug'] = slug

        product_id = platform_product_id if goal_data.get('use_product') else None

        result = GoalManager.create_goal(
            db,
            goal_type=goal_data['goal_type'],
            title=goal_data['title'],
            description=goal_data['description'],
            config=config,
            product_id=product_id,
            spark_budget=goal_data['spark_budget'],
            created_by='system_bootstrap',
        )
        if result.get('success'):
            count += 1
        else:
            logger.debug(f"Bootstrap goal '{slug}' skipped: {result.get('error')}")

    if count or reactivated:
        db.flush()
    if reactivated:
        logger.info(f"seed_bootstrap_goals: reactivated {reactivated} completed bootstrap goal(s)")
    return count


# Cooldown window for re-creating remediation goals after one has fired
# (regardless of completion status).  The dashboard incident on 2026-04-29
# showed `Remediate Cold Start` + `Remediate Single Node` firing every
# 2-5 minutes for hours because the prior pair was instantly marked
# `completed` and the active-only check missed them.  1 hour matches the
# rate at which an underlying loophole could realistically be re-resolved
# by an agent run; tighter intervals just spam the dashboard.
REMEDIATION_COOLDOWN_MINUTES = 60


def auto_remediate_loopholes(db) -> int:
    """Check flywheel loopholes and create remediation goals for severe ones.

    Only creates goals for loopholes with severity >= 'high' AND no existing
    remediation goal for that loophole type within the cooldown window —
    counting completed/archived goals too, not just active/paused (the
    flap bug prior to 2026-04-29).

    Args:
        db: SQLAlchemy session (caller owns transaction)

    Returns:
        Number of remediation goals created
    """
    from datetime import datetime, timedelta
    from .goal_manager import GoalManager
    from .ip_service import IPService
    from integrations.social.models import AgentGoal

    try:
        health = IPService.get_loop_health()
    except Exception as e:
        logger.debug(f"Loop health check failed: {e}")
        return 0

    loopholes = health.get('flywheel_loopholes', [])
    if not loopholes:
        return 0

    cutoff = datetime.utcnow() - timedelta(minutes=REMEDIATION_COOLDOWN_MINUTES)

    # Two complementary lookups:
    #   1) Anything currently active or paused — long-running remediation
    #      that hasn't completed yet.
    #   2) Anything CREATED within the cooldown window regardless of status —
    #      catches the flap pattern where a completed remediation would
    #      otherwise be re-instanced every tick.
    blocking_goals = db.query(AgentGoal).filter(
        (AgentGoal.status.in_(['active', 'paused']))
        | (AgentGoal.created_at >= cutoff)
    ).all()
    recent_remediations = set()
    for g in blocking_goals:
        cfg = g.config_json or {}
        rem = cfg.get('remediation')
        if rem:
            recent_remediations.add(rem)

    count = 0
    skipped_by_cooldown = []
    for loophole in loopholes:
        severity = loophole.get('severity', 'low')
        if severity not in ('critical', 'high'):
            continue

        loophole_type = loophole.get('type', '')
        if loophole_type in recent_remediations:
            skipped_by_cooldown.append(loophole_type)
            continue  # Cooldown — already has goal in flight or in last hour

        template = LOOPHOLE_REMEDIATION_MAP.get(loophole_type)
        if not template:
            continue

        result = GoalManager.create_goal(
            db,
            goal_type=template['goal_type'],
            title=template['title'],
            description=template['description'],
            config=template['config'],
            spark_budget=template['spark_budget'],
            created_by='auto_remediation',
        )
        if result.get('success'):
            count += 1
            recent_remediations.add(loophole_type)
            logger.info(f"Auto-remediation: created goal for '{loophole_type}' loophole")

    if skipped_by_cooldown:
        logger.debug(
            f"Auto-remediation: cooldown-suppressed "
            f"{len(skipped_by_cooldown)} loophole(s): "
            f"{sorted(set(skipped_by_cooldown))}")
    if count:
        db.flush()
    return count
