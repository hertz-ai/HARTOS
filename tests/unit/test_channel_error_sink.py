"""Behavioural tests for the canonical channel-error sink wired on the
ChannelAdapter base class.

The 2026-05-27 audit found that:
  - MetricsCollector.record_error and AdminDashboard.record_error both
    exist with full schema + persistence
  - NO real channel adapter ever called either (only test files)
  - Adapters silently failed (auth errors, SDK missing, etc.) with
    zero signal to the operator

Fix: base.ChannelAdapter.__init_subclass__ auto-wraps every method in
``_AUTO_RECORD_METHODS`` so escaping exceptions land in:
  (1) MetricsCollector — Prometheus counter
  (2) AdminDashboard   — structured admin-UI log
  (3) publish_event('setup_progress', ...) — when severity is critical
      (auth / sdk_missing) → UI shows actionable card

These tests prove the wiring works against a fake adapter — no real
Telegram / Discord / WhatsApp dependencies in CI.  Behavioural style:
patch the canonical sinks, invoke the wrapped method, assert on
recorded side-effects.  No grep tests.
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── Test adapter (fresh subclass per test so __init_subclass__ fires) ──

def _make_adapter_class(*,
                        raise_in_send: Optional[Exception] = None,  # type: ignore[name-defined]  # noqa: F821
                        raise_in_connect: Optional[Exception] = None,  # noqa: F821
                        return_send=None):
    """Build a fresh ChannelAdapter subclass.  Defined inline per-test
    so __init_subclass__ is invoked on a clean class object — without
    this, Python caches the wrapped methods and the second test sees
    the first test's wrappers."""
    from integrations.channels.base import (
        ChannelAdapter, ChannelConfig, SendResult,
    )

    class _FakeAdapter(ChannelAdapter):
        @property
        def name(self) -> str:
            return 'fake'

        async def connect(self) -> bool:
            if raise_in_connect is not None:
                raise raise_in_connect
            return True

        async def disconnect(self) -> None:
            pass

        async def send_message(self, chat_id, text, **kwargs):
            if raise_in_send is not None:
                raise raise_in_send
            return return_send or SendResult(success=True, message_id='m1')

        async def edit_message(self, *a, **kw):
            return SendResult(success=True)

        async def delete_message(self, *a, **kw):
            return True

        async def send_typing(self, *a, **kw):
            return None

        async def get_chat_info(self, *a, **kw):
            return None

    return _FakeAdapter


# Inline typing.Optional re-export so the test file is self-contained
from typing import Optional  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def sinks(monkeypatch):
    """Patch all four downstream sinks + capture every call.
    Returned dict has lists of recorded calls per channel-error path."""
    captured = {
        'metrics': [],            # list of (channel, error_type)
        'dashboard': [],          # list of kwargs dicts
        'sse': [],                # list of (topic, payload)
        'self_heal': [],          # list of (subsystem, identifier, exc_type, function, ctx)
    }

    fake_metrics = MagicMock()
    fake_metrics.record_error.side_effect = (
        lambda ch, et: captured['metrics'].append((ch, et)))
    fake_dashboard = MagicMock()

    from integrations.channels.admin import dashboard as dash_mod
    # ErrorSeverity stays real so the severity enum lookup works.
    fake_dashboard.record_error.side_effect = (
        lambda **kw: captured['dashboard'].append(kw))

    # Replace lazy-import targets — base.py imports these at call time.
    import integrations.channels.admin.metrics as metrics_mod
    monkeypatch.setattr(
        metrics_mod, 'get_metrics_collector', lambda *a, **kw: fake_metrics)
    monkeypatch.setattr(
        dash_mod, 'get_dashboard', lambda *a, **kw: fake_dashboard)

    # SSE publish stub
    import integrations.social.realtime as rt_mod
    monkeypatch.setattr(
        rt_mod, 'publish_event',
        lambda topic, data, **kw: captured['sse'].append((topic, data)))

    # Self-heal pipeline stub — capture the canonical helper invocation
    # so we can assert the (subsystem, identifier, exc_type) shape that
    # SelfHealingDispatcher's pattern_key grouping depends on.
    from hartos import exception_collector as ec_mod
    def _fake_report(subsystem, identifier, exc, function, **ctx):
        captured['self_heal'].append({
            'subsystem': subsystem,
            'identifier': identifier,
            'exc_type': type(exc).__name__,
            'exc_msg': str(exc),
            'function': function,
            'ctx': ctx,
        })
    monkeypatch.setattr(ec_mod, 'report_subsystem_failure', _fake_report)

    return captured


