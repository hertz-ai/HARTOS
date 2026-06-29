"""The agentic HOME PRODUCER - composes {hero, rows} from live context + the
local LLM and pushes it through the EXISTING compose_home feed (gap Q2).

Before this, compose_home (the renderer feed) had NO producer: the surface was
hartHome.js's offline samplePayload + direct client fetches, never an agent
composition.  These tests pin the producer's contract:

  * the deterministic backbone is built from REAL context (no fabricated data);
  * the hero leads with the REAL Spark figure, and is OMITTED (rows-only push)
    on a 0/unresolved balance so the client's own earnings hero is preserved;
  * a (possibly hallucinated) LLM composition can NEVER inject a bad accent /
    click verb / deep-link / markup - the sanitizer coerces or drops it;
  * the LLM (the heart) curates the narrative + emphasis, but a junk reply
    leaves the deterministic backbone standing (the home never breaks);
  * run_home_compose pushes through compose_home (the ONE governed transport),
    in-process via the registry or cross-process via the existing route, and a
    halted hive refuses it.

Behavioural: mock the boundaries (DB/dashboard, wallet, the model bus, the
registry/HTTP), call the REAL producer functions, assert observable output.

Run isolated (this box OOMs the full suite):
    python tests/unit/test_home_producer.py
"""
from unittest.mock import MagicMock, patch

from integrations.agent_engine import liquid_ui_service as L
from integrations.agent_engine.liquid_ui_service import (
    HOME_ROW_ACCENTS, HOME_CARD_ACTIONS, HOME_PANEL_TARGETS,
    LiquidUIService, build_home_payload, run_home_compose,
    _deterministic_home_payload, _sanitize_home_payload, _llm_curate_home)


def _ctx(spark=None):
    return {
        'time_of_day': 'this morning',
        'agents_total': 3, 'agents_running': 2,
        'continue': [
            {'title': 'Weekly recap', 'topic': 'Weekly recap',
             'icon': 'edit_note', 'action': 'resume', 'live': 'running'},
        ],
        'hive': [
            {'title': 'Invoice Chaser', 'topic': 'Invoice Chaser',
             'icon': 'receipt_long', 'action': 'open', 'target': 'communities'},
        ],
        'recipes': [
            {'title': 'Trip planner', 'topic': 'Trip planner',
             'icon': 'auto_awesome', 'badge': 'Replay', 'action': 'open',
             'target': 'recipes'},
        ],
        'owner_uid': 'u1' if spark is not None else None,
        'spark': spark,
    }


def _row_titles(payload):
    return [r['title'] for r in payload['rows']]


# ── 1. deterministic backbone from real context ──────────────────────────────

def test_deterministic_payload_rows_from_real_context():
    p = _deterministic_home_payload(_ctx(spark=2140))
    titles = _row_titles(p)
    # Continue (live agents) leads, Flagship always present, Recipes + hive too.
    assert titles[0] == 'Continue'
    assert 'Flagship agents' in titles
    assert 'Recipes' in titles
    assert any(t.startswith('Top agents in the hive') for t in titles)
    # Every row/card is on-contract (accent + action allow-sets).
    for row in p['rows']:
        assert row['accent'] in HOME_ROW_ACCENTS
        for c in row['cards']:
            assert c['action'] in HOME_CARD_ACTIONS


def test_hero_leads_with_real_spark_when_positive():
    p = _deterministic_home_payload(_ctx(spark=2140))
    assert p['hero'] is not None
    assert p['hero']['amount'] == 2140
    assert p['hero']['amount_unit'] == 'Spark'
    assert p['hero']['payout_pending'] is True   # honest: no payout rail yet
    assert p['hero']['agents'] == 2 and p['hero']['tasks'] == 3


def test_no_hero_when_zero_or_unresolved_earnings():
    # rows-only push so the client's own session-scoped earnings hero stands
    # (never clobber a real figure with an empty one).
    assert _deterministic_home_payload(_ctx(spark=None))['hero'] is None
    assert _deterministic_home_payload(_ctx(spark=0))['hero'] is None
    # ...but the rows still compose (the home is never empty).
    assert _deterministic_home_payload(_ctx(spark=0))['rows']


def test_flagship_row_is_always_present_even_with_no_agents():
    empty = {'time_of_day': 't', 'agents_total': 0, 'agents_running': 0,
             'continue': [], 'hive': [], 'recipes': [], 'spark': None}
    p = _deterministic_home_payload(empty)
    assert _row_titles(p) == ['Flagship agents']      # never blank


