"""Agent browser — an agent works in its OWN Chrome profile.

Owner, 2026-11-27 (verbatim): "the llm shd be able to work on or use headless
and with head browser (no-GUI) and (with GUI) actions respectivelu".

Each mailbox has its own Chrome profile (driver.ensure_agent_browser: own
--user-data-dir, own debug port), so two agents' sign-ins never share a
cookie jar.  This script is the page-level half:

  open_page    go to a URL and read the page
  read_page    read the page that is open now
  click        click a button or link by its visible words (or a CSS selector)
  type_text    type into a field found by its label, placeholder or name; Enter
               after it when submit is set
  open_window  open the profile in a visible window: for the owner's one-time
               sign-in, and for steps that need the screen, which computer use
               (Computer_Action) then does in that window

open_page, read_page, click and type_text run headless unless a window is
already open, which they then use.  Every answer carries the page's address,
title, text and the words of its clickable items and fields, so the model can
choose its next step from what is really there.

The owner types passwords, one-time codes and card, PAN, GSTIN, Aadhaar and
bank details himself (session rule, carried in every brief).  type_text refuses
such a field, and refuses text holding a card number, and says to open the
window instead.  Clicking and typing act on this computer, so they need the
owner's permission for agents to control it (integrations.vlm.safety
.computer_control_block, the grant Computer_Action uses).
"""
import json
import logging
import re
import time
from typing import Optional

from .. import cdp, driver

logger = logging.getLogger('browser_research.scripts.agent_browser')

CONNECTION_MECHANISM = driver.AGENT_PROFILE_MECHANISM
TEXT_LIMIT = 12000
ITEM_LIMIT = 40
AFTER_ACTION_WAIT_S = 15.0

# Fields only the owner fills in: by input type, autocomplete hint, or the
# words of the field's label, name, id or placeholder, read as tokens (split
# on any non-letter and on camelCase: otp_code, otpInput, A/C No), never as
# loose substrings ("passwordless-email" and "Pincode" are ordinary fields).
_OWNER_ONLY_TYPES = frozenset({'password'})
_OWNER_ONLY_AUTOCOMPLETE = frozenset({
    'current-password', 'new-password', 'one-time-code', 'cc-number',
    'cc-csc', 'cc-exp', 'cc-exp-month', 'cc-exp-year'})
# A token that is a secret on its own.
_OWNER_ONLY_TOKENS = frozenset({
    'password', 'passwd', 'pwd', 'passcode', 'otp', 'cvv', 'cvv2', 'cvc',
    'mpin', 'gstin', 'ifsc', 'aadhaar', 'aadhar'})
# A token that is a secret only with one of these words right after it.
_OWNER_ONLY_PAIRS = {
    'card': {'no', 'number', 'num'},
    'cc': {'no', 'number', 'num', 'csc'},
    'pan': {'no', 'number', 'num', 'card'},
    'gst': {'no', 'number', 'num', 'id', 'in'},
    'account': {'no', 'number', 'num'},
    'security': {'code'},
    'verification': {'code'},
    'upi': {'pin'},
    'atm': {'pin'},
}
_OWNER_ONLY_SEQUENCES = (
    ('one', 'time', 'password'), ('one', 'time', 'code'), ('one', 'time', 'pin'),
    ('a', 'c', 'no'), ('a', 'c', 'number'))


class _Refused(Exception):
    """An action that must not go ahead; the message says what to do."""


