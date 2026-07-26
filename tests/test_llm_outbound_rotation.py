"""PERF-2 (audit #564): bound the unbounded llm_outbound.jsonl writer (~196MB).

Pins the rotation contract for the ONE canonical rotation helper, the env cap,
and the deliberate self-critique invariant: we did NOT flip the body default to
'off' / add --log-verbosity, so the full request body (the forensic value the
live diagnosis flow relies on) is still logged by default.
"""
import importlib


def _mod():
    return importlib.import_module('core.llm_outbound_logger')


def test_rotate_when_over_cap(tmp_path):
    m = _mod()
    p = tmp_path / 'out.jsonl'
    p.write_bytes(b'x' * (2 * 1024 * 1024))  # 2 MB
    assert m._rotate_if_oversized(str(p), max_bytes=1024 * 1024) is True
    assert not p.exists()                     # original moved aside
    assert (tmp_path / 'out.jsonl.old').exists()


def test_noop_when_under_cap(tmp_path):
    m = _mod()
    p = tmp_path / 'out.jsonl'
    p.write_bytes(b'x' * 1024)                 # 1 KB
    assert m._rotate_if_oversized(str(p), max_bytes=1024 * 1024) is False
    assert p.exists()
    assert not (tmp_path / 'out.jsonl.old').exists()


def test_missing_file_is_safe(tmp_path):
    m = _mod()
    assert m._rotate_if_oversized(str(tmp_path / 'nope.jsonl')) is False


def test_single_backup_generation(tmp_path):
    m = _mod()
    p = tmp_path / 'out.jsonl'
    old = tmp_path / 'out.jsonl.old'
    old.write_bytes(b'OLD')                    # a prior backup already exists
    p.write_bytes(b'x' * (2 * 1024 * 1024))
    assert m._rotate_if_oversized(str(p), max_bytes=1024 * 1024) is True
    assert old.read_bytes() != b'OLD'          # prior .old replaced — 1 generation


def test_max_bytes_default_and_env_override(monkeypatch):
    m = _mod()
    monkeypatch.delenv('HEVOLVE_LLM_OUTBOUND_MAX_MB', raising=False)
    assert m._max_outbound_log_bytes() == 20 * 1024 * 1024
    monkeypatch.setenv('HEVOLVE_LLM_OUTBOUND_MAX_MB', '5')
    assert m._max_outbound_log_bytes() == 5 * 1024 * 1024
    monkeypatch.setenv('HEVOLVE_LLM_OUTBOUND_MAX_MB', 'garbage')
    assert m._max_outbound_log_bytes() == 20 * 1024 * 1024  # fallback, no raise


def test_full_body_still_logged_by_default(monkeypatch):
    # self-critique guard: PERF-2 must NOT have flipped the body default to
    # 'off' — that would erase the captured request body we rely on for
    # forensics.  Default ('full') returns the body verbatim (messages intact).
    m = _mod()
    monkeypatch.delenv('HEVOLVE_LLM_OUTBOUND_BODY', raising=False)
    body = {'model': 'llama', 'messages': [{'role': 'user', 'content': 'hi'}]}
    shaped = m._shape_body_for_log(body)
    assert shaped.get('messages') == body['messages']