# ── 2. the sanitizer is the guard against a hallucinated LLM composition ──────

def test_sanitize_coerces_bad_accent_action_target_and_strips_markup():
    dirty = {
        'hero': {'amount': 500, 'eyebrow': 'Earned <b>now</b>',
                 'amount_unit': 'Spark'},
        'rows': [{
            'title': 'Evil <script>row',
            'accent': 'chartreuse',                 # not in the spectrum
            'see_all': 'rm -rf /',                  # not a real panel
            'cards': [
                {'title': 'A <img onerror=x>', 'action': 'launch_nuke',
                 'target': 'etc_passwd', 'icon': 'BAD ICON!',
                 'image_url': 'javascript:alert(1)'},
                {'title': '', 'action': 'ask'},     # empty title -> dropped
            ],
        }],
    }
    clean = _sanitize_home_payload(dirty)
    assert clean is not None
    row = clean['rows'][0]
    assert row['accent'] == 'teal'                  # coerced to a safe default
    assert 'see_all' not in row                     # unknown target dropped
    assert '<' not in row['title'] and '>' not in row['title']
    assert len(row['cards']) == 1                   # the empty-title card dropped
    card = row['cards'][0]
    assert card['action'] == 'open'                 # unknown verb -> safe default
    assert 'target' not in card                     # unknown target dropped
    assert 'icon' not in card                       # non [a-z0-9_] icon dropped
    assert 'image_url' not in card                  # non-http(s) url dropped
    assert '<' not in card['title']
    # hero markup stripped, real amount kept.
    assert clean['hero']['amount'] == 500 and '<' not in clean['hero']['eyebrow']


def test_sanitize_rejects_structurally_empty():
    assert _sanitize_home_payload({'rows': 'nope'}) is None
    assert _sanitize_home_payload({'rows': [{'cards': []}]}) is None
    assert _sanitize_home_payload('not a dict') is None


def test_sanitize_hero_dropped_without_a_positive_amount():
    # A hero with no money figure must not survive (it would clobber the client).
    clean = _sanitize_home_payload({
        'hero': {'eyebrow': 'hi'},                  # no amount
        'rows': [{'title': 'R', 'cards': [{'title': 'C', 'action': 'open'}]}]})
    assert clean['hero'] is None


# ── 3. LLM curation (the heart) colours the backbone; junk -> backbone stands ─

def _fake_resp(status, response_text):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = {'response': response_text}
    return r


def test_llm_curation_applies_eyebrow_and_features_a_row():
    backbone = _deterministic_home_payload(_ctx(spark=2140))
    reply = '{"eyebrow": "Up while you slept", "feature": "Recipes"}'
    with patch('core.http_pool.pooled_post',
               return_value=_fake_resp(200, reply)):
        out = _llm_curate_home(_ctx(spark=2140), backbone, 6790)
    assert out is not None
    assert out['hero']['eyebrow'] == 'Up while you slept'
    assert out['rows'][0]['title'] == 'Recipes'      # featured row leads now


def test_build_falls_back_to_deterministic_on_llm_junk():
    ctx = _ctx(spark=2140)
    backbone_clean = _sanitize_home_payload(_deterministic_home_payload(ctx))
    with patch.object(L, '_gather_home_context', return_value=ctx), \
         patch('core.http_pool.pooled_post',
               return_value=_fake_resp(200, 'I cannot help with that.')):
        payload = build_home_payload()
    # Junk reply (no JSON) -> the deterministic backbone composition stands.
    assert payload is not None
    assert _row_titles(payload) == _row_titles(backbone_clean)
    assert payload['hero']['amount'] == 2140


def test_build_survives_a_dead_model_bus():
    ctx = _ctx(spark=0)
    with patch.object(L, '_gather_home_context', return_value=ctx), \
         patch('core.http_pool.pooled_post', side_effect=OSError('no bus')):
        payload = build_home_payload()
    assert payload is not None                        # composes offline
    assert payload['hero'] is None                    # 0 balance -> rows-only
    assert 'Flagship agents' in _row_titles(payload)


# ── 4. run_home_compose pushes through the ONE governed transport ─────────────

