"""A LiveKit that cannot be used says why, wherever a call needed it -- and
only where a call needed it.

Measured 2026-10-08 (review of Nunba 15139f15): inside Nunba.exe a traced,
partial protobuf in lib/ shadowed python-embed's complete one, so
``from livekit import api, rtc`` raised "ImportError: cannot import name
'timestamp_pb2' from 'google.protobuf' (...\\lib\\google\\protobuf\\
__init__.pyc)".  HARTOS swallowed the error, and everything a call reported
said LiveKit was "not installed" (with "pip install livekit-api" as the fix)
while it was installed.  An agent in the call was silent and deaf, and the
log pointed at the wrong cause.

These tests load the real modules against a livekit that fails to import
the same way, and read what a call reports: the token result and recording
results (which reach remote callers, so they name the failure's kind and no
path on this machine), and the warnings when a room cannot start, a reply
cannot be voiced, or an agent cannot hear (which carry the whole error).  A
deploy that hosts no LiveKit rooms (central, LIVEKIT_DISABLE) blames no
LiveKit import: its calls have no room to voice into either way.
"""
import importlib.util
import logging
import os
import types

import pytest

from tests.unit.module_swap import swap_modules

_PATH = r'C:\Program Files (x86)\HevolveAI\Nunba\lib\google\protobuf\__init__.pyc'
_ERR = f"cannot import name 'timestamp_pb2' from 'google.protobuf' ({_PATH})"
_SOCIAL = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'integrations', 'social'))


def _failing_livekit():
    """A livekit package whose subpackages fail as they did in Nunba.exe."""
    mod = types.ModuleType('livekit')
    mod.__path__ = []

    def __getattr__(name):
        raise ImportError(_ERR)

    mod.__getattr__ = __getattr__
    return mod


def _livekit_with_api(api):
    mod = types.ModuleType('livekit')
    mod.__path__ = []
    mod.api = api
    return mod


def _load(filename, livekit):
    """A fresh copy of integrations/social/<filename>, imported while
    ``livekit`` stands in for the real package.  The module everyone else
    imported is left as it was."""
    name = 'integrations.social.' + filename[:-3]
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_SOCIAL, filename))
    mod = importlib.util.module_from_spec(spec)
    with swap_modules({'livekit': livekit}):
        spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _no_real_livekit_home(monkeypatch, tmp_path):
    """Nothing here may write LiveKit dev keys into the real ~/.hevolve."""
    monkeypatch.setenv('HEVOLVE_HOME', str(tmp_path))


@pytest.fixture
def records():
    """Every record the social logger emits during the test, whatever the
    logger's propagation and level are set to elsewhere."""
    seen = []

    class _Keep(logging.Handler):
        def emit(self, record):
            seen.append(record)

    logger = logging.getLogger('hevolve_social')
    handler = _Keep(level=logging.DEBUG)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield seen
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def _warnings_naming(records, text):
    return [r for r in records
            if r.levelno >= logging.WARNING and text in r.getMessage()]


def _warnings(records):
    return [r for r in records if r.levelno >= logging.WARNING]


def _configured(svc, monkeypatch):
    monkeypatch.setattr(svc, '_resolved_config',
                        lambda: ('ws://127.0.0.1:7880', 'devkey', 's' * 32))


@pytest.fixture
def hosts_rooms(monkeypatch):
    """A flat desktop: it supervises its own LiveKit, so its calls have rooms."""
    monkeypatch.delenv('LIVEKIT_DISABLE', raising=False)
    monkeypatch.setenv('LIVEKIT_AUTOSTART', '1')


