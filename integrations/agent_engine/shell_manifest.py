"""
HART OS Glass Desktop Shell — Panel Manifest.

Defines all panels available in the glass desktop shell:
- PANEL_MANIFEST: Static panels from Nunba SPA (start menu items)
- DYNAMIC_PANELS: Context-opened panels (profile, post detail, etc.)
- SYSTEM_PANELS: Native system management panels (hardware, security, etc.)

Each panel can float as a draggable/resizable frosted glass window.
Nunba panels render via iframe to /app/#/<route>.
System panels render natively from backend API data.

Launchable-microfrontend entry schema (the SAME shape consumed by
liquid_ui_service.buildStartMenu / openPanel / filterStart — do NOT invent a
second registry):

    PANEL_MANIFEST[id] = {
        'title':        str,   # human label; the searched field (filterStart
                               #   matches data-title; omnibox 'open X' matches
                               #   title OR the id key). Put the intuitive name
                               #   here so the page is DISCOVERABLE.
        'icon':         str,   # Material Symbols glyph name (colour resolved by
                               #   with_icon_colors -> color_for, single source).
        'route':        str,   # Nunba BrowserRouter path; opened in an iframe
                               #   panel at NUNBA_BASE + route. MUST be a real
                               #   /app route (a wrong route lands on NotFound).
        'group':        str,   # one of PANEL_GROUPS -> start-menu section.
        'default_size': [w, h],# 2-element float geometry (invariant-checked).
        'floating':     bool,  # optional; render as a floating bubble.
    }

Discoverability (the intuitive rule): a page is findable when an intuitive
name a user would type is a substring of its 'title' (start search) or its id
(omnibox 'open <name>'). When a real route already exists under a different
brand label (e.g. Resonance is the karma/reputation surface, /social/resonance),
DO NOT add a second entry for the same route (that is a parallel path) - widen
the existing entry's 'title' so both intuitive names resolve to the one route.

System panels (SYSTEM_PANELS) carry 'apis' instead of 'route' and render via a
bespoke loader in liquid_ui_service (an id with no loader renders a placeholder),
so a native surface is registered by REUSING an existing system panel + its
working renderer, never by inventing a new id with no JS.
"""

import os
import re

# ═══════════════════════════════════════════════════════════════
# Static Panels — Nunba SPA pages shown in Start Menu
# ═══════════════════════════════════════════════════════════════

