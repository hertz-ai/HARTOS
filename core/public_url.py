"""This node's public base URL: the ONE reader of HARTOS_PUBLIC_URL.

A redirect payment gateway (PhonePe) returns the buyer to a URL this node
names and posts the signed confirmation to another; an OAuth provider
redirects to a third.  Each has to be a URL the outside world can reach
and that belongs to THIS node.

  configured_public_url   HARTOS_PUBLIC_URL, set by the operator (on
                          central the deploy writes it from the repo
                          secret, deploy/deepbox_deploy.sh).
  trusted_request_base    the live request's root, but only when its Host
                          is one this node was told to answer for: an
                          explicit ALLOWED_HOSTS entry (never the '*'
                          wildcard) or a loopback address.  The Host
                          header is the caller's to write; under
                          ALLOWED_HOSTS=* (the deploy default) a forged
                          Host would otherwise become the URL a gateway
                          sends the buyer and the signed confirmation to.
  internet_base_url       the configured URL, else the trusted request
                          root when the internet can reach its host
                          (is_public_host), else ''.  A loopback or LAN
                          root is this node, and nothing outside can post a
                          payment confirmation to it; a caller that needs a
                          reachable URL refuses on '' rather than guess.
  allowed_hosts           the ALLOWED_HOSTS entries, the ONE parse (the
                          request gate in security.middleware reads it too).

These were two private helpers of integrations/ap2/ap2_mandate.py while
the channels' OAuth routes kept a second reader of the same variable.  (A
``public_base_url``, the configured URL else any trusted root, lived here
for two commits with no caller once the payments path required a
reachable host; the review of f08ff3e4c found it orphaned.)
"""
import ipaddress
import os
import socket
from urllib.parse import urlparse

PUBLIC_URL_VAR = 'HARTOS_PUBLIC_URL'
ALLOWED_HOSTS_VAR = 'ALLOWED_HOSTS'
LOOPBACK_HOSTS = frozenset({'localhost', '127.0.0.1', '::1'})

#: Name suffixes the public DNS never resolves: IANA's special-use names
#: (RFC 6761 and 2606: localhost, test, example, invalid, onion, home.arpa
#: and the rest of .arpa) and the LAN conventions (.local, .localdomain,
#: .lan, .internal, .intranet, .home, .corp, .private).  A trusted request
#: from such a host is this node; no payment gateway can reach it (the
#: review of f08ff3e4c measured node.local, host.docker.internal,
#: svc.cluster.local and nas.home.arpa accepted as callback hosts).
UNROUTABLE_NAME_SUFFIXES = frozenset({
    'localhost', 'local', 'localdomain', 'lan', 'internal', 'intranet', 'home',
    'corp', 'private', 'arpa', 'test', 'example', 'invalid', 'onion',
})


def allowed_hosts() -> frozenset:
    """The ALLOWED_HOSTS entries as the operator wrote them: stripped, empties
    dropped, the '*' wildcard kept for the request gate to read.  Default
    'localhost,127.0.0.1', the shipped one."""
    return frozenset(h.strip() for h in
                     os.environ.get(ALLOWED_HOSTS_VAR, 'localhost,127.0.0.1').split(',')
                     if h.strip())


def is_public_host(host) -> bool:
    """Can the internet reach ``host``?

    ``host`` may carry a port, IPv6 brackets or a trailing dot; they are
    read off first, and malformed brackets are no host.  False for a
    loopback, private, link-local, reserved, multicast or unspecified
    literal (also in the shorthand spellings the socket layer accepts,
    '127.1' and '0x7f.1'), for 'localhost', for a bare name without a dot
    (a LAN machine's name), for a name whose last label is all digits (never
    a public TLD, RFC 1123 2.1: '256.1', '1.2.3.4.5' and the overflow
    spellings Windows' inet_addr rejects where glibc takes them) and for a
    name under UNROUTABLE_NAME_SUFFIXES; True for a global unicast address
    or any other dotted name.  An explicit ALLOWED_HOSTS entry can still be
    private (central's list carries its docker bridge address), so trust
    and reachability are two questions.  No DNS is asked.
    """
    raw = str(host or '').strip().lower()
    if not raw:
        return False
    if '[' not in raw and raw.count(':') >= 2:
        # A bare IPv6 literal carries no port; urlparse would read
        # '2606:4700:4700::1111' as host '2606' with a port.
        h = raw
    else:
        try:
            h = (urlparse('//' + raw).hostname or '').rstrip('.')
        except ValueError:
            # '[1.1.1.1]', '[]', '[::1' (the review of 9e154b956 measured
            # the raise reaching a direct caller).
            return False
    if not h or h in LOOPBACK_HOSTS:
        return False
    ip = _ip_literal(h)
    if ip is not None:
        return ip.is_global and not ip.is_multicast
    labels = h.split('.')
    if len(labels) < 2 or labels[-1].isdigit():
        return False
    return not any('.'.join(labels[i:]) in UNROUTABLE_NAME_SUFFIXES
                   for i in range(len(labels)))


def _ip_literal(h: str):
    """The address ``h`` names: by ipaddress, else by the socket layer,
    which also accepts the shorthand and hex IPv4 spellings ('127.1',
    '0x7f.1') ipaddress refuses; None when ``h`` names no address."""
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(h)))
    except (OSError, ValueError):
        return None


def configured_public_url() -> str:
    """The operator's HARTOS_PUBLIC_URL without a trailing slash, or ''."""
    return (os.environ.get(PUBLIC_URL_VAR) or '').strip().rstrip('/')


def trusted_request_base() -> str:
    """The live request's root when its Host is trusted, else '' (and ''
    outside a request)."""
    try:
        from flask import has_request_context, request
        if not has_request_context():
            return ''
        raw_host = request.host or ''
        if '@' in raw_host:
            # A Host header is never a URL with credentials.  urlparse
            # reads the part after the '@' as the host, so the trusted
            # name could be the tail of 'attacker.example@<ours>' and the
            # request root carried the userinfo into a gateway URL (the
            # review of 9e154b956 measured it under ALLOWED_HOSTS='*,<host>').
            return ''
        # urlparse's hostname drops the port and the brackets of an IPv6
        # literal ('[::1]:6777' and '[::1]' both give '::1') and lower-cases
        # the name; an rsplit on ':' got '[:' for a bracketed IPv6 host
        # without a port.
        host = (urlparse('//' + raw_host).hostname or '').lower()
        root = request.url_root.rstrip('/')
    except Exception:
        return ''
    if not host:
        return ''
    allowed = {h.lower() for h in allowed_hosts() if h != '*'}
    if host in allowed or host in LOOPBACK_HOSTS:
        return root
    return ''


def internet_base_url() -> str:
    """The configured public URL, else the trusted request root when the
    internet can reach its host (is_public_host), else ''.  For a URL a
    third party must call (a payment gateway's confirmation): a request
    from localhost or a LAN address is trusted to be this node, but a URL on
    it is one nobody outside can reach, and the money would be taken with
    the confirmation lost (the review of af987a962 measured a callback on
    localhost from a mandate approved on the desktop)."""
    configured = configured_public_url()
    if configured:
        return configured
    root = trusted_request_base()
    if root and is_public_host(urlparse(root).hostname or ''):
        return root
    return ''
