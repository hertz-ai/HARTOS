"""A stored credential's value never reaches the audit trail.

A tool runs with the real value in hand: log_tool_execution resolves the
model's {{secret:NAME}} aliases before the call, so a record of what the tool
did (a command, its outcome) can carry the value itself.  The trail is
hash-chained and kept for its retention period, so a value written once
cannot be taken back without breaking the chain.  The writer turns every
stored credential value back into its alias by the vault's own rule
(AIKeyVault.mask_secrets), for every kind of line.

Review of abffee892 (rv_abff, finding 1): `mysql -u app -p{{secret:DB_PASS}}`
reached the trail with the real password and no alias, on the chat and the
computer-use paths.
"""
import gzip
import json
import os
from types import SimpleNamespace

import pytest

from security import audit_trail
from security.audit_trail import TEXT_CAP, AuditTrail

VALUE = 'Zq9!fake-db-pass'   # a made-up value, never a real credential
ALIAS = '{{secret:DB_PASS}}'


@pytest.fixture
def vault(monkeypatch):
    """The real AIKeyVault masking rule, with one value it has resolved for a
    tool (as resolve_aliases records it) and no store or consent table
    behind it."""
    from hartos.ai_key_vault import AIKeyVault
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
    v = AIKeyVault()
    v._sm = SimpleNamespace(_cache={}, get_secret=lambda name, default='': default)
    v._resolved[VALUE] = 'DB_PASS'
    import core.tool_logging
    monkeypatch.setattr(core.tool_logging, 'credential_vault', lambda: v)
    return v


@pytest.fixture
def trail(tmp_path, monkeypatch):
    t = AuditTrail(str(tmp_path / 'trail'), start=False, role_lookup=lambda uid: None)
    monkeypatch.setattr(audit_trail, '_trail', t)
    return t


def _on_disk(trail) -> str:
    """Every byte the trail wrote, as text."""
    out = []
    for name in sorted(os.listdir(trail.root)):
        path = os.path.join(trail.root, name)
        opener = gzip.open if name.endswith('.gz') else open
        with opener(path, 'rt', encoding='utf-8') as f:
            out.append(f.read())
    return ''.join(out)


def test_a_command_run_with_a_stored_credential_is_written_with_its_alias(trail, vault):
    trail.record('action', direction='out', user_id='u1', agent_id='54',
                 text=f'mysql -u app -p{VALUE} -e "select 1"',
                 outcome=f'Exit code: 1 (login as app with {VALUE} refused)',
                 via='Shell_Command', argv=['mysql', f'-p{VALUE}'],
                 detail={'step': {'note': f'tried {VALUE} twice'}})
    trail.drain()

    raw = _on_disk(trail)
    assert VALUE not in raw
    assert json.dumps(VALUE)[1:-1] not in raw
    [line] = trail.read(agent_id='54')
    assert line['text'] == f'mysql -u app -p{ALIAS} -e "select 1"'
    assert line['extra']['outcome'] == f'Exit code: 1 (login as app with {ALIAS} refused)'
    assert line['extra']['argv'] == ['mysql', f'-p{ALIAS}']
    assert line['extra']['detail'] == {'step': {'note': f'tried {ALIAS} twice'}}
    assert trail.verify()['ok'] is True


def test_every_kind_is_masked_not_only_commands(trail, vault):
    trail.record('message', direction='in', user_id='u1', agent_id='54',
                 text=f'my db password is {VALUE}')
    trail.record('llm_call', direction='out', agent_id='54', text=f'prompt with {VALUE}')
    trail.drain()

    assert VALUE not in _on_disk(trail)
    assert {r['text'] for r in trail.read(agent_id='54')} == {
        f'my db password is {ALIAS}', f'prompt with {ALIAS}'}


def test_the_vault_is_asked_once_per_batch_not_once_per_line(trail, vault, monkeypatch):
    calls = []
    real = vault.mask_secrets

    def counting(value):
        calls.append(1)
        return real(value)
    monkeypatch.setattr(vault, 'mask_secrets', counting)

    for n in range(40):
        trail.record('action', direction='out', agent_id='54', text=f'step {n} {VALUE}')
    trail.drain()

    assert len(calls) == 1
    assert VALUE not in _on_disk(trail)
    assert len(trail.read(agent_id='54', limit=100)) == 40


