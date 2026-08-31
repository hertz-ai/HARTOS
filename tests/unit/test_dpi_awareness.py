"""Behavioural tests for core.dpi_awareness — the ONE place that sets Windows
per-monitor DPI awareness (so VLM screenshot coords and click coords agree).

The real Win32 calls can't run on the Linux CI box, so these mock sys.platform
+ ctypes.windll to drive the actual control flow: no-op off Windows, shcore
success, the shcore->user32 fallback, total-failure swallow, idempotency. 0%
covered before this file. Real function, mocked Win32 boundary, no
source-substring checks.

    python -m pytest tests/unit/test_dpi_awareness.py -q --noconftest
"""
from __future__ import annotations

import ctypes

import pytest

from core import dpi_awareness


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # The module caches success in a global — reset it so each test starts clean.
    monkeypatch.setattr(dpi_awareness, "_dpi_aware_set", False)
    yield


class _Shcore:
    def __init__(self, exc=None):
        self._exc = exc
        self.arg = None

    def SetProcessDpiAwareness(self, value):  # noqa: N802 (Win32 name)
        if self._exc:
            raise self._exc
        self.arg = value


class _User32:
    def __init__(self, exc=None):
        self._exc = exc
        self.called = False

    def SetProcessDPIAware(self):  # noqa: N802 (Win32 name)
        if self._exc:
            raise self._exc
        self.called = True


class _Windll:
    def __init__(self, shcore, user32):
        self.shcore = shcore
        self.user32 = user32


def _fake_win32(monkeypatch, shcore, user32):
    monkeypatch.setattr(dpi_awareness.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", _Windll(shcore, user32), raising=False)


class TestEnsureDpiAware:
    def test_noop_off_windows(self, monkeypatch):
        monkeypatch.setattr(dpi_awareness.sys, "platform", "linux")
        # windll must never be touched off Windows.
        monkeypatch.setattr(ctypes, "windll", object(), raising=False)
        dpi_awareness.ensure_dpi_aware()
        assert dpi_awareness.is_dpi_aware() is False

    def test_sets_via_shcore_with_per_monitor_value(self, monkeypatch):
        shcore, user32 = _Shcore(), _User32()
        _fake_win32(monkeypatch, shcore, user32)
        dpi_awareness.ensure_dpi_aware()
        assert dpi_awareness.is_dpi_aware() is True
        assert shcore.arg == dpi_awareness._PER_MONITOR_DPI_AWARE == 2
        assert user32.called is False  # shcore worked — no fallback

    def test_falls_back_to_user32_when_shcore_absent(self, monkeypatch):
        shcore = _Shcore(exc=AttributeError("no shcore"))
        user32 = _User32()
        _fake_win32(monkeypatch, shcore, user32)
        dpi_awareness.ensure_dpi_aware()
        assert user32.called is True
        assert dpi_awareness.is_dpi_aware() is True

    def test_falls_back_on_oserror_too(self, monkeypatch):
        shcore = _Shcore(exc=OSError("win7"))
        user32 = _User32()
        _fake_win32(monkeypatch, shcore, user32)
        dpi_awareness.ensure_dpi_aware()
        assert user32.called is True
        assert dpi_awareness.is_dpi_aware() is True

    def test_total_failure_is_swallowed_and_not_marked_aware(self, monkeypatch):
        shcore = _Shcore(exc=AttributeError("no shcore"))
        user32 = _User32(exc=OSError("no user32 either"))
        _fake_win32(monkeypatch, shcore, user32)
        dpi_awareness.ensure_dpi_aware()  # must NOT raise
        assert dpi_awareness.is_dpi_aware() is False

    def test_idempotent_once_set_does_not_recall_win32(self, monkeypatch):
        shcore, user32 = _Shcore(), _User32()
        _fake_win32(monkeypatch, shcore, user32)
        monkeypatch.setattr(dpi_awareness, "_dpi_aware_set", True)  # already set
        dpi_awareness.ensure_dpi_aware()
        assert shcore.arg is None, "must not re-invoke Win32 when already aware"


class TestIsDpiAware:
    def test_reflects_the_flag(self, monkeypatch):
        assert dpi_awareness.is_dpi_aware() is False
        monkeypatch.setattr(dpi_awareness, "_dpi_aware_set", True)
        assert dpi_awareness.is_dpi_aware() is True
