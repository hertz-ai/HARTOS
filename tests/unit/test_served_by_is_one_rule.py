"""A chat reply's origin is named in ONE place, with exactly three values.

Owner, 2026-10-04: ``local`` is a reply the node's own LLM wrote, ``hive`` is a
reply that was escalated to a hive peer, ``cloud`` is a reply from a cloud agent
(an agent that exists only on central).

MEASURED defect (frozen_debug.log 17:10, user 10202): "Run a thought experiment"
to agent 20 was answered by the LOCAL llama-server (all 24 model calls went to
127.0.0.1:8080) and the page badged it "Cloud".  HARTOS stamped ``node_tier`` on
its chat envelopes but never ``served_by`` ("stays caller-set", and no caller set
it), so the page fell back to its own default of 'cloud'.  The one dispatch site
that does know (the speculative dispatcher) kept the answer to itself, as the
telemetry tag 'hive_langchain_bg' / 'local_langchain_bg', which the page's badge
cannot read ('hive_langchain_bg' is not 'hive', so an escalated reply read as
On-device).

How the dispatcher's answer reaches the stamp: through a thread context
(``thread_local_data.reply_from``), NOT a keyword.  Nunba rebinds
``hart_intelligence.publish_async`` with ``_patched(topic, message, timeout=2.0)``
(routes/hartos_backend_adapter.py) and ``safe_hartos_attr`` hands the dispatcher
that wrapper, so a new keyword is a TypeError on the desktop and the expert's
reply is never published.  The end-to-end class below runs the real dispatcher
through a wrapper of exactly that shape.

Review of 2026-10-04 (rework): a reply nobody tagged is where the node's own LLM
runs (core.autogen_config.llm_stays_on_premises): a node that sends its prompts
to a public API (openrouter.ai) is not on-device, and tier.js defines local as
"no cloud egress".  Tags are matched as words ('archive' is not 'hive').  The
TTS payload carries no badge: the page plays a 'TTS' payload and returns
(Demopage.js handleDataReceived), so nothing read it.

These tests drive the REAL publish_async, _chat_reply and SpeculativeDispatcher;
only the bus, SSE, executor and persistence boundaries are replaced.

    python -m pytest tests/unit/test_served_by_is_one_rule.py -q
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
import tempfile
import threading
import types
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

CHAT_TOPIC = 'com.hertzai.hevolve.chat.user-1'


@pytest.fixture
def cs():
    import core.constants as constants
    return constants


@pytest.fixture
def tl():
    from hartos.threadlocal import thread_local_data
    return thread_local_data


@pytest.fixture(scope='module')
def hie():
    try:
        import hart_intelligence_entry as module
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f'hart_intelligence_entry is not importable here: {exc!r}')
    return module


_LLM_ENV = ('HEVOLVE_LLM_ENDPOINT_URL', 'HEVOLVE_ACTIVE_CLOUD_PROVIDER',
            'HEVOLVE_LLM_API_KEY', 'HEVOLVE_LLM_MODEL_NAME')


@pytest.fixture(autouse=True)
def own_llama_server(monkeypatch):
    """No cloud provider and no API endpoint: the node runs its own LLM.  A
    developer box with a provider exported must not decide these tests."""
    for var in _LLM_ENV:
        monkeypatch.delenv(var, raising=False)


def _on_api(monkeypatch, endpoint, provider='openrouter', tier=None):
    monkeypatch.setenv('HEVOLVE_ACTIVE_CLOUD_PROVIDER', provider)
    monkeypatch.setenv('HEVOLVE_LLM_API_KEY', 'test-key')
    monkeypatch.setenv('HEVOLVE_LLM_MODEL_NAME', 'some-model')
    if endpoint is None:
        monkeypatch.delenv('HEVOLVE_LLM_ENDPOINT_URL', raising=False)
    else:
        monkeypatch.setenv('HEVOLVE_LLM_ENDPOINT_URL', endpoint)
    if tier:
        monkeypatch.setenv('HEVOLVE_NODE_TIER', tier)


@pytest.fixture
def flat_node(monkeypatch):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')


@pytest.fixture
def central_node(monkeypatch):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')


# ─────────────────────────────────────────────────────────────────────
# The one rule
# ─────────────────────────────────────────────────────────────────────

class TestCanonicalServedBy:

    def test_the_vocabulary_is_exactly_three_values(self, cs):
        assert cs.SERVED_BY_VALUES == ('local', 'hive', 'cloud')
        assert (cs.SERVED_BY_LOCAL, cs.SERVED_BY_HIVE, cs.SERVED_BY_CLOUD) == (
            'local', 'hive', 'cloud')

    @pytest.mark.parametrize('value, tier, expected', [
        # an explicit canonical value is kept
        ('local', 'flat', 'local'),
        ('hive', 'flat', 'hive'),
        ('cloud', 'flat', 'cloud'),
        # the dispatcher's telemetry tags and the cloud pipeline's source names
        ('hive_langchain_bg', 'flat', 'hive'),
        ('local_langchain_bg', 'flat', 'local'),
        ('langchain_cloud', 'flat', 'cloud'),
        ('hevolve_cloud', 'flat', 'cloud'),
        # case and padding never decide the answer
        ('  HIVE ', 'flat', 'hive'),
        ('Cloud', 'regional', 'cloud'),
        # no usable tag: the node's own LLM wrote it
        (None, 'flat', 'local'),
        ('', 'flat', 'local'),
        (None, 'regional', 'local'),
        ('an-unknown-device-id', 'flat', 'local'),
        # words, not substrings (review: 'archive' read as hive)
        ('archive', 'flat', 'local'),
        ('beehive-sync', 'flat', 'local'),
        ('cloudless-local', 'flat', 'local'),
        # on central the node's own LLM IS the cloud, which is what every
        # client of central sees; an escalation to peers is still 'hive'
        (None, 'central', 'cloud'),
        ('', 'CENTRAL', 'cloud'),
        ('local', 'central', 'cloud'),
        ('local_langchain_bg', 'central', 'cloud'),
        ('hive', 'central', 'hive'),
        ('an-unknown-device-id', 'central', 'cloud'),
    ])
    def test_truth_table(self, cs, value, tier, expected):
        assert cs.canonical_served_by(value, tier) == expected

    def test_the_tier_comes_from_the_environment_when_not_given(
            self, cs, monkeypatch):
        monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
        assert cs.canonical_served_by(None) == 'cloud'
        monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
        assert cs.canonical_served_by(None) == 'local'
        monkeypatch.delenv('HEVOLVE_NODE_TIER')
        assert cs.canonical_served_by(None) == 'local'

    @pytest.mark.parametrize('endpoint, provider, expected', [
        # review MUST FIX 1: a flat node on a public API sends every prompt
        # off the box; its reply is not on-device
        ('https://openrouter.ai/api/v1', 'openrouter', 'cloud'),
        (None, 'openai', 'cloud'),               # the openai SDK's own base
        # an API endpoint on this machine or this LAN is still local
        ('http://localhost:8080/v1', 'custom', 'local'),
        ('http://127.0.0.1:8080/v1', 'custom', 'local'),
        ('http://192.168.0.69:8080/v1', 'custom', 'local'),
        ('http://[::1]:8080/v1', 'custom', 'local'),
        # a host NAME is not provably local
        ('http://my-nas:8080/v1', 'custom', 'cloud'),
        # a provider with no endpoint the chat callers can reach runs local
        (None, 'anthropic', 'local'),
    ])
    def test_an_untagged_reply_is_where_the_nodes_llm_runs(
            self, cs, monkeypatch, endpoint, provider, expected):
        _on_api(monkeypatch, endpoint, provider)
        assert cs.canonical_served_by(None, 'flat') == expected
        # The dispatcher tags its own (non-escalated) path local_langchain_bg
        # whatever that LLM is, so the tag names this node's LLM too: where
        # it runs (review of 028a8a959: asserting 'local' here locked in an
        # API node's reply badged On-device).  An escalation keeps its own.
        assert cs.canonical_served_by('local_langchain_bg', 'flat') == expected
        assert cs.canonical_served_by('local', 'flat') == expected
        assert cs.canonical_served_by('hive', 'flat') == 'hive'

    def test_a_regional_node_on_a_public_api_is_cloud_not_lan(
            self, cs, monkeypatch):
        """tier.js badges local + regional 'LAN'; a regional node whose LLM is
        openrouter.ai sent the prompt to the internet."""
        monkeypatch.setenv('HEVOLVE_NODE_TIER', 'regional')
        monkeypatch.setenv('HEVOLVE_LLM_ENDPOINT_URL', 'https://openrouter.ai/api/v1')
        assert cs.canonical_served_by(None) == 'cloud'
        monkeypatch.setenv('HEVOLVE_LLM_ENDPOINT_URL', 'http://10.0.0.5:8080/v1')
        assert cs.canonical_served_by(None) == 'local'

    def test_an_unreadable_configuration_is_cloud_the_pages_own_unknown(
            self, cs, monkeypatch):
        import core.autogen_config as ac

        def _broken():
            raise RuntimeError('config unreadable')
        monkeypatch.setattr(ac, 'llm_stays_on_premises', _broken)
        assert cs.canonical_served_by(None, 'flat') == 'cloud'

    @pytest.mark.parametrize('tier', ['flat', 'regional', 'central'])
    @pytest.mark.parametrize('endpoint', ['', 'https://openrouter.ai/api/v1',
                                          'http://192.168.0.69:8080/v1'])
    @pytest.mark.parametrize('provider, key', [('', ''), ('openrouter', 'k'),
                                               ('openai', 'k'), ('anthropic', 'k'),
                                               ('openrouter', '')])
    def test_one_answer_to_local_or_api(self, monkeypatch, tier, endpoint,
                                        provider, key):
        """resolve_llm_backend's kind IS configured_api_endpoint's answer:
        the badge cannot say local while the agents dial an API, or the
        reverse."""
        import core.autogen_config as ac
        monkeypatch.setenv('HEVOLVE_NODE_TIER', tier)
        for var, val in (('HEVOLVE_LLM_ENDPOINT_URL', endpoint),
                         ('HEVOLVE_ACTIVE_CLOUD_PROVIDER', provider),
                         ('HEVOLVE_LLM_API_KEY', key)):
            if val:
                monkeypatch.setenv(var, val)
            else:
                monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv('HEVOLVE_LLM_MODEL_NAME', 'm')
        kind, entry = ac.resolve_llm_backend()
        api = ac.configured_api_endpoint()
        assert kind == ('api' if api else 'local'), (kind, api)
        if kind == 'api':
            assert (entry.get('base_url') or ac.OPENAI_SDK_DEFAULT_BASE) == api

    @pytest.mark.parametrize('junk', [123, ['hive'], {}, object(), 1.5, b'hive'])
    def test_a_value_that_is_not_a_string_never_raises(self, cs, junk):
        assert cs.canonical_served_by(junk, 'flat') == 'local'
        assert cs.canonical_served_by(junk, 'central') == 'cloud'


# ─────────────────────────────────────────────────────────────────────
# reply_from: the context the dispatch site names its origin through
# ─────────────────────────────────────────────────────────────────────

class TestReplyFromContext:

    def test_names_the_origin_inside_and_gives_the_thread_its_own_back(self, tl):
        assert tl.get_served_by() is None
        with tl.reply_from('hive_langchain_bg'):
            assert tl.get_served_by() == 'hive_langchain_bg'
            with tl.reply_from('local'):
                assert tl.get_served_by() == 'local'
            assert tl.get_served_by() == 'hive_langchain_bg'
        assert tl.get_served_by() is None

    def test_an_exception_inside_the_block_still_restores_the_thread(self, tl):
        with pytest.raises(RuntimeError):
            with tl.reply_from('hive'):
                raise RuntimeError('delivery blew up')
        assert tl.get_served_by() is None

    def test_another_thread_does_not_see_it(self, tl):
        """A pooled worker must never publish under a neighbour's origin."""
        seen = []
        with tl.reply_from('hive'):
            t = threading.Thread(target=lambda: seen.append(tl.get_served_by()))
            t.start()
            t.join(10)
        assert seen == [None]


