"""A scroll the VLM is offered must actually scroll.

The loop's action list and the Qwen backend both tell the model it may
answer "scroll_up" / "scroll_down" (local_loop._VLM_ACTION_LIST, and
test_vlm_local_loop asserts that offer), but the executor had no branch
for either: every scroll came back "Unknown action", so a page longer
than one screen (a webmail inbox, a long form) could never be reached.
These tests drive the real executor with pyautogui mocked at the boundary.
"""
from unittest.mock import MagicMock, patch

import integrations.vlm.local_computer_tool as lct


def _run(action):
    gui = MagicMock()
    with patch.object(lct, 'pyautogui', gui):
        out = lct._execute_inprocess(action)
    return gui, out


class TestScrollRuns:

    def test_scroll_down_scrolls_down(self):
        gui, out = _run({'action': 'scroll_down'})
        assert not out.get('error'), out
        (clicks,), _ = gui.scroll.call_args
        assert clicks < 0

    def test_scroll_up_scrolls_up(self):
        gui, out = _run({'action': 'scroll_up'})
        assert not out.get('error'), out
        (clicks,), _ = gui.scroll.call_args
        assert clicks > 0

    def test_scroll_at_a_coordinate_scrolls_there(self):
        gui, _ = _run({'action': 'scroll_down', 'coordinate': [400, 300]})
        _, kwargs = gui.scroll.call_args
        assert (kwargs.get('x'), kwargs.get('y')) == (400, 300)

    def test_an_amount_in_value_sets_the_distance(self):
        gui, _ = _run({'action': 'scroll_down', 'text': '3'})
        (clicks,), _ = gui.scroll.call_args
        assert clicks == -3

    def test_a_non_numeric_value_falls_back_to_the_default(self):
        gui, out = _run({'action': 'scroll_up', 'text': 'a lot'})
        assert not out.get('error'), out
        (clicks,), _ = gui.scroll.call_args
        assert clicks == lct.SCROLL_DEFAULT_CLICKS


def test_every_offered_action_is_supported():
    """What the loop offers the model, the executor must accept."""
    for act in ('scroll_up', 'scroll_down'):
        assert act in lct.SUPPORTED_ACTIONS
