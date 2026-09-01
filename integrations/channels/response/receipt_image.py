"""Branded receipt PNG renderer — leg A of the billing-receipt agent.

Turns the fields `generate_receipt` collected into a receipt IMAGE with the
business logo composited, for delivery over the same channel the request
arrived on (WhatsApp etc., via MediaAttachment).

Deliberately mirrors integrations/social/og_image.py — the one existing PIL
composition in the tree (optional Pillow import, font ladder, wrapped text,
brand footer) — rather than introducing a second image stack or a PDF
library (cx_Freeze bundling cost; PNG ships everywhere WhatsApp does).

Deterministic-vs-LLM split (the receipt doc's rule): the MODEL extracts the
fields; THIS code computes balance = total − advance, formats currency,
numbers the receipt, and draws the layout. Money math never rides an LLM.
"""
import logging
import os
import re
import textwrap
import time
from typing import Dict, Optional

logger = logging.getLogger('tool_execution')

# Canvas: portrait receipt, comfortable for phone screens.
WIDTH = 800
PAD = 48
LOGO_MAX = (180, 180)

# Palette: paper-white receipt, ink text, one accent (matches the product's
# brand accent used by og_image so receipts look family, not foreign).
BG = (250, 250, 248)
INK = (24, 24, 28)
MUTED = (110, 110, 120)
ACCENT = (108, 99, 255)
RULE = (210, 210, 215)


# FreeType handles are process-global native state; re-creating them on
# every render churned handles and crashed with an access violation on the
# SECOND render in one process (Windows, PIL ImageFont.getlength — caught
# by test_receipt_image on 2026-09-01). Load each (size, bold) exactly
# once for the process lifetime; fonts are immutable so sharing is safe.
_FONT_CACHE = {}


def _font_cached(size, bold=False):
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    from PIL import ImageFont
    candidates = [
        'C:/Windows/Fonts/segoeuib.ttf' if bold else 'C:/Windows/Fonts/segoeui.ttf',
        'C:/Windows/Fonts/arialbd.ttf' if bold else 'C:/Windows/Fonts/arial.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold
        else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/System/Library/Fonts/Helvetica.ttc',
    ]
    fnt = None
    for fp in candidates:
        if os.path.exists(fp):
            try:
                fnt = ImageFont.truetype(fp, size)
                break
            except Exception:
                continue
    if fnt is None:
        fnt = ImageFont.load_default()
    _FONT_CACHE[key] = fnt
    return fnt


def _receipts_dir() -> str:
    try:
        from core.platform_paths import get_data_dir
        base = os.path.join(get_data_dir(), 'receipts')
    except ImportError:
        base = os.path.join(os.path.expanduser('~/Documents/Nunba/data'),
                            'receipts')
    os.makedirs(base, exist_ok=True)
    return base


def parse_amount(value) -> Optional[float]:
    """'₹18,000' / '18k' / '5000' → float, or None when unparseable.

    The LLM hands us user-typed money; this is the ONE place it becomes a
    number. 'k' shorthand is common in the owner's market ('18k total,
    5k advance').
    """
    if value is None:
        return None
    s = str(value).strip().lower().replace(',', '')
    s = re.sub(r'[^\d.k]', '', s)
    if not s:
        return None
    mult = 1000.0 if s.endswith('k') else 1.0
    s = s.rstrip('k')
    try:
        return float(s) * mult
    except ValueError:
        return None


def compute_balance(amount, advance) -> Optional[str]:
    """Balance = total − advance, formatted; None when either is unparseable."""
    total = parse_amount(amount)
    adv = parse_amount(advance)
    if total is None or adv is None:
        return None
    bal = total - adv
    return f'{bal:,.0f}' if bal == int(bal) else f'{bal:,.2f}'


