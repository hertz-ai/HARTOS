"""J360-J369 · Content moderation.

HiveContest opens a public submission channel → needs moderation.
Flags, appeals, reviewer persona corruption, anonymous report flow.
"""
from __future__ import annotations

import pytest

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _harness_shim import harness, skip_if_missing


class TestJ360PostFlag:
    def test_flagged_post_enters_review_queue(self):
        pytest.skip('J360 RED — post flag → moderation queue journey gap')


class TestJ361CommunityModAction:
    def test_community_mod_can_remove_post_within_scope(self):
        pytest.skip('J361 RED — community-mod authority boundary gap')


class TestJ362BanAppeal:
    def test_banned_user_appeal_review_path(self):
        pytest.skip('J362 RED — ban appeal workflow not built')


class TestJ363ContestIdeaReport:
    def test_reported_contest_idea_hidden_pending_review(self):
        skip_if_missing('integrations.agent_engine.hive_contest:submit_idea')
        pytest.skip('J363 RED — HiveContest idea moderation E2E gap')


class TestJ364ReviewerPersonaCorruption:
    def test_corrupted_reviewer_prompt_does_not_auto_approve(self):
        skip_if_missing('integrations.agent_engine.hive_consensus:'
                        'HiveConsensus')
        pytest.skip(
            'J364 RED — adversarial scenario: reviewer persona '
            'prompt-injected to always return PASS.  4-of-4 consensus '
            '(constitutional + local_probe + peer_probe + circuit_breaker) '
            'should still catch; journey asserts this defense'
        )


class TestJ365AnonReport:
    def test_anonymous_abuse_report_accepted(self):
        pytest.skip('J365 RED — anonymous (logged-out) abuse report '
                    'journey gap')


class TestJ366CSAMDetection:
    def test_csam_pattern_auto_quarantined(self):
        pytest.skip(
            'J366 RED — CSAM detection pipeline for uploaded media is '
            'critical trust-and-safety path; no journey asserts '
            '(or even that the pipeline exists)'
        )


class TestJ367DOXXDetection:
    def test_personal_info_in_public_post_flagged(self):
        skip_if_missing('security.dlp_engine:DlpEngine')
        pytest.skip('J367 — DLP engine exists for outbound; inbound '
                    'public-post DOXX detection journey untested')


class TestJ368DuplicateContent:
    def test_copy_paste_spam_detected(self):
        pytest.skip('J368 RED — near-duplicate content detection gap')


class TestJ369ModeratorBurnout:
    def test_excessive_moderation_load_surfaces_to_ops(self):
        pytest.skip('J369 RED — moderator-load telemetry untested; '
                    'burnout = degraded moderation quality, silent')
