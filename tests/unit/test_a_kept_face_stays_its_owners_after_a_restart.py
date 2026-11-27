"""A kept face stays its owner's after a restart (review hartos-17 MUST 2), and every finished face goes
on the audit trail exactly once before anything may prune it (MUST 4).

``<sid>.owner`` beside the face is the one record of both: {user, request_id, started_at, audited},
written at the session's start by the one writer (_remember_owner); the in-memory map is its
read-through cache; settle_faces records 'complete' (with the face's hash) or 'stopped' (no hash) and
marks it audited; the lifecycle's disk phase settles before it prunes.
"""
import importlib
import json
import os
import time

import pytest

af = importlib.import_module('integrations.service_tools.avatarforcing_tool')
sm = importlib.import_module('core.spoken_media')


@pytest.fixture
def out(monkeypatch, tmp_path):
    monkeypatch.setenv('HEVOLVE_MODEL_DIR', str(tmp_path / 'models'))
    monkeypatch.setattr(af, '_owners', af.OrderedDict())
    monkeypatch.setattr(sm, '_dirs_memo', {'key': None, 'at': 0.0, 'dirs': []})
    d = tmp_path / 'models' / 'avatarforcing' / 'output'
    d.mkdir(parents=True)
    return d


@pytest.fixture
def audit(monkeypatch):
    import security.audit_trail as at
    lines = []
    monkeypatch.setattr(at, 'record', lambda kind, **f: lines.append((kind, f)))
    return lines


def _age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


def _old_start(out, sid):
    rec = json.loads((out / f'{sid}.owner').read_text(encoding='utf-8'))
    rec['started_at'] = time.time() - sm.STALE_PART_S - 10
    (out / f'{sid}.owner').write_text(json.dumps(rec), encoding='utf-8')


# ── MUST 2: the owner after a restart ───────────────────────────────────────────────────────

def test_the_owner_is_kept_beside_the_face_and_read_after_a_restart(out, monkeypatch):
    af._remember_owner('abcdef012345', 42, 'req-1')
    rec = json.loads((out / 'abcdef012345.owner').read_text(encoding='utf-8'))
    assert rec['user'] == '42' and rec['request_id'] == 'req-1' and rec['audited'] is False
    monkeypatch.setattr(af, '_owners', af.OrderedDict())  # a restart: the map is empty
    assert af.stream_owner('abcdef012345') == '42'
    assert af._owners['abcdef012345'] == '42'  # cached


def test_a_local_turns_stream_has_no_owner_after_a_restart_either(out, monkeypatch):
    af._remember_owner('abcdef012346', None)
    monkeypatch.setattr(af, '_owners', af.OrderedDict())
    assert af.stream_owner('abcdef012346') is None


def test_the_first_plain_text_format_still_reads(out, monkeypatch):
    (out / 'abcdef012349.owner').write_text('77', encoding='utf-8')
    assert af.stream_owner('abcdef012349') == '77'


def test_an_unknown_or_bad_sid_reads_nothing(out):
    assert af.stream_owner('abcdef019999') is None
    assert af.stream_owner('../../etc/passwd') is None
    assert af._owner_file('not-a-sid') is None


def test_a_disk_that_refuses_keeps_the_owner_until_a_restart(out, monkeypatch):
    monkeypatch.setattr(af, '_write_owner_record', lambda sid, rec: False)
    af._remember_owner('abcdef012347', 7)
    assert af.stream_owner('abcdef012347') == '7'


def test_an_owner_record_is_never_served(out):
    af._remember_owner('abcdef012348', 9)
    assert sm.find_spoken_media('abcdef012348.owner', kind='video') is None
    assert sm.find_spoken_media('abcdef012348.owner', kind='audio') is None


# ── MUST 4: on the audit trail once, before any prune ───────────────────────────────────────

