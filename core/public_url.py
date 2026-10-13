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
  public_base_url         the configured URL, else the trusted request
                          root, else ''.  A caller that needs a reachable
                          URL refuses on '' rather than guess a host.
  internet_base_url       the same, but a trusted root counts only when
                          the internet can reach its host (is_public_host):
                          a loopback or LAN root is this node, and nothing
                          outside can post a payment confirmation to it.
  allowed_hosts           the ALLOWED_HOSTS entries, the ONE parse (the
                          request gate in security.middleware reads it too).

These were two private helpers of integrations/ap2/ap2_mandate.py while
the channels' OAuth routes kept a second reader of the same variable.
"""
import ipaddress
import os
from urllib.parse import urlparse

PUBLIC_URL_VAR = 'HARTOS_PUBLIC_URL'
ALLOWED_HOSTS_VAR = 'ALLOWED_HOSTS'
LOOPBACK_HOSTS = frozenset({'localhost', '127.0.0.1', '::1'})


def allowed_hosts() -> frozenset:
    """The ALLOWED_HOSTS entries as the operator wrote them: stripped, empties
    dropped, the '*' wildcard kept for the request gate to read.  Default
    'localhost,127.0.0.1', the shipped one."""
    return frozenset(h.strip() for h in
                     os.environ.get(ALLOWED_HOSTS_VAR, 'localhost,127.0.0.1').split(',')
                     if h.strip())


def is_public_host(host) -> bool:
    """Can the internet reach ``host``?  False for a loopback, private,
    link-local or unspecified literal, for 'localhost' and for a bare name
    without a dot (a LAN machine's name); True for a global address or a
    dotted name.  An explicit ALLOWED_HOSTS entry can still be private
    (central's list carries its docker bridge address), so trust and
    reachability are two questions."""
    h = str(host or '').strip().strip('[]').lower()
    if not h or h in LOOPBACK_HOSTS:
        return False
    try:
        return ipaddress.ip_address(h).is_global
    except ValueError:
        return '.' in h


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
        # urlparse's hostname drops the port and the brackets of an IPv6
        # literal ('[::1]:6777' and '[::1]' both give '::1') and lower-cases
        # the name; an rsplit on ':' got '[:' for a bracketed IPv6 host
        # without a port.
        host = (urlparse('//' + (request.host or '')).hostname or '').lower()
        root = request.url_root.rstrip('/')
    except Exception:
        return ''
    if not host:
        return ''
    allowed = {h.lower() for h in allowed_hosts() if h != '*'}
    if host in allowed or host in LOOPBACK_HOSTS:
        return root
    return ''


def public_base_url() -> str:
    """The configured public URL, else the trusted request root, else ''."""
    return configured_public_url() or trusted_request_base()


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
