"""Every post of a CREATE step attaches the tools its words name first, on the
real create loop.

5ad7c05f8 made CREATE attach a step's named tools before it posts the step
(create_recipe._attach_named_for_create_step).  rv_66reply's review rejected
it for two reasons this file closes:
  * a third post was missed: after a step finishes, the next step is built by
    get_execute_next_action_message and posted with no attach (the author's
    own run of 18 Nov posted step 3 there);
  * the wiring was pinned only by a source check of two call sites.

This drives the REAL loop (create_recipe.recipe -> get_response_group) with
the scripted agents of tests/unit/test_create_loop_end_to_end.py, no LLM.
The attach still runs; it is only recorded, with the group log's length at
the moment it ran.  Every "Execute Action N:" post in the log must be
preceded by an attach of step N's own text.

Two flows: the plain one, whose steps are banked from their own tool calls,
and one whose trace cannot be banked, so the loop asks the model for each
step's recipe and builds the next step with get_execute_next_action_message.

    python -m pytest tests/unit/test_every_create_step_post_attaches_its_tools_first.py -q -p no:cacheprovider
"""
import re

import pytest

import tests.unit.test_create_loop_end_to_end as e2e
from tests.unit.test_create_loop_end_to_end import create_env, _run, _content  # noqa: F401

_POST = re.compile(r'Execute Action (\d+):')


def _record(env, monkeypatch):
    cr = env.cr
    calls, builds = [], []
    real_attach = cr._attach_named_for_create_step
    real_next = cr.get_execute_next_action_message

    def _attach(agents_object, action_text, user_prompt):
        gc = env.script.gc
        calls.append((str(action_text), len(gc.messages) if gc is not None else 0))
        return real_attach(agents_object, action_text, user_prompt)

    def _next(prompt_id, user_prompt):
        out = real_next(prompt_id, user_prompt)
        builds.append(out[:60])
        return out
    monkeypatch.setattr(cr, '_attach_named_for_create_step', _attach)
    monkeypatch.setattr(cr, 'get_execute_next_action_message', _next)
    return calls, builds


def _posts(env):
    out = []
    for i, m in enumerate(env.script.gc.messages if env.script.gc else []):
        found = _POST.match(_content(m).strip())
        if found:
            out.append((i, int(found.group(1))))
    return out


def _unattached(env, calls):
    """The posts whose step text no attach call carried just before them."""
    bad = []
    for index, n in _posts(env):
        before = [text for text, length in calls if length <= index]
        if not before or before[-1] != e2e.ACTIONS[n - 1]:
            bad.append((index, n, before[-1][:40] if before else None))
    return bad


def test_every_step_post_of_the_plain_flow_attaches_first(create_env, monkeypatch):
    calls, _builds = _record(create_env, monkeypatch)
    _run(create_env)
    posts = _posts(create_env)
    assert sorted({n for _i, n in posts}) == [1, 2, 3], posts
    assert not _unattached(create_env, calls), (_unattached(create_env, calls), calls)


def test_a_step_built_after_the_last_one_finished_attaches_first(create_env, monkeypatch):
    """The trace cannot be banked, so each finished step's recipe is asked
    of the model and the loop builds the next step itself
    (get_execute_next_action_message): that post attaches first too."""
    monkeypatch.setattr(create_env.cr, '_bank_action_recipe_from_trace',
                        lambda *a, **k: False)
    calls, builds = _record(create_env, monkeypatch)
    _run(create_env, turns=6)
    assert builds, ('the loop never built a step with '
                    'get_execute_next_action_message; posts: '
                    f'{_posts(create_env)}; recipe requests: '
                    f'{create_env.script.recipe_requests}')
    assert not _unattached(create_env, calls), (_unattached(create_env, calls), calls)