# ── 1. Classifier maps exception types to canonical error_type strings ──

@pytest.mark.parametrize('exc_cls,expected', [
    ('ChannelRateLimitError',  'rate_limit'),
    ('ChannelAuthError',       'auth'),
    ('ChannelSDKMissingError', 'sdk_missing'),
    ('ChannelConnectionError', 'connect_failed'),
    ('ChannelSendError',       'send_failed'),
])
def test_classifier_known_channel_exceptions(exc_cls, expected):
    from integrations.channels import base as base_mod
    exc_class = getattr(base_mod, exc_cls)
    # ChannelRateLimitError takes retry_after; others take a message.
    try:
        exc = exc_class()
    except TypeError:
        exc = exc_class('x')
    assert base_mod.ChannelAdapter._classify_exception(exc) == expected


def test_classifier_timeouts_and_network():
    from integrations.channels.base import ChannelAdapter
    assert ChannelAdapter._classify_exception(asyncio.TimeoutError()) == 'timeout'
    assert ChannelAdapter._classify_exception(TimeoutError('x')) == 'timeout'
    assert ChannelAdapter._classify_exception(ImportError('x')) == 'sdk_missing'
    assert ChannelAdapter._classify_exception(ConnectionError('x')) == 'network'
    assert ChannelAdapter._classify_exception(OSError('x')) == 'network'


def test_classifier_unknown_falls_back():
    from integrations.channels.base import ChannelAdapter
    class WeirdError(Exception): ...
    assert ChannelAdapter._classify_exception(
        WeirdError(), fallback='send_failed') == 'send_failed'
    assert ChannelAdapter._classify_exception(
        WeirdError(), fallback='unknown_op') == 'unknown_op'


# ── 2. Severity assignment ────────────────────────────────────────

@pytest.mark.parametrize('error_type,expected_sev', [
    ('auth',           'critical'),
    ('sdk_missing',    'critical'),
    ('rate_limit',     'warning'),
    ('send_failed',    'error'),
    ('timeout',        'error'),
    ('network',        'error'),
    ('connect_failed', 'error'),
])
def test_severity_for_known_error_types(error_type, expected_sev):
    from integrations.channels.base import ChannelAdapter
    assert ChannelAdapter._severity_for(error_type) == expected_sev


# ── 3. Auto-wrap: escaping exception lands in all three sinks ─────

@pytest.mark.asyncio
async def test_send_message_auth_error_fires_all_three_sinks(sinks):
    """ChannelAuthError escaping send_message must:
       1. increment metrics counter ('fake', 'auth')
       2. write dashboard error row with severity='critical'
       3. publish setup_progress SSE with action_hint='reconfigure_fake_token'
       AND re-raise the original exception (caller contract preserved)."""
    from integrations.channels.base import ChannelAuthError, ChannelConfig
    AdapterCls = _make_adapter_class(
        raise_in_send=ChannelAuthError('token expired'))
    adapter = AdapterCls(ChannelConfig())

    with pytest.raises(ChannelAuthError, match='token expired'):
        await adapter.send_message(chat_id='c1', text='hello')

    # (1) Metrics counter
    assert sinks['metrics'] == [('fake', 'auth')]

    # (2) Dashboard structured log
    assert len(sinks['dashboard']) == 1
    dash = sinks['dashboard'][0]
    assert dash['channel'] == 'fake'
    assert dash['error_type'] == 'auth'
    assert dash['message'] == 'token expired'
    assert dash['stack_trace'] is not None  # exc was active

    # (3) setup_progress SSE — critical only
    assert len(sinks['sse']) == 1
    topic, payload = sinks['sse'][0]
    assert topic == 'setup_progress'
    assert payload['status'] == 'needs_user_action'
    assert payload['channel'] == 'fake'
    assert payload['error_type'] == 'auth'
    assert payload['action_hint'] == 'reconfigure_fake_token'


