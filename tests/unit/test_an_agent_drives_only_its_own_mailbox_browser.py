"""An agent works in its own mailbox's browser, and only its own.

Owner, 2026-11-24 (verbatim): "each agent needs a differeny mailbox investor
session needs sathish@hevolve.ai and the email provider is same unless you
have multiple instance of chrome the session cookie will be shared and
mailbox will not be isolated".  Owner, 2026-11-27 (verbatim): "the llm shd be
able to work on or use headless and with head browser (no-GUI) and (with GUI)
actions respectivelu".

So every mailbox has its own Chrome profile (driver.ensure_agent_browser), the
model drives it through ONE tool, Agent_Browser (core.agent_tools), and which
profile a turn may drive is the mailbox rule outreach already keeps
(outreach_crm_tools.browser_mailbox over goal_sender and _may_use_mailbox):
a goal's own turn drives its own goal's mailbox and no other, any other turn
names a mailbox its account may use.

The real tool is built by build_core_tool_closures and called; the boundary
is mocked: the browser dispatch (integrations.browser_research.tools.dispatch)
and the goal/consent/binding reads outreach_crm_tools makes.

    python -m pytest tests/unit/test_an_agent_drives_only_its_own_mailbox_browser.py -q
"""
import json

import pytest

from core.agent_tools import build_core_tool_closures
from core.tool_refusal import (NOTE_KEY, RETRY_AFTER_FIX, RETRY_FINISH_WITHOUT,
                                 RETRY_LATER, RETRY_NEVER, refusal_has_hint)
from integrations.agent_engine import outreach_crm_tools as oct
from integrations.browser_research import tools as br_tools

AGENT = '7731'
OWNER = 'owner-1'
STRANGER = 'stranger-9'
GOAL = 'goal-investor'
INVESTOR_BOX = 'sathish@hevolve.ai'
LASHES_BOX = 'registration@mcgroce.com'


class _Mailboxes:
    """The reads browser_mailbox makes: the turn's goal, its sender, who may
    use which mailbox, and a taken-back permission."""

    def __init__(self, monkeypatch, turn_goal=None, sender_email=INVESTOR_BOX,
                 creator=OWNER, may_use=None, withdrawn=()):
        self.may_use = may_use if may_use is not None else {
            (OWNER, INVESTOR_BOX), (OWNER, LASHES_BOX)}
        self.withdrawn = set(withdrawn)
        self.asked = []
        monkeypatch.setattr(oct, '_turn_goal_id', lambda: turn_goal)
        monkeypatch.setattr(oct, 'goal_sender', lambda gid: oct.Sender(
            str(gid), sender_email, title='Investor outreach', creator=creator))
        monkeypatch.setattr(oct, '_may_use_mailbox', self._may)
        monkeypatch.setattr(oct, '_mailbox_permission_withdrawn',
                            lambda who, box: (str(who), box) in self.withdrawn)

    def _may(self, who, box):
        self.asked.append((str(who), box))
        return (str(who), box) in self.may_use


@pytest.fixture
def dispatched(monkeypatch):
    calls = []

    def _dispatch(tool, user_id, **kwargs):
        calls.append((tool, user_id, kwargs))
        return {'success': True, 'mailbox': kwargs.get('mailbox'),
                'title': 'Inbox', 'connection_mechanism': 'agent_profile_cdp'}
    monkeypatch.setattr(br_tools, 'dispatch', _dispatch)
    return calls


def _browser(user_id=OWNER, prompt_id=AGENT):
    ctx = {
        'user_id': user_id, 'prompt_id': prompt_id, 'agent_data': {},
        'helper_fun': None, 'user_prompt': f'{user_id}_{prompt_id}',
        'request_id_list': {}, 'recent_file_id': None, 'scheduler': None,
        'send_message_to_user1': lambda *a, **k: None,
        'retrieve_json': json.loads, 'strip_json_values': lambda d: d,
        'save_conversation_db': lambda *a, **k: None,
    }
    tools = {name: fn for name, _desc, fn in build_core_tool_closures(ctx)}
    return tools['Agent_Browser']


# --- which mailbox's browser a turn may drive -------------------------------

