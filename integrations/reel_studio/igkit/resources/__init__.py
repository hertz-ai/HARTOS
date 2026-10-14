"""igkit's data files: hook formulas, the profile rubric, the on-screen text
rules and the voice-profile template.

The AI-sounding word list (upstream's slop.json) is not here: it is the
human-voice guide's one list, integrations.agent_engine.human_voice (owner
ruling A10c), and igkit.humanize reads it from there.

This folder is a package, not a plain directory, so setuptools'
package-data rule (``*.json``) ships the files with a pip install, and
Nunba's frozen build copies them with the rest of ``integrations``
(CLAUDE.md Gate 6).  It is not called ``data``: the repo's .gitignore
ignores every ``data/`` directory (runtime scratch that may hold user data),
so files there would never be committed.  Each JSON file carries a
``source`` key naming the upstream file it was derived from (MIT, see
../LICENSE.txt) and a ``modified`` list of what was changed.
"""
from __future__ import annotations

import json
import os
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))

#: The files this package ships.
DATA_FILES = ('hooks.json', 'rubric.json', 'safe_zones.json', 'voice_template.json')


def load_json(name: str) -> Any:
    """Parse one of DATA_FILES (a fresh object on every call).

    Raises KeyError for any other name, so a caller can never be steered to
    a path outside this folder.
    """
    if name not in DATA_FILES:
        raise KeyError('%r is not an igkit data file (%s)' % (name, ', '.join(DATA_FILES)))
    with open(os.path.join(_HERE, name), encoding='utf-8') as fh:
        return json.load(fh)
