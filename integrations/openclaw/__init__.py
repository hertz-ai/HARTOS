"""
OpenClaw Integration — Bidirectional bridge between HART OS and OpenClaw.

HART OS is the superset:
  - HART agents can use any ClawHub skill (3,200+ skills)
  - OpenClaw agents can access HART recipes, agents, and Nunba panels
  - ClawHub skills are mapped to HART recipe actions
  - HART recipes can be exported as ClawHub skills

Architecture:
  - clawhub_adapter.py: Parse/install/run ClawHub SKILL.md files
  - gateway_bridge.py: Connect to OpenClaw's WebSocket gateway
  - skill_exporter.py: Export HART recipes as SKILL.md for ClawHub
  - hart_skill_server.py: Expose HART agents as OpenClaw-compatible tools
"""
