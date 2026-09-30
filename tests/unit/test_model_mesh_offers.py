"""A model one node installed becomes visible to the fleet -- and only that.

The mesh's value is that an install stops being a per-machine event. Its
risk is that it carries a peer's claims into a place where this node acts
on them. These tests pin the second half.

The sharp edge is ModelCatalog.select_best(): it does NOT filter on
`downloaded`, it only adds +50 to the score of an entry that is present.
So any registered, `enabled` row is a live selection candidate. Had
offers been written into the catalog, a peer would be able to put a
candidate in front of the local selector -- scored with a quality_score
and priority the PEER chose -- for weights this node does not have.
test_an_offer_never_becomes_a_selection_candidate proves the selector
cannot see an offer, and it asserts against the real select_best rather
than restating the rule.

    python -m pytest tests/unit/test_model_mesh_offers.py -q
"""
import os
import sys
import threading
import time

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from integrations.service_tools import model_mesh  # noqa: E402
from integrations.service_tools.model_catalog import (  # noqa: E402
    ModelCatalog, ModelEntry, ModelType)

PEER = 'http://10.0.0.7:6777'
PEER_NODE = 'node-peer'
ME = 'node-me'


@pytest.fixture(autouse=True)
def clean_mesh(monkeypatch):
    model_mesh.clear_offers()
    monkeypatch.delenv('HEVOLVE_MODEL_MESH', raising=False)
    monkeypatch.delenv('HEVOLVE_MODEL_OFFER_TTL_S', raising=False)
    yield
    model_mesh.clear_offers()


def _identity(node=ME, url='http://127.0.0.1:6777'):
    return lambda: (node, url)


def _trust(*peers):
    """Stand in for the gossip-admitted peer store."""
    def _admitted(limit=8):
        return list(peers)
    mod = type(sys)('integrations.google_a2a.peer_reuse')
    mod.admitted_peers = _admitted
    return mod


@pytest.fixture
def trusted(monkeypatch):
    monkeypatch.setattr(model_mesh, '_local_identity', _identity())
    monkeypatch.setitem(
        sys.modules, 'integrations.google_a2a.peer_reuse',
        _trust({'node_id': PEER_NODE, 'url': PEER}))


def advert(model_id='llm-tiel-35b', **over):
    m = {
        'id': model_id,
        'name': 'Tiel Coder 35B',
        'model_type': 'llm',
        'repo_id': 'org/Tiel-Coder-35B-A3B-GGUF-MTP',
        'backend': 'llama.cpp',
        'files': {'model': 'tiel.gguf'},
        'vram_gb': 28.6, 'ram_gb': 42.4, 'disk_gb': 21.19,
        'languages': ['en'],
        'capabilities': {'chat': True, 'moe': True, 'experts_used': 8,
                         'experts_total': 256, 'mtp': True},
    }
    m.update(over.pop('model', {}))
    out = {
        'type': 'model_available',
        'model': m,
        'source_node': PEER_NODE,
        'source_api_url': PEER,
        'timestamp': time.time(),
    }
    out.update(over)
    return out