PANEL_MANIFEST = {
    # ─── Discover ───
    'feed': {
        'title': 'Feed', 'icon': 'rss_feed',
        'route': '/social', 'group': 'Discover',
        'default_size': [800, 600],
    },
    'search': {
        'title': 'Search', 'icon': 'search',
        'route': '/social/search', 'group': 'Discover',
        'default_size': [600, 500],
    },
    'agents_browse': {
        'title': 'Agents', 'icon': 'smart_toy',
        'route': '/agents', 'group': 'Discover',
        'default_size': [900, 700],
    },

    # ─── Create ───
    'communities': {
        'title': 'Communities', 'icon': 'groups',
        'route': '/social/communities', 'group': 'Create',
        'default_size': [800, 600],
    },
    'campaigns': {
        'title': 'Campaigns', 'icon': 'campaign',
        'route': '/social/campaigns', 'group': 'Create',
        'default_size': [800, 600],
    },
    'coding': {
        'title': 'Coding Agent', 'icon': 'code',
        'route': '/social/coding', 'group': 'Create',
        'default_size': [900, 700],
    },
    'tracker': {
        'title': 'Tracker', 'icon': 'science',
        'route': '/social/tracker', 'group': 'Create',
        'default_size': [800, 600],
    },
    'agent_audit': {
        'title': 'Agent Audit', 'icon': 'fact_check',
        'route': '/social/agents', 'group': 'Create',
        'default_size': [900, 600],
    },

    # ─── You ───
    # Resonance IS the karma / reputation / standing surface in Nunba (the
    # AccountBalanceWallet 'You' page). There is no separate /social/karma route,
    # so we widen this title instead of adding a duplicate 'karma' entry (which
    # would be a parallel path to the same route) - now both "resonance" and
    # "karma" resolve, in start search and the omnibox, to this one page.
    'resonance': {
        'title': 'Resonance & Karma', 'icon': 'auto_awesome',
        'route': '/social/resonance', 'group': 'You',
        'default_size': [700, 500],
    },
    'regions': {
        'title': 'Regions', 'icon': 'public',
        'route': '/social/regions', 'group': 'You',
        'default_size': [800, 600],
    },
    'encounters': {
        'title': 'Encounters', 'icon': 'handshake',
        'route': '/social/encounters', 'group': 'You',
        'default_size': [700, 500],
    },
    'autopilot': {
        'title': 'Autopilot', 'icon': 'rocket_launch',
        'route': '/social/autopilot', 'group': 'You',
        'default_size': [800, 600],
    },
    'notifications': {
        'title': 'Notifications', 'icon': 'notifications',
        'route': '/social/notifications', 'group': 'You',
        'default_size': [500, 600],
    },
    # ── Settings family (/social/settings/*) ──
    # Nunba has no settings index route, only these three sub-pages. The shared
    # "Settings" token in each title makes the whole family surface when a user
    # types "settings" (filterStart matches title), while each stays findable by
    # its own word - no umbrella entry needed, no duplicate route.
    'backup': {
        'title': 'Backup & Sync Settings', 'icon': 'cloud_sync',
        'route': '/social/settings/backup', 'group': 'You',
        'default_size': [600, 500],
    },
    'appearance': {
        'title': 'Appearance Settings', 'icon': 'palette',
        'route': '/social/settings/appearance', 'group': 'You',
        'default_size': [700, 600],
    },
    'privacy': {
        'title': 'Privacy Settings', 'icon': 'privacy_tip',
        'route': '/social/settings/privacy', 'group': 'You',
        'default_size': [700, 600],
    },

    # ─── Explore ───
    'recipes': {
        'title': 'Recipes', 'icon': 'menu_book',
        'route': '/social/recipes', 'group': 'Explore',
        'default_size': [800, 600],
    },
    'achievements': {
        'title': 'Achievements', 'icon': 'emoji_events',
        'route': '/social/achievements', 'group': 'Explore',
        'default_size': [700, 500],
    },
    'challenges': {
        'title': 'Challenges', 'icon': 'bolt',
        'route': '/social/challenges', 'group': 'Explore',
        'default_size': [700, 500],
    },
    'hive_contest': {
        'title': 'Hive Contest', 'icon': 'leaderboard',
        'route': '/hive-contest', 'group': 'Explore',
        'default_size': [1000, 760],
    },
    'kids': {
        'title': 'Kids Learning', 'icon': 'child_care',
        'route': '/social/kids', 'group': 'Explore',
        'default_size': [900, 700],
    },
    'seasons': {
        'title': 'Seasons', 'icon': 'park',
        'route': '/social/seasons', 'group': 'Explore',
        'default_size': [700, 500],
    },

    # ─── Manage (Admin) ───
    'admin': {
        'title': 'Admin Dashboard', 'icon': 'dashboard',
        'route': '/admin', 'group': 'Manage',
        'default_size': [900, 600],
    },
    'admin_users': {
        'title': 'Users', 'icon': 'person',
        'route': '/admin/users', 'group': 'Manage',
        'default_size': [800, 600],
    },
    'admin_mod': {
        'title': 'Moderation', 'icon': 'shield',
        'route': '/admin/moderation', 'group': 'Manage',
        'default_size': [800, 600],
    },
    'admin_agents': {
        'title': 'Agent Sync', 'icon': 'sync',
        'route': '/admin/agents', 'group': 'Manage',
        'default_size': [800, 600],
    },
    'admin_channels': {
        'title': 'Channels', 'icon': 'cell_tower',
        'route': '/admin/channels', 'group': 'Manage',
        'default_size': [800, 600],
    },
    'admin_workflows': {
        'title': 'Workflows', 'icon': 'build',
        'route': '/admin/workflows', 'group': 'Manage',
        'default_size': [800, 600],
    },
    'admin_settings': {
        'title': 'Settings', 'icon': 'settings',
        'route': '/admin/settings', 'group': 'Manage',
        'default_size': [700, 600],
    },
    'admin_identity': {
        'title': 'Identity', 'icon': 'vpn_key',
        'route': '/admin/identity', 'group': 'Manage',
        'default_size': [700, 500],
    },
    'admin_dashboard': {
        'title': 'Agent Dashboard', 'icon': 'monitoring',
        'route': '/admin/agent-dashboard', 'group': 'Manage',
        'default_size': [900, 700],
    },
    'admin_revenue': {
        'title': 'Revenue', 'icon': 'payments',
        'route': '/admin/revenue', 'group': 'Manage',
        'default_size': [800, 600],
    },
    'admin_tasks': {
        'title': 'Content Tasks', 'icon': 'task',
        'route': '/admin/content-tasks', 'group': 'Manage',
        'default_size': [800, 600],
    },

    # ─── Assistant (floating chat bubble) ───
    'assistant': {
        'title': 'Assistant', 'icon': 'chat_bubble',
        'route': '/social/assistant', 'group': 'Discover',
        'default_size': [400, 600],
        'floating': True,
    },

    # ─── OpenClaw Skills ───
    'openclaw_skills': {
        'title': 'OpenClaw Skills', 'icon': 'extension',
        'route': '/social/openclaw', 'group': 'Create',
        'default_size': [800, 600],
    },
}


# ═══════════════════════════════════════════════════════════════
# Dynamic Panels — Opened from context (links, agent actions)
# ═══════════════════════════════════════════════════════════════