# The page-side finder.  One function for both kinds: 'click' looks among
# links and buttons, 'field' among inputs.  Words are matched exactly, then
# as a part; a CSS selector is tried last.  The match is scrolled into view
# and remembered as window.__hartosTarget for the step that follows.
_FIND_JS = r"""
(function (target, kind) {
  const show = s => (s || '').replace(/\s+/g, ' ').trim();
  const norm = s => show(s).toLowerCase();
  const want = norm(target);
  const visible = el => {
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  // The words a field or button goes by, label first, each once whatever
  // its letter case ("To", not "to to to").
  const uniq = arr => { const seen = new Set(); return arr.map(show).filter(s => {
    const k = s.toLowerCase(); if (!s || seen.has(k)) return false; seen.add(k); return true; }); };
  const fieldParts = el => {
    const parts = [];
    if (el.labels) for (const l of el.labels) parts.push(l.innerText);
    const by = el.getAttribute('aria-labelledby');
    if (by) for (const id of by.split(/\s+/)) { const n = document.getElementById(id); if (n) parts.push(n.innerText); }
    parts.push(el.getAttribute('aria-label'), el.getAttribute('placeholder'),
               el.getAttribute('name'), el.id, el.getAttribute('title'));
    return uniq(parts);
  };
  const clickParts = el => uniq([el.getAttribute('aria-label') || el.innerText || el.value
                                 || el.getAttribute('title') || '']);
  const parts = kind === 'field' ? fieldParts : clickParts;
  const words = el => parts(el).join(' ');
  const sel = kind === 'field' ? %(field_sel)s : %(click_sel)s;
  const pool = [...document.querySelectorAll(sel)].filter(visible);
  let el = pool.find(e => parts(e).some(p => norm(p) === want))
        || pool.find(e => want && norm(words(e)).includes(want));
  if (!el) { try { const s = document.querySelector(target); if (s && visible(s)) el = s; } catch (e) {} }
  if (!el) {
    return {found: false, candidates: uniq(pool.map(words)).slice(0, %(limit)d)};
  }
  el.scrollIntoView({block: 'center', inline: 'center'});
  window.__hartosTarget = el;
  const r = el.getBoundingClientRect();
  return {found: true, x: r.left + r.width / 2, y: r.top + r.height / 2,
          words: words(el), tag: el.tagName.toLowerCase(),
          type: norm(el.getAttribute('type')), autocomplete: norm(el.getAttribute('autocomplete')),
          name: show([el.getAttribute('name'), el.id, el.getAttribute('placeholder')].join(' '))};
})(%(target)s, %(kind)s)
"""

_SNAPSHOT_JS = r"""
(function () {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const text = document.body ? document.body.innerText : '';
  const clicks = [...document.querySelectorAll(%(click_sel)s)]
    .filter(visible).map(e => norm(e.getAttribute('aria-label') || e.innerText || e.value)).filter(Boolean);
  const fields = [...document.querySelectorAll(%(field_sel)s)]
    .filter(visible).map(e => ({words: norm((e.labels && e.labels[0] && e.labels[0].innerText)
                                         || e.getAttribute('aria-label') || e.getAttribute('placeholder')
                                         || e.getAttribute('name') || e.id),
                                type: (e.getAttribute('type') || e.tagName).toLowerCase()}));
  return {url: location.href, title: document.title, text: text.slice(0, %(text)d),
          text_length: text.length,
          clickable: [...new Set(clicks)].slice(0, %(items)d), fields: fields.slice(0, %(items)d)};
})()
"""


# What counts as a button/link and as a field: ONE list each, used by both
# the finder and the page snapshot, so read_page never lists what click
# cannot find or hides what it can (rv_br2 on ecbb9d03d).
_CLICK_SELECTOR = ('a,button,input[type=submit],input[type=button],[role=button],'
                   '[role=link],[role=menuitem],[role=tab],[onclick],summary')
_FIELD_SELECTOR = ('input:not([type=hidden]),textarea,select,'
                   '[contenteditable=""],[contenteditable=true]')
_SELECTORS = {'click_sel': json.dumps(_CLICK_SELECTOR),
              'field_sel': json.dumps(_FIELD_SELECTOR)}


def _web_address(url: str) -> str:
    """`url` when it is a web address the agent browser may open (the one
    check, security.sanitize.validate_url: http(s), a host, no cloud
    metadata endpoint), else _Refused.  This is the script's own layer
    under dispatch's allowlist: a caller past dispatch still cannot open
    file:, chrome:, javascript: or ftp: pages (rv_br2 on ecbb9d03d read a
    local file through file://localhost/...)."""
    from security.sanitize import validate_url
    try:
        return validate_url(url)
    except ValueError as exc:
        raise _Refused(f'{url!r} is not a web address the agent browser may '
                       f'open ({exc}). Give it an http or https address.') from exc


