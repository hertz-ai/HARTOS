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


def test_an_extra_json_cannot_write_is_withheld_for_its_line_alone(trail, vault):
    """A tuple key or a value that holds itself cannot become JSON at all.
    That line keeps its kind and text with the extra withheld; the lines
    around it in the same batch are written, and the chain verifies
    (rv_2c5a, finding 1: the batch was dropped)."""
    loop = {'note': 'x'}
    loop['self'] = loop
    trail.record('message', direction='in', agent_id='54', text='before')
    trail.record('action', direction='out', agent_id='54', text=f'run {VALUE}',
                 odd={('a', 1): 'tuple key'})
    trail.record('action', direction='out', agent_id='54', text='looped', detail=loop)
    trail.record('message', direction='out', agent_id='54', text='after')
    trail.drain()

    rows = list(reversed(trail.read(agent_id='54', limit=10)))
    assert [r['text'] for r in rows] == ['before', f'run {ALIAS}', 'looped', 'after']
    assert rows[1]['extra'].get('withheld') and rows[2]['extra'].get('withheld')
    assert VALUE not in _on_disk(trail)
    assert trail.verify()['ok'] is True


class _Unprintable:
    """An object whose text cannot be read (a detached database row)."""

    def __str__(self):
        raise RuntimeError('row detached from its session')

    __repr__ = __str__


def test_a_value_whose_text_cannot_be_read_costs_only_its_own_line(trail, vault):
    """A __str__ that raises, in extra or as the text, withholds that field
    of that line; every other line of the batch is written, its stored
    credential still masked (rv_f2f8, findings 1 and 2: the batch was
    dropped)."""
    trail.record('message', direction='in', agent_id='54', text=f'before {VALUE}')
    trail.record('action', direction='out', agent_id='54', text='odd extra',
                 row=_Unprintable())
    trail.record('action', direction='out', agent_id='54', text=_Unprintable(),
                 via='Shell_Command')
    trail.record('message', direction='out', agent_id='54', text=10 ** 5000)
    trail.record('message', direction='out', agent_id='54', text=f'after {VALUE}')
    trail.drain()

    rows = list(reversed(trail.read(agent_id='54', limit=10)))
    assert len(rows) == 5
    assert rows[0]['text'] == f'before {ALIAS}' and rows[4]['text'] == f'after {ALIAS}'
    assert rows[1]['text'] == 'odd extra' and rows[1]['extra'].get('withheld')
    assert rows[2]['text_unreadable'] and 'text' not in rows[2]
    assert rows[2]['extra'] == {'via': 'Shell_Command'}
    assert rows[3]['text_unreadable'] and 'text' not in rows[3]
    assert VALUE not in _on_disk(trail)
    assert trail.verify()['ok'] is True


def test_a_deeply_nested_extra_is_cut_before_the_vault_walks_it(trail, vault):
    """600 nested lists pass JSON but exhausted the recursion limit inside
    the vault's walk, and the whole batch's text was withheld (rv_f2f8,
    finding 3).  The extra is cut at the one depth rule first: the other
    lines keep their text, masked, and the deep one keeps what fits."""
    deep = [VALUE]
    for _ in range(600):
        deep = [deep]
    trail.record('message', direction='in', agent_id='54', text=f'before {VALUE}')
    trail.record('action', direction='out', agent_id='54', text='deep',
                 detail={'note': f'used {VALUE}', 'tree': deep})
    trail.record('message', direction='out', agent_id='54', text='after')
    trail.drain()

    rows = list(reversed(trail.read(agent_id='54', limit=10)))
    assert [r.get('text') for r in rows] == [f'before {ALIAS}', 'deep', 'after']
    assert rows[1]['extra']['detail']['note'] == f'used {ALIAS}'
    assert 'nested too deep' in json.dumps(rows[1]['extra'])
    assert VALUE not in _on_disk(trail)
    assert trail.verify()['ok'] is True


def test_a_withheld_field_names_why_it_is_missing(trail, vault):
    """The line says which kind of error kept its text or extra out, so a
    reader of the trail is not left with a line that looks empty
    (rv_3cd5, finding 3)."""
    loop = {}
    loop['self'] = loop
    trail.record('action', direction='out', agent_id='54', text=_Unprintable())
    trail.record('action', direction='out', agent_id='54', text='looped', detail=loop)
    trail.drain()

    rows = list(reversed(trail.read(agent_id='54', limit=10)))
    assert rows[0]['text_unreadable'] == 'RuntimeError'
    assert 'ValueError' in rows[1]['extra']['withheld']


def test_the_depth_rule_keeps_six_levels_and_cuts_the_seventh(trail, vault):
    """The one depth rule, pinned: text is kept at any level it is reached;
    any other value at the sixth level below the extra fields is one marker."""
    trail.record('action', direction='out', agent_id='54', text='deep',
                 a={'b': {'c': {'d': {'e': {'text': 'kept', 'num': 7,
                                            'list': ['x'], 'map': {'g': 'y'}}}}}})
    trail.drain()

    [line] = trail.read(agent_id='54')
    level5 = line['extra']['a']['b']['c']['d']['e']
    assert level5 == {'text': 'kept', 'num': '[withheld: nested too deep]',
                      'list': '[withheld: nested too deep]',
                      'map': '[withheld: nested too deep]'}


def test_a_line_no_record_can_be_made_from_keeps_its_ids_and_the_rest_are_written(
        trail, vault):
    """A field the line itself cannot be made from (a request body with a
    tuple key, a ref whose text cannot be read) costs that line's content,
    not the lines after it in the batch (rv_3cd5, finding 2)."""
    trail.record('message', direction='in', agent_id='54', text=f'before {VALUE}')
    trail.record('llm_call', direction='out', agent_id='54', user_id='u1',
                 digest_of={('a', 1): 'tuple key'})
    trail.record('file', direction='in', agent_id='54', ref=_Unprintable())
    trail.record('message', direction='out', agent_id='54', text=f'after {VALUE}')
    trail.drain()

    rows = list(reversed(trail.read(agent_id='54', limit=10)))
    assert [r['kind'] for r in rows] == ['message', 'llm_call', 'file', 'message']
    assert rows[0]['text'] == f'before {ALIAS}' and rows[3]['text'] == f'after {ALIAS}'
    assert rows[1]['user_id'] == 'u1' and 'TypeError' in rows[1]['withheld']
    assert 'RuntimeError' in rows[2]['withheld'] and 'ref' not in rows[2]
    assert VALUE not in _on_disk(trail)
    assert trail.verify()['ok'] is True


def test_with_no_vault_lines_are_written_as_before(trail, monkeypatch):
    import core.tool_logging
    monkeypatch.setattr(core.tool_logging, 'credential_vault', lambda: None)

    trail.record('message', direction='in', agent_id='54', text='hello there',
                 channel='web')
    trail.drain()

    [line] = trail.read(agent_id='54')
    assert line['text'] == 'hello there'
    assert line['extra'] == {'channel': 'web'}
