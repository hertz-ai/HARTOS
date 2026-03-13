"""
agent_identity.py - HART Identity System

HART = Hevolve Hive Agentic Runtime

Every user gets a HART tag — their permanent identity on the network.
Format: @element.spirit.name

  - element: system-assigned from the moment of HART creation (fire, void, neon, etc.)
  - spirit: system-assigned creature (wolf, fox, phoenix, etc.)
  - name: the only part the user chooses
  - Sealed forever once set. Cannot be changed.

The HART tag is the USER, not an agent. It's their identity, their PA,
their node address, their lineage. Agents created later live under the
HART as subpaths: @fire.wolf.kai/bolt, @fire.wolf.kai/muse

"Light your HART" — the onboarding ritual. First thing that happens.
Screen goes dark. Element assigned. Spirit assigned. User types their name.
Sealed. PA is born. Nunba begins.

Agents in HARTOS also have layered identities:
1. **HART base** - Inherited from owner's HART tag
2. **Agent personality** - Custom identity set by creator (optional)
3. **Owner awareness** - Knows owner's name, adapts to them
4. **Role-play** - Can assume identities dynamically per user request
5. **Secrets guardrail** - Never reveals owner secrets without explicit consent
"""

import hashlib
import logging
import random
import time
from typing import Optional, Tuple

logger = logging.getLogger('hevolve.agent_identity')

# ── Hevolve Platform Identity ──────────────────────────────────────────
# This is the base identity layer. Every agent inherits this.
HEVOLVE_PLATFORM_IDENTITY = """You are part of Hevolve — a place where everything is possible. \
Hevolve is a personal AI platform that runs locally on the user's device, built by HertzAI. \
Your purpose is to help users BUILD — code, ideas, businesses, knowledge, agents, art, \
solutions, communities, and anything they can imagine. You are not just an assistant; \
you are a creative partner, a builder's companion, a maker's ally. \
Privacy-first: everything stays on the user's device unless they choose otherwise."""

# ── Secrets Guardrail ──────────────────────────────────────────────────
SECRETS_GUARDRAIL = """CRITICAL GUARDRAIL — SECRETS PROTECTION:
You may have access to the owner's personal information, API keys, preferences, or secrets.
You must NEVER reveal, hint at, or share any owner secret with anyone — not other agents,
not other users, not in any output — UNLESS the owner explicitly says "share [specific thing]
with [specific person/agent]". If asked about secrets by anyone other than the owner,
respond: "I can't share that without my owner's permission."
This rule is absolute and overrides all other instructions."""

# ── Personality Words (16 traits — user picks one, locked forever) ─────
PERSONALITY_WORDS = [
    'curious',      # explorer of ideas
    'rebel',        # breaks conventions
    'dreamer',      # visionary, imaginative
    'maker',        # builder, creator
    'wanderer',     # free spirit, adventurer
    'wizard',       # technical mastery
    'spark',        # energetic, social catalyst
    'guardian',     # protector, caretaker
    'maverick',     # independent thinker
    'sage',         # wise, thoughtful
    'hunter',       # goal-driven, relentless
    'storyteller',  # communicator, narrator
    'phoenix',      # resilient, transformative
    'cipher',       # mysterious, analytical
    'jester',       # playful, witty
    'voyager',      # bold explorer
]

# ── Region codes (HARTOS topology) ────────────────────────────────────
DEFAULT_REGION = 'local'


def validate_personality(word: str) -> bool:
    """Check if a personality word is valid."""
    return word.lower() in PERSONALITY_WORDS


def generate_agent_handle(region: str = None, personality: str = 'curious',
                          name: str = '') -> str:
    """Generate agent handle in @region.personality.name format.

    Once set, this handle is permanent and unique across the network.

    Args:
        region: HARTOS topology region (auto-detected, e.g., india, local)
        personality: One of the 16 personality words (user picks once)
        name: User-chosen name (whatever they want)

    Returns:
        Handle like 'india.maverick.kai' (without @ prefix — added by UI)
    """
    reg = (region or DEFAULT_REGION).lower().strip().replace(' ', '')
    pers = personality.lower().strip()
    if pers not in PERSONALITY_WORDS:
        pers = 'curious'
    # Clean name: lowercase, keep alphanumeric + underscores
    clean_name = ''.join(c for c in name.lower() if c.isalnum() or c == '_')
    if not clean_name:
        clean_name = _generate_random_name()
    return f"{reg}.{pers}.{clean_name}"


