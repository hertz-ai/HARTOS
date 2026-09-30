"""A failed generation must say WHICH endpoint failed.

MEASURED LIVE 2026-09-09, one reuse drive (13:10:46 -> 13:11:47).  Eleven
generations failed and every single log line read:

    integrations.agent_lightning.wrapper - ERROR - Error in generate_reply: Connection error.
    [LLM-GEN-FAIL] APIConnectionError (attempt 1/2) - re-sampling ... Error: Connection error.
    [LLM-GEN-FAIL] engine failed this generation after 2 retries
        (APIConnectionError: Connection error.).

``"Connection error."`` is not a description of anything: it is the openai
SDK's CONSTANT default message for ``APIConnectionError``.  The SDK builds
that exception as ``APIConnectionError(request=request) from err`` inside
``_base_client._request``, so the two facts that identify the failure are
both present on the object and both discarded by ``str(exc)``:

    exc.request.url   the endpoint that was dialled
    exc.__cause__     the real socket error, e.g.
                      ConnectError('[WinError 10061] No connection could be
                      made because the target machine actively refused it')

WHAT THAT COST.  With three listening-or-not endpoints on the box
(:5000 up, :8080 up, :8081 and :6777 down) the logs could not distinguish
"the copilot tier answered 5xx" from "we dialled a dead port", and those
demand opposite fixes.  Reproduced both by hand on 2026-09-09:

    POST localhost:5000/api/claude/v1  -> openai.InternalServerError 502
                                          (an APIStatusError - HAS a status)
    POST 127.0.0.1:8081/v1            -> openai.APIConnectionError
                                          'Connection error.' (no status)

Only the second matches the live lines, and nothing in the log said so.
The same chain has burned this codebase before: model_registry.py:515
carries the comment "WinError 10061 -> openai APIConnectionError ->
'_tier: direct' fallback, live-pinned 2026-09-03" for a base_url that
dialled the assigned port instead of the serving one.

THE CONTRACT.  Every LLM-generation failure this wrapper logs must name the
endpoint when the exception carries one, and the underlying cause when the
exception's own message is the SDK's content-free default.  No behaviour
changes: the classifier, the retry ladder and the fallback reply are
untouched.  This is the log line, and only the log line.

    python -m pytest tests/unit/test_llm_gen_fail_names_the_endpoint.py -q
"""
import ast
import os

import pytest


WRAPPER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'integrations', 'agent_lightning', 'wrapper.py')

DEAD_URL = 'http://127.0.0.1:8081/v1/chat/completions'
CAUSE_TEXT = '[WinError 10061] No connection could be made'


def _tree():
    with open(WRAPPER, encoding='utf-8') as fh:
        return ast.parse(fh.read())


def _conn_error():
    """The real exception, built the way the openai SDK builds it."""
    openai = pytest.importorskip('openai')
    httpx = pytest.importorskip('httpx')
    req = httpx.Request('POST', DEAD_URL)
    try:
        raise httpx.ConnectError(CAUSE_TEXT)
    except httpx.ConnectError as err:
        return openai.APIConnectionError(request=req), err


class TestTheSDKMessageReallyIsContentFree:
    """Anti-vacuity: prove the premise before asserting the fix.

    If ``str(exc)`` ever DID carry the endpoint, this whole test file would
    be banning a problem that does not exist.
    """

    def test_str_of_the_exception_names_neither_endpoint_nor_cause(self):
        exc, _cause = _conn_error()
        assert str(exc) == 'Connection error.', (
            'the premise of this file is that the SDK message is a constant; '
            f'it is now {str(exc)!r} and the file needs rewriting')
        assert DEAD_URL not in str(exc)
        assert CAUSE_TEXT not in str(exc)

    def test_but_the_object_does_carry_them(self):
        exc, cause = _conn_error()
        assert str(getattr(exc, 'request', None).url) == DEAD_URL
        assert CAUSE_TEXT in str(cause)


class TestTheDescriber:
    """THE FIX.  RED before it exists."""

    def _describe(self):
        from integrations.agent_lightning.wrapper import _describe_llm_failure
        return _describe_llm_failure

    def test_names_the_endpoint_that_was_dialled(self):
        exc, cause = _conn_error()
        exc.__cause__ = cause
        out = self._describe()(exc)
        assert DEAD_URL in out, (
            f'the failure description must name the endpoint; got {out!r}. '
            'Without it a dead port and a 5xx from a live tier are '
            'indistinguishable in the log, which is exactly what made the '
            '2026-09-09 drive unattributable.')

    def test_names_the_underlying_cause(self):
        exc, cause = _conn_error()
        exc.__cause__ = cause
        out = self._describe()(exc)
        assert '10061' in out, (
            f'the real socket error lives in __cause__ and must survive; '
            f'got {out!r}')

    def test_keeps_the_status_code_for_a_5xx(self):
        """The other live class must not regress: a 502 already says 502."""
        openai = pytest.importorskip('openai')
        httpx = pytest.importorskip('httpx')
        req = httpx.Request('POST', 'http://localhost:5000/api/claude/v1/x')
        resp = httpx.Response(502, request=req)
        exc = openai.InternalServerError('Error code: 502 - boom',
                                         response=resp, body=None)
        out = self._describe()(exc)
        assert '502' in out and 'boom' in out

    def test_never_raises_on_a_plain_exception(self):
        """It runs inside an `except` on the chat hot path.

        A describer that throws would convert a recoverable generation
        failure into a crash — strictly worse than the blindness it fixes.
        """
        assert 'kaboom' in self._describe()(ValueError('kaboom'))

    def test_never_raises_when_the_attributes_are_junk(self):
        class Weird(Exception):
            @property
            def request(self):
                raise RuntimeError('exploding attribute')

        out = self._describe()(Weird('weird'))
        assert isinstance(out, str) and out


class TestEveryLogSiteUsesIt:
    """A describer nothing calls is a vacuous fix (feedback_vacuous_guards).

    All three sites logged the same content-free string on 2026-09-09, so
    all three must be migrated — not just the loudest one.
    """

    def _describer_calls(self, node):
        return [n for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and (getattr(n.func, 'id', None)
                     or getattr(n.func, 'attr', None)) == '_describe_llm_failure']

    def test_no_log_call_formats_the_exception_with_bare_str(self):
        """The exact shape that produced 'Connection error.' 11 times."""
        offenders = []
        for node in ast.walk(_tree()):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, 'attr', None) not in ('error', 'warning'):
                continue
            if not any(isinstance(a, ast.Name) and a.id == 'logger'
                       for a in [getattr(node.func, 'value', None)]
                       if isinstance(a, ast.Name)):
                continue
            src = ast.dump(node)
            names_exc = ("id='e'" in src or "id='_last_exc'" in src)
            if names_exc and '_describe_llm_failure' not in src:
                offenders.append(getattr(node, 'lineno', -1))
        assert not offenders, (
            f'logger.error/warning at line(s) {offenders} still format the '
            f'exception without _describe_llm_failure, so they will print the '
            f"SDK's constant 'Connection error.' and name no endpoint")

    def test_all_three_failure_sites_are_migrated(self):
        calls = self._describer_calls(_tree())
        assert len(calls) >= 3, (
            f'expected the describer at all three logging sites measured on '
            f'2026-09-09 (generic except, retry banner, exhausted-retries '
            f'error); found {len(calls)}')
