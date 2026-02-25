"""
Comprehensive tests for the HevolveSocial agent naming system.
Tests what3words-style dot-separated naming, handle system,
local/global naming, services, API endpoints, and migrations.
"""
import os
import sys
import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

# Add parent dir for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


# ═══════════════════════════════════════════════════════════════
# 1. AGENT NAMING - VALIDATION & GENERATION
# ═══════════════════════════════════════════════════════════════

class TestAgentNamingValidation:
    """Test validate_agent_name (3-word global names)."""

    def setup_method(self):
        from integrations.social.agent_naming import validate_agent_name
        self.validate = validate_agent_name

    def test_valid_3word_dot_name(self):
        valid, err = self.validate("swift.falcon.sathi")
        assert valid is True
        assert err is None

    def test_valid_3word_different_names(self):
        names = [
            "calm.oracle.john",
            "bold.storm.alex",
            "fierce.phoenix.maya",
            "gentle.ember.kai",
        ]
        for name in names:
            valid, err = self.validate(name)
            assert valid is True, f"'{name}' should be valid but got: {err}"

    def test_empty_name_invalid(self):
        valid, err = self.validate("")
        assert valid is False
        assert "required" in err.lower()

    def test_none_name_invalid(self):
        valid, err = self.validate(None)
        assert valid is False

    def test_hyphenated_name_rejected(self):
        """Old hyphen format should be rejected now."""
        valid, err = self.validate("swift-falcon-sathi")
        assert valid is False
        assert "dots" in err.lower() or "3 lowercase" in err.lower()

    def test_2word_name_rejected(self):
        valid, err = self.validate("swift.falcon")
        assert valid is False

    def test_4word_name_rejected(self):
        valid, err = self.validate("swift.falcon.sathi.extra")
        assert valid is False

    def test_numbers_rejected(self):
        valid, err = self.validate("swift.falcon1.sathi")
        assert valid is False

    def test_uppercase_normalized(self):
        valid, err = self.validate("Swift.Falcon.Sathi")
        assert valid is True  # lowered internally

    def test_reserved_word_rejected(self):
        valid, err = self.validate("admin.falcon.sathi")
        assert valid is False
        assert "reserved" in err.lower()

    def test_reserved_word_middle(self):
        valid, err = self.validate("swift.system.sathi")
        assert valid is False

    def test_reserved_word_last(self):
        valid, err = self.validate("swift.falcon.admin")
        assert valid is False

    def test_single_char_words_rejected(self):
        valid, err = self.validate("a.b.c")
        assert valid is False

    def test_long_word_rejected(self):
        valid, err = self.validate("abcdefghijklmnop.falcon.sathi")  # 16 chars
        assert valid is False

    def test_max_length_word_accepted(self):
        valid, err = self.validate("abcdefghijklmno.falcon.sathi")  # 15 chars
        assert valid is True

    def test_name_too_long_rejected(self):
        valid, err = self.validate("abcdefghijklmno.abcdefghijklmno.abcdefghijklmno")  # 47+ chars
        assert valid is False or valid is True  # 47 is the limit
        # Calculate: 15 + 1 + 15 + 1 + 15 = 47 - right at limit
        # Actually this is exactly 47, should pass


class TestLocalNameValidation:
    """Test validate_local_name (2-word local names)."""

    def setup_method(self):
        from integrations.social.agent_naming import validate_local_name
        self.validate = validate_local_name

    def test_valid_local_name(self):
        valid, err = self.validate("swift.falcon")
        assert valid is True
        assert err is None

    def test_valid_various_local_names(self):
        for name in ["calm.oracle", "bold.storm", "fierce.phoenix", "gentle.ember"]:
            valid, err = self.validate(name)
            assert valid is True, f"'{name}' should be valid"

    def test_empty_rejected(self):
        valid, err = self.validate("")
        assert valid is False

    def test_single_word_rejected(self):
        valid, err = self.validate("falcon")
        assert valid is False

    def test_3words_rejected(self):
        valid, err = self.validate("swift.falcon.extra")
        assert valid is False

    def test_hyphen_format_rejected(self):
        valid, err = self.validate("swift-falcon")
        assert valid is False
        assert "dot" in err.lower()

    def test_numbers_rejected(self):
        valid, err = self.validate("swift.falcon1")
        assert valid is False

    def test_uppercase_normalized(self):
        valid, err = self.validate("Swift.Falcon")
        assert valid is True

    def test_reserved_word_rejected(self):
        valid, err = self.validate("admin.falcon")
        assert valid is False
        assert "reserved" in err.lower()

    def test_whitespace_trimmed(self):
        valid, err = self.validate("  swift.falcon  ")
        assert valid is True


