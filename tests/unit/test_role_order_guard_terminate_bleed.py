"""A consumed TERMINATE must never be merged into a later turn's content.

Measured live 2026-09-05 on the installed build, CREATE of agent "Relay"
(88601674818), action 3.  Three attempts ran in 137 ms total and the model was
never dialled — proven physically, not inferred: llama-server's /slots was
byte-identical before and after the turn, and a 4B cannot generate in 45 ms.

The chain, each link read in source rather than assumed:

  1. ChatInstructor is a UserProxyAgent with ``default_auto_reply="TERMINATE"``
     (create_recipe.py:1022), so ending action 2 leaves a message whose entire
     content is the bare token in ``groupchat.messages``.
  2. Action 3 re-enters with ``clear_history=False``, so that stale message is
     still there, now followed by the new "Execute Action 3: …" turns.
  3. ``validate_messages``' ROLE-ORDER-GUARD coalesces consecutive same-role
     messages by CONCATENATING their contents (helper.py:1115-1119).  The live
     log shows it merging exactly this shape: "coalesced 3 consecutive
     same-role pair(s) at 0+1(user), 1+2(user), 2+3(user)" — 4 messages into 1,
     where message[1] was the bare TERMINATE.
  4. The guard runs inside ``process_all_messages_before_reply``, which autogen
     applies BEFORE any reply function (conversable_agent.py:2059).
  5. ``check_termination_and_human_reply`` therefore tests the MERGED message,
     and ``_is_terminate_msg`` matches "TERMINATE" ANYWHERE in the content
     (helper.py:107) → it returns ``(True, None)`` (conversable_agent.py:1861).
  6. ``generate_reply`` returns None → ``run_chat`` breaks
     (groupchat.py:1190).  Zero LLM calls, no exception, ~15 ms per attempt.

So the retry budget burns three times instantly, ``_needs_input_reply`` fires,
and the HITL question repeats forever — measured 4/4 on Relay.

The fix keeps the alternation repair (its whole reason to exist) and only
stops a CONSUMED control token from being glued into unrelated content: a bare
TERMINATE that is not the last message is dropped, exactly as an empty
assistant placeholder already is.  A TERMINATE that IS last is untouched, so
real termination keeps working — that is what tests 2 and 3 pin.

    python -m pytest tests/unit/test_role_order_guard_terminate_bleed.py --noconftest -q
"""
import unittest

from flask import Flask

from hartos.helper import ToolMessageHandler, _is_terminate_msg


def _handler():
    return ToolMessageHandler(user_tasks={}, user_prompt='u1')


def _run(messages):
    app = Flask(__name__)
    with app.app_context():
        return _handler().validate_messages(messages)


def _u(name, content):
    return {'role': 'user', 'name': name, 'content': content}


class TerminateBleed(unittest.TestCase):

    # ---- THE REGRESSION ------------------------------------------------
    def test_stale_terminate_does_not_bleed_into_the_new_turn(self):
        """The live Relay shape: verifier JSON, consumed TERMINATE, new work."""
        out = _run([
            _u('StatusVerifier', '{"status": "completed", "action_id": 2}'),
            _u('ChatInstructor', 'TERMINATE'),
            _u('ChatInstructor', 'Execute Action 3: Extract version numbers.'),
            _u('ChatInstructor', 'Execute Action 3: Extract version numbers.'),
        ])
        self.assertTrue(out, 'the guard must not empty the message list')
        self.assertFalse(
            _is_terminate_msg(out[-1]),
            "autogen tests the LAST message with this exact predicate; a "
            "consumed TERMINATE merged into it ends the round before the "
            "model is ever called (Relay action 3, 137ms for 3 attempts)")
        self.assertIn('Execute Action 3', out[-1]['content'],
                      'the real work must survive the guard')

    # ---- termination itself must keep working --------------------------
    def test_bare_terminate_as_last_message_is_preserved(self):
        """A CURRENT terminate signal must still terminate."""
        out = _run([
            _u('Assistant', 'Here is the summary.'),
            _u('ChatInstructor', 'TERMINATE'),
        ])
        self.assertTrue(_is_terminate_msg(out[-1]),
                        'dropping a live TERMINATE would loop the group chat '
                        'forever — the opposite failure')

    def test_model_reply_ending_in_terminate_is_untouched(self):
        """autogen's own convention: a model ends its answer with TERMINATE."""
        text = 'The release is v1.2.0.\n\nTERMINATE'
        out = _run([_u('Assistant', text)])
        self.assertEqual(text, out[-1]['content'])
        self.assertTrue(_is_terminate_msg(out[-1]))

    # ---- no regression in what the guard already did -------------------
    def test_ordinary_consecutive_same_role_still_coalesces(self):
        """The alternation repair is the guard's job and must survive."""
        out = _run([
            _u('ChatInstructor', 'first part'),
            _u('ChatInstructor', 'second part'),
        ])
        self.assertEqual(1, len(out), 'consecutive same-role must still merge')
        self.assertIn('first part', out[0]['content'])
        self.assertIn('second part', out[0]['content'])

    def test_empty_assistant_placeholder_still_dropped(self):
        out = _run([
            {'role': 'assistant', 'name': 'Assistant', 'content': ''},
            _u('ChatInstructor', 'do the thing'),
        ])
        self.assertEqual(1, len(out))
        self.assertEqual('do the thing', out[0]['content'])

    def test_terminate_only_history_still_yields_a_message(self):
        """Degenerate input must not produce an empty list (autogen 400s)."""
        out = _run([
            _u('ChatInstructor', 'TERMINATE'),
            _u('ChatInstructor', 'TERMINATE'),
        ])
        self.assertTrue(out, 'never hand autogen an empty message list')


if __name__ == '__main__':
    unittest.main()
