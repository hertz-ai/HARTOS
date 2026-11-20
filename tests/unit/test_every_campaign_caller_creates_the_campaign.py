"""Every caller of CampaignService creates the campaign it was asked for.

Measured live on central (build 1bb98d54, 2026-11-20 ~05:05Z): the McGroce
marketing goal's step "create_campaign: create awareness campaign for
shoppers ... budget 60 Spark" answered, on all three attempts,

    CampaignService.create_campaign() got an unexpected keyword argument
    'creator_id'

and the goal paused needing help.  The service's signature is the one
canonical one (integrations/social/campaign_service.py); the two marketing
tools, the revenue agent's promote_api and POST /api/social/campaigns had
each drifted from it, and GET /api/social/campaigns/leaderboard passed an
``offset`` the service never took.  These tests call each caller against
the real service on a SQLite file and read the row back.
"""
import json
import uuid

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture(scope='module')
def session_factory(tmp_path_factory):
    from integrations.social.models import Base
    path = tmp_path_factory.mktemp('campaigns') / 'social.db'
    engine = create_engine(f'sqlite:///{path}')
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def social_db(session_factory, monkeypatch):
    """Every get_db reads the test database: the tools and the sign-in check
    import it from models at call time, the blueprint bound its own name."""
    import integrations.social.api_gamification as api
    import integrations.social.models as models
    monkeypatch.setattr(models, 'get_db', session_factory)
    monkeypatch.setattr(api, 'get_db', session_factory)
    return session_factory


@pytest.fixture(scope='module')
def app():
    from integrations.social.api_gamification import gamification_bp
    flask_app = Flask(__name__)
    flask_app.register_blueprint(gamification_bp)
    return flask_app


@pytest.fixture
def client(app, social_db, monkeypatch):
    monkeypatch.delenv('HEVOLVE_CLOUD_MODE', raising=False)
    return app.test_client()


def _make_person(session_factory):
    """A signed-up person: (their id, the Bearer token that signs them in)."""
    from integrations.social.models import User
    tag = uuid.uuid4().hex[:10]
    db = session_factory()
    try:
        person = User(username=f'owner_{tag}', display_name='Owner',
                      user_type='human', api_token=f'tok-{tag}')
        db.add(person)
        db.commit()
        return person.id, person.api_token
    finally:
        db.close()


def _campaigns_of(session_factory, owner_id):
    from integrations.social.models import Campaign
    db = session_factory()
    try:
        return db.query(Campaign).filter_by(owner_id=str(owner_id)).all()
    finally:
        db.close()


def _as_list(stored):
    """A target list as stored: the service writes it json-encoded."""
    return json.loads(stored) if isinstance(stored, str) else stored


class _Agents:
    """Stands in for the autogen agents a tool family registers on."""

    def __init__(self):
        self.tools = {}

    def register_for_llm(self, name, description):
        def keep(func):
            self.tools[name] = func
            return func
        return keep

    def register_for_execution(self, name):
        return lambda func: func


def _marketing_tools(owner):
    from integrations.agent_engine.marketing_tools import register_marketing_tools
    agents = _Agents()
    register_marketing_tools(agents, agents, owner)
    return agents.tools


def test_the_marketing_create_campaign_tool_creates_the_campaign(social_db):
    owner, _ = _make_person(social_db)
    out = json.loads(_marketing_tools(owner)['create_campaign'](
        name='McGroce shoppers', description='Awareness for shoppers',
        campaign_type='awareness', target_communities='grocers, cooks',
        budget=60))
    assert out['success'] is True, out
    [row] = _campaigns_of(social_db, owner)
    assert (row.name, row.goal, row.total_spark_budget) == (
        'McGroce shoppers', 'awareness', 60)
    assert _as_list(row.target_communities) == ['grocers', 'cooks']
    assert out['campaign']['id'] == row.id