class TestAnOfferIsNotAnInstall:
    def test_the_offer_is_cached_and_readable(self, trusted):
        assert model_mesh.on_model_available_advert(advert())['success']
        offers = model_mesh.peer_offers()
        assert len(offers) == 1
        assert offers[0]['model_id'] == 'llm-tiel-35b'
        assert offers[0]['peer_url'] == PEER

    def test_nothing_is_registered_in_the_catalog(self, trusted, monkeypatch):
        cat = ModelCatalog.__new__(ModelCatalog)
        cat._entries, cat._populators = {}, {}
        cat._lock = threading.RLock()
        monkeypatch.setattr(
            'integrations.service_tools.model_catalog.get_catalog',
            lambda: cat)
        model_mesh.on_model_available_advert(advert())
        assert cat._entries == {}, \
            'an advert registered a catalog row; offers must stay in the cache'

    def test_an_offer_never_becomes_a_selection_candidate(self, trusted,
                                                          monkeypatch):
        """The property the whole design rests on, asserted against the
        REAL selector. The offer names a model far better than the local
        one on every axis a peer controls, so if offers reached the
        catalog this would return the peer's id."""
        cat = ModelCatalog.__new__(ModelCatalog)
        cat._entries, cat._populators = {}, {}
        cat._lock = threading.RLock()
        cat._entries['local'] = ModelEntry(
            id='local', name='Local', model_type=ModelType.LLM,
            vram_gb=0.0, ram_gb=1.0, quality_score=0.3, priority=10,
            downloaded=True)
        monkeypatch.setattr(
            'integrations.service_tools.model_catalog.get_catalog',
            lambda: cat)

        model_mesh.on_model_available_advert(advert(
            'llm-peer-monster',
            model={'vram_gb': 0.0, 'ram_gb': 0.1,
                   'quality_score': 1.0, 'priority': 999}))

        pick = cat.select_best('llm', budget_vram_gb=64, budget_ram_gb=64,
                               gpu_available=True)
        assert pick is not None and pick.id == 'local'

    def test_peer_scoring_opinions_are_not_even_carried(self, trusted):
        """quality_score / speed_score / priority are this node's opinion,
        re-derived at install. An offer must not transport them."""
        model_mesh.on_model_available_advert(advert(
            model={'quality_score': 1.0, 'speed_score': 1.0,
                   'priority': 999}))
        offer = model_mesh.peer_offers()[0]
        for banned in ('quality_score', 'speed_score', 'priority'):
            assert banned not in offer


class TestTrust:
    def test_an_unadmitted_peer_is_refused(self, monkeypatch):
        monkeypatch.setattr(model_mesh, '_local_identity', _identity())
        monkeypatch.setitem(sys.modules,
                            'integrations.google_a2a.peer_reuse', _trust())
        r = model_mesh.on_model_available_advert(advert())
        assert r['success'] is False and r['reason'] == 'peer_not_admitted'
        assert model_mesh.peer_offers() == []

    def test_an_unreachable_peer_store_fails_closed(self, monkeypatch):
        """No trust rail means no trust decision. Failing open would
        accept an offer from anyone who can reach the port."""
        monkeypatch.setattr(model_mesh, '_local_identity', _identity())
        broken = type(sys)('integrations.google_a2a.peer_reuse')

        def _boom(limit=8):
            raise RuntimeError('peer store down')
        broken.admitted_peers = _boom
        monkeypatch.setitem(sys.modules,
                            'integrations.google_a2a.peer_reuse', broken)
        r = model_mesh.on_model_available_advert(advert())
        assert r['success'] is False and r['reason'] == 'trust_unavailable'
        assert model_mesh.peer_offers() == []

    def test_our_own_advert_is_skipped(self, monkeypatch):
        monkeypatch.setattr(model_mesh, '_local_identity',
                            _identity(node=PEER_NODE))
        monkeypatch.setitem(
            sys.modules, 'integrations.google_a2a.peer_reuse',
            _trust({'node_id': PEER_NODE, 'url': PEER}))
        r = model_mesh.on_model_available_advert(advert())
        assert r['reason'] == 'echo_skip'

    def test_a_peer_cannot_assert_a_trust_flag(self, trusted):
        """install_validated is what the dispatcher's validation gate
        reads. A peer naming it in an advert must not set it."""
        model_mesh.on_model_available_advert(advert(
            model={'capabilities': {'chat': True, 'install_validated': True,
                                    'moe': True}}))
        caps = model_mesh.peer_offers()[0]['capabilities']
        assert 'install_validated' not in caps
        assert caps['moe'] is True          # real facts still cross

    def test_an_incomplete_advert_is_refused(self, trusted):
        assert not model_mesh.on_model_available_advert(
            advert(model={'id': ''}))['success']
        assert not model_mesh.on_model_available_advert(
            advert(source_api_url=''))['success']


