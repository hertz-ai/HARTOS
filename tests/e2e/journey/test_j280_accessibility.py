"""J280-J289 · Accessibility (a11y).

Untested = excludes millions + legal liability in many jurisdictions.
Screen-reader, keyboard-only, high-contrast, zoom, reduced motion.

Most of these are UI-layer concerns tested on the Hevolve/Nunba side.
This module documents the CONTRACT the HARTOS backend must honor —
ARIA-friendly response shapes, no UI-only assumptions in errors.
"""
from __future__ import annotations

import pytest

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _harness_shim import harness, skip_if_missing


class TestJ280ScreenReaderFriendly:
    def test_api_errors_have_human_readable_messages(self):
        pytest.skip(
            'J280 RED — no invariant test that every 4xx API response '
            "carries a 'message' field suitable for a screen reader "
            '(not just a machine code)'
        )


class TestJ281KeyboardOnly:
    def test_no_endpoint_requires_mouse_coordinate_input(self):
        pytest.skip('J281 RED — invariant (UI-driver-layer): every '
                    'user action reachable via keyboard; not asserted')


class TestJ282HighContrast:
    def test_theme_service_offers_hc_preset(self):
        skip_if_missing('integrations.agent_engine.theme_service:ThemeService')
        pytest.skip('J282 RED — high-contrast theme preset journey gap')


class TestJ283Zoom:
    def test_ui_does_not_truncate_at_200_percent_zoom(self):
        pytest.skip('J283 RED — 200%-zoom regression needs Playwright-'
                    'level visual test; structural gap')


class TestJ284ReducedMotion:
    def test_prefers_reduced_motion_honored(self):
        pytest.skip('J284 RED — prefers-reduced-motion CSS media query '
                    'not tested in either UI surface')


class TestJ285ColorBlindPalette:
    def test_palette_meets_deuteranopia_contrast(self):
        pytest.skip('J285 RED — WCAG AA contrast audit of theme '
                    'palette never run')


class TestJ286AriaLiveChat:
    def test_chat_response_marked_aria_live(self):
        pytest.skip('J286 RED — chat stream has no aria-live=polite '
                    'announcement for incremental tokens')


class TestJ287VoiceOnlyMode:
    def test_voice_mode_full_task_completion_without_screen(self):
        pytest.skip(
            'J287 RED — audio-first users (blind / low-vision / '
            'hands-busy) must complete any task end-to-end without '
            'looking at screen; no test asserts this'
        )


class TestJ288CaptionsForTts:
    def test_tts_output_accompanied_by_written_transcript(self):
        pytest.skip('J288 RED — TTS with simultaneous caption (deaf / '
                    'HoH users who still want voice) journey untested')


class TestJ289LocalesA11y:
    def test_aria_labels_translated_for_non_english_users(self):
        pytest.skip('J289 RED — ARIA labels must translate with the '
                    'rest of the UI; no test asserts this parity')
