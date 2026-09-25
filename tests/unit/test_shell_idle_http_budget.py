"""The shell's idle HTTP budget, enforced over the scripts the shell actually serves.

MEASURED on the box 2026-09-22: ~18 GETs per 5 s at idle. Four pollers in every
shell document (system/metrics 4 s, ai-sensing 4 s, connectivity/summary 8 s,
dashboard/agents 5 s) for state the server keeps fresh itself. The program's
N2 line is "idle shell HTTP < 1 GET per 10 s per document"; the server now
pushes that state on the SSE stream and what remains of the polls is a slow
fallback that runs only while the stream is down.

This test is the guard that keeps it that way. It walks every script the
rendered shell loads (the inline blocks, every /shell/static include, and the
context menu hartDesktop.js injects at runtime), finds every setInterval, works
out whether the interval's closure fetches (directly or through the functions
it calls in the same file) and what cadence it asked for, and requires:

  * every fetching interval runs at FALLBACK_MIN_MS or slower AND is gated on
    the stream being down (its callback names sseUp);
  * the host document has at most MAX_POLLERS_PER_DOCUMENT fetching intervals;
  * no interval at all runs faster than DOM_TICK_MIN_MS (the DOM-only tickers:
    the clocks, the visibility engine, the orb state).

It is a source-shape guard, deliberately, and it is not the only test: the
behaviour (zero GETs per tick with the stream up, the cadence each module
asked for, what a push paints) is driven for real in test_shell_poll_diet.mjs.
A grep could not see through `setInterval(refresh, 8000)` to the fetch inside
refresh(); this resolves it, so a poller cannot come back by naming its
callback differently.

    python -m pytest -q -p no:cacheprovider tests/unit/test_shell_idle_http_budget.py
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATIC = ROOT / 'integrations' / 'agent_engine' / 'static'

#: The N2 budget: < 1 GET per 10 s per document at idle. With four fallback
#: pollers per document, 30 s each is 4 GETs per 30 s, and they run only while
#: the stream is down; with the stream up the idle figure is zero.
FALLBACK_MIN_MS = 30000
#: DOM-only tickers may run faster (no HTTP), but nothing runs sub-200 ms: the
#: orb state mirror is 200 ms, the visibility engine 250 ms.
DOM_TICK_MIN_MS = 200
#: metrics, senses, connectivity, agents. A fifth needs a reason here.
MAX_POLLERS_PER_DOCUMENT = 4

#: Fetching intervals that are NOT idle traffic: armed by an explicit user
#: operation and cleared when it ends. Each entry names why, the way the motion
#: gate's keep-list does, so "it was already there" can never be the reason.
_KEEP = {
    ('hartFlash.js', 1200): (
        'flash-to-USB progress: armed by startPolling() when the user starts '
        'a flash, cleared by stopPolling() on done/error; never runs at idle'),
}

# ── a small JS surface reader (strings and comments blanked, braces balanced) ──

_KEYWORDS = {'if', 'for', 'while', 'switch', 'catch', 'function', 'return',
             'typeof', 'new', 'setInterval', 'setTimeout', 'clearInterval',
             'clearTimeout', 'requestAnimationFrame'}


def _strip(js: str) -> str:
    """Blank string/regex/comment contents with spaces (indices preserved)."""
    out = list(js)
    i, n = 0, len(js)
    prev_sig = ''   # last significant char, to tell a regex from division

    def blank(a, b):
        for k in range(a, b):
            if out[k] not in '\n':
                out[k] = ' '
    while i < n:
        c = js[i]
        if c == '/' and i + 1 < n and js[i + 1] == '/':
            j = js.find('\n', i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
            continue
        if c == '/' and i + 1 < n and js[i + 1] == '*':
            j = js.find('*/', i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
            continue
        if c in '\'"`':
            j = i + 1
            while j < n and js[j] != c:
                if js[j] == '\\':
                    j += 1
                if js[j] == '\n' and c != '`':
                    break
                j += 1
            blank(i + 1, min(j, n))
            i = j + 1
            continue
        if c == '/' and prev_sig in '(,=:[!&|?{};+-*%<>~^' or (c == '/' and prev_sig == ''):
            j = i + 1
            while j < n and js[j] != '/' and js[j] != '\n':
                if js[j] == '\\':
                    j += 1
                elif js[j] == '[':
                    k = js.find(']', j)
                    j = k if k > 0 else j
                j += 1
            blank(i + 1, min(j, n))
            i = j + 1
            prev_sig = ')'
            continue
        if not c.isspace():
            prev_sig = c
        i += 1
    return ''.join(out)


def _balanced(src: str, i: int, open_ch='{', close_ch='}') -> int:
    """Index just past the bracket that closes the one at src[i]."""
    assert src[i] == open_ch, (src[i], i)
    depth = 0
    for k in range(i, len(src)):
        if src[k] == open_ch:
            depth += 1
        elif src[k] == close_ch:
            depth -= 1
            if depth == 0:
                return k + 1
    raise AssertionError('unbalanced %s at %d' % (open_ch, i))


def _split_top_level(args: str):
    parts, depth, cur = [], 0, []
    for ch in args:
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
        if ch == ',' and depth == 0:
            parts.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append(''.join(cur))
    return [p.strip() for p in parts]


def _function_body(src: str, name: str):
    m = re.search(r'\bfunction\s+' + re.escape(name) + r'\s*\(', src)
    if not m:
        m = re.search(r'\b' + re.escape(name) + r'\s*=\s*function\s*\(', src)
    if not m:
        return None
    b = src.find('{', m.end())
    return src[b:_balanced(src, b)]


def _callback_body(src: str, expr: str):
    """The body of an interval callback: an inline function, an arrow, or a
    named function defined in the same file."""
    e = expr.strip()
    if e.startswith('function') or '=>' in e.split('{')[0]:
        b = e.find('{')
        return e[b:_balanced(e, b)] if b >= 0 else e
    if re.fullmatch(r'[A-Za-z_$][\w$]*', e):
        return _function_body(src, e)
    return None


def _resolve_ms(expr: str, src: str):
    e = expr.strip()
    if re.fullmatch(r'\d+', e):
        return int(e)
    m = re.fullmatch(r'Math\.max\((.*)\)', e)
    if m:
        vals = [_resolve_ms(p, src) for p in _split_top_level(m.group(1))]
        return max(v for v in vals if v is not None) if any(v is not None for v in vals) else None
    m = re.fullmatch(r'[A-Za-z_$][\w$]*\.([A-Za-z_$][\w$]*)', e)
    if m:
        d = re.search(r'\b' + re.escape(m.group(1)) + r'\s*:\s*(\d+)', src)
        return int(d.group(1)) if d else None
    if re.fullmatch(r'[A-Za-z_$][\w$]*', e):
        d = re.search(r'\b(?:var|let|const)\s+' + re.escape(e) + r'\s*=\s*(\d+)', src)
        return int(d.group(1)) if d else None
    return None


def _fetches(body: str, src: str, seen=None, depth=0) -> bool:
    """Does this closure reach fetch(), directly or through same-file calls?"""
    if body is None:
        return False
    if re.search(r'\bfetch\s*\(', body):
        return True
    if depth > 5:
        return False
    seen = seen if seen is not None else set()
    for name in set(re.findall(r'\b([A-Za-z_$][\w$]*)\s*\(', body)):
        if name in _KEYWORDS or name in seen:
            continue
        seen.add(name)
        if _fetches(_function_body(src, name), src, seen, depth + 1):
            return True
    return False


def _intervals(src: str):
    out = []
    for m in re.finditer(r'\bsetInterval\s*\(', src):
        end = _balanced(src, m.end() - 1, '(', ')')
        args = _split_top_level(src[m.end():end - 1])
        if len(args) < 2:
            continue
        out.append({
            'at': m.start(),
            'callback': _callback_body(src, args[0]),
            'ms': _resolve_ms(args[1], src),
            'ms_expr': args[1],
        })
    return out


# ── the documents the shell serves ────────────────────────────────────────────

@pytest.fixture(scope='module')
def served():
    """{name: stripped source} for every script the rendered shell runs."""
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    html = LiquidUIService().render_desktop_shell()
    docs = {}
    inline = '\n'.join(re.findall(r'<script>(.*?)</script>', html, re.S))
    docs['<inline shell script>'] = _strip(inline)
    names = re.findall(r'<script[^>]*src="/shell/static/([^"]+\.js)"', html)
    names.append('hartContextMenu.js')   # injected at runtime by hartDesktop.js
    for name in names:
        if name.endswith('.min.js'):
            continue   # vendor (lottie): no HTTP of its own, not our source
        docs[name] = _strip((STATIC / name).read_text(encoding='utf-8'))
    return docs


def _all_intervals(served):
    for name, src in served.items():
        for iv in _intervals(src):
            iv['file'] = name
            yield iv


def _is_kept(iv):
    return (iv['file'], iv['ms']) in _KEEP


def test_every_interval_cadence_is_explicit(served):
    for iv in _all_intervals(served):
        assert iv['ms'] is not None, (
            '%s: setInterval cadence %r could not be resolved; make it a literal '
            'or a named constant so the budget can read it' % (iv['file'], iv['ms_expr']))


def test_no_fetching_interval_under_the_fallback_floor(served):
    for iv in _all_intervals(served):
        if not _fetches(iv['callback'], served[iv['file']]) or _is_kept(iv):
            continue
        assert iv['ms'] >= FALLBACK_MIN_MS, (
            '%s: a poller at %d ms. Idle state is PUSHED on the SSE stream; a '
            'poll here is only the fallback for a stream that is down and runs '
            'at %d ms or slower' % (iv['file'], iv['ms'], FALLBACK_MIN_MS))
        assert 'sseUp' in (iv['callback'] or ''), (
            '%s: the %d ms poller is not gated on the stream being down '
            '(its callback must check sseUp())' % (iv['file'], iv['ms']))


def test_host_document_has_at_most_four_pollers(served):
    pollers = [iv for iv in _all_intervals(served)
               if _fetches(iv['callback'], served[iv['file']]) and not _is_kept(iv)]
    where = sorted('%s @ %s ms' % (iv['file'], iv['ms']) for iv in pollers)
    assert len(pollers) <= MAX_POLLERS_PER_DOCUMENT, (
        'the host document now has %d fallback pollers: %s. A fifth kind of '
        'state should be pushed, not polled' % (len(pollers), where))
    assert len(pollers) == MAX_POLLERS_PER_DOCUMENT, (
        'expected the four fallback pollers (metrics, senses, connectivity, '
        'agents), found %s' % where)


def test_no_ticker_faster_than_the_dom_floor(served):
    for iv in _all_intervals(served):
        assert iv['ms'] >= DOM_TICK_MIN_MS, (
            '%s: a %d ms interval; nothing in the shell runs faster than %d ms'
            % (iv['file'], iv['ms'], DOM_TICK_MIN_MS))


def test_keep_list_names_real_intervals(served):
    found = {(iv['file'], iv['ms']) for iv in _all_intervals(served)}
    for key, why in _KEEP.items():
        assert why.strip(), 'a keep-list entry must say why'
        assert key in found, 'stale keep-list entry %r: no such interval is served' % (key,)


def test_the_reader_sees_through_a_named_callback():
    """Mutation check on the guard itself: `setInterval(refresh, 8000)` with
    the fetch inside refresh() is exactly the shape the old pollers had."""
    src = _strip("function refresh(){ fetch('/x'); }\nsetInterval(refresh, 8000);\n"
                 "setInterval(function(){ tick(); }, 1000);\nfunction tick(){ el.textContent = 'a /* not a comment */'; }")
    ivs = _intervals(src)
    assert [iv['ms'] for iv in ivs] == [8000, 1000]
    assert _fetches(ivs[0]['callback'], src) is True
    assert _fetches(ivs[1]['callback'], src) is False
