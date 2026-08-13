"""
Tests for HART Onboarding — "Light Your HART" ceremony.

Covers:
  - Element/spirit assignment from dimensions
  - HART tag generation
  - Onboarding session state machine
  - Name generation (fallback path)
  - Emoji combo generation
  - API route wiring
  - Identity unification (element + spirit + name)
"""

import json
import sys
import unittest
from unittest.mock import patch, MagicMock

# Legacy safety-net: generate_hart_name no longer imports
# hart_intelligence_entry on the hot path (the heavy langchain_classic +
# transformers chain is banned from onboarding — see hart_onboarding.py
# ::_llm_generate_direct docstring). This mock is kept so the existing
# `@patch.dict(sys.modules, {'hart_intelligence_entry': _mock_lgapi})`
# decorators below don't break, and to guard against any future regression
# that reintroduces the import.
_mock_lgapi = MagicMock()
_mock_lgapi.get_llm.side_effect = Exception("mocked — no LLM in tests")

from hart_onboarding import (
    ELEMENTS, SPIRITS,
    assign_element_spirit, build_hart_tag,
    generate_emoji_combo, generate_hart_name,
    _merge_dimensions, _fallback_names, _emergency_name,
    PASSION_OPTIONS, ESCAPE_OPTIONS,
    CONVERSATION_SCRIPT, ACKNOWLEDGMENTS_PASSION, ACKNOWLEDGMENT_ESCAPE,
    HARTOnboardingSession, HARTNameRegistry,
    get_or_create_session, remove_session,
    has_hart_name, get_hart_profile,
)


# ═══════════════════════════════════════════════════════════════
# Element & Spirit Assignment
# ═══════════════════════════════════════════════════════════════

class TestElementSpirit(unittest.TestCase):
    """Element and spirit assignment from emotional dimensions."""

    def test_creative_top_gives_neon(self):
        dims = {'creative': 0.9, 'curious': 0.6, 'social': 0.3}
        element, spirit = assign_element_spirit(dims)
        self.assertEqual(element, 'neon')

    def test_builder_top_gives_iron(self):
        dims = {'builder': 0.9, 'curious': 0.6}
        element, spirit = assign_element_spirit(dims)
        self.assertEqual(element, 'iron')

    def test_second_dim_gives_spirit(self):
        dims = {'creative': 0.9, 'introspective': 0.7, 'social': 0.3}
        element, spirit = assign_element_spirit(dims)
        self.assertEqual(element, 'neon')
        self.assertEqual(spirit, 'owl')  # introspective -> owl

    def test_social_builder_combo(self):
        dims = {'social': 0.9, 'builder': 0.8}
        element, spirit = assign_element_spirit(dims)
        self.assertEqual(element, 'ember')
        self.assertEqual(spirit, 'wolf')

    def test_empty_dimensions_fallback(self):
        element, spirit = assign_element_spirit({})
        self.assertEqual(element, 'void')
        self.assertEqual(spirit, 'fox')

    def test_single_dimension(self):
        dims = {'curious': 0.9}
        element, spirit = assign_element_spirit(dims)
        self.assertEqual(element, 'ether')
        self.assertEqual(spirit, 'fox')  # default second

    def test_all_elements_mapped(self):
        for dim_key in ELEMENTS:
            # Use a different key for second dim to avoid collision
            second = 'curious' if dim_key != 'curious' else 'social'
            dims = {dim_key: 0.9, second: 0.1}
            e, _ = assign_element_spirit(dims)
            self.assertEqual(e, ELEMENTS[dim_key])

    def test_all_spirits_mapped(self):
        for dim_key in SPIRITS:
            # Use a different key for top dim to avoid collision
            top = 'grounded' if dim_key != 'grounded' else 'calm'
            dims = {top: 0.95, dim_key: 0.8}
            _, s = assign_element_spirit(dims)
            self.assertEqual(s, SPIRITS[dim_key])