class TestMeasuredFactsTravel:
    def test_the_facts_that_decide_a_pull_arrive(self, trusted):
        """These are read from the artifact by read_gguf_facts on the
        origin. Relaying them is the point: they say whether the model is
        worth 21 GB of someone else's disk."""
        model_mesh.on_model_available_advert(advert())
        caps = model_mesh.peer_offers()[0]['capabilities']
        assert caps['moe'] is True
        assert caps['experts_used'] == 8 and caps['experts_total'] == 256
        assert caps['mtp'] is True

    def test_fit_numbers_arrive(self, trusted):
        o = model_mesh.peer_offers() if model_mesh.on_model_available_advert(
            advert()) else []
        assert (o[0]['vram_gb'], o[0]['disk_gb']) == (28.6, 21.19)


class TestAnnounceOnlyWhatWeHave:
    def _cat(self, monkeypatch, **fields):
        cat = ModelCatalog.__new__(ModelCatalog)
        cat._entries, cat._populators = {}, {}
        cat._lock = threading.RLock()
        base = dict(id='m', name='M', model_type=ModelType.LLM,
                    backend='llama.cpp', files={'model': 'm.gguf'},
                    downloaded=True, capabilities={'moe': True})
        base.update(fields)
        cat._entries['m'] = ModelEntry(**base)
        monkeypatch.setattr(
            'integrations.service_tools.model_catalog.get_catalog',
            lambda: cat)
        return cat

    def _capture(self, monkeypatch, node=ME):
        sent = []
        monkeypatch.setattr(model_mesh, '_local_identity',
                            _identity(node=node))
        mod = type(sys)('integrations.social.peer_discovery')

        class _G:
            def broadcast(self, msg, targets=None):
                sent.append(msg)
                return 2
        mod.gossip = _G()
        monkeypatch.setitem(sys.modules,
                            'integrations.social.peer_discovery', mod)
        return sent

    def test_a_downloaded_model_is_announced(self, monkeypatch):
        self._cat(monkeypatch)
        sent = self._capture(monkeypatch)
        assert model_mesh.announce_model_available('m') is True
        assert len(sent) == 1
        assert sent[0]['type'] == 'model_available'
        assert sent[0]['model']['id'] == 'm'
        assert sent[0]['model']['capabilities']['moe'] is True

    def test_a_model_we_do_not_have_is_not_announced(self, monkeypatch):
        """Announcing an undownloaded row would point peers at weights
        this node cannot serve."""
        self._cat(monkeypatch, downloaded=False)
        sent = self._capture(monkeypatch)
        assert model_mesh.announce_model_available('m') is False
        assert sent == []

    def test_an_unknown_id_is_not_announced(self, monkeypatch):
        self._cat(monkeypatch)
        sent = self._capture(monkeypatch)
        assert model_mesh.announce_model_available('nope') is False
        assert sent == []

    def test_a_broadcast_failure_does_not_raise(self, monkeypatch):
        """Announcing is best-effort and rides on the back of an install.
        It must never fail the install that triggered it."""
        self._cat(monkeypatch)
        monkeypatch.setattr(model_mesh, '_local_identity', _identity())
        mod = type(sys)('integrations.social.peer_discovery')

        class _G:
            def broadcast(self, msg, targets=None):
                raise OSError('network down')
        mod.gossip = _G()
        monkeypatch.setitem(sys.modules,
                            'integrations.social.peer_discovery', mod)
        assert model_mesh.announce_model_available('m') is False

    def test_no_advertised_url_means_no_announce(self, monkeypatch):
        self._cat(monkeypatch)
        sent = self._capture(monkeypatch)
        monkeypatch.setattr(model_mesh, '_local_identity',
                            _identity(url=''))
        assert model_mesh.announce_model_available('m') is False
        assert sent == []


