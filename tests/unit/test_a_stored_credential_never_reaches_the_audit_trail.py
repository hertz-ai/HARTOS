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
import errno
import gzip
import io
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


def test_an_id_that_cannot_be_written_costs_only_that_id(trail, vault):
    """A user id holding a lone surrogate (json.loads makes one from a request
    body) cannot be written as UTF-8.  Its line keeps its kind and agent and
    names the id it left out, and the lines around it are written (rv_e57b,
    finding 1: the fallback line kept the id, failed too, and the batch was
    lost with no gap line)."""
    trail.record('message', direction='in', agent_id='54', text=f'before {VALUE}')
    trail.record('message', direction='in', agent_id='54', user_id='\ud800', text='odd')
    trail.record('message', direction='out', agent_id='54', text=f'after {VALUE}')
    trail.drain()

    rows = list(reversed(trail.read(agent_id='54', limit=10)))
    assert [r['kind'] for r in rows] == ['message'] * 3
    assert rows[0]['text'] == f'before {ALIAS}' and rows[2]['text'] == f'after {ALIAS}'
    assert 'user_id' not in rows[1] and rows[1]['fields_withheld'] == ['user_id']
    assert 'could not be written' in rows[1]['withheld']
    assert VALUE not in _on_disk(trail)
    assert trail.verify()['ok'] is True


def test_a_failed_line_keeps_the_agent_and_person_its_request_was_bound_to(trail, vault):
    """The model-call line carries only its request id; the agent and person
    come from the request's binding.  When its digest cannot be read, the
    withheld line still names both, so the agent's page shows it (rv_e57b,
    finding 2)."""
    trail.bind('r1', user_id='u1', agent_id='54')
    trail.record('llm_call', direction='out', request_id='r1',
                 digest_of={('a', 1): 'tuple key'})
    trail.drain()

    [line] = trail.read(agent_id='54')
    assert (line['user_id'], line['agent_id'], line['request_id']) == ('u1', '54', 'r1')
    assert 'TypeError' in line['withheld']


def test_a_line_that_fails_after_its_hash_leaves_the_chain_whole(trail, vault):
    """json.dumps can fail after the line's hash is taken (a user id that is a
    map with a tuple key).  The chain must not move for a line that was never
    written, or every line after it fails verify() (rv_e57b, finding 3)."""
    trail.record('action', direction='out', agent_id='54', text='one')
    trail.record('action', direction='out', agent_id='54', user_id={('a',): 1}, text='two')
    trail.record('action', direction='out', agent_id='54', text='three')
    trail.drain()

    rows = list(reversed(trail.read(agent_id='54', limit=10)))
    assert len(rows) == 3 and rows[1]['fields_withheld'] == ['user_id']
    assert trail.verify()['ok'] is True


class _FailingWrites:
    """The trail's file, whose next write fails the way a full disk does."""

    def __init__(self, real):
        self._real, self.fail = real, True

    def write(self, text):
        if self.fail:
            self.fail = False
            raise OSError(28, 'No space left on device')
        return self._real.write(text)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_a_file_that_fails_mid_batch_is_followed_by_a_gap_line_naming_the_lost(
        trail, vault):
    """A full disk drops the rest of the batch.  The next line written is a gap
    line saying how many records were lost, and a flush() waiting on that batch
    is answered, not left to time out (rv_e57b, finding 3)."""
    import threading
    trail.record('action', direction='out', agent_id='54', text='kept')
    trail.drain()
    trail._file = _FailingWrites(trail._file)

    for n in range(3):
        trail.record('action', direction='out', agent_id='54', text=f'lost {n}')
    waiting = threading.Event()
    trail._q.put(waiting)
    with pytest.raises(OSError):
        trail.drain()
    assert waiting.is_set()

    trail.record('action', direction='out', agent_id='54', text='after')
    trail.drain()
    rows = trail.read(limit=10)
    gaps = [r for r in rows if r['kind'] == 'gap']
    assert len(gaps) == 1 and gaps[0]['extra']['lost'] == 3
    assert {r.get('text') for r in rows if r['kind'] == 'action'} == {'kept', 'after'}
    assert trail.verify()['ok'] is True


