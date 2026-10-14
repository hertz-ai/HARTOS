"""core.node_bin: the one answer to "where is Node.js?".

Two copies answered it before (amendment A14): the coding-tool launcher
(integrations/coding_agent/installer.launch_argv looked for the node.exe npm
installs beside its shims, else PATH) and the WhatsApp gateway supervisor
(ensure_node / ensure_npm, PATH only).  Reel Studio's animated overlays are a
third caller.  These tests drive the locator and both former owners through
it, with shutil.which as the only boundary.
"""
import os
import shutil
import sys

import pytest


def _which(table):
    """A shutil.which stand-in answering from ``table`` (one positional name,
    the shape every caller here uses)."""
    return lambda name: table.get(name)


# ── the locator ───────────────────────────────────────────────────────────

def test_the_node_beside_a_directory_wins_over_path(tmp_path, monkeypatch):
    from core.node_bin import find_node
    name = 'node.exe' if sys.platform == 'win32' else 'node'
    local = tmp_path / name
    local.write_bytes(b'')
    monkeypatch.setattr(shutil, 'which', _which({'node': '/elsewhere/node'}))
    assert find_node(beside=str(tmp_path)) == str(local)


def test_without_one_beside_it_is_the_path_node(tmp_path, monkeypatch):
    from core.node_bin import find_node
    monkeypatch.setattr(shutil, 'which', _which({'node': '/opt/node/bin/node'}))
    assert find_node(beside=str(tmp_path)) == '/opt/node/bin/node'
    assert find_node() == '/opt/node/bin/node'


def test_no_node_anywhere_is_none(tmp_path, monkeypatch):
    from core.node_bin import find_node, find_npm
    monkeypatch.setattr(shutil, 'which', _which({}))
    assert find_node() is None
    assert find_node(beside=str(tmp_path)) is None
    assert find_npm() is None


def test_npm_is_the_path_npm(monkeypatch):
    from core.node_bin import find_npm
    monkeypatch.setattr(shutil, 'which', _which({'npm': '/opt/node/bin/npm'}))
    assert find_npm() == '/opt/node/bin/npm'


def test_a_directory_named_node_beside_is_not_taken(tmp_path, monkeypatch):
    from core.node_bin import find_node
    name = 'node.exe' if sys.platform == 'win32' else 'node'
    (tmp_path / name).mkdir()
    monkeypatch.setattr(shutil, 'which', _which({'node': '/opt/node/bin/node'}))
    assert find_node(beside=str(tmp_path)) == '/opt/node/bin/node'


# ── the former owners read it ─────────────────────────────────────────────

def _shim(tmp_path, name, target_line):
    shim = tmp_path / ('%s.cmd' % name)
    shim.write_text('@ECHO off\r\nSETLOCAL\r\nCALL :find_dp0\r\n' + target_line + '\r\n')
    return str(shim)


def test_launch_argv_runs_an_npm_shim_with_the_node_beside_it(tmp_path, monkeypatch):
    """The coding-tool launcher's own case: npm's Windows installer puts
    node.exe in the shim's folder, and that Node runs the shim's script."""
    from integrations.coding_agent import installer
    shim = _shim(tmp_path, 'pi', r'"%_prog%"  "%dp0%\node_modules\pi\dist\cli.js" %*')
    (tmp_path / 'node.exe').write_bytes(b'')
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(shutil, 'which', _which({'pi': shim, 'node': r'C:\other\node.exe'}))
    argv = installer.launch_argv(['pi', '-p', 'task'])
    assert argv == [str(tmp_path / 'node.exe'),
                    os.path.join(str(tmp_path), r'node_modules\pi\dist\cli.js'), '-p', 'task']


def test_the_coding_tool_installer_asks_the_locator_for_npm(monkeypatch):
    from integrations.coding_agent import installer
    monkeypatch.setattr(shutil, 'which', _which({}))
    result = installer.install('pi')
    assert result['success'] is False
    assert 'npm not found' in result['error']


def test_the_whatsapp_gateway_launches_the_located_node(monkeypatch, tmp_path):
    from integrations.social import whatsapp_supervisor as ws
    monkeypatch.setenv('HEVOLVE_HOME', str(tmp_path))
    monkeypatch.setattr(shutil, 'which', _which({'node': '/opt/node/bin/node'}))
    cmd, _kwargs = ws._Supervisor()._build_popen()
    assert cmd[0] == '/opt/node/bin/node'


def test_the_whatsapp_gateway_says_node_is_missing(monkeypatch, tmp_path):
    from integrations.social import whatsapp_supervisor as ws
    monkeypatch.setenv('HEVOLVE_HOME', str(tmp_path))
    monkeypatch.setattr(shutil, 'which', _which({}))
    sup = ws._Supervisor()
    info = sup.start()
    assert info['running'] is False
    assert 'Node.js' in (info['last_error'] or '')


def test_the_whatsapp_deps_need_the_located_npm(monkeypatch, tmp_path):
    from integrations.social import whatsapp_supervisor as ws
    monkeypatch.setenv('HEVOLVE_HOME', str(tmp_path))
    monkeypatch.setattr(ws, '_bundled_gateway_dir', lambda: tmp_path / 'bundle')
    monkeypatch.setattr(shutil, 'which', _which({}))
    assert ws.ensure_baileys_deps() is False


@pytest.mark.parametrize('module, gone', [
    ('integrations.social.whatsapp_supervisor', ('ensure_node', 'ensure_npm')),
])
def test_the_folded_copies_are_gone(module, gone):
    """One answer: the supervisor keeps no locator of its own."""
    import importlib
    mod = importlib.import_module(module)
    for name in gone:
        assert not hasattr(mod, name), '%s still defines %s' % (module, name)