@pytest.mark.asyncio
async def test_send_message_send_failed_records_but_no_sse(sinks):
    """A generic ChannelSendError is severity='error' (not critical),
    so it lands in metrics + dashboard but NOT setup_progress.  Card
    spam would render every transient failure as an actionable prompt
    — we route those to the admin panel, not the live UI."""
    from integrations.channels.base import ChannelSendError, ChannelConfig
    AdapterCls = _make_adapter_class(
        raise_in_send=ChannelSendError('500 backend down'))
    adapter = AdapterCls(ChannelConfig())

    with pytest.raises(ChannelSendError):
        await adapter.send_message(chat_id='c1', text='hi')

    assert sinks['metrics'] == [('fake', 'send_failed')]
    assert len(sinks['dashboard']) == 1
    assert sinks['dashboard'][0]['error_type'] == 'send_failed'
    # NO SSE card — non-critical
    assert sinks['sse'] == []


@pytest.mark.asyncio
async def test_rate_limit_records_as_warning_no_sse(sinks):
    """Rate-limit is design-intent transient backoff — counter tracks
    frequency but NO setup_progress card (would be spam)."""
    from integrations.channels.base import ChannelRateLimitError, ChannelConfig
    AdapterCls = _make_adapter_class(
        raise_in_send=ChannelRateLimitError(retry_after=42))
    adapter = AdapterCls(ChannelConfig())

    with pytest.raises(ChannelRateLimitError):
        await adapter.send_message(chat_id='c1', text='hi')

    assert sinks['metrics'] == [('fake', 'rate_limit')]
    # Dashboard severity is 'warning' for rate_limit
    from integrations.channels.admin.dashboard import ErrorSeverity
    assert sinks['dashboard'][0]['severity'] == ErrorSeverity.WARNING
    assert sinks['sse'] == []


@pytest.mark.asyncio
async def test_sdk_missing_at_connect_fires_install_card(sinks):
    """ImportError or ChannelSDKMissingError on connect → critical
    setup_progress card with action_hint='install_<channel>_sdk'."""
    from integrations.channels.base import ChannelConfig
    AdapterCls = _make_adapter_class(
        raise_in_connect=ImportError(
            'No module named "discord"; install discord.py'))
    adapter = AdapterCls(ChannelConfig())

    with pytest.raises(ImportError):
        await adapter.connect()

    assert sinks['metrics'] == [('fake', 'sdk_missing')]
    assert len(sinks['sse']) == 1
    _, payload = sinks['sse'][0]
    assert payload['error_type'] == 'sdk_missing'
    assert payload['action_hint'] == 'install_fake_sdk'


# ── 3b. Self-heal pipeline push uses canonical helper ─────────────

@pytest.mark.asyncio
async def test_self_heal_push_uses_canonical_helper(sinks):
    """The (4) sink — ExceptionCollector / self-heal pipeline — must
    route through ``exception_collector.report_subsystem_failure``
    with ``subsystem='channels'`` and ``identifier=<adapter name>``.
    Without this canonical-helper enforcement each channel adapter
    would build its own ``module=f'channels.{name}'`` string —
    parallel-path drift the user explicitly called out
    (2026-05-28).  The helper centralises the module-key shape so
    SelfHealingDispatcher's pattern_key grouping clusters all
    failures on the same adapter under one self_heal goal."""
    from integrations.channels.base import ChannelAuthError, ChannelConfig
    AdapterCls = _make_adapter_class(
        raise_in_send=ChannelAuthError('token expired'))
    adapter = AdapterCls(ChannelConfig())

    with pytest.raises(ChannelAuthError):
        await adapter.send_message(chat_id='c1', text='hi')

    assert len(sinks['self_heal']) == 1
    push = sinks['self_heal'][0]
    assert push['subsystem'] == 'channels'
    assert push['identifier'] == 'fake'
    assert push['exc_type'] == 'ChannelAuthError'
    assert push['function'] == 'send_message'
    # The ctx must carry error_type + severity so the dispatcher /
    # self-heal agent can decide which repair tool to use.
    assert push['ctx'].get('error_type') == 'auth'
    assert push['ctx'].get('severity') == 'critical'