class TestHARTTag(unittest.TestCase):
    """HART tag generation."""

    def test_build_tag(self):
        tag = build_hart_tag('neon', 'owl', 'lumira')
        self.assertEqual(tag, '@owl.lumira.neon')

    def test_build_tag_all_lower(self):
        tag = build_hart_tag('iron', 'wolf', 'synthar')
        self.assertEqual(tag, '@wolf.synthar.iron')

    def test_tag_format_consistent(self):
        tag = build_hart_tag('ember', 'dolphin', 'auren')
        self.assertTrue(tag.startswith('@'))
        parts = tag[1:].split('.')
        self.assertEqual(len(parts), 3)


# ═══════════════════════════════════════════════════════════════
# Name Generation
# ═══════════════════════════════════════════════════════════════

class TestNameGeneration(unittest.TestCase):
    """Name generation (LLM-free fallback path)."""

    def test_fallback_names_returns_list(self):
        dims = {'creative': 0.9, 'introspective': 0.6}
        names = _fallback_names(dims, set())
        self.assertIsInstance(names, list)
        self.assertGreater(len(names), 0)

    def test_fallback_excludes_existing(self):
        dims = {'creative': 0.9}
        all_names = set(_fallback_names(dims, set()))
        # Remove one and check it's excluded
        if all_names:
            taken = {all_names.pop()}
            filtered = _fallback_names(dims, taken)
            for n in filtered:
                self.assertNotIn(n, taken)

    def test_fallback_deterministic(self):
        dims = {'builder': 0.9, 'curious': 0.6}
        a = _fallback_names(dims, set())
        b = _fallback_names(dims, set())
        self.assertEqual(a, b)

    def test_emergency_name_format(self):
        name = _emergency_name()
        self.assertTrue(len(name) >= 4)
        self.assertTrue(name.isalpha())

    @patch.dict(sys.modules, {'hart_intelligence_entry': _mock_lgapi})
    def test_generate_without_llm(self):
        result = generate_hart_name('en', 'music_art', 'quiet_alone')
        self.assertIn('name', result)
        self.assertIn('element', result)
        self.assertIn('spirit', result)
        self.assertIn('hart_tag', result)
        self.assertTrue(result['hart_tag'].startswith('@'))

    @patch.dict(sys.modules, {'hart_intelligence_entry': _mock_lgapi})
    def test_generate_includes_element_spirit(self):
        result = generate_hart_name('en', 'building_coding', 'ideas_possibilities')
        self.assertIn('element', result)
        self.assertIn('spirit', result)
        self.assertIn('hart_tag', result)
        # builder top -> iron, curious second -> fox
        self.assertEqual(result['element'], 'iron')


class TestEmojiCombo(unittest.TestCase):
    """Emoji combo generation."""

    def test_returns_string(self):
        dims = {'creative': 0.9, 'introspective': 0.6}
        combo = generate_emoji_combo('en_US', dims)
        self.assertIsInstance(combo, str)
        self.assertGreater(len(combo), 0)

    def test_deterministic(self):
        dims = {'curious': 0.9}
        a = generate_emoji_combo('en_IN', dims)
        b = generate_emoji_combo('en_IN', dims)
        self.assertEqual(a, b)

    def test_different_locale_different_flag(self):
        dims = {'creative': 0.9}
        india = generate_emoji_combo('en_IN', dims)
        usa = generate_emoji_combo('en_US', dims)
        # Same feeling emoji, different flag
        self.assertNotEqual(india, usa)


# ═══════════════════════════════════════════════════════════════
# Dimension Merging
# ═══════════════════════════════════════════════════════════════

class TestDimensionMerging(unittest.TestCase):
    """Merging passion + escape dimensions."""

    def test_merge_basic(self):
        dims = _merge_dimensions('music_art', 'quiet_alone')
        self.assertIn('creative', dims)
        self.assertIn('introspective', dims)

    def test_merge_takes_max(self):
        # music_art has introspective=0.6, quiet_alone has introspective=0.9
        dims = _merge_dimensions('music_art', 'quiet_alone')
        self.assertAlmostEqual(dims['introspective'], 0.9)

    def test_merge_unknown_keys(self):
        dims = _merge_dimensions('nonexistent', 'nonexistent')
        self.assertEqual(dims, {})

    def test_merge_all_passion_escape_combos(self):
        for p in PASSION_OPTIONS:
            for e in ESCAPE_OPTIONS:
                dims = _merge_dimensions(p['key'], e['key'])
                self.assertIsInstance(dims, dict)
                self.assertGreater(len(dims), 0)


