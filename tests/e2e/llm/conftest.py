"""Real-LLM end-to-end harness — drives the PRODUCTION model (whatever
``core.port_registry.get_local_llm_url`` resolves to, normally the local
llama-server on :8080) with **no mock and no canned mock_llm_server reply**.

Why this exists
---------------
The 691-file suite mocks the LLM everywhere — even the "e2e" tests inject
``mock_llm_server.py`` (canned, keyword-matched strings).  So the *agentic*
behaviour — the model actually decomposing a goal, deciding to enforce
consent, emitting the structured JSON the pipeline consumes — was never
exercised by an automated test.  These tests close that gap: a real model
in the loop, asserting the real outcome.

Discipline
----------
- They **SKIP cleanly** when no model is reachable (CI without a GPU/model),
  so they never false-green and never block the mocked suite.
- Bring a *dedicated* model up first so daemon/flywheel traffic doesn't
  contend the single 4B slot (which would make these slow + flaky):
  start a standalone llama-server (trueflow ``ai_server_start``) **or** boot
  Nunba with ``HEVOLVE_AGENT_ENGINE_ENABLED=0``.  Then::

      python -m pytest tests/e2e/llm -m llm_e2e -v

- Assertions follow ``tests/e2e/agentic_harness.py``: assert verifiable
  SIDE EFFECTS / parsed structure, **never exact tokens** (LLM output varies).
"""
import os

import pytest
import requests


def live_model_base_url():
    """Return the reachable local-model base URL (``…/v1``), or ``None``.

    Reuses the canonical resolver ``get_local_llm_url`` (which already
    probes candidate ports) so the test endpoint is exactly what the
    runtime would use; falls back to the configured/default :8080.
    """
    base = ""
    try:
        from core.port_registry import get_local_llm_url
        base = (get_local_llm_url() or "").rstrip("/")
    except Exception:
        base = ""
    if not base:
        base = (os.environ.get("HEVOLVE_LLM_ENDPOINT_URL")
                or "http://127.0.0.1:8080/v1").rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    try:
        r = requests.get(base + "/models", timeout=4)
        if r.status_code == 200 and (r.json() or {}).get("data"):
            return base
    except Exception:
        return None
    return None


class _RealLLM:
    """Thin OpenAI-compatible client over the live model — the same wire
    protocol autogen uses under the hood, so a test driving this drives the
    real inference path."""

    def __init__(self, base_url):
        self.base_url = base_url

    def chat(self, messages, *, max_tokens=512, temperature=0.0, timeout=180):
        r = requests.post(
            self.base_url.rstrip("/") + "/chat/completions",
            json={"messages": messages, "max_tokens": max_tokens,
                  "temperature": temperature},
            timeout=timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "llm_e2e: real-LLM end-to-end; SKIPS when no live local model is reachable",
    )


@pytest.fixture(scope="session")
def live_llm():
    """Session client over the live model, or skip the whole module."""
    base = live_model_base_url()
    if not base:
        pytest.skip(
            "no live local model on the canonical endpoint — start a dedicated "
            "one (trueflow ai_server_start, or Nunba with "
            "HEVOLVE_AGENT_ENGINE_ENABLED=0 to avoid daemon contention) and "
            "re-run `pytest tests/e2e/llm -m llm_e2e`")
    # enable the agentic_harness real-LLM judge for any test that uses it
    os.environ.setdefault("HEVOLVE_TEST_LLM_JUDGE", "1")
    return _RealLLM(base)
