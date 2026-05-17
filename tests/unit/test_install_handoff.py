"""
FT + NFT/security tests for the cross-device install-handoff agent tool.

Phase 1 coverage:
  FT  ─ canonical (device, locale) -> URL mapping is exhaustive
       ─ tool registers in the channel-tools closure list
       ─ tool dispatches to the channel registry with correct args
       ─ tool falls back gracefully when no binding exists
       ─ tool resolves the user's preferred binding when chat_id omitted

  NFT/security ─ Alice cannot send install link to Bob's chat_id
                 (cross-user spam guard)
              ─ install_link override outside ALLOWED_HOSTS is rejected
                (URL injection / phishing guard)
              ─ unsupported channel_type / target_device are rejected
              ─ unauthenticated caller (no user_id) is rejected

Phase 2 (pairing tokens, agentic policy) is out of scope — see
`memory/project_channel_install_handoff.md` for the backlog.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest


# ─── Canonical mapping coverage ────────────────────────────────────

def test_canonical_install_links_cover_all_devices():
    from core.install_links import (
        SUPPORTED_DEVICES, get_install_link,
    )
    for device in SUPPORTED_DEVICES:
        url = get_install_link(device)
        assert url, f"No canonical URL for {device}"
        assert url.startswith('http'), f"Invalid URL for {device}: {url}"


def test_get_install_link_returns_none_for_unknown_device():
    from core.install_links import get_install_link
    assert get_install_link('toaster') is None
    assert get_install_link('') is None
    assert get_install_link(None) is None  # type: ignore[arg-type]


def test_get_install_link_falls_back_to_default_for_unknown_locale():
    from core.install_links import get_install_link
    # 'xx-Klingon' has no override entry; must fall back to default
    assert get_install_link('windows', 'xx-Klingon') == get_install_link(
        'windows', 'default')


def test_is_supported_install_channel_matches_constants():
    from core.install_links import (
        SUPPORTED_INSTALL_CHANNELS, is_supported_install_channel,
    )
    for ch in SUPPORTED_INSTALL_CHANNELS:
        assert is_supported_install_channel(ch)
        assert is_supported_install_channel(ch.upper())  # case-insensitive
    assert not is_supported_install_channel('imessage')  # not in Phase 1
    assert not is_supported_install_channel('')


# ─── Allowlist (URL injection guard) ───────────────────────────────

@pytest.mark.parametrize('url', [
    'https://github.com/hertz-ai/Nunba/releases/latest/download/Nunba_Setup.exe',
    'https://objects.githubusercontent.com/release-asset/abc.exe',
    'https://play.google.com/store/apps/details?id=com.hertzai.hevolve',
    'https://apps.apple.com/us/app/nunba/id1234567890',
    'https://hevolve.ai/download',
    'https://docs.hevolve.ai/binaries/nunba.dmg',
    'https://testflight.apple.com/join/abc123',
    # Subdomain of allowed host is fine
    'https://release.github.com/Nunba.exe',
])
def test_is_allowed_install_link_accepts_allowlisted_hosts(url):
    from core.install_links import is_allowed_install_link
    assert is_allowed_install_link(url), f"Should allow: {url}"


@pytest.mark.parametrize('url', [
    'https://evil.example.com/Nunba.exe',
    # Typosquat — github.com.evil.example is NOT a github.com subdomain
    'https://github.com.evil.example/Nunba.exe',
    # Wrong scheme
    'ftp://github.com/Nunba.exe',
    'file:///etc/passwd',
    'javascript:alert(1)',
    # Empty / malformed
    '',
    'not a url at all',
    # No host
    'https:///Nunba.exe',
])
def test_is_allowed_install_link_rejects_non_allowlisted(url):
    from core.install_links import is_allowed_install_link
    assert not is_allowed_install_link(url), f"Should reject: {url}"


# ─── Tool registration ─────────────────────────────────────────────

def test_send_install_link_is_registered_in_channel_tool_closures():
    from integrations.channels.agent_tools import build_channel_tool_closures
    tools = build_channel_tool_closures({'user_id': 1})
    names = [t[0] for t in tools]
    assert 'send_install_link' in names

    # Verify description mentions the consent / allowlist constraints
    # (the LLM reads this — guarantees it knows to confirm + not phish)
    desc = next(t[1] for t in tools if t[0] == 'send_install_link')
    assert 'CONFIRM' in desc.upper() or 'CONFIRM' in desc, (
        "Tool description must instruct the LLM to confirm the channel."
    )


# ─── Behavioural tests ─────────────────────────────────────────────
#
# We mock the channel registry's running asyncio loop and verify the
# tool plumbs args through correctly.  The DB binding lookup is
# mocked via `integrations.social.models.get_db`.

def _build_tool(user_id=42):
    from integrations.channels.agent_tools import build_channel_tool_closures
    tools = build_channel_tool_closures({'user_id': user_id})
    return next(t[2] for t in tools if t[0] == 'send_install_link')


def _mock_binding(chat_id='tg_abc', sender_id='@alice', preferred=True):
    b = MagicMock()
    b.channel_chat_id = chat_id
    b.channel_sender_id = sender_id
    b.is_preferred = preferred
    b.is_active = True
    return b


def _mock_db_with_binding(binding=None):
    """Build a get_db mock that returns a session whose .query().filter_by().first()
    yields `binding` (or None)."""
    db = MagicMock()
    q = MagicMock()
    db.query.return_value = q
    q.filter_by.return_value = q
    q.filter.return_value = q
    q.first.return_value = binding
    return db


class _FakeLoop:
    def is_running(self):
        return True


def _mock_registry_send(success=True, error=None, message_id='msg_1'):
    """Mock registry whose .send_to_channel returns SendResult-like."""
    registry = MagicMock()
    registry._loop = _FakeLoop()

    async def _send(channel, chat_id, message, **kw):
        sr = MagicMock()
        sr.success = success
        sr.error = error
        sr.message_id = message_id
        sr._args = (channel, chat_id, message)
        return sr

    registry.send_to_channel.side_effect = _send
    return registry


def test_send_install_link_dispatches_to_registry_with_canonical_url():
    """FT: tool resolves user's preferred binding + canonical URL +
    plumbs them through to registry.send_to_channel."""
    tool = _build_tool(user_id=42)
    binding = _mock_binding(chat_id='tg_chat_42', sender_id='@u42')
    db = _mock_db_with_binding(binding)
    registry = _mock_registry_send(success=True, message_id='tg_msg_99')

    captured = {}

    async def _capture(channel, chat_id, message, **kw):
        captured['channel'] = channel
        captured['chat_id'] = chat_id
        captured['message'] = message
        sr = MagicMock()
        sr.success = True
        sr.message_id = 'tg_msg_99'
        return sr

    registry.send_to_channel.side_effect = _capture

    # asyncio.run_coroutine_threadsafe needs a real loop; we monkeypatch
    # to invoke the coroutine synchronously via asyncio.run.
    def _fake_run_threadsafe(coro, loop):
        result = asyncio.new_event_loop().run_until_complete(coro)
        fut = MagicMock()
        fut.result.return_value = result
        return fut

    with patch('integrations.social.models.get_db', return_value=db), \
         patch('integrations.channels.registry.get_registry',
               return_value=registry), \
         patch('asyncio.run_coroutine_threadsafe',
               side_effect=_fake_run_threadsafe):
        out = tool('telegram', 'windows')

    assert 'sent' in out.lower(), out
    assert captured['channel'] == 'telegram'
    assert captured['chat_id'] == 'tg_chat_42'
    assert 'Nunba_Setup.exe' in captured['message']
    assert 'github.com/hertz-ai/Nunba' in captured['message']


def test_send_install_link_returns_friendly_message_when_no_binding():
    """FT: tool falls back gracefully when user has no paired channel."""
    tool = _build_tool(user_id=42)
    db = _mock_db_with_binding(binding=None)
    registry = _mock_registry_send()

    with patch('integrations.social.models.get_db', return_value=db), \
         patch('integrations.channels.registry.get_registry',
               return_value=registry):
        out = tool('telegram', 'android')

    assert 'paired' in out.lower() or "don't have" in out.lower(), out
    # Must NOT have called the registry
    registry.send_to_channel.assert_not_called()


def test_send_install_link_rejects_unsupported_channel():
    """NFT: tool rejects channels not in SUPPORTED_INSTALL_CHANNELS."""
    tool = _build_tool(user_id=42)
    out = tool('imessage', 'macos')  # imessage not in Phase 1
    assert 'not a supported' in out.lower() or 'allowed' in out.lower()


def test_send_install_link_rejects_unsupported_device():
    """NFT: tool rejects devices not in SUPPORTED_DEVICES."""
    tool = _build_tool(user_id=42)
    out = tool('telegram', 'toaster')
    assert 'not a supported' in out.lower() or 'allowed' in out.lower()


def test_send_install_link_rejects_unauthenticated_caller():
    """NFT/security: tool refuses to dispatch without an authenticated user."""
    # Build the tool with no user_id and ensure threadlocal also returns None
    from integrations.channels.agent_tools import build_channel_tool_closures
    tools = build_channel_tool_closures({})  # no user_id
    tool = next(t[2] for t in tools if t[0] == 'send_install_link')

    with patch('integrations.channels.agent_tools._get_user_id_from_threadlocal',
               return_value=None):
        out = tool('telegram', 'android')

    assert 'identify' in out.lower() or 'authenticated' in out.lower()


def test_send_install_link_rejects_phishing_url_override():
    """NFT/security: install_link override must be on the allowlist."""
    tool = _build_tool(user_id=42)
    out = tool(
        'telegram', 'windows',
        install_link='https://evil.example.com/Nunba.exe',
    )
    assert 'allowlist' in out.lower() or 'allowed' in out.lower()


def test_send_install_link_rejects_chat_id_not_owned_by_caller():
    """NFT/security (no-spam): Alice cannot target Bob's chat_id."""
    tool = _build_tool(user_id=42)  # Alice
    # DB query returns None — Alice has no binding for this chat_id
    db = _mock_db_with_binding(binding=None)
    registry = _mock_registry_send()

    with patch('integrations.social.models.get_db', return_value=db), \
         patch('integrations.channels.registry.get_registry',
               return_value=registry):
        out = tool(
            'telegram', 'windows',
            chat_id='bobs_chat_id_999',  # Bob's chat — Alice doesn't own it
        )

    assert 'not bound' in out.lower() or 'refusing' in out.lower(), out
    registry.send_to_channel.assert_not_called()


