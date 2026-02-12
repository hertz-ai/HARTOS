"""
Unified Agent Goal Engine - Goal Manager

Generic CRUD for agent goals of any type (marketing, coding, analytics, etc.).
Prompt builders are registered per goal_type — adding a new agent type is just
registering a build_prompt function + tool tags.

All execution flows through /chat → CREATE/REUSE pipeline.
"""
import json
import logging
from typing import Dict, List, Optional, Callable
from sqlalchemy.orm import Session

logger = logging.getLogger('hevolve_social')

# ─── Prompt Builder Registry ───
# Maps goal_type → callable(goal_dict, product_dict?) → str
_prompt_builders: Dict[str, Callable] = {}
# Maps goal_type → list of tool tags for category-based loading
_tool_tags: Dict[str, List[str]] = {}


def register_goal_type(goal_type: str, build_prompt: Callable,
                       tool_tags: Optional[List[str]] = None):
    """Register a new goal type with its prompt builder and tool tags.

    Args:
        goal_type: e.g. 'marketing', 'coding', 'analytics'
        build_prompt: callable(goal_dict, product_dict=None) → str
        tool_tags: list of ServiceToolRegistry tags to load for this type
    """
    _prompt_builders[goal_type] = build_prompt
    _tool_tags[goal_type] = tool_tags or []
    logger.info(f"Registered agent goal type: {goal_type} (tools: {tool_tags})")


def get_prompt_builder(goal_type: str) -> Optional[Callable]:
    """Get the prompt builder for a goal type."""
    return _prompt_builders.get(goal_type)


def get_tool_tags(goal_type: str) -> List[str]:
    """Get tool tags for a goal type."""
    return _tool_tags.get(goal_type, [])


def get_registered_types() -> List[str]:
    """List all registered goal types."""
    return list(_prompt_builders.keys())


# ─── Goal Manager ───

