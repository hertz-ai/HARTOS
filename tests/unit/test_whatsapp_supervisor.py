"""Unit tests for integrations.social.whatsapp_supervisor.

Tests the gating, the paths and the gateway's npm setup (through the one
npm installer, model_storage.install_npm_project, with npm mocked) without
spawning Node.  The actual subprocess lifecycle is covered by an
integration test that runs only when Node + npm are present (skipped on CI
without Node).
"""

import importlib
import json
import os
import shutil
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import pytest

# Make HARTOS importable regardless of pytest invocation cwd.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from core.subprocess_safe import BoundedResult  # noqa: E402
from integrations.social import whatsapp_supervisor as ws  # noqa: E402

# By module path: the package re-exports the storage SINGLETON under the
# module's own name, so the attribute form would be the instance.
model_storage = importlib.import_module('integrations.service_tools.model_storage')


class GatingTests(unittest.TestCase):

    def setUp(self):
        # Snapshot env vars we mutate so tests don't bleed.
        self._env_keys = (
            'WHATSAPP_AUTOSTART',
            'WHATSAPP_API_URL',
            'HEVOLVE_DEPLOY_MODE',
        )
        self._saved = {k: os.environ.get(k) for k in self._env_keys}
        for k in self._env_keys:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_flat_mode_runs(self):
        os.environ['HEVOLVE_DEPLOY_MODE'] = 'flat'
        self.assertTrue(ws.supervisor_should_run())

    def test_default_regional_mode_runs(self):
        os.environ['HEVOLVE_DEPLOY_MODE'] = 'regional'
        self.assertTrue(ws.supervisor_should_run())

    def test_central_mode_skips(self):
        os.environ['HEVOLVE_DEPLOY_MODE'] = 'central'
        self.assertFalse(ws.supervisor_should_run())

    def test_autostart_zero_force_disables(self):
        os.environ['HEVOLVE_DEPLOY_MODE'] = 'flat'
        os.environ['WHATSAPP_AUTOSTART'] = '0'
        self.assertFalse(ws.supervisor_should_run())

    def test_autostart_one_force_enables_even_on_central(self):
        os.environ['HEVOLVE_DEPLOY_MODE'] = 'central'
        os.environ['WHATSAPP_AUTOSTART'] = '1'
        self.assertTrue(ws.supervisor_should_run())

    def test_remote_api_url_skips(self):
        # Operator points at a remote WAHA — supervisor stays off so
        # the existing adapter override path takes effect.
        os.environ['HEVOLVE_DEPLOY_MODE'] = 'flat'
        os.environ['WHATSAPP_API_URL'] = 'https://my-waha.example.com'
        self.assertFalse(ws.supervisor_should_run())

    def test_localhost_api_url_still_runs(self):
        os.environ['HEVOLVE_DEPLOY_MODE'] = 'flat'
        os.environ['WHATSAPP_API_URL'] = 'http://localhost:3000'
        self.assertTrue(ws.supervisor_should_run())


class PathTests(unittest.TestCase):

    def test_bundled_gateway_dir_points_at_install_path(self):
        # The bundled copy is read-only at runtime on Windows
        # (lives under Program Files in the cx_Freeze install)
        # but ships with the repo so tests can verify it exists.
        gw = ws._bundled_gateway_dir()
        self.assertTrue(gw.name == 'whatsapp')
        self.assertTrue((gw / 'gateway.js').exists())
        self.assertTrue((gw / 'package.json').exists())

    def test_whatsapp_home_under_hevolve_home(self):
        # Default ~/.hevolve/whatsapp; honours HEVOLVE_HOME override.
        os.environ['HEVOLVE_HOME'] = '/tmp/hevolve-test'
        try:
            home = ws._whatsapp_home()
            self.assertTrue(str(home).endswith(os.path.join(
                'hevolve-test', 'whatsapp')))
        finally:
            os.environ.pop('HEVOLVE_HOME', None)


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    """The gateway's npm setup with npm mocked at its one boundary
    (model_storage.run_bounded) and the bundled project copied, so a test
    can change it.  HEVOLVE_HOME is a tmp dir.

    Regression for the 2026-05-17..23 production failure: the gateway ran
    npm install inside the read-only Program Files install path (EPERM every
    boot, WhatsApp unreachable).  The gateway is set up in
    HEVOLVE_HOME/whatsapp/gateway, user-writable, by the one npm installer
    (model_storage.install_npm_project, amendment A18/A20)."""
    home = tmp_path / 'home'
    monkeypatch.setenv('HEVOLVE_HOME', str(home))
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    real = ws._bundled_gateway_dir()
    for name in ('gateway.js', 'package.json', 'package-lock.json'):
        shutil.copy2(str(real / name), str(bundle / name))
    monkeypatch.setattr(ws, '_bundled_gateway_dir', lambda: bundle)
    monkeypatch.setattr(model_storage, 'find_node', lambda beside=None: '/opt/node/bin/node')
    monkeypatch.setattr(model_storage, 'find_npm', lambda: '/opt/node/bin/npm')
    runs = []
    answer = {'npm': BoundedResult(0, 'added 80 packages', '')}

    def run_bounded(cmd, timeout=5.0, **kw):
        runs.append(SimpleNamespace(cmd=list(cmd), timeout=timeout, cwd=kw.get('cwd')))
        if answer['npm'].returncode == 0:
            os.makedirs(os.path.join(kw['cwd'], 'node_modules', '@whiskeysockets'), exist_ok=True)
        return answer['npm']
    monkeypatch.setattr(model_storage, 'run_bounded', run_bounded)
    return SimpleNamespace(home=home, bundle=bundle, runs=runs, answer=answer)