def test_a_goal_turn_drives_its_own_goals_mailbox(monkeypatch, dispatched):
    _Mailboxes(monkeypatch, turn_goal=GOAL)
    out = json.loads(_browser()('open', url='https://mail.ionos.com'))
    assert out['success'] is True
    tool, user, kwargs = dispatched[0]
    assert (tool, user) == ('Agent_Browser_Open', OWNER)
    assert kwargs == {'mailbox': INVESTOR_BOX, 'url': 'https://mail.ionos.com'}


def test_a_goal_turn_cannot_drive_another_goals_mailbox(monkeypatch, dispatched):
    _Mailboxes(monkeypatch, turn_goal=GOAL)
    out = json.loads(_browser()('read', mailbox=LASHES_BOX))
    assert dispatched == []
    assert out['success'] is False and out['status'] == 'refused'
    assert INVESTOR_BOX in out[NOTE_KEY] and LASHES_BOX in out[NOTE_KEY]


def test_naming_the_goals_own_mailbox_in_any_case_is_fine(monkeypatch, dispatched):
    _Mailboxes(monkeypatch, turn_goal=GOAL)
    json.loads(_browser()('read', mailbox=' Sathish@Hevolve.AI '))
    assert dispatched[0][2] == {'mailbox': INVESTOR_BOX}


def test_a_goal_that_names_no_mailbox_has_no_browser(monkeypatch, dispatched):
    _Mailboxes(monkeypatch, turn_goal=GOAL, sender_email=None)
    out = json.loads(_browser()('read'))
    assert dispatched == []
    assert 'sender_email' in out[NOTE_KEY]
    # rv_ab1 on 123078e38: the fix says "do this step without the browser",
    # so the step may finish without it (not "call again with that changed").
    assert out['refused'] == 'goal_has_no_mailbox'
    assert out['retry'] == RETRY_FINISH_WITHOUT


def test_a_goal_made_by_a_stranger_cannot_use_the_companys_mailbox(
        monkeypatch, dispatched):
    boxes = _Mailboxes(monkeypatch, turn_goal=GOAL, creator=STRANGER)
    out = json.loads(_browser()('read'))
    assert dispatched == []
    assert boxes.asked == [(STRANGER, INVESTOR_BOX)]
    assert out['refused'] == 'mailbox_not_allowed'


def test_a_persons_turn_must_name_the_mailbox(monkeypatch, dispatched):
    _Mailboxes(monkeypatch, turn_goal=None)
    out = json.loads(_browser()('read'))
    assert dispatched == []
    assert 'mailbox' in out[NOTE_KEY]


def test_a_person_drives_a_mailbox_they_may_use(monkeypatch, dispatched):
    _Mailboxes(monkeypatch, turn_goal=None)
    json.loads(_browser()('read', mailbox=LASHES_BOX))
    assert dispatched[0][2] == {'mailbox': LASHES_BOX}


def test_a_person_cannot_drive_a_mailbox_they_may_not_use(monkeypatch, dispatched):
    _Mailboxes(monkeypatch, turn_goal=None)
    out = json.loads(_browser(user_id=STRANGER)('read', mailbox=INVESTOR_BOX))
    assert dispatched == []
    assert out['refused'] == 'mailbox_not_allowed'
    assert refusal_has_hint(json.dumps(out))


def test_a_taken_back_permission_stops_the_browser(monkeypatch, dispatched):
    _Mailboxes(monkeypatch, turn_goal=GOAL, withdrawn={(OWNER, INVESTOR_BOX)})
    out = json.loads(_browser()('read'))
    assert dispatched == []
    assert out['status'] == 'refused'
    assert out['refused'] == 'mailbox_permission_withdrawn'
    assert out['retry'] == RETRY_NEVER


def test_a_person_may_name_another_mailbox_after_a_refused_one(monkeypatch, dispatched):
    """rv_br5 on 9103f437a: the fix says "use a mailbox this account
    connected", so in a person's turn a call with another mailbox can work."""
    _Mailboxes(monkeypatch, turn_goal=None)
    out = json.loads(_browser(user_id=STRANGER)('read', mailbox=INVESTOR_BOX))
    assert out['refused'] == 'mailbox_not_allowed'
    assert out['retry'] == RETRY_AFTER_FIX


