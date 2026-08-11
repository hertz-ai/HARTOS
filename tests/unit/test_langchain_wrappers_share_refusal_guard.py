"""Parallel-path fix #10 (LLM-wrapper half): ``ChatQwen3VL`` and ``CustomGPT``
are both LangChain ``LLM`` wrappers for the local llama server and they DRIFTED —
``CustomGPT._call`` routed through ``_pooled_post_with_refusal_check`` (the
refusal-override retry guard) while ``ChatQwen3VL._call`` used raw
``pooled_post``, so a draft refusal from ChatQwen3VL flowed straight to the user.
ChatQwen3VL now routes through the SAME helper.
"""
import re
from pathlib import Path

_TEXT = (Path(__file__).resolve().parents[2] / 'hart_intelligence_entry.py').read_text(encoding='utf-8')


def _class_body(name):
    m = re.search(rf"^class {name}\(LLM\):.*?(?=^class |\Z)", _TEXT, re.DOTALL | re.MULTILINE)
    assert m, f"{name} class not found"
    return m.group(0)


def test_chatqwen_call_now_uses_the_refusal_check_helper():
    body = _class_body('ChatQwen3VL')
    assert '_pooled_post_with_refusal_check(' in body, \
        "ChatQwen3VL._call must route through the refusal-check helper"
    # the raw pooled_post( (no refusal guard) must be gone from the wrapper
    assert not re.search(r'\bpooled_post\(', body), \
        "raw pooled_post (skipping the refusal guard) must be gone from ChatQwen3VL"


def test_both_llm_wrappers_share_the_same_refusal_guard():
    assert '_pooled_post_with_refusal_check(' in _class_body('ChatQwen3VL')
    assert '_pooled_post_with_refusal_check(' in _class_body('CustomGPT')
