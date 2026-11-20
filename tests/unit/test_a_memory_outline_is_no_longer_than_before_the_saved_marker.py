"""A memory outline is no longer than it was before the saved marker, and an
outline shown to other people claims no value.

WHAT 88d7e3aac FIXED (live, email agent, 18 Nov 2026): a saved text outlined
as None, so the 4B read its saved draft as empty and asked again.  Every leaf
now outlines as "<saved_<type>>", a text as "<saved_str>".

WHAT IT COST (rv_88d7):
  * S4: outlines grew 28% (get_saved_metadata over the desktop's stores,
    628,055 to 806,259 chars; the largest 226,922 chars), and nothing bounded
    them.  Action-file outlines grew 29% too.
  * S2: CREATE writes the creator's outline into each action file it saves
    from the model's recipe, and REUSE shows that file to every later user of
    the agent.  "<saved_str>" there tells a new person a value is saved where
    they have none (the parent showed None).

THE RULE
  * get_saved_metadata's reply is at most MEMORY_ITEM_MAX_CHARS, the bound
    MemoryGraph puts on every row the group chat writes back; a cut outline
    names the top-level keys and get_data_by_key, so no key goes unnamed;
  * an outline that fits is shown whole, saved text as "<saved_str>" (the
    88d7e3aac fix stays);
  * strip_json_values(..., keys_only=True) names the keys and puts None at
    every leaf, and CREATE's two action-file writers use it.

    python -m pytest tests/unit/test_a_memory_outline_is_no_longer_than_before_the_saved_marker.py -q --noconftest
"""
import ast
import json
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

import hartos.helper as helper
from core.agent_tools import build_core_tool_closures, person_store_id
from core.constants import MEMORY_ITEM_MAX_CHARS

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
USER, AGENT = '7', '54'
SESSION = f'{USER}_{AGENT}'


def _get_saved_metadata(own, shared=None):
    data = {AGENT: shared or {'receipt_template': 'Bill'},
            person_store_id(SESSION): own}
    ctx = {
        'building_agent': False, 'user_id': USER, 'prompt_id': AGENT,
        'agent_data': data, 'helper_fun': None, 'user_prompt': SESSION,
        'request_id_list': {}, 'recent_file_id': None, 'scheduler': None,
        'send_message_to_user1': lambda *a, **k: None,
        'retrieve_json': json.loads,
        'strip_json_values': helper.strip_json_values,
        'save_conversation_db': lambda *a, **k: None,
    }
    tools = {name: fn for name, _d, fn in build_core_tool_closures(ctx)}
    return tools['get_saved_metadata']()


def _big_store():
    """A store whose outline is far past the bound: many lessons, each a
    dict of texts, the shape of the desktop's largest store."""
    return {'lessons': {f'lesson_{i:04d}': {'title': 't', 'notes': 'n',
                                             'answer': 'a', 'score': 3}
                        for i in range(600)},
            'profile': {'name': 'Asha'}, 'streak': 4}


def test_a_large_outline_is_bounded():
    out = _get_saved_metadata(_big_store())
    assert len(str(helper.strip_json_values(_big_store()))) > MEMORY_ITEM_MAX_CHARS
    assert len(out) <= MEMORY_ITEM_MAX_CHARS, len(out)


def test_a_cut_outline_names_every_top_level_key_and_the_read():
    out = _get_saved_metadata(_big_store())
    tail = out[out.rindex('...[cut'):]
    for key in ('lessons', 'profile', 'streak', 'receipt_template'):
        assert key in tail, (key, tail)
    assert 'get_data_by_key' in tail, tail


def test_an_outline_that_fits_is_shown_whole_with_saved_text_marked():
    own = {'email_brief': {'recipient': 'a@b.c', 'body': 'Hi'}, 'step': 2}
    out = _get_saved_metadata(own)
    assert out == str(helper.strip_json_values(
        {'receipt_template': 'Bill', **own})), out
    assert "'body': '<saved_str>'" in out and '...[cut' not in out


def test_a_keys_only_outline_names_the_keys_and_claims_no_value():
    store = {'email_brief': {'recipient': 'a@b.c', 'body': 'Hi'}, 'step': 2,
             'draft': json.dumps({'to': 'x@y.z'}), 'tags': ['a', 3]}
    assert helper.strip_json_values(store, keys_only=True) == {
        'email_brief': {'recipient': None, 'body': None}, 'step': None,
        'draft': {'to': None}, 'tags': [None, None]}


def test_a_keys_only_outline_is_no_longer_than_the_old_outline():
    """The old outline put None at a text and 'redacted <type>' elsewhere;
    a keys-only outline puts None everywhere, so it is never longer."""
    store = _big_store()
    old = {k: v for k, v in store.items()}
    keys_only = str(helper.strip_json_values(store, keys_only=True))
    marked = str(helper.strip_json_values(old))
    assert len(keys_only) < len(marked)
    assert '<saved_' not in keys_only


def _action_file_outline_writers():
    """The values CREATE assigns to json_obj['metadata'] from the memory
    store: the two recipe saves (the model's recipe, and its late-save
    twin).  They sit inside the speaker selector, which cannot run without
    the pipeline, so this reads them (beside the behaviour tests above)."""
    tree = ast.parse(open(os.path.join(ROOT, 'hartos', 'create_recipe.py'),
                          encoding='utf-8').read())
    calls = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == 'metadata'
                and isinstance(node.value, ast.Call)
                and getattr(node.value.func, 'id', '') == 'strip_json_values'):
            continue
        calls.append(node)
    writers = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = list(ast.walk(fn))
        writes = any(isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Subscript)
                     and ast.unparse(n.targets[0]) == "json_obj['metadata']"
                     for n in body)
        if writes:
            for c in calls:
                if c in body:
                    writers[c.lineno] = c      # once, however deep it sits
    return [writers[k] for k in sorted(writers)]


def test_both_action_file_writers_save_a_keys_only_outline():
    writers = _action_file_outline_writers()
    assert len(writers) == 2, [ast.unparse(w) for w in writers]
    for w in writers:
        kw = {k.arg: ast.unparse(k.value) for k in w.value.keywords}
        assert kw.get('keys_only') == 'True', ast.unparse(w)
