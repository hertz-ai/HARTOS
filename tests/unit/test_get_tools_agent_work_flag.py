"""get_tools leaves the agent-work tools out when a turn asks for that.

Task #120.  Create_Agent and Agentic_Router are the two tools that turn a
conversation into agent work: an interview, an autonomous build saved under
the caller's id, or a Plan Mode consent question.  A metered Hive API call is
made by a program that can answer none of those, so its self-POST sends
``agent_tools`` False (tests/unit/test_metered_chat_is_a_conversation.py pins
the wire and that /chat hands the flag to get_ans) and
``get_tools(..., agent_tools=False)`` must not offer them.  The default is
unchanged: every person's turn keeps both.

Two layers.  The filter itself, ``_without_agent_work_tools``, is driven on
duck-typed tools and runs anywhere.  The exhaustive branch of the real
``get_tools`` needs langchain_classic to build its Tool objects, which the
dev box's venv lacks (its ``Tool`` shim raises), so that test runs where the
package is installed (CI) and is skipped here.  The memoised first-tools
branch applies the same filter and keys its cache by the flag (read; its
build reaches the heavy registries and is not driven).
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


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


def _names(tools):
    return {getattr(t, 'name', None) for t in tools}


def test_the_filter_withholds_exactly_the_agent_work_pair(hie):
    tools = [SimpleNamespace(name=n) for n in
             ('Calculator', 'Create_Agent', 'FULL_HISTORY', 'Agentic_Router',
              'List_Agents')] + [object()]          # a tool with no name is kept
    kept = hie._without_agent_work_tools(tools)
    assert _names(kept) == {'Calculator', 'FULL_HISTORY', 'List_Agents', None}
    assert hie._AGENT_WORK_TOOL_NAMES == AGENT_WORK
    # Order and identity of the survivors are untouched.
    assert [t for t in tools if getattr(t, 'name', None) not in AGENT_WORK] == kept


def test_the_default_tool_set_still_carries_both_agent_work_tools(hie):
    pytest.importorskip('langchain_classic')
    names = _names(hie.get_tools(None, is_first=False))
    assert AGENT_WORK <= names, names


def test_a_turn_that_declines_agent_work_gets_neither(hie):
    pytest.importorskip('langchain_classic')
    names = _names(hie.get_tools(None, is_first=False, agent_tools=False))
    assert not (AGENT_WORK & names), names
    # Only those two are withheld: the rest of the exhaustive set is intact.
    full = _names(hie.get_tools(None, is_first=False))
    assert names == full - AGENT_WORK
