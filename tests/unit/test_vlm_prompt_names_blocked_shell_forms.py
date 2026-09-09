"""The VLM action prompt must name the shell forms the denylist BLOCKS.

MEASURED LIVE 2026-09-09 23:42 -> 2026-09-10 00:12, agent 18088688973.
`execute_windows_or_android_command` ran for real and returned
"Not able to perform this action now please try later" -- a
core.constants.TOOL_FAILURE_RESULTS member -- so the reuse fabrication gate
correctly refused to advance, re-steered 3x, then force-advanced with
"[FABRICATED-COMPLETE] ... this action's output is NOT tool-backed".
Actions 1, 2, 4, 6 and 7 all died this way; only 3 and 5 ever ran their tools.

WHY, read not guessed.  The VLM loop's shell dispatches reach HARTOS's
`_handle_shell_command_tool`, whose denylist answered 14 distinct times (28
log lines; agent_system.log emits every record twice):

    Shell_Command refused: the command matches a destructive pattern
    ('\\bpython[23]?\\s+-c\\s')

EVERY refusal in the drive matched that ONE pattern.  It is deliberate: the
denylist comment records that an ethical-hacker review added the interpreter
wrappers because `python -c` / `perl -e` / `ruby -e` smuggle destructive code
past every other rule.  The shell layer is otherwise healthy -- 12 commands
exited 0 in the same window.

So the denylist is NOT the defect and must not be weakened to make an agent
pass.  The defect is a CONTRACT GAP: `_VLM_ACTION_LIST` tells the model to
PREFER shell for anything command-expressible and never mentions that
interpreter one-liners are refused.  The model therefore keeps choosing
`python -c "..."`, and each refusal burns one of the loop's 3 consecutive
action errors -> `exit_reason=action_error` -> the tool reports failure.
The refusal text even advises "ask the user to run it manually", which an
autonomous reuse turn cannot do.

This test couples the two MECHANICALLY rather than by prose, so a fifth
interpreter wrapper added to the denylist later cannot silently go
unannounced to the model.  It reads the denylist out of the source with AST
instead of importing hart_intelligence_entry (a 634 KB module that pulls in
Flask, autogen and the whole tool registry).

    python -m pytest tests/unit/test_vlm_prompt_names_blocked_shell_forms.py --noconftest -q
"""
import ast
import io
import os

import pytest

# this file lives at <repo>/tests/unit/, so the repo root is THREE levels up.
_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_ENTRY = os.path.join(_HARTOS, 'hart_intelligence_entry.py')


def _deny_patterns():
    """Every string literal in the `_DENY_PATTERNS` list, via AST."""
    tree = ast.parse(io.open(_ENTRY, encoding='utf-8', errors='replace').read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id == '_DENY_PATTERNS':
                if isinstance(node.value, ast.List):
                    return [e.value for e in node.value.elts
                            if isinstance(e, ast.Constant)
                            and isinstance(e.value, str)]
    pytest.fail("no _DENY_PATTERNS list in hart_intelligence_entry.py — "
                "re-point this test rather than deleting it")


# pattern-substring -> the interpreter the model would actually type.
# Keyed on the regex fragment so a renamed variable cannot break the mapping.
_WRAPPERS = {
    'python': 'python',
    'perl': 'perl',
    'ruby': 'ruby',
    'node': 'node',
    'powershell': 'powershell',
}


def _blocked_interpreters():
    """Interpreters whose one-liner form the denylist refuses."""
    out = set()
    for pat in _deny_patterns():
        # the wrapper rules are the ones matching an interpreter FLAG form:
        # `-c `, `-e `, or `-enc`.  Plain destructive rules (rm -rf, etc.)
        # carry no interpreter name and are not the model's concern here.
        if not ('-c\\s' in pat or '-e\\s' in pat or '-enc' in pat):
            continue
        for needle, name in _WRAPPERS.items():
            if needle in pat:
                out.add(name)
    return out


class TestTheDenylistStillGuards:
    """Nobody may 'fix' the agent by deleting the security control."""

    def test_python_dash_c_is_still_denied(self):
        pats = _deny_patterns()
        assert any('python' in p and '-c' in p for p in pats), (
            "the `python -c` denylist entry is GONE. It exists because an "
            "ethical-hacker review found interpreter one-liners bypass every "
            "other destructive-pattern rule. Removing it to make an agent "
            "walk green is a security regression, not a fix.")

    def test_there_are_interpreter_wrappers_to_announce(self):
        assert _blocked_interpreters(), (
            "no interpreter-wrapper patterns found — if the denylist really "
            "dropped them all, re-point this test; otherwise the extractor "
            "above is wrong and every assertion below is vacuous.")


class TestThePromptAnnouncesThem:

    def test_every_blocked_interpreter_is_named_in_the_action_list(self):
        from integrations.vlm.local_loop import _VLM_ACTION_LIST
        low = _VLM_ACTION_LIST.lower()
        missing = sorted(n for n in _blocked_interpreters() if n not in low)
        assert not missing, (
            f"_VLM_ACTION_LIST tells the model to PREFER shell but never says "
            f"these interpreters' one-liner forms are REFUSED by the safety "
            f"denylist: {missing}. Live 2026-09-09 the model chose "
            f"`python -c \"...\"` and every call was refused, burning the "
            f"loop's 3-error budget and failing the whole action.")

    def test_it_offers_the_sanctioned_alternative_WITH_the_prohibition(self):
        """Naming the ban without an alternative just moves the dead end.

        Deliberately NOT `'write_file' in _VLM_ACTION_LIST` — that token
        already appears in the unrelated "- File:" line, so such an assertion
        passes with the defect still present. Proven: it did exactly that on
        this test's first RED run. The property is that the alternative is
        stated WHERE the prohibition is, in one sentence the model can act on.
        """
        from integrations.vlm.local_loop import _VLM_ACTION_LIST
        blocked = _blocked_interpreters()
        # the sentence that mentions a blocked interpreter must also carry the
        # escape hatch; otherwise the model is told "no" and nothing else.
        carriers = [ln for ln in _VLM_ACTION_LIST.split('\n')
                    if any(n in ln.lower() for n in blocked)]
        assert carriers, "no line mentions a blocked interpreter at all"
        assert any('write_file' in ln.lower() for ln in carriers), (
            "the prohibition and the ALLOWED route must live in the same "
            "sentence: name the refused one-liner forms AND tell the model to "
            "write_file a script then run it with shell. A bare prohibition "
            "leaves the agent exactly as stuck as before.")
