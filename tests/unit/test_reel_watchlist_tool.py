"""reel_watchlist: the reel tool her agent changes her desktop's trending-song
watchlist with (owner ruling A17b): "track these accounts", "stop tracking
X", "which accounts do you track".

It is registered with the reel family, by register_reel_studio_tools on the
same seats (the Assistant and the group chat's Executor run it; nothing on
the central node, amendment A8 d), but no agent is shown its schema up
front: the owner names the accounts once in a while, and the family's
schema is held to 900 tokens per seat (plan WS-8b; measured 893 with the
seven).  request_tools, the never-say-unavailable escape, attaches it when
an agent asks for it, her Reel Studio agent included, in CREATE and REUSE.

The research Instagram account's username and password never pass through
it: it has no parameter for them, and once a watchlist exists the reply
says where the one credential consent card stands (daily_job.ask_for_account,
the path d73704a5e/5077691cc built), and that chat never takes them.

    python -m pytest tests/unit/test_reel_watchlist_tool.py -q -p no:cacheprovider
"""
import inspect
import json
import os

import pytest

from tests.unit.test_create_loop_end_to_end import create_env  # noqa: F401  (fixture)
from tests.unit.test_reel_tools_wired_both_pipelines import (  # noqa: F401  (fixture)
    HER_GOAL, PIPELINES, runs, schema, seats)
from tests.unit.test_trends_daily_job import (  # noqa: F401  (fixtures)
    FakePopen, NOW, OWNER, _asks, cards, job)

TOOL = 'reel_watchlist'


class _Agent:
    """Records what autogen's register_for_llm / register_for_execution get."""

    def __init__(self):
        self.llm = {}
        self.exe = {}

    def register_for_llm(self, *, name, description):
        def put(func):
            self.llm[name] = (description, func)
            return func
        return put

    def register_for_execution(self, *, name):
        def put(func):
            self.exe[name] = func
            return func
        return put


class _EmptyVault:
    """hartos.ai_key_vault's AIKeyVault as the job reads it, holding nothing
    the owner entered (the real vault on the card's path: ``cards``)."""

    def owner_credential_names(self):
        return set()

    def owner_credential(self, name, names=None):
        return ''


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    """Her desktop with no watchlist, an empty data dir, no research account
    and nobody named to ask (the card's real path is ``cards``')."""
    from tests.conftest import refuse_all_network
    refuse_all_network(monkeypatch)
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    from core import platform_paths
    platform_paths.reset_cache()
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    from integrations.reel_studio import jobs
    monkeypatch.setattr(jobs, '_SWEPT', True)
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
    monkeypatch.setattr('hartos.ai_key_vault.get_ai_key_vault', lambda: _EmptyVault())
    yield tmp_path
    platform_paths.reset_cache()


def _register(with_schema=True, executor=True):
    from integrations.reel_studio.tools import register_reel_studio_tools
    helper, assistant = _Agent(), _Agent()
    kw = {'executor': _Agent()} if executor else {}
    names = register_reel_studio_tools(helper, assistant, '10077', prompt_id='42',
                                       request_id_getter=lambda: None,
                                       with_schema=with_schema, **kw)
    return names, helper, assistant, kw.get('executor')


def _ok(reply):
    from core.constants import tool_reply_failed
    assert not tool_reply_failed(reply), reply
    return json.loads(reply)


# ── how it is registered ─────────────────────────────────────────────────

@pytest.mark.parametrize('with_schema', [True, False])
def test_it_rides_with_the_family_on_the_running_seats_and_no_agent_is_shown_it(
        desktop, with_schema):
    from integrations.reel_studio.tools import REEL_ON_DEMAND_TOOL_NAMES, REEL_TOOL_NAMES
    assert TOOL in REEL_ON_DEMAND_TOOL_NAMES and TOOL not in REEL_TOOL_NAMES
    names, helper, assistant, executor = _register(with_schema=with_schema)
    assert names == list(REEL_TOOL_NAMES) + list(REEL_ON_DEMAND_TOOL_NAMES)
    assert TOOL in assistant.exe and assistant.exe[TOOL] is executor.exe[TOOL]
    assert TOOL not in helper.llm and TOOL not in assistant.llm and TOOL not in helper.exe
    # request_tools matches on the docstring's first line, and attaches with it.
    first = (assistant.exe[TOOL].__doc__ or '').strip().split('\n')[0].lower()
    for word in ('track', 'instagram', 'accounts', 'trending'):
        assert word in first, first


def test_on_the_central_node_it_is_not_registered(desktop, monkeypatch):
    monkeypatch.setattr('security.key_delegation.get_node_tier', lambda: 'central')
    names, _helper, assistant, executor = _register()
    assert names == [] and TOOL not in assistant.exe and TOOL not in executor.exe


def test_it_never_takes_the_research_accounts_credentials(desktop):
    """No parameter carries a username or a password: the consent card is
    the only way in."""
    _names, _helper, assistant, _executor = _register()
    params = set(inspect.signature(assistant.exe[TOOL]).parameters)
    assert params == {'track', 'untrack', 'niche'}


