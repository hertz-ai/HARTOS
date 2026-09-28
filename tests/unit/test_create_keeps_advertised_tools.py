"""A tool the CREATE prompts tell the model to call must stay on its schema.

Measured live 2026-09-25 (agent 1790346658454, "Mail Butler", a webmail
task on the owner's desktop, Qwen3.5-4B): the CREATE prompts say "if the
request is related to Chrome or any browser, ask @Helper to use the
`execute_windows_or_android_command` tool", then the helper-schema bounding
deferred that very tool ("CREATE helper schema bounded: deferred 35 tool(s)
... execute_windows_or_android_command ..."). Across all 88 LLM calls of the
run the tool never reached the wire, the agent made 57 tool calls that were
all send_message_to_user, and the verifier still marked "Open Chrome and
navigate to webmail" completed. These tests drive the real keep rule and the
real deferral on a helper-shaped object.
"""
import ast
import os

import core.agent_tools as at

SCREEN = 'execute_windows_or_android_command'


def _entry(name):
    return {'type': 'function', 'function': {'name': name, 'parameters': {}}}


class _Helper:
    """Just the surface defer_helper_schema reads: llm_config['tools']."""

    def __init__(self, names):
        self.llm_config = {'tools': [_entry(n) for n in names]}

    def names(self):
        return {e['function']['name'] for e in self.llm_config['tools']}


class TestTheAdvertisedToolIsKept:

    def test_the_screen_tool_is_advertised(self):
        assert SCREEN in at.CREATE_ADVERTISED_TOOLS

    def test_keep_holds_the_advertised_tool_even_when_registered_unconditionally(self):
        # Registered before the Tier-2 snapshot = "unconditional" = deferred
        # unless something keeps it.  That is exactly the live case.
        helper_names = set(at.MAIN_LEG_CORE_TOOLS) | {SCREEN, 'authorize_payment'}
        keep = at.create_helper_keep(helper_names, pre_tier2_names=helper_names)
        assert SCREEN in keep

    def test_unadvertised_unconditional_tools_are_still_deferred(self):
        helper_names = set(at.MAIN_LEG_CORE_TOOLS) | {SCREEN, 'authorize_payment'}
        keep = at.create_helper_keep(helper_names, pre_tier2_names=helper_names)
        assert 'authorize_payment' not in keep

    def test_core_request_tools_services_and_tier2_are_kept(self):
        pre = set(at.MAIN_LEG_CORE_TOOLS)
        after = pre | {'marketing_post'}                 # added by the Tier-2 gate
        keep = at.create_helper_keep(after, pre_tier2_names=pre,
                                     svc_tools={'crawl4ai': object()})
        assert set(at.MAIN_LEG_CORE_TOOLS) <= keep
        assert {'request_tools', 'crawl4ai', 'marketing_post'} <= keep

    def test_the_deferral_leaves_the_screen_tool_on_the_helper(self):
        names = sorted(set(at.MAIN_LEG_CORE_TOOLS) | {SCREEN, 'authorize_payment'})
        helper = _Helper(names)
        keep = at.create_helper_keep(helper.names(), pre_tier2_names=helper.names())
        removed = at.defer_helper_schema(helper, helper.names() - keep)
        assert SCREEN in helper.names()
        assert 'authorize_payment' in removed


def test_source_guard_create_uses_the_one_advertised_list():
    """The prompt menus and the keep rule must read CREATE_ADVERTISED_TOOLS;
    a hand-copied name list is how the menu and the schema came apart."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), 'hartos', 'create_recipe.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    menu_calls, keep_calls = [], 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)
            if fn == 'main_leg_tool_menu':
                menu_calls.append(ast.unparse(node.args[0]) if node.args else '')
            if fn == 'create_helper_keep':
                keep_calls += 1
    assert menu_calls and all(a == 'CREATE_ADVERTISED_TOOLS' for a in menu_calls), menu_calls
    assert keep_calls == 1