def test_send_install_link_accepts_owned_chat_id():
    """FT/security: explicit chat_id is accepted when ownership verifies."""
    tool = _build_tool(user_id=42)
    binding = _mock_binding(chat_id='tg_alice_chat', sender_id='@alice')
    db = _mock_db_with_binding(binding)
    registry = _mock_registry_send()

    captured = {}

    async def _capture(channel, chat_id, message, **kw):
        captured['channel'] = channel
        captured['chat_id'] = chat_id
        captured['message'] = message
        sr = MagicMock()
        sr.success = True
        sr.message_id = 'msg_1'
        return sr

    registry.send_to_channel.side_effect = _capture

    def _fake_run_threadsafe(coro, loop):
        result = asyncio.new_event_loop().run_until_complete(coro)
        fut = MagicMock()
        fut.result.return_value = result
        return fut

    with patch('integrations.social.models.get_db', return_value=db), \
         patch('integrations.channels.registry.get_registry',
               return_value=registry), \
         patch('asyncio.run_coroutine_threadsafe',
               side_effect=_fake_run_threadsafe):
        out = tool('telegram', 'android', chat_id='tg_alice_chat')

    assert 'sent' in out.lower()
    assert captured['chat_id'] == 'tg_alice_chat'
    assert 'play.google.com' in captured['message']
