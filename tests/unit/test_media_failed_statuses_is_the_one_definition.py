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
    ('finished with a file', 'acestep_t',
     {'code': 200, 'data': [{'task_id': 't', 'status': 1,
                             'audio_url': 'http://node/a.wav'}]}),
])
def test_nothing_running_or_finished_is_in_the_set(case, task_id, payload):
    out = _poll(task_id, payload)
    assert out['status'] not in MEDIA_FAILED_STATUSES, (
        f'{case} read as a failure ({out["status"]!r}): a poller would give up '
        f'on a job that is still going or has already succeeded')


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
