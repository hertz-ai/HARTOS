"""A VLM re-learning may REFINE an action; it may not REDEFINE it.

THE LIVE FAILURE (agent 88719487304, installed build, drive d70, 2026-09-11
04:52 -> 05:33, session 6c2dc0fc-7c93-4fe0-973e-f7466ff63f29_88719487304).
Four ``*_vlm_agent.json`` files were written DURING the drive, each under the
id of the action the ledger happened to be pointing at, and not one of them
describes that action's work:

  id  banked action (88719487304_0_recipe.json)        file's own "action"
  --  ----------------------------------------------   -------------------
  2   execute_windows_or_android_command: 'Open        "Restart the computer to
      default web browser and navigate to the top       clear any locked files"
      result URL for HART OS documentation'             (04:56:53)
  3   execute_coding_task: 'Write Python script to     "Check the current time
      parse HART OS documentation text...'              on Windows"  (04:57:36)
  4   execute_coding_task: 'Execute the Python         "Create a new directory
      script to generate the structured JSON output'    called 'HART_OS'..."
                                                        (05:03:23)
  7   execute_coding_task: 'Generate a comprehensive   "Create a new directory
      research summary text based on the extracted      called 'HART_OS' ...
      JSON data'                                        administrative..."
                                                        (05:21:19)

``helper.load_vlm_agent_files`` parses the id back out of the FILENAME as the
action's identity (parts[2]), and ``_vlm_merged_actions`` then replaces that
action's ``recipe`` with the file's.  f8bfbcc04 already keeps the GOAL --
``action`` is in ``_VLM_PRESERVED_CONTRACT_FIELDS`` -- so after the merge
action 7 carries the RIGHT goal ("generate a research summary") and IMPOSSIBLE
steps (one ``execute_windows_or_android_command``).  The per-action attach
(703112bcd) then binds that tool because the steps name it, the model runs it,
a shell command cannot produce prose, and the verdict is:

    {'status': 'error', 'action': 'execute_coding_task', 'action_id': 7,
     'message': "The tool 'execute_windows_or_android_command' returned an
     error. The system failed to execute the command to generate the research
     summary text."}

12 bounded error cycles, 05:21 -> 05:33, no summary.  It is also
SELF-PERPETUATING: the merged step feeds the model the file's own text, the
model calls the tool with it again, and the writer rewrites the same file.

THE COST IS A FALSE PASS, NOT ONLY WRONG WORK.  Action 3's fabrication gate
demanded the SUBSTITUTED tool, the agent ran it, and the gate released --
"[REUSE] Action 3 TERMINATED, advancing", unrun=[] -- so the walk advanced
having never written the parser that is action 3's entire job.

WHERE IT COMES FROM.  reuse_recipe.py:1928, inside
``execute_windows_or_android_command``:

    if response and response['status'] == 'success':
        if not matching_recipe:                 # <- the only gate
            ...
            action_id = user_tasks[user_prompt].current_action
            recipe_data = {"action": instructions, "action_id": action_id, ...}

``matching_recipe is None`` is reached only when ``instructions`` resembles
NONE of the banked actions -- the current one INCLUDED, because the loop above
compares against every one of them.  That is precisely the evidence that this
instruction is not the current action's work, and the writer stamps it under
the current action's id anyway.  The filename is an identity claim, and it is
made in the one branch where the evidence already contradicts it.

WHY A GUARD AND NOT A NEW ID.  Appending under a fresh id was the previous
shape and was removed for cause: agent 33323830039's 1-action recipe grew to 4
over two drives, and the appended actions each carry
``can_perform_without_user_input: 'no'``, which disarms every driver.  The
writer's own comment records that.  Refusing the write leaves the action's
CREATE-authored steps intact, which is the correct fallback.

WHAT THIS GUARD DOES NOT CLAIM: that the model stops inventing desktop
commands (that is its own defect), or that the agent then reaches its goal.
It claims a re-learning cannot overwrite the steps of an action whose own
banked text it does not match.
"""

import io
import os
import re
import unittest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Overridable so the guard can be pointed at a DIFFERENT revision and proven to
# fail there.  A guard only ever run against the fixed source has not been shown
# to be capable of failing -- this suite has been bitten twice by exactly that
# (memory/feedback_vacuous_guards.md).
#   HARTOS_REUSE_SRC=<git show HEAD:hartos/reuse_recipe.py > tmp> pytest <this>
_SRC = os.environ.get('HARTOS_REUSE_SRC') or os.path.join(
    _HARTOS, 'hartos', 'reuse_recipe.py')

_THRESHOLD = 0.8