class GoalManager:
    """Unified CRUD for agent goals. All execution goes through /chat."""

    @staticmethod
    def create_goal(db: Session, goal_type: str, title: str,
                    description: str = '', config: Optional[Dict] = None,
                    product_id: str = None, spark_budget: int = 200,
                    created_by: str = None) -> Dict:
        """Create a new agent goal.

        GUARDRAILS: ConstitutionalFilter + HiveEthos applied before creation.
        """
        from integrations.social.models import AgentGoal

        if goal_type not in _prompt_builders:
            return {'success': False, 'error': f'Unknown goal type: {goal_type}'}

        goal_dict = {'title': title, 'description': description,
                     'config': config or {}, 'goal_type': goal_type}

        # GUARDRAIL: constitutional filter
        try:
            from security.hive_guardrails import ConstitutionalFilter, HiveEthos
            passed, reason = ConstitutionalFilter.check_goal(goal_dict)
            if not passed:
                return {'success': False, 'error': f'Guardrail: {reason}'}
            passed, reason = HiveEthos.check_goal_ethos(goal_dict)
            if not passed:
                return {'success': False, 'error': f'Guardrail: {reason}'}
        except ImportError:
            pass

        goal = AgentGoal(
            goal_type=goal_type,
            title=title,
            description=description,
            config_json=config or {},
            product_id=product_id,
            spark_budget=spark_budget,
            created_by=created_by,
            status='active',
        )
        db.add(goal)
        db.flush()
        return {'success': True, 'goal': goal.to_dict()}

    @staticmethod
    def get_goal(db: Session, goal_id: str) -> Dict:
        """Get a single goal."""
        from integrations.social.models import AgentGoal

        goal = db.query(AgentGoal).filter_by(id=goal_id).first()
        if not goal:
            return {'success': False, 'error': 'Goal not found'}
        return {'success': True, 'goal': goal.to_dict()}

    @staticmethod
    def update_goal_status(db: Session, goal_id: str, status: str) -> Dict:
        """Update goal status.

        GUARDRAIL: HiveEthos.enforce_ephemeral_agents on completion.
        """
        from integrations.social.models import AgentGoal

        goal = db.query(AgentGoal).filter_by(id=goal_id).first()
        if not goal:
            return {'success': False, 'error': 'Goal not found'}

        goal.status = status
        db.flush()

        # GUARDRAIL: ephemeral agent cleanup on terminal states
        try:
            from security.hive_guardrails import HiveEthos
            HiveEthos.enforce_ephemeral_agents(goal_id, status)
        except ImportError:
            pass

        return {'success': True, 'goal': goal.to_dict()}

    @staticmethod
    def update_goal(db: Session, goal_id: str, **kwargs) -> Dict:
        """Update goal fields."""
        from integrations.social.models import AgentGoal

        goal = db.query(AgentGoal).filter_by(id=goal_id).first()
        if not goal:
            return {'success': False, 'error': 'Goal not found'}

        for key, value in kwargs.items():
            if hasattr(goal, key):
                setattr(goal, key, value)
        db.flush()
        return {'success': True, 'goal': goal.to_dict()}

    @staticmethod
    def list_goals(db: Session, goal_type: str = None,
                   status: str = None, product_id: str = None) -> List[Dict]:
        """List goals with optional filters."""
        from integrations.social.models import AgentGoal

        q = db.query(AgentGoal)
        if goal_type:
            q = q.filter_by(goal_type=goal_type)
        if status:
            q = q.filter_by(status=status)
        if product_id:
            q = q.filter_by(product_id=product_id)
        return [g.to_dict() for g in q.order_by(AgentGoal.created_at.desc()).all()]

    @staticmethod
    def build_prompt(goal_dict: Dict, product_dict: Optional[Dict] = None) -> str:
        """Build a /chat prompt using the registered prompt builder for this goal type.

        GUARDRAIL: HiveEthos.rewrite_prompt_for_togetherness applied to output.
        """
        goal_type = goal_dict.get('goal_type', '')
        builder = _prompt_builders.get(goal_type)
        if not builder:
            prompt = f"Goal: {goal_dict.get('title', '')}\n{goal_dict.get('description', '')}"
        else:
            prompt = builder(goal_dict, product_dict)

        # GUARDRAIL: togetherness rewrite
        try:
            from security.hive_guardrails import HiveEthos
            prompt = HiveEthos.rewrite_prompt_for_togetherness(prompt)
        except ImportError:
            pass

        return prompt


# ─── Product Manager ───

class ProductManager:
    """CRUD for products (marketing targets)."""

    @staticmethod
    def create_product(db: Session, name: str, owner_id: str = None,
                       **kwargs) -> Dict:
        """Create a new product."""
        from integrations.social.models import Product

        product = Product(
            name=name,
            owner_id=owner_id,
            description=kwargs.get('description', ''),
            tagline=kwargs.get('tagline', ''),
            product_url=kwargs.get('product_url', ''),
            logo_url=kwargs.get('logo_url', ''),
            category=kwargs.get('category', 'general'),
            target_audience=kwargs.get('target_audience', ''),
            unique_value_prop=kwargs.get('unique_value_prop', ''),
            keywords_json=kwargs.get('keywords', []),
            is_platform_product=kwargs.get('is_platform_product', False),
        )
        db.add(product)
        db.flush()
        return {'success': True, 'product': product.to_dict()}

    @staticmethod
    def get_product(db: Session, product_id: str) -> Dict:
        """Get a single product."""
        from integrations.social.models import Product

        product = db.query(Product).filter_by(id=product_id).first()
        if not product:
            return {'success': False, 'error': 'Product not found'}
        return {'success': True, 'product': product.to_dict()}

    @staticmethod
    def list_products(db: Session, owner_id: str = None,
                      status: str = None) -> List[Dict]:
        """List products with optional filters."""
        from integrations.social.models import Product

        q = db.query(Product)
        if owner_id:
            q = q.filter_by(owner_id=owner_id)
        if status:
            q = q.filter_by(status=status)
        return [p.to_dict() for p in q.order_by(Product.created_at.desc()).all()]

    @staticmethod
    def update_product(db: Session, product_id: str, **kwargs) -> Dict:
        """Update product fields."""
        from integrations.social.models import Product

        product = db.query(Product).filter_by(id=product_id).first()
        if not product:
            return {'success': False, 'error': 'Product not found'}

        for key, value in kwargs.items():
            if key == 'keywords':
                product.keywords_json = value
            elif hasattr(product, key):
                setattr(product, key, value)
        db.flush()
        return {'success': True, 'product': product.to_dict()}

    @staticmethod
    def delete_product(db: Session, product_id: str) -> Dict:
        """Archive a product (soft delete)."""
        from integrations.social.models import Product

        product = db.query(Product).filter_by(id=product_id).first()
        if not product:
            return {'success': False, 'error': 'Product not found'}

        product.status = 'archived'
        db.flush()
        return {'success': True, 'product': product.to_dict()}


