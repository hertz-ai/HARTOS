"""The AI-control ribbon is told what each VLM step does.

The ribbon used to carry only show/hide, so it could say THAT the AI was in
control and nothing else; the step's action and reasoning stayed in the log.
Now each show request carries a one-line caption (_step_caption) as
``?text=``, which Nunba's /indicator/show puts on the ribbon beside its timer.

The caption BUILDER stays here with the loop that produces it; the ribbon
itself moved to activity_stream 2026-09-21, because it is one of the two
surfaces of a single announcement -- see
test_one_announcement_two_surfaces.py.
"""
import os
import sys

from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.vlm import activity_stream as acts  # noqa: E402
from integrations.vlm import local_loop as ll  # noqa: E402


def test_caption_reads_reasoning_then_action():
    assert ll._step_caption({'Next Action': 'left_click',
                             'Reasoning': 'Open Settings from the Start menu'}) \
        == 'Open Settings from the Start menu (left_click)'
    assert ll._step_caption({'Next Action': 'type'}) == 'type'
    assert ll._step_caption({'Reasoning': 'Task completed', 'Next Action': 'None'}) \
        == 'Task completed'
    assert ll._step_caption({}) == ''


def test_caption_is_one_line_and_bounded():
    long = ' '.join(['word'] * 80)
    line = ll._step_caption({'Reasoning': 'first\nsecond   third', 'Next Action': 'scroll'})
    assert line == 'first second third (scroll)'
    assert len(ll._step_caption({'Reasoning': long})) <= 160


def test_show_request_carries_the_caption_and_hide_carries_none():
    calls = []
    with patch('core.config_cache.is_bundled', return_value=True), \
            patch('core.config_cache._local_base', return_value='http://127.0.0.1:5000'), \
            patch('core.http_pool.pooled_get',
                  lambda url, timeout=None, **kw: calls.append((url, kw))):
        acts._ribbon(True, text='Open Settings (left_click)')
        acts._ribbon(True)
        acts._ribbon(False)
    assert calls[0] == ('http://127.0.0.1:5000/indicator/show',
                        {'params': {'text': 'Open Settings (left_click)'}})
    assert calls[1] == ('http://127.0.0.1:5000/indicator/show', {'params': None})
    assert calls[2] == ('http://127.0.0.1:5000/indicator/hide', {'params': None})


def test_outside_nunba_nothing_is_sent():
    with patch('core.config_cache.is_bundled', return_value=False), \
            patch('core.http_pool.pooled_get') as get:
        acts._ribbon(True, text='anything')
    get.assert_not_called()