# ═══════════════════════════════════════════════════════════════
# Session State Machine
# ═══════════════════════════════════════════════════════════════

class TestOnboardingSession(unittest.TestCase):
    """Onboarding session state machine."""

    def setUp(self):
        self.session = HARTOnboardingSession(user_id='test_user')

    def test_initial_phase_is_language(self):
        self.assertEqual(self.session.phase, 'language')

    def test_select_language_advances_to_greeting(self):
        result = self.session.advance(
            action='select_language',
            data={'language': 'ta'}
        )
        self.assertEqual(self.session.phase, 'greeting')
        self.assertEqual(self.session.language, 'ta')
        self.assertIn('pa_lines', result)

    def test_greeting_advances_to_passion(self):
        self.session.advance(action='select_language', data={'language': 'en'})
        result = self.session.advance()  # greeting -> passion
        self.assertEqual(self.session.phase, 'passion')

    def test_passion_answer_advances(self):
        self.session.advance(action='select_language', data={'language': 'en'})
        self.session.advance()  # greeting
        result = self.session.advance(action='answer', data={'key': 'music_art'})
        self.assertEqual(self.session.phase, 'ack_passion')
        self.assertEqual(self.session.passion_key, 'music_art')

    @patch.dict(sys.modules, {'hart_intelligence_entry': _mock_lgapi})
    @patch.object(HARTNameRegistry, 'get_all_names', return_value=set())
    def test_full_flow_to_reveal(self, _mock_names):
        s = self.session
        s.advance(action='select_language', data={'language': 'en'})
        s.advance()  # greeting -> passion
        s.advance(action='answer', data={'key': 'building_coding'})
        s.advance()  # ack_passion -> escape
        s.advance(action='answer', data={'key': 'ideas_possibilities'})
        s.advance()  # ack_escape -> pre_reveal
        result = s.advance()  # pre_reveal -> reveal
        self.assertEqual(s.phase, 'reveal')
        self.assertIn('hart_name', result)
        self.assertIn('hart_tag', result)
        self.assertIn('element', result)
        self.assertIn('spirit', result)

    @patch.dict(sys.modules, {'hart_intelligence_entry': _mock_lgapi})
    @patch.object(HARTNameRegistry, 'get_all_names', return_value=set())
    def test_reveal_includes_hart_tag(self, _mock_names):
        s = self.session
        s.advance(action='select_language', data={'language': 'en'})
        s.advance()
        s.advance(action='answer', data={'key': 'music_art'})
        s.advance()
        s.advance(action='answer', data={'key': 'quiet_alone'})
        s.advance()
        result = s.advance()
        self.assertTrue(result.get('hart_tag', '').startswith('@'))

    def test_voice_transcript_captured(self):
        s = self.session
        s.advance(action='select_language', data={'language': 'en'})
        s.advance()
        s.advance(action='answer', data={
            'key': 'music_art',
            'voice_transcript': 'I love playing guitar'
        })
        self.assertEqual(s.voice_transcript, 'I love playing guitar')

    def test_elapsed_ms_increases(self):
        import time
        result1 = self.session.advance(action='select_language', data={'language': 'en'})
        time.sleep(0.01)
        result2 = self.session.advance()
        self.assertGreater(result2['elapsed_ms'], result1['elapsed_ms'])

    def test_response_has_language(self):
        result = self.session.advance(action='select_language', data={'language': 'ja'})
        self.assertEqual(result['language'], 'ja')


class TestSessionStorage(unittest.TestCase):
    """Session creation and cleanup."""

    def test_get_or_create(self):
        s1 = get_or_create_session('test_999')
        s2 = get_or_create_session('test_999')
        self.assertIs(s1, s2)
        remove_session('test_999')

    def test_remove_session(self):
        get_or_create_session('test_998')
        remove_session('test_998')
        s = get_or_create_session('test_998')
        self.assertEqual(s.phase, 'language')
        remove_session('test_998')

    def test_different_users_different_sessions(self):
        s1 = get_or_create_session('user_a')
        s2 = get_or_create_session('user_b')
        self.assertIsNot(s1, s2)
        remove_session('user_a')
        remove_session('user_b')


