"""A channel this build cannot run names what is missing and how to get it.

rv_chan follow-up 1 (Medium) on 35d14f82e / 144d2f043, 2026-11-23: on a node
whose build carries no botbuilder (the 11-25 desktop build, which holds
Nunba ef8bd4b7 out), the owner's answer to the Teams card was refused with
"register_channel returned False (see server log)".  The reason, the
TeamsAdapter's "botbuilder not installed", was only in the server log.
0e841af43 told the owner "a component it needs is missing from this build".

rv_mc's review of 0e841af43 (2026-11-23): a refusal hints at its fix (the
owner's rule), so it names the component in plain words and how it gets
here: on a desktop it arrives with a later app update, on a server HARTOS is
installed with its dependencies.  The card's answer is not invited again
when no answer can start the channel.  Every place a channel is started
says it: the register_channel tool, the card's answer, the connect form
(POST /bindings), the login probe and the admin activate.  And a reason is
the reason of the build its reader just ran: another thread's build (boot
restore) neither clears it nor replaces it.

Each test runs the real _wire_live_adapter, register_channel, _build_adapter
and teams_adapter module, imported afresh with botbuilder's import made to
fail.  Stand-ins only for the boundaries: the channel loop, the adapter
registry, whether this node is a desktop (core.config_cache.is_bundled) and,
for the answer, the admin config store and the binding DB.
"""
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Where the social DB lives is decided once, at this import: before any test
# here says this node is a desktop.
import integrations.social.models  # noqa: F401,E402

_BOTBUILDER = ('botbuilder', 'botbuilder.core', 'botbuilder.schema',
               'botbuilder.core.teams')
_TEAMS_MODULE = 'integrations.channels.extensions.teams_adapter'
_CONNECTOR = 'the Microsoft Teams connector (botbuilder)'
_ON_A_DESKTOP = 'it arrives with a later app update'
_ON_A_SERVER = 'install HARTOS with its dependencies to add it'
_MISSING = f'this build is missing {_CONNECTOR}; {_ON_A_DESKTOP}'
_NO_ANSWER_STARTS_IT = ('Answering this card again will not start it until '
                        'that component is installed.')


@pytest.fixture()
def no_botbuilder(monkeypatch):
    """This build without botbuilder: importing it fails, and the Teams
    adapter module is imported afresh, so its import guard sees that."""
    for name in _BOTBUILDER:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.delitem(sys.modules, _TEAMS_MODULE, raising=False)
    monkeypatch.delenv('TEAMS_APP_ID', raising=False)
    monkeypatch.delenv('TEAMS_APP_PASSWORD', raising=False)
    yield
    # The module imported here has HAS_TEAMS False: drop it, and monkeypatch
    # puts back whatever was imported before this test.
    sys.modules.pop(_TEAMS_MODULE, None)


@pytest.fixture()
def desktop(monkeypatch):
    """This node is a desktop (Nunba): its build is the app's."""
    monkeypatch.setattr('core.config_cache.is_bundled', lambda: True)


def _integration():
    """The real FlaskChannelIntegration (register_channel, _build_adapter),
    with a stand-in registry and a channel loop already running."""
    from integrations.channels.flask_integration import FlaskChannelIntegration
    integration = FlaskChannelIntegration.__new__(FlaskChannelIntegration)
    integration.registry = MagicMock()
    integration.registry.get.return_value = None
    integration.ensure_running = lambda timeout_s=5.0: (MagicMock(), False)
    return integration


def _use(monkeypatch, integration):
    monkeypatch.setattr(
        'integrations.channels.flask_integration.get_channel_integration',
        lambda: integration)


def _wire(monkeypatch, integration, channel, credential, extras=None):
    from integrations.social.api_channels import _wire_live_adapter
    _use(monkeypatch, integration)
    return _wire_live_adapter(channel, credential, extras)


class _FakeApi:
    def __init__(self, channels=None):
        self._channels = dict(channels or {})

    def _save_config(self):
        pass


def _owner_answers(monkeypatch, integration, values):
    """The owner's answer to the Teams card, through consent_api."""
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-1')
    monkeypatch.setattr('integrations.channels.admin.api.get_api',
                        lambda: _FakeApi())

    def _no_db():
        raise RuntimeError('no db in this test')
    monkeypatch.setattr('integrations.social.models.get_db', _no_db)
    _use(monkeypatch, integration)
    from integrations.social.consent_api import _start_channel_from_answer
    return _start_channel_from_answer('owner-1', 'teams', values)


@pytest.mark.parametrize('bundled,how', [(True, _ON_A_DESKTOP),
                                         (False, _ON_A_SERVER)],
                         ids=['desktop', 'server'])
def test_a_teams_start_without_botbuilder_names_the_connector_and_how_to_get_it(
        no_botbuilder, monkeypatch, bundled, how):
    monkeypatch.setattr('core.config_cache.is_bundled', lambda: bundled)
    integration = _integration()
    result = _wire(monkeypatch, integration, 'teams', 'pw-secret-1',
                   {'app_id': 'app-id-1'})
    assert result['success'] is False
    error = result['error']
    assert error == f'this build is missing {_CONNECTOR}; {how}', error
    # The install command stays in the log, for whoever makes the build.
    assert 'pip' not in error
    assert 'pw-secret-1' not in error and 'app-id-1' not in error
    integration.registry.register.assert_not_called()