class _DiskFillsAfter(io.RawIOBase):
    """The trail's file on a disk that fills after ``budget`` more bytes: the
    write that crosses the limit is short and the next one fails with ENOSPC,
    the way the operating system answers, below Python's buffers.  A failure
    faked at the text layer never shows what those buffers drop (rv_36d0,
    finding 1)."""

    def __init__(self, path, budget):
        self._raw = open(path, 'ab', buffering=0)
        self.budget = budget

    def writable(self):
        return True

    def fileno(self):
        return self._raw.fileno()

    def write(self, data):
        if self.budget <= 0:
            raise OSError(errno.ENOSPC, 'No space left on device')
        n = self._raw.write(bytes(data[:self.budget]))
        self.budget -= n
        return n

    def close(self):
        if not self.closed:
            self._raw.close()
        super().close()


def _fill_disk_after(trail, budget):
    """Put the trail's open segment on a disk with ``budget`` bytes left."""
    path = os.path.join(trail.root, trail._file_name)
    trail._file.close()
    trail._file = io.TextIOWrapper(
        io.BufferedWriter(_DiskFillsAfter(path, budget), buffer_size=8192),
        encoding='utf-8')


def _segment_texts(trail):
    """Every action text on disk, segment by segment, read back whole."""
    out = []
    for name in sorted(n for n in os.listdir(trail.root)
                       if n[:4].isdigit() and n.endswith('.jsonl')):
        with open(os.path.join(trail.root, name), encoding='utf-8') as f:
            out.append([json.loads(raw).get('text') for raw in f if raw.strip()])
    return out


def test_a_disk_that_fills_mid_batch_loses_no_line_without_a_gap_line(trail, vault):
    """The disk fills while the buffers flush mid-batch, a write cut short.
    The file is cut back to the last point known whole, every record of the
    batch is counted, the next segment opens with the gap line, and the chain
    verifies (rv_36d0, finding 1: 26 lost, a gap line of 12, verify broken)."""
    trail.record('action', direction='out', agent_id='54', text='kept')
    trail.drain()
    _fill_disk_after(trail, 3000)

    for n in range(40):
        trail.record('action', direction='out', agent_id='54', text=f'lost {n} ' + 'x' * 150)
    with pytest.raises(OSError):
        trail.drain()
    trail.record('action', direction='out', agent_id='54', text='after')
    trail.drain()

    first, second = _segment_texts(trail)
    assert first == ['kept']
    assert second == [None, 'after']
    gap = next(r for r in trail.read(limit=10) if r['kind'] == 'gap')
    assert gap['extra']['lost'] == 40 and gap['extra']['write_failed'] == 40
    assert trail.verify()['ok'] is True


def test_a_disk_that_fills_at_the_batch_flush_is_counted(trail, vault):
    """A small batch reaches the disk only at its final flush.  When that flush
    fails, the lines it held are counted all the same (rv_36d0, finding 1: no
    gap line at all)."""
    trail.record('action', direction='out', agent_id='54', text='kept')
    trail.drain()
    _fill_disk_after(trail, 0)

    for n in range(3):
        trail.record('action', direction='out', agent_id='54', text=f'lost {n}')
    with pytest.raises(OSError):
        trail.drain()
    trail.record('action', direction='out', agent_id='54', text='after')
    trail.drain()

    gap = next(r for r in trail.read(limit=10) if r['kind'] == 'gap')
    assert gap['extra']['lost'] == 3
    assert [t for seg in _segment_texts(trail) for t in seg] == ['kept', None, 'after']
    assert trail.verify()['ok'] is True


def test_a_gap_line_lost_to_the_disk_carries_its_records_into_the_next(trail, vault):
    """A batch opens with a gap line for 5 records the full queue lost.  When
    the disk takes that line too, the next gap line counts the 5 and the 2
    records written after it, and names each cause (rv_36d0, finding 2)."""
    trail.record('action', direction='out', agent_id='54', text='kept')
    trail.drain()
    _fill_disk_after(trail, 0)
    trail._lost = 5

    for n in range(2):
        trail.record('action', direction='out', agent_id='54', text=f'lost {n}')
    with pytest.raises(OSError):
        trail.drain()
    trail.drain()

    gap = next(r for r in trail.read(limit=10) if r['kind'] == 'gap')
    assert (gap['extra']['lost'], gap['extra']['write_failed']) == (7, 2)
    assert 'queue was full' in gap['extra']['why'] and 'failed' in gap['extra']['why']
    assert trail.verify()['ok'] is True