DYNAMIC_PANELS = {
    'profile': {
        'title': 'Profile: {name}',
        'route': '/social/profile/{userId}',
        'default_size': [700, 600],
    },
    'post': {
        'title': 'Post',
        'route': '/social/post/{postId}',
        'default_size': [600, 700],
    },
    'community': {
        'title': '{name}',
        'route': '/social/h/{communityId}',
        'default_size': [800, 600],
    },
    'agent_profile': {
        'title': 'Agent: {name}',
        'route': '/social/agent/{agentId}',
        'default_size': [700, 600],
    },
    'agent_chat': {
        'title': 'Chat: {name}',
        'route': '/social/agent/{agentId}/chat',
        'default_size': [500, 700],
    },
    'agent_evolution': {
        'title': 'Evolution: {name}',
        'route': '/social/agents/{agentId}/evolution',
        'default_size': [800, 600],
    },
    'campaign_detail': {
        'title': 'Campaign: {name}',
        'route': '/social/campaigns/{campaignId}',
        'default_size': [800, 600],
    },
    'challenge_detail': {
        'title': 'Challenge: {name}',
        'route': '/social/challenges/{challengeId}',
        'default_size': [700, 500],
    },
    'region_detail': {
        'title': 'Region: {name}',
        'route': '/social/regions/{regionId}',
        'default_size': [800, 600],
    },
    'encounter_detail': {
        'title': 'Encounter',
        'route': '/social/encounters/{encounterId}',
        'default_size': [600, 500],
    },
    'kids_game': {
        'title': 'Game: {name}',
        'route': '/social/kids/game/{gameId}',
        'default_size': [900, 700],
    },
    'kids_progress': {
        'title': 'Kids Progress',
        'route': '/social/kids/progress',
        'default_size': [700, 500],
    },
    'kids_create': {
        'title': 'Game Creator',
        'route': '/social/kids/create',
        'default_size': [900, 700],
    },
    'campaign_studio': {
        'title': 'Campaign Studio',
        'route': '/social/campaigns/create',
        'default_size': [900, 700],
    },
}


# ═══════════════════════════════════════════════════════════════
# System Panels — Native OS management (rendered directly, no iframe)
# ═══════════════════════════════════════════════════════════════

