"""A computer-use run's file actions carry what the model sent, and work in
the run's declared workspace, the folder its shell steps run in too.

MEASURED 2026-10-05 16:12:00 and 16:12:07 IST (frozen_debug.log, coding
daemon goal f984b2cc, session c23d388c-..._31216053936): two VLM write_file
actions failed with "[Errno 2] No such file or directory: ''".  The model's
reasoning named check_pytorch.py; the executor opened ''.  Three gaps in the
loop's own contract lead there:

  * the prompt never said where a write_file path goes, and its JSON
    template described 'path' as the open_file_gui target only.  A model
    that echoes that template has its path blanked
    (parser.is_template_echo), and the executor then opened '';
  * the parser kept a fixed field set, so a model's 'content',
    'source_path', 'destination_path' and 'duration' never reached the
    executor (write_file wrote an empty file and reported it written;
    Open_file_and_copy_paste could never run);
  * a relative path resolved in the process cwd and every shell step ran
    there too (the install folder on the desktop), while the task's declared
    workspace was only a sentence in the prompt.  "write_file the script,
    then shell it", the route the prompt itself prescribes for a refused
    python -c, could not find the script it had just written.

The raw model reply of those two steps was not recorded anywhere (the loop
logs the parsed value only and llama-server logs no reply text), so which of
the first two produced the empty path is not measured.  The fix closes both,
and an action that still lacks its path is now refused by name.
"""
import ast
import json
import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from hartos.threadlocal import thread_local_data  # noqa: E402
from integrations.vlm import local_computer_tool as lct  # noqa: E402
from integrations.vlm import local_loop as ll  # noqa: E402
from integrations.vlm import parser as vlm_parser  # noqa: E402


@pytest.fixture(autouse=True)
def proc_cwd(tmp_path, monkeypatch):
    """The process cwd, a throwaway folder: the app's own folder on the
    desktop.  A relative path that is NOT re-rooted lands here, where the
    tests below look for it, never in the repository."""
    cwd = tmp_path / 'process_cwd'
    cwd.mkdir()
    (cwd / 'already_here.txt').write_text('x', encoding='utf-8')
    monkeypatch.chdir(cwd)
    return cwd


def _payload(raw: dict) -> dict:
    """What the executor receives for one model reply: the loop's own parse
    and payload builder, no copy of either."""
    return ll._build_action_payload(
        ll._parse_vlm_response(json.dumps(raw)),
        {'screen_info': '', 'parsed_content_list': []})


# ─── every field the executor reads reaches it ──────────────────────────────

def test_every_field_the_executor_reads_reaches_it():
    write = _payload({'Next Action': 'write_file', 'Reasoning': 'r',
                      'path': 'C:/w/a.py', 'content': 'print(1)',
                      'Status': 'IN_PROGRESS'})
    assert write['path'] == 'C:/w/a.py'
    assert write['content'] == 'print(1)', (
        f"the model's content was dropped before the executor: {write}")
    copy = _payload({'Next Action': 'Open_file_and_copy_paste', 'Reasoning': 'r',
                     'source_path': 'C:/w/a.py', 'destination_path': 'C:/w/b.py',
                     'Status': 'IN_PROGRESS'})
    assert copy['source_path'] == 'C:/w/a.py', copy
    assert copy['destination_path'] == 'C:/w/b.py', copy
    wait = _payload({'Next Action': 'wait', 'Reasoning': 'r', 'duration': 3,
                     'Status': 'IN_PROGRESS'})
    assert wait['duration'] == 3, wait


def test_a_wait_duration_reaches_the_executor_as_seconds():
    """Carrying 'duration' must not turn the model's "5" or -1 into a
    time.sleep that raises, where an ignored duration only waited 2 s."""
    def dur(value):
        return _payload({'Next Action': 'wait', 'Reasoning': 'r',
                         'duration': value, 'Status': 'IN_PROGRESS'}).get('duration')
    assert dur('5') == 5.0
    assert dur(-1) == 0.0
    assert dur('soon') is None
    assert dur(None) is None


def test_write_file_writes_the_models_content(tmp_path):
    target = tmp_path / 'from_content.txt'
    result = lct._execute_inprocess(_payload({
        'Next Action': 'write_file', 'Reasoning': 'r', 'path': str(target),
        'content': 'the text the model sent', 'Status': 'IN_PROGRESS'}))
    assert 'error' not in result, result
    assert target.read_text(encoding='utf-8') == 'the text the model sent'


# ─── an action without its path is refused by name, never opened ────────────

def test_a_write_with_no_path_is_refused_by_name():
    result = lct._execute_inprocess({'action': 'write_file', 'text': 'import sys'})
    assert result.get('error'), result
    assert "'path'" in result['error'], result
    assert 'Errno' not in result['error'], (
        f"open('') ran; the model reads that as a disk error: {result}")


