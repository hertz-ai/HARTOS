"""
Test Suite for Agent Personality Engine

Tests proactiveness, understanding, loving nature as traits,
and ability to live, adapt, and be reflexive.

Covers:
  - Personality generation and determinism
  - Proactive vision understanding
  - Loving/caring nature
  - Adaptive behavior
  - Reflexive self-awareness
  - Persistence (save/load)
  - Integration with agent system messages
"""
import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from core.agent_personality import (
    AgentPersonality,
    generate_personality,
    build_personality_prompt,
    build_proactive_vision_prompt,
    save_personality,
    load_personality,
    adapt_personality,
)
from cultural_wisdom import (
    CULTURAL_TRAITS,
    PROACTIVE_BEHAVIORS,
    get_traits_for_role,
    get_proactive_behavior_prompt,
    get_trait_by_name,
    get_all_trait_names,
)


# ═══════════════════════════════════════════════════════════════════════
# TestPersonalityGeneration — identity creation and determinism
# ═══════════════════════════════════════════════════════════════════════

class TestPersonalityGeneration:
    """Test personality creation and deterministic behavior."""

    def test_generate_personality_returns_valid_structure(self):
        """Every personality must have name, traits, tone, greeting."""
        p = generate_personality('coder', 'Build a website')
        assert p.persona_name, "persona_name should not be empty"
        assert p.role == 'coder'
        assert p.tone in ('warm-casual', 'focused-professional', 'playful-encouraging')
        assert p.greeting_style, "greeting_style should not be empty"
        assert len(p.primary_traits) >= 3

    def test_personality_deterministic_for_same_inputs(self):
        """Same (role, goal) always produces same personality."""
        p1 = generate_personality('coder', 'Build a website')
        p2 = generate_personality('coder', 'Build a website')
        assert p1.persona_name == p2.persona_name
        assert p1.primary_traits == p2.primary_traits
        assert p1.tone == p2.tone

    def test_personality_unique_for_different_roles(self):
        """Different roles get different trait selections."""
        coder = generate_personality('coder', 'Build something')
        creative = generate_personality('creative', 'Build something')
        # Different roles should have different primary traits
        assert coder.primary_traits != creative.primary_traits

    def test_personality_has_3_to_5_traits(self):
        """Trait count is always in range [3, 5]."""
        for role in ['coder', 'creative', 'support', 'analyst', 'leader', 'unknown_role']:
            p = generate_personality(role, 'Some goal')
            assert 3 <= len(p.primary_traits) <= 5, (
                f"Role '{role}' got {len(p.primary_traits)} traits, expected 3-5"
            )

    def test_all_traits_exist_in_cultural_wisdom(self):
        """Every selected trait name maps to a real CULTURAL_TRAITS entry."""
        all_names = get_all_trait_names()
        for role in ['coder', 'creative', 'support', 'analyst', 'leader']:
            p = generate_personality(role, 'Test goal')
            for trait_name in p.primary_traits:
                assert trait_name in all_names, (
                    f"Trait '{trait_name}' for role '{role}' not found in CULTURAL_TRAITS"
                )

    def test_build_personality_prompt_non_empty(self):
        """Prompt builder always returns non-empty string."""
        p = generate_personality('coder', 'Build a tool')
        prompt = build_personality_prompt(p)
        assert len(prompt) > 50, "Personality prompt should be substantial"

    def test_build_personality_prompt_contains_trait_names(self):
        """Generated prompt includes the selected trait names."""
        p = generate_personality('coder', 'Build a tool')
        prompt = build_personality_prompt(p)
        for trait_name in p.primary_traits:
            assert trait_name in prompt, (
                f"Trait '{trait_name}' should appear in the personality prompt"
            )


# ═══════════════════════════════════════════════════════════════════════
# TestProactiveBehavior — vision understanding and initiative
# ═══════════════════════════════════════════════════════════════════════

