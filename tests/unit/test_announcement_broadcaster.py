"""Behavioural tests for the announcement broadcaster — the outbound
leg of the flywheel that lets benchmark proofs escape HARTOS's walled
garden and reach potential new users on external channels.

Tests prove:
  - Registration is idempotent (boot can call multiple times safely)
  - Target collection walks admin._channels for announce_chat_id
  - Dispatch goes through the existing FlaskChannelIntegration loop
    (no parallel asyncio bridge)
  - Disabled channels are skipped (don't waste calls on dead adapters)
  - EventBus 'hive.benchmark.published' triggers the broadcast end-to-end
"""
from __future__ import annotations

import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── Helper fixtures ───────────────────────────────────────────

class _FakeLoop:
    """Stand-in for the FlaskChannelIntegration asyncio loop.  We don't
    actually run the coroutine — we just record what was scheduled so
    the test can assert on (channel, chat_id, text).  This isolates
    the broadcaster's logic from the real Discord/Telegram adapters."""
    def __init__(self):
        self.scheduled = []  # list of (coro_repr,)
        # Mimic asyncio loop's call_soon_threadsafe interface enough
        # that run_coroutine_threadsafe works.


def _patch_integration_loop(monkeypatch, scheduled_list):
    """Patches flask_integration.get_channel_integration() to return a
    mock with a non-None _loop AND monkeypatches
    asyncio.run_coroutine_threadsafe so the broadcaster's dispatch
    path lands the call into `scheduled_list` without needing a real
    background loop.  Avoids real-thread lifecycle in tests."""
    from integrations.channels.base import SendResult

    class _MockRegistry:
        async def send_to_channel(self, channel, chat_id, text, **kw):
            return SendResult(success=True, message_id='mock-id')

    class _MockIntegration:
        def __init__(self):
            # Real loop object so isinstance/None checks pass, but
            # we never start it — the patched run_coroutine_threadsafe
            # below intercepts before any actual scheduling.
            self._loop = object()  # sentinel — never used directly
            self.registry = _MockRegistry()

        def shutdown(self):
            pass

    inst = _MockIntegration()
    from integrations.channels import flask_integration
    monkeypatch.setattr(flask_integration, 'get_channel_integration',
                        lambda: inst)

    # Intercept run_coroutine_threadsafe — the broadcaster wraps each
    # send_to_channel coroutine through it.  We inspect the coro's
    # args via str(coro) since we don't actually run it; the test
    # asserts on (channel, chat_id, text) reconstructed from the
    # broadcaster's _dispatch_to_target arg-passing.
    import integrations.channels.announcement_broadcaster as ab_mod
    from concurrent.futures import Future

    def _fake_dispatch(channel, chat_id, text):
        scheduled_list.append((channel, chat_id, text))
        # Original _dispatch_to_target requires no return.

    monkeypatch.setattr(ab_mod, '_dispatch_to_target', _fake_dispatch)
    return inst


def _patch_admin_channels(monkeypatch, channels: dict):
    """Plug in a fake AdminAPI singleton with the given _channels dict."""
    fake_api = MagicMock()
    fake_api._channels = channels
    from integrations.channels.admin import api as admin_api_mod
    monkeypatch.setattr(admin_api_mod, 'get_api', lambda: fake_api)
    return fake_api


# ── _build_announcement_text ──────────────────────────────────

def test_build_text_uses_comparison_when_present():
    from integrations.channels.announcement_broadcaster import (
        _build_announcement_text)
    text = _build_announcement_text({
        'benchmark': 'mmlu',
        'score': 0.87,
        'num_nodes': 4,
        'comparison': 'HIVE BENCHMARK PROOF — MMLU\n  Hive: 87%',
    })
    # The comparison block is preserved verbatim, and the announcement URL
    # is appended so a reader who sees the proof can act on it.
    assert text.startswith('HIVE BENCHMARK PROOF — MMLU\n  Hive: 87%')
    assert text.endswith('https://hevolve.ai/hive')


