"""
Instagram FEED publishing, which the adapter could not do at all.

instagram_adapter.py was 806 lines of messaging: DMs, quick replies,
reactions, webhooks for comments and story mentions. Feed publishing is a
different Meta product, and none of it was there. `send_message` sends a DM;
nothing could put a post on the feed, so "post to Instagram" had no code path.

Publishing is three steps, not a POST, and each one is a place to get it
wrong:

    1. POST /{ig-user-id}/media          per image  -> creation_id
    2. POST /{ig-user-id}/media          CAROUSEL   -> children=[ids]
    3. POST /{ig-user-id}/media_publish  creation_id -> media id

These drive that sequence against a fake Graph API. What they pin is mostly
failure behaviour, because the failures are what quietly ship a broken post:
a partial carousel, a container published before it was ready, or a throttle
misread as a content problem.
"""
import asyncio
import sys
import types
import unittest

# aiohttp is imported at module scope by the adapter but never used in these
# tests; stub it so the suite runs on a box without it installed.
if 'aiohttp' not in sys.modules:
    sys.modules['aiohttp'] = types.SimpleNamespace(ClientSession=object)

from integrations.channels.base import ChannelRateLimitError  # noqa: E402
from integrations.channels.extensions.instagram_adapter import (  # noqa: E402
    InstagramAdapter, InstagramConfig,
)


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeGraph:
    """Records every call and replays queued responses per endpoint kind."""

    def __init__(self, container_ids=None, publish_id='PUBLISHED_1',
                 status_sequence=None, fail_on_slide=None):
        self.posts = []
        self.gets = []
        self._container_ids = list(container_ids or ['C1', 'C2', 'C3', 'PARENT'])
        self._publish_id = publish_id
        self._status_sequence = list(status_sequence or ['FINISHED'])
        self._fail_on_slide = fail_on_slide
        self._slide_n = 0

    def post(self, url, data=None, **kw):
        self.posts.append((url, dict(data or {})))
        if url.endswith('/media_publish'):
            return _Resp(200, {'id': self._publish_id})
        # /media -> a container
        self._slide_n += 1
        if self._fail_on_slide and self._slide_n == self._fail_on_slide:
            return _Resp(400, {'error': {'message': 'bad image', 'code': 2207026}})
        return _Resp(200, {'id': self._container_ids.pop(0)})

    def get(self, url, params=None, **kw):
        self.gets.append((url, dict(params or {})))
        state = (self._status_sequence.pop(0)
                 if self._status_sequence else 'FINISHED')
        return _Resp(200, {'status_code': state, 'status': 'detail'})


def _adapter(graph):
    a = InstagramAdapter(InstagramConfig(page_access_token='T'))
    a.instagram_config.instagram_account_id = 'IGID'
    a._session = graph
    return a


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestSinglePhoto(unittest.TestCase):

    def test_creates_a_container_then_publishes_it(self):
        g = _FakeGraph(container_ids=['C1'])
        r = _run(_adapter(g).publish_photo('https://x/img.jpg', 'hello'))

        self.assertTrue(r.success, r.error)
        self.assertEqual(r.message_id, 'PUBLISHED_1')
        # Two POSTs, in order, to the right endpoints.
        self.assertTrue(g.posts[0][0].endswith('/IGID/media'))
        self.assertTrue(g.posts[1][0].endswith('/IGID/media_publish'))
        self.assertEqual(g.posts[1][1]['creation_id'], 'C1')

    def test_caption_is_truncated_to_metas_limit(self):
        g = _FakeGraph(container_ids=['C1'])
        _run(_adapter(g).publish_photo('https://x/i.jpg', 'A' * 5000))
        self.assertEqual(len(g.posts[0][1]['caption']),
                         InstagramAdapter.CAPTION_MAX_CHARS)


class TestCarousel(unittest.TestCase):

    def test_builds_children_then_one_parent_then_publishes(self):
        g = _FakeGraph(container_ids=['C1', 'C2', 'C3', 'PARENT'])
        r = _run(_adapter(g).publish_carousel(
            ['https://x/1.jpg', 'https://x/2.jpg', 'https://x/3.jpg'], 'cap'))

        self.assertTrue(r.success, r.error)
        # 3 slides + 1 parent + 1 publish
        self.assertEqual(len(g.posts), 5)
        for i in range(3):
            self.assertEqual(g.posts[i][1]['is_carousel_item'], 'true')
        parent = g.posts[3][1]
        self.assertEqual(parent['media_type'], 'CAROUSEL')
        self.assertEqual(parent['children'], 'C1,C2,C3')
        self.assertEqual(g.posts[4][1]['creation_id'], 'PARENT')

    def test_a_failed_slide_aborts_the_whole_post(self):
        """Publishing 2 of 3 slides ships a broken argument. Worse than
        nothing, and unrecoverable once it is live."""
        g = _FakeGraph(container_ids=['C1', 'C2', 'C3', 'PARENT'],
                       fail_on_slide=2)
        r = _run(_adapter(g).publish_carousel(
            ['https://x/1.jpg', 'https://x/2.jpg', 'https://x/3.jpg']))

        self.assertFalse(r.success)
        self.assertIn('slide 2/3', r.error)
        # Nothing was published.
        self.assertFalse(any(u.endswith('/media_publish') for u, _ in g.posts))

    def test_rejects_counts_meta_will_reject(self):
        for urls in ([], ['a'], ['u'] * 11):
            r = _run(_adapter(_FakeGraph()).publish_carousel(urls))
            self.assertFalse(r.success)
            self.assertIn('carousel needs', r.error)