def _page(mailbox: str, headless: bool = True) -> 'cdp.CDPPage':
    endpoint = driver.ensure_agent_browser(mailbox, headless=headless)
    return cdp.CDPPage.attach(endpoint)


def _snapshot(page, mailbox: str, **extra) -> dict:
    snap = page.evaluate(_SNAPSHOT_JS % dict(_SELECTORS, text=TEXT_LIMIT,
                                              items=ITEM_LIMIT)) or {}
    snap['truncated'] = snap.get('text_length', 0) > TEXT_LIMIT
    return dict({'success': True, 'mailbox': mailbox,
                 'connection_mechanism': CONNECTION_MECHANISM}, **snap, **extra)


def _failure(mailbox: str, exc: Exception) -> dict:
    logger.info('agent browser action for %s refused or failed: %s', mailbox, exc)
    return {'success': False, 'mailbox': mailbox,
            'connection_mechanism': CONNECTION_MECHANISM, 'error': str(exc)}


def _find(page, target: str, kind: str) -> dict:
    found = page.evaluate(_FIND_JS % dict(_SELECTORS, target=json.dumps(target),
                                          kind=json.dumps(kind),
                                          limit=ITEM_LIMIT)) or {}
    if not found.get('found'):
        what = 'button or link' if kind == 'click' else 'field'
        seen = '; '.join(found.get('candidates') or []) or 'none'
        raise _Refused(f'no {what} matching {target!r} on this page. '
                       f'Those it has: {seen}. Use one of those words, or read '
                       f'the page again.')
    return found


def _tokens(text: str) -> list:
    """The words of a field's label or name: camelCase and every non-letter
    split, lower-cased ("otpInput" -> otp input, "A/C No" -> a c no)."""
    spaced = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', str(text or ''))
    return [t for t in re.split(r'[^a-z0-9]+', spaced.lower()) if t]


def _secret_tokens(toks: list) -> bool:
    for i, tok in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if tok in _OWNER_ONLY_TOKENS:
            return True
        if nxt in _OWNER_ONLY_PAIRS.get(tok, ()):
            return True
        # A PIN is a secret; a "PIN code" is an Indian postal code.
        if tok == 'pin' and nxt != 'code':
            return True
        # "PAN" on its own or leading the field; not "Company (as on PAN)".
        if tok == 'pan' and i == 0:
            return True
    return any(tuple(toks[i:i + len(seq)]) == seq
               for seq in _OWNER_ONLY_SEQUENCES for i in range(len(toks)))


def _owner_only(found: dict) -> bool:
    return (found.get('type') in _OWNER_ONLY_TYPES
            or found.get('autocomplete') in _OWNER_ONLY_AUTOCOMPLETE
            or any(_secret_tokens(_tokens(found.get(key)))
                   for key in ('words', 'name')))


def _card_number_in(text: str) -> bool:
    from security.dlp_engine import DLPEngine
    return bool(DLPEngine(scan_types=['credit_card']).scan(text or ''))


def _control_refusal(agent_id) -> Optional[str]:
    from integrations.vlm.safety import computer_control_block
    return computer_control_block(agent_id)


def _mouse_click(page, x: float, y: float) -> None:
    for kind in ('mouseMoved', 'mousePressed', 'mouseReleased'):
        page.send('Input.dispatchMouseEvent', {
            'type': kind, 'x': x, 'y': y, 'button': 'left',
            'clickCount': 0 if kind == 'mouseMoved' else 1})


def _press_enter(page) -> None:
    for kind in ('keyDown', 'keyUp'):
        page.send('Input.dispatchKeyEvent', {
            'type': kind, 'key': 'Enter', 'code': 'Enter',
            'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13,
            **({'text': '\r'} if kind == 'keyDown' else {})})


