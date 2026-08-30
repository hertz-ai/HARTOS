"""SmartLedger must PROVE its directory is writable, not assume it.

WHAT WENT WRONG
  SmartLedger defaults ledger_dir to the bare relative "agent_data". On a
  deployed node the process cwd is the nix store, so that resolves inside a
  read-only path. There was already a fallback to core.platform_paths for
  exactly this, but it was guarded on mkdir RAISING:

      try:
          self.ledger_dir.mkdir(parents=True, exist_ok=True)
      except OSError:
          ...fall back...

  `exist_ok=True` succeeds on a directory that already exists and is read-only.
  So mkdir returned cleanly, the fallback never ran, and the failure surfaced
  later at write time where nothing caught it. The real box logged this on every
  goal dispatch, 2026-08-27:

      [JSONBackend] Save error: [Errno 30] Read-only file system:
      'agent_data/ledger_cab370e0-..._6b3a997a-....json'

  The path in that message is still the RELATIVE one, which is the proof the
  fallback was never reached. Every ledger write the agent daemon attempted was
  silently lost, while the daemon kept dispatching goals.

THE RULE
  Writability is a property you test, not one you infer from mkdir. Probe it.

Run:
  pytest tests/unit/test_ledger_dir_writability.py -v --noconftest
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "agent-ledger-opensource"))

from agent_ledger.core import SmartLedger  # noqa: E402


def test_falls_back_when_the_dir_exists_but_is_not_writable(tmp_path, monkeypatch):
    """THE regression: the directory EXISTS, so mkdir succeeds, but writes fail."""
    ro = tmp_path / "agent_data"
    ro.mkdir()
    fallback = tmp_path / "fallback"

    real_touch = os.close

    # Make any write inside `ro` fail the way a read-only mount does, while
    # leaving mkdir(exist_ok=True) succeeding, which is the exact deployed shape.
    import pathlib
    orig_touch = pathlib.Path.touch

    def fake_touch(self, *a, **kw):
        if str(ro) in str(self):
            raise OSError(30, "Read-only file system")
        return orig_touch(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "touch", fake_touch)

    import core.platform_paths as pp  # noqa
    monkeypatch.setattr(pp, "get_agent_data_dir", lambda: str(fallback))

    led = SmartLedger("agent-1", "sess-1", ledger_dir=str(ro))
    assert str(led.ledger_dir) != str(ro), (
        "SmartLedger kept a directory it cannot write to. mkdir(exist_ok=True) "
        "succeeding on an existing read-only dir is exactly how the box lost "
        "every ledger write.")


def test_uses_the_dir_when_it_is_genuinely_writable(tmp_path):
    """Guard the other direction: a good directory must be kept, not replaced."""
    good = tmp_path / "ledgers"
    led = SmartLedger("agent-2", "sess-2", ledger_dir=str(good))
    assert str(led.ledger_dir) == str(good)
    assert good.is_dir()


def test_the_probe_file_is_cleaned_up(tmp_path):
    """The writability probe must not leave litter next to real ledgers."""
    d = tmp_path / "ledgers"
    SmartLedger("agent-3", "sess-3", ledger_dir=str(d))
    assert not list(d.glob(".hart-write-probe")), (
        "the write probe was left behind in the ledger directory")


def test_construction_never_raises_even_with_no_writable_path(tmp_path, monkeypatch):
    """Losing the ledger is bad; killing the agent daemon at construction is
    worse. The box was dispatching goals while these writes failed."""
    import pathlib
    orig_mkdir = pathlib.Path.mkdir

    def fake_mkdir(self, *a, **kw):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(pathlib.Path, "mkdir", fake_mkdir)
    led = SmartLedger("agent-4", "sess-4", ledger_dir=str(tmp_path / "nope"))
    assert led.ledger_dir is not None
