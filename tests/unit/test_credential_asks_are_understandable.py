"""An agent's credential ask reaches the owner only when a person can act on it.

Owner, 2026-11-02, typed to hartos-c9 (verbatim): "when a credential is needed
by the agent during agent creatyion time or when a agent is tasked with a task
reuiring any secret etc they will have to dynamically ask at runtime, only
thing that shd happen is it should not ask a madeupo credential as if
everyoine else understandss whjat it asks for".

Live that day (frozen_debug.log.old, 22:14:33 IST): a background goal's agent
(97339725145) called request_resource with key_name 'hive_network_config',
label 'Hive Network Configuration', used_by 'network_discovery'.  Nothing in
the code reads either name, and finding hive nodes needs no secret, yet the
owner got a consent card asking him to type one in.  Both agent tools
(core.agent_tools request_resource and Request_Resource in
hart_intelligence_entry) handed any ask to request_credential.

Now both go through ai_key_vault.request_credential_for_agent: a credential
this computer's code reads (reads_from_env) or the owner already stored goes
straight on; any other ask is judged by the node's own model, which must
reply with the two-field object the request names (who issues the secret,
then YES or NO).  YES is asked with the service and where to get it on the
card; NO goes back to the agent with no card; anything else is asked as
before and logged.  An ask that goes to the person is put a second question
(is its stated use done on the person's account at that company?).  A NO
there never refuses: the card says first that it sounds like this
computer's own work (ai_key_vault.ASK_DOUBT_NOTE), and the person decides.
The 17:53 card of 2026-11-03 (HIVENET below) is why the second question
exists.  Folding it into the first (6a1f2e46d) was REJECTED on review: the
desktop's 4B then refused 10 of 41 real asks a reviewer wrote, with no card.
The model is the boundary mocked here (core.http_pool.pooled_post); the
consent service, the database and the vault are real, as in
test_credential_ask.

    python -m pytest tests/unit/test_credential_asks_are_understandable.py -q
"""
import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

import pytest  # noqa: E402

# The real consent world (in-memory db, real vault, emitted cards captured):
# one copy, owned by the credential-ask tests.
from tests.unit.test_credential_ask import _asks, world  # noqa: E402,F401

INVENTED = json.dumps({
    'resource_type': 'credential', 'key_name': 'hive_network_config',
    'label': 'Hive Network Configuration', 'used_by': 'network_discovery',
    'description': 'Credential for accessing hive network configuration and '
                   'node information'})
WEATHER = json.dumps({
    'key_name': 'openweather_api_key', 'label': 'OpenWeather API key',
    'service': 'OpenWeather (openweathermap.org)', 'used_by': 'the forecast step',
    'description': "Used to fetch tomorrow's forecast for your city.",
    'where_to_get': 'Sign in at openweathermap.org, then open My API keys'})
# post_to_channel's ask, exactly as hartos-95's #218 builds it.
TELEGRAM = json.dumps({
    'resource_type': 'channel_secret', 'channel_type': 'telegram',
    'key_name': 'bot_token', 'label': 'Telegram Bot Token',
    'used_by': 'post_to_channel', 'description': 'Needed to post on Telegram.'})
# Live 2026-11-03 17:53:49 IST on the owner's desktop (frozen_debug.log.old,
# request daemon_0f4b1e5b-c75a-4386-adcd-d3c61663f9ea, agent 'Hive Infra
# Guardian'), verbatim: a real company named for this computer's own work.  The
# agent had googled "Hivenet API ..." five times with unrelated results, then
# asked; the check of the day asked only who issues the key, and the card
# reached the owner.
HIVENET = json.dumps({
    'resource_type': 'api_key', 'key_name': 'HIVENET_API_KEY',
    'label': 'Hivenet API key', 'service': 'Hivenet (hivenet.io)',
    'used_by': 'model provisioning agent',
    'description': 'Hivenet API key needed to monitor idle nodes and onboard '
                   'models via POST /api/models/onboard',
    'where_to_get': 'hivenet.io API documentation or developer portal'})
# A real ask from the review of 6a1f2e46d, word for word (its fresh_battery):
# the person's own PagerDuty pages them when this computer goes offline.
PAGERDUTY = json.dumps({
    'resource_type': 'api_key', 'key_name': 'PAGERDUTY_ROUTING_KEY',
    'label': 'PagerDuty integration key', 'service': 'PagerDuty',
    'used_by': 'outage alerts',
    'description': "To page you through PagerDuty when this computer's node "
                   "goes offline or a model crashes.",
    'where_to_get': 'PagerDuty, Services, Integrations, Events API v2'})
