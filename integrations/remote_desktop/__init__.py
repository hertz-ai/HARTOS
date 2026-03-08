"""
HART OS Native Remote Desktop — AnyDesk-class remote access built on HARTOS infrastructure.

Three deployment modes:
  1. Nunba bundled (Windows/macOS) — Nunba UI calls /api/remote-desktop/* endpoints
  2. Docker/standalone — CLI (hart remote-desktop) + REST API (headless)
  3. HARTOS-as-OS (bare-metal) — Nunba desktop UI + CLI + system tray

Three transport tiers (no STUN/TURN required):
  Tier 1 (LAN):     Direct WebSocket between devices
  Tier 2 (WAN):     WAMP relay through existing Crossbar router
  Tier 3 (WAN P2P): WireGuard tunnel from compute mesh

Usage:
  from integrations.remote_desktop.device_id import get_device_id
  from integrations.remote_desktop.session_manager import get_session_manager
"""
