"""aider_native creates a file the task asks for, inside its working dir only.

Measured 2026-11-18 on the installed desktop (HARTOS 30835cbf): a reply in
aider's own new-file form -- the file's name and an empty SEARCH section --
came back ``{'status': 'skipped', 'reason': 'file not found'}`` and no file
was written, so "Create a file hello.py ..." could never succeed, whatever
the model answered.  Editing an existing file worked.  The system prompt also
never said how to create a file.

Containment (test_evolution_gates_live.py, test_D_*) still holds for a new
file: outside working_dir, or outside a declared set, it is refused and not
written.

    python -m pytest tests/unit/test_aider_native_creates_a_new_file.py -q
"""
from unittest.mock import patch

import pytest

pytest.importorskip('diff_match_patch')


def _backend():
    from integrations.coding_agent.aider_native_backend import AiderNativeBackend
    return AiderNativeBackend()


def _new_file(name, content, blank_line=True):
    """A new-file block; aider writes the empty SEARCH with no blank line."""
    search = '\n' if blank_line else ''
    return (f"`{name}`\n<<<<<<< SEARCH\n{search}=======\n{content}\n"
            f">>>>>>> REPLACE\n")


@pytest.mark.parametrize('blank_line', [True, False],
                         ids=['blank-search-line', 'aider-form'])
def test_an_empty_search_creates_the_missing_file(tmp_path, blank_line):
    work = tmp_path / 'repo'
    work.mkdir()
    reply = _new_file('hello.py', 'print("hello from HART")', blank_line)

    res = _backend()._apply_edits(reply, str(work), [])

    assert [r['status'] for r in res] == ['applied'], res
    assert (work / 'hello.py').read_text(encoding='utf-8') == (
        'print("hello from HART")\n')


def test_a_new_file_may_go_in_a_new_folder_inside_the_working_dir(tmp_path):
    work = tmp_path / 'repo'
    work.mkdir()
    res = _backend()._apply_edits(
        _new_file('pkg/util.py', 'X = 1'), str(work), [])
    assert res[0]['status'] == 'applied'
    assert (work / 'pkg' / 'util.py').read_text(encoding='utf-8') == 'X = 1\n'


def test_a_missing_file_with_search_text_is_still_not_found(tmp_path):
    """Only an EMPTY search means "new file": text to find in a file that
    does not exist is a mistake, not a request to create it."""
    work = tmp_path / 'repo'
    work.mkdir()
    reply = "`gone.py`\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"
    res = _backend()._apply_edits(reply, str(work), [])
    assert res == [{'file': 'gone.py', 'status': 'skipped',
                    'reason': 'file not found'}]
    assert not (work / 'gone.py').exists()


def test_a_new_file_outside_the_working_dir_is_refused(tmp_path):
    work = tmp_path / 'repo'
    work.mkdir()
    res = _backend()._apply_edits(
        _new_file('../escape.py', 'BAD = 1'), str(work), [])
    assert res[0]['status'] == 'refused'
    assert not (tmp_path / 'escape.py').exists()


def test_a_new_file_outside_the_declared_set_is_refused(tmp_path):
    work = tmp_path / 'repo'
    work.mkdir()
    (work / 't.py').write_text('x = 1\n')
    res = _backend()._apply_edits(
        _new_file('extra.py', 'Y = 1'), str(work), ['t.py'],
        restrict_to_files=True)
    assert res[0]['status'] == 'refused'
    assert not (work / 'extra.py').exists()


def test_a_created_file_is_the_receipt(tmp_path):
    """execute() counts a created file as the applied edit it is."""
    from integrations.coding_agent.aider_native_backend import AiderNativeBackend
    from integrations.coding_agent.aider_core import hart_model_adapter as hma
    work = tmp_path / 'repo'
    work.mkdir()
    (work / 'readme.txt').write_text('notes\n')
    reply = _new_file('hello.py', 'print("hi")', blank_line=False)
    with patch.object(AiderNativeBackend, 'is_installed', return_value=True), \
            patch.object(hma, 'send_completion', return_value=reply), \
            patch.object(hma.HartModelAdapter, 'from_hartos_config',
                         return_value=object()):
        result = AiderNativeBackend().execute(
            'Create hello.py', {'working_dir': str(work),
                                'files': ['readme.txt']})
    assert result['success'] is True, result
    assert result['files_changed'] == ['hello.py']
    assert (work / 'hello.py').read_text(encoding='utf-8') == 'print("hi")\n'


@pytest.mark.parametrize('blank_line', [True, False],
                         ids=['blank-search-line', 'aider-form'])
def test_an_empty_search_never_touches_an_existing_file(tmp_path, blank_line):
    """Review of 85735cb3: an empty SEARCH reached str.replace('', X) on an
    existing file and interleaved X between every character ('ab' became
    'XaXbX'), reported as applied.  An empty SEARCH only creates."""
    work = tmp_path / 'repo'
    work.mkdir()
    (work / 'hello.py').write_text('ab\n', encoding='utf-8')
    res = _backend()._apply_edits(_new_file('hello.py', 'X', blank_line),
                                  str(work), [])
    assert res[0]['status'] == 'skipped', res
    assert 'exists' in res[0]['reason']
    assert (work / 'hello.py').read_text(encoding='utf-8') == 'ab\n'


def test_an_empty_search_on_an_existing_file_is_no_success(tmp_path):
    from integrations.coding_agent.aider_native_backend import AiderNativeBackend
    from integrations.coding_agent.aider_core import hart_model_adapter as hma
    work = tmp_path / 'repo'
    work.mkdir()
    (work / 'hello.py').write_text('ab\n', encoding='utf-8')
    with patch.object(AiderNativeBackend, 'is_installed', return_value=True), \
            patch.object(hma, 'send_completion',
                         return_value=_new_file('hello.py', 'X', False)), \
            patch.object(hma.HartModelAdapter, 'from_hartos_config',
                         return_value=object()):
        result = AiderNativeBackend().execute(
            'Create hello.py', {'working_dir': str(work), 'files': ['hello.py']})
    assert result['success'] is False
    assert result['files_changed'] == []
    assert (work / 'hello.py').read_text(encoding='utf-8') == 'ab\n'


def test_an_empty_new_file_does_not_swallow_the_next_block(tmp_path):
    """An empty REPLACE (an empty __init__.py) ends at its own marker."""
    work = tmp_path / 'repo'
    work.mkdir()
    (work / 'b.py').write_text('p\n', encoding='utf-8')
    reply = ("`pkg/__init__.py`\n<<<<<<< SEARCH\n=======\n>>>>>>> REPLACE\n"
             "`b.py`\n<<<<<<< SEARCH\np\n=======\nq\n>>>>>>> REPLACE\n")
    res = _backend()._apply_edits(reply, str(work), [])
    assert [(r['file'], r['status']) for r in res] == [
        ('pkg/__init__.py', 'applied'), ('b.py', 'applied')], res
    assert (work / 'pkg' / '__init__.py').read_text(encoding='utf-8') == ''
    assert (work / 'b.py').read_text(encoding='utf-8') == 'q\n'


def test_the_system_prompt_says_how_to_create_a_file():
    prompt = _backend()._build_system_prompt('feature', '')
    assert 'new file' in prompt.lower()
    assert '<<<<<<< SEARCH\n=======' in prompt