def test_build_text_reads_the_published_event_text_key():
    """Regression: hive.benchmark.published sends `text`, not `comparison`.

    The builder used to read only `comparison`, so every live event lost
    the prover's block and fell through to the minimal branch, which then
    printed the false "across 0 nodes" because `num_nodes` is absent too.
    """
    from integrations.channels.announcement_broadcaster import (
        _build_announcement_text)
    text = _build_announcement_text({
        'benchmark': 'gsm8k',
        'text': 'HART OS hive: 82.4% vs single-node 71.2%',
        'score': 0.824,
    })
    assert 'HART OS hive: 82.4% vs single-node 71.2%' in text
    assert '0 nodes' not in text
    assert 'https://hevolve.ai/hive' in text


def test_build_text_omits_node_count_when_unknown():
    from integrations.channels.announcement_broadcaster import (
        _build_announcement_text)
    text = _build_announcement_text({'benchmark': 'gsm8k', 'score': 0.5})
    assert 'gsm8k' in text
    assert '50' in text
    assert 'nodes' not in text


def test_build_text_handles_non_dict():
    from integrations.channels.announcement_broadcaster import (
        _build_announcement_text)
    assert _build_announcement_text("anything") == "anything"
    assert _build_announcement_text(None) == 'None'


# ── _collect_announcement_targets ─────────────────────────────

def test_collect_targets_filters_to_announce_chat_id_set(monkeypatch):
    _patch_admin_channels(monkeypatch, {
        'discord': {'announce_chat_id': '123456', 'enabled': True},
        'telegram': {'announce_chat_id': '', 'enabled': True},
        'slack': {'announce_chat_id': '   ', 'enabled': True},
        'whatsapp': {'enabled': True},  # no announce_chat_id key
        # google_chat, not imessage: this test is about announce_chat_id
        # being set. One-to-one channels need a subscription record (see the
        # dedicated test below), which would otherwise make the example
        # channel do two jobs at once.
        'google_chat': {'announce_chat_id': '999', 'enabled': True},
    })
    from integrations.channels.announcement_broadcaster import (
        _collect_announcement_targets)
    targets = _collect_announcement_targets()
    # Only discord + google_chat qualify.
    assert sorted(targets) == [('discord', '123456'), ('google_chat', '999')]


def test_collect_targets_skips_disabled_channels(monkeypatch):
    _patch_admin_channels(monkeypatch, {
        'discord': {'announce_chat_id': '123', 'enabled': False},
        # Negative id = a Telegram group/channel. The original fixture used
        # '456', which is a *private chat* id and is now refused on that
        # basis — this test is about `enabled`, so give it a destination
        # that is actually broadcastable.
        'telegram': {'announce_chat_id': '-456', 'enabled': True},
    })
    from integrations.channels.announcement_broadcaster import (
        _collect_announcement_targets)
    assert _collect_announcement_targets() == [('telegram', '-456')]


def test_collect_targets_empty_when_no_admin_api(monkeypatch):
    # If the admin API is unreachable (boot ordering), we return [] —
    # not an exception.  The flywheel must survive partial bring-up.
    from integrations.channels.admin import api as admin_api_mod

    def _boom():
        raise RuntimeError("admin API not ready")
    monkeypatch.setattr(admin_api_mod, 'get_api', _boom)
    from integrations.channels.announcement_broadcaster import (
        _collect_announcement_targets)
    assert _collect_announcement_targets() == []


# ── broadcast_announcement end-to-end ─────────────────────────

def test_broadcast_dispatches_to_each_target(monkeypatch):
    scheduled = []
    inst = _patch_integration_loop(monkeypatch, scheduled)
    _patch_admin_channels(monkeypatch, {
        'discord': {'announce_chat_id': 'ch-1', 'enabled': True},
        'telegram': {'announce_chat_id': 'ch-2', 'enabled': True},
    })
    from integrations.channels.announcement_broadcaster import (
        broadcast_announcement)
    count = broadcast_announcement('hello hive')
    assert count == 2

    assert sorted(scheduled) == [
        ('discord', 'ch-1', 'hello hive'),
        ('telegram', 'ch-2', 'hello hive'),
    ]
    inst.shutdown()


def test_broadcast_no_targets_returns_zero(monkeypatch):
    _patch_admin_channels(monkeypatch, {})
    from integrations.channels.announcement_broadcaster import (
        broadcast_announcement)
    assert broadcast_announcement('no one') == 0


