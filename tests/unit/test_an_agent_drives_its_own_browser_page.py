"""An agent drives a page in its own Chrome over the DevTools protocol.

Owner, 2026-11-27 (verbatim): "the llm shd be able to work on or use headless
and with head browser (no-GUI) and (with GUI) actions respectivelu".

integrations.browser_research.cdp is the one DevTools (CDP) client: it picks
or opens a page of the agent's own Chrome (driver.ensure_agent_browser gives
the endpoint), sends commands and reads their answers, evaluates script and
navigates.  These tests replace the websocket and the HTTP target list with
fakes; nothing starts a browser.

Run:
  pytest tests/unit/test_an_agent_drives_its_own_browser_page.py -q
"""
import json
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.browser_research import cdp  # noqa: E402


class _Socket:
    """A DevTools websocket: `answer(request)` returns the messages the
    browser sends back for one request (events first, if any)."""

    def __init__(self, answer):
        self.answer = answer
        self.sent = []
        self.queue = []
        self.closed = False

    def send(self, raw):
        request = json.loads(raw)
        self.sent.append(request)
        self.queue.extend(self.answer(request))

    def recv(self, timeout=None):
        if not self.queue:
            raise TimeoutError()
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item)

    def close(self):
        self.closed = True


def _page(answer):
    sock = _Socket(answer)
    page = cdp.CDPPage('ws://127.0.0.1:9300/devtools/page/X',
                       connect=lambda url, **kw: sock)
    return page, sock


def _reply(request, result=None, **extra):
    return dict({'id': request['id'], 'result': result or {}}, **extra)


def test_an_answer_is_matched_to_its_request_past_events(monkeypatch):
    page, sock = _page(lambda r: [
        {'method': 'Page.frameNavigated', 'params': {}},   # an event
        {'id': r['id'] + 100, 'result': {'other': True}},  # someone else's
        _reply(r, {'frameId': 'F1'}),
    ])
    assert page.send('Page.navigate', {'url': 'https://example.com'}) == {'frameId': 'F1'}
    assert sock.sent[0]['method'] == 'Page.navigate'
    assert sock.sent[0]['params'] == {'url': 'https://example.com'}


def test_each_request_gets_its_own_id(monkeypatch):
    page, sock = _page(lambda r: [_reply(r)])
    page.send('Runtime.enable')
    page.send('Runtime.enable')
    assert sock.sent[0]['id'] != sock.sent[1]['id']


def test_a_refused_command_says_which_and_why():
    page, _ = _page(lambda r: [{'id': r['id'], 'error': {'code': -32000,
                                                        'message': 'No node found'}}])
    with pytest.raises(cdp.CDPError) as err:
        page.send('DOM.focus', {'nodeId': 7})
    assert 'DOM.focus' in str(err.value) and 'No node found' in str(err.value)


def test_a_browser_that_never_answers_is_reported_not_waited_on_forever():
    page, _ = _page(lambda r: [])
    with pytest.raises(cdp.CDPError) as err:
        page.send('Runtime.evaluate', {'expression': '1'}, timeout=0.01)
    assert 'Runtime.evaluate' in str(err.value) and 'no answer' in str(err.value)


def test_a_closed_browser_is_reported():
    from websockets.exceptions import ConnectionClosedError
    page, _ = _page(lambda r: [ConnectionClosedError(None, None)])
    with pytest.raises(cdp.CDPError) as err:
        page.send('Runtime.evaluate', {'expression': '1'})
    assert 'closed' in str(err.value)


def test_evaluate_returns_the_value_and_reports_a_script_error():
    def answer(r):
        expr = r['params']['expression']
        if expr == 'document.title':
            return [_reply(r, {'result': {'type': 'string', 'value': 'Inbox'}})]
        return [_reply(r, {'result': {'type': 'object'},
                           'exceptionDetails': {'text': 'Uncaught',
                                                'exception': {'description': 'ReferenceError: nope is not defined'}}})]
    page, sock = _page(answer)
    assert page.evaluate('document.title') == 'Inbox'
    assert sock.sent[0]['params']['returnByValue'] is True
    with pytest.raises(cdp.CDPError) as err:
        page.evaluate('nope()')
    assert 'ReferenceError' in str(err.value)


