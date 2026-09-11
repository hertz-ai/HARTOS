"""Tests for the Shell_Command LangChain tool handler.

Covers the three responsibilities:
  1. Runs plain commands and captures stdout/stderr/exit code.
  2. Honours 'powershell:' / 'bash:' / 'cmd:' shell selectors.
  3. Refuses destructive patterns on the denylist.
  4. Applies a 30s timeout.
  5. Truncates long output.
  6. Handles missing interpreters and unexpected errors gracefully.
"""

import sys

import pytest
from unittest.mock import patch

from core.subprocess_safe import BoundedResult

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


def _ran(returncode=0, stdout='', stderr=''):
    """A run_bounded success — the child exited on its own, no kill.

    The handler executes through core.subprocess_safe.run_bounded (D35: a
    plain subprocess.run cannot enforce its own timeout on Windows, see
    tests/unit/test_shell_tool_is_bounded.py).  These fakes therefore return
    the REAL BoundedResult rather than a MagicMock, for two reasons:

      * MagicMock auto-creates `.timed_out` as a truthy Mock, so every one
        of these tests would silently take the timeout branch and assert
        against the wrong string.
      * The real class pins the shape, so a future change to BoundedResult
        breaks these tests instead of letting the handler drift.
    """
    return BoundedResult(returncode=returncode, stdout=stdout,
                         stderr=stderr, timed_out=False)


def _timed_out():
    """What run_bounded returns after killing a child that blew its budget.

    run_bounded NEVER raises TimeoutExpired — it kills, closes the
    parent-side pipes so the reader threads unblock, and reports the kill
    through this flag with empty output.
    """
    return BoundedResult(returncode=-1, stdout='', stderr='', timed_out=True)


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

    @patch('hart_intelligence_entry.run_bounded')
    def test_simple_echo_returns_stdout_and_exit_code(self, mock_run):
        mock_run.return_value = _ran(returncode=0, stdout='hello world\n')
        result = _handle_shell_command_tool('echo hello world')
        assert 'Exit code: 0' in result
        assert 'hello world' in result

    @patch('hart_intelligence_entry.run_bounded')
    def test_nonzero_exit_surfaces_returncode(self, mock_run):
        mock_run.return_value = _ran(returncode=1, stderr='nope')
        result = _handle_shell_command_tool('false')
        assert 'Exit code: 1' in result
        assert 'nope' in result

    @patch('hart_intelligence_entry.run_bounded')
    def test_empty_output_reports_no_output(self, mock_run):
        mock_run.return_value = _ran(returncode=0)
        result = _handle_shell_command_tool('true')
        assert '(no output)' in result


# ═══════════════════════════════════════════════════════════════════════════
# Shell selector — 'powershell:', 'bash:', 'cmd:', default
# ═══════════════════════════════════════════════════════════════════════════


class TestShellCommandShellSelector:
    # These two used to need `no_window_kwargs` stubbed out: faking
    # sys.platform='win32' on a Linux host dragged that helper down its Windows
    # branch, where subprocess.STARTUPINFO() does not exist, and the resulting
    # AttributeError was swallowed by the handler's `except Exception` (the
    # failure read as "'NoneType' object has no attribute 'args'").
    # Since D35 the console flags are applied INSIDE run_bounded, which is
    # mocked here, so that whole hazard is gone with no stub needed.
    @patch('hart_intelligence_entry.run_bounded')
    def test_default_on_windows_uses_cmd(self, mock_run):
        mock_run.return_value = _ran(returncode=0, stdout='x')
        with patch.object(sys, 'platform', 'win32'):
            _handle_shell_command_tool('dir')
        argv = mock_run.call_args.args[0]
        assert argv[0].lower() == 'cmd'
        assert argv[1] == '/c'
        assert argv[2] == 'dir'

    @patch('hart_intelligence_entry.run_bounded')
    def test_default_on_linux_uses_sh(self, mock_run):
        mock_run.return_value = _ran(returncode=0, stdout='x')
        with patch.object(sys, 'platform', 'linux'):
            _handle_shell_command_tool('ls')
        argv = mock_run.call_args.args[0]
        assert argv[0] == '/bin/sh'
        assert argv[1] == '-c'
        assert argv[2] == 'ls'

    @patch('hart_intelligence_entry.run_bounded')
    def test_powershell_selector_forces_powershell(self, mock_run):
        mock_run.return_value = _ran(returncode=0, stdout='x')
        with patch.object(sys, 'platform', 'win32'):
            _handle_shell_command_tool('powershell: Get-Process')
        argv = mock_run.call_args.args[0]
        assert argv[0].lower() == 'powershell'
        # The selector prefix is stripped before passing the command through
        assert 'Get-Process' in argv[-1]
        assert 'powershell:' not in argv[-1]

    @patch('hart_intelligence_entry.run_bounded')
    def test_bash_selector_forces_bash(self, mock_run):
        mock_run.return_value = _ran(returncode=0, stdout='x')
        with patch.object(sys, 'platform', 'linux'):
            _handle_shell_command_tool('bash: ls -la ~')
        argv = mock_run.call_args.args[0]
        assert argv[0] == 'bash'
        assert argv[-1] == 'ls -la ~'


