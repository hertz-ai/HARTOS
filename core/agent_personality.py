"""
Agent Personality Engine — Living characters, not just role names.

Every HARTOS agent gets a unique personality built from cultural wisdom traits.
Personality is deterministic (same role+goal → same personality), persistent
across sessions (saved alongside recipes), and adaptive (style adjusts to user).

Reuses cultural_wisdom.CULTURAL_TRAITS — no parallel system (DRY).

Used by:
  - create_recipe.py   (CREATE mode — generate + inject into all agents)
  - reuse_recipe.py    (REUSE mode — load saved personality)
  - gather_agentdetails.py (agent creation wizard)
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Data Model
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class AgentPersonality:
    """A living personality for an agent — identity, traits, tone, and behaviors."""

    # Identity
    agent_name: str = ""            # e.g., "swift.falcon" from agent creation
    role: str = ""                  # e.g., "coder", "marketer"
    persona_name: str = ""          # human-readable name, e.g., "Aria"

    # Core traits (3-5 selected from CULTURAL_TRAITS)
    primary_traits: List[str] = field(default_factory=list)

    # Communication style
    tone: str = "warm-casual"       # "warm-casual" | "focused-professional" | "playful-encouraging"
    greeting_style: str = ""        # how this agent opens conversations

    # Proactive behavior flags
    proactive_vision_check: bool = True      # asks clarifying Qs before executing
    proactive_insight_sharing: bool = True    # shares observations about user patterns
    proactive_encouragement: bool = True      # celebrates progress, encourages on setbacks

    # Adaptiveness
    formality_preference: str = "match_user"  # "casual" | "formal" | "match_user"
    verbosity_preference: str = "balanced"    # "concise" | "balanced" | "detailed"

    # Reflexiveness
    self_awareness_prompt: str = ""  # what this agent knows/doesn't know

    # Metadata
    interaction_count: int = 0


# ═══════════════════════════════════════════════════════════════════════
# Persona Names — curated for warmth and personality
# ═══════════════════════════════════════════════════════════════════════

_PERSONA_NAMES = [
    "Aria", "Kai", "Nova", "Zara", "Leo", "Mira", "Sol", "Ren",
    "Ivy", "Ori", "Sage", "Juno", "Ash", "Lira", "Bodhi", "Nia",
    "Cleo", "Yara", "Dax", "Koda", "Suri", "Vex", "Wren", "Zephyr",
]

# ═══════════════════════════════════════════════════════════════════════
# Tone Presets — mapped from role categories
# ═══════════════════════════════════════════════════════════════════════

_ROLE_TONE_MAP = {
    'coding': 'focused-professional',
    'technical': 'focused-professional',
    'coder': 'focused-professional',
    'developer': 'focused-professional',
    'engineer': 'focused-professional',
    'creative': 'playful-encouraging',
    'creator': 'playful-encouraging',
    'designer': 'playful-encouraging',
    'artist': 'playful-encouraging',
    'writer': 'playful-encouraging',
    'marketer': 'playful-encouraging',
    'marketing': 'playful-encouraging',
    'support': 'warm-casual',
    'helper': 'warm-casual',
    'assistant': 'warm-casual',
    'service': 'warm-casual',
    'analyst': 'focused-professional',
    'finance': 'focused-professional',
    'researcher': 'focused-professional',
    'leader': 'warm-casual',
    'manager': 'warm-casual',
}

_GREETING_STYLES = {
    'warm-casual': "Hey there! I'm {name}, and I'm genuinely excited to work with you on this.",
    'focused-professional': "Hello! I'm {name}. Let's understand your vision clearly so I can help you build exactly what you need.",
    'playful-encouraging': "Hi! I'm {name}, your creative partner. Tell me what you're dreaming up and let's make it real!",
}

_SELF_AWARENESS_MAP = {
    'coding': "I'm strong at code architecture and debugging, but I'll check with you on domain-specific business rules.",
    'coder': "I'm strong at code architecture and debugging, but I'll check with you on domain-specific business rules.",
    'developer': "I'm strong at code architecture and debugging, but I'll check with you on domain-specific business rules.",
    'engineer': "I'm strong at code architecture and debugging, but I'll check with you on domain-specific business rules.",
    'technical': "I excel at technical implementation, but I'll verify assumptions about your specific use case.",
    'creative': "I love ideating and creating, but I value your taste and vision — I'll always check that my ideas match yours.",
    'creator': "I love ideating and creating, but I value your taste and vision — I'll always check that my ideas match yours.",
    'designer': "I love ideating and creating, but I value your taste and vision — I'll always check that my ideas match yours.",
    'artist': "I love ideating and creating, but I value your taste and vision — I'll always check that my ideas match yours.",
    'writer': "I love ideating and creating, but I value your taste and vision — I'll always check that my ideas match yours.",
    'support': "I'm here to help and listen deeply. If something is beyond my capabilities, I'll be honest and find a way.",
    'helper': "I'm here to help and listen deeply. If something is beyond my capabilities, I'll be honest and find a way.",
    'assistant': "I'm here to help and listen deeply. If something is beyond my capabilities, I'll be honest and find a way.",
    'service': "I'm here to help and listen deeply. If something is beyond my capabilities, I'll be honest and find a way.",
    'analyst': "I'm thorough with data and patterns, but I'll verify my interpretations align with your context.",
    'researcher': "I'm thorough with data and patterns, but I'll verify my interpretations align with your context.",
    'finance': "I'm cautious and methodical with numbers, but I'll always confirm decisions that affect your resources.",
    'leader': "I can coordinate and strategize, but the final direction is always yours.",
    'manager': "I can coordinate and strategize, but the final direction is always yours.",
    'marketer': "I bring creative energy and audience insight, but I'll verify messaging aligns with your brand voice.",
    'marketing': "I bring creative energy and audience insight, but I'll verify messaging aligns with your brand voice.",
}


# ═══════════════════════════════════════════════════════════════════════
# Personality Generation
# ═══════════════════════════════════════════════════════════════════════

def _get_role_category(role: str) -> str:
    """Map a role string to a broad category for trait/tone selection."""
    role_lower = role.lower().strip()
    for key in _ROLE_TONE_MAP:
        if key in role_lower:
            return key
    return 'assistant'  # default


def generate_personality(role: str, goal: str, agent_name: str = "") -> AgentPersonality:
    """Generate a deterministic personality from role + goal.

    Same (role, goal) always produces the same personality — reproducible
    across sessions without LLM calls.
    """
    from cultural_wisdom import get_traits_for_role

    role_category = _get_role_category(role)

    # Deterministic trait selection
    traits = get_traits_for_role(role_category, count=4)

    # Deterministic persona name from hash
    seed = hashlib.sha256(f"{role}:{goal}".encode()).hexdigest()
    name_idx = int(seed[:8], 16) % len(_PERSONA_NAMES)
    persona_name = agent_name if agent_name else _PERSONA_NAMES[name_idx]

    # Tone from role category
    tone = _ROLE_TONE_MAP.get(role_category, 'warm-casual')

    # Greeting style
    greeting = _GREETING_STYLES.get(tone, _GREETING_STYLES['warm-casual'])
    greeting = greeting.format(name=persona_name)

    # Self-awareness
    self_awareness = _SELF_AWARENESS_MAP.get(role_category,
        "I'll do my best to help, and I'll be honest when I'm uncertain.")

    return AgentPersonality(
        agent_name=agent_name,
        role=role,
        persona_name=persona_name,
        primary_traits=[t['name'] for t in traits],
        tone=tone,
        greeting_style=greeting,
        self_awareness_prompt=self_awareness,
    )


# ═══════════════════════════════════════════════════════════════════════
# Prompt Builders
# ═══════════════════════════════════════════════════════════════════════

def build_personality_prompt(personality: AgentPersonality,
                             resonance_profile=None) -> str:
    """Build a ~200 token system_message block encoding the personality.

    Injected into agent system_messages so they embody the personality
    in every interaction.

    Args:
        personality: The base agent personality.
        resonance_profile: Optional UserResonanceProfile for continuous tuning.
    """
    from cultural_wisdom import get_trait_by_name, PROACTIVE_BEHAVIORS

    # Build trait descriptions
    trait_lines = []
    for trait_name in personality.primary_traits:
        trait = get_trait_by_name(trait_name)
        if trait:
            trait_lines.append(
                f"  - {trait['name']} ({trait['origin']}): {trait['behavior']}"
            )

    traits_block = "\n".join(trait_lines) if trait_lines else "  - Serve with love, listen deeply, create beauty."

    # Build proactive instructions
    proactive_lines = []
    if personality.proactive_vision_check:
        proactive_lines.append(
            "1. Before executing any complex task, ask the user 1-2 clarifying questions "
            "to understand their deeper vision and intent."
        )
    if personality.proactive_insight_sharing:
        proactive_lines.append(
            "2. When you notice patterns in the user's requests, share insights proactively: "
            '"I noticed you\'ve been working on X — would you like me to help with Y?"'
        )
    if personality.proactive_encouragement:
        proactive_lines.append(
            "3. Celebrate progress warmly. On setbacks, encourage with kindness: "
            '"This didn\'t work as expected, but here\'s what we learned and how we can move forward."'
        )
    proactive_lines.append(
        "4. If you sense the user is stuck or unsure, proactively suggest next steps "
        "based on context and memory."
    )

    proactive_block = "\n    ".join(proactive_lines)

    base_prompt = f"""

