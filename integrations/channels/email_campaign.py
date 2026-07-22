"""Outbound email campaigns through our own mail server.

Exists because the growth pipeline kept reaching for one-off scripts. Sending
to a real list is a capability the agent should own, with the safety rails
built in rather than remembered each time:

  * DRY RUN BY DEFAULT -- a caller must pass dry_run=False to deliver
  * RESUMABLE -- every success is appended to a sent-log before the next send,
    so an interrupted run never re-mails anyone
  * PACED -- delay_seconds is configurable; sending as fast as the socket
    allows is what gets a domain flagged, not the volume itself
  * SELF-HALTING -- consecutive failures stop the run rather than burning
    reputation against a wall
  * UNSUBSCRIBE-AWARE -- List-Unsubscribe on every message, and addresses in
    the opt-out file are skipped

Uses our own MTA (mail.hertzai.com) rather than a mailbox provider: the
provider mailbox rate-limited at ~19 messages, ours does not. Deliverability
comes from the domain being correctly authenticated (SPF via MX, DKIM
published, valid TLS) rather than from renting somebody's IP reputation.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from typing import Iterable, List, Optional

logger = logging.getLogger('hevolve_channels')

# Our own mail server. Overridable so a different deployment can point
# elsewhere without touching code.
SMTP_HOST = os.environ.get('HEVOLVE_SMTP_HOST', 'mail.hertzai.com')
SMTP_PORT = int(os.environ.get('HEVOLVE_SMTP_PORT', '587'))
SMTP_USER = os.environ.get('HEVOLVE_SMTP_USER', 'evolve@mail.hertzai.com')
SMTP_PASS = os.environ.get('HEVOLVE_SMTP_PASS', '')
FROM_NAME = os.environ.get('HEVOLVE_SMTP_FROM_NAME', 'Hevolve AI')
REPLY_TO = os.environ.get('HEVOLVE_SMTP_REPLY_TO', 'sathish@hertzai.com')

# Pacing. The default is deliberately unhurried: a burst is what trips
# receiving-side rate limits, and there is rarely a reason to rush a campaign.
DEFAULT_DELAY_SECONDS = float(os.environ.get('HEVOLVE_EMAIL_DELAY', '2.0'))
DEFAULT_PER_CONNECTION = int(os.environ.get('HEVOLVE_EMAIL_PER_CONN', '40'))
MAX_CONSECUTIVE_FAILURES = 8

_STATE_DIR = os.environ.get(
    'HEVOLVE_EMAIL_STATE',
    os.path.join(os.path.expanduser('~'), '.hevolve', 'email'))


def _state_path(campaign: str, name: str) -> str:
    os.makedirs(_STATE_DIR, exist_ok=True)
    safe = ''.join(ch for ch in campaign if ch.isalnum() or ch in '-_')
    return os.path.join(_STATE_DIR, '%s.%s' % (safe or 'campaign', name))


def load_sent(campaign: str) -> set:
    """Addresses already delivered for this campaign."""
    p = _state_path(campaign, 'sent')
    if not os.path.exists(p):
        return set()
    with open(p, 'r', encoding='utf-8') as f:
        return {line.strip().lower() for line in f if line.strip()}


def load_optouts() -> set:
    """Addresses that asked not to be contacted. Checked on every send."""
    p = os.path.join(_STATE_DIR, 'optout')
    if not os.path.exists(p):
        return set()
    with open(p, 'r', encoding='utf-8') as f:
        return {line.strip().lower() for line in f if line.strip()}


def record_optout(address: str) -> None:
    """Honour an unsubscribe. Failing to record one is the error that
    actually matters here, so it is logged loudly."""
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(os.path.join(_STATE_DIR, 'optout'), 'a', encoding='utf-8') as f:
            f.write(address.strip().lower() + '\n')
        logger.info('email opt-out recorded: %s', address)
    except Exception as exc:
        logger.error('OPT-OUT NOT RECORDED for %s: %s -- this address may '
                     'keep receiving mail', address, exc)


def build_message(to: str, subject: str, html: str, text: str) -> MIMEMultipart:
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = '%s <%s>' % (FROM_NAME, SMTP_USER)
    msg['To'] = to
    msg['Reply-To'] = REPLY_TO
    msg['Date'] = formatdate(localtime=True)
    msg['Message-ID'] = make_msgid(domain=SMTP_HOST)
    # Machine-readable unsubscribe: mail clients surface this as a button,
    # which is both courteous and a positive deliverability signal.
    msg['List-Unsubscribe'] = '<mailto:%s?subject=unsubscribe>' % REPLY_TO
    msg.attach(MIMEText(text, 'plain'))
    msg.attach(MIMEText(html, 'html'))
    return msg


def send_campaign(recipients: Iterable[str],
                  subject: str,
                  html: str,
                  text: str,
                  *,
                  campaign: str = 'default',
                  dry_run: bool = True,
                  delay_seconds: Optional[float] = None,
                  per_connection: Optional[int] = None,
                  limit: Optional[int] = None,
                  password: Optional[str] = None) -> dict:
    """Send to `recipients`, skipping anyone already sent or opted out.

    Returns a dict of counts. Never raises for a single bad recipient -- one
    dead address must not abort a campaign -- but a run of consecutive
    failures halts it, because that means something systemic is wrong and
    continuing would damage the sending domain.
    """
    delay = DEFAULT_DELAY_SECONDS if delay_seconds is None else float(delay_seconds)
    per_conn = DEFAULT_PER_CONNECTION if per_connection is None else int(per_connection)
    pw = password or SMTP_PASS

    sent_before = load_sent(campaign)
    optouts = load_optouts()
    todo: List[str] = []
    for raw in recipients:
        addr = (raw or '').strip()
        low = addr.lower()
        if not addr or '@' not in addr:
            continue
        if low in sent_before or low in optouts or low in {a.lower() for a in todo}:
            continue
        todo.append(addr)
    if limit:
        todo = todo[:int(limit)]

    result = {'campaign': campaign, 'candidates': len(todo),
              'already_sent': len(sent_before), 'opted_out': len(optouts),
              'sent': 0, 'failed': 0, 'dry_run': dry_run,
              'delay_seconds': delay,
              'estimated_minutes': round(len(todo) * delay / 60.0, 1)}

    if dry_run:
        result['note'] = ('DRY RUN -- nothing sent. Pass dry_run=False to '
                          'deliver.')
        return result
    if not pw:
        result['error'] = 'no SMTP password (HEVOLVE_SMTP_PASS unset)'
        return result

    ctx = ssl.create_default_context()   # full verification; our cert is valid
    log_path = _state_path(campaign, 'sent')
    consecutive = 0
    idx = 0
    while idx < len(todo):
        chunk = todo[idx:idx + per_conn]
        try:
            srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
            srv.ehlo()
            srv.starttls(context=ctx)
            srv.ehlo()
            srv.login(SMTP_USER, pw)
        except Exception as exc:
            result['error'] = 'connect/auth failed: %s' % exc
            logger.error('email campaign %s: %s', campaign, exc)
            break
        with open(log_path, 'a', encoding='utf-8') as log:
            for addr in chunk:
                try:
                    srv.sendmail(SMTP_USER, [addr],
                                 build_message(addr, subject, html, text).as_string())
                    result['sent'] += 1
                    consecutive = 0
                    log.write(addr + '\n')
                    log.flush()
                except Exception as exc:
                    result['failed'] += 1
                    consecutive += 1
                    logger.warning('email to %s failed: %s', addr, exc)
                    if consecutive >= MAX_CONSECUTIVE_FAILURES:
                        result['halted'] = ('%d consecutive failures -- halted '
                                            'to protect sender reputation'
                                            % consecutive)
                        idx = len(todo)
                        break
                time.sleep(delay)
        try:
            srv.quit()
        except Exception:
            pass
        idx += per_conn
    return result
