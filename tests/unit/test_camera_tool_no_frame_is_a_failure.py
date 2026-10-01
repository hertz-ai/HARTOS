"""The camera tool says what it is for, and a call with no frame is a failure.

MEASURED on the MSI desktop, agent_system.log + agent_system.log.1, which span
2026-09-22 01:59 to 2026-09-29 19:20:

    grep -E "TOOL EXECUTION SUCCESS: get_user_camera_inp latency"   -> 732
    ... the Result: line after each                                 -> 732 x
        'failed to get visual context ask user to check if the camera is
         turned on'
    ... of those, "for session: c23d388c-..."                       -> 530

c23d388c is hevolve_system_agent, the account the daemon runs goals as; no
person and no camera is behind it.  The questions it sent to the camera were
not about a picture: "What is the current gradient sync status across all HART
nodes?" (26x), "What are the rules extracted from the convergence protocol
document?" (14x).

Three things made that possible, two of them in the tool:

  * Its schema read "Get user's visual information to process somethings",
    with one free-text argument "The Question to check from visual context".
    On the main leg that is the only core tool besides google_search that
    takes any question at all, so a model whose own tool was missing asked it.
  * With no frame it RETURNED a sentence instead of raising.  So
    core.tool_logging logged TOOL EXECUTION SUCCESS, and
    core.constants.tool_reply_failed -- the one rule CREATE's trace banker and
    REUSE's fabrication gate read -- counted the call as done work.  The
    sentence also told the agent to go and ask the user about the camera.

These tests call the real hartos.helper.get_user_camera_inp and the real
closure from core.agent_tools.build_core_tool_closures, wrapped by the real
core.tool_logging.log_tool_execution.  Only the boundaries are replaced: the
in-process FrameStore lookup, the Redis client, the local VLM POST and the
vision API POST.

    python -m pytest tests/unit/test_camera_tool_no_frame_is_a_failure.py -q
"""
import json
import logging
import pickle
from typing import get_type_hints
from unittest.mock import MagicMock, patch

import pytest

np = pytest.importorskip('numpy')
redis = pytest.importorskip('redis')
flask = pytest.importorskip('flask')

from core.agent_tools import build_core_tool_closures  # noqa: E402
from core.constants import TOOL_EXECUTION_FAILED_PREFIX, tool_reply_failed  # noqa: E402
from core.session_cache import TTLCache  # noqa: E402
from core.tool_logging import log_tool_execution  # noqa: E402

# The daemon identity from the log, so the test runs the case that happened.
DAEMON_USER = 'c23d388c-07a0-4a79-816d-5b95642683c0'
NON_VISUAL_QUESTION = ('What is the current gradient sync status across all '
                       'HART nodes?')
OLD_SENTENCE = ('failed to get visual context ask user to check if the camera '
                'is turned on')


@pytest.fixture
def helper(tmp_path, monkeypatch):
    """hartos.helper inside a Flask app context (it logs through
    current_app), with no in-process FrameStore, run from a scratch cwd
    because the tool writes the frame to output_images/ relative to it."""
    import hartos.helper as h
    monkeypatch.chdir(tmp_path)
    app = flask.Flask('camera_tool_no_frame')
    with app.app_context():
        with patch('core.safe_hartos_attr.safe_hartos_attr',
                   return_value=None):
            yield h


def _refused():
    return redis.exceptions.ConnectionError(
        'Error 10061 connecting to localhost:6379. No connection could be '
        'made because the target machine actively refused it.')


def _redis_with_a_frame():
    """A Redis client that holds one frame, the way the cloud camera pipeline
    writes it (pickled BGR array)."""
    frame = np.random.randint(0, 255, (16, 16, 3), dtype=np.uint8)
    client = MagicMock()
    client.get.return_value = pickle.dumps(frame)
    return client


def _ctx(helper_fun, decorator=log_tool_execution):
    return {
        'user_id': DAEMON_USER,
        'prompt_id': '88555124130',
        'agent_data': {},
        'helper_fun': helper_fun,
        'user_prompt': f'{DAEMON_USER}_88555124130',
        'request_id_list': {f'{DAEMON_USER}_88555124130': 'r-1'},
        'recent_file_id': TTLCache(ttl_seconds=60, max_size=8,
                                   name='test_camera_recent_file_id'),
        'scheduler': None,
        'simplemem_store': None,
        'memory_graph': None,
        'log_tool_execution': decorator,
        'send_message_to_user1': lambda *a, **k: None,
        'retrieve_json': lambda s: s,
        'strip_json_values': lambda d: d,
        'save_conversation_db': lambda *a, **k: 1,
    }


