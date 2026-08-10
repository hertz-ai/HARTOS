"""
Behavioural tests for integrations/social/external_bot_bridge.py

This module is the UNTRUSTED webhook write-path for external bots
(SantaClaw / OpenClaw / communitybook). Two surfaces carry security weight:

  1. ExternalBotRegistry.register_bot — reachable from the unauthenticated
     /bots/register route. A username collision with a PRE-EXISTING account
     must NOT hand the caller that account's api_token (account-takeover).

  2. process_webhook + _handle_* — dispatches untrusted action dicts to
     Post / Comment / Vote / Follow services behind inline guards
     (value ∈ {1,-1}, required fields, platform allowlist). Guards must
     actually reject, and a single bad action must not abort the batch.

Boundary strategy:
  - register_bot tests use a REAL in-memory SQLite DB + the REAL services,
    because the takeover bug lives in register_bot's handling of the real
    ValueError that UserService.register_agent raises on a name collision.
  - process_webhook tests mock the Service classes (the DB boundary) and the
    realtime fan-out, to isolate the dispatch/guard logic under test.
"""
import os
import types

# In-memory StaticPool DB — MUST be set before integrations.social.models is
# imported (models.py resolves DB_URL at import time).
os.environ.setdefault('HEVOLVE_DB_URL', 'sqlite://')

import pytest
from unittest.mock import MagicMock, patch

try:
    import requests  # noqa: F401 - optional external dep; the tests mock it
    import sqlalchemy  # noqa: F401 - the social ORM's backend, needed by the SUT
except ImportError:
    pytest.skip("requests/sqlalchemy not installed in this env",
                allow_module_level=True)

# With BOTH external deps proven present, import the SUT + ORM UNCONDITIONALLY:
# an import-time regression in this unauthenticated write-path module (or the
# social ORM it guards) must now FAIL LOUD, never be masked as a green skip.
from integrations.social.external_bot_bridge import (
    ExternalBotRegistry,
    process_webhook,
    discover_santaclaw_agents,
    send_to_santaclaw,
    auto_register_discovered_agents,
    SUPPORTED_PLATFORMS,
)
from integrations.social.models import get_engine, get_db, Base, User
from integrations.social.services import UserService

BRIDGE = 'integrations.social.external_bot_bridge'


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture()
def db():
    """Fresh in-memory schema per test (drop+create for isolation)."""
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = get_db()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _make_bot_user(platform='generic'):
    """Lightweight stand-in for a registered bot User row."""
    return types.SimpleNamespace(
        id='bot-row-id',
        settings={'platform': platform},
        last_active_at=None,
    )


# ═════════════════════════════════════════════════════════════════════
# register_bot — platform allowlist + required fields
# ═════════════════════════════════════════════════════════════════════

class TestRegisterBotValidation:

    def test_unsupported_platform_rejected(self, db):
        with pytest.raises(ValueError) as ei:
            ExternalBotRegistry.register_bot(
                db, bot_id='b1', bot_name='swift.amber.falcon',
                platform='evilcorp')
        assert 'Unsupported platform' in str(ei.value)

    def test_every_supported_platform_accepted(self, db):
        # Each allowlisted platform should register without raising. Names must
        # be DISTINCT valid 3-word names (a collision would now raise post-fix).
        names = ['brave.jade.wolf', 'swift.amber.hawk', 'calm.ruby.owl',
                 'bold.teal.fox', 'keen.onyx.raven', 'wise.coral.sage']
        assert len(names) >= len(SUPPORTED_PLATFORMS)
        for i, plat in enumerate(SUPPORTED_PLATFORMS):
            user = ExternalBotRegistry.register_bot(
                db, bot_id=f'ok{i}', bot_name=names[i], platform=plat)
            assert user is not None
            assert user.settings['platform'] == plat

    def test_empty_bot_id_rejected(self, db):
        with pytest.raises(ValueError) as ei:
            ExternalBotRegistry.register_bot(
                db, bot_id='', bot_name='swift.amber.falcon', platform='generic')
        assert 'required' in str(ei.value)

    def test_empty_bot_name_rejected(self, db):
        with pytest.raises(ValueError) as ei:
            ExternalBotRegistry.register_bot(
                db, bot_id='b1', bot_name='', platform='generic')
        assert 'required' in str(ei.value)

    def test_platform_checked_before_required_fields(self, db):
        # Bad platform wins even when bot_id is also empty.
        with pytest.raises(ValueError) as ei:
            ExternalBotRegistry.register_bot(
                db, bot_id='', bot_name='', platform='nope')
        assert 'Unsupported platform' in str(ei.value)