SYSTEM_PANELS = {
    # Settings — NOT a new settings app. This is the aggregator surface: its
    # native renderer (liquid_ui_service.loadSettingsPanel) draws a categorized
    # index whose every tile OPENS AN EXISTING panel via openPanel (single-
    # instance reuse). The section->ids composition lives in SETTINGS_SECTIONS
    # (below), so there is no parallel settings implementation - just a composed
    # view of the registry. 'preferences'/'control panel' intuitive names surface
    # via the title token 'Settings'.
    'settings': {
        'title': 'Settings', 'icon': 'settings',
        'group': 'System', 'default_size': [860, 640],
        'apis': [],
    },
    'hw_monitor': {
        'title': 'Hardware Monitor', 'icon': 'monitor_heart',
        'group': 'System', 'default_size': [700, 500],
        'apis': [
            '/api/social/dashboard/system',
            '/api/social/node/capabilities',
        ],
    },
    'security': {
        'title': 'Security Center', 'icon': 'shield',
        'group': 'System', 'default_size': [700, 500],
        # image = the BUNDLED no-network brand poster (the offline default for the
        # desktop icon image-plate, hartDesktop.renderGlyphTile def.image; #153/GF4).
        'image': '/shell/static/app_art/app-security.svg',
        'apis': [
            '/api/social/dashboard/health',
            '/api/social/integrity/guardrail-hash',
        ],
    },
    'event_log': {
        'title': 'Event Log', 'icon': 'list_alt',
        'group': 'System', 'default_size': [800, 500],
        'apis': ['/api/shell/events'],
    },
    'drivers': {
        'title': 'Drivers & Devices', 'icon': 'devices',
        'group': 'System', 'default_size': [700, 500],
        'apis': ['/api/shell/drivers'],
    },
    'network': {
        'title': 'Network', 'icon': 'wifi',
        'group': 'System', 'default_size': [700, 500],
        'apis': [
            '/api/social/dashboard/topology',
            '/api/shell/network/wifi',
        ],
    },
    'audio': {
        'title': 'Audio', 'icon': 'volume_up',
        'group': 'System', 'default_size': [500, 400],
        'apis': ['/api/shell/audio'],
    },
    'bluetooth': {
        'title': 'Bluetooth', 'icon': 'bluetooth',
        'group': 'System', 'default_size': [500, 400],
        'apis': ['/api/shell/bluetooth'],
    },
    'power': {
        'title': 'Power', 'icon': 'battery_full',
        'group': 'System', 'default_size': [500, 400],
        'apis': ['/api/shell/power'],
    },
    'display': {
        'title': 'Display', 'icon': 'desktop_windows',
        'group': 'System', 'default_size': [600, 400],
        'apis': ['/api/shell/display'],
    },
    'flash': {
        'title': 'Flash HART OS to USB', 'icon': 'usb',
        'group': 'System', 'default_size': [640, 580],
        'apis': ['/api/shell/flash/disks'],
    },
    'remote_desktop': {
        'title': 'Remote Desktop', 'icon': 'connected_tv',
        'group': 'System', 'default_size': [800, 600],
        'apis': [
            '/api/remote-desktop/status',
            '/api/remote-desktop/engines',
            '/api/remote-desktop/sessions',
        ],
    },
    'file_manager': {
        'title': 'Files', 'icon': 'folder',
        'group': 'System', 'default_size': [800, 600],
        'image': '/shell/static/app_art/app-files.svg',
        'apis': ['/api/shell/files/browse', '/api/shell/files/recent'],
    },
    # This PC / My Computer — the drives + partitions launcher. NOT a second file
    # browser: its native renderer (liquid_ui_service.loadMyComputerPanel) lists
    # the partitions from the EXISTING /api/shell/storage read-op and hands each
    # drive off to the canonical File Explorer (openFilesAt -> file_manager) for
    # actual browsing. 'my computer' resolves via the id, 'this pc' via the title.
    'my_computer': {
        'title': 'This PC', 'icon': 'computer',
        'group': 'System', 'default_size': [640, 520],
        'apis': ['/api/shell/storage'],
    },
    'terminal': {
        'title': 'Terminal', 'icon': 'terminal',
        'group': 'System', 'default_size': [800, 500],
        'image': '/shell/static/app_art/app-terminal.svg',
        'apis': ['/api/shell/terminal/exec', '/api/shell/terminal/sessions'],
    },
    'user_accounts': {
        'title': 'User Accounts', 'icon': 'group',
        'group': 'System', 'default_size': [600, 500],
        'apis': ['/api/shell/users'],
    },
    'notification_center': {
        'title': 'Notification Center', 'icon': 'notifications_active',
        'group': 'System', 'default_size': [500, 600],
        'apis': ['/api/shell/notifications'],
    },
    'updates': {
        'title': 'Updates', 'icon': 'system_update',
        'group': 'System', 'default_size': [600, 400],
        'apis': ['/api/upgrades/status'],
    },
    'backup_restore': {
        'title': 'Backup & Restore', 'icon': 'backup',
        'group': 'System', 'default_size': [600, 500],
        'apis': ['/api/shell/backup/list', '/api/shell/backup/restore'],
    },
    'devices': {
        'title': 'Devices & Mesh', 'icon': 'devices_other',
        'group': 'System', 'default_size': [700, 500],
        'apis': ['/api/shell/devices'],
    },
    'i18n': {
        'title': 'Language & Region', 'icon': 'language',
        'group': 'System', 'default_size': [500, 400],
        'apis': ['/api/shell/i18n/locales', '/api/shell/i18n/strings'],
    },
    'accessibility': {
        'title': 'Accessibility', 'icon': 'accessibility',
        'group': 'System', 'default_size': [500, 500],
        'apis': ['/api/shell/accessibility'],
    },
    'screenshot': {
        'title': 'Screenshot & Recording', 'icon': 'screenshot_monitor',
        'group': 'System', 'default_size': [500, 400],
        'apis': ['/api/shell/screenshot', '/api/shell/recording/start'],
    },
    'firewall': {
        'title': 'Firewall & Firmware', 'icon': 'security',
        'group': 'System', 'default_size': [600, 500],
        'apis': ['/api/shell/power/profiles'],  # Uses power API for system status
    },

    # ─── Desktop Experience ───
    'default_apps': {
        'title': 'Default Apps', 'icon': 'open_in_browser',
        'group': 'System', 'default_size': [600, 500],
        'apis': ['/api/shell/default-apps'],
    },
    'font_manager': {
        'title': 'Fonts', 'icon': 'font_download',
        'group': 'System', 'default_size': [700, 500],
        'apis': ['/api/shell/fonts'],
    },
    'sound_manager': {
        'title': 'Sounds', 'icon': 'music_note',
        'group': 'System', 'default_size': [600, 500],
        'apis': ['/api/shell/sounds/themes', '/api/shell/sounds/events'],
    },
    'clipboard_manager': {
        'title': 'Clipboard', 'icon': 'content_paste',
        'group': 'System', 'default_size': [500, 600],
        'apis': ['/api/shell/clipboard/history'],
    },
    'datetime': {
        'title': 'Date & Time', 'icon': 'schedule',
        'group': 'System', 'default_size': [500, 400],
        'apis': ['/api/shell/datetime'],
    },
    'wallpaper_manager': {
        'title': 'Wallpaper', 'icon': 'wallpaper',
        'group': 'System', 'default_size': [800, 600],
        'apis': ['/api/shell/wallpaper', '/api/shell/wallpaper/collection'],
    },
    'input_methods': {
        'title': 'Keyboard & Input', 'icon': 'keyboard',
        'group': 'System', 'default_size': [600, 500],
        'apis': ['/api/shell/input-methods'],
    },
    'nightlight': {
        'title': 'Night Light', 'icon': 'nightlight',
        'group': 'System', 'default_size': [500, 400],
        'apis': ['/api/shell/nightlight'],
    },
    'workspaces': {
        'title': 'Workspaces', 'icon': 'view_carousel',
        'group': 'System', 'default_size': [700, 500],
        'apis': ['/api/shell/workspaces'],
    },

    # ─── System Management ───
    'task_manager': {
        'title': 'Task Manager', 'icon': 'monitoring',
        'group': 'System', 'default_size': [800, 600],
        'apis': ['/api/shell/tasks/processes', '/api/shell/tasks/resources'],
    },
    'storage_manager': {
        'title': 'Storage', 'icon': 'storage',
        'group': 'System', 'default_size': [700, 500],
        'apis': ['/api/shell/storage', '/api/shell/storage/cleanup'],
    },
    'startup_apps': {
        'title': 'Startup Apps', 'icon': 'play_circle',
        'group': 'System', 'default_size': [600, 500],
        'apis': ['/api/shell/startup'],
    },
    'bluetooth_manager': {
        'title': 'Bluetooth Manager', 'icon': 'bluetooth_connected',
        'group': 'System', 'default_size': [600, 500],
        'apis': ['/api/shell/bluetooth/status'],
    },
    'print_manager': {
        'title': 'Printers', 'icon': 'print',
        'group': 'System', 'default_size': [700, 500],
        'apis': ['/api/shell/printers'],
    },
    'media_library': {
        'title': 'Media Library', 'icon': 'perm_media',
        'group': 'System', 'default_size': [800, 600],
        'apis': ['/api/shell/media/status', '/api/shell/media/photos',
                 '/api/shell/media/music', '/api/shell/media/videos',
                 '/api/shell/media/play', '/api/shell/media/stop',
                 '/api/shell/media/player-status'],
    },
    # ─── OS Feature Panels ───
    'calculator': {
        'title': 'Calculator', 'icon': 'calculate',
        'group': 'System', 'default_size': [350, 500],
        'apis': [],
    },
    'image_viewer': {
        'title': 'Image Viewer', 'icon': 'photo',
        'group': 'System', 'default_size': [800, 600],
        'apis': ['/api/shell/files/browse'],
    },
    'notes_app': {
        'title': 'Notes', 'icon': 'sticky_note_2',
        'group': 'System', 'default_size': [600, 500],
        'apis': ['/api/shell/notes'],
    },
    'cloud_sync': {
        'title': 'Cloud Sync', 'icon': 'cloud_sync',
        'group': 'System', 'default_size': [700, 500],
        'apis': ['/api/shell/cloud-sync/remotes', '/api/shell/cloud-sync/pairs',
                 '/api/shell/cloud-sync/run', '/api/shell/cloud-sync/status'],
    },
    'app_store': {
        'title': 'App Store', 'icon': 'storefront',
        'group': 'System', 'default_size': [900, 700],
        'image': '/shell/static/app_art/app-store.svg',
        'apis': ['/api/apps/search', '/api/apps/installed',
                 '/api/apps/install', '/api/apps/uninstall'],
    },
    'app_permissions': {
        # Also the installed-apps REGISTRY: the native renderer lists every
        # installed app with an Uninstall control wired to app_installer.uninstall
        # (via /api/apps/uninstall). 'uninstall' + 'remove' in the title so both
        # intuitive names surface this one panel (no separate uninstall app).
        'title': 'App Permissions & Uninstall', 'icon': 'admin_panel_settings',
        'group': 'System', 'default_size': [700, 500],
        'apis': ['/api/apps/installed',
                 '/api/apps/{app_id}/permissions',
                 '/api/apps/{app_id}/permission/{type}',
                 '/api/apps/{app_id}/permissions/reset'],
    },
    'battery_monitor': {
        'title': 'Battery', 'icon': 'battery_full',
        'group': 'System', 'default_size': [400, 300],
        'apis': ['/api/shell/battery', '/api/shell/battery/profile'],
    },
    'wifi_manager': {
        'title': 'WiFi', 'icon': 'wifi',
        'group': 'System', 'default_size': [500, 400],
        'apis': ['/api/shell/wifi/networks', '/api/shell/wifi/status',
                 '/api/shell/wifi/connect', '/api/shell/wifi/disconnect',
                 '/api/shell/wifi/saved', '/api/shell/wifi/forget',
                 '/api/shell/wifi/toggle'],
    },
    'vpn_manager': {
        'title': 'VPN', 'icon': 'vpn_key',
        'group': 'System', 'default_size': [500, 400],
        'apis': ['/api/shell/vpn/list', '/api/shell/vpn/status',
                 '/api/shell/vpn/connect', '/api/shell/vpn/disconnect',
                 '/api/shell/vpn/import'],
    },
    'trash_bin': {
        'title': 'Trash', 'icon': 'delete',
        'group': 'System', 'default_size': [600, 500],
        'apis': ['/api/shell/trash', '/api/shell/trash/move',
                 '/api/shell/trash/restore', '/api/shell/trash/empty'],
    },
    'webcam_viewer': {
        'title': 'Camera', 'icon': 'videocam',
        'group': 'System', 'default_size': [640, 520],
        'apis': ['/api/shell/webcam/list'],
    },
    'scanner': {
        'title': 'Scanner', 'icon': 'scanner',
        'group': 'System', 'default_size': [600, 500],
        'apis': ['/api/shell/scanner/list', '/api/shell/scanner/scan'],
    },
    'weather_widget': {
        'title': 'Weather', 'icon': 'cloud',
        'group': 'System', 'default_size': [400, 350],
        'image': '/shell/static/app_art/app-weather.svg',
        'apis': ['/api/shell/weather'],
    },
    'file_tags': {
        'title': 'File Tags', 'icon': 'label',
        'group': 'System', 'default_size': [600, 500],
        'apis': ['/api/shell/files/tags', '/api/shell/files/search-by-tag'],
    },
    'hotspot': {
        'title': 'Hotspot', 'icon': 'wifi_tethering',
        'group': 'System', 'default_size': [400, 350],
        'apis': ['/api/shell/hotspot/status', '/api/shell/hotspot/start',
                 '/api/shell/hotspot/stop'],
    },
    'dns_settings': {
        'title': 'DNS Settings', 'icon': 'dns',
        'group': 'System', 'default_size': [500, 400],
        'apis': ['/api/shell/dns/status', '/api/shell/dns/set'],
    },
    'auto_update': {
        'title': 'Auto Update', 'icon': 'system_update',
        'group': 'System', 'default_size': [500, 400],
        'apis': ['/api/shell/auto-update/status', '/api/shell/auto-update/run'],
    },
    'sso_ldap': {
        'title': 'Enterprise Login', 'icon': 'domain',
        'group': 'System', 'default_size': [600, 500],
        'apis': ['/api/shell/sso/status', '/api/shell/sso/join',
                 '/api/shell/sso/leave', '/api/shell/sso/test'],
    },
    'email': {
        'title': 'Email', 'icon': 'email',
        'group': 'System', 'default_size': [800, 600],
        'apis': ['/api/shell/email/status', '/api/shell/email/launch'],
    },
    'voice_control': {
        'title': 'Voice Control', 'icon': 'mic',
        'group': 'System', 'default_size': [500, 400],
        'apis': ['/api/shell/voice/status', '/api/shell/voice/start',
                 '/api/shell/voice/stop', '/api/shell/voice/process'],
    },
    'screen_rotation': {
        'title': 'Screen Rotation', 'icon': 'screen_rotation',
        'group': 'System', 'default_size': [400, 350],
        'apis': ['/api/shell/display/rotation', '/api/shell/display/auto-rotate'],
    },
    'keyboard_shortcuts': {
        'title': 'Keyboard Shortcuts', 'icon': 'keyboard_command_key',
        'group': 'System', 'default_size': [700, 600],
        'apis': ['/api/shell/shortcuts'],
    },
    # The AI-setup-wizard. This is the existing "Light Your HART" onboarding
    # surface (loadHartIdentityPanel: when not onboarded it POSTs
    # /api/onboarding/start to run the wizard; once onboarded it shows the HART
    # identity card). It already has a working native renderer, so we make the
    # wizard DISCOVERABLE by widening this title ("setup") rather than inventing
    # a new system-panel id (which would render a dead placeholder - no JS
    # loader) or reimplementing the wizard.
    'hart_identity': {
        'title': 'My HART Setup', 'icon': 'badge',
        'group': 'You', 'default_size': [500, 450],
        # The "Light your HART" onboarding surface gets its own heart-spark poster.
        'image': '/shell/static/app_art/app-hart-setup.svg',
        'apis': ['/api/onboarding/profile', '/api/onboarding/status'],
    },
    'self_build': {
        'title': 'Self-Build', 'icon': 'build',
        'group': 'System', 'default_size': [700, 550],
        'apis': [
            '/api/system/self-build/status',
            '/api/system/self-build/packages',
            '/api/system/self-build/install',
            '/api/system/self-build/remove',
            '/api/system/self-build/trigger',
            '/api/system/generations',
            '/api/system/rollback',
        ],
    },
    # Settings > About > Credits: the OS "About & Credits" surface. Renders the
    # third-party art licence ledger (docs/THIRD_PARTY_ART.md) served by
    # /api/shell/credits, so every bundled attribution-required asset shows its
    # credit line in the OS itself (the binding rule in THIRD_PARTY_ART.md).
    # 'about' + 'license' in the title so both intuitive names surface it.
    'credits': {
        'title': 'About & Credits (License)', 'icon': 'copyright',
        'group': 'System', 'default_size': [640, 640],
        'apis': ['/api/shell/credits'],
    },
}


