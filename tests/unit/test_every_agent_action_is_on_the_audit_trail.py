"""security.audit_trail: what every agent received and produced, on a
tamper-evident trail that costs the turn nothing and respects the disk.

Owner, 2026-11-20: auditability of every modality per agent "for using HARTOS
in a financial institution", "logging the role etc for each action without
compromise in speed or compute", "storage space shd be respected".
"""
import gzip
import json
import os
import threading

import pytest

from security import audit_trail
from security.audit_trail import AuditTrail, TEXT_CAP, line_hash

DAY = 86400.0
T0 = 1_763_600_000.0  # 2025-11-20 UTC


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _trail(tmp_path, **kw):
    kw.setdefault('clock', Clock())
    kw.setdefault('role_lookup', lambda uid: {'kind': 'human', 'tier': 'flat',
                                              'admin': uid == 'owner'})
    kw.setdefault('start', False)
    return AuditTrail(str(tmp_path / 'trail'), **kw)


def _lines(path):
    opener = gzip.open if str(path).endswith('.gz') else open
    with opener(path, 'rt', encoding='utf-8') as f:
        return [json.loads(x) for x in f if x.strip()]


def test_record_touches_no_disk_until_the_writer_runs(tmp_path):
    t = _trail(tmp_path)
    t.record('message', direction='in', text='hello', user_id='u1', agent_id='54')
    assert not os.path.exists(t.root), 'the hot path wrote to disk'
    assert t.drain() == 1
    rows = t.read(agent_id='54')
    assert len(rows) == 1
    row = rows[0]
    assert row['kind'] == 'message' and row['dir'] == 'in'
    assert row['text'] == 'hello' and row['user_id'] == 'u1'
    assert row['person_role'] == {'kind': 'human', 'tier': 'flat', 'admin': False}
    assert row['prev'] == 'genesis' and row['hash'] == line_hash('genesis', row)


def test_the_chain_verifies_and_an_edited_line_breaks_it(tmp_path):
    t = _trail(tmp_path)
    for i in range(3):
        t.record('message', text=f'line {i}', user_id='u1', agent_id='54')
    t.drain()
    assert t.verify()['ok'] is True
    t._close()
    seg = [n for n in os.listdir(t.root) if n.endswith('.jsonl') and n[0].isdigit()][0]
    path = os.path.join(t.root, seg)
    rows = _lines(path)
    rows[1]['text'] = 'line 1 (changed)'
    with open(path, 'w', encoding='utf-8') as f:
        f.write(''.join(json.dumps(r) + '\n' for r in rows))
    report = t.verify()
    assert report['ok'] is False
    assert report['segments'][0]['reason'] == 'line 2 does not match the line before it'


def test_a_removed_line_breaks_the_chain(tmp_path):
    t = _trail(tmp_path)
    for i in range(3):
        t.record('message', text=f'line {i}', user_id='u1')
    t.drain()
    t._close()
    seg = [n for n in os.listdir(t.root) if n.endswith('.jsonl') and n[0].isdigit()][0]
    path = os.path.join(t.root, seg)
    rows = _lines(path)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(''.join(json.dumps(r) + '\n' for r in (rows[0], rows[2])))
    assert t.verify()['ok'] is False


def test_a_deleted_day_inside_retention_is_reported_missing(tmp_path):
    clock = Clock()
    t = _trail(tmp_path, clock=clock)
    t.record('message', text='day one', user_id='u1')
    t.drain()
    clock.t += DAY
    t.record('message', text='day two', user_id='u1')
    t.drain()
    gz = [n for n in os.listdir(t.root) if n.endswith('.jsonl.gz')]
    assert len(gz) == 1, 'yesterday was not compressed'
    os.remove(os.path.join(t.root, gz[0]))
    report = t.verify()
    assert report['ok'] is False
    assert report['missing'] == [gz[0][:-3]]


