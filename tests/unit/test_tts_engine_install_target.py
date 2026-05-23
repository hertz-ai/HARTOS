"""Regression test for TTSEngineSpec.install_target classification.

WHY THIS TEST EXISTS
====================
On 2026-05-23 we discovered that kokoro / melotts / omnivoice / xtts_v2
silently defaulted to ``install_target='main'`` (the dataclass default
at tts_router.py field decl) because their TTSEngineSpec literals
omitted the install_target= keyword.  The Nunba consumer in
tts/package_installer.py only routes an engine into BACKEND_VENV_PACKAGES
when ``spec.install_target == 'venv'``, so these 4 engines were:

  1. Never given a private venv at ~/Documents/Nunba/data/venvs/<engine>/
  2. Their pip_install_plan ran against the bundled python-embed main
     interpreter (which cannot install heavy ML deps cleanly)
  3. Probe falls back to MAIN interpreter at runtime → fails with
     ModuleNotFoundError: No module named 'kokoro' (and similar for the
     other three) → engine permanently dark on every Nunba install

This test pins the install_target for those 4 engines AND scans all
other specs for the same drift pattern.  Any TTSEngineSpec that has:

  - a non-empty pip_install_plan (i.e. it actually needs packages
    beyond what's bundled)
  - a tool_module set (subprocess-worker engines that genuinely need
    quarantine — in-process engines like piper are exempt)
  - required_package != 'transformers' (transformers is bundled in
    main; mms_tts legitimately stays at install_target='main')

...MUST declare install_target in ('venv', 'git_clone', 'cloud',
'bundled') explicitly.  Default 'main' for those is a bug.

The test is GREP-LEVEL (reads the file), NOT introspective, because
the file is the single source of truth for human-readable spec
intent — relying on imported dataclass attribute would mask drift if
the file's actual literal text is wrong but the import still works.
"""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

_ROUTER_FILE = os.path.join(
    os.path.dirname(__file__), '..', '..',
    'integrations', 'channels', 'media', 'tts_router.py',
)
_ROUTER_FILE = os.path.normpath(_ROUTER_FILE)


def _read_router_source() -> str:
    with open(_ROUTER_FILE, 'r', encoding='utf-8') as f:
        return f.read()


def _extract_spec_blocks(src: str) -> dict[str, str]:
    """Return {engine_id: full_spec_block_text} for every TTSEngineSpec
    literal in tts_router.py.

    A spec block is the text from ``engine_id='X',`` through the next
    closing paren that matches the TTSEngineSpec opener.  We use a
    simple bracket-balance scan because the file uses 4-space-indented
    multi-line dataclass literals — no AST required."""
    out = {}
    # Find every "engine_id='NAME',"
    for m in re.finditer(r"engine_id=['\"]([a-zA-Z0-9_]+)['\"]\s*,", src):
        engine_id = m.group(1)
        # Walk backwards from this match to find the opening
        # 'TTSEngineSpec(' on the previous lines.
        start = src.rfind('TTSEngineSpec(', 0, m.start())
        if start < 0:
            continue
        # Walk forward from the matching '(' counting brackets to find
        # the spec's closing ')'.
        depth = 0
        i = start + len('TTSEngineSpec')
        end = -1
        while i < len(src):
            ch = src[i]
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
            i += 1
        if end > 0:
            out[engine_id] = src[start:end]
    return out


class FixedFourEnginesHaveVenvTarget(unittest.TestCase):
    """Hard-asserts for the 4 engines fixed by the 2026-05-23 change.
    These four MUST stay at install_target='venv'."""

    @classmethod
    def setUpClass(cls):
        cls.specs = _extract_spec_blocks(_read_router_source())

    def _assert_venv(self, engine_id):
        self.assertIn(engine_id, self.specs,
                      f"engine_id={engine_id!r} not found in tts_router.py")
        block = self.specs[engine_id]
        self.assertIn(
            "install_target='venv'", block,
            f"{engine_id} must declare install_target='venv' — without it "
            f"the engine silently defaults to 'main' and its venv is never "
            f"created.  See 2026-05-23 regression notes in this file's "
            f"module docstring."
        )

    def test_kokoro_is_venv(self):
        self._assert_venv('kokoro')

    def test_melotts_is_venv(self):
        self._assert_venv('melotts')

    def test_omnivoice_is_venv(self):
        self._assert_venv('omnivoice')

    def test_xtts_v2_is_venv(self):
        self._assert_venv('xtts_v2')