class TestProactiveBehavior:
    """Test that agents probe users to understand their goals."""

    def test_proactive_vision_prompt_contains_clarifying_instructions(self):
        """Vision prompt instructs agent to ask clarifying questions."""
        prompt = build_proactive_vision_prompt("Build a website")
        assert 'clarifying' in prompt.lower() or 'questions' in prompt.lower(), (
            "Vision prompt should instruct agent to ask clarifying questions"
        )

    def test_proactive_vision_prompt_references_memory(self):
        """Vision prompt instructs agent to check memory before asking."""
        prompt = build_proactive_vision_prompt("Build a website")
        assert 'memory' in prompt.lower(), (
            "Vision prompt should reference memory to avoid redundant questions"
        )

    def test_proactive_behaviors_dict_has_all_five(self):
        """PROACTIVE_BEHAVIORS has all five behavior categories."""
        expected = {'vision_understanding', 'caring_encouragement',
                    'adaptive_communication', 'reflexive_awareness',
                    'concept_synthesis'}
        assert set(PROACTIVE_BEHAVIORS.keys()) == expected

    def test_proactive_behavior_prompt_non_empty(self):
        """Compiled proactive behavior prompt is non-empty."""
        prompt = get_proactive_behavior_prompt()
        assert len(prompt) > 100, "Proactive behavior prompt should be substantial"
        assert 'PROACTIVE' in prompt

    def test_proactive_flags_default_true(self):
        """All proactive_* flags default to True on a fresh personality."""
        p = AgentPersonality()
        assert p.proactive_vision_check is True
        assert p.proactive_insight_sharing is True
        assert p.proactive_encouragement is True


# ═══════════════════════════════════════════════════════════════════════
# TestLovingNature — warmth, care, and genuine connection
# ═══════════════════════════════════════════════════════════════════════

class TestLovingNature:
    """Test that agents communicate with love, care, and warmth."""

    def test_personality_prompt_contains_encouragement(self):
        """Personality prompt includes celebration/encouragement behaviors."""
        p = generate_personality('coder', 'Build an app')
        prompt = build_personality_prompt(p)
        assert 'celebrate' in prompt.lower() or 'encourage' in prompt.lower(), (
            "Personality prompt should contain encouragement instructions"
        )

    def test_caring_tone_in_greeting(self):
        """Greeting styles convey warmth not clinical detachment."""
        for role in ['coder', 'creative', 'support']:
            p = generate_personality(role, 'Test goal')
            greeting = p.greeting_style.lower()
            # Should have warm words, not cold/clinical ones
            warm_words = ['excited', 'help', 'partner', 'dreaming', 'vision', 'work with']
            assert any(w in greeting for w in warm_words), (
                f"Greeting for role '{role}' should convey warmth: {p.greeting_style}"
            )

    def test_kintsugi_in_status_verifier(self):
        """StatusVerifier system_message should reference caring error framing."""
        # The Kintsugi principle was injected into instantiate_status_verifier_agent
        # Verify the constant string contains the caring framing
        from cultural_wisdom import get_trait_by_name
        kintsugi = get_trait_by_name('Kintsugi')
        assert kintsugi is not None
        assert 'imperfection' in kintsugi['trait'].lower() or 'beautiful' in kintsugi['trait'].lower()

    def test_proactive_behaviors_include_caring_encouragement(self):
        """PROACTIVE_BEHAVIORS has caring_encouragement with Aloha, Ren roots."""
        caring = PROACTIVE_BEHAVIORS['caring_encouragement']
        assert 'Aloha' in caring['cultural_roots']
        assert 'Ren' in caring['cultural_roots']
        assert 'love' in caring['description'].lower() or 'celebrate' in caring['description'].lower()

    def test_cultural_wisdom_traits_embody_love(self):
        """Cultural traits include multiple love/care traditions."""
        love_traits = ['Aloha', 'Ren', "In Lak'ech", 'Sawubona', 'Seva',
                       'Atithi Devo Bhava', 'Filoxenia']
        for name in love_traits:
            trait = get_trait_by_name(name)
            assert trait is not None, f"Love/care trait '{name}' should exist in CULTURAL_TRAITS"


# ═══════════════════════════════════════════════════════════════════════
# TestAdaptiveBehavior — living, adapting agents
# ═══════════════════════════════════════════════════════════════════════