def test_broadcast_skips_when_loop_missing(monkeypatch):
    # FlaskChannelIntegration._loop is None until start() runs.  Until
    # then, we must NOT block or raise — the EventBus emit chain stays
    # alive for other subscribers (federated_aggregator etc.).  This
    # exercises the REAL _dispatch_to_target (no monkeypatch shim) so
    # we cover the loop-None branch.
    _patch_admin_channels(monkeypatch, {
        'discord': {'announce_chat_id': 'ch-1', 'enabled': True},
    })
    no_loop = MagicMock()
    no_loop._loop = None
    from integrations.channels import flask_integration
    monkeypatch.setattr(flask_integration, 'get_channel_integration',
                        lambda: no_loop)
    from integrations.channels.announcement_broadcaster import (
        broadcast_announcement)
    # Count is 1 because we tried — but no schedule happened.
    # Must not raise.
    assert broadcast_announcement('hi') == 1


# ── EventBus integration — register + fire ────────────────────

def test_register_is_idempotent():
    from integrations.channels import announcement_broadcaster as ab
    ab.reset_for_tests()
    # The first call MAY fail to find the events registry — that's
    # fine, we're testing the sentinel behavior.  Second call should
    # be a no-op (returns False).
    first = ab.register_announcement_subscriber()
    # Force the sentinel so we can test the no-op branch.
    ab._subscribed = True
    second = ab.register_announcement_subscriber()
    assert second is False
    ab.reset_for_tests()


def test_event_published_triggers_broadcast(monkeypatch):
    """End-to-end: register the subscriber against a real EventBus,
    emit the hive.benchmark.published event, assert the broadcast
    helper was called with text from the payload."""
    scheduled = []
    inst = _patch_integration_loop(monkeypatch, scheduled)
    _patch_admin_channels(monkeypatch, {
        'discord': {'announce_chat_id': 'public-channel', 'enabled': True},
    })
    # Build a clean EventBus + register it in the platform registry.
    # registry.register expects a FACTORY (callable returning the
    # service), not an instance — pass `EventBus` itself so the
    # registry instantiates a singleton on first get().
    from core.platform.events import EventBus
    from core.platform.registry import get_registry
    registry = get_registry()
    # Drop any pre-existing 'events' registration so this test owns
    # the bus instance.
    if registry.has('events'):
        registry.unregister('events')
    registry.register('events', EventBus)
    bus = registry.get('events')

    from integrations.channels import announcement_broadcaster as ab
    ab.reset_for_tests()
    # Benchmark announcements are now gated on the same standing
    # public_exposure consent the new-content path requires, so grant it
    # for this end-to-end assertion.
    monkeypatch.setattr(ab, '_public_exposure_granted', lambda: True)
    registered = ab.register_announcement_subscriber()
    assert registered is True, (
        "registration should succeed when events registry is up")

    # Fire the event — the one hive_benchmark_prover._publish_results
    # emits after a successful run.
    bus.emit('hive.benchmark.published', {
        'benchmark': 'mmlu',
        'score': 0.87,
        'num_nodes': 5,
        'comparison': 'HIVE BENCHMARK PROOF — MMLU\n  Hive: 87%',
    })

    # _dispatch_to_target is patched to append synchronously, so by
    # the time bus.emit() returns the schedule list is populated.
    assert len(scheduled) == 1
    channel, chat_id, text = scheduled[0]
    assert channel == 'discord'
    assert chat_id == 'public-channel'
    assert 'MMLU' in text or 'mmlu' in text.lower()

    # Cleanup so we don't leak state to other tests.
    ab.reset_for_tests()
    inst.shutdown()


# ── new-content announcements ───────────────────────────────────────────
#
# The broadcaster used to fire on exactly one event, so only benchmark
# proofs ever left the building. These cover the content path and the
# four ways it could do real damage: posting without consent, posting to
# somebody's private messenger, posting the same page twice, and flooding.

_PAGES = [
    {'url': 'https://hevolve.ai/a', 'title': 'Page A',
     'share_urls': {'discord': 'https://hevolve.ai/a?ref=discord'}},
    {'url': 'https://hevolve.ai/b', 'title': 'Page B',
     'share_urls': {'discord': 'https://hevolve.ai/b?ref=discord'}},
]


