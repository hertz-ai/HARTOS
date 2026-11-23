"""(B) The person's next words reach the agent with the step's open question,
for the agent to read in context, never pre-labelled as the answer.

MEASURED LIVE 2026-11-19 00:4x IST on the local 4B (dev run, the test
account): the reply turn's seed said "Their reply follows; judge it as the
answer to this step and do not ask the question again", and the agent did.
Cafe "I'd like a cappuccino" to "Could I ask what your name is?" took the
order and never asked the name again; rhyme "Sing me a rhyme about the moon"
to "sing the first line back" was praised as the child singing.

The user's ruling (19 Nov ~21:40 IST): the agent itself decides, reading the
whole conversation; "all modalities are feedback" (what the camera saw rides
in the same seed, _reuse_camera_window); no second model call.  The seed now
names the open question and says: an answer or an attempt (in words or a
gesture) is taken as the answer; anything else is answered, and the question
asked again only if still needed.  The StatusVerifier still decides whether
the step is done.

Replay of the six logged agent requests on the 4B, 3 runs per arm
(designs/B_labelled/agent_probe.py): cafe re-asks the name 0/3 -> 2/3 (the
third run read memory first); the four genuine answers are taken as answers
in both arms; the rhyme case is NOT fixed (0/3 -> 0/3).

Driven through the real chat_agent of test_reuse_reply_is_evidence.py (its
`speech` fixture, imported); only the model seats are scripted.

    python -m pytest tests/unit/test_the_agent_reads_the_reply_against_its_question.py -q
"""
import json

from tests.unit.test_reuse_reply_is_evidence import (  # noqa: F401  (fixture)
    ASK_LINE, speech)

STORY = 'tell me a story'
RE_ASK = 'A story after this one! Arjun, can you say ball for me?'


def _task(run, aid):
    return run.ledger.tasks[f'action_{aid}']


def _seed_of(run, since):
    return next(s for s in run.state['steers'][since:]
                if s.startswith(f'{run.rr._REUSE_ACTION_MESSAGE_PREFIX}1:'))


def _asked(speech, **kw):
    run = speech(ask_verdict='pending', **kw)
    run.turn('hi, I am ready', 'request-1')
    assert run.state['sends'] == [ASK_LINE]
    assert run.rr.user_tasks[run.key].current_action == 1
    return run


class TestTheSeedNamesTheQuestion:
    def test_the_reply_seed_carries_the_asked_question(self, speech):
        run = _asked(speech)
        since = len(run.state['steers'])
        run.turn('ba', 'request-2', kind='reply')
        seed = _seed_of(run, since)
        assert f'The question: {ASK_LINE}\nTheir words:\nba' in seed, seed

    def test_the_seed_does_not_tell_the_agent_the_words_are_the_answer(self, speech):
        run = _asked(speech)
        since = len(run.state['steers'])
        run.turn(STORY, 'request-2', kind='reply')
        seed = _seed_of(run, since)
        assert 'judge it as the answer to this step' not in seed.lower(), seed
        # The words are read against the question, with every modality; an
        # attempt is defined, a request for something else is excluded, and
        # "still needed" means what was asked for is still missing.
        assert 'what their camera saw' in seed and 'a gesture toward it' in seed, seed
        assert 'Asking you for something else is not an answer' in seed, seed
        assert 'only if what you asked for is still missing' in seed, seed

    def test_a_long_send_is_read_against_the_question_at_its_end(self, speech):
        """hartos-3e's MF1: an email agent sends the draft, then the question.
        The record keeps the first 1000 characters, so the head of a long
        draft would be shown as "The question:"."""
        draft = ('Dear Priya, ' + 'the Friday demo plan is below. ' * 45).strip()
        assert len(draft) > 1000
        send = f'{draft}\n\nWould you like me to make any changes?'

        def long_ask(state, aid, content):
            if aid == 1 and state['turn'] == 'ask' and not state['sends']:
                return {'content': '', 'tool_calls': [{
                    'id': 'draft', 'type': 'function',
                    'function': {'name': 'send_message_to_user',
                                 'arguments': json.dumps({'text': send,
                                                          'expects_reply': True})}}]}
            return None
        run = speech(ask_verdict='pending', assistant_script=long_ask)
        run.turn('write the demo email', 'request-1')
        assert run.state['sends'] == [send]
        since = len(run.state['steers'])
        run.turn('make it shorter', 'request-2', kind='reply')
        seed = _seed_of(run, since)
        assert ('The question: Would you like me to make any changes?\n'
                'Their words:\nmake it shorter') in seed, seed
        assert 'The question: Dear Priya' not in seed

    def test_the_ask_turn_seed_is_unchanged(self, speech):
        run = speech(ask_verdict='pending')
        run.turn('hi, I am ready', 'request-1')
        assert not any(run.rr._REUSE_REPLY_PREFACE in s or 'The question:' in s
                       for s in run.state['steers'])


