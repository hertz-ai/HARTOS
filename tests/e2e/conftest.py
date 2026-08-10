"""Hermetic network boundary for the e2e suite.

These tests exercise the DB / dispatch / reward pipelines, not real peer
services — the model bus, Hevolve DB and hive endpoints are ABSENT here just
as they are in CI. Without a seal a test that reaches one blackholes on recv
until the pytest timeout, and (worse) a pooled connection established by one
test and reused by a LATER test hangs the SECOND test — so several e2e tests
pass alone but the suite hangs when run together (measured: CCT + CrossSystem
+ Finance + CodingDispatch each green solo, hung in sequence).

Session-scoped + autouse so the seal is installed once for the whole e2e run,
reusing the canonical refuse_all_network helper (tests/conftest.py). A test
that legitimately needs a mocked response patches at its own call seam ABOVE
the socket (e.g. dispatch's before_dispatch / pooled_post), so it never
reaches this refusal — the seal only bites an UNMOCKED real dial, which is the
bug it exists to surface.
"""
import pytest

from tests.conftest import refuse_all_network


@pytest.fixture(scope='session', autouse=True)
def _e2e_no_network():
    mp = pytest.MonkeyPatch()
    refuse_all_network(mp, reason='e2e suite: network boundary is sealed (no peer services)')
    yield
    mp.undo()
