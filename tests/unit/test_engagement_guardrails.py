"""Tests for integrations.social.engagement_guardrails — health-first game/compute limits.

This module was 0% covered (no test anywhere). It is the "no addiction by design"
layer: soft caps + gentle suggestions on games/compute (never blocks). The three
guardrails run real DB queries over GameParticipant, so these use a real
in-memory SQLite session (same pattern as test_revenue_functional) with
controlled joined_at/result/finished_at timestamps.

SQLite does not enforce FKs by default, so participants can be inserted directly
with distinct game_session_ids (the UniqueConstraint is (game_session_id, user_id)).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from integrations.social.models import Base, GameParticipant
from integrations.social.engagement_guardrails import (
    EngagementGuardrails, MAX_GAMES_PER_DAY, COOLDOWN_AFTER_LOSS_STREAK,
    MIN_GAME_INTERVAL_MINUTES, MAX_COMPUTE_CONTINUOUS_HOURS,
)

_engine = create_engine('sqlite://', echo=False,
                        connect_args={'check_same_thread': False},
                        poolclass=StaticPool)
_Session = sessionmaker(bind=_engine, expire_on_commit=False)


@pytest.fixture(scope='function')
def db():
    Base.metadata.create_all(_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()
        Base.metadata.drop_all(_engine)


_seq = [0]


def _add(db, user_id, joined_at, result=None, finished_at=None):
    _seq[0] += 1
    p = GameParticipant(
        game_session_id=f'gs-{_seq[0]}', user_id=user_id,
        joined_at=joined_at, result=result, finished_at=finished_at)
    db.add(p)
    db.commit()
    return p


# ── check_game_limit ────────────────────────────────────────────────────────
def test_no_games_is_allowed_silent(db):
    allowed, msg = EngagementGuardrails.check_game_limit(db, 'u1')
    assert allowed is True and msg is None


def test_daily_cap_suggests_change_of_pace(db):
    now = datetime.utcnow()
    for i in range(MAX_GAMES_PER_DAY):
        _add(db, 'u1', joined_at=now - timedelta(minutes=10 + i))
    allowed, msg = EngagementGuardrails.check_game_limit(db, 'u1')
    assert allowed is True and msg and 'games today' in msg


def test_loss_streak_suggests_collaborative(db):
    now = datetime.utcnow()
    # 3 most-recent finished games are losses (ordered by finished_at desc).
    for i in range(COOLDOWN_AFTER_LOSS_STREAK):
        _add(db, 'u1', joined_at=now - timedelta(minutes=30 + i),
             result='loss', finished_at=now - timedelta(minutes=i))
    allowed, msg = EngagementGuardrails.check_game_limit(db, 'u1')
    assert allowed is True and msg and 'tough rounds' in msg


def test_loss_streak_broken_by_a_win(db):
    now = datetime.utcnow()
    # Most recent is a win -> streak counter breaks immediately -> no streak msg.
    _add(db, 'u1', joined_at=now - timedelta(minutes=30), result='loss',
         finished_at=now - timedelta(minutes=3))
    _add(db, 'u1', joined_at=now - timedelta(minutes=29), result='loss',
         finished_at=now - timedelta(minutes=2))
    _add(db, 'u1', joined_at=now - timedelta(minutes=28), result='win',
         finished_at=now - timedelta(minutes=1))
    allowed, msg = EngagementGuardrails.check_game_limit(db, 'u1')
    # No 3-loss streak and the last game is >2min ago -> silent.
    assert allowed is True and msg is None


def test_rapid_fire_suggests_a_breath(db):
    now = datetime.utcnow()
    # One recent game < MIN_GAME_INTERVAL_MINUTES ago, no streak.
    _add(db, 'u1', joined_at=now - timedelta(seconds=30))
    allowed, msg = EngagementGuardrails.check_game_limit(db, 'u1')
    assert allowed is True and msg and 'breath' in msg


def test_spaced_out_single_game_is_silent(db):
    now = datetime.utcnow()
    _add(db, 'u1', joined_at=now - timedelta(minutes=MIN_GAME_INTERVAL_MINUTES + 5))
    allowed, msg = EngagementGuardrails.check_game_limit(db, 'u1')
    assert allowed is True and msg is None


def test_limits_are_per_user(db):
    now = datetime.utcnow()
    for i in range(MAX_GAMES_PER_DAY):
        _add(db, 'heavy', joined_at=now - timedelta(minutes=10 + i))
    # A different user with no games is unaffected.
    allowed, msg = EngagementGuardrails.check_game_limit(db, 'fresh')
    assert allowed is True and msg is None


# ── check_compute_health ────────────────────────────────────────────────────
def test_compute_health_under_cap_silent(db):
    allowed, msg = EngagementGuardrails.check_compute_health(db, 'u1', 0)
    assert allowed is True and msg is None


def test_compute_health_over_cap_suggests_break(db):
    allowed, msg = EngagementGuardrails.check_compute_health(
        db, 'u1', continuous_hours=MAX_COMPUTE_CONTINUOUS_HOURS)
    assert allowed is True and msg and 'hours' in msg


# ── should_suggest_break ────────────────────────────────────────────────────
def test_suggest_break_false_when_light(db):
    now = datetime.utcnow()
    for i in range(3):
        _add(db, 'u1', joined_at=now - timedelta(minutes=10 + i))
    flag, msg = EngagementGuardrails.should_suggest_break(db, 'u1')
    assert flag is False and msg is None


def test_suggest_break_true_after_heavy_2h(db):
    now = datetime.utcnow()
    for i in range(8):
        _add(db, 'u1', joined_at=now - timedelta(minutes=5 + i))
    flag, msg = EngagementGuardrails.should_suggest_break(db, 'u1')
    assert flag is True and msg and 'break' in msg
