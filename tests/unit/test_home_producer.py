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
import json
from unittest.mock import MagicMock, patch

from integrations.agent_engine import liquid_ui_service as L
from integrations.agent_engine.liquid_ui_service import (
    HOME_ROW_ACCENTS, HOME_CARD_ACTIONS, HOME_PANEL_TARGETS,
    LiquidUIService, build_home_payload, run_home_compose,
    _deterministic_home_payload, _sanitize_home_payload, _llm_curate_home,
    _home_app_card, _home_app_cards, _home_agent_card)


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


def test_llm_curation_drives_per_row_accent_emphasis_and_mood():
    """The WIDENED compositional contract (§6a): the local LLM now colours EACH row
    (accent + emphasis) and names the ambient mood, not just {eyebrow, feature}. The
    richer JSON is accepted, applied onto the matching backbone row BY TITLE, and the
    whole thing survives the downstream sanitize guard unchanged."""
    backbone = _deterministic_home_payload(_ctx(spark=2140))
    titles = [r['title'] for r in backbone['rows']]
    target = 'Recipes' if 'Recipes' in titles else titles[-1]
    reply = json.dumps({
        'eyebrow': 'Up while you slept',
        'feature': target,
        'mood': 'aurora',                                  # ambient palette id
        'rows': [{'title': target, 'accent': 'violet', 'emphasis': 'flagship'}],
    })
    with patch('core.http_pool.pooled_post',
               return_value=_fake_resp(200, reply)):
        out = _llm_curate_home(_ctx(spark=2140), backbone, 6790)
    assert out is not None
    assert out['mood'] == 'aurora'                         # ambient mood forwarded
    lead = out['rows'][0]
    assert lead['title'] == target                         # feature row leads
    assert lead['accent'] == 'violet'                      # per-row accent applied
    assert lead.get('flagship') is True                    # emphasis applied
    # It passes the load-bearing guard intact (accent in-spectrum, mood -> slug).
    clean = _sanitize_home_payload(out)
    assert clean['rows'][0]['accent'] == 'violet'
    assert clean['mood'] == 'aurora'


def test_llm_curation_rejects_out_of_allowlist_accent_and_slugs_mood():
    """The widened authority is still bounded: a hallucinated accent outside
    HOME_ROW_ACCENTS is NOT applied (the backbone row's own accent stands, and the
    sanitizer would coerce any stray to 'teal'), and a dirty mood string is reduced
    to a safe slug the client checks against HART_PALETTES."""
    backbone = _deterministic_home_payload(_ctx(spark=2140))
    titles = [r['title'] for r in backbone['rows']]
    target = titles[0]                                     # already leads (no reorder)
    orig_accent = backbone['rows'][0]['accent']
    assert 'chartreuse' not in HOME_ROW_ACCENTS            # guard the fixture premise
    reply = json.dumps({
        'feature': target,
        'mood': 'Aurora Glow!!',                           # non-slug chars
        'rows': [{'title': target, 'accent': 'chartreuse'}],   # out of spectrum
    })
    with patch('core.http_pool.pooled_post',
               return_value=_fake_resp(200, reply)):
        out = _llm_curate_home(_ctx(spark=2140), backbone, 6790)
    # out-of-allowlist accent NOT applied — the backbone's own accent survives.
    assert out['rows'][0]['accent'] == orig_accent
    assert out['rows'][0]['accent'] in HOME_ROW_ACCENTS
    # dirty mood -> safe slug at the load-bearing sanitize guard.
    clean = _sanitize_home_payload(out)
    assert clean['mood'] == 'auroraglow'


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


# ── 6. per-type card image sourcing (#143 / steward d8) ──────────────────────

def test_home_app_card_prefers_bundled_logo_over_network():
    # OFFLINE-FIRST (#143 offline-art): a known app with a BUNDLED logo uses
    # card.image (same-origin, no network) and NEVER resolves the network poster,
    # so the marketplace/home shows real art with the internet OFF. The client
    # (makeCard) prefers card.image over card.image_url.
    with patch('integrations.agent_engine.shell_manifest.bundled_app_logo',
               return_value='/shell/static/app_art/apps/org.mozilla.firefox.svg') as bl, \
         patch('integrations.agent_engine.app_poster.resolve_app_poster') as rp:
        card = _home_app_card('org.mozilla.firefox', 'Firefox')
    bl.assert_called_once_with('org.mozilla.firefox')
    assert card['image'] == '/shell/static/app_art/apps/org.mozilla.firefox.svg'
    assert 'image_url' not in card
    rp.assert_not_called()                              # bundled logo wins
    assert card['action'] in HOME_CARD_ACTIONS
    assert card['target'] in HOME_PANEL_TARGETS         # opens the app store


def test_home_app_card_stamps_the_marketplace_poster():
    # With NO bundled logo, the producer sets card.image_url from the resolved
    # network poster; the client (W10 ImageCache) fetches the bytes once.
    with patch('integrations.agent_engine.shell_manifest.bundled_app_logo',
               return_value=None), \
         patch('integrations.agent_engine.app_poster.resolve_app_poster',
               return_value='https://dl.flathub.org/media/ff/poster.png') as r:
        card = _home_app_card('org.mozilla.firefox', 'Firefox')
    r.assert_called_once_with('org.mozilla.firefox', prefer='poster')
    assert card['image_url'] == 'https://dl.flathub.org/media/ff/poster.png'
    assert 'image' not in card
    assert card['action'] in HOME_CARD_ACTIONS
    assert card['target'] in HOME_PANEL_TARGETS         # opens the app store


