"""A VLM shell step runs the command the model meant, not the prompt's own
placeholder.

MEASURED 2026-10-04 17:02:12 (gui_app.log.1, daemon user c23d388c, prompt
42958291468, task "Search for the file containing the function
llm.create_recipe_assistant..."):

    Action: shell value='cd C:/Users/sathi/Documents/Nunba/data/coding && f'
    VLM shell action dispatching: cmd='shell command when Next Action is shell'

The loop's prompt shows the model

    "command": "shell command when Next Action is shell",

as the SHAPE of the field; the 4B echoed that text back as the field's value
while putting the real command in "value", and the executor
(local_computer_tool shell branch, ``action.get('command', text)``) preferred
the echoed placeholder.  cmd.exe answered "'shell' is not recognized", the
run hit three consecutive action errors and closed as action_error -- the
exit whose close the page then lost (test_computer_use_activity_stream).
Three such dispatches that day (16:50:20, 16:50:23, 17:02:12).

ONE rule, at the parser: a field whose text is the template's own placeholder
is an empty field, so the executor falls back to the model's value.  The
templates in local_loop reference the parser's constants, so the prompt and
the filter cannot drift apart.
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.vlm import parser as vlm_parser  # noqa: E402
from integrations.vlm import local_computer_tool as lct  # noqa: E402
from integrations.vlm import local_loop as ll  # noqa: E402

_REAL_COMMAND = ('cd C:/Users/sathi/Documents/Nunba/data/coding && '
                 'findstr /S /N /C:"def generate_reply" *.py')
_LIVE = json.dumps({
    'Reasoning': 'The previous shell command failed because task is not a '
                 'recognized command; search the directory instead.',
    'Next Action': 'shell',
    'coordinate': None,
    'value': _REAL_COMMAND,
    'command': 'shell command when Next Action is shell',
    'path': 'file or app name when Next Action is open_file_gui',
    'Status': 'IN_PROGRESS',
})


def test_an_echoed_placeholder_is_not_a_command():
    pa = vlm_parser.parse_vlm_action(_LIVE, expected_shape='action_json')
    assert pa.action == 'shell'
    assert pa.command == '', (
        f'the template placeholder was read as the command: {pa.command!r}')
    assert pa.path == ''
    assert pa.text == _REAL_COMMAND
    out = pa.to_action_json_dict()
    assert 'command' not in out and 'path' not in out
    assert out['value'] == _REAL_COMMAND


def test_the_other_spelling_of_the_placeholder_is_filtered_too():
    """local_loop carries the placeholder in two templates with two
    wordings ('shell command string when...' and 'shell command when...');
    both are the prompt talking, neither is a command."""
    for echo in ('shell command string when Next Action is shell',
                 'Shell command when next action is shell',
                 '  shell command when Next Action is shell  '):
        raw = json.dumps({'Next Action': 'shell', 'Reasoning': 'r',
                          'value': 'dir', 'command': echo, 'Status': 'IN_PROGRESS'})
        pa = vlm_parser.parse_vlm_action(raw, expected_shape='action_json')
        assert pa.command == '', echo
        assert pa.text == 'dir'


def test_a_real_command_and_a_real_path_still_come_through():
    raw = json.dumps({'Next Action': 'shell', 'Reasoning': 'list it',
                      'command': 'dir C:/Users/sathi/Documents/Nunba/data/coding',
                      'Status': 'IN_PROGRESS'})
    pa = vlm_parser.parse_vlm_action(raw, expected_shape='action_json')
    assert pa.command == 'dir C:/Users/sathi/Documents/Nunba/data/coding'
    assert pa.to_action_json_dict()['command'] == pa.command
    raw2 = json.dumps({'Next Action': 'open_file_gui', 'Reasoning': 'open it',
                       'path': 'C:/x/notes.txt', 'Status': 'IN_PROGRESS'})
    assert vlm_parser.parse_vlm_action(
        raw2, expected_shape='action_json').path == 'C:/x/notes.txt'


def test_the_prompts_show_the_model_the_placeholder_the_parser_filters():
    """The filter and the prompts are one constant: a template that stopped
    saying what the parser filters would re-open the hole silently."""
    import inspect
    src = inspect.getsource(ll)
    assert 'SHELL_COMMAND_PLACEHOLDER' in src and 'OPEN_PATH_PLACEHOLDER' in src
    # Behavioural half: the module-level prompt really carries the constant.
    assert vlm_parser.SHELL_COMMAND_PLACEHOLDER in ll.SYSTEM_PROMPT
    assert vlm_parser.OPEN_PATH_PLACEHOLDER in ll.SYSTEM_PROMPT
    assert vlm_parser.is_template_echo(vlm_parser.SHELL_COMMAND_PLACEHOLDER)
    assert vlm_parser.is_template_echo(vlm_parser.OPEN_PATH_PLACEHOLDER)
    assert not vlm_parser.is_template_echo('dir')
    assert not vlm_parser.is_template_echo('')


def test_the_executor_runs_the_value_when_the_command_field_is_empty():
    """The executor's own fallback: a caller that passes command='' (the
    HTTP tier, an older parser) still runs the model's value -- never a
    'needs command string' refusal, never the placeholder."""
    ran = []
    with patch('core.safe_hartos_attr.safe_hartos_attr',
               return_value=lambda cmd: ran.append(cmd) or 'Exit code: 0\nok'):
        result = lct._execute_inprocess(
            {'action': 'shell', 'value': 'dir', 'command': ''})
    assert ran == ['dir'], result
    assert result['status'] == 'ok'


# ─── 'shell:' / 'terminal:' in front of a command ───────────────────────────
# The models also write the TOOL's name in front of the command ("shell: dir",
# 4 dispatches on 2026-10-04); cmd.exe answered "'shell:' is not recognized".
# The Shell_Command handler already understands 'powershell:' / 'bash:' /
# 'cmd:' selectors in ONE place; 'shell:' and 'terminal:' now name the default
# shell there.  The handler is lifted from hart_intelligence_entry (that module
# does not import on this box), with run_bounded replaced by a recorder, so the
# argv the OS would get is what is asserted.

import ast
import logging
import os as _os
from unittest.mock import MagicMock

_HIE = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__)))), 'hart_intelligence_entry.py')


def _lifted_shell_handler(ran):
    from core.subprocess_safe import BoundedResult
    src = open(_HIE, encoding='utf-8').read()
    tree = ast.parse(src)
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == '_handle_shell_command_tool')
    ns = {
        'sys': sys, 'os': _os, 'logging': logging, 'json': json,
        'SHELL_COMMAND_TIMEOUT_S': 30,
        'thread_local_data': MagicMock(get_user_id=lambda: '', get_prompt_id=lambda: ''),
        'app': MagicMock(), 'logger': MagicMock(), 'current_app': MagicMock(),
        'run_bounded': lambda argv, timeout=None, **kw: ran.append(list(argv)) or BoundedResult(
            returncode=0, stdout='ok', stderr='', timed_out=False),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), _HIE, 'exec'), ns)
    return ns['_handle_shell_command_tool']


def test_shell_and_terminal_prefixes_name_the_default_shell():
    ran = []
    handler = _lifted_shell_handler(ran)
    with patch('integrations.vlm.safety.computer_operation_refusal', return_value=None), \
            patch('integrations.vlm.safety.computer_control_block', return_value=None), \
            patch('integrations.vlm.activity_stream.current_run', return_value=MagicMock()):
        out = handler('shell: dir')
        handler('terminal: dir')
        handler('powershell: Get-ChildItem')
    assert out.startswith('Exit code: 0'), out
    default = ['cmd', '/c', 'dir'] if sys.platform == 'win32' else ['/bin/sh', '-c', 'dir']
    assert ran[0] == default, ran[0]
    assert ran[1] == default, ran[1]
    assert ran[2][-1] == 'Get-ChildItem' and ran[2][0] in ('powershell', 'pwsh'), ran[2]