def test_a_goal_whose_creator_may_not_use_its_mailbox_says_never_retry(
        monkeypatch, dispatched):
    _Mailboxes(monkeypatch, turn_goal=GOAL, creator=STRANGER)
    out = json.loads(_browser()('read'))
    assert out['refused'] == 'mailbox_not_allowed'
    assert out['retry'] == RETRY_NEVER


def test_an_address_the_real_dispatch_refuses_is_named_as_such(monkeypatch):
    """rv_br5 on 9103f437a: dispatch refused a metadata or file: address
    before the script ran and gave no reason, so the tool coded it
    step_not_done; the kind is now named by dispatch too."""
    _Mailboxes(monkeypatch, turn_goal=GOAL)
    monkeypatch.setattr(br_tools.audit, 'append', lambda **kw: None)
    for url in ('http://169.254.169.254/latest/meta-data/', 'file:///c:/x'):
        out = json.loads(_browser()('open', url=url))
        assert out['success'] is False
        assert out['refused'] == 'not_a_web_address' and out['retry'] == RETRY_AFTER_FIX
        assert 'http or https' not in out[NOTE_KEY] or url.startswith('file:')


def test_naming_another_goals_mailbox_says_which_one_to_give(monkeypatch, dispatched):
    _Mailboxes(monkeypatch, turn_goal=GOAL)
    out = json.loads(_browser()('read', mailbox=LASHES_BOX))
    assert out['refused'] == 'not_this_goals_mailbox'
    assert out['retry'] == RETRY_AFTER_FIX


# --- one tool, five steps ---------------------------------------------------

def test_click_and_type_carry_the_agent_for_the_control_permission(
        monkeypatch, dispatched):
    _Mailboxes(monkeypatch, turn_goal=GOAL)
    browser = _browser()
    browser('click', target='Compose')
    browser('type', target='To', text='buyer@example.com', submit=True)
    assert dispatched[0][:1] == ('Agent_Browser_Click',)
    assert dispatched[0][2] == {'mailbox': INVESTOR_BOX, 'target': 'Compose',
                                'agent_id': AGENT}
    assert dispatched[1][:1] == ('Agent_Browser_Type',)
    assert dispatched[1][2] == {'mailbox': INVESTOR_BOX, 'field': 'To',
                                'text': 'buyer@example.com', 'submit': True,
                                'agent_id': AGENT}


def test_window_opens_the_profile_with_or_without_an_address(monkeypatch, dispatched):
    _Mailboxes(monkeypatch, turn_goal=GOAL)
    browser = _browser()
    browser('window')
    browser('window', url='https://mail.ionos.com')
    assert [c[0] for c in dispatched] == ['Agent_Browser_Window'] * 2
    assert dispatched[0][2] == {'mailbox': INVESTOR_BOX}
    assert dispatched[1][2] == {'mailbox': INVESTOR_BOX,
                                'url': 'https://mail.ionos.com'}


@pytest.mark.parametrize('call, word', [
    (('scroll',), 'open, read, click, type, window'),
    (('open',), 'url'),
    (('click',), 'target'),
    (('type',), 'target'),
    (('type', 'To'), 'text'),
])
def test_a_call_missing_what_its_step_needs_says_what_to_give(
        monkeypatch, dispatched, call, word):
    _Mailboxes(monkeypatch, turn_goal=GOAL)
    action, *rest = call
    out = json.loads(_browser()(action, target=rest[0] if rest else None))
    assert dispatched == []
    assert out['status'] == 'refused' and word in out[NOTE_KEY]


def test_a_step_the_browser_could_not_do_says_why_and_what_next(monkeypatch):
    _Mailboxes(monkeypatch, turn_goal=GOAL)
    monkeypatch.setattr(br_tools, 'dispatch', lambda tool, user_id, **kw: {
        'success': False, 'mailbox': kw['mailbox'],
        'error': "no button or link matching 'Send' on this page. Those it "
                 "has: Compose. Use one of those words, or read the page again."})
    out = json.loads(_browser()('click', target='Send'))
    assert out['success'] is False
    assert "matching 'Send'" in out[NOTE_KEY]
    assert refusal_has_hint(json.dumps(out))