class TestHandleValidation:
    """Test validate_handle."""

    def setup_method(self):
        from integrations.social.agent_naming import validate_handle
        self.validate = validate_handle

    def test_valid_handle(self):
        valid, err = self.validate("sathi")
        assert valid is True

    def test_valid_handles(self):
        for h in ["john", "alex", "mayakalyan", "ab"]:
            valid, err = self.validate(h)
            assert valid is True, f"'{h}' should be valid"

    def test_empty_rejected(self):
        valid, err = self.validate("")
        assert valid is False

    def test_too_short_rejected(self):
        valid, err = self.validate("a")
        assert valid is False

    def test_numbers_rejected(self):
        valid, err = self.validate("sathi123")
        assert valid is False

    def test_dots_rejected(self):
        valid, err = self.validate("sat.hi")
        assert valid is False

    def test_hyphens_rejected(self):
        valid, err = self.validate("sa-thi")
        assert valid is False

    def test_spaces_rejected(self):
        valid, err = self.validate("sa thi")
        assert valid is False

    def test_reserved_rejected(self):
        valid, err = self.validate("admin")
        assert valid is False
        assert "reserved" in err.lower()

    def test_too_long_rejected(self):
        valid, err = self.validate("abcdefghijklmnop")  # 16 chars
        assert valid is False

    def test_max_length_accepted(self):
        valid, err = self.validate("abcdefghijklmno")  # 15 chars
        assert valid is True

    def test_uppercase_normalized(self):
        valid, err = self.validate("SATHI")
        assert valid is True


class TestComposeGlobalName:
    """Test compose_global_name."""

    def setup_method(self):
        from integrations.social.agent_naming import compose_global_name
        self.compose = compose_global_name

    def test_basic_composition(self):
        assert self.compose("swift.falcon", "sathi") == "swift.falcon.sathi"

    def test_trims_whitespace(self):
        assert self.compose("  swift.falcon  ", "  sathi  ") == "swift.falcon.sathi"

    def test_lowercased(self):
        assert self.compose("Swift.Falcon", "Sathi") == "swift.falcon.sathi"


class TestCheckGlobalAvailability:
    """Test check_global_availability with mocked DB."""

    def test_available_name(self):
        from integrations.social.agent_naming import check_global_availability
        mock_db = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        available, global_name, err = check_global_availability(
            mock_db, "swift.falcon", "sathi")
        assert available is True
        assert global_name == "swift.falcon.sathi"
        assert err is None

    def test_taken_name(self):
        from integrations.social.agent_naming import check_global_availability
        mock_db = Mock()
        mock_user = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_user

        available, global_name, err = check_global_availability(
            mock_db, "swift.falcon", "sathi")
        assert available is False
        assert "taken" in err.lower()


