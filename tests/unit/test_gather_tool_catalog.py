"""The autonomous gather/plan author must KNOW its full tool set so it plans
with the right tool per step (recall -> get_chat_history, search -> google_search,
code -> execute_coding_task) instead of forcing every step into GUI automation.

Behavioural: call the real select_autonomous_tool_catalog() and assert the catalog
is returned by default (planner sees the tools) and suppressed only on explicit
opt-out. The prompt CONTENT + the model's planning are verified live; this guards
the regression-able default-on policy. No grep/source-shape assertions.
"""
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# gather_agentdetails type-annotates module-level signatures with
# autogen.AssistantAgent (evaluated at import time), so importing it crashes
# when autogen is absent (CI). Skip cleanly, matching the suite-wide pattern.
pytest.importorskip('autogen', reason='autogen not installed')

from hartos import gather_agentdetails as ga  # noqa: E402

_ENV = 'HEVOLVE_AUTONOMOUS_GATHER_TOOL_MAP'


def _set(v):
    if v is None:
        os.environ.pop(_ENV, None)
    else:
        os.environ[_ENV] = v


def test_catalog_on_by_default():
    old = os.environ.get(_ENV)
    try:
        _set(None)
        assert ga.select_autonomous_tool_catalog() == ga.AUTONOMOUS_TOOL_CATALOG
    finally:
        _set(old)


def test_catalog_suppressed_on_opt_out():
    old = os.environ.get(_ENV)
    try:
        for v in ('0', 'false', 'off', 'no', 'OFF', ' Off '):
            _set(v)
            assert ga.select_autonomous_tool_catalog() == "", v
    finally:
        _set(old)


def test_catalog_on_for_any_non_off_value():
    # Legacy '1'/'on' and empty/garbage keep it ON — only explicit off disables.
    old = os.environ.get(_ENV)
    try:
        for v in ('1', 'true', 'on', 'yes', '', 'garbage'):
            _set(v)
            assert ga.select_autonomous_tool_catalog() == ga.AUTONOMOUS_TOOL_CATALOG, v
    finally:
        _set(old)


def test_catalog_lists_the_recall_and_web_tools():
    # The catalog the planner receives must name the dedicated tools that
    # replace blind GUI automation — get_chat_history (recall) + google_search.
    cat = ga.AUTONOMOUS_TOOL_CATALOG
    assert 'get_chat_history' in cat
    assert 'google_search' in cat
    assert 'execute_coding_task' in cat


if __name__ == '__main__':
    test_catalog_on_by_default(); print('PASS on-by-default')
    test_catalog_suppressed_on_opt_out(); print('PASS opt-out suppresses')
    test_catalog_on_for_any_non_off_value(); print('PASS on for non-off values')
    test_catalog_lists_the_recall_and_web_tools(); print('PASS catalog names the tools')
    print('OK 4/4')
