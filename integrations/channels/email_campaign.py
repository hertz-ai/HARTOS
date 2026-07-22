"""Outbound email campaigns through our own mail server.

Exists because the growth pipeline kept reaching for one-off scripts. Sending
to a real list is a capability the agent should own, with the safety rails
built in rather than remembered each time:

  * DRY RUN BY DEFAULT -- a caller must pass dry_run=False to deliver
  * RESUMABLE -- every success is appended to a sent-log before the next send,
    so an interrupted run never re-mails anyone
  * PACED -- delay_seconds is configurable; sending as fast as the socket
    allows is what gets a domain flagged, not the volume itself
  * WARMED UP -- a daily cap that ramps over the first week. Per-message
    pacing is not sufficient on its own: a domain with no sending history
    that delivers seventeen thousand messages in a day is blocked on
    reputation grounds no matter how evenly they were spaced
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
import re
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
# A person, not a role account. 'evolve@' reads as a system mailbox and
# costs trust before the mail is even opened.
SMTP_USER = os.environ.get('HEVOLVE_SMTP_USER', 'sathish@mail.hertzai.com')
SMTP_PASS = os.environ.get('HEVOLVE_SMTP_PASS', '')
FROM_NAME = os.environ.get('HEVOLVE_SMTP_FROM_NAME', 'Sathish at Hevolve')
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


# Warm-up schedule: how many messages a day the domain may send, by day of
# the campaign. A new sending domain has no reputation, and the receiving
# side treats a sudden five-figure day from an unknown IP as exactly what it
# looks like. Hotmail, Yahoo and AOL throttle first and ask questions later,
# and 89% of this list sits behind those four providers.
#
# The point is not caution for its own sake. A flat 17,280/day run gets the
# domain blocked in the first day, which means the remaining ~70,000 people
# never receive anything AND ordinary mail from hertzai.com starts landing in
# spam. The ramp is what makes sending to everyone actually possible.
WARMUP_SCHEDULE = [500, 1000, 2500, 5000, 10000, 15000, 20000]


def warmup_cap(day_index: int) -> int:
    """Messages allowed on day `day_index` (0-based) of a campaign."""
    if day_index < 0:
        return 0
    if day_index >= len(WARMUP_SCHEDULE):
        return WARMUP_SCHEDULE[-1]
    return WARMUP_SCHEDULE[day_index]


def _today() -> str:
    return time.strftime('%Y-%m-%d')


def load_sent(campaign: str) -> set:
    """Addresses already delivered for this campaign."""
    p = _state_path(campaign, 'sent')
    if not os.path.exists(p):
        return set()
    out = set()
    with open(p, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # Newer rows are 'YYYY-MM-DD<TAB>address'. Older rows are the bare
            # address, so both are read rather than silently re-mailing
            # everyone who was contacted before timestamps existed.
            out.add(line.split('\t')[-1].strip().lower())
    return out


def sent_on(campaign: str, day: Optional[str] = None) -> int:
    """How many went out on `day` (default today). Drives the daily cap."""
    p = _state_path(campaign, 'sent')
    if not os.path.exists(p):
        return 0
    day = day or _today()
    n = 0
    with open(p, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith(day + '\t'):
                n += 1
    return n


def campaign_days(campaign: str) -> int:
    """Index into the warm-up ramp: how many days this campaign sent on
    BEFORE today.

    Today is excluded deliberately. Counting it would make a campaign that
    has already sent this morning read as being a day further along than it
    is, so every resumed run would step the ramp up early -- and since the
    ramp is what keeps the domain out of trouble, an off-by-one here spends
    reputation rather than a few minutes.
    """
    p = _state_path(campaign, 'sent')
    if not os.path.exists(p):
        return 0
    today = _today()
    days = set()
    with open(p, 'r', encoding='utf-8') as f:
        for line in f:
            if '\t' in line:
                d = line.split('\t', 1)[0]
                if d != today:
                    days.add(d)
    return len(days)


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


def tracking_token(address: str, campaign: str) -> str:
    """Short opaque per-recipient token.

    An HMAC of the address rather than the address itself, so a click URL
    never carries somebody's email in plain text where it lands in server
    logs, Referer headers and analytics. It is one-way: we can confirm a
    token belongs to a known recipient by recomputing it, but the URL alone
    discloses nothing.
    """
    import hashlib
    import hmac
    secret = os.environ.get('HEVOLVE_TRACK_SECRET', 'hevolve-campaign')
    mac = hmac.new(secret.encode(), ('%s|%s' % (campaign, address.lower())).encode(),
                   hashlib.sha256)
    return mac.hexdigest()[:12]


def add_tracking(body: str, campaign: str, address: str,
                 source: str = 'email') -> str:
    """Tag our own links so a click can be attributed.

    Adds ref (which channel), c (which campaign) and t (which recipient,
    opaquely). Without c and t, '?ref=email' says traffic came from some
    email at some point, which is not enough to tell whether a campaign
    worked -- that ambiguity is why the first batch's clicks could not be
    distinguished from a mail scanner's.

    Only rewrites links to domains we own; a link to github.com is left
    alone rather than decorated with our parameters.
    """
    token = tracking_token(address, campaign)

    def _tag(match):
        url = match.group(0)
        if not any(d in url for d in ('hevolve.ai', 'hertzai.com')):
            return url
        # Strip any ref/c/t already on the URL before adding ours. The copy
        # links to '/download?ref=email' by hand, and blindly appending
        # produced '?ref=email&ref=email&c=...'. Duplicate keys are not an
        # error to a query parser, they just make analytics read whichever
        # one it happens to pick, which is worse than being wrong loudly.
        base, sep, query = url.partition('?')
        keep = [p for p in query.split('&')
                if p and p.split('=', 1)[0] not in ('ref', 'c', 't')]
        keep.extend(['ref=%s' % source, 'c=%s' % campaign, 't=%s' % token])
        return base + '?' + '&'.join(keep)

    return re.sub(r'https?://[^\s"\'<>)]+', _tag, body)


def build_message(to: str, subject: str, html: str, text: str,
                  campaign: str = 'default') -> MIMEMultipart:
    # Tag our own links per recipient so a click is attributable to this
    # campaign and this person. Done here rather than at the call site so no
    # future caller can forget it and leave another batch of unattributable
    # clicks behind.
    html = add_tracking(html, campaign, to)
    text = add_tracking(text, campaign, to)

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
                  daily_cap: Optional[int] = None,
                  warmup: bool = True,
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
    # `seen` is carried rather than recomputed. This loop used to test
    # membership against `{a.lower() for a in todo}`, which rebuilt the whole
    # set on every recipient: quadratic, and invisible at the few-hundred
    # sizes it was written against. At 77,369 recipients it is ~3e9
    # operations, and the run simply appeared to hang before sending anything.
    seen: set = set()
    for raw in recipients:
        addr = (raw or '').strip()
        low = addr.lower()
        if not addr or '@' not in addr:
            continue
        if low in sent_before or low in optouts or low in seen:
            continue
        seen.add(low)
        todo.append(addr)
    if limit:
        todo = todo[:int(limit)]

    # Apply the daily cap before anything is sent, so an over-large run is
    # trimmed rather than started and abandoned partway.
    day_index = campaign_days(campaign)
    already_today = sent_on(campaign)
    cap = None
    if daily_cap is not None:
        cap = int(daily_cap)
    elif warmup:
        cap = warmup_cap(day_index)
    remaining_today = None
    if cap is not None:
        remaining_today = max(0, cap - already_today)
        todo = todo[:remaining_today]

    result = {'campaign': campaign, 'candidates': len(todo),
              'already_sent': len(sent_before), 'opted_out': len(optouts),
              'sent': 0, 'failed': 0, 'dry_run': dry_run,
              'delay_seconds': delay,
              'estimated_minutes': round(len(todo) * delay / 60.0, 1)}
    if cap is not None:
        result['daily_cap'] = cap
        result['campaign_day'] = day_index + 1
        result['sent_today_before'] = already_today
        result['remaining_today'] = remaining_today

    if dry_run:
        result['note'] = ('DRY RUN -- nothing sent. Pass dry_run=False to '
                          'deliver.')
        return result
    if not pw:
        result['error'] = 'no SMTP password (HEVOLVE_SMTP_PASS unset)'
        return result

    # A resume-log prevents re-sending across runs but NOT across CONCURRENT
    # runs: two processes each read the log at start, so anything sent while
    # both are alive is sent twice. That is not hypothetical -- it happened,
    # and 36 people received this campaign twice before the second process was
    # found. An agent that can call this tool can call it twice, so the guard
    # has to be in here rather than in operator discipline.
    lock_path = _state_path(campaign, 'lock')
    try:
        # O_EXCL is the atomic part: whoever creates the file wins, and a
        # second caller cannot mistake "I just made it" for "it was already
        # there".
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        holder = ''
        try:
            with open(lock_path, 'r', encoding='utf-8') as f:
                holder = f.read().strip()
        except Exception:
            pass
        result['error'] = (
            "campaign '%s' is already running (lock held by pid %s at %s). "
            "Refusing to start a second sender -- concurrent runs double-send. "
            "Delete the lock file if that process is gone."
            % (campaign, holder or 'unknown', lock_path))
        return result

    ctx = ssl.create_default_context()   # full verification; our cert is valid
    log_path = _state_path(campaign, 'sent')
    consecutive = 0
    idx = 0
    try:
        return _run_sends(todo, subject, html, text, ctx, log_path, pw,
                          per_conn, delay, result, campaign)
    finally:
        # Always release, including on an exception -- a stale lock that
        # blocks every future run is its own outage.
        try:
            os.unlink(lock_path)
        except Exception:
            pass


def _run_sends(todo, subject, html, text, ctx, log_path, pw,
               per_conn, delay, result, campaign):
    """The send loop itself. Split out so the lock in send_campaign() can be
    released in a finally: without wrapping the whole body in a try:."""
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
                                 build_message(addr, subject, html, text,
                                               campaign).as_string())
                    result['sent'] += 1
                    consecutive = 0
                    # Dated so the daily cap can be enforced across restarts.
                    log.write('%s\t%s\n' % (_today(), addr))
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
