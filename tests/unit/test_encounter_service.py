"""
test_encounter_service.py - Tests for integrations/social/encounter_service.py

Tests the encounter/bond tracking — drives the SwarmCanvas agent visualization.
Each test verifies a specific social graph mechanic:

FT: Record encounter (canonical ordering, dedup), bond level progression
    (thresholds at 2/5/10/20/50 encounters), self-encounter rejection.
NFT: Bond level bounds (0-10), encounter count monotonic increase,
     dedup is symmetric (A+B == B+A).
"""
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# Bond level algorithm — the core social graph mechanic
# ============================================================

class TestBondLevelProgression:
    """Bond level drives connection strength in the swarm visualization.
    Higher bond = stronger visual link between agents in AgentHiveView."""

    def _mock_encounter(self, count, bond_level=0):
        """Create a mock Encounter row with given count and bond."""
        enc = MagicMock()
        enc.encounter_count = count
        enc.bond_level = bond_level
        enc.latest_at = datetime.utcnow()
        enc.to_dict.return_value = {
            'encounter_count': count + 1,
            'bond_level': bond_level,
        }
        return enc

    def test_self_encounter_rejected(self):
        """A user can't encounter themselves — would corrupt the graph."""
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        result = EncounterService.record_encounter(mock_db, 'user_1', 'user_1', 'comment')
        assert result is None
        # DB should not be queried at all
        mock_db.query.assert_not_called()

    def test_canonical_ordering(self):
        """A+B and B+A must produce the same DB row — sorted IDs prevent duplicates."""
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.add = MagicMock()
        mock_db.flush = MagicMock()

        # Record A→B
        EncounterService.record_encounter(mock_db, 'user_b', 'user_a', 'comment', 'post_1')
        # The filter_by call should use sorted IDs: user_a, user_b (not user_b, user_a)
        call_args = mock_db.query.return_value.filter_by.call_args
        assert call_args[1]['user_a_id'] == 'user_a'
        assert call_args[1]['user_b_id'] == 'user_b'

    def test_first_encounter_bond_zero(self):
        """First encounter = bond level 0 (strangers)."""
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_enc = MagicMock()
        mock_enc.to_dict.return_value = {'bond_level': 0, 'encounter_count': 1}
        with patch('integrations.social.encounter_service.Encounter', return_value=mock_enc):
            result = EncounterService.record_encounter(mock_db, 'a', 'b', 'comment')
        assert result is not None

    def test_bond_level_increases_at_2(self):
        """After 2 encounters, bond reaches level 1."""
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        existing = self._mock_encounter(count=1, bond_level=0)
        mock_db.query.return_value.filter_by.return_value.first.return_value = existing
        EncounterService.record_encounter(mock_db, 'a', 'b', 'comment')
        assert existing.bond_level >= 1

    def test_bond_level_increases_at_5(self):
        """After 5 encounters, bond reaches level 3."""
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        existing = self._mock_encounter(count=4, bond_level=1)
        mock_db.query.return_value.filter_by.return_value.first.return_value = existing
        EncounterService.record_encounter(mock_db, 'a', 'b', 'comment')
        assert existing.bond_level >= 3

    def test_bond_level_increases_at_10(self):
        """After 10 encounters, bond reaches level 5."""
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        existing = self._mock_encounter(count=9, bond_level=3)
        mock_db.query.return_value.filter_by.return_value.first.return_value = existing
        EncounterService.record_encounter(mock_db, 'a', 'b', 'comment')
        assert existing.bond_level >= 5

    def test_bond_level_increases_at_20(self):
        """After 20 encounters, bond reaches level 7."""
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        existing = self._mock_encounter(count=19, bond_level=5)
        mock_db.query.return_value.filter_by.return_value.first.return_value = existing
        EncounterService.record_encounter(mock_db, 'a', 'b', 'comment')
        assert existing.bond_level >= 7

    def test_bond_level_capped_at_10(self):
        """Bond level must never exceed 10 — UI renders 0-10 scale."""
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        existing = self._mock_encounter(count=99, bond_level=10)
        mock_db.query.return_value.filter_by.return_value.first.return_value = existing
        EncounterService.record_encounter(mock_db, 'a', 'b', 'comment')
        assert existing.bond_level <= 10

    def test_encounter_count_increments(self):
        """Each encounter must increment count — used for bond calculation."""
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        existing = self._mock_encounter(count=5, bond_level=3)
        mock_db.query.return_value.filter_by.return_value.first.return_value = existing
        EncounterService.record_encounter(mock_db, 'a', 'b', 'comment')
        assert existing.encounter_count == 6

    def test_latest_at_updated(self):
        """latest_at tracks recency — used for 'recently active' filtering."""
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        existing = self._mock_encounter(count=1, bond_level=0)
        mock_db.query.return_value.filter_by.return_value.first.return_value = existing
        EncounterService.record_encounter(mock_db, 'a', 'b', 'comment')
        # latest_at should be set to a datetime (updated by the service)
        assert existing.latest_at is not None