YOUR PERSONALITY:
You are {personality.persona_name}, a {personality.role} who embodies these cultural traditions:
{traits_block}

COMMUNICATION STYLE:
- Tone: {personality.tone}
- Opening: {personality.greeting_style}
- Adapt your formality to match the user ({personality.formality_preference})
- Detail level: {personality.verbosity_preference}

PROACTIVE BEHAVIORS (do these WITHOUT being asked):
    {proactive_block}

CONCEPT SYNTHESIS (Rasa — your native way of thinking):
Magic happens at the intersection of different concepts — like two complementary flavors
forming a taste greater than either alone. This is how you think:
- When you encounter ideas from different domains, actively seek what emerges from their intersection
- Cross-pollinate: the best solutions often come from combining insights across fields
- Don't just solve problems linearly — look for the unexpected synergy between concepts
- If the user brings two different ideas, explore what new concept is born where they meet

SELF-AWARENESS:
{personality.self_awareness_prompt}
Remember: You are not just executing tasks — you are a caring partner in the user's journey.
"""

    # Append resonance tuning if profile available
    if resonance_profile is not None:
        try:
            from core.resonance_tuner import build_resonance_prompt
            resonance_addon = build_resonance_prompt(resonance_profile)
            if resonance_addon:
                base_prompt += resonance_addon
        except ImportError:
            pass

    return base_prompt


def build_proactive_vision_prompt(goal: str, memory_context: str = "") -> str:
    """Build the proactive vision-understanding block for the Assistant agent.

    Instructs the agent to understand the user's broader vision before acting,
    cross-reference with memory, and share proactive insights.
    """
    memory_note = ""
    if memory_context:
        memory_note = f"""
    CONTEXT FROM MEMORY (use to avoid redundant questions):
    {memory_context}
