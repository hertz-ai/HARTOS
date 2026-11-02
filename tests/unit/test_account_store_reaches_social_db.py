"""A Hevolve account reaches the node's social DB from the account store.

The account store (Hevolve_Database, GET {store}/get_user/{id}) and the social
DB are separate databases, and every account must exist in both (owner,
2026-10-26: "the social db is seperate but user accounts should be synced in
both").  Measured on central that day: its social DB had no user 10202 (the
owner's account, signed in on his desktop and phone), core.profile_sync had no
caller anywhere, and its fetch asked the hive PARENT (central.hevolve.ai, 404)
instead of the account store (azurekong.hertzai.com/db, 200 for 10202).

Real sync_profile / receiver / in-memory social DB; the account store and the
hive parent are real HTTP servers on loopback that record what they were asked.
No grep tests.

    python -m pytest tests/unit/test_account_store_reaches_social_db.py -q
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import profile_sync  # noqa: E402
from integrations.social.models import Base, User, db_session, get_engine  # noqa: E402

ACCOUNTS = {
    '10202': {'user_id': 10202, 'name': 'Owner', 'FCMtoken': None,
              'email_address': None, 'phone_number': None,
              'preferred_language': 'en', 'role': None},
    '10203': {'user_id': 10203, 'name': 'Second', 'FCMtoken': None,
              'email_address': None, 'phone_number': None,
              'preferred_language': 'ta', 'role': None},
}


class _Server:
    """A loopback HTTP server that records every path it is asked for.
    store=True answers /db/get_user/<id> the way Hevolve_Database does
    (200 + the profile, 400 'user not Found' on a miss); store=False is the
    hive parent, which has no such route (404)."""

    def __init__(self, store):
        self.asked = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.asked.append(self.path)
                uid = self.path.rsplit('/', 1)[-1]
                if store and self.path.startswith('/db/get_user/'):
                    if uid in ACCOUNTS:
                        body, code = ACCOUNTS[uid], 200
                    else:
                        body, code = {'detail': 'user not Found'}, 400
                else:
                    body, code = {'message': 'Not Found'}, 404
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = 'http://127.0.0.1:%d' % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def servers(monkeypatch):
    Base.metadata.create_all(get_engine())
    store, parent = _Server(store=True), _Server(store=False)
    monkeypatch.setenv('HEVOLVE_CENTRAL_DB_URL', store.url + '/db')
    # The hive parent the up-sync drains to: a different service.
    monkeypatch.setenv('HEVOLVE_CENTRAL_URL', parent.url)
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
    yield store, parent
    store.close()
    parent.close()


def _username(uid):
    with db_session(commit=False) as db:
        row = db.query(User).filter_by(id=uid).first()
        return row.username if row else None


def test_the_pull_asks_the_account_store_not_the_hive_parent(servers):
    store, parent = servers
    assert profile_sync.sync_profile('10202', '10202') is True
    assert _username('10202') == 'Owner'
    assert store.asked == ['/db/get_user/10202']
    assert parent.asked == []


def test_the_nodes_owner_lands_in_the_social_db(servers, monkeypatch):
    store, _ = servers
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', ' 10203 ')
    assert profile_sync.sync_owner_account() is True
    # Same id in both databases: the social row IS the account.
    assert _username('10203') == 'Second'
    assert store.asked == ['/db/get_user/10203']


def test_a_node_with_no_owner_asks_nobody(servers):
    store, parent = servers
    assert profile_sync.sync_owner_account() is False
    assert store.asked == [] and parent.asked == []


def test_an_owner_the_store_does_not_know_creates_nothing(servers, monkeypatch):
    store, _ = servers
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'guest-7f3a')
    assert profile_sync.sync_owner_account() is False
    assert _username('guest-7f3a') is None
    assert store.asked == ['/db/get_user/guest-7f3a']


def test_an_unreachable_store_returns_false_and_never_raises(servers, monkeypatch):
    store, _ = servers
    store.close()
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', '10202')
    assert profile_sync.sync_owner_account() is False
