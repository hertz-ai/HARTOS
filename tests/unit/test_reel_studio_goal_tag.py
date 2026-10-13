"""The 'reel_studio' goal tag: detected precisely, and it quiets look-alikes.

Reel Studio (plan WS-8a step 6).  Her agent's words trip three other tags by
accident -- "Tamil songs" reads as media, "brand"/"engagement" as marketing,
"follow up with brides" / "contact me" as outreach (plan C8) -- and each of
those tags hands the agent a Tier-2 tool family it has no use for (music
generation, campaigns, an email CRM) inside a context window the reel family
needs.  The suppression lives in resolve_goal_tags, so stored and detected
tags are treated alike, and it keeps a family when her words really ask for it
(compose a jingle, run a campaign, a cold email).

Also here: the creation hint a client may send (USER_ASSIGNABLE_GOAL_TAGS)
and the deterministic stamp the saved record gets (stamp_goal_tags).

Functional: real detect/resolve on real text, plus a precision run over the
in-repo goal corpus (every seeded bootstrap goal and every registered goal
type's built prompt), the same corpus test_commerce_wiring measures.
"""
import pytest

from integrations.agent_engine import goal_manager as gm
from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
from integrations.agent_engine.marketing_tools import (
    LOCAL_ONLY_GOAL_TAGS, USER_ASSIGNABLE_GOAL_TAGS, detect_goal_tags,
    drop_goal_tag_hint, held_goal_tag_hint, hold_goal_tag_hint, is_local_only,
    record_goal_text, resolve_goal_tags, stamp_goal_tags,
    user_assignable_goal_tags,
)

HER_GOAL = "Instagram reel editor for my bridal makeup, Tamil songs"


class TestDetection:

    def test_her_goal_is_a_reel_studio_goal_and_not_a_media_one(self):
        tags = resolve_goal_tags(None, HER_GOAL)
        assert 'reel_studio' in tags
        assert 'media' not in tags, (
            "'songs' tripped the media family (music generation) on a reel "
            "editor: got %r" % (tags,))

    @pytest.mark.parametrize('phrase', [
        'instagram reel', 'reel editor', 'reel studio', 'makeup reel',
        'bridal makeup', 'makeup artist', 'make a reel', 'edit my reel',
    ])
    def test_each_reel_phrase_detects_it(self, phrase):
        assert 'reel_studio' in detect_goal_tags('Please be my %s for clients' % phrase)

    def test_plural_and_case_still_match(self):
        assert 'reel_studio' in detect_goal_tags('Edit My Instagram Reels every week')
        assert 'reel_studio' in detect_goal_tags('cut my bridal makeup reels to the beat')

    @pytest.mark.parametrize('text', [
        'Freelance marketplace for designers',          # 'reel' inside a word
        'I do my own makeup before work',               # makeup alone
        'reeling from the news',
        'summarize this text file',
        'post a video to instagram',                    # instagram alone
    ])
    def test_look_alikes_do_not_detect_it(self, text):
        assert 'reel_studio' not in detect_goal_tags(text)

    def test_precision_across_the_in_repo_goal_corpus(self):
        """Measured, not assumed: no seeded goal and no goal type's prompt is
        a reel studio.  A hit here would make that agent local-only and drop
        its marketing/media/outreach tools."""
        docs = {}
        for s in SEED_BOOTSTRAP_GOALS:
            docs['seed:' + s.get('slug', '')] = ' '.join(
                str(s.get(k, '')) for k in ('title', 'description'))
        for t in gm.get_registered_types():
            try:
                docs['type:' + t] = str(gm.get_prompt_builder(t)(
                    {'title': t, 'description': '', 'goal_type': t,
                     'config': {}}) or '')
            except Exception:
                continue
        assert len(docs) > 50, 'the corpus did not load; this test would be vacuous'
        hits = sorted(n for n, text in docs.items()
                      if 'reel_studio' in detect_goal_tags(text))
        assert hits == []

    @pytest.mark.parametrize('text', [
        'fix the bug fix in repo', 'compose a song about rain',
        'Plan a marketing campaign', 'follow up with every lead in the crm',
        'summarize this text file', '',
    ])
    def test_a_goal_without_reel_studio_resolves_exactly_as_before(self, text):
        assert resolve_goal_tags(None, text) == detect_goal_tags(text)


