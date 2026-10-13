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

These were two private helpers of integrations/ap2/ap2_mandate.py while
the channels' OAuth routes kept a second reader of the same variable.
"""
import os

PUBLIC_URL_VAR = 'HARTOS_PUBLIC_URL'
ALLOWED_HOSTS_VAR = 'ALLOWED_HOSTS'
LOOPBACK_HOSTS = frozenset({'localhost', '127.0.0.1', '::1'})


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
        host = (request.host or '').rsplit(':', 1)[0].strip('[]').lower()
        root = request.url_root.rstrip('/')
    except Exception:
        return ''
    if not host:
        return ''
    allowed = {h.strip().lower() for h in
               os.environ.get(ALLOWED_HOSTS_VAR, 'localhost,127.0.0.1').split(',')
               if h.strip() and h.strip() != '*'}
    if host in allowed or host in LOOPBACK_HOSTS:
        return root
    return ''


def public_base_url() -> str:
    """The configured public URL, else the trusted request root, else ''."""
    return configured_public_url() or trusted_request_base()
