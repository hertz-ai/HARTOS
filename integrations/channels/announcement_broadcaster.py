"""Outbound announcement broadcast — pushes Nunba's hive proofs to
externally-configured channel destinations (Discord, Telegram, Slack,
etc.) so the flywheel's self-advertising leg escapes the HARTOS-only
internal feed and reaches potential new users.

How it fits:

  hive_benchmark_prover._publish_results
    │
    ├── PostService.create  → on_new_post  → web/desktop/Android/iOS subscribers
    │
    └── emit_event('hive.benchmark.published')
                                      │
                                      ▼
                          announcement_broadcaster (THIS FILE)
                                      │
                                      ▼
                        ChannelRegistry.send_to_channel
                          (Discord, Telegram, Slack, ...)
                                      │
                                      ▼
                            ← external visitors discover HARTOS
                                  + sign up = flywheel spin

Listens for the 'hive.benchmark.published' EventBus event (emitted by
hive_benchmark_prover._publish_results around line 1197) and dispatches
to every channel in AdminAPI._channels that has an `announce_chat_id`
field set.  Uses the existing FlaskChannelIntegration's background
asyncio loop — fire-and-forget so a slow Discord API can't block the
EventBus emit chain.

Operators configure announcement targets via the standard admin
channel-config endpoint: set the channel's `announce_chat_id` to the
target chat where Nunba should post benchmark proofs (e.g. Discord
channel ID, Telegram chat ID, Slack channel ID).  No new model — the
field rides inside the existing channel-config dict so the admin UI
doesn't need a separate page.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve_channels')


# Module-level state for idempotent subscription wiring.  The boot
# path calls register_announcement_subscriber() exactly once; calling
# it again is a no-op (so reload-during-dev doesn't double-fire).
_subscribed = False
_subscribe_lock = threading.Lock()


def _build_announcement_text(payload: Dict[str, Any]) -> str:
    """Format the EventBus payload into a single string for outbound
    posting.  Mirrors the structure hive_benchmark_prover constructs
    for the internal social post, with a short header and the
    pre-built `comparison` block (which already includes baseline
    comparisons + the "distributed privacy, zero cloud cost" tag-
    line).  If `comparison` is missing the caller didn't go through
    _publish_results and we fall back to a minimal one-liner."""
    if not isinstance(payload, dict):
        return str(payload)
    comparison = payload.get('comparison')
    if comparison:
        return comparison
    # Minimal fallback so something useful still lands externally.
    bench = payload.get('benchmark', 'unknown')
    score = payload.get('score', 0)
    nodes = payload.get('num_nodes', 0)
    return (
        f"HIVE BENCHMARK PROOF — {bench}\n"
        f"  Score {score:.1%} across {nodes} nodes."
    )


def _collect_announcement_targets() -> List[Tuple[str, str]]:
    """Walk AdminAPI._channels and return [(channel_type, chat_id), ...]
    for every entry that has a non-empty `announce_chat_id` AND is
    marked enabled.  No DB hit — _channels is the canonical in-memory
    config store (also persisted by AdminAPI._save_config / restored
    by _load_config at startup)."""
    try:
        from integrations.channels.admin.api import get_api
    except Exception as exc:
        logger.debug("AdminAPI not importable yet: %s", exc)
        return []
    try:
        api = get_api()
    except Exception as exc:
        logger.debug("AdminAPI singleton not ready: %s", exc)
        return []
    targets: List[Tuple[str, str]] = []
    for channel_type, cfg in (getattr(api, '_channels', {}) or {}).items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get('enabled', True) is False:
            continue
        chat_id = (cfg.get('announce_chat_id') or '').strip()
        if not chat_id:
            continue
        targets.append((channel_type, chat_id))
    return targets


def _dispatch_to_target(channel: str, chat_id: str, text: str) -> None:
    """Schedule a fire-and-forget send_to_channel on the channel
    integration's background asyncio loop.  We DON'T block on the
    result — a slow Discord API or unreachable Telegram bot must
    not back-pressure the EventBus thread."""
    try:
        from integrations.channels.flask_integration import (
            get_channel_integration)
    except Exception as exc:
        logger.debug("channel integration unavailable: %s", exc)
        return
    integration = get_channel_integration()
    loop = getattr(integration, '_loop', None)
    if loop is None:
        logger.debug(
            "channel integration loop not running yet — skipping "
            "announcement to %s/%s", channel, chat_id)
        return
    coro = integration.registry.send_to_channel(channel, chat_id, text)
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception as exc:
        logger.warning(
            "failed to schedule announcement to %s/%s: %s",
            channel, chat_id, exc)
        return

    # Attach a callback so we LOG the outcome (success / error) but
    # never block the caller.  Bounded timeout so a stuck adapter
    # doesn't pile up coroutines forever.
    def _on_done(fut):
        try:
            result = fut.result(timeout=0)  # already done by the time
        except Exception as exc:
            logger.warning(
                "announcement send to %s/%s failed: %s",
                channel, chat_id, exc)
            return
        ok = getattr(result, 'success', None)
        if ok is False:
            err = getattr(result, 'error', '<no error>')
            logger.warning(
                "announcement send to %s/%s returned failure: %s",
                channel, chat_id, err)
        else:
            logger.info(
                "announcement sent to %s/%s", channel, chat_id)

    future.add_done_callback(_on_done)


def broadcast_announcement(text: str) -> int:
    """Send `text` to every announcement target.  Returns the number
    of targets dispatched (NOT the number that succeeded — see logs
    for outcome).  Exposed as a public helper so other call sites
    (manual announce endpoint, dispatch from CLI) can reuse the same
    path instead of duplicating the iteration logic."""
    targets = _collect_announcement_targets()
    if not targets:
        logger.debug(
            "broadcast_announcement: no targets configured — set "
            "announce_chat_id on at least one admin._channels entry "
            "to enable external proof broadcast")
        return 0
    for channel, chat_id in targets:
        _dispatch_to_target(channel, chat_id, text)
    return len(targets)


def _on_benchmark_published(_topic: str, data: Any) -> None:
    """EventBus callback.  Sync — schedules the async dispatch via
    run_coroutine_threadsafe.  Any exception is logged but never
    propagated so the EventBus emit chain stays intact for other
    subscribers (federated_aggregator, etc.)."""
    try:
        text = _build_announcement_text(data or {})
        count = broadcast_announcement(text)
        if count:
            logger.info(
                "hive.benchmark.published → dispatched to %d external "
                "channel target(s)", count)
    except Exception as exc:
        logger.warning(
            "announcement_broadcaster: dispatch failed: %s", exc)


def register_announcement_subscriber() -> bool:
    """Idempotent EventBus wire-up.  Call once at boot — subsequent
    calls are no-ops (per the _subscribed sentinel).  Returns True
    if the subscription was newly registered, False if it was already
    in place or the EventBus is unavailable (e.g. platform not yet
    bootstrapped — caller is expected to retry on the next tick).

    Uses the canonical core.platform.registry → 'events' lookup that
    federated_aggregator + others use, instead of a direct
    EventBus() instantiation that would fork the bus identity."""
    global _subscribed
    with _subscribe_lock:
        if _subscribed:
            return False
        try:
            from core.platform.registry import get_registry
            registry = get_registry()
            if not registry.has('events'):
                logger.debug(
                    "announcement_broadcaster: events service not "
                    "registered yet — caller should retry")
                return False
            bus = registry.get('events')
            bus.on('hive.benchmark.published', _on_benchmark_published)
            _subscribed = True
            logger.info(
                "announcement_broadcaster: subscribed to "
                "hive.benchmark.published")
            return True
        except Exception as exc:
            logger.debug(
                "announcement_broadcaster: subscribe skipped — %s", exc)
            return False


def reset_for_tests() -> None:
    """Test helper — drops the _subscribed sentinel so tests can
    re-register the listener against a fresh EventBus.  Not for
    production code."""
    global _subscribed
    with _subscribe_lock:
        _subscribed = False
