"""The registry-exposed whisper tool must actually transcribe.

``WhisperTool.register_functions`` publishes the tool with
``base_url='inprocess://whisper'``.  ``requests`` has no connection
adapter for an ``inprocess://`` scheme, so with no ``native_handler`` on
the endpoints every agent-side call through ``service_tool_registry``
(the autogen / langchain surface that ``RuntimeToolManager
.get_autogen_tools`` hands out) falls through to
``registry.create_endpoint_function``'s HTTP branch and returns

    {"success": false,
     "error": "No connection adapters were found for
               'inprocess://whisper/transcribe'"}

— measured on this box 2026-09-21 by calling
``RuntimeToolManager().get_autogen_tools()['whisper_transcribe']
(audio_path=...)`` after a successful ``setup_tool('whisper')``.  The
in-process module entry point ``whisper_tool.whisper_transcribe`` (used
directly by hart_intelligence_entry, model_bus_service, the channels
audio path, …) worked in the same run, so this broke the REGISTRY
surface only.

Every other in-process service tool already routes through
``native_handler`` — ``crawl4ai_tool._native_crawl``,
``gh_pr_tool.gh_pr_open``, ``seo_audit_tool.seo_audit_score`` — and
``registry.create_endpoint_function`` checks for it before it ever
builds a URL.  Whisper was the only in-process tool that did not supply
one.

These tests import the real registration path and call the real
generated closure; only the STT entry points and the HTTP pool are
mocked, so a regression to the HTTP branch fails loudly.
"""

import json

import pytest

from integrations.service_tools import whisper_tool
from integrations.service_tools.registry import service_tool_registry
from integrations.service_tools.whisper_tool import WhisperTool


@pytest.fixture
def registered(monkeypatch):
    """Register whisper for real, and make any HTTP attempt an error.

    If the endpoint ever loses its native_handler again, the closure
    takes the ``pooled_post`` branch and this fixture turns that silent
    JSON error into a loud failure.
    """
    def _explode(*args, **kwargs):  # pragma: no cover - only on regression
        raise AssertionError(
            "registry took the HTTP branch for an in-process tool: "
            f"args={args!r} kwargs={kwargs!r}"
        )

    import core.http_pool as http_pool
    monkeypatch.setattr(http_pool, "pooled_post", _explode)
    monkeypatch.setattr(http_pool, "pooled_get", _explode)

    saved = service_tool_registry._tools.get("whisper")
    WhisperTool.register_functions()
    yield service_tool_registry
    if saved is not None:
        service_tool_registry._tools["whisper"] = saved
    else:
        service_tool_registry._tools.pop("whisper", None)


def test_transcribe_endpoint_reaches_whisper_transcribe(registered, monkeypatch):
    """The generated whisper_transcribe closure must call the STT entry
    point with the caller's audio_path + language and return its JSON."""
    seen = {}

    def fake_transcribe(audio_path, language=None):
        seen["audio_path"] = audio_path
        seen["language"] = language
        return json.dumps({"text": "hello from stt", "language": "en"})

    monkeypatch.setattr(whisper_tool, "whisper_transcribe", fake_transcribe)

    fn = registered.get_all_tool_functions()["whisper_transcribe"]
    out = fn(audio_path="/tmp/a.wav", language="en")

    assert seen == {"audio_path": "/tmp/a.wav", "language": "en"}
    assert json.loads(out)["text"] == "hello from stt"


def test_transcribe_endpoint_language_is_optional(registered, monkeypatch):
    """language is optional in params_schema; omitting it must not raise
    and must reach the entry point as None (auto-detect)."""
    seen = {}

    def fake_transcribe(audio_path, language=None):
        seen["audio_path"] = audio_path
        seen["language"] = language
        return json.dumps({"text": "auto", "language": "fr"})

    monkeypatch.setattr(whisper_tool, "whisper_transcribe", fake_transcribe)

    fn = registered.get_all_tool_functions()["whisper_transcribe"]
    out = fn(audio_path="/tmp/b.wav")

    assert seen == {"audio_path": "/tmp/b.wav", "language": None}
    assert json.loads(out)["language"] == "fr"


def test_detect_language_endpoint_reaches_whisper_detect_language(
        registered, monkeypatch):
    """The detect_language endpoint must reach its own entry point."""
    seen = {}

    def fake_detect(audio_path):
        seen["audio_path"] = audio_path
        return json.dumps({"language": "de", "probability": 0.97})

    monkeypatch.setattr(whisper_tool, "whisper_detect_language", fake_detect)

    fn = registered.get_all_tool_functions()["whisper_detect_language"]
    out = fn(audio_path="/tmp/c.wav")

    assert seen == {"audio_path": "/tmp/c.wav"}
    assert json.loads(out)["language"] == "de"


def test_both_endpoints_declare_a_native_handler(registered):
    """Mirrors tests/unit/test_gh_pr_tool.py's native_handler assertion —
    the registry only skips the URL branch when this key is present."""
    endpoints = registered._tools["whisper"].endpoints
    assert endpoints["transcribe"]["native_handler"] is whisper_tool._native_transcribe
    assert (endpoints["detect_language"]["native_handler"]
            is whisper_tool._native_detect_language)