# ═══════════════════════════════════════════════════════════════
# Conversation Script Completeness
# ═══════════════════════════════════════════════════════════════

class TestConversationScript(unittest.TestCase):
    """All conversation lines exist in all languages."""

    REQUIRED_LANGS = ['en', 'ta', 'hi', 'es', 'fr', 'ja', 'ko', 'zh']

    def test_all_phases_have_lines(self):
        for phase in ['language_prompt', 'greeting', 'question_passion',
                       'question_escape', 'pre_reveal', 'reveal_intro',
                       'post_reveal']:
            self.assertIn(phase, CONVERSATION_SCRIPT, f"Missing phase: {phase}")

    def test_all_phases_have_required_languages(self):
        for phase, lines in CONVERSATION_SCRIPT.items():
            for lang in self.REQUIRED_LANGS:
                self.assertIn(lang, lines, f"Phase '{phase}' missing lang '{lang}'")

    def test_passion_options_count(self):
        self.assertEqual(len(PASSION_OPTIONS), 6)

    def test_escape_options_count(self):
        self.assertEqual(len(ESCAPE_OPTIONS), 6)

    def test_all_passions_have_labels(self):
        for opt in PASSION_OPTIONS:
            self.assertIn('labels', opt)
            self.assertIn('en', opt['labels'])

    def test_all_escapes_have_labels(self):
        for opt in ESCAPE_OPTIONS:
            self.assertIn('labels', opt)
            self.assertIn('en', opt['labels'])

    def test_all_passions_have_dimensions(self):
        for opt in PASSION_OPTIONS:
            self.assertIn('dimensions', opt)
            self.assertIsInstance(opt['dimensions'], dict)

    def test_all_passions_have_acknowledgments(self):
        for opt in PASSION_OPTIONS:
            key = opt['key']
            self.assertIn(key, ACKNOWLEDGMENTS_PASSION,
                          f"Missing ack for passion '{key}'")

    def test_escape_acknowledgment_exists(self):
        for lang in self.REQUIRED_LANGS:
            self.assertIn(lang, ACKNOWLEDGMENT_ESCAPE)


# ═══════════════════════════════════════════════════════════════
# API Routes (mock Flask)
# ═══════════════════════════════════════════════════════════════

class TestOnboardingRoutes(unittest.TestCase):
    """Verify onboarding API routes register and respond."""

    @classmethod
    def setUpClass(cls):
        try:
            from flask import Flask
        except ImportError:
            raise unittest.SkipTest("Flask not installed")

        app = Flask(__name__)
        app.config['TESTING'] = True

        from integrations.agent_engine.onboarding_routes import (
            register_onboarding_routes)
        register_onboarding_routes(app)
        cls.client = app.test_client()

    def test_start_route_exists(self):
        resp = self.client.post('/api/onboarding/start',
                                json={'user_id': 'test_route'})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])

    def test_advance_route_exists(self):
        resp = self.client.post('/api/onboarding/advance',
                                json={'user_id': 'test_route',
                                      'action': 'select_language',
                                      'data': {'language': 'en'}})
        self.assertEqual(resp.status_code, 200)

    def test_status_route_exists(self):
        resp = self.client.get('/api/onboarding/status?user_id=test_route')
        self.assertEqual(resp.status_code, 200)

    def test_profile_not_onboarded(self):
        resp = self.client.get('/api/onboarding/profile?user_id=nonexistent_999')
        self.assertEqual(resp.status_code, 404)

    def tearDown(self):
        remove_session('test_route')


# ═══════════════════════════════════════════════════════════════
# Identity Unification
# ═══════════════════════════════════════════════════════════════