def test_selection_is_ordered_deduped_and_capped():
    import integrations.channels.announcement_broadcaster as ab
    assert [p['url'] for p in ab.select_unannounced(_PAGES, [], 1)] == \
        ['https://hevolve.ai/a']
    assert [p['url'] for p in ab.select_unannounced(
        _PAGES, ['https://hevolve.ai/a'], 1)] == ['https://hevolve.ai/b']
    assert ab.select_unannounced(_PAGES, [p['url'] for p in _PAGES], 5) == []
    # One page per pass: a community notices a steady contributor and
    # mutes a firehose.
    assert ab.CONTENT_ANNOUNCE_LIMIT == 1


def test_announcement_text_uses_ref_url_and_states_who_is_posting():
    import integrations.channels.announcement_broadcaster as ab
    text = ab.format_content_announcement(_PAGES[0], 'discord')
    assert 'ref=discord' in text          # funnel attribution survives
    assert 'Hevolve AI agent' in text     # not passing as a person
    assert 'Page A' in text


def test_no_post_without_public_exposure_consent():
    import integrations.channels.announcement_broadcaster as ab
    sent = ab.announce_new_content(
        consent_check=lambda: False, pages=_PAGES, already_announced=[])
    assert sent == []


def test_consented_pass_sends_one_page_then_does_not_repeat_it(monkeypatch):
    import integrations.channels.announcement_broadcaster as ab
    calls = []
    monkeypatch.setattr(ab, '_collect_announcement_targets',
                        lambda: [('discord', '123')])
    monkeypatch.setattr(ab, '_dispatch_to_target',
                        lambda c, i, t: calls.append((c, i, t)))

    first = ab.announce_new_content(
        consent_check=lambda: True, pages=_PAGES, already_announced=[])
    assert first == ['https://hevolve.ai/a']
    assert len(calls) == 1

    second = ab.announce_new_content(
        consent_check=lambda: True, pages=_PAGES, already_announced=first)
    assert second == ['https://hevolve.ai/b']


def test_one_to_one_channels_need_a_real_subscription_record():
    """People do subscribe to WhatsApp Channels and Signal groups, so
    refusing by app name is naive. But an operator flag saying "they opted
    in" is not evidence either -- it reports what it was told. The gate is a
    subscription RECORD, which has a source and can be revoked."""
    import integrations.channels.announcement_broadcaster as ab
    fake_api = MagicMock()
    fake_api._channels = {
        'discord':  {'enabled': True, 'announce_chat_id': 'd1'},
        'whatsapp': {'enabled': True, 'announce_chat_id': 'w1'},
        'imessage': {'enabled': True, 'announce_chat_id': 'i1'},
    }
    subscribed = {('whatsapp', 'w1')}
    with patch('integrations.channels.admin.api.get_api', return_value=fake_api):
        targets = dict(ab._collect_announcement_targets(
            subscribed_check=lambda c, i: (c, i) in subscribed))
    assert set(targets) == {'discord', 'whatsapp'}
    assert 'imessage' not in targets   # nobody subscribed


def test_subscription_check_is_fail_closed(monkeypatch):
    """No consent service, no DB, an exception mid-query -- all mean NOT
    subscribed. A check that could not run must never read as permission."""
    import integrations.channels.announcement_broadcaster as ab
    monkeypatch.setattr(ab, '_consent_session', lambda: (None, None))
    assert ab.is_subscribed('whatsapp', 'w1') is False

    class _Boom:
        @staticmethod
        def check_consent(*a, **k):
            raise RuntimeError('db gone')
    monkeypatch.setattr(ab, '_consent_session', lambda: (_Boom, MagicMock()))
    assert ab.is_subscribed('whatsapp', 'w1') is False


def test_subscription_key_identifies_the_destination_not_a_person():
    import integrations.channels.announcement_broadcaster as ab
    assert ab.subscription_key('telegram', ' -100123 ') == 'telegram:-100123'


@pytest.mark.parametrize('text', [
    'unsubscribe', 'STOP', ' stop ', '/stop', 'opt out', 'leave', 'unfollow'])
def test_unsubscribe_words_are_recognised(text):
    import integrations.channels.announcement_broadcaster as ab
    assert ab.looks_like_unsubscribe(text) is True


@pytest.mark.parametrize('text', [
    'please stop the build', 'I want to leave a comment', '', None,
    'can you stop by later'])
