"""reel_motion: the reel tool her agent asks for animated overlays with (A14).

"Add an animated end card with my price to my reels", "a logo sting",
"stop the overlays".  Registered on demand with reel_watchlist (no schema up
front: request_tools attaches it by its docstring's first line), on her
Reel Studio agent only.  Deterministic: it keeps her choice in her voice
profile (motion_overlays, through voice_profile.update_field, the one
writer) and says where Remotion stands on this computer; choosing overlays
asks for the one-time setup through motion.runtime.ensure_ready (consent
card, free-disk check and a bounded wait in RuntimeToolManager).
reel_make then hands every reel her choice, her hook text and her end-card
words (services, price band, ask) for the pipeline's motion step.

Remotion itself is OFF by default (HEVOLVE_REEL_MOTION unset): her choice is
still kept and the reels get the plain-ffmpeg versions, and the reply says
the owner has to confirm the license first.

The fixtures are test_reel_tools.py's (real store, voice profile and
pipeline around a fake production).
"""
import json

import pytest

from tests.unit.test_reel_tools import _ok, _tools, _upload, env, UID  # noqa: F401  (fixture)

TOOL = 'reel_motion'


def _reply(t, **kw):
    return t.run[TOOL](**kw)


def _profile():
    from integrations.reel_studio import voice_profile
    return voice_profile.load_voice(UID)


@pytest.fixture
def remotion(monkeypatch):
    """motion.runtime as the tool sees it: ensure_ready and status recorded
    and scripted (the flag is unset unless a test sets it)."""
    from integrations.reel_studio.motion import runtime
    calls = []
    answer = {'state': runtime.OFF, 'detail': '', 'status': runtime.OFF}

    def ensure_ready(*, wait_s, progress=None):
        calls.append(('ensure', wait_s))
        if progress is not None:
            progress('Setting up animated overlays...')
        return runtime.Readiness(answer['state'], answer['detail'])
    monkeypatch.setattr(runtime, 'ensure_ready', ensure_ready)
    monkeypatch.setattr(runtime, 'status', lambda: answer['status'])
    return type('R', (), {'calls': calls, 'answer': answer, 'runtime': runtime})


def test_it_is_on_demand_on_her_agent_with_a_findable_first_line(env):
    from integrations.reel_studio.tools import REEL_ON_DEMAND_TOOL_NAMES, REEL_TOOL_NAMES
    assert TOOL in REEL_ON_DEMAND_TOOL_NAMES and TOOL not in REEL_TOOL_NAMES
    t = _tools(env)
    assert TOOL in t.assistant.exe and TOOL not in t.assistant.llm and TOOL not in t.helper.llm
    first = (t.run[TOOL].__doc__ or '').strip().split('\n')[0]
    for word in ('animated', 'end card', 'logo', 'title'):
        assert word in first.lower(), first


def test_with_remotion_off_by_default_her_choice_is_kept_and_the_reply_says_why(env, monkeypatch):
    """The real runtime and row: HEVOLVE_REEL_MOTION unset."""
    monkeypatch.delenv('HEVOLVE_REEL_MOTION', raising=False)
    import integrations.agent_engine.capability_setup as capability_setup
    asked = []
    monkeypatch.setattr(capability_setup, 'ask_owner_for_setup',
                        lambda *a, **k: asked.append(a) or 'granted')
    t = _tools(env)
    reply = _ok(_reply(t, overlays='end card, Logo-Sting'))
    assert reply['overlays'] == ['end_card', 'logo_sting']
    assert _profile()['motion_overlays'] == ['end_card', 'logo_sting']
    assert reply['animated'] == 'off'
    assert 'license' in reply['message'].lower() and 'plain' in reply['message'].lower()
    assert asked == [], 'off: the owner is not asked'


def test_an_unknown_overlay_is_refused_and_nothing_is_saved(env, remotion):
    from core.constants import tool_reply_failed
    t = _tools(env)
    reply = _reply(t, overlays='confetti, title')
    assert tool_reply_failed(reply)
    for name in ('title', 'logo_sting', 'end_card', 'wipe'):
        assert name in reply
    assert 'motion_overlays' not in _profile() and remotion.calls == []


@pytest.mark.parametrize('word', ['none', 'off', 'stop'])
def test_none_stops_them(env, remotion, word):
    t = _tools(env)
    _reply(t, overlays='title')
    reply = _ok(_reply(t, overlays=word))
    assert reply['overlays'] == [] and 'motion_overlays' not in _profile()


def test_asking_with_nothing_lists_her_choice_and_sets_nothing_up(env, remotion):
    t = _tools(env)
    _reply(t, overlays='wipe')
    remotion.calls.clear()
    remotion.answer['status'] = remotion.runtime.NOT_SET_UP
    reply = _ok(_reply(t))
    assert reply['overlays'] == ['wipe'] and reply['animated'] == 'not_set_up'
    assert remotion.calls == []
    assert set(reply['templates']) == {'title', 'logo_sting', 'end_card', 'wipe'}


@pytest.mark.parametrize('state, said', [
    ('ready', 'next reel'), ('asked', 'consent card'), ('declined', 'said no'),
    ('setting_up', 'being set up'), ('unavailable', 'nobody'), ('failed', 'could not'),
])
def test_choosing_overlays_asks_for_the_setup_and_says_where_it_stands(env, remotion, monkeypatch,
                                                                        state, said):
    remotion.answer['state'] = state
    shown = []
    from core.peer_link import crossbar_publish
    monkeypatch.setattr(crossbar_publish, 'publish_chat_stage',
                        lambda stage, **kw: shown.append((stage, kw)) or True)
    t = _tools(env)
    reply = _ok(_reply(t, overlays='title'))
    assert reply['animated'] == state and said in reply['message'], reply['message']
    [(_, wait)] = remotion.calls
    assert 0 < wait <= 120, 'a bounded wait'
    assert shown and shown[0][1]['user_id'] == UID and shown[0][1]['request_id'] == 'req-1'


def test_reel_make_hands_every_reel_her_choice_her_hook_and_her_card_words(env, remotion):
    from integrations.reel_studio import voice_profile
    for field, value in (('services', 'bridal and reception makeup, saree draping'),
                         ('price_band', 'bridal from Rs 15k'), ('cta', 'DM for bookings')):
        voice_profile.update_field(UID, field, value)
    t = _tools(env)
    _reply(t, overlays='end_card, title')
    _upload(env, b'clip-a' * 100)
    _ok(t.run['reel_make'](hook_text='Bridal glow in 20s'))
    motion = env.produced[0]['motion']
    assert motion['overlays'] == ['end_card', 'title']
    assert motion['hook_text'] == 'Bridal glow in 20s'
    assert motion['card_text'] == ['bridal and reception makeup, saree draping',
                                   'bridal from Rs 15k', 'DM for bookings']


def test_without_a_choice_reel_make_asks_for_no_overlays(env):
    t = _tools(env)
    _upload(env, b'clip-b' * 100)
    _ok(t.run['reel_make']())
    assert 'motion' not in env.produced[0]


def test_her_profile_refuses_an_unknown_overlay_by_any_way_in(env):
    from integrations.reel_studio import voice_profile
    with pytest.raises(ValueError):
        voice_profile.update_field(UID, 'motion_overlays', 'title, confetti')
    assert voice_profile.update_field(UID, 'motion_overlays', 'Title, WIPE')['motion_overlays'] == \
        ['title', 'wipe']
