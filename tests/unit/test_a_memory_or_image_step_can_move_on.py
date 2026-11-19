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
    found = store.search('textile compliance requirements for export buyers')
    contents = [r.item.content for r in found]
    # 'Export rules for rice.' holds one of the words: not enough to be found
    assert sorted(contents) == [
        'Textile compliance checklist, Tirupur mills.',
        'Textile compliance: EU buyers ask for OEKO-TEX and REACH papers.'], contents


def test_a_query_that_matches_keeps_its_results(store):
    found = store.search('rice moisture')
    assert [r.item.content for r in found] == [
        'Rice moisture must stay under 14 percent.']


def test_a_query_none_of_whose_words_is_saved_finds_nothing(store):
    assert store.search('seafood freight insurance') == []


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
