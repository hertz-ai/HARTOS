"""
Publish tools — let an agent COMPOSE a social post, and only a person send it.

WHY THIS EXISTS

instagram_adapter.py has had publish_photo() and publish_carousel() for a
while. Nothing outside tests/unit/test_instagram_publishing.py has ever called
either one. Measured 2026-08-20: no tool, no route, no goal, no caller. So the
capability was written, tested, and unreachable, and the obvious next move was
to build browser automation alongside it -- a second path to a thing that
already worked.

The reason it was unreachable is structural. Fifteen register_*_tools families
exist. reuse_recipe.py, which is the runtime a channel conversation reaches
through /chat, wires core, memory and channel tools, plus marketing and
ip_protection behind detect_goal_tags. Nine families are wired nowhere on that
path, and there was no publish family at all. This adds one and registers it
beside the channel tools.

UNGATED, DELIBERATELY

Not behind detect_goal_tags. That gate keyword-matches the prompt for 'market',
'campaign', 'viral' and friends, so a family behind it is reachable only when
the wording happens to match. register_news_tools was orphaned by exactly that.
An agent asked "post this to Instagram" should be able to, without having to
say a magic word first.

STAGING, NOT SENDING

The agent can compose and stage. It cannot publish. There is no publish tool in
this file and that is the point, not an omission:

  - Posting is outward-facing and irreversible. A wrong post is not a wrong
    answer; it is a wrong answer other people can screenshot.
  - The standing rule on this project is that agent-authored public content is
    disclosed and operator-gated. A tool the agent can call is neither.
  - It keeps the account safe. Meta bans accounts for automated posting, and a
    human clicking send in a real session is not automation.

Sending is a separate, human-triggered action. When that endpoint exists it
should read this queue; it must not import a function from here that posts.

DISCLOSURE IS NOT OPTIONAL

Every staged post carries authored_by='agent'. The sending path is expected to
keep it. An agent-written post that presents as hand-written is the one thing
this project has repeatedly refused to build.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Platforms a post may be staged for. An adapter existing is not the same as
#: publishing being implemented on it, so this list is the ones whose adapter
#: actually has a publish path today.
SUPPORTED_PLATFORMS = ("instagram",)

#: Instagram's own limits, mirrored from instagram_adapter so a post is
#: rejected while a person can still fix it rather than at Meta's API.
CAPTION_MAX_CHARS = 2200
CAROUSEL_MIN_SLIDES = 2
CAROUSEL_MAX_SLIDES = 10


def _staging_path() -> str:
    """Where staged posts live.

    Under get_data_dir() so it inherits NUNBA_DATA_DIR and survives a container
    rebuild. The marketing funnel learned this the hard way: it wrote to the
    image's writable layer and lost 29 events on a deploy.
    """
    try:
        from core.platform_paths import get_data_dir
        data_dir = get_data_dir()
    except Exception:
        data_dir = os.path.expanduser("~/.hartos")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "staged_posts.jsonl")


def _append(row: Dict[str, Any]) -> None:
    with open(_staging_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def read_staged_posts(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every staged post, newest last. Public so an operator UI can read it.

    Deliberately a plain reader: the approval surface owns what happens next,
    and nothing in this module may send.
    """
    path = _staging_path()
    if not os.path.exists(path):
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if status and row.get("status") != status:
                continue
            rows.append(row)
    return rows


