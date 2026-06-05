"""Behavioural tests for inbound channel→feed ingestion (task #62, 2026-05-30).

ChannelAdapter._dispatch_message now mirrors opted-in channels' GROUP posts
into the Nunba social feed via cross_channel.ingest_channel_message ("posts
from other channels auto-created in Nunba"). The privacy gates are the
load-bearing assertions:
  1. a private 1:1 DM is NEVER mirrored (is_group hard gate),
  2. only channels in HEVOLVE_INGEST_CHANNELS are mirrored (default OFF),
  3. empty content is skipped.
"""
from __future__ import annotations

import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import integrations.channels.base as base  # noqa: E402
from integrations.channels.base import ChannelAdapter, Message  # noqa: E402

_INGEST = 'integrations.social.cross_channel.ingest_channel_message'


class _SyncThread:
    """Run the ingest target synchronously so the test can assert on it
    (the real code fans it onto a daemon thread)."""
    def __init__(self, target=None, daemon=None, name=None):
        self._t = target

    def start(self):
        if self._t:
            self._t()


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    base._ingest_channels_cache = None
    monkeypatch.setattr(threading, 'Thread', _SyncThread)
    yield
    base._ingest_channels_cache = None


def _msg(channel='discord', is_group=True, text='hello from the channel', mid='m1'):
    return Message(id=mid, channel=channel, sender_id='s1', sender_name='Alice',
                   chat_id='c1', text=text, is_group=is_group)


def _ingest(message):
    # call the method unbound — it uses no instance state
    ChannelAdapter._maybe_ingest_to_feed(MagicMock(), message)


def test_group_post_from_opted_channel_is_mirrored(monkeypatch):
    monkeypatch.setenv('HEVOLVE_INGEST_CHANNELS', 'discord,telegram')
    base._ingest_channels_cache = None
    with patch(_INGEST) as mk:
        _ingest(_msg(channel='discord', is_group=True))
    assert mk.called, "an opted-in channel's group post must mirror to the feed"
    args, kwargs = mk.call_args
    assert args[0] == 'discord'
    assert kwargs.get('message_id') == 'm1'


def test_direct_message_is_never_mirrored(monkeypatch):
    """The privacy invariant: a 1:1 DM must NEVER become a public Nunba post,
    even with the channel opted in (or '*')."""
    monkeypatch.setenv('HEVOLVE_INGEST_CHANNELS', '*')
    base._ingest_channels_cache = None
    with patch(_INGEST) as mk:
        _ingest(_msg(channel='discord', is_group=False))
    assert not mk.called, "DMs must never be mirrored into the public feed"


def test_non_opted_channel_is_not_mirrored(monkeypatch):
    monkeypatch.setenv('HEVOLVE_INGEST_CHANNELS', 'telegram')
    base._ingest_channels_cache = None
    with patch(_INGEST) as mk:
        _ingest(_msg(channel='discord', is_group=True))
    assert not mk.called


def test_default_is_off(monkeypatch):
    monkeypatch.delenv('HEVOLVE_INGEST_CHANNELS', raising=False)
    base._ingest_channels_cache = None
    with patch(_INGEST) as mk:
        _ingest(_msg(channel='discord', is_group=True))
    assert not mk.called, "default (no env) must ingest nothing"


def test_empty_content_skipped(monkeypatch):
    monkeypatch.setenv('HEVOLVE_INGEST_CHANNELS', '*')
    base._ingest_channels_cache = None
    with patch(_INGEST) as mk:
        _ingest(_msg(channel='discord', is_group=True, text=''))
    assert not mk.called


def test_wildcard_mirrors_any_group_channel(monkeypatch):
    monkeypatch.setenv('HEVOLVE_INGEST_CHANNELS', '*')
    base._ingest_channels_cache = None
    with patch(_INGEST) as mk:
        _ingest(_msg(channel='some_new_channel', is_group=True))
    assert mk.called
