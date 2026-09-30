"""A credential reaches the model only as an alias, and reaches a tool only
as its real value.

Owner requirement (2026-09-25): the user gives a credential once, it is kept
encrypted, and the LLM works with a pseudonymous alias.  The real value is
substituted deterministically where the tool runs and never flows back to the
model, the logs or chat.

The alias is {{secret:NAME}}.  AIKeyVault owns the format, the resolution and
the masking; the tool-execution chokepoints (core.tool_logging and the VLM
execute_action) only call it.  These tests drive the real vault, the real
decorator and the real execute_action.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

from hartos.ai_key_vault import AIKeyVault  # noqa: E402

SECRET = 'Tr0ub4dor&3-horse'


@pytest.fixture
def vault(monkeypatch):
    monkeypatch.delenv('HEVOLVE_MASTER_KEY', raising=False)
    monkeypatch.delenv('SITE_PASSWORD', raising=False)
    from security.secrets_manager import SecretsManager
    SecretsManager.reset()
    AIKeyVault.reset()
    v = AIKeyVault.get_instance()
    v.store_credential('site_password', SECRET)
    yield v
    AIKeyVault.reset()
    SecretsManager.reset()
    os.environ.pop('SITE_PASSWORD', None)


def test_alias_names_the_stored_credential(vault):
    assert vault.alias_for('site_password') == '{{secret:SITE_PASSWORD}}'


def test_resolve_replaces_aliases_everywhere_and_leaves_unknown_ones(vault):
    alias = vault.alias_for('site_password')
    got = vault.resolve_aliases({
        'command': f'login --pass {alias}',
        'steps': [alias, {'text': alias}],
        'other': '{{secret:NOT_STORED}}',
        'n': 3,
    })
    assert got == {
        'command': f'login --pass {SECRET}',
        'steps': [SECRET, {'text': SECRET}],
        'other': '{{secret:NOT_STORED}}',
        'n': 3,
    }


def test_mask_turns_every_stored_value_back_into_its_alias(vault):
    text = f'Logged in with {SECRET}; again {SECRET}.'
    masked = vault.mask_secrets(text)
    assert SECRET not in masked
    assert masked.count('{{secret:SITE_PASSWORD}}') == 2
    assert vault.mask_secrets('nothing secret here') == 'nothing secret here'


def test_a_tool_gets_the_real_value_and_the_model_gets_the_alias(vault):
    from core.tool_logging import log_tool_execution
    seen = {}

    @log_tool_execution
    def echo_tool(password: str) -> str:
        seen['password'] = password
        return f'server said: welcome, your password {password} is valid'

    result = echo_tool(password=vault.alias_for('site_password'))
    assert seen['password'] == SECRET, 'the tool must receive the real value'
    assert SECRET not in result, 'the tool result must not carry it back'
    assert '{{secret:SITE_PASSWORD}}' in result


def test_the_vlm_types_the_real_value_and_reports_neither(vault, monkeypatch):
    from integrations.vlm import local_computer_tool as lct
    typed = []

    class _Gui:
        def typewrite(self, text, interval=0):
            typed.append(text)

    monkeypatch.setattr(lct, 'pyautogui', _Gui())
    monkeypatch.setattr(lct, 'pyperclip', None)
    result = lct.execute_action(
        {'action': 'type', 'text': vault.alias_for('site_password')},
        'inprocess')
    assert typed == [SECRET]
    assert SECRET not in str(result)
