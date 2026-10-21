"""igkit.skills: the skill texts her agent reads through the reel_skill tool:
three adapted from instagram-agent-skill on the words of a reel, and
reel_edit, HARTOS's own, on how reel_make cuts it.

ig_reel, ig_caption and ig_human are derived from instagram-agent-skill
@d03c56b skills/ig-reel, skills/ig-caption and skills/ig-human SKILL.md,
Copyright (c) 2026 Jake Schincariol, MIT License (see ../LICENSE.txt).  They
were rewritten for a local model and for her: under MAX_SKILL_CHARS each
(upstream's were 3-6.5K characters, too much for a 12K context), naming the
reel_* tools instead of command-line scripts, single-line frontmatter
values (the registry parser reads a YAML '>-' block as the literal '>-').
ig_viral is not shipped: its own text forbids the collector the trends job
is.

reel_edit is not derived from anything: it is HARTOS's own text, written
for HARTOS under its Apache-2.0 license (owner ruling A15, 2026-10-14).  It
says what reel_make decides by itself (filmed order, cuts on the beat, the
reveal held last, cuts only, text clear of Instagram's buttons) and what her
agent chooses.  OpenMontage (AGPL-3.0) was read as a reference only; none of
its code, prompts or skill files is in it.  Its figures are the planner's
own (tests/unit/test_reel_edit_skill.py).

They are parsed with the skill registry's own parser
(integrations.skills.registry.SkillInfo.from_skill_md) and are NEVER
registered in the global skill registry, so other agents and casual chat
never see them.  Each skill folder is a package so setuptools' package-data
rule ships its SKILL.md (CLAUDE.md Gate 6).

This loader lives in the package init: a sibling igkit/skills.py module
would be shadowed by this package and never imported.
"""
from __future__ import annotations

import functools
import os
from typing import Dict, List

# Where a skill text names the reel hashtag cap, it writes the human-voice
# guide's token (HASHTAG_LIMIT_TOKEN here); the loader fills it with the
# guide's REEL_HASHTAG_LIMIT, so the text never holds a second copy of the
# number (owner ruling A10c).
from integrations.agent_engine.human_voice import (  # noqa: F401
    REEL_HASHTAG_LIMIT_TOKEN as HASHTAG_LIMIT_TOKEN, with_reel_hashtag_limit)

#: The skills her agent can read, in the order list_skills reports them;
#: reel_skill's name parameter names each one from here.
SKILL_NAMES = ('ig_reel', 'ig_caption', 'ig_human', 'reel_edit')
#: The most characters a skill file may have (frontmatter included).
MAX_SKILL_CHARS = 2000

_HERE = os.path.dirname(os.path.abspath(__file__))


def _canonical(name) -> str:
    """'ig-reel', ' IG_Reel ' -> 'ig_reel'; KeyError for anything else."""
    if not isinstance(name, str):
        raise KeyError(name)
    key = name.strip().lower().replace('-', '_')
    if key not in SKILL_NAMES:
        raise KeyError('%r is not a Reel Studio skill (%s)' % (name, ', '.join(SKILL_NAMES)))
    return key


@functools.lru_cache(maxsize=None)
def _load(key: str):
    # The registry's parser, used here as a library: nothing is registered.
    from integrations.skills.registry import SkillInfo
    path = os.path.join(_HERE, key, 'SKILL.md')
    with open(path, encoding='utf-8') as fh:
        return SkillInfo.from_skill_md(with_reel_hashtag_limit(fh.read()), source='builtin',
                                       source_path=path)


def get_skill_text(name: str) -> str:
    """The instructions of one skill (the SKILL.md body, without its
    frontmatter).  ``name`` is one of SKILL_NAMES; a hyphen or any case is
    accepted.  Raises KeyError for any other name."""
    return _load(_canonical(name)).instructions


def list_skills() -> List[Dict[str, str]]:
    """``[{'name', 'description'}]`` for every skill, in SKILL_NAMES order."""
    return [{'name': key, 'description': _load(key).description} for key in SKILL_NAMES]
