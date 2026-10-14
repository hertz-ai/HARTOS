"""integrations.reel_studio.igkit.skills: the SKILL.md texts her agent reads
through reel_skill -- the three adapted ones (ig_reel, ig_caption, ig_human)
and reel_edit, HARTOS's own (owner ruling A15; tests/unit/
test_reel_edit_skill.py).

They are package data parsed with the skill registry's own frontmatter
parser, they stay under 2,000 characters each (a local model's 12K context
cannot carry the 3-6.5K upstream texts, plan finding 38), and they are never
registered in the global skill registry, so other agents and casual chat
never see them (finding 39).  ig_viral is not shipped: its own text forbids
the collector the trends job is.
"""
import hashlib
import os

import pytest

from integrations.agent_engine.human_voice import REEL_HASHTAG_LIMIT
from integrations.reel_studio import igkit
from integrations.reel_studio.igkit import skills
from integrations.skills import registry

#: sha256 of upstream's LICENSE (instagram-agent-skill @d03c56b), LF endings.
UPSTREAM_LICENSE_SHA256 = '857c8d5276809c8196b3cd7cb3300324768bfc562b2b6281b06d6865058e186c'


def _skill_file(name):
    return os.path.join(os.path.dirname(skills.__file__), name, 'SKILL.md')


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def test_the_shipped_skills():
    assert skills.SKILL_NAMES == ('ig_reel', 'ig_caption', 'ig_human', 'reel_edit')
    assert skills.MAX_SKILL_CHARS == 2000


@pytest.mark.parametrize('name', ['ig_reel', 'ig_caption', 'ig_human', 'reel_edit'])
def test_each_skill_is_short_and_read_with_the_registry_parser(name):
    raw = _read(_skill_file(name))
    assert len(raw) <= skills.MAX_SKILL_CHARS
    meta, body = registry._parse_frontmatter(raw)
    assert meta['name'] == name
    # Single-line values: the registry parser reads a YAML '>-' block as the
    # literal text '>-' (skill-port finding 1), so none is used here.
    assert isinstance(meta['description'], str)
    assert meta['description'] not in ('', '>-', '>', '|', '|-')
    assert meta['disable-model-invocation'] is True
    # The one change the loader makes: the reel hashtag cap's token becomes
    # the human-voice guide's number (owner ruling A10c).
    assert skills.get_skill_text(name) == body.replace(skills.HASHTAG_LIMIT_TOKEN,
                                                       str(REEL_HASHTAG_LIMIT))
    assert body.startswith('# ' + name)


def test_list_skills_names_and_describes_them():
    listed = skills.list_skills()
    assert [s['name'] for s in listed] == list(skills.SKILL_NAMES)
    assert all(s['description'] and '\n' not in s['description'] for s in listed)


def test_a_hyphenated_name_is_the_same_skill():
    assert skills.get_skill_text('ig-reel') == skills.get_skill_text('ig_reel')
    assert skills.get_skill_text(' IG_Caption ') == skills.get_skill_text('ig_caption')


@pytest.mark.parametrize('bad', ['ig_viral', '../ig_reel', 'ig_reel/../../x', 'ig_reel\\..',
                                 '', None, 42, 'SKILL.md'])
def test_unknown_names_are_refused(bad):
    with pytest.raises(KeyError):
        skills.get_skill_text(bad)


def test_the_global_skill_registry_is_never_touched():
    before = [s['name'] for s in registry.skill_registry.list_skills()]
    skills.list_skills()
    for name in skills.SKILL_NAMES:
        skills.get_skill_text(name)
    after = [s['name'] for s in registry.skill_registry.list_skills()]
    assert after == before
    assert not set(skills.SKILL_NAMES) & set(after)


def test_the_mit_license_ships_verbatim_beside_the_code():
    text = _read(os.path.join(os.path.dirname(igkit.__file__), 'LICENSE.txt'))
    digest = hashlib.sha256(text.replace('\r\n', '\n').encode('utf-8')).hexdigest()
    assert digest == UPSTREAM_LICENSE_SHA256


def test_skill_folders_are_packages_so_pip_ships_their_text():
    """setuptools package-data ships *.md only inside packages (Gate 6)."""
    import importlib
    for name in skills.SKILL_NAMES:
        mod = importlib.import_module('integrations.reel_studio.igkit.skills.' + name)
        assert os.path.isfile(os.path.join(os.path.dirname(mod.__file__), 'SKILL.md'))
    res = importlib.import_module('integrations.reel_studio.igkit.resources')
    # slop.json is not one of them: its list is the human-voice guide's.
    assert res.DATA_FILES == ('hooks.json', 'rubric.json', 'safe_zones.json',
                              'voice_template.json')
    for fn in res.DATA_FILES:
        assert os.path.isfile(os.path.join(os.path.dirname(res.__file__), fn))
