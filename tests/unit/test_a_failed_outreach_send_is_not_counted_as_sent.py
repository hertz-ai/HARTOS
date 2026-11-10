"""An outreach email that did not go out is not counted as sent.

Read 2026-11-04 (task 234): send_outreach_email answered success true
whatever the sender said, and moved the prospect to 'contacted' with one
more email sent; a follow-up step was marked 'sent' when its email failed;
the journey's first email counted as sent whenever the result named a
sender ('via' is set on failures too); and a meeting was marked proposed
when that email failed.  So a sales agent with no working email channel
reported a pipeline of contacted prospects who never received anything,
and their follow-ups were used up.

These tests run the real outreach tools, the real follow-up checker, the
real journey engine and the real _send_email over the real channel
registry and FlaskChannelIntegration on a running loop.  The email channel
is a ChannelAdapter subclass standing in for the mail server; the
prospect store is a file in the test's own folder.
"""
import asyncio
import json
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from integrations.channels.base import (
    ChannelAdapter, ChannelConfig, ChannelStatus, SendResult,
)


class _EmailChannel(ChannelAdapter):
    """The email channel: answers each email with the result it is given."""

    def __init__(self, ok=True, error=None):
        super().__init__(ChannelConfig())
        self._running = True
        self.status = ChannelStatus.CONNECTED
        self.ok, self.error = ok, error
        self.emails = []

    @property
    def name(self):
        return 'email'

    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send_message(self, chat_id, text, reply_to=None, media=None, buttons=None):
        return SendResult(success=self.ok, message_id='m1', error=self.error)

    async def edit_message(self, chat_id, message_id, text, buttons=None):
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id, message_id):
        return True

    async def send_typing(self, chat_id):
        return None

    async def get_chat_info(self, chat_id):
        return None

    async def send_email(self, to, subject, body, cc=None, bcc=None,
                         attachments=None, html_body=None):
        self.emails.append((to, subject))
        return SendResult(success=self.ok, message_id='e1', error=self.error)


@pytest.fixture
def channels(monkeypatch):
    """A running channel integration over a fresh registry, installed as
    the process's own (get_registry / get_channel_integration)."""
    from integrations.channels import flask_integration
    from integrations.channels import registry as registry_mod
    from integrations.channels.registry import ChannelRegistry

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    registry = ChannelRegistry()
    integration = flask_integration.FlaskChannelIntegration.__new__(
        flask_integration.FlaskChannelIntegration)
    integration.registry = registry
    integration._loop = loop
    integration._thread = None
    monkeypatch.setattr(registry_mod, '_registry', registry)
    monkeypatch.setattr(flask_integration, '_integration', integration)
    yield SimpleNamespace(registry=registry, loop=loop)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The outreach prospect store, in this test's own folder."""
    from integrations.agent_engine import outreach_crm_tools
    monkeypatch.setattr(outreach_crm_tools, '_PROSPECT_FILE',
                        str(tmp_path / 'outreach_prospects.json'))
    return outreach_crm_tools


@pytest.fixture
def no_mailer(monkeypatch):
    """The mailer fallback must not be reached when the email channel
    answered; record it if it is."""
    from core import http_pool
    calls = []
    monkeypatch.setattr(http_pool, 'pooled_post',
                        lambda *a, **k: calls.append((a, k)))
    return calls


def _tools(store, monkeypatch):
    """The outreach tools exactly as register_outreach_tools builds them."""
    from core import labeled_autogen_function
    tools = {}
    monkeypatch.setattr(labeled_autogen_function, 'register_labeled_function',
                        lambda func, **kw: tools.__setitem__(func.__name__, func))
    store.register_outreach_tools(object(), object(), 'owner-1')
    return tools


def _seed(store, **prospect):
    p = dict({'id': 'p1', 'company': 'Shop', 'contact_name': 'Ana',
              'email': 'ana@shop.example', 'stage': 'new', 'emails_sent': 0,
              'last_email_at': None, 'last_reply_at': None,
              'created_by': 'owner-1'}, **prospect)
    data = {'prospects': {'p1': p}, 'sequences': {}, 'sent_log': []}
    store._save_prospects(data)
    return data


