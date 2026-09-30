"""#112: core.token_utils is the single source for token counting (tiktoken with
a word-split fallback) — replacing HIE's module-global `encoding` + inline
len(encode()) chains. Behavioural: call the real functions, assert sane counts,
and force the no-tiktoken path to prove the fallback. No grep tests.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.token_utils as tu  # noqa: E402
from core.token_utils import count_tokens_for_text, count_tokens_for_messages  # noqa: E402


def test_counts_text_positive():
    n = count_tokens_for_text("hello world, this is a token counting test")
    assert isinstance(n, int) and n > 0


def test_empty_text_is_zero():
    assert count_tokens_for_text("") == 0


def test_messages_sum_positive():
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there friend, how are you"},
    ]
    n = count_tokens_for_messages(msgs)
    assert isinstance(n, int) and n > 0


def test_fallback_when_tiktoken_unavailable(monkeypatch):
    # Force the no-tiktoken path: _get_encoding -> None -> char/N approximation.
    monkeypatch.setattr(tu, '_get_encoding', lambda model=None: None)
    s = "one two three four five"
    expected = max(0, int(len(s) / tu._CHARS_PER_TOKEN_FALLBACK))
    got = count_tokens_for_text(s)
    assert got == expected and got > 0   # == proves the fallback ran (not tiktoken)


def test_non_string_is_coerced():
    # Accepts Any (coerces to str) — must not raise, must count >= 1.
    assert count_tokens_for_text(1234567) >= 1


# ── #104: one bound for text, one fair share for bundled tool results ──

def test_bound_text_keeps_the_head_and_marks_the_cut():
    from core.token_utils import bound_text
    assert bound_text('short', 100) == 'short'
    out = bound_text('x' * 500, 100)
    assert len(out) <= 100
    assert out.startswith('xxxx') and out.endswith(' ...[cut]')
    assert bound_text(None, 10) == 'None'


def test_truncate_text_to_tokens_keeps_the_head():
    from core.token_utils import truncate_text_to_tokens
    text = ' '.join(f'word{i}' for i in range(500))
    out = truncate_text_to_tokens(text, 50)
    assert text.startswith(out[:20])
    assert count_tokens_for_text(out) <= 52
    assert truncate_text_to_tokens('hi', 50) == 'hi'


def test_results_that_fit_are_left_alone():
    """#104 review, measured on the create chain: get_user_id plus an
    840-token search fit a 1000-token allowance, and an even split still cut
    the search to 500."""
    from core.token_utils import fit_texts_to_token_budget
    search = ' '.join(f'result {i}: a snippet' for i in range(120))
    assert count_tokens_for_text(search) + count_tokens_for_text('7001') <= 1000
    texts = ['7001', search]
    assert fit_texts_to_token_budget(texts, 1000) == texts


def test_over_budget_the_short_keep_theirs_and_the_long_share_the_rest():
    from core.token_utils import fit_texts_to_token_budget
    short = 'user 7001'
    long_a = ' '.join(f'alpha{i}' for i in range(2000))
    long_b = ' '.join(f'beta{i}' for i in range(2000))
    out = fit_texts_to_token_budget([short, long_a, long_b], 600)
    assert out[0] == short
    # a few tokens of slack: a cut re-encodes at a token boundary
    assert sum(count_tokens_for_text(t) for t in out) <= 610
    assert abs(count_tokens_for_text(out[1]) - count_tokens_for_text(out[2])) <= 5


if __name__ == '__main__':
    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)

    test_counts_text_positive(); print('PASS counts-positive')
    test_empty_text_is_zero(); print('PASS empty-zero')
    test_messages_sum_positive(); print('PASS messages-sum')
    test_fallback_when_tiktoken_unavailable(_MP()); print('PASS fallback')
    test_non_string_is_coerced(); print('PASS coerce')
    print('OK 5/5')