# Verbatim from the live drive: what the recipe says vs what the file claimed.
# Kept as data, not prose, so the arithmetic below is about the real strings.
_SUBSTITUTIONS = {
    2: ("execute_windows_or_android_command: 'Open default web browser and "
        "navigate to the top result URL for HART OS documentation'",
        'Restart the computer to clear any locked files and ensure a clean '
        'state for the next operation.'),
    3: ("execute_coding_task: 'Write Python script to parse HART OS "
        "documentation text and extract performance metrics'",
        'Check the current time on Windows'),
    4: ("execute_coding_task: 'Execute the Python script to generate the "
        "structured JSON output'",
        "Create a new directory called 'HART_OS' in the user's home directory "
        'and initialize a Git repository with the default branch'),
    7: ("execute_coding_task: 'Generate a comprehensive research summary text "
        "based on the extracted JSON data'",
        "Create a new directory called 'HART_OS' in the current directory with "
        'full administrative permissions. If the directory already exists, '
        'report that it exists.'),
}


def _src():
    return io.open(_SRC, encoding='utf-8', errors='replace').read()


def _overlap_ratio(a, b):
    """The metric ``similar_instructions`` uses, verbatim (reuse_recipe:1811)."""
    w1, w2 = set(a.lower().split()), set(b.lower().split())
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / max(len(w1), len(w2))


def _norm():
    """The canonical normaliser, imported from its real home.

    Imported rather than reimplemented: a local copy would score whatever this
    file says and prove nothing about what the pipeline does.  Same reasoning
    as test_action_matches_its_own_recipe, which shares this helper.
    """
    from hartos.helper import strip_authored_tool_prefix
    return strip_authored_tool_prefix


def _write_region(src):
    """The writer's decision region: the direct-file check through the write.

    Bounded by two comments that are part of the code's own narration, so the
    region moves with the code instead of being pinned to line numbers.
    """
    m = re.search(r'# Direct file check as backup(.*?)'
                  r'Generated recipe data saved to', src, re.S)
    return m.group(0) if m else ''


def _func(name, src):
    """One top-level function's source, bounded by the next TOP-LEVEL statement.

    Not ``(?=^def )``: reuse_recipe has module-level code between functions and
    the greedy form swallows it.
    """
    m = re.search(r'^def %s\(.*\n(?:(?:[ \t].*)?\n)*' % re.escape(name), src, re.M)
    return m.group(0) if m else ''


class TestTheComparatorCanTellThemApart(unittest.TestCase):
    """The fix rests on one assumption; measure it before asserting on it.

    If a substituted instruction scored ABOVE the gate against the action it
    replaced, no identity check could refuse the write and the fix would be
    the wrong shape.
    """

    def test_every_substitution_scores_below_the_gate(self):
        n = _norm()
        for aid, (banked, claimed) in sorted(_SUBSTITUTIONS.items()):
            score = _overlap_ratio(n(banked), n(claimed))
            self.assertLess(
                score, _THRESHOLD,
                'action %d: the file that overwrote it scores %.4f against '
                'the banked action, i.e. the comparator cannot tell the '
                'substitution apart from a refinement -- the identity guard '
                'would be unable to refuse it' % (aid, score))

    def test_each_banked_action_still_matches_itself(self):
        """A guard that refuses everything is worse than the bug."""
        n = _norm()
        for aid, (banked, _claimed) in sorted(_SUBSTITUTIONS.items()):
            score = _overlap_ratio(n(banked), n(banked))
            self.assertGreaterEqual(
                score, _THRESHOLD,
                'action %d no longer matches its own text (%.4f) -- the guard '
                'would refuse a legitimate re-learning too' % (aid, score))


