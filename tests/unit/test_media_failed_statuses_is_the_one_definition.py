"""MEDIA_FAILED_STATUSES is the one answer to "did this media job fail?".

Before 2026-09-23 three pollers each wrote their own answer:
  core/agent_tools.py bind_game_sound       ``in ('failed', 'error')``
  Nunba routes/kids_media_routes.py         ``in ('failed', 'error')``
  Nunba tts/tts_engine.py                   ``== 'failed'``        <- N1
and the third one polled every failed composition to its 120 s deadline,
because check_media_status reports an AceStep failure as ``'error'``.

The behavioural half proves the constant against its PRODUCER: every failure
check_media_status can actually report is in it, and nothing still running or
finished is. A constant nobody checked against the producer would only move
the drift one step back.

The guard half pins the pollers to the constant (DRY across files, where no
single behavioural test could see a fourth hand-written tuple appear). It is
not the only test here.
"""
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from integrations.service_tools.media_agent import (
    MEDIA_FAILED_STATUSES,
    check_media_status,
)

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
NUNBA = os.path.join(os.path.dirname(REPO), 'Nunba-HART-Companion')


def _poll(task_id, payload):
    resp = MagicMock(status_code=200)
    resp.json.return_value = payload
    with patch('integrations.service_tools.media_agent._get_tool_base_url',
               return_value='http://127.0.0.1:1'), \
         patch('core.http_pool.pooled_post', return_value=resp):
        return json.loads(check_media_status(task_id=task_id))


FAILURES = {
    'acestep numeric 2 (failed)': (
        'acestep_t', {'code': 200, 'data': [{'task_id': 't', 'status': 2}]}),
    'acestep succeeded with nothing saved': (
        'acestep_t', {'code': 200, 'data': [{'task_id': 't', 'status': 1}]}),
    'acestep knows nothing about the task': (
        'acestep_t', {'code': 200, 'data': []}),
    # 6759fbfa6 (hartos-3a F1): a finished job whose file is not on this
    # node is an error. The path does not exist, so _keep_composition
    # returns before it ever touches composer_output_dir().
    'acestep finished but its file is not on this node': (
        'acestep_t', {'code': 200, 'data': [{'task_id': 't', 'status': 1,
            'result': json.dumps([{'file': '/v1/audio?path=%2Fnowhere%2Fgone.wav',
                                   'status': 1}])}]}),
    'video sidecar passes its own failed through': (
        'wan2gp_t', {'status': 'failed', 'error': 'oom'}),
}


@pytest.mark.parametrize('case', sorted(FAILURES))
def test_every_failure_the_producer_reports_is_in_the_set(case):
    task_id, payload = FAILURES[case]
    out = _poll(task_id, payload)
    assert out['status'] in MEDIA_FAILED_STATUSES, (
        f'{case}: check_media_status reported {out["status"]!r}, which a '
        f'poller testing `in MEDIA_FAILED_STATUSES` would not treat as terminal')


@pytest.mark.parametrize('case,task_id,payload', [
    ('still running', 'acestep_t',
     {'code': 200, 'data': [{'task_id': 't', 'status': 0, 'progress': 30}]}),
])
def test_nothing_running_or_finished_is_in_the_set(case, task_id, payload):
    out = _poll(task_id, payload)
    assert out['status'] not in MEDIA_FAILED_STATUSES, (
        f'{case} read as a failure ({out["status"]!r}): a poller would give up '
        f'on a job that is still going or has already succeeded')


def test_a_job_finished_with_a_file_is_not_in_the_set(tmp_path, monkeypatch):
    """The finished case needs a REAL file: since 6759fbfa6 (hartos-3a F1)
    a finished AceStep job is kept into composer_output_dir(), and one whose
    file is not on this node is an error. AceStep's real shape is
    ``/v1/audio?path=<temp file>`` in a JSON string under 'result'
    (MEASURED 2026-09-22); the output dir is redirected to tmp_path so the
    unstubbed keep logic never writes into the real acestep tool dir."""
    import urllib.parse
    import integrations.service_tools.media_agent as ma
    temp = tmp_path / 'acestep_tmp' / 'a.wav'
    temp.parent.mkdir()
    temp.write_bytes(b'RIFF\x00\x00\x00\x00WAVE')
    monkeypatch.setattr(ma, 'composer_output_dir', lambda: tmp_path / 'kept')
    result = json.dumps([{'file': '/v1/audio?path=' + urllib.parse.quote(str(temp)),
                          'status': 1}])
    out = _poll('acestep_t', {'code': 200, 'data': [
        {'task_id': 't', 'status': 1, 'result': result}]})
    assert out['status'] not in MEDIA_FAILED_STATUSES, (
        f'finished with a file read as a failure ({out["status"]!r}): a poller '
        f'would give up on a job that has already succeeded')
    assert out['status'] == 'completed', out


# The pollers, pinned to the constant. Each entry: file, and the phrase that
# must NOT appear any more (the hand-written failure tuple or equality).
_POLLERS = [
    (os.path.join(REPO, 'core', 'agent_tools.py'), "state in ('failed', 'error')"),
    (os.path.join(NUNBA, 'tts', 'tts_engine.py'), "poll.get('status') == 'failed'"),
    (os.path.join(NUNBA, 'routes', 'kids_media_routes.py'),
     "progress.get('status') in ('failed', 'error')"),
]


@pytest.mark.parametrize('path,old_literal', _POLLERS)
def test_source_guard_every_poller_reads_the_one_constant(path, old_literal):
    if not os.path.isfile(path):
        pytest.skip(f'{os.path.basename(os.path.dirname(path))} checkout absent '
                    '(HARTOS-only CI); verified where both repos are present')
    src = open(path, encoding='utf-8').read()
    assert old_literal not in src, (
        f'{path} tests a media status against its own literal again; use '
        f'MEDIA_FAILED_STATUSES')
    assert 'MEDIA_FAILED_STATUSES' in src, (
        f'{path} no longer reads MEDIA_FAILED_STATUSES')
