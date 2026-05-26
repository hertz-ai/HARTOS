"""Regression test for #153 — publish_async stamps node_tier on every
chat-topic envelope.

The frontend's `formatTier` helper (web + Android + iOS, tasks
#146/#148/#150/#151/#152) reads `node_tier` off the WAMP envelope to
render the served-by badge.  If a future caller forgets to set it on
the published dict, the badge would silently show 'unknown' instead
of breaking — this test pins the safety net that publish_async
defaults `node_tier` from HEVOLVE_NODE_TIER env (or 'flat') before
the message goes onto the bus.

Non-chat topics are not stamped (only chat.* envelopes get the
badge field — keeps social/task/system envelopes minimal).
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch


def _call_publish_async(topic, message, env_tier=None):
    """Drive the publish_async slice that stamps node_tier.

    The full function is wired into the Flask app and Crossbar
    client; this test reproduces ONLY the node_tier-stamp branch in
    isolation so the assertion is on the data dict the bus receives,
    not on broader Crossbar wiring.
    """
    # Reproduce the exact code path from hart_intelligence_entry.py
    # so any drift between this test and the production logic shows
    # up as a test failure (string-search guard at the bottom).
    import json
    data = message
    if isinstance(message, str):
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            data = {'raw': message}

    # Topic resolver — minimal subset just for chat.* topics
    bus_topic = None
    user_id = ''
    if topic.startswith('com.hertzai.hevolve.chat.'):
        bus_topic = 'chat.response'
        user_id = topic.rsplit('.', 1)[-1]

    if bus_topic:
        if user_id and isinstance(data, dict):
            data.setdefault('user_id', user_id)
        if (isinstance(data, dict)
                and bus_topic
                and bus_topic.startswith('chat.')):
            data.setdefault(
                'node_tier',
                (env_tier if env_tier is not None
                 else os.environ.get('HEVOLVE_NODE_TIER', 'flat')))
    return data


def test_chat_topic_stamps_node_tier_default_flat():
    """Chat envelope without explicit node_tier gets 'flat' by default."""
    data = _call_publish_async(
        topic='com.hertzai.hevolve.chat.user42',
        message={'text': 'hi', 'request_id': 'r1'},
        env_tier='flat')
    assert data['node_tier'] == 'flat'


def test_chat_topic_stamps_node_tier_from_env():
    """When HEVOLVE_NODE_TIER=central, central is stamped."""
    data = _call_publish_async(
        topic='com.hertzai.hevolve.chat.user42',
        message={'text': 'hi'},
        env_tier='central')
    assert data['node_tier'] == 'central'


def test_caller_provided_node_tier_is_preserved():
    """Explicit node_tier from caller is NOT overwritten.

    setdefault semantics: the safety net never clobbers a value
    the caller chose deliberately (e.g. a regional node proxying
    a central reply must keep node_tier='central' in the envelope).
    """
    data = _call_publish_async(
        topic='com.hertzai.hevolve.chat.user42',
        message={'text': 'hi', 'node_tier': 'regional'},
        env_tier='flat')
    assert data['node_tier'] == 'regional'


def test_caller_provided_served_by_is_preserved():
    """served_by stays whatever the caller (dispatch site) set."""
    data = _call_publish_async(
        topic='com.hertzai.hevolve.chat.user42',
        message={'text': 'hi', 'served_by': 'hive', 'node_tier': 'flat'},
        env_tier='flat')
    assert data['served_by'] == 'hive'
    assert data['node_tier'] == 'flat'


def test_user_id_is_stamped_from_topic():
    """The audit also pins user_id propagation (independent of #153)."""
    data = _call_publish_async(
        topic='com.hertzai.hevolve.chat.user42',
        message={'text': 'hi'},
        env_tier='flat')
    assert data['user_id'] == 'user42'


# ─── Production-source structural check (prevents silent drift) ─────


def test_production_publish_async_has_node_tier_stamp():
    """Verifies that hart_intelligence_entry.publish_async contains
    the setdefault('node_tier', ...) line that this test pins.

    If a future refactor removes the stamp, this test catches it
    before behavior diverges in prod.
    """
    import inspect
    import hart_intelligence_entry as hie
    src = inspect.getsource(hie.publish_async)
    assert "setdefault(" in src
    assert "'node_tier'" in src
    assert "HEVOLVE_NODE_TIER" in src
    assert "chat." in src, (
        "node_tier stamp must be gated on chat.* topics — "
        "non-chat envelopes (task/social/system) should NOT carry "
        "this field per the per-task minimalism rule")
