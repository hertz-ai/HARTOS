"""motion.layout: where and when each animated overlay plays (amendment A14).

Pure placement on a planned EDL (the golden reference EDL: two clips, a
5 s final-look reveal from 15 s, her face in the reveal at canvas px
369-711 x 576-960, a still logo top-left).  Every plan laid as 'motion'
overlays must pass contracts.validate_edl: inside SAFE_ZONE, the end card
never over her face in the final look's hold, the wipe over the reveal's
opening only.
"""
import pytest

from integrations.reel_studio import contracts as C
from integrations.reel_studio.motion import layout as L
from tests.unit.reel_media_fixtures import golden_edl

ALL = ('title', 'logo_sting', 'end_card', 'wipe')
CARD = ['Bridal makeup', 'Bridal from Rs 15k', 'DM for bookings']


def _laid(edl, plan):
    """The plan laid into the EDL as motion overlays (fake clip paths)."""
    out = dict(edl, overlays=list(edl['overlays']))
    for k, item in enumerate(plan.items):
        out['overlays'].append({'type': 'motion', 'template': item.template,
                                'src': 'C:/reel/work/motion_%d.mov' % k, 'box': list(item.box),
                                'from_s': item.from_s, 'to_s': item.to_s})
    return out


def _item(plan, template):
    return next(i for i in plan.items if i.template == template)


def _inside_zone(box):
    z = C.SAFE_ZONE
    return z['x_min'] <= box[0] and box[2] <= z['x_max'] and z['y_min'] <= box[1] and box[3] <= z['y_max']


def test_all_four_fit_the_golden_reel_and_the_edl_stays_valid():
    edl = golden_edl()
    plan = L.plan_motion(edl, ALL, hook_text='Bridal look in 20s', card_text=CARD)
    assert [i.template for i in plan.items] == ['logo_sting', 'title', 'end_card', 'wipe']
    assert plan.notes == []
    assert C.validate_edl(_laid(edl, plan)) == []
    for item in plan.items:
        w, h = item.box[2] - item.box[0], item.box[3] - item.box[1]
        assert w % 2 == 0 and h % 2 == 0 and w > 0 and h > 0, item


def test_the_title_sits_below_the_logo_and_plays_first():
    edl = golden_edl()
    plan = L.plan_motion(edl, ['title'], hook_text='Bridal look in 20s')
    title = _item(plan, 'title')
    logo_bottom = C.logo_box(edl['overlays'][1])[3]
    assert title.box[1] >= logo_bottom + L.GAP_PX
    assert _inside_zone(title.box)
    assert (title.from_s, title.to_s) == (0.0, L.TITLE_S)
    assert title.props['text'] == 'Bridal look in 20s'


def test_with_a_sting_the_title_sits_below_the_sting():
    plan = L.plan_motion(golden_edl(), ['title', 'logo_sting'], hook_text='Glow')
    sting, title = _item(plan, 'logo_sting'), _item(plan, 'title')
    assert title.box[1] >= sting.box[3] + L.GAP_PX


def test_the_sting_surrounds_her_logo_and_plays_before_it_stands_still():
    edl = golden_edl()
    sting = _item(L.plan_motion(edl, ['logo_sting']), 'logo_sting')
    x0, y0, x1, y1 = C.logo_box(edl['overlays'][1])
    assert sting.box[0] <= x0 and sting.box[2] >= x1 and sting.box[1] <= y0 and sting.box[3] >= y1
    assert _inside_zone(sting.box)
    assert (sting.from_s, sting.to_s) == (0.0, L.LOGO_STING_S) and sting.image == 'logo'


@pytest.mark.parametrize('template, change, why', [
    ('logo_sting', lambda e: e.update(overlays=[o for o in e['overlays'] if o['type'] != 'logo']),
     L.NO_LOGO),
    ('title', lambda e: None, L.NO_TEXT),
    ('end_card', lambda e: None, L.NO_CARD_TEXT),
])
def test_a_template_without_what_it_shows_is_left_out_and_said(template, change, why):
    edl = golden_edl()
    change(edl)
    plan = L.plan_motion(edl, [template], hook_text='', card_text=['', '  '])
    assert plan.items == []
    assert plan.notes == [L.SKIP_NOTE % (template, why)]


