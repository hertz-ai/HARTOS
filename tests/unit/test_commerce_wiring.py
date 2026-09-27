"""How the McGroce commerce tools reach an agent.

Behavioural: the 'commerce' goal tag (detect_goal_tags) fires on McGroce work
and nowhere else in the in-repo goal corpus; the mcgroce_shopper /
mcgroce_merchant goal types exist with their tool tags and their prompts
name only tools the registrar really provides.

Source guards (clearly labelled): create_recipe / reuse_recipe run the Tier-2
dispatch inside multi-thousand-line agent builders that cannot be driven in a
unit test, so an AST check pins that each leg calls register_commerce_tools
under ``if 'commerce' in goal_tags``.  The registrar itself is covered
behaviourally in test_commerce_tools.py.
"""
import ast
import os
import re

import pytest

from integrations.agent_engine import goal_manager as gm
from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
from integrations.agent_engine.marketing_tools import detect_goal_tags
from integrations.commerce.commerce_tools import COMMERCE_TOOLS

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _corpus():
    docs = {}
    for s in SEED_BOOTSTRAP_GOALS:
        docs['seed:' + s.get('slug', '')] = ' '.join(
            str(s.get(k, '')) for k in ('title', 'description'))
    for t in gm.get_registered_types():
        try:
            docs['type:' + t] = str(gm.get_prompt_builder(t)(
                {'title': t, 'description': '', 'goal_type': t, 'config': {}}) or '')
        except Exception:
            continue
    return docs


class TestCommerceTagPrecision:
    def test_fires_only_on_mcgroce_work_across_the_goal_corpus(self):
        hits = {n for n, text in _corpus().items()
                if 'commerce' in detect_goal_tags(text)}
        assert hits == {'seed:bootstrap_p2p_grocery', 'type:p2p_grocery',
                        'type:mcgroce_shopper', 'type:mcgroce_merchant'}

    @pytest.mark.parametrize('text', [
        'git checkout main and rebase', 'checkout the release branch',
        'send an order confirmation email', 'plan a rideshare pool',
    ])
    def test_does_not_fire_on_lookalikes(self, text):
        assert 'commerce' not in detect_goal_tags(text)

    @pytest.mark.parametrize('text', [
        '[mcgroce_ctx]{"page":"home"}\nadd 2 milk',
        'please add to cart the paneer', 'what is my order status',
        'help me onboard merchant stores', 'create a new sku for ghee',
        'place a grocery order', 'proceed to checkout',
    ])
    def test_fires_on_commerce_requests(self, text):
        assert 'commerce' in detect_goal_tags(text)


class TestGoalTypes:
    def test_types_and_tool_tags(self):
        assert gm.get_tool_tags('mcgroce_shopper') == ['commerce']
        assert gm.get_tool_tags('mcgroce_merchant') == ['commerce', 'marketing']
        assert 'commerce' in gm.get_tool_tags('p2p_grocery')

    def test_merchant_prompt_attaches_marketing_but_not_outreach(self):
        text = gm.get_prompt_builder('mcgroce_merchant')({})
        tags = detect_goal_tags(text)
        assert {'commerce', 'marketing'} <= set(tags)
        assert 'outreach' not in tags

    @pytest.mark.parametrize('goal_type', ['mcgroce_shopper', 'mcgroce_merchant',
                                           'p2p_grocery'])
    def test_prompts_name_only_real_commerce_tools(self, goal_type):
        text = gm.get_prompt_builder(goal_type)({'config': {}})
        named = set(re.findall(r'\bcommerce_[a-z_]+', text))
        assert named, goal_type
        assert named <= {t['name'] for t in COMMERCE_TOOLS}

    def test_grocery_full_search_points_at_the_live_endpoint(self):
        text = gm.get_prompt_builder('p2p_grocery')({'config': {}})
        assert '/catalog/search?q=' in text
        assert '/search/{query} — full search' not in text


def _commerce_branch_calls(path):
    tree = ast.parse(open(os.path.join(REPO, path), encoding='utf-8').read())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        t = node.test
        if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Constant)
                and t.left.value == 'commerce'
                and isinstance(t.ops[0], ast.In)
                and isinstance(t.comparators[0], ast.Name)
                and t.comparators[0].id == 'goal_tags'):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                        and sub.func.id == 'register_commerce_tools'):
                    found.append(sub)
    return found


@pytest.mark.parametrize('leg', ['hartos/create_recipe.py', 'hartos/reuse_recipe.py'])
def test_source_guard_tier2_dispatches_commerce(leg):
    calls = _commerce_branch_calls(leg)
    assert len(calls) == 1, f'{leg}: expected one gated register_commerce_tools call'
    args = [a.id for a in calls[0].args if isinstance(a, ast.Name)]
    assert args == ['helper', 'assistant', 'user_id']
