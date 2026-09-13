"""A lightning-instrumented agent must speak as ITSELF inside a GroupChat.

THE LIVE FAILURE (2026-09-13, installed Nunba, CREATE of agent 87400889007,
flow 1): the Assistant's LLM call at 10:26:29 generated 138 tokens and no tool
call, yet the group log recorded the Assistant's turn as the ChatInstructor's
dispatch, verbatim ("Execute Action 1: verify_device_permissions ...").  The
StatusVerifier answered "completed", and the action was banked having run
nothing.  That day's logs hold 25 Execute dispatches and 24 such echoes, and
all 61 Assistant entries carry role=assistant -- the role a GroupChatManager
gives messages it SENT, never ones it received.  Replaying the exact logged
request against the same model returned a tool call twice and fresh prose once:
the model never echoed.

instrument_autogen_agent returned the AgentLightningWrapper PROXY, and create
and reuse put that proxy into their GroupChat.  The proxy forwards attribute
reads to the real agent, so its send() runs as the real agent and the manager
files the reply under the real agent.  run_chat then reads
last_message(proxy) and gets the manager's own broadcast to the proxy, which
GroupChat.append relabels as the Assistant's message.
"""

from unittest.mock import patch

import autogen
from openai.types.chat import ChatCompletion

from integrations.agent_lightning import wrapper as lightning

REPLY = 'the agent really answered'
DISPATCH = 'Execute Action 1: verify_device_permissions'


def _completion(*_args, **_kwargs):
    make = getattr(ChatCompletion, 'model_validate', None) or ChatCompletion.parse_obj
    return make({
        'id': 'x', 'object': 'chat.completion', 'created': 0, 'model': 'local',
        'choices': [{'index': 0, 'finish_reason': 'stop',
                     'message': {'role': 'assistant', 'content': REPLY}}],
        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})


def _assistant():
    return autogen.AssistantAgent(
        'Assistant',
        llm_config={'config_list': [{'model': 'local', 'api_key': 'x',
                                     'base_url': 'http://127.0.0.1:9/v1'}],
                    'cache_seed': None})


def _instrument(agent):
    with patch.object(lightning, 'is_enabled', return_value=True):
        return lightning.instrument_autogen_agent(
            agent, 'create_recipe_assistant_test',
            track_rewards=False, auto_trace=False)


def _assistant_turns(assistant):
    """One dispatch -> one Assistant turn; the group log's Assistant entries."""
    instructor = autogen.UserProxyAgent(
        'ChatInstructor', human_input_mode='NEVER',
        code_execution_config=False, llm_config=False)
    verifier = autogen.UserProxyAgent(
        'StatusVerifier', human_input_mode='NEVER',
        code_execution_config=False, llm_config=False)

    def pick(last_speaker, _groupchat):
        return assistant if last_speaker is instructor else None

    group = autogen.GroupChat(
        agents=[assistant, instructor, verifier], messages=[], max_round=4,
        speaker_selection_method=pick, allow_repeat_speaker=False)
    manager = autogen.GroupChatManager(groupchat=group, llm_config=False)
    with patch('openai.resources.chat.completions.Completions.create', _completion):
        instructor.initiate_chat(manager, message=DISPATCH,
                                 clear_history=True, silent=True)
    return [m.get('content') for m in group.messages if m.get('name') == 'Assistant']


def test_the_group_log_records_the_instrumented_agents_own_reply():
    """THE LIVE SHAPE: the dispatch must not come back as the agent's turn."""
    turns = _assistant_turns(_instrument(_assistant()))
    assert turns == [REPLY], (
        "the group log recorded %r as the Assistant's turn -- the manager's "
        "broadcast, not the model's reply" % (turns,))


def test_the_instrumentation_is_still_applied():
    """What instrumenting is FOR: the agent's generate_reply is traced."""
    agent = _instrument(_assistant())
    assert hasattr(agent.generate_reply, '__wrapped__')
