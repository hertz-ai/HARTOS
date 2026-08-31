"""#718 — the gather-info SALVAGE path must not stamp its stub 'completed'.

When `gather_info` fails to parse N times, hart_intelligence_entry salvages a
placeholder config so creation stops looping.  That is legitimate.  What is not
legitimate is the status it writes: the adjacent log line calls it
"Too many gather_info failures ... salvaging partial config", and the dict it
builds is explicitly named `partial` — then sets ``'status': 'completed'``.

MEASURED 2026-08-29 on the live corpus (Documents/Nunba/data/prompts):

    624 agent configs, 610 with status='completed'
    485 of the unreusable ones have ZERO action recipe files on disk
    367 of those 485 carry this salvage path's exact fingerprint —
        agent_name='auto.agent<last4>' AND name='Agent <prompt_id>'

So this one literal accounts for 367 configs that every reader believes are
finished agents.  Both listing endpoints compute
``'is_active': data.get('status', '') == 'completed'``
(hart_intelligence_entry.py:10648 and :10703), so a stub whose creation FAILED
is published as an active agent.

Why 'pending' and not a new token: the codebase already uses 'pending' for
not-done in this exact vocabulary — create_recipe.py:2208 documents
``{"status":"completed" | "pending"}`` and the salvage stub's OWN action
literal already carries ``'status': 'pending'``.  No new vocabulary.

AST-level so it cannot be defeated by moving code, and so it needs no Flask
app / heavy import.  This is a literal-value defect; the literal is what we pin.
"""
import ast
import os

_HIE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'hart_intelligence_entry.py')


def _salvage_dicts():
    """Every dict literal that is unmistakably the gather-info salvage stub.

    Identified by its own fingerprint — the two keys whose f-string values are
    what the 367 on-disk configs actually carry — not by line number.
    """
    with open(_HIE, 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if 'status' in keys and 'agent_name' in keys and 'name' in keys \
                and 'flows' in keys and 'extra_information' in keys:
            found.append(dict(zip(keys, node.values)))
    return found


def test_the_salvage_stub_is_still_findable():
    """Guard the guard: if the literal moves or is renamed, fail loudly rather
    than silently passing on zero matches (a vacuous guard is not a guard)."""
    assert _salvage_dicts(), (
        'could not locate the gather-info salvage dict in '
        'hart_intelligence_entry.py — re-point this test rather than deleting it')


def test_salvage_stub_does_not_claim_completed():
    for d in _salvage_dicts():
        status = d.get('status')
        assert isinstance(status, ast.Constant), (
            'salvage status must stay a plain literal so it is auditable')
        assert status.value != 'completed', (
            "the salvage path writes a config it calls 'partial' after N FAILED "
            "gather turns; stamping it 'completed' publishes a failed creation "
            "as an active agent (367 such configs on disk, 2026-08-29)")


def test_salvage_stub_uses_the_existing_not_done_token():
    """Do not invent vocabulary — 'pending' already means not-done here."""
    for d in _salvage_dicts():
        assert d['status'].value == 'pending', (
            f"expected the codebase's existing 'pending' token, got "
            f"{d['status'].value!r}")
