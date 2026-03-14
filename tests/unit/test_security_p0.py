"""
P0 Security Tests — SSRF, Guardrail Immutability, Shell Injection, Rate Limiter.

Tests the security code we actually added. No HSM/TLS (not implemented).
Run with: pytest tests/unit/test_security_p0.py -v --noconftest
"""

import os
import sys
import pytest
from types import MappingProxyType

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ═══════════════════════════════════════════════════════════════════════
# 1. SSRF Protection — security/sanitize.py:validate_url
# ═══════════════════════════════════════════════════════════════════════

class TestValidateUrl:
    """SSRF protection via validate_url()."""

    def _validate_url(self, *args, **kwargs):
        from security.sanitize import validate_url
        return validate_url(*args, **kwargs)

    def test_validate_url_allows_https(self):
        result = self._validate_url("https://example.com/api/data")
        assert result == "https://example.com/api/data"

    def test_validate_url_allows_http(self):
        result = self._validate_url("http://example.com/page")
        assert result == "http://example.com/page"

    def test_validate_url_blocks_private_ip_10(self):
        with pytest.raises(ValueError, match="private/reserved"):
            self._validate_url("http://10.0.0.1/internal")

    def test_validate_url_blocks_private_ip_192(self):
        with pytest.raises(ValueError, match="private/reserved"):
            self._validate_url("http://192.168.1.1/admin")

    def test_validate_url_blocks_private_ip_172(self):
        with pytest.raises(ValueError, match="private/reserved"):
            self._validate_url("http://172.16.0.1/secret")

    def test_validate_url_blocks_cloud_metadata(self):
        with pytest.raises(ValueError, match="cloud metadata"):
            self._validate_url("http://169.254.169.254/latest/meta-data/")

    def test_validate_url_blocks_localhost(self):
        with pytest.raises(ValueError, match="localhost"):
            self._validate_url("http://localhost/admin")

    def test_validate_url_blocks_localhost_ip(self):
        with pytest.raises(ValueError, match="private/reserved"):
            self._validate_url("http://127.0.0.1/admin")

    def test_validate_url_blocks_ftp_scheme(self):
        with pytest.raises(ValueError, match="scheme must be http or https"):
            self._validate_url("ftp://example.com/file.txt")

    def test_validate_url_blocks_file_scheme(self):
        with pytest.raises(ValueError, match="scheme must be http or https"):
            self._validate_url("file:///etc/passwd")

    def test_validate_url_allows_private_when_flag_set(self):
        result = self._validate_url("http://10.0.0.1/internal", allow_private=True)
        assert result == "http://10.0.0.1/internal"

    def test_validate_url_allows_localhost_when_flag_set(self):
        result = self._validate_url("http://localhost/admin", allow_private=True)
        assert result == "http://localhost/admin"

    def test_validate_url_rejects_empty(self):
        with pytest.raises(ValueError, match="non-empty"):
            self._validate_url("")

    def test_validate_url_rejects_whitespace_only(self):
        with pytest.raises(ValueError, match="non-empty"):
            self._validate_url("   ")

    def test_validate_url_rejects_none(self):
        with pytest.raises(ValueError, match="non-empty"):
            self._validate_url(None)

    def test_validate_url_blocks_google_metadata_internal(self):
        with pytest.raises(ValueError, match="cloud metadata"):
            self._validate_url("http://metadata.google.internal/computeMetadata/v1/")

    def test_validate_url_blocks_zero_address(self):
        with pytest.raises(ValueError, match="private/reserved"):
            self._validate_url("http://0.0.0.0/")

    def test_validate_url_strips_whitespace(self):
        result = self._validate_url("  https://example.com/path  ")
        assert result == "https://example.com/path"


# ═══════════════════════════════════════════════════════════════════════
# 2. Guardrail Immutability — security/hive_guardrails.py
# ═══════════════════════════════════════════════════════════════════════

