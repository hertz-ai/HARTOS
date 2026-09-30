"""The #725 sync must not hand over a transcript whose tool calls are unanswered.

MEASURED LIVE 2026-09-07 (installed build PID 3976, agent 18088688973), via the
[725-SYNC-COMPOSITION] diagnostic.  `manager._oai_messages` is keyed PER AGENT,
and every one of 16 samples in a single drive looked like this (* = the buffer
`max(..., key=len)` picked):

    User*:n=30,calls=9,answers=0  | Helper:n=30,calls=9,answers=0
    multi_role_agent:n=30,calls=9,answers=0 | Executor:n=30,calls=9,answers=0
    ChatInstructor:n=30,calls=9,answers=0   | StatusVerifier:n=30,calls=9,answers=0
    Assistant:n=14,calls=10,answers=4   <-- the ONLY buffer holding answers

THE PICKED BUFFER HAD answers=0 IN 16 OF 16 SAMPLES.  Six buffers are
identical-length broadcast copies — the group manager relays each message to
every member, so they are one conversation seen from six seats.  The executing
agent's own buffer carries the role=tool results and is SHORTER, so selecting
the longest can never reach it.  "Longest" is not a weak proxy for "most
complete"; here it is ANTI-CORRELATED with it.

Downstream, that is the whole of #786.  helper.py:1897 treats a tool_call_id
with no matching answer as "historical pending" and mints
HISTORICAL_TOOL_PLACEHOLDER (:1940) to satisfy an API that rejects an
unanswered tool_call — 83 placeholders over 30 repair events in that one drive.
On the wire the median tool result was 45 chars (exactly the placeholder
string), 116/119 under 120 chars, 1/119 carrying a URL.  So google_search
really fetched five engines with HTTP 200s and the model still produced a brief
with 0 URLs, 0 "Source(s):", 0 arxiv refs — it cannot cite what it never
received.

WHY MERGE RATHER THAN RE-PICK: the long buffer is the right SKELETON (it has
every agent's turns); the short one merely holds answers the skeleton is
missing.  Picking the answer-bearing buffer instead would drop the other
agents' messages.  So keep the base and splice the real answers into the slots
helper.py would otherwise fill with a placeholder — the same "fill the answer
slot" operation already in the codebase, with the real result.

    python -m pytest tests/unit/test_reuse_sync_merges_tool_answers.py --noconftest -q
"""
import pytest


def _call(base, buffers):
    rr = pytest.importorskip('hartos.reuse_recipe')
    return rr._merge_tool_answers(base, buffers)


def _assistant(call_id, name='Assistant'):
    return {'role': 'assistant', 'name': name,
            'tool_calls': [{'id': call_id, 'type': 'function',
                            'function': {'name': 'google_search', 'arguments': '{}'}}]}


def _answer(call_id, content):
    return {'role': 'tool', 'tool_call_id': call_id, 'name': 'google_search',
            'content': content}


