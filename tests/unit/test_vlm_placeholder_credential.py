"""The VLM computer-use loop must never type a FABRICATED credential.

MEASURED LIVE 2026-09-10, and this is the whole defect in one line: driving
"post to Twitter", the VLM loop reached x.com's login form, had no credential,
invented one, typed it into the real field, and reported success:

    action: 'Type the user credentials into the visible "Email or username"
             input field.'
    result: 'Typed: user@example.com...'         'ok': True
    result: 'Typed: your_email@example.com...'   'ok': True

Both strings are RFC 2606 reserved-for-documentation addresses; neither can
ever be a real account.  Neither is a literal on the computer-use path --
grepped both repos: the only repo hits are a MentionService parse fixture
(tests/test_phase7b_mentions.py:109) and aider's git-config helper, which uses
the DIFFERENT literal testuser@example.com.  So the model generated them.

WHY THIS IS A SAFETY DEFECT, NOT A COSMETIC ONE: repeated bad logins against a
real account trip rate-limiting and security locks.  The loop had no
"I lack a credential, stop" branch, so it filled the field the way it fills
any other field, and `ok: True` told every downstream consumer it worked.

THE GUARD IS DELIBERATELY NARROW.  marketing_tools._mentions documents a prior
incident where loose keywords produced 361 spurious matches, so this only
fires when the WHOLE trimmed text is a bare credential-shaped token whose
domain is reserved, or which carries a placeholder prefix.  Prose that merely
mentions example.com ("visit example.com for docs") must pass -- typing that
into a document is legitimate and blocking it would be a regression.

    python -m pytest tests/unit/test_vlm_placeholder_credential.py --noconftest -q
"""
import pytest


def _checker():
    from integrations.vlm.safety import is_placeholder_credential
    return is_placeholder_credential


class TestTheObservedFailureIsBlocked:
    """The two strings actually typed into x.com on 2026-09-10."""

    @pytest.mark.parametrize("text", [
        "user@example.com",
        "your_email@example.com",
    ])
    def test_observed_placeholder_is_blocked(self, text):
        block = _checker()({'action': 'type', 'text': text})
        assert block, (
            f"{text!r} was typed into a REAL x.com login field and reported "
            f"ok:True; it must be refused before pyautogui is called")

    def test_block_reason_names_the_cause(self):
        block = _checker()({'action': 'type', 'text': 'user@example.com'})
        assert 'placeholder' in block.lower(), (
            f"block reason must say why, got {block!r}")

    def test_value_key_alias_is_covered(self):
        """_execute_inprocess reads text via action.get('text', action.get('value')).

        A guard that only reads 'text' would be bypassed by the 'value'
        spelling -- exactly the kind of half-covered gate that reads as fixed
        and is not.
        """
        block = _checker()({'action': 'type', 'value': 'user@example.com'})
        assert block, "the 'value' key alias bypasses the guard"


class TestPrecisionGuardsAgainstOverBlocking:
    """A guard that blocks legitimate typing is a regression, not a fix."""

    def test_prose_mentioning_example_com_is_allowed(self):
        block = _checker()({
            'action': 'type',
            'text': 'See the docs at example.com for the full API reference.'})
        assert block is None, (
            f"prose that merely mentions the domain must be typeable, got {block!r}")

    def test_a_real_looking_address_is_allowed(self):
        block = _checker()({'action': 'type', 'text': 'sathish@hevolve.ai'})
        assert block is None, f"a real address must pass, got {block!r}"

    def test_ordinary_text_is_allowed(self):
        block = _checker()({
            'action': 'type', 'text': 'the guardian angel principle'})
        assert block is None, f"ordinary text must pass, got {block!r}"

    def test_non_type_actions_are_out_of_scope(self):
        """Only typing puts a credential into a field.

        A 'shell' action whose payload happens to contain the token is a
        different concern and must not be swept up here.
        """
        block = _checker()({'action': 'shell', 'text': 'user@example.com'})
        assert block is None, f"only 'type' is in scope, got {block!r}"

    def test_empty_text_is_allowed(self):
        assert _checker()({'action': 'type', 'text': ''}) is None
        assert _checker()({'action': 'type'}) is None


class TestReservedDomainCoverage:
    """RFC 2606 / RFC 6761 reserve these; none can be a real account."""

    @pytest.mark.parametrize("text", [
        "admin@example.org",
        "someone@example.net",
        "me@something.invalid",
        "user@host.test",
    ])
    def test_reserved_domains_blocked(self, text):
        assert _checker()({'action': 'type', 'text': text}), (
            f"{text!r} uses a reserved documentation domain")

    @pytest.mark.parametrize("text", [
        "your_email@gmail.com",
        "youremail@gmail.com",
        "yourname@gmail.com",
    ])
    def test_placeholder_prefixes_blocked(self, text):
        assert _checker()({'action': 'type', 'text': text}), (
            f"{text!r} is a placeholder the model filled in for a real one")


class TestTheGuardIsActuallyWired:
    """A checker nothing calls is a vacuous fix.

    These assert the REFUSAL reaches execute_action's return value, which is
    what the VLM loop actually consumes -- not just that the predicate works.
    The live caller is local_loop.py:690, which passes
    safety=env_flag('HEVOLVE_VLM_LOOP_SAFETY', True), i.e. ON by default, so
    this path is the one that typed user@example.com into x.com.
    """

    def test_execute_action_refuses_the_fabricated_credential(self):
        from integrations.vlm.local_computer_tool import execute_action
        res = execute_action(
            {'action': 'type', 'text': 'user@example.com'},
            'inprocess', safety=True)
        assert res.get('status') == 'safety_blocked', (
            f"execute_action still performed the type; got {res!r}")
        assert 'placeholder' in (res.get('safety_block') or '').lower(), res
        assert res.get('output') == '', (
            "a refused action must not report typed output")

    def test_execute_action_still_allows_ordinary_text(self):
        """No-regression: the guard must not block normal typing.

        Asserts only that we get PAST the safety gate -- the action itself may
        fail later for want of a display, which is fine and not what is
        under test.
        """
        from integrations.vlm.local_computer_tool import execute_action
        res = execute_action(
            {'action': 'type', 'text': 'the guardian angel principle'},
            'inprocess', safety=True)
        assert res.get('status') != 'safety_blocked', (
            f"ordinary text was blocked -- regression; got {res!r}")