class TestTheWriteIsGatedOnIdentity(unittest.TestCase):
    """RED until the write checks whose action it is claiming."""

    def setUp(self):
        self.region = _write_region(_src())
        self.assertTrue(
            self.region,
            'the writer region moved; re-point this guard before trusting it')

    def test_the_write_is_not_gated_on_matching_recipe_alone(self):
        m = re.search(r'if not matching_recipe[^\n]*', self.region)
        self.assertTrue(m, 'the write gate moved out of the region')
        line = m.group(0)
        self.assertNotEqual(
            line.strip(), 'if not matching_recipe:',
            'the write is still gated ONLY on "no banked action matched" -- '
            'which is exactly the evidence that this instruction is not the '
            "current action's work.  Four files were written under the wrong "
            'action id on one drive (2026-09-11 04:56-05:21).')

    def _operand_block(self):
        """The statements that define the gate's SECOND operand.

        Scoped deliberately.  Asserting these tokens against the whole region
        ahead of the gate would be VACUOUS: on HEAD that stretch already
        contains ``current_action_id`` 3 times and ``similar_instructions``
        once (both measured), so such a test passes against the broken source
        and proves nothing -- the exact trap recorded in
        memory/feedback_vacuous_guards.md.  So: read the operand's NAME off
        the gate line, then look only at where that name is assigned.
        """
        gate = re.search(r'if not matching_recipe([^\n:]*)', self.region)
        extra = (gate.group(1) if gate else '').strip()
        self.assertTrue(
            extra, 'the gate carries no second operand to trace')
        names = [n for n in re.findall(r'[A-Za-z_]\w*', extra)
                 if n not in ('and', 'or', 'not')]
        self.assertTrue(names, 'the second operand names no identifier')

        # Follow the operand's DEPENDENCY CLOSURE, not just its own assignment:
        # the check is naturally two statements (read the banked action, then
        # compare against it), and stopping at one level would report the
        # comparison as if it came from nowhere.  Bounded, so a cycle or a
        # common name cannot spin.
        seen, frontier, block = set(), list(names), []
        for _ in range(4):
            nxt = []
            for name in frontier:
                if name in seen:
                    continue
                seen.add(name)
                for m in re.finditer(
                        r'^([ \t]*)%s\s*=(?:.*\n)((?:\1[ \t].*\n|[ \t]*\n)*)'
                        % re.escape(name), self.region, re.M):
                    block.append(m.group(0))
                    nxt.extend(re.findall(r'[A-Za-z_]\w*', m.group(0)))
            frontier = nxt
            if not frontier:
                break
        return ''.join(block), names

    def test_the_gate_consults_the_current_action(self):
        block, names = self._operand_block()
        self.assertIn(
            'current_action_id', block,
            'the identity check (%s) is not computed from the id the file '
            'will actually claim, so it can vet one action and write another'
            % ', '.join(names))

    def test_it_reuses_the_existing_comparator(self):
        block, names = self._operand_block()
        self.assertIn(
            'similar_instructions', block,
            'the identity check (%s) uses a second rule instead of the '
            'comparator already in this function; two rules for "is this the '
            'same action" will drift' % ', '.join(names))

    def test_it_reads_the_banked_action_through_the_shared_accessor(self):
        block, names = self._operand_block()
        self.assertIn(
            '_reuse_action_at', block,
            'the identity check (%s) inlines its own id->action lookup, a '
            'third copy of what _reuse_action_tool_names and '
            '_reuse_action_declares_tool already do' % ', '.join(names))

    def test_a_refusal_is_logged_loudly_enough_to_measure(self):
        """gui_app.log captures ZERO debug lines (0 of 45,599 measured).

        Whole region, and ``warning`` specifically: the region already carries
        exactly one ``logger.error`` on HEAD ("Error reading direct VLM file"),
        so accepting error|debug here would pass against the broken source.
        """
        levels = re.findall(r'current_app\.logger\.(\w+)\(', self.region)
        self.assertIn(
            'warning', levels,
            'a refused re-learning must be countable in production. debug is '
            'invisible there (0 DEBUG lines in 45,599 measured gui_app.log '
            'lines) and info is drowned, so the next drive could not measure '
            'whether the guard fired -- or whether it fires too often and is '
            'costing legitimate re-learnings.  Levels found: %r' % (levels,))


class TestOneLookupNotThree(unittest.TestCase):
    """DRY: "the banked action at this id" gets ONE derivation.

    ``_reuse_action_tool_names`` and ``_reuse_action_declares_tool`` already
    inline the same two-line lookup.  The identity guard would be the third.
    """

    def setUp(self):
        self.src = _src()

    def test_the_accessor_exists(self):
        self.assertTrue(
            _func('_reuse_action_at', self.src),
            'no single accessor for the banked action at an id; the identity '
            'guard would be a third inline copy of the same lookup')

    def test_both_existing_readers_use_it(self):
        for name in ('_reuse_action_tool_names', '_reuse_action_declares_tool'):
            body = _func(name, self.src)
            self.assertTrue(body, '%s moved' % name)
            self.assertIn(
                '_reuse_action_at', body,
                '%s still inlines its own lookup, so the three copies can '
                'drift on how an action id maps to an action' % name)

    def test_the_accessor_never_raises(self):
        """It runs on the dispatch path; it must not be able to kill a turn."""
        body = _func('_reuse_action_at', self.src)
        self.assertIn('except Exception', body,
                      'the accessor must fail closed, like its two callers')


if __name__ == '__main__':
    unittest.main()