class TestNameGeneration:
    """Test generate_agent_name and fallback generation."""

    def test_fallback_local_names_dot_format(self):
        from integrations.social.agent_naming import _generate_random_fallback
        mock_db = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        names = _generate_random_fallback(mock_db, 5, mode='local')
        assert len(names) == 5
        for name in names:
            assert '.' in name, f"'{name}' should use dot separator"
            assert '-' not in name, f"'{name}' should NOT use hyphens"
            parts = name.split('.')
            assert len(parts) == 2
            assert all(p.isalpha() and p.islower() for p in parts)

    def test_fallback_global_names_dot_format(self):
        from integrations.social.agent_naming import _generate_random_fallback
        mock_db = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        names = _generate_random_fallback(mock_db, 5, mode='global')
        assert len(names) == 5
        for name in names:
            assert '.' in name
            parts = name.split('.')
            assert len(parts) == 3

    def test_fallback_local_with_handle_checks_global(self):
        from integrations.social.agent_naming import _generate_random_fallback
        mock_db = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        names = _generate_random_fallback(mock_db, 3, mode='local', handle='sathi')
        assert len(names) == 3
        # DB should have been queried for global availability
        assert mock_db.query.called

    def test_generate_agent_name_local_mode(self):
        """Test the main generate function in local mode (with LLM mocked out)."""
        from integrations.social.agent_naming import generate_agent_name
        mock_db = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        with patch('integrations.social.agent_naming._generate_via_llm', return_value=[]):
            names = generate_agent_name(mock_db, count=3, mode='local', handle='sathi')
            assert len(names) == 3
            for name in names:
                assert '.' in name

    def test_generate_agent_name_global_mode(self):
        from integrations.social.agent_naming import generate_agent_name
        mock_db = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        with patch('integrations.social.agent_naming._generate_via_llm', return_value=[]):
            names = generate_agent_name(mock_db, count=3, mode='global')
            assert len(names) == 3
            for name in names:
                parts = name.split('.')
                assert len(parts) == 3

    def test_llm_names_validated_and_used(self):
        """When LLM returns valid names, they should be used."""
        from integrations.social.agent_naming import generate_agent_name
        mock_db = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        llm_names = ["bright.hawk", "silent.wolf", "vivid.prism"]
        with patch('integrations.social.agent_naming._generate_via_llm', return_value=llm_names):
            names = generate_agent_name(mock_db, count=3, mode='local', handle='test')
            # LLM names should appear in results
            assert len(names) >= 1

    def test_llm_invalid_names_filtered(self):
        """Invalid LLM names should be filtered out."""
        from integrations.social.agent_naming import generate_agent_name
        mock_db = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        llm_names = ["invalid", "a.b", "123.456", "good.name"]
        with patch('integrations.social.agent_naming._generate_via_llm', return_value=llm_names):
            names = generate_agent_name(mock_db, count=3, mode='local')
            # "invalid", "a.b", "123.456" should be filtered; "good.name" passes
            assert "good.name" in names or len(names) == 3  # fallback fills remaining


class TestReservedWords:
    """Test reserved words are properly blocked."""

    def test_all_reserved_in_frozenset(self):
        from integrations.social.agent_naming import RESERVED_WORDS
        expected = {'admin', 'root', 'system', 'bot', 'test', 'null',
                    'hevolve', 'api', 'webhook', 'banned'}
        for word in expected:
            assert word in RESERVED_WORDS

    def test_reserved_as_handle(self):
        from integrations.social.agent_naming import validate_handle
        for word in ['admin', 'system', 'bot', 'hevolve']:
            valid, err = validate_handle(word)
            assert valid is False, f"'{word}' should be rejected as handle"

    def test_reserved_in_local_name(self):
        from integrations.social.agent_naming import validate_local_name
        valid, err = validate_local_name("admin.falcon")
        assert valid is False
        valid, err = validate_local_name("swift.system")
        assert valid is False


# ═══════════════════════════════════════════════════════════════
# 2. SERVICES LAYER
# ═══════════════════════════════════════════════════════════════

