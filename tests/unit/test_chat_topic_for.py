"""Drift-guard test for `chat_topic_for(user_id)`.

The legacy WAMP topic ``com.hertzai.hevolve.chat.{user_id}`` is
subscribed by Android RN, Web SPA, and Nunba's Python adapter (see
``memory/reference_chat_topic_subscribers.md``).  This helper
centralises the f-string that built it so the day we retire the
legacy name we have one place to edit, but the OUTPUT must remain
byte-identical to the historical f-string.

This test fails CI if any future change rewords the helper output —
that would silently break every subscriber on every platform.
"""
from core.peer_link.message_bus import chat_topic_for


def test_chat_topic_for_matches_inline_fstring():
    """Helper output must equal what callers wrote inline before."""
    for user_id in ('user_alpha', 'g_02d12543ce57f2b2', '123', ''):
        expected = f'com.hertzai.hevolve.chat.{user_id}'
        assert chat_topic_for(user_id) == expected, (
            f"chat_topic_for({user_id!r}) returned "
            f"{chat_topic_for(user_id)!r}, expected {expected!r}.  "
            f"Wire-format change would silently break Android, Web SPA, "
            f"and Nunba adapter subscribers — all key on the legacy "
            f"prefix com.hertzai.hevolve.chat. ."
        )


def test_chat_topic_for_round_trips_through_resolve_legacy_topic():
    """The new helper must produce a string that
    `resolve_legacy_topic` correctly maps back to ``chat.response``,
    so MessageBus's TOPIC_MAP reverse lookup keeps working when this
    helper's output is published via the legacy publish_async path."""
    from core.peer_link.message_bus import resolve_legacy_topic
    user_id = 'user_xyz'
    legacy = chat_topic_for(user_id)
    bus_topic, suffix = resolve_legacy_topic(legacy)
    assert bus_topic == 'chat.response', (
        f"resolve_legacy_topic({legacy!r}) returned bus_topic={bus_topic!r}; "
        f"expected 'chat.response'.  TOPIC_MAP reverse lookup must keep "
        f"working with strings the helper produces, otherwise legacy "
        f"publish_async callers lose 4-leg fan-out (LOCAL/SSE/PEERLINK)."
    )
    assert suffix == user_id, (
        f"suffix extraction broken: got {suffix!r}, expected {user_id!r}")


def test_chat_topic_for_known_user_id_shape():
    """Sanity: the topic prefix must remain ``com.hertzai.hevolve.chat.``
    so subscribers' subscription strings keep matching."""
    out = chat_topic_for('test_user')
    assert out.startswith('com.hertzai.hevolve.chat.'), (
        f"Topic prefix changed — Android AutobahnConnectionManager and "
        f"Web crossbarWorker subscribe by prefix; any drift breaks them. "
        f"Got: {out!r}")
