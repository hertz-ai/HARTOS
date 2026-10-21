// The animated title's size and rows (templates.js's Title).  HARTOS,
// Apache-2.0; written for HARTOS, no third-party code.
//
// No dependency, so a test runs this file under plain Node
// (tests/unit/test_reel_motion_title_fit.py).  The template hands in the
// measure: the drawn width of a word at a size, from the browser's own
// text machinery.
//
// The title's words sit on a plate in rows.  fitTitle picks the largest
// font size, up to the template's pick from the text alone (titleCap, the
// size it drew before), at which every row fits the plate's width and the
// plate -- its rows, the gaps between them, its padding and the accent rule
// under it -- fits the box's height with TITLE.margin to spare above and
// below, and lays the rows out; the template draws exactly those rows, and
// its plate clips what it holds (the letters' shadow, a word springing in),
// so nothing of the title is drawn outside the plate.
// Review r3 of b0fa8c26e: the size came from the box's width and the
// character count only, so in a real render a four-word Tamil hook wrapped
// into more rows than the layout's 300 px box holds and lost its first and
// last rows, and a six-word English hook's plate was clipped at the top and
// bottom (at the layout's 180 px floor its last word was cut).
// Deterministic: the same words, measure and box give the same title.

const TITLE = Object.freeze({
  weight: 800,        // font weight
  lineHeight: 1.12,   // a row's height, in ems
  rowGap: 0.08,       // between rows, in ems
  columnGap: 0.28,    // between words, in ems
  padY: 0.22,         // plate padding above and below the rows, in ems
  padX: 0.4,          // plate padding left and right, in ems
  rule: 0.06,         // the accent rule under the plate, in ems,
  ruleMin: 3,         // and never thinner than this (px)
  radius: 0.35,       // the plate's corners, in ems
  maxWidth: 0.94,     // the plate's widest, a share of the box's width
  maxSize: 0.3,       // the largest size, a share of the box's height
  margin: 0.02,       // kept clear above and below the plate, a share of the box's height
  minSize: 24,        // the smallest size tried (px)
});

/** The plate's measures (px) at font size ``size`` in a box ``width`` wide. */
function titleGeometry(size, width) {
  const padX = Math.round(size * TITLE.padX);
  const maxWidth = Math.round(width * TITLE.maxWidth);
  return {
    size,
    lineHeight: size * TITLE.lineHeight,
    rowGap: Math.round(size * TITLE.rowGap),
    columnGap: Math.round(size * TITLE.columnGap),
    padY: Math.round(size * TITLE.padY),
    padX,
    rule: Math.max(TITLE.ruleMin, Math.round(size * TITLE.rule)),
    radius: Math.round(size * TITLE.radius),
    maxWidth,
    inner: maxWidth - 2 * padX,   // the widest a row may be
  };
}

/** The template's size from the text alone (what it drew before the fit):
 *  the box height's maxSize share, smaller for more characters (counted up
 *  to 22) or a longer word. */
function titleCap(words, width, height) {
  const chars = Math.max(6, Math.min(words.join(' ').length, 22));
  const longest = words.reduce((n, w) => Math.max(n, w.length), 1);
  return Math.round(Math.min(height * TITLE.maxSize,
                             (width * 0.86) / (Math.max(chars / 1.6, longest) * 0.58)));
}

/** ``words`` laid in rows at ``size`` in ``box`` ({width, height}): each row
 *  as many words, in order, as fit the plate's inner width; ``measure(word,
 *  size)`` is a word's drawn width (rounded up, plus a pixel).  ``height`` is
 *  the plate's, ``widest`` its widest row; ``fits``: the rows fit the plate
 *  and the plate fits the box less TITLE.margin above and below. */
function layoutTitle(words, measure, size, box) {
  const geometry = titleGeometry(size, box.width);
  const rows = [];
  let row = null;
  for (const word of words) {
    const w = Math.ceil(measure(word, size)) + 1;
    if (row && row.width + geometry.columnGap + w <= geometry.inner) {
      row.words.push(word);
      row.width += geometry.columnGap + w;
    } else {
      row = {words: [word], width: w};
      rows.push(row);
    }
  }
  const n = rows.length;
  const height = n ? n * geometry.lineHeight + (n - 1) * geometry.rowGap
    + 2 * geometry.padY + geometry.rule : 0;
  const widest = rows.reduce((m, r) => Math.max(m, r.width), 0);
  const room = box.height * (1 - 2 * TITLE.margin);
  return {geometry, rows, height, widest, fits: widest <= geometry.inner && height <= room};
}

/** The title at the largest size from titleCap down to TITLE.minSize that
 *  fits ``box`` (layoutTitle); at TITLE.minSize when none does (far past
 *  the six words reel_edit allows), so no word is ever dropped. */
function fitTitle(words, measure, box) {
  const cap = titleCap(words, box.width, box.height);
  const floor = Math.min(cap, TITLE.minSize);
  let laid = null;
  for (let size = cap; size >= floor; size -= 1) {
    laid = layoutTitle(words, measure, size, box);
    if (laid.fits) {
      break;
    }
  }
  return laid;
}

module.exports = {TITLE, titleGeometry, titleCap, layoutTitle, fitTitle};
