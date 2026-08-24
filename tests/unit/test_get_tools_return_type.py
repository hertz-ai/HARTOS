"""get_tools(is_first=False) must return Tool OBJECTS, not rendered text.

Live 2026-08-24 08:23:44 (installed build 6, val-la6): the else branch
returned `"\\n".join(f"> {tool.name}: {tool.description}" ...)` — a STRING
— and get_ans passed it straight to
ConversationalChatAgent.create_prompt (langchain_classic
conversational_chat/base.py:99 iterates `for tool in tools`), which died
with `AttributeError: 'str' object has no attribute 'name'`.
chatbot_routes caught it and the tool-less `'_tier': 'direct'` fallback
answered — the "I do not have access to a List_Agents tool or any
external tool set" replies were the FALLBACK talking, on EVERY
casual_conv=False turn.  The branch was unreachable dead code until the
classifier override (15ed5874); all 3 crashes ever logged are the 3
override-fired turns (05:55, 06:59, 08:23).

The string shape existed for ONE consumer: CustomGPT's intent-swap
splice (`prompt[:start] + tools + prompt[end:]` between <TOOLS_START>/
<TOOLS_END>).  That consumer now renders locally; the producer returns
the objects both branches' primary consumer (get_ans) needs.

Source pins — importing hart_intelligence_entry needs the full app env.

    python -m pytest tests/unit/test_get_tools_return_type.py --noconftest -q
"""
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_SRC = open(os.path.join(ROOT, 'hart_intelligence_entry.py'),
            encoding='utf-8').read()


def _get_tools_body():
    m = re.search(r'\ndef get_tools\(.*?\n(?=# custom GPT|\nclass )',
                  _SRC, re.DOTALL)
    assert m, 'get_tools body not found'
    return m.group(0)


def test_get_tools_never_returns_rendered_string():
    body = _get_tools_body()
    assert 'return tool_strings' not in body, (
        "get_tools returns a rendered string again — create_prompt at "
        "hie:7608 crashes with \"'str' object has no attribute 'name'\" "
        "and every casual_conv=False turn falls back to the tool-less "
        "'direct' tier (live 2026-08-24 08:23:44)")
    # the exhaustive branch must end by returning the object list
    assert re.search(r'\n        return tools\s*\n', body), (
        'is_first=False branch must return the Tool list, symmetric with '
        'the is_first=True branch')


def test_intent_swap_consumer_renders_locally():
    """The one consumer that needs text — CustomGPT's <TOOLS_START>
    splice — renders at its own call site now."""
    m = re.search(
        r'tools = get_tools\(thread_local_data\.get_global_intent\(\)\)'
        r'(.{0,700}?)prompt\[:start_index\]', _SRC, re.DOTALL)
    assert m, 'intent-swap consumer not found'
    between = m.group(1)
    assert 'tool_strings' in between and '.name' in between, (
        'intent-swap splice must render the Tool list to text locally — '
        'splicing raw Tool objects into the prompt string would TypeError')
    assert 'prompt[:start_index] + tool_strings' in _SRC, (
        'splice must insert the locally-rendered string, not the raw list')
