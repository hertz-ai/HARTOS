"""The marketing agent's create_ad tool creates the ad it was asked for.

The tool called AdService.create_ad with ``target_url=`` and
``targeting_json=``; the service takes ``click_url`` and ``targeting``, so
every call answered "unexpected keyword argument" and no marketing goal
ever placed an ad (found beside the live create_campaign failure on
central, build 1bb98d54, 2026-11-20).  When the service refused (budget
under its minimum, not enough Spark) the tool still said success.  These
tests call the real tool against the real service on a SQLite file and
read the ad row and the wallet back.
"""
import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture(scope='module')
def session_factory(tmp_path_factory):
    from integrations.social.models import Base
    path = tmp_path_factory.mktemp('ads') / 'social.db'
    engine = create_engine(f'sqlite:///{path}')
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def social_db(session_factory, monkeypatch):
    """The tool imports get_db from models at call time."""
    import integrations.social.models as models
    monkeypatch.setattr(models, 'get_db', session_factory)
    return session_factory


def _make_advertiser(session_factory, spark):
    """A person with ``spark`` in their wallet; returns their id."""
    from integrations.social.models import ResonanceWallet, User
    db = session_factory()
    try:
        person = User(username=f'advertiser_{uuid.uuid4().hex[:10]}',
                      user_type='human')
        db.add(person)
        db.flush()
        db.add(ResonanceWallet(user_id=person.id, spark=spark,
                               spark_lifetime=spark))
        db.commit()
        return person.id
    finally:
        db.close()


def _ads_and_spark(session_factory, advertiser):
    from integrations.social.models import AdUnit, ResonanceWallet
    db = session_factory()
    try:
        ads = db.query(AdUnit).filter_by(advertiser_id=advertiser).all()
        spark = db.query(ResonanceWallet).filter_by(
            user_id=advertiser).one().spark
        return ads, spark
    finally:
        db.close()


class _Agents:
    """Stands in for the autogen agents the tool family registers on."""

    def __init__(self):
        self.tools = {}

    def register_for_llm(self, name, description):
        def keep(func):
            self.tools[name] = func
            return func
        return keep

    def register_for_execution(self, name):
        return lambda func: func


def _create_ad(advertiser, **kwargs):
    from integrations.agent_engine.marketing_tools import register_marketing_tools
    agents = _Agents()
    register_marketing_tools(agents, agents, advertiser)
    return json.loads(agents.tools['create_ad'](**kwargs))


def test_the_create_ad_tool_creates_the_ad_with_its_link_and_audience(social_db):
    advertiser = _make_advertiser(social_db, spark=500)
    out = _create_ad(advertiser, title='Lash kits', content='Salon-grade lashes',
                     target_url='https://shop.example/lashes', ad_type='native',
                     budget=60, target_audience='makeup artists in Chennai')
    assert out['success'] is True, out
    [ad], spark = _ads_and_spark(social_db, advertiser)
    assert (ad.title, ad.click_url, ad.ad_type, ad.budget_spark) == (
        'Lash kits', 'https://shop.example/lashes', 'native', 60)
    assert ad.targeting_json == {'audience': 'makeup artists in Chennai'}
    assert spark == 440
    assert out['ad']['id'] == ad.id


def test_an_ad_without_an_audience_has_no_targeting(social_db):
    advertiser = _make_advertiser(social_db, spark=500)
    out = _create_ad(advertiser, title='Lash kits', content='Salon-grade lashes',
                     target_url='https://shop.example/lashes')
    assert out['success'] is True, out
    [ad], _spark = _ads_and_spark(social_db, advertiser)
    assert ad.targeting_json == {}


def test_a_refused_ad_is_reported_as_not_created(social_db):
    advertiser = _make_advertiser(social_db, spark=10)
    out = _create_ad(advertiser, title='Lash kits', content='Salon-grade lashes',
                     target_url='https://shop.example/lashes', budget=60)
    assert out['success'] is False
    assert 'Insufficient Spark' in out['error']
    ads, spark = _ads_and_spark(social_db, advertiser)
    assert (ads, spark) == ([], 10)
