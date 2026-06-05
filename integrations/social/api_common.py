"""Shared Flask response/request helpers for the social ``api_*.py`` blueprints.

Single source for the ``{success, data, meta}`` / ``{success, error}`` response
envelope, the pagination meta, the request-body parser, and the id generator —
each previously copy-pasted into ~13 api_*.py files (3 of which had drifted on
the ``_ok`` signature).  Import from here instead of redefining.

The ``_ok`` signature is the canonical (with ``meta``) form; the drifted files
that defined ``_ok(data, status=200)`` without ``meta`` must reconcile their
call sites (any positional ``_ok(data, 200)`` would pass 200 as ``meta``) before
switching to this import.
"""
from flask import jsonify, request

from .models import _uuid  # re-export the one canonical id generator

__all__ = ['_ok', '_err', '_paginate', '_get_json', '_uuid']


def _ok(data=None, meta=None, status=200):
    """``{'success': True[, 'data'][, 'meta']}`` envelope → ``(response, status)``."""
    r = {'success': True}
    if data is not None:
        r['data'] = data
    if meta is not None:
        r['meta'] = meta
    return jsonify(r), status


def _err(msg, status=400):
    """``{'success': False, 'error': msg}`` envelope → ``(response, status)``."""
    return jsonify({'success': False, 'error': msg}), status


def _paginate(total, limit, offset):
    """Pagination meta dict; ``has_more`` is ``offset + limit < total``."""
    return {'total': total, 'limit': limit, 'offset': offset,
            'has_more': offset + limit < total}


def _get_json():
    """Parse the request body as JSON, tolerant of bad/empty bodies → ``{}``."""
    return request.get_json(force=True, silent=True) or {}
