"""Browser Research subsystem — "browser-as-human" research and action layer.

Tier model (see memory/project_browser_research_subsystem.md):
  T1 real-time messaging  — existing 31 websocket adapters in integrations/channels/
                            (Discord/Slack/Telegram/WhatsApp/Matrix/Teams/...)
                            UNTOUCHED by this package.  Zero regression.
  T2 read/post-as-user    — NEW.  Obscura (Rust, CDP) driver attaches to user's
                            running Chrome (B2 default) or launches its own
                            stealth profile (B1 fallback).
                            Twitter/Reddit/LinkedIn/Bilibili/XHS/Weibo/etc.
  T3 public reads         — yt-dlp + Jina Reader + plain HTTP.  No browser.

Canonical primitives this package EXTENDS (no parallel paths — per
memory/feedback_unification_reuse_contract.md):
  - core.agent_tools         — tools register here, not in a parallel registry.
  - consent_service          — gate via has_capability(user, scope).
  - liquid_ui_service        — result cards emit via agent_ui_update.
  - core.platform_paths      — data/log dirs.  No hardcoded paths.
  - audit log                — single canonical web_research_audit.log.

Lazy load: Obscura/Playwright bytes are NOT imported until a T2 tool fires.
T3 tools and the dispatcher work fine without any browser dependency.
"""
__all__ = ['dispatch', 'tools', 'audit', 'driver']
