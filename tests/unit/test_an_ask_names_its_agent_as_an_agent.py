"""A card says that an AGENT asks, so it never reads as a person asking.

Owner, 2026-11-11 ~03:30 IST, of the card on his Nunba desktop titled
"Vijai asks you": "vijai is agent name but question is why does the question
appear like another user asking question insteadof qualifying as agebt".
Measured: ask 09c2ca4c at 02:55:26 IST, by agent 70264903070 (the finance
goal's agent, persona and display name "Vijai"), to user 10202, title
"Vijai asks you".  person_ask.ask titled a card f'{name} asks you', and the
consent push f"{who} needs your OK": an agent with a person's name reads as
that person.

Also measured on central 2026-11-10: "Agent 69922589936 asks you" for agent
93731340723, whose mirror carried the placeholder of ANOTHER id; the name
check matched only its own id, so a bare id reached the owner as a name.

Real person_ask.ask, NotificationService and in-memory social DB; the card
leg (liquid_ui_service.push_agent_ui) and the push are recorded by the
``legs`` fixture of test_person_ask.

    python -m pytest tests/unit/test_an_ask_names_its_agent_as_an_agent.py -q
"""
from __future__ import annotations

import os
import sys

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.social.models import Notification, User, db_session  # noqa: E402
from tests.unit.test_person_ask import _settle, legs  # noqa: E402,F401

VIJAI = '70264903070'
WHY = ('Verifying the 90/9/1 revenue split distribution model for User, '
       'Infrastructure, and Central pools')
#: The form the finance agent sent at 02:55:26 IST (its labels, its reason).
AS_SENT = {'type': 'form', 'description': WHY, 'fields': [
    {'name': 'User_Pool_Percentage', 'label': 'User Pool Percentage', 'type': 'number'},
    {'name': 'Infrastructure_Pool_Percentage', 'label': 'Infrastructure Pool Percentage',
     'type': 'number'},
    {'name': 'Central_Pool_Percentage', 'label': 'Central Pool Percentage', 'type': 'number'}]}


def _agent_named(agent_id, name, username=None):
    with db_session() as db:
        if not db.query(User).filter_by(agent_id=agent_id).first():
            db.add(User(id='agent-' + agent_id,
                        username=username or 'finance.local.' + agent_id,
                        display_name=name, user_type='agent', agent_id=agent_id,
                        api_token='tok-' + agent_id))


def _ask(agent, person='10202'):
    """A background ask; a person holds at most 3 open (person_ask), so a
    test past the third asks someone else."""
    from integrations.agent_engine import person_ask
    return person_ask.ask(person, agent, dict(AS_SENT), prompt_id=agent,
                          background=True)


def _row(ask_id):
    with db_session(commit=False) as db:
        n = db.query(Notification).filter_by(id=ask_id).first()
        return None if n is None else n.message


def test_the_card_bell_and_push_say_an_agent_asks(legs):
    """THE 02:55 CARD: 'Vijai asks you' read as a person; now it names an agent."""
    _agent_named(VIJAI, 'Vijai')
    out = _ask(VIJAI)
    assert out['ok'] is True, out
    _agent, card, _user = legs['cards'][-1]
    assert card['title'] == 'Agent Vijai asks you'
    assert _row(out['ask_id']) == 'Agent Vijai asks you'
    _settle()
    assert ('Agent Vijai asks you', WHY) in [(t, b) for _u, t, b, _d in legs['pushes']]


def test_another_ids_placeholder_gives_way_to_the_handle(legs):
    """Central 11-10: 'Agent 69922589936 asks you' showed a bare id as a name.
    The lookup's next name is the mirror's username, the agent's handle."""
    _agent_named('93731340723', 'Agent 69922589936', username='swift.amber.fox')
    out = _ask('93731340723')
    assert out['ok'] is True, out
    _agent, card, _user = legs['cards'][-1]
    assert card['title'] == 'Agent swift.amber.fox asks you'


def test_no_name_at_all_says_your_agent(legs):
    """Display name AND handle are placeholders (the mirror's own fallback
    username is 'agent-<id>'): no name reaches the card."""
    _agent_named('93731340724', 'Agent 69922589936', username='agent-69922589936')
    out = _ask('93731340724')
    assert out['ok'] is True, out
    _agent, card, _user = legs['cards'][-1]
    assert card['title'] == 'Your agent needs your answer'


def test_the_consent_push_says_an_agent_needs_the_ok():
    from integrations.social.consent_service import consent_prompt_push
    push = consent_prompt_push({'user_id': '10202', 'agent_name': 'Vijai',
                                'consent_type': 'camera'}, 'r1')
    assert push['title'] == 'Agent Vijai needs your OK'
    assert push['body'] == 'Agent Vijai asks for camera.'


