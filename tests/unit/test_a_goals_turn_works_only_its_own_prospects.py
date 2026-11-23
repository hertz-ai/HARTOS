"""A goal's turn sees and writes only its own goal's prospects (#240).

Measured live on central, 2026-11-20 ~04:00Z: goal 5ba50f98 ("Rice export:
turn buyer orders into supplier quote requests and escalate") planned "for
each new prospect with no outreach, generate a personalized outreach email
... signature sathish / founder, HevolveAI", drafted a pitch for "Livestock
Robotics" and named Intello Labs, MAF RODA AGROBOTIC, Agrograde, REEMOON,
Segritech, Dairy Robotics and IceRobo as its targets.  Central's prospect
store held 89 prospects: 87 of no goal (old agri-robotics leads) and 2 of
goal 716cbcea.

Read on origin/main 61c8d87e: refuse_other_goals_prospect let any turn write
to a prospect no goal holds, so a goal's own turn could email, sequence or
advance it; and every listing a goal's turn reads (get_pipeline_status,
check_pending_followups, list_sent_emails, view_journey_pipeline,
run_journey_tick, the sales prompt's LIVE PIPELINE STATE) showed it the
whole store.

Now a goal's own turn is refused a prospect of no goal, with what to do
instead, and is shown only its own goal's prospects (store_seen_by).  A
person's turn and the daemon's tick keep the whole store, as before.

The real outreach tools, journey tools, follow-up checker and sales prompt,
on the mailbox tests' node fixture (the real channel registry, EmailAdapter
and social DB); mocked there: the mail servers, the mailer, the admin config.
"""
import json
from unittest.mock import MagicMock

from tests.unit.test_each_goal_sends_from_its_own_mailbox import (  # noqa: F401
    HEVOLVE, MCGROCE, OWNER, _connect, _in_turn, _journey_tools, _legacy_mailbox,
    _mailbox, _prospect, _quiet_journey, _tools, channels, mail, mailer, node)

NODE_BOX = 'owner@desktop.example'          # _legacy_mailbox's address
LEAD = 'lead@agri-robotics.example.org'
ANA = 'ana@clinic.example.org'
RAVI = 'ravi@salon.example.org'


def _goalless_lead(tools):
    """An old lead no goal holds: made outside any goal's turn."""
    return _prospect(tools, 'Agri Robotics Co', LEAD)


# ── writes ───────────────────────────────────────────────────────────

def test_a_goals_turn_is_refused_a_prospect_no_goal_holds(node, monkeypatch):
    """FAILS BEFORE THE FIX: the speech goal's turn emailed the lead from the
    node's mailbox (a flat node sends an email of no goal), sequenced it,
    moved it and ran its journey."""
    _quiet_journey(monkeypatch, node.store)
    _legacy_mailbox(node, monkeypatch)
    tools = _tools(monkeypatch, node.store)
    journey = _journey_tools(monkeypatch)
    lead = _goalless_lead(tools)
    step = json.dumps([{'delay_days': 0, 'subject': 'bump', 'body_html': '<p>hi</p>'}])

    outs = [json.loads(_in_turn('g-speech', fn, *args)) for fn, args in (
        (tools['send_outreach_email'], (lead, 'a question', '<p>hello</p>')),
        (tools['create_followup_sequence'], (lead, 'bump', step)),
        (tools['move_prospect_stage'], (lead, 'won')),
        (journey['send_prospect_message'], (lead, 'hello', 'email')),
        (journey['advance_prospect_stage'], (lead, 'research')))]

    for out in outs:
        assert out['success'] is False and out['refused'] == 'goalless_prospect', out
        assert out['retry'] == 'after_fix'
        assert "'Agri Robotics Co' belongs to no goal" in out['note']
        assert "goal 'Speech therapy centres'" in out['note']
        assert 'create_prospect' in out['note']
    row = node.store._load_prospects()
    assert row['sequences'] == {}
    assert row['prospects'][lead]['stage'] == 'new'
    assert 'journey_stage' not in row['prospects'][lead]
    assert node.mail.sent == [] and node.mailer == []
    assert node.store.list_held_drafts() == []


def test_a_goals_turn_still_writes_to_its_own_prospect(node, monkeypatch):
    _connect(HEVOLVE)
    _mailbox(node.channels, HEVOLVE)
    tools = _tools(monkeypatch, node.store)
    own = _prospect(tools, 'Clinic', ANA, 'g-speech')

    out = json.loads(_in_turn('g-speech', tools['send_outreach_email'],
                              own, 'a question', '<p>hello</p>'))

    assert out['success'] is True and out['held'] is True and out['sent'] is False
    (draft,) = node.store.list_held_drafts(goal_id='g-speech')
    assert draft['prospect_id'] == own
    assert node.mail.sent == []


def test_a_persons_turn_still_emails_a_prospect_no_goal_holds(node, monkeypatch):
    _legacy_mailbox(node, monkeypatch)
    tools = _tools(monkeypatch, node.store)
    lead = _goalless_lead(tools)

    out = json.loads(tools['send_outreach_email'](lead, 'a question', '<p>hello</p>'))

    assert out['success'] is True and out['sent'] is True and 'refused' not in out
    assert [m['to'] for m in node.mail.sent_from(NODE_BOX)] == [LEAD]


# ── what a goal's turn is shown ──────────────────────────────────────