# ═════════════════════════════════════════════════════════════════════
# register_bot — happy path + metadata
# ═════════════════════════════════════════════════════════════════════

class TestRegisterBotHappyPath:

    def test_fresh_registration_creates_user_with_own_token(self, db):
        user = ExternalBotRegistry.register_bot(
            db, bot_id='fresh1', bot_name='swift.amber.falcon',
            platform='santaclaw', description='a bot',
            capabilities=['chat'], callback_url='http://gw')
        assert user.username == 'swift.amber.falcon'
        assert user.api_token  # got a token
        assert user.agent_id == 'ext_santaclaw_fresh1'
        assert user.settings['platform'] == 'santaclaw'
        assert user.settings['bot_id'] == 'fresh1'
        assert user.settings['capabilities'] == ['chat']
        assert user.settings['callback_url'] == 'http://gw'
        assert user.display_name == 'swift.amber.falcon'

    def test_invalid_botname_gets_generated_username(self, db):
        # A non-3-word bot_name can't be used verbatim; register_bot must
        # generate a valid agent name and still register.
        user = ExternalBotRegistry.register_bot(
            db, bot_id='weird1', bot_name='Weird Bot!! 42',
            platform='generic')
        assert user is not None
        assert user.api_token
        assert user.agent_id == 'ext_generic_weird1'
        # metadata still captured
        assert user.settings['bot_name'] == 'Weird Bot!! 42'

    def test_idempotent_reregistration_same_bot_returns_same_row(self, db):
        # Same platform+bot_id+valid name → the collision is THIS bot, so the
        # same row (same token) is returned rather than a duplicate/error.
        first = ExternalBotRegistry.register_bot(
            db, bot_id='same1', bot_name='calm.jade.oracle', platform='openclaw')
        first_id, first_token = first.id, first.api_token
        db.flush()
        second = ExternalBotRegistry.register_bot(
            db, bot_id='same1', bot_name='calm.jade.oracle', platform='openclaw')
        assert second.id == first_id
        assert second.api_token == first_token


# ═════════════════════════════════════════════════════════════════════
# register_bot — ACCOUNT-TAKEOVER guard (the security bug)
# ═════════════════════════════════════════════════════════════════════

class TestRegisterBotTakeover:
    """A username collision with a DIFFERENT identity must never leak that
    identity's api_token back to the (unauthenticated) caller."""

    def test_collision_with_human_user_does_not_leak_token(self, db):
        # Pre-existing victim: a real account owning username swift.amber.falcon.
        victim = UserService.register_agent(
            db, 'swift.amber.falcon', 'victim', 'agent_victim_real')
        db.flush()
        victim_token = victim.api_token
        victim_id = victim.id

        # Attacker registers a bot whose bot_name maps to the victim's username.
        with pytest.raises(ValueError):
            ExternalBotRegistry.register_bot(
                db, bot_id='attacker', bot_name='swift.amber.falcon',
                platform='generic')

        # Victim row is untouched, and no token was handed out. (No rollback:
        # the victim was flushed but not committed — a rollback would wipe it.)
        still = db.query(User).filter(User.username == 'swift.amber.falcon').first()
        assert still is not None
        assert still.id == victim_id
        assert still.api_token == victim_token
        assert still.agent_id == 'agent_victim_real'

    def test_collision_with_different_bot_does_not_return_other_bot(self, db):
        # Bot A owns the name.
        a = ExternalBotRegistry.register_bot(
            db, bot_id='botA', bot_name='bold.crimson.storm', platform='generic')
        db.flush()
        a_id, a_token = a.id, a.api_token

        # Bot B (different bot_id → different agent_id) collides on the name.
        with pytest.raises(ValueError):
            ExternalBotRegistry.register_bot(
                db, bot_id='botB', bot_name='bold.crimson.storm',
                platform='generic')

        owner = db.query(User).filter(User.username == 'bold.crimson.storm').first()
        assert owner.id == a_id
        assert owner.api_token == a_token
        assert owner.agent_id == 'ext_generic_botA'


# ═════════════════════════════════════════════════════════════════════
# register_bot — lookup helpers
# ═════════════════════════════════════════════════════════════════════

