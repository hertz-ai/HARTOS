"""The VLM loop's `shell` action must tell the model which shell it is.

MEASURED 2026-09-30 10:56-11:03 on the owner's Windows install (gui_app.log):
the loop was told `shell: run any shell/PowerShell/bash command ... running
git/npm/python`.  The model then emitted `cat > f.py << 'EOF'`, `ls -R`, and
`python f.py`; the shell action runs under cmd.exe, so every one returned
"'cat' is not recognized" / "'ls' is not recognized" / "'python' is not
recognized".  The model retried the same program under three spellings, hit
the 3-consecutive-error limit, and aborted (exit_reason=action_error) six
times in that window.

The prompt, not the safety layer, set that trap: it advertised a dialect the
Windows handler does not run.  `_shell_dialect_note` is the one place that
names the dialect, and the action list embeds it.
"""
import platform

from integrations.vlm import local_loop
from integrations.vlm.local_loop import _VLM_ACTION_LIST, _shell_dialect_note


class TestShellDialectNote:
    def test_windows_names_cmd_and_warns_off_unix_tools(self):
        note = _shell_dialect_note('Windows').lower()
        assert 'cmd.exe' in note
        for unix_only in ('cat', 'ls', 'heredoc'):
            assert unix_only in note, (
                f"the Windows note must name {unix_only!r} as unavailable")

    def test_windows_tells_the_model_not_to_retry_unrecognized_programs(self):
        note = _shell_dialect_note('Windows').lower()
        assert 'not recognized' in note
        assert 'blocker' in note

    def test_windows_names_the_powershell_selector_the_handler_accepts(self):
        # hart_intelligence_entry._handle_shell_command_tool parses
        # 'powershell: <cmd>'; the note must use that exact form.
        assert 'powershell:' in _shell_dialect_note('Windows').lower()

    def test_other_platforms_do_not_claim_cmd(self):
        for name in ('Linux', 'Darwin'):
            assert 'cmd.exe' not in _shell_dialect_note(name).lower()

    def test_action_list_embeds_this_platforms_note(self):
        assert _shell_dialect_note(platform.system()) in _VLM_ACTION_LIST

    def test_windows_action_list_no_longer_promises_bash(self):
        # The unconditional "shell/PowerShell/bash command" claim is the
        # advertised dialect the Windows handler does not run.
        if platform.system() == 'Windows':
            assert 'PowerShell/bash command' not in _VLM_ACTION_LIST

    def test_existing_contract_phrases_survive(self):
        # Other suites pin these; the dialect note is additive.
        low = _VLM_ACTION_LIST.lower()
        for phrase in ('python -c', 'write_file', 'open_file_gui', 'prefer'):
            assert phrase in low
        assert local_loop._VLM_ACTION_LIST in local_loop.SYSTEM_PROMPT