def test_the_gateway_is_set_up_in_a_writable_dir_under_hevolve_home(gateway):
    gw = ws._gateway_dir()
    assert gw == gateway.home / 'whatsapp' / 'gateway'
    assert gw.resolve() != ws._bundled_gateway_dir().resolve()
    assert ws.ensure_baileys_deps() is True
    # The project's files are staged where npm runs, never in the bundle.
    for name in ('gateway.js', 'package.json', 'package-lock.json'):
        assert (gw / name).read_bytes() == (gateway.bundle / name).read_bytes(), name
    [npm] = gateway.runs
    assert os.path.samefile(npm.cwd, gw) and 'install' in npm.cmd
    assert not (gateway.bundle / 'node_modules').exists()


def test_the_gateway_install_keeps_its_180_second_bound(gateway):
    """It runs in the HARTOS bootstrap thread, before Nunba's chat start."""
    ws.ensure_baileys_deps()
    assert [r.timeout for r in gateway.runs] == [ws.GATEWAY_NPM_TIMEOUT_S] == [180.0]
    assert '--prefer-offline' in gateway.runs[0].cmd


def test_a_second_start_installs_nothing(gateway):
    assert ws.ensure_baileys_deps() is True
    gateway.runs.clear()
    assert ws.ensure_baileys_deps() is True
    assert gateway.runs == []


def test_a_changed_package_json_installs_again(gateway):
    """The drift the review of 38a220495 found: the old copy returned True
    as soon as node_modules existed, so a HARTOS update that changed
    package.json (staged by _gateway_dir) never reinstalled.  The one
    installer's receipt names the project's files."""
    assert ws.ensure_baileys_deps() is True
    doc = json.loads((gateway.bundle / 'package.json').read_text(encoding='utf-8'))
    doc['dependencies']['express'] = '^4.22.0'
    (gateway.bundle / 'package.json').write_text(json.dumps(doc), encoding='utf-8')
    gateway.runs.clear()
    assert ws.ensure_baileys_deps() is True
    assert len(gateway.runs) == 1, 'the changed project is installed again'
    staged = json.loads((ws._gateway_dir() / 'package.json').read_text(encoding='utf-8'))
    assert staged['dependencies']['express'] == '^4.22.0'


def test_a_failed_install_is_false_and_retried_next_start(gateway):
    gateway.answer['npm'] = BoundedResult(1, 'npm ERR! EPERM', '')
    assert ws.ensure_baileys_deps() is False
    gateway.answer['npm'] = BoundedResult(0, 'added 80 packages', '')
    gateway.runs.clear()
    assert ws.ensure_baileys_deps() is True
    assert len(gateway.runs) == 1


def test_a_timed_out_install_is_false(gateway):
    gateway.answer['npm'] = BoundedResult(-1, '', '', timed_out=True)
    assert ws.ensure_baileys_deps() is False


# ── an install from before the fold (review of c4c73dd41) ────────────────
#
# The old ensure_baileys_deps returned True as soon as node_modules existed,
# so a working gateway was never set up again.  Its folder holds gateway.js
# and package.json (copied), npm's own package-lock.json and node_modules,
# and no receipt; the first start after the fold therefore runs one setup
# there.  With the shipped lock now matching package.json, npm installs that
# locked tree; measured on a copy of a live gateway (baileys 7.0.0-rc13):
# online with an empty npm cache 'added 1 package, removed 1 package, and
# changed 28 packages in 6s'; offline with an empty cache npm failed
# ENOTCACHED (exit 1) and left node_modules as it was.