# ─────────────────────────────────────────────────────────────────────
# publish_async: every chat envelope leaves with the field
# ─────────────────────────────────────────────────────────────────────

class _Bus:
    def __init__(self):
        self.published = []

    def publish(self, topic, data, **kw):
        self.published.append((topic, dict(data), kw))
        return 'msg-1'


def _sse_recorder(sink):
    def _record(event_type, data, user_id=None):
        sink.append((event_type, dict(data), user_id))
        return True
    return _record


class TestPublishAsyncStampsServedBy:

    @staticmethod
    def _publish(hie, monkeypatch, topic, message):
        bus = _Bus()
        sse = []
        monkeypatch.setattr(hie, 'client', None, raising=False)
        with patch('core.peer_link.message_bus.get_message_bus',
                   return_value=bus), \
                patch('core.platform.events.broadcast_sse_safe',
                      side_effect=_sse_recorder(sse)):
            hie.publish_async(topic, message)
        chat = [p for p in bus.published if p[0].startswith('chat.')]
        assert chat, f'no chat envelope reached the bus: {bus.published!r}'
        return chat[0][1], sse

    def test_publish_async_keeps_the_three_argument_contract_nunba_wraps(
            self, hie):
        """Nunba's wrapper is ``_patched(topic, message, timeout=2.0)``: a
        fourth parameter on the real function invites a caller to pass it
        through that wrapper, which raises TypeError on the desktop."""
        params = list(inspect.signature(hie.publish_async).parameters)
        assert params == ['topic', 'message', 'timeout']

    def test_a_reply_with_no_origin_is_local_on_a_flat_node(
            self, hie, monkeypatch, flat_node):
        data, sse = self._publish(hie, monkeypatch, CHAT_TOPIC,
                                  {'text': ['hi']})
        assert data['served_by'] == 'local'
        assert sse and sse[0][1]['served_by'] == 'local'

    def test_the_same_reply_is_cloud_on_central(
            self, hie, monkeypatch, central_node):
        data, sse = self._publish(hie, monkeypatch, CHAT_TOPIC,
                                  {'text': ['hi']})
        assert data['served_by'] == 'cloud'
        assert sse[0][1]['served_by'] == 'cloud'

    def test_a_dispatcher_tag_is_named_in_the_canonical_vocabulary(
            self, hie, monkeypatch, flat_node):
        data, sse = self._publish(
            hie, monkeypatch, CHAT_TOPIC,
            {'text': ['hi'], 'served_by': 'hive_langchain_bg'})
        assert data['served_by'] == 'hive'
        assert sse[0][1]['served_by'] == 'hive'

    def test_the_dispatch_site_names_the_origin_of_a_plain_text_publish(
            self, hie, monkeypatch, flat_node, tl):
        """The expert's text goes out as a bare string (a bare string is what
        mobile clients read), so the origin rides beside it, in the context."""
        with tl.reply_from('hive_langchain_bg'):
            data, sse = self._publish(hie, monkeypatch, CHAT_TOPIC,
                                      'the expert says so')
        assert data['raw'] == 'the expert says so'
        assert data['served_by'] == 'hive'
        assert sse[0][1]['served_by'] == 'hive'

    def test_the_envelopes_own_origin_beats_the_ambient_one(
            self, hie, monkeypatch, flat_node, tl):
        with tl.reply_from('hive'):
            data, _ = self._publish(
                hie, monkeypatch, CHAT_TOPIC,
                {'text': ['x'], 'served_by': 'local'})
        assert data['served_by'] == 'local'

    def test_a_publish_after_the_block_is_the_nodes_own_llm_again(
            self, hie, monkeypatch, flat_node, tl):
        with tl.reply_from('hive'):
            pass
        data, _ = self._publish(hie, monkeypatch, CHAT_TOPIC,
                                {'text': ['next turn']})
        assert data['served_by'] == 'local'

    def test_an_envelope_naming_central_is_cloud_even_on_a_flat_node(
            self, hie, monkeypatch, flat_node):
        """A regional/flat node relaying central's reply keeps node_tier =
        'central' (test_publish_async_node_tier); the badge follows it."""
        data, _ = self._publish(
            hie, monkeypatch, CHAT_TOPIC,
            {'text': ['x'], 'node_tier': 'central'})
        assert data['node_tier'] == 'central'
        assert data['served_by'] == 'cloud'

    def test_a_reply_from_a_node_on_a_public_api_is_cloud(
            self, hie, monkeypatch, flat_node):
        _on_api(monkeypatch, 'https://openrouter.ai/api/v1')
        data, sse = self._publish(hie, monkeypatch, CHAT_TOPIC, {'text': ['hi']})
        assert data['served_by'] == 'cloud'
        assert sse[0][1]['served_by'] == 'cloud'

    def test_a_failing_stamp_never_costs_the_delivery(
            self, hie, monkeypatch, flat_node):
        """Review: the stamp sat outside publish_async's try, so a raise there
        delivered nothing.  The reply goes out; only the badge is missing."""
        import core.constants as constants

        def _broken(*a, **k):
            raise RuntimeError('rule broke')
        monkeypatch.setattr(constants, 'canonical_served_by', _broken)
        data, sse = self._publish(hie, monkeypatch, CHAT_TOPIC, {'text': ['hi']})
        assert data['text'] == ['hi']
        assert 'served_by' not in data
        assert sse and sse[0][1]['text'] == ['hi']

    def test_a_topic_that_is_not_chat_is_not_stamped(
            self, hie, monkeypatch, flat_node):
        bus = _Bus()
        monkeypatch.setattr(hie, 'client', None, raising=False)
        with patch('core.peer_link.message_bus.get_message_bus',
                   return_value=bus), \
                patch('core.platform.events.broadcast_sse_safe',
                      return_value=True):
            hie.publish_async('com.hertzai.hevolve.confirmation',
                              {'status': 'ok'})
        assert bus.published, 'nothing was published'
        for topic, data, _kw in bus.published:
            assert 'served_by' not in data, (topic, data)


