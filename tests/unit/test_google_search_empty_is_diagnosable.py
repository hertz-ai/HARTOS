"""google_search must never report failure as an indistinguishable empty list.

MEASURED LIVE 2026-09-07 on the installed build, from the request bodies the
StatusVerifier actually received (llm_outbound.jsonl, identified by its own
system message):

    tool                 seen  EMPTY  empty%
    google_search         158     79   50.0%
    get_system_health      95      0    0.0%
    send_message_to_user   79      0    0.0%
    ...every other tool     -      0    0.0%

Half of all google_search calls hand back the two-character payload '[]'.
No other tool does this.

WHY THAT IS A DEFECT AND NOT JUST A QUIET DAY.  top5_results has three
distinct failure exits and every one returns the SAME bare []:

    :7153  search is None      -- GOOGLE_API_KEY / GOOGLE_CSE_ID missing
    :7161  search.results()    -- raised (network, quota/429, auth)
    :7169  no 'link' in any result   <-- and this one logs NOTHING

So "the API key is missing", "we were rate-limited", and "the web genuinely
had no answer" are indistinguishable to every consumer, including the model.

WHICH EXIT IS ACTUALLY FIRING, measured: 47 'INSIDE google search'
invocations in the live log and ZERO occurrences of any of the three logged
warnings.  By elimination it is :7169 -- the silent one.  A tool that fails
without logging cannot be diagnosed from a bug report.

THE DOWNSTREAM DAMAGE, all previously measured and all one cause:
  * @log_tool_execution records TOOL EXECUTION SUCCESS (it returned normally)
  * FAB-GUARD scores the action 'executed', unrun=[]
  * StatusVerifier sees a REAL (non-placeholder) but empty result and says
    'pending' -- which is CORRECT on that evidence; the work truly did not
    happen.  The action never advances.
  * asked for a *cited* brief with zero sources, the model invents URLs
    (#786: ai.google/discoveries, x.ai/grok-1 -- zero occurrences on the wire)

CONVENTION THIS RESTORES.  google_search's own neighbour in the same
registry returns a sentence, not an empty container:
    core/agent_tools.py:1153
        return "No matching visual/screen descriptions found in the given
                time range."

    python -m pytest tests/unit/test_google_search_empty_is_diagnosable.py --noconftest -q
"""
import inspect
import re
import pytest


def _src():
    """Source of top5_results only -- the function under test.

    Read at source level rather than executed: hart_intelligence_entry is a
    very large module whose import pulls langchain + the whole tool registry,
    and the defect IS the literal `return []`, which source reading catches
    exactly.  Same technique as tests/unit/test_reuse_completion_terminates.py.
    """
    path = __file__.rsplit("tests", 1)[0] + "hart_intelligence_entry.py"
    text = open(path, encoding="utf-8", errors="replace").read()
    start = text.index("def top5_results(")
    # up to the next top-level def/class
    m = re.search(r"\n(?:def |class )", text[start + 10:])
    return text[start:start + 10 + (m.start() if m else len(text))]


class TestFailureIsDistinguishable:

    def test_no_bare_empty_list_return(self):
        """Every exit must say something; none may return a naked []."""
        hits = re.findall(r"^\s*return \[\]\s*$", _src(), re.MULTILINE)
        assert not hits, (
            f"{len(hits)} exit(s) still `return []`, making 'no API key', "
            f"'rate-limited' and 'the web had nothing' indistinguishable to "
            f"the model, to FAB-GUARD and to StatusVerifier")

    def test_the_silent_exit_now_logs(self):
        """:7169 was the ONLY exit with no diagnostic, and it is the one firing.

        47 live invocations produced zero warnings from the other two exits.
        """
        src = _src()
        # Window forward from the branch itself.  Do NOT slice to the next
        # literal "return": the diagnostic text legitimately contains the word
        # "returning", so that cut lands inside the comment and hides the very
        # logger line being asserted (my first version did exactly that).
        i = src.index("if not top_2_search_res_link")
        branch = src[i:i + 1200]
        assert "logger" in branch, (
            "the no-link exit must log what search.results() actually returned "
            "— it is the exit that fires live and it used to say nothing")

    def test_every_failure_exit_names_itself(self):
        """A consumer must be able to tell the three failures apart."""
        src = _src()
        for marker in ("GOOGLE_API_KEY", "search.results"):
            assert marker in src, f"{marker!r} missing from the diagnostics"
        # Each failure return should carry text, not an empty container.
        returns = re.findall(r"^\s*return (.+)$", src, re.MULTILINE)
        empties = [r for r in returns if r.strip() in ("[]", "{}", "''", '""')]
        assert not empties, f"bare empty returns remain: {empties}"


class TestSuccessPathUntouched:

    def test_still_returns_final_res_on_success(self):
        """The fix must not disturb the working path."""
        assert "return final_res" in _src()

    def test_dead_fallback_is_recorded(self):
        """`if len(final_res) == 0` after an unconditional append is vacuous.

        final_res.append(...) runs on every path that reaches it, so the
        search.results(query, 4) fallback below it is unreachable.  NOT fixed
        in the same change as the diagnostics -- it is a separate defect and
        gets its own commit -- but it must be visibly marked so the next
        reader does not trust it.
        """
        src = _src()
        if "if len(final_res) == 0" in src:
            i = src.index("if len(final_res) == 0")
            # 900, not 400: the marking comment is ~600 chars, and a 400-char
            # lookback cut it off — the same too-narrow-window error this
            # suite exists to catch, made by the suite itself.
            window = src[max(0, i - 900):i]
            assert ("unreachable" in window.lower()
                    or "vacuous" in window.lower()
                    or "dead" in window.lower()), (
                "the unreachable len(final_res)==0 fallback must be marked, "
                "otherwise it reads as live error handling")
