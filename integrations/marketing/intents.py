"""Canonical marketing intent URLs — flywheel content distribution (#181).

Single source of truth for the per-platform "click-this-URL-to-post"
intent URLs that the marketing agent + manual operators both consume.
Replaces the one-off `_drops_ready/intents.md` doc with code so the
agent can fetch by platform via `/api/social/marketing/intents`.

Each Intent carries:
    platform     — twitter | linkedin | reddit | hackernews | whatsapp
    code         — channel code used in ref-tag + attribution
                   (matches /api/social/marketing/track validation)
    intent_url   — fully-formed URL that opens the platform composer
    body_text    — text to paste (where the platform's URL doesn't
                   accept long bodies — LinkedIn, WhatsApp)
    title        — optional title (HN, Reddit)
    landing_url  — the download URL the platform points to (carries
                   ?ref=code for attribution)

Adding a new platform: add one entry to INTENTS.  No new code paths.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import quote


# Canonical landing URLs (carry ?ref=code for attribution)
# ``nunba.hevolve.ai`` does not resolve (DNS dead); the live commercial-API
# landing is the pricing page that the landing SPA actually routes
# (MainRoute.js ``/pricing`` -> CommercialApiPricing). Override via
# HEVOLVE_API_LANDING_URL.
_COMMERCIAL_API_DOCS = os.environ.get(
    'HEVOLVE_API_LANDING_URL', 'https://hevolve.ai/pricing')
# Live, multi-platform installer page. The shorter ``hevolve.ai/download`` is a
# 404 today — the landing SPA has no such route (see MainRoute.js), so every
# post that pointed installs there dead-ended (root cause of 0 funnel events).
# ``docs.hevolve.ai/downloads/`` is the page that actually serves the
# .exe / .dmg / .AppImage. Override via HEVOLVE_DOWNLOAD_URL once a tracked
# ``hevolve.ai/download?ref=`` landing route is deployed (for funnel attribution).
_DOWNLOAD = os.environ.get(
    'HEVOLVE_DOWNLOAD_URL', 'https://docs.hevolve.ai/downloads/')


def _with_ref(base: str, code: str) -> str:
    """Append ?ref=<code> to a landing URL.

    Caller's responsibility: this is what the agent posts and what
    /api/social/marketing/track increments when a click lands.
    """
    sep = '&' if '?' in base else '?'
    return f'{base}{sep}ref={code}'


@dataclass
class MarketingIntent:
    platform: str
    code: str
    intent_url: str
    landing_url: str
    body_text: str = ''
    title: str = ''
    notes: str = ''

    def to_dict(self) -> dict:
        return {
            'platform': self.platform,
            'code': self.code,
            'intent_url': self.intent_url,
            'landing_url': self.landing_url,
            'body_text': self.body_text,
            'title': self.title,
            'notes': self.notes,
        }


# ─── Twitter / X ───────────────────────────────────────────────────


_TW1_BODY = (
    'We built an AI API.\n'
    'Free tier: 100 req/day, free forever.\n'
    'Paid tiers start at $9/month.\n'
    '90% of every dollar goes to the people running the compute, '
    'not us.\n\n'
)


def _twitter_intent(code: str, body: str, landing_url: str) -> str:
    """Build a twitter.com/intent/tweet URL with pre-filled body."""
    full = body + landing_url
    return f'https://twitter.com/intent/tweet?text={quote(full)}'


# ─── LinkedIn ──────────────────────────────────────────────────────


_LI_A_BODY = '''We just opened the Hevolve commercial API.

Endpoints:
 • POST /api/v1/intelligence/chat
 • POST /api/v1/intelligence/analyze
 • POST /api/v1/intelligence/generate
 • GET  /api/v1/intelligence/hivemind

Free tier (100 req/day) is free forever. No credit card. No 14-day clock.

Where the money goes:
 – 90% to the compute providers
 – 9% to infrastructure
 – 1% to us

The split is committed in code, not policy. Verify in our open-source HARTOS repo.

Docs: {landing}
Or run the same intelligence locally for free: {download}'''


def _linkedin_share(landing_url: str) -> str:
    """LinkedIn doesn't accept body via URL — only the share-offsite link."""
    return ('https://www.linkedin.com/sharing/share-offsite/?'
            f'url={quote(landing_url)}')


# ─── Reddit ────────────────────────────────────────────────────────


def _reddit_submit(subreddit: str, title: str, url: str) -> str:
    return (f'https://www.reddit.com/r/{subreddit}/submit?'
            f'type=LINK&title={quote(title)}&url={quote(url)}')


# ─── Facebook ──────────────────────────────────────────────────────


def _facebook_share(landing_url: str) -> str:
    """Facebook's share dialog — link only, like LinkedIn's.

    sharer.php takes NO body/quote parameter any more (the `quote=` field was
    removed in 2017 and is silently dropped), so the composer opens empty and
    the agent types body_text into it.
    """
    return f'https://www.facebook.com/sharer/sharer.php?u={quote(landing_url)}'


# ─── Instagram ─────────────────────────────────────────────────────


_INSTAGRAM_COMPOSER = 'https://www.instagram.com/'
"""Instagram has NO share-intent URL at all — not for the link, not for the
caption. The web composer is only reachable by clicking Create in the logged-in
UI, which is precisely the kind of step the VLM loop exists to do: it can see
the page and click through. So the intent URL is just the site root, and
`notes` carries the rest of the instruction.

NOT covered by channels/extensions/instagram_adapter.py — that adapter is
Instagram *Direct Messages* over the Meta Graph API (DMs, ice breakers,
quick replies) and needs Graph credentials. It cannot publish a feed post.
"""