# ─────────────────────────────────────────────────────────────────────
# _chat_reply: the HTTP reply carries it, and the spoken bubble gets it
# ─────────────────────────────────────────────────────────────────────

class TestChatReplyCarriesServedBy:

    @staticmethod
    def _reply(hie, tl, text='hello there', **payload):
        """Run the REAL _chat_reply; the fake TTS records whether it was
        called (its payload carries no badge, so no origin travels to it)."""
        origins = []
        fake_tts = MagicMock(
            side_effect=lambda *a, **k: origins.append(tl.get_served_by()))
        with patch.object(hie, '_tts_synthesize_and_publish', fake_tts), \
                patch('integrations.social.chat_messages.'
                      'persist_and_publish_async', MagicMock()), \
                hie.app.test_request_context('/chat', method='POST', json={}):
            resp = hie._chat_reply('user-1', 'req-1', text, **payload)
            body = resp.get_json()
        return body, origins

    def test_the_reply_is_local_on_a_flat_node(self, hie, tl, flat_node):
        body, origins = self._reply(hie, tl)
        assert body['served_by'] == 'local'
        assert body['response'] == 'hello there'
        assert len(origins) == 1, "the reply was not spoken"

    def test_the_reply_is_cloud_on_central(self, hie, tl, central_node):
        body, _ = self._reply(hie, tl)
        assert body['served_by'] == 'cloud'

    def test_a_payload_tag_is_named_in_the_canonical_vocabulary(
            self, hie, tl, flat_node):
        body, _ = self._reply(hie, tl, served_by='hive_langchain_bg')
        assert body['served_by'] == 'hive'

    def test_an_empty_reply_still_says_where_it_came_from(
            self, hie, tl, flat_node):
        body, origins = self._reply(hie, tl, text='')
        assert body['served_by'] == 'local'
        assert origins == []

    def test_the_thread_is_given_back_once_the_reply_is_sent(
            self, hie, tl, flat_node):
        self._reply(hie, tl, served_by='hive')
        assert tl.get_served_by() is None

    def test_the_voice_is_not_handed_an_origin(self, hie, tl, flat_node):
        """The spoken reply carries no badge, so _chat_reply names no origin
        around the TTS call (review of 028a8a959: restoring reply_from there
        survived every test)."""
        _body, origins = self._reply(hie, tl, served_by='hive_langchain_bg')
        assert origins == [None], origins