class TestMergeRecoversRealAnswers:

    def test_THE_REGRESSION_answer_from_a_shorter_sibling_is_recovered(self):
        """The measured case: base has the call, only a SHORTER buffer has the answer."""
        base = [{'role': 'user', 'content': 'go'}, _assistant('call_1'),
                {'role': 'user', 'content': 'next'}]
        sibling = [_assistant('call_1'), _answer('call_1', 'RESULT: startpage.com/x')]
        out = _call(base, [base, sibling])
        tools = [m for m in out if m.get('role') == 'tool']
        assert len(tools) == 1, f'the real answer must be spliced in, got {out}'
        assert tools[0]['tool_call_id'] == 'call_1'
        assert 'startpage.com/x' in tools[0]['content'], (
            'the REAL content must survive — a placeholder here is the defect')

    def test_answer_lands_immediately_after_its_assistant_message(self):
        """Position matters: the API pairs a tool answer to the call before it."""
        base = [{'role': 'user', 'content': 'go'}, _assistant('call_1'),
                {'role': 'user', 'content': 'later'}]
        sibling = [_answer('call_1', 'R1')]
        out = _call(base, [base, sibling])
        idx_a = next(i for i, m in enumerate(out) if m.get('tool_calls'))
        assert out[idx_a + 1].get('role') == 'tool', f'must follow its call: {out}'

    def test_already_answered_calls_are_untouched(self):
        """Must not duplicate an answer the base already carries."""
        base = [_assistant('call_1'), _answer('call_1', 'ORIGINAL')]
        sibling = [_assistant('call_1'), _answer('call_1', 'DUPLICATE')]
        out = _call(base, [base, sibling])
        tools = [m for m in out if m.get('role') == 'tool']
        assert len(tools) == 1, f'no duplicate answer slot: {out}'
        assert tools[0]['content'] == 'ORIGINAL'

    def test_unanswerable_call_is_left_for_helper_to_placeholder(self):
        """No sibling has it — leave the base alone; helper.py:1940 fills the slot.

        This function must NOT invent content.  A missing answer that stays
        missing is correct; manufacturing one here would be the fabrication the
        verification contract forbids.
        """
        base = [_assistant('call_missing')]
        out = _call(base, [base])
        assert [m for m in out if m.get('role') == 'tool'] == []

    def test_several_unanswered_calls_across_several_buffers(self):
        base = [_assistant('a'), _assistant('b'), _assistant('c')]
        buf1 = [_answer('a', 'A-RESULT')]
        buf2 = [_answer('c', 'C-RESULT')]
        out = _call(base, [base, buf1, buf2])
        got = {m['tool_call_id']: m['content'] for m in out if m.get('role') == 'tool'}
        assert got == {'a': 'A-RESULT', 'c': 'C-RESULT'}, (
            f'recover every answer that exists, and only those: {got}')

    def test_base_messages_are_never_dropped(self):
        base = [{'role': 'user', 'content': 'u1'}, _assistant('call_1'),
                {'role': 'assistant', 'name': 'StatusVerifier', 'content': 'v'}]
        sibling = [_answer('call_1', 'R')]
        out = _call(base, [base, sibling])
        for m in base:
            assert m in out, f'merge must be additive, lost {m}'

    def test_tool_calls_on_a_role_user_message_still_count(self):
        """THE LIVE SHAPE — and the bug my first implementation shipped.

        `manager._oai_messages[agent]` stores the conversation FROM THAT
        AGENT'S SEAT: the agent's own turns are role='assistant', everyone
        else's are role='user'.  The buffer the sync picks is keyed "User", so
        the Helper/Assistant tool calls inside it arrive as role='user'.

        My first version collected announced ids only from role=='assistant',
        so on the real data `announced` was empty, the function returned at its
        early exit, and it spliced NOTHING.  Measured live 2026-09-07 after
        deploying 50e4df7ae: 15 sync events, "spliced 0 real tool answer(s)"
        every time, while the composition line showed the picked buffer at
        calls=35/answers=0 beside a sibling at calls=19/answers=10.

        The unit tests passed because the `_assistant()` helper in this file
        hard-codes role='assistant' — they tested a shape the live system does
        not produce.  A tool_calls key is what makes a message a call; the role
        it happens to be stored under is a function of whose buffer you are in.
        """
        base = [{'role': 'user', 'name': 'Helper',
                 'tool_calls': [{'id': 'call_u', 'type': 'function',
                                 'function': {'name': 'google_search',
                                              'arguments': '{}'}}]}]
        sibling = [_answer('call_u', 'RESULT FROM THE OTHER SEAT')]
        out = _call(base, [base, sibling])
        tools = [m for m in out if m.get('role') == 'tool']
        assert len(tools) == 1, (
            'a tool_calls message stored as role=user is still a call — '
            f'its answer must be recovered, got {out}')
        assert 'OTHER SEAT' in tools[0]['content']

    def test_consolidated_reply_ids_live_in_tool_responses(self):
        """THE REAL AUTOGEN SHAPE — and why two live fixes still spliced 0.

        Read from autogen's own generate_tool_calls_reply
        (agentchat/conversable_agent.py): when tools run it returns

            {"role": "tool",
             "tool_responses": [ {"tool_call_id": ..., "role": "tool",
                                  "content": ...}, ... ],
             "content": "\\n\\n".join(...)}

        There is NO top-level tool_call_id on that message — the ids are
        NESTED inside tool_responses.  Both earlier versions of the merge did
        `tid = m.get('tool_call_id')`, which is None for every such reply, so
        `tid in missing` was never true and nothing was ever spliced.  That is
        exactly the measured signature: 38 sync events, answers visible in a
        sibling buffer, 0 spliced, on a byte-verified deploy.

        helper.py already models this shape — is_consolidated_response()
        (helper.py:1276) keys on 'tool_responses' — so the reader must too.
        """
        base = [_assistant('a'), _assistant('b')]
        consolidated = [{
            'role': 'tool',
            'content': 'A-RESULT\n\nB-RESULT',
            'tool_responses': [
                {'tool_call_id': 'a', 'role': 'tool', 'content': 'A-RESULT'},
                {'tool_call_id': 'b', 'role': 'tool', 'content': 'B-RESULT'},
            ],
        }]
        out = _call(base, [base, consolidated])
        tools = [m for m in out if m.get('role') == 'tool']
        assert tools, f'a consolidated reply answers real calls — splice it: {out}'
        blob = ' '.join(str(m.get('content', '')) for m in tools)
        assert 'A-RESULT' in blob and 'B-RESULT' in blob, (
            f'both nested answers must survive, got {tools}')

    def test_consolidated_reply_answering_nothing_is_not_spliced(self):
        """Must not drag in a reply whose ids the base never announced."""
        base = [_assistant('a')]
        other = [{'role': 'tool', 'content': 'X',
                  'tool_responses': [{'tool_call_id': 'zzz', 'role': 'tool',
                                      'content': 'X'}]}]
        out = _call(base, [base, other])
        assert [m for m in out if m.get('role') == 'tool'] == []

    def test_malformed_input_never_raises(self):
        """Runs on every sync of a live turn — it must not be able to kill one."""
        assert _call([], []) == []
        _call([{'role': 'assistant', 'tool_calls': 'not-a-list'}], [])
        _call([None, 42, {'role': 'tool'}], [[None], [{}]])
        _call([_assistant('x')], [[{'role': 'tool'}]])   # answer with no id
