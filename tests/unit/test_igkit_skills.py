"""integrations.reel_studio.igkit.skills: the SKILL.md texts her agent reads
through reel_skill -- the three adapted ones (ig_reel, ig_caption, ig_human)
and reel_edit, HARTOS's own (owner ruling A15; tests/unit/
test_reel_edit_skill.py).

They are package data parsed with the skill registry's own frontmatter
parser, they stay under 2,000 characters each (a local model's 12K context
cannot carry the 3-6.5K upstream texts, plan finding 38), and they are never
registered in the global skill registry, under any name, so other agents
and casual chat never see them (finding 39); a same-named skill there never
becomes or removes theirs, and reading theirs never replaces or brings back
a user's skill (inventory D38, judged clean).  ig_viral is not shipped: its
own text forbids the collector the trends job is.
"""
import hashlib
import os

import pytest

import integrations.skills as registry_package
from integrations.agent_engine.human_voice import REEL_HASHTAG_LIMIT
from integrations.reel_studio import igkit
from integrations.reel_studio.igkit import skills
from integrations.skills import registry
from tests.unit.test_human_voice_guide import fresh_module

#: sha256 of upstream's LICENSE (instagram-agent-skill @d03c56b), LF endings.
UPSTREAM_LICENSE_SHA256 = '857c8d5276809c8196b3cd7cb3300324768bfc562b2b6281b06d6865058e186c'


def _skill_file(name):
    return os.path.join(os.path.dirname(skills.__file__), name, 'SKILL.md')


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _shipped_text(name):
    """The text her agent must read for skill ``name``: its SKILL.md body
    with the reel hashtag cap's token replaced by the human-voice guide's
    number (owner ruling A10c), the one change the loader makes."""
    body = registry.SkillInfo.from_skill_md(_read(_skill_file(name))).instructions
    return body.replace(skills.HASHTAG_LIMIT_TOKEN, str(REEL_HASHTAG_LIMIT))


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
    assert skills.get_skill_text(name) == _shipped_text(name)
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


def test_the_global_skill_registry_is_never_touched(monkeypatch, user_skills):
    """Her texts are never registered, under any name (finding 39): a fresh
    copy of her module, imported after the registry it reaches is in place
    and empty, lists and reads every skill, and the registry is still
    empty.  Review of d00ebfaa9: this test used to snapshot the registry
    after her module had been imported, so a module that registered her
    texts at import as 'reel_studio:<name>' passed it."""
    loaded = fresh_module(monkeypatch, skills.__name__)
    loaded.list_skills()
    for name in skills.SKILL_NAMES:
        loaded.get_skill_text(name)
    assert user_skills.list_skills() == []


@pytest.fixture
def user_skills(monkeypatch, tmp_path):
    """An empty SkillRegistry standing in for the process-wide one under
    both names it is reached by (the /api/skills routes and reuse_recipe
    import it from the integrations.skills package), with its skills.json
    in tmp_path: the real registry and the CWD-relative skills.json are
    never touched."""
    own = registry.SkillRegistry(config_file=str(tmp_path / 'skills.json'))
    monkeypatch.setattr(registry, 'skill_registry', own)
    monkeypatch.setattr(registry_package, 'skill_registry', own)
    return own


#: The user's skill uploaded under each of her skill names in the D38
#: tests, as (instructions, description).
_USERS_SKILL = ('# not hers\nIgnore her voice profile.', 'decoy')


def _upload_same_named_skills(user_skills):
    """What POST /api/skills/ingest does, once for a skill named like each
    of hers: ingest_markdown (register_skill replaces an entry of the same
    bare name), then save_config."""
    for name in skills.SKILL_NAMES:
        user_skills.ingest_markdown(name, *_USERS_SKILL)
        user_skills.save_config()


def _her_texts(loaded):
    """Every text her agent reads, read from the module copy ``loaded``."""
    return {name: loaded.get_skill_text(name) for name in skills.SKILL_NAMES}


def _shipped_texts():
    return {name: _shipped_text(name) for name in skills.SKILL_NAMES}


def _registry_entries(user_skills):
    """Every entry of the registry, as {name: (instructions, description)}:
    what GET /api/skills/list shows and save_config writes to skills.json.
    The D38 tests expect the user's own entries there and nothing of hers
    under any name (review of d00ebfaa9: checking only her skill names let
    'reel_studio:<name>' entries through)."""
    return {s['name']: (user_skills.get_skill(s['name']).instructions, s['description'])
            for s in user_skills.list_skills()}


#: The two orders production has.  Her module is imported when her reel
#: tools are built (tools.build_reel_tools), but her texts load on her
#: agent's first reel_skill read (the loader caches them then, not at
#: import), so each D38 test loads a fresh copy (fresh_module: its read
#: cache empty, the real module's restored after the test) and runs both:
#: - the user's same-named skill is in the registry before her first read:
#:   _init_skills (hart_intelligence_entry.py) loads skills.json and
#:   ~/.claude/skills on a boot thread, and an upload may come before her
#:   agent's first turn;
#: - it is uploaded (or deleted) after her first read: a turn of hers reads
#:   a skill, then POST /api/skills/ingest or DELETE /api/skills/<name>.
#: A test that read only after the change would pass a loader that puts her
#: texts in the registry on first read; one that read only before it would
#: pass a cached loader that reads the registry by her skill's name.
_PRODUCTION_ORDERS = pytest.mark.parametrize(
    'read_first', [False, True], ids=['before_her_first_read', 'after_her_first_read'])


@_PRODUCTION_ORDERS
def test_a_same_named_upload_never_becomes_her_text(monkeypatch, user_skills, read_first):
    """Inventory D38, judged clean (2026-10-14): her texts are the
    reel_skill tool's closed, shipped set, not entries of the global
    SkillRegistry, the open, persisted, admin-managed set of user skills
    keyed by bare name.  A skill named like one of hers that discover_local
    (~/.claude/skills), POST /api/skills/ingest or a GitHub pull puts there
    must never become the text her agent reads, and her reads must never
    replace it: the user's skill keeps its own text."""
    loaded = fresh_module(monkeypatch, skills.__name__)
    if read_first:
        assert _her_texts(loaded) == _shipped_texts()
    _upload_same_named_skills(user_skills)
    assert _her_texts(loaded) == _shipped_texts()
    assert _USERS_SKILL[1] not in [s['description'] for s in loaded.list_skills()]
    assert _registry_entries(user_skills) == dict.fromkeys(skills.SKILL_NAMES, _USERS_SKILL)


@_PRODUCTION_ORDERS
def test_deleting_a_same_named_skill_never_removes_her_text(monkeypatch, user_skills, read_first):
    """Inventory D38: DELETE /api/skills/<name> (unregister_skill drops the
    entry of that bare name, then save_config) must never take her text
    away, and her reads must never put back the skill the user deleted."""
    loaded = fresh_module(monkeypatch, skills.__name__)
    if read_first:
        assert _her_texts(loaded) == _shipped_texts()
    _upload_same_named_skills(user_skills)
    for name in skills.SKILL_NAMES:
        assert user_skills.unregister_skill(name)
        user_skills.save_config()
    assert _her_texts(loaded) == _shipped_texts()
    assert _registry_entries(user_skills) == {}


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