def test_the_referral_campaign_tool_creates_a_conversion_campaign(social_db):
    owner, _ = _make_person(social_db)
    out = json.loads(_marketing_tools(owner)['create_referral_campaign'](
        name='Bring a friend', description='Referral push', budget=40,
        target_communities='cooks'))
    assert out['success'] is True, out
    [row] = _campaigns_of(social_db, owner)
    assert (row.goal, row.total_spark_budget) == ('conversion', 40)
    assert _as_list(row.target_communities) == ['cooks']
    assert out['campaign']['referral_code']


def test_the_revenue_promote_api_tool_creates_an_awareness_campaign(social_db):
    from integrations.agent_engine.revenue_tools import register_revenue_tools
    owner, _ = _make_person(social_db)
    agents = _Agents()
    register_revenue_tools(agents, agents, owner)
    out = json.loads(agents.tools['promote_api'](
        campaign_name='API launch', target_audience='developers',
        channels='platform,twitter'))
    assert out['campaign_created'] is True, out
    [row] = _campaigns_of(social_db, owner)
    assert row.goal == 'awareness'
    # promote_api names no budget, so the campaign has the service's own.
    assert row.total_spark_budget == 100
    assert _as_list(row.target_communities) == ['platform', 'twitter']


def test_the_route_creates_the_campaign_it_was_sent(client, social_db):
    owner, token = _make_person(social_db)
    resp = client.post('/api/social/campaigns', json={
        'name': 'Summer launch', 'description': 'New range',
        'goal': 'engagement', 'total_spark_budget': 70,
        'product_url': 'https://shop.example/summer',
        'target_regions': ['Asia Pacific'], 'target_communities': ['cooks'],
    }, headers={'Authorization': f'Bearer {token}'})
    assert resp.status_code == 201, resp.get_json()
    [row] = _campaigns_of(social_db, owner)
    assert (row.name, row.goal, row.total_spark_budget, row.product_url) == (
        'Summer launch', 'engagement', 70, 'https://shop.example/summer')
    assert _as_list(row.target_regions) == ['Asia Pacific']
    assert _as_list(row.target_communities) == ['cooks']
    assert resp.get_json()['data']['id'] == row.id


def test_the_route_keeps_the_signed_in_owner_whatever_the_body_says(
        client, social_db):
    owner, token = _make_person(social_db)
    someone_else, _ = _make_person(social_db)
    resp = client.post('/api/social/campaigns', json={
        'name': 'Not yours', 'owner_id': someone_else,
    }, headers={'Authorization': f'Bearer {token}'})
    assert resp.status_code == 201, resp.get_json()
    assert [r.name for r in _campaigns_of(social_db, owner)] == ['Not yours']
    assert _campaigns_of(social_db, someone_else) == []


@pytest.mark.parametrize('body', [{}, {'name': '   '}, {'name': 7}])
def test_the_route_refuses_a_campaign_without_a_name_and_says_how(
        client, social_db, body):
    owner, token = _make_person(social_db)
    resp = client.post('/api/social/campaigns', json=body,
                       headers={'Authorization': f'Bearer {token}'})
    assert resp.status_code == 400
    answer = resp.get_json()
    assert answer['success'] is False
    assert '"name"' in answer['error'] and 'JSON body' in answer['error']
    assert _campaigns_of(social_db, owner) == []


def test_the_campaign_leaderboard_answers_and_pages(client, social_db):
    from integrations.social.models import Campaign
    owner, _ = _make_person(social_db)
    db = social_db()
    try:
        ranked = Campaign(owner_id=owner, name='Ranked', goal='awareness',
                          status='active', total_spark_budget=100,
                          spark_spent=10, conversions=3)
        db.add(ranked)
        db.commit()
        ranked_id = ranked.id
    finally:
        db.close()
    resp = client.get('/api/social/campaigns/leaderboard')
    assert resp.status_code == 200, resp.get_json()
    ids = [entry['campaign_id'] for entry in resp.get_json()['data']]
    assert ranked_id in ids
    past_the_end = client.get(
        f'/api/social/campaigns/leaderboard?offset={len(ids)}')
    assert past_the_end.status_code == 200
    assert past_the_end.get_json()['data'] == []