class TestNativeShellInvocationIsUnderstood:
    """The NATIVE CLI form must select the same shell as the colon form.

    D73, live-measured 2026-09-11 on agent 89091774807.  The VLM loop wrote
    the form every model knows::

        powershell -Command "Get-PSDrive -Name C | Select-Object ..."

    The selector only understood ``powershell: <cmd>`` (a COLON), so this
    fell through to the Windows default and ran as::

        cmd /c powershell -Command "Get-PSDrive -Name C | Select-Object ..."

    MEASURED consequence of that nesting — reproduced byte-exact:

        rc     = 0
        stdout = 'Get-PSDrive -Name C | Select-Object -ExpandProperty FreeGB'
        stderr = ''

    Exit 0 with the command echoed back as its own output.  That is worse
    than an error: every honesty gate downstream reads it as SUCCESS.  The
    VLM concluded "The output shows 'FreeGB : 100.0'" — a number present
    nowhere in that output — set exit_reason=done, FAB-GUARD passed the
    action because the tool HAD executed, and the reuse model then told the
    user the machine had 127.4 GB free.  Real figure: 6.32 GB.

    Dispatched directly (the fix), the same command returns rc=1 with
    'Property "FreeGB" cannot be found' on stderr — an honest failure the
    model can act on.

    One concept, one parser: both spellings resolve through the SAME argv
    builder.  A second dispatch path would be the very drift this fixes.
    """

    @patch('hart_intelligence_entry.run_bounded')
    def test_powershell_dash_command_is_not_nested_under_cmd(self, mock_run):
        """THE live failure, verbatim from the 09:55:19 log line."""
        mock_run.return_value = _ran(returncode=0, stdout='x')
        with patch.object(sys, 'platform', 'win32'):
            _handle_shell_command_tool(
                'powershell -Command "Get-PSDrive -Name C | '
                'Select-Object -ExpandProperty FreeGB"')
        argv = mock_run.call_args.args[0]
        assert argv[0].lower() != 'cmd', (
            'nested cmd /c powershell — returns exit 0 with the command '
            'echoed as stdout, which reads as success to every gate above it')
        assert argv[0].lower().startswith('powershell')
        assert 'Get-PSDrive' in argv[-1]
        assert '-Command' not in argv[-1], 'the wrapper was not stripped'

    @patch('hart_intelligence_entry.run_bounded')
    def test_powershell_dash_c_shorthand(self, mock_run):
        mock_run.return_value = _ran(returncode=0, stdout='x')
        with patch.object(sys, 'platform', 'win32'):
            _handle_shell_command_tool('powershell -c "Get-Process"')
        argv = mock_run.call_args.args[0]
        assert argv[0].lower().startswith('powershell')
        assert argv[-1] == 'Get-Process'

    @patch('hart_intelligence_entry.run_bounded')
    def test_powershell_exe_with_noprofile_flags(self, mock_run):
        """Models copy the fully-flagged form from documentation."""
        mock_run.return_value = _ran(returncode=0, stdout='x')
        with patch.object(sys, 'platform', 'win32'):
            _handle_shell_command_tool(
                'powershell.exe -NoProfile -NonInteractive -Command "Get-Date"')
        argv = mock_run.call_args.args[0]
        assert argv[0].lower().startswith('powershell')
        assert argv[-1] == 'Get-Date'

    @patch('hart_intelligence_entry.run_bounded')
    def test_bash_dash_c_native(self, mock_run):
        mock_run.return_value = _ran(returncode=0, stdout='x')
        with patch.object(sys, 'platform', 'linux'):
            _handle_shell_command_tool("bash -c 'ls -la ~'")
        argv = mock_run.call_args.args[0]
        assert argv[0] == 'bash'
        assert argv[-1] == 'ls -la ~'

    @patch('hart_intelligence_entry.run_bounded')
    def test_cmd_slash_c_native_is_not_double_nested(self, mock_run):
        mock_run.return_value = _ran(returncode=0, stdout='x')
        with patch.object(sys, 'platform', 'win32'):
            _handle_shell_command_tool('cmd /c dir C:\\Users')
        argv = mock_run.call_args.args[0]
        assert argv[0].lower() == 'cmd'
        assert argv[-1] == 'dir C:\\Users', (
            'cmd /c cmd /c <x> — the wrapper must be consumed, not stacked')

    # ---- the wrapper must NOT swallow ordinary commands -------------------

    @patch('hart_intelligence_entry.run_bounded')
    def test_a_command_that_merely_mentions_a_shell_is_untouched(self, mock_run):
        """`echo powershell -Command hi` is not a shell selector."""
        mock_run.return_value = _ran(returncode=0, stdout='x')
        with patch.object(sys, 'platform', 'win32'):
            _handle_shell_command_tool('echo powershell -Command hi')
        argv = mock_run.call_args.args[0]
        assert argv[0].lower() == 'cmd'
        assert argv[-1] == 'echo powershell -Command hi'

    # ---- the denylist must be exactly as strong as before -----------------

    @pytest.mark.parametrize('cmd', [
        'powershell -Command "Remove-Item -Recurse -Force C:\\Windows"',
        'powershell -Command "Format-Volume -DriveLetter C"',
        'bash -c "rm -rf /"',
        'cmd /c del /s /q C:\\Users',
        # -enc is obfuscation; it is NOT a -Command form, so it must still
        # reach the denylist on the unstripped string.
        'powershell -enc ZQBjAGgAbwAgAGgAaQA=',
    ])
    @patch('hart_intelligence_entry.run_bounded')
    def test_denylist_still_blocks_through_the_native_wrapper(self, mock_run, cmd):
        """Stripping the wrapper must not open a bypass.

        The patterns are substring searches, so the destructive text is still
        seen after the wrapper is removed.  This pins that — a regression here
        would be a security hole, not a cosmetic one.
        """
        result = _handle_shell_command_tool(cmd)
        assert 'refused' in result.lower(), f'bypass opened: {cmd!r}'
        mock_run.assert_not_called()


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
    @patch('hart_intelligence_entry.run_bounded')
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
    @patch('hart_intelligence_entry.run_bounded')
    def test_benign_allowed(self, mock_run, cmd):
        mock_run.return_value = _ran(returncode=0, stdout='ok')
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
    @patch('hart_intelligence_entry.run_bounded')
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
    @patch('hart_intelligence_entry.run_bounded')
    def test_legitimate_unicode_still_allowed(self, mock_run, cmd):
        mock_run.return_value = _ran(returncode=0, stdout='ok')
        result = _handle_shell_command_tool(cmd)
        assert 'refused' not in result.lower()
        mock_run.assert_called_once()

    @patch('hart_intelligence_entry.run_bounded')
    def test_raw_text_is_executed_not_normalized(self, mock_run):
        """If the raw command contains legitimate unicode filename chars,
        subprocess must receive the RAW bytes, not the NFKC-normalized
        version. Otherwise 'cat ﬁle.txt' (U+FB01) would execute as
        'cat file.txt' and fail to find the real ligature file."""
        mock_run.return_value = _ran(returncode=0, stdout='ok')
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

    @patch('hart_intelligence_entry.run_bounded')
    def test_denylist_check_is_case_insensitive_after_normalize(self, mock_run):
        """Mixed-case full-width should still hit the denylist."""
        result = _handle_shell_command_tool('Ｒm -RF ~')
        assert 'refused' in result.lower()
        mock_run.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Timeout handling