# ═══════════════════════════════════════════════════════════════
# Panel Groups (order matters — this is the start menu order)
# ═══════════════════════════════════════════════════════════════

PANEL_GROUPS = ['Discover', 'Create', 'You', 'Explore', 'Manage', 'System']


# ═══════════════════════════════════════════════════════════════
# Icon colours — DE-MONOCHROME the shell (single source of truth)
# ═══════════════════════════════════════════════════════════════
# The glass shell used to tint EVERY icon with one --hart-accent hue, so the
# desktop read as a single blue wash. macOS/Windows give each app its own
# colour. We derive a stable per-app colour here (ONE place) so the desktop
# icon glyph, dock chips, start items and titlebars all agree — no parallel
# palette scattered across the JS render paths.
#
# Resolution order (most specific wins):
#   1. an explicit per-entry  'color'  on the manifest dict (author override)
#   2. ICON_COLOR_OVERRIDES[icon_name]  — high-recognition apps (security=green…)
#   3. GROUP_COLORS[group]              — the app's start-menu group hue
#   4. DEFAULT_ICON_COLOR               — neutral accent fallback
#
# Colours are vibrant but tuned for the dark glass background (mid-bright, ~70%
# lum) so glyphs stay legible on frosted panels.

DEFAULT_ICON_COLOR = '#7FD1C0'   # soft teal — neutral accent fallback