def test_an_echoed_path_placeholder_is_refused_not_opened(proc_cwd):
    """The 16:12 shape as far as the record goes: the content arrived, the
    path did not.  An echoed template path is blanked by the parser, so the
    refusal has to come from the executor, and must name the field."""
    for echo in (vlm_parser.OPEN_PATH_PLACEHOLDER,
                 'file or app name when Next Action is open_file_gui'):
        payload = _payload({'Next Action': 'write_file', 'Reasoning': 'r',
                            'value': 'import sys', 'path': echo,
                            'Status': 'IN_PROGRESS'})
        assert 'path' not in payload, (echo, payload)
        result = lct._execute_inprocess(payload)
        assert "'path'" in result.get('error', ''), (echo, result)
    assert sorted(os.listdir(proc_cwd)) == ['already_here.txt'], (
        'a placeholder was written as a file name')


def test_read_and_copy_with_no_path_are_refused_by_name():
    read = lct._execute_inprocess({'action': 'read_file_and_understand'})
    assert "'path'" in read.get('error', ''), read
    copy = lct._execute_inprocess({'action': 'Open_file_and_copy_paste',
                                   'source_path': '', 'destination_path': ''})
    assert "'source_path'" in copy.get('error', ''), copy
    assert 'Errno' not in copy.get('error', ''), copy


# ─── the run's workspace: where relative paths and shell steps resolve ─────

def test_the_workspace_is_the_runs_and_comes_back_after(tmp_path):
    assert thread_local_data.get_workspace() is None
    inner = tmp_path / 'inner'
    with thread_local_data.workspace(str(tmp_path)):
        assert thread_local_data.get_workspace() == str(tmp_path)
        with thread_local_data.workspace(str(inner)):
            assert thread_local_data.get_workspace() == str(inner)
        assert thread_local_data.get_workspace() == str(tmp_path)
        # No workspace named: the enclosing one stands.
        with thread_local_data.workspace(''):
            assert thread_local_data.get_workspace() == str(tmp_path)
    assert thread_local_data.get_workspace() is None


def test_relative_file_paths_resolve_in_the_run_workspace(tmp_path, proc_cwd):
    work = tmp_path / 'workspace'
    work.mkdir()
    with thread_local_data.workspace(str(work)):
        wrote = lct._execute_inprocess(
            {'action': 'write_file', 'path': 'check.py', 'text': 'print(1)'})
        read = lct._execute_inprocess(
            {'action': 'read_file_and_understand', 'path': 'check.py'})
        listed = lct._execute_inprocess({'action': 'list_folders_and_files'})
        copied = lct._execute_inprocess(
            {'action': 'Open_file_and_copy_paste', 'source_path': 'check.py',
             'destination_path': 'copy.py'})
    assert 'error' not in wrote, wrote
    assert (work / 'check.py').read_text(encoding='utf-8') == 'print(1)'
    assert read.get('output') == 'print(1)', read
    assert sorted(listed.get('output', '').split('\n')) == ['check.py'], listed
    assert 'error' not in copied, copied
    assert (work / 'copy.py').read_text(encoding='utf-8') == 'print(1)'
    assert sorted(os.listdir(proc_cwd)) == ['already_here.txt'], (
        'a relative path was resolved in the process cwd')


def test_outside_a_run_relative_paths_are_left_alone(proc_cwd):
    """No declared workspace (a direct caller): nothing is re-rooted, so the
    listing is the process cwd's as before."""
    listed = lct._execute_inprocess({'action': 'list_folders_and_files'})
    assert listed['output'].split('\n') == ['already_here.txt'], listed


_HIE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'hart_intelligence_entry.py')


def _lifted_shell_handler(calls):
    """hart_intelligence_entry._handle_shell_command_tool as written (that
    module does not import on this box), with the REAL thread-local store
    and run_bounded replaced by a recorder of its argv and keywords."""
    from core.subprocess_safe import BoundedResult
    tree = ast.parse(open(_HIE, encoding='utf-8').read())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == '_handle_shell_command_tool')

    def _run(argv, timeout=None, **kw):
        calls.append((list(argv), kw))
        return BoundedResult(returncode=0, stdout='ok', stderr='', timed_out=False)

    ns = {'sys': sys, 'os': os, 'logging': logging, 'json': json,
          'SHELL_COMMAND_TIMEOUT_S': 30, 'thread_local_data': thread_local_data,
          'app': MagicMock(), 'logger': MagicMock(), 'current_app': MagicMock(),
          'run_bounded': _run}
    exec(compile(ast.Module(body=[node], type_ignores=[]), _HIE, 'exec'), ns)
    return ns['_handle_shell_command_tool']