def _due_sequence(store, steps=1, **prospect):
    """p1 with an active sequence whose ``steps`` steps are all due."""
    data = _seed(store, **dict({'stage': 'contacted', 'emails_sent': 1}, **prospect))
    due = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    data['sequences']['s1'] = {
        'id': 's1', 'prospect_id': 'p1', 'name': 'bump', 'status': 'active',
        'exit_on_reply': True,
        'steps': [{'step_number': n + 2, 'delay_days': 3, 'status': 'pending',
                   'scheduled_at': due, 'subject': f'bump {n + 2}',
                   'body_html': '<p>hi again</p>', 'sent_at': None}
                  for n in range(steps)]}
    store._save_prospects(data)
    return data


@pytest.fixture
def no_wait(store, monkeypatch):
    """A refused planned email may be tried again at once."""
    monkeypatch.setattr(store, 'OUTREACH_RETRY_WAIT', timedelta(0), raising=False)


def _tries(store):
    """OUTREACH_SEND_ATTEMPTS.  1c4b2b650 named it FOLLOWUP_SEND_ATTEMPTS:
    read either, so a run on that commit fails by what it sends, not by a
    missing name."""
    return getattr(store, 'OUTREACH_SEND_ATTEMPTS',
                   getattr(store, 'FOLLOWUP_SEND_ATTEMPTS', 3))


# ── send_outreach_email ────────────────────────────────────────────

def test_a_refused_email_leaves_the_prospect_as_it_was(channels, store, no_mailer, monkeypatch):
    channels.registry.register(_EmailChannel(ok=False, error='550 mailbox unavailable'))
    _seed(store)

    out = json.loads(_tools(store, monkeypatch)['send_outreach_email'](
        'p1', 'your shop on mcgroce', '<p>hello Ana</p>'))

    assert out['success'] is False
    assert '550' in json.dumps(out)
    p = store._load_prospects()['prospects']['p1']
    assert (p['stage'], p['emails_sent'], p['last_email_at']) == ('new', 0, None)
    assert store._load_prospects()['sent_log'] == []
    assert no_mailer == []


def test_a_delivered_email_still_moves_the_prospect_to_contacted(channels, store, no_mailer, monkeypatch):
    email = _EmailChannel(ok=True)
    channels.registry.register(email)
    _seed(store)

    out = json.loads(_tools(store, monkeypatch)['send_outreach_email'](
        'p1', 'your shop on mcgroce', '<p>hello Ana</p>'))

    assert out['success'] is True and out['prospect_stage'] == 'contacted'
    assert email.emails == [('ana@shop.example', 'your shop on mcgroce')]
    p = store._load_prospects()['prospects']['p1']
    assert (p['stage'], p['emails_sent']) == ('contacted', 1)
    assert [e['subject'] for e in store._load_prospects()['sent_log']] == ['your shop on mcgroce']


# ── follow-ups ─────────────────────────────────────────────────────

def test_a_follow_up_that_failed_stays_due_and_is_not_counted(channels, store, no_mailer):
    channels.registry.register(_EmailChannel(ok=False, error='550 mailbox unavailable'))
    _due_sequence(store)

    tick = store.check_pending_followups_daemon()

    assert tick['sent'] == 0
    data = store._load_prospects()
    step = data['sequences']['s1']['steps'][0]
    assert step['status'] == 'pending' and step['sent_at'] is None
    assert data['sequences']['s1']['status'] == 'active'
    assert data['prospects']['p1']['emails_sent'] == 1


@pytest.mark.parametrize('steps', [1, 2])
def test_a_follow_up_that_keeps_failing_ends_its_sequence(channels, store, no_mailer, no_wait, steps):
    """Given up after OUTREACH_SEND_ATTEMPTS refused sends, and the later
    step is not sent to the same refusing address (review of 1c4b2b650:
    3 steps cost 9 refused sends and ended 'completed').  A one-step
    sequence ends 'failed' too, not 'completed' as if it had been sent."""
    email = _EmailChannel(ok=False, error='550 mailbox unavailable')
    channels.registry.register(email)
    _due_sequence(store, steps=steps)

    for _ in range(10):
        store.check_pending_followups_daemon()

    data = store._load_prospects()
    first = data['sequences']['s1']['steps'][0]
    assert first['status'] == 'failed'
    assert first['attempts'] == _tries(store)
    assert '550' in first['last_error']
    for later in data['sequences']['s1']['steps'][1:]:
        assert later['status'] == 'pending' and 'attempts' not in later
    assert data['sequences']['s1']['status'] == 'failed'
    assert len(email.emails) == _tries(store)


