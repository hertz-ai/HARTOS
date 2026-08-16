"""The canonical /chat client: idempotent normalizer, declared classification.

THE BUG IT CLOSES
─────────────────
No shared /chat client existed. 9 of 11 callers never mentioned request_id, and
hart_intelligence_entry does no defaulting, so dispatch.is_genuine_user_request
classified every one of them as BACKGROUND — including D-Bus users, CLI users, the
desktop intent bar, and every Discord/Telegram/WhatsApp user. Background turns do
not fire the foreground gate, are admitted to the llama scheduler as kind='daemon'
(which is never preempted FOR), and run on the closable client so a later preempt
can abort the human's own request.

These are behavioural tests against the real functions. The passthrough tests are
the important ones: request_id is a correlation + dedup key, and a second minting
layer reintroduces the duplicate-TTS class already fixed once
(hart_intelligence_entry.py:2350).
"""
import os
import sys
import unittest
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from core.chat_client import (  # noqa: E402
    DAEMON_PREFIX, daemon_request_id, is_user_turn, mint_request_id,
    normalize_chat_body, post_chat,
)


class TheNormalizerIsIdempotent(unittest.TestCase):
    """Steward: 'idempotent if any previous layer already handled what it
    enforces and act like passthrough for those. No redundant parallel
    generations.'"""

    def test_an_existing_request_id_passes_through_UNTOUCHED(self):
        body = {'user_id': 'u', 'prompt': 'hi', 'request_id': 'from-nunba-1234'}
        out = normalize_chat_body(body)
        self.assertEqual('from-nunba-1234', out['request_id'],
                         "a previous layer's request_id was REGENERATED — that "
                         "breaks the frozen_debug<->llm_outbound 1:1 correlation "
                         "and re-opens the duplicate-TTS dedup bug")

    def test_passthrough_wins_even_when_a_daemon_id_is_offered(self):
        """An upstream id is authoritative; a caller's hint must not override it."""
        out = normalize_chat_body({'request_id': 'real-user-9'}, daemon_id='goal7')
        self.assertEqual('real-user-9', out['request_id'])

    def test_normalizing_twice_changes_nothing(self):
        once = normalize_chat_body({'prompt': 'hi'})
        twice = normalize_chat_body(once)
        self.assertEqual(once['request_id'], twice['request_id'],
                         "second pass re-minted — the normalizer is not idempotent")

    def test_the_callers_dict_is_NEVER_mutated(self):
        """Callers reuse bodies; editing the input turns one missing field into a
        shared-state bug."""
        body = {'prompt': 'hi'}
        normalize_chat_body(body)
        self.assertNotIn('request_id', body)

    def test_an_EMPTY_string_id_is_treated_as_absent(self):
        """'' is what a forgotten field looks like after a .get(...,'') — it must
        be filled, not passed through as a valid id."""
        out = normalize_chat_body({'request_id': ''})
        self.assertTrue(out['request_id'], "empty request_id was passed through")


class ClassificationIsDeclaredNotInferred(unittest.TestCase):
    """Steward policy: chat-session-backed turns are PRIORITY; seeded goals and
    daemon agents are DAEMON by default — and both say so explicitly."""

    def test_a_conversation_turn_is_classified_as_a_USER(self):
        out = normalize_chat_body({'prompt': 'install firefox'})
        self.assertTrue(is_user_turn(out),
                        "a conversation-backed turn was not classified as a user — "
                        "it will queue behind the flywheel and never preempt")

    def test_daemon_work_carries_an_EXPLICIT_tag(self):
        out = normalize_chat_body({'prompt': 'seeded goal'}, daemon_id='abc123')
        self.assertEqual('daemon_abc123', out['request_id'])
        self.assertFalse(is_user_turn(out))

    def test_the_daemon_tag_matches_dispatch_s_rule_exactly(self):
        """dispatch.is_genuine_user_request is the SOLE authority; this must agree
        with it, not carry a second rule."""
        from integrations.agent_engine.dispatch import is_genuine_user_request
        user = normalize_chat_body({'prompt': 'x'})
        daemon = normalize_chat_body({'prompt': 'x'}, daemon_id='g1')
        self.assertTrue(is_genuine_user_request(user['request_id']))
        self.assertFalse(is_genuine_user_request(daemon['request_id']))

    def test_emptiness_is_no_longer_a_classification(self):
        """The whole defect: an omitted field was indistinguishable from a
        declared daemon. After normalization neither side is ever empty."""
        self.assertNotEqual('', normalize_chat_body({})['request_id'])
        self.assertNotEqual('', normalize_chat_body({}, daemon_id='g')['request_id'])


class ItAgreesWithNunbaOnShape(unittest.TestCase):

    def test_minted_ids_use_nunbas_rule(self):
        """Nunba chatbot_routes.py:2529 mints str(int(time.time())). Divergent
        shapes are how correlation keys rot, so this must match."""
        rid = mint_request_id()
        self.assertTrue(rid.isdigit(), "minted id is not Nunba's numeric shape: %r" % rid)

    def test_daemon_prefix_is_the_one_spelling(self):
        self.assertEqual('daemon_', DAEMON_PREFIX)
        self.assertTrue(daemon_request_id('x').startswith(DAEMON_PREFIX))


class ItPostsThroughTheOneScheduledPath(unittest.TestCase):

    def test_post_chat_goes_via_pooled_post_not_a_second_http_path(self):
        """pooled_post is where llama slot admission + foreground preempt live.
        A bare requests.post would bypass the scheduler entirely."""
        with patch('core.http_pool.pooled_post') as pp:
            post_chat('http://localhost:6777/chat', {'prompt': 'hi'})
        self.assertTrue(pp.called, "post_chat did not route through pooled_post")
        sent = pp.call_args.kwargs['json']
        self.assertTrue(is_user_turn(sent),
                        "the body that actually went on the wire was not a user turn")

    def test_it_does_not_impose_the_old_30s_budget(self):
        """liquid_ui_service hardcoded timeout=30 against a /chat measured at
        49-76s, so the A2UI compose loop could only ever time out."""
        with patch('core.http_pool.pooled_post') as pp:
            post_chat('http://x/chat', {'prompt': 'hi'})
        self.assertGreater(pp.call_args.kwargs['timeout'], 30,
                           "default timeout is still under the measured /chat p50")


if __name__ == '__main__':
    unittest.main()
