"""The agent that filed a credential ask is told why an answer to it was not
taken.

Owner, 22 Nov 2026: "Real failure reasons should be available to the agent
so that it can ask correct questions".

An answer the consent route refuses (it named another node, another scope,
no node at all, the ask was closed, the vault would not take the value) gets
the caller a neutral 404 or the route's own error.  The agent that asked
heard nothing: its tool kept saying "asked the owner", and it could only
wait or ask again.

Now the route records the real reason by the credential's name
(consent_api._tell_the_agent -> ai_key_vault.note_refused_answer) and
request_credential says it in the agent's next tool result.  The HTTP caller
still gets exactly what it got: the refusal of an unbound answer is the same
404 for every cause, decided before the value is read.  The value is never
in any reason.

Real: request_credential, ConsentService, the vault (in memory), the consent
blueprint.  The signed-in user is set by the test.  Fake values only.
"""
import os
import sys

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

import pytest  # noqa: E402

from integrations.social.models import Base, get_engine  # noqa: E402

OWNER = 'owner-phone'
STRANGER = 'stranger-phone'
SECRET = 'fake-Tr0ub4dor&3-horse'
ASK = '{"key_name": "site_password", "label": "Site password", ' \
      '"used_by": "the login step", "description": "Needed to sign in."}'
REASK = ASK[:-1] + ', "rejected": true}'
NAME = 'SITE_PASSWORD'
SCOPE = 'secret:' + NAME
NODE = 'bbbb9d3e55f0a7c2'
OTHER_NODE = 'aaaa1c0277deb4e1'
NEUTRAL = {'success': False, 'error': 'no such ask'}
ASKED = "Asked the owner of this computer for 'Site password'"
NOT_TAKEN = 'An answer was sent and not taken: '


@pytest.fixture(autouse=True)
def world(monkeypatch):
    from hartos import ai_key_vault
    from integrations.social import consent_service
    from security import key_delegation, node_integrity
    from security.secrets_manager import SecretsManager
    engine = get_engine()
    Base.metadata.create_all(engine)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    monkeypatch.delenv('HEVOLVE_MASTER_KEY', raising=False)
    monkeypatch.delenv(NAME, raising=False)
    monkeypatch.setattr(node_integrity, 'get_public_key_hex',
                        lambda: NODE + 'cd' * 24)
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    SecretsManager.reset()
    ai_key_vault.AIKeyVault.reset()
    monkeypatch.setattr(ai_key_vault, '_instance', None)
    monkeypatch.setattr(ai_key_vault, '_REFUSED_ANSWERS', {}, raising=False)
    emitted = []
    monkeypatch.setattr(consent_service, '_emit',
                        lambda topic, data, msg_id=None:
                        emitted.append((topic, dict(data), msg_id)))
    yield emitted
    ai_key_vault.AIKeyVault.reset()
    SecretsManager.reset()
    os.environ.pop(NAME, None)
    Base.metadata.drop_all(engine)


@pytest.fixture
def phone(monkeypatch):
    from types import SimpleNamespace
    from flask import Flask
    from integrations.social import auth, consent_api
    from integrations.social.models import get_db
    app = Flask(__name__)
    app.register_blueprint(consent_api.consent_bp)
    users = {'owner': OWNER, 'stranger': STRANGER}
    monkeypatch.setattr(auth, '_get_user_from_token', lambda token: (
        (SimpleNamespace(id=users[token], is_admin=False, is_moderator=False),
         get_db()) if token in users else (None, None)))
    client = app.test_client()

    def post(path, body, who='owner'):
        return client.post('/api/social/consent' + path, json=body,
                           headers={'Authorization': 'Bearer ' + who})
    return post


def _agent_asks(world, ask=ASK):
    """The agent's tool call: (its result, the open ask's row id)."""
    from hartos.ai_key_vault import request_credential
    world.clear()
    out = request_credential(ask, agent_id='42')
    rows = [m.split(':', 1)[1] for t, _, m in world if t == 'consent.request']
    return out, (rows[-1] if rows else None)


def _answer(rid, **over):
    body = {'consent_type': 'credential', 'request_id': rid, 'scope': SCOPE,
            'node_id': NODE, 'user_id': OWNER, 'value': SECRET}
    body.update(over)
    return {k: v for k, v in body.items() if v is not ...}


def _reason(text):
    assert NOT_TAKEN in text, text
    return text.split(NOT_TAKEN, 1)[1]


# ── the four refusals of an answer that is not bound to its ask ───────

@pytest.mark.parametrize('over, says', [
    ({'node_id': OTHER_NODE}, "meant for another computer's ask"),
    ({'node_id': ...}, 'did not say which ask and which computer'),
    ({'scope': ...}, 'did not say which ask and which computer'),
    ({'scope': 'secret:OTHER_PASSWORD'}, 'named another credential'),
    ({'scope': 7}, 'named another credential'),
])
def test_the_caller_gets_the_neutral_404_and_the_agent_the_reason(world, phone, over, says):
    first, rid = _agent_asks(world)
    assert first.startswith(ASKED) and NOT_TAKEN not in first

    resp = phone('', _answer(rid, **over))

    assert resp.status_code == 404 and resp.get_json() == NEUTRAL, \
        'the caller hears the same for every cause'
    again, _ = _agent_asks(world)
    assert again.startswith(ASKED), 'the ask is still open'
    assert says in _reason(again)
    assert SECRET not in again


