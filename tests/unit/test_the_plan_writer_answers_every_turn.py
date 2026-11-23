"""The plan writer answers every message a person sends it, and a creation
that cannot be read keeps the request the person began with.

LIVE 2026-11-23 (installed Nunba 11c53312b + HARTOS e4f597137, local 4B
Qwen3.5-4B on :8082, account 10202, prompt_id 346 "Tamil Speech Buddy").  The
person sent the outline below, then answered the writer's questions.  Turns
1 to 10 each made one model call (llm_outbound.jsonl, source autogen.gather,
the last at 18:46:01 for request 6f90de90) and each reply was a readable
{"status": "pending", "question": ...}.  Turn 11, "Yes, that is accurate.",
made NO model call: frozen_debug.log shows "user_proxy_10202 (to
assistant_10202): Yes, that is accurate." and no reply, 34 ms from
'INSIDE GATHER INFo' to 'json_repair failed', no POST to :8082, and no
autogen.gather entry for request 6eb1032d.

Why: the writer is an autogen AssistantAgent with
max_consecutive_auto_reply=10, and a gather turn is user_proxy.send().
autogen counts the writer's replies to one sender and resets the count only
in initiate_chat (_prepare_chat) or on reaching the cap
(ConversableAgent.check_termination_and_human_reply, autogen-agentchat
0.2.37).  So the 11th message of a creation got no reply, and
get_agent_response read the last entry of the history, the person's own
words, as the writer's answer.  retrieve_json found no JSON in them,
"gather_info parse error on turn 11", and at turn 11 (>= MAX_GATHER_TURNS-2)
chat() salvaged at once: it saved 346.json with goal "Yes, that is
accurate.", one persona "Assistant" and one step "Respond to user".  The
outline, with its three personas, and ten answered questions were lost.

The writer tests build the real writer (create_agents_for_user) and give it
the model through autogen's own plug-in point (register_model_client), so
every reply passes the real termination check and generate_oai_reply.  The
salvage tests drive the real chat() through the gather-mirror harness.
"""
import json
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

import pytest  # noqa: E402

pytest.importorskip('autogen', reason='autogen not installed')

USER = '10202'
PROMPT = '346'

#: What the person sent, turn by turn, verbatim from the turn-10 request body
#: (llm_outbound.jsonl) and frozen_debug.log for turn 11.
OUTLINE = (
    "Speech therapy agent for a young child who speaks Tamil at home. Help the "
    "child first respond to their own name, then name their family members, "
    "building on the words the child already knows. Personas: Child, Parent, "
    "Therapist. Don't assume the child's name, family or known words: find "
    "them out from the people in the chat. Talk with the child in Tamil, keep "
    "it playful and short, and adapt when the child doesn't respond.")
PERSON_TURNS = (
    OUTLINE,
    "Call it Tamil Speech Buddy.",
    "Two flows: first the child responds to their own name, then the child "
    "names their family members.",
    "The child responds when called by their own name: looks up, answers, or "
    "says their name back.",
    "First find out the child's name from the parent or whoever is with the "
    "child, and remember it. Then call the child by that name in Tamil, wait "
    "a few seconds for a response, praise any response warmly, and if there "
    "is none, call again gently and ask the child to look here.",
    "The Parent gives the child's name. If no parent has joined, ask whoever "
    "is at the child's device. The Therapist observes and reviews progress in "
    "their own flow.",
    "Yes, that structure is right.",
    "Yes, that captures it. Go on to the family members flow.",
    "First ask the parent, or whoever is at the child's device, which family "
    "members the child knows and what the child calls them, and remember "
    "those words. Then say one family word at a time in Tamil, ask the child "
    "who it is, wait for a reply, praise any attempt, and say the word again "
    "if the child does not respond. Start with words the child already knows "
    "before adding new ones.",
    "The child names at least one family member in Tamil when asked.",
    "Yes, that is accurate.",
)
LAST_ANSWER = PERSON_TURNS[-1]

#: The 4B's reply to turn 10, verbatim (frozen_debug.log 18:46:01): the shape
#: of every reply it gave in this creation.
LIVE_TURN_10_REPLY = (
    '{"status": "pending", "question": "Perfect goal! Now, let\'s lock in the '
    '**actions** for the **Child** persona in the Family Members flow:\\n\\n'
    '1. Ask the Parent/Device User which family members the child knows and '
    'the Tamil words used.\\n2. Store these words in memory.\\n3. Select one '
    'known family word.\\n4. Say the family word in Tamil.\\n5. Ask the child '
    'to identify who it is.\\n6. Wait for a reply.\\n7. Praise any attempt '
    'warmly.\\n8. If no response, repeat the word.\\n\\nDoes this accurately '
    'reflect your plan for this flow?"}')


class WriterModel:
    """The writer's model, as an autogen model client: it answers the n-th
    call with a pending question in the live shape, and keeps what each call
    was asked."""
    asked = []

    def __init__(self, config, **kwargs):
        self.config = config

    def create(self, params):
        WriterModel.asked.append(params['messages'][-1]['content'])
        n = len(WriterModel.asked)
        text = (LIVE_TURN_10_REPLY if n == 10 else json.dumps(
            {'status': 'pending', 'question': f'Question {n + 1}?'}))
        message = SimpleNamespace(content=text, function_call=None, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)],
                               model='local', cost=0)

    def message_retrieval(self, response):
        return [choice.message.content for choice in response.choices]

    def cost(self, response):
        return 0

    @staticmethod
    def get_usage(response):
        return {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0,
                'cost': 0, 'model': 'local'}