def render_receipt_png(fields: Dict[str, str],
                       logo_path: Optional[str] = None,
                       out_dir: Optional[str] = None) -> Optional[str]:
    """Render the receipt PNG. Returns the file path, or None without Pillow.

    fields keys (all strings, missing → blank line skipped):
      business_name, client_name, service, amount, currency, date,
      event_timing, advance, balance, notes, receipt_no
    """
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
    except ImportError:
        logger.debug('Pillow not installed — receipt stays text-only')
        return None

    f = {k: str(v or '').strip() for k, v in (fields or {}).items()}
    currency = f.get('currency') or 'INR'
    receipt_no = f.get('receipt_no') or time.strftime('R%Y%m%d-%H%M%S')

    def font(size, bold=False):
        return _font_cached(size, bold)

    f_title = font(40, bold=True)
    f_head = font(26, bold=True)
    f_body = font(24)
    f_small = font(18)

    # Measure-then-draw: rows we will actually paint.
    rows = []
    for label, key in (('Received from', 'client_name'),
                       ('For', 'service'),
                       ('Date', 'date'),
                       ('Event timing', 'event_timing')):
        if f.get(key):
            rows.append((label, f[key]))

    money = []
    if f.get('amount'):
        money.append(('Total', f'{currency} {f["amount"]}'))
    if f.get('advance'):
        money.append(('Advance received', f'{currency} {f["advance"]}'))
    if f.get('balance'):
        money.append(('Balance due', f'{currency} {f["balance"]}'))

    note_lines = textwrap.wrap(f.get('notes', ''), width=52)[:4]

    logo_img = None
    if logo_path and os.path.isfile(logo_path):
        try:
            from PIL import Image
            logo_img = Image.open(logo_path).convert('RGBA')
            logo_img.thumbnail(LOGO_MAX)
        except Exception as e:
            logger.warning('receipt logo unreadable (%s): %s', logo_path, e)
            logo_img = None

    height = (PAD + (logo_img.height if logo_img else 0) + 24
              + 56              # business name
              + 64              # RECEIPT bar
              + 40 * len(rows) + 24
              + (44 * len(money) + 28 if money else 0)
              + (30 * len(note_lines) + 20 if note_lines else 0)
              + 90)             # footer
    height = max(height, 560)

    from PIL import Image, ImageDraw
    img = Image.new('RGB', (WIDTH, height), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (WIDTH, 8)], fill=ACCENT)

    y = PAD
    if logo_img is not None:
        img.paste(logo_img, ((WIDTH - logo_img.width) // 2, y), logo_img)
        y += logo_img.height + 24

    if f.get('business_name'):
        w = draw.textlength(f['business_name'], font=f_head)
        draw.text(((WIDTH - w) / 2, y), f['business_name'], fill=INK, font=f_head)
        y += 44
    w = draw.textlength('RECEIPT', font=f_title)
    draw.text(((WIDTH - w) / 2, y), 'RECEIPT', fill=ACCENT, font=f_title)
    y += 52
    w = draw.textlength(receipt_no, font=f_small)
    draw.text(((WIDTH - w) / 2, y), receipt_no, fill=MUTED, font=f_small)
    y += 34
    draw.line([(PAD, y), (WIDTH - PAD, y)], fill=RULE, width=2)
    y += 20

    for label, value in rows:
        draw.text((PAD, y), label, fill=MUTED, font=f_body)
        w = draw.textlength(value, font=f_body)
        draw.text((WIDTH - PAD - w, y), value, fill=INK, font=f_body)
        y += 40
    y += 8

    if money:
        draw.line([(PAD, y), (WIDTH - PAD, y)], fill=RULE, width=2)
        y += 16
        for label, value in money:
            bold = label == 'Balance due'
            fnt = f_head if bold else f_body
            draw.text((PAD, y), label, fill=(INK if bold else MUTED), font=fnt)
            w = draw.textlength(value, font=fnt)
            draw.text((WIDTH - PAD - w, y), value,
                      fill=(ACCENT if bold else INK), font=fnt)
            y += 44
        y += 8

    if note_lines:
        for line in note_lines:
            draw.text((PAD, y), line, fill=MUTED, font=f_small)
            y += 30
        y += 8

    draw.rectangle([(0, height - 56), (WIDTH, height)], fill=(24, 24, 32))
    draw.text((PAD, height - 42), 'Thank you for your business',
              fill=(200, 200, 210), font=f_small)

    out_dir = out_dir or _receipts_dir()
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f'receipt_{receipt_no}.png')
    img.save(out_path, 'PNG', optimize=True)
    return out_path
