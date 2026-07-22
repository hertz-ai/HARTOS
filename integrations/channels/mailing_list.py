"""One consolidated mailing list, validated before anything is sent.

Two problems this solves.

The first is fragmentation. Addresses live in several places (the user table,
old campaign exports, whatever a colleague has in a spreadsheet) and sending
from each separately means the same person gets the same mail more than once,
suppressions in one list are invisible to the others, and nobody can say how
many people we can actually reach. consolidate() merges every source into a
single deduplicated list, and dedupe is normalised (case, whitespace, and
Gmail dots/plus-tags) so near-duplicates collapse properly.

The second is bounces. A high bounce rate is the fastest way to get a sending
domain flagged, and it is entirely avoidable: most dead addresses can be
spotted for free before a single message goes out. The first batch sent to
facahaj@gsgajs.in, which bounced immediately, and that was predictable from
the domain alone.

Validation here is deliberately all free and local. No paid verification API.
In rough order of how much each catches:

  * syntax            -- malformed addresses
  * disposable domain -- throwaway inboxes, guaranteed non-engagement
  * typo domain       -- gmial.com and friends, real people who mistyped
  * role account      -- info@, admin@: higher complaint risk, lower interest
  * MX lookup         -- THE important one. A domain with no MX cannot receive
                         mail at all, so every send to it is a guaranteed
                         bounce. Results are cached per domain, so a list of
                         7,000 addresses costs a few hundred DNS lookups.

Nothing is silently dropped: every rejection carries a reason, so the caller
can see what was excluded and argue with it.
"""
from __future__ import annotations

import logging
import re
import socket
import subprocess
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger('hevolve_channels')

# Deliberately permissive: the MX check does the real work, and an overly
# strict pattern rejects valid but unusual addresses.
#
# Written as a POSITIVE class on purpose. The previous version was a negated
# class, [^@\s,;<>()\[\]\\]+, and it did not mean what it looks like: the
# trailing \\] did not close the set, so the class ran on and swallowed the
# '+', the '@' and the whole following [A-Za-z0-9] group, closing only at
# THAT bracket. The pattern silently degraded to "one non-alphanumeric
# character, then dot-something", which cannot match an '@' at all because
# '@' is inside the negated set. Result: classify() returned 'bad_syntax'
# for every real address ever passed to it. re.DEBUG shows it immediately;
# reading the pattern does not. No escaped brackets here, and '-' is last so
# it is a literal rather than a range.
_SYNTAX = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+"
                     r"@[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}$")

DISPOSABLE_DOMAINS = {
    'mailinator.com', 'guerrillamail.com', 'yopmail.com', '10minutemail.com',
    'tempmail.com', 'temp-mail.org', 'throwawaymail.com', 'trashmail.com',
    'sharklasers.com', 'getnada.com', 'maildrop.cc', 'fakeinbox.com',
    'dispostable.com', 'mailnesia.com', 'mintemail.com', 'spamgourmet.com',
}

# Real people who mistyped a real provider. Worth reporting separately from
# junk, because these are recoverable if anyone wants to correct them.
TYPO_DOMAINS = {
    'gmial.com', 'gmai.com', 'gmail.co', 'gmail.cm', 'gnail.com',
    'gmaill.com', 'yahooo.com', 'yaho.com', 'hotmial.com', 'hotmai.com',
    'outlok.com', 'rediffmai.com', 'gmail.con', 'yahoo.con',
}

# Shared inboxes. Not invalid, but they belong to a function rather than a
# person: lower engagement and a higher chance of being marked as spam.
ROLE_LOCALPARTS = {
    'info', 'admin', 'support', 'sales', 'contact', 'help', 'noreply',
    'no-reply', 'postmaster', 'webmaster', 'abuse', 'billing', 'careers',
    'hr', 'jobs', 'office', 'team', 'service', 'enquiry', 'enquiries',
}

_mx_cache: Dict[str, bool] = {}