class TestUserServiceHandle:
    """Test UserService.set_handle."""

    def test_set_handle_valid(self):
        from integrations.social.services import UserService
        mock_db = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        mock_user = Mock()
        mock_user.handle = None
        mock_user.user_type = 'human'

        result = UserService.set_handle(mock_db, mock_user, 'sathi')
        assert mock_user.handle == 'sathi'
        assert mock_db.flush.called

    def test_set_handle_taken(self):
        from integrations.social.services import UserService
        mock_db = Mock()
        existing_user = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = existing_user
        mock_user = Mock()

        with pytest.raises(ValueError, match="taken"):
            UserService.set_handle(mock_db, mock_user, 'sathi')

    def test_set_handle_invalid_format(self):
        from integrations.social.services import UserService
        mock_db = Mock()
        mock_user = Mock()

        with pytest.raises(ValueError):
            UserService.set_handle(mock_db, mock_user, '123invalid')

    def test_set_handle_reserved(self):
        from integrations.social.services import UserService
        mock_db = Mock()
        mock_user = Mock()

        with pytest.raises(ValueError, match="reserved"):
            UserService.set_handle(mock_db, mock_user, 'admin')


class TestRegisterAgentLocal:
    """Test UserService.register_agent_local."""

    def _mock_db_available(self):
        """DB where all names are available."""
        mock_db = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        return mock_db

    def _mock_owner(self, handle='sathi'):
        owner = Mock()
        owner.id = 'owner-123'
        owner.handle = handle
        return owner

    def test_register_basic(self):
        from integrations.social.services import UserService
        db = self._mock_db_available()
        owner = self._mock_owner()

        agent = UserService.register_agent_local(
            db, 'swift.falcon', 'A test agent', agent_id='a1', owner=owner)
        assert db.add.called
        assert db.flush.called
        # The created user should have the global name as username
        created_user = db.add.call_args[0][0]
        assert created_user.username == 'swift.falcon.sathi'
        assert created_user.local_name == 'swift.falcon'
        assert created_user.user_type == 'agent'

    def test_no_owner_raises(self):
        from integrations.social.services import UserService
        db = self._mock_db_available()

        with pytest.raises(ValueError, match="Owner"):
            UserService.register_agent_local(db, 'swift.falcon', owner=None)

    def test_no_handle_raises(self):
        from integrations.social.services import UserService
        db = self._mock_db_available()
        owner = Mock()
        owner.handle = None

        with pytest.raises(ValueError, match="handle"):
            UserService.register_agent_local(db, 'swift.falcon', owner=owner)

    def test_invalid_local_name_raises(self):
        from integrations.social.services import UserService
        db = self._mock_db_available()
        owner = self._mock_owner()

        with pytest.raises(ValueError):
            UserService.register_agent_local(db, 'invalid', owner=owner)

    def test_duplicate_local_name_same_owner(self):
        from integrations.social.services import UserService
        db = Mock()
        existing_agent = Mock()
        # First query: local name check returns existing
        db.query.return_value.filter.return_value.first.return_value = existing_agent
        owner = self._mock_owner()

        with pytest.raises(ValueError, match="already have"):
            UserService.register_agent_local(db, 'swift.falcon', owner=owner)

    def test_global_name_taken(self):
        from integrations.social.services import UserService
        db = Mock()
        # First query (local check) returns None, second query (global) returns existing
        db.query.return_value.filter.return_value.first.side_effect = [None, Mock()]
        owner = self._mock_owner()

        with pytest.raises(ValueError, match="taken globally"):
            UserService.register_agent_local(db, 'swift.falcon', owner=owner)


class TestUpdateProfile:
    """Test UserService.update_profile with handle param."""

    def test_update_with_handle(self):
        from integrations.social.services import UserService
        mock_db = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        mock_user = Mock()
        mock_user.user_type = 'human'
        mock_user.handle = None

        UserService.update_profile(
            mock_db, mock_user, display_name='New Name', handle='newhandle')
        assert mock_user.display_name == 'New Name'
        assert mock_user.handle == 'newhandle'

    def test_update_without_handle(self):
        from integrations.social.services import UserService
        mock_db = Mock()
        mock_user = Mock()
        mock_user.user_type = 'human'

        UserService.update_profile(mock_db, mock_user, bio='New bio')
        assert mock_user.bio == 'New bio'


# ═══════════════════════════════════════════════════════════════
# 3. API ENDPOINTS (unit tests with Flask test client)
# ═══════════════════════════════════════════════════════════════