# The reply shapes the requests demand (_ASK_CHECK_SCHEMA, _ASK_USE_SCHEMA).
YES = json.dumps({'issued_by': 'OpenWeather', 'answer': 'YES'})
NO = json.dumps({'issued_by': 'NONE', 'answer': 'NO'})
USED = json.dumps({'their_account_used': 'YES'})
NOT_USED = json.dumps({'their_account_used': 'NO'})
TOLD = 'no outside company, website or app gives it out'


@pytest.fixture
def model(monkeypatch):
    """The node's own model.  The first question (credential_ask_check) gets
    ``state.reply``; the second (credential_ask_use) gets ``state.use_reply``.
    None: the model is down for that question.  A callable is handed the
    schema the request holds the model to and returns the reply, for a model
    that answers whatever fields it is asked."""
    from core import autogen_config, http_pool
    state = SimpleNamespace(reply=YES, use_reply=USED, prompts=[], bodies=[])
    monkeypatch.setattr(autogen_config, 'llm_http_target',
                        lambda draft=False: ('http://model.invalid/v1/chat/completions',
                                             {}, 'test-model'))

    def post(url, headers=None, json=None, timeout=None, **_):
        state.prompts.append(json['messages'][-1]['content'])
        state.bodies.append(json)
        name = json['response_format']['json_schema']['name']
        reply = state.use_reply if name == 'credential_ask_use' else state.reply
        if callable(reply):
            reply = reply(json['response_format']['json_schema']['schema'])
        if reply is None:
            raise ConnectionError('model down')
        return SimpleNamespace(status_code=200, json=lambda: {
            'choices': [{'message': {'content': reply}}]})

    monkeypatch.setattr(http_pool, 'pooled_post', post)
    return state


def _request_resource():
    from core import agent_tools
    ctx = {
        'user_id': 'someone-remote', 'prompt_id': '42', 'agent_data': {},
        'helper_fun': MagicMock(), 'user_prompt': 's-1', 'request_id_list': [],
        'recent_file_id': {}, 'scheduler': MagicMock(),
        'log_tool_execution': lambda f: f,
        'send_message_to_user1': MagicMock(), 'retrieve_json': lambda v: v,
        'strip_json_values': lambda v: v, 'save_conversation_db': MagicMock(),
    }
    return {n: f for n, _d, f in agent_tools.build_core_tool_closures(ctx)}['request_resource']


def _cards(emitted):
    return [d for topic, d in emitted if topic == 'consent.request']


def test_a_made_up_credential_never_reaches_the_owner(world, model):
    model.reply = NO
    out = _request_resource()(INVENTED)
    assert _cards(world) == [], 'the made-up ask reached the owner as a card'
    assert _asks() == []
    assert 'Hive Network Configuration' in out and TOLD in out, (
        'the agent is not told why it was not asked')
    assert len(model.prompts) == 1 and 'network_discovery' in model.prompts[0]


def test_the_model_is_held_to_two_words(world, model):
    """The request names the reply's shape, so the server holds the model to
    it: each answer is YES or NO, never free text to read a word out of."""
    _request_resource()(WEATHER)
    check, use = (b['response_format'] for b in model.bodies)
    assert check['type'] == use['type'] == 'json_schema'
    schema = check['json_schema']['schema']
    assert schema['properties']['answer']['enum'] == ['YES', 'NO']
    assert set(schema['required']) == {'issued_by', 'answer'}
    use_schema = use['json_schema']['schema']
    assert use_schema['properties']['their_account_used']['enum'] == ['YES', 'NO']
    assert use_schema['required'] == ['their_account_used']


def test_a_real_company_named_for_this_computers_own_work_is_asked_with_a_doubt(world, model):
    """The 17:53 card: Hivenet is a real company, so the first question lets it
    through; the second says monitoring idle nodes and onboarding models
    through this node's own /api/models/onboard is not done on the person's
    account there.  The card goes out with the doubt right after the line
    that names the key, ahead of the agent's own words, so the person reads
    it before deciding; it is never refused on that alone (review of
    6a1f2e46d: the 4B says NO there for real asks too, e.g. metrics sent to
    the person's Datadog)."""
    from hartos.ai_key_vault import ASK_DOUBT_NOTE
    model.reply = json.dumps({'issued_by': 'Hivenet', 'answer': 'YES'})
    model.use_reply = NOT_USED
    out = _request_resource()(HIVENET)
    cards = _cards(world)
    assert len(cards) == 1, 'the ask was refused on the doubt alone'
    ask = json.loads(HIVENET)
    assert cards[0]['reason'].startswith(
        f"{ask['label']} is needed for {ask['used_by']}. {ASK_DOUBT_NOTE}"), (
        'the card does not say, right after naming the key, that this sounds '
        'like the computer\'s own work')
    assert 'monitor idle nodes' in cards[0]['reason']
    assert '{{secret:HIVENET_API_KEY}}' in out
    assert '/api/models/onboard' in model.prompts[1], (
        'the second question was not shown what the secret is for')


