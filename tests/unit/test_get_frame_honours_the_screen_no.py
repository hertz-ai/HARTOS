"""hart_intelligence_entry.get_frame's desktop-screenshot fallback honours
the owner's No to the screen.

Review of 55a6cde43 / 00fc7a37b (measured): after a No to both feeds the
FrameStore is empty (the gate drops frames), so get_frame fell through to
ImageGrab.grab() and Visual_Context_Camera saw the desktop the owner had
just refused.  The fallback now sits behind the one existing gate,
core.ai_sensing.allowed('screen'), the same check _handle_screenshot_tool
makes.

hart_intelligence_entry cannot be imported in a unit test (it boots the
whole runtime), so get_frame is lifted out of it by AST and run with the
module-level names it reads supplied; PIL.ImageGrab.grab is the only
stand-in, and it records whether it was called.

    python -m pytest tests/unit/test_get_frame_honours_the_screen_no.py -q
"""
import ast
import os
import sys

import pytest

np = pytest.importorskip('numpy')
flask = pytest.importorskip('flask')
ImageGrab = pytest.importorskip('PIL.ImageGrab')

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_ENTRY = os.path.join(_ROOT, 'hart_intelligence_entry.py')


@pytest.fixture
def get_frame(monkeypatch):
    """The entry's get_frame, an empty FrameStore behind it, no Redis, and a
    grab() that records each call and returns a small image."""
    from integrations.vision.frame_store import FrameStore
    with open(_ENTRY, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    nodes = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name == 'get_frame']
    assert len(nodes) == 1
    store = FrameStore()
    grabs = []

    class _Shot:
        def __array__(self, dtype=None, copy=None):
            return np.zeros((4, 4, 3), dtype=np.uint8)

    def _grab(*a, **k):
        grabs.append(1)
        return _Shot()

    monkeypatch.setattr(ImageGrab, 'grab', _grab)
    app = flask.Flask('get_frame_gate_test')
    ns = {'np': np, 'app': app, 'redis_client': None,
          'get_frame_store': lambda: store}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), _ENTRY, 'exec'), ns)
    return ns['get_frame'], grabs


@pytest.fixture
def gate():
    from core import ai_sensing
    yield ai_sensing
    ai_sensing.withhold('screen', False)
    ai_sensing.set_sense('screen', False)


def test_a_no_to_the_screen_means_no_screenshot(get_frame, gate):
    fn, grabs = get_frame
    gate.withhold('screen', True)
    assert fn('u1') is None, 'the desktop was handed out after a No'
    assert grabs == [], 'ImageGrab.grab() ran after the owner said No'


def test_the_eye_buttons_screen_cut_means_no_screenshot(get_frame, gate):
    fn, grabs = get_frame
    gate.set_sense('screen', True)
    assert fn('u1') is None
    assert grabs == []


def test_with_the_screen_allowed_the_fallback_still_grabs(get_frame, gate):
    """The control: the fallback exists for computer-use mode and must keep
    working when nothing has been refused."""
    fn, grabs = get_frame
    frame = fn('u1')
    assert frame is not None and frame.shape == (4, 4, 3)
    assert grabs == [1]
