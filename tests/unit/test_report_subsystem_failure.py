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
    from exception_collector import ExceptionCollector
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
    from exception_collector import report_subsystem_failure

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
    from exception_collector import report_subsystem_failure

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
    from exception_collector import report_subsystem_failure

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
    from exception_collector import report_subsystem_failure

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
    from exception_collector import report_subsystem_failure

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
    from exception_collector import report_subsystem_failure, ExceptionCollector

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
    from exception_collector import report_subsystem_failure

    try:
        raise RuntimeError('test')
    except RuntimeError as e:
        report_subsystem_failure(subsystem, identifier, e, 'op')

    rec = fresh_collector.get_unresolved()[0]
    assert rec.module == expected_module
