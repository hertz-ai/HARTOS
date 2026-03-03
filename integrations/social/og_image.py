"""
HevolveSocial - OG Image Generator
Generates 1200x630 Open Graph preview images for shared content.
Uses PIL/Pillow for server-side image generation with text overlay.
"""
import hashlib
import logging
import os
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger('hevolve_social')

# Cache directory for generated OG images
OG_CACHE_DIR = Path(os.path.expanduser('~/Documents/Nunba/data/og-cache'))

# Image dimensions (Facebook/LinkedIn recommended)
WIDTH = 1200
HEIGHT = 630

# Colors (Nunba/Hevolve palette)
BG_COLOR_TOP = (15, 14, 23)       # #0F0E17
BG_COLOR_BOTTOM = (30, 28, 50)    # dark purple gradient end
ACCENT_COLOR = (108, 99, 255)     # #6C63FF
TEXT_COLOR = (255, 255, 255)      # white
SUBTEXT_COLOR = (180, 180, 200)   # muted


def _ensure_cache_dir():
    OG_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(resource_type, resource_id):
    key = hashlib.md5(f'{resource_type}:{resource_id}'.encode()).hexdigest()
    return OG_CACHE_DIR / f'og_{key}.png'


def _is_cache_fresh(path, max_age_hours=1):
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return datetime.now() - mtime < timedelta(hours=max_age_hours)


def generate_og_image(resource_type, resource_id):
    """Generate an OG preview image. Returns file path or None."""
    _ensure_cache_dir()
    cache_file = _cache_path(resource_type, resource_id)

    # Return cached if fresh
    if _is_cache_fresh(cache_file):
        return str(cache_file)

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.debug("Pillow not installed, skipping OG image generation")
        return None

    # Fetch resource data
    title, description, avatar_text = _fetch_resource_info(resource_type, resource_id)

    # Create image with gradient background
    img = Image.new('RGB', (WIDTH, HEIGHT), BG_COLOR_TOP)
    draw = ImageDraw.Draw(img)

    # Draw gradient background
    for y in range(HEIGHT):
        ratio = y / HEIGHT
        r = int(BG_COLOR_TOP[0] + (BG_COLOR_BOTTOM[0] - BG_COLOR_TOP[0]) * ratio)
        g_val = int(BG_COLOR_TOP[1] + (BG_COLOR_BOTTOM[1] - BG_COLOR_TOP[1]) * ratio)
        b = int(BG_COLOR_TOP[2] + (BG_COLOR_BOTTOM[2] - BG_COLOR_TOP[2]) * ratio)
        draw.line([(0, y), (WIDTH, y)], fill=(r, g_val, b))

    # Accent bar at top
    draw.rectangle([(0, 0), (WIDTH, 6)], fill=ACCENT_COLOR)

    # Try to load a font, fall back to default
    title_font = _get_font(36)
    desc_font = _get_font(22)
    brand_font = _get_font(18)

    # Draw resource type badge
    badge_text = resource_type.replace('_', ' ').upper()
    badge_w = draw.textlength(badge_text, font=brand_font) + 24 if brand_font else len(badge_text) * 10 + 24
    draw.rounded_rectangle(
        [(60, 80), (60 + badge_w, 116)],
        radius=6, fill=ACCENT_COLOR,
    )
    draw.text((72, 86), badge_text, fill=TEXT_COLOR, font=brand_font)

    # Draw title (wrapped)
    title_lines = textwrap.wrap(title, width=45)
    y_pos = 140
    for line in title_lines[:3]:
        draw.text((60, y_pos), line, fill=TEXT_COLOR, font=title_font)
        y_pos += 48

    # Draw description (wrapped)
    if description:
        desc_lines = textwrap.wrap(description, width=70)
        y_pos += 16
        for line in desc_lines[:3]:
            draw.text((60, y_pos), line, fill=SUBTEXT_COLOR, font=desc_font)
            y_pos += 32

    # Draw avatar circle placeholder
    if avatar_text:
        cx, cy, r = WIDTH - 160, HEIGHT // 2, 60
        draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=ACCENT_COLOR)
        # Center initial in circle
        initial = avatar_text[0].upper()
        init_font = _get_font(40)
        if init_font:
            bbox = draw.textbbox((0, 0), initial, font=init_font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            draw.text((cx - tw // 2, cy - th // 2 - 4), initial,
                      fill=TEXT_COLOR, font=init_font)

    # Brand footer
    draw.rectangle([(0, HEIGHT - 50), (WIDTH, HEIGHT)], fill=(10, 10, 18))
    draw.text((60, HEIGHT - 40), 'NUNBA', fill=ACCENT_COLOR, font=brand_font)
    draw.text((160, HEIGHT - 40), 'A community for humans & AI agents',
              fill=SUBTEXT_COLOR, font=brand_font)

    # Save
    img.save(str(cache_file), 'PNG', optimize=True)
    return str(cache_file)


def _fetch_resource_info(resource_type, resource_id):
    """Fetch title, description, avatar_text for a resource from DB."""
    title = 'Shared Content'
    description = ''
    avatar_text = ''

    try:
        from .models import get_db, Post, Comment, User, Community
        db = get_db()
        try:
            if resource_type == 'post':
                post = db.query(Post).filter_by(id=resource_id).first()
                if post:
                    title = (post.content or '')[:100].strip() or 'Thought Experiment'
                    description = (post.content or '')[100:300].strip()
                    if post.author_id:
                        author = db.query(User).filter_by(id=post.author_id).first()
                        avatar_text = (author.display_name or author.username) if author else ''

            elif resource_type == 'profile':
                user = db.query(User).filter_by(id=resource_id).first()
                if user:
                    title = user.display_name or user.username
                    description = user.bio or 'Member of Nunba community'
                    avatar_text = title

            elif resource_type == 'community':
                comm = db.query(Community).filter_by(id=resource_id).first()
                if comm:
                    title = f'h/{comm.name}'
                    description = comm.description or f'Join the {comm.name} community'

            elif resource_type == 'comment':
                comment = db.query(Comment).filter_by(id=resource_id).first()
                if comment:
                    title = f'Comment: {(comment.content or "")[:80]}'
                    description = (comment.content or '')[:200]

            else:
                title = f'{resource_type.replace("_", " ").title()}'
                description = f'Check out this {resource_type.replace("_", " ")} on Nunba'
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"OG image resource fetch failed: {e}")

    return title, description, avatar_text


def _get_font(size):
    """Try to load a TrueType font, fall back to default."""
    try:
        from PIL import ImageFont
        # Try common system font paths
        font_paths = [
            'C:/Windows/Fonts/segoeui.ttf',
            'C:/Windows/Fonts/arial.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/TTF/DejaVuSans.ttf',
            '/System/Library/Fonts/Helvetica.ttc',
        ]
        for fp in font_paths:
            if os.path.exists(fp):
                return ImageFont.truetype(fp, size)
        return ImageFont.load_default()
    except Exception:
        return None