def test_secrets_and_card_numbers_are_hidden_but_who_is_kept(tmp_path):
    t = _trail(tmp_path)
    t.record('message', text='my password=hunter2xyz card 4111 1111 1111 1111 '
                              'mail ravi@bank.example', user_id='u1')
    t.drain()
    row = t.read()[0]
    assert 'hunter2xyz' not in row['text']
    assert '4111 1111 1111 1111' not in row['text']
    assert 'ravi@bank.example' in row['text'], 'an auditor must see who'
    assert row['secrets_hidden'] >= 2


def test_long_text_is_capped_with_the_checksum_of_the_whole(tmp_path):
    t = _trail(tmp_path)
    big = 'a' * (TEXT_CAP + 500)
    t.record('message', text=big, user_id='u1')
    t.drain()
    row = t.read()[0]
    assert len(row['text']) == TEXT_CAP
    assert row['text_len'] == TEXT_CAP + 500
    import hashlib
    assert row['text_sha256'] == hashlib.sha256(big.encode()).hexdigest()


def test_a_full_queue_never_blocks_and_the_loss_is_written_down(tmp_path):
    t = _trail(tmp_path, queue_size=2)
    for i in range(5):
        t.record('message', text=str(i), user_id='u1')  # must not block or raise
    t.drain()
    rows = list(reversed(t.read(limit=10)))  # oldest first; ties keep file order
    gaps = [r for r in rows if r['kind'] == 'gap']
    assert len(gaps) == 1 and gaps[0]['extra']['lost'] == 3
    assert [r['text'] for r in rows if r['kind'] == 'message'] == ['0', '1']


def test_past_the_size_cap_text_is_withheld_and_checksums_kept(tmp_path):
    t = _trail(tmp_path, max_bytes=1)
    t.record('message', text='first', user_id='u1')
    t.drain()  # first batch: nothing on disk yet, so no limit
    t._last_storage_check = 0.0
    t.record('message', text='second secret plan', user_id='u1')
    t.drain()
    rows = list(reversed(t.read(limit=10)))  # oldest first; ties keep file order
    kinds = [r['kind'] for r in rows]
    assert 'storage_limit' in kinds
    second = [r for r in rows if r['kind'] == 'message'][-1]
    assert 'text' not in second
    assert second['text_len'] == len('second secret plan') and second['text_sha256']
    t.max_bytes = 10**9
    t._last_storage_check = 0.0
    t.record('message', text='third', user_id='u1')
    t.drain()
    rows = list(reversed(t.read(limit=10)))  # oldest first; ties keep file order
    assert rows[-2]['kind'] == 'storage_ok'
    assert rows[-1]['text'] == 'third'
    assert t.verify()['ok'] is True


def test_days_past_retention_are_deleted_and_recent_ones_kept(tmp_path):
    clock = Clock()
    t = _trail(tmp_path, clock=clock, retention_days=2)
    t.record('message', text='old', user_id='u1')
    t.drain()
    clock.t += DAY
    t.record('message', text='newer', user_id='u1')
    t.drain()
    clock.t += 2 * DAY
    t.record('message', text='today', user_id='u1')
    t.drain()
    texts = [r['text'] for r in t.read(limit=10)]
    assert texts == ['today', 'newer']
    assert t.verify()['ok'] is True, 'a day removed by retention is not "missing"'


def test_a_request_bound_once_attributes_its_later_records(tmp_path):
    t = _trail(tmp_path)
    t.bind('req-1', user_id='owner', agent_id='54', agent_role='Tutor', flow_id=0,
           action_id=2)
    t.record('llm_call', direction='out', request_id='req-1', model='qwen', size=812)
    t.drain()
    row = t.read(agent_id='54')[0]
    assert row['user_id'] == 'owner' and row['agent_role'] == 'Tutor'
    assert row['action_id'] == 2 and row['flow_id'] == 0
    assert row['person_role']['admin'] is True


def test_read_filters_by_agent_and_kind_newest_first(tmp_path):
    clock = Clock()
    t = _trail(tmp_path, clock=clock)
    for i, (kind, agent) in enumerate([('message', '54'), ('file', '54'),
                                       ('message', '7'), ('image_caption', '54')]):
        clock.t = T0 + i
        t.record(kind, text=f'r{i}', user_id='u1', agent_id=agent)
    t.drain()
    assert [r['text'] for r in t.read(agent_id='54')] == ['r3', 'r1', 'r0']
    assert [r['text'] for r in t.read(agent_id='54', kinds=['message', 'file'])] == ['r1', 'r0']