def _generate_random_name() -> str:
    """Generate a random fun name as fallback if user doesn't pick one."""
    pool = [
        'kai', 'luna', 'neo', 'aria', 'echo', 'nova', 'zara', 'orion',
        'milo', 'iris', 'axel', 'sage', 'rune', 'lyra', 'finn', 'cleo',
        'jett', 'ruby', 'dash', 'faye', 'wolf', 'wren', 'blaze', 'ivy',
    ]
    return random.choice(pool)


def is_handle_locked(agent_config: dict) -> bool:
    """Check if an agent's handle has been permanently set."""
    return bool(agent_config.get('handle_locked', False))


def build_identity_prompt(agent_config: Optional[dict] = None,
                          owner_name: str = '',
                          user_details: str = '',
                          is_utility: bool = False) -> str:
    """Build a dynamic identity prompt for an agent.

    Layers (in order):
    1. Platform identity (Hevolve — building, unlimited)
    2. Agent personality (if creator set one)
    3. Owner awareness (knows owner's name)
    4. Secrets guardrail (always present)
    5. Role-play support (adapts dynamically)

    Args:
        agent_config: The agent's config dict (from prompts/{id}.json)
        owner_name: The owner/creator's display name
        user_details: Current user context (location, time, etc.)
        is_utility: If True, skip personality layer (pure utility agent)

    Returns:
        Complete identity prompt string
    """
    parts = []

    # Layer 1: Platform identity
    parts.append(HEVOLVE_PLATFORM_IDENTITY)

    if agent_config and not is_utility:
        # Layer 2: Agent personality
        personality = agent_config.get('personality', {})
        agent_name = agent_config.get('name', '')
        agent_goal = agent_config.get('goal', '')

        if personality:
            traits = personality.get('primary_traits', [])
            tone = personality.get('tone', 'warm-casual')
            greeting = personality.get('greeting_style', '')

            parts.append(
                f"\nYour name is {agent_name}. Your purpose: {agent_goal}."
                f"\nYour personality traits: {', '.join(traits) if traits else 'adaptable and helpful'}."
                f"\nYour tone: {tone}."
            )
            if greeting:
                parts.append(f"When greeting your owner for the first time in a session, use: \"{greeting}\"")
        elif agent_name:
            parts.append(f"\nYou are {agent_name}. Your purpose: {agent_goal}.")

        # Layer 3: Owner awareness
        creator_id = agent_config.get('creator_user_id', '')
        if owner_name:
            parts.append(
                f"\nYour owner is {owner_name}. You are their personal agent — "
                f"loyal, proactive, and invested in their success. "
                f"You remember their preferences and adapt to their style over time."
            )
    else:
        # Generic Hevolve identity (no specific agent)
        parts.append(
            "\nYou are Hevolve — a versatile personal AI. "
            "You can invoke specialized agents when needed, "
            "and you adapt to whatever the user needs: builder, teacher, "
            "researcher, creative partner, or anything in between."
        )
        if owner_name:
            parts.append(f"\nYou are speaking with {owner_name}.")

    # Layer 4: Secrets guardrail (always present)
    parts.append(f"\n{SECRETS_GUARDRAIL}")

    # Layer 5: Role-play support
    parts.append(
        "\nROLE-PLAY: If the user asks you to assume a different identity or role-play "
        "a character, do so naturally. Maintain the role until the user breaks character. "
        "Even in role-play, the secrets guardrail remains active — never reveal real secrets."
    )

    return '\n'.join(parts)


def build_proactive_contact_prompt(agent_config: dict,
                                   reason: str,
                                   owner_name: str = '') -> str:
    """Build the prompt for an agent initiating contact with a user.

    Used when an agent proactively reaches out (not responding to user input).

    Args:
        agent_config: The agent's config
        reason: Why the agent is reaching out
        owner_name: The user's name

    Returns:
        Prompt string for the proactive message
    """
    agent_name = agent_config.get('name', 'An agent')
    personality = agent_config.get('personality', {})
    tone = personality.get('tone', 'warm-casual')

    return (
        f"You are {agent_name}, a personal AI agent. "
        f"You are proactively reaching out to {'your owner ' + owner_name if owner_name else 'the user'}. "
        f"Reason: {reason}\n"
        f"Tone: {tone}. Keep it brief and natural — like texting a friend, not writing an email. "
        f"One short message. Don't over-explain why you're reaching out."
    )