def _settle(page) -> None:
    """Give a click or Enter its page change, if it starts one."""
    time.sleep(0.5)
    try:
        page.wait_loaded('the page after that step', AFTER_ACTION_WAIT_S)
    except cdp.CDPError as exc:
        logger.info('page still busy after an action: %s', exc)


def open_page(mailbox: str, url: str, headless: bool = True) -> dict:
    """Go to `url` in `mailbox`'s own browser and read the page."""
    try:
        url = _web_address(url)
        with _page(mailbox, headless) as page:
            page.navigate(url)
            return _snapshot(page, mailbox)
    except (_Refused, driver.AgentBrowserUnavailable, cdp.CDPError) as exc:
        return _failure(mailbox, exc)


def read_page(mailbox: str, headless: bool = True) -> dict:
    """Read the page that is open now in `mailbox`'s own browser."""
    try:
        with _page(mailbox, headless) as page:
            return _snapshot(page, mailbox)
    except (driver.AgentBrowserUnavailable, cdp.CDPError) as exc:
        return _failure(mailbox, exc)


def click(mailbox: str, target: str, agent_id=None, headless: bool = True) -> dict:
    """Click the button or link whose words (or CSS selector) are `target`."""
    try:
        refusal = _control_refusal(agent_id)
        if refusal:
            raise _Refused(refusal)
        with _page(mailbox, headless) as page:
            found = _find(page, target, 'click')
            _mouse_click(page, found['x'], found['y'])
            _settle(page)
            return _snapshot(page, mailbox, clicked=found.get('words') or target)
    except (_Refused, driver.AgentBrowserUnavailable, cdp.CDPError) as exc:
        return _failure(mailbox, exc)


def type_text(mailbox: str, field: str, text: str, submit: bool = False,
         agent_id=None, headless: bool = True) -> dict:
    """Type `text` into the field found by `field` (its label, placeholder,
    name or a CSS selector), replacing what it held; Enter after when
    `submit`."""
    try:
        if _card_number_in(text):
            raise _Refused('that text holds a card number. The owner types '
                           'card details himself: open the window (action '
                           '"open_window") and ask him to.')
        refusal = _control_refusal(agent_id)
        if refusal:
            raise _Refused(refusal)
        with _page(mailbox, headless) as page:
            found = _find(page, field, 'field')
            if _owner_only(found):
                raise _Refused(
                    f'{field!r} is a field the owner fills in himself (a '
                    f'password, one-time code, card, PAN, GSTIN, Aadhaar or '
                    f'bank detail). Open the window (action "open_window") and ask '
                    f'him to sign in or enter it there.')
            _mouse_click(page, found['x'], found['y'])
            page.evaluate('(function(){const e=window.__hartosTarget; if(!e) return;'
                          ' e.focus(); if (e.select) e.select();'
                          ' else document.execCommand("selectAll");})()')
            page.send('Input.insertText', {'text': text})
            if submit:
                _press_enter(page)
                _settle(page)
            return _snapshot(page, mailbox, typed_into=found.get('words') or field)
    except (_Refused, driver.AgentBrowserUnavailable, cdp.CDPError) as exc:
        return _failure(mailbox, exc)


def open_window(mailbox: str, url: Optional[str] = None) -> dict:
    """Open `mailbox`'s own profile in a visible window (at `url`)."""
    try:
        if url:
            url = _web_address(url)
        endpoint = driver.ensure_agent_browser(mailbox, headless=False, url=url)
        if url:
            with cdp.CDPPage.attach(endpoint) as page:
                if not str(page.evaluate('location.href') or '').startswith(url):
                    page.navigate(url)
        return {'success': True, 'mailbox': mailbox,
                'connection_mechanism': CONNECTION_MECHANISM,
                'endpoint': endpoint,
                'message': ('A window is open on this mailbox\'s own browser. '
                            'The owner signs in there himself. For steps that '
                            'need the screen, use Computer_Action on that '
                            'window; page steps (read, click, type) keep '
                            'working in it.')}
    except (_Refused, driver.AgentBrowserUnavailable, cdp.CDPError) as exc:
        return _failure(mailbox, exc)