class TestIdentityUnification(unittest.TestCase):
    """Element + spirit + name form the unified HART identity."""

    def test_music_quiet_combo(self):
        """music_art + quiet_alone => neon.owl.{name}"""
        dims = _merge_dimensions('music_art', 'quiet_alone')
        e, s = assign_element_spirit(dims)
        self.assertEqual(e, 'neon')    # creative top
        self.assertEqual(s, 'owl')     # introspective second

    def test_building_ideas_combo(self):
        """building_coding + ideas_possibilities => iron.fox.{name}"""
        dims = _merge_dimensions('building_coding', 'ideas_possibilities')
        e, s = assign_element_spirit(dims)
        self.assertEqual(e, 'iron')    # builder top
        self.assertEqual(s, 'fox')     # curious second

    def test_people_love_combo(self):
        """people_stories + people_love => ember.deer.{name}"""
        dims = _merge_dimensions('people_stories', 'people_love')
        e, s = assign_element_spirit(dims)
        self.assertEqual(e, 'ember')   # social top
        self.assertEqual(s, 'deer')    # empathetic second

    def test_nature_open_combo(self):
        """nature_movement + nature_open => stone.hawk.{name}"""
        dims = _merge_dimensions('nature_movement', 'nature_open')
        e, s = assign_element_spirit(dims)
        # grounded=0.9, free=0.9 — tie broken by order in dict
        # Both could be top, depends on dict ordering
        self.assertIn(e, ['stone', 'wind'])

    @patch.dict(sys.modules, {'hart_intelligence_entry': _mock_lgapi})
    @patch.dict('os.environ', {
        'HART_CENTRAL_ELEMENT': 'stone',
        'HART_REGIONAL_SPIRIT': 'hawk',
    })
    def test_full_generate_has_tag(self):
        result = generate_hart_name('en', 'games_strategy', 'building_something')
        tag = result['hart_tag']
        self.assertTrue(tag.startswith('@'))
        parts = tag[1:].split('.')
        self.assertEqual(len(parts), 3)
        # Tag format: @central.regional.name
        self.assertEqual(parts[0], 'stone')
        self.assertEqual(parts[1], 'hawk')
        self.assertEqual(parts[2], result['name'])



# ═══════════════════════════════════════════════════════════════
# seal_name persistence + idempotent repair
# ═══════════════════════════════════════════════════════════════
# These use a REAL in-memory SQLAlchemy session on purpose.  The defect
# guarded here is SQLAlchemy's identity-based change detection:
# `settings = user.settings or {}` hands back the ORM's OWN dict whenever
# settings is non-empty, so mutating it and assigning it back is a no-op
# and `settings['hart']` is silently dropped -- while the scalar
# `user.handle = clean` on the neighbouring line persists fine.  A
# hand-rolled fake session would happily "persist" both and the test
# would pass against the broken code, proving nothing.
#
# Live evidence that motivated this (2026-08-13): a guest row showed
# handle=None, settings={} after a completed ceremony, and
# /api/hart/check returned has_hart:false forever.

from contextlib import contextmanager  # noqa: E402

try:
    from sqlalchemy import Column, JSON, String, create_engine
    from sqlalchemy.orm import sessionmaker
    try:
        from sqlalchemy.orm import declarative_base
    except ImportError:  # SQLAlchemy 1.3
        from sqlalchemy.ext.declarative import declarative_base
    _HAVE_SQLA = True
except ImportError:  # pragma: no cover
    _HAVE_SQLA = False


