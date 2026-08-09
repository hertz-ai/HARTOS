"""
Tests for integrations/agent_engine/tool_allowlist.py — model tier tool restrictions.

Run: pytest tests/unit/test_tool_allowlist.py -v --noconftest
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from integrations.agent_engine.tool_allowlist import (
    filter_tools_for_model, check_tool_allowed,
    _resolve_tier, get_capability_summary,
    _FAST_TOOLS, _BALANCED_TOOLS,
)

# The registry is the real boundary _resolve_tier depends on. Import it at
# module load; if a heavy optional dep (torch/zipvoice) ever breaks the import,
# the guard lets the registry-touching tests skip cleanly instead of erroring.
try:
    from integrations.agent_engine.model_registry import (
        model_registry, ModelBackend, ModelTier,
    )
    _REGISTRY_IMPORT_OK = True
    _REGISTRY_IMPORT_ERR = None
except Exception as _err:  # pragma: no cover - only if optional deps break
    _REGISTRY_IMPORT_OK = False
    _REGISTRY_IMPORT_ERR = _err

_SKIP_REASON = f"model_registry unavailable: {_REGISTRY_IMPORT_ERR}"


def _make_tools(*names):
    return [{'name': n, 'description': f'{n} tool'} for n in names]


class _FakeBackend:
    """Minimal stand-in for a ModelBackend — carries only the .tier attribute
    that the real registry return value exposes."""
    __slots__ = ('tier',)

    def __init__(self, tier):
        self.tier = tier


class TestFilterToolsForModel(unittest.TestCase):
    """Filter tool list by model tier."""

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_fast_model_restricted_to_read_only(self, mock_tier):
        from integrations.agent_engine.model_registry import ModelTier
        mock_tier.return_value = ModelTier.FAST

        all_tools = _make_tools('web_search', 'read_file', 'write_file', 'delete_file')
        filtered = filter_tools_for_model('groq-llama', all_tools)

        names = [t['name'] for t in filtered]
        self.assertIn('web_search', names)
        self.assertIn('read_file', names)
        self.assertNotIn('write_file', names)
        self.assertNotIn('delete_file', names)

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_balanced_model_gets_write_tools(self, mock_tier):
        from integrations.agent_engine.model_registry import ModelTier
        mock_tier.return_value = ModelTier.BALANCED

        all_tools = _make_tools('web_search', 'write_file', 'send_message', 'delete_file')
        filtered = filter_tools_for_model('gpt-4o-mini', all_tools)

        names = [t['name'] for t in filtered]
        self.assertIn('web_search', names)
        self.assertIn('write_file', names)
        self.assertIn('send_message', names)
        self.assertNotIn('delete_file', names)

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_expert_model_unrestricted(self, mock_tier):
        from integrations.agent_engine.model_registry import ModelTier
        mock_tier.return_value = ModelTier.EXPERT

        all_tools = _make_tools('web_search', 'write_file', 'delete_file', 'admin_panel')
        filtered = filter_tools_for_model('gpt-4.1', all_tools)

        self.assertEqual(len(filtered), len(all_tools))

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_unknown_model_fail_closed(self, mock_tier):
        mock_tier.return_value = None

        all_tools = _make_tools('web_search', 'read_file')
        filtered = filter_tools_for_model('unknown-model', all_tools)

        self.assertEqual(len(filtered), 0, "Unknown model should get no tools (fail-closed)")


class TestCheckToolAllowed(unittest.TestCase):
    """Gate function for individual tool checks."""

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_fast_allowed_read(self, mock_tier):
        from integrations.agent_engine.model_registry import ModelTier
        mock_tier.return_value = ModelTier.FAST

        allowed, reason = check_tool_allowed('groq-llama', 'web_search')
        self.assertTrue(allowed)

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_fast_blocked_write(self, mock_tier):
        from integrations.agent_engine.model_registry import ModelTier
        mock_tier.return_value = ModelTier.FAST

        allowed, reason = check_tool_allowed('groq-llama', 'write_file')
        self.assertFalse(allowed)
        self.assertIn('not allowed', reason)

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_unknown_blocked(self, mock_tier):
        mock_tier.return_value = None

        allowed, reason = check_tool_allowed('mystery', 'web_search')
        self.assertFalse(allowed)
        self.assertIn('fail-closed', reason)


class TestToolSets(unittest.TestCase):
    """Validate tool set hierarchy."""

    def test_fast_is_subset_of_balanced(self):
        self.assertTrue(_FAST_TOOLS.issubset(_BALANCED_TOOLS))

    def test_fast_has_no_write_tools(self):
        write_tools = {'write_file', 'send_message', 'create_task', 'update_task'}
        self.assertEqual(len(_FAST_TOOLS & write_tools), 0)


# ─────────────────────────────────────────────────────────────────────────
# The real authorization root: _resolve_tier(). Every prior test mocked it
# out, so a registry-lookup regression (wrong method name, dict-vs-object
# return shape, or the fail-closed degrade branch) was invisible. These
# tests exercise the REAL function, mocking only the registry boundary.
# ─────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(_REGISTRY_IMPORT_OK, _SKIP_REASON)
class TestResolveTierBoundaryMocked(unittest.TestCase):
    """Drive _resolve_tier by mocking only model_registry.get_model()."""

    def test_backend_object_tier_attribute(self):
        # Real registry returns ModelBackend objects; tier is read off .tier.
        with patch.object(model_registry, 'get_model',
                          return_value=_FakeBackend(ModelTier.BALANCED)):
            self.assertIs(_resolve_tier('m'), ModelTier.BALANCED)

    def test_dict_entry_tier_key(self):
        # Forward-compat: a dict-shaped entry resolves via its 'tier' key.
        with patch.object(model_registry, 'get_model',
                          return_value={'tier': ModelTier.EXPERT}):
            self.assertIs(_resolve_tier('m'), ModelTier.EXPERT)

    def test_dict_entry_model_tier_fallback(self):
        # 'tier' absent → fall back to the 'model_tier' key.
        with patch.object(model_registry, 'get_model',
                          return_value={'model_tier': ModelTier.FAST}):
            self.assertIs(_resolve_tier('m'), ModelTier.FAST)

    def test_registry_returns_none_is_unknown(self):
        with patch.object(model_registry, 'get_model', return_value=None):
            self.assertIsNone(_resolve_tier('ghost'))

    def test_registry_raises_fails_closed(self):
        # Degrade branch: any registry error must resolve to None so the
        # caller denies all tools rather than over-permitting.
        with patch.object(model_registry, 'get_model',
                          side_effect=RuntimeError('registry down')):
            self.assertIsNone(_resolve_tier('m'))

    def test_registry_attribute_error_fails_closed(self):
        # Mirrors the historical bug shape (calling a non-existent registry
        # method raised AttributeError): it must degrade to None, not raise.
        with patch.object(model_registry, 'get_model',
                          side_effect=AttributeError('no such method')):
            self.assertIsNone(_resolve_tier('m'))


@unittest.skipUnless(_REGISTRY_IMPORT_OK, _SKIP_REASON)
class TestResolveTierRealRegistry(unittest.TestCase):
    """End-to-end through the REAL model_registry — no boundary mock. This is
    the test that actually catches a get()/get_model() + object-vs-dict
    regression: it registers real ModelBackends and asserts the whole
    filter/gate pipeline reflects their tier."""

    _CFG = {'model': 'x', 'api_key': 'dummy', 'base_url': 'inprocess://test'}
    _IDS = {
        'ta-fast-model': ModelTier.FAST,
        'ta-balanced-model': ModelTier.BALANCED,
        'ta-expert-model': ModelTier.EXPERT,
        'ta-draft-model': ModelTier.DRAFT,
    }

    def setUp(self):
        for mid, tier in self._IDS.items():
            model_registry.register(
                ModelBackend(mid, mid, tier, dict(self._CFG)))

    def tearDown(self):
        for mid in self._IDS:
            model_registry.unregister(mid)

    def test_real_fast_model_resolves_fast(self):
        self.assertIs(_resolve_tier('ta-fast-model'), ModelTier.FAST)

    def test_real_balanced_model_resolves_balanced(self):
        self.assertIs(_resolve_tier('ta-balanced-model'), ModelTier.BALANCED)

    def test_real_expert_model_resolves_expert(self):
        self.assertIs(_resolve_tier('ta-expert-model'), ModelTier.EXPERT)

    def test_real_draft_model_resolves_draft(self):
        self.assertIs(_resolve_tier('ta-draft-model'), ModelTier.DRAFT)

    def test_real_unknown_model_resolves_none(self):
        self.assertIsNone(_resolve_tier('ta-does-not-exist-zzz'))

    def test_real_fast_model_filtered_to_read_only(self):
        tools = _make_tools('web_search', 'read_file', 'write_file',
                            'send_message', 'delete_file')
        filtered = [t['name'] for t in filter_tools_for_model('ta-fast-model', tools)]
        self.assertIn('web_search', filtered)
        self.assertIn('read_file', filtered)
        self.assertNotIn('write_file', filtered)
        self.assertNotIn('send_message', filtered)
        self.assertNotIn('delete_file', filtered)

    def test_real_fast_model_gate_allows_read_denies_write(self):
        ok, _ = check_tool_allowed('ta-fast-model', 'web_search')
        self.assertTrue(ok)
        denied, reason = check_tool_allowed('ta-fast-model', 'write_file')
        self.assertFalse(denied)
        self.assertIn('not allowed', reason)

    def test_real_balanced_model_gets_write_tools(self):
        ok, _ = check_tool_allowed('ta-balanced-model', 'write_file')
        self.assertTrue(ok)
        ok2, _ = check_tool_allowed('ta-balanced-model', 'web_search')
        self.assertTrue(ok2)

    def test_real_expert_model_unrestricted(self):
        tools = _make_tools('web_search', 'write_file', 'delete_file', 'admin_panel')
        self.assertEqual(len(filter_tools_for_model('ta-expert-model', tools)), len(tools))
        ok, _ = check_tool_allowed('ta-expert-model', 'admin_panel')
        self.assertTrue(ok)

    def test_real_unknown_model_fail_closed(self):
        tools = _make_tools('web_search', 'read_file')
        self.assertEqual(filter_tools_for_model('ta-does-not-exist-zzz', tools), [])
        ok, reason = check_tool_allowed('ta-does-not-exist-zzz', 'web_search')
        self.assertFalse(ok)
        self.assertIn('fail-closed', reason)

    def test_real_draft_model_not_unrestricted(self):
        # DRAFT is a tiny first-responder/classifier; it must NOT receive
        # write/admin tools. An unmapped tier must fail closed, never be read
        # as EXPERT-unrestricted.
        tools = _make_tools('web_search', 'write_file', 'delete_file', 'admin_panel')
        filtered = [t['name'] for t in filter_tools_for_model('ta-draft-model', tools)]
        self.assertNotIn('write_file', filtered)
        self.assertNotIn('delete_file', filtered)
        self.assertNotIn('admin_panel', filtered)
        ok, _ = check_tool_allowed('ta-draft-model', 'write_file')
        self.assertFalse(ok)


@unittest.skipUnless(_REGISTRY_IMPORT_OK, _SKIP_REASON)
class TestUnmappedTierFailsClosed(unittest.TestCase):
    """A resolved-but-unmapped tier (DRAFT) must fail closed, not be treated
    as unrestricted. Mocks only _resolve_tier to isolate the tier→tool map."""

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_draft_filter_fails_closed(self, mock_tier):
        mock_tier.return_value = ModelTier.DRAFT
        tools = _make_tools('web_search', 'write_file', 'admin_panel')
        self.assertEqual(
            filter_tools_for_model('draft-model', tools), [],
            "DRAFT has no tool mapping → must fail closed, not run unrestricted")

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_draft_gate_denies(self, mock_tier):
        mock_tier.return_value = ModelTier.DRAFT
        allowed, _ = check_tool_allowed('draft-model', 'write_file')
        self.assertFalse(allowed, "DRAFT tier must not be granted unrestricted access")

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_expert_still_unrestricted(self, mock_tier):
        # Guard: the fail-closed change must NOT break EXPERT (explicit None).
        mock_tier.return_value = ModelTier.EXPERT
        tools = _make_tools('web_search', 'write_file', 'admin_panel')
        self.assertEqual(len(filter_tools_for_model('x', tools)), len(tools))
        allowed, _ = check_tool_allowed('x', 'admin_panel')
        self.assertTrue(allowed)


@unittest.skipUnless(_REGISTRY_IMPORT_OK, _SKIP_REASON)
class TestFilterEdgeCases(unittest.TestCase):
    """Malformed / empty input handling for the filter."""

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_empty_tool_list_fast(self, mock_tier):
        mock_tier.return_value = ModelTier.FAST
        self.assertEqual(filter_tools_for_model('m', []), [])

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_empty_tool_list_expert(self, mock_tier):
        mock_tier.return_value = ModelTier.EXPERT
        self.assertEqual(filter_tools_for_model('m', []), [])

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_tool_missing_name_excluded_for_fast(self, mock_tier):
        # A tool dict with no 'name' key → name is None → not in allowlist →
        # dropped for a restricted tier (never accidentally allowed).
        mock_tier.return_value = ModelTier.FAST
        tools = [{'description': 'no name'}, {'name': 'web_search'}]
        filtered = filter_tools_for_model('m', tools)
        self.assertEqual([t.get('name') for t in filtered], ['web_search'])

    @patch('integrations.agent_engine.tool_allowlist._resolve_tier')
    def test_tool_missing_name_kept_for_expert(self, mock_tier):
        # Expert is unrestricted → whole list passes through unchanged.
        mock_tier.return_value = ModelTier.EXPERT
        tools = [{'description': 'no name'}, {'name': 'web_search'}]
        self.assertEqual(len(filter_tools_for_model('m', tools)), 2)


class TestCapabilitySummary(unittest.TestCase):
    """get_capability_summary() must always yield the static-tool phrases and
    never raise even when every dynamic source is unavailable."""

    def test_includes_static_tool_phrases(self):
        summary = get_capability_summary()
        self.assertIsInstance(summary, str)
        self.assertIn('web search', summary)   # _FAST_TOOLS phrase
        self.assertIn('write files', summary)  # _BALANCED_TOOLS phrase

    def test_returns_nonempty(self):
        self.assertTrue(len(get_capability_summary()) > 0)


if __name__ == '__main__':
    unittest.main()
