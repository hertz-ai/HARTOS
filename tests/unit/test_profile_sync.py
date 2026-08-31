"""Behavioural tests for core.profile_sync — PULL a central profile DOWN into
the local social store via the EXISTING SyncEngine._handle_sync_user receiver.

Focus is the pure payload builder (the down-sync GATE: one central id -> exactly
one sync_user payload, with a deterministically non-empty username the receiver
requires) plus the best-effort fetch/sync contract (never raises; False on any
miss). 0% covered before this file. Real functions, mocked boundaries (requests,
db_session, receiver), behavioural asserts — no source-substring checks.

    python -m pytest tests/unit/test_profile_sync.py -q --noconftest
"""
from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

import pytest

from core import profile_sync


# ── build_user_sync_payload (pure) ──────────────────────────────────────────
class TestBuildPayload:
    def test_full_response_maps_every_field(self):
        resp = {
            "name": "Alice Doe", "FCMtoken": "tok-123",
            "preferred_language": "ta", "email_address": "a@x.io",
            "phone_number": "+100", "role": "member",
        }
        p = profile_sync.build_user_sync_payload("local-uuid", 77, resp)
        assert p["user_id"] == "local-uuid"          # keyed on the LOCAL id
        assert p["username"] == "Alice Doe"
        assert p["display_name"] == "Alice Doe"
        assert p["central_user_id"] == "77"          # carried through as str
        assert p["fcm_token"] == "tok-123"
        assert p["preferred_language"] == "ta"
        assert p["email"] == "a@x.io"                # email_address -> email
        assert p["phone"] == "+100"                  # phone_number -> phone
        assert p["role"] == "member"

    def test_empty_name_derives_username_from_central_id(self):
        # username is REQUIRED non-empty by the receiver; empty name must never
        # produce an empty username (would early-return / mint dupes).
        p = profile_sync.build_user_sync_payload("l1", 42, {"name": ""})
        assert p["username"] == "42"
        assert p["display_name"] == "42"

    def test_whitespace_name_is_stripped(self):
        p = profile_sync.build_user_sync_payload("l1", 42, {"name": "  Bob  "})
        assert p["username"] == "Bob"

    def test_missing_name_key_falls_back_to_central_id(self):
        p = profile_sync.build_user_sync_payload("l1", 9, {})
        assert p["username"] == "9"

    def test_non_dict_response_is_treated_as_empty(self):
        for junk in (None, "a string", 123, ["list"]):
            p = profile_sync.build_user_sync_payload("l1", 5, junk)
            assert p["username"] == "5"
            assert "fcm_token" not in p

    def test_none_central_id_yields_empty_central_and_username(self):
        p = profile_sync.build_user_sync_payload("l1", None, {"name": ""})
        assert p["central_user_id"] == ""
        assert p["username"] == ""   # both empty — receiver will reject, by design

    def test_fcm_token_accepts_either_key(self):
        assert profile_sync.build_user_sync_payload(
            "l", 1, {"name": "n", "fcm_token": "lower"})["fcm_token"] == "lower"
        assert profile_sync.build_user_sync_payload(
            "l", 1, {"name": "n", "FCMtoken": "upper"})["fcm_token"] == "upper"

    def test_absent_optional_fields_are_omitted_not_nulled(self):
        p = profile_sync.build_user_sync_payload("l", 1, {"name": "n"})
        for k in ("fcm_token", "preferred_language", "email", "phone", "role"):
            assert k not in p, f"{k} must be omitted when absent, not set to None"


# ── fetch_central_profile ───────────────────────────────────────────────────
class TestFetchCentralProfile:
    def test_empty_central_id_returns_none(self):
        assert profile_sync.fetch_central_profile(None) is None
        assert profile_sync.fetch_central_profile("") is None

    def test_no_central_configured_returns_none(self, monkeypatch):
        monkeypatch.setattr(profile_sync, "_central_url", lambda: "")
        assert profile_sync.fetch_central_profile(5) is None

    def test_200_dict_is_returned(self, monkeypatch):
        monkeypatch.setattr(profile_sync, "_central_url", lambda: "http://c")
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"name": "X"}
        import requests
        monkeypatch.setattr(requests, "get", lambda *a, **k: resp)
        assert profile_sync.fetch_central_profile(5) == {"name": "X"}

    def test_non_200_returns_none(self, monkeypatch):
        monkeypatch.setattr(profile_sync, "_central_url", lambda: "http://c")
        import requests
        monkeypatch.setattr(requests, "get",
                            lambda *a, **k: MagicMock(status_code=404))
        assert profile_sync.fetch_central_profile(5) is None

    def test_non_dict_json_returns_none(self, monkeypatch):
        monkeypatch.setattr(profile_sync, "_central_url", lambda: "http://c")
        resp = MagicMock(status_code=200)
        resp.json.return_value = ["not", "a", "dict"]
        import requests
        monkeypatch.setattr(requests, "get", lambda *a, **k: resp)
        assert profile_sync.fetch_central_profile(5) is None

    def test_request_exception_returns_none_never_raises(self, monkeypatch):
        monkeypatch.setattr(profile_sync, "_central_url", lambda: "http://c")
        import requests

        def _boom(*a, **k):
            raise requests.RequestException("down")

        monkeypatch.setattr(requests, "get", _boom)
        assert profile_sync.fetch_central_profile(5) is None


# ── sync_profile (orchestration + the single-user gate) ─────────────────────
class TestSyncProfile:
    def test_missing_ids_return_false(self):
        assert profile_sync.sync_profile("", 1) is False
        assert profile_sync.sync_profile("l", None) is False

    def test_unreachable_central_returns_false(self, monkeypatch):
        monkeypatch.setattr(profile_sync, "fetch_central_profile", lambda cid, **k: None)
        assert profile_sync.sync_profile("l", 1) is False

    def test_happy_path_feeds_exactly_one_payload_to_the_receiver(self, monkeypatch):
        monkeypatch.setattr(profile_sync, "fetch_central_profile",
                            lambda cid, **k: {"name": "Zed", "FCMtoken": "t"})

        @contextlib.contextmanager
        def _fake_session():
            yield MagicMock()

        monkeypatch.setattr("integrations.social.models.db_session", _fake_session)
        handler = MagicMock()
        monkeypatch.setattr(
            "integrations.social.sync_engine.SyncEngine._handle_sync_user", handler)

        assert profile_sync.sync_profile("local-1", 77) is True
        handler.assert_called_once()                 # the GATE: exactly one user
        _db, payload = handler.call_args.args
        assert payload["user_id"] == "local-1"
        assert payload["central_user_id"] == "77"
        assert payload["username"] == "Zed"

    def test_receiver_hiccup_returns_false_never_raises(self, monkeypatch):
        monkeypatch.setattr(profile_sync, "fetch_central_profile",
                            lambda cid, **k: {"name": "Zed"})

        @contextlib.contextmanager
        def _fake_session():
            yield MagicMock()

        monkeypatch.setattr("integrations.social.models.db_session", _fake_session)

        def _boom(db, payload):
            raise RuntimeError("receiver down")

        monkeypatch.setattr(
            "integrations.social.sync_engine.SyncEngine._handle_sync_user", _boom)
        assert profile_sync.sync_profile("local-1", 77) is False