def test_a_goals_turn_lists_only_its_own_prospects_and_a_person_lists_all(node, monkeypatch):
    """FAILS BEFORE THE FIX: the speech goal's turn was shown the lashes
    goal's prospect, the lead of no goal, their sent emails and the CRM's
    whole deal pipeline."""
    tools = _tools(monkeypatch, node.store)
    journey = _journey_tools(monkeypatch)
    speech = _prospect(tools, 'Clinic', ANA, 'g-speech')
    lashes = _prospect(tools, 'Salon', RAVI, 'g-lashes')
    lead = _goalless_lead(tools)
    data = node.store._load_prospects()
    data['sent_log'] = [{'prospect_id': pid, 'subject': 'hello',
                         'sent_at': f'2026-11-1{i}T00:00:00'}
                        for i, pid in enumerate((speech, lashes, lead))]
    node.store._save_prospects(data)
    crm = MagicMock()           # after the creates: they sync to the CRM too
    crm.get_pipeline_status.return_value = {'deals': ['Agri Robotics Co']}
    monkeypatch.setattr(node.store, '_get_erxes', lambda: crm)

    def seen(goal):
        def call(fn):
            return json.loads(_in_turn(goal, fn) if goal else fn())
        status = call(tools['get_pipeline_status'])
        sent = call(tools['list_sent_emails'])
        view = call(journey['view_journey_pipeline'])
        return ({p['id'] for rows in status['pipeline'].values() for p in rows},
                status['total_prospects'], status['erxes_pipeline'],
                {e['prospect_id'] for e in sent['emails']},
                {p['company'] for stage in view['pipeline'].values()
                 for p in stage['prospects']},
                view['total'])

    assert seen('g-speech') == ({speech}, 1, None, {speech}, {'Clinic'}, 1)
    assert seen(None) == ({speech, lashes, lead}, 3, {'deals': ['Agri Robotics Co']},
                          {speech, lashes, lead},
                          {'Clinic', 'Salon', 'Agri Robotics Co'}, 3)


def test_a_goals_prompt_shows_only_its_own_prospects(node, monkeypatch):
    """FAILS BEFORE THE FIX: the sales prompt's LIVE PIPELINE STATE named
    every prospect in the store, other goals' and no goal's too, and told
    the agent to email each 'new' one.  A goal dict with no id (no goal to
    narrow to) still reads the whole store."""
    from integrations.agent_engine.goal_manager import GoalManager
    tools = _tools(monkeypatch, node.store)
    _prospect(tools, 'Clinic', ANA, 'g-speech')
    _prospect(tools, 'Salon', RAVI, 'g-lashes')
    _goalless_lead(tools)
    goal = {'goal_type': 'outreach', 'title': 'Speech therapy centres',
            'description': 'find clinics'}

    own = GoalManager.build_prompt(dict(goal, id='g-speech'))
    whole = GoalManager.build_prompt(goal)

    assert 'Clinic' in own and 'Total prospects: 1' in own
    assert 'Salon' not in own and 'Agri Robotics Co' not in own
    assert all(c in whole for c in ('Clinic', 'Salon', 'Agri Robotics Co'))
    assert 'Total prospects: 3' in whole


# ── what a goal's batch tools work on ────────────────────────────────

def test_a_goals_follow_up_check_sends_only_its_own_goals_steps(node, monkeypatch):
    """FAILS BEFORE THE FIX: the speech goal's check sent the lashes goal's
    due step too.  The daemon's tick still sends every goal's step, each
    from its own goal's mailbox."""
    for address in (HEVOLVE, MCGROCE):
        _connect(address)
        _mailbox(node.channels, address)
    tools = _tools(monkeypatch, node.store)
    speech = _prospect(tools, 'Clinic', ANA, 'g-speech')
    lashes = _prospect(tools, 'Salon', RAVI, 'g-lashes')
    step = json.dumps([{'delay_days': 0, 'subject': 'bump',
                        'body_html': '<p>hi again</p>'}])
    for pid, goal in ((speech, 'g-speech'), (lashes, 'g-lashes')):
        _in_turn(goal, tools['create_followup_sequence'], pid, 'bump', step)
        node.store.mark_goal_copy_verified(goal, verified_by=OWNER)

    checked = json.loads(_in_turn('g-speech', tools['check_pending_followups']))

    assert [a['prospect'] for a in checked['actions']] == ['Clinic']
    assert [m['to'] for m in node.mail.sent_from(HEVOLVE)] == [ANA]
    assert node.mail.sent_from(MCGROCE) == []
    assert node.store.check_pending_followups_daemon()['sent'] == 1
    assert [m['to'] for m in node.mail.sent_from(MCGROCE)] == [RAVI]


def test_a_goals_journey_tick_leaves_a_prospect_no_goal_holds(node, monkeypatch):
    """FAILS BEFORE THE FIX: run_journey_tick in the speech goal's turn moved
    the lead of no goal on and sent it the journey's first email.  A tick
    outside any goal's turn still does, as before."""
    _quiet_journey(monkeypatch, node.store)
    _legacy_mailbox(node, monkeypatch)
    tools = _tools(monkeypatch, node.store)
    journey = _journey_tools(monkeypatch)
    lead = _goalless_lead(tools)
    data = node.store._load_prospects()
    data['prospects'][lead].update(
        journey_stage='research',
        email_draft={'subject': 'hello', 'body': '<p>hi there</p>'})
    node.store._save_prospects(data)

    def stage():
        return node.store._load_prospects()['prospects'][lead].get('journey_stage')

    _in_turn('g-speech', journey['run_journey_tick'])

    assert stage() == 'research' and node.mail.sent == []
    journey['run_journey_tick']()
    assert stage() == 'outreach'
    assert [m['to'] for m in node.mail.sent_from(NODE_BOX)] == [LEAD]