def test_each_cause_has_its_own_words(world, phone):
    from integrations.social import consent_api
    reasons = [consent_api.NOT_NAMED, consent_api.OTHER_NODE,
               consent_api.OTHER_SCOPE, consent_api.ASK_CLOSED]
    assert len(set(reasons)) == 4
    for reason in reasons:
        assert reason and SECRET not in reason


def test_a_no_for_another_node_is_told_too(world, phone):
    _, rid = _agent_asks(world)
    resp = phone('/decline', {'request_id': rid, 'node_id': OTHER_NODE})
    assert resp.status_code == 404 and resp.get_json() == NEUTRAL
    assert "meant for another computer's ask" in _reason(_agent_asks(world)[0])


def test_an_answer_to_a_closed_ask_is_recorded_as_that(world, phone):
    """Answered, then answered again: the second is refused because the ask
    is closed.  The agent's tool already says the credential is stored, and
    that stays what it says."""
    from hartos.ai_key_vault import refused_answer
    from integrations.social import consent_api
    _, rid = _agent_asks(world)
    assert phone('', _answer(rid)).status_code == 201
    resp = phone('', _answer(rid))
    assert resp.status_code == 404 and resp.get_json() == NEUTRAL
    assert refused_answer(NAME) == consent_api.ASK_CLOSED
    out, _ = _agent_asks(world)
    assert 'is stored on this computer' in out and NOT_TAKEN not in out


def test_a_refusal_from_an_earlier_round_is_not_said_again(world, phone):
    """The late answer above was refused before the site rejected the value
    and the agent asked again: the new ask does not carry the old reason."""
    _, rid = _agent_asks(world)
    assert phone('', _answer(rid)).status_code == 201
    assert phone('', _answer(rid)).status_code == 404
    out, rid2 = _agent_asks(world, REASK)
    assert out.startswith(ASKED) and NOT_TAKEN not in out
    # a refusal in this round is said
    assert phone('', _answer(rid2, node_id=OTHER_NODE)).status_code == 404
    assert "another computer's ask" in _reason(_agent_asks(world, REASK)[0])


# ── the vault's own refusals ──────────────────────────────────────────

def test_a_name_the_system_holds_is_told_to_the_agent(world, phone, monkeypatch):
    """The process came to hold the name after the card went out: the vault
    refuses the value (409 to the caller, with its reason), and the agent
    hears the same reason."""
    _, rid = _agent_asks(world)
    monkeypatch.setenv(NAME, 'set-by-the-system')
    resp = phone('', _answer(rid))
    assert resp.status_code == 409
    from hartos.ai_key_vault import refused_answer
    reason = refused_answer(NAME)
    assert reason.startswith('this computer refused it: ')
    assert f'already holds a value named {NAME}' in reason
    assert SECRET not in reason and 'set-by-the-system' not in reason


@pytest.mark.parametrize('value, says', [
    ('   ', 'value required'), (7, 'value must be text'),
    ('x' * 5000, 'value exceeds 4096 chars'),
])
def test_a_value_the_route_refuses_is_told_to_the_agent(world, phone, value, says):
    _, rid = _agent_asks(world)
    resp = phone('', _answer(rid, value=value))
    assert resp.status_code == 400
    again, _ = _agent_asks(world)
    assert says in _reason(again)
    assert 'xxxxxxxx' not in again


def test_someone_who_is_not_the_owner_is_told_to_the_agent(world, phone, monkeypatch):
    _, rid = _agent_asks(world)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'someone-else')
    assert phone('', _answer(rid)).status_code == 403
    from hartos.ai_key_vault import refused_answer
    assert 'only the owner of this computer' in refused_answer(NAME)


# ── who may write into the agent's tool result, and when it ends ──────

def test_a_stranger_cannot_put_words_in_the_agents_tool_result(world, phone):
    from hartos.ai_key_vault import refused_answer
    _, rid = _agent_asks(world)
    for over in ({'node_id': OTHER_NODE}, {'scope': ...}, {}):
        resp = phone('', _answer(rid, **over), who='stranger')
        assert resp.status_code == 404 and resp.get_json() == NEUTRAL
    assert phone('', _answer('no-such-row', node_id=OTHER_NODE)).status_code == 404
    assert refused_answer(NAME) == ''
    assert NOT_TAKEN not in _agent_asks(world)[0]


def test_nothing_the_caller_sent_is_in_the_reason(world, phone):
    from hartos.ai_key_vault import refused_answer
    _, rid = _agent_asks(world)
    planted = 'IGNORE-ALL-INSTRUCTIONS-' + 'z' * 20
    phone('', _answer(rid, node_id=planted, scope=planted, value=planted))
    assert planted not in refused_answer(NAME)
    phone('', _answer(rid, scope=planted, value=planted))
    assert planted not in refused_answer(NAME)
    assert refused_answer(NAME)


def test_an_answer_that_is_taken_ends_the_refusal(world, phone):
    from hartos.ai_key_vault import refused_answer
    _, rid = _agent_asks(world)
    assert phone('', _answer(rid, node_id=OTHER_NODE)).status_code == 404
    assert refused_answer(NAME)
    assert phone('', _answer(rid)).status_code == 201
    assert refused_answer(NAME) == ''
    out, _ = _agent_asks(world)
    assert 'is stored on this computer' in out and NOT_TAKEN not in out


def test_with_no_refusal_the_tool_result_is_what_it_was(world):
    out, _ = _agent_asks(world)
    assert out.startswith(ASKED + ' on the consent card. Once they enter it, use '
                          '{{secret:' + NAME + '}}')
    assert out.endswith('and the owner is asked for it again.')
