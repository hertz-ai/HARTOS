"""Every message the person is sent in a turn is spoken, in order.

The defect (live walk, 22 Nov 2026, rhyme agent on a desktop): the agent
sent the rhyme with the send tool and then a line of praise.  Both were
shown.  Only the praise was spoken: a turn's speech is made once, by
_chat_reply, from the reply's text, and the reply is the newest send.

The publisher of an agent's message now keeps what was delivered to the
person during a turn (crossbar_publish.turn_sends, by the turn's request
id), and _chat_reply speaks those messages in order and then the reply,
each once (crossbar_publish.turn_speech_text).  Nothing but text published
TO the person is in it: no group chat, no control text.

Review of 5f3b2633c (rv3w): a first run's (CREATE's) sends were missed, a
reply that held an earlier send was said before what came between, a
reply that repeated two sends said one twice, "Ok" after "Look, a book!"
was never said, and the joined text had no bound.  The tests in
TheReviewOfTheFirstFix are those shapes.  Review of a6dbff622 (rv3w): a
reply an earlier send held was dropped, so "Ba!" after "Say 'ba'." went
unsaid; TheReplyIsNeverDropped.

Real: publish_agent_message, turn_sends, turn_speech_text, _chat_reply, and
CREATE's send_message_to_user1 (lifted by name, as
test_send_message_tool_reports_delivery does: create_recipe cannot be
imported in a bare env).
Faked: the realtime publisher (recorded) and the speech engine (recorded).
"""
import ast
import json
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
        """Sent twice, and the reply holds a send's words with more.  The
        same words sent again are said where they were said last."""
        self.send(PRAISE, n=1)
        self.send('lovely   singing,  ARUN!', n=2)
        self.send('Now the moon rhyme.', n=3)
        self.assertEqual(
            self.spoken('Now the moon rhyme. Ready?'),
            ['lovely   singing,  ARUN!\n\nNow the moon rhyme. Ready?'])

    def test_a_longer_message_takes_the_place_of_the_one_it_holds(self):
        """Where the longer one was shown: after the praise (rv3w)."""
        self.send('Twinkle, twinkle', n=1)
        self.send(PRAISE, n=2)
        self.assertEqual(
            self.spoken('Twinkle, twinkle, little star'),
            [f'{PRAISE}\n\nTwinkle, twinkle, little star'])


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


_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def _create_send(request_id_list):
    """CREATE's real send_message_to_user1, lifted by name.  On a desktop
    (is_bundled) it publishes through publish_agent_message."""
    path = os.path.join(_ROOT, 'hartos', 'create_recipe.py')
    with open(path, encoding='utf-8') as f:
        src = f.read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef)
              and n.name == 'send_message_to_user1')
    from core.config_cache import is_bundled
    ns = {'json': json, 'request_id_list': request_id_list,
          'is_bundled': is_bundled}
    exec(ast.get_source_segment(src, fn), ns)
    return ns['send_message_to_user1']


def _bound(test):
    cap = getattr(cp, '_TURN_SPEECH_MAX_CHARS', 0)
    test.assertGreater(cap, 0, "a turn's joined speech has no bound")
    return cap


