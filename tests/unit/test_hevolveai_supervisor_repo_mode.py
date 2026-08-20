"""Repo mode is a DEV affordance. It must never decide what an INSTALL runs.

Repo mode exists so a developer can iterate on the hevolveai checkout
without push+clone. It was ALSO reachable from a frozen build, because
`_resolve_repo_root` appended `~/PycharmProjects/hevolveai` unconditionally
while the sibling candidate right above it was already gated on
`not sys.frozen`.

Consequence, measured on this box 2026-08-20: an INSTALLED Nunba spawned
    C:\\Python310\\python.exe  ~/PycharmProjects/hevolveai/run_server.py
instead of the bundled, Cython-compiled, HevolveArmor-protected package.
`_build_cmd` returns EARLY in repo mode, so the armor hook and every
bundled .pyd were skipped entirely. Any hevolveai evidence read from
~/Documents/Nunba/logs on such a box describes the CHECKOUT, not what
ships -- the "measured the wrong tree" failure, wired in by default.

The wedge it was added for (2026-08: a stale installed package that
crash-looped while the checkout booted) is spent: the bundle now imports
clean -- 99/99 hevolveai modules resolve through ArmoredLoader.

HEVOLVEAI_HOME still works in a frozen build, deliberately. That is the
explicit dev opt-in and is exactly the "test latest changes without
push/clone" use case. What is removed is only the IMPLICIT discovery that
hijacked installs nobody opted in for.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from integrations.agent_engine import hevolveai_supervisor as sup  # noqa: E402


class TestRepoModeIsDevOnly(unittest.TestCase):

    def _fake_home_with_checkout(self, tmp):
        """A home dir that DOES contain ~/PycharmProjects/hevolveai."""
        repo = Path(tmp) / 'PycharmProjects' / 'hevolveai'
        repo.mkdir(parents=True, exist_ok=True)
        (repo / 'run_server.py').write_text('# stub\n', encoding='utf-8')
        return Path(tmp), repo

    def test_frozen_ignores_the_implicit_pycharm_checkout(self):
        """THE GUARD. A frozen install must run the bundle, not the checkout."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            home, repo = self._fake_home_with_checkout(tmp)
            with mock.patch.object(sys, 'frozen', True, create=True), \
                    mock.patch.object(Path, 'home', staticmethod(lambda: home)), \
                    mock.patch.dict(os.environ, {'HEVOLVEAI_HOME': ''}):
                self.assertIsNone(
                    sup._resolve_repo_root(),
                    f'frozen build resolved repo mode to {repo} -- an '
                    'INSTALLED app would spawn the checkout and skip the '
                    'bundled armored package entirely')

    def test_dev_still_finds_a_checkout(self):
        """The dev affordance must survive -- that is the whole point of it.

        Deliberately does NOT pin WHICH checkout. In a real dev tree the
        SIBLING candidate (next to the HARTOS repo) legitimately wins before
        the ~/PycharmProjects one, so asserting the temp path here would be
        asserting my fixture rather than the behaviour.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            home, _repo = self._fake_home_with_checkout(tmp)
            with mock.patch.object(sys, 'frozen', False, create=True), \
                    mock.patch.object(Path, 'home', staticmethod(lambda: home)), \
                    mock.patch.dict(os.environ, {'HEVOLVEAI_HOME': ''}):
                found = sup._resolve_repo_root()
            self.assertIsNotNone(
                found, 'dev mode lost repo mode -- the push/clone-free '
                       'iteration path this feature exists for is broken')
            self.assertTrue(
                (found / 'run_server.py').is_file(),
                f'{found} has no run_server.py, so it is not a usable checkout')

    def test_explicit_HEVOLVEAI_HOME_still_wins_even_when_frozen(self):
        """Deliberate opt-in stays available in a frozen build: that IS the
        'test my latest changes without push/clone' path. Only the implicit
        discovery is removed."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / 'my-checkout'
            override.mkdir(parents=True)
            (override / 'run_server.py').write_text('# stub\n', encoding='utf-8')
            with mock.patch.object(sys, 'frozen', True, create=True), \
                    mock.patch.dict(os.environ,
                                    {'HEVOLVEAI_HOME': str(override)}):
                self.assertEqual(sup._resolve_repo_root(), override)

    def test_both_implicit_candidates_live_inside_the_frozen_gate(self):
        """DRY/consistency: the sibling probe was already frozen-gated and the
        PycharmProjects probe was not. Pin that they now share ONE gate.

        Uses the AST, not text. A substring check ("does 'PycharmProjects'
        appear after the gate line") PASSES while the bug is live, because
        'after the gate' and 'inside the gate' read identically in flat text.
        That is a vacuous guard -- it cannot fail for the defect it names.
        Block membership is the thing that actually distinguishes them.
        """
        import ast
        import inspect
        import textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(sup._resolve_repo_root)))
        fn = tree.body[0]

        def _mentions_pycharm(nodes):
            return any(
                isinstance(n, ast.Constant) and n.value == 'PycharmProjects'
                for node in nodes for n in ast.walk(node))

        frozen_ifs = [n for n in ast.walk(fn) if isinstance(n, ast.If)
                      and 'frozen' in ast.dump(n.test)]
        self.assertEqual(len(frozen_ifs), 1,
                         'expected exactly ONE frozen gate, not a second copy '
                         'that can drift out of sync')
        self.assertTrue(
            _mentions_pycharm(frozen_ifs[0].body),
            'the ~/PycharmProjects candidate is NOT inside the frozen gate; a '
            'frozen install will discover the dev checkout and skip the bundle')
        # ...and nowhere else in the function body at top level.
        top_level = [n for n in fn.body if n not in frozen_ifs]
        self.assertFalse(
            _mentions_pycharm(top_level),
            'a second, ungated ~/PycharmProjects candidate still exists')


if __name__ == '__main__':
    unittest.main()