def test_a_complete_face_is_recorded_once_with_its_hash_and_its_starts_request(out, audit):
    af._remember_owner('abcdef012345', 42, 'req-1')
    (out / 'abcdef012345.mp4').write_bytes(b'face')
    assert af.settle_faces() == 1
    ((kind, f),) = audit
    import hashlib
    assert kind == 'video_out' and f['outcome'] == 'complete'
    assert f['sha256'] == hashlib.sha256(b'face').hexdigest() and f['size'] == 4  # hashed at settle
    assert f['ref'] == str(out / 'abcdef012345.mp4') and f['request_id'] == 'req-1'
    assert f['user_id'] == '42' and f['mime'] == 'video/mp4'


def test_a_stopped_face_is_recorded_stopped_with_no_hash(out, audit):
    af._remember_owner('abcdef012345', 42, 'req-2')
    assert af.settle_faces() == 0  # too recent to tell a stopped face from one being made
    _old_start(out, 'abcdef012345')
    assert af.settle_faces() == 1
    ((kind, f),) = audit
    assert f['outcome'] == 'stopped' and f['ref'] is None and f['sha256'] is None


def test_a_face_being_made_is_not_settled_yet(out, audit):
    af._remember_owner('abcdef012345', 42, 'req-3')
    _old_start(out, 'abcdef012345')
    (out / 'abcdef012345.mp4.part').write_bytes(b'half')
    assert af.settle_faces() == 0 and audit == []


def test_settling_twice_records_nothing_twice(out, audit):
    af._remember_owner('abcdef012345', 42, 'req-1')
    (out / 'abcdef012345.mp4').write_bytes(b'face')
    af.settle_faces()
    af.settle_faces()
    assert len(audit) == 1


def test_a_restart_between_start_and_settle_still_records_once(out, audit, monkeypatch):
    af._remember_owner('abcdef012345', 42, 'req-1')
    monkeypatch.setattr(af, '_owners', af.OrderedDict())  # the process restarted
    (out / 'abcdef012345.mp4').write_bytes(b'face')
    assert af.settle_faces() == 1 and audit[0][1]['request_id'] == 'req-1'


def test_the_first_format_settles_with_an_unknown_request(out, audit):
    (out / 'abcdef012349.owner').write_text('77', encoding='utf-8')
    (out / 'abcdef012349.mp4').write_bytes(b'face')
    assert af.settle_faces() == 1
    assert audit[0][1]['request_id'] is None and audit[0][1]['user_id'] == '77'


def test_a_malformed_record_never_stops_the_other_faces(out, audit, caplog):
    (out / 'aaaaaaaaaaaa.owner').write_text('[1, 2]', encoding='utf-8')  # names nobody
    (out / 'aaaaaaaaaaaa.mp4').write_bytes(b'x' * 5000)
    af._remember_owner('abcdef012345', 42, 'req-1')
    (out / 'abcdef012345.mp4').write_bytes(b'face')
    assert af.settle_faces() == 1
    assert not (out / 'aaaaaaaaaaaa.owner').exists() and 'names nobody' in caplog.text
    assert sorted(f['outcome'] for _k, f in audit) == ['complete', 'unknown_owner']
    _age(out / 'aaaaaaaaaaaa.mp4', 9000)
    sm.prune_spoken_media(pct_of_free=0.0001, free_bytes=lambda _p: 1000)
    assert not (out / 'aaaaaaaaaaaa.mp4').exists()  # prunable again once its bad record is gone


def test_the_prune_never_removes_an_unaudited_face_or_its_record(out, audit):
    af._remember_owner('abcdef012345', 42, 'req-1')
    face = out / 'abcdef012345.mp4'
    face.write_bytes(b'x' * 5000)
    _age(face, 9000)
    _age(out / 'abcdef012345.owner', sm.STALE_PART_S + 10)
    sm.prune_spoken_media(pct_of_free=0.0001, free_bytes=lambda _p: 1000)
    assert face.exists() and (out / 'abcdef012345.owner').exists()
    af.settle_faces()
    sm.prune_spoken_media(pct_of_free=0.0001, free_bytes=lambda _p: 1000)
    assert not face.exists() and not (out / 'abcdef012345.owner').exists()