def normalise(address: str) -> str:
    """Canonical form used for dedupe.

    Gmail ignores dots and anything after a plus in the local part, so
    'a.b+news@gmail.com' and 'ab@gmail.com' are the same inbox. Treating them
    as different is how one person receives the same campaign twice.
    """
    addr = (address or '').strip().strip('<>').lower()
    if '@' not in addr:
        return addr
    local, _, domain = addr.rpartition('@')
    if domain in ('gmail.com', 'googlemail.com'):
        local = local.split('+', 1)[0].replace('.', '')
        domain = 'gmail.com'
    else:
        local = local.split('+', 1)[0]
    return '%s@%s' % (local, domain)


def domain_has_mx(domain: str, timeout: int = 5) -> bool:
    """Can this domain receive mail at all?

    Tries dnspython, then the `nslookup`/`host` binaries, and finally falls
    back to "does the domain resolve at all", which is weaker but still
    catches invented domains. Cached per domain: a 7,000-address list is only
    a few hundred distinct domains.
    """
    domain = (domain or '').lower().strip()
    if not domain:
        return False
    if domain in _mx_cache:
        return _mx_cache[domain]

    ok = None
    try:
        import dns.resolver  # type: ignore
        try:
            answers = dns.resolver.resolve(domain, 'MX', lifetime=timeout)
            ok = len(answers) > 0
        except Exception:
            ok = False
    except ImportError:
        pass

    if ok is None:
        for cmd in (['nslookup', '-type=MX', domain],
                    ['host', '-t', 'MX', domain]):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True,
                                     timeout=timeout).stdout.lower()
                if 'mail exchanger' in out or 'handled by' in out:
                    ok = True
                    break
                if out.strip():
                    ok = False
            except Exception:
                continue

    if ok is None:
        # Last resort: a domain that does not resolve certainly cannot
        # receive mail. A domain that resolves might still lack MX, so this
        # is permissive by design rather than wrong.
        try:
            socket.getaddrinfo(domain, None)
            ok = True
        except Exception:
            ok = False

    _mx_cache[domain] = bool(ok)
    return bool(ok)


# Providers that answer truthfully at RCPT TO, measured rather than assumed.
#
# The test: probe a mailbox the provider has ALREADY hard-bounced with 5.1.1
# and see whether it rejects it a second time. Results from the mail VM:
#
#   gmail.com     550 5.1.1 The email account ... does not exist   truthful
#   hotmail.com   250 2.1.5 Recipient OK                           accepts all
#   yahoo.com     250 recipient <...> ok                           accepts all
#   aol.com       250 recipient <...> ok                           accepts all
#
# Microsoft, Yahoo and AOL accept every recipient at SMTP time and bounce
# asynchronously afterwards. Verification against them does not merely fail,
# it LIES: every dead mailbox comes back 250 and would be recorded as good.
# That is worse than not checking, because it manufactures false confidence
# in exactly the addresses that are about to bounce.
#
# So: verify Gmail, and for everyone else accept that dead mailboxes are only
# discoverable by sending and reading the bounce. That is what the ramp and
# the bounce handler are for.
VERIFIABLE_PROVIDERS = frozenset({
    'gmail.com', 'googlemail.com',
})

# Probing must originate from the mail server. The same probe from a machine
# without a PTR record and sending history gets the connection closed by
# Microsoft, Yahoo and AOL before RCPT is even reached, which reads as
# 'inconclusive' and is easy to mistake for 'unverifiable'.
PROBE_HELO = 'mail.hertzai.com'
PROBE_FROM = 'sathish@mail.hertzai.com'


