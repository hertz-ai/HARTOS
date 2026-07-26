"""P5: the 10 TB central origin-store ceiling. FederationManager.enforce_asset_
ceiling caps federated_posts via LRU eviction — over-cap evicts the COLDEST
(non-boosted, oldest received_at) rows; boosted rows are retained; the origin
node still holds the post so retrieval falls back to the source peer (or 410).
Behavioural — real in-memory SQLite + real SQL aggregation + real eviction.

    python -m pytest tests/unit/test_sync_asset_ceiling.py --noconftest -q
"""
import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db():
    import integrations.social.federation  # noqa: F401 — load social models once, canonical path
    from integrations.social.models import FederatedPost
    engine = create_engine('sqlite:///:memory:')
    FederatedPost.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _post(db, pid, content, received, boosted=False):
    from integrations.social.models import FederatedPost
    p = FederatedPost(id=pid, origin_node_id='n', origin_post_id=pid,
                      content=content, received_at=received, is_boosted=boosted)
    db.add(p)
    db.flush()
    return p


def _ids(db):
    from integrations.social.models import FederatedPost
    return {r.id for r in db.query(FederatedPost).all()}


def test_under_ceiling_is_noop(db):
    from integrations.social.federation import federation
    _post(db, 'a', 'x' * 100, datetime.datetime(2026, 6, 1))
    out = federation.enforce_asset_ceiling(db, max_bytes=10_000)
    assert out['evicted'] == 0 and out['under_ceiling'] is True
    assert _ids(db) == {'a'}


def test_over_ceiling_evicts_coldest_first(db):
    from integrations.social.federation import federation
    _post(db, 'old', 'x' * 100, datetime.datetime(2026, 6, 1))
    _post(db, 'mid', 'x' * 100, datetime.datetime(2026, 6, 2))
    _post(db, 'new', 'x' * 100, datetime.datetime(2026, 6, 3))
    out = federation.enforce_asset_ceiling(db, max_bytes=250, batch=1)
    assert out['under_ceiling'] is True
    assert _ids(db) == {'mid', 'new'}            # coldest ('old') evicted first


def test_boosted_posts_are_retained(db):
    from integrations.social.federation import federation
    _post(db, 'boost', 'x' * 100, datetime.datetime(2026, 6, 1), boosted=True)
    _post(db, 'cold', 'x' * 100, datetime.datetime(2026, 6, 2))
    out = federation.enforce_asset_ceiling(db, max_bytes=50, batch=10)
    assert _ids(db) == {'boost'}                 # cold evicted, boosted kept
    assert out['evicted'] == 1
    assert out['under_ceiling'] is False         # boosted alone still over cap — honest


def test_only_boosted_left_stops_eviction(db):
    from integrations.social.federation import federation
    _post(db, 'b1', 'x' * 100, datetime.datetime(2026, 6, 1), boosted=True)
    out = federation.enforce_asset_ceiling(db, max_bytes=10, batch=10)
    assert out['evicted'] == 0 and out['under_ceiling'] is False   # cannot evict boosted


def test_maybe_enforce_ceiling_throttles(db, monkeypatch):
    from integrations.social.federation import federation, FederationManager
    calls = []
    monkeypatch.setattr(federation, 'enforce_asset_ceiling',
                        lambda d, **k: calls.append(1))
    monkeypatch.setattr(FederationManager, '_CEILING_CHECK_EVERY', 3)
    monkeypatch.setattr(FederationManager, '_inbox_since_check', 0)
    for _ in range(5):
        federation._maybe_enforce_ceiling(db)
    assert len(calls) == 1                        # enforced once (at the 3rd of 5)
