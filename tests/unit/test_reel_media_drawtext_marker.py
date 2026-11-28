"""requires_drawtext skips a test on an ffmpeg that cannot draw text.

CI's distro ffmpeg has no libfreetype, so the end-card test that draws real
text died with ``Filter not found`` instead of skipping the way requires_ffmpeg
skips for no binary.  core.media_bin.ffmpeg_filters is the one place that
knows which filters a binary has; the marker asks it.
"""
import pytest

from core import media_bin
from tests.unit import reel_media_fixtures as fx


@pytest.fixture
def with_ffmpeg(monkeypatch):
    monkeypatch.setattr(fx, 'ffmpeg_available', lambda: True)


def test_a_binary_with_drawtext_is_available(with_ffmpeg, monkeypatch):
    monkeypatch.setattr(media_bin, 'ffmpeg_filters', lambda: frozenset({'scale', 'drawtext'}))
    assert fx.drawtext_available() is True


def test_a_binary_without_drawtext_is_not_available(with_ffmpeg, monkeypatch):
    monkeypatch.setattr(media_bin, 'ffmpeg_filters', lambda: frozenset({'scale', 'concat'}))
    assert fx.drawtext_available() is False


def test_no_binary_is_not_available_and_lists_nothing(monkeypatch):
    monkeypatch.setattr(fx, 'ffmpeg_available', lambda: False)

    def _boom():
        raise AssertionError('must not run ffmpeg when there is none')
    monkeypatch.setattr(media_bin, 'ffmpeg_filters', _boom)
    assert fx.drawtext_available() is False


def test_a_failed_listing_is_not_available(with_ffmpeg, monkeypatch):
    def _fail():
        raise media_bin.FFmpegFailed('ffmpeg -filters failed')
    monkeypatch.setattr(media_bin, 'ffmpeg_filters', _fail)
    assert fx.drawtext_available() is False
