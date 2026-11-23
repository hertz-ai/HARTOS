"""The memory graph stores what was said, never the group chat's own control
traffic.

WHAT WENT WRONG (desktop build 75, langchain.log)
  The Research Assistant (93676680129) searched its long-term memory for
  'saved research notes'.  On 13 Nov 19:15 the search returned the loop's own
  step line, 'Execute Action 2: search_long_term_memory for saved research
  notes ,Latest User message: ...'; on 14 Nov 21:03 it returned a
  StatusVerifier verdict JSON.  The verifier then judged the tool corrupted and
  the step failed on every turn.  On this desktop search_long_term_memory reads
  the MemoryGraph (SimpleMem is not installed), and graph_conversation_sink
  stored every group message over five characters, so each step line and each
  verdict became a memory, and a step that searches for its own title finds it.

THE RULE (graph sink only; the seed history and SimpleMem are not touched)
  - a StatusVerifier message is never stored: a verdict is control, the ledger
    keeps it;
  - a step line the loop sends (create's 'Execute Action N: <step> ,Latest
    User message: <words>', reuse's 'Perform this action -> Action #N:<step>
    ...') is stored as the person's words only, spoken by 'User', and not at
    all when it carries none;
  - every other message is stored as before.

ONE READER OF "IS THIS A STEP LINE" (audit row H11)
  The sink used to test the two openings itself (startswith 'Execute Action '
  / 'Perform this action -> Action #') beside hartos.lifecycle_hooks.
  dispatch_action_id, the parser every other reader uses.  The copy missed
  what create also sends: 'Properly Execute Action N:' (the retry after a
  failed round), '[retry:<tag>] ...' posts, the next step's 'Execute Action
  N: <step> ' with no person's words, and a marker behind leading whitespace.
  Each was stored whole as a memory.  The sink now asks dispatch_action_id.
  Carried over from the copy: both word layouts (the tests above the H11
  block, unchanged).  Not carried: the copy also took an un-numbered 'Execute
  Action <text> ,Latest User message:' line for a step line; no producer
  writes one, and dispatch_action_id says it is not a dispatch, so it is
  stored as any other text (test_text_that_only_opens_like_a_step_line...).

    python -m pytest tests/unit/test_the_memory_graph_keeps_no_control_text.py -q --noconftest
"""
import json

from integrations.channels.memory.shared_history import graph_conversation_sink

SESSION = 'u1_93676680129'


class _Graph:
    def __init__(self):
        self.rows = []

    def register_conversation(self, speaker, content, session_id):
        self.rows.append((speaker, content, session_id))
        return f'm{len(self.rows)}'


def _store(*messages):
    graph = _Graph()
    sink = graph_conversation_sink(graph, SESSION)
    for m in messages:
        sink(m)
    return graph.rows


_VERDICT = json.dumps({
    'status': 'error', 'action': 'search_long_term_memory for saved research notes',
    'action_id': 2, 'message': 'Action failed.',
    'evidence': {'message_index': 28, 'kind': 'tool_receipt'}}, indent=2)


def test_a_verdict_is_never_stored():
    rows = _store({'role': 'user', 'name': 'StatusVerifier', 'content': _VERDICT})
    assert rows == [], rows


def test_a_create_step_line_is_stored_as_the_persons_words_only():
    from hartos.create_recipe import step_instruction
    line = step_instruction(2, 'search_long_term_memory for saved research notes',
                            'Find three facts about speech apps for children.')
    rows = _store({'role': 'user', 'name': 'ChatInstructor', 'content': line})
    assert rows == [('User', 'Find three facts about speech apps for children.',
                     SESSION)], rows


def test_a_reuse_dispatch_is_stored_as_the_persons_words_only():
    import hartos.reuse_recipe as rr
    from core.constants import ACTION_STEPS_SEPARATOR
    line = (f"{rr._REUSE_ACTION_MESSAGE_PREFIX}1:greet the person by their name"
            f"\n\nHi, I want to practise ordering at a cafe."
            f"{ACTION_STEPS_SEPARATOR}[{{'get_data_by_key(key)': {{}}}}]")
    rows = _store({'role': 'user', 'name': 'ChatInstructor', 'content': line})
    assert rows == [('User', 'Hi, I want to practise ordering at a cafe.',
                     SESSION)], rows


def test_a_step_line_with_no_words_stores_nothing():
    import hartos.reuse_recipe as rr
    from core.constants import ACTION_STEPS_SEPARATOR
    from hartos.create_recipe import step_instruction
    rows = _store(
        {'role': 'user', 'name': 'ChatInstructor',
         'content': f"{rr._REUSE_ACTION_MESSAGE_PREFIX}2:save the tip"
                    f"{ACTION_STEPS_SEPARATOR}[]"},
        {'role': 'user', 'name': 'ChatInstructor',
         'content': step_instruction(3, 'google_search for the topic', '')})
    assert rows == [], rows