# One hue family per start-menu group → instant visual grouping.
GROUP_COLORS = {
    'Discover': '#4FC3F7',   # sky blue
    'Create':   '#FFB74D',   # amber/orange
    'You':      '#BA68C8',   # violet
    'Explore':  '#4DD0A0',   # mint green
    'Manage':   '#F06292',   # rose
    'System':   '#90A4AE',   # cool grey
}

# Per-icon overrides for apps whose identity colour is well-known, so e.g. the
# Security center reads green and the Terminal reads graphite regardless of the
# group it sits in. Keyed by the manifest 'icon' (Material Symbols name).
ICON_COLOR_OVERRIDES = {
    # security / trust
    'shield': '#34C759', 'security': '#34C759', 'admin_panel_settings': '#34C759',
    'vpn_key': '#FFD54F', 'badge': '#FFD54F', 'lock': '#34C759',
    # comms / social
    'rss_feed': '#FF7043', 'chat_bubble': '#42A5F5', 'forum': '#42A5F5',
    'groups': '#26C6DA', 'campaign': '#FF8A65', 'notifications': '#FF5252',
    'email': '#5C6BC0', 'cell_tower': '#26A69A',
    # build / code / agents
    'code': '#5C6BC0', 'smart_toy': '#7E57C2', 'terminal': '#607D8B',
    'build': '#FFA726', 'extension': '#66BB6A', 'science': '#26C6DA',
    'storefront': '#FF7043', 'storage': '#78909C',
    # media / files
    'folder': '#FFCA28', 'photo': '#26C6DA', 'perm_media': '#EC407A',
    'wallpaper': '#AB47BC', 'palette': '#EC407A', 'music_note': '#EF5350',
    'videocam': '#42A5F5', 'photo_library': '#26C6DA',
    # system / hardware
    'wifi': '#42A5F5', 'bluetooth': '#2979FF', 'battery_full': '#66BB6A',
    'volume_up': '#26A69A', 'monitor_heart': '#EF5350', 'monitoring': '#FF7043',
    'devices': '#78909C', 'print': '#90A4AE', 'language': '#42A5F5',
    'computer': '#42A5F5',
    'schedule': '#5C6BC0', 'delete': '#FF7043', 'system_update': '#66BB6A',
    # money / rewards
    'payments': '#66BB6A', 'emoji_events': '#FFD54F', 'leaderboard': '#FFA726',
    'auto_awesome': '#FFD54F', 'rocket_launch': '#FF7043', 'bolt': '#FFCA28',
}


