"""Owner rule: no silent failures, and a fallback says it is one, and why.

Each test drives a REAL function into its failure or fallback path and asserts
two things: the observable outcome (what the caller gets) and a WARNING that
names what was lost.  Before this change each of these paths either returned
quietly or logged at DEBUG/INFO, which production logging does not show.
"""
import asyncio
import logging
import sys
import types

import pytest

from integrations.channels.base import (
    ChannelAdapter, ChannelConfig, Message, SendResult)
from integrations.channels.flask_integration import FlaskChannelIntegration


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def _integration(loop=None):
    fi = FlaskChannelIntegration.__new__(FlaskChannelIntegration)
    fi.registry = types.SimpleNamespace()
    fi._loop = loop
    fi._thread = None
    return fi


# ── The channel loop owner ───────────────────────────────────────────────

def test_send_without_a_loop_warns_with_the_target(caplog):
    caplog.set_level(logging.WARNING)
    assert _integration().send_threadsafe('discord', 'c42', 'hi') is None
    assert any('discord/c42' in w and 'NOT delivered' in w for w in _warnings(caplog))


def test_loop_that_never_starts_warns(caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    fi = _integration()
    monkeypatch.setattr(fi, 'start', lambda: None)
    assert fi.ensure_running(timeout_s=0.2) == (None, True)
    assert any('did not come up' in w for w in _warnings(caplog))


def test_router_reply_without_a_loop_warns(caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    from integrations.channels import flask_integration
    from integrations.channels.response.router import ChannelResponseRouter
    monkeypatch.setattr(flask_integration, 'get_channel_integration',
                        lambda: _integration())
    assert ChannelResponseRouter().deliver_to_chat('slack', 'C1', 'x') is False
    assert any('NOT delivered' in w and 'slack' in w for w in _warnings(caplog))


# ── The registry's reply fallbacks ───────────────────────────────────────

class _TextOnlyAdapter(ChannelAdapter):
    """An adapter predating the media kwarg."""

    def __init__(self):
        super().__init__(ChannelConfig(token='t'))
        self.sent = []

    @property
    def name(self):
        return 'discord'

    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send_message(self, chat_id, text, reply_to=None):
        self.sent.append(text)
        return SendResult(success=True, message_id='1')

    async def edit_message(self, *a, **k):
        return SendResult(success=True)

    async def delete_message(self, *a, **k):
        return True

    async def send_typing(self, chat_id):
        return None

    async def get_chat_info(self, chat_id):
        return {}


def test_media_fallback_says_the_attachment_was_dropped(caplog, tmp_path, monkeypatch):
    caplog.set_level(logging.WARNING)
    from integrations.channels import registry as reg_mod
    monkeypatch.setattr(reg_mod, 'extract_media_markers',
                        lambda text: ('the text', ['an-attachment']))
    registry = reg_mod.ChannelRegistry()
    adapter = _TextOnlyAdapter()
    registry.register(adapter)
    registry.set_agent_handler(lambda message: 'reply [[MEDIA:x]]')

    async def _run():
        await adapter.start()
        await registry._route_to_agent(Message(
            id='m1', channel='discord', sender_id='s1', chat_id='c1', text='hi'))

    asyncio.run(_run())
    assert adapter.sent == ['the text']
    assert any('1 attachment(s) dropped' in w for w in _warnings(caplog))


# ── Configuration fallbacks ──────────────────────────────────────────────

@pytest.mark.parametrize('raw', ['abc', '0', '-5'])
def test_bad_agent_timeout_falls_back_loudly(caplog, monkeypatch, raw):
    caplog.set_level(logging.WARNING)
    from integrations.channels.chat_contract import (
        agent_turn_timeout, DEFAULT_AGENT_TURN_TIMEOUT_S)
    monkeypatch.setenv('HEVOLVE_CHANNEL_AGENT_TIMEOUT', raw)
    assert agent_turn_timeout() == DEFAULT_AGENT_TURN_TIMEOUT_S
    assert any('HEVOLVE_CHANNEL_AGENT_TIMEOUT' in w for w in _warnings(caplog))


def test_good_agent_timeout_is_used_quietly(caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    from integrations.channels.chat_contract import agent_turn_timeout
    monkeypatch.setenv('HEVOLVE_CHANNEL_AGENT_TIMEOUT', '600')
    assert agent_turn_timeout() == 600
    assert _warnings(caplog) == []


# ── The heartbeat sleep primitive ────────────────────────────────────────

def test_a_failing_heartbeat_warns_once_and_the_sleep_survives(caplog):
    caplog.set_level(logging.WARNING)
    from security.node_watchdog import sleep_with_heartbeat

    class _BrokenWatchdog:
        calls = 0

        def heartbeat(self, name):
            self.calls += 1
            raise RuntimeError('registry gone')

    wd = _BrokenWatchdog()
    sleep_with_heartbeat('probe', 0.05, chunk_seconds=0.01, watchdog=wd)
    assert wd.calls >= 3          # it kept sleeping and kept trying
    beats = [w for w in _warnings(caplog) if 'heartbeat failed' in w]
    assert len(beats) == 1 and 'probe' in beats[0]


def test_daemon_sleep_fallback_warns(caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    import threading
    from integrations.coding_agent.coding_daemon import CodingAgentDaemon
    monkeypatch.setitem(sys.modules, 'security.node_watchdog', None)
    daemon = CodingAgentDaemon.__new__(CodingAgentDaemon)
    daemon._stop_event = threading.Event()
    daemon._running = True
    daemon._wd_sleep(0.01)
    assert any('coding_daemon' in w and 'plain sleep' in w for w in _warnings(caplog))


# ── The speculative expert ───────────────────────────────────────────────

def _dispatcher():
    from integrations.agent_engine.speculative_dispatcher import SpeculativeDispatcher
    return SpeculativeDispatcher(model_registry=types.SimpleNamespace())


def test_a_delegated_draft_with_no_prepared_expert_warns(caplog):
    caplog.set_level(logging.WARNING)
    result = {'speculation_id': 'spec-1', 'delegate': 'local'}
    assert _dispatcher().schedule_expert_for_draft(result) is False
    assert any('spec-1' in w and 'no prepared expert' in w for w in _warnings(caplog))


def test_a_draft_that_did_not_delegate_stays_quiet(caplog):
    caplog.set_level(logging.WARNING)
    result = {'speculation_id': 'spec-2', 'delegate': 'none'}
    assert _dispatcher().schedule_expert_for_draft(result) is False
    assert _warnings(caplog) == []


def test_no_expert_model_warns(caplog):
    caplog.set_level(logging.WARNING)
    d = _dispatcher()
    d._remember_pending_expert('spec-3', {'expert_model': None, 'delegate': 'hive'})
    assert d.schedule_expert_for_draft({'speculation_id': 'spec-3'}) is False
    assert any('spec-3' in w and 'no expert model' in w for w in _warnings(caplog))


# ── STT model fallback ───────────────────────────────────────────────────

def test_stt_fallback_to_a_cached_model_warns(caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    import integrations.service_tools.whisper_tool as wt
    monkeypatch.setitem(sys.modules, 'sherpa_onnx', types.ModuleType('sherpa_onnx'))
    big, small = list(wt._SHERPA_MODELS)[:2]
    monkeypatch.setitem(wt._CATALOG_ID_TO_SHERPA, 'stt-t-big', big)
    monkeypatch.setitem(wt._CATALOG_ID_TO_SHERPA, 'stt-t-small', small)
    ranking = ['stt-t-big', 'stt-t-small']
    monkeypatch.setattr(wt, '_catalog_stt_entry', lambda exclude=None: next(
        (types.SimpleNamespace(id=c) for c in ranking if c not in (exclude or ())),
        None))
    monkeypatch.setattr(wt, '_sherpa_model_cached', lambda key: key == small)
    # The uncached pick would start a REAL fetch into the user's model dir;
    # a run killed mid-extract left a truncated model there (2026-09-30).
    monkeypatch.setattr(wt, '_download_in_background', lambda name: False)

    assert wt.select_whisper_model() == small
    assert any(big in w and small in w and 'not downloaded' in w
               for w in _warnings(caplog))


# ── Inbound: an empty /chat reply is a failure, not "I processed it" ─────

def test_an_empty_chat_reply_is_not_passed_off_as_success(caplog):
    caplog.set_level(logging.WARNING)
    from unittest.mock import Mock, patch
    from tests.unit.test_channel_inbound_contract import _bare_integration, _msg
    fi = _bare_integration()
    with patch('integrations.channels.flask_integration.pooled_post',
               lambda *a, **k: Mock(status_code=200, json=lambda: {'agent_id': 'a1'})):
        reply = fi._handle_message(_msg())
    # '' is the registry's "agent produced nothing" signal: it warns and sends
    # the canonical failure sentence (test_declined_message_is_silent).
    assert reply == ''
    fi._response_router.route_response.assert_not_called()
    assert any('no reply text' in w and "['agent_id']" in w for w in _warnings(caplog))


@pytest.mark.parametrize('raw', ['', 'four', '0'])
def test_a_bad_worker_count_does_not_break_the_channel_registry_import(raw):
    """HEVOLVE_CHANNEL_AGENT_WORKERS is read at import; a bare int() made a
    junk or zero value take every channel adapter down with the module."""
    import os
    import subprocess
    env = dict(os.environ, HEVOLVE_CHANNEL_AGENT_WORKERS=raw)
    out = subprocess.run(
        [sys.executable, '-c',
         'import integrations.channels.registry as r; '
         'print(r._AGENT_HANDLER_POOL._max_workers)'],
        env=env, capture_output=True, text=True, timeout=120,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == '4'
