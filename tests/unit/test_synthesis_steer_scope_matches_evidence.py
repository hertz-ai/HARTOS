"""The synthesis steer may not assert more than its evidence supports.

MEASURED LIVE 2026-09-09 18:10 (agent 33323830039), and this is the whole
defect: the steer told the model

    "These tools did NOT execute in this conversation:
     execute_windows_or_android_command"

while that tool had executed 25 seconds earlier IN THAT SAME CONVERSATION --
  18:10:07  INSIDE execute_windows_or_android_command
  18:10:19  [FAB-GUARD] action 1 ... executed=[...]; unrun=[]
  18:10:32  the steer above, fired for action 2

The cause is a SCOPE MISMATCH, not a wrong value. The caller computes

    _unrun = _reuse_outstanding_tools(user_prompt,
                 _reuse_current_action_id(user_prompt), group_chat)

i.e. outstanding FOR THE CURRENT ACTION. The sentence then generalises that
per-action fact to the whole conversation. The same steer also orders the
model to "report what WAS actually done, using only the real tool results
present in this conversation" -- so it is told to use results it has just
been told do not exist.

    python -m pytest tests/unit/test_synthesis_steer_scope_matches_evidence.py --noconftest -q
"""
import re

REPO = __file__.rsplit("tests", 1)[0]


def _src():
    return open(REPO + "hartos/reuse_recipe.py", encoding="utf-8",
                errors="replace").read()


def _steer_text(src):
    """The ASSEMBLED steer, not its source form.

    The constant is built from implicitly-concatenated literals, so
    "Do not describe unexecuted " + "work as done" never appears contiguously
    in the file. Asserting on raw source therefore fails on wording that IS
    present — my first version of this test did exactly that. Join the
    adjacent literals the way Python does before asserting on prose.
    """
    i = src.index("_REUSE_SYNTHESIS_STEER_INCOMPLETE = (")
    block = src[i:src.index("\n)\n", i)]
    # drop the `" <newline+indent> "` seams between adjacent string literals
    return re.sub(r'"\s*\n\s*[\'"]', "", block)


class TestScopeMatchesEvidence:

    def test_unrun_is_computed_per_action(self):
        """Anchor: if this stops being per-action the test below is moot."""
        src = _src()
        assert re.search(r"_reuse_outstanding_tools\(\s*\n?\s*user_prompt,\s*\n?"
                         r"\s*_reuse_current_action_id\(user_prompt\)", src), (
            "the steer's `unrun` is no longer computed from the CURRENT "
            "ACTION id -- re-check what scope the wording may claim")

    def test_steer_does_not_claim_conversation_scope(self):
        """A per-action fact may not be stated as a conversation-wide one."""
        text = _steer_text(_src())
        assert "did NOT execute in this conversation" not in text, (
            "the steer asserts the tools never executed IN THIS CONVERSATION, "
            "but `unrun` only says they are outstanding for the CURRENT "
            "ACTION. Live 2026-09-09 18:10:32 this told agent 33323830039 "
            "that execute_windows_or_android_command had not run, 25s after "
            "it ran (18:10:07) and after FAB-GUARD recorded unrun=[] for "
            "action 1 (18:10:19).")

    def test_steer_names_the_action_scope(self):
        """State the scope the evidence actually has."""
        text = _steer_text(_src()).lower()
        assert "action" in text, (
            "the steer must say the tools are outstanding FOR THIS ACTION, "
            "so the claim matches _reuse_outstanding_tools' per-action scope")


class TestTheRestOfTheSteerIsUnchanged:
    """The scope fix must not disturb what already works."""

    def test_still_asks_for_message2userfinal(self):
        assert "message2userfinal" in _steer_text(_src()), (
            "the extractor unwraps this key; a different shape produces an "
            "answer nobody reads (#797/D31)")

    def test_still_forbids_further_tool_calls(self):
        t = _steer_text(_src())
        assert "Do NOT run any tool again" in t
        assert "do NOT emit another status object" in t

    def test_still_forbids_describing_unexecuted_work_as_done(self):
        assert "Do not describe unexecuted work as done" in _steer_text(_src())
