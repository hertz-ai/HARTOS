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


# LIVE 2026-10-08 18:47 (prompt_id 54, user 10202, request a54-8bfa532bd98d):
# the person confirmed a review listing three actions; the model's final JSON
# kept the first two, dropped the third and put the flow's sub_goal inside
# "actions" as an object.  It was saved as is: a flow missing the step the
# person confirmed, with an entry no step can be built from.
LIVE_ACTION_REPLACED_BY_SUB_GOAL = {
    'status': 'completed', 'name': 'Personalised Learning Agent',
    'agent_name': 'teach.local.radha',
    'flows': [{'flow_name': 'teach', 'persona': 'Tutor', 'actions': [
        'Call get_user_id, then get_data_by_key with the key teach.<that user '
        "id> to read this learner's saved progress.",
        "Write this turn's reply to the learner yourself, as your own chat "
        'message to them, following the Teach Yourself rules.',
        {'sub_goal': 'Every message from a learner gets the right next '
                     "teaching step and that learner's progress is kept."}]}],
}


def test_an_entry_that_is_not_an_action_is_not_buildable():
    assert hie._config_is_buildable(LIVE_ACTION_REPLACED_BY_SUB_GOAL) is False, (
        'saved on 2026-10-08 with the confirmed third action missing and a '
        'sub_goal object in its place')


def test_every_action_shape_a_saved_config_carries_is_buildable():
    """Plain text, and the {'action': ...} dict saved configs also hold."""
    for actions in (['read the file'],
                    [{'action': 'read the file', 'action_id': 1}],
                    ['read the file', {'action': 'summarise it'}]):
        cfg = {'flows': [{'flow_name': 'main', 'actions': actions}]}
        assert hie._config_is_buildable(cfg) is True, actions


def test_an_action_with_no_text_is_not_buildable():
    for actions in ([''], ['   '], [None], [{'action': ''}], [{'action_id': 1}],
                    ['read the file', 7], 'read the file', 'summarise'):
        cfg = {'flows': [{'flow_name': 'main', 'actions': actions}]}
        assert hie._config_is_buildable(cfg) is False, actions


def test_malformed_input_does_not_raise():
    """A gate must not become a new crash site on the main creation path."""
    for bad in (None, {}, {'flows': None}, {'flows': 'nope'},
                {'flows': [None]}, {'flows': [[]]}, 'not-a-dict'):
        assert hie._config_is_buildable(bad) is False, repr(bad)


def test_reply_exists_and_asks_for_steps():
    txt = hie._EMPTY_BUILD_REPLY.lower()
    assert 'step' in txt or 'action' in txt, hie._EMPTY_BUILD_REPLY
