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


#: Every line of the HARTOS-for-robotics pitch that is wrong for another
#: product: its robotics framing, its one-slot-per-vertical scarcity and Q2
#: deadline, its example subject, and HevolveAI's founder as the seller
#: (review of 44f21b312 found each of these in every product prompt).
NOT_FOR_A_PRODUCT = (
    'robotics', 'Robotics', 'HARTOS (Hevolve Hive', 'on-device AI?',
    'Hive network value', 'one slot per vertical', 'Q2',
    'quick question about digit', 'HARTOS Sales & Marketing Agent',
    'HARTOS solves', 'companies in underrepresented verticals', 'info@domain',
    'Creator: Sathish', 'founder', 'Replies go to', 'https://hevolve.ai',
)


@pytest.mark.parametrize('goal_type', ['sales', 'outreach'])
def test_a_sales_goal_for_a_product_pitches_that_product(store, goal_type):
    """FAILS BEFORE THE FIX: both prompts pitched HARTOS for robotics."""
    prompt = _prompt(goal_type, GROCERY)

    assert 'You are the sales agent for McGroce.' in prompt
    assert 'PRODUCT: McGroce' in prompt
    assert 'Groceries from the corner shop to your door in 30 minutes' in prompt
    assert ('Target the people and businesses McGroce is for: '
            'households and small shops in Chennai') in prompt
    assert 'what McGroce is' in prompt and '{company} + McGroce?' in prompt
    assert ('Look for pain points McGroce solves '
            '(local shops keep 95% of every sale)') in prompt
    assert 'quick question about grocery' in prompt
    assert '- Sign as: the McGroce team / https://example.test/mcgroce' in prompt
    for wrong in NOT_FOR_A_PRODUCT:
        assert wrong not in prompt, wrong


def test_a_hevolveai_product_keeps_hevolveai_as_the_seller(store):
    """HevolveAI's own product (is_platform_product): HevolveAI still sells
    and signs, but nothing of the robotics pitch stays."""
    prompt = _prompt('sales', dict(GROCERY, is_platform_product=True))

    assert 'Creator: Sathish, founder of HevolveAI' in prompt
    assert '- Sign as: sathish / founder, HevolveAI / hevolve.ai' in prompt
    for wrong in ('robotics', 'Robotics', 'one slot per vertical', 'Q2',
                  'quick question about digit', 'HARTOS Sales & Marketing Agent'):
        assert wrong not in prompt, wrong


def test_the_default_prompt_is_the_old_one_word_for_word(store):
    """No product and no goal words: the prompt is byte for byte the one
    before #233 (sha256 of ff45f77a4's, 8320 characters), so a cut or
    changed line of the default pitch fails here."""
    import hashlib
    from integrations.agent_engine.journey_engine import _build_sales_prompt
    prompt = _build_sales_prompt({})

    assert len(prompt) == 8320
    assert hashlib.sha256(prompt.encode('utf-8')).hexdigest() == (
        'd62beafea85ea766147def96c7c6b0bbe7e6beed3abc1a2059eade930064864f')


@pytest.mark.parametrize('keywords, shown', [
    (7, None),
    ('grocery, kirana', 'grocery, kirana'),
    ({'grocery': 1}, None),
    (None, None),
    ([1, None, '  ', True, 'kirana'], '1, kirana'),
])
@pytest.mark.parametrize('goal_type', ['sales', 'marketing'])
def test_a_product_row_of_any_shape_never_breaks_the_prompt(store, goal_type,
                                                           keywords, shown):
    """A Product row is a person's own text and its keywords are not
    type-checked on write.  FAILS BEFORE THE FIX: keywords 7 raised
    TypeError in both builders and a text came out letter by letter."""
    prompt = _prompt(goal_type, dict(GROCERY, keywords=keywords))

    assert 'McGroce' in prompt
    shown_keywords = [ln.split('Keywords:', 1)[1].strip()
                      for ln in prompt.splitlines() if 'Keywords:' in ln]
    assert [k for k in shown_keywords if k] == ([shown] if shown else [])


def test_a_long_product_field_is_capped(store):
    """Product fields are capped like a goal's description: a 5000-letter
    description shows its first 1000 letters."""
    prompt = _prompt('sales', dict(GROCERY, description='d' * 5000))

    assert 'd' * 1000 in prompt and 'd' * 1001 not in prompt


def test_the_marketing_prompt_shows_a_whole_row_as_before(store):
    """The marketing builder reads the row through the same reader
    (goal_manager.product_fields) and shows a well-formed row as before."""
    prompt = _prompt('marketing', GROCERY)

    assert ('PRODUCT YOU ARE MARKETING:\n'
            '  Name: McGroce\n'
            '  Description: Groceries from the corner shop to your door in 30 minutes\n'
            '  Tagline: your kirana, online\n'
            '  Target audience: households and small shops in Chennai\n'
            '  Unique value: local shops keep 95% of every sale\n'
            '  Product URL: https://example.test/mcgroce\n'
            '  Keywords: grocery, delivery, kirana\n'
            '  Category: ecommerce\n') in prompt


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