def test_a_refused_follow_up_waits_before_it_is_tried_again(channels, store, no_mailer):
    email = _EmailChannel(ok=False, error='451 try later')
    channels.registry.register(email)
    _due_sequence(store)

    for _ in range(5):
        store.check_pending_followups_daemon()
    assert len(email.emails) == 1

    data = store._load_prospects()
    step = data['sequences']['s1']['steps'][0]
    assert step['status'] == 'pending'
    step['retry_after'] = (datetime.utcnow() - timedelta(seconds=1)).isoformat()
    store._save_prospects(data)
    store.check_pending_followups_daemon()
    assert len(email.emails) == 2


def test_a_step_waiting_to_be_tried_again_is_not_overtaken_by_the_next(channels, store, no_mailer):
    email = _EmailChannel(ok=False, error='451 try later')
    channels.registry.register(email)
    _due_sequence(store, steps=2)

    for _ in range(3):
        store.check_pending_followups_daemon()

    assert [s for _, s in email.emails] == ['bump 2']
    second = store._load_prospects()['sequences']['s1']['steps'][1]
    assert second['status'] == 'pending' and 'attempts' not in second


def test_a_delivered_follow_up_is_counted(channels, store, no_mailer):
    channels.registry.register(_EmailChannel(ok=True))
    _due_sequence(store)

    tick = store.check_pending_followups_daemon()

    assert tick['sent'] == 1
    data = store._load_prospects()
    assert data['sequences']['s1']['steps'][0]['status'] == 'sent'
    assert data['prospects']['p1']['emails_sent'] == 2


# ── the sales journey ──────────────────────────────────────────────

def test_the_journeys_first_email_is_not_counted_when_it_failed(channels, store, no_mailer):
    from integrations.agent_engine.journey_engine import JourneyEngine
    channels.registry.register(_EmailChannel(ok=False, error='550 mailbox unavailable'))
    prospect = {'id': 'p1', 'email': 'ana@shop.example', 'stage': 'new',
                'emails_sent': 0,
                'email_draft': {'subject': 'hi', 'body': '<p>hello</p>'}}

    result = JourneyEngine()._action_send_email(prospect, {})

    assert result.get('sent') is not True
    assert prospect['emails_sent'] == 0 and prospect['stage'] == 'new'
    assert 'last_email_at' not in prospect


def test_a_meeting_is_not_marked_proposed_when_its_email_failed(channels, store, no_mailer):
    from integrations.agent_engine.journey_engine import JourneyEngine
    channels.registry.register(_EmailChannel(ok=False, error='550 mailbox unavailable'))
    prospect = {'id': 'p1', 'email': 'ana@shop.example', 'stage': 'replied'}

    result = JourneyEngine()._action_propose_meeting(prospect, {})

    assert result.get('proposed') is not True
    assert not prospect.get('meeting_proposed')


# ── the daemon's journey tick (agent_daemon: the follow-up checker, then
# journey_daemon_tick, every 5 ticks) ───────────────────────────────

def _outreach_prospect(store):
    _seed(store, journey_stage='outreach',
          email_draft={'subject': 'hi', 'body': '<p>hello</p>'})


def test_the_journeys_first_email_is_given_up_after_its_tries(channels, store, no_mailer, no_wait):
    """FAILS ON 1c4b2b650: a refused first email went out again on every
    journey tick (10 sends in 10 ticks)."""
    from integrations.agent_engine.journey_engine import journey_daemon_tick
    email = _EmailChannel(ok=False, error='550 mailbox unavailable')
    channels.registry.register(email)
    _outreach_prospect(store)

    for _ in range(10):
        journey_daemon_tick()

    assert len(email.emails) == _tries(store)
    p = store._load_prospects()['prospects']['p1']
    assert p['email_draft']['status'] == 'failed'
    assert (p['emails_sent'], p['stage']) == (0, 'new')


def test_the_journeys_refused_first_email_waits_before_it_is_tried_again(channels, store, no_mailer):
    from integrations.agent_engine.journey_engine import journey_daemon_tick
    email = _EmailChannel(ok=False, error='451 try later')
    channels.registry.register(email)
    _outreach_prospect(store)

    for _ in range(5):
        journey_daemon_tick()

    assert len(email.emails) == 1
    assert store._load_prospects()['prospects']['p1']['email_draft']['attempts'] == 1


