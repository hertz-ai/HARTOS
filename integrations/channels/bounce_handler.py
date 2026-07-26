"""Read bounces and unsubscribe replies back out of the mailbox.

The gap this closes. MX validation, which runs before a campaign, proves a
DOMAIN can receive mail. It says nothing about whether a particular mailbox
exists. On a list of historical addresses the majority of dead entries are
dead mailboxes at perfectly live domains -- an abandoned Hotmail account is
invisible to every pre-send check there is, and announces itself only by
bouncing.

That matters more than it sounds. Receiving systems judge a sender largely
on bounce rate, and a few percent sustained is enough to get a domain
filtered or blocklisted. A sender that keeps mailing addresses which already
hard-bounced is producing exactly the signal that gets it blocked, and doing
so repeatedly. Sending without reading bounces is the actual risk, not the
volume.

What this does:

  * connects to our own mailbox over IMAP
  * finds delivery status notifications (RFC 3464) and the informal bounces
    from servers that do not send proper ones
  * pulls the failed recipient and status code out of them
  * suppresses 5.x.x (permanent) so those addresses are never mailed again
  * leaves 4.x.x (temporary) alone -- a full mailbox or a greylist is not a
    reason to drop somebody forever
  * records replies containing 'unsubscribe' as opt-outs

Conservative on purpose. An address is only suppressed when a permanent
failure can actually be parsed out of the message; anything ambiguous is
reported for a human to look at rather than acted on. Wrongly suppressing a
real person is a silent, permanent loss, and it is the error that would
never be noticed.
"""
from __future__ import annotations

import email
import imaplib
import logging
import os
import re
from email.message import Message
from typing import Dict, List, Optional, Tuple

from integrations.channels.email_campaign import (
    SMTP_PASS, SMTP_USER, load_bounced, record_bounce, record_optout)

logger = logging.getLogger('hevolve_channels')

IMAP_HOST = os.environ.get('HEVOLVE_IMAP_HOST', 'mail.hertzai.com')
IMAP_PORT = int(os.environ.get('HEVOLVE_IMAP_PORT', '993'))

# Status codes as they appear in a delivery-status part.
_STATUS = re.compile(r'^\s*Status:\s*([245])\.(\d+)\.(\d+)', re.I | re.M)
_FINAL_RCPT = re.compile(
    r'^\s*(?:Final|Original)-Recipient:\s*(?:rfc822;)?\s*([^\s<>]+@[^\s<>]+)',
    re.I | re.M)
# Fallback for servers that do not send a machine-readable report.
_INLINE_FAIL = re.compile(
    r'(?:^|\s)<?([A-Za-z0-9._%+\-]+@[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,})>?'
    r'[^\n]{0,80}?(?:user unknown|does not exist|no such user|'
    r'mailbox unavailable|recipient rejected|address rejected|'
    r'account has been disabled|not found)', re.I)

BOUNCE_SENDERS = ('mailer-daemon', 'postmaster', 'mail delivery', 'mailer_daemon')


def _walk_text(msg: Message):
    """Every text-ish part of a message, decoded defensively.

    Bounce reports are frequently malformed -- wrong charsets, missing
    boundaries, base64 that does not decode -- and a parser that raises on
    the first bad one processes nothing.
    """
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_content_maintype() == 'multipart':
            continue

        # message/delivery-status is the part that actually carries
        # Final-Recipient and Status, and it is NOT text: its payload is a
        # list of header-only Message objects. get_payload(decode=True)
        # returns None for it, and str() of the list gives a Python repr
        # rather than the headers. Reconstructing them explicitly is the
        # difference between parsing a bounce and filing it as unreadable --
        # 262 of 275 real bounces were being missed this way.
        if ctype == 'message/delivery-status':
            try:
                for sub in part.get_payload():
                    if isinstance(sub, Message):
                        yield '\n'.join('%s: %s' % (k, v)
                                        for k, v in sub.items())
            except Exception:
                pass
            continue

        if not (ctype.startswith('text/') or ctype.startswith('message/')):
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                raw = part.get_payload()
                if isinstance(raw, list):
                    payload = '\n'.join(
                        s.as_string() for s in raw if isinstance(s, Message))
                else:
                    payload = str(raw)
            else:
                payload = payload.decode(part.get_content_charset() or 'utf-8',
                                         'replace')
            yield payload
        except Exception:
            continue


