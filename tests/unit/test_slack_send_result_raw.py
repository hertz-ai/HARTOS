"""slack_adapter.py's SendResult.raw=dict(response) crash.

Found 2026-08-24 testing a brand-new Slack channel binding (never tested
before): every successful chat_postMessage/files_upload_v2/chat_update call
crashed building its own SendResult with
"cannot convert dictionary update sequence element #0 to a sequence",
because slack_sdk's AsyncSlackResponse has no __iter__ (only __aiter__) --
dict(response) always fails, regardless of environment. The real payload
lives in response.data. Reproduced directly against a real
AsyncSlackResponse instance, not a mock of the failure.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

pytest.importorskip('slack_sdk', reason='slack_sdk not installed')

from slack_sdk.web.async_slack_response import AsyncSlackResponse


def _fake_response(data):
    return AsyncSlackResponse(
        client=None, http_verb='POST', api_url='https://slack.com/api/chat.postMessage',
        req_args={}, data=data, headers={}, status_code=200,
    )


class TestAsyncSlackResponseDictConversion:
    def test_dict_of_response_itself_always_fails(self):
        # Documents the actual bug: dict(response) is never valid for this
        # slack_sdk type, confirming why the adapter must not do this.
        response = _fake_response({'ok': True, 'channel': 'C1', 'ts': '123.456'})
        with pytest.raises(TypeError, match='cannot convert dictionary update sequence'):
            dict(response)

    def test_dict_of_response_data_is_the_fix(self):
        response = _fake_response({'ok': True, 'channel': 'C1', 'ts': '123.456'})
        raw = dict(response.data)
        assert raw == {'ok': True, 'channel': 'C1', 'ts': '123.456'}