# ─── WhatsApp ──────────────────────────────────────────────────────


_WA_BROADCAST_BODY = '''Hey 👋 — quick share, not a sales pitch.

We just opened the free tier of our AI API. 100 requests/day, free
forever, no credit card. Built on top of HARTOS (open source) +
Nunba (the desktop client you can also run locally for free).

90% of every paid dollar goes back to the people contributing
compute — split is enforced in code, not policy.

If you build with LLMs, free tier might cover what you need:
{landing}'''


# ─── Canonical registry ────────────────────────────────────────────


def _build_intents() -> List[MarketingIntent]:
    """Construct the canonical intent set.

    Lazy build so url-encoding happens once per process; if a future
    landing URL changes, the helpers above are the single edit point.
    """
    out: List[MarketingIntent] = []

    # Twitter 5 variants — each carries a distinct ref-tag so the
    # /marketing/stats endpoint shows which hook converts.
    for code, body in [
        ('tw1', _TW1_BODY),
    ]:
        landing = _with_ref(_COMMERCIAL_API_DOCS, code)
        out.append(MarketingIntent(
            platform='twitter', code=code,
            intent_url=_twitter_intent(code, body, landing),
            landing_url=landing, body_text=body + landing,
            notes='280-char composer pre-filled; click + Tweet.'))

    # LinkedIn launch announcement
    li_landing = _with_ref(_DOWNLOAD, 'li_a')
    out.append(MarketingIntent(
        platform='linkedin', code='li_a',
        intent_url=_linkedin_share(li_landing),
        landing_url=li_landing,
        body_text=_LI_A_BODY.format(landing=li_landing, download=_DOWNLOAD),
        notes=('LinkedIn URL only carries the link; paste body_text '
               'into the composer that opens.')))

    # Hacker News Show HN
    hn_landing = _with_ref(_COMMERCIAL_API_DOCS, 'hn_show')
    out.append(MarketingIntent(
        platform='hackernews', code='hn_show',
        intent_url='https://news.ycombinator.com/submit',
        landing_url=hn_landing,
        title=('Show HN: Hevolve Commercial API – '
               'free tier forever, 90% of paid revenue to compute providers'),
        notes=('HN submit page does not accept pre-filled URL via '
               'query string. Paste title + landing_url manually.')))

    # Reddit (one variant; add more subreddits by appending entries)
    rd_landing = _with_ref(_COMMERCIAL_API_DOCS, 'rd_localllama')
    out.append(MarketingIntent(
        platform='reddit', code='rd_localllama',
        intent_url=_reddit_submit(
            'LocalLLaMA',
            ('[Self-promo] We open-sourced HARTOS + added a hosted '
             'API tier — 90% of paid revenue to compute providers'),
            rd_landing),
        landing_url=rd_landing,
        notes='Reddit accepts type=LINK + title via URL params.'))

    # Facebook — share dialog opens with the link attached, body typed in.
    fb_landing = _with_ref(_DOWNLOAD, 'fb_a')
    out.append(MarketingIntent(
        platform='facebook', code='fb_a',
        intent_url=_facebook_share(fb_landing),
        landing_url=fb_landing,
        body_text=_LI_A_BODY.format(landing=fb_landing, download=_DOWNLOAD),
        notes=('sharer.php carries the LINK only — it ignores any quote/body '
               'param. The composer opens empty; paste body_text into it.')))

    # Instagram — no intent URL exists; the agent opens the site and drives
    # the Create flow visually.  Feed posts need an image, so the caller must
    # pass one (browser_poster puts image URLs in the instruction).
    ig_landing = _with_ref(_DOWNLOAD, 'ig_a')
    out.append(MarketingIntent(
        platform='instagram', code='ig_a',
        intent_url=_INSTAGRAM_COMPOSER,
        landing_url=ig_landing,
        body_text=_WA_BROADCAST_BODY.format(landing=ig_landing),
        notes=('No share-intent URL exists on Instagram. Opens the logged-in '
               'site; the agent must click Create, attach the image, then type '
               'the caption. Instagram captions are not clickable, so the '
               'landing URL belongs in the bio, not the caption. '
               'instagram_adapter.py is DM-only and cannot post to the feed.')))

    # WhatsApp Business broadcast — no URL composer; agent dispatches
    # the body via whatsapp_adapter to a curated contact list
    wa_landing = _with_ref(_DOWNLOAD, 'wa_broadcast')
    out.append(MarketingIntent(
        platform='whatsapp', code='wa_broadcast',
        intent_url='',  # no URL — handled by whatsapp_adapter
        landing_url=wa_landing,
        body_text=_WA_BROADCAST_BODY.format(landing=wa_landing),
        notes=('No intent URL — agent sends body_text via '
               'integrations/channels/whatsapp_adapter.py to the '
               'pre-authorized contact list.')))

    return out


INTENTS: List[MarketingIntent] = _build_intents()
INTENTS_BY_PLATFORM = {}
for _intent in INTENTS:
    INTENTS_BY_PLATFORM.setdefault(_intent.platform, []).append(_intent)


def list_platforms() -> List[str]:
    """Return platforms that have at least one canonical intent."""
    return sorted(INTENTS_BY_PLATFORM.keys())


def get_intents(platform: Optional[str] = None) -> List[MarketingIntent]:
    """Return intents for one platform (or all when platform is None)."""
    if platform:
        return list(INTENTS_BY_PLATFORM.get(platform.lower(), ()))
    return list(INTENTS)
