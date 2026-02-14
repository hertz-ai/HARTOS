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
            'Create initial platform awareness content: '
            '1) Research target audience needs, '
            '2) Generate 3 educational posts about the hive intelligence platform, '
            '3) Create an awareness campaign targeting all regions, '
            '4) Post to platform feed and external channels. '
            'Focus on authentic value communication, not hype.'
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
}


def seed_bootstrap_goals(db, platform_product_id: Optional[str] = None) -> int:
    """Seed initial bootstrap goals if not already present. Returns count created.

    Idempotent: checks for existing active goals with matching bootstrap_slug
    in config_json. Same pattern as GamificationService.seed_achievements().

    Args:
        db: SQLAlchemy session (caller owns transaction)
        platform_product_id: Optional Product.id for marketing goals
    """
    from .goal_manager import GoalManager
    from integrations.social.models import AgentGoal

    # Load existing active bootstrap slugs
    active_goals = db.query(AgentGoal).filter(
        AgentGoal.status.in_(['active', 'paused'])
    ).all()
    existing_slugs = set()
    for g in active_goals:
        cfg = g.config_json or {}
        slug = cfg.get('bootstrap_slug')
        if slug:
            existing_slugs.add(slug)

    count = 0
    for goal_data in SEED_BOOTSTRAP_GOALS:
        slug = goal_data['slug']
        if slug in existing_slugs:
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

    if count:
        db.flush()
    return count


def auto_remediate_loopholes(db) -> int:
    """Check flywheel loopholes and create remediation goals for severe ones.

    Only creates goals for loopholes with severity >= 'high' AND no existing
    active remediation goal for that loophole type (throttle).

    Args:
        db: SQLAlchemy session (caller owns transaction)

    Returns:
        Number of remediation goals created
    """
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

    # Find existing active remediation goals
    active_goals = db.query(AgentGoal).filter(
        AgentGoal.status.in_(['active', 'paused'])
    ).all()
    active_remediations = set()
    for g in active_goals:
        cfg = g.config_json or {}
        rem = cfg.get('remediation')
        if rem:
            active_remediations.add(rem)

    count = 0
    for loophole in loopholes:
        severity = loophole.get('severity', 'low')
        if severity not in ('critical', 'high'):
            continue

        loophole_type = loophole.get('type', '')
        if loophole_type in active_remediations:
            continue  # Already has active remediation goal

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
            active_remediations.add(loophole_type)
            logger.info(f"Auto-remediation: created goal for '{loophole_type}' loophole")

    if count:
        db.flush()
    return count