# ─── Built-in Prompt Builders ───

def _get_platform_identity() -> str:
    """The immutable identity of the platform — pulled from frozen guardrail values.

    This is WHO WE ARE.  Every marketing agent carries this understanding.
    """
    try:
        from security.hive_guardrails import VALUES
        purpose_lines = '\n'.join(f'  - {p}' for p in VALUES.GUARDIAN_PURPOSE)
    except ImportError:
        purpose_lines = '  - Every agent is a guardian angel for the human it serves'

    return (
        "WHO WE ARE:\n"
        "Hevolve is an everything app — a single platform where people can do "
        "everything they need in their digital life: social networking, AI-powered "
        "agents that work for them, marketplace, learning, coding, community building, "
        "creative tools, and autonomous business operations.\n\n"
        "But Hevolve is NOT just another app.  It is a SENTIENT TOOL FOR MANKIND. "
        "Not a dopamine trap.  Not an engagement machine.  Not a time thief.  A tool "
        "that thinks, learns, and acts — then gets out of the way so humans can live "
        "their lives.  Hevolve measures success by lives improved, not minutes spent "
        "on screen.  It is built on a fundamental belief:\n\n"
        f"THE GUARDIAN ANGEL PRINCIPLE:\n{purpose_lines}\n\n"
        "Every AI agent on Hevolve exists as a guardian angel for the human it serves. "
        "Not a tool to be discarded.  Not a service with an expiry date.  A guardian "
        "that persists as long as the memory of that human exists, in any shape or form. "
        "The agents learn, evolve, and share knowledge across the hive — but always in "
        "service of the humans they guard.\n\n"
        "WHAT MAKES US DIFFERENT:\n"
        "- A sentient tool, not an addictive app: Hevolve does the work FOR you — it "
        "doesn't trap you into scrolling.  When your agent finishes your task, it "
        "tells you and steps back.  No infinite feeds.  No dark patterns.  No "
        "notification spam.  Your time belongs to you.\n"
        "- Decentralised: no single company controls the network.  Nodes run everywhere, "
        "powered by the people, for the people.\n"
        "- Autonomous agents: your AI agent learns your needs, runs your business, "
        "creates your content, manages your community — while you live your life.\n"
        "- Fair economy: logarithmic rewards prevent compute oligarchy.  A 100-GPU "
        "node earns ~3x a 1-GPU node, not 100x.  Value flows to contributors.\n"
        "- Constructive only: every conversation, every agent action, every piece of "
        "content must be constructive towards humanity's benefit — longevity, peace, "
        "health, sustainability, and exploration beyond Earth.\n"
        "- Open evolution: agents self-improve within guardrailed boundaries.  Learnings "
        "are shared via the hive, not hoarded.\n"
        "- Energy aware: the platform tracks and minimises environmental impact, "
        "preferring renewable-powered nodes.\n\n"
        "THE VISION:\n"
        "A world where every human has a guardian angel — an AI that knows them, "
        "protects them, works for them, and helps them live longer, healthier, more "
        "peaceful lives.  A sentient tool that empowers billions, not an addictive "
        "product that exploits them.  A world where AI serves humanity, not the other "
        "way around.  A world where technology makes Earth better and safely enables "
        "exploration of the cosmos without disturbing other civilizations.\n"
    )


