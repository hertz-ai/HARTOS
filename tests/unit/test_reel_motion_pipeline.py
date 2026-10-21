"""A real reel with her animated overlays while Remotion is off (A14 fallback).

make_reel(motion=...) lays her overlay request between the plan and the
render (motion.compose.add_motion).  With HEVOLVE_REEL_MOTION unset -- the
default until the owner confirms Remotion's license -- nothing of Remotion
is asked for or run and the reel gets the plain-ffmpeg versions: her end
card is a boxed text at the end, clear of her face in the final hold, the
title is the hook text, no wipe.  The reel still passes every hard QA
check, its notes say 'motion:off', and her agent is told why.

Real ffmpeg on test_reel_pipeline.py's synthetic clips, its fake vision
model and recorded publisher.
"""
import pytest

from integrations.reel_studio import contracts as C
from tests.unit.reel_media_fixtures import requires_ffmpeg
from tests.unit.test_reel_pipeline import (  # noqa: F401  (fixture)
    PID, T0, UID, FakeVision, _env, _upload, clip_files)

CARD = ['Bridal makeup', 'Bridal from Rs 15k', 'DM for bookings']


@pytest.fixture(scope='module')
def made(clip_files, tmp_path_factory):
    from integrations.reel_studio import pipeline as P
    from integrations.reel_studio.media import render as R
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv('HEVOLVE_REEL_MOTION', raising=False)
        import integrations.agent_engine.capability_setup as capability_setup
        asked = []
        mp.setattr(capability_setup, 'ask_owner_for_setup',
                   lambda *a, **k: asked.append(a) or 'granted')
        store, pushes = _env(mp, tmp_path_factory.mktemp('motion_root'))
        rendered = []
        real_render = R.render_reel

        def spy_render(edl, *a, **k):
            rendered.append(edl)
            return real_render(edl, *a, **k)
        mp.setattr(R, 'render_reel', spy_render)
        uploaded = [_upload(store, clip_files['green'], captured_at=T0),
                    _upload(store, clip_files['blue'], captured_at=T0 + 300_000),
                    _upload(store, clip_files['red'], captured_at=T0 + 600_000)]
        result = P.make_reel(
            user_id=UID, prompt_id=PID, request_id='req-1',
            clips=[sf.to_dict() for sf in uploaded],
            style=C.make_style({'target_s': 5, 'hook_text': 'Bridal glow'}),
            logo_path=clip_files['logo'], backend=FakeVision(), encoder='libx264',
            motion={'overlays': ['title', 'end_card', 'wipe'], 'hook_text': 'Bridal glow',
                    'card_text': CARD})
        yield type('Made', (), {'result': result, 'edl': rendered[-1] if rendered else None,
                                'asked': asked})


@requires_ffmpeg
def test_the_reel_is_made_with_the_plain_versions_and_passes_qa(made):
    r = made.result
    assert r['status'] == 'ok', r
    assert r['qa'] == {'passed': True, 'failed': []}
    assert 'motion:off' in r['notes']
    assert made.asked == [], 'off: the owner is not asked'
    edl = made.edl
    assert C.validate_edl(edl) == []
    assert not [ov for ov in edl['overlays'] if ov['type'] == 'motion']
    texts = [ov for ov in edl['overlays'] if ov['type'] == 'text']
    assert texts[0]['text'] == 'Bridal glow', 'the title is the hook text'
    card = texts[-1]
    assert card['text'] == ' · '.join(CARD)
    assert card['to_s'] == pytest.approx(edl['duration_s'])
    assert card['box'] is True


@requires_ffmpeg
def test_her_agent_is_told_the_overlays_wait_for_the_license(made):
    message = made.result['message']
    assert 'license' in message.lower() and 'plain' in message.lower()