def test_a_person_asking_keeps_their_own_name():
    """A device ask is a person's (the phone's owner): no 'Agent' in front."""
    from integrations.social.consent_service import consent_prompt_push
    push = consent_prompt_push({'user_id': '10202', 'requester_name': 'Ravi',
                                'consent_type': 'peer_admission'}, 'r2')
    assert push['title'] == 'Ravi needs your OK'


def test_an_unnamed_asker_stays_an_agent():
    from integrations.social.consent_service import consent_prompt_push
    push = consent_prompt_push({'user_id': '10202', 'consent_type': 'camera'}, 'r3')
    assert push['title'] == 'An agent needs your OK'


def test_a_name_that_already_says_agent_is_not_doubled(legs):
    """Review of 11dbbd8ef, m1: 'Agent Agent Smith asks you'."""
    _agent_named('93731340725', 'Agent Smith')
    out = _ask('93731340725', person='10203')
    assert out['ok'] is True, out
    _agent, card, _user = legs['cards'][-1]
    assert card['title'] == 'Agent Smith asks you'


def test_the_bridge_placeholder_is_no_name(legs):
    """Review of 11dbbd8ef, m2: agent_bridge's mirrors carry 'agent_<id>_<n>'
    (303 of 322 local mirrors), which reached a card as a name."""
    _agent_named('27130929014_0', 'agent_27130929014_0', username='agent_27130929014_0')
    out = _ask('27130929014_0', person='10204')
    assert out['ok'] is True, out
    _agent, card, _user = legs['cards'][-1]
    assert card['title'] == 'Your agent needs your answer'


def test_the_consent_push_names_the_agent_when_the_scrub_cannot_run(monkeypatch):
    """The reason is dropped when the egress scrub fails; the agent stays an
    agent in what replaces it (review of 11dbbd8ef, surviving mutant M11)."""
    import security.edge_privacy as ep
    from integrations.social.consent_service import consent_prompt_push

    def _broken(_text):
        raise RuntimeError('scrub down')
    monkeypatch.setattr(ep, 'scrub_text', _broken)
    push = consent_prompt_push({'user_id': '10202', 'agent_name': 'Vijai',
                                'consent_type': 'camera', 'reason': 'see the desk'}, 'r4')
    assert push['body'] == 'Agent Vijai asks for camera.'


def test_the_computer_control_ask_names_the_agent_not_its_id():
    """Review of 11dbbd8ef, m4: the reason read 'Agent 70264903070 asks to
    control this computer.' on the push and the desktop card."""
    from integrations.vlm import safety
    _agent_named(VIJAI, 'Vijai')
    covers = safety.COMPUTER_CONTROL_COVERS
    assert safety._computer_control_reason(VIJAI) == f'Agent Vijai asks to {covers}.'
    # No name kept for it: no bare id either.
    assert safety._computer_control_reason('93731340799') == (
        f'One of your agents asks to {covers}.')
    assert safety._computer_control_reason(None) == (
        f'An agent that could not be identified asks to {covers}.')


def test_the_computer_control_block_files_the_named_reason(monkeypatch):
    """The reason the owner's ask carries comes from that one place."""
    from integrations.vlm import safety
    _agent_named(VIJAI, 'Vijai')
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', '10202')
    filed = []

    def _answer(owner, agent, reason):
        filed.append(reason)
        return None                      # not answered yet
    monkeypatch.setattr(safety, '_computer_control_answer', _answer)
    out = safety.computer_control_block(VIJAI, wait=False)
    assert out and out.startswith('Not run'), out
    assert filed == [f'Agent Vijai asks to {safety.COMPUTER_CONTROL_COVERS}.']


def test_placeholders_of_any_id_are_not_names():
    from integrations.social import consent_service as cs
    assert cs._is_a_name('Agent 69922589936', '93731340723') is False
    assert cs._is_a_name('agent-1793366231447', '93731340723') is False
    assert cs._is_a_name('agent_27130929014_0', '27130929014_0') is False
    # Six digits is a placeholder (surviving mutant M12: seven) ...
    assert cs._is_a_name('Agent 123456', VIJAI) is False
    # ... but only as the whole name (surviving mutant M8: a search).
    assert cs._is_a_name('Backup of Agent 1234567', VIJAI) is True
    # Real names stay names, digits and all.
    assert cs._is_a_name('Vijai', VIJAI) is True
    assert cs._is_a_name('Agent Smith', VIJAI) is True
    assert cs._is_a_name('Agent 7', VIJAI) is True
    assert cs._is_a_name('Studio 1 Assistant', '1') is True
