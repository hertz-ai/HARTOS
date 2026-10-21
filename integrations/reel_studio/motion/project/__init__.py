"""Reel Studio's Remotion project: our own templates (templates.js), the
bundle step (prepare.js) and the renderer (render.js), HARTOS source,
Apache-2.0.

A package only so that setup.py / pyproject.toml package-data ships the
.js and .json files with a pip install (Gate 6).  Nothing here is Python to
import.  Its npm packages and Remotion's headless Chrome are never in the
repo or a build: RuntimeToolManager sets this project up at runtime, on
demand, in the models dir (its 'reel_motion' row; model_storage.
install_npm_project copies every file here but the Python ones).
"""