def test_the_end_card_plays_last_below_her_face():
    edl = golden_edl()
    card = _item(L.plan_motion(edl, ['end_card'], card_text=CARD), 'end_card')
    face = C.segment_face_box(edl, edl['segments'][-1])
    assert card.box[1] >= face[3] + L.GAP_PX, 'below her chin'
    assert (card.from_s, card.to_s) == (20.0 - L.END_CARD_S, 20.0)
    assert card.props['lines'] == CARD
    assert _inside_zone(card.box)


def test_with_no_room_beside_her_face_the_end_card_ends_before_the_hold():
    edl = golden_edl()
    # A face filling most of the frame: no band of the safe zone above or
    # below it holds a card.
    edl['segments'][-1]['face'] = {'x': 0.36, 'y': 0.1, 'w': 0.28, 'h': 0.85}
    plan = L.plan_motion(edl, ['end_card'], card_text=CARD)
    card = _item(plan, 'end_card')
    hold = C.QUALITY_BAR['reveal']['min_hold_s']
    assert card.to_s == pytest.approx(20.0 - hold)
    assert card.to_s - card.from_s == pytest.approx(L.END_CARD_S)
    assert C.validate_edl(_laid(edl, plan)) == []


def test_the_wipe_opens_the_reveal_from_her_bare_face():
    edl = golden_edl()
    plan = L.plan_motion(edl, ['wipe'])
    wipe = _item(plan, 'wipe')
    assert wipe.box == (0.0, 0.0, 1080.0, 1920.0)
    assert (wipe.from_s, wipe.to_s) == (15.0, 15.0 + L.WIPE_S)
    assert plan.before_segment == 0 and edl['segments'][0]['stage'] == 'bare_face'
    assert wipe.image == 'before'


@pytest.mark.parametrize('reveal_s, expect', [(2.5, 1.0), (2.0, None), (5.0, L.WIPE_S)])
def test_the_wipe_leaves_the_final_look_its_hold(reveal_s, expect):
    edl = golden_edl()
    last = edl['segments'][-1]
    last['out_start_s'] = 20.0 - reveal_s
    edl['segments'][-2]['out_end_s'] = last['out_start_s']
    plan = L.plan_motion(edl, ['wipe'])
    if expect is None:
        assert plan.items == [] and plan.notes == [L.SKIP_NOTE % ('wipe', L.REVEAL_TOO_SHORT)]
    else:
        wipe = _item(plan, 'wipe')
        assert wipe.to_s - wipe.from_s == pytest.approx(expect)
        assert 20.0 - wipe.to_s >= C.QUALITY_BAR['reveal']['min_hold_s'] - 1e-6


def test_without_a_finished_look_there_is_no_wipe():
    edl = golden_edl()
    edl['final_look_found'] = False
    plan = L.plan_motion(edl, ['wipe'])
    assert plan.items == [] and plan.notes == [L.SKIP_NOTE % ('wipe', L.NO_FINAL_LOOK)]


def test_card_lines_keep_her_words_short():
    assert L.card_lines(['bridal and reception makeup, saree draping', '', 'Rs 15k',
                         'DM for bookings', 'a fourth line']) == [
        'bridal and reception makeup', 'Rs 15k', 'DM for bookings']
    long = 'airbrush bridal makeup for destination weddings across Tamil Nadu'
    out = L.card_lines([long])[0]
    assert len(out) <= L.CARD_LINE_CHARS and long.startswith(out)
    assert L.card_lines([None, 7, '  ']) == []


def test_the_same_reel_gets_the_same_plan_and_unknown_templates_are_ignored():
    a = L.plan_motion(golden_edl(), ALL + ('confetti', 'title'), hook_text='Glow', card_text=CARD)
    b = L.plan_motion(golden_edl(), ALL, hook_text='Glow', card_text=CARD)
    assert a == b
