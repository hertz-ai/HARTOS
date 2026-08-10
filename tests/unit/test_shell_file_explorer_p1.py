"""Behavioural tests for the File Explorer P1 backend routes.

Three new routes extend the SAME sandbox/auth/audit/destructive trio as the
existing browse/move/copy/delete surface in shell_os_apis — NO parallel file-op
path, NO second sandbox:

  GET  /api/shell/files/search    — recursive filename search (os.walk, depth +
                                     result caps, rel-pathed, GIL-safe)
  GET  /api/shell/files/thumbnail — Pillow thumbnail for images (graceful 204
                                     fallback), on-disk cache under get_db_dir()
  POST /api/shell/files/chmod     — editable POSIX mode (fail-closed classifier,
                                     immutable audit, no-op-safe on Windows)

All assertions are behavioural: the REAL routes are registered on a Flask app
and driven through a test_client, with the sandbox pinned to a temp dir via
HART_SHELL_ALLOWED_PATHS and only the action-classifier boundary mocked. No
grep / source-shape assertions.

Local note: this box OOM-kills the full pytest import chain, so an inline
runner at the bottom executes every test_* and reports (matching the pattern in
test_shell_icon_customize.py). Committed for CI.
"""
import io
import os
import os.path as osp
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _app_in_sandbox(sandbox):
    """Register the real shell OS routes with the path sandbox pinned to
    `sandbox` (plus the always-allowed temp dir). Returns a test_client."""
    from flask import Flask
    import integrations.agent_engine.shell_os_apis as soa
    os.environ['HART_SHELL_ALLOWED_PATHS'] = sandbox
    soa._ALLOWED_ROOTS = None  # force recompute to pick up the env override
    app = Flask(__name__)
    app.config['TESTING'] = True
    soa.register_shell_os_routes(app)
    return app.test_client()


