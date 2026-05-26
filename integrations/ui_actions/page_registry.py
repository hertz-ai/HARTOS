"""page_registry — single source of truth for Nunba/Hevolve app pages.

Each PageEntry names one destination that the LiquidUI action bar can
surface, plus the phrases that should route to it. The backend owns this
registry so the LangChain agent can see the full set of destinations as
part of its tool context — saying 'take me to the model manager' lands
on /admin/models deterministically instead of devolving into chitchat.

Adding a new page:
    1. Add a PageEntry below with a stable `id` (used as the external key).
    2. List the route + descriptive label + short description.
    3. Fill `keywords` with the verbs/nouns a user might speak when
       trying to reach this page. Keep them lowercase and distinctive.

The frontend mirrors this registry (same ids + routes) in
`landing-page/src/config/pageRegistry.js`. Keep the two in sync when
you add or rename entries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional


@dataclass(frozen=True)
class PageEntry:
    """One destination the LiquidUI action bar can surface."""

    id: str                  # stable key shared with frontend
    label: str               # short button label (≤20 chars)
    route: str               # SPA route path
    description: str         # one-line description for the LLM + tooltip
    keywords: frozenset      # lowercase tokens that trigger the match
    icon: str = 'open_in_new'  # MUI icon name, optional
    requires_role: Optional[str] = None  # 'central'|'regional'|None
    category: str = 'general'


def _ks(*words: str) -> frozenset:
    """frozenset of lowercase keywords for cleaner literal tables."""
    return frozenset(w.lower() for w in words)


PAGE_REGISTRY: tuple[PageEntry, ...] = (
    # ── Social ──────────────────────────────────────────────────────
    PageEntry(
        id='social_feed',
        label='Social Hub',
        route='/social',
        description='Main social feed — posts, comments, discussions, votes.',
        keywords=_ks('social', 'feed', 'posts', 'discussions', 'community',
                     'home', 'timeline'),
        icon='forum',
        category='social',
    ),
    PageEntry(
        id='agent_chat',
        label='Agent Chat',
        route='/local',
        description='Chat with the local AI agent (this screen).',
        keywords=_ks('chat', 'agent', 'talk', 'assistant', 'local'),
        icon='chat',
        category='chat',
    ),
    PageEntry(
        id='games',
        label='Games',
        route='/social/games',
        description='Play kids learning games and co-op puzzles.',
        keywords=_ks('game', 'games', 'play', 'puzzle', 'learning'),
        icon='sports_esports',
        category='play',
    ),
    PageEntry(
        id='kids',
        label='Kids Learning',
        route='/social/kids',
        description='Kids learning activities and educational content.',
        keywords=_ks('kids', 'children', 'learning', 'education', 'learn'),
        icon='school',
        category='play',
    ),
    PageEntry(
        id='marketplace',
        label='Marketplace',
        route='/social/marketplace',
        description='Browse and install agents, skills, and experiences.',
        keywords=_ks('marketplace', 'market', 'store', 'shop', 'browse',
                     'install', 'discover'),
        icon='storefront',
        category='discover',
    ),
    PageEntry(
        id='mcp_tools',
        label='MCP Tools',
        route='/social/tools',
        description='Browse and configure MCP (Model Context Protocol) tools.',
        keywords=_ks('mcp', 'tools', 'integrations', 'plugins'),
        icon='extension',
        category='discover',
    ),
    PageEntry(
        id='autopilot',
        label='Autopilot',
        route='/social/autopilot',
        description='Configure autonomous agent workflows.',
        keywords=_ks('autopilot', 'workflow', 'automation', 'autonomous',
                     'pipeline'),
        icon='precision_manufacturing',
        category='agents',
    ),
    # ── Admin ───────────────────────────────────────────────────────
    PageEntry(
        id='admin_models',
        label='Model Management',
        route='/admin/models',
        description='Load, unload, download, and swap LLM/TTS/STT/VLM models.',
        keywords=_ks('models', 'model', 'management', 'llm', 'tts', 'stt',
                     'vlm', 'download', 'load', 'unload'),
        icon='memory',
        requires_role='central',
        category='admin',
    ),
    PageEntry(
        id='admin_channels',
        label='Channels',
        route='/admin/channels',
        description=(
            'Connect / manage messaging channels (WhatsApp, Telegram, '
            'Slack, Discord, etc.).'
        ),
        keywords=_ks('channels', 'channel', 'whatsapp', 'telegram', 'slack',
                     'discord', 'messaging', 'connect', 'integrations'),
        icon='hub',
        requires_role='central',
        category='admin',
    ),
    PageEntry(
        id='admin_providers',
        label='AI Providers',
        route='/admin/providers',
        description='Configure AI providers (OpenAI, Anthropic, local, etc.).',
        keywords=_ks('providers', 'provider', 'api', 'openai', 'anthropic',
                     'gateway', 'keys'),
        icon='cloud',
        requires_role='central',
        category='admin',
    ),
    PageEntry(
        id='admin_users',
        label='Users',
        route='/admin/users',
        description='Manage users, roles, permissions.',
        keywords=_ks('users', 'user', 'members', 'permissions', 'roles',
                     'admin'),
        icon='people',
        requires_role='central',
        category='admin',
    ),
    PageEntry(
        id='admin_home',
        label='Admin',
        route='/admin',
        description='Admin dashboard overview.',
        keywords=_ks('admin', 'dashboard', 'settings', 'configuration'),
        icon='admin_panel_settings',
        requires_role='central',
        category='admin',
    ),
)

_BY_ID: dict[str, PageEntry] = {p.id: p for p in PAGE_REGISTRY}


def list_pages(
    user_role: str = 'flat',
    category: Optional[str] = None,
) -> list[PageEntry]:
    """Return pages visible to the given user role, optionally filtered.

    Role gate: 'central' > 'regional' > 'flat' > 'guest'. A page that
    requires 'central' is hidden from 'flat'/'guest' users so we don't
    surface actions the frontend can't execute.
    """
    _order = {'guest': 0, 'flat': 1, 'regional': 2, 'central': 3}
    user_rank = _order.get(user_role, 1)
    out: list[PageEntry] = []
    for p in PAGE_REGISTRY:
        if p.requires_role:
            needed = _order.get(p.requires_role, 3)
            if user_rank < needed:
                continue
        if category and p.category != category:
            continue
        out.append(p)
    return out


def resolve_page(
    query: str,
    user_role: str = 'flat',
    top_k: int = 3,
) -> list[tuple[PageEntry, int]]:
    """Score pages against a natural-language query.

    Lightweight lexical ranker: +3 for each keyword hit, +5 if the
    page label (lowercased) is a substring of the query, +2 for id
    match. No ML, no embedding calls — this runs on every message so
    it has to be cheap and deterministic.

    Returns up to `top_k` (PageEntry, score) pairs with score > 0,
    sorted by score descending. Pages the user can't access are filtered.
    """
    if not query:
        return []
    q = query.lower()
    visible = {p.id for p in list_pages(user_role=user_role)}
    scored: list[tuple[PageEntry, int]] = []
    for page in PAGE_REGISTRY:
        if page.id not in visible:
            continue
        score = 0
        if page.label.lower() in q:
            score += 5
        if page.id.lower() in q:
            score += 2
        for kw in page.keywords:
            if kw in q:
                score += 3
        if score > 0:
            scored.append((page, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:top_k]


def page_to_ui_action(page: PageEntry) -> dict:
    """Serialize a PageEntry as a ui_action dict for the chat response.

    The frontend's LiquidActionBar consumes this exact shape, so keep
    the keys stable or update both sides together.
    """
    return {
        'id': page.id,
        'type': 'navigate',
        'label': page.label,
        'route': page.route,
        'icon': page.icon,
        'description': page.description,
        'category': page.category,
    }