@pytest.mark.parametrize('reason, code, retry, said, not_said', [
    # rv_ab1 on 123078e38 (M2): one refusal mode for every failed step told
    # the model to read the page and retry even when no retry can help.
    ('no_match', 'nothing_matching', RETRY_AFTER_FIX, "action 'read'", None),
    ('owner_only', 'owner_fills_it_in', RETRY_AFTER_FIX, "action 'window'", "action 'read'"),
    ('no_control', 'control_not_allowed', RETRY_NEVER, None, "action 'read'"),
    ('browser_unavailable', 'browser_unavailable', RETRY_LATER, None, "action 'read'"),
    ('page_error', 'page_did_not_answer', RETRY_LATER, None, None),
    ('not_web_address', 'not_a_web_address', RETRY_AFTER_FIX, 'another address', 'http or https'),
])
def test_each_kind_of_failed_step_says_whether_a_retry_can_help(
        monkeypatch, reason, code, retry, said, not_said):
    _Mailboxes(monkeypatch, turn_goal=GOAL)
    monkeypatch.setattr(br_tools, 'dispatch', lambda tool, user_id, **kw: {
        'success': False, 'mailbox': kw['mailbox'], 'reason': reason,
        'error': 'the script said so'})
    out = json.loads(_browser()('click', target='Send'))
    assert out['refused'] == code and out['retry'] == retry
    assert 'the script said so' in out[NOTE_KEY]
    if said:
        assert said in out[NOTE_KEY]
    if not_said:
        assert not_said not in out[NOTE_KEY]


@pytest.mark.parametrize('alias, route', [
    # rv_ab1 on 123078e38 (M3): the script's own refusals say action
    # "open_window"; the tool must take the words it tells the model to use.
    ('open_window', 'Agent_Browser_Window'), ('open_page', 'Agent_Browser_Open'),
    ('read_page', 'Agent_Browser_Read'), ('type_text', 'Agent_Browser_Type'),
    ('Window', 'Agent_Browser_Window'),
])
def test_the_scripts_own_action_names_are_taken(monkeypatch, dispatched, alias, route):
    _Mailboxes(monkeypatch, turn_goal=GOAL)
    _browser()(alias, url='https://mail.ionos.com', target='To', text='x')
    assert dispatched[0][0] == route


def test_the_tool_is_offered_on_need_not_on_every_turn():
    """Not on the main leg's always-on menu: a recipe step that names it, or
    request_tools, attaches it (agent_tool_menu.discover_and_attach)."""
    from core.agent_tool_menu import MAIN_LEG_CORE_TOOLS
    assert 'Agent_Browser' not in MAIN_LEG_CORE_TOOLS
    ctx_tools = build_core_tool_closures({
        'user_id': OWNER, 'prompt_id': AGENT, 'agent_data': {},
        'helper_fun': None, 'user_prompt': f'{OWNER}_{AGENT}',
        'request_id_list': {}, 'recent_file_id': None, 'scheduler': None,
        'send_message_to_user1': lambda *a, **k: None,
        'retrieve_json': json.loads, 'strip_json_values': lambda d: d,
        'save_conversation_db': lambda *a, **k: None})
    # rv_ab1 on 123078e38 (M1): the time and visual legs register every
    # closure unasked_tools lets through; the browser rode onto both.
    from core.agent_tool_menu import command_tools, unasked_tools
    assert 'Agent_Browser' not in [n for n, _d, _f in unasked_tools(ctx_tools)]
    assert 'Agent_Browser' not in [n for n, _d, _f in command_tools(ctx_tools)]
    assert 'Shell_Command' not in [n for n, _d, _f in unasked_tools(ctx_tools)]
    desc = {name: d for name, d, _fn in ctx_tools}['Agent_Browser']
    for word in ('browser', 'mailbox', 'sign', 'window', 'click', 'type'):
        assert word in desc.lower()
    assert '{' not in desc and '}' not in desc   # LangChain tool text
