"""A memory search or an image step that finds nothing says so, and says how
to move on.

WHAT WENT WRONG (central, build a59caf223, 19 Nov)
  Four daemon goals were paused needs_help on routine steps:
  'search_long_term_memory: search saved facts for textile compliance ...'
  (the step made no progress), 'search_long_term_memory: recall saved facts on
  the HertzAI commission agent trade model ...' (did not complete after 3
  attempts) and two text_2_image steps (3 attempts; looped).

  Measured read-only on central (a /tmp copy of each goal's memory graph):
  SimpleMem is not installed there, so search_long_term_memory reads the
  MemoryGraph, whose rows carry no embeddings, so its "hybrid" search is the
  FTS leg alone.  That leg joined every word of the query with AND.  The
  textile goal's graph has 54 rows, 21 with 'textile' and 20 with
  'compliance', yet 'textile compliance requirements for export buyers'
  matched 0 rows, and so did 'HertzAI commission agent trade model rice
  buyers' on the rice goal (57 rows): one word missing from a row hid it.
  The tool then answered a bare 'No relevant memories found.', and a raised
  error answered 'Memory search unavailable.', neither of which says what to
  do next.

  text_2_image returned hartos.helper.txt2img's answer as is, and that is ''
  for a failed request, an open circuit breaker and a reply with no image
  alike: the model was handed an empty string and called again.

THE RULE
  - a query whose words are not all in one row still finds the rows that hold
    two or more of its words (of four letters or more); a query that already
    matches keeps its results;
  - an empty recall names the query and says to carry on without saved facts;
    a failed read is a core.tool_refusal refusal;
  - text_2_image with no image is a core.tool_refusal refusal that says to
    carry on without the image.

    python -m pytest tests/unit/test_a_memory_or_image_step_can_move_on.py -q
"""
from types import SimpleNamespace
from unittest import mock

import pytest

from core.constants import tool_reply_failed
from core.tool_refusal import refusal_has_hint


def _tools(memory_graph=None, helper_fun=None):
    from core.agent_tools import build_core_tool_closures
    ctx = {k: None for k in (
        'user_id', 'prompt_id', 'agent_data', 'helper_fun', 'user_prompt',
        'request_id_list', 'recent_file_id', 'scheduler',
        'send_message_to_user1', 'retrieve_json', 'strip_json_values',
        'save_conversation_db')}
    ctx.update(user_id=1, prompt_id='p1', agent_data={}, user_prompt='s1',
               request_id_list={'s1': 'r1'}, memory_graph=memory_graph,
               simplemem_store=None, helper_fun=helper_fun or mock.Mock(),
               send_message_to_user1=mock.Mock(), retrieve_json=lambda v: v)
    return {n: f for n, _, f in build_core_tool_closures(ctx)}


@pytest.fixture()
def store(tmp_path):
    from integrations.channels.memory.memory_store import MemoryStore
    s = MemoryStore(db_path=tmp_path / 'm.db')
    if not getattr(s, '_fts_available', False):
        pytest.skip('this sqlite has no FTS5')
    s.add('Textile compliance: EU buyers ask for OEKO-TEX and REACH papers.')
    s.add('Textile compliance checklist, Tirupur mills.')
    s.add('Rice moisture must stay under 14 percent.')
    s.add('Export rules for rice.')
    yield s
    s.close()


def test_a_query_with_a_word_no_row_holds_still_finds_its_rows(store):
    found = store.search('textile compliance requirements for export buyers',
                         partial_match=True)
    contents = [r.item.content for r in found]
    # 'Export rules for rice.' holds one of the words: not enough to be found
    assert sorted(contents) == [
        'Textile compliance checklist, Tirupur mills.',
        'Textile compliance: EU buyers ask for OEKO-TEX and REACH papers.'], contents


def test_a_query_that_matches_keeps_its_results(store):
    found = store.search('rice moisture', partial_match=True)
    assert [r.item.content for r in found] == [
        'Rice moisture must stay under 14 percent.']


def test_a_query_none_of_whose_words_is_saved_finds_nothing(store):
    assert store.search('seafood freight insurance', partial_match=True) == []


# ── review of e0b313bdd ────────────────────────────────────────────────

def test_a_search_is_exact_unless_it_asks_for_some_words(store):
    """Review F1: the some-words match served every MemoryStore.search,
    get_data_by_key's saved-value lookup among them."""
    assert store.search('textile compliance requirements for export buyers') == []


def test_a_key_never_saved_is_not_answered_by_a_row_sharing_its_words(tmp_path):
    """Review F1 (CRITICAL): 'customer.email' answered with 'The customer
    asked whether email delivery is slow on weekends.' and 'budget' with 'Our
    budget talk went nowhere today.'; the parent answered Key not found."""
    from core.agent_tools import KEY_NOT_FOUND
    from integrations.channels.memory.memory_graph import MemoryGraph
    g = MemoryGraph(db_path=str(tmp_path / 'g'), user_id='1')
    try:
        g.register('[KV] customer.name = "Ravi"', {'memory_type': 'fact'})
        g.register('The customer asked whether email delivery is slow on '
                   'weekends.', {'memory_type': 'conversation'})
        g.register('Our budget talk went nowhere today.',
                   {'memory_type': 'conversation'})
        read = _tools(memory_graph=g)['get_data_by_key']
        for key in ('customer.email', 'budget'):
            out = read(key)
            assert out.startswith(KEY_NOT_FOUND.strip().rstrip('.')), (key, out)
            assert 'weekends' not in out and 'nowhere' not in out, (key, out)
    finally:
        g.close()


