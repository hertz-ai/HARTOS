"""The 'reel_studio' goal tag: precise, stored-only in its effects, inert on central.

Reel Studio (plan WS-8a step 6, amendment A8).  Three rules:

* DETECTION fires only on words that mean editing her OWN videos ('reel
  studio', 'edit my reel', 'edit my videos', 'edit my clips').  Words about an
  audience or marketing content -- 'makeup artist', 'bridal makeup',
  'instagram reel', 'reel editor' -- do not: a commerce/marketing/outreach goal
  that sources makeup artists as leads or posts reels to sell them a product
  is not a reel studio, and must keep its marketing, outreach and commerce
  tools.  Detection grants the reel tools and nothing else.
* Only the STORED tag -- stamped on a record solely from her explicit
  creation hint -- quiets the look-alike families her agent's words trip by
  accident ("Tamil songs" reads as media, "brand"/"engagement" as marketing,
  "follow up with brides" as outreach; plan C8), and keeps the agent on this
  node.  A family she really asks for (compose a jingle, run a campaign, a
  cold email) is kept.
* On the CENTRAL node (get_node_tier() == 'central') the tag is inert: no reel
  tools, no suppression, no local-only pin, no stamp.

Functional: real detect/resolve/stamp on real text, plus a precision run over
the in-repo goal corpus (every seeded bootstrap goal and every registered goal
type's built prompt, the corpus test_commerce_wiring measures) together with
commerce-lane goals.  The node tier is pinned per test by replacing
security.key_delegation.get_node_tier, the one tier source.
"""
import pytest

from core.intelligence_preference import IntelligencePreference
from integrations.agent_engine import goal_manager as gm
from integrations.agent_engine import marketing_tools
from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
from integrations.agent_engine.marketing_tools import (
    GOAL_TAG_PREFERENCES, USER_ASSIGNABLE_GOAL_TAGS, detect_goal_tags,
    drop_goal_tag_hint, goal_tags_preference, held_goal_tag_hint,
    hold_goal_tag_hint, is_local_only, resolve_goal_tags, stamp_goal_tags,
    user_assignable_goal_tags,
)

#: Her agent's own words: they name her reels and trip media (songs).
HER_GOAL = "Edit my reels for my bridal clients, Tamil songs"

#: Goals of a commerce lane whose customers are makeup artists: none of them
#: is a reel studio, whatever words about makeup or reels they use.
COMMERCE_LANE = [
    'Find bridal makeup artists in Chennai and pitch our lash extensions',
    'Source makeup artists in Mumbai as leads for eyelash sales',
    'Post Instagram reels showcasing our lash extensions to makeup artists',
    'hire a reel editor for our lash brand',
    'Run a lead gen campaign for bridal makeup artists and send a cold email '
    'about our lash extensions',
    'Onboard my store on McGroce and post an Instagram reel about the new SKU '
    'to makeup artists',
]

#: The phrases that used to detect reel_studio and must not any more.
RETIRED_PHRASES = ['makeup artist', 'bridal makeup', 'instagram reel',
                   'makeup reel', 'make a reel', 'reel editor']


def _pin_tier(monkeypatch, tier):
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: tier)


@pytest.fixture(autouse=True)
def desktop(monkeypatch):
    """Every test runs on a desktop (flat) node unless it says central."""
    _pin_tier(monkeypatch, 'flat')


@pytest.fixture
def central(monkeypatch):
    _pin_tier(monkeypatch, 'central')


class TestDetection:

    @pytest.mark.parametrize('phrase', [
        'reel studio', 'edit my reel', 'edit my videos', 'edit my clips',
    ])
    def test_each_own_video_phrase_detects_it(self, phrase):
        assert 'reel_studio' in detect_goal_tags('Please %s for my clients' % phrase)

    @pytest.mark.parametrize('phrase', RETIRED_PHRASES)
    def test_audience_and_content_words_do_not_detect_it(self, phrase):
        assert 'reel_studio' not in detect_goal_tags('Please be my %s for clients' % phrase)

    def test_plural_and_case_still_match(self):
        assert 'reel_studio' in detect_goal_tags('Edit My Reels every week')
        assert 'reel_studio' in detect_goal_tags('open my REEL STUDIO')

    @pytest.mark.parametrize('text', [
        'Freelance marketplace for designers',          # 'reel' inside a word
        'I do my own makeup before work',               # makeup alone
        'reeling from the news',
        'summarize this text file',
        'post a video to instagram',                    # instagram alone
        'edit my clipboard history',                    # not 'edit my clips'
        'edit my video game save',                      # not 'edit my videos'
    ])
    def test_look_alikes_do_not_detect_it(self, text):
        assert 'reel_studio' not in detect_goal_tags(text)

    @pytest.mark.parametrize('text', COMMERCE_LANE)
    def test_a_commerce_lane_goal_is_not_a_reel_studio(self, text):
        """No reel tag, and every family its words detect is kept."""
        tags = resolve_goal_tags(None, text)
        assert 'reel_studio' not in tags
        assert tags == detect_goal_tags(text)

    def test_the_commerce_lane_keeps_marketing_outreach_and_commerce(self):
        text = COMMERCE_LANE[4] + '. ' + COMMERCE_LANE[5]
        tags = resolve_goal_tags(None, text)
        assert {'marketing', 'outreach', 'commerce'} <= set(tags), tags
        assert 'reel_studio' not in tags

    def test_precision_across_the_in_repo_goal_corpus(self):
        """Measured, not assumed: no seeded goal, no goal type's prompt and no
        commerce-lane goal is a reel studio.  A hit here would hand that agent
        the reel tools."""
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
        for i, text in enumerate(COMMERCE_LANE):
            docs['commerce_lane:%d' % i] = text
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

    def test_her_stored_agent_is_a_reel_studio_and_not_a_media_one(self):
        tags = resolve_goal_tags(['reel_studio'], HER_GOAL)
        assert 'reel_studio' in tags
        assert 'media' not in tags, (
            "'songs' tripped the media family (music generation) on her reel "
            "agent: got %r" % (tags,))

    def test_detection_alone_quiets_nothing(self):
        """No stored tag: the reel tools are added, nothing is taken away."""
        assert 'media' in detect_goal_tags(HER_GOAL), 'precondition: songs trip media'
        tags = resolve_goal_tags(None, HER_GOAL)
        assert 'reel_studio' in tags and 'media' in tags, tags
        text = 'reel studio for my salon: follow up with brides, contact me'
        assert {'reel_studio', 'outreach'} <= set(resolve_goal_tags(None, text))

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
        tags = resolve_goal_tags(['reel_studio'], text)
        assert 'reel_studio' in tags and family in tags, tags

    def test_stored_look_alikes_are_suppressed_too(self):
        """A stored 'media' on her agent goes the same way a detected one does."""
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

    @pytest.mark.parametrize('text', COMMERCE_LANE)
    def test_a_stored_commerce_lane_agent_keeps_its_families(self, text):
        stored = ['commerce', 'marketing', 'outreach']
        assert resolve_goal_tags(stored, text) == sorted(
            set(stored) | set(detect_goal_tags(text)))