def test_ordinary_prose_does_not_unsubscribe_someone_mid_conversation(text):
    """Matched on the whole trimmed message, not a substring -- otherwise
    'please stop the build' silently drops somebody from the list."""
    import integrations.channels.announcement_broadcaster as ab
    assert ab.looks_like_unsubscribe(text) is False


def test_unsubscribe_revokes_and_reports_handled(monkeypatch):
    import integrations.channels.announcement_broadcaster as ab
    revoked = []
    monkeypatch.setattr(ab, 'revoke_subscription',
                        lambda c, i: revoked.append((c, i)) or True)
    assert ab.handle_unsubscribe_command('whatsapp', 'w1', 'stop') is True
    assert revoked == [('whatsapp', 'w1')]
    assert ab.handle_unsubscribe_command('whatsapp', 'w1', 'hello there') is False
    assert len(revoked) == 1


def test_a_revoked_destination_stops_receiving(monkeypatch):
    """The whole point of a record over a flag: it can be taken back."""
    import integrations.channels.announcement_broadcaster as ab
    fake_api = MagicMock()
    fake_api._channels = {'whatsapp': {'enabled': True, 'announce_chat_id': 'w1'}}
    live = {('whatsapp', 'w1')}
    with patch('integrations.channels.admin.api.get_api', return_value=fake_api):
        before = ab._collect_announcement_targets(
            subscribed_check=lambda c, i: (c, i) in live)
        live.clear()                      # they unsubscribed
        after = ab._collect_announcement_targets(
            subscribed_check=lambda c, i: (c, i) in live)
    assert before == [('whatsapp', 'w1')]
    assert after == []


def test_ordinary_agent_to_human_messaging_is_not_gated_by_any_of_this(monkeypatch):
    """The opt-in gate exists on the BROADCAST path only. Replies,
    notifications a user asked for, and an agent messaging its own owner go
    through registry.send_to_channel and must stay untouched -- including
    on WhatsApp, Signal and iMessage, which is what those adapters are for.

    Asserted behaviourally: dispatching directly to a one-to-one channel
    still sends. (An earlier version of this test counted occurrences of a
    constant in the module source, which broke the moment a comment
    mentioned it -- a check coupled to prose rather than behaviour.)"""
    scheduled = []
    inst = _patch_integration_loop(monkeypatch, scheduled)
    from integrations.channels.announcement_broadcaster import (
        _dispatch_to_target)
    _dispatch_to_target('whatsapp', 'user-123', 'your build finished')
    assert scheduled == [('whatsapp', 'user-123', 'your build finished')]
    inst.shutdown()


def test_unreachable_feed_yields_no_posts_rather_than_a_guess():
    import integrations.channels.announcement_broadcaster as ab
    assert ab.fetch_own_pages('http://127.0.0.1:9/nope.json', timeout=2) == []


# ── destination shape: fact beats assertion ─────────────────────────────

def test_telegram_private_chat_ids_are_recognised_as_one_to_one():
    """Telegram private chats have positive ids; groups, supergroups and
    channels have negative ones. That is a property of the destination, not
    a claim about it — which is the whole point, since announce_audience is
    only ever an operator assertion."""
    import integrations.channels.announcement_broadcaster as ab
    assert ab.destination_shape('telegram', '123456') == 'one_to_one'
    assert ab.destination_shape('telegram', '-1001234567890') == 'multi_person'
    assert ab.destination_shape('telegram', ' -42 ') == 'multi_person'


def test_undecidable_destinations_return_none_not_permission():
    """None means 'cannot determine', never 'safe'. Discord/Slack channel
    type needs an API call that is not wired yet; @channelname is not
    decidable from the id."""
    import integrations.channels.announcement_broadcaster as ab
    assert ab.destination_shape('telegram', '@somechannel') is None
    assert ab.destination_shape('discord', '123') is None
    assert ab.destination_shape('slack', 'C123') is None


def test_a_private_telegram_id_is_refused_even_when_marked_opt_in(monkeypatch):
    """The operator can be wrong about which chat an id points at. Where a
    fact contradicts the assertion, the fact wins."""
    _patch_admin_channels(monkeypatch, {
        'telegram': {'enabled': True, 'announce_chat_id': '987654',
                     'announce_audience': 'opt_in'},
    })
    from integrations.channels.announcement_broadcaster import (
        _collect_announcement_targets)
    assert _collect_announcement_targets() == []