# ─────────────────────────────────────────────────────────────────────
# the spoken reply is the voice, not a bubble: no leg carries a badge
# ─────────────────────────────────────────────────────────────────────

class _InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class _ThreadExecutor:
    """The real shape: the synthesis runs on another thread."""

    def submit(self, fn, *args, **kwargs):
        t = threading.Thread(target=fn, args=args, kwargs=kwargs)
        t.start()
        t.join(30)


class TestASpokenReplyCarriesNoBadge:
    """Review of 028a8a959 (12:22Z): the page plays a 'TTS' payload and
    returns (Demopage.js handleDataReceived), so the voice carries no badge.
    The payload the TTS site builds had none, but publish_async stamped the
    chat.pupit envelope it maps the pupit topic to, and the task.confirmation
    copy of it, with the WRONG origin: the synthesis thread no longer knows
    it, so a hive expert's spoken answer went out 'local'.

    The real _tts_synthesize_and_publish runs on a real worker thread and
    calls the real publish_async; only the TTS engine, the bus and SSE are
    replaced (the engine with tests.unit.module_swap.swap_modules, never a
    patch.dict on sys.modules)."""

    @staticmethod
    def _speak(hie, monkeypatch, tl, origin, executor):
        from tests.unit.module_swap import swap_modules
        from integrations.channels.media import tts_text_normalizer as tn
        wav = os.path.join(tempfile.mkdtemp(), 'reply.wav')
        with open(wav, 'wb') as fh:
            fh.write(b'RIFF')
        engine = types.ModuleType('tts.tts_engine')
        engine.get_tts_engine = lambda: types.SimpleNamespace(
            backend_name='fake-engine')
        engine.synthesize_text = lambda t, language='en', **k: wav
        pkg = types.ModuleType('tts')
        pkg.tts_engine = engine
        bus, sse = _Bus(), []
        monkeypatch.setenv('HEVOLVE_EXTERNAL_URL', 'http://node.test')
        monkeypatch.setattr(hie, 'client', None, raising=False)
        with swap_modules({'tts': pkg, 'tts.tts_engine': engine}), \
                patch.object(hie, '_tts_executor', executor), \
                patch.object(tn, '_llm_normalize', lambda *a, **k: None), \
                patch('core.peer_link.message_bus.get_message_bus',
                      return_value=bus), \
                patch('core.platform.events.broadcast_sse_safe',
                      side_effect=_sse_recorder(sse)):
            with tl.reply_from(origin):
                hie._tts_synthesize_and_publish(
                    'a spoken answer', 'user-1', 'req-1', language='en')
        return bus.published, sse

    @pytest.mark.parametrize('executor', [_InlineExecutor(), _ThreadExecutor()],
                             ids=['inline', 'worker-thread'])
    @pytest.mark.parametrize('origin', [None, 'hive_langchain_bg'])
    def test_no_leg_of_a_spoken_reply_carries_a_badge(
            self, hie, monkeypatch, tl, flat_node, origin, executor):
        legs, sse = self._speak(hie, monkeypatch, tl, origin, executor)
        topics = [topic for topic, _data, _kw in legs]
        assert 'chat.pupit' in topics, topics
        assert 'task.confirmation' in topics, topics
        assert sse, 'the voice never reached SSE: nothing to assert'
        for topic, data, _kw in legs:
            assert 'served_by' not in data, (topic, data)
        for _evt, data, _user in sse:
            assert data.get('action') == 'TTS', data
            assert 'served_by' not in data, data