def classify_message(msg: Message) -> Tuple[str, Optional[str], str, str]:
    """(kind, address, code, reason).

    kind is 'hard', 'soft', 'unsubscribe', or 'other'.
    """
    frm = (msg.get('From') or '').lower()
    subj = (msg.get('Subject') or '').lower()
    body = '\n'.join(_walk_text(msg))

    if 'unsubscribe' in subj or re.search(r'^\s*unsubscribe\s*$', body,
                                          re.I | re.M):
        sender = email.utils.parseaddr(msg.get('From') or '')[1]
        if sender:
            return 'unsubscribe', sender.lower(), '', 'replied unsubscribe'

    looks_like_bounce = (
        any(s in frm for s in BOUNCE_SENDERS)
        or 'delivery status notification' in subj
        or 'undelivered mail' in subj
        or 'returned to sender' in subj
        or 'delivery has failed' in subj
        or msg.get_content_type() == 'multipart/report')
    if not looks_like_bounce:
        return 'other', None, '', ''

    addr = None
    m = _FINAL_RCPT.search(body)
    if m:
        addr = m.group(1).strip().strip('<>').lower()

    st = _STATUS.search(body)
    if st:
        cls, a, b = st.group(1), st.group(2), st.group(3)
        code = '%s.%s.%s' % (cls, a, b)
        if addr is None:
            m2 = _INLINE_FAIL.search(body)
            addr = m2.group(1).lower() if m2 else None
        if addr is None:
            return 'other', None, code, 'bounce with no parsable recipient'
        if cls != '5':
            return 'soft', addr, code, subj[:100]

        # A 5.x.x is permanent, but permanent about WHAT matters enormously.
        #
        # 5.7.x is a policy rejection: the receiver refused THIS MESSAGE or
        # THIS SENDER. It says nothing about whether the mailbox exists. On
        # 2026-07-26 every one of the 400 most recent bounces was
        #   550-5.7.1 [<our mail IP>] Gmail has detected that this message is
        #   likely unsolicited mail ... this message has been blocked
        # against addresses that had been RCPT-verified live. Treating those
        # as dead mailboxes would have permanently suppressed 400+ real
        # readers -- the silent, unnoticed, irreversible error this module's
        # own docstring warns about -- while hiding the actual problem, which
        # is that our sending IP is blocked and no amount of list hygiene
        # fixes it.
        #
        # 5.2.2 is over-quota: a full mailbox is a temporary condition and a
        # person who clears it is still reachable, so it is treated as soft
        # for the same reason 4.x.x is.
        if a == '7':
            return 'blocked', addr, code, subj[:100]
        if code == '5.2.2':
            return 'soft', addr, code, subj[:100]
        return 'hard', addr, code, subj[:100]

    # No machine-readable status. Only act when the wording is unambiguous.
    m2 = _INLINE_FAIL.search(body)
    if m2:
        return 'hard', m2.group(1).lower(), '', m2.group(0)[:100].strip()
    return 'other', addr, '', 'bounce not parsable'