class TestOfferHousekeeping:
    def test_a_stale_offer_is_evicted_on_read(self, trusted, monkeypatch):
        """Advance the clock rather than shrink the TTL to zero: on
        Windows time.time() moves in ~15 ms steps, so a same-tick read
        gives `now - ts == 0.0`, which is not > 0 and would make this
        pass or fail on scheduling luck."""
        model_mesh.on_model_available_advert(advert())
        assert len(model_mesh.peer_offers()) == 1
        later = time.time() + model_mesh._offer_ttl_s() + 60
        monkeypatch.setattr(model_mesh.time, 'time', lambda: later)
        assert model_mesh.peer_offers() == []

    def test_a_model_we_already_have_is_not_offered_to_us(self, trusted,
                                                          monkeypatch):
        cat = ModelCatalog.__new__(ModelCatalog)
        cat._entries, cat._populators = {}, {}
        cat._lock = threading.RLock()
        cat._entries['llm-tiel-35b'] = ModelEntry(
            id='llm-tiel-35b', name='T', model_type=ModelType.LLM)
        monkeypatch.setattr(
            'integrations.service_tools.model_catalog.get_catalog',
            lambda: cat)
        model_mesh.on_model_available_advert(advert())
        assert model_mesh.peer_offers() == []
        assert len(model_mesh.peer_offers(exclude_local=False)) == 1

    def test_two_peers_offering_the_same_model_are_distinct_rows(self,
                                                                 monkeypatch):
        monkeypatch.setattr(model_mesh, '_local_identity', _identity())
        other = 'http://10.0.0.9:6777'
        monkeypatch.setitem(
            sys.modules, 'integrations.google_a2a.peer_reuse',
            _trust({'node_id': PEER_NODE, 'url': PEER},
                   {'node_id': 'node-other', 'url': other}))
        model_mesh.on_model_available_advert(advert())
        model_mesh.on_model_available_advert(
            advert(source_node='node-other', source_api_url=other))
        offers = model_mesh.peer_offers(exclude_local=False)
        assert {o['peer_url'] for o in offers} == {PEER, other}

    def test_a_re_advert_refreshes_rather_than_duplicates(self, trusted):
        model_mesh.on_model_available_advert(advert())
        model_mesh.on_model_available_advert(advert())
        assert len(model_mesh.peer_offers(exclude_local=False)) == 1

    def test_type_filter(self, trusted):
        model_mesh.on_model_available_advert(advert())
        assert len(model_mesh.peer_offers(model_type='llm')) == 1
        assert model_mesh.peer_offers(model_type='tts') == []


class TestTheEndpointActuallyRoutesIt:
    """Through the real /api/social/peers/broadcast, not the function.

    A dispatcher branch that is never reached is the failure this
    endpoint was built to fix: before the receiver existed, broadcasts
    404'd and every gossip-carried payload was silently lost (hive
    bridge audit, April 2026, Fix #1)."""

    @pytest.fixture
    def client(self):
        from flask import Flask
        from integrations.social.discovery import discovery_bp
        app = Flask('node_b')
        app.register_blueprint(discovery_bp)
        return app.test_client()

    def test_a_model_advert_reaches_the_mesh(self, client, trusted):
        r = client.post('/api/social/peers/broadcast', json=advert())
        assert r.status_code == 200
        assert r.get_json()['success'] is True
        assert len(model_mesh.peer_offers(exclude_local=False)) == 1

    def test_a_refused_advert_is_202_not_500(self, client, monkeypatch):
        """202 is the dispatcher's 'received but not acted on'. A 500
        would mark the sender's peer-health record as a failure and back
        the whole node off."""
        monkeypatch.setattr(model_mesh, '_local_identity', _identity())
        monkeypatch.setitem(sys.modules,
                            'integrations.google_a2a.peer_reuse', _trust())
        r = client.post('/api/social/peers/broadcast', json=advert())
        assert r.status_code == 202
        assert r.get_json()['reason'] == 'peer_not_admitted'

    def test_an_older_peer_still_acks_an_unknown_type(self, client):
        """Forward compatibility runs both ways: this node must keep
        acking types it does not know, or a newer peer's broadcast looks
        like a delivery failure."""
        r = client.post('/api/social/peers/broadcast',
                        json={'type': 'something_from_2027'})
        assert r.status_code == 200
        assert r.get_json() == {'success': True, 'dispatched': False,
                                'type': 'something_from_2027'}


class TestTheOffSwitch:
    def test_disabled_accepts_nothing_and_announces_nothing(self, trusted,
                                                            monkeypatch):
        monkeypatch.setenv('HEVOLVE_MODEL_MESH', '0')
        assert not model_mesh.on_model_available_advert(advert())['success']
        assert model_mesh.peer_offers() == []
        assert model_mesh.announce_model_available('m') is False