# ─────────────────────────────────────────────────────────────────────
# the escalation: the dispatcher knows, and now says so
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def local_registry():
    from integrations.agent_engine.model_registry import (
        ModelBackend, ModelRegistry, ModelTier)
    reg = ModelRegistry()
    reg.register(ModelBackend(
        model_id='qwen3.5-4b-local', display_name='Qwen3.5 4B (Local)',
        tier=ModelTier.FAST,
        config_list_entry={'model': 'Qwen3.5-4B', 'api_key': 'dummy',
                           'base_url': 'http://localhost:8080/v1',
                           'price': [0, 0]},
        avg_latency_ms=700.0, accuracy_score=0.60, is_local=True))
    return reg


@pytest.fixture
def hive_registry():
    from integrations.agent_engine.model_registry import (
        ModelBackend, ModelRegistry, ModelTier)
    reg = ModelRegistry()
    reg.register(ModelBackend(
        model_id='hive-node-alpha-qwen-27b', display_name='Hive: Qwen 27B',
        tier=ModelTier.EXPERT,
        config_list_entry={'model': 'qwen-27b', 'api_key': 'tok',
                           'base_url': 'https://node-alpha.example.com/v1',
                           'price': [0, 0]},
        avg_latency_ms=2000.0, accuracy_score=0.85, is_local=False))
    return reg


