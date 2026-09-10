"""An action earns a fresh round window only for progress on ITS OWN work.

THE LIVE FAILURE THIS ENCODES (2026-09-10, agent 88719487304 action 9,
rid d58-verify-215101):

    22:12:50  [FAB-GUARD] action 9 names ['send_message_to_user',
                'save_to_long_term_memory']; executed=['get_data_by_key',
                'save_data_in_memory']; unrun=['send_message_to_user', ...]
    22:12:50  [REUSE-ROUNDS] action 9 produced new tool evidence (71 -> 73)
                — resetting its round allowance (turn spend 34/108)
    ... 22:13:23 (73->76) ... 22:14:19 (79->84) ... 22:15:55 (86->89) ...
    22:21:47  (121 -> 124) — resetting its round allowance (spend 51/108)

Nine minutes, 17 rounds, 53 tool calls — and `unrun` NEVER SHRANK.  The
action's own named tools never ran once.  send_message_to_user is the tool
that hands the deliverable to the user, so the user received nothing while
the loop reported progress every round.  One of the calls that bought a
fresh window was txt2img — image generation, on an action whose job is
"Deliver the final research summary and structured JSON data to the user".

ROOT CAUSE — reuse_recipe.py ~5403 (while1) and its while2 twin ~6702:

    _evidence_now = _reuse_evidence_count(group_chat)
    if _evidence_now > _action_evidence:
        ... resetting its round allowance

`_reuse_evidence_count` counts EVERY ``role == 'tool'`` result in the chat
(see its docstring at :3564).  It is deliberately tool-name-agnostic, so a
result for ANY tool satisfies `>` and buys the action another window.  An
action that never runs its own tools can therefore spin until the TURN
ceiling, which is what burned 108 rounds here.

WHY THE CODE IS LIKE THIS — read, not guessed.  The block's own comment
records the 2026-09-09 fix it came from: action 2's tool executed at
05:55:49,887 and the per-action cap ended the turn 0.445 s later, "A cap
meant to stop STALLS ended an action that was moving."  That intent is
correct and must be preserved.  What is wrong is the SIGNAL: "the action is
moving" is not "some tool somewhere produced a result".

THE CONTRACT THESE TESTS PIN:
  * an action that NAMES tools resets its allowance only when one of THOSE
    tools produces a real result (its unrun set shrinks);
  * an action that names NO tool keeps today's behaviour — the global
    evidence count is the only progress signal it has, and D46 already
    governs whether a prose action may complete;
  * an unmeasurable count (-1) can still never be read as progress.
"""

import io
import os
import re
import unittest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _reuse_src():
    return io.open(os.path.join(_HARTOS, 'hartos', 'reuse_recipe.py'),
                   encoding='utf-8', errors='replace').read()


def _reset_blocks(src):
    """Every `[REUSE-ROUNDS] ... resetting its round allowance` site, with
    the ~25 lines above it that decide whether the reset fires."""
    out = []
    for m in re.finditer(r'resetting its round allowance', src):
        start = src.rfind('\n', 0, max(0, m.start() - 1400))
        out.append(src[start if start > 0 else 0:m.end()])
    return out


class TestResetSitesExist(unittest.TestCase):
    """If these move, the guards below must be re-pointed, not deleted."""

    def test_both_loops_have_a_reset_site(self):
        blocks = _reset_blocks(_reuse_src())
        self.assertEqual(
            len(blocks), 2,
            'expected exactly 2 round-allowance reset sites (while1 ~5403 and '
            'while2 ~6702); found %d — re-point this guard' % len(blocks))


class TestResetKeyedOnOwnToolProgress(unittest.TestCase):
    """RED until the reset consults the action's OWN tools."""

    def test_reset_is_not_driven_by_the_bare_global_count(self):
        """The global count may only appear as the names-no-tool FALLBACK.

        Precision matters here: `_reuse_evidence_count` is still the right
        answer for an action that names no tool, so its mere presence is not
        the defect.  The defect is it being the FIRST and only thing
        `_evidence_now` is set from.  This asserts the per-action measure is
        what seeds `_evidence_now`, and that any use of the global counter
        after it is reached only through the `is None` fallback.
        """
        offenders = []
        for i, block in enumerate(_reset_blocks(_reuse_src())):
            seed = re.search(r'_evidence_now\s*=\s*(\w+)\(', block)
            if not seed or seed.group(1) != '_reuse_own_tool_progress':
                offenders.append((i + 1, seed.group(1) if seed else None))
                continue
            # any later global-count use must sit under `if ... is None:`
            tail = block[seed.end():]
            for m in re.finditer(r'_evidence_now\s*=\s*_reuse_evidence_count\(', tail):
                before = tail[:m.start()]
                if not re.search(r'if\s+_evidence_now\s+is\s+None\s*:\s*$',
                                 before.rstrip('\n ').split('\n')[-1] + '\n'
                                 if before.strip() else 'x'):
                    prev_line = before.rstrip().split('\n')[-1] if before.strip() else ''
                    if 'is None' not in prev_line:
                        offenders.append((i + 1, 'unguarded global fallback'))
        self.assertEqual(
            offenders, [],
            'reset site(s) %s still fire on ANY tool result: '
            '_reuse_evidence_count(group_chat) is tool-name-agnostic '
            '(reuse_recipe.py:3564), so an action whose own named tools never '
            'run buys a fresh window with every unrelated call — agent '
            '88719487304 action 9, 2026-09-10 22:12:50-22:21:47, 17 rounds / '
            '53 tool calls with unrun never shrinking' % offenders)

    def test_reset_consults_the_current_action(self):
        """The signal must be per-ACTION, not per-chat."""
        missing = []
        for i, block in enumerate(_reset_blocks(_reuse_src())):
            # Some per-action progress notion must appear in the deciding
            # block: the action's own unrun/executed set, or a helper that
            # takes the action id.
            if not re.search(r'unrun|_action_evidence_count|_reuse_action_progress|'
                             r'referenced|_reuse_fabricated_tools', block):
                missing.append(i + 1)
        self.assertEqual(
            missing, [],
            'reset site(s) %s decide progress without reference to the '
            'action\'s own named tools; the per-action unrun set is already '
            'computed by _reuse_fabricated_tools on these same rounds — it is '
            'what prints executed=[...]/unrun=[...] beside every one of these '
            'log lines' % missing)


class TestPreservedIntent(unittest.TestCase):
    """The 2026-09-09 fix this replaces must not be undone."""

    def test_unmeasurable_evidence_is_still_never_progress(self):
        """-1 must not extend a budget (docstring contract at :3564)."""
        src = _reuse_src()
        self.assertIn(
            'Returns -1 when it cannot be measured', src,
            '_reuse_evidence_count lost its unmeasurable contract')
        for i, block in enumerate(_reset_blocks(src)):
            self.assertNotIn(
                '>=', block.split('resetting its round allowance')[0][-300:],
                'reset site %d compares with >=, so an unmeasurable or flat '
                'count would read as progress; the contract is strict >' % (i + 1))

    def test_the_stall_cap_still_exists(self):
        """Removing the cap entirely would re-open the 2026-09-06 wedge."""
        src = _reuse_src()
        self.assertIn('_round_budget', src,
                      'the TURN round ceiling is gone — a spinning action '
                      'would now be unbounded')


if __name__ == '__main__':
    unittest.main()