def build_publish_tool_closures(ctx: Dict[str, Any]) -> List[tuple]:
    """Session-scoped publish tools.

    Returns list of (name, description, func), the same shape
    core/agent_tools.py and integrations/channels/agent_tools.py use.
    """
    ctx = ctx or {}
    user_id = ctx.get("user_id")
    log_tool_execution = ctx.get("log_tool_execution") or (lambda f: f)

    tools: List[tuple] = []

    @log_tool_execution
    def stage_social_post(
        platform: str,
        caption: str,
        image_urls: str = "",
        source_note: str = "",
    ) -> str:
        """Stage a social post for a person to review and send.

        This does NOT publish. It puts the post in a queue that a human
        approves. Say so to the user rather than implying it went out.

        Args:
            platform: one of: instagram
            caption: the post text. Instagram allows 2200 characters.
            image_urls: comma-separated image URLs. One for a single photo,
                two to ten for a carousel. Each must be a public https URL
                that the platform's servers can fetch.
            source_note: where the claims come from. Include it for anything
                factual; a post that cites nothing should not be staged.

        Returns:
            A human-readable confirmation with the staged post id.
        """
        platform = (platform or "").strip().lower()
        if platform not in SUPPORTED_PLATFORMS:
            return (
                f"Cannot stage for '{platform}'. Publishing is implemented for: "
                f"{', '.join(SUPPORTED_PLATFORMS)}. Other adapters can send "
                f"messages but have no publish path."
            )

        caption = (caption or "").strip()
        if not caption:
            return "Cannot stage an empty caption."
        if len(caption) > CAPTION_MAX_CHARS:
            return (
                f"Caption is {len(caption)} characters; {platform} allows "
                f"{CAPTION_MAX_CHARS}. Shorten it and stage again."
            )

        urls = [u.strip() for u in (image_urls or "").split(",") if u.strip()]
        for u in urls:
            if not u.startswith("https://"):
                return (
                    f"Image URL '{u[:60]}' is not https. The platform fetches "
                    f"images from a public URL, so a local path or an http link "
                    f"cannot work."
                )
        if len(urls) > CAROUSEL_MAX_SLIDES:
            return (
                f"{len(urls)} images given; a carousel holds at most "
                f"{CAROUSEL_MAX_SLIDES}."
            )

        post_id = str(uuid.uuid4())
        _append({
            "id": post_id,
            "status": "staged",
            "platform": platform,
            "caption": caption,
            "image_urls": urls,
            "source_note": (source_note or "").strip(),
            # Never omitted. An agent-written post that presents as
            # hand-written is the thing this project refuses to build.
            "authored_by": "agent",
            "staged_by_user_id": user_id,
            "staged_at": datetime.now(timezone.utc).isoformat(),
        })
        logger.info("staged %s post %s (%d image(s))", platform, post_id, len(urls))

        kind = (
            "carousel" if len(urls) >= CAROUSEL_MIN_SLIDES
            else "photo" if urls else "text-only"
        )
        warn = (
            ""
            if urls
            else " Note: no images, and Instagram requires at least one, so this "
                 "cannot be sent as-is."
        )
        cite = "" if (source_note or "").strip() else (
            " No source was given; add one before a person reviews it."
        )
        return (
            f"Staged a {kind} post for {platform} as {post_id}. It is NOT "
            f"published: a person has to review and send it.{warn}{cite}"
        )

    tools.append((
        "stage_social_post",
        "Stage a social media post for human review. Does NOT publish it. "
        "Use for Instagram posts; provide caption, comma-separated https image "
        "URLs, and a source note for factual claims.",
        stage_social_post,
    ))

    @log_tool_execution
    def list_staged_posts(status: str = "staged") -> str:
        """List posts waiting for a person to review.

        Args:
            status: 'staged' (default), or 'all' for every status.
        """
        rows = read_staged_posts(None if status == "all" else status)
        if not rows:
            return "No staged posts."
        lines = []
        for r in rows[-20:]:
            lines.append(
                f"{r.get('id', '?')[:8]}  {r.get('platform', '?')}  "
                f"{len(r.get('image_urls') or [])} image(s)  "
                f"{(r.get('caption') or '')[:70]}"
            )
        return f"{len(rows)} post(s):\n" + "\n".join(lines)

    tools.append((
        "list_staged_posts",
        "List social posts staged and waiting for a person to review and send.",
        list_staged_posts,
    ))

    return tools


def register_publish_tools(helper, executor, ctx: Optional[Dict[str, Any]] = None) -> None:
    """Register publish tools on an AutoGen helper/executor pair.

    Same idiom as register_channel_tools: build the closures, hand them to
    register_core_tools, which owns the autogen binding.
    """
    tools = build_publish_tool_closures(ctx or {})
    from core.agent_tools import register_core_tools
    register_core_tools(tools, helper, executor)
    logger.info("registered %d publish tool(s)", len(tools))
