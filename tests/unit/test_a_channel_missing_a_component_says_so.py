"""A channel this build cannot run says so, not "register_channel returned False".

rv_chan follow-up 1 (Medium) on 35d14f82e / 144d2f043, 2026-11-23: on a node
whose build carries no botbuilder (the 11-25 desktop build, which holds
Nunba ef8bd4b7 out), the owner's answer to the Teams card was refused with
"register_channel returned False (see server log)".  The reason, the
TeamsAdapter's "botbuilder not installed", was only in the server log.

_build_adapter now keeps a reason the owner can use beside
register_channel's bool (FlaskChannelIntegration.build_refusal), and
_wire_live_adapter refuses with it: this node can't run Microsoft Teams yet,
because a component it needs is missing from this build.

Each test runs the real _wire_live_adapter, register_channel, _build_adapter
and teams_adapter module, imported afresh with botbuilder's import made to
fail.  Stand-ins only for the boundaries: the channel loop, the adapter
registry, and (for the answer) the admin config store and the binding DB.
"""
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_BOTBUILDER = ('botbuilder', 'botbuilder.core', 'botbuilder.schema',
               'botbuilder.core.teams')
_TEAMS_MODULE = 'integrations.channels.extensions.teams_adapter'
_MISSING = ("this node can't run Microsoft Teams yet, because a component it "
            "needs is missing from this build")


@pytest.fixture()
def no_botbuilder(monkeypatch):
    """This build without botbuilder: importing it fails, and the Teams
    adapter module is imported afresh, so its import guard sees that."""
    for name in _BOTBUILDER:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.delitem(sys.modules, _TEAMS_MODULE, raising=False)
    yield
    # The module imported here has HAS_TEAMS False: drop it, and monkeypatch
    # puts back whatever was imported before this test.
    sys.modules.pop(_TEAMS_MODULE, None)


def _integration():
    """The real FlaskChannelIntegration (register_channel, _build_adapter),
    with a stand-in registry and a channel loop already running."""
    from integrations.channels.flask_integration import FlaskChannelIntegration
    integration = FlaskChannelIntegration.__new__(FlaskChannelIntegration)
    integration.registry = MagicMock()
    integration.registry.get.return_value = None
    integration.ensure_running = lambda timeout_s=5.0: (MagicMock(), False)
    return integration


def _wire(monkeypatch, integration, channel, credential, extras=None):
    from integrations.social.api_channels import _wire_live_adapter
    monkeypatch.setattr(
        'integrations.channels.flask_integration.get_channel_integration',
        lambda: integration)
    return _wire_live_adapter(channel, credential, extras)


class _FakeApi:
    def __init__(self):
        self._channels = {}

    def _save_config(self):
        pass


def test_a_teams_start_without_botbuilder_says_a_component_is_missing(
        no_botbuilder, monkeypatch):
    integration = _integration()
    result = _wire(monkeypatch, integration, 'teams', 'pw-secret-1',
                   {'app_id': 'app-id-1'})
    assert result['success'] is False
    error = result['error']
    assert _MISSING in error, error
    assert 'returned False' not in error and 'server log' not in error
    # Nothing a person can't act on: no install command, no module name.
    assert 'pip' not in error and 'botbuilder' not in error
    assert 'pw-secret-1' not in error and 'app-id-1' not in error
    integration.registry.register.assert_not_called()


def test_the_owner_reads_it_where_their_teams_answer_is_refused(
        no_botbuilder, monkeypatch):
    """The answer to the Teams card goes through consent_api's
    _start_channel_from_answer: its 409 says what the owner can't fix by
    typing again."""
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-1')
    monkeypatch.setattr('integrations.channels.admin.api.get_api',
                        lambda: _FakeApi())

    def _no_db():
        raise RuntimeError('no db in this test')
    monkeypatch.setattr('integrations.social.models.get_db', _no_db)
    integration = _integration()
    monkeypatch.setattr(
        'integrations.channels.flask_integration.get_channel_integration',
        lambda: integration)
    from integrations.social.consent_api import _start_channel_from_answer
    refused = _start_channel_from_answer(
        'owner-1', 'teams', {'app_id': 'app-id-1', 'app_password': 'pw-secret-1'})
    assert refused is not None
    message, status = refused
    assert status == 409
    assert _MISSING in message, message
    assert 'returned False' not in message and 'server log' not in message
    assert 'pw-secret-1' not in message


def test_a_refusal_the_build_does_not_explain_still_points_at_the_log(
        no_botbuilder, monkeypatch):
    """The reason is this build's, not a remembered one: once the adapter
    builds, a refusal after it (the registry refusing it) is not told as a
    missing component."""
    integration = _integration()
    first = _wire(monkeypatch, integration, 'teams', 'pw-1', {'app_id': 'a-1'})
    assert _MISSING in first['error']

    teams = sys.modules[_TEAMS_MODULE]
    monkeypatch.setattr(teams, 'create_teams_adapter',
                        MagicMock(return_value=SimpleNamespace(name='teams')))
    integration.registry.register.side_effect = RuntimeError('registry full')
    second = _wire(monkeypatch, integration, 'teams', 'pw-1', {'app_id': 'a-1'})
    assert second['success'] is False
    assert _MISSING not in second['error']
    assert second['error'] == 'register_channel returned False (see server log)'


def test_a_channel_whose_component_is_here_is_built_and_started(
        no_botbuilder, monkeypatch):
    """Neighbour: with the adapter built, nothing is refused and the start
    is scheduled."""
    import importlib
    teams = importlib.import_module(_TEAMS_MODULE)
    monkeypatch.setattr(teams, 'create_teams_adapter',
                        MagicMock(return_value=SimpleNamespace(name='teams')))
    integration = _integration()
    adapter = MagicMock()
    integration.registry.get.side_effect = [None, adapter]
    started = []
    monkeypatch.setattr('asyncio.run_coroutine_threadsafe',
                        lambda coro, loop: started.append(coro))
    result = _wire(monkeypatch, integration, 'teams', 'pw-1', {'app_id': 'a-1'})
    assert result['success'] is True, result
    integration.registry.register.assert_called_once()
    assert len(started) == 1
