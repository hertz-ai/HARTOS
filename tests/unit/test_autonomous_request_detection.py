"""dispatch.is_current_request_autonomous() — the signal that picks the CREATE
assistant's AUTONOMOUS vs INTERACTIVE system prompt (#85 root cause).

Live 2026-06-04: a daemon coding goal ran with the INTERACTIVE assistant prompt
("you may ask the user clarifying questions"), asked one, and stalled forever
because no user was there to answer. The pipeline now derives autonomy from the
thread-local request_id via this helper. Behavioural tests: set the real
thread-local, call the real helper, assert the verdict. No grep tests.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hartos.threadlocal import thread_local_data
from integrations.agent_engine.dispatch import is_current_request_autonomous


def _set_rid(rid):
    thread_local_data.set_request_id(rid)


def teardown_function(_):
    thread_local_data.set_request_id('')


def test_daemon_request_is_autonomous():
    _set_rid('daemon_2114453493')          # the daemon stamps this prefix
    assert is_current_request_autonomous() is True


def test_genuine_user_request_is_interactive():
    _set_rid('user_42_173000')             # a real user turn
    assert is_current_request_autonomous() is False


def test_missing_request_id_defaults_interactive():
    _set_rid('')                           # outside a request context
    assert is_current_request_autonomous() is False


# -- audit row H10: the owner answers for a turn id the caller holds --
# core.agent_tools.recall_runs_on_a_background_turn used to carry its own copy
# of this rule (a non-empty id that is not a person's) so it could ask about
# the id with_llm_context bound instead of the thread-local one.

def test_a_given_goal_id_is_autonomous_whatever_the_thread_holds():
    _set_rid('user_42_173000')
    assert is_current_request_autonomous('daemon_2114453493') is True


def test_a_given_persons_id_is_interactive_whatever_the_thread_holds():
    _set_rid('daemon_2114453493')
    assert is_current_request_autonomous('user_42_173000') is False


def test_an_empty_given_id_reads_the_thread_as_before():
    _set_rid('daemon_2114453493')
    assert is_current_request_autonomous('') is True
    assert is_current_request_autonomous(None) is True
    _set_rid('user_42_173000')
    assert is_current_request_autonomous('') is False
    _set_rid('')
    assert is_current_request_autonomous('') is False
