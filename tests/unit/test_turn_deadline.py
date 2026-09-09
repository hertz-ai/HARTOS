"""Wall-clock bound on a single turn's GroupChat round-robin.

Measured 2026-08-12: a plain "hello" ran 1408s (23m28s).  max_round=10 did
fire, but ten rounds of a local 4B at ~90-140s each is 23 minutes.  The
existing reuse-loop bounds never saw it -- they guard the loop that runs
AFTER initiate_chat returns (`inside reuse while1` count was 0).

These tests pin the deadline helpers' contract.  The autogen side of it --
that a custom speaker_selection_method returning None ends the chat
gracefully -- is asserted in test_none_return_terminates_via_autogen below,
read straight from the installed autogen rather than assumed.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from reuse_recipe import (  # noqa: E402
    _begin_turn_deadline,
    _clear_turn_deadline,
    _turn_deadline_exceeded,
)


@pytest.fixture(autouse=True)
def _clean():
    _clear_turn_deadline('u_test')
    yield
    _clear_turn_deadline('u_test')
    os.environ.pop('HEVOLVE_TURN_MAX_SECONDS', None)


def test_unarmed_turn_is_never_expired():
    """No deadline armed => old unbounded behaviour, not an instant kill.

    A selector reached without _begin_turn_deadline must not be terminated
    by a missing or stale entry.
    """
    assert _turn_deadline_exceeded('never_armed') is False


def test_armed_turn_is_not_immediately_expired():
    _begin_turn_deadline('u_test')
    assert _turn_deadline_exceeded('u_test') is False


def test_turn_expires_once_past_the_deadline():
    os.environ['HEVOLVE_TURN_MAX_SECONDS'] = '0.05'
    _begin_turn_deadline('u_test')
    assert _turn_deadline_exceeded('u_test') is False
    time.sleep(0.1)
    assert _turn_deadline_exceeded('u_test') is True


def test_clearing_disarms():
    os.environ['HEVOLVE_TURN_MAX_SECONDS'] = '0.05'
    _begin_turn_deadline('u_test')
    time.sleep(0.1)
    assert _turn_deadline_exceeded('u_test') is True
    _clear_turn_deadline('u_test')
    # Agents are cached and reused across turns.  A stale deadline left behind
    # would kill the NEXT turn instantly, which is the failure mode the
    # finally: in get_agent_response exists to prevent.
    assert _turn_deadline_exceeded('u_test') is False


def test_a_selector_for_another_session_is_not_terminated():
    """Cached agents are shared; their closures capture their own user_prompt.

    An armed turn must never terminate a selector belonging to a different
    session that happens to run on this thread.
    """
    os.environ['HEVOLVE_TURN_MAX_SECONDS'] = '0.05'
    _begin_turn_deadline('u_test')
    time.sleep(0.1)
    assert _turn_deadline_exceeded('u_test') is True
    assert _turn_deadline_exceeded('some_other_session') is False


def test_concurrent_turns_do_not_disarm_each_other():
    """The bug this fixes, pinned.

    The speculative/expert-dispatch re-entry runs a SECOND turn under the same
    user_prompt on another thread.  With a session-keyed dict, the first turn
    to finish popped the shared entry and silently disarmed the one still
    running.  Deadline state is per-thread, so that cannot happen.
    """
    import threading as _t

    os.environ['HEVOLVE_TURN_MAX_SECONDS'] = '0.05'
    _begin_turn_deadline('u_test')
    time.sleep(0.1)
    assert _turn_deadline_exceeded('u_test') is True

    # A concurrent turn for the SAME session finishes and disarms itself.
    done = _t.Event()

    def _other_turn():
        _begin_turn_deadline('u_test')
        _clear_turn_deadline('u_test')
        done.set()

    t = _t.Thread(target=_other_turn)
    t.start()
    done.wait(5)
    t.join(5)

    # This thread's turn must still be expired -- the other turn's finally:
    # must not have reached across and disarmed it.
    assert _turn_deadline_exceeded('u_test') is True


def test_an_unarmed_thread_is_unaffected_by_another_threads_deadline():
    import threading as _t

    os.environ['HEVOLVE_TURN_MAX_SECONDS'] = '0.05'
    _begin_turn_deadline('u_test')
    time.sleep(0.1)

    result = {}

    def _fresh_thread():
        result['expired'] = _turn_deadline_exceeded('u_test')

    t = _t.Thread(target=_fresh_thread)
    t.start()
    t.join(5)
    assert result['expired'] is False


def test_rearming_extends():
    os.environ['HEVOLVE_TURN_MAX_SECONDS'] = '0.05'
    _begin_turn_deadline('u_test')
    time.sleep(0.1)
    assert _turn_deadline_exceeded('u_test') is True
    os.environ['HEVOLVE_TURN_MAX_SECONDS'] = '30'
    _begin_turn_deadline('u_test')
    assert _turn_deadline_exceeded('u_test') is False


def test_only_if_unset_does_not_extend_an_armed_clock():
    """The whole point of arming at /chat: the deeper arm must not restart it.

    get_agent_response arms with only_if_unset=True.  If that re-armed, the
    clock would reset partway through and hand back exactly the pre-turn
    overhead (~84s measured on real Discord traffic) that entry-point arming
    exists to include.
    """
    os.environ['HEVOLVE_TURN_MAX_SECONDS'] = '30'
    first = _begin_turn_deadline('u_test')
    time.sleep(0.05)
    second = _begin_turn_deadline('u_test', only_if_unset=True)
    assert second == first, 'only_if_unset must not restart the clock'


def test_only_if_unset_arms_when_nothing_is_running():
    """Callers that never go through /chat must still get a bound."""
    os.environ['HEVOLVE_TURN_MAX_SECONDS'] = '30'
    _clear_turn_deadline('u_test')
    armed = _begin_turn_deadline('u_test', only_if_unset=True)
    assert armed > time.time()
    assert _turn_deadline_exceeded('u_test') is False


def test_an_expired_clock_is_not_extended_either():
    """An already-expired turn must stay expired, not get a fresh budget."""
    os.environ['HEVOLVE_TURN_MAX_SECONDS'] = '0.05'
    _begin_turn_deadline('u_test')
    time.sleep(0.1)
    assert _turn_deadline_exceeded('u_test') is True
    os.environ['HEVOLVE_TURN_MAX_SECONDS'] = '300'
    _begin_turn_deadline('u_test', only_if_unset=True)
    assert _turn_deadline_exceeded('u_test') is True


def test_a_stale_deadline_would_expire_the_next_request_on_this_thread():
    """Why teardown_request must disarm.

    Flask reuses worker threads.  If a request ended without clearing, the
    next request on that thread would inherit an expired deadline and be
    killed on its first selector call.
    """
    os.environ['HEVOLVE_TURN_MAX_SECONDS'] = '0.05'
    _begin_turn_deadline('u_test')
    time.sleep(0.1)
    # Simulate the next request arriving on the same thread WITHOUT teardown.
    assert _turn_deadline_exceeded('u_test') is True, (
        'a stale armed deadline poisons the next request — teardown_request '
        'is what prevents this')
    # With teardown having run, the thread is clean again.
    _clear_turn_deadline('u_test')
    assert _turn_deadline_exceeded('u_test') is False


def test_default_is_150_seconds():
    os.environ.pop('HEVOLVE_TURN_MAX_SECONDS', None)
    deadline = _begin_turn_deadline('u_test')
    assert 145 <= deadline - time.time() <= 150


def test_none_return_terminates_via_autogen():
    """The mechanism the bound relies on, asserted against installed autogen.

    A custom speaker_selection_method returning None must raise
    NoEligibleSpeaker, and run_chat must catch it and break -- otherwise
    returning None from state_transition would surface as an exception and
    the turn would be answered with a traceback instead of a reply.
    """
    import inspect
    from autogen.agentchat import groupchat as gc

    src = inspect.getsource(gc.GroupChat._prepare_and_select_agents)
    assert 'NoEligibleSpeaker' in src
    assert 'returned None' in src

    run_chat_src = inspect.getsource(gc.GroupChatManager.run_chat)
    assert 'except NoEligibleSpeaker' in run_chat_src
    # The handler must be a plain break: messages already appended survive and
    # the caller's reply-extraction still runs.
    handler = run_chat_src.split('except NoEligibleSpeaker')[1]
    assert 'break' in handler.split('\n')[1:4][-1] or 'break' in handler[:200]
