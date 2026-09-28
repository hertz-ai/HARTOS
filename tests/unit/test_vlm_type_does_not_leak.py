"""The VLM 'type' action must not hand what it typed back to anyone.

Two leaks, both on the path a credential takes when the loop fills a login
form (the owner's requirement: the real value is used, never shown):

  * the action's result, which goes straight back into the model's context,
    carried the first 50 characters typed ("Typed: <text>...");
  * the text was pasted through the system clipboard and left there, readable
    by any process until the user copied something else.

These tests run the real _execute_inprocess with recording fakes for
pyautogui and pyperclip.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

from integrations.vlm import local_computer_tool as lct  # noqa: E402

SECRET = 'hunter2-Correct-Horse'


class _FakeGui:
    def __init__(self):
        self.calls = []

    def hotkey(self, *keys):
        self.calls.append(('hotkey', keys))

    def typewrite(self, text, interval=0):
        self.calls.append(('typewrite', text))


class _FakeClipboard:
    def __init__(self, before):
        self.value = before
        self.history = []

    def copy(self, text):
        self.value = text
        self.history.append(text)

    def paste(self):
        return self.value


@pytest.fixture
def gui(monkeypatch):
    fake = _FakeGui()
    monkeypatch.setattr(lct, 'pyautogui', fake)
    monkeypatch.setattr(lct.time, 'sleep', lambda _s: None)
    return fake


def test_result_does_not_carry_the_typed_text(gui, monkeypatch):
    monkeypatch.setattr(lct, 'pyperclip', _FakeClipboard('earlier copy'))
    result = lct._execute_inprocess({'action': 'type', 'text': SECRET})
    assert SECRET[:5] not in result['output']
    assert result['output'].startswith('Typed')
    assert str(len(SECRET)) in result['output'], 'it still says how much was typed'


def test_clipboard_is_restored_after_the_paste(gui, monkeypatch):
    board = _FakeClipboard('earlier copy')
    monkeypatch.setattr(lct, 'pyperclip', board)
    lct._execute_inprocess({'action': 'type', 'text': SECRET})
    assert ('hotkey', ('ctrl', 'v')) in gui.calls, 'the text is still pasted'
    assert board.history[0] == SECRET
    assert board.value == 'earlier copy', 'the secret must not stay on the clipboard'


def test_without_a_clipboard_it_types_and_still_does_not_echo(gui, monkeypatch):
    monkeypatch.setattr(lct, 'pyperclip', None)
    result = lct._execute_inprocess({'action': 'type', 'text': SECRET})
    assert ('typewrite', SECRET) in gui.calls
    assert SECRET[:5] not in result['output']
