"""The agent-work tools stay out of a turn that declined them, on every path.

Task #120.  Create_Agent and Agentic_Router (core.constants.AGENT_WORK_TOOL_NAMES)
are the two tools that turn a conversation into agent work: an interview, an
autonomous build saved under the caller's id, or a Plan Mode consent
question.  A metered Hive API call is made by a program that can answer none
of those, so its self-POST sends ``agent_tools`` False.  /chat records that
on the request's thread-local (thread_local_data.set_agent_tools) and
get_tools reads it whenever a caller passes no explicit value, so the three
callers of a turn agree: get_ans's two calls, and the model wrapper's prompt
rebuild on later steps (CustomGPT._call), which the review of 5a66a3308
measured re-listing both tools in the prompt text when the flag travelled as
a keyword.

The exhaustive branch builds langchain Tool objects and this box's venv has
no langchain_classic (the module's Tool is a shim that raises), so these
tests swap in a duck-typed Tool, the way that review's own probe did, and
drive the real get_tools and the real _call.  With langchain installed (CI)
the same tests run against the real Tool.
"""
import os
import sys
import tempfile
from types import SimpleNamespace

os.environ['HEVOLVE_DB_PATH'] = ':memory:'
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

import pytest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

AGENT_WORK = {'Create_Agent', 'Agentic_Router'}


class _Tool:
    """A duck-typed langchain Tool: name, description, func."""
    def __init__(self, name, func=None, description='', **_):
        self.name, self.func, self.description = name, func, description


class _Stop(BaseException):
    """Raised by the stubbed model POST: the rebuilt prompt is on its way to
    the model.  BaseException, because _call's own except clauses swallow
    Exception and post again."""


class _Recorder:
    """Stands in for app.logger: keeps every info line (the rebuilt prompt is
    logged at INFO before the POST), ignores the rest."""
    def __init__(self):
        self.lines = []

    def info(self, msg, *args, **_):
        self.lines.append(str(msg) % args if args else str(msg))

    def __getattr__(self, _name):
        return lambda *a, **k: None


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


@pytest.fixture
def tools_buildable(hie, monkeypatch):
    """The exhaustive branch runs here: Tool is duck-typed where langchain is
    absent, the google-search loader answers nothing (it needs a key), and
    the thread-local starts and ends on a turn that allows everything."""
    if not getattr(hie, '_LANGCHAIN_OK', True):
        monkeypatch.setattr(hie, 'Tool', _Tool)
    monkeypatch.setattr(hie, '_safe_load_google_search', lambda: [])
    hie.thread_local_data.set_agent_tools(True)
    yield
    hie.thread_local_data.set_agent_tools(True)


def _names(tools):
    return {getattr(t, 'name', None) for t in tools}


def test_the_filter_withholds_exactly_the_agent_work_pair(hie):
    tools = [SimpleNamespace(name=n) for n in
             ('Calculator', 'Create_Agent', 'FULL_HISTORY', 'Agentic_Router',
              'List_Agents')] + [object()]          # a tool with no name is kept
    kept = hie._without_agent_work_tools(tools)
    assert _names(kept) == {'Calculator', 'FULL_HISTORY', 'List_Agents', None}
    assert hie.AGENT_WORK_TOOL_NAMES == AGENT_WORK
    # Order and identity of the survivors are untouched.
    assert [t for t in tools if getattr(t, 'name', None) not in AGENT_WORK] == kept


def test_the_default_exhaustive_set_still_carries_both(hie, tools_buildable):
    names = _names(hie.get_tools(None, is_first=False))
    assert AGENT_WORK <= names, names


def test_an_explicit_decline_withholds_only_the_pair(hie, tools_buildable):
    full = _names(hie.get_tools(None, is_first=False))
    names = _names(hie.get_tools(None, is_first=False, agent_tools=False))
    assert names == full - AGENT_WORK, names


def test_a_caller_that_passes_no_flag_reads_the_turns_thread_local(hie, tools_buildable):
    """The rebuild in CustomGPT._call calls get_tools(intent) with no flag;
    the turn's thread-local is what it must read, both ways."""
    hie.thread_local_data.set_agent_tools(False)
    assert not (AGENT_WORK & _names(hie.get_tools(None, is_first=False)))
    hie.thread_local_data.set_agent_tools(True)
    assert AGENT_WORK <= _names(hie.get_tools(None, is_first=False))


def test_the_rebuilt_prompt_on_a_later_step_omits_the_pair(hie, tools_buildable, monkeypatch):
    """The second and later model calls rebuild the tool list into the prompt
    text between <TOOLS_START> and <TOOLS_END> when the intent moved, and log
    the rebuilt prompt before posting it.  On a turn that declined agent work
    the rebuilt text must not name the pair (the review of 5a66a3308
    measured it naming both) and must still carry the rest of the set."""
    monkeypatch.setattr(hie, '_g12_kick_off_student', lambda prompt: None)
    monkeypatch.setattr(hie, 'publish_chat_stage', lambda *a, **k: None)
    monkeypatch.setattr(hie, 'GPT_API', 'http://localhost:1/v1/chat/completions')

    def stop(*a, **k):
        raise _Stop()
    monkeypatch.setattr(hie, '_pooled_post_with_refusal_check', stop)
    log = _Recorder()
    monkeypatch.setattr(hie.app, 'logger', log)
    hie.thread_local_data.set_agent_tools(False)
    hie.thread_local_data.set_global_intent(None)
    hie.thread_local_data.set_req_token_count(0)
    try:
        model = hie.CustomGPT(casual_conv=False)      # langchain's pydantic LLM
    except TypeError:
        model = hie.CustomGPT()                       # no langchain: the base is object
        model.casual_conv = False
    model.count, model.previous_intent, model.call_gpt4 = 1, 'google_search', 0
    with pytest.raises(_Stop):
        model._call('header <TOOLS_START>\n> Old: tools\n<TOOLS_END> tail')
    rebuilt = [line for line in log.lines if '> FULL_HISTORY:' in line]
    assert rebuilt, 'the rebuilt prompt was not logged'
    text = rebuilt[-1]
    assert text.startswith('header ') and text.endswith(' tail')
    assert '<TOOLS_START>' not in text
    assert '> Create_Agent:' not in text and '> Agentic_Router:' not in text
    assert '> List_Agents:' in text        # the rest of the set is still there