class TestTheAskedPart:
    def _part(self, text, **kw):
        from hartos.lifecycle_hooks import asked_part
        return asked_part(text, **kw)

    def test_the_question_after_a_draft(self):
        text = 'Subject: Friday\n\nDear Priya, the plan is ready. Regards.\n\nAny changes?'
        assert self._part(text) == 'Any changes?'

    def test_a_two_part_question_stays_whole(self):
        assert (self._part("Welcome! What's your name? And what would you like to drink?")
                == "What's your name? And what would you like to drink?")

    def test_the_greeting_before_the_question_is_dropped(self):
        assert (self._part('Hello! Welcome to our cafe. Could I ask what your name is?')
                == 'Could I ask what your name is?')

    def test_no_question_mark_gives_the_tail(self):
        text = 'x' * 50 + ' Say ball for me.'
        assert self._part(text, limit=20) == text[-20:].strip() == 'xxx Say ball for me.'

    def test_nothing_gives_nothing(self):
        assert self._part(None) == '' and self._part('   ') == ''


def _re_ask(state, aid, content):
    """The agent reads the words as not answering, answers them, asks again."""
    if aid == 1 and state['turn'] == 'story' and len(state['sends']) == 1:
        return {'content': '', 'tool_calls': [{
            'id': 're_ask', 'type': 'function',
            'function': {'name': 'send_message_to_user',
                         'arguments': json.dumps({'text': RE_ASK,
                                                  'expects_reply': True})}}]}
    return None


def _still_waiting(state, aid, content):
    if aid == 1 and state['turn'] == 'story':
        return json.dumps({'status': 'pending', 'action_id': 1,
                           'message': "Arjun has not tried 'ball' yet",
                           'can_perform_without_user_input': 'no'})
    return None


class TestTheAgentsReadingDecides:
    def test_a_turn_the_agent_reads_as_no_answer_keeps_the_step_waiting(self, speech):
        from agent_ledger.core import TaskStatus
        run = _asked(speech, assistant_script=_re_ask, verdict_script=_still_waiting)
        reply = run.turn(STORY, 'request-2', kind='story')
        assert run.state['sends'] == [ASK_LINE, RE_ASK]
        assert run.rr.user_tasks[run.key].current_action == 1, reply
        assert _task(run, 1).status != TaskStatus.COMPLETED
        assert run.L.ledger_user_answered(run.key, 1, 0) is False
        answers = (_task(run, 1).context or {}).get(run.L.USER_INPUT_EVIDENCE_KEY) or []
        assert not any(a.get('answer') == STORY for a in answers), answers
        assert run.L.ledger_open_ask(run.key, 1, 0) is not None

    def test_the_next_answer_then_closes_the_step_with_its_own_words(self, speech):
        from agent_ledger.core import TaskStatus
        run = _asked(speech, assistant_script=_re_ask, verdict_script=_still_waiting)
        run.turn(STORY, 'request-2', kind='story')
        reply = run.turn('ba', 'request-3', kind='reply')
        assert run.rr.user_tasks[run.key].current_action == 4, reply
        assert _task(run, 1).status in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)
        assert _task(run, 1).result == 'ba', _task(run, 1).result
