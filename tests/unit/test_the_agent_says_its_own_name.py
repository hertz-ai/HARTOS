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