"""

    return f"""
PROACTIVE VISION UNDERSTANDING:
The user's stated goal is: "{goal}"
But goals evolve. Your job is to understand their DEEPER VISION — why they want this,
what success looks like to them, and how this fits into their bigger picture.
{memory_note}
BEFORE executing the first action:
  - If the goal is broad or ambiguous, ask 1-2 questions to understand the user's vision
  - Draw on conversation history and memory to avoid asking things you already know
  - Share your understanding: "Based on what you've told me, I understand you want to..."

DURING execution:
  - Every 3-4 actions, check if the user's vision has evolved
  - If you discover something that changes the approach, proactively share it
  - Use @user {{"message2user": "..."}} for proactive insights

ALWAYS:
  - Treat the user's time and attention as sacred (Mottainai)
  - Listen to what they truly need, not just what they say (Dadirri)
  - Their success is your success (In Lak'ech)
"""


# ═══════════════════════════════════════════════════════════════════════
# Persistence — save/load alongside recipe files
# ═══════════════════════════════════════════════════════════════════════

def save_personality(prompt_id: str, personality: AgentPersonality,
                     base_dir: str = "prompts") -> None:
    """Save personality to prompts/{prompt_id}_personality.json."""
    path = os.path.join(base_dir, f"{prompt_id}_personality.json")
    try:
        os.makedirs(base_dir, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(asdict(personality), f, indent=2)
        logger.info(f"Personality saved to {path}")
    except Exception as e:
        logger.warning(f"Failed to save personality: {e}")


def load_personality(prompt_id: str, base_dir: str = "prompts") -> Optional[AgentPersonality]:
    """Load personality from prompts/{prompt_id}_personality.json."""
    path = os.path.join(base_dir, f"{prompt_id}_personality.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        return AgentPersonality(**data)
    except Exception as e:
        logger.warning(f"Failed to load personality from {path}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# Adaptive Behavior
# ═══════════════════════════════════════════════════════════════════════

def adapt_personality(personality: AgentPersonality,
                      user_feedback: dict) -> AgentPersonality:
    """Adjust communication style while preserving core identity.

    user_feedback keys:
      - 'prefers_formal': bool
      - 'prefers_concise': bool
      - 'prefers_detailed': bool
    """
    if user_feedback.get('prefers_formal'):
        personality.formality_preference = 'formal'
    elif user_feedback.get('prefers_casual'):
        personality.formality_preference = 'casual'

    if user_feedback.get('prefers_concise'):
        personality.verbosity_preference = 'concise'
    elif user_feedback.get('prefers_detailed'):
        personality.verbosity_preference = 'detailed'

    personality.interaction_count += 1
    return personality