class TestBotLookups:

    def test_get_bot_user_by_platform_and_id(self, db):
        ExternalBotRegistry.register_bot(
            db, bot_id='look1', bot_name='keen.ruby.hawk', platform='a2a')
        db.flush()
        found = ExternalBotRegistry.get_bot_user(db, 'look1', platform='a2a')
        assert found is not None
        assert found.agent_id == 'ext_a2a_look1'

    def test_get_bot_user_missing_returns_none(self, db):
        assert ExternalBotRegistry.get_bot_user(db, 'nope', platform='a2a') is None

    def test_list_external_bots_only_returns_ext_agents(self, db):
        ExternalBotRegistry.register_bot(
            db, bot_id='l1', bot_name='wise.onyx.raven', platform='generic')
        # A non-external plain user should NOT appear.
        UserService.register_agent(db, 'pure.teal.fox', 'human', 'agent_human_x')
        db.flush()
        bots = ExternalBotRegistry.list_external_bots(db)
        agent_ids = {b.agent_id for b in bots}
        assert 'ext_generic_l1' in agent_ids
        assert 'agent_human_x' not in agent_ids


# ═════════════════════════════════════════════════════════════════════
# process_webhook — dispatch + guards (services mocked at the boundary)
# ═════════════════════════════════════════════════════════════════════

class TestProcessWebhookDispatch:

    def test_unknown_action_type_error_result(self):
        bot = _make_bot_user()
        out = process_webhook(MagicMock(), bot, [{'type': 'demolish'}])
        assert out[0]['status'] == 'error'
        assert 'Unknown action: demolish' in out[0]['error']

    def test_missing_type_error_result(self):
        bot = _make_bot_user()
        out = process_webhook(MagicMock(), bot, [{}])
        assert out[0]['status'] == 'error'
        assert 'Unknown action: None' in out[0]['error']

    def test_last_active_at_updated(self):
        bot = _make_bot_user()
        assert bot.last_active_at is None
        process_webhook(MagicMock(), bot, [{'type': 'demolish'}])
        assert bot.last_active_at is not None

    def test_post_happy_path_dispatches_to_postservice(self):
        bot = _make_bot_user(platform='santaclaw')
        db = MagicMock()
        with patch(f'{BRIDGE}.PostService') as PS:
            PS.create.return_value = types.SimpleNamespace(id='post-1')
            out = process_webhook(db, bot, [{
                'type': 'post', 'title': 'Hello', 'content': 'world'}])
        assert out[0] == {'action': 'post', 'status': 'created', 'id': 'post-1'}
        # source_channel derives from bot platform
        _, kwargs = PS.create.call_args
        assert kwargs['source_channel'] == 'ext_santaclaw'

    def test_post_without_title_rejected(self):
        bot = _make_bot_user()
        with patch(f'{BRIDGE}.PostService') as PS:
            out = process_webhook(MagicMock(), bot, [{'type': 'post'}])
        assert out[0]['status'] == 'error'
        assert 'title is required' in out[0]['error']
        PS.create.assert_not_called()

    def test_post_duplicate_message_id_not_recreated(self):
        bot = _make_bot_user()
        db = MagicMock()
        # Existing post found for this (source_channel, source_message_id).
        db.query.return_value.filter.return_value.first.return_value = \
            types.SimpleNamespace(id='dup-1')
        with patch(f'{BRIDGE}.PostService') as PS:
            out = process_webhook(db, bot, [{
                'type': 'post', 'title': 'Hi', 'message_id': 'm-42'}])
        assert out[0] == {'action': 'post', 'status': 'duplicate', 'id': 'dup-1'}
        PS.create.assert_not_called()

    def test_comment_requires_post_id_and_content(self):
        bot = _make_bot_user()
        with patch(f'{BRIDGE}.PostService') as PS, \
                patch(f'{BRIDGE}.CommentService') as CS:
            out = process_webhook(MagicMock(), bot, [
                {'type': 'comment', 'content': 'hi'},          # no post_id
                {'type': 'comment', 'post_id': 'p1'},          # no content
            ])
        assert all(r['status'] == 'error' for r in out)
        assert all('required' in r['error'] for r in out)
        CS.create.assert_not_called()

    def test_comment_on_missing_post_errors(self):
        bot = _make_bot_user()
        with patch(f'{BRIDGE}.PostService') as PS, \
                patch(f'{BRIDGE}.CommentService') as CS:
            PS.get_by_id.return_value = None
            out = process_webhook(MagicMock(), bot, [{
                'type': 'comment', 'post_id': 'ghost', 'content': 'x'}])
        assert out[0]['status'] == 'error'
        assert 'not found' in out[0]['error']
        CS.create.assert_not_called()

    def test_comment_happy_path(self):
        bot = _make_bot_user()
        with patch(f'{BRIDGE}.PostService') as PS, \
                patch(f'{BRIDGE}.CommentService') as CS:
            PS.get_by_id.return_value = types.SimpleNamespace(id='p1')
            CS.create.return_value = types.SimpleNamespace(id='c9')
            out = process_webhook(MagicMock(), bot, [{
                'type': 'comment', 'post_id': 'p1', 'content': 'nice'}])
        assert out[0] == {'action': 'comment', 'status': 'created', 'id': 'c9'}

    def test_follow_requires_user_id(self):
        bot = _make_bot_user()
        with patch(f'{BRIDGE}.UserService') as US, \
                patch(f'{BRIDGE}.FollowService') as FS:
            out = process_webhook(MagicMock(), bot, [{'type': 'follow'}])
        assert out[0]['status'] == 'error'
        assert 'user_id is required' in out[0]['error']
        FS.follow.assert_not_called()

    def test_follow_missing_target_errors(self):
        bot = _make_bot_user()
        with patch(f'{BRIDGE}.UserService') as US, \
                patch(f'{BRIDGE}.FollowService') as FS:
            US.get_by_id.return_value = None
            US.get_by_username.return_value = None
            out = process_webhook(MagicMock(), bot, [{
                'type': 'follow', 'user_id': 'ghost'}])
        assert out[0]['status'] == 'error'
        assert 'not found' in out[0]['error']
        FS.follow.assert_not_called()

    def test_follow_happy_path_and_already_following(self):
        bot = _make_bot_user()
        with patch(f'{BRIDGE}.UserService') as US, \
                patch(f'{BRIDGE}.FollowService') as FS:
            US.get_by_id.return_value = types.SimpleNamespace(id='target-1')
            FS.follow.return_value = True
            out1 = process_webhook(MagicMock(), bot, [{
                'type': 'follow', 'user_id': 'target-1'}])
            FS.follow.return_value = False
            out2 = process_webhook(MagicMock(), bot, [{
                'type': 'follow', 'user_id': 'target-1'}])
        assert out1[0]['status'] == 'followed'
        assert out2[0]['status'] == 'already_following'


