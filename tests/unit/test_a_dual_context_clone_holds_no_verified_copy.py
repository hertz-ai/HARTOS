"""A dual-context clone of an experiment's goal holds no verified copy.

POST /api/social/tracker/dual-context clones a thought-experiment goal into N
goals, each with the source's config.  The owner's verification of a goal's
follow-up copy (verified_copy, and who and when) has ONE writer,
outreach_crm_tools.mark_goal_copy_verified: a clone that carried it would be a
second way for those keys to reach a goal (review of 68f9fa4b1, R2).  The
clone keeps the rest of the source's config.

Behavioural: the real tracker blueprint, dashboard_service's steering gate and
the models on SQLite; patched: the token store and the audit log.
"""
import os
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from integrations.social.api_tracker import tracker_bp  # noqa: E402
from integrations.social.models import AgentGoal, Base, User  # noqa: E402

VERIFIED = {'verified_copy': ['9f2c'], 'copy_verified': True,
            'copy_verified_by': 'the-owner', 'copy_verified_at': '2026-11-17T00:00:00'}


@pytest.fixture
def world(monkeypatch):
    for var in ('TRUSTED_PROXY', 'NUNBA_CI', 'HEVOLVE_TRUST_KONG', 'HEVOLVE_CLOUD_MODE'):
        monkeypatch.delenv(var, raising=False)
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    db = maker()
    owner = User(username=f'u_{uuid.uuid4().hex[:10]}', user_type='human')
    db.add(owner)
    db.commit()
    owner_id = str(owner.id)
    post_id, goal_id = uuid.uuid4().hex[:12], uuid.uuid4().hex
    db.add(AgentGoal(id=goal_id, owner_id=owner_id, created_by=owner_id,
                     goal_type='thought_experiment', title='an experiment',
                     status='active', config_json={'post_id': post_id, 'rounds': 3,
                                                   **VERIFIED}))
    db.commit()
    db.close()
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(tracker_bp)
    user = SimpleNamespace(id=owner_id, is_admin=False, role='flat', is_banned=False)
    with patch('integrations.social.auth._get_user_from_token',
               side_effect=lambda token: (user, maker())), \
            patch('security.immutable_audit_log.get_audit_log'):
        yield SimpleNamespace(client=app.test_client(), maker=maker,
                              post_id=post_id, goal_id=goal_id)


def test_a_clone_keeps_the_sources_config_but_not_its_verified_copy(world):
    resp = world.client.post(
        '/api/social/tracker/dual-context', headers={'Authorization': 'Bearer t'},
        json={'post_id': world.post_id,
              'contexts': [{'label': 'a'}, {'label': 'b', 'system_prompt_override': 'be brief'}]})

    assert resp.status_code == 201, resp.get_json()
    db = world.maker()
    try:
        clones = db.query(AgentGoal).filter(AgentGoal.id != world.goal_id).all()
        configs = [dict(c.config_json) for c in clones]
        source = dict(db.query(AgentGoal).filter_by(id=world.goal_id).one().config_json)
    finally:
        db.close()
    assert len(configs) == 2
    for config in configs:
        assert not set(VERIFIED) & set(config)
        assert config['post_id'] == world.post_id and config['rounds'] == 3
        assert config['source_goal_id'] == world.goal_id
    assert {c['dual_context_label'] for c in configs} == {'a', 'b'}
    assert source['verified_copy'] == VERIFIED['verified_copy']      # the source is untouched