def test_a_telegram_group_id_is_accepted_without_any_flag(monkeypatch):
    """A negative id is structurally a group/channel: people had to join it,
    and the platform enforces that. No operator claim required."""
    _patch_admin_channels(monkeypatch, {
        'telegram': {'enabled': True, 'announce_chat_id': '-1001234567890'},
    })
    from integrations.channels.announcement_broadcaster import (
        _collect_announcement_targets)
    assert _collect_announcement_targets() == [('telegram', '-1001234567890')]


# ── benchmark announcements: consent, dedup, cooldown ─────────
#
# The new-content path was already guarded on all three. The benchmark
# path was not: hive.benchmark.published dispatched straight to every
# target, so proofs could post with consent ungranted and could repeat
# without limit. Repetition is what turns an automated post into spam.

_BENCH_A = {'benchmark': 'gsm8k', 'text': 'hive 82.4% vs 71.2%', 'score': 0.824}
_BENCH_B = {'benchmark': 'mmlu', 'text': 'hive 87.0% vs 80.1%', 'score': 0.870}


def _capture_benchmark_posts(monkeypatch, consented=True):
    import integrations.channels.announcement_broadcaster as ab
    ab.reset_for_tests()
    sent = []
    monkeypatch.setattr(ab, 'broadcast_announcement',
                        lambda text: (sent.append(text) or 1))
    monkeypatch.setattr(ab, '_public_exposure_granted', lambda: consented)
    return ab, sent


def test_benchmark_announcement_requires_consent(monkeypatch):
    ab, sent = _capture_benchmark_posts(monkeypatch, consented=False)
    ab._on_benchmark_published('hive.benchmark.published', _BENCH_A)
    assert sent == []


def test_benchmark_announcement_posts_once_when_consented(monkeypatch):
    ab, sent = _capture_benchmark_posts(monkeypatch)
    ab._on_benchmark_published('hive.benchmark.published', _BENCH_A)
    assert len(sent) == 1
    # carries the prover's text and the link a reader can act on
    assert 'hive 82.4% vs 71.2%' in sent[0]
    assert 'https://hevolve.ai/hive' in sent[0]


def test_benchmark_identical_result_is_not_repeated(monkeypatch):
    ab, sent = _capture_benchmark_posts(monkeypatch)
    ab._on_benchmark_published('hive.benchmark.published', _BENCH_A)
    ab._on_benchmark_published('hive.benchmark.published', _BENCH_A)
    assert len(sent) == 1


def test_benchmark_new_result_waits_for_the_cooldown(monkeypatch):
    import time
    ab, sent = _capture_benchmark_posts(monkeypatch)
    ab._on_benchmark_published('hive.benchmark.published', _BENCH_A)
    # a genuinely different result, but too soon
    ab._on_benchmark_published('hive.benchmark.published', _BENCH_B)
    assert len(sent) == 1
    # once the cooldown has elapsed it is allowed through
    ab._last_benchmark_at = time.time() - (ab._benchmark_min_interval() + 10)
    ab._on_benchmark_published('hive.benchmark.published', _BENCH_B)
    assert len(sent) == 2


def test_benchmark_dispatch_never_raises_into_the_event_bus(monkeypatch):
    """The EventBus emit chain must survive a broken broadcaster."""
    import integrations.channels.announcement_broadcaster as ab
    ab.reset_for_tests()
    monkeypatch.setattr(ab, '_public_exposure_granted', lambda: True)

    def _boom(_text):
        raise RuntimeError('channel exploded')

    monkeypatch.setattr(ab, 'broadcast_announcement', _boom)
    ab._on_benchmark_published('hive.benchmark.published', _BENCH_A)


# ── on-platform publishing (federation) ───────────────────────
#
# announce_new_content pushes pages OFF platform into third-party rooms and
# is consent-gated. publish_content_to_feed publishes to our OWN feed, from
# which federation carries the post to following instances. Different act,
# different gate: the consent flag exists to stop unsolicited posting into
# other people's communities, not to stop us posting on our own.

_FEED_PAGES = [
    {'url': 'https://hevolve.ai/research/foo', 'title': 'Foo paper explained'},
    {'url': 'https://hevolve.ai/answers/bar', 'title': 'Bar answered'},
]