@pytest.fixture
def _shell_allowed():
    with patch('integrations.vlm.safety.computer_operation_refusal', return_value=None), \
            patch('integrations.vlm.safety.computer_control_block', return_value=None), \
            patch('integrations.vlm.activity_stream.current_run', return_value=MagicMock()):
        yield


@pytest.mark.usefixtures('_shell_allowed')
def test_shell_steps_run_in_the_run_workspace(tmp_path):
    calls = []
    handler = _lifted_shell_handler(calls)
    with thread_local_data.workspace(str(tmp_path)):
        assert handler('dir').startswith('Exit code: 0')
    handler('dir')
    with thread_local_data.workspace(str(tmp_path / 'not_there')):
        handler('dir')
    assert calls[0][1].get('cwd') == str(tmp_path), calls[0]
    assert 'cwd' not in calls[1][1], f'outside a run the cwd changed: {calls[1]}'
    # A workspace that does not exist is not handed to the OS, which would
    # fail the spawn and read as "interpreter not found".
    assert 'cwd' not in calls[2][1], calls[2]


# ─── one run, end to end: write the script, then run it, in one folder ─────

def _backend(replies):
    b = MagicMock()
    b.route_task.return_value = 'multi_step'
    b._call_api.side_effect = list(replies)
    b.try_taskbar_pre_check.return_value = None
    b.detect_grounding_bias.return_value = None
    b.retry_with_elimination.return_value = None
    return b


@pytest.mark.usefixtures('computer_control_granted')
def test_a_run_writes_then_runs_in_its_declared_workspace(tmp_path, monkeypatch,
                                                         proc_cwd):
    # The loop's safety layer writes a real audit file under ~/.nunba.
    monkeypatch.setenv('HEVOLVE_VLM_LOOP_SAFETY', '0')
    replies = [
        json.dumps({'Reasoning': 'write the script first', 'Next Action': 'write_file',
                    'path': 'check_pytorch.py', 'value': 'print(1)',
                    'Status': 'IN_PROGRESS'}),
        json.dumps({'Reasoning': 'run it', 'Next Action': 'shell',
                    'command': 'python check_pytorch.py', 'Status': 'IN_PROGRESS'}),
        json.dumps({'Reasoning': 'done', 'Next Action': 'None', 'Status': 'DONE'}),
    ]
    backend = _backend(replies)
    shell_saw = []
    work = tmp_path / 'workspace'
    work.mkdir()

    def _shell(cmd):
        shell_saw.append((cmd, thread_local_data.get_workspace()))
        return 'Exit code: 0\n1'

    with patch('integrations.vlm.qwen3vl_backend.get_qwen3vl_backend',
               return_value=backend), \
            patch('integrations.vlm.local_computer_tool.take_screenshot',
                  return_value='b64'), \
            patch('integrations.vlm.local_computer_tool.foreground_window_handle',
                  return_value=None), \
            patch('core.safe_hartos_attr.safe_hartos_attr', return_value=_shell), \
            patch('integrations.vlm.activity_stream.open_run', return_value=MagicMock(run_id='r1')), \
            patch('integrations.vlm.activity_stream.record_activity'), \
            patch('integrations.vlm.activity_stream.resolve_steering_agent_id', return_value=''), \
            patch('integrations.vlm.local_loop.time.sleep'):
        result = ll.run_local_agentic_loop(
            {'instruction_to_vlm_agent': 'check pytorch', 'user_id': 'u1',
             'prompt_id': 'p1', 'workspace_root': str(work)},
            tier='inprocess', max_iterations=5)

    assert result['exit_reason'] == 'done', result
    assert sorted(os.listdir(proc_cwd)) == ['already_here.txt'], (
        'the script was written into the process cwd')
    assert (work / 'check_pytorch.py').read_text(encoding='utf-8') == 'print(1)'
    assert shell_saw == [('python check_pytorch.py', str(work))], shell_saw
    assert thread_local_data.get_workspace() is None, 'the run left its workspace behind'
    # What the model was told: where a file action's fields go.
    first_prompt = backend._call_api.call_args_list[0].args[0][0]['content'][0]['text']
    next_action_line = next(ln for ln in first_prompt.split('\n')
                            if ln.strip().startswith('"Next Action"'))
    assert 'write_file' in next_action_line, next_action_line
    write_doc = [ln for ln in first_prompt.split('\n')
                 if 'write_file' in ln and "'path'" in ln and "'value'" in ln]
    assert write_doc, 'the prompt never says where write_file takes its path and text'
    path_line = next(ln for ln in first_prompt.split('\n')
                     if ln.strip().startswith('"path"'))
    assert 'write_file' in path_line, path_line
    assert vlm_parser.is_template_echo(vlm_parser.OPEN_PATH_PLACEHOLDER)
