"""Claw-code upstream compatibility tests.

These tests verify that the hart-bridge PyO3 crate is compatible with the
current claw-code source. They run at build time and block upgrades if the
bridge's Rust FFI contract is broken.

The contract: hart-bridge calls these runtime functions with these signatures.
If claw-code renames, removes, or changes parameter types, these tests fail
BEFORE the broken code reaches production.

Run: pytest tests/unit/test_claw_compat.py -v --noconftest
"""
import json
import os
import subprocess
import tempfile

import pytest

# ═══════════════════════════════════════════════════════════════════════
# Gate 1: Rust compilation — does hart-bridge still compile against upstream?
# ═══════════════════════════════════════════════════════════════════════

CLAW_RUST_DIR = os.path.join(
    os.path.dirname(__file__), '..', '..', 'claw_native', 'rust'
)


class TestRustCompilation:
    """Verify hart-bridge compiles against current claw-code source."""

    @pytest.fixture(autouse=True)
    def _check_cargo(self):
        if not os.path.isfile(os.path.join(CLAW_RUST_DIR, 'Cargo.toml')):
            pytest.skip("claw_native/rust not present")
        # Check cargo is available
        try:
            subprocess.run(['cargo', '--version'], capture_output=True, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            pytest.skip("cargo not installed")

    def test_hart_bridge_compiles(self):
        """The bridge crate must compile against current upstream crates."""
        result = subprocess.run(
            ['cargo', 'check', '--package', 'hart-bridge'],
            cwd=CLAW_RUST_DIR,
            capture_output=True,
            text=True,
            timeout=300,  # Cold build can take 2-3 min; incremental is <10s
        )
        assert result.returncode == 0, (
            f"hart-bridge compilation failed — upstream API changed?\n"
            f"stderr:\n{result.stderr[-500:]}"
        )


# ═══════════════════════════════════════════════════════════════════════
# Gate 2: Python FFI — does claw_bridge.pyd still expose expected functions?
# ═══════════════════════════════════════════════════════════════════════

class TestPythonFFI:
    """Verify claw_bridge exposes the expected function signatures."""

    @pytest.fixture(autouse=True)
    def _check_bridge(self):
        try:
            import claw_bridge
            self.bridge = claw_bridge
        except ImportError:
            pytest.skip("claw_bridge.pyd not compiled")

    EXPECTED_FUNCTIONS = [
        'execute_bash',
        'read_file',
        'write_file',
        'edit_file',
        'glob_search',
        'grep_search',
    ]

    def test_all_functions_exist(self):
        """Every function the HARTOS backend calls must exist."""
        missing = [f for f in self.EXPECTED_FUNCTIONS
                   if not hasattr(self.bridge, f)]
        assert not missing, f"Missing functions in claw_bridge: {missing}"

    def test_execute_bash_signature(self):
        """execute_bash(command: str, timeout_ms: int) -> str"""
        result = self.bridge.execute_bash('echo compat_test')
        parsed = json.loads(result)
        assert 'stdout' in parsed, "execute_bash must return JSON with 'stdout'"
        assert 'interrupted' in parsed, "execute_bash must return JSON with 'interrupted'"

    def test_read_file_signature(self):
        """read_file(path: str, offset: int, limit: int) -> str"""
        result = self.bridge.read_file(__file__, 0, 5)
        parsed = json.loads(result)
        assert 'file' in parsed, "read_file must return JSON with 'file'"

    def test_write_file_signature(self):
        """write_file(path: str, content: str) -> str"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write('test')
            path = f.name
        try:
            result = self.bridge.write_file(path, 'compat_test')
            parsed = json.loads(result)
            assert 'filePath' in parsed or 'file_path' in parsed, \
                "write_file must return JSON with file path"
        finally:
            os.unlink(path)

    def test_edit_file_signature(self):
        """edit_file(path: str, old: str, new: str, replace_all: bool) -> str"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write('old_text here')
            path = f.name
        try:
            result = self.bridge.edit_file(path, 'old_text', 'new_text')
            parsed = json.loads(result)
            assert 'filePath' in parsed or 'file_path' in parsed, \
                "edit_file must return JSON with file path"
        finally:
            os.unlink(path)

    def test_glob_search_signature(self):
        """glob_search(pattern: str, path: str) -> str"""
        result = self.bridge.glob_search('*.py', os.path.dirname(__file__))
        parsed = json.loads(result)
        assert 'numFiles' in parsed or 'num_files' in parsed, \
            "glob_search must return JSON with file count"

    def test_grep_search_signature(self):
        """grep_search(pattern: str, path: str, glob: str, case_insensitive: bool) -> str"""
        result = self.bridge.grep_search('def test_', __file__)
        parsed = json.loads(result)
        assert 'numFiles' in parsed or 'num_files' in parsed, \
            "grep_search must return JSON with file count"


# ═══════════════════════════════════════════════════════════════════════
# Gate 3: Version pin — is the upstream commit what we expect?
# ═══════════════════════════════════════════════════════════════════════

class TestLicenseAndPin:
    """Verify license and pin metadata (works without .git)."""

    CLAW_ROOT = os.path.join(CLAW_RUST_DIR, '..')

    def _get_pinned(self):
        pinned_path = os.path.join(self.CLAW_ROOT, 'PINNED.json')
        if not os.path.exists(pinned_path):
            pytest.skip("PINNED.json not found")
        with open(pinned_path) as f:
            return json.load(f)

    def test_license_still_mit(self):
        """Upstream license must remain MIT — if changed, we must re-evaluate."""
        pinned = self._get_pinned()
        assert pinned.get('license') == 'MIT', "PINNED.json license field is not MIT"

        # Check actual LICENSE file in the Rust workspace
        for license_path in [
            os.path.join(CLAW_RUST_DIR, 'LICENSE'),
            os.path.join(self.CLAW_ROOT, 'LICENSE'),
        ]:
            if os.path.exists(license_path):
                with open(license_path, encoding='utf-8', errors='ignore') as f:
                    content = f.read(500).lower()
                assert 'mit' in content, (
                    f"License file {license_path} does not contain 'MIT'. "
                    f"Upstream may have changed license — STOP and review before upgrading."
                )
                return
        # No LICENSE file found — check Cargo.toml
        cargo_path = os.path.join(CLAW_RUST_DIR, 'Cargo.toml')
        if os.path.exists(cargo_path):
            with open(cargo_path) as f:
                content = f.read()
            assert 'license = "MIT"' in content, (
                "No LICENSE file and Cargo.toml doesn't declare MIT. "
                "Review upstream licensing before upgrading."
            )

    def test_bridge_crate_declared_in_pin(self):
        """PINNED.json must reference the hart-bridge crate."""
        pinned = self._get_pinned()
        assert 'hart-bridge' in pinned.get('bridge_crate', ''), \
            "PINNED.json doesn't reference hart-bridge crate"

    def test_pinned_commit_field_present(self):
        """PINNED.json must have a pinned_commit for provenance tracking."""
        pinned = self._get_pinned()
        commit = pinned.get('pinned_commit', '')
        assert len(commit) >= 7, f"pinned_commit too short: {commit!r}"


class TestVersionPin:
    """Git-dependent version checks (skipped when vendored without .git)."""

    CLAW_ROOT = os.path.join(CLAW_RUST_DIR, '..')

    @pytest.fixture(autouse=True)
    def _check_git(self):
        if not os.path.isdir(os.path.join(self.CLAW_ROOT, '.git')):
            pytest.skip("claw_native vendored without .git — git checks skipped")

    def _get_pinned(self):
        pinned_path = os.path.join(self.CLAW_ROOT, 'PINNED.json')
        if not os.path.exists(pinned_path):
            pytest.skip("PINNED.json not found")
        with open(pinned_path) as f:
            return json.load(f)

    def test_pinned_commit_matches(self):
        """Current HEAD must match PINNED.json commit."""
        pinned = self._get_pinned()
        expected = pinned['pinned_commit']

        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=self.CLAW_ROOT,
            capture_output=True, text=True,
        )
        current = result.stdout.strip()

        assert current.startswith(expected) or expected.startswith(current), (
            f"claw_native HEAD is {current} but PINNED.json expects {expected}. "
            f"Run the upgrade procedure in PINNED.json before merging upstream changes."
        )

    def test_remote_is_canonical(self):
        """Origin must point to the canonical repo, not a fork."""
        pinned = self._get_pinned()
        expected_upstream = pinned.get('upstream', '')

        result = subprocess.run(
            ['git', 'remote', 'get-url', 'origin'],
            cwd=self.CLAW_ROOT,
            capture_output=True, text=True,
        )
        actual = result.stdout.strip()

        assert expected_upstream in actual, (
            f"claw_native remote is {actual} but expected {expected_upstream}. "
            f"Source may be a fork — verify provenance before trusting."
        )

    def test_no_unsigned_commits_since_pin(self):
        """Warn if there are commits beyond the pinned one (local modifications)."""
        pinned = self._get_pinned()
        expected = pinned['pinned_commit']

        result = subprocess.run(
            ['git', 'log', '--oneline', f'{expected[:7]}..HEAD'],
            cwd=self.CLAW_ROOT,
            capture_output=True, text=True,
        )
        extra_commits = result.stdout.strip()
        if extra_commits:
            # Local commits (like hart-bridge) are expected, but flag them
            lines = extra_commits.strip().split('\n')
            non_bridge = [l for l in lines if 'hart-bridge' not in l.lower()
                          and 'PINNED' not in l]
            assert not non_bridge, (
                f"Found {len(non_bridge)} non-bridge commits since pinned: "
                f"{non_bridge}. These may be unauthorized upstream changes."
            )
