"""Tests for core.hub_allowlist — runtime trusted HF org allowlist."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import hub_allowlist as hub
from core.hub_allowlist import (
    DEFAULT_TRUSTED_ORGS,
    HubAllowlist,
)


@pytest.fixture
def tmp_allowlist(tmp_path):
    """Fresh allowlist file per test — no shared state, no singleton."""
    cfg = tmp_path / 'hub_allowlist.json'
    return HubAllowlist(config_path=cfg)


def test_seed_defaults_on_first_load(tmp_path):
    """Cold install: file doesn't exist → seed all 27 defaults to disk."""
    cfg = tmp_path / 'hub_allowlist.json'
    assert not cfg.exists()
    al = HubAllowlist(config_path=cfg)
    assert cfg.exists()
    raw = json.loads(cfg.read_text(encoding='utf-8'))
    assert set(raw.keys()) == set(DEFAULT_TRUSTED_ORGS.keys())
    # Every entry has a reason — the audit-trail contract.
    for org in raw:
        assert raw[org]['reason']


def test_is_trusted_case_insensitive(tmp_allowlist):
    """HF org names resolve case-insensitively at the resolver — the
    allowlist MUST too, or an attacker can clone `qwen` as `Qwen`."""
    assert tmp_allowlist.is_trusted('qwen') is True
    assert tmp_allowlist.is_trusted('Qwen') is True
    assert tmp_allowlist.is_trusted('QWEN') is True
    assert tmp_allowlist.is_trusted('not-real-org') is False


def test_add_and_remove_round_trip(tmp_allowlist):
    """Add → is_trusted True → remove → is_trusted False; persisted to disk."""
    tmp_allowlist.add('acme-corp', 'enterprise tenant internal models')
    assert tmp_allowlist.is_trusted('acme-corp') is True
    # Re-load from disk via a fresh instance and verify persistence.
    fresh = HubAllowlist(config_path=tmp_allowlist._path)
    assert fresh.is_trusted('acme-corp') is True
    assert fresh.remove('acme-corp') is True
    # Idempotent remove
    assert fresh.remove('acme-corp') is False
    # Re-load and verify gone
    fresher = HubAllowlist(config_path=tmp_allowlist._path)
    assert fresher.is_trusted('acme-corp') is False


def test_add_rejects_invalid_org(tmp_allowlist):
    """Slashes, whitespace, empty strings, and non-ASCII (homoglyph defense)
    all rejected with ValueError so the admin handler surfaces the message."""
    with pytest.raises(ValueError):
        tmp_allowlist.add('', 'no name')
    with pytest.raises(ValueError):
        tmp_allowlist.add('has/slash', 'should fail')
    with pytest.raises(ValueError):
        tmp_allowlist.add('has space', 'should fail')
    with pytest.raises(ValueError):
        # Latin small i with acute (homoglyph for 'i') — same defense as
        # _normalize_hf_id in main.py.
        tmp_allowlist.add('a\u00ed4bharat', 'homoglyph attack')


def test_add_rejects_empty_reason(tmp_allowlist):
    """Reason is the audit trail — empty string defeats the purpose, reject."""
    with pytest.raises(ValueError):
        tmp_allowlist.add('valid-org', '')
    with pytest.raises(ValueError):
        tmp_allowlist.add('valid-org', None)  # type: ignore[arg-type]


def test_list_returns_metadata(tmp_allowlist):
    """The admin UI renders org + reason + added_at in a table; verify shape."""
    items = tmp_allowlist.list()
    assert len(items) >= len(DEFAULT_TRUSTED_ORGS)
    for entry in items:
        assert 'org' in entry
        assert 'reason' in entry
        assert 'added_at' in entry


def test_legacy_list_format_load(tmp_path):
    """A pre-existing config file with the legacy list-of-strings format
    should be tolerated (the `_TRUSTED_HF_ORGS` frozenset → JSON dump
    that an early operator might have done by hand)."""
    cfg = tmp_path / 'hub_allowlist.json'
    cfg.write_text(json.dumps(['google', 'microsoft']), encoding='utf-8')
    al = HubAllowlist(config_path=cfg)
    assert al.is_trusted('google') is True
    assert al.is_trusted('microsoft') is True
    # Defaults are NOT re-merged on legacy import — operator's curated
    # list is the source of truth.
    assert al.is_trusted('Qwen') is False


def test_corrupt_config_falls_back_to_defaults(tmp_path):
    """A corrupt JSON must not kill startup — log + re-seed defaults."""
    cfg = tmp_path / 'hub_allowlist.json'
    cfg.write_text("{not valid json", encoding='utf-8')
    al = HubAllowlist(config_path=cfg)
    # Defaults restored
    assert al.is_trusted('hertz-ai') is True


