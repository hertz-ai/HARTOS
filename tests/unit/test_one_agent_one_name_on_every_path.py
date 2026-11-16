"""One agent, one name: CREATE, REUSE and a casual turn say the same name.

Review of HARTOS f3db01f44 (REJECTED, hartos-d9's reviewer,
review_f3db_b343_1111.md), measured over the desktop's real agents:
  1. The casual identity (hartos.agent_identity.build_identity_prompt) said
     the raw config name when the config had no 'personality' dict (1190 of
     1238 configs), so it named the agent differently from REUSE for 338 of
     519 agents ("HARTOS_KidAttention_SpeechTherapy" vs "Ori" for the
     speech-therapy agent) and said "Agent <id>" for 427.
  2. Given names with '_' or '.' were dropped as ids, so the consent card
     and the agent used two rules for 233 agents.
  3. The fallback pick was seeded by role and internal name, which 444 saved
     personalities share: 452 of 792 agents said "Cleo".
  4. No test saw the agent id passed in; six mutants dropping it survived.

The pipeline cases are the reviewer's probe (rv_f3db_evidence/
test_rv_f3db_probe_names.py), adopted: the real create_agents and
create_agents_for_user on the pipelines fixture, and the real
build_identity_prompt on the same record.
"""
from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from tests.unit.test_create_loop_end_to_end import (  # noqa: E402,F401
    PROMPT_ID, create_env)
from tests.unit.test_the_agent_personality_is_the_agents_own import (  # noqa: E402,F401
    _line, pipelines)

_PLACEHOLDER_SPOKEN = re.compile(r'agent[ _-]?\d{2,}|\b\d{6,}\b', re.IGNORECASE)


def _casual_identity(record):
    from hartos.agent_identity import build_identity_prompt
    lines = [ln.strip() for ln in build_identity_prompt(record, 'Owner', '').splitlines()
             if ln.strip().startswith(('You are ', 'Your name is '))]
    return lines[-1] if lines else ''


def _said(pipes, env, name):
    """What CREATE, REUSE and a casual turn tell the agent its name is, for a
    record named ``name`` (no 'personality' dict, no action files)."""
    path = env.prompts / f'{PROMPT_ID}.json'
    rec = json.loads(path.read_text(encoding='utf-8'))
    rec['name'] = name
    rec['prompt_id'] = PROMPT_ID
    path.write_text(json.dumps(rec), encoding='utf-8')
    create, reuse = pipes.create().system_message, pipes.reuse().system_message
    said = {'create': _line(create), 'reuse': _line(reuse), 'casual': _casual_identity(rec)}
    said['name'] = said['create'][len('You are '):].split(',')[0]
    return said


@pytest.mark.parametrize('given', ['Syllable Coach', 'Math_Tutor',
                                   'HARTOS_KidAttention_SpeechTherapy', 'peer.trader'])
def test_a_given_name_is_said_as_given_on_every_path(pipelines, create_env, given):
    """Finding 1 and 2: the casual turn says what CREATE and REUSE say, and a
    name with '_' or '.' is the name the person gave."""
    s = _said(pipelines, create_env, given)
    assert s['name'] == given, s
    assert s['reuse'] == s['create'], s
    assert s['casual'].startswith(f'You are {given}.'), s


@pytest.mark.parametrize('given', [f'Agent {PROMPT_ID}', 'Agent 11470436451', ''])
def test_with_no_name_every_path_says_the_same_picked_name(pipelines, create_env, given):
    """Finding 1, the "Agent <id>" half, and finding 4: with no usable name
    the pick is seeded by the agent's id, so a path that dropped the id
    would say a different name."""
    s = _said(pipelines, create_env, given)
    for k in ('create', 'reuse', 'casual'):
        assert not _PLACEHOLDER_SPOKEN.search(s[k]), (k, s)
        assert 'speech.local.coach' not in s[k], (k, s)
    assert s['reuse'] == s['create'], s
    assert s['casual'].startswith(f"You are {s['name']}."), s


def test_a_saved_human_persona_is_said_on_every_path(pipelines, create_env):
    """Finding 1 on the speech-therapy agent's shape: its saved personality
    says a human name and its config has no usable name.  The casual turn
    reads the saved file CREATE and REUSE read, so all three say that name
    ('Tara' is not in the pick list, so a path that skipped the file would
    say a pick)."""
    from core.agent_personality import generate_personality, save_personality
    saved = generate_personality('Speech coach', 'Help a child respond to their name',
                                 'speech.local.coach')
    saved.persona_name = 'Tara'
    save_personality(str(PROMPT_ID), saved)
    s = _said(pipelines, create_env, f'Agent {PROMPT_ID}')
    assert s['name'] == 'Tara', s
    assert s['reuse'] == s['create'], s
    assert s['casual'].startswith('You are Tara.'), s


def test_a_short_id_placeholder_is_not_said_by_the_casual_turn(tmp_path, monkeypatch):
    """Finding 4: "Agent 13" for agent 13 is a placeholder only when the id
    is passed (the shared pattern needs six digits)."""
    from core import agent_personality as ap
    monkeypatch.setattr(ap, '_resolve_prompts_dir', lambda base_dir=None: str(tmp_path))
    line = _casual_identity({'name': 'Agent 13', 'prompt_id': '13', 'goal': 'g'})
    assert line.startswith('You are ') and 'Agent 13' not in line, line
    picked = ap.spoken_name(None, 'Agent 13', '13')
    assert picked != 'Agent 13' and line.startswith(f'You are {picked}.'), (line, picked)


class TestOneRule:

    def test_the_card_and_the_agent_read_one_name(self):
        """Finding 2: what the consent card accepts as a name is what the
        agent says."""
        from core.agent_personality import spoken_name
        from integrations.social.consent_service import _is_a_name
        for name in ('HARTOS_KidAttention_SpeechTherapy', 'peer.trader', 'Ivy',
                     'Agent 13', '13', 'Agent 11470436451'):
            card = name if _is_a_name(name, '13') else None
            agent = spoken_name(None, name, '13')
            assert (agent == name) == (card is not None), (name, card, agent)

    def test_an_internal_id_in_the_saved_persona_is_not_said(self):
        """The 11-10 defect the id check exists for: 'skill.local.x' saved
        as the persona name is not spoken; a human persona is."""
        from core.agent_personality import spoken_name
        assert spoken_name({'persona_name': 'skill.local.syllable_coach'}, '', '13') \
            != 'skill.local.syllable_coach'
        assert spoken_name({'persona_name': 'Coach'}, '', 'coach') == 'Coach'

    def test_agents_sharing_a_role_and_internal_name_get_different_picks(self):
        """Finding 3: 40 agents with the same saved role and internal name,
        no usable display name.  Seeded by role and name they all said one
        name; seeded by id they spread over the list."""
        from core.agent_personality import spoken_name
        p = {'role': 'Executor', 'agent_name': 'skill.local.name',
             'persona_name': 'skill.local.name'}
        names = {spoken_name(p, '', str(1790000000000 + i)) for i in range(40)}
        assert len(names) >= 10, names

    def test_the_same_agent_gets_the_same_pick_every_time(self):
        from core.agent_personality import spoken_name
        p = {'role': 'Executor', 'agent_name': 'skill.local.name'}
        assert len({spoken_name(p, '', '1794031134533') for _ in range(5)}) == 1