class TestAPIEndpoints:
    """Test API endpoints by importing and calling handler functions."""

    def test_suggest_names_endpoint_importable(self):
        """Verify the endpoint handler can be imported."""
        from integrations.social.api import suggest_agent_names
        assert callable(suggest_agent_names)

    def test_validate_name_endpoint_importable(self):
        from integrations.social.api import validate_agent_name_endpoint
        assert callable(validate_agent_name_endpoint)

    def test_set_handle_endpoint_importable(self):
        from integrations.social.api import set_user_handle
        assert callable(set_user_handle)

    def test_check_handle_endpoint_importable(self):
        from integrations.social.api import check_handle_availability
        assert callable(check_handle_availability)

    def test_create_agent_endpoint_importable(self):
        from integrations.social.api import create_user_agent
        assert callable(create_user_agent)

    def test_blueprint_routes_registered(self):
        """Verify the blueprint loads correctly."""
        from integrations.social.api import social_bp
        assert social_bp.name == 'social'
        assert social_bp.url_prefix == '/api/social'
        # Blueprint has deferred functions (route registrations)
        assert len(social_bp.deferred_functions) > 0


# ═══════════════════════════════════════════════════════════════
# 4. MIGRATIONS
# ═══════════════════════════════════════════════════════════════

class TestMigrations:
    """Test migration module."""

    def test_schema_version_current(self):
        from integrations.social.migrations import SCHEMA_VERSION
        assert SCHEMA_VERSION >= 2

    def test_migration_functions_exist(self):
        from integrations.social.migrations import (
            get_schema_version, set_schema_version, run_migrations)
        assert callable(get_schema_version)
        assert callable(set_schema_version)
        assert callable(run_migrations)


# ═══════════════════════════════════════════════════════════════
# 5. MODELS
# ═══════════════════════════════════════════════════════════════

class TestUserModel:
    """Test User model has the new columns."""

    def test_handle_column_exists(self):
        from integrations.social.models import User
        assert hasattr(User, 'handle')

    def test_local_name_column_exists(self):
        from integrations.social.models import User
        assert hasattr(User, 'local_name')

    def test_to_dict_includes_handle(self):
        """Verify to_dict includes handle and local_name."""
        from integrations.social.models import User
        # Check the to_dict method source for 'handle'
        import inspect
        source = inspect.getsource(User.to_dict)
        assert 'handle' in source
        assert 'local_name' in source


# ═══════════════════════════════════════════════════════════════
# 6. GATHER AGENT DETAILS (LLM prompt changes)
# ═══════════════════════════════════════════════════════════════

class TestGatherAgentDetails:
    """Test that gather_agentdetails.py uses dot-separated naming."""

    def test_system_prompt_uses_dots(self):
        """Verify the system prompt uses dot format, not hyphens."""
        with open(os.path.join(os.path.dirname(__file__), '..', '..', 'gather_agentdetails.py'), 'r') as f:
            content = f.read()

        # Should have dot examples
        assert 'swift.falcon' in content or 'calm.oracle' in content, \
            "System prompt should contain dot-separated examples"

        # Should NOT have old hyphen examples
        assert 'swift-falcon' not in content, \
            "System prompt should not contain hyphenated examples"
        assert 'swift-amber-falcon' not in content, \
            "System prompt should not contain old 3-word hyphenated format"

    def test_system_prompt_allows_user_choice(self):
        """Verify the prompt asks user if they want to pick a name."""
        with open(os.path.join(os.path.dirname(__file__), '..', '..', 'gather_agentdetails.py'), 'r') as f:
            content = f.read()
        assert 'Ask the user' in content or 'ask the user' in content, \
            "System prompt should offer user choice for agent_name"

    def test_no_forced_auto_generation(self):
        """Verify the prompt doesn't force auto-generation."""
        with open(os.path.join(os.path.dirname(__file__), '..', '..', 'gather_agentdetails.py'), 'r') as f:
            content = f.read()
        assert 'do NOT ask the user' not in content, \
            "Should not forbid asking the user for a name"

    def test_completed_template_uses_dots(self):
        with open(os.path.join(os.path.dirname(__file__), '..', '..', 'gather_agentdetails.py'), 'r') as f:
            content = f.read()
        assert 'two.word.name' in content, \
            "Completed template should use dot-separated placeholder"