def _guardrails_open(monkeypatch):
    import security.hive_guardrails as hg
    monkeypatch.setattr(hg.HiveCircuitBreaker, 'is_halted', lambda: False)
    monkeypatch.setattr(hg.ConstitutionalFilter, 'check_prompt',
                        staticmethod(lambda p: (True, '')))


def _dispatcher(registry):
    from integrations.agent_engine.speculative_dispatcher import (
        SpeculativeDispatcher)
    d = SpeculativeDispatcher(model_registry=registry)
    d._health_probe_enabled = False
    return d


class TestExpertDeliveryNamesItsOrigin:

    @staticmethod
    def _collapsed(dispatcher, tl, expert, reply):
        """Run the real collapsed path; the faked delivery records the origin
        the thread named AT THE MOMENT it was called, and its call shape."""
        seen = []
        with patch.object(dispatcher, '_dispatch_expert_langchain',
                          return_value=reply), \
                patch.object(dispatcher, '_deliver_expert_response',
                             side_effect=lambda *a, **k: seen.append(
                                 (tl.get_served_by(), a, k))), \
                patch.object(dispatcher, '_record_interaction_safely'):
            dispatcher._run_collapsed_expert_path(
                'spec-1', 'p', 'r', expert, 'u', 'pid', None, 'general')
        return seen

    def test_a_remote_expert_is_delivered_as_hive(
            self, hive_registry, monkeypatch, tl):
        _guardrails_open(monkeypatch)
        d = _dispatcher(hive_registry)
        seen = self._collapsed(
            d, tl, hive_registry.get_expert_model(), 'hive answer')
        assert seen == [
            ('hive_langchain_bg', ('u', 'pid', 'spec-1', 'hive answer'), {})]
        assert d._results['spec-1']['served_by'] == 'hive_langchain_bg'

    def test_a_local_expert_is_delivered_as_local(
            self, local_registry, monkeypatch, tl):
        _guardrails_open(monkeypatch)
        d = _dispatcher(local_registry)
        seen = self._collapsed(
            d, tl, local_registry.get_fast_model(), 'local answer')
        assert seen == [
            ('local_langchain_bg', ('u', 'pid', 'spec-1', 'local answer'), {})]
        assert d._results['spec-1']['served_by'] == 'local_langchain_bg'

    def test_the_thread_is_given_back_after_the_delivery(
            self, hive_registry, monkeypatch, tl):
        _guardrails_open(monkeypatch)
        d = _dispatcher(hive_registry)
        self._collapsed(d, tl, hive_registry.get_expert_model(), 'hive answer')
        assert tl.get_served_by() is None

    def test_a_failed_delivery_still_gives_the_thread_back(
            self, hive_registry, monkeypatch, tl):
        _guardrails_open(monkeypatch)
        d = _dispatcher(hive_registry)
        with patch.object(d, '_dispatch_expert_langchain',
                          return_value='hive answer'), \
                patch.object(d, '_deliver_expert_response',
                             side_effect=RuntimeError('bus down')), \
                patch.object(d, '_record_interaction_safely'):
            with pytest.raises(RuntimeError):
                d._run_collapsed_expert_path(
                    'spec-1', 'p', 'r', hive_registry.get_expert_model(),
                    'u', 'pid', None, 'general')
        assert tl.get_served_by() is None

    def test_a_delivery_with_no_origin_makes_the_old_calls(
            self, local_registry):
        """_deliver_expert_response keeps its signature and its exact calls:
        callers and tests that predate the field are untouched."""
        from core.peer_link.message_bus import chat_topic_for
        tts, pub = MagicMock(), MagicMock()
        attrs = {'_tts_synthesize_and_publish': tts, 'publish_async': pub}
        with patch('core.safe_hartos_attr.safe_hartos_attr',
                   side_effect=attrs.get):
            _dispatcher(local_registry)._deliver_expert_response(
                'u', 'pid', 'spec-1', 'the answer')
        tts.assert_called_once_with('the answer', 'u', 'spec-1')
        pub.assert_called_once_with(chat_topic_for('u'), 'the answer')