def test_a_real_ask_the_model_doubts_still_reaches_the_person(world, model):
    """The regression that REJECTED 6a1f2e46d.  Asked in one question whether
    a company issues the key AND the use is done on the person's account
    there, the desktop's Qwen3.5-4B answered this ask with issued_by
    PagerDuty, their_account_used NO, answer NO (reviewer's runs, 2026-11-03,
    twice), and it was refused with no card.  This model answers every
    question the way it answered that one: PagerDuty issues it, and NO, the
    use is not on the person's account.  Only "nobody issues it" refuses; a
    doubt about the use goes on the card and the person decides."""
    def desktop_4b(schema):
        asked = set(schema['properties'])
        reply = {'issued_by': 'PagerDuty', 'their_account_used': 'NO'}
        if 'answer' in asked:
            # Folded into one question the use decides the answer (measured);
            # the issuer question's answer is YES for a named company.
            reply['answer'] = 'NO' if 'their_account_used' in asked else 'YES'
        return json.dumps({k: v for k, v in reply.items() if k in asked})
    model.reply = model.use_reply = desktop_4b
    _request_resource()(PAGERDUTY)
    cards = _cards(world)
    assert len(cards) == 1, 'a real ask was refused with no card'
    from hartos.ai_key_vault import ASK_DOUBT_NOTE
    assert ASK_DOUBT_NOTE in cards[0]['reason']
    assert 'Events API v2' in cards[0]['reason']


def test_a_doubt_reaches_the_card_when_the_agent_names_no_service(world, model):
    """Nothing tells the agent it must give service or where_to_get, and the
    17:53 kind of ask is the one most likely to leave them out: the doubt
    still goes on the card."""
    from hartos.ai_key_vault import ASK_DOUBT_NOTE
    bare = {k: v for k, v in json.loads(HIVENET).items()
            if k not in ('service', 'where_to_get')}
    model.reply = json.dumps({'issued_by': 'Hivenet', 'answer': 'YES'})
    model.use_reply = NOT_USED
    _request_resource()(json.dumps(bare))
    cards = _cards(world)
    assert len(cards) == 1
    assert ASK_DOUBT_NOTE in cards[0]['reason']


def test_a_real_company_used_on_the_persons_account_is_asked_plainly(world, model):
    """The same company, for a step done on the person's account there: a
    plain card, no doubt on it."""
    from hartos.ai_key_vault import ASK_DOUBT_NOTE
    model.reply = json.dumps({'issued_by': 'Hivenet', 'answer': 'YES'})
    model.use_reply = USED
    _request_resource()(json.dumps(dict(
        json.loads(HIVENET), used_by='rent_compute',
        description='To rent extra compute on your Hivenet account for the '
                    'overnight batch.')))
    cards = _cards(world)
    assert len(cards) == 1
    assert ASK_DOUBT_NOTE not in cards[0]['reason']


@pytest.mark.parametrize('use_reply', [
    None,                                           # the model is down
    'NO, this is the computer\'s own work',         # not the asked-for object
    json.dumps({'their_account_used': 'MAYBE'}),
])
def test_a_second_answer_that_is_not_a_clear_no_adds_no_doubt(world, model, use_reply):
    """Not knowing adds nothing: the card goes out as it would have."""
    from hartos.ai_key_vault import ASK_DOUBT_NOTE
    model.use_reply = use_reply
    _request_resource()(WEATHER)
    cards = _cards(world)
    assert len(cards) == 1
    assert ASK_DOUBT_NOTE not in cards[0]['reason']


def test_a_refused_ask_is_not_put_the_second_question(world, model):
    model.reply = NO
    _request_resource()(INVENTED)
    assert len(model.bodies) == 1


def test_an_ask_a_person_can_act_on_reaches_them_with_where_to_get_it(world, model):
    model.reply = YES
    out = _request_resource()(WEATHER)
    cards = _cards(world)
    assert len(cards) == 1
    assert 'OpenWeather (openweathermap.org)' in cards[0]['reason']
    assert 'Sign in at openweathermap.org, then open My API keys' in cards[0]['reason'], (
        'the card does not say where to get the value')
    assert '{{secret:OPENWEATHER_API_KEY}}' in out