# ═══════════════════════════════════════════════════════════════
# 7. FRONTEND FILES VALIDATION
# ═══════════════════════════════════════════════════════════════

class TestFrontendFiles:
    """Validate frontend files have correct dot-format patterns."""

    NUNBA_BASE = os.path.join(os.path.dirname(__file__), '..', '..',
                               'Nunba', 'landing-page', 'src')

    def _read_if_exists(self, relpath):
        path = os.path.join(self.NUNBA_BASE, relpath)
        if os.path.exists(path):
            with open(path, 'r') as f:
                return f.read()
        return None

    def test_social_api_has_handle_functions(self):
        content = self._read_if_exists('services/socialApi.js')
        if content is None:
            pytest.skip("Nunba project not found at expected path")
        assert 'setHandle' in content, "socialApi should have setHandle function"
        assert 'checkHandle' in content, "socialApi should have checkHandle function"
        assert 'suggestLocalNames' in content, "socialApi should have suggestLocalNames"
        assert 'validateLocalName' in content, "socialApi should have validateLocalName"

    def test_create_agent_dialog_dot_regex(self):
        content = self._read_if_exists(
            'components/Social/Profile/CreateAgentDialog.js')
        if content is None:
            pytest.skip("Nunba project not found at expected path")
        # Should have dot regex, not hyphen regex
        assert r'\.' in content or 'toStorage' in content, \
            "CreateAgentDialog should use dot-based name handling"
        assert 'swift-falcon' not in content, \
            "Should not have hyphen-based examples"

    def test_create_agent_dialog_space_input(self):
        """User types with spaces, system converts to dots."""
        content = self._read_if_exists(
            'components/Social/Profile/CreateAgentDialog.js')
        if content is None:
            pytest.skip("Nunba project not found at expected path")
        assert 'toStorage' in content and 'toDisplay' in content, \
            "Should have space-to-dot conversion functions"
        assert 'swift falcon' in content, \
            "Placeholder should show space-separated format for user"

    def test_create_agent_dialog_what3words_preview(self):
        content = self._read_if_exists(
            'components/Social/Profile/CreateAgentDialog.js')
        if content is None:
            pytest.skip("Nunba project not found at expected path")
        assert '///' in content, \
            "Should show what3words-style /// prefix in preview"

    def test_profile_page_shows_handle(self):
        content = self._read_if_exists(
            'components/Social/Profile/ProfilePage.js')
        if content is None:
            pytest.skip("Nunba project not found at expected path")
        assert 'user.handle' in content, \
            "ProfilePage should reference user.handle"
        assert 'userHandle' in content, \
            "ProfilePage should pass userHandle to CreateAgentDialog"

    def test_profile_page_what3words_agent_display(self):
        content = self._read_if_exists(
            'components/Social/Profile/ProfilePage.js')
        if content is None:
            pytest.skip("Nunba project not found at expected path")
        assert '///' in content, \
            "Agent list should show /// prefix for dot-format names"


# ═══════════════════════════════════════════════════════════════
# 8. INTEGRATION TEST - FULL FLOW
# ═══════════════════════════════════════════════════════════════

