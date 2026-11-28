"""The app's own notes stay out of what the agent says to the person.

LIVE 2026-11-28 12:37 IST, desktop, agent 13 ("Nia"), local Qwen 4B.  An
open-mic "Yeah" started a turn and the spoken reply was:

    "I've reviewed the conversation logs, and I can see that voice B spoke
     twice in the last couple of minutes, confirming "Yeah" at 12:35:07 and
     12:36:26. The user ID associated with this account is 10202 ..."

What the model had been given (langchain.log 12:36:52-12:37:13):

  1. under the person's word, a voices note for ONE voice, headed "Voices
     heard on this person's account ... work out who each voice is from the
     conversation", each line with its clock time;
  2. "follow these steps: [{'no-op: action completed without tool
     execution': ...}]", the banker's record of a toolless action shown as
     a step, to which it answered "The action is complete." and called
     get_user_id;
  3. a prompt rule ending "Important: Get the user Id yourself always";
  4. after the gate refused that, "Write the action's actual result now, in
     full", which it answered with a report of its own work.

A replay of the logged turn on the same model showed
that rewording the note alone does not stop it (3/10 -> 2/10 leaks), so the
fix is in WHAT is given, and these tests pin that on the real code:

  * the voices note is given when it tells the reader something (the device
    knows two voices, or a line carries sounds for a speech agent) and
    carries no clock time and no order to the model;
  * the banker's no-tool record is shown as a step nowhere;
  * the user-id rule gives no order to fetch the id, in both prompts;
  * the toolless steer asks for the reply to the person.

    python -m pytest tests/unit/test_the_agent_does_not_read_out_the_apps_notes.py --noconftest -q
"""
import inspect
import re
import time

import pytest

PERSON = 'apps_notes_test'
RR = 'hartos.reuse_recipe'


class _FakeExtractor:
    """Stands in for sherpa_onnx's extractor at the model boundary."""


@pytest.fixture
def vp(monkeypatch):
    from integrations.audio import voiceprint
    import numpy as np
    voices = {'person': [1.0, 0.0, 0.0], 'other': [0.0, 1.0, 0.0]}
    monkeypatch.setattr(voiceprint, '_extractor', _FakeExtractor())
    monkeypatch.setattr(voiceprint, '_read_audio',
                        lambda path: np.full(voiceprint.SAMPLE_RATE, 0.1, dtype='float32'))
    monkeypatch.setattr(voiceprint, '_embed_samples',
                        lambda ex, samples: list(voices[voiceprint._current]))
    voiceprint.forget(PERSON)
    yield voiceprint
    voiceprint.forget(PERSON)


def _hear(vp, who, words):
    vp._current = who
    return vp.hear(PERSON, 'a.wav', words)


@pytest.fixture
def rr():
    return pytest.importorskip(RR)


class TestTheVoicesNoteIsGivenWhenItSaysSomething:

    def test_the_live_window_of_one_voice_is_not_given(self, vp):
        """THE DEFECT: two lines of the same voice saying "Yeah"."""
        _hear(vp, 'person', 'Yeah')
        _hear(vp, 'person', 'Yeah')
        assert vp.voice_window(PERSON) == ''

    def test_two_known_voices_are_told_apart(self, vp):
        _hear(vp, 'person', 'my name is Arun')
        _hear(vp, 'other', 'wait, one minute')
        window = vp.voice_window(PERSON)
        assert window.startswith(vp.VOICE_WINDOW_HEADER)
        assert window.endswith('voice A: "my name is Arun"\nvoice B: "wait, one minute"')

    def test_the_live_window_is_not_given(self, vp):
        """THE LIVE INPUT (langchain.log 12:36:52): the device remembered an
        earlier voice A, and the last two minutes held voice B twice.  A gate
        on the voices the device knows gave those two lines again, only
        reworded (review of 95339d8ff); the voices are counted in the
        window."""
        _hear(vp, 'person', 'hello')
        assert _hear(vp, 'other', 'Yeah')['voice'] == 'B'
        old = time.time() - vp.WINDOW_S - 5
        rows = vp._heard[PERSON]
        rows.clear()
        rows.append((old, 'A', 1.0, 'hello', ''))
        rows.append((time.time() - 80, 'B', 1.0, 'Yeah', ''))
        rows.append((time.time(), 'B', 1.0, 'Yeah', ''))
        assert len(vp._voices[PERSON]) == 2
        assert vp.voice_window(PERSON) == ''

    def test_one_voice_with_its_sounds_reaches_a_speech_agent(self, vp):
        """A child alone is one voice; the sounds are the capability."""
        _hear(vp, 'person', 'amma')
        vp._heard[PERSON].clear()
        vp._heard[PERSON].append((time.time(), 'A', 1.0, 'amma', 'a m a'))
        window = vp.voice_window(PERSON, phones=True)
        assert window.endswith('voice A: "amma" [a m a]')
        assert vp.VOICE_WINDOW_PHONES in window
        assert vp.voice_window(PERSON) == '', (
            'the same one voice, read without sounds, tells the reader nothing')

    def test_no_line_carries_a_clock_time(self, vp):
        _hear(vp, 'person', 'one')
        _hear(vp, 'other', 'two')
        assert not re.search(r'\d\d:\d\d', vp.voice_window(PERSON))

    def test_the_header_gives_the_model_no_order_and_names_no_account(self, vp):
        head = vp.VOICE_WINDOW_HEADER.lower()
        assert 'work out' not in head and 'account' not in head
        assert 'not a message from the person' in head