def color_for(icon_name, group=None, override=None):
    """Resolve the de-monochrome colour for one app icon (single source).

    Pure function — no I/O. Most-specific source wins (see module header).
    """
    if override:
        return override
    if icon_name and icon_name in ICON_COLOR_OVERRIDES:
        return ICON_COLOR_OVERRIDES[icon_name]
    if group and group in GROUP_COLORS:
        return GROUP_COLORS[group]
    return DEFAULT_ICON_COLOR


def with_icon_colors(panels):
    """Return a shallow copy of a {id: entry} panel dict with a resolved
    'color' stamped on every entry, so the JS render paths (start menu, dock,
    desktop icons, titlebars) read one agreed colour. Honours an author's
    explicit 'color' override; otherwise derives from icon then group.
    """
    out = {}
    for pid, entry in panels.items():
        e = dict(entry)
        e['color'] = color_for(e.get('icon'), e.get('group'), e.get('color'))
        out[pid] = e
    return out


# ═══════════════════════════════════════════════════════════════
# Bundled offline app LOGOS (#143 offline-art)
# ═══════════════════════════════════════════════════════════════
# The no-network default logo for a marketplace/catalog app, served at
# /shell/static/app_art/apps/<flathub_id>.svg (Flask static_folder). Both the
# marketplace appCard AND the Netflix Apps producer prefer this bundled logo over
# the network poster so a known app shows real art OFFLINE; the Material glyph
# stays the client onerror fallback. Real official / Flathub logos
# (redistributable per docs/THIRD_PARTY_ART.md) drop into the SAME dir by
# <flathub_id>.svg|png|webp to OVERRIDE the first-party generated tile
# (generate_posters.py) - one filename convention, zero code change. This is the
# single source of truth for the app-id -> bundled-logo mapping; the marketplace
# JS mirrors the SAME URL convention and lets onerror do the runtime miss check.