def test_compose_home_now_builds_then_pushes(_unused=None):
    svc = LiquidUIService(a2ui_enabled=True)
    payload = {'hero': {'amount': 9, 'amount_unit': 'Spark'},
               'rows': [{'title': 'R', 'accent': 'teal',
                         'cards': [{'title': 'C', 'action': 'open'}]}]}
    with patch.object(L, 'build_home_payload', return_value=payload), \
         patch.object(svc, 'compose_home', return_value=True) as push:
        ok = svc.compose_home_now(reason='unit')
    assert ok is True
    push.assert_called_once()
    kw = push.call_args.kwargs
    assert kw['hero'] == payload['hero']
    assert kw['rows'] == payload['rows']
    assert kw['agent_id'] == 'home_composer'         # gated like any agent push


def test_run_home_compose_prefers_the_live_in_process_shell():
    svc = MagicMock()
    svc.compose_home_now.return_value = True
    reg = MagicMock()
    reg.get_or_none.return_value = svc
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=False), \
         patch('core.platform.registry.get_registry', return_value=reg):
        ok = run_home_compose(reason='idle_tick')
    assert ok is True
    svc.compose_home_now.assert_called_once_with(reason='idle_tick')


def test_run_home_compose_cross_process_posts_to_existing_route():
    payload = {'hero': None, 'rows': [{'title': 'R', 'accent': 'teal',
               'cards': [{'title': 'C', 'action': 'open'}]}]}
    reg = MagicMock()
    reg.get_or_none.return_value = None               # no in-process shell
    resp = MagicMock(); resp.status_code = 200
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=False), \
         patch('core.platform.registry.get_registry', return_value=reg), \
         patch.object(L, 'build_home_payload', return_value=payload), \
         patch('core.http_pool.pooled_post', return_value=resp) as post:
        ok = run_home_compose()
    assert ok is True
    url = post.call_args[0][0]
    assert url.endswith('/api/home/compose')          # the EXISTING route
    assert post.call_args.kwargs['json']['payload'] == payload


def test_run_home_compose_refuses_when_hive_halted():
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=True), \
         patch.object(L, 'build_home_payload') as build:
        ok = run_home_compose()
    assert ok is False
    build.assert_not_called()                         # no LLM spent while halted


# ── 5. the autonomous daemon DRIVES the producer when idle (no new loop) ──────

def _daemon():
    from integrations.agent_engine.agent_daemon import AgentDaemon
    return AgentDaemon()


def test_daemon_composes_home_when_idle():
    d = _daemon()
    called = {}
    import integrations.agent_engine.dispatch as dispatch
    with patch.object(dispatch, 'should_yield_to_user', return_value=False), \
         patch.object(L, 'run_home_compose',
                      side_effect=lambda reason: called.update(reason=reason)):
        d._spawn_home_compose_async()
        t = getattr(d, '_home_compose_thread', None)
        if t is not None:
            t.join(timeout=5)
    assert called.get('reason') == 'idle_tick'      # composed via the feed
    assert d._next_home_compose_at > 0              # cadence armed


def test_daemon_yields_to_an_active_user():
    d = _daemon()
    import integrations.agent_engine.dispatch as dispatch
    with patch.object(dispatch, 'should_yield_to_user', return_value=True), \
         patch.object(L, 'run_home_compose') as run:
        d._spawn_home_compose_async()
        t = getattr(d, '_home_compose_thread', None)
        if t is not None:
            t.join(timeout=5)
    run.assert_not_called()                         # the LLM is left to the user
    assert d._next_home_compose_at == 0.0           # not armed -> retries next tick


def test_daemon_respects_the_compose_cadence():
    import time as _t
    d = _daemon()
    d._next_home_compose_at = _t.time() + 9999       # composed very recently
    import integrations.agent_engine.dispatch as dispatch
    with patch.object(dispatch, 'should_yield_to_user', return_value=False), \
         patch.object(L, 'run_home_compose') as run:
        d._spawn_home_compose_async()
    run.assert_not_called()                          # within the interval -> skip


if __name__ == '__main__':
    import sys
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith('test_') and callable(v)]
    failed = 0
    for name, fn in fns:
        try:
            fn() if fn.__code__.co_argcount == 0 else fn(None)
            print('  OK  ', name)
        except Exception as e:
            failed += 1
            import traceback
            print(' FAIL ', name, '->', repr(e))
            traceback.print_exc()
    print('RESULT:', 'ALL PASS' if not failed else (str(failed) + ' FAILED'))
    sys.exit(1 if failed else 0)