class TestTheNoToolRecordIsNotAStep:

    def _action(self, rr, steps):
        return {'action': 'Respond to user', 'recipe_source': rr._TRACE_RECIPE_SOURCE,
                'recipe': steps}

    def test_the_banker_and_the_reader_share_the_one_text(self, rr):
        from core.constants import NO_TOOL_STEP_RECORD
        import hartos.create_recipe as cr
        assert NO_TOOL_STEP_RECORD == 'no-op: action completed without tool execution', (
            'saved recipes hold this text; it must not change')
        assert "'steps': NO_TOOL_STEP_RECORD" in inspect.getsource(cr)
        assert 'no-op: action completed' not in inspect.getsource(cr)

    def test_the_live_action_shows_no_step(self, rr):
        from core.constants import NO_TOOL_STEP_RECORD
        shown = rr._saved_action_without_values(self._action(rr, [
            {'steps': NO_TOOL_STEP_RECORD, 'tool_name': '', 'generalized_functions': '',
             'agent_to_perform_this_action': 'Assistant'}]))
        assert shown['recipe'] == []
        assert 'completed' not in str(shown['recipe'])

    def test_real_steps_stay_and_the_saved_action_is_not_changed(self, rr):
        from core.constants import NO_TOOL_STEP_RECORD
        saved = self._action(rr, [
            {'steps': NO_TOOL_STEP_RECORD, 'tool_name': ''},
            {'steps': 'send_message_to_user({"text": "hi"})',
             'tool_name': 'send_message_to_user'}])
        shown = rr._saved_action_without_values(saved)
        assert [s['tool_name'] for s in shown['recipe']] == ['send_message_to_user']
        assert len(saved['recipe']) == 2

    def test_both_places_that_show_steps_read_through_it(self, rr):
        """The dispatch message and the prompt's recipe block."""
        assert '_saved_action_without_values(' in inspect.getsource(
            rr._build_reuse_action_message)
        assert inspect.getsource(rr).count('_saved_action_without_values(') >= 4


class TestTheUserIdIsNotSomethingToFetchOrSay:

    @pytest.mark.parametrize('module', ['hartos.reuse_recipe', 'hartos.create_recipe'])
    def test_the_prompt_rule_gives_no_order_to_fetch_it(self, module):
        src = inspect.getsource(pytest.importorskip(module))
        assert 'Get the user Id yourself always' not in src
        assert "never mention the user_id to the user" in src
        # A saved step may name get_user_id itself (review of 95339d8ff).
        assert "Only when a tool or the step asks for a user_id" in src

    def test_the_tool_says_the_id_is_not_for_the_person(self):
        import core.agent_tools as at
        src = inspect.getsource(at)
        at_tool = src[src.index('"get_user_id",'):][:400]
        assert 'never say or show it to them' in at_tool


class TestTheToollessSteerAsksForTheReply:

    def test_it_asks_for_the_persons_answer_not_a_report(self, rr):
        src = inspect.getsource(rr._reuse_fab_steer_message)
        assert "Write the action's actual" not in src
        assert 'as your reply to them' in src
        assert 'not a report of what you did' in src

    def test_it_is_still_recognised_as_this_modules_own_text(self, rr):
        """The steer is told from an answer by its marker, not its wording."""
        assert rr._REUSE_NOT_COMPLETE_MARKER == ' is NOT complete: '
        assert '_REUSE_NOT_COMPLETE_MARKER' in inspect.getsource(
            rr._reuse_fab_steer_message)