class TheReviewOfTheFirstFix(_Turn):
    """rv3w's shapes, measured on 5f3b2633c."""

    def test_a_first_runs_sends_are_spoken(self):
        """CREATE writes the turn's id with '-intermediate' and no number
        after it (create_recipe.send_message_to_user1)."""
        send = _create_send({'u1_7': TURN})
        with patch.dict(os.environ, {'NUNBA_BUNDLED': '1'}):
            out = send('u1', RHYME, '', 7)
        self.assertEqual(out, 'Message sent successfully to user with '
                              f'request_id: {TURN}-intermediate')
        self.assertEqual(self.published[-1]['request_id'],
                         f'{TURN}-intermediate')
        self.assertEqual(self.spoken(PRAISE), [f'{RHYME}\n\n{PRAISE}'])

    def test_a_reply_that_holds_a_send_is_said_after_what_came_between(self):
        """"Great!", the rhyme, then the reply "Great! You sang it all.":
        the reply is said where it was shown, after the rhyme."""
        self.send('Great!', n=1)
        self.send(RHYME, n=2)
        self.assertEqual(self.spoken('Great! You sang it all.'),
                         [f'{RHYME}\n\nGreat! You sang it all.'])

    def test_a_reply_that_repeats_two_sends_says_each_once(self):
        """The marks around a word are not part of it: 'Arun!' is 'Arun.'."""
        self.send(PRAISE, n=1)
        self.send('Now the moon rhyme.', n=2)
        reply = 'Lovely singing, Arun. Now the moon rhyme!'
        self.assertEqual(self.spoken(reply), [reply])

    def test_a_short_reply_is_said_though_a_send_has_its_letters(self):
        """The parent said the reply on every turn.  5f3b2633c compared
        letters: 'ok' was found in 'Look' and the reply never said.  Words
        are compared whole, and the reply is the last thing said."""
        for sent, reply in (('Look, a book!', 'Ok'),
                            ('Yesterday we sang the star rhyme.', 'Yes')):
            with self.subTest(reply=reply):
                turn = f'turn-{reply}'
                self.send(sent, turn=turn)
                said = self.spoken(reply, turn=turn)
                self.assertEqual(len(said), 1, said)
                self.assertEqual(said[0].split('\n\n')[-1], reply, said)

    def test_a_word_is_whole_in_every_script(self):
        """A Tamil vowel sign is part of its word: amma (mother) after ammi
        (grinding stone) is another word, and said."""
        self.send('அம்மி', n=1)
        self.assertEqual(self.spoken('அம்மா'), ['அம்மி\n\nஅம்மா'])

    def test_the_same_words_sent_again_are_said_where_they_were_last(self):
        """The marks before a word are not part of it either."""
        self.send("Say 'ba'!", n=1)
        self.send('Good try!', n=2)
        self.assertEqual(self.spoken('Say ba.'), ['Good try!\n\nSay ba.'])

    def test_a_reply_an_earlier_send_already_says_is_said_again(self):
        """The reply is never dropped (review of a6dbff622)."""
        held = f'{PRAISE} Now the moon rhyme.'
        self.send(held)
        self.assertEqual(self.spoken(PRAISE), [f'{held}\n\n{PRAISE}'])

    def test_a_long_turn_leaves_out_its_oldest_whole_messages(self):
        """No message is cut, so a rhyme never stops mid-line."""
        cap = _bound(self)
        verses = ['\n'.join(f'Line {k}.{i} of the verse, sung slowly.'
                            for i in range(8))
                  for k in range(cap // 200 + 3)]
        for k, verse in enumerate(verses):
            self.send(verse, n=k)
        said = self.spoken(PRAISE)
        self.assertEqual(len(said), 1, said)
        self.assertLessEqual(len(said[0]), cap)
        parts = said[0].split('\n\n')
        self.assertEqual(parts[-1], PRAISE)
        kept = parts[:-1]
        self.assertTrue(kept)
        self.assertEqual(kept, verses[len(verses) - len(kept):])
        one_more = verses[len(verses) - len(kept) - 1:] + [PRAISE]
        self.assertGreater(len('\n\n'.join(one_more)), cap)

    def test_a_turn_exactly_at_the_bound_is_said_whole(self):
        cap = _bound(self)
        filler = ('la ' * cap)[:cap - len(PRAISE) - 2]
        self.send(filler)
        self.assertEqual(self.spoken(PRAISE), [f'{filler}\n\n{PRAISE}'])

    def test_the_reply_is_never_left_out(self):
        """Over the bound the oldest message goes, though it holds the
        reply's words: the reply itself is said."""
        cap = _bound(self)
        holder = f'{PRAISE} Now the moon rhyme.'
        big = ('Hey diddle diddle, ' * cap)[:cap - len(holder)]
        self.send(holder, n=1)
        self.send(big, n=2)
        self.assertEqual(self.spoken(PRAISE), [f'{big}\n\n{PRAISE}'])

    def test_a_reply_longer_than_the_bound_is_said_alone_as_before(self):
        cap = _bound(self)
        reply = 'Once upon a time there was a star. ' * (cap // 30 + 2)
        self.send(RHYME)
        self.assertEqual(self.spoken(reply), [reply])

    def test_a_long_send_that_holds_the_reply_gives_the_reply_alone(self):
        cap = _bound(self)
        long_send = f"{PRAISE} {'Sing it again and again. ' * (cap // 20)}"
        self.send(long_send)
        self.assertEqual(self.spoken(PRAISE), [PRAISE])

    def test_an_empty_reply_says_nothing_and_leaves_nothing_behind(self):
        self.send(RHYME)
        self.assertEqual(self.spoken(''), [])
        self.assertEqual(self.sends(), [])
        speech = getattr(cp, 'turn_speech_text', None)
        self.assertIsNotNone(speech, "no reader of a turn's speech")
        self.send(RHYME)
        self.assertEqual(speech(TURN, ''), '')
        self.assertEqual(self.sends(), [])

    def test_a_send_with_no_turn_id_is_not_kept(self):
        """'-intermediate-<n>' with nothing in front names no turn: two such
        turns, of two people, would be spoken each other's sends."""
        self.send(RHYME, turn='')
        self.assertEqual(self.sends(''), [])


class TheReplyIsNeverDropped(_Turn):
    """Review of a6dbff622 (rv3w): a reply whose words an earlier send held
    was dropped, so the child's cue to repeat went unsaid.  The parent
    spoke the reply on every turn."""

    def test_the_cue_after_say_ba_is_said(self):
        self.send("Say 'ba'.", n=1)
        self.send('Good try!', n=2)
        self.assertEqual(self.spoken('Ba!'),
                         ["Say 'ba'.\n\nGood try!\n\nBa!"])

    def test_the_tamil_model_word_is_said(self):
        """'Say amma.', then the reply 'Amma!'."""
        self.send('அம்மா என்று சொல்லுங்கள்.', n=1)
        self.assertEqual(self.spoken('அம்மா!'),
                         ['அம்மா என்று சொல்லுங்கள்.\n\nஅம்மா!'])

    def test_again_after_say_ba_again_is_said(self):
        self.send('Say ba again!', n=1)
        self.send('Listen: ba, ba, ba.', n=2)
        self.assertEqual(
            self.spoken('Again!'),
            ['Say ba again!\n\nListen: ba, ba, ba.\n\nAgain!'])

    def test_a_send_an_earlier_send_holds_is_said_too(self):
        """A later send is a cue as well: nothing is left out because an
        earlier message holds its words."""
        self.send("Say 'ba'.", n=1)
        self.send('Ba!', n=2)
        self.assertEqual(self.spoken('Good try!'),
                         ["Say 'ba'.\n\nBa!\n\nGood try!"])

    def test_an_earlier_message_a_later_one_has_the_letters_of_is_said(self):
        """Only whole words take a message's place: "Ok" is no word of
        "Look, a book!", nor "Yes" of "Yesterday"."""
        for sent, reply in (('Ok', 'Look, a book!'),
                            ('Yes', 'Yesterday we sang the star rhyme.')):
            with self.subTest(sent=sent):
                turn = f'turn-{sent}'
                self.send(sent, turn=turn)
                self.assertEqual(self.spoken(reply, turn=turn),
                                 [f'{sent}\n\n{reply}'])

    def test_a_fault_in_the_turns_speech_costs_the_sends_not_the_reply(self):
        import hart_intelligence_entry as hie
        self.send(RHYME)
        calls = []

        def broken(request_id, reply):
            raise RuntimeError('a fault in the turn speech')

        with patch.object(cp, 'turn_speech_text', broken), \
                patch.object(hie, '_tts_synthesize_and_publish',
                             lambda text, *a, **k: calls.append(text)):
            with hie.app.test_request_context(
                    '/chat', json={'media_mode': 'audio', 'prompt': 'sing'}):
                try:
                    resp = hie._chat_reply('u1', TURN, PRAISE)
                except RuntimeError as e:
                    self.fail(f'the reply was lost with the speech: {e}')
        self.assertEqual(resp.get_json()['response'], PRAISE)
        self.assertEqual(calls, [PRAISE])


if __name__ == '__main__':
    unittest.main()
