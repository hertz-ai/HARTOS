"""Guard: core tool closures must work when user_id is a UUID, not an int.

Measured live 2026-09-07 on the installed build, from `agent_system.log`
(`TOOL EXECUTION START/SUCCESS/ERROR`, the canonical markers emitted by
core.tool_logging).  Window 03:47 -> 08:05, 278 executions:

    tool                      start   ok   err
    get_user_camera_inp          10    0    10   <-- every call
    get_user_uploaded_file        4    0     4   <-- every call
    (every other tool)          264  253     0

Two distinct failures, one shared cause — both tools assume `user_id` is
an integer, and on this deployment it is a UUID string:

    get_user_camera_inp  - invalid literal for int() with base 10:
                           'cf125371-5b6a-4e00-beae-f42513cf47ab'
    get_user_uploaded_file - '6c2dc0fc-7c93-4fe0-973e-f7466ff63f29'

The second is a bare `KeyError`, which is why its message is just the
quoted id.  Over the three log rotations the totals are 152/152 and
44/44 — neither tool has EVER succeeded on this box.

Why it matters beyond these two tools: both are in
`MAIN_LEG_CORE_TOOLS`, so they are offered on every reuse body (629 of
642 tool-carrying bodies in the same window).  An agent whose action
needs the camera or an uploaded file gets a tool that is advertised,
selected, called — and cannot ever succeed.

Neither callee needs an int:
  * helper.get_user_camera_inp (helper.py:2161) is annotated `user_id:int`
    but its first statement is `get_frame(str(user_id))` and its only
    other use is f-string interpolation into a filename.  The `int()` at
    the call site is pure loss.
  * `recent_file_id` is a TTLCache written only when a file is actually
    uploaded (create_recipe.py:6106, reuse_recipe.py:4483).  A missing key
    IS the "no file uploaded" case the function's own else-branch already
    answers -- it just raised before reaching it.  `.get()` is the same
    class's documented safe read ("drop-in replacement for dict (supports
    [] operator, .get(), etc.)") and keeps touch-on-read and the loader.

The int path is kept working on purpose: cloud deployments key users by
an integer PK, and dropping `int()` passes an int through unchanged.

    python -m pytest tests/unit/test_core_tools_uuid_user_id.py -q
"""
import unittest

from core.agent_tools import build_core_tool_closures
from core.session_cache import TTLCache

# The exact id from the live failures, so this test fails for the real reason.
UUID_USER = 'cf125371-5b6a-4e00-beae-f42513cf47ab'


class _RecordingHelperFun:
    """Records what the closure actually passed down to the helper layer."""

    def __init__(self):
        self.camera_calls = []

    def get_user_camera_inp(self, inp, user_id, request_id):
        self.camera_calls.append((inp, user_id, request_id))
        return 'visual answer'

    def __getattr__(self, _name):
        return lambda *a, **k: ''


def _ctx(user_id, recent_file_id=None, helper_fun=None):
    """Real factory input.  log_tool_execution is left out deliberately.

    Without the decorator an exception propagates instead of being turned
    into the "Tool execution failed: {...}" envelope, so a defect shows up
    as the actual error rather than a string the assertion has to sniff.
    """
    return {
        'user_id': user_id,
        'prompt_id': 'p1',
        'agent_data': {},
        'helper_fun': helper_fun or _RecordingHelperFun(),
        'user_prompt': 'u1_p1',
        'request_id_list': {'u1_p1': 'r1'},
        'recent_file_id': (recent_file_id if recent_file_id is not None
                           else TTLCache(ttl_seconds=60, max_size=8,
                                         name='test_recent_file_id')),
        'scheduler': None,
        'simplemem_store': None,
        'memory_graph': None,
        'send_message_to_user1': lambda *a, **k: None,
        'retrieve_json': lambda s: s,
        'strip_json_values': lambda d: d,
        'save_conversation_db': lambda *a, **k: 1,
    }


def _tool(ctx, name):
    for tool_name, _desc, fn in build_core_tool_closures(ctx):
        if tool_name == name:
            return fn
    raise AssertionError(f'{name} was not built by the factory')


