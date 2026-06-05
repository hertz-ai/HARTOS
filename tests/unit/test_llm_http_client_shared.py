"""Perf-regression tests for the shared LLM httpx.Client (2026-06-01).

py-spy showed autogen rebuilding the OpenAI/httpx client on every
register_for_llm, each rebuild reloading the full CA bundle via
ssl.create_default_context — the #1 GIL hog (a bare 'hi' took ~2m27s).  The fix:
ONE shared httpx.Client (core.http_pool.get_llm_http_client) injected as
http_client into the autogen config_list (core.autogen_config), so the SSL
context is built once for the process.  These pin that behaviour.
"""
from __future__ import annotations

import os
import ssl
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.http_pool as hp  # noqa: E402
import core.autogen_config as ac  # noqa: E402


def _reset_singleton():
    hp._llm_httpx_client = None


def test_get_llm_http_client_is_singleton():
    _reset_singleton()
    import httpx
    c1 = hp.get_llm_http_client()
    c2 = hp.get_llm_http_client()
    assert c1 is c2, "must be a process-wide singleton"
    assert isinstance(c1, httpx.Client)


def test_shared_client_builds_ssl_context_at_most_once(monkeypatch):
    """The whole point: repeated access does NOT rebuild the client, so the
    expensive ssl.create_default_context (CA-bundle parse) runs at most once —
    not once per autogen tool registration."""
    _reset_singleton()
    orig = ssl.create_default_context
    calls = []

    def _counting(*a, **k):
        calls.append(1)
        return orig(*a, **k)

    monkeypatch.setattr(ssl, 'create_default_context', _counting)
    clients = [hp.get_llm_http_client() for _ in range(6)]
    assert all(c is clients[0] for c in clients), "all calls share one client"
    assert len(calls) <= 1, (
        f"shared client must build the SSL context at most once, "
        f"got {len(calls)} CA-bundle loads")


def test_autogen_config_injects_shared_http_client(monkeypatch):
    """Every autogen config entry carries the shared client, so openai reuses it
    instead of building a fresh SSL context per tool registration."""
    _reset_singleton()
    # force the local-llm path (no cloud env configured)
    for k in ('HEVOLVE_NODE_TIER', 'HEVOLVE_ACTIVE_CLOUD_PROVIDER',
              'HEVOLVE_LLM_ENDPOINT_URL', 'HEVOLVE_LLM_API_KEY'):
        monkeypatch.delenv(k, raising=False)
    cfgs = ac.get_autogen_config_list()
    assert cfgs, "config_list must not be empty"
    shared = hp.get_llm_http_client()
    for c in cfgs:
        assert c.get('http_client') is shared, (
            "each config entry must carry the shared httpx client")


def test_shared_client_is_deepcopy_safe():
    """Regression (flywheel REUSE crash, 2026-06-02): a plain httpx.Client is not
    deep-copyable, and autogen/openai deepcopies the llm_config it lives in.  The
    shared client is a SINGLETON, so deepcopy/copy MUST return the SAME instance
    (never duplicate the pool)."""
    import copy
    import httpx
    _reset_singleton()
    c = hp.get_llm_http_client()
    assert copy.deepcopy(c) is c, "deepcopy of the shared client must return itself"
    assert copy.copy(c) is c, "copy of the shared client must return itself"
    assert isinstance(c, httpx.Client), "must still be a usable httpx.Client"


def test_autogen_config_list_survives_deepcopy(monkeypatch):
    """The exact failure seen in frozen_debug.log:

        Some ERROR IN REUSE RECIPE Please implement __deepcopy__ method for each
        value class in llm_config to support deepcopy

    autogen deepcopies the config_list when building/replaying agents; with the
    shared http_client embedded, that copy MUST NOT raise, and the client must
    stay the shared singleton on the other side."""
    import copy
    _reset_singleton()
    for k in ('HEVOLVE_NODE_TIER', 'HEVOLVE_ACTIVE_CLOUD_PROVIDER',
              'HEVOLVE_LLM_ENDPOINT_URL', 'HEVOLVE_LLM_API_KEY'):
        monkeypatch.delenv(k, raising=False)
    cfgs = ac.get_autogen_config_list()
    shared = hp.get_llm_http_client()
    copied = copy.deepcopy(cfgs)  # used to raise — the REUSE crash
    assert copied and copied[0].get('http_client') is shared, (
        "deepcopied config must keep the shared client instance")


def test_http_client_key_is_openai_forwardable():
    """'http_client' must be exactly the kwarg openai.OpenAI accepts — autogen
    routes OpenAI.__init__ kwonly args straight through (oai/client.py:452,514),
    so this is what makes the injection actually reach openai."""
    import inspect
    try:
        from openai import OpenAI
    except Exception:
        import pytest
        pytest.skip("openai not importable in this env")
    kwonly = inspect.getfullargspec(OpenAI.__init__).kwonlyargs
    assert 'http_client' in kwonly, (
        "openai.OpenAI no longer accepts http_client — the injection key must "
        "be updated to match")