def test_the_lifecycle_settles_before_it_prunes(monkeypatch):
    lifecycle = importlib.import_module('integrations.service_tools.model_lifecycle')
    order = []
    monkeypatch.setattr(af, 'settle_faces', lambda: order.append('settle'))
    monkeypatch.setattr(sm, 'prune_spoken_media', lambda **k: order.append('prune'))
    mgr = lifecycle.ModelLifecycleManager()
    mgr._maybe_prune_spoken_media(False)
    assert order == ['settle', 'prune']


def test_a_settle_that_raises_never_stops_the_prune(monkeypatch):
    lifecycle = importlib.import_module('integrations.service_tools.model_lifecycle')
    pruned = []
    monkeypatch.setattr(af, 'settle_faces', lambda: (_ for _ in ()).throw(RuntimeError('audit down')))
    monkeypatch.setattr(sm, 'prune_spoken_media', lambda **k: pruned.append(1))
    lifecycle.ModelLifecycleManager()._maybe_prune_spoken_media(False)
    assert pruned == [1]


def test_the_turns_agent_is_kept_from_its_binding_and_put_on_the_line(out, audit, monkeypatch):
    """rv_face2 D3: the agent page selects by agent_id; the binding is in memory only, so it is copied
    into the record at the session's start and survives a restart."""
    import security.audit_trail as at
    monkeypatch.setattr(at, 'bound', lambda rid: {'agent_id': 'p9'} if rid == 'req-1' else {})
    af._remember_owner('abcdef012345', 42, 'req-1')
    monkeypatch.setattr(af, '_owners', af.OrderedDict())  # a restart
    (out / 'abcdef012345.mp4').write_bytes(b'face')
    af.settle_faces()
    assert audit[0][1]['agent_id'] == 'p9'


def test_video_out_is_a_kind_the_agent_page_can_show():
    import security.audit_trail as at
    assert 'video_out' in at.KINDS and 'video_out' in at.VIEW_KINDS


def test_faces_waiting_for_their_audit_still_count_against_the_share(out, audit):
    """rv_face2 D6: an unaudited face is not prunable yet, but it is held; the cap counts it."""
    af._remember_owner('abcdef012345', 42, 'req-1')
    waiting = out / 'abcdef012345.mp4'
    waiting.write_bytes(b'x' * 10000)
    small = out / 'bbbbbbbbbbbb.mp4'
    small.write_bytes(b'x' * 100)
    _age(small, 9000)
    # 2 % of (1000 free + 10100 held) = 222 bytes: only the waiting face puts it over the share
    sm.prune_spoken_media(pct_of_free=2, free_bytes=lambda _p: 1000)
    assert waiting.exists() and not small.exists()



def test_a_volume_with_no_inodes_still_prunes_each_file(out, audit, monkeypatch):
    """rv_face2 D: a network share can report st_ino 0 for every file.  Keyed by inode, every file
    would share one key and none would ever be counted as freed; a file with one name is keyed by
    its path, so the oldest still goes."""
    from pathlib import Path
    real_stat = Path.stat

    def no_inode(self, *a, **k):
        st = real_stat(self, *a, **k)
        return os.stat_result((st.st_mode, 0, st.st_dev, 1, st.st_uid, st.st_gid, st.st_size,
                               int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))
    old = out / 'aaaaaaaaaaaa.mp4'
    old.write_bytes(b'x' * 5000)
    _age(old, 9000)
    new = out / 'bbbbbbbbbbbb.mp4'
    new.write_bytes(b'x' * 5000)
    monkeypatch.setattr(Path, 'stat', no_inode)
    # 50 % of (1000 free + 10000 held) = 5500 bytes: one 5000-byte face must go, the oldest
    got = sm.prune_spoken_media(pct_of_free=50, free_bytes=lambda _p: 1000)
    assert not old.exists() and new.exists()
    assert got['files'] == 1 and got['bytes'] == 5000