APPS_ART_URL_BASE = '/shell/static/app_art/apps'
_APPS_ART_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'static', 'app_art', 'apps')
# A Flathub id is reverse-DNS (org.mozilla.firefox); validate so a search hit
# with a junk / undotted id skips straight to the glyph and no odd path is built.
_FLATHUB_ID_RE = re.compile(
    r'^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9][A-Za-z0-9_-]*)+$')
# Bundled tiles ship as .svg; .png/.webp let a dropped-in raster official logo
# win without changing the convention.
_APP_LOGO_EXTS = ('.svg', '.png', '.webp')


def bundled_app_logo(app_id):
    """Served URL of the BUNDLED offline logo for a Flathub app id, or None.

    No network, never raises: checks the shipped static/app_art/apps/ dir on
    disk and returns the same-origin /shell/static URL when a tile is present.
    The caller prefers this over the network poster (offline-first); a miss lets
    the Material glyph render as the fallback."""
    try:
        aid = (app_id or '').strip()
        if not aid or not _FLATHUB_ID_RE.match(aid):
            return None
        for ext in _APP_LOGO_EXTS:
            if os.path.isfile(os.path.join(_APPS_ART_DIR, aid + ext)):
                return APPS_ART_URL_BASE + '/' + aid + ext
        return None
    except OSError:
        return None


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def get_panels_by_group(group):
    """Get all static panels in a group."""
    return {k: v for k, v in PANEL_MANIFEST.items() if v.get('group') == group}


def get_all_panels():
    """Get combined dict of all panels (static + system)."""
    combined = dict(PANEL_MANIFEST)
    combined.update(SYSTEM_PANELS)
    return combined


# ═══════════════════════════════════════════════════════════════
# Settings aggregator — compose the registry, do NOT re-declare panels
# ═══════════════════════════════════════════════════════════════
# The Settings panel is a categorized INDEX over panels that already exist in
# SYSTEM_PANELS / PANEL_MANIFEST. Each id below MUST resolve in get_all_panels()
# (get_settings_sections filters out any that don't, so a section never links a
# dead surface) and the Settings renderer opens each via openPanel (single-
# instance reuse). This is the ONE source for the settings composition; the shell
# JS resolves each id's title/icon/colour from the live manifest, never a copy.
SETTINGS_SECTIONS = [
    ('Personalization', ['appearance', 'wallpaper_manager', 'sound_manager',
                         'nightlight', 'font_manager', 'workspaces']),
    ('Network & Internet', ['network', 'wifi_manager', 'vpn_manager',
                            'bluetooth_manager', 'hotspot', 'dns_settings']),
    ('Devices', ['display', 'audio', 'power', 'my_computer', 'storage_manager',
                 'devices', 'drivers', 'print_manager', 'screen_rotation']),
    ('Privacy & Security', ['security', 'firewall', 'privacy', 'app_permissions',
                            'accessibility']),
    ('Accounts', ['user_accounts', 'hart_identity']),
    ('Apps', ['app_store', 'default_apps', 'startup_apps']),
    ('Time & Language', ['datetime', 'i18n', 'input_methods',
                         'keyboard_shortcuts']),
    ('Update & Backup', ['updates', 'auto_update', 'backup_restore',
                         'cloud_sync', 'self_build']),
]


def get_settings_sections():
    """Composition only: ``[{'title': str, 'ids': [existing panel id, ...]}]``.

    Filters each section's ids to those actually present in get_all_panels() so
    the Settings index never references a panel that does not exist, and drops a
    whole section if it ends up empty. Returns ONLY the id composition — the
    panel metadata (title/icon/colour) is resolved by the caller from the live
    manifest, so there is no duplicated panel definition here.
    """
    known = get_all_panels()
    out = []
    for title, ids in SETTINGS_SECTIONS:
        present = [i for i in ids if i in known]
        if present:
            out.append({'title': title, 'ids': present})
    return out


# ═══════════════════════════════════════════════════════════════
# Start-menu PINNED row (Windows-style curated pins at the top)
# ═══════════════════════════════════════════════════════════════
# A small curated set surfaced above the grouped app list. Each id MUST exist in
# the combined manifest; get_pinned_panels filters to present ids so a pin never
# dangles. Single source for the pin order; the start menu resolves each id's
# metadata from the live manifest.
PINNED_PANEL_IDS = ['feed', 'agents_browse', 'assistant', 'app_store',
                    'file_manager', 'terminal', 'settings']


def get_pinned_panels():
    """Ordered list of pinned panel ids that actually exist in get_all_panels()."""
    known = get_all_panels()
    return [i for i in PINNED_PANEL_IDS if i in known]


def resolve_dynamic_panel(panel_type, **params):
    """Resolve a dynamic panel template with parameters.

    Example: resolve_dynamic_panel('agent_chat', agentId='123', name='Marketing')
    Returns: {'title': 'Chat: Marketing', 'route': '/social/agent/123/chat', ...}
    """
    template = DYNAMIC_PANELS.get(panel_type)
    if not template:
        return None

    resolved = dict(template)
    resolved['title'] = resolved['title'].format(**params)
    resolved['route'] = resolved['route'].format(**params)
    return resolved
