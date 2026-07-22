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
from typing import Dict, Iterable, List, Tuple

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
