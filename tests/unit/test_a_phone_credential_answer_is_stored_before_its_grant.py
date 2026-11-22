"""A credential answered from the phone is granted only once the vault holds
exactly what was typed.

Review of 677a4c795 (MUST 2), 22 Nov 2026: store_credential returns as if it
stored, and writes nothing, for a name this process already holds and never
stored (PATH, a proxy, NUNBA_CI) and for a node secret, yet the route said
"stored" and granted, so the grant named that setting an owner credential and
its {{secret:NAME}} resolved to the system's value.  The node-secret check
was case-sensitive, so a lower-case name passed it; any JSON value was
str()-ed; and a value whose grant failed still resolved (store_credential
counted it as the owner's at once).

Owner ruling 2026-11-22: "Yes it can send password over internet."  The
phone's answer carries the value to the node that asked; these tests keep
what makes that safe.

Real: the consent blueprint, ConsentService, the vault and request_credential
on an in-memory database (the fixtures of
test_the_phone_answers_a_credential_ask.py).  Mocked: the ask's delivery.
Every value here is a fake test value.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

from tests.unit.test_the_phone_answers_a_credential_ask import (  # noqa: E402,F401 (fixtures)
    ASK, NAME, OWNER, SCOPE, _grants, _pushed_request_id, _value, phone, world)

TYPED = 'fake-typed-value-7Q'
HELD = 'FX37_HELD_SETTING'          # a name this process holds, never stored
SYSTEM = 'fake-system-value-for-a-setting'


class _Records(logging.Handler):
    """Every log record of every logger, as text."""

    def __init__(self):
        super().__init__(level=1)
        self.text = []

    def emit(self, record):
        msg = record.getMessage()
        if record.exc_info:
            msg += logging.Formatter().formatException(record.exc_info)
        self.text.append(msg + ' ' + repr(record.args))


def _all_records(monkeypatch):
    records = _Records()
    root = logging.getLogger()
    root.addHandler(records)
    monkeypatch.setattr(root, 'level', 1)
    for lg in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(lg, logging.Logger):
            monkeypatch.setattr(lg, 'disabled', False)
            monkeypatch.setattr(lg, 'level', 1)
            if not lg.propagate:
                lg.addHandler(records)
    return records


def _file_ask(scope):
    """An open credential ask on file for the owner, as some code filed it
    (request_credential files none for a held name or a node secret)."""
    from integrations.social.consent_service import ConsentService
    from integrations.social.models import db_session
    with db_session() as db:
        return ConsentService.request_consent(db, OWNER, 'credential',
                                              scope=scope).id


def _granted(scope):
    from integrations.social.models import UserConsent, db_session
    with db_session() as db:
        return [r.id for r in db.query(UserConsent).filter_by(
            user_id=OWNER, consent_type='credential', scope=scope).all()
            if r.granted_at is not None]


def _answer(phone, rid, value):
    return phone('', {'consent_type': 'credential', 'request_id': rid,
                      'value': value})


def test_a_name_the_process_holds_is_refused_and_never_granted(world, phone, monkeypatch):
    from hartos.ai_key_vault import get_ai_key_vault
    rid = _file_ask('secret:' + HELD)
    monkeypatch.setenv(HELD, SYSTEM)

    resp = _answer(phone, rid, TYPED)

    assert resp.status_code == 500
    assert 'nothing was granted' in resp.get_json()['error']
    assert _granted('secret:' + HELD) == []
    assert os.environ[HELD] == SYSTEM
    vault = get_ai_key_vault()
    assert vault.owner_credential(HELD) == ''
    alias = '{{secret:' + HELD + '}}'
    assert vault.resolve_aliases(alias) == alias        # never the system value


def test_a_lower_case_node_secret_name_is_refused(world, phone):
    from hartos.ai_key_vault import get_ai_key_vault
    rid = _file_ask('secret:redis_url')

    resp = _answer(phone, rid, TYPED)

    assert resp.status_code == 400
    assert _granted('secret:redis_url') == []
    assert get_ai_key_vault().get_tool_key('REDIS_URL') != TYPED


def test_a_value_that_is_not_text_is_refused(world, phone):
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id=None)
    rid = _pushed_request_id(world)

    for value in ({'password': TYPED}, [TYPED], 12345, True):
        resp = _answer(phone, rid, value)
        assert resp.status_code == 400, value
    assert _grants() == []
    assert _value() == ''


def test_a_value_whose_grant_fails_resolves_nowhere(world, phone):
    from unittest.mock import patch
    from hartos.ai_key_vault import get_ai_key_vault, request_credential
    from integrations.social import consent_api
    request_credential(ASK, agent_id=None)
    rid = _pushed_request_id(world)

    with patch.object(consent_api.ConsentService, 'grant_consent',
                      side_effect=RuntimeError('database is locked')):
        try:
            status = _answer(phone, rid, TYPED).status_code
        except RuntimeError:
            status = 500                # a test client that re-raises it

    assert status == 500
    assert _grants() == []
    vault = get_ai_key_vault()
    assert vault.owner_credential(NAME) == ''
    assert vault.resolve_aliases('{{secret:' + NAME + '}}') == '{{secret:' + NAME + '}}'


def test_the_typed_value_resolves_only_while_its_grant_stands(world, phone):
    from hartos.ai_key_vault import get_ai_key_vault, request_credential
    from integrations.social.consent_service import ConsentService
    from integrations.social.models import db_session
    request_credential(ASK, agent_id=None)
    rid = _pushed_request_id(world)

    assert _answer(phone, rid, TYPED).status_code == 201

    vault = get_ai_key_vault()
    assert vault.resolve_aliases('{{secret:' + NAME + '}}') == TYPED
    assert NAME not in os.environ                   # never configuration
    with db_session() as db:
        ConsentService.revoke_consent(db, OWNER, 'credential', SCOPE)
    assert vault.owner_credential(NAME) == ''


def test_the_typed_value_is_in_no_log_record(world, phone, monkeypatch):
    from hartos.ai_key_vault import request_credential
    records = _all_records(monkeypatch)
    request_credential(ASK, agent_id='42')
    rid = _pushed_request_id(world)
    monkeypatch.setenv(HELD, SYSTEM)
    held = _file_ask('secret:' + HELD)

    assert _answer(phone, rid, TYPED).status_code == 201
    assert _answer(phone, rid, TYPED).status_code == 404
    assert _answer(phone, held, TYPED).status_code == 500
    assert _answer(phone, _file_ask('secret:redis_url'), TYPED).status_code == 400

    assert records.text, 'no log record was captured'
    assert sum(TYPED in t for t in records.text) == 0