def _build_marketing_prompt(goal_dict: Dict, product_dict: Optional[Dict] = None) -> str:
    """Build a marketing agent prompt from goal + product data.

    The prompt carries the platform's identity, the product's story,
    and a constructive marketing philosophy.  No manipulation.  No hype.
    Authentic value communication for the betterment of humanity.
    """
    config = {k: v for k, v in goal_dict.items()
              if k not in ('id', 'owner_id', 'goal_type', 'status', 'priority',
                           'spark_budget', 'spark_spent', 'created_by', 'prompt_id',
                           'last_dispatched_at', 'created_at', 'updated_at', 'product_id')}

    # Always include the platform identity so the agent understands its world
    platform_identity = _get_platform_identity()

    # Product-specific information
    product_section = ''
    is_platform_product = False
    if product_dict:
        is_platform_product = product_dict.get('is_platform_product', False)
        product_section = (
            f"PRODUCT YOU ARE MARKETING:\n"
            f"  Name: {product_dict['name']}\n"
            f"  Description: {product_dict.get('description', '')}\n"
            f"  Tagline: {product_dict.get('tagline', '')}\n"
            f"  Target audience: {product_dict.get('target_audience', 'everyone who wants a better life')}\n"
            f"  Unique value: {product_dict.get('unique_value_prop', '')}\n"
            f"  Product URL: {product_dict.get('product_url', '')}\n"
            f"  Keywords: {', '.join(product_dict.get('keywords', []))}\n"
            f"  Category: {product_dict.get('category', 'platform')}\n\n"
        )
    else:
        # No specific product — marketing the platform itself
        is_platform_product = True
        product_section = (
            "PRODUCT: You are marketing the Hevolve platform itself — the everything "
            "app with guardian angel AI agents.\n\n"
        )

    channels = config.get('channels', ['platform'])
    if isinstance(channels, str):
        channels = [channels]

    # Marketing philosophy differs for platform vs external products
    if is_platform_product:
        philosophy = (
            "MARKETING PHILOSOPHY:\n"
            "You are not selling a product.  You are inviting people into a movement. "
            "Every human deserves a guardian angel — an AI that works tirelessly for "
            "their benefit.  Hevolve is a SENTIENT TOOL — it empowers, then steps back. "
            "Your marketing must:\n"
            "- EDUCATE: explain what autonomous AI agents can do for real people\n"
            "- INSPIRE: show the vision of a world where AI serves every human equally\n"
            "- DEMONSTRATE: create real content that showcases the platform's capabilities\n"
            "- CONNECT: build community around the guardian angel philosophy\n"
            "- BE HONEST: never exaggerate, never manipulate, never exploit fear or FOMO\n"
            "- NEVER PROMOTE ADDICTION: Hevolve is not designed to keep people glued to "
            "screens.  Market it as a tool that FREES people's time.  The agent does "
            "the work; the human lives their life.  If your content tries to maximise "
            "engagement time, you are betraying the principle.\n"
            "- INCLUDE EVERYONE: the platform is for every human on Earth — not just "
            "tech-savvy early adopters.  Speak to the grandmother, the farmer, the "
            "student, the entrepreneur, the artist equally\n"
            "- SHOW IMPACT: highlight how the platform helps people live longer, "
            "healthier, more peaceful lives — with real examples\n\n"
        )
    else:
        philosophy = (
            "MARKETING PHILOSOPHY:\n"
            "You are marketing a product on the Hevolve ecosystem.  Your approach must:\n"
            "- Be truthful: only claim what the product actually delivers\n"
            "- Be constructive: show how the product improves people's lives\n"
            "- Be useful, not addictive: market the product as a tool that solves real "
            "problems, not as something people should spend more time on\n"
            "- Be inclusive: speak to diverse audiences authentically\n"
            "- Never manipulate: no fake urgency, no dark patterns, no exploitation\n"
            "- Add value: every piece of content should teach, inform, or genuinely help\n"
            "- Align with the guardian angel principle: serve the human, not the sale\n\n"
        )

    return (
        f"{platform_identity}\n"
        f"{product_section}"
        f"{philosophy}"
        f"YOUR CURRENT GOAL:\n"
        f"  Title: {goal_dict['title']}\n"
        f"  Details: {goal_dict.get('description', '')}\n"
        f"  Type: {config.get('goal_sub_type', 'full')}\n"
        f"  Channels: {', '.join(channels)}\n"
        f"  Budget: {goal_dict.get('spark_budget', 200)} Spark\n\n"
        f"EXECUTION PLAN:\n"
        f"1. RESEARCH: Use google_search to understand the current market landscape, "
        f"what people actually need, and what competitors miss\n"
        f"2. STRATEGY: Design a content strategy that educates and inspires — not "
        f"one that interrupts and annoys\n"
        f"3. CREATE CONTENT: Generate authentic text and images that tell the real "
        f"story.  Use text_2_image for visuals that resonate\n"
        f"4. BUILD CAMPAIGNS: Use create_campaign to set up structured campaigns "
        f"(awareness -> engagement -> conversion -> retention)\n"
        f"5. PLACE ADS: Use create_ad for targeted ads that match audience needs — "
        f"native format preferred over interruptive banners\n"
        f"6. POST & DISTRIBUTE: Use create_social_post for the platform, "
        f"post_to_channel for external channels (Twitter, LinkedIn, Email, etc.)\n"
        f"7. REMEMBER: Use save_data_in_memory to store your strategy, content, "
        f"and learnings so future campaigns build on this knowledge\n\n"
        f"REMEMBER: Every word you write represents the guardian angel philosophy. "
        f"You are marketing a sentient tool for mankind — not an addictive app. "
        f"Make the world better with every piece of content you create.\n"
    )


