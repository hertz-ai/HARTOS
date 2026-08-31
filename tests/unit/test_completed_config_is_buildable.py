"""#718 — "completed" must mean the config contains something buildable.

LIVE-PROVEN 2026-08-29 20:00, driven end-to-end through the real /chat API
(prompt_id 88013682884, agent VAL718PROBE).  I described an agent with three
explicit actions; turn 2 echoed all three back to me correctly.  The config
that got saved was this:

    {"status": "completed", "is_active": true,
     "personas": "", "tools": "",
     "flows": [{"flow_name": "", "persona": "", "actions": [], "sub_goal": ""}],
     "goal": "Read a text file and write a one-line summary",
     "personality": { ...fully populated... }}

Every STRUCTURAL field empty; name, goal and personality populated.

ATTRIBUTION from the same log window (offsets recorded before the probe):
    'gather_info parse error' : 0
    'salvaging partial'       : 0
    'COMPLETED STATUS'        : 1
    'Agent config saved'      : 1
and the third `new_res:` line matches the on-disk config byte for byte.  So the
parse was faithful and this was the NORMAL completion path -- not the salvage
path (4ac26248) and not a parse failure.  The model returned an empty shell and
nothing checked it.

Consequence: saved with is_active=true, published as a working agent, and it can
NEVER produce a flow recipe because there are no actions to execute.  It is a
fresh member of the 485-config population in #718 -- created by me, live, today.
"""
# ruff: noqa: TID251 - the ban targets runtime WORKERS (use safe_hartos_attr);
# this test must exercise the real module-level predicate it is guarding.
import hart_intelligence_entry as hie

# The exact object the model returned (gui_app.log 2026-08-29 20:00:00,211).
LIVE_EMPTY_SHELL = {
    'status': 'completed', 'name': 'VAL718PROBE',
    'agent_name': 'summarize.local.aria', 'broadcast_agent': False,
    'personas': '', 'tools': '',
    'flows': [{'flow_name': '', 'persona': '', 'actions': [], 'sub_goal': ''}],
    'goal': 'Read a text file and write a one-line summary',
    'personality': {'primary_traits': ['Meraki', 'Sisu'], 'tone': 'warm-casual'},
}


def test_the_live_empty_shell_is_not_buildable():
    assert hie._config_is_buildable(LIVE_EMPTY_SHELL) is False, (
        'this exact object was saved as a completed, is_active agent on '
        '2026-08-29 and can never build - it has no actions')


def test_a_real_config_is_buildable():
    cfg = {'status': 'completed', 'flows': [{
        'flow_name': 'main', 'persona': 'Assistant', 'sub_goal': 'summarise',
        'actions': [{'action': 'read the file', 'action_id': 1}]}]}
    assert hie._config_is_buildable(cfg) is True


def test_actions_in_a_later_flow_still_count():
    """Only flow 0 gates reuse, but a config with any actions is buildable."""
    cfg = {'flows': [{'actions': []}, {'actions': [{'action': 'x'}]}]}
    assert hie._config_is_buildable(cfg) is True


def test_malformed_input_does_not_raise():
    """A gate must not become a new crash site on the main creation path."""
    for bad in (None, {}, {'flows': None}, {'flows': 'nope'},
                {'flows': [None]}, {'flows': [[]]}, 'not-a-dict'):
        assert hie._config_is_buildable(bad) is False, repr(bad)


def test_reply_exists_and_asks_for_steps():
    txt = hie._EMPTY_BUILD_REPLY.lower()
    assert 'step' in txt or 'action' in txt, hie._EMPTY_BUILD_REPLY
