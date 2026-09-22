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
