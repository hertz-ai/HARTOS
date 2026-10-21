"""A downloaded GGUF is complete by ONE rule, kept beside the weight table.

The rule was Nunba's (LlamaInstaller.get_model_path + _is_gguf_complete): a
weight file counts only when it is at least 90% of its registered size --
never under 100 MB -- and starts with the GGUF magic.  The 90% is of the size
in MODEL_WEIGHT_BYTES (Nunba reads it through model_weight_bytes), so a rule
that needs the table can only live where the table does: here.  HARTOS had
two copies of part of it: hf_model_resolver._validate_gguf (the magic only)
and nothing at all in the 0.8B's own lookup, which took any file of the right
name.

These tests write real bytes in a temp dir.  Sizes that would mean writing
half a gigabyte are scaled down through the table itself (a registered size
of 1000 bytes) and the floor (GGUF_MIN_BYTES), never by stubbing the rule.

    python -m pytest tests/unit/test_gguf_complete_is_one_rule.py -q
"""
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

import integrations.service_tools.model_catalog as mc


def _write(path, size, magic=b'GGUF'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(magic + b'\0' * (size - len(magic)))
    return path


# ── the floor: 90% of the registered size, never under 100 MB ────────────

def test_the_floor_is_ninety_percent_of_the_registered_size():
    """Nunba's get_model_path: max(100_000_000, int(expected * 0.90)).
    The 0.8B's measured 558,772,480 bytes give 502,895,232."""
    assert mc.gguf_min_bytes(558_772_480) == 502_895_232
    assert mc.gguf_min_bytes(2_912_109_728) == int(2_912_109_728 * 0.90)


@pytest.mark.parametrize('expected', [None, 0, 50_000_000])
def test_an_unknown_or_small_size_leaves_the_100_mb_floor(expected):
    """No registered size (None, or Nunba's 0 for a preset the table does
    not know) and a size whose 90% is under 100 MB both give the floor."""
    assert mc.gguf_min_bytes(expected) == 100_000_000


def test_the_floor_is_read_when_asked():
    """A module value, read at call time (so the 0.8B lookup's tests can
    scale it down with real files rather than stub the rule)."""
    old = mc.GGUF_MIN_BYTES
    try:
        mc.GGUF_MIN_BYTES = 0
        assert mc.gguf_min_bytes(1000) == 900
    finally:
        mc.GGUF_MIN_BYTES = old


# ── the check: a file, at least that big, starting with the magic ────────

def test_a_file_of_at_least_the_floor_with_the_magic_is_complete(tmp_path):
    path = _write(tmp_path / 'w.gguf', 900)
    assert mc.gguf_complete(path, 900) is True
    assert mc.gguf_complete(str(path), 900) is True          # str or Path


def test_one_byte_short_of_the_floor_is_a_partial_download(tmp_path):
    assert mc.gguf_complete(_write(tmp_path / 'w.gguf', 899), 900) is False


@pytest.mark.parametrize('magic', [b'GGML', b'\0\0\0\0', b'<htm'])
def test_any_other_first_four_bytes_is_not_a_gguf(tmp_path, magic):
    """A truncated or wrong file at any size (an HTML error page saved under
    the weight's name is the common one)."""
    assert mc.gguf_complete(_write(tmp_path / 'w.gguf', 900, magic), 0) is False


def test_without_a_floor_only_the_magic_is_asked(tmp_path):
    """min_bytes defaults to 0: the magic alone, which is what
    hf_model_resolver asks of a file it is about to reuse."""
    assert mc.gguf_complete(_write(tmp_path / 'tiny.gguf', 4)) is True


@pytest.mark.parametrize('what', ['missing', 'directory', 'none'])
def test_nothing_usable_is_false_and_never_raises(tmp_path, what):
    target = {'missing': tmp_path / 'absent.gguf', 'directory': tmp_path,
              'none': None}[what]
    assert mc.gguf_complete(target, 0) is False


# ── hf_model_resolver asks the same rule, not its own magic read ─────────

@pytest.fixture
def resolver(tmp_path, monkeypatch):
    """An HFModelResolver with its home at tmp_path, a fake huggingface_hub
    whose download writes ``fetched`` bytes, and no manifest writes."""
    from integrations.service_tools.hf_model_resolver import HFModelResolver
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    calls = []

    def hf_hub_download(repo_id, filename, local_dir, **kw):
        calls.append(filename)
        return str(_write(tmp_path / 'fetched' / filename, 64))
    hub = types.ModuleType('huggingface_hub')
    hub.hf_hub_download = hf_hub_download
    monkeypatch.setitem(sys.modules, 'huggingface_hub', hub)
    r = HFModelResolver()
    monkeypatch.setattr(r, '_get_storage', lambda: MagicMock())
    r.calls = calls
    r.local = (tmp_path / '.hevolve' / 'models' / 'gguf' / 'org--repo-GGUF'
               / 'm-Q4_K_M.gguf')
    return r


def test_a_gguf_already_there_is_reused(resolver):
    _write(resolver.local, 64)
    assert resolver.download('org/repo-GGUF', 'm-Q4_K_M.gguf') == resolver.local
    assert resolver.calls == []


def test_a_non_gguf_already_there_is_replaced(resolver):
    _write(resolver.local, 64, b'<htm')
    resolver.download('org/repo-GGUF', 'm-Q4_K_M.gguf')
    assert resolver.calls == ['m-Q4_K_M.gguf']
    assert not resolver.local.exists(), 'the non-GGUF was kept'


def test_the_resolver_asks_the_one_rule(resolver, monkeypatch):
    """Its own copy of the magic read (_validate_gguf) is gone: what the
    rule says is what the resolver does, even for a file whose bytes say
    GGUF."""
    _write(resolver.local, 64)
    asked = []

    def rule(path, min_bytes=0):
        asked.append((os.path.basename(str(path)), min_bytes))
        return False
    monkeypatch.setattr(mc, 'gguf_complete', rule)
    resolver.download('org/repo-GGUF', 'm-Q4_K_M.gguf')
    assert asked and asked[0] == ('m-Q4_K_M.gguf', 0)
    assert resolver.calls == ['m-Q4_K_M.gguf']