@pytest.fixture
def writer():
    """The real interactive writer for USER's creation of PROMPT, on the
    model above, kept where gather_info keeps it."""
    from hartos import gather_agentdetails as gad
    from hartos.threadlocal import thread_local_data
    WriterModel.asked = []
    thread_local_data.set_model_config_override(
        [{'model': 'local', 'model_client_cls': 'WriterModel'}])
    try:
        assistant, user_proxy = gad.create_agents_for_user(USER)
    finally:
        thread_local_data.clear_model_config_override()
    assistant.register_model_client(model_client_cls=WriterModel)
    key = f'{USER}_{PROMPT}'
    gad.user_agents[key] = (assistant, user_proxy)
    yield assistant, user_proxy
    gad.user_agents.pop(key, None)


def _send(writer, text):
    from hartos.gather_agentdetails import get_agent_response
    assistant, user_proxy = writer
    return get_agent_response(assistant, user_proxy, text)


def test_the_eleventh_message_of_a_creation_is_answered_by_the_model(writer):
    """FAILS BEFORE THE FIX: the 11th send made no model call and the turn
    read "Yes, that is accurate." back as the writer's answer."""
    replies = [_send(writer, text) for text in PERSON_TURNS]
    assert replies[9] == LIVE_TURN_10_REPLY
    assert replies[10] != LAST_ANSWER, (
        "the person's own words came back as the writer's answer")
    assert json.loads(replies[10])['status'] == 'pending', replies[10]
    assert WriterModel.asked == list(PERSON_TURNS), (
        f'the model was asked {len(WriterModel.asked)} times for '
        f'{len(PERSON_TURNS)} messages')


def test_a_long_creation_is_answered_on_every_turn(writer):
    """No message is dropped later either (FAILS BEFORE THE FIX: every 11th
    send went unanswered, the 11th and the 22nd here)."""
    texts = [f'answer {n}' for n in range(1, 24)]
    replies = [_send(writer, text) for text in texts]
    assert [r for r, t in zip(replies, texts) if r == t] == []
    assert len(WriterModel.asked) == len(texts)


def test_a_message_the_writer_does_not_answer_is_not_read_as_its_answer(writer):
    """When the writer gives no reply, the turn says so; the person's words
    are never handed on as the plan (FAILS BEFORE THE FIX: they were)."""
    from core.agent_tools import is_user_facing_error
    assistant, user_proxy = writer
    assistant.update_max_consecutive_auto_reply(0, user_proxy)  # it will not answer
    reply = _send(writer, LAST_ANSWER)
    assert reply != LAST_ANSWER
    assert is_user_facing_error(reply), reply
    assert 'no answer' in reply, reply
    assert WriterModel.asked == []
    # The message stays once, unanswered, as a failed send leaves it.
    assert [m['content'] for m in user_proxy.chat_messages[assistant]] == [LAST_ANSWER]


def test_the_request_a_creation_began_with_is_the_writers_first_message(writer):
    from hartos.gather_agentdetails import first_request
    for text in PERSON_TURNS:
        _send(writer, text)
    assert first_request(USER, PROMPT) == OUTLINE
    assert first_request(USER, 'no-such-prompt') is None


# --- chat()'s salvage, through the gather-mirror harness -------------------

def _salvaged(tmp_path, reply, *, turn_before, writer_history):
    from tests.unit.test_gather_turn_mirrors_user_side import PROMPT_ID, _drive
    payload, _persist, _memory, sent = _drive(
        tmp_path, reply, turn_before=turn_before, user_text=LAST_ANSWER,
        writer_history=writer_history)
    assert payload['Agent_status'] == 'Review Mode', payload
    return json.loads((tmp_path / f'{PROMPT_ID}.json').read_text()), sent


def test_a_salvaged_creation_keeps_the_request_it_began_with(tmp_path):
    """Turn 11 of 346 as the installed build read it: the 'reply' was the
    person's own last answer.  FAILS BEFORE THE FIX: the goal and the flow's
    sub_goal were "Yes, that is accurate."."""
    saved, _sent = _salvaged(tmp_path, LAST_ANSWER, turn_before=10,
                             writer_history=list(PERSON_TURNS[:-1]))
    assert saved['goal'] == OUTLINE, saved['goal']
    assert 'Personas: Child, Parent, Therapist' in saved['goal']
    assert saved['flows'][0]['sub_goal'] == OUTLINE
    assert saved['status'] == 'pending'


def test_the_last_turns_salvage_never_saves_the_wrap_up_instruction(tmp_path):
    """On the forced-completion turn the writer is sent the wrap-up
    instruction, not the person's words.  FAILS BEFORE THE FIX: that
    instruction was saved as the agent's goal."""
    saved, sent = _salvaged(tmp_path, 'not a config', turn_before=11,
                            writer_history=list(PERSON_TURNS[:-1]))
    assert sent and sent[0].startswith('Please finalize'), sent
    assert saved['goal'] == OUTLINE, saved['goal']


class _NoWriter(list):
    """A writer that holds no message (none was kept in this process)."""

    def append(self, item):
        pass


def test_with_no_writer_history_the_salvage_keeps_the_persons_words(tmp_path):
    """Nothing better is held: the person's own words of this turn, never the
    wrap-up instruction (FAILS BEFORE THE FIX)."""
    saved, _sent = _salvaged(tmp_path, 'not a config', turn_before=11,
                             writer_history=_NoWriter())
    assert saved['goal'] == LAST_ANSWER, saved['goal']
