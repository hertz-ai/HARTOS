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
    was asked.  With ``fails`` set, each call raises it, as the OpenAI client
    raises a server's refusal."""
    asked = []
    fails = None

    def __init__(self, config, **kwargs):
        self.config = config

    def create(self, params):
        WriterModel.asked.append(params['messages'][-1]['content'])
        if WriterModel.fails is not None:
            raise WriterModel.fails
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
    WriterModel.fails = None
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
    WriterModel.fails = None


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
    are never handed on as the plan (FAILS BEFORE THE FIX: they were).  It
    says it in the person's words: what happened and what to do, send the
    same message again, which the kept message makes true.  FAILS ON
    7112a12f7: "the plan writer gave no answer to that message" named a part
    the person never sees and asked nothing of them (review of 8871f98a4)."""
    from core.agent_tools import is_user_facing_error
    assistant, user_proxy = writer
    assistant.update_max_consecutive_auto_reply(0, user_proxy)  # it will not answer
    reply = _send(writer, LAST_ANSWER)
    assert reply != LAST_ANSWER
    assert is_user_facing_error(reply), reply
    assert 'no answer' in reply, reply
    assert 'send the same message again' in reply, reply
    assert 'plan writer' not in reply, reply
    assert WriterModel.asked == []
    # The message stays once, unanswered, as a failed send leaves it.
    assert [m['content'] for m in user_proxy.chat_messages[assistant]] == [LAST_ANSWER]


#: What llama.cpp answers a request longer than its context, as the OpenAI
#: client raises it (the live 400 is quoted in core/agent_tool_menu.py).
LLAMA_OVERFLOW = (
    "Error code: 400 - {'error': {'code': 400, 'message': 'request (13378 "
    "tokens) exceeds the available context size (12288 tokens), try "
    "increasing it', 'type': 'exceed_context_size_error'}}")


# --- chat()'s salvage, through the gather-mirror harness -------------------

#: What the wrap-up asks the writer for: the config of 346, in the shape of
#: gather's completed template, with the steps the 4B itself proposed in its
#: replies to turns 7 and 10 and the three personas of the outline.
CONFIG_346 = {
    'status': 'completed', 'name': 'Tamil Speech Buddy',
    'agent_name': 'teach.local.buddy', 'broadcast_agent': False,
    'personas': [{'name': 'Child', 'description': 'The young learner'},
                 {'name': 'Parent', 'description': "Knows the child's words"},
                 {'name': 'Therapist', 'description': 'Reviews progress'}],
    'flows': [
        {'flow_name': 'Recognize Self', 'persona': 'Child',
         'actions': ["Check if a Parent or other helper is present; if not, ask "
                     "the person on the device for the child's name.",
                     "Store the child's name in memory.",
                     'Call the child by name in Tamil.',
                     'Wait for a response.',
                     'If a response occurs, praise the child warmly in Tamil.',
                     'If no response, wait and call again gently.'],
         'sub_goal': 'The child responds when called by their own name.'},
        {'flow_name': 'Identify Family', 'persona': 'Child',
         'actions': ['Ask the Parent/Device User which family members the child '
                     'knows and the Tamil words used.',
                     'Store these words in memory.',
                     'Say one known family word in Tamil and ask the child who it is.',
                     'Praise any attempt warmly; if no response, repeat the word.'],
         'sub_goal': 'The child names at least one family member in Tamil when asked.'},
        {'flow_name': 'Review Progress', 'persona': 'Therapist',
         'actions': ["Review the child's progress in both flows."],
         'sub_goal': 'The therapist sees how the child is doing.'}],
    'goal': OUTLINE}
UNREADABLE = LAST_ANSWER  # what the installed build read back on turn 11


class _Session:
    """One creation of 346, as the turns of one test share it: what the
    writer was sent before (each gather adds its message), the replies in a
    row that could not be read, the request the creation began with, and
    the review mark (what the person was shown last)."""

    def __init__(self, history=None, request=OUTLINE):
        from tests.unit.test_gather_turn_mirrors_user_side import PROMPT_ID, USER_ID
        key = f'{USER_ID}_{PROMPT_ID}'
        self.history = list(PERSON_TURNS[:-2]) if history is None else history
        self.failures = {}
        self.requests = {key: request} if request else {}
        self.reviews = {}


def _turn(tmp_path, reply, *, turn_before, session, text=LAST_ANSWER):
    """One gather turn of 346 through the real chat(): turn ``turn_before+1``,
    the writer answers ``reply``, ``session`` (a _Session) is the
    creation's.  Returns (payload, what the writer was sent in this turn, the
    saved config or None)."""
    from tests.unit.test_gather_turn_mirrors_user_side import PROMPT_ID, _drive
    payload, _persist, _memory, sent = _drive(
        tmp_path, reply, turn_before=turn_before, user_text=text,
        writer_history=session.history, reviews_shown=session.reviews,
        extra_ns={'_gather_failures': session.failures,
                  '_gather_requests': session.requests})
    path = tmp_path / f'{PROMPT_ID}.json'
    return payload, sent, (json.loads(path.read_text()) if path.exists() else None)


def _shows_config_346(payload):
    """The turn showed CONFIG_346 for the person's yes (#205): its steps,
    numbered, and how to answer."""
    text = payload['response']
    return (payload['Agent_status'] == 'Creation Mode'
            and '1. Check if a Parent or other helper is present' in text
            and "1. Review the child's progress in both flows." in text
            and 'Reply yes to build it' in text)


def test_the_first_unreadable_reply_late_in_a_creation_is_not_salvaged(tmp_path):
    """Turn 11 of 346: one unreadable reply.  The person reads it and the
    creation goes on (FAILS ON 8871f98a4: a stub was saved at once)."""
    s = _Session()
    payload, sent, saved = _turn(tmp_path, UNREADABLE, turn_before=10, session=s)
    assert saved is None, 'a stub was saved on the first unreadable reply'
    assert payload['Agent_status'] == 'Creation Mode', payload
    assert sent == [LAST_ANSWER], 'the writer was asked again'


def test_a_writer_that_fails_once_then_answers_saves_its_config(tmp_path):
    """The 346 replay: turn 11 unreadable, turn 12 (the wrap-up) answered.
    The model's config is saved, with the outline's personas, not a stub
    (FAILS ON 8871f98a4: the stub was saved on turn 11).  Turn 12's answer
    is saved unshown: the turn cap's own gap (#269), not the salvage's."""
    s = _Session()
    _p, _s, saved = _turn(tmp_path, UNREADABLE, turn_before=10, session=s)
    assert saved is None, 'turn 11 saved a stub'
    payload, sent, saved = _turn(tmp_path, json.dumps(CONFIG_346), turn_before=11,
                                 session=s)
    assert payload['Agent_status'] == 'Review Mode', payload
    assert sent[0].startswith('Please finalize'), sent
    assert [p['name'] for p in saved['personas']] == ['Child', 'Parent', 'Therapist']
    assert saved['flows'] == CONFIG_346['flows']


def test_a_second_unreadable_reply_in_a_row_asks_the_writer_once_and_shows_its_config(
        tmp_path):
    """Turns 10 and 11 unreadable: before any stub the writer is sent the
    wrap-up once (FAILS ON 8871f98a4: turn 10 saved a stub, and the writer
    was never asked).  Its config is SHOWN, as any config the person has
    not seen is (#205), and their plain yes builds it as shown, without
    asking the writer.  FAILS ON 7112a12f7: turn 11 saved it unseen and
    answered "Got Agent details successfully..." (review of 7112a12f7)."""
    s = _Session()
    _p, _s, saved = _turn(tmp_path, UNREADABLE, turn_before=9, session=s,
                          text=PERSON_TURNS[9])
    assert saved is None, 'turn 10 saved a stub'
    payload, sent, saved = _turn(
        tmp_path, [UNREADABLE, json.dumps(CONFIG_346)], turn_before=10, session=s)
    assert len(sent) == 2 and sent[1].startswith('Please finalize'), sent
    assert saved is None, 'the config was saved before the person saw it'
    assert _shows_config_346(payload), payload
    payload, sent, saved = _turn(tmp_path, 'not asked', turn_before=11,
                                 session=s, text='Yes.')
    assert sent == [], 'the yes went to the writer'
    assert payload['Agent_status'] == 'Review Mode', payload
    assert saved['flows'] == CONFIG_346['flows'], saved
    assert [p['name'] for p in saved['personas']] == ['Child', 'Parent', 'Therapist']


def test_a_change_to_the_salvaged_config_goes_to_the_writer_and_is_shown(tmp_path):
    """Turns 9 and 10 unreadable, the wrap-up's config shown on turn 10.  The
    person's change is their own and goes to the writer, and the config it
    writes, with a step they have not seen, is shown again before anything
    is built: the confirmation every config gets (#205).  FAILS ON
    7112a12f7: turn 10 saved the wrap-up's config."""
    s = _Session()
    _turn(tmp_path, UNREADABLE, turn_before=8, session=s, text=PERSON_TURNS[8])
    payload, _sent, saved = _turn(
        tmp_path, [UNREADABLE, json.dumps(CONFIG_346)], turn_before=9, session=s,
        text=PERSON_TURNS[9])
    assert saved is None and _shows_config_346(payload), payload
    change = 'Also say goodbye to the child in Tamil at the end.'
    changed = json.loads(json.dumps(CONFIG_346))
    changed['flows'][1]['actions'].append('Say goodbye to the child in Tamil.')
    payload, sent, saved = _turn(tmp_path, json.dumps(changed), turn_before=10,
                                 session=s, text=change)
    assert sent == [change], sent
    assert saved is None, 'a step the person never saw was built'
    assert payload['Agent_status'] == 'Creation Mode', payload
    assert '5. Say goodbye to the child in Tamil.' in payload['response'], payload


def test_a_writer_that_fails_twice_is_asked_once_then_the_stub_keeps_the_outline(
        tmp_path):
    """Turns 10 and 11 unreadable and the wrap-up too: one ask, then the stub,
    whose goal is the outline (FAILS ON 8871f98a4: the stub came on turn 10
    with no ask)."""
    s = _Session()
    _p, _s, saved = _turn(tmp_path, UNREADABLE, turn_before=9, session=s,
                          text=PERSON_TURNS[9])
    assert saved is None, 'turn 10 saved a stub'
    payload, sent, saved = _turn(tmp_path, UNREADABLE, turn_before=10, session=s)
    assert len(sent) == 2 and sent[1].startswith('Please finalize'), sent
    assert payload['Agent_status'] == 'Review Mode', payload
    assert saved['goal'] == OUTLINE, saved['goal']
    assert 'Personas: Child, Parent, Therapist' in saved['goal']
    assert saved['flows'][0]['sub_goal'] == OUTLINE
    assert saved['status'] == 'pending'


def test_a_question_in_answer_to_the_wrap_up_is_not_saved_as_the_config(tmp_path):
    """The writer answers the wrap-up with one more question: readable, but
    no config to build, so the stub is saved (FAILS ON 8871f98a4: turn 10
    saved a stub)."""
    s = _Session()
    _p, _s, saved = _turn(tmp_path, UNREADABLE, turn_before=9, session=s,
                          text=PERSON_TURNS[9])
    assert saved is None, 'turn 10 saved a stub'
    question = json.dumps({'status': 'pending', 'question': 'Anything else?'})
    _p, sent, saved = _turn(tmp_path, [UNREADABLE, question], turn_before=10,
                            session=s)
    assert len(sent) == 2, sent
    assert saved['status'] == 'pending' and saved['goal'] == OUTLINE, saved


def test_a_pending_config_in_answer_to_the_wrap_up_saves_the_stub(tmp_path):
    """The writer answers the wrap-up with its flows but "status": "pending":
    not a completed config.  It is not shown for a yes, which would build
    nothing (a confirmed pending config is read as a question again), and
    not saved unseen; the stub is saved.  FAILS ON 7112a12f7: saved as the
    writer wrote it, never shown."""
    s = _Session()
    _turn(tmp_path, UNREADABLE, turn_before=9, session=s, text=PERSON_TURNS[9])
    pending = dict(CONFIG_346, status='pending')
    payload, sent, saved = _turn(tmp_path, [UNREADABLE, json.dumps(pending)],
                                 turn_before=10, session=s)
    assert len(sent) == 2, sent
    assert payload['Agent_status'] == 'Review Mode', payload
    assert saved['goal'] == OUTLINE, saved
    assert saved['flows'][0]['actions'] == ['Respond to user'], saved


def test_two_unreadable_replies_early_in_a_creation_are_not_salvaged(tmp_path):
    """Before turn 10 a person's creation is never salvaged, however many
    replies in a row could not be read (as on 8871f98a4)."""
    s = _Session()
    for turn_before in (2, 3):
        payload, _s, saved = _turn(tmp_path, UNREADABLE, turn_before=turn_before,
                                   session=s)
        assert saved is None and payload['Agent_status'] == 'Creation Mode'


def test_the_late_salvage_begins_at_turn_ten_not_turn_nine(tmp_path):
    """The boundary: unreadable on turns 8 and 9 is two in a row, and turn 9
    is still not salvaged (the writer is sent only the person's words);
    unreadable again on turn 10 is.  Review of 7112a12f7: salvaging from
    turn 9 (MAX_GATHER_TURNS - 3) passed every test of the file."""
    s = _Session()
    for turn_before in (7, 8):
        text = PERSON_TURNS[turn_before]
        payload, sent, saved = _turn(tmp_path, UNREADABLE, turn_before=turn_before,
                                     session=s, text=text)
        assert sent == [text], f'turn {turn_before + 1} asked for the wrap-up'
        assert saved is None and payload['Agent_status'] == 'Creation Mode', payload
    payload, sent, saved = _turn(tmp_path, UNREADABLE, turn_before=9, session=s,
                                 text=PERSON_TURNS[9])
    assert len(sent) == 2 and sent[1].startswith('Please finalize'), sent
    assert payload['Agent_status'] == 'Review Mode', payload
    assert saved['status'] == 'pending' and saved['goal'] == OUTLINE, saved


def test_a_readable_reply_between_two_failures_starts_the_count_again(tmp_path):
    """Unreadable on turn 9, a question on turn 10, unreadable on turn 11:
    not two in a row, so no stub (FAILS ON 8871f98a4)."""
    s = _Session()
    _turn(tmp_path, UNREADABLE, turn_before=8, session=s, text=PERSON_TURNS[8])
    _turn(tmp_path, json.dumps({'status': 'pending', 'question': 'Sub-goal?'}),
          turn_before=9, session=s, text=PERSON_TURNS[9])
    payload, _sent, saved = _turn(tmp_path, UNREADABLE, turn_before=10, session=s)
    assert saved is None and payload['Agent_status'] == 'Creation Mode', payload


def test_a_fatal_error_is_salvaged_at_once_without_asking_again(tmp_path):
    """A context overflow is not retried: the wrap-up would overflow too
    (passes on 8871f98a4 as well)."""
    s = _Session()
    payload, sent, saved = _turn(tmp_path, 'Context size has been exceeded',
                                 turn_before=3, session=s)
    assert sent == [LAST_ANSWER], sent
    assert payload['Agent_status'] == 'Review Mode' and saved['goal'] == OUTLINE


def test_an_overflow_the_model_raises_is_judged_as_one_in_its_reply(writer, tmp_path):
    """The real writer, whose model refuses turn 3 as longer than its
    context.  The writer's conversation only grows, so it would be refused
    again: the creation is salvaged at once, as when the overflow comes in
    the reply's text, and the person never reads the server's words (#716).
    FAILS ON 7112a12f7: raised, the overflow became the snag sentence
    ("please try again in a moment"), which is never fatal, so the person
    was told to try again turn after turn (review of 7112a12f7)."""
    from core.agent_tools import is_user_facing_error
    for text in PERSON_TURNS[:2]:
        _send(writer, text)
    WriterModel.fails = RuntimeError(LLAMA_OVERFLOW)
    reply = _send(writer, PERSON_TURNS[2])
    assert is_user_facing_error(reply), reply
    assert 'context size' not in reply.lower() and '400' not in reply, reply
    payload, sent, saved = _turn(tmp_path, reply, turn_before=2, session=_Session(),
                                 text=PERSON_TURNS[2])
    assert sent == [PERSON_TURNS[2]], 'the writer was asked again'
    assert payload['Agent_status'] == 'Review Mode', payload
    assert saved['status'] == 'pending' and saved['goal'] == OUTLINE, saved


def test_the_last_turns_salvage_never_saves_the_wrap_up_instruction(tmp_path):
    """On the forced-completion turn the writer is sent the wrap-up
    instruction, not the person's words, and asked once more when its answer
    cannot be read.  FAILS BEFORE THE FIX: that instruction was saved as the
    agent's goal."""
    s = _Session()
    payload, sent, saved = _turn(tmp_path, 'not a config', turn_before=11, session=s)
    assert [m[:15] for m in sent] == ['Please finalize'] * 2, sent
    assert payload['Agent_status'] == 'Review Mode', payload
    assert saved['goal'] == OUTLINE, saved['goal']


def test_on_the_last_turn_an_unreadable_reply_is_asked_again_once_and_shown(tmp_path):
    """Turn 12's unreadable reply, asked again: the config it writes is
    shown and built on the yes, like the salvage's on turns 10 and 11.
    FAILS ON 7112a12f7: it was saved unseen."""
    s = _Session()
    payload, sent, saved = _turn(tmp_path, ['not a config', json.dumps(CONFIG_346)],
                                 turn_before=11, session=s)
    assert len(sent) == 2, sent
    assert saved is None and _shows_config_346(payload), payload
    payload, sent, saved = _turn(tmp_path, 'not asked', turn_before=12,
                                 session=s, text='yes')
    assert sent == [] and payload['Agent_status'] == 'Review Mode', payload
    assert saved['flows'] == CONFIG_346['flows'], saved


SECOND_REQUEST = ('A reading buddy for my son, who is learning to read '
                  'English: one short story a day, read aloud together.')


def test_a_second_creation_under_one_prompt_id_keeps_its_own_request(tmp_path):
    """A creation's salvage keeps the request THAT creation began with.  The
    writer of a prompt_id outlives its creation (gather_agentdetails
    .user_agents is never pruned), so the first message it holds is the
    first creation's.  A saved creation leaves no request behind.  FAILS ON
    7112a12f7: the second creation's stub took the first one's outline as
    its goal (review of 8871f98a4)."""
    from tests.unit.test_gather_turn_mirrors_user_side import PROMPT_ID
    s = _Session(history=[], request=None)  # one writer, never pruned
    question = json.dumps({'status': 'pending', 'question': 'What is its name?'})
    for request in (OUTLINE, SECOND_REQUEST):
        (tmp_path / f'{PROMPT_ID}.json').unlink(missing_ok=True)
        _turn(tmp_path, question, turn_before=0, session=s, text=request)
        _turn(tmp_path, UNREADABLE, turn_before=9, session=s)
        payload, _sent, saved = _turn(tmp_path, UNREADABLE, turn_before=10, session=s)
        assert payload['Agent_status'] == 'Review Mode', payload
        assert saved['goal'] == request, saved['goal']
        assert not s.requests, 'the saved creation left its request behind'


def test_a_stale_session_forgets_its_failures_and_its_request():
    """_cleanup_stale_agents drops a session's failure count and request with
    its turn count, so a person back after the TTL does not start one
    failure in, and nothing outlives its creation (FAILS ON 7112a12f7 for
    the request, which it did not keep)."""
    import ast
    import time
    from tests.unit.test_gather_turn_mirrors_user_side import _SRC
    tree = ast.parse(open(_SRC, encoding='utf-8').read())
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)
           and n.name == '_cleanup_stale_agents']
    ns = {'time': time, '_AGENT_TTL': 3600,
          '_agent_timestamps': {'u_2': time.time()},
          'review_agents': {}, 'conversation_agent': {}, 'resume_build_input': {},
          '_gather_turn_counts': {'u_1': 10, 'u_2': 3},
          '_gather_reviews_shown': {}, '_gather_failures': {'u_1': 1, 'u_2': 1},
          '_gather_requests': {'u_1': OUTLINE, 'u_2': SECOND_REQUEST}}
    exec(compile(ast.fix_missing_locations(ast.Module(body=fns, type_ignores=[])),
                 _SRC, 'exec'), ns)
    ns['_cleanup_stale_agents']()
    assert ns['_gather_failures'] == {'u_2': 1}
    assert ns['_gather_requests'] == {'u_2': SECOND_REQUEST}


class _NoWriter(list):
    """A writer that holds no message (none was kept in this process)."""

    def append(self, item):
        pass


def test_with_no_request_held_the_salvage_keeps_the_persons_words(tmp_path):
    """Nothing better is held: the person's own words of this turn, never the
    wrap-up instruction (FAILS BEFORE THE FIX)."""
    _p, _sent, saved = _turn(tmp_path, 'not a config', turn_before=11,
                             session=_Session(history=_NoWriter(), request=None))
    assert saved['goal'] == LAST_ANSWER, saved['goal']