def _daemon_cycles(store, n):
    """What agent_daemon runs every 5 ticks, ``n`` times."""
    from integrations.agent_engine.journey_engine import journey_daemon_tick
    for _ in range(n):
        store.check_pending_followups_daemon()
        journey_daemon_tick()


def _nurture_pipeline(store, steps=1):
    """p1 in the journey's nurture stage with a due sequence, and two more
    nurture prospects with no sequence."""
    now = datetime.utcnow().isoformat()
    data = _due_sequence(store, steps=steps, journey_stage='nurture', last_email_at=now)
    for pid in ('p2', 'p3'):
        data['prospects'][pid] = dict(data['prospects']['p1'], id=pid,
                                      email=f'{pid}@shop.example')
    store._save_prospects(data)


def test_a_refused_follow_up_is_counted_once_per_send_in_the_daemons_order(
        channels, store, no_mailer, no_wait):
    """FAILS ON 1c4b2b650: the journey tick ran the checker once per nurture
    prospect, each loading and saving the store, then saved its older copy
    over their tries, so one step took 6 refused sends, not 3."""
    email = _EmailChannel(ok=False, error='550 mailbox unavailable')
    channels.registry.register(email)
    _nurture_pipeline(store)

    _daemon_cycles(store, 5)

    assert len(email.emails) == _tries(store)
    step = store._load_prospects()['sequences']['s1']['steps'][0]
    assert (step['status'], step['attempts']) == ('failed', _tries(store))


def test_a_delivered_follow_up_goes_out_once_from_the_journey_tick(channels, store, no_mailer):
    """FAILS ON 1c4b2b650 (and before it): the journey tick's older copy put
    the delivered step back to 'pending', so it was sent again."""
    from integrations.agent_engine.journey_engine import journey_daemon_tick
    email = _EmailChannel(ok=True)
    channels.registry.register(email)
    _nurture_pipeline(store)

    for _ in range(3):
        journey_daemon_tick()

    assert [s for _, s in email.emails] == ['bump 2']
    data = store._load_prospects()
    assert data['sequences']['s1']['steps'][0]['status'] == 'sent'
    assert data['prospects']['p1']['emails_sent'] == 2


def _journey_tools(store, monkeypatch):
    """The journey tools exactly as register_journey_tools builds them."""
    from core import labeled_autogen_function
    from integrations.agent_engine.journey_engine import register_journey_tools
    tools = {}
    monkeypatch.setattr(labeled_autogen_function, 'register_labeled_function',
                        lambda func, **kw: tools.__setitem__(func.__name__, func))
    register_journey_tools(object(), object(), 'owner-1')
    return tools


def test_the_run_journey_tick_tool_sends_a_delivered_step_once(channels, store, no_mailer, monkeypatch):
    """Review of 6da8b09c7: drop store=data from run_journey_tick and the
    tool's older copy puts the delivered step back to pending."""
    email = _EmailChannel(ok=True)
    channels.registry.register(email)
    _nurture_pipeline(store)
    tools = _journey_tools(store, monkeypatch)

    for _ in range(3):
        tools['run_journey_tick']()

    assert [s for _, s in email.emails] == ['bump 2']


def test_the_advance_prospect_stage_tool_sends_a_delivered_step_once(channels, store, no_mailer, monkeypatch):
    """Review of 6da8b09c7: drop store=data from advance_prospect_stage and
    the step it sends on entering nurture goes out again on the next tick."""
    email = _EmailChannel(ok=True)
    channels.registry.register(email)
    _due_sequence(store, journey_stage='outreach',
                  last_email_at=datetime.utcnow().isoformat())
    tools = _journey_tools(store, monkeypatch)

    moved = json.loads(tools['advance_prospect_stage']('p1', 'nurture'))
    tools['run_journey_tick']()

    assert moved['moved'] is True
    assert [s for _, s in email.emails] == ['bump 2']


@pytest.fixture
def warned(store, monkeypatch):
    """The outreach log's WARNING lines."""
    lines = []
    monkeypatch.setattr(store.logger, 'warning',
                        lambda msg, *a, **k: lines.append(msg % a if a else msg))
    return lines


def _summary(out):
    """A tool result's refused emails as {'latest', 'given_up', 'waiting'}.
    Since fe99feac3's review the counts sit at the head of the result
    ('not_delivered_count') and the rows stay in 'not_delivered'; earlier
    commits kept one 'not_delivered' dict (or list).  Read every shape, so a
    run on an earlier commit fails by what it shows, not by a missing key."""
    out = json.loads(out)
    if 'not_delivered_count' in out:
        return dict(out['not_delivered_count'], latest=out['not_delivered'])
    return out['not_delivered']


