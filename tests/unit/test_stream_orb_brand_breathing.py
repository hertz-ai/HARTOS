"""Pytest wrapper for the STREAM-orb behavioural harness.

Two DIFFERENT things live in the Node harness, and they are run separately
because only one of them is built.

SECTION C — the hero float — IS implemented and gates for real.
    place() is the single style writer; it must stamp a z-index that floats the
    orb above app windows (a focused .panel reaches 999) yet below the
    persistent chrome (ctx-menu 3000, taskbar 8000), keep pointer-events
    reachable, and never focus() anything during init (never trap focus).

SECTIONS A + B — the orb REDESIGN — are a SPECIFICATION that was never built.
    They describe an edgeless orb: never strokes, every fill a gradient object
    rather than a flat colour string, the full teal + violet + magenta brand
    spectrum, five layered arcs per frame, and a breath that both rises and
    falls.

    The shipped renderer does none of that. Its default style 'vibrant' sets
    rings:true, which routes through drawRing() to ctx.stroke(), so the orb
    legitimately draws stroked concentric rings today.

    This is NOT a regression, and the distinction cost real evidence to
    establish — the harness was run against three different renderers:

        today's renderer                          -> 10 failures
        pre-customization-hub (6a61f995^)         -> the SAME 10, so the
                                                     customization hub did not
                                                     break it
        the renderer at the harness's OWN         -> 11 failures, including
        introducing commit (47183e45, 2026-06-29)    "the off-brand purple is
                                                     gone", the very thing it
                                                     exists to prove

    So it has never passed, at any commit. It was committed red as a statement
    of intent and stayed invisible for five weeks because the pytest job was
    timing out — the release gate could not report it.

    Parked deliberately (steward's call, 2026-08-04) rather than deleted: the
    design intent is worth keeping, and rewriting the assertions to match
    today's ringed orb would destroy the record of what was wanted without
    anyone deciding to abandon it. Resolving it means either building the
    redesign — a visible change to the desktop centrepiece, so it goes through
    HOME_DESKTOP_DESIGN_CHECKLIST.md — or consciously dropping it. Task #37.

    strict=True on purpose: the day the redesign lands, this XPASSes and FAILS,
    which is the signal to delete the marker. A non-strict xfail would let a
    built redesign sit silently mislabelled as unbuilt — the same class of
    false-healthy signal this repo keeps finding.
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_stream_orb_brand_breathing.mjs')


def _run(section):
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the orb behavioural harness')
    r = subprocess.run([node, MJS, section], capture_output=True, text=True,
                       timeout=60)
    # Surface the per-assertion log so CI shows exactly which intent broke.
    assert r.returncode == 0, (
        f'orb harness section {section} failed:\n' + r.stdout + r.stderr)
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


def test_hero_floats_over_windows_without_trapping_focus():
    """Section C — implemented, and a real gate."""
    _run('C')


@pytest.mark.xfail(
    strict=True,
    reason="STREAM-orb redesign is SPECIFIED but never built — see this "
           "module's docstring. It has failed at every commit including its "
           "own (47183e45). If this XPASSes the redesign has landed: delete "
           "the marker. Task #37.")
def test_orb_is_edgeless_with_the_full_brand_spectrum():
    """Section A — the unbuilt redesign spec."""
    _run('A')


@pytest.mark.xfail(
    strict=True,
    reason="STREAM-orb breathing is SPECIFIED but never built — the shipped "
           "renderer does not produce the 5-arc frames this measures, so the "
           "core-radius range comes back empty. Task #37.")
def test_the_orb_breathes_and_energy_intensifies_it():
    """Section B — the unbuilt redesign spec."""
    _run('B')


if __name__ == '__main__':
    test_hero_floats_over_windows_without_trapping_focus()
    print('section C OK (A/B are the unbuilt redesign spec)')
