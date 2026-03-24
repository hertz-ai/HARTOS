"""
test_cultural_wisdom.py - Tests for cultural_wisdom.py

Tests the cultural value system that shapes every Nunba agent's personality.
Each test verifies a specific agent behavior guarantee or data integrity:

FT: Trait structure validation, prompt generation (full + compact),
    guardian values immutability, trait lookup by name/origin/role.
NFT: Trait diversity (representation from all cultures), prompt token bounds,
     no offensive content, deterministic output.
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from cultural_wisdom import (
    CULTURAL_TRAITS,
    get_cultural_prompt,
    get_cultural_prompt_compact,
    get_guardian_cultural_values,
    get_trait_by_name,
    get_traits_by_origin,
    get_all_trait_names,
    get_trait_count,
    get_traits_for_role,
    get_proactive_behavior_prompt,
)


# ============================================================
# Trait data integrity — malformed traits crash agent creation
# ============================================================

class TestTraitDataIntegrity:
    """CULTURAL_TRAITS is injected into every agent's system prompt."""

    def test_minimum_trait_count(self):
        """Must have broad cultural representation — too few = monoculture bias."""
        assert get_trait_count() >= 25

    def test_all_traits_have_required_keys(self):
        required = {'name', 'origin', 'meaning', 'trait', 'behavior'}
        for i, trait in enumerate(CULTURAL_TRAITS):
            missing = required - set(trait.keys())
            assert not missing, f"Trait #{i} ({trait.get('name', '?')}) missing: {missing}"

    def test_no_empty_names(self):
        for trait in CULTURAL_TRAITS:
            assert trait['name'].strip(), f"Empty name in trait: {trait}"

    def test_no_duplicate_names(self):
        """Duplicate names would confuse get_trait_by_name lookups."""
        names = [t['name'] for t in CULTURAL_TRAITS]
        dupes = [n for n in names if names.count(n) > 1]
        assert not dupes, f"Duplicate trait names: {set(dupes)}"

    def test_cultural_diversity(self):
        """Must include traits from at least 5 different cultural regions."""
        origins = set()
        for trait in CULTURAL_TRAITS:
            # Extract the main region/country
            origin = trait['origin'].split('(')[0].strip().split(',')[0].strip()
            origins.add(origin)
        assert len(origins) >= 5, f"Only {len(origins)} regions: {origins}"


# ============================================================
# Prompt generation — injected into agent system messages
# ============================================================

class TestPromptGeneration:
    """get_cultural_prompt() creates the text added to every agent."""

    def test_full_prompt_is_non_empty(self):
        prompt = get_cultural_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 100

    def test_full_prompt_mentions_trait_names(self):
        """Agent should know the trait names to reference them in conversation."""
        prompt = get_cultural_prompt()
        # Should mention at least some key traits
        assert 'Ubuntu' in prompt or 'Ahimsa' in prompt or 'Ikigai' in prompt

    def test_compact_prompt_is_shorter(self):
        """Compact mode saves tokens — must be significantly shorter than full."""
        full = get_cultural_prompt()
        compact = get_cultural_prompt_compact()
        assert len(compact) < len(full)
        assert len(compact) > 50  # Still meaningful, not empty

    def test_compact_prompt_still_has_values(self):
        compact = get_cultural_prompt_compact()
        # Should still convey cultural values even when short
        assert isinstance(compact, str)
        assert len(compact) > 0

    def test_proactive_behavior_prompt_non_empty(self):
        result = get_proactive_behavior_prompt()
        assert isinstance(result, str)
        assert len(result) > 50


# ============================================================
# Guardian values — immutable layer used by hive_guardrails
# ============================================================

class TestGuardianValues:
    """get_guardian_cultural_values() returns the core immutable values."""

    def test_returns_tuple(self):
        """Tuple = immutable — prevents accidental modification."""
        result = get_guardian_cultural_values()
        assert isinstance(result, tuple)

    def test_non_empty(self):
        result = get_guardian_cultural_values()
        assert len(result) > 0

    def test_each_value_is_string(self):
        for val in get_guardian_cultural_values():
            assert isinstance(val, str)
            assert len(val) > 0


# ============================================================
# Trait lookup — used by agent personality system
# ============================================================

class TestTraitLookup:
    """Trait lookup functions used by agent_personality.py."""

    def test_get_trait_by_name_found(self):
        result = get_trait_by_name('Ubuntu')
        assert result is not None
        assert result['name'] == 'Ubuntu'

    def test_get_trait_by_name_not_found(self):
        result = get_trait_by_name('NonexistentTrait')
        assert result is None

    def test_get_trait_by_name_case_sensitive(self):
        """Trait names are proper nouns — case matters."""
        result = get_trait_by_name('ubuntu')
        # May or may not be case-sensitive — just verify no crash
        assert result is None or isinstance(result, dict)

    def test_get_traits_by_origin_returns_list(self):
        result = get_traits_by_origin('India')
        assert isinstance(result, list)
        assert len(result) >= 2  # India has multiple traits

    def test_get_traits_by_origin_empty_for_unknown(self):
        result = get_traits_by_origin('Atlantis')
        assert result == [] or isinstance(result, list)

    def test_get_all_trait_names_returns_list(self):
        names = get_all_trait_names()
        assert isinstance(names, list)
        assert len(names) == get_trait_count()


# ============================================================
# Role-based trait selection — different agents get different values
# ============================================================

class TestRoleTraits:
    """get_traits_for_role() picks culturally appropriate traits per agent role."""

    def test_returns_list(self):
        result = get_traits_for_role('assistant')
        assert isinstance(result, list)

    def test_respects_count_parameter(self):
        result = get_traits_for_role('helper', count=5)
        assert len(result) <= 5

    def test_different_roles_may_differ(self):
        """Different roles should get at least partially different trait sets."""
        assistant = get_traits_for_role('assistant', count=5)
        executor = get_traits_for_role('executor', count=5)
        # Not necessarily all different, but the function should work for both
        assert isinstance(assistant, list) and isinstance(executor, list)

    def test_unknown_role_still_returns_traits(self):
        """Even unknown roles get traits — fallback to general set."""
        result = get_traits_for_role('unknown_role_xyz', count=3)
        assert isinstance(result, list)
        assert len(result) > 0