def process_mailbox(*, host: str = IMAP_HOST,
                    user: str = SMTP_USER,
                    password: Optional[str] = None,
                    mailbox: str = 'INBOX',
                    limit: int = 2000,
                    dry_run: bool = True,
                    mark_seen: bool = False) -> dict:
    """Scan the mailbox and suppress what should be suppressed.

    dry_run defaults True so the first call reports what it WOULD suppress.
    Suppression is irreversible in practice -- nobody re-adds an address --
    so it gets the same preview-then-confirm treatment as sending.
    """
    pw = password or SMTP_PASS
    out = {'scanned': 0, 'hard': 0, 'soft': 0, 'blocked': 0, 'unsubscribe': 0,
           'unparsed': 0, 'already_suppressed': 0, 'dry_run': dry_run,
           'suppressed': [], 'unparsed_subjects': [], 'blocked_samples': []}
    if not pw:
        out['error'] = 'no password (HEVOLVE_SMTP_PASS unset)'
        return out

    known = load_bounced()
    try:
        conn = imaplib.IMAP4_SSL(host, IMAP_PORT)
        conn.login(user, pw)
        conn.select(mailbox, readonly=not mark_seen)
    except Exception as exc:
        out['error'] = 'imap connect/login failed: %s' % exc
        return out

    try:
        typ, data = conn.search(None, 'ALL')
        ids = data[0].split() if typ == 'OK' and data and data[0] else []
        ids = ids[-int(limit):]
        for num in ids:
            try:
                typ, raw = conn.fetch(num, '(RFC822)')
                if typ != 'OK' or not raw or not raw[0]:
                    continue
                msg = email.message_from_bytes(raw[0][1])
            except Exception:
                continue
            out['scanned'] += 1
            kind, addr, code, reason = classify_message(msg)

            if kind == 'hard' and addr:
                if addr in known:
                    out['already_suppressed'] += 1
                    continue
                out['hard'] += 1
                out['suppressed'].append('%s (%s)' % (addr, code or 'no code'))
                if not dry_run:
                    record_bounce(addr, code, reason)
                    known.add(addr)
            elif kind == 'blocked':
                # The receiver refused us, not them. Never suppressed: the
                # address is very likely fine and will be reachable again once
                # sender reputation recovers. Counted separately and loudly,
                # because a rising 'blocked' count is the only early warning
                # that the domain or IP is being filtered -- and it is the one
                # number that must never be read as "these people are gone".
                out['blocked'] += 1
                if len(out['blocked_samples']) < 5:
                    out['blocked_samples'].append(
                        '%s (%s)' % (addr, code or 'no code'))
            elif kind == 'soft':
                # Temporary. Deliberately not suppressed: a full mailbox or a
                # greylist is not a reason to drop somebody permanently.
                out['soft'] += 1
            elif kind == 'unsubscribe' and addr:
                out['unsubscribe'] += 1
                if not dry_run:
                    record_optout(addr)
            elif reason.startswith('bounce'):
                out['unparsed'] += 1
                if len(out['unparsed_subjects']) < 10:
                    out['unparsed_subjects'].append(
                        (msg.get('Subject') or '')[:90])
    finally:
        try:
            conn.close()
        except Exception:
            pass
        try:
            conn.logout()
        except Exception:
            pass

    out['suppressed'] = out['suppressed'][:50]
    if dry_run and (out['hard'] or out['unsubscribe']):
        out['note'] = ('DRY RUN -- nothing suppressed. Call with '
                       'dry_run=False to apply.')
    # Sending harder into a policy block deepens it. Say so where whoever ran
    # this will actually see it, rather than leaving it to be inferred from a
    # column of numbers.
    if out['blocked'] and out['blocked'] >= max(out['hard'], 1):
        out['ALERT'] = (
            '%d policy rejections (5.7.x) -- the receiver is blocking THIS '
            'SENDER, not these people. Do not suppress them and do not keep '
            'sending: pause, fix reputation (auth, volume ramp, complaint '
            'rate), then resume.' % out['blocked'])
    return out


def _main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(
        prog='bounce_handler',
        description='Suppress hard-bounced addresses and honour unsubscribe '
                    'replies.')
    ap.add_argument('--mailbox', default='INBOX')
    ap.add_argument('--limit', type=int, default=2000)
    ap.add_argument('--apply', action='store_true',
                    help='actually suppress; omit to preview')
    args = ap.parse_args(argv)

    res = process_mailbox(mailbox=args.mailbox, limit=args.limit,
                          dry_run=not args.apply,
                          password=os.environ.get('HEVOLVE_SMTP_PASS'))
    for k, v in res.items():
        if isinstance(v, list):
            print('%-20s %d' % (k, len(v)))
            for item in v[:12]:
                print('    %s' % item)
        else:
            print('%-20s %s' % (k, v))
    return 1 if res.get('error') else 0


if __name__ == '__main__':
    import sys as _sys
    _sys.exit(_main())