def _listed(store, monkeypatch):
    """not_delivered as both agent tools return it, one per tool."""
    return (
        _summary(_tools(store, monkeypatch)['get_pipeline_status']()),
        _summary(_journey_tools(store, monkeypatch)['view_journey_pipeline']()))


def _entries(listed):
    """The listed emails as (prospect, which email, given up).  5970e07a1
    returned a bare list keyed 'email': read both, so a run on that commit
    fails by what it lists, not by the shape."""
    rows = listed['latest'] if isinstance(listed, dict) else listed
    return sorted((e['prospect_id'], e.get('which', e.get('email')), e['given_up'])
                  for e in rows)


def test_a_given_up_first_email_is_logged_and_listed(channels, store, no_mailer, no_wait, warned, monkeypatch):
    """Review of 6da8b09c7: a given-up first email reached no log, and every
    read the agent has still showed the prospect as contacted with nothing
    wrong."""
    from integrations.agent_engine.journey_engine import journey_daemon_tick
    channels.registry.register(_EmailChannel(ok=False, error='550 mailbox unavailable'))
    _outreach_prospect(store)

    for _ in range(3):
        journey_daemon_tick()

    assert any('first email' in w and 'given up' in w and '550' in w for w in warned), warned
    for listed in _listed(store, monkeypatch):
        assert _entries(listed) == [('p1', 'first email', True)], listed
        assert (listed['given_up'], listed['waiting']) == (1, 0), listed
        assert '550' in listed['latest'][0]['last_error']


def test_a_first_email_that_reached_its_prospect_later_is_not_listed(channels, store, no_mailer, monkeypatch):
    """Review of 5970e07a1: a first email refused once and delivered on its
    retry was listed as not delivered forever, and so was a given-up one
    after the agent's own send_outreach_email reached the prospect."""
    from integrations.agent_engine.journey_engine import journey_daemon_tick
    email = _EmailChannel(ok=False, error='451 try later')
    channels.registry.register(email)
    _outreach_prospect(store)
    journey_daemon_tick()                               # refused, waits
    email.ok, email.error = True, None
    data = store._load_prospects()
    data['prospects']['p1']['email_draft']['retry_after'] = (
        datetime.utcnow() - timedelta(seconds=1)).isoformat()
    store._save_prospects(data)

    journey_daemon_tick()                               # delivered

    assert len(email.emails) == 2
    assert store._load_prospects()['prospects']['p1']['emails_sent'] == 1
    for listed in _listed(store, monkeypatch):
        assert _entries(listed) == [], listed


def test_a_given_up_first_email_the_agent_then_delivered_is_not_listed(
        channels, store, no_mailer, no_wait, monkeypatch):
    from integrations.agent_engine.journey_engine import journey_daemon_tick
    email = _EmailChannel(ok=False, error='550 mailbox unavailable')
    channels.registry.register(email)
    _outreach_prospect(store)
    for _ in range(3):
        journey_daemon_tick()                           # given up
    email.ok, email.error = True, None

    sent = json.loads(_tools(store, monkeypatch)['send_outreach_email'](
        'p1', 'your shop on mcgroce', '<p>hello Ana</p>'))

    assert sent['success'] is True
    for listed in _listed(store, monkeypatch):
        assert _entries(listed) == [], listed


