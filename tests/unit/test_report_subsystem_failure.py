"""Behavioural tests for ``exception_collector.report_subsystem_failure``
— the single canonical entry point every silent-failure surface uses
to feed the self-heal pipeline (2026-05-28).

Without one helper enforcing the module-key shape, each subsystem
would build its own ``module=f'<subsystem>.{id}'`` string and the
SelfHealingDispatcher's ``pattern_key`` grouping would drift
unpredictably across:

  - tts.<backend>      (engine demotion / install fail)
  - channels.<adapter> (auth / sdk_missing / send_failed)
  - vlm.<model>        (load fail / OOM)
  - llm.<model>        (load fail / context overflow)
  - daemon.<name>      (thread death)
  - tool.<name>        (dynamic registration miss)

These tests lock the contract so future subsystems don't re-invent
the format.  Behavioural style: real ExceptionCollector singleton,
mocked nothing.  We push real exceptions and inspect the records
the collector stored.
"""
from __future__ import annotations

import os
import sys

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def fresh_collector():
    """Reset the singleton so each test sees an empty buffer."""
    from hartos.exception_collector import ExceptionCollector
    ExceptionCollector.reset_instance()
    collector = ExceptionCollector.get_instance()
    try:
        yield collector
    finally:
        ExceptionCollector.reset_instance()


# ── Module-key shape ─────────────────────────────────────────────

def test_module_key_is_subsystem_dot_identifier(fresh_collector):
    """``report_subsystem_failure('tts', 'indic_parler', exc, fn)``
    must produce ``module='tts.indic_parler'`` — the convention
    SelfHealingDispatcher's pattern_key splits on."""
    from hartos.exception_collector import report_subsystem_failure

    try:
        raise RuntimeError('parler version conflict')
    except RuntimeError as e:
        report_subsystem_failure('tts', 'indic_parler', e,
                                 '_record_backend_failure')

    recs = fresh_collector.get_unresolved()
    assert len(recs) == 1
    rec = recs[0]
    assert rec.module == 'tts.indic_parler'
    assert rec.function == '_record_backend_failure'
    assert rec.exc_type == 'RuntimeError'
    # pattern_key format = "{ExcType}::{module}::{function}"
    assert rec.pattern_key == (
        'RuntimeError::tts.indic_parler::_record_backend_failure')


def test_module_key_omits_dot_when_identifier_missing(fresh_collector):
    """A subsystem-wide failure with no specific identifier (e.g.
    the agent_daemon supervisor itself died) should produce
    ``module='daemon'`` not ``module='daemon.'``."""
    from hartos.exception_collector import report_subsystem_failure

    try:
        raise SystemExit('supervisor thread died')
    except SystemExit as e:
        report_subsystem_failure('daemon', '', e, 'tick')

    rec = fresh_collector.get_unresolved()[0]
    assert rec.module == 'daemon'  # no trailing dot
    assert rec.pattern_key == 'SystemExit::daemon::tick'


# ── Context preserved + subsystem identity injected ──────────────

def test_context_includes_subsystem_identity_for_repair_tool_routing(
        fresh_collector):
    """The self-heal agent picks the right repair tool based on
    ``record.context['subsystem']`` — without this, an agent
    receiving a self_heal goal can't tell if it's a TTS install
    fix or a channel reconnect fix."""
    from hartos.exception_collector import report_subsystem_failure

    try:
        raise ImportError('No module named discord.py')
    except ImportError as e:
        report_subsystem_failure('channels', 'discord', e, 'connect',
                                 error_type='sdk_missing',
                                 severity='critical',
                                 retry_count=3)

    rec = fresh_collector.get_unresolved()[0]
    assert rec.context['subsystem'] == 'channels'
    assert rec.context['identifier'] == 'discord'
    assert rec.context['error_type'] == 'sdk_missing'
    assert rec.context['severity'] == 'critical'
    assert rec.context['retry_count'] == 3


# ── Pattern grouping clusters same-subsystem failures together ───

def test_three_failures_on_same_backend_share_one_pattern_key(
        fresh_collector):
    """SelfHealingDispatcher's _min_occurrences threshold relies on
    same-pattern-key dedup.  Three independent TelegramError
    instances on the same adapter MUST land under one pattern_key
    so the dispatcher counts them together and creates ONE
    self_heal goal — not three."""
    from hartos.exception_collector import report_subsystem_failure

    class TelegramError(Exception):
        pass

    for i in range(3):
        try:
            raise TelegramError(f'send 5{i}0 backend timeout')
        except TelegramError as e:
            report_subsystem_failure('channels', 'telegram', e,
                                     'send_message')

    patterns = fresh_collector.get_patterns(min_count=1)
    assert len(patterns) == 1, (
        f"expected 1 pattern_key with 3 occurrences, got "
        f"{len(patterns)} patterns: {list(patterns.keys())}"
    )
    pattern_key = next(iter(patterns))
    assert pattern_key == 'TelegramError::channels.telegram::send_message'
    assert len(patterns[pattern_key]) == 3