@pytest.fixture
def hosts_no_rooms(monkeypatch):
    """Central: LIVEKIT_DISABLE, no operator LiveKit -- calls are p2p mesh."""
    for name in ('LIVEKIT_URL', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET',
                 'LIVEKIT_AUTOSTART', 'HEVOLVE_DEPLOY_MODE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('LIVEKIT_DISABLE', '1')


class TestATokenThatCannotBeSigned:

    def test_names_the_import_failure_and_logs_the_whole_error(
            self, monkeypatch, records):
        svc = _load('livekit_service.py', _failing_livekit())
        _configured(svc, monkeypatch)
        r = svc.LiveKitService.issue_token('call-1', 'user-1')
        assert r['mode'] == 'livekit_pending'
        assert r['reason'] == 'livekit-api did not import (ImportError)'
        assert _warnings_naming(records, _ERR), \
            'a call that gets no room token must be logged with its cause'

    def test_a_token_reason_names_no_path_on_this_machine(self, monkeypatch):
        """The reason goes to whoever asked for the token -- on a regional
        host, a remote caller."""
        svc = _load('livekit_service.py', _failing_livekit())
        _configured(svc, monkeypatch)
        reason = svc.LiveKitService.issue_token('call-1', 'user-1')['reason']
        assert _PATH not in reason and 'pip install' not in reason

    def test_names_the_signing_failure_when_the_sdk_imported(
            self, monkeypatch, records):
        class _Token:
            def __init__(self, key, secret):
                pass

            def __getattr__(self, name):
                return lambda *a, **k: self

            def to_jwt(self):
                raise ValueError('secret is too short for HS256')

        api = types.ModuleType('livekit.api')
        api.VideoGrants = lambda **k: types.SimpleNamespace(**k)
        api.AccessToken = _Token
        svc = _load('livekit_service.py', _livekit_with_api(api))
        _configured(svc, monkeypatch)
        r = svc.LiveKitService.issue_token('call-1', 'user-1')
        assert r['mode'] == 'livekit_pending'
        assert r['reason'] == 'token signing failed (ValueError)'
        assert _warnings_naming(records, 'secret is too short for HS256')


class TestARecordingThatCannotStart:

    def test_start_and_stop_name_the_import_failure(self, monkeypatch, records):
        svc = _load('livekit_service.py', _failing_livekit())
        _configured(svc, monkeypatch)
        started = svc.LiveKitService.start_recording('call-1')
        stopped = svc.LiveKitService.stop_recording('egress-1')
        for result in (started, stopped):
            assert result['ok'] is False
            assert result['reason'] == 'livekit-api did not import (ImportError)'
        assert len(_warnings_naming(records, _ERR)) == 2


class TestTheRealtimeSdkCannotBeUsed:

    def test_a_room_that_cannot_start_says_why(self, records):
        room = _load('_livekit_room.py', _failing_livekit())

        class _Room(room._LiveKitRoomThread):
            async def _async_main(self):
                return None

        assert _Room('call-1', 'ws://127.0.0.1:7880', 'tok').start() is False
        assert _warnings_naming(records, _ERR)

    def test_a_reply_that_cannot_be_voiced_says_why(
            self, monkeypatch, records, hosts_rooms):
        from integrations.social import agent_voice_bridge as avb
        monkeypatch.setattr(avb, '_HAS_LIVEKIT_RTC', False)
        monkeypatch.setattr(avb, '_livekit_room',
                            _load('_livekit_room.py', _failing_livekit()),
                            raising=False)
        worker = avb.AgentBridgeWorker('call-1', 'agent-1', 'owner-1', {})
        worker._publish_audio_for('Here is your answer.')
        assert _warnings_naming(records, _ERR)

    def test_an_agent_that_cannot_hear_the_call_says_why(
            self, monkeypatch, records, hosts_rooms):
        from integrations.social import agent_voice_bridge as avb
        monkeypatch.setattr(avb, '_HAS_LIVEKIT_RTC', False)
        monkeypatch.setattr(avb, '_livekit_room',
                            _load('_livekit_room.py', _failing_livekit()),
                            raising=False)
        avb._ensure_call_subscriber('call-1')
        assert _warnings_naming(records, _ERR)


class TestADeployWithNoRooms:
    """Central / LIVEKIT_DISABLE: a call is p2p mesh, there is no room to
    voice into or hear from, so a missing rtc is not the cause of anything."""

    def test_a_reply_is_logged_as_unvoiced_without_blaming_livekit(
            self, monkeypatch, records, hosts_no_rooms):
        from integrations.social import agent_voice_bridge as avb
        monkeypatch.setattr(avb, '_HAS_LIVEKIT_RTC', False)
        monkeypatch.setattr(avb, '_livekit_room',
                            _load('_livekit_room.py', _failing_livekit()),
                            raising=False)
        avb.AgentBridgeWorker('call-1', 'agent-1', 'owner-1', {}
                              )._publish_audio_for('Here is your answer.')
        assert _warnings(records) == []
        assert any('no LiveKit room' in r.getMessage() for r in records)

    def test_attaching_an_agent_warns_nothing(
            self, monkeypatch, records, hosts_no_rooms):
        from integrations.social import agent_voice_bridge as avb
        monkeypatch.setattr(avb, '_HAS_LIVEKIT_RTC', False)
        monkeypatch.setattr(avb, '_livekit_room',
                            _load('_livekit_room.py', _failing_livekit()),
                            raising=False)
        avb._ensure_call_subscriber('call-1')
        assert _warnings(records) == []


class TestARoomWithNoToken:
    """rtc works, but the room token could not be made (livekit-api broken,
    signing failed): the reply is unvoiced because of that, and says so --
    not "p2p/central"."""

    @pytest.fixture
    def pending(self, monkeypatch, hosts_rooms):
        from integrations.social import agent_voice_bridge as avb
        from integrations.social.livekit_service import LiveKitService
        monkeypatch.setattr(avb, '_HAS_LIVEKIT_RTC', True)
        monkeypatch.setattr(LiveKitService, 'issue_token', staticmethod(
            lambda *a, **k: {'mode': 'livekit_pending', 'url': 'ws://127.0.0.1:7880',
                             'token': '',
                             'reason': 'livekit-api did not import (ImportError)'}))
        return avb

    def test_the_reply_names_the_tokens_reason(self, pending, records):
        pending.AgentBridgeWorker('call-1', 'agent-1', 'owner-1', {}
                                  )._publish_audio_for('Here is your answer.')
        assert _warnings_naming(records, 'livekit-api did not import (ImportError)')
        assert not any('p2p/central' in r.getMessage() for r in records)

    def test_the_agent_that_cannot_hear_names_it_too(self, pending, records):
        pending._ensure_call_subscriber('call-1')
        assert _warnings_naming(records, 'livekit-api did not import (ImportError)')
