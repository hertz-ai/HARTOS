"""Behavioural test: FrameCapture.record_to_video assembles captured frames into
a shareable video artifact (2026-06-02).

This is the missing "frames -> shareable demo video" step for the marketing
flywheel (record a demo of Nunba/HARTOS running, then post_to_channel(media_url)).
The test mocks the screen-capture BOUNDARY (capture_frame -> synthetic JPEGs),
calls the REAL assembly, and asserts a readable on-disk video/gif comes out with
the frames we fed in.
"""
from __future__ import annotations

import io
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _synthetic_jpeg(color):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (64, 48), color).save(buf, format='JPEG')
    return buf.getvalue()


def test_record_to_video_assembles_captured_frames(tmp_path, monkeypatch):
    try:
        import imageio.v2 as imageio  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        pytest.skip("imageio/PIL not available in this env")

    from integrations.remote_desktop.frame_capture import FrameCapture, FrameConfig

    cap = FrameCapture(FrameConfig(max_fps=10))
    palette = [_synthetic_jpeg((255, 0, 0)),
               _synthetic_jpeg((0, 255, 0)),
               _synthetic_jpeg((0, 0, 255))]
    seq = iter(palette * 20)
    # Mock the capture BOUNDARY — no real screen/display needed.
    monkeypatch.setattr(cap, 'capture_frame', lambda: next(seq, palette[-1]))

    out = tmp_path / 'demo.mp4'
    res = cap.record_to_video(duration_s=0.6, fps=5, output_path=str(out))

    assert res['ok'] is True, res
    assert res['frames'] >= 1, res
    assert res['format'] in ('mp4', 'gif'), res
    assert os.path.exists(res['path']) and os.path.getsize(res['path']) > 0, res

    # The artifact must be a real, readable video/gif carrying the frames.
    import imageio.v2 as imageio
    back = imageio.mimread(res['path'])
    assert len(back) >= 1, "assembled file has no frames"


def test_record_to_video_reports_error_when_no_frames(monkeypatch, tmp_path):
    """No capture backend / black screen -> a clean error, never a crash."""
    try:
        import imageio.v2 as imageio  # noqa: F401
    except ImportError:
        pytest.skip("imageio not available in this env")
    from integrations.remote_desktop.frame_capture import FrameCapture, FrameConfig
    cap = FrameCapture(FrameConfig(max_fps=10))
    monkeypatch.setattr(cap, 'capture_frame', lambda: None)  # nothing captured
    res = cap.record_to_video(duration_s=0.3, fps=5,
                              output_path=str(tmp_path / 'x.mp4'))
    assert res['ok'] is False and 'no frames' in res['error'].lower(), res
