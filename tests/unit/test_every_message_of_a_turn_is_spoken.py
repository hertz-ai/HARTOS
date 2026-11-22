"""Every message the person is sent in a turn is spoken, in order.

The defect (live walk, 22 Nov 2026, rhyme agent on a desktop): the agent
sent the rhyme with the send tool and then a line of praise.  Both were
shown.  Only the praise was spoken: a turn's speech is made once, by
_chat_reply, from the reply's text, and the reply is the newest send.

The publisher of an agent's message now keeps what was delivered to the
person during a turn (crossbar_publish.turn_sends, by the turn's request
id), and _chat_reply speaks those messages in order and then the reply,
each once (_turn_speech_text).  Nothing but text published TO the person
is in it: no group chat, no control text.

Real: publish_agent_message, turn_sends, _turn_speech_text, _chat_reply.
Faked: the realtime publisher (recorded) and the speech engine (recorded).
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.peer_link import crossbar_publish as cp  # noqa: E402

TURN = 'c0bee2d3-706e-4d53-98bf-ec7f1bd7df0f'
RHYME = 'Twinkle, twinkle, little star,\nhow I wonder what you are.'
PRAISE = 'Lovely singing, Arun!'


class _Turn(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._prev = os.environ.get('HEVOLVE_CACHE_DIR')
        os.environ['HEVOLVE_CACHE_DIR'] = self._tmpdir
        self.published = []
        self._attr = patch(
            'core.safe_hartos_attr.safe_hartos_attr',
            lambda name: (lambda topic, payload, **flags:
                          self.published.append(payload))
            if name == 'publish_async' else None)
        self._attr.start()
        self._kept = patch.dict(getattr(cp, '_turn_sends', {}), {}, clear=True)
        self._kept.start()

    def tearDown(self):
        self._kept.stop()
        self._attr.stop()
        if self._prev is None:
            os.environ.pop('HEVOLVE_CACHE_DIR', None)
        else:
            os.environ['HEVOLVE_CACHE_DIR'] = self._prev

    def send(self, text, turn=TURN, n=1234):
        """A mid-turn send, as send_message_to_user1 publishes it."""
        return cp.publish_agent_message(
            text=text, user_id='u1', prompt_id=7,
            request_id=f'{turn}-intermediate-{n}')

    def sends(self, turn=TURN):
        reader = getattr(cp, 'turn_sends', None)
        self.assertIsNotNone(reader, "the publisher keeps no record of a "
                                     "turn's sends")
        return reader(turn)

    def spoken(self, reply, turn=TURN, body=None):
        """What _chat_reply hands the speech engine for this turn."""
        import hart_intelligence_entry as hie
        calls = []
        with patch.object(hie, '_tts_synthesize_and_publish',
                          lambda text, *a, **k: calls.append(text)):
            with hie.app.test_request_context(
                    '/chat', json=body or {'media_mode': 'audio',
                                           'prompt': 'sing'}):
                hie._chat_reply('u1', turn, reply)
        return calls


class TheWholeTurnIsSpoken(_Turn):

    def test_the_rhyme_is_spoken_before_the_praise(self):
        """The reply is the newest send; the one before it was never said."""
        self.assertTrue(self.send(RHYME, n=1111))
        self.assertTrue(self.send(PRAISE, n=2222))
        self.assertEqual(self.spoken(PRAISE), [f'{RHYME}\n\n{PRAISE}'])

    def test_a_reply_that_is_no_send_is_spoken_after_them(self):
        self.send(RHYME)
        self.assertEqual(self.spoken('Shall we sing another?'),
                         [f'{RHYME}\n\nShall we sing another?'])

    def test_a_turn_that_sent_nothing_is_spoken_as_before(self):
        self.assertEqual(self.spoken('  Hello there.  '), ['  Hello there.  '])

    def test_one_send_that_is_the_reply_is_said_once(self):
        self.send(PRAISE)
        self.assertEqual(self.spoken(PRAISE), [PRAISE])

    def test_the_same_words_are_not_said_twice(self):
        """Sent twice, and the reply holds a send's words with more."""
        self.send(PRAISE, n=1)
        self.send('lovely   singing,  ARUN!', n=2)
        self.send('Now the moon rhyme.', n=3)
        self.assertEqual(
            self.spoken('Now the moon rhyme. Ready?'),
            [f'{PRAISE}\n\nNow the moon rhyme. Ready?'])

    def test_a_longer_message_takes_the_place_of_the_one_it_holds(self):
        self.send('Twinkle, twinkle', n=1)
        self.send(PRAISE, n=2)
        self.assertEqual(
            self.spoken('Twinkle, twinkle, little star'),
            [f'Twinkle, twinkle, little star\n\n{PRAISE}'])


