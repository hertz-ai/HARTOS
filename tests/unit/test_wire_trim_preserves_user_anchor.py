"""#730a — the wire-trim left-drop must never remove the last user message.

Measured on the live 500s (2026-08-30 17:48:11-12, source=autogen.reuse,
thread spec_expert_0, 3 occurrences in llm_outbound.jsonl):

    post-trim roles: ['system', 'assistant', 'assistant', 'tool', 'assistant']
    llama-server:    500 — the Qwen3.5 chat template raised
                     "raise_exception('No user query found in messages.')"

The template's tool branch fires whenever a role='tool' message survives in
the history, and it then searches backward for a user message to anchor on.
`_trim_to_budget` left-drops from index 1 with no regard for role, so the
earliest messages — where the user's task instruction lives — go first, and
the surviving assistant/tool tail is exactly the shape the template refuses.
The function's own docstring promises to "always keep at least the system
message + the most-recent user/assistant message"; the code kept only
"most-recent message", whatever its role.

Every such request is rejected by llama-server, which killed the reuse
ACTION loop (the conversation leg worked; action execution died) and any
daemon goal whose history grows past the budget.

    python -m pytest tests/unit/test_wire_trim_preserves_user_anchor.py --noconftest -q
"""
import core.llm_outbound_logger as lol


def _msg(role, content, **extra):
    d = {'role': role, 'content': content}
    d.update(extra)
    return d


def _long_conversation():
    """system + early user task + a long assistant/tool tail (the live shape)."""
    msgs = [
        _msg('system', 's' * 400),
        _msg('user', 'Collect inference feedback and coordinate fine-tuning.'),
    ]
    for i in range(12):
        msgs.append(_msg('assistant', ('plan step %d ' % i) + 'a' * 300))
        msgs.append(_msg('tool', '{"result": "%d"}' % i))
    msgs.append(_msg('assistant', 'final ' + 'a' * 300))
    return msgs


def _trim_with_tiny_budget(monkeypatch, messages):
    monkeypatch.setattr(lol, '_get_budget_per_slot', lambda: 900)
    body = {'model': 'llama', 'messages': messages, 'max_tokens': 64}
    trimmed, n_dropped, _, _, _, _ = lol._trim_to_budget(body)
    assert n_dropped > 0, 'fixture failed to force any trimming'
    return trimmed['messages']


def test_trim_keeps_the_last_user_message(monkeypatch):
    out = _trim_with_tiny_budget(monkeypatch, _long_conversation())
    roles = [m.get('role') for m in out]
    assert 'user' in roles, (
        'trim dropped every user message (roles=%r) — llama.cpp\'s Qwen3.5 '
        'template rejects tool-carrying histories with no user anchor '
        '("No user query found in messages."), so this request 500s' % roles)


def test_a_body_with_no_user_message_gets_exactly_one_seeded(monkeypatch):
    """CONTRACT CHANGED 2026-09-03 by 339e891da -- deliberately, and this is
    the record.

    aa40350 (2026-08-30) added this file and put this case OUT of scope: "a
    producer that never sent a user message is out of this fix's scope; the
    trimmer must not invent one, only refuse to delete an existing one."  The
    test asserted ``all(role != 'user')`` to pin that.

    That scope line did not survive contact with the live system.  On
    2026-09-03 03:40:07 a reuse reply reached the wire as
    ``[system, assistant, tool, assistant]`` -- user-less at the PRODUCER, not
    by trimming -- and llama-server's Qwen3 template 500'd with "No user query
    found in messages."  The preserve-only logic could not help, because there
    was nothing to preserve.  339e891 therefore made the wire seed one user
    turn for any user-less body, on the grounds that the wire is the single
    chokepoint every outbound body crosses, and says so in the log.

    So refusing to invent a user turn is no longer the contract, and the old
    assertion had become a guard against the fix.  Two things must be pinned
    instead.  (1) The seed survives the TRIMMED path: ``test_wire_seeds_user_turn``
    covers the under-budget early-return, this covers the over-budget branch
    where messages are actually dropped -- the same branch #730a is about.
    (2) What "must not invent one" still means, asserted below: seeding is
    idempotent and never adds a SECOND user turn.

    MERGE NOTE 2026-09-11: two lanes rewrote this same test for this same
    contract change, under two names.  One body asserted only that a user role
    survives; this one additionally pins the COUNT, the seed CONSTANT and the
    POSITION, and adds the idempotence case below -- it subsumes the other
    assertion rather than competing with it, so there is one test here, not
    two near-duplicates.  The weaker lane's docstring history is folded in
    above; nothing it guarded was dropped.
    """
    from core.constants import WIRE_USER_SEED_TEXT
    msgs = [_msg('system', 's' * 400)]
    for i in range(12):
        msgs.append(_msg('assistant', 'a' * 300))
        msgs.append(_msg('tool', '{"r": %d}' % i))
    out = _trim_with_tiny_budget(monkeypatch, msgs)

    users = [m for m in out if m.get('role') == 'user']
    assert len(users) == 1, (
        'a user-less body must come back with exactly one seeded user turn, '
        'got %d (roles=%r)' % (len(users), [m.get('role') for m in out]))
    assert users[0].get('content') == WIRE_USER_SEED_TEXT, (
        'the seed must be the shared constant, not a string invented here')
    # Seeded AFTER the system message: the template anchors backward from the
    # tool turns, and a user turn ahead of the system prompt is a different
    # conversation shape.
    assert out[0].get('role') == 'system'
    assert out[1].get('role') == 'user'


def test_seeding_is_idempotent_when_a_user_turn_already_exists(monkeypatch):
    """The half of "must not invent one" that survived: a body that HAS a user
    turn is never given a second one, so the seed cannot displace the real
    anchor the template is meant to find."""
    from core.constants import WIRE_USER_SEED_TEXT
    out = _trim_with_tiny_budget(monkeypatch, _long_conversation())
    users = [m for m in out if m.get('role') == 'user']
    assert len(users) == 1, 'seeded a duplicate user turn (roles=%r)' % (
        [m.get('role') for m in out],)
    assert users[0].get('content') != WIRE_USER_SEED_TEXT, (
        "the real user task was replaced by the seed placeholder")


def test_trim_still_reaches_budget_with_anchor_kept(monkeypatch):
    """Keeping the anchor must not break the trim's budget contract —
    everything else around it is still droppable."""
    messages = _long_conversation()
    monkeypatch.setattr(lol, '_get_budget_per_slot', lambda: 900)
    body = {'model': 'llama', 'messages': messages, 'max_tokens': 64}
    _, n_dropped, _, _, est_after, budget = lol._trim_to_budget(body)
    assert n_dropped > 0
    assert est_after <= budget, (
        'post-trim estimate %d exceeds budget %d' % (est_after, budget))
