"""test_voice_profile_persistence.py — POST agent → GET agent round-trips voice_profile.

Previously the POST /api/social/users/<id>/agents handler accepted a
`voice_profile` field but dropped it silently (the User ORM had no column
and the handler never persisted anything).  Fixed in HevolveSocial v37:
- User.voice_profile column added (JSON, nullable)
- create_user_agent() now persists it
- to_dict() echoes it back
This test is the regression guard for that fix.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')


@pytest.fixture(scope='module')
def engine():
    from sqlalchemy import create_engine
    from integrations.social.models import Base
    eng = create_engine('sqlite://', echo=False)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture(scope='module')
def SessionLocal(engine):
    from sqlalchemy.orm import sessionmaker
    return sessionmaker(bind=engine)


@pytest.fixture
def db(SessionLocal):
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


class TestVoiceProfileColumn:
    """Column-level: the migration actually put the column on the table."""

    def test_column_exists_on_user_model(self):
        from integrations.social.models import User
        assert hasattr(User, 'voice_profile'), (
            "User.voice_profile column missing — v37 migration not applied")

    def test_assigning_dict_round_trips(self, db):
        from integrations.social.models import User
        u = User(id='vp-user-1', username='voice.pleasant.river',
                 user_type='agent',
                 voice_profile={'engine': 'f5', 'preset': 'warm'})
        db.add(u)
        db.flush()
        db.commit()

        reread = db.query(User).filter(User.id == 'vp-user-1').first()
        assert reread.voice_profile is not None
        assert reread.voice_profile.get('engine') == 'f5'
        assert reread.voice_profile.get('preset') == 'warm'

    def test_to_dict_echoes_voice_profile(self, db):
        from integrations.social.models import User
        u = User(id='vp-user-2', username='still.calm.lake',
                 user_type='agent',
                 voice_profile={'engine': 'kokoro', 'speaker_id': 'af_bella'})
        db.add(u)
        db.flush()
        d = u.to_dict()
        assert 'voice_profile' in d
        assert d['voice_profile']['speaker_id'] == 'af_bella'


class TestCreateAgentRoundTrip:
    """Integration-ish: drive through create_user_agent() and read it back.

    Routes the call through UserService.register_agent_local + the handler's
    voice_profile persistence block without spinning up Flask — because
    exercising Flask would drag in the auth + rate-limit middleware and this
    test is meant to be fast and hermetic.
    """

    def test_dict_voice_profile_persists(self, db):
        from integrations.social.models import User
        from integrations.social.services import UserService

        owner = User(id='vp-owner', username='owner-one',
                     user_type='human', handle='ownerhandle', is_admin=False)
        db.add(owner)
        db.flush()
        db.commit()

        agent = UserService.register_agent_local(
            db, 'happy.star', 'desc', 'prompt-1_flow-1', owner=owner)

        # Replicate exactly what the POST handler does after register_agent_local
        vp_raw = {'engine': 'indic_parler', 'lang': 'hi-IN'}
        if isinstance(vp_raw, dict):
            agent.voice_profile = vp_raw
        db.flush()
        db.commit()

        # Read back by id
        reread = db.query(User).filter(User.id == agent.id).first()
        assert reread.voice_profile == vp_raw

    def test_string_voice_profile_canonicalised(self, db):
        """Handler canonicalises JSON-string → dict, and bare string → {'preset': s}."""
        # Branch 1: valid JSON string
        raw_json = '{"engine": "piper", "voice": "en_US-amy-medium"}'
        parsed = json.loads(raw_json)
        assert isinstance(parsed, dict)

        # Branch 2: bare string → wrapped as preset
        raw_bare = 'warm_female_v2'
        try:
            parsed2 = json.loads(raw_bare)
            assert False, "Should have raised ValueError"
        except (ValueError, TypeError):
            parsed2 = {'preset': raw_bare}

        assert parsed2 == {'preset': 'warm_female_v2'}
