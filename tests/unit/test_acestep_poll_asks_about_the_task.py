"""A poll that names no task cannot report the task's state.

`check_media_status` asked AceStep's /query_result for results using
``{'task_id': <id>}``. That endpoint reads ``task_id_list``:

    acestep/api/http/query_result_route.py:57
        task_ids = parse_task_id_list(body.get("task_id_list", "[]"))

so the key was missing, `parse_task_id_list` returned the default `"[]"`
parsed to an empty list, `collect_query_results` iterated nothing, and
`wrap_response([])` came back. With no item to read, `data.get('status',
'unknown')` answered **'unknown'** -- for a task that had completed, and
equally for one that had failed. The state was never consulted, so every
outcome read the same.

The second half is the response shape. `/query_result` is a BATCH endpoint:
`wrap_response(data_list)` puts a LIST under `data`, one item per requested
id. `_unwrap_envelope` only unwrapped a dict, so even a correctly-addressed
poll would have fallen back to the outer envelope and answered 'unknown'
again. Both halves are needed or neither works.

acestep_tool's schema was corrected for this on 2026-09-21; the poller that
actually calls the endpoint was not. Found by another session reporting
"a failed composition reports unknown", which they attributed to a third
error envelope. There is no third envelope -- the request simply never
named the task.
"""
import json
from unittest.mock import MagicMock, patch

import pytest


def _resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r


@pytest.fixture
def poll():
    from integrations.service_tools.media_agent import check_media_status
    return check_media_status


class TestTheRequestNamesTheTask:

    def test_acestep_sends_task_id_list(self, poll):
        with patch('integrations.service_tools.media_agent._get_tool_base_url',
                   return_value='http://127.0.0.1:54257'), \
             patch('core.http_pool.pooled_post',
                   return_value=_resp({'code': 200, 'data': []})) as post:
            poll(task_id='acestep_abc123')

        body = post.call_args.kwargs['json']
        assert 'task_id_list' in body, (
            "/query_result reads task_id_list; a bare task_id parses as '[]' "
            f"and asks about nothing. Sent: {body!r}")
        assert body['task_id_list'] == ['abc123']

    def test_the_other_tools_still_send_task_id(self, poll):
        """wan2gp and ltx2 use /check_result, a different contract."""
        for prefix in ('wan2gp', 'ltx2'):
            with patch('integrations.service_tools.media_agent._get_tool_base_url',
                       return_value='http://127.0.0.1:9999'), \
                 patch('core.http_pool.pooled_post',
                       return_value=_resp({'status': 'processing'})) as post:
                poll(task_id=f'{prefix}_xyz')
            body = post.call_args.kwargs['json']
            assert body == {'task_id': 'xyz'}, f'{prefix}: {body!r}'


class TestTheBatchAnswerIsRead:

    def test_a_completed_task_reads_completed(self, poll):
        payload = {'code': 200, 'data': [
            {'task_id': 'abc123', 'status': 'completed',
             'audio_url': 'http://node/song.wav'},
        ]}
        with patch('integrations.service_tools.media_agent._get_tool_base_url',
                   return_value='http://127.0.0.1:54257'), \
             patch('core.http_pool.pooled_post', return_value=_resp(payload)):
            out = json.loads(poll(task_id='acestep_abc123'))

        assert out['status'] == 'completed', out
        assert out.get('results') == [
            {'type': 'audio', 'url': 'http://node/song.wav'}], out

    def test_a_failed_task_reads_failed_not_unknown(self, poll):
        """The case the other session hit: a model that failed to load."""
        payload = {'code': 200, 'data': [
            {'task_id': 'abc123', 'status': 'failed',
             'error': 'model failed to load'},
        ]}
        with patch('integrations.service_tools.media_agent._get_tool_base_url',
                   return_value='http://127.0.0.1:54257'), \
             patch('core.http_pool.pooled_post', return_value=_resp(payload)):
            out = json.loads(poll(task_id='acestep_abc123'))

        assert out['status'] == 'failed', (
            f"a failed composition must not read as unknown: {out!r}")

    def test_the_right_item_is_picked_out_of_a_batch(self, poll):
        payload = {'code': 200, 'data': [
            {'task_id': 'other', 'status': 'completed'},
            {'task_id': 'abc123', 'status': 'processing', 'progress': 42},
        ]}
        with patch('integrations.service_tools.media_agent._get_tool_base_url',
                   return_value='http://127.0.0.1:54257'), \
             patch('core.http_pool.pooled_post', return_value=_resp(payload)):
            out = json.loads(poll(task_id='acestep_abc123'))

        assert out['status'] == 'processing', out
        assert out['progress'] == 42, out

    def test_an_empty_batch_is_not_reported_as_a_state(self, poll):
        """If the server genuinely knows nothing about the id, say so rather
        than inventing a status -- that ambiguity is what hid this bug."""
        with patch('integrations.service_tools.media_agent._get_tool_base_url',
                   return_value='http://127.0.0.1:54257'), \
             patch('core.http_pool.pooled_post',
                   return_value=_resp({'code': 200, 'data': []})):
            out = json.loads(poll(task_id='acestep_gone'))

        assert out['status'] == 'error', out
        assert 'gone' in out.get('error', ''), out
