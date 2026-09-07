"""An unanswered tool slot must be filled with the REAL result when one exists.

THE DEFECT THIS GUARDS (measured live 2026-09-07, agent 18088688973).
google_search really ran and really fetched five engines with HTTP 200s, and
the brief it produced cited nothing: 0 URLs, 0 "Source(s):", 0 refs.  On the
wire the median tool result was 45 characters -- exactly
HISTORICAL_TOOL_PLACEHOLDER -- with 116/119 results under 120 chars and 1/119
carrying a URL.  The model cannot cite what it never received.

WHY THE ANSWER WAS MISSING.  Tools execute in a pairwise Assistant<->Executor
exchange, not in the group broadcast (reuse_recipe.py:3363 records this).  So
the seat whose request is being built legitimately never saw a role=tool
answer, helper.py called those calls "historical pending", and minted a
placeholder into the slot -- 88 of them in a single drive.

WHY THIS IS THE RIGHT PLACE.  helper.ToolMessageHandler.apply_transform is an
autogen TransformMessages capability: it runs on the exact message list that
becomes the LLM request body.  group_chat.messages is NOT that list -- it
feeds speaker-selection and progress checks only -- so splicing answers there
cannot displace a placeholder.  This is the one site that decides what fills
the slot, and the change is only WHAT it fills with.

    python -m pytest tests/unit/test_placeholder_prefers_real_answer.py --noconftest -q
"""
import pytest


def _mod():
    return pytest.importorskip('hartos.helper')


class _FakeAgent:
    """Minimal stand-in for a ConversableAgent: just its _oai_messages."""

    def __init__(self, conv):
        self._oai_messages = {'manager': conv}


def _consolidated(pairs):
    """The real autogen shape: ids nested in tool_responses, none on the outer."""
    return {
        'role': 'tool',
        'content': '\n\n'.join(c for _, c in pairs),
        'tool_responses': [
            {'tool_call_id': i, 'role': 'tool', 'content': c} for i, c in pairs
        ],
    }


class TestRealToolAnswerLookup:

    def test_finds_answer_nested_in_tool_responses(self):
        h = _mod()
        peer = _FakeAgent([_consolidated([('a', 'RESULT-A https://arxiv.org/x'),
                                          ('b', 'RESULT-B')])])
        handler = h.ToolMessageHandler(peer_agents=[peer])
        assert 'arxiv.org/x' in handler.real_tool_answer('a')
        assert handler.real_tool_answer('b') == 'RESULT-B'

    def test_picks_this_calls_own_entry_not_the_joined_blob(self):
        """A batch answers several calls; each slot gets ITS result, not all of them."""
        h = _mod()
        peer = _FakeAgent([_consolidated([('a', 'AAA'), ('b', 'BBB')])])
        handler = h.ToolMessageHandler(peer_agents=[peer])
        assert handler.real_tool_answer('a') == 'AAA'
        assert 'BBB' not in handler.real_tool_answer('a')

    def test_plain_per_call_shape_still_works(self):
        h = _mod()
        peer = _FakeAgent([{'role': 'tool', 'tool_call_id': 'z', 'content': 'ZED'}])
        handler = h.ToolMessageHandler(peer_agents=[peer])
        assert handler.real_tool_answer('z') == 'ZED'

    def test_unknown_id_returns_None_so_the_placeholder_stands(self):
        """Must NOT invent content.  A truly unanswered call stays unanswered,
        so the fabrication gate downstream still sees the truth."""
        h = _mod()
        peer = _FakeAgent([_consolidated([('a', 'AAA')])])
        handler = h.ToolMessageHandler(peer_agents=[peer])
        assert handler.real_tool_answer('nope') is None

    def test_never_returns_the_placeholder_as_if_it_were_real(self):
        """A peer buffer can already contain a minted placeholder; recycling it
        would launder a manufactured string into a 'real answer'."""
        h = _mod()
        peer = _FakeAgent([{'role': 'tool', 'tool_call_id': 'p',
                            'content': h.HISTORICAL_TOOL_PLACEHOLDER}])
        handler = h.ToolMessageHandler(peer_agents=[peer])
        assert handler.real_tool_answer('p') is None

    def test_no_peers_configured_is_the_old_behaviour(self):
        h = _mod()
        assert h.ToolMessageHandler().real_tool_answer('a') is None

    def test_never_raises_on_junk_peers(self):
        """Runs inside the transform on EVERY llm call — it must not kill a turn."""
        h = _mod()

        class Exploding:
            @property
            def _oai_messages(self):
                raise RuntimeError('boom')

        for peers in ([None], [object()], [Exploding()],
                      [_FakeAgent(None)], [_FakeAgent([None, 42, {}])]):
            assert h.ToolMessageHandler(peer_agents=peers).real_tool_answer('a') is None


class TestAnsweredCallIdsIsTheOneReader:

    def test_reads_both_shapes(self):
        h = _mod()
        assert h.answered_call_ids(
            {'role': 'tool', 'tool_call_id': 'x', 'content': 'c'}) == {'x'}
        assert h.answered_call_ids(_consolidated([('a', '1'), ('b', '2')])) == {'a', 'b'}

    def test_single_entry_consolidated_is_still_read(self):
        """is_consolidated_response requires len > 1; this reader must not,
        because a one-call reply hides its id in the same place."""
        h = _mod()
        assert h.answered_call_ids(_consolidated([('solo', 'S')])) == {'solo'}

    def test_non_tool_messages_answer_nothing(self):
        h = _mod()
        assert h.answered_call_ids({'role': 'assistant', 'tool_call_id': 'x'}) == set()
        assert h.answered_call_ids(None) == set()
        assert h.answered_call_ids({'role': 'tool'}) == set()

    def test_reuse_imports_this_one_and_keeps_no_copy(self):
        """DRY guard: one definition, so a shape change is learned once."""
        rr = pytest.importorskip('hartos.reuse_recipe')
        h = _mod()
        assert rr.answered_call_ids is h.answered_call_ids
        assert not hasattr(rr, '_answered_call_ids'), (
            'the private copy is back — reuse must import the canonical reader')
