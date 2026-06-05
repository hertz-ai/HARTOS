"""#46 — Comment carries agent_id + privacy (verified against a real DB).

agent_id distinguishes agent- vs human-authored comments post-hoc (author_id is
the owning User either way); privacy=None inherits the post's privacy, a value
overrides it.  Added to both the canonical sql.models.SocialComment and the
HARTOS _models_local.Comment fallback (parity) + CommentService.create accepts
them.  Verified here against an in-memory SQLite round-trip + to_dict.
"""
from __future__ import annotations

import inspect
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_comment_round_trips_agent_id_and_privacy():
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import integrations.social.models as M
    except Exception as e:
        pytest.skip(f"social models / sqlalchemy unavailable: {e}")

    engine = create_engine('sqlite:///:memory:')
    M.Comment.__table__.create(bind=engine, checkfirst=True)  # FK enforcement off by default
    db = sessionmaker(bind=engine)()
    try:
        db.add(M.Comment(id='c1', post_id='p1', author_id='u1', content='hi there',
                         agent_id='agent-7', privacy='community'))
        db.commit()
        got = db.query(M.Comment).filter(M.Comment.id == 'c1').first()
        assert got.agent_id == 'agent-7'
        assert got.privacy == 'community'
        d = got.to_dict()
        assert d['agent_id'] == 'agent-7' and d['privacy'] == 'community'

        # default (human-authored, no override) → both None, still serialized
        db.add(M.Comment(id='c2', post_id='p1', author_id='u2', content='human'))
        db.commit()
        d2 = db.query(M.Comment).filter(M.Comment.id == 'c2').first().to_dict()
        assert d2['agent_id'] is None and d2['privacy'] is None
    finally:
        db.close()


def test_comment_service_create_accepts_agent_id_and_privacy():
    """The service must thread agent_id/privacy through (signature contract)."""
    try:
        from integrations.social.services import CommentService
    except Exception as e:
        pytest.skip(f"CommentService unavailable: {e}")
    params = inspect.signature(CommentService.create).parameters
    assert 'agent_id' in params and 'privacy' in params, (
        "CommentService.create must accept agent_id + privacy so agent-authored "
        "comments are distinguishable")
    assert params['agent_id'].default is None and params['privacy'].default is None, (
        "new params must be optional (backward-compatible with existing callers)")
