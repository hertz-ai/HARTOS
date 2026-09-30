"""A tool execution must say WHOSE it was.

MEASURED LIVE 2026-09-09 22:04-22:13.  Agent 18088688973's action 1 named
``execute_windows_or_android_command``.  The evidence available afterwards was:

    22:05:51 [FAB-GUARD] action 1 ... unrun=['execute_windows_or_android_command']
             for session: 6c2dc0fc-...-f7466ff63f29_18088688973
    22:06:08 INSIDE execute_windows_or_android_command

FAB-GUARD is session-qualified but is a SNAPSHOT taken at gate time -- both its
readings (22:05:51, 22:06:02) predate the 22:06:08 execution, so "unrun" was
true when asked and stale by the time the tool ran.  The execution line that
would settle it carries NO session, and FOUR sessions were interleaved in that
window (18088688973, 65708210992, 70264903070, 52612946585).  So "did this
agent's named tool actually run?" -- the exact question the whole live-drive
verification turns on -- was UNANSWERABLE from the logs.

This is the same defect class as HARTOS 2eaec2be4 (FAB-GUARD carried no
session) at a different marker, and the fix is the same one: qualify it at the
PRODUCER.  ``core/tool_logging.py`` is the canonical chokepoint (#509) --
~40 tools across core/agent_tools.py, integrations/channels/agent_tools.py and
reuse_recipe.py's 48 decorated tools all pass through it -- and it ALREADY
reads the identity two lines above, in ``_emit_tool_call_stage``, via
``hartos.threadlocal.thread_local_data``.  So this adds no machinery and no
second path; it spends context that is already in scope.

These are BEHAVIOURAL tests: they decorate a real function, set the real
thread-local, invoke it, and read the real emitted records.  A source-text
assertion would pass on a line that never executes.

    python -m pytest tests/unit/test_tool_execution_is_session_qualified.py --noconftest -q
"""
import logging

import pytest

pytest.importorskip("core.tool_logging")
from core.tool_logging import log_tool_execution  # noqa: E402

try:
    from hartos.threadlocal import thread_local_data
except Exception:  # pragma: no cover - import shape differs outside the app
    thread_local_data = None


@pytest.fixture
def caplog_tools(caplog):
    caplog.set_level(logging.INFO, logger="agent_logger")
    return caplog


def _set_ctx(user_id, prompt_id):
    """Populate the thread-local the decorator already consumes."""
    if thread_local_data is None:
        pytest.skip("hartos.threadlocal unavailable in this environment")
    for setter, val in (("set_user_id", user_id), ("set_prompt_id", prompt_id)):
        fn = getattr(thread_local_data, setter, None)
        if fn is None:
            pytest.skip(f"thread_local_data has no {setter}")
        fn(val)


def _messages(caplog):
    return [r.getMessage() for r in caplog.records]


class TestTheExecutionLineNamesItsSession:

    def test_success_line_carries_the_session(self, caplog_tools):
        _set_ctx("6c2dc0fc-7c93-4fe0-973e-f7466ff63f29", "18088688973")

        @log_tool_execution
        def execute_windows_or_android_command(cmd: str) -> str:
            return "ran"

        execute_windows_or_android_command("dir")

        success = [m for m in _messages(caplog_tools)
                   if "TOOL EXECUTION SUCCESS" in m]
        assert success, "no TOOL EXECUTION SUCCESS line was emitted at all"
        assert any("18088688973" in m for m in success), (
            "the tool-execution line does not name the session, so with several "
            "agents interleaved in one server.log you cannot tell whose tool "
            "ran. Live 22:06:08 this made an execution unattributable while "
            "FAB-GUARD's stale snapshot said 'unrun'. Expected the existing "
            "'for session: <user>_<prompt_id>' suffix.\nGot: %s" % success)

    def test_it_uses_the_established_for_session_idiom(self, caplog_tools):
        """Do not invent a second vocabulary for the same fact."""
        _set_ctx("6c2dc0fc-7c93-4fe0-973e-f7466ff63f29", "18088688973")

        @log_tool_execution
        def google_search(q: str) -> str:
            return "hits"

        google_search("anything")

        success = [m for m in _messages(caplog_tools)
                   if "TOOL EXECUTION SUCCESS" in m]
        assert any("for session:" in m for m in success), (
            "use the codebase's existing 'for session: {user}_{prompt_id}' "
            "suffix -- the same one 'Retrieved current_action_id' and "
            "[FAB-GUARD] use -- so ONE grep attributes every marker")


class TestItDegradesInsteadOfCrashing:
    """A tool must still run when there is no chat context (daemon ticks)."""

    def test_no_context_still_executes_and_logs(self, caplog_tools):
        _set_ctx(None, None)

        @log_tool_execution
        def save_data_in_memory(k: str) -> str:
            return "saved"

        assert save_data_in_memory("x") == "saved", (
            "a missing thread-local must never block the tool")
        assert any("TOOL EXECUTION SUCCESS" in m
                   for m in _messages(caplog_tools)), (
            "the success line must still be emitted without a session")


class TestThePayloadSurvives:
    """Attribution is ADDED, never swapped in for the existing evidence."""

    def test_tool_name_and_latency_remain(self, caplog_tools):
        _set_ctx("6c2dc0fc-7c93-4fe0-973e-f7466ff63f29", "18088688973")

        @log_tool_execution
        def crawl4ai_crawl(url: str) -> str:
            return "page"

        crawl4ai_crawl("http://example.invalid")

        success = [m for m in _messages(caplog_tools)
                   if "TOOL EXECUTION SUCCESS" in m]
        assert any("crawl4ai_crawl" in m for m in success), (
            "the tool name is what makes the line greppable per tool")
        assert any("latency_ms=" in m for m in success), (
            "latency_ms is existing evidence; adding the session must not "
            "displace it")
