"""Canonical social api response/request helpers (api_common.py) — #97.

Behavioural tests of the single-sourced _ok/_err/_paginate/_uuid that the
api_*.py blueprints previously each cloned.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_paginate_has_more_boundary():
    from integrations.social.api_common import _paginate
    assert _paginate(100, 10, 0) == {
        'total': 100, 'limit': 10, 'offset': 0, 'has_more': True}
    assert _paginate(10, 10, 0)['has_more'] is False     # 10 < 10 -> False
    assert _paginate(25, 10, 20)['has_more'] is False     # 30 < 25 -> False
    assert _paginate(25, 10, 10)['has_more'] is True      # 20 < 25 -> True


def test_ok_err_envelope_shapes():
    from flask import Flask
    from integrations.social.api_common import _ok, _err
    app = Flask(__name__)
    with app.app_context():
        resp, status = _ok({'x': 1}, meta={'p': 2})
        assert status == 200
        assert resp.get_json() == {'success': True, 'data': {'x': 1}, 'meta': {'p': 2}}
        # no data / no meta -> bare success (the canonical 'with meta' form)
        bare, _ = _ok()
        assert bare.get_json() == {'success': True}
        e, es = _err('boom', 404)
        assert es == 404 and e.get_json() == {'success': False, 'error': 'boom'}


def test_uuid_reexport_is_the_models_canonical():
    from integrations.social import api_common, models
    assert api_common._uuid is models._uuid


def test_api_blueprint_uses_the_canonical_helpers():
    # api.py was migrated off its local clones to import from api_common.
    from integrations.social import api, api_common
    assert api._ok is api_common._ok
    assert api._err is api_common._err
    assert api._paginate is api_common._paginate
    assert api._get_json is api_common._get_json