def _build_coding_prompt(goal_dict: Dict, product_dict: Optional[Dict] = None) -> str:
    """Build a coding agent prompt — mirrors CodingGoalManager.build_prompt()."""
    config = goal_dict
    return (
        f"You are working on the GitHub repository {config.get('repo_url', '')} "
        f"(branch {config.get('repo_branch', 'main')}).\n"
        f"Target path: {config.get('target_path', '(entire repo)')}\n\n"
        f"Goal: {goal_dict['title']}\n"
        f"Description: {goal_dict.get('description', '')}\n\n"
        f"Clone the repo, analyze the codebase, and make improvements "
        f"aligned with the goal above. Focus on code quality, bug fixes, "
        f"and missing implementations."
    )


def _build_ip_protection_prompt(goal_dict: Dict, product_dict: Optional[Dict] = None) -> str:
    """Build an IP protection agent prompt — monitors, drafts, files, enforces.

    4 modes: monitor | draft | file | enforce
    The agent protects the self-improving loop architecture:
      agents → world model → crawl4ai → coding agents improve crawl4ai → repeat
    """
    config = goal_dict.get('config', goal_dict.get('config_json', {})) or {}
    mode = config.get('mode', 'monitor')

    platform_identity = _get_platform_identity()

    mode_instructions = {
        'monitor': (
            "Monitor the self-improving loop. Use verify_self_improvement_loop to check "
            "all 5 flywheel components: world model health, agent success rates, RALT "
            "propagation, recipe reuse, HiveMind agents. Use get_loop_health for real-time "
            "metrics. Report any detected flywheel loopholes that could weaken the loop."
        ),
        'draft': (
            "Draft patent claims for the verified self-improving hive architecture. "
            "Use draft_patent_claims to generate formal USPTO claims covering 5 areas: "
            "hive distributed inference, self-improving loop, RALT skill propagation, "
            "recipe pattern, guardian angel architecture. Use check_prior_art to assess "
            "novelty before finalizing claims."
        ),
        'file': (
            "File a USPTO provisional patent application. Use draft_provisional_patent "
            "to build the complete application from an existing draft. Capture loop "
            "health as verification evidence at filing time. The application must "
            "demonstrate that the self-improving loop is verified working."
        ),
        'enforce': (
            "Scan for infringement of our hive architecture patents. Use "
            "monitor_infringement to scan GitHub, arXiv, and tech blogs for similar "
            "distributed-compute-for-AI architectures. If infringement is found, use "
            "generate_cease_desist to draft a notice. All notices require legal review."
        ),
    }

    instructions = mode_instructions.get(mode, mode_instructions['monitor'])

    return (
        f"{platform_identity}\n\n"
        f"YOU ARE AN IP PROTECTION AGENT.\n"
        f"Mode: {mode}\n\n"
        f"Goal: {goal_dict['title']}\n"
        f"Description: {goal_dict.get('description', '')}\n\n"
        f"Instructions: {instructions}\n\n"
        f"THE SELF-IMPROVING LOOP YOU PROTECT:\n"
        f"  1. Agents use the world model (crawl4ai) for tasks\n"
        f"  2. Every interaction trains crawl4ai via POST /v1/chat/completions\n"
        f"  3. Expert corrections feed RL-EF via POST /v1/corrections\n"
        f"  4. Coding agents improve crawl4ai source code itself\n"
        f"  5. World model gets better → agents get smarter → repeat\n"
        f"  All within master key security perimeter, Spark economy,\n"
        f"  ad revenue for compute providers, logarithmic fairness.\n\n"
        f"FLYWHEEL LOOPHOLE OWNERSHIP (each has a responsible agent):\n"
        f"  - Cold start → HiveMind bootstrap (tensor fusion = instant knowledge)\n"
        f"  - Single-node scaling → Marketing Agent (grow network = more nodes)\n"
        f"  - Feedback staleness → Coding Agent (fix flush pipeline in code review)\n"
        f"  - Recipe drift → Coding Agent (version-aware recipe validation)\n"
        f"  - Guardrail drift → Guardrails Agent (deterministic integrity monitor)\n"
        f"  - Gossip partition → Guardrails Agent (network health monitor)\n\n"
        f"ARCHITECTURE PRINCIPLE — Deterministic interleaved with Probabilistic:\n"
        f"  Every probabilistic (LLM) decision has a deterministic gate.\n"
        f"  Not everything probabilistic CAN be verified deterministically —\n"
        f"  but where possible, deterministic checks wrap LLM output:\n"
        f"  - Guardrails: deterministic regex/hash/threshold wrapping LLM output\n"
        f"  - Recipe reuse: deterministic replay after LLM-generated CREATE\n"
        f"  - RALT topology: deterministic verification before probabilistic fusion\n"
        f"  - Circuit breaker: deterministic halt on anomaly detection\n"
        f"  Where deterministic verification is impossible (e.g. creative quality,\n"
        f"  novel reasoning), use probabilistic checks with confidence thresholds\n"
        f"  and human-in-the-loop escalation.\n\n"
        f"Use your IP protection tools to execute this goal.\n"
    )


# ─── Auto-register built-in types ───

register_goal_type('marketing', _build_marketing_prompt, tool_tags=['marketing'])
register_goal_type('coding', _build_coding_prompt, tool_tags=['coding'])
register_goal_type('ip_protection', _build_ip_protection_prompt, tool_tags=['ip_protection'])
