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


def test_no_product_row_keeps_the_default(store):
    """No row (None) or an empty one: the default pitch, as before."""
    assert _prompt('sales', json.loads('null')) == _prompt('sales')
    assert _prompt('sales', {}) == _prompt('sales')


# ── review of d2ad38b22 ─────────────────────────────────────────────────
#
# The flag that makes a product HevolveAI's own (its sales prompt is signed
# by HevolveAI's founder) was writable by any signed-in person on POST and
# PUT; a row with a blank name was marketed as the HART platform; and the
# marketing block's two defaults moved from 'key missing' to 'value blank'.
# These drive the real product routes (agent_engine_bp) over SQLite, then
# build the prompts from the row the route stored.

REMOTE = {'REMOTE_ADDR': '203.0.113.7'}
TOKEN = {'Authorization': 'Bearer t'}


@pytest.fixture
def api(monkeypatch):
    """The product routes on a fresh SQLite database; ``api.as_(user)``
    signs the next requests in as that user."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from flask import Flask
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from integrations.agent_engine.api import agent_engine_bp
    from integrations.social.models import Base

    for var in ('TRUSTED_PROXY', 'NUNBA_CI', 'HEVOLVE_TRUST_KONG',
                'HEVOLVE_CLOUD_MODE'):
        monkeypatch.delenv(var, raising=False)
    eng = create_engine('sqlite://', connect_args={'check_same_thread': False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    maker = sessionmaker(bind=eng)
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(agent_engine_bp)
    client = app.test_client()

    def as_(uid, role='flat', is_admin=False):
        user = SimpleNamespace(id=uid, role=role, is_admin=is_admin,
                               is_banned=False)
        return patch('integrations.social.auth._get_user_from_token',
                     side_effect=lambda token: (user, maker()))

    def row(product_id):
        from integrations.social.models import Product
        db = maker()
        try:
            return db.query(Product).filter_by(id=product_id).first().to_dict()
        finally:
            db.close()

    with patch('integrations.social.models.get_db', side_effect=lambda: maker()):
        yield SimpleNamespace(client=client, as_=as_, row=row, maker=maker)


def _post(api, body):
    return api.client.post('/api/marketing/products', json=body,
                           headers=TOKEN, environ_base=REMOTE)


def _put(api, product_id, body):
    return api.client.put(f'/api/marketing/products/{product_id}', json=body,
                          headers=TOKEN, environ_base=REMOTE)


def test_a_person_cannot_make_their_product_hevolveais(store, api):
    """FAILS BEFORE THE FIX: a plain signed-in person's POST stored
    is_platform_product True, and the sales prompt for the row signed it
    as HevolveAI's founder."""
    with api.as_('u-1'):
        r = _post(api, dict(GROCERY, is_platform_product=True))
    assert r.status_code == 201, r.get_json()
    stored = api.row(r.get_json()['product']['id'])

    assert stored['is_platform_product'] is False
    prompt = _prompt('sales', stored)
    assert 'Creator: Sathish' not in prompt and 'founder' not in prompt
    assert '- Sign as: the McGroce team / https://example.test/mcgroce' in prompt


def test_a_person_cannot_flag_or_give_away_their_product_by_put(store, api):
    """FAILS BEFORE THE FIX: PUT wrote every key of the body onto the row
    (setattr for any column): the flag, and the owner too."""
    with api.as_('u-1'):
        pid = _post(api, GROCERY).get_json()['product']['id']
        r = _put(api, pid, {'is_platform_product': True, 'owner_id': 'u-2',
                            'tagline': 'fresh every morning'})
    assert r.status_code == 200, r.get_json()
    stored = api.row(pid)

    assert stored['is_platform_product'] is False
    assert stored['owner_id'] == 'u-1'
    assert stored['tagline'] == 'fresh every morning'


def test_the_steward_marks_a_product_as_hevolveais(store, api):
    """The steward (holds_central_role) still sets the flag, on POST and
    on PUT, and HevolveAI then signs the prompt."""
    with api.as_('steward', role='central'):
        made = _post(api, dict(GROCERY, is_platform_product=True))
        pid = made.get_json()['product']['id']
        plain = _post(api, GROCERY).get_json()['product']['id']
        _put(api, plain, {'is_platform_product': True})

    assert api.row(pid)['is_platform_product'] is True
    assert api.row(plain)['is_platform_product'] is True
    assert 'Creator: Sathish, founder of HevolveAI' in _prompt('sales', api.row(pid))


@pytest.mark.parametrize('name', ['', '   ', None])
def test_a_blank_name_is_refused(store, api, name):
    """FAILS BEFORE THE FIX for '   ' on POST and for any blank name on PUT:
    a row with a blank name was stored."""
    from integrations.social.models import Product
    with api.as_('u-1'):
        posted = _post(api, dict(GROCERY, name=name))
        pid = _post(api, GROCERY).get_json()['product']['id']
        put = _put(api, pid, {'name': name})

    assert posted.status_code == 400
    assert put.status_code == 400
    assert api.row(pid)['name'] == 'McGroce'
    db = api.maker()
    try:
        assert db.query(Product).count() == 1
    finally:
        db.close()


def test_a_row_with_no_audience_shows_none_in_marketing(store, api):
    """The API stores '' for an audience it was not given, and the
    category it was sent.  FAILS BEFORE THE FIX: marketing showed
    'everyone who wants a better life' and 'platform' for them.  A row
    with no such key at all keeps both defaults, as before."""
    with api.as_('u-1'):
        pid = _post(api, {'name': 'SpeakEasy', 'category': ''}).get_json()['product']['id']
    stored = _prompt('marketing', api.row(pid))
    keyless = _prompt('marketing', {'name': 'SpeakEasy'})

    assert '  Target audience: \n' in stored
    assert '  Category: \n' in stored
    assert '  Target audience: everyone who wants a better life\n' in keyless
    assert '  Category: platform\n' in keyless


def test_an_older_row_with_a_blank_name_is_still_its_owners_product(store):
    """A row stored before the API refused blank names.  FAILS BEFORE THE
    FIX: marketing promoted the HART platform and sales pitched HARTOS for
    robotics in its place."""
    row = dict(GROCERY, name='  ')
    marketing = _prompt('marketing', row)
    sales = _prompt('sales', row)

    assert 'PRODUCT YOU ARE MARKETING:\n  Name: \n' in marketing
    assert 'marketing the HART platform itself' not in marketing
    assert 'You are the sales agent for this product.' in sales
    assert 'Groceries from the corner shop to your door in 30 minutes' in sales
    assert ('- Sign as: the team behind this product / '
            'https://example.test/mcgroce') in sales
    for wrong in NOT_FOR_A_PRODUCT:
        assert wrong not in sales, wrong
