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


# Channels whose default surface is a conversation with one person rather
# than a room somebody chose to join.  Pushing an announcement at those
# recipients is unsolicited commercial messaging: spam plainly, a TRAI
# UCC/DND problem in India, and a fast route to a permanently banned
# number.
#
# But the brand of the app is the wrong test, and a blanket ban is wrong
# with it -- WhatsApp Channels and Signal groups people actually joined are
# legitimate opt-in broadcast surfaces.  What matters is whether the
# recipient asked.  So these require the operator to affirm it per
# destination with `announce_audience: 'opt_in'`; unset means refused.
# Fail-closed on the accident, open to the deliberate.
#
# NOTE this gate applies ONLY to announcement broadcast.  Ordinary
# agent-to-human messaging on these channels -- replies, notifications a
# user asked for, an agent talking to its owner -- goes through
# registry.send_to_channel (agent_tools, self_chat, flask_integration) and
# is untouched by any of this.
PERSON_TO_PERSON_CHANNELS = frozenset({'signal', 'whatsapp', 'imessage'})
OPT_IN_AUDIENCE_VALUES = frozenset({'opt_in', 'opt-in', 'subscribed'})


def destination_shape(channel: str, chat_id: str) -> Optional[str]:
    """Classify a destination as 'multi_person', 'one_to_one', or None.

    This exists because announce_audience is only an operator ASSERTION.
    There is no subscription surface in this system: nothing records who
    subscribed, nothing offers an unsubscribe, and nothing checks either at
    send time.  A flag that reports what it was told is not evidence, so
    wherever the destination's shape is a checkable fact, use the fact.

    Telegram is the case where it is free: private chats carry positive
    ids, while groups, supergroups and channels carry negative ones.  That
    is a property of the destination rather than a claim about it, so a
    positive Telegram id is refused even when the operator marked the entry
    opt-in — the operator can be mistaken about which chat an id refers to.

    None means "cannot determine here", NOT "safe".  Callers keep whatever
    other gate applies; None never grants anything on its own.
    """
    if channel == 'telegram':
        try:
            return 'one_to_one' if int(str(chat_id).strip()) > 0 else 'multi_person'
        except (TypeError, ValueError):
            return None  # @channelname and similar — not decidable from the id
    # Discord/Slack channel-vs-DM is decidable, but only via an API call
    # (channel type lookup). Until that is wired, say so rather than guess.
    return None


def _collect_announcement_targets() -> List[Tuple[str, str]]:
    """Walk AdminAPI._channels and return [(channel_type, chat_id), ...]
    for every entry that has a non-empty `announce_chat_id` AND is
    marked enabled.  No DB hit — _channels is the canonical in-memory
    config store (also persisted by AdminAPI._save_config / restored
    by _load_config at startup).

    Person-to-person channels are refused regardless of configuration
    (see PERSON_TO_PERSON_CHANNELS)."""
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
        # Where the destination's shape is a checkable fact, the fact wins
        # over the operator's claim about it.
        shape = destination_shape(channel_type, chat_id)
        if shape == 'one_to_one':
            logger.warning(
                "refusing announcement target %s/%s: the destination id is a "
                "private one-to-one chat. announce_audience cannot override "
                "this — it is a property of the destination, not a setting.",
                channel_type, chat_id)
            continue

        if channel_type in PERSON_TO_PERSON_CHANNELS and shape != 'multi_person':
            audience = str(cfg.get('announce_audience') or '').strip().lower()
            if audience not in OPT_IN_AUDIENCE_VALUES:
                logger.warning(
                    "refusing announcement target %s/%s: %s defaults to "
                    "one-to-one messaging. If this destination is a channel "
                    "or group people subscribed to, set announce_audience: "
                    "'opt_in' on it to confirm that and enable broadcast.",
                    channel_type, chat_id, channel_type)
                continue
            logger.info(
                "announcement target %s/%s allowed: operator marked it as an "
                "opt-in audience", channel_type, chat_id)
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


# ── new-content announcements ───────────────────────────────────────────
#
# The broadcaster originally fired on exactly one event, hive.benchmark.
# published, so the only thing that ever reached an external channel was a
# benchmark proof.  Every page hevolve.ai publishes -- the answers, the
# incident write-ups -- was invisible to distribution.
#
# Source of truth is the published feed at /ai-news-feed.json rather than
# the web repo's files: it is already generated deterministically by
# scripts/fetch-ai-news.js, already carries the per-channel ?ref= URLs the
# funnel counts via /marketing/track, and reading it over HTTP means HARTOS
# needs no path into a sibling repository.

