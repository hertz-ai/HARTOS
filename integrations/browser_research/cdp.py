"""Browser Research — the one DevTools protocol (CDP) client.

An agent's own Chrome (driver.ensure_agent_browser, one profile per mailbox)
listens on a debug port.  This module talks to a page of that Chrome over the
DevTools websocket: it picks a page (or opens one), sends commands and reads
their answers, evaluates script and navigates.  It is what headless,
page-level actions run on (owner 2026-11-27, verbatim: "the llm shd be able
to work on or use headless and with head browser (no-GUI) and (with GUI)
actions respectivelu"); actions that need a window run through computer use
on the agent's own headed window instead.

The websocket is websockets' sync client (the pinned websockets==14.2, as
core/peer_link uses it).  The client sends no Origin header, so Chrome's
--remote-allow-origins check does not apply.
"""
import json
import logging
import time
import urllib.request
from typing import Callable, Optional

logger = logging.getLogger('browser_research.cdp')

CDP_COMMAND_TIMEOUT_S = 30.0
CDP_HTTP_TIMEOUT_S = 5.0
CDP_NAVIGATE_WAIT_S = 30.0
# Page text and DOM reads can be large; websockets' default 1 MiB cap would
# drop them.
CDP_MAX_MESSAGE_BYTES = 64 * 1024 * 1024
# Answers that mean "the page was swapped under this call" during a
# navigation, not a failure of the page.
_CONTEXT_GONE = ('Execution context was destroyed',
                 'Cannot find context with specified id',
                 'Inspected target navigated or closed')


class CDPError(RuntimeError):
    """A DevTools command failed.  The message names the command, says what
    the browser answered, and what to do about it."""


def _http_json(url: str, method: str = 'GET'):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=CDP_HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode('utf-8', 'replace'))


def _default_connect(url: str, **kwargs):
    import websockets.sync.client as ws_client
    return ws_client.connect(url, **kwargs)


class CDPPage:
    """One page of a Chrome, driven over its DevTools websocket."""

    def __init__(self, ws_url: str, timeout: float = CDP_COMMAND_TIMEOUT_S,
                 connect: Optional[Callable] = None) -> None:
        self.ws_url = ws_url
        self.timeout = timeout
        self._next_id = 0
        self._ws = (connect or _default_connect)(
            ws_url, open_timeout=10, close_timeout=5,
            max_size=CDP_MAX_MESSAGE_BYTES)

    @classmethod
    def attach(cls, endpoint: str, timeout: float = CDP_COMMAND_TIMEOUT_S,
               connect: Optional[Callable] = None) -> 'CDPPage':
        """The first page of the Chrome at `endpoint` (http://host:port),
        or a new blank page when it has none."""
        endpoint = endpoint.rstrip('/')
        try:
            targets = _http_json(f'{endpoint}/json/list')
            pages = [t for t in targets if t.get('type') == 'page'
                     and t.get('webSocketDebuggerUrl')]
            target = pages[0] if pages else _http_json(
                f'{endpoint}/json/new?about:blank', method='PUT')
        except (OSError, ValueError) as exc:
            raise CDPError(
                f'the browser at {endpoint} did not list its pages ({exc}). '
                f'Start it with driver.ensure_agent_browser(<mailbox>) and use '
                f'the endpoint it returns.') from exc
        ws_url = target.get('webSocketDebuggerUrl') if isinstance(target, dict) else None
        if not ws_url:
            raise CDPError(
                f'the browser at {endpoint} gave no page to attach to. Close '
                f'it and start it again with driver.ensure_agent_browser.')
        return cls(ws_url, timeout=timeout, connect=connect)

    def send(self, method: str, params: Optional[dict] = None,
             timeout: Optional[float] = None) -> dict:
        """Send one command and return its result.  Events and answers to
        other requests that arrive meanwhile are skipped."""
        from websockets.exceptions import ConnectionClosed
        self._next_id += 1
        req_id = self._next_id
        wait_s = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + wait_s
        try:
            self._ws.send(json.dumps({'id': req_id, 'method': method,
                                      'params': params or {}}))
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError()
                msg = json.loads(self._ws.recv(timeout=left))
                if msg.get('id') != req_id:
                    continue
                if 'error' in msg:
                    err = msg['error'] or {}
                    raise CDPError(f'{method} failed: {err.get("message", err)}')
                return msg.get('result') or {}
        except TimeoutError:
            raise CDPError(
                f'{method} got no answer within {wait_s:g} s; the page may be '
                f'stuck. Reload it or close the agent browser and start it '
                f'again.') from None
        except ConnectionClosed as exc:
            raise CDPError(
                f'the browser closed the connection during {method} ({exc}). '
                f'Start it again with driver.ensure_agent_browser.') from exc

    def evaluate(self, expression: str, timeout: Optional[float] = None):
        """The value of `expression` in the page (promises awaited)."""
        result = self.send('Runtime.evaluate', {
            'expression': expression, 'returnByValue': True,
            'awaitPromise': True}, timeout=timeout)
        details = result.get('exceptionDetails')
        if details:
            exc = details.get('exception') or {}
            raise CDPError(f'the page script failed: '
                           f'{exc.get("description") or details.get("text")}')
        return (result.get('result') or {}).get('value')

    def navigate(self, url: str, wait_s: float = CDP_NAVIGATE_WAIT_S) -> None:
        """Open `url` in this page and wait until it has loaded."""
        result = self.send('Page.navigate', {'url': url})
        if result.get('errorText'):
            raise CDPError(f'could not open {url}: {result["errorText"]}. '
                           f'Check the address and the network.')
        self.wait_loaded(url, wait_s)

    def wait_loaded(self, what: str = 'the page',
                    wait_s: float = CDP_NAVIGATE_WAIT_S) -> None:
        """Wait until the page in this tab has loaded (document.readyState
        'complete'; 'interactive' is accepted at the deadline).  A page
        swapped mid-check (a navigation a click started) is checked again."""
        deadline = time.monotonic() + wait_s
        state = None
        while True:
            try:
                state = self.evaluate('document.readyState')
            except CDPError as exc:
                if not any(gone in str(exc) for gone in _CONTEXT_GONE):
                    raise
                state = None
            if state == 'complete':
                return
            if time.monotonic() >= deadline:
                if state == 'interactive':
                    return
                raise CDPError(f'{what} was still loading after {wait_s:g} s. '
                               f'Read what is there, or try again later.')
            time.sleep(0.2)

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception as exc:  # closing a dead socket is not an error
            logger.debug('closing the DevTools socket %s: %s', self.ws_url, exc)

    def __enter__(self) -> 'CDPPage':
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
