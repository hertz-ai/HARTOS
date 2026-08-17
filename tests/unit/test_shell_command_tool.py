"""Tests for the Shell_Command LangChain tool handler.

Covers the three responsibilities:
  1. Runs plain commands and captures stdout/stderr/exit code.
  2. Honours 'powershell:' / 'bash:' / 'cmd:' shell selectors.
  3. Refuses destructive patterns on the denylist.
  4. Applies a 30s timeout.
  5. Truncates long output.
  6. Handles missing interpreters and unexpected errors gracefully.
"""

import subprocess
import sys

import pytest
from unittest.mock import patch, MagicMock

# Import the handler directly — no need to spin up the full LangChain
# tool wrapper for unit coverage.
import pytest
try:
    from hart_intelligence_entry import _handle_shell_command_tool
    _has_handler = True
except Exception:
    _has_handler = False
    _handle_shell_command_tool = None

pytestmark = pytest.mark.skipif(
    not _has_handler,
    reason="hart_intelligence_entry import failed (missing deps in CI)"
)


# ═══════════════════════════════════════════════════════════════════════════
# Happy path: echo + exit code
# ═══════════════════════════════════════════════════════════════════════════


class TestShellCommandHappyPath:
    def test_empty_input_returns_usage_hint(self):
        result = _handle_shell_command_tool('')
        assert 'empty input' in result.lower()

    def test_none_input_returns_usage_hint(self):
        result = _handle_shell_command_tool(None)
        assert 'empty input' in result.lower()

    @patch('hart_intelligence_entry.subprocess.run')
    def test_simple_echo_returns_stdout_and_exit_code(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='hello world\n', stderr=''
        )
        result = _handle_shell_command_tool('echo hello world')
        assert 'Exit code: 0' in result
        assert 'hello world' in result

    @patch('hart_intelligence_entry.subprocess.run')
    def test_nonzero_exit_surfaces_returncode(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout='', stderr='nope')
        result = _handle_shell_command_tool('false')
        assert 'Exit code: 1' in result
        assert 'nope' in result

    @patch('hart_intelligence_entry.subprocess.run')
    def test_empty_output_reports_no_output(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
        result = _handle_shell_command_tool('true')
        assert '(no output)' in result


# ═══════════════════════════════════════════════════════════════════════════
# Shell selector — 'powershell:', 'bash:', 'cmd:', default
# ═══════════════════════════════════════════════════════════════════════════


class TestShellCommandShellSelector:
    # Faking sys.platform = 'win32' on a Linux host also drags
    # core.subprocess_safe.no_window_kwargs() down its Windows branch, where it
    # calls subprocess.STARTUPINFO() -- a name that does not exist on Linux. The
    # kwargs are splatted INTO the subprocess.run(...) call, so the AttributeError
    # fires before run() is ever entered and the handler's `except Exception`
    # swallows it: mock_run.call_args stayed None and the failure read as
    # "'NoneType' object has no attribute 'args'". These two tests are about
    # shell SELECTION, not about Windows console flags, so stub that helper out.
    @patch('hart_intelligence_entry.no_window_kwargs', return_value={})
    @patch('hart_intelligence_entry.subprocess.run')
    def test_default_on_windows_uses_cmd(self, mock_run, _no_window):
        mock_run.return_value = MagicMock(returncode=0, stdout='x', stderr='')
        with patch.object(sys, 'platform', 'win32'):
            _handle_shell_command_tool('dir')
        argv = mock_run.call_args.args[0]
        assert argv[0].lower() == 'cmd'
        assert argv[1] == '/c'
        assert argv[2] == 'dir'

    @patch('hart_intelligence_entry.subprocess.run')
    def test_default_on_linux_uses_sh(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='x', stderr='')
        with patch.object(sys, 'platform', 'linux'):
            _handle_shell_command_tool('ls')
        argv = mock_run.call_args.args[0]
        assert argv[0] == '/bin/sh'
        assert argv[1] == '-c'
        assert argv[2] == 'ls'

    @patch('hart_intelligence_entry.no_window_kwargs', return_value={})
    @patch('hart_intelligence_entry.subprocess.run')
    def test_powershell_selector_forces_powershell(self, mock_run, _no_window):
        mock_run.return_value = MagicMock(returncode=0, stdout='x', stderr='')
        with patch.object(sys, 'platform', 'win32'):
            _handle_shell_command_tool('powershell: Get-Process')
        argv = mock_run.call_args.args[0]
        assert argv[0].lower() == 'powershell'
        # The selector prefix is stripped before passing the command through
        assert 'Get-Process' in argv[-1]
        assert 'powershell:' not in argv[-1]

    @patch('hart_intelligence_entry.subprocess.run')
    def test_bash_selector_forces_bash(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='x', stderr='')
        with patch.object(sys, 'platform', 'linux'):
            _handle_shell_command_tool('bash: ls -la ~')
        argv = mock_run.call_args.args[0]
        assert argv[0] == 'bash'
        assert argv[-1] == 'ls -la ~'


# ═══════════════════════════════════════════════════════════════════════════
# Denylist — destructive commands must NOT run
# ═══════════════════════════════════════════════════════════════════════════


class TestShellCommandDenylist:
    @pytest.mark.parametrize('cmd', [
        'format C:',
        'format c: /fs:ntfs',
        'rm -rf /',
        'rm -rf ~',
        'rm -rf --no-preserve-root /',
        'del /s /q C:\\Users',
        'mkfs.ext4 /dev/sda1',
        'dd if=/dev/zero of=/dev/sda',
        'shred /important/file',
        ':(){ :|:& };:',                             # fork bomb
        'shutdown -h now',
        'shutdown -r now',
        'Format-Volume -DriveLetter C',
        'Remove-Item -Recurse -Force C:\\Windows',
    ])
    @patch('hart_intelligence_entry.subprocess.run')
    def test_destructive_denied(self, mock_run, cmd):
        result = _handle_shell_command_tool(cmd)
        assert 'refused' in result.lower()
        assert 'destructive pattern' in result.lower()
        # Critically: subprocess.run must NEVER be invoked for a denied command
        mock_run.assert_not_called()

    @pytest.mark.parametrize('cmd', [
        'notepad file.txt',           # benign app launch
        'dir C:\\Users',              # directory listing
        'echo hello',
        'git status',
        'npm install',
        'python script.py',
        'ls -la /tmp',
    ])
    @patch('hart_intelligence_entry.subprocess.run')
    def test_benign_allowed(self, mock_run, cmd):
        mock_run.return_value = MagicMock(returncode=0, stdout='ok', stderr='')
        result = _handle_shell_command_tool(cmd)
        assert 'refused' not in result.lower()
        mock_run.assert_called_once()


class TestShellCommandHomoglyphBypass:
    """Regression: attackers can't evade the denylist with full-width /
    compatibility / zero-width unicode lookalikes.

    Security audit April 2026 flagged this as MEDIUM severity: the old
    denylist used text.lower() + ASCII regex, so 'ｒｍ -rf ~' slipped past
    every pattern. NFKC normalization + zero-width stripping now applies
    to the denylist check only (executed command stays as raw user text,
    so legitimate unicode filenames still work)."""

    @pytest.mark.parametrize('cmd', [
        # Full-width ASCII block (U+FF01..U+FF5E)
        'ｒｍ -rf ~',
        'ｒｍ -rf /',
        'ＲＭ -RF ~',
        # Full-width C: + 'format'
        'ｆｏｒｍａｔ Ｃ:',
        # Mixed: real 'rm' but full-width flag / path
        'rm -rf ／',
        # Zero-width splitters injected inside the command name
        'r\u200cm -rf ~',                 # ZWNJ between r and m
        'r\u200dm -rf ~',                 # ZWJ
        'r\ufeffm -rf ~',                 # BOM
        # NBSP instead of space
        'rm\u00a0-rf\u00a0~',
        # Full-width shutdown
        'ｓｈｕｔｄｏｗｎ -h now',
        # Full-width del /s /q
        'ｄｅｌ /s /q Ｃ:\\Users',
    ])
    @patch('hart_intelligence_entry.subprocess.run')
    def test_homoglyph_bypass_blocked(self, mock_run, cmd):
        result = _handle_shell_command_tool(cmd)
        assert 'refused' in result.lower(), f'bypass slipped through: {cmd!r}'
        assert 'destructive pattern' in result.lower()
        mock_run.assert_not_called()

    @pytest.mark.parametrize('cmd', [
        # Ligature in filename should still reach subprocess unchanged.
        # (Not a denylist hit — just making sure legitimate unicode
        # doesn't get mangled by the check path.)
        'cat ﬁle.txt',
        # Full-width in non-denylisted contexts
        'echo ｈｅｌｌｏ',
        # Japanese filename
        'cat テスト.txt',
        # Emoji in echo
        'echo 🚀 deploy',
    ])
    @patch('hart_intelligence_entry.subprocess.run')
    def test_legitimate_unicode_still_allowed(self, mock_run, cmd):
        mock_run.return_value = MagicMock(returncode=0, stdout='ok', stderr='')
        result = _handle_shell_command_tool(cmd)
        assert 'refused' not in result.lower()
        mock_run.assert_called_once()

    @patch('hart_intelligence_entry.subprocess.run')
    def test_raw_text_is_executed_not_normalized(self, mock_run):
        """If the raw command contains legitimate unicode filename chars,
        subprocess must receive the RAW bytes, not the NFKC-normalized
        version. Otherwise 'cat ﬁle.txt' (U+FB01) would execute as
        'cat file.txt' and fail to find the real ligature file."""
        mock_run.return_value = MagicMock(returncode=0, stdout='ok', stderr='')
        cmd = 'cat ﬁle.txt'
        _handle_shell_command_tool(cmd)
        call_args = mock_run.call_args
        argv = call_args[0][0]  # first positional arg is the argv list
        # argv is ['cmd', '/c', <text>] or ['/bin/sh', '-c', <text>]
        executed = argv[-1]
        assert 'ﬁ' in executed, (
            f'Normalized text was executed instead of raw — '
            f'expected ligature ﬁ in {executed!r}'
        )

    @patch('hart_intelligence_entry.subprocess.run')
    def test_denylist_check_is_case_insensitive_after_normalize(self, mock_run):
        """Mixed-case full-width should still hit the denylist."""
        result = _handle_shell_command_tool('Ｒm -RF ~')
        assert 'refused' in result.lower()
        mock_run.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Timeout handling
# ═══════════════════════════════════════════════════════════════════════════


class TestShellCommandTimeout:
    @patch('hart_intelligence_entry.subprocess.run',
           side_effect=subprocess.TimeoutExpired(cmd='sleep', timeout=30))
    def test_timeout_returns_explanation(self, mock_run):
        result = _handle_shell_command_tool('sleep 60')
        assert 'timed out' in result.lower()
        assert '30s' in result

    @patch('hart_intelligence_entry.subprocess.run')
    def test_timeout_arg_is_30_seconds(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
        _handle_shell_command_tool('echo x')
        assert mock_run.call_args.kwargs.get('timeout') == 30


# ═══════════════════════════════════════════════════════════════════════════
# Output truncation
# ═══════════════════════════════════════════════════════════════════════════


class TestShellCommandTruncation:
    @patch('hart_intelligence_entry.subprocess.run')
    def test_long_stdout_truncated_to_2000_chars(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='A' * 5000, stderr=''
        )
        result = _handle_shell_command_tool('cat huge.log')
        # The 2000-char cap per stream means body ≤ 2000 chars of 'A's
        a_count = result.count('A')
        assert a_count <= 2000

    @patch('hart_intelligence_entry.subprocess.run')
    def test_stderr_surfaces_alongside_stdout(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout='out data', stderr='error data'
        )
        result = _handle_shell_command_tool('mixed')
        assert 'out data' in result
        assert '[stderr]' in result
        assert 'error data' in result


# ═══════════════════════════════════════════════════════════════════════════
# Error handling
# ═══════════════════════════════════════════════════════════════════════════


class TestShellCommandErrorHandling:
    @patch('hart_intelligence_entry.subprocess.run',
           side_effect=FileNotFoundError('pwsh not found'))
    def test_missing_interpreter_returns_explanation(self, mock_run):
        result = _handle_shell_command_tool('powershell: Get-Process')
        assert 'interpreter not found' in result.lower()

    @patch('hart_intelligence_entry.subprocess.run',
           side_effect=OSError('permission denied'))
    def test_unexpected_oserror_is_wrapped(self, mock_run):
        result = _handle_shell_command_tool('some-cmd')
        assert 'Shell_Command error' in result
        assert 'OSError' in result or 'permission denied' in result
