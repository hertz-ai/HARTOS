"""Chat-bubble publishers — priority-49 'Thinking' bubble + future siblings.

Sibling modules in this package, one per topic family:
  - ui_commands.py: HARTOS→phone fleet commands
  - realtime.py: social / community events
  - crossbar_publish.py (this): chat-bubble messages on
    com.hertzai.hevolve.chat.{user_id}

All route through publish_async, which itself fans out via
MessageBus.publish (LOCAL + SSE + PEERLINK + CROSSBAR).  The
hartos_backend_adapter._capture_thinking monkey-patch on
publish_async still fires, so per-request thinking buffers keep
working with no adapter change.

The 'preffered_language' typo is preserved on the wire because
the historical schema has carried it for a long time and frontends
may key on the misspelt field name.

Latent bug NOT fixed here:
  Two FULL-schema callers (autogen GroupChat + action-start tap)
  historically pass request_id="123456" as a literal placeholder.
  All their traces collapse into one buffer key in Nunba's
  adapter.  Migrating those callers preserves the placeholder for
  byte-identical output; fixing the propagation is tracked
  separately.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Zoom-box stub — autogen FULL shape ships this so the avatar /
# book-parsing UI sees a stable schema.  All-zero coords mean
# "no zoom"; consumer treats as absent.
_ZOOM_STUB: dict = {
    'top_left':     {'x': 0, 'y': 0},
    'top_right':    {'x': 0, 'y': 0},
    'bottom_right': {'x': 0, 'y': 0},
    'bottom_left':  {'x': 0, 'y': 0},
}


def publish_thinking_trace(
    *,
    text: Any,
    user_id: str,
    request_id: str = '',
    bot_type: str = 'Agent',
    full_schema: bool = False,
    preffered_language: str = 'en-US',
) -> bool:
    """Build a priority-49 'Thinking' bubble + publish to the user's chat topic.

    Args:
        text: Bubble body. Coerced to str via str() if not already a
            string — autogen taps occasionally pass non-str content
            whose default repr was historically what the wire carried.
        user_id: Target user. Empty / None returns False without publishing.
        request_id: Per-request span id used by Nunba's adapter to key
            trace buffers.
        bot_type: 'Agent' for normal pipeline traces, 'ComputeRouter'
            for compute-routing status pushes.
        full_schema: True for the autogen GroupChat / action-start
            taps (adds zoom_bounding_box, page/analogy image URLs,
            preffered_language). False for tool closures and routing
            status pushes.
        preffered_language: Language tag (typo preserved). Only
            emitted when full_schema=True.

    Returns:
        True if publish_async was invoked, False if HARTOS publisher
        is unresolvable or user_id is empty.  Exceptions inside
        publish_async are logged at debug and absorbed.
    """
    if not user_id:
        return False

    text_str = text if isinstance(text, str) else str(text)

    if full_schema:
        envelope = {
            'text': [text_str],
            'priority': 49,
            'action': 'Thinking',
            'historical_request_id': [],
            'preffered_language': preffered_language,
            'options': [],
            'newoptions': [],
            'bot_type': bot_type,
            'page_image_url': '',
            'analogy_image_url': '',
            'request_id': request_id,
            'zoom_bounding_box': _ZOOM_STUB,
        }
    else:
        envelope = {
            'text': [text_str],
            'priority': 49,
            'action': 'Thinking',
            'bot_type': bot_type,
            'request_id': request_id,
            'historical_request_id': [],
            'options': [],
            'newoptions': [],
        }

    try:
        from core.safe_hartos_attr import safe_hartos_attr
        publish_async = safe_hartos_attr('publish_async')
        if publish_async is None:
            logger.debug(
                "publish_thinking_trace: HARTOS publish_async unresolvable")
            return False
        publish_async(
            f'com.hertzai.hevolve.chat.{user_id}',
            json.dumps(envelope),
        )
        return True
    except Exception as e:
        logger.debug(f"publish_thinking_trace failed: {e}")
        return False