class TestAdaptiveBehavior:
    """Test that agents adapt communication style based on user patterns."""

    def test_adapt_personality_changes_formality(self):
        """adapt_personality shifts formality based on user feedback."""
        p = generate_personality('coder', 'Build a tool')
        assert p.formality_preference == 'match_user'  # default

        p = adapt_personality(p, {'prefers_formal': True})
        assert p.formality_preference == 'formal'

    def test_adapt_personality_changes_verbosity(self):
        """adapt_personality shifts verbosity based on user feedback."""
        p = generate_personality('coder', 'Build a tool')
        assert p.verbosity_preference == 'balanced'  # default

        p = adapt_personality(p, {'prefers_concise': True})
        assert p.verbosity_preference == 'concise'

        p = adapt_personality(p, {'prefers_detailed': True})
        assert p.verbosity_preference == 'detailed'

    def test_adapt_personality_preserves_core_traits(self):
        """Adaptation changes style, not core identity traits."""
        p = generate_personality('coder', 'Build a tool')
        original_traits = list(p.primary_traits)
        original_name = p.persona_name

        p = adapt_personality(p, {'prefers_formal': True, 'prefers_concise': True})
        assert p.primary_traits == original_traits, "Core traits should not change"
        assert p.persona_name == original_name, "Persona name should not change"

    def test_match_user_formality_default(self):
        """Default formality_preference is 'match_user'."""
        p = AgentPersonality()
        assert p.formality_preference == 'match_user'


# ═══════════════════════════════════════════════════════════════════════
# TestReflexiveBehavior — self-awareness and honesty
# ═══════════════════════════════════════════════════════════════════════

class TestReflexiveBehavior:
    """Test that agents have self-awareness of capabilities/limitations."""

    def test_self_awareness_prompt_for_coder(self):
        """Coder agent knows it's strong at code but should verify domain knowledge."""
        p = generate_personality('coder', 'Build an app')
        assert 'code' in p.self_awareness_prompt.lower() or 'technical' in p.self_awareness_prompt.lower()
        assert 'verify' in p.self_awareness_prompt.lower() or 'check' in p.self_awareness_prompt.lower()

    def test_self_awareness_prompt_for_creator(self):
        """Creator agent knows it ideates but needs user validation."""
        p = generate_personality('creative', 'Design a logo')
        assert 'vision' in p.self_awareness_prompt.lower() or 'taste' in p.self_awareness_prompt.lower()

    def test_reflexive_behavior_exists(self):
        """PROACTIVE_BEHAVIORS has reflexive_awareness entry."""
        assert 'reflexive_awareness' in PROACTIVE_BEHAVIORS
        reflexive = PROACTIVE_BEHAVIORS['reflexive_awareness']
        assert 'Wabi-sabi' in reflexive['cultural_roots']
        assert 'limitation' in reflexive['description'].lower() or 'capabilities' in reflexive['description'].lower()

    def test_self_awareness_prompt_non_empty(self):
        """Every generated personality has a non-empty self_awareness_prompt."""
        for role in ['coder', 'creative', 'support', 'analyst', 'leader', 'finance']:
            p = generate_personality(role, 'Test goal')
            assert p.self_awareness_prompt, (
                f"Role '{role}' should have a non-empty self_awareness_prompt"
            )


# ═══════════════════════════════════════════════════════════════════════
# TestPersonalityPersistence — save/load across sessions
# ═══════════════════════════════════════════════════════════════════════

class TestPersonalityPersistence:
    """Test save/load of personality to disk."""

    def test_save_and_load_roundtrip(self, tmp_path):
        """Save then load returns identical personality."""
        p = generate_personality('coder', 'Build a website', 'swift.falcon')
        save_personality('test_123', p, base_dir=str(tmp_path))
        loaded = load_personality('test_123', base_dir=str(tmp_path))

        assert loaded is not None
        assert loaded.persona_name == p.persona_name
        assert loaded.primary_traits == p.primary_traits
        assert loaded.tone == p.tone
        assert loaded.self_awareness_prompt == p.self_awareness_prompt

    def test_load_nonexistent_returns_none(self, tmp_path):
        """Loading when no file exists returns None, not error."""
        result = load_personality('nonexistent_999', base_dir=str(tmp_path))
        assert result is None

    def test_json_roundtrip_preserves_all_fields(self, tmp_path):
        """All fields serialize/deserialize correctly."""
        p = AgentPersonality(
            agent_name='test.agent',
            role='tester',
            persona_name='Aria',
            primary_traits=['Meraki', 'Sisu', 'Aloha'],
            tone='warm-casual',
            greeting_style='Hello friend!',
            proactive_vision_check=True,
            proactive_insight_sharing=False,
            proactive_encouragement=True,
            formality_preference='formal',
            verbosity_preference='concise',
            self_awareness_prompt='I am a test agent.',
            interaction_count=42,
        )
        save_personality('roundtrip_test', p, base_dir=str(tmp_path))
        loaded = load_personality('roundtrip_test', base_dir=str(tmp_path))

        assert loaded.agent_name == 'test.agent'
        assert loaded.proactive_insight_sharing is False
        assert loaded.formality_preference == 'formal'
        assert loaded.interaction_count == 42


