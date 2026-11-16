"""An agent introduces itself by the name the person gave it, never by its
internal id.

MEASURED live 2026-11-10 on build 7f8504b7: the Syllable Coach agent opened
its reply to a parent with "Hey there! I'm skill.local.syllable_coach".
generate_personality (core/agent_personality.py) took the agent's internal
id (config 'agent_name', 'skill.local.<x>') as its persona name and SAVED a
greeting formatted with it.  On that desktop 916 of 920 saved personalities
carry persona_name == agent_name.

The rule, read where the prompt is built so the saved files are fixed
without rewriting them (claude-autostart-29's review of the proposal):
  * the spoken name is what the person called the agent (its config's
    display 'name');
  * else the saved persona name, unless that is the internal id;
  * else a pick from _PERSONA_NAMES, the same for the same agent every turn;
  * the saved greeting says the spoken name: where it was formatted with
    another name, that known value is replaced;
  * both prompt builders (agent_personality.build_personality_prompt and
    agent_identity.build_identity_prompt) use the one helper, so one turn
    never names the agent twice.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import agent_personality as ap  # noqa: E402

_ID = 'skill.local.syllable_coach'
_DISPLAY = 'Syllable Coach Ball Exercise'


def _saved_with_the_id():
    """A personality as 916 of 920 were saved: named and greeting by the id."""
    return ap.AgentPersonality(
        agent_name=_ID, role='assistant', persona_name=_ID,
        primary_traits=[], tone='warm-casual',
        greeting_style=f"Hey there! I'm {_ID}, and I'm genuinely excited to work with you on this.")


class TestTheSpokenName:

    def test_the_display_name_is_spoken(self):
        assert ap.spoken_name(_saved_with_the_id(), _DISPLAY) == _DISPLAY

    def test_without_a_display_name_the_id_is_never_spoken(self):
        name = ap.spoken_name(_saved_with_the_id(), '')
        assert name and name != _ID and '.' not in name
        assert name in ap._PERSONA_NAMES

    def test_the_picked_name_is_the_same_every_turn(self):
        assert ap.spoken_name(_saved_with_the_id(), '') == ap.spoken_name(_saved_with_the_id(), '')

    def test_two_agents_need_not_share_the_picked_name(self):
        other = _saved_with_the_id()
        other.agent_name = other.persona_name = 'skill.local.word_builder'
        names = {ap.spoken_name(_saved_with_the_id(), ''), ap.spoken_name(other, '')}
        # Deterministic per agent; the pool has 24 names, these two differ.
        assert len(names) == 2

    def test_a_real_persona_name_is_kept_when_no_display_name(self):
        p = _saved_with_the_id()
        p.persona_name = 'Aria'
        assert ap.spoken_name(p, '') == 'Aria'

    def test_a_display_name_of_only_spaces_is_no_display_name(self):
        assert ap.spoken_name(_saved_with_the_id(), '   ') != '   '

    def test_a_personality_kept_as_a_dict_is_read_the_same(self):
        d = {'agent_name': _ID, 'persona_name': _ID,
             'greeting_style': f"Hi! I'm {_ID}, your creative partner."}
        assert ap.spoken_name(d, _DISPLAY) == _DISPLAY
        assert ap.spoken_greeting(d, _DISPLAY) == f"Hi! I'm {_DISPLAY}, your creative partner."


class TestTheGreetingSaysTheSpokenName:

    def test_a_greeting_saved_with_the_id_says_the_display_name(self):
        g = ap.spoken_greeting(_saved_with_the_id(), _DISPLAY)
        assert _ID not in g
        assert g.startswith(f"Hey there! I'm {_DISPLAY},")

    def test_a_greeting_already_saying_the_spoken_name_is_unchanged(self):
        p = _saved_with_the_id()
        p.persona_name = 'Aria'
        p.greeting_style = "Hey there! I'm Aria, and I'm genuinely excited to work with you on this."
        assert ap.spoken_greeting(p, '') == p.greeting_style

    def test_an_empty_greeting_stays_empty(self):
        p = _saved_with_the_id()
        p.greeting_style = ''
        assert ap.spoken_greeting(p, _DISPLAY) == ''


class TestOnlyANameIsSpoken:
    """Review of 4b9ba08ad, must-fixes 2 and 3: placeholders built from an id
    were spoken ("Agent 11470436451"; 427 of 1236 desktop configs are named
    so), and a persona that is a human name was replaced because the agent's
    id was the same word."""

    _PID = '70264903070'

    def _bare(self, persona=_ID):
        return ap.AgentPersonality(agent_name=_ID, role='assistant', persona_name=persona,
                                   primary_traits=[], tone='warm-casual',
                                   greeting_style='', self_awareness_prompt='')

    def test_a_placeholder_display_name_is_never_spoken(self):
        p = self._bare()
        for placeholder in (f'Agent {self._PID}', f'agent_{self._PID}', f'agent-{self._PID}',
                            self._PID, 'Agent 11470436451', '424201'):
            spoken = ap.spoken_name(p, placeholder, self._PID)
            assert spoken in ap._PERSONA_NAMES, (placeholder, spoken)

    def test_a_placeholder_persona_is_never_spoken(self):
        p = self._bare(persona=f'Agent {self._PID}')
        assert ap.spoken_name(p, '', self._PID) in ap._PERSONA_NAMES

    def test_a_human_persona_equal_to_the_agent_id_is_kept(self):
        p = ap.AgentPersonality(agent_name='Coach', role='assistant', persona_name='Coach',
                                primary_traits=[], tone='warm-casual',
                                greeting_style='', self_awareness_prompt='')
        assert ap.spoken_name(p) == 'Coach'

    def test_a_short_number_in_a_real_name_is_still_a_name(self):
        assert ap.spoken_name(self._bare(), 'Studio 1 Assistant', '1') == 'Studio 1 Assistant'

    def test_the_one_rule_is_the_consent_cards_rule_too(self):
        """consent_service.agent_display_name reads the same rule (hartos-d9:
        'agent_99_0' for id '99_0' and 'agent_65' for '65' were accepted)."""
        from integrations.social import consent_service as cs
        for name, aid in (('agent_99_0', '99_0'), ('agent_65', '65'), ('Agent 69922589936', '1'),
                          ('70264903070', '1'), ('', '1')):
            assert cs._is_a_name(name, aid) is False, (name, aid)
            assert ap.is_a_name(name, aid) is False, (name, aid)
        assert cs._is_a_name('Syllable Coach', '65') is True


class TestBothPromptBuildersSayOneName:

    def test_the_personality_block_never_says_the_id(self):
        named = ap.with_spoken_name(_saved_with_the_id(), _DISPLAY)
        block = ap.build_personality_prompt(named, execution_mode=True)
        assert _ID not in block
        assert f'You are {_DISPLAY}' in block
        assert f"I'm {_DISPLAY}" in block

    def test_naming_leaves_the_saved_personality_as_it_was(self):
        saved = _saved_with_the_id()
        ap.with_spoken_name(saved, _DISPLAY)
        assert saved.persona_name == _ID and _ID in saved.greeting_style
        assert saved.agent_name == _ID

    def test_the_personality_block_without_a_display_name_says_the_pick(self):
        block = ap.build_personality_prompt(_saved_with_the_id(), execution_mode=True)
        pick = ap.spoken_name(_saved_with_the_id(), '')
        assert _ID not in block
        assert f'You are {pick}' in block

    def test_the_identity_block_says_the_same_name_and_greeting(self):
        from hartos.agent_identity import build_identity_prompt
        cfg = {'name': _DISPLAY, 'agent_name': _ID, 'goal': 'Practise the b sound',
               'personality': {'agent_name': _ID, 'persona_name': _ID, 'tone': 'warm-casual',
                               'primary_traits': [],
                               'greeting_style': f"Hey there! I'm {_ID}, and I'm excited."}}
        block = build_identity_prompt(cfg, owner_name='', user_details='')
        assert _ID not in block
        assert f'Your name is {_DISPLAY}.' in block
        assert f"I'm {_DISPLAY}, and I'm excited." in block

    def test_the_identity_block_without_a_display_name_never_reads_name_is_dot(self):
        from hartos.agent_identity import build_identity_prompt
        cfg = {'name': '', 'agent_name': _ID, 'goal': 'Practise the b sound',
               'personality': {'agent_name': _ID, 'persona_name': _ID, 'tone': 'warm-casual',
                               'primary_traits': [], 'greeting_style': ''}}
        block = build_identity_prompt(cfg, owner_name='', user_details='')
        assert 'Your name is .' not in block
        assert _ID not in block