@unittest.skipUnless(_HAVE_SQLA, "SQLAlchemy required")
class TestSealNamePersistence(unittest.TestCase):
    """seal_name must persist BOTH handle AND settings['hart']."""

    def setUp(self):
        Base = declarative_base()

        class _User(Base):
            __tablename__ = 'users'
            id = Column(String(64), primary_key=True)
            username = Column(String(64))
            handle = Column(String(64), unique=True)
            display_name = Column(String(64))
            settings = Column(JSON, default=dict)

        self.User = _User
        engine = create_engine('sqlite://')
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)

        _Session = self.Session

        @contextmanager
        def _db_session(commit=True):
            s = _Session()
            try:
                yield s
                if commit:
                    s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        fake = MagicMock()
        fake.db_session = _db_session
        fake.User = _User
        self._patcher = patch.dict(
            sys.modules, {'integrations.social.models': fake})
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

        # Cloud uniqueness check does network I/O -- always available here.
        self._cloud = patch.object(
            HARTNameRegistry, '_check_cloud_available', return_value=True)
        self._cloud.start()
        self.addCleanup(self._cloud.stop)

    def _add_user(self, uid='u1', settings=None, handle=None):
        s = self.Session()
        s.add(self.User(id=uid, username='guest_x', handle=handle,
                        settings=settings if settings is not None else {}))
        s.commit()
        s.close()

    def _row(self, uid='u1'):
        s = self.Session()
        try:
            return s.query(self.User).filter_by(id=uid).first()
        finally:
            s.close()

    @staticmethod
    def _seal(uid, name, dims=None):
        """Call seal_name the way the route does (chatbot_routes.py:4330)
        -- all keywords, so a signature change surfaces here too."""
        return HARTNameRegistry.seal_name(
            user_id=uid, name=name, dimensions=dims or {},
            emoji_combo='', language='en', locale='en_US',
            passion_key='', escape_key='')

    def test_seal_persists_handle(self):
        self._add_user()
        self.assertTrue(self._seal('u1', 'kalvaris'))
        self.assertEqual(self._row().handle, 'kalvaris')

    def test_seal_persists_hart_bucket_on_empty_settings(self):
        self._add_user(settings={})
        self.assertTrue(self._seal('u1', 'kalvaris'))
        st = self._row().settings or {}
        self.assertTrue(st.get('hart', {}).get('sealed') is True,
                        f"hart bucket missing/unsealed: {st!r}")

    def test_seal_persists_hart_bucket_when_settings_already_populated(self):
        """THE regression: a user with prior settings (e.g. a theme) loses
        the hart bucket because the dict is mutated in place."""
        self._add_user(settings={'theme': 'dark'})
        self.assertTrue(self._seal('u1', 'kalvaris'))
        st = self._row().settings or {}
        self.assertEqual(st.get('theme'), 'dark', "clobbered unrelated keys")
        self.assertTrue(st.get('hart', {}).get('sealed') is True,
                        f"hart bucket dropped by in-place mutation: {st!r}")

    def test_has_hart_name_true_after_seal(self):
        """End-to-end: the reader that gates onboarding must agree."""
        self._add_user(settings={'theme': 'dark'})
        self.assertTrue(self._seal('u1', 'kalvaris'))
        self.assertTrue(has_hart_name('u1'))

    def test_reseal_same_name_repairs_missing_bucket(self):
        """Field-repair path: rows already corrupted by the bug have
        handle set but no bucket.  Re-sealing the SAME name must heal
        them, not 409 forever."""
        self._add_user(handle='kalvaris', settings={'theme': 'dark'})
        self.assertTrue(self._seal('u1', 'kalvaris'))
        self.assertTrue(has_hart_name('u1'))

    def test_reseal_same_name_does_not_rewrite_sealed_bucket(self):
        """Permanence (docstring: 'Once sealed, it cannot be changed').
        A second seal of the same name reports success WITHOUT mutating
        the recorded dimensions/sealed_at."""
        self._add_user()
        self.assertTrue(self._seal('u1', 'kalvaris', {'creative': 0.9}))
        first = dict(self._row().settings['hart'])
        self.assertTrue(self._seal('u1', 'kalvaris', {'builder': 0.1}))
        after = self._row().settings['hart']
        self.assertEqual(first.get('sealed_at'), after.get('sealed_at'),
                         "sealed_at rewritten -- permanence broken")
        self.assertEqual(first.get('dimensions'), after.get('dimensions'),
                         "dimensions rewritten -- permanence broken")

    def test_different_name_when_already_sealed_is_rejected(self):
        """The permanence guard stays: a DIFFERENT name is refused."""
        self._add_user(handle='kalvaris')
        self.assertFalse(self._seal('u1', 'othername'))
        self.assertEqual(self._row().handle, 'kalvaris')

    def test_missing_user_is_rejected(self):
        self.assertFalse(self._seal('nope', 'kalvaris'))



if __name__ == '__main__':
    unittest.main()