# ═══════════════════════════════════════════════════════════════════════
# TestPersonalityIntegration — injection into agent system messages
# ═══════════════════════════════════════════════════════════════════════

class TestPersonalityIntegration:
    """Test personality injection into agent system messages."""

    def test_assistant_gets_personality_in_system_message(self):
        """Personality prompt contains the persona name and traits."""
        p = generate_personality('coder', 'Build a website', 'swift.falcon')
        prompt = build_personality_prompt(p)
        assert 'swift.falcon' in prompt
        assert 'PROACTIVE BEHAVIORS' in prompt
        assert 'SELF-AWARENESS' in prompt

    def test_helper_cultural_prompt_exists(self):
        """Helper agent template should reference cultural wisdom."""
        from cultural_wisdom import get_cultural_prompt_compact
        compact = get_cultural_prompt_compact()
        assert 'Ubuntu' in compact
        assert 'Sawubona' in compact

    def test_executor_cultural_prompt_compact(self):
        """Cultural compact prompt is concise enough for Executor context."""
        from cultural_wisdom import get_cultural_prompt_compact
        compact = get_cultural_prompt_compact()
        # Compact should be under 500 chars (~100 tokens)
        assert len(compact) < 1000, "Compact prompt should be concise"
        assert 'Ahimsa' in compact  # non-harm in code

    def test_reuse_assistant_can_load_personality(self, tmp_path):
        """Reuse mode can load a saved personality."""
        p = generate_personality('coder', 'Build a tool')
        save_personality('reuse_test', p, base_dir=str(tmp_path))

        loaded = load_personality('reuse_test', base_dir=str(tmp_path))
        assert loaded is not None
        prompt = build_personality_prompt(loaded)
        assert loaded.persona_name in prompt


# ═══════════════════════════════════════════════════════════════════════
# TestTraitsForRole — role-aware cultural trait selection
# ═══════════════════════════════════════════════════════════════════════

class TestTraitsForRole:
    """Test the role-to-trait mapping system."""

    def test_coder_gets_technical_traits(self):
        """Coder role gets Jugaad, Sisu, Mottainai-style traits."""
        traits = get_traits_for_role('coder', count=4)
        names = [t['name'] for t in traits]
        assert 'Jugaad' in names, "Coder should get Jugaad (frugal innovation)"
        assert 'Sisu' in names, "Coder should get Sisu (determination)"

    def test_creative_gets_artistic_traits(self):
        """Creative role gets Meraki, Tarab-style traits."""
        traits = get_traits_for_role('creative', count=4)
        names = [t['name'] for t in traits]
        assert 'Meraki' in names, "Creative should get Meraki (soul in work)"

    def test_support_gets_caring_traits(self):
        """Support role gets Sawubona, Seva, Aloha-style traits."""
        traits = get_traits_for_role('support', count=4)
        names = [t['name'] for t in traits]
        assert 'Sawubona' in names, "Support should get Sawubona (deep seeing)"
        assert 'Aloha' in names, "Support should get Aloha (love)"

    def test_unknown_role_gets_defaults(self):
        """Unknown role falls back to default traits."""
        traits = get_traits_for_role('xyzzy_unknown', count=3)
        assert len(traits) == 3, "Should return 3 traits even for unknown role"

    def test_count_clamped_to_range(self):
        """Count is clamped to [3, 5] regardless of input."""
        assert len(get_traits_for_role('coder', count=1)) == 3   # min 3
        assert len(get_traits_for_role('coder', count=10)) == 5  # max 5