def test_common_words_are_not_what_two_words_means(tmp_path):
    """Review F3: 'what about seafood prices' found two rows that share only
    'what' and 'about' with it."""
    from integrations.channels.memory.memory_store import MemoryStore
    s = MemoryStore(db_path=tmp_path / 's.db')
    try:
        s.add('What do you think about that, said the customer.')
        s.add('Tell me about what happened with the shipment.')
        assert s.search('what about seafood prices', partial_match=True) == []
    finally:
        s.close()


def test_rows_holding_two_words_are_not_crowded_out_by_one_word_rows(tmp_path):
    """Review: the limit was applied before the two-word filter, so 25
    short rows holding one word pushed out the 3 that hold two."""
    from integrations.channels.memory.memory_store import MemoryStore
    s = MemoryStore(db_path=tmp_path / 'l.db')
    try:
        for i in range(25):
            s.add(f'invoice {i}')
        for i in range(3):
            s.add(f'The freight forwarder sent the invoice for container {i} '
                  + 'and more words ' * 20)
        got = s.search('freight invoice overdue', max_results=10,
                       partial_match=True)
        assert len(got) == 3 and all('freight' in r.item.content for r in got),             [r.item.content[:30] for r in got]
    finally:
        s.close()


def test_recall_memory_answers_like_search_long_term_memory(tmp_path):
    """Review F4: the MemoryGraph tool set's recall_memory kept 'No matching
    memories found.' and 'Memory recall failed: {e}'."""
    from integrations.channels.memory.agent_memory_tools import create_memory_tools
    from integrations.channels.memory.memory_graph import MemoryGraph
    g = MemoryGraph(db_path=str(tmp_path / 'g'), user_id='1')
    try:
        g.register('Textile compliance: EU buyers ask for OEKO-TEX papers.',
                   {'memory_type': 'fact'})
        recall = create_memory_tools(g, '1', 's1')['recall_memory'][0]
        assert 'OEKO-TEX' in recall('textile compliance rules for buyers')
        empty = recall('seafood freight insurance')
        assert empty.startswith('No relevant memories found'), empty
        assert 'carry on' in empty.lower() and not tool_reply_failed(empty)
        broken = mock.Mock()
        broken.recall.side_effect = RuntimeError('database is locked')
        out = create_memory_tools(broken, '1', 's1')['recall_memory'][0]('x')
        assert tool_reply_failed(out) and refusal_has_hint(out), out
    finally:
        g.close()


def test_an_empty_recall_names_the_query_and_says_carry_on():
    graph = mock.Mock()
    graph.recall.return_value = []
    out = _tools(memory_graph=graph)['search_long_term_memory'](
        'textile compliance requirements')
    assert out.startswith('No relevant memories found'), out
    assert 'textile compliance requirements' in out
    assert 'carry on' in out.lower(), out
    # finding nothing is an answer, not a failed call: the step may end on it
    assert not tool_reply_failed(out)


def test_a_failed_memory_read_is_a_refusal_that_says_what_to_do():
    graph = mock.Mock()
    graph.recall.side_effect = RuntimeError('database is locked')
    out = _tools(memory_graph=graph)['search_long_term_memory']('textile')
    assert tool_reply_failed(out), out
    assert refusal_has_hint(out), out
    assert 'RuntimeError' in out


def test_recalled_rows_are_still_returned():
    graph = mock.Mock()
    graph.recall.return_value = [SimpleNamespace(content='OEKO-TEX needed')]
    out = _tools(memory_graph=graph)['search_long_term_memory']('textile')
    assert out == 'OEKO-TEX needed'
    # free-text recall is the one search that asks for some-words matching
    assert graph.recall.call_args.kwargs.get('partial_match') is True


@pytest.mark.parametrize('nothing', ['', None])
def test_no_image_is_a_refusal_that_says_carry_on_without_it(nothing):
    helper = mock.Mock()
    helper.txt2img.return_value = nothing
    out = _tools(helper_fun=helper)['text_2_image']('a warm kitchen scene')
    assert tool_reply_failed(out), repr(out)
    assert refusal_has_hint(out), out
    assert 'without the image' in out, out
    helper.txt2img.assert_called_once_with('a warm kitchen scene')


def test_a_made_image_is_returned_as_is():
    helper = mock.Mock()
    helper.txt2img.return_value = 'https://img.example/k.png'
    out = _tools(helper_fun=helper)['text_2_image']('a lightbulb')
    assert out == 'https://img.example/k.png'