def test_home_app_card_without_a_poster_falls_back_to_brand_art():
    # No bundled logo AND no network poster -> neither field set, so the client
    # paints the deterministic brand-art tile.
    with patch('integrations.agent_engine.shell_manifest.bundled_app_logo',
               return_value=None), \
         patch('integrations.agent_engine.app_poster.resolve_app_poster',
               return_value=None):
        card = _home_app_card('com.example.NoArt', 'No Art')
    assert 'image_url' not in card and 'image' not in card
    assert card['title'] == 'No Art' and card['icon'] == 'apps'


def test_apps_row_lists_installed_first_then_flagship_fill():
    installed = [{'name': 'My Editor', 'app_id': 'com.example.Editor',
                  'platform': 'flatpak'}]
    with patch('integrations.agent_engine.app_poster.resolve_app_poster',
               side_effect=lambda aid, prefer='poster':
                   ('https://art/' + aid) if aid else None):
        cards = _home_app_cards(installed)
    titles = [c['title'] for c in cards]
    assert titles[0] == 'My Editor'                     # the user's app leads
    assert 'Firefox' in titles                          # flagship fill follows
    assert len(cards) <= 8


def test_apps_row_appears_in_the_deterministic_payload():
    ctx = _ctx(spark=None)
    ctx['apps'] = [{'title': 'Firefox', 'topic': 'Firefox', 'icon': 'apps',
                    'action': 'open', 'target': 'app_store',
                    'image_url': 'https://dl.flathub.org/x.png'}]
    p = _deterministic_home_payload(ctx)
    apps = [r for r in p['rows'] if r['title'] == 'Apps']
    assert len(apps) == 1
    assert apps[0]['see_all'] == 'app_store'
    # The poster survives the sanitizer (http(s) allow-list).
    clean = _sanitize_home_payload(p)
    crow = [r for r in clean['rows'] if r['title'] == 'Apps'][0]
    assert crow['cards'][0]['image_url'] == 'https://dl.flathub.org/x.png'


def test_home_agent_card_prefers_central_art():
    # OFFLINE-FIRST (#143): the CENTRAL-owned agent image (resolved by name) is
    # consulted BEFORE the generated art and stamped on card.image (which the
    # client prefers), so an agent shows its real owned art with the net OFF and
    # the local generator is never even probed.
    with patch('integrations.agent_engine.app_poster.central_agent_art',
               return_value='/shell/agent-art/auto-research') as ca, \
         patch('integrations.agent_engine.app_poster.agent_art_url') as gen:
        card = _home_agent_card({'name': 'Auto Research', 'type': 'research_goal',
                                 'status': 'running'}, 'resume')
    ca.assert_called_once_with('Auto Research')
    assert card['image'] == '/shell/agent-art/auto-research'
    assert 'image_url' not in card
    gen.assert_not_called()                            # central art wins
    assert card['live'] == 'running'


def test_agent_card_uses_generated_art_when_a_generator_is_reachable():
    # No central image -> fall to the LOCAL generated art on card.image_url.
    with patch('integrations.agent_engine.app_poster.central_agent_art',
               return_value=None), \
         patch('integrations.agent_engine.app_poster.agent_art_url',
               return_value='https://gen.local/art/abc.png'):
        card = _home_agent_card({'name': 'Auto Research', 'type': 'research_goal',
                                 'status': 'running'}, 'resume')
    assert card['image_url'] == 'https://gen.local/art/abc.png'
    assert 'image' not in card
    assert card['live'] == 'running'


def test_agent_card_falls_back_to_brand_art_when_no_generator():
    # The HONEST default today: no central image AND no on-device generator ->
    # neither field -> the client composites HartBrandArt + the scrim + the name.
    with patch('integrations.agent_engine.app_poster.central_agent_art',
               return_value=None), \
         patch('integrations.agent_engine.app_poster.agent_art_url',
               return_value=None):
        card = _home_agent_card({'name': 'Tutor', 'type': 'tutor_goal'}, 'ask')
    assert 'image_url' not in card and 'image' not in card


def test_sanitizer_keeps_same_origin_card_image_but_drops_hostile():
    # card.image survives ONLY for the scoped served prefixes; a scheme-smuggling
    # or off-origin string is dropped (the client loads card.image directly).
    keep = L._home_sanitize_card({
        'title': 'Firefox', 'action': 'open', 'target': 'app_store',
        'image': '/shell/static/app_art/apps/org.mozilla.firefox.svg'})
    assert keep['image'] == '/shell/static/app_art/apps/org.mozilla.firefox.svg'
    agent = L._home_sanitize_card({
        'title': 'Auto Research', 'action': 'ask',
        'image': '/shell/agent-art/auto-research'})
    assert agent['image'] == '/shell/agent-art/auto-research'
    for bad in ('javascript:alert(1)', 'data:image/svg+xml,x',
                'https://evil.example/x.png', '/etc/passwd', '../secret'):
        c = L._home_sanitize_card({'title': 'X', 'action': 'open', 'image': bad})
        assert 'image' not in c


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