class OnlyThisTurnsSends(_Turn):

    def test_another_turns_send_is_not_spoken_here(self):
        self.send(RHYME, turn='another-turn')
        self.assertEqual(self.spoken(PRAISE), [PRAISE])
        self.assertEqual(self.sends('another-turn'), [RHYME])

    def test_a_turn_is_spoken_once(self):
        self.send(RHYME)
        self.spoken(PRAISE)
        self.assertEqual(self.spoken(PRAISE), [PRAISE])

    def test_a_message_published_outside_a_turn_is_not_kept(self):
        """Reel Studio's notices carry their own request id."""
        self.assertTrue(cp.publish_agent_message(
            text='Your reel is ready.', user_id='u1', prompt_id=7,
            request_id='reel-job-42'))
        self.assertEqual(self.sends('reel-job-42'), [])
        self.assertEqual(dict(getattr(cp, '_turn_sends', {'x': 1})), {})

    def test_a_send_that_was_not_delivered_is_not_spoken(self):
        with patch('core.safe_hartos_attr.safe_hartos_attr',
                   lambda name: None):
            self.assertFalse(self.send(RHYME))
        self.assertEqual(self.spoken(PRAISE), [PRAISE])

    def test_a_send_whose_publish_raised_is_not_spoken(self):
        def boom(topic, payload, **flags):
            raise RuntimeError('realtime is down')
        with patch('core.safe_hartos_attr.safe_hartos_attr',
                   lambda name: boom):
            self.assertFalse(self.send(RHYME))
        self.assertEqual(self.spoken(PRAISE), [PRAISE])

    def test_empty_text_is_not_kept(self):
        self.send('   ')
        self.assertEqual(self.sends(), [])

    def test_the_text_kept_is_the_text_published(self):
        self.send(RHYME)
        self.assertEqual(self.published[-1]['text'], [RHYME])
        self.assertEqual(self.sends(), [RHYME])

    def test_only_so_many_turns_are_kept(self):
        """A background turn's sends are never collected."""
        cap = getattr(cp, '_TURN_SENDS_KEPT', 0)
        self.assertGreater(cap, 0)
        for i in range(cap + 5):
            self.send('one', turn=f'turn-{i}')
        self.assertEqual(len(cp._turn_sends), cap)
        self.assertEqual(self.sends('turn-0'), [])
        self.assertEqual(self.sends(f'turn-{cap + 4}'), ['one'])


class ASilentTurnStaysSilent(_Turn):

    def test_a_text_mode_turn_speaks_nothing_and_leaves_nothing_behind(self):
        self.send(RHYME)
        self.assertEqual(
            self.spoken(PRAISE, body={'media_mode': 'text', 'prompt': 'x'}),
            [])
        self.assertEqual(self.sends(), [])

    def test_a_background_turn_speaks_nothing(self):
        self.send(RHYME, turn='daemon_goal-1')
        import hart_intelligence_entry as hie
        calls = []
        with patch.object(hie, '_tts_synthesize_and_publish',
                          lambda text, *a, **k: calls.append(text)):
            with hie.app.app_context():
                hie._chat_reply('u1', 'daemon_goal-1', PRAISE)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