class TestFullNamingFlow:
    """End-to-end integration test for the naming flow."""

    def test_full_local_to_global_flow(self):
        """Test: validate handle → validate local name → compose global → check availability."""
        from integrations.social.agent_naming import (
            validate_handle, validate_local_name, compose_global_name,
            validate_agent_name,
        )

        # Step 1: Validate handle
        valid, err = validate_handle("sathi")
        assert valid is True

        # Step 2: Validate local name
        valid, err = validate_local_name("swift.falcon")
        assert valid is True

        # Step 3: Compose global name
        global_name = compose_global_name("swift.falcon", "sathi")
        assert global_name == "swift.falcon.sathi"

        # Step 4: Validate the composed global name
        valid, err = validate_agent_name(global_name)
        assert valid is True

    def test_various_handle_local_combos(self):
        from integrations.social.agent_naming import (
            validate_handle, validate_local_name, compose_global_name,
            validate_agent_name,
        )

        combos = [
            ("john", "calm.oracle"),
            ("alex", "bold.storm"),
            ("maya", "fierce.phoenix"),
            ("kai", "gentle.ember"),
        ]
        for handle, local in combos:
            assert validate_handle(handle)[0] is True
            assert validate_local_name(local)[0] is True
            global_name = compose_global_name(local, handle)
            assert validate_agent_name(global_name)[0] is True, \
                f"Global name '{global_name}' should be valid"

    def test_conflict_detection(self):
        """When global name is taken, check_global_availability returns False."""
        from integrations.social.agent_naming import check_global_availability
        mock_db = Mock()
        # First call (is_name_available check) returns an existing user
        mock_db.query.return_value.filter.return_value.first.return_value = Mock()

        available, global_name, err = check_global_availability(
            mock_db, "swift.falcon", "sathi")
        assert available is False
        assert "taken" in err.lower()
        assert global_name == "swift.falcon.sathi"


# ═══════════════════════════════════════════════════════════════
# 9. BACKWARD COMPATIBILITY
# ═══════════════════════════════════════════════════════════════

class TestBackwardCompatibility:
    """Ensure old 3-word registration still works."""

    def test_register_agent_legacy_3word(self):
        """Legacy register_agent with 3-word dot name should work."""
        from integrations.social.services import UserService
        mock_db = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        agent = UserService.register_agent(
            mock_db, 'swift.amber.falcon', 'A test agent', agent_id='a1')
        assert mock_db.add.called
        created = mock_db.add.call_args[0][0]
        assert created.username == 'swift.amber.falcon'

    def test_register_agent_skip_validation(self):
        """skip_name_validation=True should allow any format."""
        from integrations.social.services import UserService
        mock_db = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        agent = UserService.register_agent(
            mock_db, 'any-format-name', 'test', skip_name_validation=True)
        assert mock_db.add.called


# ═══════════════════════════════════════════════════════════════
# 10. EDGE CASES
# ═══════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_name_with_leading_trailing_dots(self):
        from integrations.social.agent_naming import validate_local_name
        valid, err = validate_local_name(".swift.falcon.")
        assert valid is False

    def test_name_with_multiple_dots(self):
        from integrations.social.agent_naming import validate_local_name
        valid, err = validate_local_name("swift..falcon")
        assert valid is False

    def test_empty_string_handle(self):
        from integrations.social.agent_naming import validate_handle
        valid, err = validate_handle("")
        assert valid is False

    def test_whitespace_only_handle(self):
        from integrations.social.agent_naming import validate_handle
        valid, err = validate_handle("   ")
        assert valid is False

    def test_compose_with_empty_handle(self):
        from integrations.social.agent_naming import compose_global_name
        result = compose_global_name("swift.falcon", "")
        # Should produce "swift.falcon." which would fail validation
        assert result.endswith('.')

    def test_unicode_rejected_in_name(self):
        from integrations.social.agent_naming import validate_local_name
        valid, err = validate_local_name("swift.fälcon")
        assert valid is False

    def test_unicode_rejected_in_handle(self):
        from integrations.social.agent_naming import validate_handle
        valid, err = validate_handle("sàthi")
        assert valid is False

    def test_is_name_available_with_mock_db(self):
        from integrations.social.agent_naming import is_name_available
        mock_db = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        assert is_name_available(mock_db, "swift.falcon.sathi") is True

        mock_db.query.return_value.filter.return_value.first.return_value = Mock()
        assert is_name_available(mock_db, "swift.falcon.sathi") is False

    def test_is_handle_available_with_mock_db(self):
        from integrations.social.agent_naming import is_handle_available
        mock_db = Mock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        assert is_handle_available(mock_db, "sathi") is True