class TestContainerReadiness(unittest.TestCase):

    def test_waits_for_finished_before_publishing(self):
        """Publishing an IN_PROGRESS container fails with code 9007, so the
        poll is load-bearing, not a courtesy."""
        g = _FakeGraph(container_ids=['C1'],
                       status_sequence=['IN_PROGRESS', 'IN_PROGRESS', 'FINISHED'])
        a = _adapter(g)
        a._CONTAINER_POLL_INTERVAL_S = 0
        r = _run(a.publish_photo('https://x/i.jpg'))
        self.assertTrue(r.success, r.error)
        self.assertEqual(len(g.gets), 3)

    def test_error_state_is_terminal(self):
        g = _FakeGraph(container_ids=['C1'], status_sequence=['ERROR'])
        r = _run(_adapter(g).publish_photo('https://x/i.jpg'))
        self.assertFalse(r.success)
        self.assertIn('ERROR', r.error)
        self.assertFalse(any(u.endswith('/media_publish') for u, _ in g.posts))

    def test_gives_up_rather_than_polling_forever(self):
        g = _FakeGraph(container_ids=['C1'],
                       status_sequence=['IN_PROGRESS'] * 50)
        a = _adapter(g)
        a._CONTAINER_POLL_INTERVAL_S = 0
        r = _run(a.publish_photo('https://x/i.jpg'))
        self.assertFalse(r.success)
        self.assertIn('not ready', r.error)


class TestErrorMapping(unittest.TestCase):

    def test_throttle_codes_raise_rather_than_read_as_content_failure(self):
        """4/17/32 are throughput limits. Reporting them as a content error
        makes a caller rewrite a post that was never the problem."""
        for code in (4, 17, 32):
            with self.assertRaises(ChannelRateLimitError):
                InstagramAdapter._publish_error({'error': {'code': code}})

    def test_content_errors_surface_message_and_subcode(self):
        r = InstagramAdapter._publish_error({'error': {
            'message': 'Unsupported format', 'code': 2207026,
            'error_subcode': 2207004}})
        self.assertFalse(r.success)
        self.assertIn('Unsupported format', r.error)
        self.assertIn('2207004', r.error)


class TestPreconditions(unittest.TestCase):

    def test_refuses_without_an_account_id(self):
        a = InstagramAdapter(InstagramConfig(page_access_token='T'))
        a._session = _FakeGraph()
        r = _run(a.publish_photo('https://x/i.jpg'))
        self.assertFalse(r.success)
        self.assertIn('connect()', r.error)

    def test_refuses_without_a_session(self):
        a = InstagramAdapter(InstagramConfig(page_access_token='T'))
        a.instagram_config.instagram_account_id = 'IGID'
        r = _run(a.publish_photo('https://x/i.jpg'))
        self.assertFalse(r.success)
        self.assertIn('Not connected', r.error)


class TestReel(unittest.TestCase):
    """Video reels — the daemon video-editor's output. Feed publishing existed
    only for images (photo/carousel); a reel is media_type=REELS + a video_url,
    same create->poll->publish container flow."""

    def test_publishes_a_reel_with_video_url_and_media_type(self):
        g = _FakeGraph()
        r = _run(_adapter(g).publish_reel('https://x/v.mp4', 'my reel'))
        self.assertTrue(r.success)
        self.assertEqual(r.message_id, 'PUBLISHED_1')
        media = next(d for u, d in g.posts if u.endswith('/media'))
        self.assertEqual(media.get('media_type'), 'REELS')
        self.assertEqual(media.get('video_url'), 'https://x/v.mp4')
        self.assertEqual(media.get('caption'), 'my reel')
        self.assertEqual([u.split('/')[-1] for u, _ in g.posts],
                         ['media', 'media_publish'])

    def test_truncates_caption_to_the_limit(self):
        g = _FakeGraph()
        _run(_adapter(g).publish_reel('https://x/v.mp4', 'A' * 5000))
        media = next(d for u, d in g.posts if u.endswith('/media'))
        self.assertEqual(len(media['caption']),
                         InstagramAdapter.CAPTION_MAX_CHARS)

    def test_a_failed_container_never_publishes(self):
        g = _FakeGraph(status_sequence=['ERROR'])
        r = _run(_adapter(g).publish_reel('https://x/v.mp4', 'x'))
        self.assertFalse(r.success)
        self.assertIn('ERROR', r.error)
        self.assertFalse(
            any(u.endswith('/media_publish') for u, _ in g.posts))


if __name__ == '__main__':
    unittest.main()