def test_a_storage_limit_line_the_disk_refuses_is_written_on_the_next_check(
        tmp_path, vault, monkeypatch):
    """Going over the size cap writes a storage_limit line before text is
    withheld.  When the disk refuses that line, the batch is counted and the
    switch waits for the line, so a later line never loses its text with
    nothing saying why (rv_36d0, findings 2 and 3)."""
    now = [1_800_000_000.0]
    t = AuditTrail(str(tmp_path / 'trail'), start=False, role_lookup=lambda uid: None,
                   max_bytes=1, min_free_bytes=0, clock=lambda: now[0])
    monkeypatch.setattr(audit_trail, '_trail', t)
    t.record('action', direction='out', agent_id='54', text='one')
    t.drain()
    t._file = _FailingWrites(t._file)
    now[0] += 61

    t.record('action', direction='out', agent_id='54', text='two')
    with pytest.raises(OSError):
        t.drain()
    assert t._withheld is False
    now[0] += 61
    t.record('action', direction='out', agent_id='54', text='three')
    t.drain()

    rows = t.read(limit=10)
    kinds = [r['kind'] for r in rows]
    assert kinds.count('storage_limit') == 1 and kinds.count('gap') == 1
    assert next(r for r in rows if r['kind'] == 'gap')['extra']['lost'] == 1
    three = next(r for r in rows if r['kind'] == 'action' and r.get('text_len') == 5)
    assert 'text' not in three
    assert t.verify()['ok'] is True


def _segment_kinds(trail):
    """Every line's kind on disk, segment by segment."""
    out = []
    for name in sorted(n for n in os.listdir(trail.root)
                       if n[:4].isdigit() and n.endswith('.jsonl')):
        with open(os.path.join(trail.root, name), encoding='utf-8') as f:
            out.append([json.loads(raw)['kind'] for raw in f if raw.strip()])
    return out


def _disk_full_for(monkeypatch, wanted, budget):
    """Files the trail opens for appending whose path ``wanted`` accepts go to
    a disk with ``budget[0]`` bytes left (read at each open, so a test frees
    the disk by raising it)."""
    real_open = open

    def opener(path, mode='r', *args, **kwargs):
        if 'a' in mode and wanted(os.path.basename(str(path))):
            return io.TextIOWrapper(
                io.BufferedWriter(_DiskFillsAfter(path, budget[0]), buffer_size=8192),
                encoding='utf-8')
        return real_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(audit_trail, 'open', opener, raising=False)


def test_a_storage_limit_line_lost_at_the_flush_takes_its_switch_with_it(
        tmp_path, vault, monkeypatch):
    """On a real disk the storage_limit line is buffered: its append returns
    and text is switched off, and the disk refuses it only at the flush, where
    the cut-back removes it.  The switch goes back with the line and storage is
    checked again on the next batch, so text is never withheld with nothing on
    disk saying why, and the lost storage line is not counted as a lost record
    (rv_d9ed, finding 1: 'three' lost its text with no storage_limit line, and
    the gap said 2 lost for 1)."""
    now = [1_800_000_000.0]
    t = AuditTrail(str(tmp_path / 'trail'), start=False, role_lookup=lambda uid: None,
                   max_bytes=1, min_free_bytes=0, clock=lambda: now[0])
    monkeypatch.setattr(audit_trail, '_trail', t)
    t.record('action', direction='out', agent_id='54', text='one')
    t.drain()
    _fill_disk_after(t, 0)
    now[0] += 61

    t.record('action', direction='out', agent_id='54', text='two')
    with pytest.raises(OSError):
        t.drain()
    assert t._withheld is False

    t.record('action', direction='out', agent_id='54', text='three')
    t.drain()

    assert _segment_kinds(t) == [['action'], ['storage_limit', 'gap', 'action']]
    rows = t.read(limit=10)
    gap = next(r for r in rows if r['kind'] == 'gap')
    assert (gap['extra']['lost'], gap['extra']['write_failed']) == (1, 1)
    three = next(r for r in rows if r['kind'] == 'action' and r.get('text_len') == 5)
    assert 'text' not in three
    assert t.verify()['ok'] is True


