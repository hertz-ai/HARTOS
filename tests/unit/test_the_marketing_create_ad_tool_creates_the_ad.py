"""The marketing agent's create_ad tool creates the ad it was asked for.

The tool called AdService.create_ad with ``target_url=`` and
``targeting_json=``; the service takes ``click_url`` and ``targeting``, so
every call answered "unexpected keyword argument" and no marketing goal
ever placed an ad (found beside the live create_campaign failure on
central, build 1bb98d54, 2026-11-20).  When the service refused (budget
under its minimum, not enough Spark) the tool still said success.  These
tests call the real tool against the real service on a SQLite file and
read the ad row and the wallet back.

A refusal says what was wrong and how to fix it (core.tool_refusal).  Live
on central 2026-11-20, McGroce goal 09bbc67a: its create_ad step ran as
'system_daemon', which has no users row and no Spark (a wallet made for it
starts at 0), and the tool answered {"error": "Insufficient Spark",
"spark_balance": 0} three times; the goal paused needs_help.  Now the
refusal names the account, its balance and the budget; when no budget the
tool accepts fits the balance, a goal turn may finish the step without the
ad, and a budget under the minimum names the minimum to retry with.
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


def _create_ad_reply(advertiser, **kwargs):
    """The tool's reply text, as the agent reads it."""
    from integrations.agent_engine.marketing_tools import register_marketing_tools
    agents = _Agents()
    register_marketing_tools(agents, agents, advertiser)
    return agents.tools['create_ad'](**kwargs)


def _create_ad(advertiser, **kwargs):
    return json.loads(_create_ad_reply(advertiser, **kwargs))


_AD = dict(title='Lash kits', content='Salon-grade lashes',
           target_url='https://shop.example/lashes', ad_type='native')


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


def _rows_for(session_factory, advertiser):
    """(ads, wallets) stored for ``advertiser``."""
    from integrations.social.models import AdUnit, ResonanceWallet
    db = session_factory()
    try:
        return (db.query(AdUnit).filter_by(advertiser_id=advertiser).all(),
                db.query(ResonanceWallet).filter_by(user_id=advertiser).all())
    finally:
        db.close()


@pytest.mark.parametrize('budget', [50, 60])
def test_the_system_account_without_spark_may_finish_its_step_without_the_ad(
        social_db, budget):
    """09bbc67a: 'system_daemon' has no users row and no wallet."""
    from core.constants import tool_reply_failed
    from core.tool_refusal import refusal_has_hint, refusal_lets_step_finish
    reply = _create_ad_reply('system_daemon', budget=budget, **_AD)
    out = json.loads(reply)
    assert out['success'] is False
    assert (out['error'], out['spark_balance']) == ('Insufficient Spark', 0)
    note = out['note']
    assert "'system_daemon'" in note
    assert 'has 0 Spark' in note and f'needs {budget}' in note
    assert 'owner' in note and 'without the ad' in note
    assert refusal_has_hint(reply) and tool_reply_failed(reply)
    assert refusal_lets_step_finish(reply, 'create_ad') is True
    # Nothing made, nothing charged, and no empty wallet left behind.
    assert _rows_for(social_db, 'system_daemon') == ([], [])


def test_on_a_persons_turn_the_same_refusal_says_to_tell_them(social_db,
                                                             monkeypatch):
    import core.agent_tools as agent_tools
    from core.tool_refusal import RETRY_NEVER, refusal_lets_step_finish
    monkeypatch.setattr(agent_tools, 'is_a_persons_request', lambda _id: True)
    advertiser = _make_advertiser(social_db, spark=10)
    reply = _create_ad_reply(advertiser, budget=60, **_AD)
    out = json.loads(reply)
    assert out['retry'] == RETRY_NEVER
    assert 'has 10 Spark' in out['note'] and 'tell the person' in out['note']
    assert refusal_lets_step_finish(reply, 'create_ad') is False


def test_a_budget_under_the_minimum_names_the_minimum_to_retry_with(social_db):
    from core.tool_refusal import (RETRY_AFTER_FIX, refusal_has_hint,
                                   refusal_lets_step_finish)
    advertiser = _make_advertiser(social_db, spark=500)
    reply = _create_ad_reply(advertiser, budget=10, **_AD)
    out = json.loads(reply)
    assert out['success'] is False and out['retry'] == RETRY_AFTER_FIX
    assert 'budget 10' in out['note'] and 'at least 50' in out['note']
    assert refusal_has_hint(reply)
    assert refusal_lets_step_finish(reply, 'create_ad') is False
    ads, spark = _ads_and_spark(social_db, advertiser)
    assert (ads, spark) == ([], 500)


def test_spark_short_of_the_budget_but_enough_for_a_smaller_ad_says_how_much(
        social_db):
    """A lower budget would work here, so the step is not let go."""
    from core.tool_refusal import RETRY_AFTER_FIX, refusal_lets_step_finish
    advertiser = _make_advertiser(social_db, spark=55)
    reply = _create_ad_reply(advertiser, budget=60, **_AD)
    out = json.loads(reply)
    assert out['retry'] == RETRY_AFTER_FIX
    assert 'has 55 Spark' in out['note'] and 'at most 55' in out['note']
    assert refusal_lets_step_finish(reply, 'create_ad') is False


def test_an_ad_without_its_link_says_to_give_one(social_db):
    from core.tool_refusal import RETRY_AFTER_FIX, refusal_has_hint
    advertiser = _make_advertiser(social_db, spark=500)
    reply = _create_ad_reply(advertiser, title='Lash kits', content='x',
                             target_url='', budget=60)
    out = json.loads(reply)
    assert out['success'] is False and out['retry'] == RETRY_AFTER_FIX
    assert 'target_url' in out['note'] and refusal_has_hint(reply)