def test_the_background_writer_writes_and_flush_waits_for_it(tmp_path):
    t = _trail(tmp_path, start=True)
    t.record('transcript', text='heard this', user_id='u1', agent_id='54')
    assert t.flush(5.0) is True
    assert t.read(agent_id='54')[0]['text'] == 'heard this'
    assert t._thread is not None and t._thread.name == 'audit-trail'


def test_media_is_a_pointer_and_checksum_never_a_copy(tmp_path):
    media = tmp_path / 'clip.wav'
    media.write_bytes(b'RIFF' + b'\0' * 2000)
    sha, size = audit_trail.sha256_file(str(media))
    t = _trail(tmp_path)
    t.record('audio', ref=str(media), sha256=sha, size=size, mime='audio/wav',
             user_id='u1')
    t.drain()
    row = t.read()[0]
    assert row['ref'] == str(media) and row['sha256'] == sha and row['bytes'] == 2004
    trail_bytes = sum(os.path.getsize(os.path.join(t.root, n)) for n in os.listdir(t.root))
    assert trail_bytes < 2004, 'the media was copied into the trail'


def test_module_record_never_raises(monkeypatch):
    def boom():
        raise RuntimeError('no trail')
    monkeypatch.setattr(audit_trail, 'get_trail', boom)
    audit_trail.record('message', text='x')
    audit_trail.bind('r', user_id='u')


# ── review of a67486cf3 ──────────────────────────────────────────────

def test_a_record_after_close_keeps_every_chain_intact(tmp_path):
    """Daemon threads still record at exit, after close().  Appended to the
    closed file with a fresh chain, verify() reported tampering."""
    t = _trail(tmp_path)
    t.record('message', text='before close', user_id='u1')
    t.close()
    t.record('message', text='after close', user_id='u1')
    t.drain()
    t.close()
    report = t.verify()
    assert report['ok'] is True, report
    assert len(report['segments']) == 2
    assert [r['text'] for r in reversed(t.read())] == ['before close', 'after close']


def test_close_while_a_writer_thread_records_keeps_every_chain_intact(tmp_path):
    """close() runs while the background writer is still emptying the queue,
    and the recording thread keeps going after it: lines really do arrive
    once the day's file is closed (review of a67486cf3, probe B)."""
    t = AuditTrail(str(tmp_path / 'trail'), role_lookup=lambda uid: None,  # real thread
                   max_bytes=10**12, min_free_bytes=0)  # no storage line in the count
    queued, closed, done = threading.Event(), threading.Event(), threading.Event()

    def writer():
        for i in range(2000):
            t.record('message', text=f'before {i}', user_id='u1')
        queued.set()
        closed.wait(10)
        for i in range(2000):
            t.record('message', text=f'after {i}', user_id='u1')
        done.set()

    th = threading.Thread(target=writer)
    th.start()
    assert queued.wait(10)
    t.close()  # races the background thread for the queued lines
    closed.set()
    assert done.wait(10)
    th.join(10)
    assert t.flush(5.0)
    t.close()
    report = t.verify()
    assert report['ok'] is True, [s for s in report['segments'] if not s['ok']]
    assert len(report['segments']) >= 2
    assert sum(s['lines'] for s in report['segments']) == 4000


def test_secrets_in_extra_fields_are_hidden_too(tmp_path):
    t = _trail(tmp_path)
    t.record('action', user_id='u1', step='log in with password=hunter2xyz then pay',
             api_token='tok_live_abc123', names={'file': 'card 4111 1111 1111 1111.pdf'},
             state='COMPLETED')
    t.drain()
    extra = t.read()[0]['extra']
    assert 'hunter2xyz' not in extra['step'] and extra['step'].endswith('then pay')
    assert extra['api_token'] == '[REDACTED]'
    assert '4111 1111 1111 1111' not in extra['names']['file']
    assert extra['state'] == 'COMPLETED'
