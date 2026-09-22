"""A draft that says "a bigger model must take this" is not the answer.

MEASURED 2026-09-22 08:52 on the MSI desktop (server.log.1:130526-130600).
The person typed "great open a notepad and type hi in it".  The 0.8B draft
answered in 3.9 s:

    {"reply": "I've opened a notepad for you and typed \"hi\" inside.",
     "delegate": "local", "confidence": 0.95, "is_casual": true, ...}

Nothing ran.  No get_tools line, no get_ans, no autogen -- the only LLM
call in the window is the draft.  The reply went to TTS one second later
and was spoken.  Computer_Action and Shell_Command sit in the is_first
tool set that turn would have loaded, so the machine could have done it.

Why: hart_intelligence_entry's routing branch read

    elif (result.get('delegate') in ('local', 'hive')
          and not result.get('is_casual') ...)

and the draft had emitted BOTH delegate='local' and is_casual=true.  The
casual verdict vetoed the hand-off, the `else` returned the draft's text
as final.  The dispatcher's own reply-replacing guards all require
delegate == 'none', so none of them swapped the text for a standby either.
The AGENT_BOUND guard's comment records the identical defect on a
different branch: "the 13:30 live turn shipped 'saved and confirmed' while
nothing had run".  It was fixed for delegate='none' only.

escalation_reasons.CLASSIFIER_DELEGATE already declares delegate='local'
the baseline escalation.  The code now honours its own taxonomy: the
predicate below is what the branch reads, and is_casual has no vote.
"""
from integrations.agent_engine.escalation_reasons import (
    EscalationReason, draft_delegates)


# The exact six envelopes surviving in server.log.1 / .4 and gui_app.log.2
# / .5 on 2026-09-22.  One of them fabricated; five were real chit-chat.
_LIVE_ENVELOPES = [
    # speculation_id, delegate, is_casual, reply_len
    ('2e7c9c1c-033', 'none', True, 37),
    ('43e93b07-915', 'none', True, 37),
    ('a907f158-8cc', 'none', False, 95),
    ('b7411c35-b46', 'none', True, 31),
    ('c77082b0-af7', 'local', True, 52),   # "I've opened a notepad..."
    ('deed8c04-5fe', 'none', True, 32),
]


def test_the_notepad_turn_hands_off_despite_is_casual():
    """The envelope that fabricated.  delegate wins; is_casual has no vote."""
    assert draft_delegates('local') is True


def test_only_the_fabricating_envelope_hands_off():
    """Against the six real envelopes: exactly the notepad turn delegates,
    so the fast path is untouched for the greetings it exists for."""
    handed_off = [sid for sid, delegate, _casual, _n in _LIVE_ENVELOPES
                  if draft_delegates(delegate)]
    assert handed_off == ['c77082b0-af7'], handed_off


def test_is_casual_cannot_veto_a_hand_off():
    """Both orders of the contradiction read the same way: the draft said
    it cannot finish, so it does not finish."""
    for casual in (True, False):
        assert draft_delegates('local') is True, casual
        assert draft_delegates('hive') is True, casual


def test_the_draft_that_answers_for_itself_is_left_alone():
    for delegate in ('none', '', None, 'NONE', ' none '):
        assert draft_delegates(delegate) is False, repr(delegate)


def test_the_predicate_reads_the_taxonomy_it_sits_beside():
    """CLASSIFIER_DELEGATE names local/hive the baseline escalation; the
    predicate must agree with the enum's own docstring, not restate it."""
    assert EscalationReason.CLASSIFIER_DELEGATE.value == 'classifier_delegate'
    assert draft_delegates('Local') and draft_delegates('HIVE')
    assert draft_delegates(EscalationReason.CLASSIFIER_DELEGATE) is False, (
        'an EscalationReason is a WHY, not a delegate target')


# -- the /chat branch itself ------------------------------------------------
#
# The tests above prove the PREDICATE.  They cannot see the branch that uses
# it: re-adding `and not result.get('is_casual')` to the /chat elif left all
# of them, and the regex pin in test_draft_first_chat_route (which accepts
# any condition after `_draft_delegates(result)`), green -- fix-all's
# cross-review, 2026-09-23, by running it.  No interpreter here imports
# hart_intelligence_entry, so the branch is checked on its AST: the one
# condition that decides "the draft hands off" must not consult is_casual.

import ast as _ast
import os as _os

_ENTRY = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                       '..', '..', 'hart_intelligence_entry.py')


def _hand_off_conditions(src):
    """Every if/elif test inside chat() that calls _draft_delegates."""
    tree = _ast.parse(src)
    chat = next(n for n in _ast.walk(tree)
                if isinstance(n, _ast.FunctionDef) and n.name == 'chat')
    out = []
    for node in _ast.walk(chat):
        if isinstance(node, _ast.If) and any(
                isinstance(c, _ast.Call) and getattr(c.func, 'id', '') == '_draft_delegates'
                for c in _ast.walk(node.test)):
            out.append(node.test)
    return out


def _casual_votes(test_expr):
    """True if the condition reads is_casual anywhere (a .get('is_casual'),
    a ['is_casual'] subscript, or a name carrying it)."""
    for n in _ast.walk(test_expr):
        if isinstance(n, _ast.Constant) and n.value == 'is_casual':
            return True
        if isinstance(n, _ast.Name) and 'is_casual' in n.id:
            return True
    return False


def test_source_guard_the_chat_hand_off_gives_is_casual_no_vote():
    src = open(_ENTRY, encoding='utf-8').read()
    conds = _hand_off_conditions(src)
    assert len(conds) == 1, (
        f'expected exactly one hand-off branch in chat(), found {len(conds)}')
    assert not _casual_votes(conds[0]), (
        'the /chat hand-off condition reads is_casual again: a draft that '
        'says delegate=local would be answered by its own text when it also '
        'says casual -- the "opened a notepad" fabrication (7824ed7b6)')


def test_source_guard_can_fail_on_the_pre_fix_condition():
    """The guard is only worth something if it can go red: it must flag the
    literal condition 7824ed7b6 removed."""
    pre_fix = (
        "def chat():\n"
        "    if x:\n        pass\n"
        "    elif (_draft_delegates(result)\n"
        "          and not result.get('is_casual')\n"
        "          and not _create_intent_actionable):\n"
        "        pass\n")
    conds = _hand_off_conditions(pre_fix)
    assert len(conds) == 1 and _casual_votes(conds[0])