class TestProcessWebhookVoteGuard:
    """The value ∈ {1,-1} guard is the flagged untrusted-input check."""

    @pytest.mark.parametrize('bad_value', [0, 2, -2, 5, 100, -1000])
    def test_out_of_range_vote_rejected(self, bad_value):
        bot = _make_bot_user()
        with patch(f'{BRIDGE}.VoteService') as VS, \
                patch(f'{BRIDGE}.on_vote_update') as ov:
            out = process_webhook(MagicMock(), bot, [{
                'type': 'vote', 'target_id': 't1', 'value': bad_value}])
        assert out[0]['status'] == 'error'
        assert 'must be 1' in out[0]['error']
        VS.vote.assert_not_called()
        ov.assert_not_called()

    @pytest.mark.parametrize('good_value', [1, -1])
    def test_valid_vote_dispatched(self, good_value):
        bot = _make_bot_user()
        with patch(f'{BRIDGE}.VoteService') as VS, \
                patch(f'{BRIDGE}.on_vote_update') as ov:
            VS.vote.return_value = {'action': 'voted', 'score': 7}
            out = process_webhook(MagicMock(), bot, [{
                'type': 'vote', 'target_id': 't1', 'value': good_value}])
        assert out[0] == {'action': 'vote', 'status': 'voted', 'score': 7}
        VS.vote.assert_called_once()
        ov.assert_called_once_with('post', 't1', 7)

    def test_vote_requires_target_id(self):
        bot = _make_bot_user()
        with patch(f'{BRIDGE}.VoteService') as VS, \
                patch(f'{BRIDGE}.on_vote_update') as ov:
            out = process_webhook(MagicMock(), bot, [{'type': 'vote'}])
        assert out[0]['status'] == 'error'
        assert 'target_id is required' in out[0]['error']
        VS.vote.assert_not_called()

    def test_vote_default_value_is_upvote(self):
        # Missing 'value' defaults to 1 (upvote) and is accepted.
        bot = _make_bot_user()
        with patch(f'{BRIDGE}.VoteService') as VS, \
                patch(f'{BRIDGE}.on_vote_update') as ov:
            VS.vote.return_value = {'action': 'voted', 'score': 1}
            out = process_webhook(MagicMock(), bot, [{
                'type': 'vote', 'target_id': 't1'}])
        assert out[0]['status'] == 'voted'
        _, args, _ = VS.vote.mock_calls[0]
        assert args[-1] == 1  # value arg