def mailbox_exists(address: str, *, timeout: int = 25,
                   smtp=None) -> Tuple[Optional[bool], str]:
    """(exists, detail). exists is None when the provider cannot be trusted.

    Sends RCPT TO and then disconnects. No DATA is transmitted, so no message
    is ever delivered -- this is strictly less work for the receiving server
    than the send it replaces, which makes probing before sending cheaper
    than discovering the same fact by bouncing.

    Returns None rather than True for accept-all providers. Reporting their
    250 as 'exists' is the failure mode this function is written to avoid.
    """
    import smtplib

    addr = (address or '').strip().lower()
    domain = addr.rpartition('@')[2]
    if not domain:
        return False, 'no domain'
    if domain not in VERIFIABLE_PROVIDERS:
        return None, 'provider accepts all recipients at RCPT; not verifiable'

    host = _mx_host(domain)
    if not host:
        return False, 'no MX'

    srv = smtp
    try:
        if srv is None:
            srv = smtplib.SMTP(host, 25, timeout=timeout)
            srv.ehlo(PROBE_HELO)
            srv.mail(PROBE_FROM)
        code, msg = srv.rcpt(addr)
        detail = (msg.decode('utf-8', 'replace') if isinstance(msg, bytes)
                  else str(msg))[:120]
        if 200 <= code < 300:
            return True, detail
        if 500 <= code < 600:
            return False, detail
        # 4xx is a deferral (greylisting, rate limit), not an answer.
        return None, 'deferred: %s' % detail
    except Exception as exc:
        return None, 'probe failed: %s' % exc
    finally:
        if smtp is None and srv is not None:
            try:
                srv.quit()
            except Exception:
                pass


def _mx_host(domain: str) -> Optional[str]:
    """Lowest-preference MX for a domain, or None."""
    try:
        import dns.resolver  # type: ignore
        rows = sorted((r.preference, str(r.exchange).rstrip('.'))
                      for r in dns.resolver.resolve(domain, 'MX'))
        return rows[0][1] if rows else None
    except Exception:
        pass
    try:
        out = subprocess.run(['dig', '+short', 'MX', domain],
                             capture_output=True, text=True, timeout=10).stdout
        rows = sorted((int(l.split()[0]), l.split()[1].rstrip('.'))
                      for l in out.strip().splitlines() if len(l.split()) == 2)
        return rows[0][1] if rows else None
    except Exception:
        return None


def classify(address: str, *, check_mx: bool = True) -> Tuple[bool, str]:
    """(sendable, reason). reason is 'ok' when sendable."""
    addr = (address or '').strip()
    if not addr or not _SYNTAX.match(addr):
        return False, 'bad_syntax'
    local, _, domain = addr.lower().rpartition('@')
    if domain in DISPOSABLE_DOMAINS:
        return False, 'disposable_domain'
    if domain in TYPO_DOMAINS:
        return False, 'typo_domain'
    if local in ROLE_LOCALPARTS:
        return False, 'role_account'
    if check_mx and not domain_has_mx(domain):
        return False, 'no_mx'
    return True, 'ok'


def consolidate(sources: Dict[str, Iterable[str]],
                *,
                check_mx: bool = True,
                suppress: Iterable[str] = ()) -> dict:
    """Merge every source into ONE validated list.

    `sources` maps a source name to its addresses, so the result can say where
    each address came from. `suppress` is anything already opted out.

    Returns the clean list plus a breakdown of what was rejected and why --
    the rejections are the interesting part, since they are what would have
    bounced.
    """
    suppressed = {normalise(a) for a in suppress}
    seen: Dict[str, str] = {}          # normalised -> original
    origin: Dict[str, List[str]] = {}  # normalised -> [source names]
    duplicates = 0

    for source_name, addresses in sources.items():
        for raw in addresses or ():
            addr = (raw or '').strip()
            if not addr:
                continue
            key = normalise(addr)
            if key in seen:
                duplicates += 1
                if source_name not in origin[key]:
                    origin[key].append(source_name)
                continue
            seen[key] = addr
            origin[key] = [source_name]

    sendable: List[str] = []
    rejected: Dict[str, List[str]] = {}
    for key, original in seen.items():
        if key in suppressed:
            rejected.setdefault('suppressed', []).append(original)
            continue
        ok, reason = classify(original, check_mx=check_mx)
        if ok:
            sendable.append(original)
        else:
            rejected.setdefault(reason, []).append(original)

    return {
        'sendable': sendable,
        'sendable_count': len(sendable),
        'input_total': sum(len(list(v or ())) for v in sources.values()),
        'unique_after_dedupe': len(seen),
        'duplicates_removed': duplicates,
        'rejected_counts': {k: len(v) for k, v in sorted(rejected.items())},
        'rejected': rejected,
        'sources': {k: len(list(v or ())) for k, v in sources.items()},
        'domains_checked': len(_mx_cache),
        'multi_source': sum(1 for v in origin.values() if len(v) > 1),
    }