def test_the_owner_reads_it_where_their_teams_answer_is_refused(
        no_botbuilder, desktop, monkeypatch):
    """The answer to the Teams card goes through consent_api's
    _start_channel_from_answer: its 409 names what is missing and how it
    arrives, and says that answering the card again will not start it."""
    refused = _owner_answers(monkeypatch, _integration(),
                             {'app_id': 'app-id-1', 'app_password': 'pw-secret-1'})
    assert refused is not None
    message, status = refused
    assert status == 409
    assert _MISSING in message, message
    assert message.endswith(_NO_ANSWER_STARTS_IT), message
    assert 'returned False' not in message and 'server log' not in message
    assert 'pw-secret-1' not in message


def test_a_refusal_an_answer_may_fix_does_not_say_no_answer_will(
        no_botbuilder, desktop, monkeypatch):
    """Neighbour: when the adapter builds and the start is refused for a
    reason the build does not give, the card is not said to be unanswerable."""
    import importlib
    teams = importlib.import_module(_TEAMS_MODULE)
    monkeypatch.setattr(teams, 'create_teams_adapter',
                        MagicMock(return_value=SimpleNamespace(name='teams')))
    integration = _integration()
    integration.registry.register.side_effect = RuntimeError('registry full')
    refused = _owner_answers(monkeypatch, integration,
                             {'app_id': 'app-id-1', 'app_password': 'pw-secret-1'})
    assert refused is not None
    message, status = refused
    assert status == 409
    assert _NO_ANSWER_STARTS_IT not in message, message
    assert _CONNECTOR not in message


def test_a_refusal_the_build_does_not_explain_still_points_at_the_log(
        no_botbuilder, desktop, monkeypatch):
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
    assert _CONNECTOR not in second['error']
    assert second['error'] == 'register_channel returned False (see server log)'


def test_the_connect_form_says_it_too(no_botbuilder, desktop, monkeypatch):
    """The setup form posts every field to POST /api/social/channels/bindings.
    That route keeps one credential per channel, so Teams' app id went to
    the start as its password, the password was dropped, and the start was
    refused as "register_channel returned False".  Teams' values now go to
    the register_channel tool, as a mailbox's do, and the form reads why."""
    from flask import Flask
    from integrations.social.api import social_bp
    from integrations.social.api_channels import channel_user_bp
    from integrations.social.models import (
        Base, UserChannelBinding, db_session, get_engine)
    from integrations.social.rate_limiter import get_limiter
    Base.metadata.create_all(get_engine())
    monkeypatch.setattr('integrations.channels.admin.api.get_api',
                        lambda: _FakeApi())
    monkeypatch.setattr('integrations.channels.agent_tools._report_when_live',
                        MagicMock(return_value=True))
    integration = _integration()
    _use(monkeypatch, integration)
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(social_bp)
    app.register_blueprint(channel_user_bp)
    get_limiter()._buckets.clear()
    client = app.test_client()
    reg = client.post('/api/social/auth/register', json={
        'username': 'teams_form_owner', 'password': 'testpass123',
        'display_name': 'Teams Form Owner'}).get_json()['data']
    try:
        resp = client.post(
            '/api/social/channels/bindings',
            headers={'Authorization': f"Bearer {reg['api_token']}"},
            json={'channel_type': 'teams', 'app_id': 'app-id-1',
                  'app_password': 'pw-secret-1'})
        body = resp.get_json()
        assert body['success'] is False, body
        error = body['error']
        assert _MISSING in error, error
        assert 'returned False' not in error and 'server log' not in error
        assert 'pw-secret-1' not in error
        integration.registry.register.assert_not_called()
    finally:
        with db_session() as db:
            db.query(UserChannelBinding).filter_by(
                user_id=str(reg['id'])).delete()


def test_a_login_probe_says_it_too(no_botbuilder, desktop):
    """probe_login builds the adapter the same way: its refusal is the
    build's reason, not "cannot be used to connect (see the server log)"."""
    ok, why, refused, answered = _integration().probe_login(
        'teams', 'pw-1', app_id='a-1')
    assert (ok, refused, answered) == (False, False, False)
    assert why == _MISSING, why


def test_the_admin_activate_says_it_too(no_botbuilder, desktop, monkeypatch):
    """POST /api/admin/channels/teams/activate registers from the saved
    entry (its app id from the environment): a refused build is reported
    with its reason beside activated False."""
    from flask import Flask
    from integrations.channels.admin.api import activate_channel
    monkeypatch.setenv('TEAMS_APP_ID', 'a-1')
    monkeypatch.setattr(
        'integrations.channels.admin.api.get_api',
        lambda: _FakeApi({'teams': {'enabled': True, 'token': 'pw-1'}}))
    _use(monkeypatch, _integration())
    with Flask(__name__).test_request_context(method='POST'):
        data = activate_channel('teams').get_json()['data']
    assert data['activated'] is False
    assert data.get('error') == _MISSING, data


def test_another_threads_build_neither_clears_nor_replaces_this_ones_reason(
        no_botbuilder, desktop):
    """Boot restore builds channels on its own thread.  A reason belongs to
    the build its reader just ran: another thread's build of the same channel
    (here one refused for a missing setting, with no reason) between this
    build and its read leaves this thread's reason as it was, and is told
    nothing of it."""
    integration = _integration()
    assert integration.register_channel('teams', token='pw-1',
                                        app_id='a-1') is False
    seen = {}

    def _restore():
        seen['built'] = integration._build_adapter('teams', token='pw-1')
        seen['reason'] = integration.build_refusal('teams')
    other = threading.Thread(target=_restore)
    other.start()
    other.join(timeout=30)
    assert seen == {'built': None, 'reason': None}
    assert integration.build_refusal('teams') == _MISSING


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