# ═══════════════════════════════════════════════════════════════════════════


class TestShellCommandTimeout:
    @patch('hart_intelligence_entry.run_bounded')
    def test_timeout_returns_explanation(self, mock_run):
        # run_bounded reports a killed child via the flag, never by raising:
        # it has to survive the kill to close the pipes (D35).
        mock_run.return_value = _timed_out()
        result = _handle_shell_command_tool('sleep 60')
        assert 'timed out' in result.lower()
        assert '30s' in result

    @patch('hart_intelligence_entry.run_bounded')
    def test_timeout_is_not_reported_as_a_failed_exit(self, mock_run):
        """The kill must not be laundered into an ordinary non-zero exit.

        BoundedResult carries returncode=-1 on timeout.  A handler that
        checked only the return code would tell the agent "Exit code: -1"
        with no output — indistinguishable from a command that genuinely
        failed, and it would lose the "use Execute_Coding_Task" steer.
        """
        mock_run.return_value = _timed_out()
        result = _handle_shell_command_tool('sleep 60')
        assert 'Exit code' not in result
        assert 'Execute_Coding_Task' in result

    @patch('hart_intelligence_entry.run_bounded')
    def test_timeout_arg_is_30_seconds(self, mock_run):
        mock_run.return_value = _ran(returncode=0)
        _handle_shell_command_tool('echo x')
        assert mock_run.call_args.kwargs.get('timeout') == 30


