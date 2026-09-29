"""Every BlueBubbles API call carries the server password; no other host does.

BlueBubbles authenticates with a ``?password=`` query param (a header gets a
401).  When the session-wide header was dropped for the query param, only
three calls (server/info, message/query, message/text) gained it, so edit,
unsend, typing, react, mark-read, chat info, chat/new, attachment send and
attachment download all went out unauthenticated.  IMessageAdapter._api now
carries it for every call.

Drives the REAL adapter against two REAL local HTTP servers: a stand-in
BlueBubbles that records each request's password, and an unrelated host
serving an attachment's source URL, which must never see the password.
"""
import asyncio

import pytest

aiohttp = pytest.importorskip('aiohttp')
from aiohttp import web  # noqa: E402

from integrations.channels.base import ChannelConfig, MediaAttachment  # noqa: E402
from integrations.channels.imessage_adapter import IMessageAdapter  # noqa: E402

PASSWORD = 's3cret'


async def _serve(handler):
    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f'http://127.0.0.1:{port}'


def test_every_bluebubbles_call_is_authenticated_and_no_other_host_is(tmp_path):
    seen, external = [], []

    async def bluebubbles(request):
        seen.append((request.method, request.path,
                     request.query.get('password')))
        return web.json_response({'data': {'guid': 'g1'}})

    async def elsewhere(request):
        external.append(dict(request.query))
        return web.Response(body=b'img', content_type='image/png')

    async def run():
        bb_runner, bb_url = await _serve(bluebubbles)
        ext_runner, ext_url = await _serve(elsewhere)
        adapter = IMessageAdapter(ChannelConfig(
            token=PASSWORD, extra={'api_url': bb_url}))
        adapter._session = aiohttp.ClientSession()
        dest = tmp_path / 'a.bin'
        try:
            await adapter.send_message('chat1', 'hi')
            await adapter.send_message('chat1', 'pic', media=[MediaAttachment(
                type=None, url=f'{ext_url}/pic.png', file_name='pic.png',
                mime_type='image/png')])
            await adapter.edit_message('chat1', 'm1', 'edited')
            await adapter.delete_message('chat1', 'm1')
            await adapter.send_typing('chat1')
            await adapter.stop_typing('chat1')
            await adapter.get_chat_info('chat1')
            await adapter.send_tapback('chat1', 'm1', 'love')
            await adapter.mark_read('chat1')
            await adapter.create_group(['+15550001'], name='g')
            await adapter.download_attachment('att1', str(dest))
        finally:
            await adapter._session.close()
            await bb_runner.cleanup()
            await ext_runner.cleanup()

    asyncio.run(run())

    paths = {path for _, path, _ in seen}
    assert {'/api/v1/message/text', '/api/v1/message/attachment',
            '/api/v1/message/m1/edit', '/api/v1/message/m1/unsend',
            '/api/v1/chat/chat1/typing', '/api/v1/chat/chat1',
            '/api/v1/message/react', '/api/v1/chat/chat1/read',
            '/api/v1/chat/new', '/api/v1/attachment/att1/download'} <= paths
    unauthenticated = [(m, p) for m, p, pw in seen if pw != PASSWORD]
    assert unauthenticated == []
    # The attachment's source host was fetched, without the password.
    assert external == [{}]