def test_what_the_agents_and_tools_say_is_stored_as_before():
    rows = _store(
        {'role': 'assistant', 'name': 'Assistant',
         'content': 'Speech apps for children often use games to keep practice short.'},
        {'role': 'tool', 'name': 'Helper', 'content': 'Three results about speech apps.'})
    assert rows == [
        ('Assistant', 'Speech apps for children often use games to keep practice short.',
         SESSION),
        ('Helper', 'Three results about speech apps.', SESSION)], rows


def test_other_instructor_text_is_stored_as_before():
    """A message the loop sends in neither step format (a person's raw words on
    reuse's fallback path) keeps today's behaviour: nothing is dropped on a
    guess."""
    rows = _store({'role': 'user', 'name': 'ChatInstructor',
                   'content': 'My son Arjun is five and loves trains.'})
    assert rows == [('ChatInstructor', 'My son Arjun is five and loves trains.',
                     SESSION)], rows


# ── H11: the forms the sink's own copy of the marker missed ──

def _instructor(content):
    return {'role': 'user', 'name': 'ChatInstructor', 'content': content}


def test_creates_retry_line_is_control_text():
    """create_recipe's Failure=True retry: 'Properly Execute Action N: <step> '
    carries no person's words."""
    rows = _store(_instructor(
        'Properly Execute Action 2: search_long_term_memory for saved '
        'research notes '))
    assert rows == [], rows


def test_creates_next_step_line_is_control_text():
    """get_execute_next_action_message: 'Execute Action N: <step> ', no words."""
    rows = _store(_instructor(
        'Execute Action 3: search_long_term_memory for saved research notes '))
    assert rows == [], rows


def test_a_tagged_retry_post_is_a_step_line():
    from hartos.create_recipe import _retry_marker, step_instruction
    tag = _retry_marker('tool_call')
    rows = _store(
        _instructor(f"{tag} {step_instruction(2, 'search for the notes', 'Find three facts about trains.')}"),
        _instructor(f'{tag} Properly Execute Action 2: search for the notes '))
    assert rows == [('User', 'Find three facts about trains.', SESSION)], rows


def test_a_step_line_behind_leading_whitespace_is_still_a_step_line():
    import hartos.reuse_recipe as rr
    from core.constants import ACTION_STEPS_SEPARATOR
    from hartos.create_recipe import step_instruction
    rows = _store(
        _instructor('\n ' + step_instruction(2, 'search for the notes',
                                             'Find three facts about trains.')),
        _instructor(f"\n{rr._REUSE_ACTION_MESSAGE_PREFIX}1:greet the person"
                    f"\n\nHi, I want to practise ordering."
                    f"{ACTION_STEPS_SEPARATOR}[]"),
        _instructor(f"  {rr._REUSE_ACTION_MESSAGE_PREFIX}2:save the tip"
                    f"{ACTION_STEPS_SEPARATOR}[]"))
    assert rows == [('User', 'Find three facts about trains.', SESSION),
                    ('User', 'Hi, I want to practise ordering.', SESSION)], rows


def test_a_retry_that_quotes_a_reuse_marker_reads_creates_layout():
    """The layout follows the line's OWN leading marker: a create line whose
    person's words quote a reuse dispatch is still read by create's mark."""
    import hartos.reuse_recipe as rr
    from hartos.create_recipe import step_instruction
    words = f"what does {rr._REUSE_ACTION_MESSAGE_PREFIX}1: mean?\n\nplease explain"
    rows = _store(_instructor(step_instruction(1, 'answer the person', words)))
    assert rows == [('User', words, SESSION)], rows


def test_the_persons_words_start_at_the_first_mark():
    """The person can type the mark; everything after the producer's own
    (first) mark is theirs."""
    from hartos.create_recipe import step_instruction
    words = 'what does ,Latest User message: mean in my log?'
    rows = _store(_instructor(step_instruction(1, 'answer the person', words)))
    assert rows == [('User', words, SESSION)], rows


def test_text_that_only_opens_like_a_step_line_is_stored_as_any_other_text():
    """No number, no dispatch (dispatch_action_id is None): the sink's old
    copy took this for a step line and kept only the tail."""
    text = 'Execute Action items from the meeting ,Latest User message: none yet'
    rows = _store(_instructor(text))
    assert rows == [('ChatInstructor', text, SESSION)], rows


def test_the_sink_keeps_no_marker_of_its_own():
    """One reader: the sink has no opening-of-a-step-line constant beside
    lifecycle_hooks._DISPATCH_MARKER."""
    import integrations.channels.memory.shared_history as sh
    assert not hasattr(sh, '_CREATE_STEP_START')
    assert not hasattr(sh, '_REUSE_STEP_START')