def test_a_credential_the_code_reads_is_asked_without_the_model(world, model):
    """post_to_channel's bot token: TELEGRAM_BOT_TOKEN is a channel key this
    computer reads, so no model call stands between it and the card."""
    _request_resource()(TELEGRAM)
    assert model.prompts == []
    assert [a[1] for a in _asks()] == ['secret:TELEGRAM_BOT_TOKEN']
    assert len(_cards(world)) == 1


def test_a_stored_credential_is_answered_without_the_model(world, model):
    from hartos.ai_key_vault import get_ai_key_vault
    get_ai_key_vault().store_credential('openweather_api_key', 'k-123')
    out = _request_resource()(WEATHER)
    assert model.prompts == []
    assert '{{secret:OPENWEATHER_API_KEY}}' in out and 'k-123' not in out
    assert _cards(world) == []


@pytest.mark.parametrize('reply', [
    # What the desktop's model wrote on 2026-11-03 when asked for free text.
    "NO, because this request asks for the computer's own network configuration",
    # A NO that names who issues it contradicts itself.
    json.dumps({'issued_by': 'OpenWeather', 'answer': 'NO'}),
    json.dumps(['NO']),
    # A reply whose content is a list of parts, not text (review of 133ab4a07,
    # M2: it raised TypeError out of the tool).
    [{'type': 'text', 'text': NO}],
])
def test_a_reply_that_is_not_a_clear_no_is_not_a_refusal(world, model, caplog, reply):
    """Refusing a real ask breaks the agent; asking a made-up one is what
    happened before.  So only the asked-for object saying NONE and NO refuses;
    anything else is no answer, asked as before and logged."""
    model.reply = reply
    with caplog.at_level(logging.WARNING):
        _request_resource()(WEATHER)
    assert len(_cards(world)) == 1
    assert any('could not be checked' in r.getMessage() for r in caplog.records)


def test_with_no_model_the_ask_goes_on_and_says_so(world, model, caplog):
    model.reply = None
    with caplog.at_level(logging.WARNING):
        _request_resource()(WEATHER)
    assert len(_cards(world)) == 1
    assert any('could not be checked' in r.getMessage() for r in caplog.records)


def test_the_langchain_request_resource_tool_takes_the_same_road(world, model, monkeypatch):
    import hart_intelligence_entry as hie
    monkeypatch.setattr(hie.thread_local_data, 'get_prompt_id', lambda: '42', raising=False)
    model.reply = NO
    out = hie._handle_request_resource(INVENTED)
    assert _cards(world) == []
    assert TOLD in out


def test_the_description_fits_the_chat_agent_prompt():
    """hart_intelligence_entry hands this description to LangChain's
    Request_Resource tool, and the chat agent's prompt is an f-string template
    that lists each tool as '> name: description'.  A single brace in the
    description becomes a template slot, and then every chat turn with tools
    fails before the model runs.  Live on 2026-11-03 06:58 IST, request
    msg_1793669313883_upp4xc: "Missing some input keys: {'"key_name"'}", and
    the person was told their local AI was busy.  The check is LangChain's
    own (the formatter its prompt templates validate with)."""
    from langchain_core.utils.formatting import formatter
    from hartos.ai_key_vault import REQUEST_RESOURCE_DESCRIPTION
    human = ('TOOLS\n> Request_Resource: ' + REQUEST_RESOURCE_DESCRIPTION
             + '\nUSER INPUT: {input}')
    formatter.validate_input_variables(human, ['input'])
    # resource_type too: request_credential files the ask under it (or
    # 'api_key' when it is missing).
    for field in ('resource_type', 'key_name', 'service', 'where_to_get'):
        assert field in REQUEST_RESOURCE_DESCRIPTION, (
            f'the description no longer tells the agent to give {field}')


def test_both_tools_describe_the_ask_the_same_way():
    """The description the model reads says what may be asked; one text."""
    from hartos.ai_key_vault import REQUEST_RESOURCE_DESCRIPTION
    from core import agent_tools
    ctx = {'user_id': 'u', 'prompt_id': '42', 'agent_data': {}, 'helper_fun': MagicMock(),
           'user_prompt': 's-1', 'request_id_list': [], 'recent_file_id': {},
           'scheduler': MagicMock(), 'log_tool_execution': lambda f: f,
           'send_message_to_user1': MagicMock(), 'retrieve_json': lambda v: v,
           'strip_json_values': lambda v: v, 'save_conversation_db': MagicMock()}
    described = {n: d for n, d, _f in agent_tools.build_core_tool_closures(ctx)}
    assert described['request_resource'] == REQUEST_RESOURCE_DESCRIPTION
    assert 'configuration value' not in REQUEST_RESOURCE_DESCRIPTION