@pytest.mark.asyncio
async def test_self_heal_push_skipped_when_no_exception(sinks):
    """Manual ``_record_channel_error(error_type, exc=None)`` (the
    opt-in path for adapters that swallow + return SendResult)
    should NOT push to the self-heal pipeline — ExceptionCollector
    requires a real Python exception for the type + traceback
    extraction."""
    from integrations.channels.base import ChannelConfig
    AdapterCls = _make_adapter_class()
    adapter = AdapterCls(ChannelConfig())

    adapter._record_channel_error(
        'send_failed', exc=None, context={'method': 'custom_op'})

    # metrics + dashboard still fire (they take strings)
    assert sinks['metrics'] == [('fake', 'send_failed')]
    assert len(sinks['dashboard']) == 1
    # but self-heal does NOT (no Python exception to record)
    assert sinks['self_heal'] == []


# ── 4. Caller contract: success path is untouched ─────────────────

@pytest.mark.asyncio
async def test_success_path_no_side_effects(sinks):
    """When send_message returns normally, the wrapper records NOTHING
    on any of the 4 sinks (metrics, dashboard, sse, self-heal)."""
    from integrations.channels.base import ChannelConfig
    AdapterCls = _make_adapter_class()
    adapter = AdapterCls(ChannelConfig())

    result = await adapter.send_message(chat_id='c1', text='hi')
    assert result.success is True
    assert sinks['metrics'] == []
    assert sinks['dashboard'] == []
    assert sinks['sse'] == []
    assert sinks['self_heal'] == []


# ── 5. Manual _record_channel_error call (opt-in from internal except) ──

def test_manual_record_call_works_outside_wrapped_method(sinks):
    """Adapters with internal try/except that swallow exceptions can
    still report them by calling self._record_channel_error directly.
    This is the opt-in path documented in the base class."""
    from integrations.channels.base import ChannelConfig, ChannelSendError
    AdapterCls = _make_adapter_class()
    adapter = AdapterCls(ChannelConfig())

    adapter._record_channel_error(
        'send_failed',
        ChannelSendError('swallowed inside adapter'),
        context={'method': 'custom_internal_op'})

    assert sinks['metrics'] == [('fake', 'send_failed')]
    assert len(sinks['dashboard']) == 1
    assert sinks['dashboard'][0]['context']['method'] == 'custom_internal_op'


# ── 6. Idempotency of __init_subclass__ wrapping ──────────────────

def test_wrapping_is_idempotent():
    """Re-loading the adapter class (e.g. importlib.reload in dev)
    must not stack the wrapper N times.  The _AUTO_RECORD_WRAPPED_ATTR
    sentinel short-circuits double-wraps."""
    from integrations.channels.base import (
        ChannelAdapter, _AUTO_RECORD_WRAPPED_ATTR,
    )
    AdapterCls = _make_adapter_class()
    method = AdapterCls.__dict__['send_message']
    assert getattr(method, _AUTO_RECORD_WRAPPED_ATTR, False) is True
    # Calling __init_subclass__ manually a second time must be a no-op.
    ChannelAdapter.__init_subclass__.__func__(AdapterCls)
    method_after = AdapterCls.__dict__['send_message']
    # Same object reference — not re-wrapped.
    assert method_after is method


# ── 7. Sink failure does NOT mask the original exception ──────────

@pytest.mark.asyncio
async def test_sink_failure_does_not_swallow_original_exception(monkeypatch):
    """If the metrics / dashboard / SSE machinery itself raises, the
    wrapper must still re-raise the adapter's original exception so
    the caller's contract is preserved.  Observability must not
    become a new failure surface."""
    from integrations.channels.base import (
        ChannelAuthError, ChannelConfig,
    )
    import integrations.channels.admin.metrics as metrics_mod

    def _boom_metrics(*a, **kw):
        raise RuntimeError("metrics DB down")
    monkeypatch.setattr(metrics_mod, 'get_metrics_collector', _boom_metrics)

    AdapterCls = _make_adapter_class(
        raise_in_send=ChannelAuthError('token bad'))
    adapter = AdapterCls(ChannelConfig())

    # Original ChannelAuthError must still bubble up despite the
    # broken sink.
    with pytest.raises(ChannelAuthError, match='token bad'):
        await adapter.send_message(chat_id='c1', text='hi')
