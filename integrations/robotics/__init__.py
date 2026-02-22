"""
HART Robotics — Embodiment orchestration layer.

Bridges LLM-langchain learning/orchestration to Hevolve-Core native embodiment.
Hevolve-Core owns hard real-time (PID, Kalman, SLAM, navigation).
This package owns safety, sensor routing, capability advertisement,
and the learning feedback loop.

Everything is optional.  No GPIO → no GPIO bridge.  No Hevolve-Core → HTTP fallback.
A Raspberry Pi Zero running gossip + fleet commands is still a valid node.
"""
