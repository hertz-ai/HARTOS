"""motion/project/fit.js: the animated title's size fits its box (amendment A14).

Review r3 of b0fa8c26e (REJECTED, templates.js:43): the Title template took
its font size from the box width and the character count only.  In a real
Remotion render, at the layout's 770x300 title box, the four-word Tamil hook
TAMIL_HOOK lost its first and last rows, and the six-word English hook
ENGLISH_HOOK filled the box with its plate clipped at the top and bottom; at
TITLE_MIN_H (180 px) its last word was cut.  The template now takes its size
from fit.js's fitTitle: the largest size, up to the template's own pick from
the text (titleCap, what it drew before), at which every row fits the
plate's width and the plate fits the box's height; the template draws
exactly the rows fitTitle laid out.

fit.js is plain CommonJS with no dependency, so these tests run the real
file under Node (core.node_bin, the one locator; skipped where there is no
Node, like this repo's other JS harnesses).  The word widths come from a
stand-in for the browser's measure, wider than the Windows font draws Tamil
(1.04 em a letter, measured in a real render on 2026-10-15).  The browser's
own measure and the drawn frames are checked by the opt-in real render,
tests/standalone/reel_motion_render.py.
"""
import json
import os
import subprocess

import pytest

from core.node_bin import find_node
from integrations.reel_studio.motion import layout

FIT_JS = os.path.join(os.path.dirname(layout.__file__), 'project', 'fit.js')

TAMIL_HOOK = 'மணப்பெண் அலங்காரம் இப்படித்தான் மாறியது'
ENGLISH_HOOK = 'Watch her bridal transformation unfold today'
TITLE_BOX_W = 770           # layout._title's width: the safe zone less SIDE_INSET_PX
BOXES = [(TITLE_BOX_W, layout.TITLE_H), (TITLE_BOX_W, layout.TITLE_MIN_H)]

# Runs fit.js on each job read from stdin; prints one JSON answer per job.
_DRIVER = r"""
const fit = require(process.argv[1]);
// A stand-in for the browser's measure, in ems a character: no width for a
// combining mark, a spacing vowel sign half an em, an Indic letter 1.1 em
// (wider than drawn), a capital 0.75, anything else 0.62.
const em = (ch) => /[\p{Mn}\p{Me}‌‍]/u.test(ch) ? 0
  : /\p{Mc}/u.test(ch) ? 0.55
  : /[ऀ-෿]/u.test(ch) ? 1.1
  : /\p{Lu}/u.test(ch) ? 0.75 : 0.62;
const measure = (word, size) => size * Array.from(word).reduce((n, ch) => n + em(ch), 0);
const rows = (lay) => lay.rows.map((r) => r.words);
const jobs = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = jobs.map(({words, width, height}) => {
  const box = {width, height};
  const got = fit.fitTitle(words, measure, box);
  const cap = fit.titleCap(words, width, height);
  const bigger = fit.layoutTitle(words, measure, got.geometry.size + 1, box);
  const atCap = fit.layoutTitle(words, measure, cap, box);
  return {size: got.geometry.size, rows: rows(got), height: got.height, widest: got.widest,
          inner: got.geometry.inner, fits: got.fits, cap,
          bigger_fits: bigger.fits, at_cap_fits: atCap.fits, at_cap_rows: rows(atCap),
          at_cap_height: atCap.height};
});
process.stdout.write(JSON.stringify({title: fit.TITLE, out}));
"""


def _fit(*jobs):
    """fit.js's fitTitle on each job: (words, box width, box height)."""
    node = find_node()
    if not node:
        pytest.skip('Node.js is not available to run fit.js')
    payload = [{'words': words, 'width': w, 'height': h} for words, w, h in jobs]
    run = subprocess.run([node, '-e', _DRIVER, FIT_JS], input=json.dumps(payload),
                         capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert run.returncode == 0, run.stderr
    answer = json.loads(run.stdout)
    return answer['title'], answer['out']


def _room(title, height):
    """The plate's tallest: the box's height less the margin kept above and below."""
    return height * (1 - 2 * title['margin'])


@pytest.mark.parametrize('hook', [TAMIL_HOOK, ENGLISH_HOOK], ids=['tamil', 'english'])
@pytest.mark.parametrize('box', BOXES, ids=lambda b: '%dx%d' % b)
def test_a_long_hook_fits_its_title_box(hook, box):
    words = hook.split()
    title, [got] = _fit((words, *box))
    width, height = box
    # The size the template drew before overflowed the box: that is the bug.
    assert not got['at_cap_fits'] and got['at_cap_height'] > _room(title, height), got
    # Now every word is there, in order, on rows that fit the plate's width,
    # and the plate fits the box's height.
    assert got['fits'] and [w for row in got['rows'] for w in row] == words, got
    assert got['height'] <= _room(title, height) and got['widest'] <= got['inner'], got
    assert got['inner'] <= round(width * title['maxWidth']), got
    # The largest such size: one pixel more does not fit.
    assert title['minSize'] <= got['size'] < got['cap'] and not got['bigger_fits'], got


def test_a_short_hook_keeps_the_size_it_had():
    """What fitted before is drawn as before: the template's own pick."""
    title, [got] = _fit((['Bridal', 'glow'], TITLE_BOX_W, layout.TITLE_H))
    assert got['size'] == got['cap'] == round(layout.TITLE_H * title['maxSize'])
    assert got['rows'] == [['Bridal', 'glow']] and got['fits'] and got['at_cap_fits']


def test_the_same_words_get_the_same_title_in_any_box_order():
    jobs = [(TAMIL_HOOK.split(), *box) for box in BOXES] + [(ENGLISH_HOOK.split(), *BOXES[0])]
    _title, first = _fit(*jobs)
    _title, again = _fit(*reversed(jobs))
    assert first == list(reversed(again))


def test_a_title_that_cannot_fit_stops_at_the_smallest_size():
    """Far past reel_edit's six words: the size never goes below
    TITLE.minSize, and no word is dropped."""
    words = ['word%02d' % i for i in range(60)]
    title, [got] = _fit((words, TITLE_BOX_W, layout.TITLE_MIN_H))
    assert got['size'] == title['minSize'] and not got['fits']
    assert [w for row in got['rows'] for w in row] == words


def test_no_words_lay_no_rows():
    _title, [got] = _fit(([], TITLE_BOX_W, layout.TITLE_H))
    assert got['rows'] == [] and got['height'] == 0 and got['fits']