class TestSuppression:

    def test_compose_a_song_alone_is_still_media(self):
        assert 'media' in resolve_goal_tags(None, 'compose a song')

    def test_follow_up_with_brides_is_not_outreach_for_her_agent(self):
        text = 'follow up with brides, contact me for bookings'
        assert 'outreach' in detect_goal_tags(text), 'precondition: the words trip it'
        tags = resolve_goal_tags(['reel_studio'], text)
        assert 'reel_studio' in tags
        assert 'outreach' not in tags

    def test_brand_and_engagement_are_not_marketing_for_her_agent(self):
        text = 'grow my brand engagement with better reels'
        assert 'marketing' in detect_goal_tags(text)
        assert 'marketing' not in resolve_goal_tags(['reel_studio'], text)

    @pytest.mark.parametrize('text,family', [
        ('compose a jingle for my bridal makeup reel', 'media'),
        ('add vocals to my makeup reel', 'media'),
        ('run a campaign for my bridal makeup studio', 'marketing'),
        ('advertise my makeup artist services', 'marketing'),
        ('send a cold email to wedding planners about my makeup reels', 'outreach'),
        ('track brides in a crm for my makeup reels', 'outreach'),
    ])
    def test_a_family_she_really_asks_for_is_kept(self, text, family):
        tags = resolve_goal_tags(None, text)
        assert 'reel_studio' in tags and family in tags, tags

    def test_stored_tags_are_suppressed_too(self):
        """Suppression lives in resolve_goal_tags, so a stored 'media' on a
        reel agent goes the same way a detected one does."""
        assert resolve_goal_tags(['reel_studio', 'media'], 'Tamil songs') == ['reel_studio']

    def test_an_action_object_as_the_text_keeps_the_stored_tag(self):
        """CREATE passes a helper.Action, whose str() is a repr: the stored
        tag is what decides there (plan C7)."""
        class Action:
            pass
        assert resolve_goal_tags(['reel_studio'], Action()) == ['reel_studio']

    def test_other_agents_are_untouched(self):
        text = 'follow up with leads and compose a jingle for the brand'
        assert resolve_goal_tags(['news'], text) == sorted(
            {'news'} | set(detect_goal_tags(text)))


class TestHintAndStamp:

    def test_the_assignable_and_local_only_sets(self):
        assert USER_ASSIGNABLE_GOAL_TAGS == frozenset({'reel_studio'})
        assert LOCAL_ONLY_GOAL_TAGS == frozenset({'reel_studio'})

    @pytest.mark.parametrize('raw,want', [
        (['reel_studio'], ['reel_studio']),
        (['reel_studio', 'marketing', 'reel_studio', 3, None], ['reel_studio']),
        (['marketing', 'coding'], []),
        ('reel_studio', []),          # a hint is a list, never a bare string
        ({'reel_studio': 1}, []),
        (None, []), ([], []), (42, []),
    ])
    def test_only_assignable_tags_survive_the_hint(self, raw, want):
        assert user_assignable_goal_tags(raw) == want

    def test_record_text_reads_goal_flows_and_extra_information(self):
        record = {
            'goal': 'Help the salon',
            'flows': [{'flow_name': 'Editing', 'sub_goal': 'cut the clips',
                       'actions': ['edit my reel', {'action': 'add the logo'}]},
                      'junk'],
            'extra_information': 'Tamil songs only',
            'personas': [{'name': 'Bride'}],
        }
        text = record_goal_text(record)
        for words in ('Help the salon', 'Editing', 'cut the clips', 'edit my reel',
                      'add the logo', 'Tamil songs only'):
            assert words in text
        assert record_goal_text(None) == '' and record_goal_text('junk') == ''

    def test_stamp_from_the_hint(self):
        assert stamp_goal_tags(['reel_studio'], {'goal': 'help my salon'}) == ['reel_studio']

    def test_stamp_from_the_saved_goal_without_a_hint(self):
        record = {'goal': 'Make bridal makeup reels from my clips', 'flows': []}
        assert stamp_goal_tags(None, record) == ['reel_studio']

    def test_no_stamp_for_any_other_agent(self):
        record = {'goal': 'Plan a marketing campaign and compose a jingle',
                  'flows': [{'actions': ['post to social media']}]}
        assert stamp_goal_tags(None, record) == []
        assert stamp_goal_tags(['marketing'], record) == []

    def test_a_hint_is_held_across_turns_until_dropped(self):
        key = 'u-hint_9001'
        drop_goal_tag_hint(key)
        assert hold_goal_tag_hint(key, ['reel_studio', 'marketing']) == ['reel_studio']
        assert hold_goal_tag_hint(key, None) == ['reel_studio'], (
            'a later turn without the hint must not lose it')
        assert held_goal_tag_hint(key) == ['reel_studio']
        assert held_goal_tag_hint('u-hint_other') == []
        drop_goal_tag_hint(key)
        assert held_goal_tag_hint(key) == []

    @pytest.mark.parametrize('tags,want', [
        (['reel_studio'], True), (('news', 'reel_studio'), True),
        ({'reel_studio'}, True), (['news'], False), ([], False),
        ('reel_studio', False), (None, False), ([None, 3], False),
    ])
    def test_local_only_is_read_from_a_collection_of_tags(self, tags, want):
        assert is_local_only(tags) is want
