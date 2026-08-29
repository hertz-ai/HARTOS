"""`is_create_agent` must mean the SAME thing at both sites that read it.

MEASURED LIVE 2026-08-29 21:30 (clean session, user clean-1788019205).
Prompt: "What is 17 multiplied by 4? Reply with just the number."
draft-telemetry: confidence 0.5, delegate 'local', is_casual False,
is_create_agent True.  The reply was the draft's own 158-char ramble
about recalling a conversation.  No arithmetic, no tools, no escalation.

Why, in hart_intelligence_entry.py's draft-first elif chain:

  1. `if is_create_agent and _draft_conf >= _DRAFT_INTENT_CONFIDENCE`
     -> False.  0.5 < 0.75, so the CREATE route correctly declines to act
     on an uncertain flag.  This gate is right and stays.

  2. `elif _vision_keyword_override(prompt)`  -> False.

  3. `elif delegate in ('local','hive') and not is_casual
        and not is_create_agent`
     -> False, blocked SOLELY by the raw flag being truthy.

  4. `else:` -> returns `result['response']`, the draft's raw text, as the
     user-visible answer.

So a turn whose create-intent is set but NOT TRUSTED can neither create
nor delegate: branch 1 refuses to act on the flag, and branch 3 still
treats it as authoritative.  One name, two vocabularies — site 1 reads
`is_create_agent` as "trusted create intent", site 3 reads it as "any
create signal at all".

This is the 2026-05-07 regression recurring through a different door.
Branch 3's own comment records it: "Pre-fix this fell through to the
`else` standby reply ('I'll gather that research...'), promising work
that never happened."  Branch 3 was added to close that hole; an
untrusted is_create_agent re-opens it.

The invariant: whatever predicate decides that create-intent is
ACTIONABLE must also be the predicate that decides it SUPPRESSES
delegation.  An untrusted flag has to be treated as absent, consistently.

    python -m pytest tests/unit/test_create_intent_trust_is_one_predicate.py --noconftest -q
"""
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_SRC = open(os.path.join(ROOT, 'hart_intelligence_entry.py'),
            encoding='utf-8').read()


def _delegate_branch_condition():
    """Source text of the `delegate in ('local','hive')` elif condition."""
    m = re.search(
        r"elif \(result\.get\('delegate'\) in \('local', 'hive'\)(.*?)\):",
        _SRC, re.DOTALL)
    assert m, "delegate local/hive branch not found — chain was restructured"
    return m.group(1)


def test_delegate_branch_is_not_blocked_by_an_untrusted_flag():
    """Branch 3 must not gate on the RAW is_create_agent flag.

    Gating on the raw flag is what strands a low-confidence turn between
    'too uncertain to create' and 'not allowed to delegate'.
    """
    cond = _delegate_branch_condition()
    assert not re.search(r"not\s+result\.get\('is_create_agent'\)", cond), (
        "branch 3 still gates on the RAW is_create_agent flag; a turn with "
        "is_create_agent=True below _DRAFT_INTENT_CONFIDENCE falls through "
        "to the else and the draft's raw reply is served as the answer "
        "(measured live: 'What is 17 multiplied by 4?' -> a memory-recall "
        "ramble, conf 0.5)")


def test_both_sites_share_one_trust_predicate():
    """The actionable-create predicate must be computed once and reused.

    Two independent spellings of 'is this create intent real?' is exactly
    how the two vocabularies drifted apart in the first place.
    """
    assert '_create_intent_actionable' in _SRC, (
        'expected a single named predicate for "create intent is actionable"')
    uses = len(re.findall(r'_create_intent_actionable', _SRC))
    assert uses >= 3, (
        f'expected one definition + at least two consumers, found {uses} '
        'references — both the CREATE branch and the delegate branch must '
        'read the same predicate')


def test_confidence_floor_still_guards_the_create_route():
    """Anti-vacuous: the fix must not simply delete the confidence gate.

    Dropping _DRAFT_INTENT_CONFIDENCE would 'fix' this test by letting a
    coin-flip classification force an irreversible CREATE — the opposite
    of what the floor exists for.
    """
    assert '_DRAFT_INTENT_CONFIDENCE' in _SRC, 'confidence floor removed'
    m = re.search(r'_DRAFT_INTENT_CONFIDENCE\s*=\s*([0-9.]+)', _SRC)
    assert m, '_DRAFT_INTENT_CONFIDENCE assignment not found'
    assert float(m.group(1)) > 0.0, 'confidence floor neutralised to 0'


def test_pinned_log_substring_is_preserved():
    """test_casual_conv_classifier_override.py regex-pins this exact text.

    Kept verbatim on purpose: that pin encodes an owner design decision
    (casual_conv is session shape, not a per-turn verdict). The literal
    'is_create_agent=False' in the message now reads as "no ACTIONABLE
    create intent" — the condition changed, the pinned string did not.
    """
    assert 'is_casual=False, is_create_agent=False — routing to "' in _SRC, (
        'pinned log substring changed — this breaks '
        'test_casual_conv_classifier_override.test_no_classifier_override_'
        'of_casual_conv, which greps for it')