class TestExpertEnvelopeCarriesItsOrigin:
    """The real dispatcher -> the real publish_async, reached the way the
    dispatcher reaches it: through safe_hartos_attr, which on the desktop
    returns Nunba's wrapper.  A keyword-based design fails HERE: the wrapper
    has three parameters, the publish raises, the reply is never sent.

    What this proves is the ENVELOPE: the expert's chat.* envelope reaches
    SSE carrying its origin.  It does not prove a badge: the expert's text
    goes out as a bare string, i.e. {'raw': ...}, and the page renders
    text[0] (Demopage.js handleDataReceived), so this envelope is not drawn
    at all -- a rendering gap of its own, not closed here."""

    @staticmethod
    def _run(hie, monkeypatch, tl, registry, expert):
        _guardrails_open(monkeypatch)
        d = _dispatcher(registry)
        bus, sse, spoken = _Bus(), [], []
        real_publish = hie.publish_async

        def nunba_wrapper(topic, message, timeout=2.0):
            # Shape of routes/hartos_backend_adapter.py's _patched.
            real_publish(topic, message, timeout)

        def fake_tts(text, user_id, request_id, **kw):
            spoken.append((text, tl.get_served_by(), kw))

        attrs = {'publish_async': nunba_wrapper,
                 '_tts_synthesize_and_publish': fake_tts}
        monkeypatch.setattr(hie, 'client', None, raising=False)
        with patch('core.safe_hartos_attr.safe_hartos_attr',
                   side_effect=attrs.get), \
                patch('core.peer_link.message_bus.get_message_bus',
                      return_value=bus), \
                patch('core.platform.events.broadcast_sse_safe',
                      side_effect=_sse_recorder(sse)), \
                patch.object(d, '_dispatch_expert_langchain',
                             return_value='an expert answer'), \
                patch.object(d, '_record_interaction_safely'):
            d._run_collapsed_expert_path(
                'spec-1', 'p', 'r', expert, 'user-1', 'pid', None, 'general')
        return sse, spoken

    def test_a_hive_expert_envelope_names_hive(
            self, hie, monkeypatch, tl, flat_node, hive_registry):
        sse, spoken = self._run(hie, monkeypatch, tl, hive_registry,
                                hive_registry.get_expert_model())
        assert [p['raw'] for _e, p, _u in sse] == ['an expert answer']
        assert sse[0][1]['served_by'] == 'hive'
        assert [t for t, _o, _k in spoken] == ['an expert answer']

    def test_a_local_expert_envelope_names_local(
            self, hie, monkeypatch, tl, flat_node, local_registry):
        sse, spoken = self._run(hie, monkeypatch, tl, local_registry,
                                local_registry.get_fast_model())
        assert sse[0][1]['served_by'] == 'local'
        assert [t for t, _o, _k in spoken] == ['an expert answer']

    def test_the_same_local_expert_is_cloud_on_central(
            self, hie, monkeypatch, tl, central_node, local_registry):
        sse, _ = self._run(hie, monkeypatch, tl, local_registry,
                           local_registry.get_fast_model())
        assert sse[0][1]['served_by'] == 'cloud'

    def test_the_nodes_own_model_on_a_public_api_is_cloud(
            self, hie, monkeypatch, tl, flat_node):
        """Review of 028a8a959 (12:22Z), blocker 1: with openrouter as the
        node's only text model (is_local=True in its catalog entry, the
        node's own path) the dispatcher tags the reply local_langchain_bg,
        and the envelope said 'local' while the same node's untagged reply
        said 'cloud'."""
        from integrations.agent_engine.model_registry import (
            ModelBackend, ModelRegistry, ModelTier)
        _on_api(monkeypatch, 'https://openrouter.ai/api/v1')
        reg = ModelRegistry()
        reg.register(ModelBackend(
            model_id='openrouter-node-model', display_name='OpenRouter model',
            tier=ModelTier.FAST,
            config_list_entry={'model': 'some-model', 'api_key': 'test-key',
                               'base_url': 'https://openrouter.ai/api/v1',
                               'price': [0, 0]},
            avg_latency_ms=900.0, accuracy_score=0.7, is_local=True))
        sse, _ = self._run(hie, monkeypatch, tl, reg, reg.get_fast_model())
        assert sse[0][1]['served_by'] == 'cloud'