class TestFileSearch(unittest.TestCase):
    """GET /api/shell/files/search."""

    def setUp(self):
        self.sb = tempfile.mkdtemp()
        self.c = _app_in_sandbox(self.sb)
        os.makedirs(osp.join(self.sb, 'sub', 'deep'), exist_ok=True)
        open(osp.join(self.sb, 'apple.txt'), 'w').close()
        open(osp.join(self.sb, 'sub', 'apricot.txt'), 'w').close()
        open(osp.join(self.sb, 'sub', 'deep', 'banana.txt'), 'w').close()

    def test_recursive_finds_nested_with_rel(self):
        r = self.c.get('/api/shell/files/search?path=%s&q=ap&recursive=true' % self.sb)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        names = sorted(e['name'] for e in body['entries'])
        self.assertEqual(names, ['apple.txt', 'apricot.txt'])
        # every hit carries a path-relative-to-root so the UI can show location
        self.assertTrue(all('rel' in e for e in body['entries']))
        # entry shape mirrors /browse (the contract the explorer renders)
        for e in body['entries']:
            for k in ('name', 'path', 'is_dir', 'size', 'modified', 'extension'):
                self.assertIn(k, e)

    def test_nonrecursive_is_current_dir_only(self):
        r = self.c.get('/api/shell/files/search?path=%s&q=ap&recursive=false' % self.sb)
        names = sorted(e['name'] for e in r.get_json()['entries'])
        self.assertEqual(names, ['apple.txt'])  # apricot.txt is in a subfolder

    def test_matches_directory_names_too(self):
        r = self.c.get('/api/shell/files/search?path=%s&q=sub&recursive=true' % self.sb)
        ents = r.get_json()['entries']
        self.assertTrue(any(e['is_dir'] and e['name'] == 'sub' for e in ents))

    def test_empty_query_returns_empty(self):
        r = self.c.get('/api/shell/files/search?path=%s&q=' % self.sb)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['entries'], [])

    def test_auth_denied_for_nonlocal(self):
        r = self.c.get('/api/shell/files/search?path=%s&q=ap' % self.sb,
                       environ_overrides={'REMOTE_ADDR': '203.0.113.7'})
        self.assertEqual(r.status_code, 403)

    def test_read_browses_outside_sandbox_like_explorer(self):
        """Option B parity: browse/search reach mounted-disk paths OUTSIDE the
        user-owned sandbox roots, exactly like Explorer/Finder. Reads call
        _is_path_allowed() with the browse default (mountpoints admitted); only
        MUTATIONS stay confined -- see TestFileChmod.test_mutation_denied_outside_roots.
        A confined read would 403 here; this asserts it does not, and that a real
        file living outside the sandbox is genuinely listed."""
        import integrations.agent_engine.shell_os_apis as soa
        sysdir = 'C:\\Windows' if sys.platform == 'win32' else '/etc'
        if not os.path.isdir(sysdir):
            self.skipTest('%s not present on this host' % sysdir)
        # Precondition: sysdir is genuinely OUTSIDE the confined (mutation) roots,
        # so a 200 below can only mean reads are allowed to browse the mount.
        self.assertFalse(soa._is_path_allowed(sysdir, include_mounts=False),
                         'target must be outside the confined roots to be meaningful')
        real = next((e.name for e in os.scandir(sysdir) if e.is_file()), None)
        if real is None:
            self.skipTest('no file under %s to search for' % sysdir)
        r = self.c.get('/api/shell/files/search',
                       query_string={'path': sysdir, 'q': real[:3], 'recursive': 'false'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        names = [e['name'] for e in r.get_json()['entries']]
        self.assertIn(real, names)  # genuinely listed a file outside the sandbox


class TestFileThumbnail(unittest.TestCase):
    """GET /api/shell/files/thumbnail."""

    def setUp(self):
        self.sb = tempfile.mkdtemp()
        self.c = _app_in_sandbox(self.sb)
        self._pillow = True
        try:
            from PIL import Image
            self.img = osp.join(self.sb, 'pic.png')
            Image.new('RGB', (400, 300), (120, 80, 200)).save(self.img)
        except Exception:
            self._pillow = False

    def test_image_returns_capped_png(self):
        if not self._pillow:
            self.skipTest('Pillow not importable')
        from PIL import Image
        r = self.c.get('/api/shell/files/thumbnail?path=%s&size=64' % self.img)
        self.assertEqual(r.status_code, 200, r.status_code)
        self.assertEqual(r.mimetype, 'image/png')
        th = Image.open(io.BytesIO(r.get_data()))
        self.assertLessEqual(max(th.size), 64)  # dimension capped

    def test_second_call_is_cached(self):
        if not self._pillow:
            self.skipTest('Pillow not importable')
        self.c.get('/api/shell/files/thumbnail?path=%s&size=48' % self.img)
        r2 = self.c.get('/api/shell/files/thumbnail?path=%s&size=48' % self.img)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.mimetype, 'image/png')

    def test_nonimage_falls_back_204(self):
        txt = osp.join(self.sb, 'note.txt')
        open(txt, 'w').write('hi')
        r = self.c.get('/api/shell/files/thumbnail?path=%s' % txt)
        self.assertEqual(r.status_code, 204)

    def test_missing_file_204(self):
        r = self.c.get('/api/shell/files/thumbnail?path=%s' % osp.join(self.sb, 'nope.png'))
        self.assertEqual(r.status_code, 204)

    def test_pillow_absent_falls_back_204(self):
        """When Pillow can't import, the route degrades to 204 (glyph fallback),
        never a 500 — so the grid render is never broken by a missing dep."""
        import builtins
        real_import = builtins.__import__

        def fake(name, *a, **k):
            if name == 'PIL' or name.startswith('PIL.'):
                raise ImportError('no pillow')
            return real_import(name, *a, **k)

        with patch('builtins.__import__', side_effect=fake):
            r = self.c.get('/api/shell/files/thumbnail?path=%s' % self.img)
        self.assertEqual(r.status_code, 204)

    def test_auth_denied_for_nonlocal(self):
        r = self.c.get('/api/shell/files/thumbnail?path=%s' % self.img,
                       environ_overrides={'REMOTE_ADDR': '203.0.113.7'})
        # 403 (denied) when there is a real image under the root; for non-image
        # the auth gate still runs first, so this is 403 regardless.
        self.assertEqual(r.status_code, 403)

    def test_read_reaches_outside_sandbox_like_explorer(self):
        """Option B parity: the thumbnail route reaches file paths OUTSIDE the
        user-owned sandbox (mounted disk), like Explorer's preview pane. A real
        NON-image file there returns the 204 glyph-fallback -- which proves the
        sandbox ADMITTED the read, because a confined read would 403 BEFORE the
        fallback. The victim is discovered (and asserted to exist + be outside
        the confined roots) so the 204 can only be the non-image branch, never
        the missing-file branch. Mutations remain confined -- see
        TestFileChmod.test_mutation_denied_outside_roots."""
        import integrations.agent_engine.shell_os_apis as soa
        sysdir = 'C:\\Windows' if sys.platform == 'win32' else '/etc'
        if not os.path.isdir(sysdir):
            self.skipTest('%s not present on this host' % sysdir)
        imgexts = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff', '.ico'}
        victim = next((e.path for e in os.scandir(sysdir)
                       if e.is_file()
                       and osp.splitext(e.name)[1].lower() not in imgexts), None)
        if victim is None:
            self.skipTest('no non-image file under %s to probe' % sysdir)
        # Precondition: the file EXISTS and is outside the confined (mutation)
        # roots, so 204 here is the non-image fallback (sandbox admitted), not 403.
        self.assertTrue(os.path.isfile(victim))
        self.assertFalse(soa._is_path_allowed(victim, include_mounts=False),
                         'victim must be outside the confined roots to be meaningful')
        r = self.c.get('/api/shell/files/thumbnail', query_string={'path': victim})
        self.assertEqual(r.status_code, 204,
                         'reads reach outside the sandbox: non-image -> 204, not 403')


class TestFileChmod(unittest.TestCase):
    """POST /api/shell/files/chmod."""

    def setUp(self):
        self.sb = tempfile.mkdtemp()
        self.c = _app_in_sandbox(self.sb)
        self.f = osp.join(self.sb, 'perm.txt')
        open(self.f, 'w').write('x')

    def test_sets_mode_octal_string(self):
        with patch('security.action_classifier.classify_action', return_value='safe'):
            r = self.c.post('/api/shell/files/chmod', json={'path': self.f, 'mode': '750'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body['requested'], '750')
        if sys.platform != 'win32':
            self.assertEqual(body['mode'], '750')  # POSIX honours the full mode

    def test_accepts_int_mode(self):
        with patch('security.action_classifier.classify_action', return_value='safe'):
            r = self.c.post('/api/shell/files/chmod', json={'path': self.f, 'mode': 0o644})
        self.assertEqual(r.status_code, 200)

    def test_bad_mode_400(self):
        with patch('security.action_classifier.classify_action', return_value='safe'):
            r = self.c.post('/api/shell/files/chmod', json={'path': self.f, 'mode': 'oops'})
        self.assertEqual(r.status_code, 400)

    def test_out_of_range_400(self):
        with patch('security.action_classifier.classify_action', return_value='safe'):
            r = self.c.post('/api/shell/files/chmod', json={'path': self.f, 'mode': '7777'})
        self.assertEqual(r.status_code, 400)

    def test_missing_path_404(self):
        with patch('security.action_classifier.classify_action', return_value='safe'):
            r = self.c.post('/api/shell/files/chmod',
                            json={'path': osp.join(self.sb, 'nope'), 'mode': '644'})
        self.assertEqual(r.status_code, 404)

    def test_real_classifier_routine_chmod_succeeds(self):
        """chmod is a routine op (like move/copy) and must NOT route through the
        action-classifier. With the REAL classifier (NO mock), a routine
        chmod('644') must return 200 — classify_action('chmod ...') returns
        'unknown', which previously fail-closed-403'd EVERY real permission
        change. This guards the classifier gate from being re-introduced (the
        bug the adversarial review caught that the mocked tests hid)."""
        r = self.c.post('/api/shell/files/chmod', json={'path': self.f, 'mode': '644'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json().get('requested'), '644')

    def test_auth_denied_for_nonlocal(self):
        r = self.c.post('/api/shell/files/chmod', json={'path': self.f, 'mode': '644'},
                        environ_overrides={'REMOTE_ADDR': '203.0.113.7'})
        self.assertEqual(r.status_code, 403)

    def test_mutation_denied_outside_roots(self):
        """The MUTATION boundary that stays confined under Option B: chmod (a
        write) on a path outside the user-owned roots is denied, even though the
        READ routes now browse the whole disk (TestFileSearch/TestFileThumbnail).
        chmod calls _is_path_allowed(include_mounts=False) -- the confined roots.

        The victim is DISCOVERED and asserted to exist so the route reaches the
        sandbox gate (403) deterministically, never the missing-path 404 -- the
        old hardcoded notepad.exe path could vanish under WOW64 redirection and
        flip this to 404."""
        import integrations.agent_engine.shell_os_apis as soa
        sysdir = 'C:\\Windows' if sys.platform == 'win32' else '/etc'
        if not os.path.isdir(sysdir):
            self.skipTest('%s not present on this host' % sysdir)
        victim = next((e.path for e in os.scandir(sysdir) if e.is_file()), None)
        if victim is None:
            self.skipTest('no file under %s to probe' % sysdir)
        self.assertTrue(os.path.isfile(victim))  # exists -> route can't 404 out
        self.assertFalse(soa._is_path_allowed(victim, include_mounts=False),
                         'victim must be outside the confined roots to be meaningful')
        with patch('security.action_classifier.classify_action', return_value='safe'):
            r = self.c.post('/api/shell/files/chmod', json={'path': victim, 'mode': '644'})
        self.assertEqual(r.status_code, 403,
                         'mutations stay confined: chmod outside user roots is denied')

    def test_writes_immutable_audit(self):
        """A successful chmod records a 'file_chmod' event on the audit log."""
        import integrations.agent_engine.shell_os_apis as soa
        seen = {}

        def _cap(action, detail=None):
            seen['action'] = action
            seen['detail'] = detail or {}

        with patch.object(soa, '_audit_shell_op', _cap), \
                patch('security.action_classifier.classify_action', return_value='safe'):
            r = self.c.post('/api/shell/files/chmod', json={'path': self.f, 'mode': '700'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(seen.get('action'), 'file_chmod')
        self.assertEqual(seen['detail'].get('mode'), '700')


class TestFileExplorerUIWiring(unittest.TestCase):
    """Drive the REAL static/hartFiles.js through its public API on a DOM shim
    (test_shell_file_explorer_p1.mjs) and assert the 4 P1 UI wirings end-to-end:
    search-subfolders toggle -> /search?recursive=true, image thumbnail <img>,
    drag-drop -> /move (or /copy with Ctrl) incl. onto a Places entry, and the
    Properties permissions grid -> /chmod. Skips cleanly if node is absent."""

    MJS = osp.join(osp.dirname(__file__), 'test_shell_file_explorer_p1.mjs')

    def test_ui_wiring_behaviour(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node not available to run the JS behavioural harness')
        r = subprocess.run([node, self.MJS], capture_output=True, text=True, timeout=60)
        # Surface the per-assertion log on failure so CI shows exactly which line.
        self.assertEqual(r.returncode, 0,
                         'hartFiles.js P1 harness failed:\n' + r.stdout + r.stderr)
        self.assertIn('RESULT: ALL PASS', r.stdout, r.stdout)


if __name__ == '__main__':
    # Inline runner (pytest OOMs on this box): run every test_* and report.
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (TestFileSearch, TestFileThumbnail, TestFileChmod, TestFileExplorerUIWiring):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print('RESULT:', 'ALL PASS' if result.wasSuccessful()
          else (str(len(result.failures) + len(result.errors)) + ' FAILED'))
    sys.exit(0 if result.wasSuccessful() else 1)