def test_different_subsystems_split_pattern_keys(fresh_collector):
    """A timeout on TTS and a timeout on Discord must NOT cluster
    into one pattern_key — different subsystems require different
    repair tools."""
    from hartos.exception_collector import report_subsystem_failure

    try:
        raise TimeoutError('tts synth took 60s')
    except TimeoutError as e:
        report_subsystem_failure('tts', 'indic_parler', e, 'synthesize')
    try:
        raise TimeoutError('discord webhook 30s')
    except TimeoutError as e:
        report_subsystem_failure('channels', 'discord', e, 'send_message')

    patterns = fresh_collector.get_patterns(min_count=1)
    assert len(patterns) == 2  # two distinct pattern_keys
    keys = sorted(patterns.keys())
    assert keys == [
        'TimeoutError::channels.discord::send_message',
        'TimeoutError::tts.indic_parler::synthesize',
    ]


# ── Fire-and-forget: never raises into caller ────────────────────

def test_helper_never_raises(monkeypatch):
    """Helper must swallow ALL internal errors — observability must
    not become a failure surface for the caller."""
    from hartos.exception_collector import report_subsystem_failure, ExceptionCollector

    # Break the collector so the inner .record() raises.
    def _boom(*a, **kw):
        raise RuntimeError("collector broke")
    monkeypatch.setattr(ExceptionCollector.get_instance(), 'record', _boom)

    try:
        raise ValueError('caller exception')
    except ValueError as e:
        # MUST NOT raise — caller is already in an except block; we
        # can't make a bad situation worse.
        report_subsystem_failure('tts', 'broken', e, 'fn')


# ── Subsystem taxonomy: lock the convention so future drift is caught ──

@pytest.mark.parametrize('subsystem,identifier,expected_module', [
    ('tts',      'indic_parler',     'tts.indic_parler'),
    ('tts',      'chatterbox_turbo', 'tts.chatterbox_turbo'),
    ('channels', 'telegram',         'channels.telegram'),
    ('channels', 'discord',          'channels.discord'),
    ('vlm',      'minicpm',          'vlm.minicpm'),
    ('vlm',      'qwen3_4b_vl',      'vlm.qwen3_4b_vl'),
    ('llm',      'qwen3_4b',         'llm.qwen3_4b'),
    ('daemon',   'agent_daemon',     'daemon.agent_daemon'),
    ('daemon',   'hive_benchmark_prover', 'daemon.hive_benchmark_prover'),
    ('tool',     'pdf_extract',      'tool.pdf_extract'),
])
def test_subsystem_taxonomy_uniform(fresh_collector, subsystem,
                                    identifier, expected_module):
    """Lock the canonical subsystem.identifier shape across every
    subsystem that will use this helper.  If a future subsystem
    invents 'channels::discord' or 'tts-indic_parler' instead, the
    pattern_key grouping breaks and the dispatcher can't dedup."""
    from hartos.exception_collector import report_subsystem_failure

    try:
        raise RuntimeError('test')
    except RuntimeError as e:
        report_subsystem_failure(subsystem, identifier, e, 'op')

    rec = fresh_collector.get_unresolved()[0]
    assert rec.module == expected_module


# ── AutoReportSubsystemFailures mixin ─────────────────────────────

def test_mixin_auto_wraps_listed_methods(fresh_collector):
    """A class that inherits the mixin + sets SUBSYSTEM +
    AUTO_REPORTED_METHODS has those methods auto-wrapped at
    class-load time.  Exceptions escaping wrapped methods feed the
    self-heal pipeline with the right (subsystem, identifier)
    shape — zero per-method except-block edits required."""
    from hartos.exception_collector import AutoReportSubsystemFailures

    class FakeBackend(AutoReportSubsystemFailures):
        SUBSYSTEM = 'vlm'
        AUTO_REPORTED_METHODS = ('start', 'describe')

        @property
        def name(self) -> str:
            return 'fake_backend'

        def start(self):
            raise RuntimeError('CUDA OOM')

        def describe(self, frame_bytes):
            raise ImportError('missing transformers')

    inst = FakeBackend()
    with pytest.raises(RuntimeError, match='CUDA OOM'):
        inst.start()
    with pytest.raises(ImportError, match='missing transformers'):
        inst.describe(b'jpeg-bytes')

    recs = fresh_collector.get_unresolved()
    assert len(recs) == 2
    by_function = {r.function: r for r in recs}
    assert by_function['start'].module == 'vlm.fake_backend'
    assert by_function['start'].exc_type == 'RuntimeError'
    assert by_function['describe'].module == 'vlm.fake_backend'
    assert by_function['describe'].exc_type == 'ImportError'


