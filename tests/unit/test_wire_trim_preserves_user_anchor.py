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


def test_trim_without_any_user_message_is_unchanged_behavior(monkeypatch):
    """A producer that never sent a user message is out of this fix's scope —
    the trimmer must not invent one, only refuse to delete an existing one."""
    msgs = [_msg('system', 's' * 400)]
    for i in range(12):
        msgs.append(_msg('assistant', 'a' * 300))
        msgs.append(_msg('tool', '{"r": %d}' % i))
    out = _trim_with_tiny_budget(monkeypatch, msgs)
    assert all(m.get('role') != 'user' for m in out)


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
