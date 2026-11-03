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
reply with the object the request names (who issues the secret, whether the
use is done on the person's account there, then YES or NO).  YES is asked
with the service and where to get it on the card; NO goes back to the agent
with no card; anything else is asked as before and logged.  The 17:53 card of
2026-11-03 (HIVENET below) is why the middle question exists.  The model is the boundary mocked here
(core.http_pool.pooled_post); the consent service, the database and the
vault are real, as in test_credential_ask.

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
# The reply shape the request demands (_ASK_CHECK_SCHEMA).
YES = json.dumps({'issued_by': 'OpenWeather', 'their_account_used': 'YES',
                  'answer': 'YES'})
NO = json.dumps({'issued_by': 'NONE', 'their_account_used': 'NO', 'answer': 'NO'})
TOLD = 'no person could act on it'


@pytest.fixture
def model(monkeypatch):
    """The node's own model, answering with ``state.reply`` (None: down)."""
    from core import autogen_config, http_pool
    state = SimpleNamespace(reply=YES, prompts=[], bodies=[])
    monkeypatch.setattr(autogen_config, 'llm_http_target',
                        lambda draft=False: ('http://model.invalid/v1/chat/completions',
                                             {}, 'test-model'))

    def post(url, headers=None, json=None, timeout=None, **_):
        state.prompts.append(json['messages'][-1]['content'])
        state.bodies.append(json)
        if state.reply is None:
            raise ConnectionError('model down')
        return SimpleNamespace(status_code=200, json=lambda: {
            'choices': [{'message': {'content': state.reply}}]})

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
    it: the answers are YES or NO, never free text to read a word out of."""
    _request_resource()(INVENTED)
    fmt = model.bodies[0]['response_format']
    schema = fmt['json_schema']['schema']
    assert fmt['type'] == 'json_schema'
    assert schema['properties']['answer']['enum'] == ['YES', 'NO']
    assert schema['properties']['their_account_used']['enum'] == ['YES', 'NO']
    assert set(schema['required']) == {'issued_by', 'their_account_used', 'answer'}


def test_a_real_company_named_for_this_computers_own_work_is_not_asked(world, model):
    """The 17:53 card: Hivenet is a real company, but monitoring idle nodes and
    onboarding models through this node's own /api/models/onboard is this
    computer's work, done on no account of the person's.  The model answers
    that the person's account there is not used, and no card goes out."""
    model.reply = json.dumps({'issued_by': 'Hivenet', 'their_account_used': 'NO',
                              'answer': 'NO'})
    out = _request_resource()(HIVENET)
    assert _cards(world) == [], 'the made-up Hivenet ask reached the owner as a card'
    assert _asks() == []
    assert 'Hivenet API key' in out and TOLD in out
    assert '/api/models/onboard' in model.prompts[0], (
        'the model was not shown what the secret is for')


def test_a_real_company_used_on_the_persons_account_is_asked(world, model):
    """The same company, for a step done on the person's account there: asked."""
    model.reply = json.dumps({'issued_by': 'Hivenet', 'their_account_used': 'YES',
                              'answer': 'YES'})
    _request_resource()(json.dumps(dict(
        json.loads(HIVENET), used_by='rent_compute',
        description='To rent extra compute on your Hivenet account for the '
                    'overnight batch.')))
    assert len(_cards(world)) == 1


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
    # A NO that names who issues it and says the person's account is used
    # contradicts itself.
    json.dumps({'issued_by': 'OpenWeather', 'their_account_used': 'YES',
                'answer': 'NO'}),
    # A YES that says the person's account is not used contradicts itself.
    json.dumps({'issued_by': 'OpenWeather', 'their_account_used': 'NO',
                'answer': 'YES'}),
    # A YES that names nobody who issues it contradicts itself.
    json.dumps({'issued_by': 'NONE', 'their_account_used': 'YES', 'answer': 'YES'}),
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