def test_the_follow_up_listing_holds_only_steps_that_did_not_reach_their_prospect(
        channels, store, no_mailer, monkeypatch):
    """Review of 5970e07a1: a step in a sequence a reply had ended was
    listed as waiting with a retry time though it is never tried again,
    and nothing pinned the follow-up half of the listing.  Four prospects,
    each refused once: p1 waits, p2 is given up, p3 is delivered on its
    retry, p4 replies.  Only p1 and p2 are listed."""
    email = _EmailChannel(ok=False, error='451 try later')
    channels.registry.register(email)
    data = _due_sequence(store)
    for n, pid in enumerate(('p2', 'p3', 'p4'), 2):
        data['prospects'][pid] = dict(data['prospects']['p1'], id=pid,
                                      company=f'Shop {n}', email=f'{pid}@shop.example')
        data['sequences'][f's{n}'] = dict(json.loads(json.dumps(data['sequences']['s1'])),
                                          id=f's{n}', prospect_id=pid)
    now = datetime.utcnow()
    store.send_due_followups(data, now)                 # all four refused once
    later = now
    for _ in range(_tries(store) - 1):                  # p2: given up
        later += timedelta(hours=10)
        store.send_due_followups(data, later, prospect_id='p2')
    email.ok, email.error = True, None
    store.send_due_followups(data, later, prospect_id='p3')     # delivered
    data['prospects']['p4']['last_reply_at'] = later.isoformat()
    store.send_due_followups(data, later, prospect_id='p4')     # ended by the reply
    store._save_prospects(data)
    assert data['sequences']['s3']['steps'][0]['status'] == 'sent'
    assert data['sequences']['s4']['status'] == 'completed'

    for listed in _listed(store, monkeypatch):
        assert _entries(listed) == [('p1', 'follow-up step 2', False),
                                    ('p2', 'follow-up step 2', True)], listed
        rows = {e['prospect_id']: e for e in listed['latest']}
        assert rows['p1']['retry_after'] and rows['p1']['attempts'] == 1
        assert rows['p2']['retry_after'] is None and rows['p2']['attempts'] == _tries(store)
        assert (listed['given_up'], listed['waiting']) == (1, 1)


def _twenty_bounced(store, channels):
    """20 prospects whose first email was refused until given up (by the
    real journey ticks), and 30 more that are fine."""
    from integrations.agent_engine.journey_engine import journey_daemon_tick
    channels.registry.register(_EmailChannel(ok=False, error='550 mailbox unavailable'))
    data = {'prospects': {}, 'sequences': {}, 'sent_log': []}
    for n in range(20):
        pid = f'b{n}'
        data['prospects'][pid] = {
            'id': pid, 'company': f'Bounced shop {n}', 'contact_name': 'Ana',
            'email': f'{pid}@shop.example', 'stage': 'new', 'emails_sent': 0,
            'last_email_at': None, 'last_reply_at': None, 'created_by': 'owner-1',
            'journey_stage': 'outreach',
            'email_draft': {'subject': 'hi', 'body': '<p>hello</p>'}}
    store._save_prospects(data)
    for _ in range(_tries(store)):
        journey_daemon_tick()                           # all 20 given up
    data = store._load_prospects()
    for n in range(30):                                 # and 30 that are fine
        pid = f'f{n}'
        data['prospects'][pid] = dict(data['prospects']['b0'], id=pid,
                                      company=f'Fine shop {n}', email=f'{pid}@shop.example',
                                      emails_sent=1, stage='contacted',
                                      journey_stage='nurture')
        data['prospects'][pid].pop('email_draft')
    store._save_prospects(data)


def test_a_long_bounce_list_is_bounded_and_the_trimmed_view_keeps_the_ab_stats(
        channels, store, no_mailer, no_wait, monkeypatch):
    """Review of 5970e07a1: with 20 of 50 prospects bounced the journey view
    grew from 6.1K to 10.8K chars, and the wire trim's middle cut at 4,000
    or 8,000 chars (n_ctx 4096 on the installed build) then dropped the A/B
    stats, which sat before the list.  Now the counts cover all of them,
    the list holds the newest UNDELIVERED_LIST_LIMIT, and ab_test is last."""
    from core.llm_outbound_logger import _truncate_msg_content
    _twenty_bounced(store, channels)

    out = _journey_tools(store, monkeypatch)['view_journey_pipeline']()

    assert len(out) > 4000, len(out)                    # the 4,000 cut happens
    for budget in (4000, 8000):
        cut, _ = _truncate_msg_content({'role': 'tool', 'content': out},
                                       budget, '...', lambda c: c)
        assert '"ab_test"' in cut['content'], (budget, len(out))
        # Review of 82a1248ee: the counts sat before the list, where the
        # cut removed them, so the agent could not tell 20 had bounced.
        assert '"given_up": 20' in cut['content'], (budget, len(out))
    status = _tools(store, monkeypatch)['get_pipeline_status']()
    cut, _ = _truncate_msg_content({'role': 'tool', 'content': status},
                                   4000, '...', lambda c: c)
    assert '"given_up": 20' in cut['content'], len(status)
    listed = _summary(out)
    limit = getattr(store, 'UNDELIVERED_LIST_LIMIT', 10)
    assert (listed['given_up'], listed['waiting']) == (20, 0)
    assert len(listed['latest']) == limit
    tried = [e['last_tried_at'] for e in listed['latest']]
    assert all(tried) and tried == sorted(tried, reverse=True)
    pipeline = _summary(status)
    assert (pipeline['given_up'], len(pipeline['latest'])) == (20, limit)


