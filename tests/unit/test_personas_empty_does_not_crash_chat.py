"""A prompt with NO personas must not 500 the whole /chat request.

THE REQUIREMENT
───────────────
Create-agent has to work on a potato: a 0.8B Qwen on CPU with no GPU. That is
the floor HART targets, not an unsupported edge case, so every path that depends
on a model returning well-formed JSON has to degrade instead of crashing.

THE BUG
───────
create_agents_for_role branches:

    if len(personas) > 1:   ...multi-persona selection chat...
    else:                   ...personas[0]['name']...

so ZERO personas falls into the else and indexes [0]. Reported from a real box:
"IndexError: list index out of range at reuse_recipe.py:913 (personas[0]) when
the model returns empty personas".

Empty is the normal small-model outcome. The config read above it is wrapped in
a try that only logs at .info, so a truncated or malformed persona blob leaves
`personas` silently [] and the next line raises — surfacing to the user as
"Sorry, I encountered an error" with nothing explaining why.

WHAT THIS PINS
──────────────
No personas means there is no role to choose between — an ordinary single-role
agent. It must be named and carried, never crashed on.
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from core.constants import DEFAULT_SINGLE_ROLE  # noqa: E402


def _import_reuse_recipe():
    """Import reuse_recipe with the WAMP deps stubbed, restoring sys.modules.

    reuse_recipe imports autobahn at module scope; it is declared in
    requirements.txt and present on CI, but absent from the orphaned dev venv
    (memory/reference_broken_venv_test_invocation_2026-07-28), so this file
    could not be checked on the box it was written on without a stub.

    NOT patch.dict(sys.modules, ...). Its teardown does `in_dict.clear()` then
    `in_dict.update(original)`, so EVERY module imported inside the block is
    evicted on exit — including reuse_recipe itself. Each call then re-imported
    a FRESH module object with FRESH TTLCaches, so the caller was reading
    different `agents_roles` than the one the call under test had just written.
    Three assertions failed that way while the fix was working perfectly.

    So: install only the stubs that are missing, import, then remove exactly
    those keys again — leaving reuse_recipe (and everything else the import
    pulled in) cached, which is what the caller needs to observe.

    Also deliberately no `pkg.attr = stub` alias: a raw attribute assignment on
    a real package object is never undone and leaks into every later test in
    the process — the bug that produced ~50 unrelated failures in
    test_dashboard_snapshot.py / test_a2a_graph.py (fixed in ab407eda).
    autobahn is top-level and nothing here re-exports it, so no parent to alias.
    """
    import types
    from unittest.mock import MagicMock

    added = []
    # txaio joined the list when reuse_recipe grew `import txaio;
    # txaio.use_asyncio()` at module scope (line ~108) -- same optional-WAMP
    # family, same stub treatment (use_asyncio() on a MagicMock is a no-op).
    # Without it, every test here fails collection on a runner without the
    # WAMP stack installed (gate run 31828127465, all 7 tests).
    for name in ('autobahn', 'autobahn.asyncio', 'autobahn.asyncio.component',
                 'txaio'):
        if name not in sys.modules:
            m = MagicMock()
            m.__spec__ = types.SimpleNamespace()
            sys.modules[name] = m
            added.append(name)
    try:
        import reuse_recipe
    finally:
        for name in added:            # remove ONLY what we added
            sys.modules.pop(name, None)
    return reuse_recipe


def _call_with_config(tmpdir, config_obj):
    """Drive the REAL create_agents_for_role against a prompt config on disk."""
    reuse_recipe = _import_reuse_recipe()

    path = os.path.join(tmpdir, 'prompt.json')
    with open(path, 'w', encoding='utf-8') as fh:
        if config_obj is not None:
            json.dump(config_obj, fh)
        else:
            fh.write('{"flows": [], "truncated_by_small_model": ')  # invalid JSON

    app = MagicMock()
    with patch.object(reuse_recipe, 'current_app', app), \
            patch.object(reuse_recipe.helper_fun, 'safe_prompt_path',
                         return_value=path):
        reuse_recipe.agents_session.clear()
        reuse_recipe.agents_roles.clear()
        return reuse_recipe.create_agents_for_role('u1', 'p1'), app


class NoPersonasDegradesInsteadOfCrashing(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix='hart-personas-')

    def test_EMPTY_persona_list_does_not_raise(self):
        """The exact crash: personas == [] used to IndexError."""
        try:
            result, _ = _call_with_config(self.tmp, {'personas': []})
        except IndexError:
            self.fail("personas=[] raised IndexError — this 500s /chat on any "
                      "small model that returns an empty persona list")
        self.assertTrue(result[-1], "should take the single-role path")

    def test_MISSING_personas_key_does_not_raise(self):
        """A truncated model answer often drops the key entirely."""
        try:
            _call_with_config(self.tmp, {'flows': []})
        except IndexError:
            self.fail("a config with no 'personas' key raised IndexError")

    def test_UNPARSEABLE_config_does_not_raise(self):
        """Malformed JSON is the 0.8B failure mode Ramesh reported."""
        try:
            _call_with_config(self.tmp, None)
        except IndexError:
            self.fail("an unparseable prompt config raised IndexError")

    def test_the_role_is_NAMED_not_left_blank(self):
        """A blank role would break the session/roles maps downstream."""
        reuse_recipe = _import_reuse_recipe()
        _call_with_config(self.tmp, {'personas': []})
        roles = reuse_recipe.agents_roles.get('u1_p1') or {}
        self.assertEqual(DEFAULT_SINGLE_ROLE, roles.get('u1'),
                         "the degraded path must still register a usable role")

    def test_the_degrade_is_LOGGED_as_a_warning(self):
        """Silent degrade is what made this invisible for so long."""
        _, app = _call_with_config(self.tmp, {'personas': []})
        self.assertTrue(app.logger.warning.called,
                        "no personas was handled silently — an operator has no "
                        "way to tell a small-model parse failure from an agent "
                        "that genuinely has one role")

    def test_a_REAL_single_persona_still_wins(self):
        """The fix must not flatten a genuine persona to the default."""
        reuse_recipe = _import_reuse_recipe()
        _call_with_config(self.tmp, {'personas': [{'name': 'Coach'}]})
        roles = reuse_recipe.agents_roles.get('u1_p1') or {}
        self.assertEqual('Coach', roles.get('u1'),
                         "a declared persona name was replaced by the default")

    def test_a_persona_with_NO_name_falls_back(self):
        """{'description': ...} with no 'name' used to KeyError."""
        reuse_recipe = _import_reuse_recipe()
        _call_with_config(self.tmp, {'personas': [{'description': 'no name'}]})
        roles = reuse_recipe.agents_roles.get('u1_p1') or {}
        self.assertEqual(DEFAULT_SINGLE_ROLE, roles.get('u1'))


if __name__ == '__main__':
    unittest.main()
