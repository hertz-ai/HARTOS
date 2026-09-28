"""The admin config saved before bff95ab44 is carried to the new location.

bff95ab44 moved admin_config.json from agent_data/ beside the package (in
the installed app, under Program Files) to the user's data dir
(core.platform_paths.get_agent_data_dir).  Nothing read the old file after
that, so an upgrade silently dropped every saved channel, workflow and the
agent identity.

Now, once: when the new file does not exist and the old one does, the old
one's content is copied to the new place.  The old file is never deleted or
changed (another install, or a rollback, may still read it), a file already
at the new place is never overwritten, and an old file that is not valid
JSON is not copied.

The old location is derived from the module's __file__, exactly as before
bff95ab44; each test points __file__ into tmp_path so no test reads or
writes a real config.

    python -m pytest tests/unit/test_admin_config_legacy_migration.py -q
"""
import json
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

SAVED = {
    'channels': {'discord': {'bot_token': 'tok', 'announce_chat_id': '123'}},
    'workflows': {},
    'identity': {'agent_id': 'ag1', 'display_name': 'Nunba'},
}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    import integrations.channels.admin.api as admin_api
    from integrations.channels.admin.api import AdminAPI

    # The installed layout: <lib>/integrations/channels/admin/api.py, and
    # the old config at <lib>/agent_data/admin_config.json.
    lib = tmp_path / 'lib'
    monkeypatch.setattr(admin_api, '__file__',
                        str(lib / 'integrations' / 'channels' / 'admin' / 'api.py'))
    legacy = lib / 'agent_data' / 'admin_config.json'
    new = tmp_path / 'userdata' / 'agent_data' / 'admin_config.json'
    monkeypatch.setattr(AdminAPI, '_config_path', lambda self: str(new))
    return legacy, new


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def test_the_old_config_is_copied_once_and_kept(paths):
    from integrations.channels.admin.api import AdminAPI

    legacy, new = paths
    _write(legacy, json.dumps(SAVED))
    before = legacy.read_bytes()

    api = AdminAPI()

    assert api._channels == SAVED['channels'], 'the saved channels were lost'
    assert api._identity is not None and api._identity.agent_id == 'ag1'
    assert json.loads(new.read_text(encoding='utf-8')) == SAVED
    assert legacy.read_bytes() == before, 'the old file was changed or removed'


def test_a_config_already_at_the_new_place_is_never_overwritten(paths):
    from integrations.channels.admin.api import AdminAPI

    legacy, new = paths
    _write(legacy, json.dumps(SAVED))
    current = {'channels': {'slack': {'x': 1}}, 'workflows': {}, 'identity': None}
    _write(new, json.dumps(current))

    api = AdminAPI()

    assert api._channels == {'slack': {'x': 1}}
    assert json.loads(new.read_text(encoding='utf-8')) == current


def test_an_unreadable_old_config_is_not_copied(paths):
    from integrations.channels.admin.api import AdminAPI

    legacy, new = paths
    _write(legacy, '{not json')

    api = AdminAPI()

    assert api._channels == {}
    assert not new.exists()
    assert legacy.read_text(encoding='utf-8') == '{not json'


def test_no_old_config_is_a_no_op(paths):
    from integrations.channels.admin.api import AdminAPI

    legacy, new = paths
    api = AdminAPI()
    assert api._channels == {} and not new.exists() and not legacy.exists()