@pytest.mark.parametrize('tool', ['view_journey_pipeline', 'get_pipeline_status'])
def test_the_seats_token_cut_keeps_the_bounce_counts(channels, store, no_mailer, no_wait,
                                                     monkeypatch, tool):
    """Review of fe99feac3: the create and reuse seats register these tools
    behind hartos.helper.token_limiter, which keeps only the head of a tool
    message, and that cut comes before the wire trim.  Counts moved to the
    tail survived the wire trim and were cut there: the agent could not
    tell that 20 had bounced."""
    from core.constants import (AUTOGEN_MESSAGE_TOKEN_BUDGET,
                                AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE)
    from hartos.helper import token_limiter
    _twenty_bounced(store, channels)
    tools = dict(_tools(store, monkeypatch), **_journey_tools(store, monkeypatch))
    out = tools[tool]()
    turn = [
        {'role': 'user', 'name': 'ChatInstructor',
         'content': 'Check the outreach pipeline and say what bounced.'},
        {'role': 'assistant', 'name': 'Assistant', 'content': '',
         'tool_calls': [{'id': 'call_1', 'type': 'function',
                         'function': {'name': tool, 'arguments': '{}'}}]},
        {'role': 'tool', 'name': 'Assistant', 'tool_call_id': 'call_1',
         'content': out},
    ]
    limiter = token_limiter(max_tokens=AUTOGEN_MESSAGE_TOKEN_BUDGET,
                            max_tokens_per_message=AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
                            min_tokens=0)

    kept = [m for m in limiter.apply_transform(turn) if m.get('role') == 'tool']

    assert len(kept) == 1 and len(kept[0]['content']) < len(out), (
        'the seat cut did not cut this result', len(out))
    assert '"given_up": 20' in kept[0]['content'], kept[0]['content'][:300]


def test_a_follow_up_given_up_on_the_journey_path_is_logged(channels, store, no_mailer, no_wait, warned):
    """Review of 6da8b09c7: the journey tick's checks logged nothing; only
    the daemon checker's wrapper did."""
    from integrations.agent_engine.journey_engine import journey_daemon_tick
    channels.registry.register(_EmailChannel(ok=False, error='550 mailbox unavailable'))
    _nurture_pipeline(store)

    for _ in range(3):
        journey_daemon_tick()

    refused = [w for w in warned if 'follow-up step 2' in w]
    assert len(refused) == _tries(store), warned
    assert 'given up' in refused[-1]


def test_a_waiting_step_is_reported_and_is_not_overdue(channels, store, no_mailer):
    """Review of 6da8b09c7: while a refused step waited, the check returned
    nothing and the sales prompt still counted it overdue and told the agent
    to investigate."""
    from integrations.agent_engine.journey_engine import _build_sales_prompt
    channels.registry.register(_EmailChannel(ok=False, error='451 try later'))
    _due_sequence(store)
    store.check_pending_followups_daemon()

    checked = json.loads(store._check_pending_followups_impl())

    waiting = [a for a in checked['actions'] if a['action'] == 'followup_waiting']
    assert [(a['step'], a['attempts']) for a in waiting] == [(2, 1)], checked
    assert waiting[0]['retry_after'] and '451' in waiting[0]['error']
    assert 'Pending follow-ups: 1 (overdue: 0)' in _build_sales_prompt({})


def test_a_step_behind_a_waiting_step_is_not_overdue(channels, store, no_mailer):
    """Review of 5970e07a1 (its own case): two due steps, the first refused
    and waiting.  The check sends nothing, so the prompt read 'overdue: 1'
    and told the agent to investigate a step the check never reaches."""
    from integrations.agent_engine.journey_engine import _build_sales_prompt
    email = _EmailChannel(ok=False, error='451 try later')
    channels.registry.register(email)
    _due_sequence(store, steps=2)
    store.check_pending_followups_daemon()

    checked = json.loads(store._check_pending_followups_impl())

    assert [a['action'] for a in checked['actions']] == ['followup_waiting'], checked
    assert [s for _, s in email.emails] == ['bump 2']
    assert 'Pending follow-ups: 2 (overdue: 0)' in _build_sales_prompt({})