# ═══════════════════════════════════════════════════════════════════════════
# Output truncation
# ═══════════════════════════════════════════════════════════════════════════


class TestShellCommandTruncation:
    @patch('hart_intelligence_entry.run_bounded')
    def test_long_stdout_truncated_to_2000_chars(self, mock_run):
        mock_run.return_value = _ran(returncode=0, stdout='A' * 5000)
        result = _handle_shell_command_tool('cat huge.log')
        # The 2000-char cap per stream means body ≤ 2000 chars of 'A's
        a_count = result.count('A')
        assert a_count <= 2000

    @patch('hart_intelligence_entry.run_bounded')
    def test_stderr_surfaces_alongside_stdout(self, mock_run):
        mock_run.return_value = _ran(returncode=1, stdout='out data',
                                     stderr='error data')
        result = _handle_shell_command_tool('mixed')
        assert 'out data' in result
        assert '[stderr]' in result
        assert 'error data' in result


# ═══════════════════════════════════════════════════════════════════════════
# Error handling
# ═══════════════════════════════════════════════════════════════════════════


class TestShellCommandErrorHandling:
    # run_bounded documents that FileNotFoundError and other OSErrors from
    # Popen PROPAGATE — the caller decides "tool missing" vs "tool failed" —
    # so both handlers below are still reached unchanged after the migration.
    @patch('hart_intelligence_entry.run_bounded',
           side_effect=FileNotFoundError('pwsh not found'))
    def test_missing_interpreter_returns_explanation(self, mock_run):
        result = _handle_shell_command_tool('powershell: Get-Process')
        assert 'interpreter not found' in result.lower()

    @patch('hart_intelligence_entry.run_bounded',
           side_effect=OSError('permission denied'))
    def test_unexpected_oserror_is_wrapped(self, mock_run):
        result = _handle_shell_command_tool('some-cmd')
        assert 'Shell_Command error' in result
        assert 'OSError' in result or 'permission denied' in result
