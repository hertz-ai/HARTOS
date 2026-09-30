"""Guard: the StatusVerifier must judge on evidence, not on its own powers.

Live root cause, measured 2026-09-06 20:06-20:17 on the installed build
(agent 89555447799, drive 6).  `GOT COMPLETED FOR ACTION` was 0 for the
whole run, and the selector parsed 7 verdicts whose statuses were
pending x5, open, error, incomplete -- never once "completed".  So no
action can ever be marked done, no matter how well it ran.

The verifier states its own reason verbatim:

    "The action requires a google_search tool to be executed by the
     assistant to retrieve data from decentralized AI platform
     communities. Since the current agent cannot perform this search,
     the status is pending for the helper agent to complete the search
     query and extract the structured summary."

    "The subtask requires executing a google_search tool ... Since the
     current agent cannot perform this search, the status is pending for
     the helper agent to execute the search and report back the findings."

In that SAME window agent_system.log (core.tool_logging, the canonical
execution record) shows google_search 8 START / 8 SUCCESS / 0 ERROR, plus
send_message_to_roles 4/4, execute_windows_or_android_command 2/2,
save_data_in_memory 2/2, get_data_by_key 2/2 -- 18 successes, 0 errors.
The search it calls impossible had already happened, eight times.

The mechanism is the system message.  It correctly says

    "Report status only-do not perform actions yourself and do not try
     calling any functions/tools."

and the model reasons FROM that instruction to the wrong conclusion: it
cannot call google_search, therefore the step is "pending".  Nothing in
the prompt tells it that a tool RESULT already sitting in the conversation
is proof the step ran, so "I can't do this" silently becomes "this isn't
done" and the action defers to "the helper agent" forever.

This is a prompt defect, so the honest test is a drift guard on the
instruction text -- it pins that the correction is present and cannot be
dropped by a future edit.  It is NOT behavioural proof; the behavioural
proof is a live run producing a "completed" verdict, which is tracked
separately.  Stating that plainly because a guard that looks behavioural
but is not is how a vacuous guard gets shipped.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

_SRC = os.path.join(os.path.dirname(__file__), '..', '..',
                    'hartos', 'reuse_recipe.py')


def _status_verifier_system_message():
    """The StatusVerifier's system_message literal, from source.

    Read from source rather than by constructing the agent: building it
    needs autogen + a live llm_config + a Flask app context, none of which
    this invariant depends on.
    """
    with open(_SRC, encoding='utf-8') as fh:
        src = fh.read()
    i = src.index('name="StatusVerifier"')
    j = src.index('system_message=', i)
    # the literal runs to the is_termination_msg kwarg that follows it
    k = src.index('is_termination_msg=', j)
    return src[j:k]


def test_verifier_is_told_to_judge_from_tool_results_in_the_conversation():
    """The load-bearing instruction: evidence, not self-capability."""
    msg = _status_verifier_system_message().lower()
    assert 'tool result' in msg or 'tool results' in msg, (
        "the StatusVerifier is never told that a tool RESULT already in the "
        "conversation proves the step ran.  Measured live 2026-09-06: it "
        "answered 'pending ... since the current agent cannot perform this "
        "search' for an action whose google_search had already executed 8/8 "
        "successfully, and GOT COMPLETED FOR ACTION stayed 0 all run.")


def test_verifier_is_forbidden_from_pending_because_it_cannot_call_tools():
    """Pin the exact wrong inference, so it cannot come back.

    Separate from the test above so the failure message says WHICH half is
    missing: the positive rule (judge on results) or the negative one
    (never justify pending by your own inability).
    """
    msg = _status_verifier_system_message().lower()
    has_negative = (
        'cannot call' in msg or 'cannot perform' in msg
        or 'inability' in msg or 'unable to call' in msg)
    assert has_negative, (
        "nothing forbids the verifier from answering 'pending' with the "
        "reason that IT cannot run the tool.  That inference — 'I can't "
        "search, therefore it isn't searched' — is the measured defect; the "
        "verifier's own capability says nothing about whether the Assistant "
        "already called the tool.")


def test_the_report_status_only_rule_is_kept():
    """The fix must not 'work' by letting the verifier call tools.

    Non-vacuity: deleting the do-not-call rule would also stop the wrong
    inference, and would be a far worse bug (a verifier that performs the
    work it is meant to judge).  Pin the rule stays.
    """
    msg = _status_verifier_system_message().lower()
    assert 'do not perform actions yourself' in msg, (
        'the verifier must still be forbidden from performing actions — the '
        'fix is to change what it INFERS from that limit, not to remove it')
    assert re.search(r'do not try calling any functions', msg), (
        'the do-not-call-tools rule must remain')


def test_completed_remains_conditional_on_success():
    """A verifier that always says completed is as useless as never.

    The prompt's "Only mark an action as Completed if all the steps are
    successfully completed" is the counterweight to the new evidence rule;
    losing it would turn every action into a fabricated completion, which
    is exactly what the fabrication gate exists to catch.
    """
    msg = _status_verifier_system_message().lower()
    assert 'only mark an action as "completed"' in msg, (
        'the success precondition for "completed" must survive the fix')