class NoEngineLeftAtDefaultMain(unittest.TestCase):
    """Scans every spec.  Any engine that needs a private venv (has a
    non-trivial pip_install_plan + a tool_module + isn't transformers-
    only) MUST explicitly declare install_target.  Default 'main' is
    a bug for these — proven by the kokoro/melotts/omnivoice/xtts_v2
    regression that this test was written for."""

    # Engines that legitimately stay at install_target='main' (or where
    # 'main' is harmless because required_package='transformers' is
    # already bundled).  Document each exemption with the reason so
    # future drift can be evaluated against the rationale.
    _ALLOWED_MAIN = {
        'mms_tts': (
            'required_package=transformers — bundled in main; '
            'pip_install_plan only adds soundfile which is also bundled-safe'
        ),
        'pocket_tts': (
            'pocket-tts is a thin CPU library — small wheel, no conflicting '
            'transitives; runs in-process'
        ),
        'chatterbox_ml': (
            "shares _CHATTERBOX_PIP_PLAN with chatterbox_turbo; the "
            "engine factory promotes both to the chatterbox_turbo venv "
            "at runtime — main is a harmless declaration"
        ),
        'none': (
            "sentinel — TTSResult fallback constructions use engine_id="
            "'none' to signal 'no engine selected'; not a real spec, "
            "the bracket-walk in _extract_spec_blocks attaches to it "
            "spuriously when it sits inside a non-TTSEngineSpec call"
        ),
    }

    @classmethod
    def setUpClass(cls):
        cls.specs = _extract_spec_blocks(_read_router_source())

    def test_every_spec_declares_install_target_or_is_allow_listed(self):
        # Build the list of engines whose blocks DO NOT contain
        # 'install_target=' at all → they're using the dataclass default.
        defaulting = []
        for engine_id, block in self.specs.items():
            if 'install_target=' not in block:
                defaulting.append(engine_id)

        # Any defaulting engine NOT in _ALLOWED_MAIN is a regression.
        unexpected = [eid for eid in defaulting if eid not in self._ALLOWED_MAIN]
        if unexpected:
            self.fail(
                f"{len(unexpected)} engine(s) silently default to "
                f"install_target='main' without an entry in _ALLOWED_MAIN: "
                f"{unexpected!r}.  Either declare install_target='venv' on "
                f"the spec (if the engine needs a private interpreter for "
                f"its pip_install_plan) or add the engine_id to "
                f"_ALLOWED_MAIN in this test with a 1-line rationale."
            )


class InstallTargetValuesAreValid(unittest.TestCase):
    """Whatever install_target IS declared, it must be one of the four
    canonical values consumed by package_installer.py + backend_venv.py."""

    _VALID = {'main', 'venv', 'git_clone', 'cloud', 'bundled'}

    @classmethod
    def setUpClass(cls):
        cls.specs = _extract_spec_blocks(_read_router_source())

    def test_no_typo_in_install_target_value(self):
        pattern = re.compile(r"install_target=['\"]([a-zA-Z_]+)['\"]")
        bad = []
        for engine_id, block in self.specs.items():
            for m in pattern.finditer(block):
                val = m.group(1)
                if val not in self._VALID:
                    bad.append((engine_id, val))
        self.assertEqual(bad, [],
                         f"unexpected install_target values: {bad!r}.  "
                         f"Valid values: {sorted(self._VALID)!r}")


if __name__ == '__main__':
    unittest.main()