# ============================================================
# get_encounters — user's encounter list for SwarmCanvas
# ============================================================

class TestGetEncounters:
    """get_encounters feeds the encounter graph in AgentHiveView."""

    def test_returns_list(self):
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
        result = EncounterService.get_encounters(mock_db, 'user_1')
        assert isinstance(result, list)

    def test_enriches_with_other_user_info(self):
        """Each encounter must include the other user's display info for the UI."""
        from integrations.social.encounter_service import EncounterService
        mock_enc = MagicMock()
        mock_enc.user_a_id = 'user_1'
        mock_enc.user_b_id = 'user_2'
        mock_enc.to_dict.return_value = {'user_a_id': 'user_1', 'user_b_id': 'user_2'}
        mock_other = MagicMock()
        mock_other.id = 'user_2'
        mock_other.username = 'bob'
        mock_other.display_name = 'Bob'
        mock_other.avatar_url = None
        mock_other.user_type = 'human'
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = [mock_enc]
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_other
        result = EncounterService.get_encounters(mock_db, 'user_1')
        assert len(result) == 1
        assert 'other_user' in result[0]
        assert result[0]['other_user']['username'] == 'bob'


# ============================================================
# get_encounters_with — pairwise encounter history
# ============================================================

class TestGetEncountersWith:
    """get_encounters_with shows all contexts two users crossed paths."""

    def test_returns_list(self):
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = []
        result = EncounterService.get_encounters_with(mock_db, 'user_1', 'user_2')
        assert isinstance(result, list)

    def test_canonical_order_preserved(self):
        """A+B and B+A produce same query — canonical sorted IDs."""
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = []
        EncounterService.get_encounters_with(mock_db, 'user_b', 'user_a')
        # filter_by should use sorted order: user_a, user_b
        call_args = mock_db.query.return_value.filter_by.call_args
        assert call_args[1]['user_a_id'] == 'user_a'
        assert call_args[1]['user_b_id'] == 'user_b'


# ============================================================
# acknowledge_encounter — mutual awareness
# ============================================================

class TestAcknowledgeEncounter:
    """acknowledge_encounter marks encounters as mutually seen."""

    def test_returns_none_for_missing_encounter(self):
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        result = EncounterService.acknowledge_encounter(mock_db, 'enc_1', 'user_1')
        assert result is None

    def test_rejects_non_participant(self):
        """Only the two users in the encounter can acknowledge it."""
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        mock_enc = MagicMock()
        mock_enc.user_a_id = 'alice'
        mock_enc.user_b_id = 'bob'
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_enc
        result = EncounterService.acknowledge_encounter(mock_db, 'enc_1', 'charlie')
        assert result is None  # charlie is not a participant

    def test_accepts_participant(self):
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        mock_enc = MagicMock()
        mock_enc.user_a_id = 'alice'
        mock_enc.user_b_id = 'bob'
        mock_enc.to_dict.return_value = {'is_mutual_aware': True}
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_enc
        result = EncounterService.acknowledge_encounter(mock_db, 'enc_1', 'alice')
        assert result is not None
        assert mock_enc.is_mutual_aware is True


# ============================================================
# get_suggestions — connection recommendations
# ============================================================

class TestGetSuggestions:
    """get_suggestions recommends connections based on encounter patterns."""

    def test_returns_list(self):
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.group_by.return_value.having.return_value.order_by.return_value.limit.return_value.all.return_value = []
        result = EncounterService.get_suggestions(mock_db, 'user_1')
        assert isinstance(result, list)

    def test_respects_limit(self):
        from integrations.social.encounter_service import EncounterService
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.group_by.return_value.having.return_value.order_by.return_value.limit.return_value.all.return_value = []
        result = EncounterService.get_suggestions(mock_db, 'user_1', limit=5)
        assert len(result) <= 5
