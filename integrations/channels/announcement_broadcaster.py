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
# recipient asked.  So these require an actual subscription record for the
# destination (see is_subscribed); no record means refused.  Fail-closed on
# the accident, open to anyone who genuinely asked to be there.
#
# NOTE this gate applies ONLY to announcement broadcast.  Ordinary
# agent-to-human messaging on these channels -- replies, notifications a
# user asked for, an agent talking to its owner -- goes through
# registry.send_to_channel (agent_tools, self_chat, flask_integration) and
# is untouched by any of this.
PERSON_TO_PERSON_CHANNELS = frozenset({'signal', 'whatsapp', 'imessage'})


def destination_shape(channel: str, chat_id: str) -> Optional[str]:
    """Classify a destination as 'multi_person', 'one_to_one', or None.

    Consent evidence comes in two grades and this is the stronger one.  A
    subscription record says somebody asked; platform-enforced membership
    says somebody other than us verified that they did.  Where the shape of
    a destination is a checkable fact, prefer the fact -- it cannot be wrong
    about which chat an id refers to, and a record can.

    Telegram is the case where it is free: private chats carry positive
    ids, while groups, supergroups and channels carry negative ones.  A
    negative id therefore needs no record at all, and a positive one needs a
    real subscription before anything is sent to it.

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


SUBSCRIPTION_CONSENT = 'announcement_subscription'
SUBSCRIPTION_SCOPE = 'announcements'
# What a person sends to stop receiving broadcasts. Kept deliberately wide:
# somebody trying to leave should not have to guess the magic word.
UNSUBSCRIBE_WORDS = frozenset({
    'unsubscribe', 'stop', 'stopall', 'quit', 'optout', 'opt-out', 'opt out',
    'leave', 'no more', 'unfollow',
})


def subscription_key(channel: str, chat_id: str) -> str:
    """Identity of a broadcast destination.

    The subscriber is a Telegram group or a Discord channel, not a HARTOS
    user, so the consent row is keyed by destination rather than person."""
    return f"{channel}:{str(chat_id).strip()}"


def _consent_session():
    """(ConsentService, db) or (None, None). Never raises: every caller
    treats an unavailable service as 'not subscribed'."""
    try:
        from integrations.social.consent_service import ConsentService
        from integrations.social import get_session
        return ConsentService, get_session()
    except Exception as exc:
        logger.debug("consent service unavailable: %s", exc)
        return None, None


def record_subscription(channel: str, chat_id: str, source: str = 'unknown') -> bool:
    """Record that a destination asked to receive broadcasts.

    `source` is how they asked -- a bot command, an admin action, an import.
    It is stored so a subscription can be audited later rather than taken on
    faith, which is the entire point of this table existing."""
    svc, db = _consent_session()
    if not svc:
        return False
    try:
        svc.grant_consent(db, subscription_key(channel, chat_id),
                          SUBSCRIPTION_CONSENT, scope=SUBSCRIPTION_SCOPE)
        logger.info("announcement subscription recorded for %s/%s (source=%s)",
                    channel, chat_id, source)
        return True
    except Exception as exc:
        logger.warning("could not record subscription for %s/%s: %s",
                       channel, chat_id, exc)
        return False
    finally:
        try:
            db.close()
        except Exception:
            pass


def revoke_subscription(channel: str, chat_id: str) -> bool:
    """Stop broadcasting to a destination. Failing to revoke is the one
    error here that must be loud: it means somebody asked to leave and we
    may keep messaging them."""
    svc, db = _consent_session()
    if not svc:
        logger.error("UNSUBSCRIBE NOT RECORDED for %s/%s: consent service "
                     "unavailable. This destination may keep receiving "
                     "broadcasts.", channel, chat_id)
        return False
    try:
        svc.revoke_consent(db, subscription_key(channel, chat_id),
                           SUBSCRIPTION_CONSENT, scope=SUBSCRIPTION_SCOPE)
        logger.info("announcement subscription revoked for %s/%s", channel, chat_id)
        return True
    except Exception as exc:
        logger.error("UNSUBSCRIBE FAILED for %s/%s: %s", channel, chat_id, exc)
        return False
    finally:
        try:
            db.close()
        except Exception:
            pass


def is_subscribed(channel: str, chat_id: str) -> bool:
    """Fail-closed check for a real subscription record."""
    svc, db = _consent_session()
    if not svc:
        return False
    try:
        return bool(svc.check_consent(db, subscription_key(channel, chat_id),
                                      SUBSCRIPTION_CONSENT,
                                      scope=SUBSCRIPTION_SCOPE))
    except Exception as exc:
        logger.warning("subscription check failed for %s/%s, treating as not "
                       "subscribed: %s", channel, chat_id, exc)
        return False
    finally:
        try:
            db.close()
        except Exception:
            pass


def looks_like_unsubscribe(text: str) -> bool:
    """Does an inbound message mean 'stop messaging me'?

    Matched on the whole message, trimmed and case-folded, so ordinary prose
    that happens to contain 'stop' does not silently unsubscribe somebody who
    was mid-conversation."""
    if not text:
        return False
    cleaned = str(text).strip().strip('/!.').lower()
    return cleaned in UNSUBSCRIBE_WORDS


def handle_unsubscribe_command(channel: str, chat_id: str, text: str) -> bool:
    """Adapter hook: call on every inbound message. Returns True if the
    message was an unsubscribe and has been handled."""
    if not looks_like_unsubscribe(text):
        return False
    revoke_subscription(channel, chat_id)
    return True


def _collect_announcement_targets(subscribed_check=None) -> List[Tuple[str, str]]:
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
    subscribed = subscribed_check or is_subscribed
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
        # Two ways a destination can be legitimate, and neither is a flag.
        #
        #   1. The platform enforces membership. A Telegram group or channel
        #      (negative id) is reachable only by people who joined it, so
        #      somebody other than us verified the opt-in.
        #   2. We hold a subscription record. Somebody asked, it was written
        #      down with a source, and they can unsubscribe.
        #
        # announce_audience used to stand in for both. It was an assertion by
        # the operator with nothing behind it: no subscriber list, no
        # unsubscribe path, nothing checked at send time. A destination whose
        # only credential is a claim that people opted in cannot be defended,
        # so the flag is removed rather than kept as a fallback -- two sources
        # of truth for consent is worse than one real one.
        shape = destination_shape(channel_type, chat_id)
        needs_record = (shape == 'one_to_one'
                        or (channel_type in PERSON_TO_PERSON_CHANNELS
                            and shape != 'multi_person'))
        if needs_record and not subscribed(channel_type, chat_id):
            logger.warning(
                "refusing announcement target %s/%s: one-to-one destination "
                "with no subscription record. It becomes eligible the moment "
                "someone actually subscribes, and stops the moment they "
                "unsubscribe.", channel_type, chat_id)
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
