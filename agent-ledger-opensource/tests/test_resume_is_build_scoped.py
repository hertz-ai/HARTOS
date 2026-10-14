"""A session is resumed only by the build that left it unfinished.

THE LIVE FAILURE (2026-10-10 14:47 IST, installed Nunba, agent 54, user 10202):

    create_ledger_from_actions: resuming in-flight session 10202_54_1791464434498
    Loaded 20 tasks from ledger backend
    [USER-INPUT-GATE] Keeping action 2 blocked: durable resume from a genuine
    user reply failed

Agent 54 had just been rebuilt through the gather interview: one flow of
THREE actions.  Its CREATE turn resumed the session an earlier build of agent
54 had left on 2026-10-08: TWENTY actions with other texts, its action_2
FAILED.  Task ids are positions, so the new build's action_2 was that old
FAILED task.  The user-input block refuses a task that is not in progress, so
the action asked the person a question no answer could release.

A build is its list of actions.  A session left unfinished by another list is
another build's and is not resumed; the same build still resumes its own.
"""

from agent_ledger.core import (Task, TaskStatus, TaskType,
                               _find_resumable_session,
                               create_ledger_from_actions)

AGENT, USER = '54', '10202'
OLD_SESSION = '10202_54_1000'
# The live shape, shortened: the earlier build's steps and the new build's
# share a first step and differ after it.
OLD_BUILD = ['Call get_user_id, then get_data_by_key.',
             'Write the reply to the learner from their latest message.',
             'Call save_data_in_memory.',
             'Write the reply to the learner again.',
             'Call save_data_in_memory again.']
NEW_BUILD = ['Call get_user_id, then get_data_by_key.',
             'Write the reply to the learner yourself, never through a tool.',
             'Call save_data_in_memory.']


def _leave_unfinished(tmp_path, monkeypatch, actions):
    # agent_data/ resolves against the CWD; isolate it from the real one.
    monkeypatch.chdir(tmp_path)
    return create_ledger_from_actions(agent_id=AGENT, session_id=OLD_SESSION,
                                      actions=actions, flow_id=0)


def test_a_new_build_does_not_resume_a_session_another_build_left(
        tmp_path, monkeypatch):
    """THE LIVE SHAPE."""
    _leave_unfinished(tmp_path, monkeypatch, OLD_BUILD)
    ledger = create_ledger_from_actions(user_id=USER, prompt_id=AGENT,
                                        actions=NEW_BUILD, flow_id=0)
    assert ledger.session_id != OLD_SESSION, (
        "the new build resumed another build's session: its action_2 is that "
        "build's task, so the new action_2 inherits its status")
    assert sorted(ledger.tasks) == ['action_1', 'action_2', 'action_3']
    assert ledger.tasks['action_2'].description == NEW_BUILD[1]
    assert ledger.tasks['action_2'].status == TaskStatus.PENDING


def test_the_same_steps_and_more_of_them_are_another_build(
        tmp_path, monkeypatch):
    """The old session's first three steps ARE the new ones; its two extra
    steps make it a different build all the same."""
    _leave_unfinished(tmp_path, monkeypatch, NEW_BUILD + OLD_BUILD[3:])
    ledger = create_ledger_from_actions(user_id=USER, prompt_id=AGENT,
                                        actions=NEW_BUILD, flow_id=0)
    assert ledger.session_id != OLD_SESSION
    assert sorted(ledger.tasks) == ['action_1', 'action_2', 'action_3']


def test_fewer_of_the_same_steps_are_another_build(tmp_path, monkeypatch):
    """A build that has since gained a step: the old session holds the first
    two of the three, which is not this build either."""
    _leave_unfinished(tmp_path, monkeypatch, NEW_BUILD[:2])
    ledger = create_ledger_from_actions(user_id=USER, prompt_id=AGENT,
                                        actions=NEW_BUILD, flow_id=0)
    assert ledger.session_id != OLD_SESSION


def test_as_many_steps_with_another_text_are_another_build(
        tmp_path, monkeypatch):
    """Same ids, one text rewritten (the interview reworded step 2): the
    session holds the other step 2, so it is another build's."""
    _leave_unfinished(tmp_path, monkeypatch, OLD_BUILD[:3])
    ledger = create_ledger_from_actions(user_id=USER, prompt_id=AGENT,
                                        actions=NEW_BUILD, flow_id=0)
    assert ledger.session_id != OLD_SESSION
    assert ledger.tasks['action_2'].description == NEW_BUILD[1]


def test_the_same_build_still_resumes_its_own_session(tmp_path, monkeypatch):
    _leave_unfinished(tmp_path, monkeypatch, NEW_BUILD)
    ledger = create_ledger_from_actions(user_id=USER, prompt_id=AGENT,
                                        actions=NEW_BUILD, flow_id=0)
    assert ledger.session_id == OLD_SESSION


def test_dict_actions_are_compared_by_the_text_the_ledger_stores(
        tmp_path, monkeypatch):
    """REUSE passes recipe actions as dicts; their text is what the ledger
    stores as the task's description, so the same build matches."""
    as_dicts = [{'action_id': i, 'action': text}
                for i, text in enumerate(NEW_BUILD, 1)]
    _leave_unfinished(tmp_path, monkeypatch, as_dicts)
    ledger = create_ledger_from_actions(user_id=USER, prompt_id=AGENT,
                                        actions=as_dicts, flow_id=0)
    assert ledger.session_id == OLD_SESSION


def test_tasks_a_run_added_do_not_make_it_another_build(tmp_path, monkeypatch):
    """A resumed build may hold tasks its run added (a dynamic task, a
    sub-task); only its action_<n> steps say which build it is."""
    old = _leave_unfinished(tmp_path, monkeypatch, NEW_BUILD)
    old.add_task(Task(task_id='dynamic_1', description='look a word up',
                      task_type=TaskType.AUTONOMOUS))
    old.add_task(Task(task_id='action_2_seq_1', description='part of step 2',
                      task_type=TaskType.INTERMEDIATE))
    ledger = create_ledger_from_actions(user_id=USER, prompt_id=AGENT,
                                        actions=NEW_BUILD, flow_id=0)
    assert ledger.session_id == OLD_SESSION


def test_a_caller_that_names_no_actions_keeps_the_unscoped_meaning(
        tmp_path, monkeypatch):
    """integrations/vlm/activity_stream attaches to the recipe's own session
    with actions=[]; with no steps to compare, any unfinished session of the
    flow is still resumed, as before."""
    _leave_unfinished(tmp_path, monkeypatch, OLD_BUILD)
    ledger = create_ledger_from_actions(user_id=USER, prompt_id=AGENT,
                                        actions=[], flow_id=0)
    assert ledger.session_id == OLD_SESSION
    ledger_dir = str(tmp_path / 'agent_data')
    assert _find_resumable_session(AGENT, USER, ledger_dir,
                                   flow_id=0) == OLD_SESSION
