"""Guard: the create flow must not clobber an agent's bound methods.

Measured defect, 2026-09-07.  Four sites in ``hartos/create_recipe.py``
(:5232, :5292, :5394, :5413) did::

    assistant_agent.update_system_message = '...instruction text...'

``update_system_message`` is a METHOD on autogen's ConversableAgent
(``agentchat/conversable_agent.py``; line differs between the two autogen
copies installed on this box), which sets
``self._oai_system_message[0]['content']``.  Assigning a str to that name
therefore did two things, neither of them the intended one:

  1. It did NOT update the system message — ``_oai_system_message`` was
     never touched, so the "respond strictly in an array [] format" /
     scheduler-schema steering never became a system message.
  2. It shadowed the bound method with a str, leaving the name
     non-callable for the rest of that agent's life.  Any later
     ``agent.update_system_message(...)`` would raise
     ``TypeError: 'str' object is not callable`` — the correct call form
     is already used at ``hartos/reuse_recipe.py:1446``.

The assignments were dead as well as destructive: at every one of the four
sites the byte-identical instruction is ALSO passed as ``message`` to
``chat_instructor.initiate_chat``, which is what actually reaches the
model.  So deleting them preserves behaviour and restores the method.

This test is functional: it runs the real ``create_recipe`` function
against a real ``autogen.ConversableAgent`` and asserts on the agent's
resulting state.  It is not a source-text or AST assertion.

Assertion 3 is the non-vacuity twin — it pins that removing the
assignment did not remove the steering, which still has to reach the
model through ``message``.  Without it this file would pass just as well
if someone deleted the instruction entirely.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

BASE_PERSONA = 'BASE PERSONA — must survive the scheduler step'


class _RecordingInstructor:
    """Stands in for chat_instructor; records what was actually sent."""

    def __init__(self):
        self.sent = []

    def initiate_chat(self, recipient=None, message=None, **kwargs):
        self.sent.append(message)
        return None


def _make_agent():
    import autogen
    return autogen.ConversableAgent(
        name='Assistant', llm_config=False, human_input_mode='NEVER',
        system_message=BASE_PERSONA)


def test_scheduler_step_leaves_update_system_message_callable():
    """The load-bearing assertion — this is the 2026-09-07 defect."""
    from hartos import create_recipe

    agent = _make_agent()
    assert callable(agent.update_system_message), (
        'precondition: autogen must expose update_system_message as a '
        'method, else this test proves nothing')

    instructor = _RecordingInstructor()
    create_recipe.begin_agent_convo_to_get_schedulers_not_last(
        agent, instructor, object(), 'pid-under-test',
        [{'action_id': 1, 'action': 'do the thing'}], 'user-prompt')

    assert callable(agent.update_system_message), (
        "the create flow replaced the agent's bound update_system_message "
        'with a str.  autogen defines it as a method on ConversableAgent; '
        'assigning to the name shadows it, so '
        'the system message is never set AND any later call raises '
        "TypeError: 'str' object is not callable.  The correct call form "
        'is at reuse_recipe.py:1446.  got %r' % (agent.update_system_message,))


def test_scheduler_step_does_not_wipe_the_base_system_message():
    """Non-regression: the fix must not start overwriting the persona.

    Converting the assignment into a real ``update_system_message(...)``
    call would have replaced the agent's whole system message with the
    scheduler schema — worse than the no-op it replaced.  Deleting the
    dead assignment is what keeps this green.
    """
    from hartos import create_recipe

    agent = _make_agent()
    create_recipe.begin_agent_convo_to_get_schedulers_not_last(
        agent, _RecordingInstructor(), object(), 'pid-under-test',
        [{'action_id': 1, 'action': 'do the thing'}], 'user-prompt')

    assert agent.system_message == BASE_PERSONA, (
        'the scheduler step overwrote the agent persona; it is only '
        'supposed to send a message.  got %r' % (agent.system_message,))


def test_the_scheduler_instruction_still_reaches_the_model():
    """Non-vacuity: the steering must still be sent, via `message`."""
    from hartos import create_recipe

    instructor = _RecordingInstructor()
    create_recipe.begin_agent_convo_to_get_schedulers_not_last(
        _make_agent(), instructor, object(), 'pid-under-test',
        [{'action_id': 1, 'action': 'do the thing'}], 'user-prompt')

    assert instructor.sent, 'the scheduler step sent nothing at all'
    body = instructor.sent[0]
    for token in ('scheduled_tasks', 'action_entry_point', 'cron_expression'):
        assert token in body, (
            'the scheduler schema no longer reaches the model — deleting '
            'the dead system-message assignment must not delete the '
            'instruction itself, which travels in `message`.  missing %r'
            % (token,))
