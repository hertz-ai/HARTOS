"""Real-LLM anchor #1 — the CREATE pipeline's load-bearing capability.

CREATE can only ever produce a recipe if the local model can turn a goal
into the structured JSON the decomposer consumes.  This is the documented
#134 failure mode: the 4B emitting *prose where the pipeline expects
tool/plan JSON* (which is why ~52% of autonomous turns 500'd and the
flywheel's completion rate sat near 9%).  Every mocked test hid this,
because the mock always returned perfect JSON.

This drives the PRODUCTION autogen config -> the live model ->
``retrieve_json`` (the exact extractor ``create_recipe`` uses) and asserts
the PARSED structure, never exact tokens.  If the model can't do it, this
fails loudly — which is the point.

    python -m pytest tests/e2e/llm/test_llm_create_decompose.py -m llm_e2e -v
"""
import pytest

pytestmark = pytest.mark.llm_e2e


def test_production_config_resolves_to_the_live_model(live_llm):
    """The production autogen config_list must point at a reachable model —
    if this drifts (the recurring 8080-vs-8082 class of bug), the whole
    runtime silently can't reach its brain."""
    from core.autogen_config import get_autogen_config_list
    cfg = get_autogen_config_list()
    assert cfg and cfg[0].get("base_url"), f"no base_url in config: {cfg!r}"
    assert cfg[0]["base_url"].rstrip("/").endswith("/v1"), cfg[0]["base_url"]


def test_model_emits_plan_json_retrieve_json_can_parse(live_llm):
    """goal -> live model -> retrieve_json must yield a usable actions list.

    Asserts the SHAPE (dict/list with an actions array of {action_id, action})
    — not the wording — so it tolerates the model's natural variance while
    still proving the capability the pipeline depends on.
    """
    from helper import retrieve_json
    from core.autogen_config import get_autogen_config_list

    base = get_autogen_config_list()[0]["base_url"]
    client = type(live_llm)(base)  # drive the PRODUCTION-config endpoint
    prompt = (
        "Decompose the goal into JSON ONLY (no prose, no markdown fences): an "
        'object with an "actions" array, each item having "action_id" '
        '(integer) and "action" (short string).\n\n'
        'Goal: Create a text file and write "hello world" into it.')

    content = client.chat([{"role": "user", "content": prompt}], max_tokens=400)
    parsed = retrieve_json(content)

    assert isinstance(parsed, (dict, list)), (
        "retrieve_json could not parse the model output (the #134 prose-not-"
        f"JSON failure) — raw output was: {content[:300]!r}")
    actions = parsed.get("actions") if isinstance(parsed, dict) else parsed
    assert isinstance(actions, list) and actions, f"no actions array: {parsed!r}"
    assert any(isinstance(a, dict) and "action" in a for a in actions), (
        f"actions malformed (missing 'action' key): {actions!r}")
