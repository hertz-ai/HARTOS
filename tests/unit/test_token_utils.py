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
