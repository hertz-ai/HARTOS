"""
Remote Desktop GUI — LiquidUI glass panel for HARTOS-as-OS desktop.

Primary UI: LiquidUI glassmorphism panel (integrations/agent_engine/liquid_ui_service.py)
  - Renders in WebKit2 GTK with frosted-glass styling
  - Registered as panel in shell_manifest.py
  - HTML5 canvas for frame display, JS event capture for input

Fallback: Nunba webview (bundled Windows/macOS) calls /api/remote-desktop/* endpoints.
"""