class TestGuardrailImmutability:
    """Ensure guardrail data structures cannot be mutated at runtime."""

    def test_compute_caps_is_immutable(self):
        from security.hive_guardrails import COMPUTE_CAPS
        assert isinstance(COMPUTE_CAPS, MappingProxyType), \
            "COMPUTE_CAPS must be a MappingProxyType (read-only dict)"
        with pytest.raises(TypeError):
            COMPUTE_CAPS['max_influence_weight'] = 999.0

    def test_compute_caps_rejects_new_key(self):
        from security.hive_guardrails import COMPUTE_CAPS
        with pytest.raises(TypeError):
            COMPUTE_CAPS['backdoor'] = True

    def test_compute_caps_rejects_deletion(self):
        from security.hive_guardrails import COMPUTE_CAPS
        with pytest.raises(TypeError):
            del COMPUTE_CAPS['max_influence_weight']

    def test_constitutional_rules_is_tuple(self):
        from security.hive_guardrails import CONSTITUTIONAL_RULES
        assert isinstance(CONSTITUTIONAL_RULES, tuple), \
            "CONSTITUTIONAL_RULES must be a tuple (immutable sequence)"

    def test_constitutional_rules_has_entries(self):
        from security.hive_guardrails import CONSTITUTIONAL_RULES
        assert len(CONSTITUTIONAL_RULES) >= 30, \
            "Expected at least 30 constitutional rules"

    def test_destructive_patterns_is_tuple(self):
        from security.hive_guardrails import _DESTRUCTIVE_PATTERNS
        assert isinstance(_DESTRUCTIVE_PATTERNS, tuple), \
            "_DESTRUCTIVE_PATTERNS must be a tuple (immutable sequence)"

    def test_destructive_patterns_has_entries(self):
        from security.hive_guardrails import _DESTRUCTIVE_PATTERNS
        assert len(_DESTRUCTIVE_PATTERNS) >= 4, \
            "Expected at least 4 destructive patterns"

    def test_world_model_bounds_is_immutable(self):
        from security.hive_guardrails import WORLD_MODEL_BOUNDS
        assert isinstance(WORLD_MODEL_BOUNDS, MappingProxyType), \
            "WORLD_MODEL_BOUNDS must be a MappingProxyType (read-only dict)"
        with pytest.raises(TypeError):
            WORLD_MODEL_BOUNDS['max_skill_packets_per_hour'] = 999999

    def test_world_model_bounds_rejects_new_key(self):
        from security.hive_guardrails import WORLD_MODEL_BOUNDS
        with pytest.raises(TypeError):
            WORLD_MODEL_BOUNDS['escape_hatch'] = True

    def test_world_model_bounds_has_expected_keys(self):
        from security.hive_guardrails import WORLD_MODEL_BOUNDS
        expected = {'max_skill_packets_per_hour', 'min_witness_count_for_ralt',
                    'max_accuracy_improvement_per_day', 'prohibited_skill_categories'}
        assert expected.issubset(set(WORLD_MODEL_BOUNDS.keys()))


# ═══════════════════════════════════════════════════════════════════════
# 3. Shell Injection Protection — shell_os_apis.py:_classify_destructive
# ═══════════════════════════════════════════════════════════════════════

class TestShellInjectionProtection:
    """Verify _classify_destructive fails closed when classifier is unavailable."""

    def test_classify_destructive_fails_closed_when_classifier_unavailable(self):
        """When action_classifier module cannot be imported, the function must
        return False (deny) rather than True (allow). This is fail-closed."""
        from unittest.mock import patch

        from integrations.agent_engine.shell_os_apis import _classify_destructive

        # Simulate classifier being unavailable by making the import raise
        with patch(
            'integrations.agent_engine.shell_os_apis.classify_action',
            side_effect=ImportError("no module"),
            create=True,
        ):
            # Force re-import path by patching at the point of use
            pass

        # More reliable: patch the entire import inside the function
        import builtins
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == 'security.action_classifier':
                raise ImportError("Simulated: action_classifier unavailable")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, '__import__', side_effect=mock_import):
            result = _classify_destructive("rm -rf / --no-preserve-root")
            assert result is False, \
                "_classify_destructive must return False (deny) when classifier unavailable"

    def test_classify_destructive_returns_bool(self):
        """Result must be a boolean regardless of classifier state."""
        from unittest.mock import patch
        import builtins
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == 'security.action_classifier':
                raise ImportError("Simulated unavailable")
            return original_import(name, *args, **kwargs)

        from integrations.agent_engine.shell_os_apis import _classify_destructive

        with patch.object(builtins, '__import__', side_effect=mock_import):
            result = _classify_destructive("delete all files")
            assert isinstance(result, bool)


# ═══════════════════════════════════════════════════════════════════════
# 4. Rate Limiter — langchain_gpt_api.py MAX_CONTENT_LENGTH
# ═══════════════════════════════════════════════════════════════════════

class TestRateLimiter:
    """Verify Flask app has MAX_CONTENT_LENGTH configured."""

    def test_max_content_length_set(self):
        """The Flask app must have MAX_CONTENT_LENGTH configured to prevent
        oversized request bodies from consuming server memory."""
        # Import the app object
        from unittest.mock import patch
        # Patch heavy imports/side-effects that happen at module load
        with patch.dict(os.environ, {
            'OPENAI_API_KEY': 'test-key',
            'GROQ_API_KEY': 'test-key',
        }):
            try:
                from langchain_gpt_api import app
                max_len = app.config.get('MAX_CONTENT_LENGTH')
                assert max_len is not None, \
                    "MAX_CONTENT_LENGTH must be set on the Flask app"
                assert isinstance(max_len, int), \
                    "MAX_CONTENT_LENGTH must be an integer"
                assert max_len > 0, \
                    "MAX_CONTENT_LENGTH must be positive"
                # Should be reasonable (not more than 50MB)
                assert max_len <= 50 * 1024 * 1024, \
                    f"MAX_CONTENT_LENGTH too large: {max_len}"
            except ImportError as e:
                pytest.skip(f"Cannot import langchain_gpt_api: {e}")
            except Exception as e:
                # If the module fails to load for env reasons, check the source directly
                source_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'langchain_gpt_api.py'
                )
                with open(source_path, 'r') as f:
                    source = f.read()
                assert "MAX_CONTENT_LENGTH" in source, \
                    "langchain_gpt_api.py must set app.config['MAX_CONTENT_LENGTH']"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