def test_a_step_not_yet_due_is_neither_sent_nor_overdue(channels, store, no_mailer):
    """Review of 82a1248ee: no test had a step whose time had not come, so
    a checker that sent it early, or a count that called it overdue,
    passed every test.  Step 2 is due, step 3 is two days out."""
    from integrations.agent_engine.journey_engine import _build_sales_prompt
    email = _EmailChannel(ok=True)
    channels.registry.register(email)
    data = _due_sequence(store, steps=2)
    data['sequences']['s1']['steps'][1]['scheduled_at'] = (
        datetime.utcnow() + timedelta(days=2)).isoformat()
    store._save_prospects(data)
    assert 'Pending follow-ups: 2 (overdue: 1)' in _build_sales_prompt({})

    store.check_pending_followups_daemon()
    store.check_pending_followups_daemon()

    assert [s for _, s in email.emails] == ['bump 2']
    steps = store._load_prospects()['sequences']['s1']['steps']
    assert [s['status'] for s in steps] == ['sent', 'pending']
    assert 'Pending follow-ups: 1 (overdue: 0)' in _build_sales_prompt({})


def test_planned_emails_never_tried_are_not_listed(channels, store, no_mailer, monkeypatch):
    """Review of 82a1248ee: an untried pending step and an untried first
    email draft are planned, not undelivered; nothing pinned that."""
    _due_sequence(store, emails_sent=0, journey_stage='outreach',
                  email_draft={'subject': 'hi', 'body': '<p>hello</p>'})

    for listed in _listed(store, monkeypatch):
        assert _entries(listed) == [], listed
        assert (listed['given_up'], listed['waiting']) == (0, 0), listed


def test_due_steps_with_nothing_waiting_are_all_overdue(store):
    """Unchanged by the reviews: a sequence whose next step is due counts
    every step past its time."""
    from integrations.agent_engine.journey_engine import _build_sales_prompt
    _due_sequence(store, steps=2)

    assert 'Pending follow-ups: 2 (overdue: 2)' in _build_sales_prompt({})


def test_a_refusal_on_the_daemons_check_is_logged_once(channels, store, no_mailer, warned):
    """Review nit of 5970e07a1: 'logged in one place' was unpinned; a second
    warning in the daemon checker's wrapper passed every test."""
    channels.registry.register(_EmailChannel(ok=False, error='550 mailbox unavailable'))
    _due_sequence(store)

    store.check_pending_followups_daemon()

    assert len([w for w in warned if '550' in w]) == 1, warned


def test_the_wait_grows_with_each_refused_try(channels, store, no_mailer):
    """1 h after the first refusal, 2 h after the second."""
    channels.registry.register(_EmailChannel(ok=False, error='451 try later'))
    _due_sequence(store)
    waits = []
    for _ in range(2):
        before = datetime.utcnow()
        store.check_pending_followups_daemon()
        data = store._load_prospects()
        step = data['sequences']['s1']['steps'][0]
        waits.append(datetime.fromisoformat(step['retry_after']) - before)
        step['retry_after'] = (datetime.utcnow() - timedelta(seconds=1)).isoformat()
        store._save_prospects(data)

    assert [round(w.total_seconds() / 3600) for w in waits] == [1, 2], waits


def test_a_mailer_refusal_says_what_the_mailer_answered(channels, store, monkeypatch):
    """No email channel: the mailer answers 500, and the step keeps that."""
    from core import http_pool
    monkeypatch.setattr(http_pool, 'pooled_post', lambda *a, **k: SimpleNamespace(
        status_code=500, json=lambda: {}, text=''))
    _due_sequence(store)

    store.check_pending_followups_daemon()

    step = store._load_prospects()['sequences']['s1']['steps'][0]
    assert step['last_error'] == 'mailer_service answered 500'


def test_a_journey_tick_sends_only_its_nurture_prospects_steps(channels, store, no_mailer):
    """A prospect outside the journey's nurture stage is the daemon
    checker's: a nurture prospect's check does not send its steps."""
    from integrations.agent_engine.journey_engine import journey_daemon_tick
    email = _EmailChannel(ok=True)
    channels.registry.register(email)
    data = _due_sequence(store)                     # p1: stage 'contacted'
    data['prospects']['p2'] = dict(data['prospects']['p1'], id='p2',
                                   journey_stage='nurture',
                                   last_email_at=datetime.utcnow().isoformat())
    store._save_prospects(data)

    journey_daemon_tick()

    assert email.emails == []
