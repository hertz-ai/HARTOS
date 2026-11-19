"""An agent is built once and used by many people: the person it serves is a
blank it fills per person, never one named child baked into its steps.

MEASURED on the owner's desktop, 2026-11-16: the speech agent
"Arjun Tamil Speech Practice" (1794540972213, built 11-13 through autonomous
CREATE) carries "Arjun" in its name, agent_name (teach.local.arjun), persona
descriptions, goal, greeting and steps ("send_message_to_user to Arjun:
'அர்ஜுன்!'").  For the owner's own daughter it would call out another child's
name; the only way round it was to build a new agent.  Owner, verbatim: "why
the agent needs to be rebuilt just for a name change wtf is wrong with
creation".

Cause, in the plan author's own words: its example sub_goal was
"Arjun says ball", and its last rule was "Preserve EVERY detail of the task
description in the plan", so a child's name in the description became part
of the agent.  The send tools already refuse an unfilled blank and ask for the
real value (core.agent_tools.unfilled_text_placeholder, 63abdc857), so an
agent that writes <child_name> fills it per person at run time.

    python -m pytest tests/unit/test_an_agent_is_built_for_anyone_not_one_named_person.py -q
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DESCRIPTION = ("Help Arjun, a young child who speaks Tamil at home, first respond "
               "to his name and then name his family members.")


def _words():
    from hartos import gather_agentdetails as ga
    assistant, _proxy = ga.create_agents_for_user('u-anyone', autonomous=True,
                                                  initial_description=DESCRIPTION)
    # Whitespace collapsed: the prompt wraps its lines, a phrase may span two.
    return ' '.join(assistant.system_message.split())


def test_the_plan_author_is_told_people_are_blanks_not_part_of_the_agent():
    words = _words()
    assert 'used by many people' in words, words[-1500:]
    for blank in ('<child_name>', '<home_language>', '<known_words>'):
        assert blank in words, blank
    assert "steps as one particular person's: write its blank instead" in words


def test_the_plan_authors_own_example_names_no_child():
    words = _words()
    rules = words[words.index('FLOWS:'):]
    assert 'Arjun says ball' not in rules
    assert '<child_name> says ball' in rules


def test_keeping_every_detail_does_not_keep_a_persons_name():
    words = _words()
    assert "a person's name or personal detail is kept as its blank" in words