def _camera_tool(ctx):
    for name, desc, fn in build_core_tool_closures(ctx):
        if name == 'get_user_camera_inp':
            return desc, fn
    raise AssertionError('get_user_camera_inp was not built by the factory')


# ---------------------------------------------------------------------------
# No frame
# ---------------------------------------------------------------------------

def test_no_frame_raises_instead_of_answering(helper):
    """THE REGRESSION.  Pre-fix this returned OLD_SENTENCE as the answer."""
    with patch.object(helper, 'redis_client') as rc:
        rc.get.side_effect = _refused()
        with pytest.raises(RuntimeError) as err:
            helper.get_user_camera_inp(NON_VISUAL_QUESTION, DAEMON_USER, 'r-1')
    text = str(err.value).lower()
    assert 'camera' in text, err.value
    # The frame lookup's Redis leg is an implementation detail: naming it sent
    # two agents off to "start Redis" on 2026-09-13 (test_get_frame_redis_down).
    assert 'redis' not in text, err.value


def test_through_the_tool_wrapper_it_is_a_failed_call(helper, caplog):
    """What the model and the pipeline actually see: the canonical failure
    envelope, which tool_reply_failed reads as a failure, and an ERROR line
    where 732 SUCCESS lines used to be."""
    caplog.set_level(logging.INFO, logger='agent_logger')
    _desc, tool = _camera_tool(_ctx(helper))
    with patch.object(helper, 'redis_client') as rc:
        rc.get.side_effect = _refused()
        reply = tool(NON_VISUAL_QUESTION)

    assert reply != OLD_SENTENCE
    assert tool_reply_failed(reply), reply
    envelope = json.loads(reply[len(TOOL_EXECUTION_FAILED_PREFIX):])
    assert envelope['tool_function'] == 'get_user_camera_inp'
    assert 'camera' in envelope['error_message'].lower()

    lines = [r.getMessage() for r in caplog.records]
    assert any(m.startswith('TOOL EXECUTION ERROR: get_user_camera_inp')
               for m in lines), lines
    assert not any(m.startswith('TOOL EXECUTION SUCCESS: get_user_camera_inp')
                   for m in lines), lines


# ---------------------------------------------------------------------------
# A frame, but no vision model could read it
# ---------------------------------------------------------------------------

def test_a_frame_no_vision_model_could_read_is_a_failure(helper):
    """Local VLM down AND the vision API unreachable: the question was not
    answered, so the call must not come back as an answer.  Pre-fix this
    returned OLD_SENTENCE."""
    with patch.object(helper, 'redis_client', _redis_with_a_frame()), \
            patch.object(helper.requests, 'post',
                         side_effect=ConnectionError('local VLM down')), \
            patch.object(helper, 'pooled_post',
                         side_effect=ConnectionError('vision API down')), \
            patch('core.config_cache.get_vision_api',
                  return_value='http://vision.invalid/upload'):
        with pytest.raises(RuntimeError) as err:
            helper.get_user_camera_inp('what am I holding?', 7, 'r-2')
    assert 'camera' in str(err.value).lower(), err.value
    # The cause is chained, not lost: the log's traceback shows why.
    assert isinstance(err.value.__cause__, ConnectionError)


def test_a_frame_the_local_vision_model_reads_is_still_answered(helper):
    """Control.  A fix that made every call fail would pass the tests above;
    with a frame and a working local VLM the tool must still answer."""
    ok = MagicMock(status_code=200)
    ok.json.return_value = {
        'choices': [{'message': {'content': 'You are holding a red mug.'}}]}
    with patch.object(helper, 'redis_client', _redis_with_a_frame()), \
            patch.object(helper.requests, 'post', return_value=ok) as post, \
            patch.object(helper, 'pooled_post') as cloud:
        answer = helper.get_user_camera_inp('what am I holding?', 7, 'r-3')
    assert answer == 'You are holding a red mug.'
    assert post.call_count == 1
    cloud.assert_not_called()


# ---------------------------------------------------------------------------
# What the model is told the tool is for
# ---------------------------------------------------------------------------

def test_the_schema_says_it_only_looks_at_the_camera(helper):
    """The description and the argument the model reads are the tool's
    contract with it.  Pre-fix: "Get user's visual information to process
    somethings" and "The Question to check from visual context", which never
    say camera and read as a general question tool."""
    desc, tool = _camera_tool(_ctx(helper))
    low = desc.lower()
    assert 'camera' in low, desc
    assert 'only' in low and 'cannot answer' in low, desc

    hint = get_type_hints(tool, include_extras=True)['inp']
    arg_text = ' '.join(str(m) for m in getattr(hint, '__metadata__', ()))
    assert 'camera' in arg_text.lower(), arg_text
