"""A window-CLOSING action must not fire at a window it did not target.

MEASURED LIVE 2026-09-10.  Task: "Close the HART Marketing Dashboard window
by clicking the X".  What the loop actually did:

    11:33:25  Action: hotkey value='alt+tab'    <- focus moves to UNKNOWN
    11:33:29  Action: hotkey value='alt+tab'    <- focus moves again
    11:33:31  Action: hotkey value='alt+f4'     <- closes whatever it landed on

54 alt+f4 fired that day.  alt+f4 against an arbitrary focused window can
destroy unsaved work, and unlike a stray click it is not recoverable.

THE GUARD THAT SHOULD HAVE CAUGHT THIS WAS BROKEN TWO WAYS:

1. INERT, FOR TWO INDEPENDENT REASONS.
   (a) _REASONING_MISMATCH_PATTERNS held exactly two hardcoded app names --
       ('mobaxt','mobaxt') and ('notepad','notepad').  "HART Marketing
       Dashboard", Chrome, Twitter can never match.
   (b) DEEPER, and found only by checking the live payload builder:
       local_loop._build_action_payload NEVER COPIED 'Reasoning' INTO THE
       PAYLOAD.  It forwards action/coordinate/text plus six file+shell
       keys, and drops the rest.  So execute_action received no reasoning
       at all and every reasoning-based check was dead on the live path
       regardless of its pattern list.  The VLM does emit the field
       (local_loop.py:629/673/711 read action_json['Reasoning']) -- only
       the hand-off lost it.
   Live count that day: 0 firings out of 54 alt+f4.  Cause (b) alone is
   sufficient to explain that zero, and fixing only (a) would have left
   this guard just as dead.

2. POST-HOC even when it does fire.  local_computer_tool.py computes
   _mismatch at :253 but consumes it at :298 -- AFTER _execute_inprocess has
   already sent the keystroke.  It sets result['window_mismatch'] on an
   action that already happened.  Detecting and then acting anyway is not a
   guard.

SCOPE IS DELIBERATELY NARROW.  Only DESTRUCTIVE window actions are blocked,
and only on POSITIVE evidence of mismatch: the reasoning names a target and
the foreground window is not it.  Reasoning that names no target is allowed
through unchanged -- "close the window" with nothing to compare must keep
working, or agents lose the ability to close windows at all.  That
regression would be worse than the bug.

    python -m pytest tests/unit/test_vlm_destructive_window_guard.py --noconftest -q
"""
import pytest


def _guard():
    from integrations.vlm.local_computer_tool import _check_destructive_window_mismatch
    return _check_destructive_window_mismatch


class TestTheObservedFailureIsBlocked:
    """The exact 2026-09-10 sequence."""

    def test_alt_f4_at_the_wrong_window_is_blocked(self):
        block = _guard()(
            {'action': 'hotkey', 'text': 'alt+f4',
             'Reasoning': 'Close the HART Marketing Dashboard window by '
                          'clicking the X'},
            active_window='Google Chrome - Twitter / X')
        assert block, (
            'alt+f4 fired while Chrome was foreground and the task named the '
            'HART Marketing Dashboard -- this closes the wrong window')

    def test_block_reason_names_both_sides(self):
        block = _guard()(
            {'action': 'hotkey', 'text': 'alt+f4',
             'Reasoning': 'Close the HART Marketing Dashboard window'},
            active_window='Google Chrome')
        assert 'HART Marketing Dashboard' in block and 'Chrome' in block, (
            f'reason must name the intended target AND the actual window, '
            f'got {block!r}')

    def test_ctrl_w_is_also_destructive(self):
        assert _guard()(
            {'action': 'hotkey', 'text': 'ctrl+w',
             'Reasoning': 'Close the Budget Spreadsheet tab'},
            active_window='Slack'), 'ctrl+w closes things too'


class TestItFiresOnTheRightWindow:
    """Matching target -> allowed.  The agent must still do its job."""

    def test_alt_f4_at_the_named_window_is_allowed(self):
        block = _guard()(
            {'action': 'hotkey', 'text': 'alt+f4',
             'Reasoning': 'Close the HART Marketing Dashboard window'},
            active_window='HART Marketing Dashboard - Chrome')
        assert block is None, f'this IS the target window, got {block!r}'

    def test_partial_case_insensitive_match_counts(self):
        block = _guard()(
            {'action': 'hotkey', 'text': 'alt+f4',
             'Reasoning': 'Close the Marketing Dashboard'},
            active_window='marketing dashboard — mozilla firefox')
        assert block is None, f'case/substring match should pass, got {block!r}'