# ─────────────────────────────────────────────────────────────────────
# the guard that keeps it at one
# ─────────────────────────────────────────────────────────────────────

#: Files allowed to WRITE a ``served_by`` key, and why.  Anything else is a
#: second rule for where a reply came from, and must go through
#: core.constants.canonical_served_by instead.
_ALLOWED_WRITERS = {
    'core/constants.py': 'the rule itself',
    'hart_intelligence_entry.py': 'publish_async and _chat_reply stamp the '
                                  'canonical value',
    'integrations/agent_engine/speculative_dispatcher.py':
        'the dispatch site: its telemetry tag, normalized on delivery',
    'integrations/agent_engine/compute_mesh_service.py':
        'a DIFFERENT meaning: the id of the device that ran an offloaded '
        'compute job, never a chat tier',
}
#: Nunba writes one of its own: the /chat HTTP reply's hard-coded 'local'
#: (routes/chatbot_routes.py), which should pass HARTOS's served_by through
#: instead (Nunba follow-up #905).  Any OTHER Nunba writer fails this guard.
_ALLOWED_NUNBA_WRITERS = {
    'routes/chatbot_routes.py': "the /chat HTTP reply's hard-coded 'local'; "
                                'follow-up #905 passes HARTOS\'s value through',
}
_SKIP_DIRS = {'tests', 'build', 'dist', 'node_modules', '.git', '__pycache__',
              'scripts', 'runs', 'python-embed', 'landing-page', '_drops_ready',
              '_buildsrc'}


def _writes_served_by(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value == 'served_by':
                    return True
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [
                node.target]
            for t in targets:
                if (isinstance(t, ast.Subscript)
                        and isinstance(t.slice, ast.Constant)
                        and t.slice.value == 'served_by'):
                    return True
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == 'served_by':
                    return True
            # d.setdefault('served_by', ...) writes it too (review)
            if (isinstance(node.func, ast.Attribute)
                    and node.func.attr in ('setdefault', '__setitem__')
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == 'served_by'):
                return True
    return False


def _writers_under(root):
    """Every .py under ``root`` that writes a served_by key, as root-relative
    paths.  Underscore modules and package __init__ files are scanned too:
    skipping them hid 189 + 241 files from the first version of this guard."""
    found = []
    for here, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS
                   and not d.startswith('venv') and not d.startswith('.')]
        for name in files:
            if not name.endswith('.py'):
                continue
            path = os.path.join(here, name)
            try:
                with open(path, encoding='utf-8') as fh:
                    tree = ast.parse(fh.read())
            except (SyntaxError, UnicodeDecodeError, OSError, ValueError):
                continue
            if _writes_served_by(tree):
                found.append(os.path.relpath(path, root).replace('\\', '/'))
    return found


@pytest.mark.parametrize('src, writes', [
    ("d['served_by'] = 'x'", True),
    ("d.setdefault('served_by', 'x')", True),
    ("d.update(served_by='x')", True),
    ("d = {'served_by': 'x'}", True),
    ("d.get('served_by')", False),
    ("x = d['served_by']", False),
])
def test_the_guard_sees_every_way_to_write_it(src, writes):
    assert _writes_served_by(ast.parse(src)) is writes


def test_source_guard_one_rule_for_where_a_reply_came_from():
    offenders = [rel for rel in _writers_under(PROJECT_ROOT)
                 if rel not in _ALLOWED_WRITERS]
    assert not offenders, (
        'These files write a served_by of their own.  A reply\'s origin is '
        'named by core.constants.canonical_served_by (local | hive | cloud); '
        f'add the file to _ALLOWED_WRITERS only with a reason: {offenders}')


def test_source_guard_the_sibling_repo_writes_no_second_rule():
    """Review: the guard could not see Nunba, which writes the /chat HTTP
    reply the page badges.  Its one known writer is listed with its
    follow-up; any other fails here."""
    nunba = os.path.join(os.path.dirname(PROJECT_ROOT), 'Nunba-HART-Companion')
    if not os.path.isdir(os.path.join(nunba, 'routes')):
        pytest.skip('the Nunba checkout is not beside this one')
    offenders = [rel for rel in _writers_under(nunba)
                 if rel not in _ALLOWED_NUNBA_WRITERS]
    assert not offenders, (
        f'Nunba writes served_by of its own in {offenders}: pass the value '
        'HARTOS stamped through instead')