def test_a_manifest_row_the_full_disk_cuts_short_is_written_whole_later(
        trail, vault, monkeypatch):
    """A segment is listed in the manifest exactly when a full disk closes it.
    A row cut short is kept and written whole once the disk has room, on a
    line of its own, so it never swallows the row after it and a deleted
    segment is still caught (rv_d9ed, finding 2: the torn row and the next
    merged, and verify() missed the deleted segment)."""
    trail.record('action', direction='out', agent_id='54', text='kept')
    trail.drain()
    first = trail._file_name
    _fill_disk_after(trail, 0)
    manifest_room = [10]
    _disk_full_for(monkeypatch, lambda name: name == 'manifest.jsonl', manifest_room)

    trail.record('action', direction='out', agent_id='54', text='lost')
    with pytest.raises(OSError):
        trail.drain()
    manifest_room[0] = 10 ** 9
    trail.record('action', direction='out', agent_id='54', text='after')
    trail.drain()
    trail.close()

    listed = []
    with open(os.path.join(trail.root, 'manifest.jsonl'), encoding='utf-8') as f:
        for raw in f:
            try:
                listed.append(json.loads(raw)['segment'])
            except ValueError:
                pass   # the torn row: unreadable, and alone on its line
    assert listed.count(first) == 1 and len(listed) == 2
    os.remove(os.path.join(trail.root, first))
    report = trail.verify()
    assert report['missing'] == [first] and report['ok'] is False


def test_a_disk_that_stays_full_leaves_no_empty_segments_behind(
        trail, vault, monkeypatch):
    """While the disk stays full every batch opens a segment and loses it.  A
    segment that never held a whole line is removed instead of listed, so a
    full disk is not asked for a manifest row per batch and no empty files
    pile up; every record is still counted (rv_d9ed: a new empty segment and a
    manifest write per batch while the disk is full)."""
    trail.record('action', direction='out', agent_id='54', text='kept')
    trail.drain()
    first = trail._file_name
    _fill_disk_after(trail, 0)
    segment_room = [0]
    _disk_full_for(monkeypatch, lambda name: name[:4].isdigit(), segment_room)

    for n in range(4):
        trail.record('action', direction='out', agent_id='54', text=f'lost {n}')
        with pytest.raises(OSError):
            trail.drain()
    assert [n for n in os.listdir(trail.root) if n[:4].isdigit()] == [first]
    assert trail._manifest_pending == []

    segment_room[0] = 10 ** 9
    trail.record('action', direction='out', agent_id='54', text='after')
    trail.drain()
    gap = next(r for r in trail.read(limit=10) if r['kind'] == 'gap')
    assert (gap['extra']['lost'], gap['extra']['write_failed']) == (4, 4)
    assert trail.verify()['ok'] is True


def test_a_bool_id_is_not_kept_on_a_line_written_without_its_content(trail, vault):
    """True is not an id.  When the line has to be written without its
    content, a bool user id is left out and named (rv_36d0, finding 2)."""
    trail.record('llm_call', direction='out', agent_id='54', user_id=True,
                 digest_of={('a', 1): 'tuple key'})
    trail.drain()

    [line] = trail.read(agent_id='54')
    assert 'user_id' not in line and line['fields_withheld'] == ['user_id']


def test_with_no_vault_lines_are_written_as_before(trail, monkeypatch):
    import core.tool_logging
    monkeypatch.setattr(core.tool_logging, 'credential_vault', lambda: None)

    trail.record('message', direction='in', agent_id='54', text='hello there',
                 channel='web')
    trail.drain()

    [line] = trail.read(agent_id='54')
    assert line['text'] == 'hello there'
    assert line['extra'] == {'channel': 'web'}