# ── what it does ─────────────────────────────────────────────────────────

def test_her_agent_tracks_lists_and_stops_tracking_accounts(desktop):
    from integrations.trends import daily_job, watchlist
    run = _register()[2].exe[TOOL]
    body = _ok(run(track='@glam_by_meena, bridal.chennai', niche='bridal makeup'))
    assert body['tracked'] == ['glam_by_meena', 'bridal.chennai']
    assert body['added'] == ['glam_by_meena', 'bridal.chennai']
    assert body['niche'] == 'bridal_makeup'
    assert [a['handle'] for a in watchlist.read_watchlist()['accounts']] == body['tracked']
    assert daily_job.why_no_list() != 'no_watchlist'      # the job sees them
    listed = _ok(run())                                   # "which accounts do you track"
    assert listed['tracked'] == ['glam_by_meena', 'bridal.chennai'] and listed['added'] == []
    body = _ok(run(untrack='BRIDAL.CHENNAI'))
    assert body['removed'] == ['bridal.chennai'] and body['tracked'] == ['glam_by_meena']
    for reply in (listed, body):
        assert reply['message'] and 'trending' in reply


def test_a_handle_that_is_not_one_is_refused_and_said(desktop):
    from core.constants import tool_reply_failed
    run = _register()[2].exe[TOOL]
    reply = run(track='not a handle!')
    assert tool_reply_failed(reply) and 'handle!' in reply
    assert not os.path.exists(os.path.join(str(desktop), 'trends', 'watchlist.json'))
    body = _ok(run(track='mua_e, sp@ce!'))
    assert body['added'] == ['mua_e'] and [r['value'] for r in body['refused']] == ['sp@ce!']


def test_a_watchlist_the_desktop_cannot_change_is_said_in_the_failure_form(desktop):
    from core.constants import tool_reply_failed
    os.makedirs(os.path.join(str(desktop), 'trends', 'watchlist.json'))
    reply = _register()[2].exe[TOOL](track='mua_e')
    assert tool_reply_failed(reply) and TOOL in reply


def test_with_no_accounts_tracked_nothing_is_asked_for(job, cards):
    """Listing an empty watchlist files no credential card: a password for a
    job that cannot run is not asked for (5077691cc)."""
    from integrations.reel_studio import jobs
    cards.mp.setattr(jobs, '_SWEPT', True)
    os.remove(job.tmp / 'trends' / 'watchlist.json')
    body = _ok(_register()[2].exe[TOOL]())
    assert body['tracked'] == [] and body['trending']['reason'] == 'no_watchlist'
    assert _asks(cards) == []


def test_once_she_tracks_accounts_the_owner_is_asked_on_the_consent_card_only(job, cards):
    """The research account is asked for on the one consent card, for the
    owner of this computer, and the reply tells her agent never to take it
    in chat."""
    from integrations.reel_studio import jobs
    from integrations.trends import daily_job as dj
    cards.mp.setattr(jobs, '_SWEPT', True)
    os.remove(job.tmp / 'trends' / 'watchlist.json')
    body = _ok(_register()[2].exe[TOOL](track='mua_c'))
    assert body['tracked'] == ['mua_c']
    assert body['trending']['reason'] == 'account_asked'
    assert 'consent card' in body['trending']['note']
    assert sorted(a['scope'] for a, _ in _asks(cards)) == sorted(
        ['secret:' + dj.USERNAME_CREDENTIAL, 'secret:' + dj.PASSWORD_CREDENTIAL])
    for ask, _ in _asks(cards):
        assert ask['user_id'] == OWNER and ask['agent_id'] is None
    assert 'never in chat' in body['message']
    assert 'password' not in json.dumps(body['tracked'])
    assert FakePopen.made == []


# ── her agent reaches it, in both pipelines ──────────────────────────────

@pytest.fixture
def roomy(monkeypatch):
    """A window the attach fits into, so nothing it attaches is deferred."""
    from core import llm_outbound_logger as wire
    monkeypatch.setattr(wire, '_get_budget_per_slot', lambda: 1 << 17)


@pytest.mark.parametrize('pipeline', PIPELINES)
def test_her_reel_studio_agent_gets_it_when_she_asks_to_track_accounts(seats, roomy, pipeline):
    built = seats(pipeline, goal_tags=['reel_studio'], goal=HER_GOAL)
    assert TOOL not in schema(built.helper) | schema(built.assistant), (
        'the premise: no agent is shown it up front')
    assert TOOL in runs(built.assistant) and TOOL in runs(built.executor), pipeline
    request_tools = built.assistant.function_map['request_tools']
    with seats.env.app.app_context():
        reply = request_tools('track these instagram accounts for trending songs')
    assert TOOL in schema(built.helper), (
        f'{pipeline}: request_tools found no {TOOL} for "track these accounts"')
    assert TOOL in reply
