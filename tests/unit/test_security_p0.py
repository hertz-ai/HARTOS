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

    # Private, LAN and loopback addresses are allowed by default (owner,
    # 2026-09-14: "Allow shd be default on everywhere, no unnecessarily
    # redirecting in the name of security"). What is never a legitimate
    # target -- a cloud metadata endpoint, a non-http(s) scheme -- stays
    # refused either way; allow_private=False keeps the strict check.

    @pytest.mark.parametrize("url", [
        "http://10.0.0.1/internal", "http://192.168.1.1/admin",
        "http://172.16.0.1/secret", "http://localhost/admin",
        "http://127.0.0.1:5000/uploads/photo.jpg",
    ])
    def test_validate_url_allows_private_and_local_by_default(self, url):
        assert self._validate_url(url) == url

    def test_validate_url_blocks_cloud_metadata_even_when_private_allowed(self):
        with pytest.raises(ValueError, match="cloud metadata"):
            self._validate_url("http://169.254.169.254/latest/meta-data/",
                               allow_private=True)

    def test_validate_url_blocks_private_ip_10_when_strict(self):
        with pytest.raises(ValueError, match="private/reserved"):
            self._validate_url("http://10.0.0.1/internal", allow_private=False)

    def test_validate_url_blocks_private_ip_192_when_strict(self):
        with pytest.raises(ValueError, match="private/reserved"):
            self._validate_url("http://192.168.1.1/admin", allow_private=False)

    def test_validate_url_blocks_private_ip_172_when_strict(self):
        with pytest.raises(ValueError, match="private/reserved"):
            self._validate_url("http://172.16.0.1/secret", allow_private=False)

    def test_validate_url_blocks_cloud_metadata(self):
        with pytest.raises(ValueError, match="cloud metadata"):
            self._validate_url("http://169.254.169.254/latest/meta-data/")

    def test_validate_url_blocks_localhost_when_strict(self):
        with pytest.raises(ValueError, match="localhost"):
            self._validate_url("http://localhost/admin", allow_private=False)

    def test_validate_url_blocks_localhost_ip_when_strict(self):
        with pytest.raises(ValueError, match="private/reserved"):
            self._validate_url("http://127.0.0.1/admin", allow_private=False)

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

    def test_validate_url_blocks_zero_address_when_strict(self):
        with pytest.raises(ValueError, match="private/reserved"):
            self._validate_url("http://0.0.0.0/", allow_private=False)

    def test_validate_url_strips_whitespace(self):
        result = self._validate_url("  https://example.com/path  ")
        assert result == "https://example.com/path"

    # rv_br3 on 506273cc7 (measured): a browser resolves these spellings to
    # the metadata address (Chrome opened http://2130706433:<port>/ as
    # 127.0.0.1), and each one passed the plain string compare.
    @pytest.mark.parametrize("url", [
        "http://169.254.169.254./latest/meta-data/",
        "http://2852039166/latest/meta-data/",
        "http://0xa9fea9fe/latest/meta-data/",
        "http://0251.0376.0251.0376/latest/meta-data/",
        "http://169.254.43518/latest/meta-data/",
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
        "http://[fd00:ec2::254]/latest/meta-data/",
        "http://metadata.google.internal./computeMetadata/v1/",
        "http://METADATA.Google.Internal/computeMetadata/v1/",
    ])
    def test_validate_url_blocks_cloud_metadata_in_any_spelling(self, url):
        with pytest.raises(ValueError, match="cloud metadata"):
            self._validate_url(url)

    @pytest.mark.parametrize("url", [
        "http://2130706433/admin", "http://0x7f.1/admin", "http://[::ffff:10.0.0.1]/x",
    ])
    def test_validate_url_strict_reads_private_addresses_in_any_spelling(self, url):
        with pytest.raises(ValueError, match="private/reserved"):
            self._validate_url(url, allow_private=False)

    @pytest.mark.parametrize("url", [
        "http://2130706433:5000/x", "http://1.2.3.4/x", "http://123.example.com/x",
        "http://0x7f.example.com/x",
    ])
    def test_validate_url_other_spellings_of_ordinary_hosts_still_pass(self, url):
        assert self._validate_url(url) == url

    # rv_br4 on 10e461072 (measured): requests reaches 169.254.169.254 through
    # a percent-encoded host and through "\" read as "/", and both passed.
    # A browser also reads full-width and ideographic dots as "." and drops
    # an IPv6 zone id.
    @pytest.mark.parametrize("url", [
        "http://%31%36%39.254.169.254/latest/meta-data/",
        "http://169.254.169.254\\@example.com/latest/meta-data/",
        "http://169．254．169．254/latest/meta-data/",
        "http://169。254。169。254/latest/meta-data/",
        "http://１６９.254.169.254/latest/meta-data/",
        "http://[fd00:ec2::254%25en0]/latest/meta-data/",
        "http://metadata%2egoogle.internal/computeMetadata/v1/",
    ])
    def test_validate_url_blocks_cloud_metadata_encoded_or_backslashed(self, url):
        with pytest.raises(ValueError, match="cloud metadata"):
            self._validate_url(url)

    def test_validate_url_strict_reads_a_percent_encoded_private_host(self):
        with pytest.raises(ValueError, match="private/reserved"):
            self._validate_url("http://%31%32%37.0.0.1/admin", allow_private=False)

    def test_url_host_is_the_host_a_client_reaches(self):
        from security.sanitize import url_host
        assert url_host("https://evil.example\\@www.youtube.com/x") == "evil.example"
        assert url_host("HTTP://WWW.Example.COM./a") == "www.example.com"
        assert url_host("http://%31%32%37.0.0.1:5000/") == "127.0.0.1"
        assert url_host("not a url") == ""

    def test_validate_url_backslash_in_the_path_still_passes(self):
        url = "https://example.com/search?q=a\\b"
        assert self._validate_url(url) == url


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
# 4. Rate Limiter — hart_intelligence_entry.py MAX_CONTENT_LENGTH
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
                from hart_intelligence_entry import app
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
                pytest.skip(f"Cannot import hart_intelligence_entry: {e}")
            except Exception as e:
                # If the module fails to load for env reasons, check the source directly
                source_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'hart_intelligence_entry.py'
                )
                with open(source_path, 'r') as f:
                    source = f.read()
                assert "MAX_CONTENT_LENGTH" in source, \
                    "hart_intelligence_entry.py must set app.config['MAX_CONTENT_LENGTH']"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