CONTENT_FEED_URL = 'https://hevolve.ai/ai-news-feed.json'
# How many pages may go out in a single pass.  One.  A community notices a
# steady contributor and mutes a firehose, and a backlog of twenty pages
# dumped at once reads as exactly the latter.
CONTENT_ANNOUNCE_LIMIT = 1


def fetch_own_pages(url: str = CONTENT_FEED_URL, timeout: int = 20) -> List[Dict[str, Any]]:
    """Read own_pages from the published agent feed.  Returns [] on any
    failure -- a distribution pass that cannot read the feed must be a
    no-op, never a guess about what to post."""
    import json
    import urllib.request
    try:
        req = urllib.request.Request(
            url, headers={'User-Agent': 'HARTOS-AnnouncementBroadcaster/1.0'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        pages = data.get('own_pages') or []
        return [p for p in pages if isinstance(p, dict) and p.get('url')]
    except Exception as exc:
        logger.warning("could not read content feed %s: %s", url, exc)
        return []


def select_unannounced(pages: List[Dict[str, Any]],
                       already: Any,
                       limit: int = CONTENT_ANNOUNCE_LIMIT) -> List[Dict[str, Any]]:
    """Pick up to `limit` pages that have not been announced before.

    Deterministic: feed order is preserved, so the oldest unannounced page
    goes first and a page cannot jump the queue between passes.  Pure, so
    the selection rule is testable without a network or a channel."""
    seen = set(already or ())
    out = []
    for page in pages:
        if page.get('url') in seen:
            continue
        out.append(page)
        if len(out) >= max(0, limit):
            break
    return out


def format_content_announcement(page: Dict[str, Any], channel: str) -> str:
    """Compose the outbound text for one page.

    States who is posting.  This is not decoration: the account is
    Hevolve's, the writing is Hevolve's, and a post that lets a reader
    assume otherwise is the thing we do not do.  It is also what keeps the
    post inside the self-promotion rules most communities publish."""
    title = (page.get('title') or '').strip()
    share = (page.get('share_urls') or {}).get(channel) or page.get('url')
    return (
        f"{title}\n"
        f"{share}\n\n"
        f"Posted by the Hevolve AI agent. We build HART OS and Nunba; "
        f"this is our own write-up."
    )


def _public_exposure_granted() -> bool:
    """Fail-closed check of the standing public_exposure consent.

    Reuses ConsentService rather than reading user_consents directly, so
    there is one definition of consent in the system.  Any error -- no DB,
    no service, no session -- means NOT granted: an external post must
    never happen because a check failed to run."""
    try:
        from integrations.social.consent_service import ConsentService
        from integrations.social import get_session
    except Exception as exc:
        logger.debug("consent service unavailable, treating as denied: %s", exc)
        return False
    try:
        db = get_session()
    except Exception as exc:
        logger.debug("no db session for consent check, denied: %s", exc)
        return False
    try:
        import os
        user_id = os.environ.get('HEVOLVE_SYSTEM_USER_ID', 'system')
        return bool(ConsentService.check_consent(db, user_id, 'public_exposure'))
    except Exception as exc:
        logger.warning("consent check failed, refusing to post: %s", exc)
        return False
    finally:
        try:
            db.close()
        except Exception:
            pass


def announce_new_content(limit: int = CONTENT_ANNOUNCE_LIMIT,
                         already_announced: Any = None,
                         consent_check=None,
                         pages=None) -> List[str]:
    """Announce up to `limit` not-yet-announced pages to every configured
    target.  Returns the URLs actually dispatched, for the caller to
    persist so the next pass skips them.

    consent_check / pages are injectable so the orchestration is testable
    without a database or a network."""
    check = consent_check or _public_exposure_granted
    if not check():
        logger.info(
            "new-content announcement skipped: public_exposure consent not "
            "granted (set HEVOLVE_AUTONOMOUS_MARKETING to grant it)")
        return []

    candidates = pages if pages is not None else fetch_own_pages()
    chosen = select_unannounced(candidates, already_announced, limit)
    if not chosen:
        return []

    targets = _collect_announcement_targets()
    if not targets:
        logger.debug("no announcement targets configured")
        return []

    sent = []
    for page in chosen:
        for channel, chat_id in targets:
            _dispatch_to_target(
                channel, chat_id, format_content_announcement(page, channel))
        sent.append(page['url'])
        logger.info("announced %s to %d target(s)", page['url'], len(targets))
    return sent


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
