"""A session is resumed only for the flow it was left unfinished in.

THE LIVE FAILURE (2026-09-13 10:02, installed Nunba, agent 87400889007 --
2 flows, flow 0 banked on disk):

    create_ledger_from_actions: resuming in-flight session ..._1789153289150
    Loaded 16 tasks from ledger backend
    [RESUMING] Resuming from Flow 1, Action 1
    [AUTO-ADVANCE] action 1 done but recipe not saved -- requesting recipe

Flow 1 was handed flow 0's session.  That session counted as unfinished only
because it held 8 pending copies of flow 0's actions (the old string
numbering, fixed in 348fc7e82), and _find_resumable_session accepted an
unfinished task of ANY flow.  Task ids are positions within a flow, so flow
1's action_1 was flow 0's COMPLETED action_1; create_recipe's AUTO-ADVANCE
trusts the ledger status of the current action and asked for flow 1 action
1's recipe before that action ever ran.  Actions 2 and 3 also had their
recipes authored before they executed.

One ledger holds one flow of one prompt (create_ledger_from_actions; the
[NEXT-FLOW] branch builds a fresh ledger per flow), so resuming must look
only at the requested flow's tasks.
"""

from agent_ledger.core import _find_resumable_session, create_ledger_from_actions

AGENT, USER = '87400889007', 'u'
OLD_SESSION = 'u_87400889007_1000'
FLOW_0 = ['verify_device_permissions', 'fetch_current_location',
          'query_weather_api']
FLOW_1 = ['verify_device_permissions', 'fetch_activity_metrics']


def _leave_flow_0_unfinished(tmp_path, monkeypatch):
    # agent_data/ resolves against the CWD; isolate it from the real one.
    monkeypatch.chdir(tmp_path)
    create_ledger_from_actions(agent_id=AGENT, session_id=OLD_SESSION,
                               actions=FLOW_0, flow_id=0)


def test_flow_1_does_not_resume_a_session_left_unfinished_by_flow_0(tmp_path, monkeypatch):
    """THE LIVE SHAPE."""
    _leave_flow_0_unfinished(tmp_path, monkeypatch)
    ledger = create_ledger_from_actions(user_id=USER, prompt_id=AGENT,
                                        actions=FLOW_1, flow_id=1)
    assert ledger.session_id != OLD_SESSION, (
        "flow 1 resumed flow 0's session -- its action_N are flow 0's tasks, "
        'so flow 1 reads their status as its own')
    assert sorted(ledger.tasks) == ['action_1', 'action_2']
    assert {t.recipe_flow_id for t in ledger.tasks.values()} == {1}


def test_a_flow_still_resumes_its_own_unfinished_session(tmp_path, monkeypatch):
    _leave_flow_0_unfinished(tmp_path, monkeypatch)
    ledger = create_ledger_from_actions(user_id=USER, prompt_id=AGENT,
                                        actions=FLOW_0, flow_id=0)
    assert ledger.session_id == OLD_SESSION


def test_find_resumable_session_is_scoped_by_flow(tmp_path, monkeypatch):
    _leave_flow_0_unfinished(tmp_path, monkeypatch)
    ledger_dir = str(tmp_path / 'agent_data')
    assert _find_resumable_session(AGENT, USER, ledger_dir, flow_id=1) is None
    assert _find_resumable_session(AGENT, USER, ledger_dir, flow_id=0) == OLD_SESSION
    # An unscoped call keeps its meaning: any unfinished flow.
    assert _find_resumable_session(AGENT, USER, ledger_dir) == OLD_SESSION
