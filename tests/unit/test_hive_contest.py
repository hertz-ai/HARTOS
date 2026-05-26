"""Unit tests for integrations.agent_engine.hive_contest.

Confirms the contest module:
  - Exposes the three tracks (digital / embodied / human_wellness)
  - score_event maps event → weight correctly per track
  - score_event silently ignores unknown event types (safety)
  - Cross-track spam doesn't double-score (weight 0 for wrong track)
  - Contest window comes from env or sensible defaults
  - Claude Code MCP snippet is paste-ready
  - register_participant is idempotent (no new table)

Unit-scope: does NOT hit the DB.  Integration tests that wire
ResonanceService + GamificationService live under tests/integration/.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from integrations.agent_engine import hive_contest as hc


class TracksTest(unittest.TestCase):
    def test_three_tracks_exposed(self):
        self.assertEqual(
            {t.value for t in hc.ContestTrack},
            {'digital', 'embodied', 'human_wellness'},
        )

    def test_each_track_has_weights(self):
        for track in hc.ContestTrack:
            self.assertIn(track, hc.SCORE_WEIGHTS)
            self.assertGreater(len(hc.SCORE_WEIGHTS[track]), 0)

    def test_embodied_rewards_robot_episodes(self):
        # Physical-world contributions must score in the embodied track
        w = hc.SCORE_WEIGHTS[hc.ContestTrack.EMBODIED]
        self.assertIn('robot_episodes_success', w)
        self.assertGreater(w['robot_episodes_success'], 0)

    def test_human_wellness_rewards_wellness_attestation(self):
        # HUMAN_WELLNESS must weight wellness outcomes highest
        w = hc.SCORE_WEIGHTS[hc.ContestTrack.HUMAN_WELLNESS]
        self.assertIn('wellness_outcomes_attested', w)
        highest = max(w.values())
        self.assertEqual(highest, w['wellness_outcomes_attested'])


class EventWeightTest(unittest.TestCase):
    def test_unknown_event_returns_zero(self):
        w = hc._event_weight('invented_event_type', hc.ContestTrack.DIGITAL)
        self.assertEqual(w, 0.0)

    def test_digital_rewards_recipes(self):
        w = hc._event_weight('recipe_published', hc.ContestTrack.DIGITAL)
        self.assertGreater(w, 0)

    def test_digital_does_not_reward_robot_episodes(self):
        # Cross-track spam prevention: a robot episode should not
        # score on the digital track — otherwise a user could push
        # the same event across all three tracks and get paid 3×.
        w = hc._event_weight('robot_episode_success', hc.ContestTrack.DIGITAL)
        self.assertEqual(w, 0.0)

    def test_human_wellness_does_not_reward_gpu_hours(self):
        w = hc._event_weight('gpu_hour_served', hc.ContestTrack.HUMAN_WELLNESS)
        self.assertEqual(w, 0.0)


class ScoreEventTest(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()

    def test_unknown_event_scores_zero_and_does_not_call_award(self):
        with patch('integrations.social.resonance_engine.ResonanceService.award_spark') as mock:
            amt = hc.score_event(self.db, 'u1', 'nonsense_event')
            self.assertEqual(amt, 0)
            mock.assert_not_called()

    def test_known_event_awards_weighted_amount(self):
        with patch('integrations.social.resonance_engine.ResonanceService.award_spark') as mock:
            amt = hc.score_event(
                self.db, 'u1', 'recipe_published',
                track=hc.ContestTrack.DIGITAL,
            )
            self.assertGreater(amt, 0)
            mock.assert_called_once()
            # source_type must be prefixed with contest: for ledger filtering
            call_kwargs = mock.call_args.kwargs
            self.assertTrue(
                call_kwargs['source_type'].startswith('contest:'),
                call_kwargs,
            )

    def test_multiplier_scales_amount(self):
        with patch('integrations.social.resonance_engine.ResonanceService.award_spark'):
            base = hc.score_event(
                self.db, 'u1', 'recipe_published',
                track=hc.ContestTrack.DIGITAL, multiplier=1.0,
            )
            doubled = hc.score_event(
                self.db, 'u1', 'recipe_published',
                track=hc.ContestTrack.DIGITAL, multiplier=2.0,
            )
            self.assertEqual(doubled, base * 2)

    def test_negative_multiplier_clamped_to_zero(self):
        with patch('integrations.social.resonance_engine.ResonanceService.award_spark') as mock:
            amt = hc.score_event(
                self.db, 'u1', 'recipe_published',
                track=hc.ContestTrack.DIGITAL, multiplier=-5.0,
            )
            self.assertEqual(amt, 0)
            mock.assert_not_called()

    def test_wrong_track_scores_zero(self):
        # robot_episode on digital track = 0, no award
        with patch('integrations.social.resonance_engine.ResonanceService.award_spark') as mock:
            amt = hc.score_event(
                self.db, 'u1', 'robot_episode_success',
                track=hc.ContestTrack.DIGITAL,
            )
            self.assertEqual(amt, 0)
            mock.assert_not_called()


class WindowTest(unittest.TestCase):
    def test_default_window_is_30_days_from_now(self):
        for var in ('HEVOLVE_CONTEST_START', 'HEVOLVE_CONTEST_END'):
            os.environ.pop(var, None)
        window = hc.get_contest_window()
        delta = window['end'] - window['start']
        self.assertGreaterEqual(delta, timedelta(days=29, hours=23))
        self.assertLessEqual(delta, timedelta(days=31))

    def test_env_override_respected(self):
        os.environ['HEVOLVE_CONTEST_START'] = '2026-05-01T00:00:00'
        os.environ['HEVOLVE_CONTEST_END'] = '2026-06-15T23:59:59'
        try:
            window = hc.get_contest_window()
            self.assertEqual(window['start'].year, 2026)
            self.assertEqual(window['start'].month, 5)
            self.assertEqual(window['end'].month, 6)
            self.assertEqual(window['end'].day, 15)
        finally:
            os.environ.pop('HEVOLVE_CONTEST_START', None)
            os.environ.pop('HEVOLVE_CONTEST_END', None)

    def test_invalid_env_falls_back_to_default(self):
        os.environ['HEVOLVE_CONTEST_START'] = 'not-a-date'
        try:
            window = hc.get_contest_window()
            self.assertIsInstance(window['start'], datetime)
        finally:
            os.environ.pop('HEVOLVE_CONTEST_START', None)


class InfoPayloadTest(unittest.TestCase):
    def test_info_has_tracks_and_onramp(self):
        info = hc.get_contest_info()
        self.assertIn('tracks', info)
        self.assertEqual(len(info['tracks']), 3)
        track_ids = {t['id'] for t in info['tracks']}
        self.assertEqual(
            track_ids, {'digital', 'embodied', 'human_wellness'},
        )
        self.assertIn('how_to_join', info)
        self.assertTrue(
            any('docs.hevolve.ai/downloads' in line for line in info['how_to_join']),
            info['how_to_join'],
        )

    def test_humans_first_language_is_present(self):
        info = hc.get_contest_info()
        self.assertIn('humans_first_principle', info)
        text = info['humans_first_principle'].lower()
        self.assertIn('human', text)

    def test_prize_model_uses_canonical_split(self):
        info = hc.get_contest_info()
        prize = info.get('prize_model', {})
        # Must reference the 90/9/1 split — never invent a parallel
        # contest-specific payout scheme
        self.assertIn('spark_split_90_9_1', prize)


class MCPSnippetTest(unittest.TestCase):
    def test_snippet_mentions_hartos_server(self):
        snippet = hc.claude_code_mcp_snippet()
        self.assertIn('hartos', snippet)
        self.assertIn('mcpServers', snippet)

    def test_snippet_uses_hart_cli(self):
        # hart CLI is the canonical entry point (CLAUDE.md mentions it).
        # Pointing at anything else here would drift.
        snippet = hc.claude_code_mcp_snippet()
        self.assertIn('"command": "hart"', snippet)


class RegisterParticipantTest(unittest.TestCase):
    def test_idempotent_on_second_call(self):
        # Fake DB that returns an existing transaction on the second call
        db = MagicMock()
        fake_txn = MagicMock()
        fake_txn.created_at = datetime.now(timezone.utc)
        # First call: no prior transaction
        db.query.return_value.filter.return_value.first.side_effect = [
            None,       # first call: no prior registration
            fake_txn,   # second call: registration exists
        ]
        with patch('integrations.social.resonance_engine.ResonanceService.award_spark'):
            first = hc.register_participant(db, 'u1')
            second = hc.register_participant(db, 'u1')
        self.assertTrue(first['ok'])
        self.assertFalse(first.get('already_registered'))
        self.assertTrue(second['ok'])
        self.assertTrue(second['already_registered'])


if __name__ == '__main__':
    unittest.main()
