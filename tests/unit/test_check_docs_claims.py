"""scripts/check_docs_claims.py reports the AI-sounding words in the public
docs from the human-voice guide's ONE list (amendment A20, inventory D30).

The checker used to keep FILLER, a third list of AI words beside the guide's
(integrations/agent_engine/human_voice.py) and igkit's old slop.json, plus
TELLS for 'load-bearing'.  Now:

* 'filler' findings are the guide's WORDS and PHRASES, so a tell the guide
  learns is reported in the docs too;
* 'tell' findings are the guide's DOCS_TELLS, the words Hacker News counted
  in HARTOS's docs (load-bearing, honest, genuine, earns its keep), which
  her caption humanizer must not rewrite;
* both match through the guide's one matcher, human_voice.tell_pattern,
  with any ending, as FILLER's stems did ('honest' finds 'honestly');
* a word is one finding, under the longest tell that covers it.

The checker runs in CI on a bare Python (no requirements installed), so it
must reach the guide without importing HARTOS's packages.
"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, 'scripts', 'check_docs_claims.py')
GUIDE = os.path.join(REPO, 'integrations', 'agent_engine', 'human_voice.py')


@pytest.fixture
def cdc():
    """A fresh copy of the checker script, loaded the way CI runs it (as a
    file, not as part of a package)."""
    spec = importlib.util.spec_from_file_location('check_docs_claims', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _found(cdc, tmp_path, text, kinds=('tell', 'filler')):
    """[(line, kind, detail)] the checker reports for a doc holding ``text``."""
    doc = tmp_path / 'doc.md'
    doc.write_text(text, encoding='utf-8')
    return [(line, kind, detail) for _, line, kind, detail in cdc.check(str(doc))
            if kind in kinds]


# ── the fold ────────────────────────────────────────────────────────────────

def test_the_docs_are_checked_against_the_guides_list(cdc, tmp_path):
    # None of these was on FILLER; all three are guide tells.
    found = _found(cdc, tmp_path, 'We foster synergy across the ecosystem.\n')
    assert sorted(found) == [(1, 'filler', 'ecosystem'), (1, 'filler', 'foster'),
                             (1, 'filler', 'synergy')], found


def test_a_tell_the_guide_learns_is_reported_in_the_docs(cdc, tmp_path, monkeypatch):
    hv = cdc.human_voice
    monkeypatch.setattr(hv, 'WORDS', hv.WORDS + (hv.Tell('glamazing', 'lovely', 'adjectives'),))
    assert _found(cdc, tmp_path, 'A glamazing release.\n') == [(1, 'filler', 'glamazing')]


def test_the_checker_reads_the_guides_own_file(cdc):
    from integrations.agent_engine import human_voice
    assert os.path.samefile(cdc.human_voice.__file__, GUIDE)
    assert ([t.find for t in cdc.human_voice.WORDS + cdc.human_voice.PHRASES]
            == [t.find for t in human_voice.WORDS + human_voice.PHRASES])
    assert cdc.human_voice.DOCS_TELLS == human_voice.DOCS_TELLS


def test_it_runs_on_a_bare_python_like_the_ci_job(tmp_path):
    # docs-check.yml installs nothing.  -S drops site-packages (requests and
    # the rest of HARTOS's requirements), -I ignores PYTHONPATH and the cwd.
    doc = tmp_path / 'doc.md'
    doc.write_text('We foster synergy.\n', encoding='utf-8')
    code = (
        'import importlib.util, json, sys\n'
        'spec = importlib.util.spec_from_file_location("check_docs_claims", sys.argv[1])\n'
        'm = importlib.util.module_from_spec(spec)\n'
        'spec.loader.exec_module(m)\n'
        'print(json.dumps({"requests": importlib.util.find_spec("requests") is not None,\n'
        '                  "found": m.check(sys.argv[2])}))\n')
    run = subprocess.run([sys.executable, '-S', '-I', '-c', code, SCRIPT, str(doc)],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    if out['requests']:
        pytest.skip('this interpreter keeps requests without site-packages')
    assert sorted(f[3] for f in out['found'] if f[2] == 'filler') == ['foster', 'synergy']


# ── the words Hacker News counted ───────────────────────────────────────────

@pytest.mark.parametrize('line, tell', [
    ('It is honestly fine.', 'honest'),
    ('The pool is genuinely shared.', 'genuine'),
    ('This flag earns its keep.', 'earns its keep'),
    ('That cache earned its keep.', 'earned its keep'),
    ('The load bearing step.', 'load bearing'),
])
def test_the_words_hacker_news_counted_are_tells_with_any_ending(cdc, tmp_path, line, tell):
    # They were 'filler' (honest, genuine, earns its keep) before: the
    # checker's docstring always listed them as known tells.
    assert _found(cdc, tmp_path, line + '\n') == [(1, 'tell', tell)]


def test_load_bearing_is_one_finding(cdc, tmp_path):
    # TELLS and FILLER both named it, so its line was reported twice.
    assert _found(cdc, tmp_path, 'A load-bearing claim.\n') == [(1, 'tell', 'load-bearing')]


# ── nothing FILLER caught is lost ───────────────────────────────────────────

@pytest.mark.parametrize('word', [
    'delves', 'seamless', 'seamlessly', 'leveraged', 'leverages', 'empowering',
    'empowerment', 'unleashed', 'elevated', 'revolutionize', 'revolutionized',
    'revolutionizing', 'game-changing', 'game-changers', 'cutting-edge',
    'supercharged', 'effortless', 'effortlessly', 'testament to', 'tapestry',
    'dive into',
])
def test_every_form_the_old_list_caught_is_still_reported(cdc, tmp_path, word):
    found = _found(cdc, tmp_path, 'It is %s the plan.\n' % word, kinds=('filler',))
    assert len(found) == 1, (word, found)


# ── how a tell matches ──────────────────────────────────────────────────────

@pytest.mark.parametrize('line, tell', [
    ('We are leveraging it.', 'leveraging'),     # not 'leverage' as well
    ('Let us delve into it.', 'delve into'),     # not 'delve' as well
    ('It runs seamlessly.', 'seamlessly'),       # not 'seamless' as well
    ('A testament to care.', 'a testament to'),  # not 'testament to' as well
])
def test_a_word_is_one_finding_under_its_longest_tell(cdc, tmp_path, line, tell):
    assert _found(cdc, tmp_path, line + '\n') == [(1, 'filler', tell)]


def test_a_phrase_matches_across_any_run_of_spaces(cdc, tmp_path):
    assert _found(cdc, tmp_path, 'Time to dive   into it.\n') == [(1, 'filler', 'dive into')]


def test_a_tell_that_ends_on_punctuation_is_reported(cdc, tmp_path):
    found = _found(cdc, tmp_path, 'What are your thoughts? Open an issue.\n')
    assert found == [(1, 'filler', 'what are your thoughts?')]


def test_plain_words_are_not_reported(cdc, tmp_path):
    assert _found(cdc, tmp_path, 'The node keeps a ledger per agent.\n',
                  kinds=('tell', 'filler', 'dash', 'no-x-no-y')) == []
