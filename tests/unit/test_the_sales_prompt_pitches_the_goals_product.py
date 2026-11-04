"""A sales goal's prompt pitches the goal's own product.

Read 2026-11-04 (task #233/#234): both sales prompt builders,
outreach_crm_tools.build_outreach_prompt ('outreach' goals) and
journey_engine._build_sales_prompt ('sales' goals), ignored the product the
daemon passes them (agent_daemon builds product_dict from the goal's
product_id) and pitched HARTOS-for-robotics whatever the goal sold.  So a
sales goal for a grocery, a ride service or a speech clinic would have
told its agent to find and cold-email robotics companies.  Two goal types
had two builders for one job, and the outreach one also listed every
prospect's email address in the prompt.

Now one builder serves both goal types; it pitches the goal's product when
the goal names one, and the HARTOS pitch, word for word, only when it names
none.

These tests build the prompts the way the daemon does
(GoalManager.build_prompt, with the builders goal_manager registers at
import) over a real prospect store in the test's own folder.
"""
import json

import pytest

GROCERY = {
    'id': 'p-grocery', 'name': 'McGroce',
    'description': 'Groceries from the corner shop to your door in 30 minutes',
    'tagline': 'your kirana, online', 'product_url': 'https://example.test/mcgroce',
    'category': 'ecommerce',
    'target_audience': 'households and small shops in Chennai',
    'unique_value_prop': 'local shops keep 95% of every sale',
    'keywords': ['grocery', 'delivery', 'kirana'], 'is_platform_product': False,
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The outreach prospect store, in this test's own folder."""
    from integrations.agent_engine import outreach_crm_tools
    monkeypatch.setattr(outreach_crm_tools, '_PROSPECT_FILE',
                        str(tmp_path / 'outreach_prospects.json'))
    return outreach_crm_tools


def _prompt(goal_type, product=None):
    from integrations.agent_engine.goal_manager import GoalManager
    return GoalManager.build_prompt(
        {'goal_type': goal_type, 'title': 'Find buyers', 'description': 'grow sales',
         'config': {}}, product)


@pytest.mark.parametrize('goal_type', ['sales', 'outreach'])
def test_a_sales_goal_for_a_product_pitches_that_product(store, goal_type):
    """FAILS BEFORE THE FIX: both prompts pitched HARTOS for robotics."""
    prompt = _prompt(goal_type, GROCERY)

    assert 'PRODUCT: McGroce' in prompt
    assert 'Groceries from the corner shop to your door in 30 minutes' in prompt
    assert ('Target the people and businesses McGroce is for: '
            'households and small shops in Chennai') in prompt
    assert 'what McGroce is' in prompt and '{company} + McGroce?' in prompt
    assert 'local shops keep 95% of every sale' in prompt
    for robotics in ('robotics companies', 'HARTOS (Hevolve Hive', 'on-device AI?',
                     'Why robotics companies care', 'Hive network value'):
        assert robotics not in prompt, robotics


def test_a_sales_goal_with_no_product_keeps_the_hartos_pitch(store):
    """Unchanged: no product named, the HARTOS-for-robotics pitch stays
    (passes before the fix too: it pins that nothing else moved)."""
    prompt = _prompt('sales')

    for line in ('PRODUCT: HARTOS (Hevolve Hive Agentic Runtime OS)',
                 'Why robotics companies care:',
                 '   - Target robotics companies across these verticals:',
                 '   - Look for pain points HARTOS solves (latency, offline ops, '
                 'multi-sensor fusion)',
                 '{company} + on-device AI?', 'what HARTOS is',
                 'HARTOS pitch > Hive network value',
                 '-- find new robotics companies'):
        assert line in prompt, line
    assert '%(' not in prompt


@pytest.mark.parametrize('goal_type', ['sales', 'outreach'])
def test_the_goals_own_words_reach_the_prompt(store, goal_type):
    """What the person who set the goal asked for reaches the agent.  The
    old 'outreach' builder showed the title and description (this pins
    that the fold keeps them); FAILS BEFORE THE FIX for 'sales', whose
    builder never showed them."""
    from integrations.agent_engine.goal_manager import GoalManager
    prompt = GoalManager.build_prompt(
        {'goal_type': goal_type, 'title': 'Sign up 20 kirana shops',
         'description': 'start with Mylapore and T. Nagar'}, GROCERY)

    assert 'Sign up 20 kirana shops' in prompt
    assert 'start with Mylapore and T. Nagar' in prompt


def test_a_goal_with_no_words_adds_no_goal_section(store):
    """No title and no description: no empty GOAL heading."""
    from integrations.agent_engine.journey_engine import _build_sales_prompt
    prompt = _build_sales_prompt({})

    assert 'YOUR GOAL' not in prompt
    assert 'Website: https://hevolve.ai\n\n' + '=' * 60 + '\nPRODUCT: HARTOS' in prompt


def test_one_builder_serves_both_goal_types(store):
    """'outreach' and 'sales' goals get the same prompt for the same
    product: one builder, not two that drift."""
    assert _prompt('outreach', GROCERY) == _prompt('sales', GROCERY)
    assert _prompt('outreach') == _prompt('sales')


def test_the_prompt_lists_no_prospect_email_address(store):
    """FAILS BEFORE THE FIX: the outreach prompt listed every prospect's
    email address."""
    store._save_prospects({'prospects': {'p1': {
        'id': 'p1', 'company': 'Shop', 'contact_name': 'Ana',
        'email': 'ana@shop.example', 'stage': 'new', 'emails_sent': 0}},
        'sequences': {}, 'sent_log': []})

    for goal_type in ('outreach', 'sales'):
        prompt = _prompt(goal_type, GROCERY)
        assert 'Shop' in prompt
        assert 'ana@shop.example' not in prompt, goal_type


def test_a_product_with_only_a_name_still_reads_whole(store):
    """A Product row with nothing but a name: no empty labels, no format
    error, and the targets fall back to the people who need it."""
    prompt = _prompt('sales', {'name': 'SpeakEasy', 'keywords': ['speech therapy']})

    assert 'PRODUCT: SpeakEasy' in prompt
    assert 'What it is:' not in prompt
    assert ('Target the people and businesses SpeakEasy is for: the people and '
            'businesses who need it (speech therapy)') in prompt
    assert '%(' not in prompt


def test_a_product_with_no_name_is_no_product(store):
    """A product dict without a name cannot be pitched: the default stays."""
    assert _prompt('sales', {'name': '  ', 'description': 'x'}) == _prompt('sales')
    assert _prompt('sales', json.loads('null')) == _prompt('sales')
