"""Reel Studio: a makeup artist's own agent that turns her phone clips into an
Instagram reel on her desktop.

``contracts`` holds the shared schemas, value types and constants every part
builds against.  ``media`` holds the deterministic editor (analysis, beats,
moments, EDL, render, QA).  The agent tools live in ``tools``.

This package init imports nothing, so agent construction can import
``integrations.reel_studio.tools`` without pulling cv2 or ffmpeg in.
"""