def _an_install_from_before_the_fold(gateway):
    gw = gateway.home / 'whatsapp' / 'gateway'
    baileys = gw / 'node_modules' / '@whiskeysockets' / 'baileys'
    baileys.mkdir(parents=True)
    (baileys / 'package.json').write_text('{"version": "7.0.0-rc13"}')
    for name in ('gateway.js', 'package.json'):
        shutil.copy2(str(gateway.bundle / name), str(gw / name))
    (gw / 'package-lock.json').write_text('{"lockfileVersion": 3, "packages": {}}')
    return gw


def test_an_install_from_before_the_fold_is_set_up_once(gateway):
    gw = _an_install_from_before_the_fold(gateway)
    assert ws.ensure_baileys_deps() is True
    [npm] = gateway.runs
    assert os.path.samefile(npm.cwd, gw) and '--prefer-offline' in npm.cmd
    # The shipped lock is what npm installs from; npm's packages stay.
    assert (gw / 'package-lock.json').read_bytes() == \
        (gateway.bundle / 'package-lock.json').read_bytes()
    assert (gw / 'node_modules' / '@whiskeysockets' / 'baileys' / 'package.json').is_file()
    gateway.runs.clear()
    assert ws.ensure_baileys_deps() is True and gateway.runs == [], 'set up: never again'


def test_an_install_from_before_the_fold_keeps_running_when_its_setup_fails(gateway, caplog):
    """The old code started the gateway on the node_modules it had; so does
    this when the setup fails (offline, say), and says why.  No receipt is
    written, so the next start tries the setup again."""
    _an_install_from_before_the_fold(gateway)
    gateway.answer['npm'] = BoundedResult(1, '', 'npm error code ENOTCACHED')
    with caplog.at_level('WARNING', logger=ws.logger.name):
        assert ws.ensure_baileys_deps() is True
    assert 'ENOTCACHED' in caplog.text and 'node_modules' in caplog.text
    gateway.runs.clear()
    assert ws.ensure_baileys_deps() is True
    assert len(gateway.runs) == 1, 'no receipt: the next start sets it up again'


@pytest.mark.parametrize('error', [PermissionError(13, 'Access is denied'),
                                   RuntimeError('a bug in the installer')])
def test_no_failure_of_the_setup_escapes(gateway, monkeypatch, error):
    """ensure_baileys_deps runs in the HARTOS bootstrap thread: whatever the
    setup raises is a failed setup, never an exception into start()."""
    def boom(*a, **k):
        raise error
    monkeypatch.setattr(model_storage.ModelStorageManager, 'install_npm_project', boom)
    assert ws.ensure_baileys_deps() is False, 'nothing set up before: no gateway'
    _an_install_from_before_the_fold(gateway)
    assert ws.ensure_baileys_deps() is True, 'a working install keeps running'


def test_the_supervisor_keeps_no_npm_installer_of_its_own(gateway, monkeypatch):
    """One installer (A18): the gateway's npm runs only through
    model_storage, so with model_storage's runner refusing, nothing else
    launches npm."""
    import subprocess as sp
    spawned = []
    monkeypatch.setattr(sp, 'run', lambda *a, **k: spawned.append(a) or None)
    monkeypatch.setattr(sp, 'Popen', lambda *a, **k: spawned.append(a) or None)
    assert ws.ensure_baileys_deps() is True
    assert spawned == [] and len(gateway.runs) == 1


class StartSupervisorTests(unittest.TestCase):

    def test_skipped_when_gated_off(self):
        with mock.patch.object(
            ws, 'supervisor_should_run', return_value=False
        ):
            info = ws.start_supervisor()
        self.assertFalse(info['running'])
        self.assertEqual(info['reason'], 'gated_off')

    def test_skipped_when_node_missing(self):
        # supervisor_should_run True, but the Node locator (core.node_bin,
        # imported as find_node) finds none → last_error is the "install
        # Node" hint, no spawn attempted.
        with (
            mock.patch.object(ws, 'supervisor_should_run', return_value=True),
            mock.patch.object(ws, 'find_node', return_value=None),
            mock.patch.object(ws, 'ensure_baileys_deps', return_value=False),
        ):
            # Reset the singleton so this test runs cleanly even after
            # a previous test left it instantiated.
            ws._supervisor = None
            info = ws.start_supervisor()
        self.assertFalse(info.get('running'))
        self.assertIn('Node.js', info.get('last_error', ''))


if __name__ == '__main__':
    unittest.main()
