"""
HART OS Glass Desktop Shell — Panel Manifest.

Defines all panels available in the glass desktop shell:
- PANEL_MANIFEST: Static panels from Nunba SPA (start menu items)
- DYNAMIC_PANELS: Context-opened panels (profile, post detail, etc.)
- SYSTEM_PANELS: Native system management panels (hardware, security, etc.)

Each panel can float as a draggable/resizable frosted glass window.
Nunba panels render via iframe to /app/#/<route>.
System panels render natively from backend API data.
"""

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
    'resonance': {
        'title': 'Resonance', 'icon': 'auto_awesome',
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
    'backup': {
        'title': 'Backup & Sync', 'icon': 'cloud_sync',
        'route': '/social/settings/backup', 'group': 'You',
        'default_size': [600, 500],
    },
    'appearance': {
        'title': 'Appearance', 'icon': 'palette',
        'route': '/social/settings/appearance', 'group': 'You',
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
        'apis': ['/api/shell/files/browse', '/api/shell/files/recent'],
    },
    'terminal': {
        'title': 'Terminal', 'icon': 'terminal',
        'group': 'System', 'default_size': [800, 500],
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
        'apis': ['/api/apps/search', '/api/apps/installed',
                 '/api/apps/install', '/api/apps/uninstall'],
    },
    'app_permissions': {
        'title': 'App Permissions', 'icon': 'admin_panel_settings',
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
    'hart_identity': {
        'title': 'My HART', 'icon': 'badge',
        'group': 'You', 'default_size': [500, 450],
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
}


# ═══════════════════════════════════════════════════════════════
# Panel Groups (order matters — this is the start menu order)
# ═══════════════════════════════════════════════════════════════

PANEL_GROUPS = ['Discover', 'Create', 'You', 'Explore', 'Manage', 'System']


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