def test_unexpected_root_type_falls_back_to_defaults(tmp_path):
    """A JSON scalar root (neither list nor dict) is invalid — the internal
    ValueError must fail SAFE to defaults, exactly like a parse error."""
    cfg = tmp_path / 'hub_allowlist.json'
    cfg.write_text(json.dumps(42), encoding='utf-8')   # int root
    al = HubAllowlist(config_path=cfg)
    assert al.is_trusted('google') is True             # seeded, not crashed


def test_dict_with_scalar_value_is_coerced_to_reason(tmp_path):
    """Legacy dict {org: reason_string} (value not a dict) is coerced to a
    proper entry rather than dropped — the reason string is preserved."""
    cfg = tmp_path / 'hub_allowlist.json'
    cfg.write_text(json.dumps({'acme': 'just a reason'}), encoding='utf-8')
    al = HubAllowlist(config_path=cfg)
    assert al.is_trusted('acme') is True
    assert al.list()[0]['reason'] == 'just a reason'


def test_add_rejects_interior_tab_and_newline(tmp_allowlist):
    """The gate rejects '/' or WHITESPACE — that means any interior whitespace
    (tab, newline), not just the ASCII space. An HF org id is [A-Za-z0-9-], so
    a tab/newline is always malformed and must not slip past the supply-chain
    gate on a `' ' in org` check that only caught the literal space."""
    for bad in ('a\tb', 'a\nb', 'a\rb'):
        with pytest.raises(ValueError, match="'/' or whitespace"):
            tmp_allowlist.add(bad, 'should fail')
        assert tmp_allowlist.is_trusted(bad) is False


@pytest.mark.parametrize('junk', ['', None, 123, [], {}])
def test_is_trusted_rejects_empty_and_non_string(tmp_allowlist, junk):
    """The hot-path trust check must be junk-safe: empty / None / non-str all
    return False without raising (never let bad input read as trusted)."""
    assert tmp_allowlist.is_trusted(junk) is False


@pytest.mark.parametrize('junk', ['', None, 123])
def test_remove_rejects_empty_and_non_string(tmp_allowlist, junk):
    """remove() is idempotent+junk-safe: bad input returns False, not a raise."""
    assert tmp_allowlist.remove(junk) is False


def test_remove_is_case_insensitive(tmp_allowlist):
    """An operator revoking a takeover'd publisher must hit it regardless of
    the casing it was stored under — else `Qwen` survives a `remove('qwen')`."""
    tmp_allowlist.add('AcmeCorp', 'tenant')
    assert tmp_allowlist.remove('acmecorp') is True     # different casing
    assert tmp_allowlist.is_trusted('AcmeCorp') is False


def test_save_oserror_is_swallowed_and_memory_survives(tmp_path, monkeypatch):
    """A disk hiccup on the atomic replace must not break the operator flow:
    the add still lands IN MEMORY, no exception escapes, and the failed write
    simply doesn't persist (a reload won't see it)."""
    cfg = tmp_path / 'hub_allowlist.json'
    al = HubAllowlist(config_path=cfg)                  # seeds+saves cleanly

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(hub.os, 'replace', _boom)
    al.add('acme-corp', 'tenant')                       # must NOT raise
    assert al.is_trusted('acme-corp') is True           # in-memory add survived
    # replace failed -> nothing persisted -> a fresh reader won't see it
    assert HubAllowlist(config_path=cfg).is_trusted('acme-corp') is False


def test_default_path_is_under_home_dot_nunba():
    """The default config path mirrors ~/.nunba/mcp.token so operators learn
    one config root.  Pure Path construction — writes nothing."""
    p = hub._default_path()
    assert p.name == 'hub_allowlist.json'
    assert p.parent.name == '.nunba'
    assert p.parent.parent == Path.home()


def test_get_allowlist_singleton_and_reset(monkeypatch, tmp_path):
    """get_allowlist() memoizes one instance; reset_for_tests() clears it so a
    test can swap the config path.  Keep the default off the real ~/.nunba."""
    monkeypatch.setattr(hub, '_default_path',
                        lambda: tmp_path / 'singleton.json')
    hub.reset_for_tests()
    try:
        first = hub.get_allowlist()
        assert hub.get_allowlist() is first             # memoized
        hub.reset_for_tests()
        assert hub.get_allowlist() is not first         # cleared -> new instance
    finally:
        hub.reset_for_tests()