class TestPrecisionGuardsAgainstOverBlocking:
    """A guard that stops legitimate closes is a worse bug than the one it fixes."""

    def test_no_named_target_is_allowed(self):
        """"close the window" names nothing -- nothing to compare, so allow."""
        block = _guard()(
            {'action': 'hotkey', 'text': 'alt+f4',
             'Reasoning': 'close the window'},
            active_window='Some App')
        assert block is None, (
            f'with no named target there is no evidence of mismatch; '
            f'blocking here would stop agents closing windows at all, '
            f'got {block!r}')

    def test_no_reasoning_at_all_is_allowed(self):
        assert _guard()({'action': 'hotkey', 'text': 'alt+f4'},
                        active_window='Some App') is None

    def test_unknown_active_window_is_allowed(self):
        """Cannot compare against nothing -- do not block on ignorance."""
        assert _guard()(
            {'action': 'hotkey', 'text': 'alt+f4',
             'Reasoning': 'Close the HART Marketing Dashboard'},
            active_window=None) is None

    @pytest.mark.parametrize("act,text", [
        ('hotkey', 'alt+tab'),      # focus change, not destructive
        ('left_click', None),       # a wrong click is recoverable
        ('type', 'hello'),
        ('key', 'enter'),
    ])
    def test_non_destructive_actions_are_out_of_scope(self, act, text):
        block = _guard()(
            {'action': act, 'text': text,
             'Reasoning': 'Close the HART Marketing Dashboard window'},
            active_window='Totally Different App')
        assert block is None, (
            f'{act}/{text} is not destructive; only irreversible window '
            f'closing is gated here, got {block!r}')


class TestItBlocksBeforeExecuting:
    """The old guard annotated AFTER the keystroke.  This one must refuse."""

    def test_execute_action_refuses_the_destructive_mismatch(self, monkeypatch):
        import integrations.vlm.local_computer_tool as lct
        monkeypatch.setattr(lct, 'get_active_window_info',
                            lambda: 'Google Chrome - Twitter / X')
        fired = []
        monkeypatch.setattr(lct, '_execute_inprocess',
                            lambda a: fired.append(a) or {'output': 'ran'})
        res = lct.execute_action(
            {'action': 'hotkey', 'text': 'alt+f4',
             'Reasoning': 'Close the HART Marketing Dashboard window'},
            'inprocess', safety=True)
        assert not fired, (
            'the keystroke was SENT -- the guard annotated instead of '
            'blocking, which is the original defect')
        assert res.get('status') == 'safety_blocked', res


class TestTheReasoningReachesTheGuardOnTheLivePath:
    """A guard that never receives the reasoning is decoration.

    The live VLM loop does not call execute_action with the raw model JSON
    -- it calls _build_action_payload first.  That builder forwards a fixed
    key list, so a check reading action['Reasoning'] sees nothing unless
    'Reasoning' is on that list.  This is the assertion that separates a
    real fix from a vacuous one, and it FAILED when first written.
    """

    def test_payload_builder_forwards_reasoning(self):
        from integrations.vlm.local_loop import _build_action_payload
        payload = _build_action_payload(
            {'Next Action': 'hotkey', 'value': 'alt+f4',
             'Reasoning': 'Close the HART Marketing Dashboard window'},
            {})
        assert payload.get('Reasoning') == (
            'Close the HART Marketing Dashboard window'), (
            'the live payload drops Reasoning, so every reasoning-based '
            'safety check is inert in production no matter how good its '
            f'matching is. Payload was: {payload!r}')

    def test_built_payload_is_blocked_end_to_end(self):
        """Builder output -> guard, with nothing hand-assembled in between."""
        from integrations.vlm.local_loop import _build_action_payload
        from integrations.vlm.local_computer_tool import (
            _check_destructive_window_mismatch)
        payload = _build_action_payload(
            {'Next Action': 'hotkey', 'value': 'alt+f4',
             'Reasoning': 'Close the HART Marketing Dashboard window'},
            {})
        assert _check_destructive_window_mismatch(
            payload, active_window='Google Chrome - Twitter / X'), (
            'the REAL payload the loop builds is not blocked -- the guard '
            'only works on hand-written dicts, which is exactly the trap '
            'this class exists to catch')