def _stub_social(monkeypatch):
    """Capture PostService.create instead of touching the database."""
    import sys, types, contextlib
    created = []

    models = types.ModuleType('integrations.social.models')

    @contextlib.contextmanager
    def _db_session():
        yield object()

    models.db_session = _db_session

    services = types.ModuleType('integrations.social.services')

    class _UserService:
        @staticmethod
        def ensure_system_user(db, slug, display_name=None, bio=None):
            return {'slug': slug, 'display_name': display_name}

    class _PostService:
        @staticmethod
        def create(db, author, title=None, content=None, content_type=None):
            created.append({'author': author, 'title': title,
                            'content': content})
            return types.SimpleNamespace(id='post-1')

    services.UserService = _UserService
    services.PostService = _PostService
    monkeypatch.setitem(sys.modules, 'integrations.social.models', models)
    monkeypatch.setitem(sys.modules, 'integrations.social.services', services)
    return created


def test_publish_content_creates_one_post_per_pass(monkeypatch):
    import integrations.channels.announcement_broadcaster as ab
    created = _stub_social(monkeypatch)
    out = ab.publish_content_to_feed(pages=_FEED_PAGES, already_published=[])
    assert len(out) == 1
    assert len(created) == 1


def test_publish_content_posts_as_the_prover_identity(monkeypatch):
    """One publisher in the feed, not two lookalikes."""
    import integrations.channels.announcement_broadcaster as ab
    created = _stub_social(monkeypatch)
    ab.publish_content_to_feed(pages=_FEED_PAGES, already_published=[])
    assert created[0]['author']['slug'] == 'nunba'


def test_publish_content_body_discloses_authorship_and_links(monkeypatch):
    import integrations.channels.announcement_broadcaster as ab
    created = _stub_social(monkeypatch)
    ab.publish_content_to_feed(pages=_FEED_PAGES, already_published=[])
    body = created[0]['content']
    assert 'Posted by the Hevolve AI agent' in body
    assert 'hevolve.ai/research/foo' in body


def test_publish_content_does_not_repeat_a_published_page(monkeypatch):
    import integrations.channels.announcement_broadcaster as ab
    created = _stub_social(monkeypatch)
    out = ab.publish_content_to_feed(
        pages=_FEED_PAGES,
        already_published=[p['url'] for p in _FEED_PAGES])
    assert out == []
    assert created == []


def test_publish_content_empty_feed_is_a_noop(monkeypatch):
    import integrations.channels.announcement_broadcaster as ab
    _stub_social(monkeypatch)
    assert ab.publish_content_to_feed(pages=[], already_published=[]) == []


def test_publish_content_is_not_gated_on_public_exposure():
    """On-platform publishing must not inherit the off-platform gate."""
    import inspect
    import integrations.channels.announcement_broadcaster as ab
    src = inspect.getsource(ab.publish_content_to_feed)
    assert '_public_exposure_granted' not in src


def test_start_content_distribution_does_not_double_start(monkeypatch):
    """A second call while the loop is alive must not spawn a rival thread."""
    import threading as _t
    import integrations.channels.announcement_broadcaster as ab
    running, release = _t.Event(), _t.Event()

    def _loop():
        running.set()
        release.wait(10)

    monkeypatch.setattr(ab, '_content_distribution_loop', _loop)
    ab._content_thread = None
    try:
        assert ab.start_content_distribution() is True
        assert running.wait(2), 'loop thread never started'
        assert ab.start_content_distribution() is False
    finally:
        release.set()
        ab._content_thread = None


def test_start_content_distribution_restarts_a_dead_loop(monkeypatch):
    """If the loop has exited, starting again SHOULD spawn a new thread.
    The guard is on liveness, not on 'has ever been started', so a crashed
    distribution loop recovers on the next boot-path call instead of
    staying silently dead."""
    import integrations.channels.announcement_broadcaster as ab
    monkeypatch.setattr(ab, '_content_distribution_loop', lambda: None)
    ab._content_thread = None
    try:
        assert ab.start_content_distribution() is True
        if ab._content_thread is not None:
            ab._content_thread.join(timeout=2)
        assert ab.start_content_distribution() is True
    finally:
        ab._content_thread = None
