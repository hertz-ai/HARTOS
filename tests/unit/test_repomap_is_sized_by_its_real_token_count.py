"""A repo map is sized by the token count of the map it built (#131).

RepoMap fits its map to a token budget by binary search, counting each trial
tree.  Its counter sampled every Nth line and scaled the sample up.  A tree of
untagged files is "\\nname\\n" per file, so every other line is blank; an even N
sampled only the blank lines and a 15,311-token map read as 1,927.  Measured on
the installed desktop's coding workspace (5,171 files): the 2,048-token budget
produced a 15,425-token system prompt, and llama-server answered HTTP 400 against
its 12,288-token context on every native coding step.
"""
import pytest

pytest.importorskip('tiktoken')
pytest.importorskip('tree_sitter')
pytest.importorskip('grep_ast.tsl', reason='grep_ast.tsl not available')

from integrations.coding_agent.aider_core.hart_model_adapter import HartModelAdapter
from integrations.coding_agent.aider_core.io_adapter import SimpleIO
from integrations.coding_agent.aider_core.repomap import RepoMap

# AiderNativeBackend._get_repo_map's map_tokens.
_BUDGET = 2048
# RepoMap accepts a trial tree within this fraction of the budget.
_ACCEPTED_ERROR = 0.15


def _repo_map(root):
    return RepoMap(root=str(root), io=SimpleIO(),
                   main_model=HartModelAdapter(), map_tokens=_BUDGET)


def test_a_map_of_alternating_blank_and_name_lines_is_counted_in_full(tmp_path):
    tree = ''.join(f'\ndocs\\section_{i:04d}\\notes_{i:04d}.txt\n'
                   for i in range(1035))
    # The sampling step the old counter derived from this tree is even, and
    # even steps landed on the blank lines only.
    assert (len(tree.splitlines()) // 100) % 2 == 0

    exact = HartModelAdapter().token_count(tree)

    assert _repo_map(tmp_path).token_count(tree) == pytest.approx(exact, rel=0.05)


def test_a_map_of_untagged_files_stays_inside_its_budget(tmp_path):
    files = []
    # 2,000 files built a 19,774-token map under the sampling counter; the
    # counter's error depends on the sample step, so the count is fixed.
    for i in range(2000):
        path = tmp_path / 'notes' / f'section_{i:04d}' / f'entry_{i:04d}.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('plain text, no definitions to tag\n', encoding='utf-8')
        files.append(str(path))

    built = _repo_map(tmp_path).get_repo_map(chat_files=[], other_files=files)

    assert built
    assert HartModelAdapter().token_count(built) <= _BUDGET * (1 + _ACCEPTED_ERROR)