class TestCentralNode:
    """On central the tag means nothing: the network's own goals run there."""

    def test_a_stored_tag_grants_no_reel_tools_and_quiets_nothing(self, central):
        tags = resolve_goal_tags(['reel_studio', 'media'], HER_GOAL)
        assert 'reel_studio' not in tags
        assert 'media' in tags, tags

    def test_a_detected_tag_grants_no_reel_tools(self, central):
        assert resolve_goal_tags(None, 'edit my clips') == []
        text = 'edit my reels, follow up with brides, contact me'
        assert resolve_goal_tags(None, text) == [
            t for t in detect_goal_tags(text) if t != 'reel_studio']

    def test_no_family_is_suppressed(self, central):
        text = 'follow up with brides, grow my brand engagement, Tamil songs'
        assert resolve_goal_tags(['reel_studio'], text) == sorted(detect_goal_tags(text))
        assert {'marketing', 'outreach', 'media'} <= set(detect_goal_tags(text))

    def test_nothing_is_local_only(self, central):
        assert is_local_only(['reel_studio']) is False
        assert is_local_only(('news', 'reel_studio')) is False
        assert goal_tags_preference(['reel_studio']) is None

    def test_nothing_is_stamped(self, central):
        assert stamp_goal_tags(['reel_studio']) is None
        assert stamp_goal_tags(['reel_studio'], ['news']) is None

    @pytest.mark.parametrize('text', COMMERCE_LANE)
    def test_commerce_lane_goals_resolve_exactly_as_detected(self, central, text):
        assert resolve_goal_tags(None, text) == detect_goal_tags(text)

    def test_the_tier_is_asked_at_call_time(self, monkeypatch):
        assert is_local_only(['reel_studio']) is True
        _pin_tier(monkeypatch, 'central')
        assert is_local_only(['reel_studio']) is False
        _pin_tier(monkeypatch, 'regional')
        assert is_local_only(['reel_studio']) is True


class TestHintAndStamp:

    def test_the_assignable_set(self):
        assert USER_ASSIGNABLE_GOAL_TAGS == frozenset({'reel_studio'})

    def test_a_reel_agent_keeps_to_this_node_in_the_canonical_vocabulary(self):
        """Amendment A9 e: the tag sets a core.intelligence_preference value,
        not a rule and a bare 'local_only' string of its own."""
        assert dict(GOAL_TAG_PREFERENCES) == {
            'reel_studio': IntelligencePreference.LOCAL_ONLY}
        assert not hasattr(marketing_tools, 'LOCAL_ONLY_GOAL_TAGS')
        assert goal_tags_preference(['news', 'reel_studio']) is IntelligencePreference.LOCAL_ONLY

    @pytest.mark.parametrize('tags', [['news'], [], None, 'reel_studio', [None, 3]])
    def test_any_other_agent_sets_no_preference(self, tags):
        assert goal_tags_preference(tags) is None

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

    def test_stamp_from_the_hint(self):
        assert stamp_goal_tags(['reel_studio']) == ['reel_studio']
        assert stamp_goal_tags(['reel_studio', 'marketing']) == ['reel_studio']
        assert stamp_goal_tags(['reel_studio'], ['news', 'reel_studio']) == [
            'news', 'reel_studio']

    @pytest.mark.parametrize('hint', [None, [], ['marketing'], 'reel_studio'])
    def test_no_stamp_without_an_explicit_hint(self, hint):
        assert stamp_goal_tags(hint) is None
        assert stamp_goal_tags(hint, ['news', 'news']) is None, (
            "a record with no assignable tag of its own is left exactly as it is")

    def test_the_gathered_records_own_reel_tag_is_not_trusted(self):
        """The gather LLM's JSON is the record; a reel_studio it wrote without
        her hint is dropped, its other tags kept."""
        assert stamp_goal_tags(None, ['reel_studio', 'news']) == ['news']
        assert stamp_goal_tags(None, ['reel_studio']) == []
        assert stamp_goal_tags(['marketing'], ['reel_studio']) == []

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