@pytest.mark.asyncio
async def test_mixin_handles_async_methods(fresh_collector):
    """Async methods are detected via inspect.iscoroutinefunction and
    get an async wrapper.  Same self-heal push behavior, awaitable."""
    from hartos.exception_collector import AutoReportSubsystemFailures

    class FakeAsyncBackend(AutoReportSubsystemFailures):
        SUBSYSTEM = 'llm'
        AUTO_REPORTED_METHODS = ('load',)
        name = 'fake_async'

        async def load(self):
            raise ConnectionError('llama-server unreachable')

    inst = FakeAsyncBackend()
    with pytest.raises(ConnectionError):
        await inst.load()

    rec = fresh_collector.get_unresolved()[0]
    assert rec.module == 'llm.fake_async'
    assert rec.function == 'load'


def test_mixin_wrap_is_idempotent(fresh_collector):
    """Re-running __init_subclass__ (e.g. module reload in dev) MUST
    NOT stack wrappers — the sentinel attribute on the wrapper
    short-circuits double-wraps."""
    from hartos.exception_collector import (
        AutoReportSubsystemFailures, _AUTO_REPORT_WRAPPED_ATTR,
    )

    class FakeIdem(AutoReportSubsystemFailures):
        SUBSYSTEM = 'tool'
        AUTO_REPORTED_METHODS = ('register',)
        name = 'fake_idem'

        def register(self):
            raise ValueError('bad spec')

    method = FakeIdem.__dict__['register']
    assert getattr(method, _AUTO_REPORT_WRAPPED_ATTR, False) is True
    # Manual re-invoke of __init_subclass__ — must be a no-op.
    AutoReportSubsystemFailures.__init_subclass__.__func__(FakeIdem)
    method_after = FakeIdem.__dict__['register']
    assert method_after is method  # same object, not re-wrapped


def test_mixin_success_path_no_record(fresh_collector):
    """When wrapped method returns normally, the collector sees
    NOTHING — wrap is transparent on the happy path."""
    from hartos.exception_collector import AutoReportSubsystemFailures

    class FakeOK(AutoReportSubsystemFailures):
        SUBSYSTEM = 'daemon'
        AUTO_REPORTED_METHODS = ('tick',)
        name = 'fake_daemon'

        def tick(self):
            return 'ok'

    inst = FakeOK()
    assert inst.tick() == 'ok'
    assert fresh_collector.get_unresolved() == []


def test_mixin_intermediate_base_without_subsystem_passes_through(
        fresh_collector):
    """An intermediate base class that inherits the mixin but does
    NOT set SUBSYSTEM (e.g. an abstract base under the mixin)
    must NOT trigger wrap until a concrete subclass sets it.
    Prevents the mixin from accidentally wrapping abstract methods
    on the intermediate."""
    from hartos.exception_collector import AutoReportSubsystemFailures

    class IntermediateAbstract(AutoReportSubsystemFailures):
        # SUBSYSTEM intentionally left empty — this is an abstract layer
        AUTO_REPORTED_METHODS = ('do_thing',)

        def do_thing(self):
            raise RuntimeError('should never run on this abstract')

    class ConcreteLeaf(IntermediateAbstract):
        SUBSYSTEM = 'tool'
        AUTO_REPORTED_METHODS = ('do_thing',)
        name = 'concrete_leaf'

        def do_thing(self):
            raise RuntimeError('leaf failure')

    leaf = ConcreteLeaf()
    with pytest.raises(RuntimeError, match='leaf failure'):
        leaf.do_thing()

    rec = fresh_collector.get_unresolved()[0]
    assert rec.module == 'tool.concrete_leaf'


def test_mixin_identifier_fallback_to_class_name(fresh_collector):
    """A class without ``name`` attribute falls back to the class
    name as identifier — keeps the helper safe even when subclasses
    forget the convention."""
    from hartos.exception_collector import AutoReportSubsystemFailures

    class FakeNoName(AutoReportSubsystemFailures):
        SUBSYSTEM = 'daemon'
        AUTO_REPORTED_METHODS = ('boot',)

        # NO `name` attribute set

        def boot(self):
            # Use Exception (not BaseException-only like SystemExit)
            # so the mixin's `except Exception` catches it.  Catching
            # BaseException in the wrap would mask KeyboardInterrupt
            # — that's design intent.
            raise RuntimeError('died')

    inst = FakeNoName()
    with pytest.raises(RuntimeError):
        inst.boot()

    rec = fresh_collector.get_unresolved()[0]
    assert rec.module == 'daemon.FakeNoName'