def test_a_value_cut_by_the_text_cap_leaves_no_prefix_behind(trail, vault):
    text = 'x' * (TEXT_CAP - 5) + VALUE + ' tail'
    trail.record('action', direction='out', agent_id='54', text=text)
    trail.drain()

    raw = _on_disk(trail)
    assert VALUE[:5] not in raw
    [line] = trail.read(agent_id='54')
    assert line['text_len'] == len(text) - len(VALUE) + len(ALIAS)


def test_when_masking_fails_the_text_and_extra_are_withheld_never_written_raw(
        trail, vault, monkeypatch):
    def broken(value):
        raise RuntimeError('vault store unreadable')
    monkeypatch.setattr(vault, 'mask_secrets', broken)

    trail.record('action', direction='out', user_id='u1', agent_id='54',
                 text=f'mysql -p{VALUE}', outcome='Exit code: 0', via='Shell_Command')
    trail.drain()

    assert VALUE not in _on_disk(trail)
    [line] = trail.read(agent_id='54')
    assert 'text' not in line
    assert line['text_len'] == len(f'mysql -p{VALUE}')
    assert len(line['text_sha256']) == 64
    assert 'outcome' not in line['extra'] and line['extra'].get('withheld')
    assert (line['kind'], line['user_id']) == ('action', 'u1')
    assert trail.verify()['ok'] is True


@pytest.mark.parametrize('name', ['DB_PASS', 'GITHUB_TOKEN', 'API_KEY', 'AUTH_TOKEN_2',
                                  'SMTP_PASSWORD'])
def test_the_pattern_redactor_leaves_the_vaults_alias_alone(name):
    """The alias names a credential, it is not one: redact_secrets keeps it,
    so the trail can say which credential a command used."""
    from hartos.ai_key_vault import SECRET_ALIAS_RE, AIKeyVault
    from security.secret_redactor import redact_secrets
    alias = AIKeyVault().alias_for(name)
    assert SECRET_ALIAS_RE.fullmatch(alias)
    text = f'login -p{alias} --user app'
    assert redact_secrets(text) == (text, 0)


@pytest.mark.parametrize('text', [
    'secret: hunter2', '{{secret: hunter2}}', '{secret:hunter2}', 'secret=abc',
    '{"secret": "v4lue"}', 'DB_PASSWORD=hunter2', 'token=shortvalue',
    # alias braces around a password typed in any other case (rv_cdf5 F1)
    '{{secret:hunter2}}', '{{Secret:Hunter_2}}', '{{SECRET:HUNTER2}}',
    'my password is {{secret:correcthorsebatterystaple}}'])
def test_the_pattern_redactor_still_hides_a_secret_that_is_not_an_alias(text):
    from security.secret_redactor import redact_secrets
    redacted, count = redact_secrets(text)
    assert count >= 1 and '[REDACTED:' in redacted


def test_a_value_inside_a_non_string_extra_is_masked_too(trail, vault):
    """An exception, bytes or a set in extra is written as its text; that
    text is masked like any other (rv_cdf5, finding 2)."""
    trail.record('action', direction='out', agent_id='54', text='login',
                 error=RuntimeError(f'login with {VALUE} refused'),
                 raw=f'pw={VALUE}'.encode(), seen={f'{VALUE}'})
    trail.drain()

    raw = _on_disk(trail)
    assert VALUE not in raw
    [line] = trail.read(agent_id='54')
    assert ALIAS in line['extra']['error']


def test_with_no_vault_lines_are_written_as_before(trail, monkeypatch):
    import core.tool_logging
    monkeypatch.setattr(core.tool_logging, 'credential_vault', lambda: None)

    trail.record('message', direction='in', agent_id='54', text='hello there',
                 channel='web')
    trail.drain()

    [line] = trail.read(agent_id='54')
    assert line['text'] == 'hello there'
    assert line['extra'] == {'channel': 'web'}