class CoreToolsAcceptUuidUserId(unittest.TestCase):

    def test_camera_tool_runs_for_a_uuid_user(self):
        """THE REGRESSION (10/10 live failures).

        Pre-fix this raises ValueError: invalid literal for int().
        """
        helper = _RecordingHelperFun()
        fn = _tool(_ctx(UUID_USER, helper_fun=helper), 'get_user_camera_inp')
        result = fn('what is on my desk?')
        self.assertEqual(result, 'visual answer')
        self.assertEqual(
            helper.camera_calls[0][1], UUID_USER,
            'the id must reach the helper intact — it is used as '
            'get_frame(str(user_id)) and as a filename component, so '
            'mangling it loses the frame')

    def test_camera_tool_still_passes_an_int_user_through(self):
        """No regression for integer-keyed (cloud) deployments."""
        helper = _RecordingHelperFun()
        fn = _tool(_ctx(7, helper_fun=helper), 'get_user_camera_inp')
        fn('what is on my desk?')
        self.assertEqual(helper.camera_calls[0][1], 7)

    def test_uploaded_file_tool_answers_honestly_when_nothing_uploaded(self):
        """THE REGRESSION (4/4 live failures).

        Pre-fix this raises KeyError(UUID_USER).  The honest answer already
        exists in the function; it was unreachable.
        """
        fn = _tool(_ctx(UUID_USER), 'get_user_uploaded_file')
        self.assertEqual(fn(), 'No file uploaded from user')

    def test_uploaded_file_tool_still_reports_a_real_upload(self):
        """Proves the fix did not simply swallow the lookup.

        A guard that always answers "nothing here" would pass the test
        above while destroying the feature.
        """
        cache = TTLCache(ttl_seconds=60, max_size=8, name='test_recent_file_id')
        cache[UUID_USER] = 'file-abc123'
        fn = _tool(_ctx(UUID_USER, recent_file_id=cache),
                   'get_user_uploaded_file')
        self.assertIn('file-abc123', fn())

    def test_uploaded_file_tool_reports_nothing_when_the_entry_is_empty(self):
        """A recorded-but-empty entry is still "no file", not a crash."""
        cache = TTLCache(ttl_seconds=60, max_size=8, name='test_recent_file_id')
        cache[UUID_USER] = None
        fn = _tool(_ctx(UUID_USER, recent_file_id=cache),
                   'get_user_uploaded_file')
        self.assertEqual(fn(), 'No file uploaded from user')


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class GenerateVideoAvatarAcceptsUuidUserId(unittest.TestCase):
    """Generate_video's default (avatar) branch saves the turn through the
    REAL hartos.helper.save_conversation_db; only the HTTP boundary is mocked.

    Measured live (agent_system.log 2026-09-22 09:25:46,540):
        TOOL EXECUTION ERROR: Generate_video - invalid literal for int()
        with base 10: 'c23d388c-07a0-4a79-816d-5b95642683c0'
    save_conversation_db built its payload with int(user_id), so every
    avatar video for a UUID user died before any request was sent.  The
    bundled /conversation route (Nunba routes/db_routes.py) stores the id
    as given; central's schema (user_id: int) receives an int user's int.
    """

    def _run(self, user_id):
        import json
        from unittest import mock

        from hartos import helper

        posts = []

        def fake_post(url, data=None, headers=None, timeout=None, **_kw):
            posts.append((url, json.loads(data)))
            return _FakeResponse({'conv_id': 41})

        avatar = {'image_url': 'http://img', 'voice_id': 3,
                  'audio_sample_url': 'http://voice', 'openvoice': False}
        ctx = _ctx(user_id)
        ctx['save_conversation_db'] = helper.save_conversation_db
        with mock.patch.object(helper, 'pooled_post', side_effect=fake_post), \
                mock.patch('core.agent_tools.pooled_post',
                           side_effect=fake_post), \
                mock.patch('core.config_cache.get_db_url',
                           return_value='http://db.local'), \
                mock.patch('core.teacher_avatar.lookup_avatar',
                           return_value=avatar):
            result = _tool(ctx, 'Generate_video')('I love coding!', 0, True)
        return result, posts

    def test_avatar_video_is_saved_and_queued_for_a_uuid_user(self):
        """THE REGRESSION.  Pre-fix: ValueError from int(UUID_USER)."""
        result, posts = self._run(UUID_USER)
        self.assertIn('conv_id:41', result)
        conv = [body for url, body in posts
                if url == 'http://db.local/conversation']
        self.assertEqual(len(conv), 1, posts)
        self.assertEqual(conv[0]['user_id'], UUID_USER,
                         'the id must reach /conversation intact')
        queued = [body for url, body in posts
                  if url == 'http://db.local/video_generate_save']
        self.assertEqual(len(queued), 1, 'the video request must be sent')
        self.assertEqual(queued[0]['conv_id'], 41)

    def test_avatar_video_still_sends_an_int_user_as_an_int(self):
        """No regression for integer-keyed (cloud) deployments."""
        _result, posts = self._run(7)
        conv = [body for url, body in posts
                if url == 'http://db.local/conversation']
        self.assertEqual(conv[0]['user_id'], 7)
        self.assertIsInstance(conv[0]['user_id'], int)


if __name__ == '__main__':
    unittest.main()