def test_navigate_reports_a_page_that_cannot_open():
    page, _ = _page(lambda r: [_reply(r, {'frameId': 'F', 'errorText': 'net::ERR_NAME_NOT_RESOLVED'})])
    with pytest.raises(cdp.CDPError) as err:
        page.navigate('https://no-such-host.invalid')
    text = str(err.value)
    assert 'no-such-host.invalid' in text and 'ERR_NAME_NOT_RESOLVED' in text


def test_navigate_waits_for_the_page_through_a_context_swap(monkeypatch):
    monkeypatch.setattr(cdp.time, 'sleep', lambda s: None)
    states = iter(['swap', 'loading', 'complete'])

    def answer(r):
        if r['method'] == 'Page.navigate':
            return [_reply(r, {'frameId': 'F', 'loaderId': 'L'})]
        state = next(states)
        if state == 'swap':
            return [{'id': r['id'], 'error': {'code': -32000,
                                              'message': 'Execution context was destroyed.'}}]
        return [_reply(r, {'result': {'type': 'string', 'value': state}})]

    page, sock = _page(answer)
    page.navigate('https://email.ionos.com')
    assert [m['method'] for m in sock.sent] == [
        'Page.navigate', 'Runtime.evaluate', 'Runtime.evaluate', 'Runtime.evaluate']


def test_navigate_that_never_loads_says_so(monkeypatch):
    monkeypatch.setattr(cdp.time, 'sleep', lambda s: None)

    def answer(r):
        if r['method'] == 'Page.navigate':
            return [_reply(r, {'frameId': 'F'})]
        return [_reply(r, {'result': {'type': 'string', 'value': 'loading'}})]

    page, _ = _page(answer)
    with pytest.raises(cdp.CDPError) as err:
        page.navigate('https://slow.example', wait_s=0.01)
    assert 'slow.example' in str(err.value) and 'still loading' in str(err.value)


def test_attach_takes_the_first_page_not_a_worker(monkeypatch):
    calls = []

    def http_json(url, method='GET'):
        calls.append((method, url))
        return [
            {'type': 'service_worker', 'webSocketDebuggerUrl': 'ws://w'},
            {'type': 'page', 'url': 'https://email.ionos.com',
             'webSocketDebuggerUrl': 'ws://127.0.0.1:9300/devtools/page/P1'},
        ]

    monkeypatch.setattr(cdp, '_http_json', http_json)
    opened = []
    page = cdp.CDPPage.attach('http://127.0.0.1:9300',
                              connect=lambda url, **kw: opened.append(url) or _Socket(lambda r: []))
    assert opened == ['ws://127.0.0.1:9300/devtools/page/P1']
    assert calls == [('GET', 'http://127.0.0.1:9300/json/list')]
    page.close()


def test_attach_opens_a_page_when_the_browser_has_none(monkeypatch):
    calls = []

    def http_json(url, method='GET'):
        calls.append((method, url))
        if url.endswith('/json/list'):
            return []
        return {'type': 'page', 'webSocketDebuggerUrl': 'ws://127.0.0.1:9300/devtools/page/NEW'}

    monkeypatch.setattr(cdp, '_http_json', http_json)
    opened = []
    cdp.CDPPage.attach('http://127.0.0.1:9300',
                       connect=lambda url, **kw: opened.append(url) or _Socket(lambda r: []))
    assert calls[1] == ('PUT', 'http://127.0.0.1:9300/json/new?about:blank'), (
        'Chrome 111+ opens a new page only on PUT')
    assert opened == ['ws://127.0.0.1:9300/devtools/page/NEW']


def test_a_browser_that_does_not_answer_its_target_list_says_what_to_do(monkeypatch):
    def http_json(url, method='GET'):
        raise OSError('connection refused')

    monkeypatch.setattr(cdp, '_http_json', http_json)
    with pytest.raises(cdp.CDPError) as err:
        cdp.CDPPage.attach('http://127.0.0.1:9300')
    text = str(err.value)
    assert '127.0.0.1:9300' in text and 'ensure_agent_browser' in text


def test_close_closes_the_socket_and_a_page_works_as_a_context_manager():
    sock = _Socket(lambda r: [])
    with cdp.CDPPage('ws://x', connect=lambda url, **kw: sock):
        pass
    assert sock.closed
