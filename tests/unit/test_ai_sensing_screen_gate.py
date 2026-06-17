"""B3-wire: the AI 'screen' sense must be a REAL kill-switch, not a dead flag.

Before this, core.ai_sensing.allowed('screen') had ZERO enforcing consumers —
the computer-use grab (local_computer_tool.take_screenshot) and the LLM
screenshot tool grabbed the screen regardless of the human's choice. Now the
grab itself refuses when 'screen' is cut, mirroring the working mic gate.

Behavioural: mock only the ai_sensing boundary; assert the grab refuses when
cut and proceeds when allowed. Must land before native windows / screencast
(Phase 5/7) add new capture surfaces.

    python -m pytest tests/unit/test_ai_sensing_screen_gate.py --noconftest -p no:capture -q
"""
from unittest.mock import patch

import pytest


def test_grab_refused_when_screen_sense_cut():
    from integrations.vlm import local_computer_tool
    with patch('core.ai_sensing.allowed', return_value=False):
        with pytest.raises(PermissionError):
            local_computer_tool.take_screenshot('http')


def test_grab_proceeds_when_screen_allowed():
    from integrations.vlm import local_computer_tool
    with patch('core.ai_sensing.allowed', return_value=True), \
         patch.object(local_computer_tool, 'pooled_get') as pg:
        pg.return_value.raise_for_status = lambda: None
        pg.return_value.json.return_value = {'base64_image': 'IMGDATA'}
        out = local_computer_tool.take_screenshot('http')
    assert out == 'IMGDATA'