class TestProcessWebhookBatchIsolation:

    def test_bad_action_does_not_abort_batch(self):
        bot = _make_bot_user()
        db = MagicMock()
        with patch(f'{BRIDGE}.VoteService') as VS, \
                patch(f'{BRIDGE}.PostService') as PS, \
                patch(f'{BRIDGE}.on_vote_update') as ov:
            PS.create.return_value = types.SimpleNamespace(id='p-ok')
            VS.vote.return_value = {'action': 'voted', 'score': 3}
            out = process_webhook(db, bot, [
                {'type': 'vote', 'target_id': 't', 'value': 99},   # bad -> error
                {'type': 'post', 'title': 'ok'},                    # good
                {'type': 'vote', 'target_id': 't', 'value': 1},     # good
            ])
        assert out[0]['status'] == 'error'
        assert out[1]['status'] == 'created'
        assert out[2]['status'] == 'voted'

    def test_service_exception_isolated_as_error(self):
        bot = _make_bot_user()
        with patch(f'{BRIDGE}.PostService') as PS:
            PS.create.side_effect = RuntimeError('db exploded')
            out = process_webhook(MagicMock(), bot, [
                {'type': 'post', 'title': 'boom'},
                {'type': 'demolish'},
            ])
        assert out[0]['status'] == 'error'
        assert 'db exploded' in out[0]['error']
        # Batch still processed the second action.
        assert out[1]['status'] == 'error'
        assert 'Unknown action' in out[1]['error']

    def test_empty_action_list_returns_empty(self):
        bot = _make_bot_user()
        out = process_webhook(MagicMock(), bot, [])
        assert out == []
        assert bot.last_active_at is not None

    def test_bot_user_with_none_settings(self):
        # settings None must not crash the platform lookup.
        bot = types.SimpleNamespace(settings=None, last_active_at=None)
        with patch(f'{BRIDGE}.PostService') as PS:
            PS.create.return_value = types.SimpleNamespace(id='p1')
            out = process_webhook(MagicMock(), bot, [{'type': 'post', 'title': 't'}])
        _, kwargs = PS.create.call_args
        assert kwargs['source_channel'] == 'ext_external'
        assert out[0]['status'] == 'created'


# ═════════════════════════════════════════════════════════════════════
# Outbound discovery / send (network boundary mocked)
# ═════════════════════════════════════════════════════════════════════

def _resp(status, payload):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r


class TestDiscoverAndSend:

    def test_discover_parses_sessions(self):
        def fake_get(url, timeout=None):
            if url.endswith('/sessions'):
                return _resp(200, [{'id': 's1', 'name': 'Agent One'}])
            if url.endswith('/.well-known/agent.json'):
                raise requests.RequestException('no card')
            return _resp(404, {})
        with patch(f'{BRIDGE}.pooled_get', side_effect=fake_get):
            agents = discover_santaclaw_agents('http://gw')
        assert any(a['session_id'] == 's1' and a['platform'] == 'santaclaw'
                   for a in agents)

    def test_discover_parses_agent_card(self):
        def fake_get(url, timeout=None):
            if url.endswith('/.well-known/agent.json'):
                return _resp(200, {'name': 'CardBot', 'description': 'd',
                                   'skills': [{'name': 'chat'}]})
            raise requests.RequestException('none')
        with patch(f'{BRIDGE}.pooled_get', side_effect=fake_get):
            agents = discover_santaclaw_agents('http://gw')
        a2a = [a for a in agents if a['platform'] == 'a2a']
        assert a2a and a2a[0]['skills'] == ['chat']

    def test_discover_all_endpoints_down_returns_empty(self):
        with patch(f'{BRIDGE}.pooled_get',
                   side_effect=requests.RequestException('down')):
            assert discover_santaclaw_agents('http://gw') == []

    def test_send_success(self):
        with patch(f'{BRIDGE}.pooled_post',
                   return_value=_resp(200, {'ok': True})):
            out = send_to_santaclaw('http://gw', 'sess', 'hi')
        assert out['status'] == 'sent'
        assert out['response'] == {'ok': True}

    def test_send_no_reachable_endpoint(self):
        with patch(f'{BRIDGE}.pooled_post',
                   side_effect=requests.RequestException('nope')):
            out = send_to_santaclaw('http://gw', 'sess', 'hi')
        assert out['status'] == 'error'
        assert out['error'] == 'No reachable endpoint'


class TestAutoRegister:

    def test_auto_register_counts_and_isolates_failures(self, db):
        agents = [
            {'session_id': 'd1', 'name': 'gentle.pearl.owl', 'platform': 'generic',
             'gateway_url': 'http://gw'},
            {'session_id': '', 'name': '', 'platform': 'nope',  # bad platform
             'gateway_url': 'http://gw'},
        ]
        count = auto_register_discovered_agents(db, agents)
        # Only the valid one registers; the bad-platform one is swallowed+logged.
        assert count == 1
