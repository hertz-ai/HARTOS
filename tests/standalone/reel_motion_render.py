"""Opt-in real render of Reel Studio's animated overlays with Remotion (A14).

Unit tests mock the Node / npm / browser boundary.  This script does the
real thing on a machine where Remotion may run, and is never collected by
pytest (no test_ prefix):

    set HEVOLVE_REEL_MOTION=1        (only once the Remotion license that
                                      applies to you is in place: NOTICE)
    python tests/standalone/reel_motion_render.py --out D:/scratch/motion
    python tests/standalone/reel_motion_render.py --out D:/scratch/motion --setup

1. Refuses unless HEVOLVE_REEL_MOTION is set (RuntimeToolManager's
   'reel_motion' row is off without it).
2. Remotion not set up: says so, or with --setup sets it up through the one
   npm installer the row uses (model_storage.install_npm_project, with the
   row's project, prepare script and free-disk check).  Running --setup is
   the operator's own yes on this machine, so it does not file a consent
   card; an agent's setup always goes through RuntimeToolManager.setup_tool
   and the card.  It downloads a few hundred MB into the models dir
   (HEVOLVE_MODEL_DIR honoured).
3. Makes a 6 s 1080x1920 test reel EDL from an ffmpeg test clip with a
   final look, asks for all four templates (with a test logo and end-card
   words), runs motion.compose.add_motion with the real runtime (Remotion
   renders ProRes 4444 clips with alpha), renders the reel (media.render),
   runs QA (media.qa), and checks: the step's note is motion:rendered, every
   hard QA check passes, and the frame inside each overlay's box while it
   plays differs from the same frame of a plain render.
4. Renders the animated title alone for LONG_HOOKS, in the title box the
   layout gives this reel and at layout.TITLE_MIN_H, and checks every
   frame: its letters are drawn, and nothing of it -- plate, letters or
   their shadow, at any alpha -- touches the box's edges (a cut row does).
   Review r3 of b0fa8c26e: a four-word Tamil hook lost its first and last
   rows, a six-word English one was clipped.  A frame after the words have
   settled is written over grey as title_<n>_<height>.png in the work
   folder, to look at.

Exit 0 when every check passes, 1 when one fails, 2 when it cannot run here.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: Hooks inside reel_edit's six words that the first real render cut.
LONG_HOOKS = ('மணப்பெண் அலங்காரம் இப்படித்தான் மாறியது',
              'Watch her bridal transformation unfold today')
#: When the title's frame to look at is taken (seconds): its words have
#: settled, its fade-out has not begun.
TITLE_SETTLED_S = 1.5


def _say(*parts):
    print(' '.join(str(p) for p in parts), flush=True)


def _setup(row, tool):
    from integrations.service_tools.runtime_manager import runtime_tool_manager as rtm
    started = time.monotonic()
    rtm.storage.install_npm_project(tool, row['npm_project'], prepare=row.get('npm_prepare'),
                                    min_free_bytes=int(row.get('npm_min_free_bytes') or 0))
    _say('setup finished in %.0f s into %s' % (time.monotonic() - started,
                                               rtm.storage.get_tool_dir(tool)))


def _fixtures():
    """tests/unit/reel_media_fixtures.py, loaded by its path.  Importing
    HARTOS puts agent-ledger-opensource, which has a ``tests`` package of
    its own, ahead of the repo root on sys.path (measured 2026-10-15: index
    0 against the root's 1), so ``from tests.unit.reel_media_fixtures``
    found the wrong ``tests`` and this script died before rendering."""
    import importlib.util
    path = os.path.join(ROOT, 'tests', 'unit', 'reel_media_fixtures.py')
    spec = importlib.util.spec_from_file_location('reel_media_fixtures', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _edl(work):
    from integrations.reel_studio import contracts as C
    from integrations.reel_studio.contracts import ClipCapture
    from integrations.reel_studio.media.probe import probe
    make_clip = _fixtures().make_clip
    src = make_clip(work, 'reel_src.mp4', size=(1080, 1920), fps=30, duration_s=7.0)
    x = 'a' * 64
    clip = probe(src).to_edl_clip(ClipCapture(clip=x, order=0, captured_at_ms=1760247082000,
                                              src='phone'))
    face = {'x': 0.35, 'y': 0.3, 'w': 0.3, 'h': 0.17}

    def seg(i0, i1, o0, o1, stage, role):
        return {'clip': x, 'in_s': i0, 'out_s': i1, 'speed': 1.0, 'out_start_s': o0,
                'out_end_s': o1, 'crop': {'cx': 0.5, 'cy': 0.5, 'zoom': 1.0}, 'face': dict(face),
                'transition': {'type': 'cut', 'dur_s': 0.0}, 'stage': stage, 'appeal': 7.0,
                'role': role}
    logo = os.path.join(work, 'logo.png')
    from core import media_bin
    media_bin.run_ffmpeg(['-y', '-loglevel', 'error', '-f', 'lavfi', '-i',
                          'color=c=0xC2185B:size=360x180', '-frames:v', '1', logo], timeout_s=60)
    return {'schema': C.EDL_SCHEMA, 'canvas': dict(C.CANVAS), 'target_s': 6.0,
            'duration_s': 6.0, 'audio': None, 'final_look_found': True, 'hook_first': False,
            'capture_src': 'phone', 'reveal': {'clip': x, 't_s': 5.0},
            'notes': [C.capture_note('phone')],
            'beats': {'source': 'synthetic', 'song_key': None, 'bpm': 120.0,
                      'times_s': [0.5 * k for k in range(0, 14)], 'confidence': 0.0},
            'clips': {x: clip},
            'segments': [seg(0.0, 3.0, 0.0, 3.0, 'bare_face', 'process'),
                         seg(3.5, 6.5, 3.0, 6.0, 'final_look', 'reveal')],
            'overlays': [
                {'type': 'text', 'text': 'Bridal glow', 'font': 'auto', 'size_px': 72,
                 'color': '#FFFFFF', 'box': True, 'x': 0.393519, 'y': 0.25, 'anchor': 'center',
                 'from_s': 0.0, 'to_s': 2.0},
                {'type': 'logo', 'src': logo, 'corner': 'top_left', 'width_px': 180,
                 'height_px': 90, 'margin_px': 48, 'opacity': 0.9, 'from_s': 0.0, 'to_s': None}]}


def _frame(path, t_s):
    import numpy as np
    from core import media_bin
    w, h = 270, 480
    stream = media_bin.ffmpeg_stream(
        ['-loglevel', 'error', '-ss', '%.3f' % t_s, '-i', path, '-frames:v', '1', '-vf',
         'scale=%d:%d' % (w, h), '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1'],
        frame_bytes=w * h * 3, timeout_s=60)
    try:
        return np.frombuffer(next(stream), np.uint8).reshape(h, w, 3).astype(int)
    finally:
        stream.close()


def _alphas(path, w, h):
    """The alpha of every frame of a clip, as (h, w) arrays."""
    import numpy as np
    from core import media_bin
    stream = media_bin.ffmpeg_stream(
        ['-loglevel', 'error', '-i', path, '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1'],
        frame_bytes=w * h * 4, timeout_s=120)
    try:
        for buf in stream:
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 4)[:, :, 3]
    finally:
        stream.close()


def _title_checks(work, edl):
    """Step 4 of the module note; the failures."""
    from integrations.reel_studio import contracts as C
    from integrations.reel_studio.motion import layout, runtime
    from core import media_bin
    fps = int(C.CANVAS['fps'])
    jobs = []
    for n, hook in enumerate(LONG_HOOKS):
        [item] = layout.plan_motion(edl, ['title'], hook_text=hook).items
        x0, y0, x1, y1 = item.box
        for height in sorted({int(y1 - y0), layout.TITLE_MIN_H}, reverse=True):
            props = dict(item.props, width=int(x1 - x0), height=height, fps=fps,
                         durationInFrames=int(round((item.to_s - item.from_s) * fps)))
            jobs.append({'id': layout.TEMPLATE_IDS['title'], 'props': props,
                         'out': os.path.join(work, 'title_%d_%d.mov' % (n, height))})
    runtime.render(jobs, os.path.join(work, 'titles'))
    failures = []
    for job in jobs:
        w, h = job['props']['width'], job['props']['height']
        media_bin.run_ffmpeg(
            ['-y', '-loglevel', 'error', '-ss', '%.3f' % TITLE_SETTLED_S, '-i', job['out'],
             '-filter_complex', 'color=c=0x505050:s=%dx%d[bg];[bg][0:v]overlay=format=auto'
             % (w, h), '-frames:v', '1', job['out'][:-4] + '.png'], timeout_s=60)
        name = '%r at %dx%d' % (job['props']['text'], w, h)
        letters, cut, margins = False, [], [h, h, w, w]
        for k, alpha in enumerate(_alphas(job['out'], w, h)):
            letters = letters or bool((alpha >= 250).any())     # opaque: the letters
            rows, cols = alpha.any(axis=1).nonzero()[0], alpha.any(axis=0).nonzero()[0]
            if not len(rows):
                continue
            seen = [rows.min(), h - 1 - rows.max(), cols.min(), w - 1 - cols.max()]
            margins = [min(m, s) for m, s in zip(margins, seen)]
            if 0 in seen:
                cut.append(k)
        _say('title %s: fewest clear px above, below, left, right in any frame: %s'
             % (name, ', '.join(str(int(m)) for m in margins)))
        if not letters:
            failures.append('the title %s drew no letters' % name)
        if cut:
            failures.append('the title %s touches its box edge (cut) in frames %s' % (name, cut))
    return failures


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--out', default=None, help='work folder (default: a temp folder)')
    parser.add_argument('--setup', action='store_true',
                        help='set Remotion up first if it is not (a few hundred MB)')
    args = parser.parse_args(argv)

    from integrations.reel_studio.motion import compose, runtime
    from integrations.service_tools.runtime_manager import TOOL_CONFIGS
    row = TOOL_CONFIGS[runtime.TOOL]
    if not runtime.enabled():
        _say('CANNOT RUN: %s is not set (Remotion is off until the owner confirms its license)'
             % row['enabled_by'])
        return 2
    if runtime.status() != runtime.READY:
        if not args.setup:
            _say('CANNOT RUN: Remotion is not set up here; run again with --setup')
            return 2
        _setup(row, runtime.TOOL)
        if runtime.status() != runtime.READY:
            _say('FAIL: the setup finished but Remotion is not ready')
            return 1

    from integrations.reel_studio import contracts as C
    from integrations.reel_studio.media import qa, render
    work = os.path.abspath(args.out or tempfile.mkdtemp(prefix='reel_motion_'))
    os.makedirs(work, exist_ok=True)
    edl = _edl(work)
    request = {'overlays': ['title', 'logo_sting', 'end_card', 'wipe'],
               'hook_text': 'Bridal glow',
               'card_text': ['Bridal makeup', 'Bridal from Rs 15k', 'DM for bookings']}
    started = time.monotonic()
    composed, notes = compose.add_motion(edl, request, workdir=work, progress=_say, wait_s=0)
    _say('motion step: %.1f s, notes %s' % (time.monotonic() - started, notes))
    failures = []
    if compose.NOTE_MOTION % compose.RENDERED not in notes:
        failures.append('the overlays were not rendered: %s' % notes)
    problems = C.validate_edl(composed)
    if problems:
        failures.append('the composed EDL does not validate: %s' % problems[:3])
    reel = os.path.join(work, 'reel_motion.mp4')
    plain = os.path.join(work, 'reel_plain.mp4')
    render.render_reel(composed, reel, workdir=os.path.join(work, 'r1'), encoder='libx264')
    render.render_reel(dict(edl, overlays=[]), plain, workdir=os.path.join(work, 'r2'),
                       encoder='libx264')
    report = qa.run_qa(reel, composed)
    # Every hard check (report['checks'] holds the hard rows only), duration
    # included: the test EDL's 6 s cut is its 6 s target (review of
    # 0b3441092: this left 'duration' out without saying why).
    hard_failed = [r['id'] for r in report['checks'] if not r['ok']]
    if hard_failed:
        failures.append('hard QA checks failed: %s' % hard_failed)
    fx, fy = 270 / 1080.0, 480 / 1920.0
    for ov in composed['overlays']:
        if ov['type'] != 'motion':
            continue
        t = (ov['from_s'] + ov['to_s']) / 2.0
        x0, y0, x1, y1 = ov['box']
        a = _frame(reel, t)[int(y0 * fy):int(y1 * fy), int(x0 * fx):int(x1 * fx)]
        b = _frame(plain, t)[int(y0 * fy):int(y1 * fy), int(x0 * fx):int(x1 * fx)]
        diff = float(abs(a - b).mean())
        _say('%s at %.2f s: mean difference %.1f inside its box' % (ov['template'], t, diff))
        if diff < 4.0:
            failures.append('%s left no visible mark in its box' % ov['template'])
    failures.extend(_title_checks(work, edl))
    _say('reel:', reel)
    for f in failures:
        _say('FAIL:', f)
    if not failures:
        _say('PASS: Remotion rendered %d overlays, composited with alpha, QA hard checks pass'
             % len([o for o in composed['overlays'] if o['type'] == 'motion']))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
