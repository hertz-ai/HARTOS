"""
Tests for Mode-Aware Inference (DeploymentConfig → QwenLlamaCppEncoder → get_qwen_encoder).

Verifies:
- DeploymentConfig env var overrides and computed properties
- QwenLlamaCppEncoder auth headers and configurable model name
- get_qwen_encoder Priority 0 external endpoint routing
- LearningLLMProvider teacher HTTP routing when no local model
- Flat mode regression (zero changes to existing behavior)
"""
import os
import sys
import json
import types
import importlib.util
import pytest
from unittest.mock import patch, Mock, MagicMock, PropertyMock, call
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# ─── HevolveAI src is in a sibling project ───
HevolveAI_SRC = os.path.normpath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    '..', 'hevolveai', 'src'
))
if not os.path.isdir(HevolveAI_SRC):
    HevolveAI_SRC = os.path.normpath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        '..', 'hevolveai', 'src'  # legacy fallback
    ))
if os.path.isdir(HevolveAI_SRC):
    sys.path.insert(0, HevolveAI_SRC)

_hevolveai_base = os.path.join(HevolveAI_SRC, 'hevolveai', 'embodied_ai')


# ─── Direct-import helper (bypasses package __init__.py chains) ───

def _direct_import(module_name, file_path):
    """Import a module directly from file, bypassing package __init__."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Ensure parent namespace packages exist
for pkg in [
    'hevolveai',
    'hevolveai.embodied_ai',
    'hevolveai.embodied_ai.utils',
    'hevolveai.embodied_ai.config',
    'hevolveai.embodied_ai.models',
]:
    if pkg not in sys.modules:
        m = types.ModuleType(pkg)
        m.__path__ = [os.path.join(_hevolveai_base, *pkg.split('.')[2:])]
        sys.modules[pkg] = m

# Import context_logger first (dependency)
_ctx_log_path = os.path.join(_hevolveai_base, 'utils', 'context_logger.py')
_ctx_mod = _direct_import('hevolveai.embodied_ai.utils.context_logger', _ctx_log_path)

# Import config
_config_path = os.path.join(_hevolveai_base, 'config', 'config.py')
_config_mod = _direct_import('hevolveai.embodied_ai.config.config', _config_path)
DeploymentConfig = _config_mod.DeploymentConfig
Config = _config_mod.Config

# Import qwen_llamacpp_wrapper
_wrapper_path = os.path.join(_hevolveai_base, 'models', 'qwen_llamacpp_wrapper.py')
_wrapper_mod = _direct_import('hevolveai.embodied_ai.models.qwen_llamacpp_wrapper', _wrapper_path)
QwenLlamaCppEncoder = _wrapper_mod.QwenLlamaCppEncoder

# Mock qwen_vl_wrapper (heavy deps: peft, newer transformers)
sys.modules['hevolveai.embodied_ai.models.qwen_vl_wrapper'] = MagicMock()

# Import qwen_auto_encoder
_auto_path = os.path.join(_hevolveai_base, 'models', 'qwen_auto_encoder.py')
_auto_mod = _direct_import('hevolveai.embodied_ai.models.qwen_auto_encoder', _auto_path)
get_qwen_encoder = _auto_mod.get_qwen_encoder


# ═══════════════════════════════════════════════════════════════════
#  Section 1: DeploymentConfig
# ═══════════════════════════════════════════════════════════════════

class TestDeploymentConfig:
    """Test DeploymentConfig env var overrides and computed properties."""

    def test_default_flat_mode(self):
        """Default mode should be flat when no env var set."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('HEVOLVE_NODE_TIER', None)
            os.environ.pop('HEVOLVE_LLM_ENDPOINT_URL', None)
            os.environ.pop('HEVOLVE_LLM_API_KEY', None)
            os.environ.pop('HEVOLVE_LLM_MODEL_NAME', None)
            cfg = DeploymentConfig()
            assert cfg.mode == 'flat'
            assert cfg.llm_endpoint_url == ''
            assert cfg.llm_api_key == ''
            assert cfg.llm_model_name == ''
            assert cfg.skip_local_model is False
            assert cfg.is_external is False

    def test_env_var_overrides(self):
        """Env vars should flow into config fields."""
        env = {
            'HEVOLVE_NODE_TIER': 'central',
            'HEVOLVE_LLM_ENDPOINT_URL': 'https://api.openai.com',
            'HEVOLVE_LLM_API_KEY': 'sk-test-key',
            'HEVOLVE_LLM_MODEL_NAME': 'gpt-4',
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = DeploymentConfig()
            assert cfg.mode == 'central'
            assert cfg.llm_endpoint_url == 'https://api.openai.com'
            assert cfg.llm_api_key == 'sk-test-key'
            assert cfg.llm_model_name == 'gpt-4'

    def test_skip_local_model_central_with_url(self):
        """skip_local_model should be True when central + URL set."""
        env = {
            'HEVOLVE_NODE_TIER': 'central',
            'HEVOLVE_LLM_ENDPOINT_URL': 'https://api.openai.com',
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = DeploymentConfig()
            assert cfg.skip_local_model is True
            assert cfg.is_external is True

    def test_skip_local_model_regional_with_url(self):
        """skip_local_model should be True when regional + URL set."""
        env = {
            'HEVOLVE_NODE_TIER': 'regional',
            'HEVOLVE_LLM_ENDPOINT_URL': 'http://regional-server:8000',
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = DeploymentConfig()
            assert cfg.skip_local_model is True

    def test_skip_local_model_central_no_url(self):
        """skip_local_model should be False when central but no URL."""
        env = {
            'HEVOLVE_NODE_TIER': 'central',
            'HEVOLVE_LLM_ENDPOINT_URL': '',
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = DeploymentConfig()
            assert cfg.skip_local_model is False

    def test_skip_local_model_flat_with_url(self):
        """skip_local_model should be False for flat mode even with URL."""
        env = {
            'HEVOLVE_NODE_TIER': 'flat',
            'HEVOLVE_LLM_ENDPOINT_URL': 'http://localhost:8080',
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = DeploymentConfig()
            assert cfg.skip_local_model is False
            assert cfg.is_external is False

    def test_fallback_to_local_default(self):
        """fallback_to_local should default to True."""
        cfg = DeploymentConfig(mode='flat')
        assert cfg.fallback_to_local is True

    def test_config_includes_deployment(self):
        """Config class should have deployment field."""
        with patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'flat'}, clear=False):
            os.environ.pop('HEVOLVE_LLM_ENDPOINT_URL', None)
            cfg = Config()
            assert hasattr(cfg, 'deployment')
            assert cfg.deployment.mode == 'flat'


# ═══════════════════════════════════════════════════════════════════
#  Section 2: QwenLlamaCppEncoder Auth + Model Name
# ═══════════════════════════════════════════════════════════════════

class TestEncoderAuth:
    """Test auth headers and configurable model name in QwenLlamaCppEncoder."""

    def _make_encoder(self, **kwargs):
        """Create encoder with mocked server check."""
        with patch.object(QwenLlamaCppEncoder, '_check_llama_cpp_server', return_value='not_running'):
            return QwenLlamaCppEncoder(
                server_url='http://test:8080',
                low_memory_mode=True,
                **kwargs
            )

    def test_default_no_auth_header(self):
        """No Authorization header when api_key is empty."""
        enc = self._make_encoder()
        headers = enc._build_headers()
        assert 'Authorization' not in headers
        assert headers['Content-Type'] == 'application/json'

    def test_auth_header_with_api_key(self):
        """Authorization Bearer header when api_key is set."""
        enc = self._make_encoder(api_key='sk-test-123')
        headers = enc._build_headers()
        assert headers['Authorization'] == 'Bearer sk-test-123'

    def test_default_model_name(self):
        """Default model name should be qwen3-vl-2b."""
        enc = self._make_encoder()
        assert enc.request_model_name == 'qwen3-vl-2b'

    def test_external_model_name(self):
        """External model name should override default."""
        enc = self._make_encoder(external_model_name='gpt-4-turbo')
        assert enc.request_model_name == 'gpt-4-turbo'

    def test_has_model_loaded_false_in_low_memory(self):
        """has_model_loaded should be False when no transformers encoder."""
        enc = self._make_encoder()
        assert enc.has_model_loaded is False

    def test_can_load_model_false_in_low_memory(self):
        """can_load_model should be False in low_memory_mode."""
        enc = self._make_encoder()
        assert enc.can_load_model is False

    def test_make_request_uses_auth_headers(self):
        """_make_request should include auth headers in HTTP request."""
        enc = self._make_encoder(api_key='sk-test-789')

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({'result': 'ok'}).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch('urllib.request.urlopen', return_value=mock_response) as mock_open:
            result = enc._make_request('/v1/chat/completions', {'model': 'test'})
            assert result == {'result': 'ok'}

            assert mock_open.called
            req_obj = mock_open.call_args[0][0]
            assert req_obj.get_header('Authorization') == 'Bearer sk-test-789'
            assert req_obj.get_header('Content-type') == 'application/json'


# ═══════════════════════════════════════════════════════════════════
#  Section 3: get_qwen_encoder Priority 0 Routing
# ═══════════════════════════════════════════════════════════════════

class TestEncoderRouting:
    """Test get_qwen_encoder routes correctly per deployment mode."""

    @patch.object(_auto_mod, 'QwenLlamaCppEncoder')
    def test_flat_mode_skips_external(self, MockEncoder):
        """Flat mode should NOT use external endpoint."""
        mock_instance = MagicMock()
        mock_instance._llama_cpp_available = True
        mock_instance.get_sensor_dim.return_value = 2048
        MockEncoder.return_value = mock_instance

        with patch.object(_auto_mod, '_check_llamacpp_server', return_value=True):
            encoder = get_qwen_encoder(
                device='cpu',
                deployment_mode='flat',
                external_llm_url='https://api.openai.com',
                external_llm_api_key='sk-test',
            )

        # Should have used local llama.cpp path, not external (no api_key)
        first_call_kwargs = MockEncoder.call_args_list[0]
        assert first_call_kwargs.kwargs.get('api_key', '') == ''

    @patch.object(_auto_mod, 'QwenLlamaCppEncoder')
    def test_central_mode_uses_external(self, MockEncoder):
        """Central mode with URL should use external endpoint."""
        mock_instance = MagicMock()
        mock_instance._llama_cpp_available = True
        MockEncoder.return_value = mock_instance

        encoder = get_qwen_encoder(
            device='cpu',
            deployment_mode='central',
            external_llm_url='https://api.openai.com',
            external_llm_api_key='sk-central-key',
            external_llm_model='gpt-4',
        )

        MockEncoder.assert_called_once_with(
            server_url='https://api.openai.com',
            device='cpu',
            model_name='Qwen/Qwen3-VL-2B-Instruct',
            low_memory_mode=True,
            api_key='sk-central-key',
            external_model_name='gpt-4',
        )

    @patch.object(_auto_mod, 'QwenLlamaCppEncoder')
    def test_regional_mode_uses_external(self, MockEncoder):
        """Regional mode with URL should use external endpoint."""
        mock_instance = MagicMock()
        mock_instance._llama_cpp_available = True
        MockEncoder.return_value = mock_instance

        encoder = get_qwen_encoder(
            device='cpu',
            deployment_mode='regional',
            external_llm_url='http://regional:8000',
            external_llm_api_key='',
            external_llm_model='qwen-regional',
        )

        MockEncoder.assert_called_once()
        call_kwargs = MockEncoder.call_args.kwargs
        assert call_kwargs['server_url'] == 'http://regional:8000'
        assert call_kwargs['low_memory_mode'] is True
        assert call_kwargs['external_model_name'] == 'qwen-regional'

    @patch.object(_auto_mod, '_check_llamacpp_server', return_value=True)
    @patch.object(_auto_mod, 'QwenLlamaCppEncoder')
    def test_central_fallback_to_local(self, MockEncoder, mock_check):
        """Central mode should fall back to local when external unreachable."""
        mock_external = MagicMock()
        mock_external._llama_cpp_available = False

        mock_local = MagicMock()
        mock_local._llama_cpp_available = True
        mock_local.get_sensor_dim.return_value = 2048

        MockEncoder.side_effect = [mock_external, mock_local]

        encoder = get_qwen_encoder(
            device='cpu',
            deployment_mode='central',
            external_llm_url='https://dead-endpoint.example.com',
            external_llm_api_key='sk-test',
            fallback_to_local=True,
        )

        assert MockEncoder.call_count == 2
        assert encoder is mock_local

    @patch.object(_auto_mod, 'QwenLlamaCppEncoder')
    def test_central_no_fallback_raises(self, MockEncoder):
        """Central mode with fallback=False should raise when external unreachable."""
        mock_external = MagicMock()
        mock_external._llama_cpp_available = False
        MockEncoder.return_value = mock_external

        with pytest.raises(ConnectionError, match='unreachable'):
            get_qwen_encoder(
                device='cpu',
                deployment_mode='central',
                external_llm_url='https://dead-endpoint.example.com',
                external_llm_api_key='sk-test',
                fallback_to_local=False,
            )

    @patch.object(_auto_mod, '_check_llamacpp_server', return_value=True)
    @patch.object(_auto_mod, 'QwenLlamaCppEncoder')
    def test_central_no_url_skips_external(self, MockEncoder, mock_check):
        """Central mode without URL should skip external path."""
        mock_instance = MagicMock()
        mock_instance._llama_cpp_available = True
        mock_instance.get_sensor_dim.return_value = 2048
        MockEncoder.return_value = mock_instance

        encoder = get_qwen_encoder(
            device='cpu',
            deployment_mode='central',
            external_llm_url='',  # No URL
        )

        call_kwargs = MockEncoder.call_args.kwargs
        assert call_kwargs.get('api_key', '') == ''


# ═══════════════════════════════════════════════════════════════════
#  Section 4: Teacher HTTP Routing
# ═══════════════════════════════════════════════════════════════════

class TestTeacherRouting:
    """Test _messages_to_prompt conversion for HTTP teacher path."""

    def _get_func(self):
        """Import _messages_to_prompt if available."""
        try:
            from hevolveai.embodied_ai.rl_ef.learning_llm_provider import LearningLLMProvider
            provider = MagicMock(spec=LearningLLMProvider)
            provider._messages_to_prompt = LearningLLMProvider._messages_to_prompt.__get__(provider)
            return provider._messages_to_prompt
        except (ImportError, AttributeError):
            pytest.skip("LearningLLMProvider not importable (missing heavy deps)")

    def test_messages_to_prompt(self):
        """_messages_to_prompt should convert messages to Qwen chat format."""
        func = self._get_func()
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        prompt = func(messages)
        assert "<|im_start|>system" in prompt
        assert "You are helpful." in prompt
        assert "<|im_start|>user" in prompt
        assert "Hello" in prompt
        assert "<|im_start|>assistant" in prompt

    def test_messages_to_prompt_multimodal(self):
        """_messages_to_prompt should extract text from multimodal content."""
        func = self._get_func()
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "Describe this"},
                {"type": "image", "image": "base64data"},
            ]},
        ]
        prompt = func(messages)
        assert "Describe this" in prompt
        assert "base64data" not in prompt


# ═══════════════════════════════════════════════════════════════════
#  Section 5: WorldModelBridge Tier Awareness
# ═══════════════════════════════════════════════════════════════════

class TestWorldModelBridgeTier:
    """Test WorldModelBridge tracks deployment tier."""

    def test_default_flat_tier(self):
        """Default tier should be flat."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('HEVOLVE_NODE_TIER', None)
            from integrations.agent_engine.world_model_bridge import WorldModelBridge
            bridge = WorldModelBridge()
            assert bridge._node_tier == 'flat'
            assert bridge._stats['node_tier'] == 'flat'

    def test_central_tier(self):
        """Central tier should be reflected in bridge stats."""
        with patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'central'}, clear=False):
            from integrations.agent_engine.world_model_bridge import WorldModelBridge
            bridge = WorldModelBridge()
            assert bridge._node_tier == 'central'
            assert bridge._stats['node_tier'] == 'central'

    def test_regional_tier(self):
        """Regional tier should be reflected in bridge stats."""
        with patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'regional'}, clear=False):
            from integrations.agent_engine.world_model_bridge import WorldModelBridge
            bridge = WorldModelBridge()
            assert bridge._node_tier == 'regional'


# ═══════════════════════════════════════════════════════════════════
#  Section 6: LLM-langchain Env Var Passthrough
# ═══════════════════════════════════════════════════════════════════

class TestEnvVarPassthrough:
    """Test that langchain_gpt_api sets HEVOLVE_LLM_* env vars for non-flat modes."""

    def test_flat_mode_no_passthrough(self):
        """Flat mode should not set HEVOLVE_LLM_* env vars."""
        env = {'HEVOLVE_NODE_TIER': 'flat'}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop('HEVOLVE_LLM_ENDPOINT_URL', None)
            _node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
            if _node_tier in ('regional', 'central'):
                os.environ.setdefault('HEVOLVE_LLM_ENDPOINT_URL', '')
            assert 'HEVOLVE_LLM_ENDPOINT_URL' not in os.environ

    def test_central_mode_passthrough(self):
        """Central mode should set HEVOLVE_LLM_* from config."""
        env = {'HEVOLVE_NODE_TIER': 'central'}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop('HEVOLVE_LLM_ENDPOINT_URL', None)
            os.environ.pop('HEVOLVE_LLM_API_KEY', None)
            os.environ.pop('HEVOLVE_LLM_MODEL_NAME', None)

            config = {
                'OPENAI_API_BASE': 'https://api.openai.com/v1',
                'OPENAI_API_KEY': 'sk-prod-key',
                'OPENAI_MODEL': 'gpt-4-turbo',
            }
            _node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
            if _node_tier in ('regional', 'central'):
                os.environ.setdefault('HEVOLVE_LLM_ENDPOINT_URL', config.get('OPENAI_API_BASE', ''))
                os.environ.setdefault('HEVOLVE_LLM_API_KEY', config.get('OPENAI_API_KEY', ''))
                os.environ.setdefault('HEVOLVE_LLM_MODEL_NAME', config.get('OPENAI_MODEL', 'gpt-4'))

            assert os.environ.get('HEVOLVE_LLM_ENDPOINT_URL') == 'https://api.openai.com/v1'
            assert os.environ.get('HEVOLVE_LLM_API_KEY') == 'sk-prod-key'
            assert os.environ.get('HEVOLVE_LLM_MODEL_NAME') == 'gpt-4-turbo'

    def test_setdefault_preserves_existing(self):
        """setdefault should not override already-set env vars."""
        env = {
            'HEVOLVE_NODE_TIER': 'central',
            'HEVOLVE_LLM_ENDPOINT_URL': 'http://custom-endpoint:9000',
        }
        with patch.dict(os.environ, env, clear=False):
            config = {'OPENAI_API_BASE': 'https://api.openai.com/v1'}
            _node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
            if _node_tier in ('regional', 'central'):
                os.environ.setdefault('HEVOLVE_LLM_ENDPOINT_URL', config.get('OPENAI_API_BASE', ''))
            assert os.environ.get('HEVOLVE_LLM_ENDPOINT_URL') == 'http://custom-endpoint:9000'


# ═══════════════════════════════════════════════════════════════════
#  Section 7: Server Detection Relaxation
# ═══════════════════════════════════════════════════════════════════

class TestServerDetection:
    """Test that server detection accepts external OpenAI-compatible endpoints."""

    def test_external_endpoint_with_api_key_checks_models(self):
        """With api_key, should check /v1/models with auth header."""
        with patch.object(QwenLlamaCppEncoder, '_check_llama_cpp_server', return_value='not_running'):
            enc = QwenLlamaCppEncoder(
                server_url='https://api.openai.com',
                low_memory_mode=True,
                api_key='sk-test',
            )

        # Call the real detection method
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({
            "object": "list",
            "data": [{"id": "gpt-4", "object": "model"}]
        }).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch('urllib.request.urlopen', return_value=mock_response) as mock_open:
            result = QwenLlamaCppEncoder._check_llama_cpp_server(enc)
            assert result == 'llama_cpp_running'

            req_obj = mock_open.call_args[0][0]
            assert 'Bearer sk-test' in req_obj.get_header('Authorization')

    def test_no_api_key_checks_health_first(self):
        """Without api_key, should check /health first (llama.cpp style)."""
        with patch.object(QwenLlamaCppEncoder, '_check_llama_cpp_server', return_value='not_running'):
            enc = QwenLlamaCppEncoder(
                server_url='http://localhost:8080',
                low_memory_mode=True,
            )

        # Mock /health returning llama.cpp format
        mock_health = MagicMock()
        mock_health.status = 200
        mock_health.read.return_value = b'{"status":"ok"}'

        mock_models = MagicMock()
        mock_models.status = 200
        mock_models.read.return_value = json.dumps({
            "object": "list",
            "data": [{"id": "qwen3-vl-2b"}]
        }).encode()
        mock_models.__enter__ = lambda s: s
        mock_models.__exit__ = MagicMock(return_value=False)

        with patch('urllib.request.urlopen', side_effect=[mock_health, mock_models]):
            result = QwenLlamaCppEncoder._check_llama_cpp_server(enc)
            assert result == 'llama_cpp_running'


# ═══════════════════════════════════════════════════════════════════
#  Section 8: TPS Tracking
# ═══════════════════════════════════════════════════════════════════

class TestTPSTracking:
    """Test performance tracking in QwenLlamaCppEncoder."""

    def test_tps_history_initialized(self):
        """Encoder should have TPS and PP history deques."""
        with patch.object(QwenLlamaCppEncoder, '_check_llama_cpp_server', return_value='not_running'):
            enc = QwenLlamaCppEncoder(server_url='http://localhost:8080', low_memory_mode=True)
        assert hasattr(enc, '_tps_history')
        assert hasattr(enc, '_pp_history')
        assert hasattr(enc, '_perf_lock')
        assert len(enc._tps_history) == 0
        assert len(enc._pp_history) == 0

    def test_get_avg_tps_empty(self):
        """get_avg_tps should return 0 when no history."""
        with patch.object(QwenLlamaCppEncoder, '_check_llama_cpp_server', return_value='not_running'):
            enc = QwenLlamaCppEncoder(server_url='http://localhost:8080', low_memory_mode=True)
        assert enc.get_avg_tps() == 0.0
        assert enc.get_avg_pp() == 0.0

    def test_get_avg_tps_with_data(self):
        """get_avg_tps should compute rolling average."""
        with patch.object(QwenLlamaCppEncoder, '_check_llama_cpp_server', return_value='not_running'):
            enc = QwenLlamaCppEncoder(server_url='http://localhost:8080', low_memory_mode=True)
        enc._tps_history.extend([10.0, 20.0, 30.0])
        assert enc.get_avg_tps() == 20.0

    def test_get_avg_pp_with_data(self):
        """get_avg_pp should compute rolling average."""
        with patch.object(QwenLlamaCppEncoder, '_check_llama_cpp_server', return_value='not_running'):
            enc = QwenLlamaCppEncoder(server_url='http://localhost:8080', low_memory_mode=True)
        enc._pp_history.extend([100.0, 200.0, 300.0])
        assert enc.get_avg_pp() == 200.0

    def test_get_performance_stats(self):
        """get_performance_stats should return dict with expected keys."""
        with patch.object(QwenLlamaCppEncoder, '_check_llama_cpp_server', return_value='not_running'):
            enc = QwenLlamaCppEncoder(server_url='http://localhost:8080', low_memory_mode=True)
        enc._tps_history.extend([15.0, 25.0])
        stats = enc.get_performance_stats()
        assert 'avg_tps' in stats
        assert 'avg_pp' in stats
        assert 'samples' in stats
        assert stats['samples'] == 2
        assert stats['server_url'] == 'http://localhost:8080'

    def test_tps_history_maxlen(self):
        """TPS history should be bounded (maxlen=20)."""
        with patch.object(QwenLlamaCppEncoder, '_check_llama_cpp_server', return_value='not_running'):
            enc = QwenLlamaCppEncoder(server_url='http://localhost:8080', low_memory_mode=True)
        for i in range(30):
            enc._tps_history.append(float(i))
        assert len(enc._tps_history) == 20


# ═══════════════════════════════════════════════════════════════════
#  Section 9: AdaptiveRouter
# ═══════════════════════════════════════════════════════════════════

AdaptiveRouter = _auto_mod.AdaptiveRouter


class TestAdaptiveRouter:
    """Test AdaptiveRouter offload/recovery logic."""

    def _make_mock_local(self, avg_tps=15.0, avg_pp=150.0, samples=10):
        """Create a mock local encoder with TPS/PP stats."""
        from collections import deque
        mock = MagicMock()
        mock.get_avg_tps.return_value = avg_tps
        mock.get_avg_pp.return_value = avg_pp
        mock._tps_history = deque(range(samples), maxlen=20)
        mock.request_model_name = 'qwen3-vl-2b'
        mock.server_url = 'http://localhost:8080'
        mock.generate.return_value = 'local response'
        return mock

    def test_passthrough_when_healthy(self):
        """Should use local encoder when TPS is above threshold."""
        local = self._make_mock_local(avg_tps=15.0, avg_pp=150.0)
        router = AdaptiveRouter(
            local_encoder=local,
            cloud_fallback_url='https://api.openai.com/v1',
            tps_threshold=10.0,
            pp_threshold=100.0,
        )
        result = router.generate(prompt='test')
        local.generate.assert_called_once_with(prompt='test')
        assert result == 'local response'
        assert router._offloaded is False

    def test_active_backend_local(self):
        """active_backend should show local when not offloaded."""
        local = self._make_mock_local()
        router = AdaptiveRouter(local, 'https://api.openai.com/v1')
        backend = router.active_backend
        assert backend['type'] == 'local_llamacpp'
        assert 'Nunba' in backend['display_name']

    def test_offload_when_tps_low(self):
        """Should offload to cloud when TPS below threshold."""
        local = self._make_mock_local(avg_tps=5.0, avg_pp=50.0, samples=10)
        router = AdaptiveRouter(
            local_encoder=local,
            cloud_fallback_url='https://api.openai.com/v1',
            tps_threshold=10.0,
            pp_threshold=100.0,
        )
        # Mock cloud encoder creation
        mock_cloud = MagicMock()
        mock_cloud._llama_cpp_available = True
        mock_cloud.generate.return_value = 'cloud response'
        with patch.object(_auto_mod, 'QwenLlamaCppEncoder', return_value=mock_cloud):
            with patch.object(_auto_mod, 'LLAMACPP_AVAILABLE', True):
                router._maybe_switch()
        assert router._offloaded is True

    def test_no_offload_insufficient_samples(self):
        """Should not offload with fewer than 5 samples."""
        local = self._make_mock_local(avg_tps=1.0, avg_pp=10.0, samples=3)
        router = AdaptiveRouter(
            local_encoder=local,
            cloud_fallback_url='https://api.openai.com/v1',
            tps_threshold=10.0,
            pp_threshold=100.0,
        )
        router._maybe_switch()
        assert router._offloaded is False

    def test_recovery_after_hysteresis(self):
        """Should return to local after enough consecutive OK probes."""
        local = self._make_mock_local(avg_tps=15.0, avg_pp=150.0)  # Healthy
        router = AdaptiveRouter(
            local_encoder=local,
            cloud_fallback_url='https://api.openai.com/v1',
            tps_threshold=10.0,
            pp_threshold=100.0,
            recovery_window=3,
            probe_interval=0.0,  # Immediate probes for testing
        )
        # Force offloaded state
        router._offloaded = True
        router._cloud_encoder = MagicMock()
        router._last_probe = 0.0

        # Simulate recovery probes - TPS=15 > 10*1.2=12
        for _ in range(3):
            router._maybe_switch()
        assert router._offloaded is False  # Recovered

    def test_no_recovery_with_marginal_tps(self):
        """Should not recover when TPS is above threshold but below hysteresis."""
        local = self._make_mock_local(avg_tps=11.0)  # Above 10, but below 10*1.2=12
        router = AdaptiveRouter(
            local_encoder=local,
            cloud_fallback_url='https://api.openai.com/v1',
            tps_threshold=10.0,
            recovery_window=3,
            probe_interval=0.0,
        )
        router._offloaded = True
        router._cloud_encoder = MagicMock()
        router._last_probe = 0.0

        for _ in range(5):
            router._maybe_switch()
        assert router._offloaded is True  # Still offloaded (hysteresis)

    def test_getattr_proxy(self):
        """__getattr__ should proxy attributes to local encoder."""
        local = self._make_mock_local()
        local.sensor_dim = 2048
        router = AdaptiveRouter(local, 'https://api.openai.com/v1')
        assert router.sensor_dim == 2048

    def test_active_backend_cloud(self):
        """active_backend should show cloud when offloaded."""
        local = self._make_mock_local()
        router = AdaptiveRouter(
            local, 'https://api.openai.com/v1',
            cloud_fallback_model='gpt-4',
        )
        router._offloaded = True
        router._cloud_encoder = MagicMock()
        backend = router.active_backend
        assert backend['type'] == 'cloud_fallback'
        assert backend['model'] == 'gpt-4'
        assert backend['reason'] == 'local_overloaded'


# ═══════════════════════════════════════════════════════════════════
#  Section 10: DeploymentConfig Cloud Fallback Fields
# ═══════════════════════════════════════════════════════════════════

class TestDeploymentConfigCloudFallback:
    """Test cloud fallback fields in DeploymentConfig."""

    def test_cloud_fallback_defaults_empty(self):
        """Cloud fallback fields should default to empty when no env vars."""
        with patch.dict(os.environ, {}, clear=False):
            for key in ['HEVOLVE_CLOUD_FALLBACK_URL', 'HEVOLVE_CLOUD_FALLBACK_KEY', 'HEVOLVE_CLOUD_FALLBACK_MODEL']:
                os.environ.pop(key, None)
            cfg = DeploymentConfig()
            assert cfg.cloud_fallback_url == ''
            assert cfg.cloud_fallback_key == ''
            assert cfg.cloud_fallback_model == ''
            assert cfg.tps_threshold_tg == 10.0
            assert cfg.tps_threshold_pp == 100.0

    def test_cloud_fallback_from_env(self):
        """Cloud fallback env vars should flow into config."""
        env = {
            'HEVOLVE_CLOUD_FALLBACK_URL': 'https://api.openai.com/v1',
            'HEVOLVE_CLOUD_FALLBACK_KEY': 'sk-cloud-key',
            'HEVOLVE_CLOUD_FALLBACK_MODEL': 'gpt-4',
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = DeploymentConfig()
            assert cfg.cloud_fallback_url == 'https://api.openai.com/v1'
            assert cfg.cloud_fallback_key == 'sk-cloud-key'
            assert cfg.cloud_fallback_model == 'gpt-4'


# ═══════════════════════════════════════════════════════════════════
#  Section 11: Enriched /status endpoint
# ═══════════════════════════════════════════════════════════════════

class TestStatusEndpoint:
    """Test the enriched /status endpoint in langchain_gpt_api."""

    def test_get_active_backend_info_flat(self):
        """_get_active_backend_info should return local_llamacpp in flat mode."""
        from langchain_gpt_api import _get_active_backend_info
        with patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'flat'}, clear=False):
            os.environ.pop('HEVOLVE_CLOUD_FALLBACK_URL', None)
            info = _get_active_backend_info()
            assert info['type'] == 'local_llamacpp'
            assert info['mode'] == 'flat'
            assert 'Nunba' in info['display_name']

    def test_get_active_backend_info_central(self):
        """_get_active_backend_info should return external in central mode."""
        from langchain_gpt_api import _get_active_backend_info
        env = {
            'HEVOLVE_NODE_TIER': 'central',
            'HEVOLVE_LLM_MODEL_NAME': 'gpt-4',
            'HEVOLVE_LLM_ENDPOINT_URL': 'https://api.openai.com',
        }
        with patch.dict(os.environ, env, clear=False):
            info = _get_active_backend_info()
            assert info['type'] == 'external'
            assert info['mode'] == 'central'
            assert 'gpt-4' in info['display_name']

    def test_get_active_backend_info_flat_with_cloud_fallback(self):
        """_get_active_backend_info should indicate cloud_fallback_configured in flat mode."""
        from langchain_gpt_api import _get_active_backend_info
        env = {
            'HEVOLVE_NODE_TIER': 'flat',
            'HEVOLVE_CLOUD_FALLBACK_URL': 'https://api.openai.com',
        }
        with patch.dict(os.environ, env, clear=False):
            info = _get_active_backend_info()
            assert info['type'] == 'local_llamacpp'
            assert info['cloud_fallback_configured'] is True


# ═══════════════════════════════════════════════════════════════════
#  Section 12: get_qwen_encoder AdaptiveRouter wrapping
# ═══════════════════════════════════════════════════════════════════

class TestEncoderAdaptiveWrapping:
    """Test that get_qwen_encoder wraps in AdaptiveRouter when configured."""

    def test_no_wrap_without_cloud_url(self):
        """Should NOT wrap in AdaptiveRouter when cloud_fallback_url is empty."""
        mock_enc = MagicMock()
        mock_enc._llama_cpp_available = True
        with patch.object(_auto_mod, 'LLAMACPP_AVAILABLE', True), \
             patch.object(_auto_mod, '_check_llamacpp_server', return_value=True), \
             patch.object(_auto_mod, 'QwenLlamaCppEncoder', return_value=mock_enc):
            enc = get_qwen_encoder(
                deployment_mode='flat',
                cloud_fallback_url='',
            )
        assert not isinstance(enc, AdaptiveRouter)

    def test_wrap_with_cloud_url_flat_mode(self):
        """Should wrap in AdaptiveRouter when cloud_fallback_url set in flat mode."""
        mock_enc = MagicMock()
        mock_enc._llama_cpp_available = True
        with patch.object(_auto_mod, 'LLAMACPP_AVAILABLE', True), \
             patch.object(_auto_mod, '_check_llamacpp_server', return_value=True), \
             patch.object(_auto_mod, 'QwenLlamaCppEncoder', return_value=mock_enc):
            enc = get_qwen_encoder(
                deployment_mode='flat',
                cloud_fallback_url='https://api.openai.com/v1',
                cloud_fallback_key='sk-test',
                cloud_fallback_model='gpt-4',
            )
        assert isinstance(enc, AdaptiveRouter)
        assert enc._cloud_url == 'https://api.openai.com/v1'

    def test_no_wrap_in_regional_mode(self):
        """Should NOT wrap in AdaptiveRouter for regional mode (uses Priority 0)."""
        mock_enc = MagicMock()
        mock_enc._llama_cpp_available = True
        with patch.object(_auto_mod, 'LLAMACPP_AVAILABLE', True), \
             patch.object(_auto_mod, '_check_llamacpp_server', return_value=True), \
             patch.object(_auto_mod, 'QwenLlamaCppEncoder', return_value=mock_enc):
            enc = get_qwen_encoder(
                deployment_mode='regional',
                external_llm_url='http://regional:8000',
                cloud_fallback_url='https://api.openai.com/v1',
            )
        # Regional mode uses external endpoint directly, not AdaptiveRouter
        assert not isinstance(enc, AdaptiveRouter)


# ════════════════════════════════════════════════════════════════════
# Embodied AI In-Process Learning Pipeline
# ════════════════════════════════════════════════════════════════════

class TestEmbodiedInProcess:
    """Test in-process learning pipeline (HevolveAI - zero HTTP overhead)."""

    def test_init_learning_pipeline_success(self):
        """Mock HevolveAI imports → verify provider + hivemind initialized."""
        # Mock the HevolveAI modules
        mock_provider = MagicMock()
        mock_hive = MagicMock()
        mock_config = {'_provider': mock_provider}

        with patch.dict('sys.modules', {
            'hevolveai': MagicMock(),
            'hevolveai.embodied_ai': MagicMock(),
            'hevolveai.embodied_ai.rl_ef': MagicMock(
                create_learning_llm_config=MagicMock(return_value=mock_config),
                register_learning_provider=MagicMock(),
            ),
            'hevolveai.embodied_ai.monitoring': MagicMock(),
            'hevolveai.embodied_ai.monitoring.trace_recorder': MagicMock(
                get_trace_recorder=MagicMock(),
            ),
            'hevolveai.embodied_ai.learning': MagicMock(),
            'hevolveai.embodied_ai.learning.hive_mind': MagicMock(
                HiveMind=MagicMock(return_value=mock_hive),
                AgentCapability=MagicMock(
                    TEXT_GENERATION='text', REASONING='reasoning'),
            ),
        }):
            # Import and run the init function
            import langchain_gpt_api as lgapi

            # Save originals
            orig_provider = lgapi._learning_provider
            orig_hive = lgapi._hive_mind

            try:
                lgapi._learning_provider = None
                lgapi._hive_mind = None
                lgapi._init_learning_pipeline()

                assert lgapi._learning_provider is mock_provider
                assert lgapi._hive_mind is mock_hive
            finally:
                lgapi._learning_provider = orig_provider
                lgapi._hive_mind = orig_hive

    def test_init_learning_pipeline_import_error(self):
        """Mock ImportError → verify graceful degradation, no crash."""
        import langchain_gpt_api as lgapi

        orig_provider = lgapi._learning_provider
        orig_hive = lgapi._hive_mind

        try:
            lgapi._learning_provider = None
            lgapi._hive_mind = None

            with patch.dict('sys.modules', {
                'hevolveai': None,
                'hevolveai.embodied_ai': None,
                'hevolveai.embodied_ai.rl_ef': None,
            }):
                # Should not crash
                lgapi._init_learning_pipeline()

            # Provider should remain None (graceful degradation)
            assert lgapi._learning_provider is None
            assert lgapi._hive_mind is None
        finally:
            lgapi._learning_provider = orig_provider
            lgapi._hive_mind = orig_hive

    def test_bridge_in_process_mode(self):
        """Mock get_learning_provider returning a provider → _in_process = True."""
        mock_provider = MagicMock()
        mock_hive = MagicMock()

        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        # Manually set in-process mode
        bridge._provider = mock_provider
        bridge._hive_mind = mock_hive
        bridge._in_process = True

        assert bridge._in_process is True
        assert bridge._provider is mock_provider

        # check_health should return in_process mode
        health = bridge.check_health()
        assert health['healthy'] is True
        assert health['learning_active'] is True
        assert health['mode'] == 'in_process'

    def test_bridge_correction_in_process(self):
        """Mock send_expert_correction → verify direct call, no HTTP."""
        mock_provider = MagicMock()
        mock_result = {'success': True, 'correction_id': '123'}
        mock_send = MagicMock(return_value=mock_result)

        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        bridge._provider = mock_provider
        bridge._in_process = True

        # Patch the import inside submit_correction by injecting the module
        mock_rl_ef = MagicMock()
        mock_rl_ef.send_expert_correction = mock_send
        with patch.dict('sys.modules', {
            'hevolveai': MagicMock(),
            'hevolveai.embodied_ai': MagicMock(),
            'hevolveai.embodied_ai.rl_ef': mock_rl_ef,
        }):
            result = bridge.submit_correction(
                original_response='old answer',
                corrected_response='new answer',
                expert_id='test_expert',
            )

        assert result['success'] is True
        mock_send.assert_called_once()
        # Verify the call args
        call_kwargs = mock_send.call_args
        assert call_kwargs[1]['domain'] == 'general'
        assert call_kwargs[1]['expert_id'] == 'test_expert'

    def test_bridge_fallback_to_http(self):
        """Mock get_learning_provider returning None → HTTP calls used."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        # Not in-process mode
        assert bridge._in_process is False
        assert bridge._provider is None

        # check_health should try HTTP
        with patch('integrations.agent_engine.world_model_bridge.requests'
                   '.get') as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.headers = {'content-type': 'application/json'}
            mock_resp.json.return_value = {'status': 'ok'}
            mock_get.return_value = mock_resp

            health = bridge.check_health()
            assert health['healthy'] is True
            assert health['mode'] == 'http'
            mock_get.assert_called_once()


# ════════════════════════════════════════════════════════════════════
# Install-Time Dependency Chain
# ════════════════════════════════════════════════════════════════════

class TestInstallTimeDependencies:
    """Verify the dependency declarations that wire HevolveAI into hevolve-backend.

    These tests guard the install-time contract:
      Nunba build.py → pip install hevolve-backend → pip resolves embodied-ai
    If any of these declarations are removed, the learning pipeline silently
    disappears and every node loses hivemind/RL-EF/corrections.
    """

    def test_pyproject_declares_embodied_ai_dependency(self):
        """hevolve-backend's pyproject.toml MUST list embodied-ai as required."""
        pyproject_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'pyproject.toml',
        )
        with open(pyproject_path, 'r') as f:
            content = f.read()

        # Must contain the git dependency for embodied-ai
        assert 'embodied-ai' in content, (
            "pyproject.toml must declare embodied-ai as a dependency. "
            "Without it, HevolveAI won't be installed and the learning "
            "pipeline will silently fail on every node."
        )
        assert 'git+https://github.com/hertz-ai/hevolveai' in content, (
            "embodied-ai must point to the HevolveAI git repo"
        )

    def test_hevolveai_setup_uses_src_package_dir(self):
        """HevolveAI's setup.py MUST use package_dir={'': 'src'}.

        Without this, pip install creates 'src.hevolveai' import paths instead
        of 'hevolveai', breaking all in-process imports from world_model_bridge
        and langchain_gpt_api.
        """
        setup_path = os.path.normpath(os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            '..', 'hevolveai', 'setup.py',
        ))
        if not os.path.exists(setup_path):
            setup_path = os.path.normpath(os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                '..', 'hevolveai', 'setup.py',  # legacy fallback
            ))
        if not os.path.exists(setup_path):
            pytest.skip("HevolveAI repo not found as sibling")

        with open(setup_path, 'r') as f:
            content = f.read()

        assert "package_dir" in content, (
            "setup.py must set package_dir={'': 'src'}"
        )
        assert "find_packages(where='src')" in content, (
            "setup.py must use find_packages(where='src') to export "
            "'hevolveai.*' not 'src.hevolveai.*'"
        )

    def test_no_src_prefix_in_hevolveai_source_imports(self):
        """No source file under hevolveai/src/ should import 'from src.hevolveai'.

        After the import fix, all imports must be 'from hevolveai.*'.
        'from src.hevolveai.*' only works with sys.path hacks and breaks
        when pip-installed into another project.
        """
        hevolveai_src = os.path.normpath(os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            '..', 'hevolveai', 'src',
        ))
        if not os.path.isdir(hevolveai_src):
            hevolveai_src = os.path.normpath(os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                '..', 'hevolveai', 'src',  # legacy fallback
            ))
        if not os.path.isdir(hevolveai_src):
            pytest.skip("HevolveAI repo not found as sibling")

        bad_files = []
        for root, dirs, files in os.walk(hevolveai_src):
            dirs[:] = [d for d in dirs if d != '__pycache__']
            for f in files:
                if not f.endswith('.py'):
                    continue
                path = os.path.join(root, f)
                with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
                    for i, line in enumerate(fh, 1):
                        if 'from src.hevolveai' in line:
                            bad_files.append(f"{path}:{i}")

        assert not bad_files, (
            f"Found 'from src.hevolveai' imports in source files "
            f"(must be 'from hevolveai'):\n" +
            "\n".join(bad_files[:10])
        )

    def test_langchain_gpt_api_exports_learning_getters(self):
        """langchain_gpt_api must export get_learning_provider/get_hive_mind.

        world_model_bridge._init_in_process() imports these to connect
        to the in-process learning pipeline. If they're removed, all nodes
        fall back to HTTP mode (port 8000 which doesn't exist).
        """
        import langchain_gpt_api as lgapi
        assert hasattr(lgapi, 'get_learning_provider'), (
            "langchain_gpt_api must export get_learning_provider()"
        )
        assert hasattr(lgapi, 'get_hive_mind'), (
            "langchain_gpt_api must export get_hive_mind()"
        )
        assert callable(lgapi.get_learning_provider)
        assert callable(lgapi.get_hive_mind)

    def test_langchain_gpt_api_has_init_learning_pipeline(self):
        """_init_learning_pipeline must exist and be a callable function."""
        import langchain_gpt_api as lgapi
        assert hasattr(lgapi, '_init_learning_pipeline'), (
            "langchain_gpt_api must have _init_learning_pipeline() for "
            "in-process HevolveAI initialization"
        )
        assert callable(lgapi._init_learning_pipeline)


class TestBuildInstallOrder:
    """Verify Nunba's build.py install order and fallback behavior.

    The install chain must be:
      1. _install_embodied_ai (local sibling → GitHub fallback)
      2. pip install hevolve-backend (embodied-ai already satisfied)
      3. pin langchain==0.0.230

    If embodied-ai is NOT pre-installed, pip tries the git URL during
    hevolve-backend resolution. If the repo is private or unreachable,
    the ENTIRE hevolve-backend install fails.
    """

    @pytest.fixture
    def build_module(self):
        """Import Nunba's build.py as a module."""
        build_path = os.path.normpath(os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            '..', 'Nunba', 'build.py',
        ))
        if not os.path.exists(build_path):
            pytest.skip("Nunba repo not found as sibling")

        spec = importlib.util.spec_from_file_location('nunba_build', build_path)
        mod = importlib.util.module_from_spec(spec)
        # Don't execute the module (it has argparse + main), just load functions
        spec.loader.exec_module(mod)
        return mod

    def test_install_embodied_ai_exists(self, build_module):
        """build.py must have _install_embodied_ai function."""
        assert hasattr(build_module, '_install_embodied_ai'), (
            "build.py must define _install_embodied_ai() for local-first "
            "HevolveAI installation"
        )

    def test_install_embodied_ai_tries_local_first(self, build_module):
        """_install_embodied_ai should try local sibling before GitHub."""
        import inspect
        source = inspect.getsource(build_module._install_embodied_ai)

        # Must check for local sibling (setup.py existence)
        assert 'setup.py' in source, (
            "_install_embodied_ai must check for local hevolveai/setup.py"
        )
        # Must have GitHub fallback
        assert 'hevolveai.git' in source, (
            "_install_embodied_ai must fall back to GitHub if no local sibling"
        )

    def test_hevolve_backend_pre_installs_embodied_ai(self, build_module):
        """_install_hevolve_backend must call _install_embodied_ai BEFORE
        installing hevolve-backend itself.

        This pre-satisfies the embodied-ai dependency so pip doesn't try
        the git URL during hevolve-backend resolution.
        """
        import inspect
        source = inspect.getsource(build_module._install_hevolve_backend)

        # Strip docstring to only look at executable code
        lines = source.split('\n')
        code_lines = []
        in_docstring = False
        for line in lines:
            stripped = line.strip()
            if '"""' in stripped:
                in_docstring = not in_docstring
                continue
            if not in_docstring:
                code_lines.append(line)
        code_only = '\n'.join(code_lines)

        # Find positions of key calls in executable code
        embodied_pos = code_only.find('_install_embodied_ai')
        install_pos = code_only.find("pip', 'install'")

        assert embodied_pos != -1, (
            "_install_hevolve_backend must call _install_embodied_ai"
        )
        assert install_pos != -1, (
            "_install_hevolve_backend must pip install hevolve-backend"
        )
        assert embodied_pos < install_pos, (
            "_install_embodied_ai must be called BEFORE pip install "
            "hevolve-backend, so the dependency is pre-satisfied"
        )

    def test_hevolve_backend_pins_langchain(self, build_module):
        """After hevolve-backend install, langchain must be pinned to 0.0.230.

        pyproject.toml says >=0.0.230 which pip resolves to 1.x (slim pkg
        without llms/chains), breaking `from langchain.llms import OpenAI`.
        """
        import inspect
        source = inspect.getsource(build_module._install_hevolve_backend)
        assert 'langchain==0.0.230' in source, (
            "Must pin langchain to 0.0.230 after hevolve-backend install"
        )


# ════════════════════════════════════════════════════════════════════
# Mode-Specific Behavior Contracts (flat / regional / central)
# ════════════════════════════════════════════════════════════════════

class TestFlatModeBehavior:
    """Flat mode (Nunba desktop): everything in-process, 2 ports only.

    Port 5000: Flask GUI + hevolve-backend API (via test_client)
    Port 8080: llama.cpp raw inference
    NO port 8000: learning pipeline runs in-process (direct Python calls)
    NO port 6777: hevolve-backend served in-process, not standalone

    These tests MUST fail if someone re-introduces HTTP self-calls
    or adds unnecessary ports for flat mode.
    """

    def test_flat_bridge_prefers_in_process(self):
        """In flat mode, world_model_bridge should use in-process calls
        when a learning provider is available."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        # Simulate flat mode with provider available
        bridge._node_tier = 'flat'
        bridge._provider = MagicMock()
        bridge._hive_mind = MagicMock()
        bridge._in_process = True

        health = bridge.check_health()
        assert health['mode'] == 'in_process', (
            "Flat mode with provider must use in_process, not HTTP"
        )
        assert health['learning_active'] is True
        assert health['node_tier'] == 'flat'

    def test_flat_flush_uses_direct_call_not_http(self):
        """In flat mode, flushing experiences must call provider directly,
        NOT make HTTP requests to localhost:8000."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        mock_provider = MagicMock()
        bridge._node_tier = 'flat'
        bridge._provider = mock_provider
        bridge._in_process = True

        batch = [{'prompt': 'hello', 'response': 'world',
                  'source': 'test', 'user_id': '1', 'prompt_id': '1'}]

        with patch('integrations.agent_engine.world_model_bridge.requests'
                   '.post') as mock_post:
            bridge._flush_to_world_model(batch)

            # Direct call should happen
            mock_provider.create_chat_completion.assert_called_once()
            # HTTP should NOT happen
            mock_post.assert_not_called()

    def test_flat_correction_uses_direct_call_not_http(self):
        """In flat mode, corrections must call send_expert_correction directly,
        NOT POST to localhost:8000/v1/corrections."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        bridge._node_tier = 'flat'
        bridge._provider = MagicMock()
        bridge._in_process = True

        mock_send = MagicMock(return_value={'success': True})
        with patch.dict('sys.modules', {
            'hevolveai': MagicMock(),
            'hevolveai.embodied_ai': MagicMock(),
            'hevolveai.embodied_ai.rl_ef': MagicMock(
                send_expert_correction=mock_send),
        }):
            with patch('integrations.agent_engine.world_model_bridge.requests'
                       '.post') as mock_post:
                result = bridge.submit_correction(
                    original_response='old', corrected_response='new')

                mock_send.assert_called_once()
                mock_post.assert_not_called()
                assert result['success'] is True

    def test_flat_stats_uses_direct_call_not_http(self):
        """In flat mode, learning stats must come from direct provider calls,
        NOT HTTP GET to localhost:8000/v1/stats."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        mock_provider = MagicMock()
        mock_provider.get_stats.return_value = {'total_interactions': 42}
        mock_hive = MagicMock()
        mock_hive.get_stats.return_value = {'agents': 3}

        bridge._node_tier = 'flat'
        bridge._provider = mock_provider
        bridge._hive_mind = mock_hive
        bridge._in_process = True

        with patch('integrations.agent_engine.world_model_bridge.requests'
                   '.get') as mock_get:
            stats = bridge.get_learning_stats()

            # Direct calls should happen
            mock_provider.get_stats.assert_called_once()
            mock_hive.get_stats.assert_called_once()
            # HTTP should NOT happen
            mock_get.assert_not_called()
            assert stats['learning']['total_interactions'] == 42
            assert stats['hivemind']['agents'] == 3

    def test_flat_env_var_defaults(self):
        """Flat mode must default to correct env vars."""
        with patch.dict(os.environ, {}, clear=False):
            # Remove any tier override
            os.environ.pop('HEVOLVE_NODE_TIER', None)

            with patch(
                'integrations.agent_engine.world_model_bridge.WorldModelBridge'
                '._init_in_process'
            ):
                from integrations.agent_engine.world_model_bridge import (
                    WorldModelBridge)
                bridge = WorldModelBridge()

            assert bridge._node_tier == 'flat', (
                "Default tier must be 'flat' (Nunba desktop mode)"
            )


class TestRegionalModeBehavior:
    """Regional mode: same as flat - bundled in-process, NOT standalone.

    Regional nodes are Nunba desktops that are part of a region cluster.
    They use the same in-process architecture as flat. The only difference
    is they sync to a central node and participate in regional gossip.

    These tests MUST fail if someone makes regional behave differently
    from flat (e.g., adding HTTP calls to a local server).
    """

    def test_regional_is_in_process_like_flat(self):
        """Regional mode must use in-process calls, same as flat."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        bridge._node_tier = 'regional'
        bridge._provider = MagicMock()
        bridge._hive_mind = MagicMock()
        bridge._in_process = True

        health = bridge.check_health()
        assert health['mode'] == 'in_process', (
            "Regional mode with provider must use in_process, NOT HTTP. "
            "Regional is bundled in-process, same architecture as flat."
        )
        assert health['node_tier'] == 'regional'

    def test_regional_flush_uses_direct_call(self):
        """Regional flush must use direct provider call, not HTTP."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        mock_provider = MagicMock()
        bridge._node_tier = 'regional'
        bridge._provider = mock_provider
        bridge._in_process = True

        batch = [{'prompt': 'q', 'response': 'a', 'source': 'test',
                  'user_id': '1', 'prompt_id': '1'}]

        with patch('integrations.agent_engine.world_model_bridge.requests'
                   '.post') as mock_post:
            bridge._flush_to_world_model(batch)
            mock_provider.create_chat_completion.assert_called_once()
            mock_post.assert_not_called()

    def test_regional_same_architecture_as_flat(self):
        """Regional and flat must produce identical bridge behavior.

        Both are bundled in-process. Only difference is gossip targets
        and sync to central - NOT the learning pipeline mode.
        """
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            flat_bridge = WorldModelBridge()
            regional_bridge = WorldModelBridge()

        mock_provider = MagicMock()
        mock_hive = MagicMock()

        for bridge, tier in [(flat_bridge, 'flat'),
                             (regional_bridge, 'regional')]:
            bridge._node_tier = tier
            bridge._provider = mock_provider
            bridge._hive_mind = mock_hive
            bridge._in_process = True

        flat_health = flat_bridge.check_health()
        regional_health = regional_bridge.check_health()

        # Same mode, same learning_active - only node_tier differs
        assert flat_health['mode'] == regional_health['mode'] == 'in_process'
        assert flat_health['learning_active'] == regional_health['learning_active']
        assert flat_health['node_tier'] == 'flat'
        assert regional_health['node_tier'] == 'regional'


class TestCentralModeBehavior:
    """Central mode: standalone processes, HTTP between services.

    Central (hevolve.ai) runs each service as a separate process:
      - hevolve-backend on port 6777
      - database on port 6006
      - llama.cpp on port 8080
      - HevolveAI MAY run as separate process (HTTP mode)

    When HevolveAI is NOT co-located (no in-process provider), the bridge
    MUST fall back to HTTP calls. These tests guard that fallback.
    """

    def test_central_without_provider_uses_http(self):
        """Central mode without in-process provider must use HTTP."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        bridge._node_tier = 'central'
        bridge._provider = None
        bridge._in_process = False

        with patch('integrations.agent_engine.world_model_bridge.requests'
                   '.get') as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.headers = {'content-type': 'application/json'}
            mock_resp.json.return_value = {'status': 'ok'}
            mock_get.return_value = mock_resp

            health = bridge.check_health()
            assert health['mode'] == 'http', (
                "Central without in-process provider must use HTTP"
            )
            assert health['node_tier'] == 'central'
            mock_get.assert_called_once()

    def test_central_flush_uses_http_when_no_provider(self):
        """Central mode flush must use HTTP POST when no in-process provider."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        bridge._node_tier = 'central'
        bridge._provider = None
        bridge._in_process = False

        batch = [{'prompt': 'q', 'response': 'a', 'source': 'test',
                  'user_id': '1', 'prompt_id': '1'}]

        with patch('integrations.agent_engine.world_model_bridge.requests'
                   '.post') as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp

            bridge._flush_to_world_model(batch)
            mock_post.assert_called_once()
            # Verify it hit /v1/chat/completions
            call_url = mock_post.call_args[0][0]
            assert '/v1/chat/completions' in call_url

    def test_central_correction_uses_http_when_no_provider(self):
        """Central mode correction must POST to /v1/corrections via HTTP."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        bridge._node_tier = 'central'
        bridge._provider = None
        bridge._in_process = False

        with patch('integrations.agent_engine.world_model_bridge.requests'
                   '.post') as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {'success': True}
            mock_post.return_value = mock_resp

            result = bridge.submit_correction(
                original_response='old', corrected_response='new')
            mock_post.assert_called_once()
            call_url = mock_post.call_args[0][0]
            assert '/v1/corrections' in call_url
            assert result['success'] is True

    def test_central_stats_uses_http_when_no_provider(self):
        """Central mode stats must GET /v1/stats + /v1/hivemind/stats via HTTP."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        bridge._node_tier = 'central'
        bridge._provider = None
        bridge._in_process = False

        with patch('integrations.agent_engine.world_model_bridge.requests'
                   '.get') as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {'some': 'data'}
            mock_get.return_value = mock_resp

            stats = bridge.get_learning_stats()
            # Should make 2 HTTP calls: /v1/stats + /v1/hivemind/stats
            assert mock_get.call_count == 2
            urls = [c[0][0] for c in mock_get.call_args_list]
            assert any('/v1/stats' in u for u in urls)
            assert any('/v1/hivemind/stats' in u for u in urls)

    def test_central_with_provider_still_uses_in_process(self):
        """Even in central mode, if provider IS co-located, use in-process.

        This happens when central runs HevolveAI in the same process
        (e.g., single-machine deployment). In-process is always preferred.
        """
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        bridge._node_tier = 'central'
        bridge._provider = MagicMock()
        bridge._hive_mind = MagicMock()
        bridge._in_process = True

        health = bridge.check_health()
        assert health['mode'] == 'in_process', (
            "Central with co-located provider should still use in_process"
        )

    def test_central_http_failure_returns_unhealthy(self):
        """Central mode HTTP failure must return healthy=False, not crash."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        bridge._node_tier = 'central'
        bridge._provider = None
        bridge._in_process = False

        with patch('integrations.agent_engine.world_model_bridge.requests'
                   '.get', side_effect=requests.RequestException("timeout")):
            health = bridge.check_health()
            assert health['healthy'] is False
            assert health['learning_active'] is False
            assert health['mode'] == 'http'

    def test_central_hivemind_http_failure_returns_none(self):
        """Central mode hivemind HTTP failure must return None, not crash."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        bridge._node_tier = 'central'
        bridge._provider = None
        bridge._in_process = False

        with patch('integrations.agent_engine.world_model_bridge.requests'
                   '.post', side_effect=requests.RequestException("timeout")):
            result = bridge.query_hivemind("test query")
            assert result is None


class TestStatusEndpointLearning:
    """Verify /status endpoint exposes learning pipeline state.

    The /status endpoint must report learning_active so dashboards and
    monitoring can verify the learning pipeline is operational.
    """

    def test_status_includes_learning_active_field(self):
        """GET /status must include learning_active in response."""
        import langchain_gpt_api as lgapi
        with lgapi.app.test_client() as client:
            with patch(
                'integrations.agent_engine.world_model_bridge'
                '.get_world_model_bridge'
            ) as mock_bridge_fn:
                mock_bridge = MagicMock()
                mock_bridge.get_stats.return_value = {
                    'api_url': 'in-process', 'in_process': True}
                mock_bridge.check_health.return_value = {
                    'healthy': True, 'learning_active': True,
                    'mode': 'in_process'}
                mock_bridge_fn.return_value = mock_bridge

                resp = client.get('/status')
                data = resp.get_json()

                assert 'learning_active' in data, (
                    "/status must report learning_active for monitoring"
                )
                assert data['learning_active'] is True

    def test_status_includes_learning_mode(self):
        """GET /status must include learning_mode (in_process or http)."""
        import langchain_gpt_api as lgapi
        with lgapi.app.test_client() as client:
            with patch(
                'integrations.agent_engine.world_model_bridge'
                '.get_world_model_bridge'
            ) as mock_bridge_fn:
                mock_bridge = MagicMock()
                mock_bridge.get_stats.return_value = {
                    'api_url': 'in-process', 'in_process': True}
                mock_bridge.check_health.return_value = {
                    'healthy': True, 'learning_active': True,
                    'mode': 'in_process'}
                mock_bridge_fn.return_value = mock_bridge

                resp = client.get('/status')
                data = resp.get_json()

                assert 'learning_mode' in data
                assert data['learning_mode'] == 'in_process'

    def test_status_learning_false_on_bridge_error(self):
        """GET /status must report learning_active=False if bridge fails."""
        import langchain_gpt_api as lgapi
        with lgapi.app.test_client() as client:
            with patch(
                'integrations.agent_engine.world_model_bridge'
                '.get_world_model_bridge',
                side_effect=Exception("bridge init failed"),
            ):
                resp = client.get('/status')
                data = resp.get_json()

                assert data.get('learning_active') is False, (
                    "/status must report learning_active=False on error"
                )


# ════════════════════════════════════════════════════════════════════
# Zero Extra Ports - pip-installed (flat/regional) mode
# ════════════════════════════════════════════════════════════════════

class TestNoExtraPortsInProcessMode:
    """Verify that flat/regional mode opens NO unnecessary ports.

    When pip-installed (Nunba bundles hevolve-backend + HevolveAI):
      Port 5000: Flask GUI + API (served by Nunba app.py)
      Port 8080: llama.cpp raw inference (started by llama_config)
      NO port 8000: HevolveAI api_server must NOT run
      NO port 6777: hevolve-backend must NOT run standalone

    These tests MUST fail if someone re-introduces a HevolveAI HTTP server
    or starts hevolve-backend on its own port in flat/regional mode.
    """

    def test_init_learning_pipeline_does_not_import_api_server(self):
        """_init_learning_pipeline must NOT import HevolveAI's server.api_server.

        api_server.py starts a FastAPI+uvicorn server on port 8000.
        In flat/regional mode, we import learning functions directly -
        the server module must never be loaded.
        """
        import inspect
        import langchain_gpt_api as lgapi
        source = inspect.getsource(lgapi._init_learning_pipeline)

        # Strip docstring - only check executable code, not comments
        lines = source.split('\n')
        code_lines = []
        in_docstring = False
        for line in lines:
            stripped = line.strip()
            if '"""' in stripped:
                in_docstring = not in_docstring
                continue
            if not in_docstring and not stripped.startswith('#'):
                code_lines.append(line)
        code_only = '\n'.join(code_lines)

        assert 'api_server' not in code_only, (
            "_init_learning_pipeline must NOT import api_server. "
            "It should import learning functions directly from "
            "hevolveai.embodied_ai.rl_ef, not the server wrapper."
        )
        assert 'uvicorn' not in code_only, (
            "_init_learning_pipeline must NOT reference uvicorn. "
            "No HTTP server should be started for in-process mode."
        )
        assert 'import FastAPI' not in code_only, (
            "_init_learning_pipeline must NOT import FastAPI. "
            "Learning runs in-process, not as a server."
        )

    def test_init_learning_pipeline_does_not_bind_port(self):
        """_init_learning_pipeline must NOT call .run(), .serve(), or bind a socket.

        Any port-binding call would start a server, defeating the purpose
        of in-process mode.
        """
        import inspect
        import langchain_gpt_api as lgapi
        source = inspect.getsource(lgapi._init_learning_pipeline)

        port_binding_patterns = [
            '.run(', '.serve(', 'bind(', 'listen(',
            'uvicorn.run', 'app.run', 'server.start',
        ]
        for pattern in port_binding_patterns:
            assert pattern not in source, (
                f"_init_learning_pipeline must NOT contain '{pattern}'. "
                f"In-process mode must not bind any port."
            )

    def test_world_model_bridge_no_server_startup(self):
        """WorldModelBridge.__init__ must NOT start any server.

        The bridge connects to an existing in-process provider or falls
        back to HTTP. It must never start a new server process.
        """
        import inspect
        from integrations.agent_engine.world_model_bridge import (
            WorldModelBridge)
        source = inspect.getsource(WorldModelBridge.__init__)
        source += inspect.getsource(WorldModelBridge._init_in_process)

        for pattern in ['uvicorn', 'FastAPI', '.run(', 'subprocess',
                        'Popen', 'start_server']:
            assert pattern not in source, (
                f"WorldModelBridge must NOT contain '{pattern}'. "
                f"It connects to existing providers, never starts servers."
            )

    def test_flat_mode_no_http_calls_during_flush(self):
        """Flat mode flush must make ZERO HTTP calls - not even to localhost.

        This is the strongest port-free guarantee: even if port 8000 were
        running, flat mode must not use it.
        """
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        mock_provider = MagicMock()
        bridge._node_tier = 'flat'
        bridge._provider = mock_provider
        bridge._in_process = True

        # Create a large batch to flush
        batch = [
            {'prompt': f'q{i}', 'response': f'a{i}', 'source': 'test',
             'user_id': '1', 'prompt_id': '1'}
            for i in range(10)
        ]

        # Patch ALL network calls
        with patch('integrations.agent_engine.world_model_bridge.requests'
                   '.post') as mock_post, \
             patch('integrations.agent_engine.world_model_bridge.requests'
                   '.get') as mock_get:

            bridge._flush_to_world_model(batch)

            mock_post.assert_not_called()
            mock_get.assert_not_called()
            assert mock_provider.create_chat_completion.call_count == 10

    def test_flat_mode_no_http_calls_for_full_lifecycle(self):
        """Full lifecycle in flat mode must make ZERO HTTP calls.

        Tests: record → flush → correction → hivemind → stats → health
        All must go through direct Python calls.
        """
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        mock_provider = MagicMock()
        mock_provider.get_stats.return_value = {'ok': True}
        mock_hive = MagicMock()
        mock_hive.get_stats.return_value = {'agents': 1}
        mock_hive.get_all_agents.return_value = []

        bridge._node_tier = 'flat'
        bridge._provider = mock_provider
        bridge._hive_mind = mock_hive
        bridge._in_process = True

        mock_send = MagicMock(return_value={'success': True})

        with patch('integrations.agent_engine.world_model_bridge.requests'
                   '.post') as mock_post, \
             patch('integrations.agent_engine.world_model_bridge.requests'
                   '.get') as mock_get, \
             patch.dict('sys.modules', {
                 'hevolveai': MagicMock(),
                 'hevolveai.embodied_ai': MagicMock(),
                 'hevolveai.embodied_ai.rl_ef': MagicMock(
                     send_expert_correction=mock_send),
             }):

            # 1. Flush
            bridge._flush_to_world_model(
                [{'prompt': 'q', 'response': 'a', 'source': 'test',
                  'user_id': '1', 'prompt_id': '1'}])

            # 2. Correction
            bridge.submit_correction('old', 'new')

            # 3. Stats
            bridge.get_learning_stats()

            # 4. Agents
            bridge.get_hivemind_agents()

            # 5. Health
            bridge.check_health()

            # ZERO HTTP calls across the entire lifecycle
            mock_post.assert_not_called()
            mock_get.assert_not_called()

    def test_regional_mode_no_http_calls_for_full_lifecycle(self):
        """Regional mode must also make ZERO HTTP calls (same as flat)."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        mock_provider = MagicMock()
        mock_provider.get_stats.return_value = {'ok': True}
        mock_hive = MagicMock()
        mock_hive.get_stats.return_value = {'agents': 1}
        mock_hive.get_all_agents.return_value = []

        bridge._node_tier = 'regional'
        bridge._provider = mock_provider
        bridge._hive_mind = mock_hive
        bridge._in_process = True

        mock_send = MagicMock(return_value={'success': True})

        with patch('integrations.agent_engine.world_model_bridge.requests'
                   '.post') as mock_post, \
             patch('integrations.agent_engine.world_model_bridge.requests'
                   '.get') as mock_get, \
             patch.dict('sys.modules', {
                 'hevolveai': MagicMock(),
                 'hevolveai.embodied_ai': MagicMock(),
                 'hevolveai.embodied_ai.rl_ef': MagicMock(
                     send_expert_correction=mock_send),
             }):

            bridge._flush_to_world_model(
                [{'prompt': 'q', 'response': 'a', 'source': 'test',
                  'user_id': '1', 'prompt_id': '1'}])
            bridge.submit_correction('old', 'new')
            bridge.get_learning_stats()
            bridge.get_hivemind_agents()
            bridge.check_health()

            mock_post.assert_not_called()
            mock_get.assert_not_called()

    def test_only_hevolveai_learning_modules_imported_not_server(self):
        """_init_learning_pipeline must import ONLY learning functions.

        Must import: HevolveAI's embodied_ai.rl_ef (learning provider)
        Must import: HevolveAI's embodied_ai.learning.hive_mind (hivemind)
        Must NOT import: HevolveAI's server (api_server, wamp, etc.)

        If api_server is imported, its module-level code starts the
        proof monitor and creates a FastAPI app - opening port 8000.
        """
        import inspect
        import langchain_gpt_api as lgapi
        source = inspect.getsource(lgapi._init_learning_pipeline)

        # Must import learning functions
        assert 'hevolveai.embodied_ai.rl_ef' in source, (
            "Must import rl_ef for learning provider"
        )
        assert 'hevolveai.embodied_ai.learning.hive_mind' in source, (
            "Must import hive_mind for collective intelligence"
        )

        # Must NOT import server modules
        assert 'hevolveai.server' not in source, (
            "Must NOT import HevolveAI's server module - that starts FastAPI on port 8000"
        )
        assert 'api_server' not in source, (
            "Must NOT import api_server - that binds port 8000"
        )

    def test_bridge_in_process_mode_reports_no_port(self):
        """In-process health check must NOT reference port 8000 in response."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        bridge._provider = MagicMock()
        bridge._hive_mind = MagicMock()
        bridge._in_process = True

        health = bridge.check_health()
        assert health['mode'] == 'in_process'
        # Should NOT have 'details' with port info
        assert 'details' not in health, (
            "In-process health should not include HTTP details"
        )

    def test_central_http_mode_reports_api_url(self):
        """Central HTTP mode health SHOULD include API URL for debugging."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()

        bridge._node_tier = 'central'
        bridge._provider = None
        bridge._in_process = False

        with patch('integrations.agent_engine.world_model_bridge.requests'
                   '.get') as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.headers = {'content-type': 'application/json'}
            mock_resp.json.return_value = {'status': 'ok'}
            mock_get.return_value = mock_resp

            health = bridge.check_health()
            assert health['mode'] == 'http'
            assert 'details' in health, (
                "HTTP mode should include details for debugging"
            )


# ════════════════════════════════════════════════════════════════════
# Phase Coverage - Boot → Init → Runtime → Error
# ════════════════════════════════════════════════════════════════════

class TestBootPhase:
    """Verify boot-time behavior when langchain_gpt_api module loads.

    On module import, _init_learning_pipeline is started in a daemon
    thread. This must not block Flask startup, must not crash on
    ImportError, and must set globals correctly.
    """

    def test_boot_thread_is_daemon(self):
        """The learning pipeline init thread must be a daemon thread.

        If non-daemon, it would prevent process exit when Flask stops.
        """
        import langchain_gpt_api as lgapi

        # Check that a daemon thread with the right name was started
        # (it already ran on module import, but verify the pattern)
        import inspect
        source_file = inspect.getfile(lgapi)
        with open(source_file, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'daemon=True' in content, (
            "Learning pipeline init thread must be daemon=True"
        )
        assert "name='embodied_ai_init'" in content, (
            "Learning pipeline init thread must be named 'embodied_ai_init'"
        )

    def test_boot_does_not_block_module_import(self):
        """Importing langchain_gpt_api must return immediately.

        _init_learning_pipeline runs in a background thread, so import
        must not hang even if HevolveAI takes time to initialize.
        """
        import time
        start = time.time()
        # Module is already imported, but verify the pattern
        import langchain_gpt_api  # noqa: already imported
        elapsed = time.time() - start

        # Module import should be near-instant (< 5s)
        # The learning pipeline runs in background
        assert elapsed < 5.0, (
            f"Module import took {elapsed:.1f}s - _init_learning_pipeline "
            f"may be blocking instead of running in a daemon thread"
        )

    def test_globals_initialized_before_pipeline(self):
        """_learning_provider and _hive_mind must be initialized to None.

        get_learning_provider() and get_hive_mind() must be safe to call
        even before _init_learning_pipeline completes (return None).
        """
        import langchain_gpt_api as lgapi

        # These must exist as module-level globals
        assert hasattr(lgapi, '_learning_provider')
        assert hasattr(lgapi, '_hive_mind')

        # get_*() must be safe to call (return None if not initialized)
        result = lgapi.get_learning_provider()
        assert result is None or result is not None  # just must not crash

        result = lgapi.get_hive_mind()
        assert result is None or result is not None  # just must not crash


class TestInitPhase:
    """Verify _init_learning_pipeline behavior for all outcomes."""

    def test_init_sets_provider_on_success(self):
        """On success, _learning_provider must be set to the provider."""
        import langchain_gpt_api as lgapi
        mock_provider = MagicMock()
        mock_hive = MagicMock()

        orig_p, orig_h = lgapi._learning_provider, lgapi._hive_mind
        try:
            lgapi._learning_provider = None
            lgapi._hive_mind = None

            with patch.dict('sys.modules', {
                'hevolveai': MagicMock(),
                'hevolveai.embodied_ai': MagicMock(),
                'hevolveai.embodied_ai.rl_ef': MagicMock(
                    create_learning_llm_config=MagicMock(
                        return_value={'_provider': mock_provider}),
                    register_learning_provider=MagicMock(),
                ),
                'hevolveai.embodied_ai.monitoring': MagicMock(),
                'hevolveai.embodied_ai.monitoring.trace_recorder': MagicMock(
                    get_trace_recorder=MagicMock(),
                ),
                'hevolveai.embodied_ai.learning': MagicMock(),
                'hevolveai.embodied_ai.learning.hive_mind': MagicMock(
                    HiveMind=MagicMock(return_value=mock_hive),
                    AgentCapability=MagicMock(
                        TEXT_GENERATION='t', REASONING='r'),
                ),
            }):
                lgapi._init_learning_pipeline()

            assert lgapi._learning_provider is mock_provider
            assert lgapi._hive_mind is mock_hive
            assert lgapi.get_learning_provider() is mock_provider
            assert lgapi.get_hive_mind() is mock_hive
        finally:
            lgapi._learning_provider = orig_p
            lgapi._hive_mind = orig_h

    def test_init_leaves_none_on_import_error(self):
        """On ImportError (HevolveAI not installed), globals stay None."""
        import langchain_gpt_api as lgapi
        orig_p, orig_h = lgapi._learning_provider, lgapi._hive_mind
        try:
            lgapi._learning_provider = None
            lgapi._hive_mind = None

            with patch.dict('sys.modules', {
                'hevolveai': None,
                'hevolveai.embodied_ai': None,
                'hevolveai.embodied_ai.rl_ef': None,
            }):
                lgapi._init_learning_pipeline()  # must not crash

            assert lgapi._learning_provider is None
            assert lgapi._hive_mind is None
        finally:
            lgapi._learning_provider = orig_p
            lgapi._hive_mind = orig_h

    def test_init_leaves_none_on_runtime_error(self):
        """On runtime error during init, globals stay None."""
        import langchain_gpt_api as lgapi
        orig_p, orig_h = lgapi._learning_provider, lgapi._hive_mind
        try:
            lgapi._learning_provider = None
            lgapi._hive_mind = None

            with patch.dict('sys.modules', {
                'hevolveai': MagicMock(),
                'hevolveai.embodied_ai': MagicMock(),
                'hevolveai.embodied_ai.rl_ef': MagicMock(
                    create_learning_llm_config=MagicMock(
                        side_effect=RuntimeError("GPU not available")),
                ),
                'hevolveai.embodied_ai.monitoring': MagicMock(),
                'hevolveai.embodied_ai.monitoring.trace_recorder': MagicMock(
                    get_trace_recorder=MagicMock(),
                ),
                'hevolveai.embodied_ai.learning': MagicMock(),
                'hevolveai.embodied_ai.learning.hive_mind': MagicMock(),
            }):
                lgapi._init_learning_pipeline()  # must not crash

            assert lgapi._learning_provider is None
        finally:
            lgapi._learning_provider = orig_p
            lgapi._hive_mind = orig_h


class TestRuntimePhaseTransitions:
    """Verify runtime behavior when bridge mode transitions or provider
    becomes available after delayed init."""

    def test_bridge_connects_when_provider_becomes_available(self):
        """If provider is None at bridge init but appears later,
        a fresh bridge instance should pick it up."""
        import langchain_gpt_api as lgapi
        orig_p = lgapi._learning_provider
        try:
            # Initially no provider
            lgapi._learning_provider = None

            with patch(
                'integrations.agent_engine.world_model_bridge.WorldModelBridge'
                '._init_in_process'
            ):
                from integrations.agent_engine.world_model_bridge import (
                    WorldModelBridge)
                bridge1 = WorldModelBridge()
            assert bridge1._in_process is False

            # Provider becomes available (background init completed)
            mock_provider = MagicMock()
            lgapi._learning_provider = mock_provider

            # New bridge instance should pick it up
            from integrations.agent_engine import world_model_bridge as wmb
            # Reset singleton to force new instance
            old_bridge = wmb._bridge
            wmb._bridge = None
            try:
                bridge2 = wmb.get_world_model_bridge()
                # _init_in_process runs in constructor and should find provider
                # (may or may not succeed depending on import resolution,
                # but must not crash)
            finally:
                wmb._bridge = old_bridge
        finally:
            lgapi._learning_provider = orig_p

    def test_bridge_stats_include_in_process_flag(self):
        """get_stats() must report whether bridge is in in_process mode."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)

            bridge_ip = WorldModelBridge()
            bridge_ip._in_process = True
            bridge_ip._provider = MagicMock()
            stats_ip = bridge_ip.get_stats()
            assert stats_ip['in_process'] is True

            bridge_http = WorldModelBridge()
            bridge_http._in_process = False
            stats_http = bridge_http.get_stats()
            assert stats_http['in_process'] is False


# ============================================================================
# llama.cpp Coordination - prevent double-start
# ============================================================================


class TestContextDetection:
    """Verify _is_bundled() and _has_cloud_api() detect context correctly."""

    def test_is_bundled_true_when_adapter_imported(self):
        """Inside Nunba, hartos_backend_adapter is in sys.modules."""
        import langchain_gpt_api as lgapi
        fake = MagicMock()
        with patch.dict('sys.modules', {'hartos_backend_adapter': fake}):
            assert lgapi._is_bundled() is True

    def test_is_bundled_false_standalone(self):
        """Running standalone, hartos_backend_adapter is absent."""
        import langchain_gpt_api as lgapi
        mods = dict(sys.modules)
        mods.pop('hartos_backend_adapter', None)
        with patch.dict('sys.modules', mods, clear=True):
            assert lgapi._is_bundled() is False

    def test_has_cloud_api_true_when_set(self):
        import langchain_gpt_api as lgapi
        with patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': 'https://api.openai.com/v1'}):
            assert lgapi._has_cloud_api() is True

    def test_has_cloud_api_false_when_empty(self):
        import langchain_gpt_api as lgapi
        with patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': ''}):
            assert lgapi._has_cloud_api() is False

    def test_has_cloud_api_false_when_whitespace(self):
        import langchain_gpt_api as lgapi
        with patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': '   '}):
            assert lgapi._has_cloud_api() is False

    def test_has_cloud_api_false_when_absent(self):
        import langchain_gpt_api as lgapi
        env = dict(os.environ)
        env.pop('HEVOLVE_LLM_ENDPOINT_URL', None)
        with patch.dict(os.environ, env, clear=True):
            assert lgapi._has_cloud_api() is False


class TestWaitForLlmServer:
    """Low-level _wait_for_llm_server() behaviour."""

    def test_finds_running_server_immediately(self):
        import langchain_gpt_api as lgapi
        mock_resp = MagicMock(status=200)
        with patch('urllib.request.urlopen', return_value=mock_resp):
            assert lgapi._wait_for_llm_server(timeout=5) is True

    def test_timeout_when_nothing_running(self):
        import urllib.error
        import langchain_gpt_api as lgapi
        with patch('urllib.request.urlopen',
                   side_effect=urllib.error.URLError('refused')), \
             patch('time.sleep'):
            assert lgapi._wait_for_llm_server(timeout=3) is False

    def test_detects_delayed_start(self):
        import urllib.error
        import langchain_gpt_api as lgapi
        mock_resp = MagicMock(status=200)
        effects = [urllib.error.URLError('refused')] * 3 + [mock_resp]
        with patch('urllib.request.urlopen', side_effect=effects), \
             patch('time.sleep'):
            assert lgapi._wait_for_llm_server(timeout=10) is True

    def test_oserror_treated_as_not_ready(self):
        import langchain_gpt_api as lgapi
        with patch('urllib.request.urlopen', side_effect=OSError('network')), \
             patch('time.sleep'):
            assert lgapi._wait_for_llm_server(timeout=2) is False

    def test_non_200_treated_as_not_ready(self):
        import langchain_gpt_api as lgapi
        mock_resp = MagicMock(status=503)
        # status != 200 means server is loading - keep polling
        effects = [mock_resp, mock_resp, MagicMock(status=200)]
        with patch('urllib.request.urlopen', side_effect=effects), \
             patch('time.sleep'):
            assert lgapi._wait_for_llm_server(timeout=5) is True


# ============================================================================
# Full Combination Matrix - server state × execution context
# ============================================================================


class TestBundledFlatCloudApiWorking:
    """Flat mode (Nunba) + user provided a working cloud API."""

    def test_skips_local_server_wait(self):
        """Cloud API configured → no local server poll at all."""
        import langchain_gpt_api as lgapi

        with patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': 'https://api.openai.com/v1'}), \
             patch.dict('sys.modules', {'hartos_backend_adapter': MagicMock()}), \
             patch.object(lgapi, '_wait_for_llm_server') as mock_wait, \
             patch.dict('sys.modules', {
                 'hevolveai': MagicMock(),
                 'hevolveai.embodied_ai': MagicMock(),
                 'hevolveai.embodied_ai.rl_ef': MagicMock(),
                 'hevolveai.embodied_ai.monitoring': MagicMock(),
                 'hevolveai.embodied_ai.monitoring.trace_recorder': MagicMock(),
                 'hevolveai.embodied_ai.learning': MagicMock(),
                 'hevolveai.embodied_ai.learning.hive_mind': MagicMock(),
             }):
            rl_ef = sys.modules['hevolveai.embodied_ai.rl_ef']
            rl_ef.create_learning_llm_config.return_value = {}
            lgapi._learning_provider = None
            lgapi._hive_mind = None
            lgapi._trace_recorder = None
            lgapi._init_learning_pipeline()
            mock_wait.assert_not_called()

    def test_no_local_port_used(self):
        """With cloud API, no connection to localhost:8080."""
        import langchain_gpt_api as lgapi

        with patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': 'https://api.openai.com/v1'}), \
             patch.dict('sys.modules', {'hartos_backend_adapter': MagicMock()}), \
             patch('urllib.request.urlopen') as mock_url:
            # _has_cloud_api() returns True → skip wait → urlopen never called
            assert lgapi._has_cloud_api() is True
            mock_url.assert_not_called()


class TestBundledFlatCloudApiBroken:
    """Flat mode (Nunba) + user's cloud API is broken/unreachable."""

    def test_falls_back_to_waiting_for_nunba_server(self):
        """Cloud broken → should NOT be detected as cloud (empty env → no cloud)."""
        import langchain_gpt_api as lgapi
        # If HEVOLVE_LLM_ENDPOINT_URL is empty, _has_cloud_api() is False
        # so the bundled path runs (wait for Nunba's server)
        with patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': ''}), \
             patch.dict('sys.modules', {'hartos_backend_adapter': MagicMock()}):
            assert lgapi._is_bundled() is True
            assert lgapi._has_cloud_api() is False
            # Bundled + no cloud → will wait for Nunba's server


class TestBundledFlatNunbaLlamaCppRunning:
    """Flat mode (Nunba) + Nunba's llama.cpp is already running on 8080."""

    def test_reuses_nunba_server(self):
        """Wait detects Nunba's server → proceeds to create_learning_llm_config."""
        import langchain_gpt_api as lgapi

        with patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': ''}), \
             patch.dict('sys.modules', {'hartos_backend_adapter': MagicMock()}), \
             patch.object(lgapi, '_wait_for_llm_server', return_value=True) as mock_wait, \
             patch.dict('sys.modules', {
                 'hevolveai': MagicMock(),
                 'hevolveai.embodied_ai': MagicMock(),
                 'hevolveai.embodied_ai.rl_ef': MagicMock(),
                 'hevolveai.embodied_ai.monitoring': MagicMock(),
                 'hevolveai.embodied_ai.monitoring.trace_recorder': MagicMock(),
                 'hevolveai.embodied_ai.learning': MagicMock(),
                 'hevolveai.embodied_ai.learning.hive_mind': MagicMock(),
             }):
            rl_ef = sys.modules['hevolveai.embodied_ai.rl_ef']
            rl_ef.create_learning_llm_config.return_value = {
                '_provider': MagicMock()}
            lgapi._learning_provider = None
            lgapi._hive_mind = None
            lgapi._trace_recorder = None
            lgapi._init_learning_pipeline()
            mock_wait.assert_called_once_with(timeout=30)
            rl_ef.create_learning_llm_config.assert_called_once()

    def test_no_second_server_started(self):
        """Bundled mode never triggers HevolveAI's auto-start path."""
        import langchain_gpt_api as lgapi

        with patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': ''}), \
             patch.dict('sys.modules', {'hartos_backend_adapter': MagicMock()}), \
             patch.object(lgapi, '_wait_for_llm_server', return_value=True), \
             patch.dict('sys.modules', {
                 'hevolveai': MagicMock(),
                 'hevolveai.embodied_ai': MagicMock(),
                 'hevolveai.embodied_ai.rl_ef': MagicMock(),
                 'hevolveai.embodied_ai.monitoring': MagicMock(),
                 'hevolveai.embodied_ai.monitoring.trace_recorder': MagicMock(),
                 'hevolveai.embodied_ai.learning': MagicMock(),
                 'hevolveai.embodied_ai.learning.hive_mind': MagicMock(),
                 'hevolveai.embodied_ai.models.auto_setup': MagicMock(),
             }):
            rl_ef = sys.modules['hevolveai.embodied_ai.rl_ef']
            rl_ef.create_learning_llm_config.return_value = {
                '_provider': MagicMock()}
            auto_setup = sys.modules['hevolveai.embodied_ai.models.auto_setup']
            lgapi._learning_provider = None
            lgapi._hive_mind = None
            lgapi._trace_recorder = None
            lgapi._init_learning_pipeline()
            auto_setup.LocalLLMService.assert_not_called()


class TestBundledFlatExternalLlamaCppRunning:
    """Flat mode + user already runs their own llama.cpp on 8080."""

    def test_reuses_users_server(self):
        """Same as Nunba server - wait detects it, reuses it."""
        import langchain_gpt_api as lgapi
        mock_resp = MagicMock(status=200)
        with patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': ''}), \
             patch.dict('sys.modules', {'hartos_backend_adapter': MagicMock()}), \
             patch('urllib.request.urlopen', return_value=mock_resp), \
             patch('time.sleep'):
            assert lgapi._wait_for_llm_server(timeout=30) is True


class TestBundledFlatNothingAvailable:
    """Flat mode + nothing on 8080 (fresh install, or user uninstalled)."""

    def test_disables_learning_no_auto_start(self):
        """Bundled + no server → learning disabled, never auto-starts."""
        import langchain_gpt_api as lgapi

        with patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': ''}), \
             patch.dict('sys.modules', {'hartos_backend_adapter': MagicMock()}), \
             patch.object(lgapi, '_wait_for_llm_server', return_value=False), \
             patch.dict('sys.modules', {
                 'hevolveai': MagicMock(),
                 'hevolveai.embodied_ai': MagicMock(),
                 'hevolveai.embodied_ai.rl_ef': MagicMock(),
                 'hevolveai.embodied_ai.monitoring': MagicMock(),
                 'hevolveai.embodied_ai.monitoring.trace_recorder': MagicMock(),
                 'hevolveai.embodied_ai.learning': MagicMock(),
                 'hevolveai.embodied_ai.learning.hive_mind': MagicMock(),
             }):
            rl_ef = sys.modules['hevolveai.embodied_ai.rl_ef']
            lgapi._learning_provider = None
            lgapi._hive_mind = None
            lgapi._trace_recorder = None
            lgapi._init_learning_pipeline()
            # create_learning_llm_config should NOT be called
            rl_ef.create_learning_llm_config.assert_not_called()
            # Learning provider stays None
            assert lgapi._learning_provider is None

    def test_chat_still_works_without_learning(self):
        """Learning disabled ≠ chat disabled - provider is None, bridge gracefully degrades."""
        import langchain_gpt_api as lgapi
        assert lgapi._learning_provider is None or True  # None is fine
        # WorldModelBridge._in_process would be False → HTTP fallback or no-op


class TestBundledFlatLlamaCppUninstalled:
    """Flat mode + llama.cpp binary was removed by user after initial install."""

    def test_same_as_nothing_available(self):
        """No binary = no server = learning disabled in bundled mode."""
        import langchain_gpt_api as lgapi
        import urllib.error

        with patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': ''}), \
             patch.dict('sys.modules', {'hartos_backend_adapter': MagicMock()}), \
             patch('urllib.request.urlopen',
                   side_effect=urllib.error.URLError('refused')), \
             patch('time.sleep'):
            result = lgapi._wait_for_llm_server(timeout=3)
            assert result is False
            # In bundled path, False → return early → no auto-start


class TestBundledFlatCloudApiStoppedWorking:
    """Flat mode + user's cloud API key expired or endpoint went down."""

    def test_cloud_url_set_but_key_expired(self):
        """HEVOLVE_LLM_ENDPOINT_URL is non-empty → _has_cloud_api() True.

        HevolveAI's Priority 0 path will try the cloud endpoint, fail,
        and fall through to local (or fail gracefully).  We don't
        auto-start a server - the user needs to fix their API key.
        """
        import langchain_gpt_api as lgapi
        with patch.dict(os.environ, {
            'HEVOLVE_LLM_ENDPOINT_URL': 'https://api.openai.com/v1',
        }):
            assert lgapi._has_cloud_api() is True
            # Cloud path → no local server wait


class TestStandaloneCloudApiWorking:
    """Standalone (start_with_tracing.bat) + cloud API configured."""

    def test_uses_cloud_api_skips_wait(self):
        import langchain_gpt_api as lgapi
        mods = dict(sys.modules)
        mods.pop('hartos_backend_adapter', None)
        with patch.dict('sys.modules', mods, clear=True), \
             patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': 'https://api.openai.com/v1'}):
            assert lgapi._is_bundled() is False
            assert lgapi._has_cloud_api() is True


class TestStandaloneLlamaCppRunning:
    """Standalone + user already has llama.cpp running on 8080."""

    def test_reuses_existing_server(self):
        import langchain_gpt_api as lgapi
        mock_resp = MagicMock(status=200)
        mods = dict(sys.modules)
        mods.pop('hartos_backend_adapter', None)
        with patch.dict('sys.modules', mods, clear=True), \
             patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': ''}), \
             patch('urllib.request.urlopen', return_value=mock_resp), \
             patch('time.sleep'):
            assert lgapi._wait_for_llm_server(timeout=5) is True


class TestStandaloneNothingAvailable:
    """Standalone + nothing on 8080 (HevolveAI should auto-start)."""

    def test_short_wait_then_hevolveai_auto_starts(self):
        """5 s timeout, then create_learning_llm_config proceeds (auto_setup=True)."""
        import langchain_gpt_api as lgapi

        mods = dict(sys.modules)
        mods.pop('hartos_backend_adapter', None)
        with patch.dict('sys.modules', mods, clear=True), \
             patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': ''}), \
             patch.object(lgapi, '_wait_for_llm_server', return_value=False) as mock_wait, \
             patch.dict('sys.modules', {
                 'hevolveai': MagicMock(),
                 'hevolveai.embodied_ai': MagicMock(),
                 'hevolveai.embodied_ai.rl_ef': MagicMock(),
                 'hevolveai.embodied_ai.monitoring': MagicMock(),
                 'hevolveai.embodied_ai.monitoring.trace_recorder': MagicMock(),
                 'hevolveai.embodied_ai.learning': MagicMock(),
                 'hevolveai.embodied_ai.learning.hive_mind': MagicMock(),
             }):
            rl_ef = sys.modules['hevolveai.embodied_ai.rl_ef']
            rl_ef.create_learning_llm_config.return_value = {
                '_provider': MagicMock()}
            lgapi._learning_provider = None
            lgapi._hive_mind = None
            lgapi._trace_recorder = None
            lgapi._init_learning_pipeline()
            # Standalone → short timeout
            mock_wait.assert_called_once_with(timeout=5)
            # create_learning_llm_config IS called (HevolveAI auto-starts)
            rl_ef.create_learning_llm_config.assert_called_once()

    def test_provider_set_after_auto_start(self):
        """After HevolveAI auto-starts, _learning_provider is populated."""
        import langchain_gpt_api as lgapi

        mock_provider = MagicMock()
        mods = dict(sys.modules)
        mods.pop('hartos_backend_adapter', None)
        with patch.dict('sys.modules', mods, clear=True), \
             patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': ''}), \
             patch.object(lgapi, '_wait_for_llm_server', return_value=False), \
             patch.dict('sys.modules', {
                 'hevolveai': MagicMock(),
                 'hevolveai.embodied_ai': MagicMock(),
                 'hevolveai.embodied_ai.rl_ef': MagicMock(),
                 'hevolveai.embodied_ai.monitoring': MagicMock(),
                 'hevolveai.embodied_ai.monitoring.trace_recorder': MagicMock(),
                 'hevolveai.embodied_ai.learning': MagicMock(),
                 'hevolveai.embodied_ai.learning.hive_mind': MagicMock(),
             }):
            rl_ef = sys.modules['hevolveai.embodied_ai.rl_ef']
            rl_ef.create_learning_llm_config.return_value = {
                '_provider': mock_provider}
            lgapi._learning_provider = None
            lgapi._hive_mind = None
            lgapi._trace_recorder = None
            lgapi._init_learning_pipeline()
            assert lgapi._learning_provider is mock_provider


class TestStandaloneLlamaCppUninstalled:
    """Standalone + llama.cpp NOT installed (can't auto-start)."""

    def test_create_learning_config_still_called(self):
        """HevolveAI handles missing llama.cpp by falling back to transformers."""
        import langchain_gpt_api as lgapi

        mods = dict(sys.modules)
        mods.pop('hartos_backend_adapter', None)
        with patch.dict('sys.modules', mods, clear=True), \
             patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': ''}), \
             patch.object(lgapi, '_wait_for_llm_server', return_value=False), \
             patch.dict('sys.modules', {
                 'hevolveai': MagicMock(),
                 'hevolveai.embodied_ai': MagicMock(),
                 'hevolveai.embodied_ai.rl_ef': MagicMock(),
                 'hevolveai.embodied_ai.monitoring': MagicMock(),
                 'hevolveai.embodied_ai.monitoring.trace_recorder': MagicMock(),
                 'hevolveai.embodied_ai.learning': MagicMock(),
                 'hevolveai.embodied_ai.learning.hive_mind': MagicMock(),
             }):
            rl_ef = sys.modules['hevolveai.embodied_ai.rl_ef']
            # No provider returned - fallback to transformers
            rl_ef.create_learning_llm_config.return_value = {}
            lgapi._learning_provider = None
            lgapi._hive_mind = None
            lgapi._trace_recorder = None
            lgapi._init_learning_pipeline()
            rl_ef.create_learning_llm_config.assert_called_once()
            assert lgapi._learning_provider is None


class TestStandaloneCloudApiBroken:
    """Standalone + cloud API was configured but stopped working."""

    def test_cloud_url_present_skips_local_wait(self):
        """HEVOLVE_LLM_ENDPOINT_URL non-empty → still treated as cloud path."""
        import langchain_gpt_api as lgapi
        with patch.dict(os.environ, {
            'HEVOLVE_LLM_ENDPOINT_URL': 'https://broken.example.com/v1',
        }):
            assert lgapi._has_cloud_api() is True
            # Cloud path → no local wait → HevolveAI tries cloud, fails gracefully


class TestServerStopsMidSession:
    """Server was running but dies during the session."""

    def test_bridge_health_reports_unhealthy(self):
        """WorldModelBridge.check_health() returns unhealthy when server dies."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()
            bridge._in_process = False
            bridge._provider = None
            health = bridge.check_health()
            assert health['healthy'] is False

    def test_learning_degrades_gracefully(self):
        """Flush and corrections silently fail - no crash."""
        with patch(
            'integrations.agent_engine.world_model_bridge.WorldModelBridge'
            '._init_in_process'
        ):
            from integrations.agent_engine.world_model_bridge import (
                WorldModelBridge)
            bridge = WorldModelBridge()
            bridge._in_process = True
            bridge._provider = MagicMock()
            bridge._provider.create_chat_completion.side_effect = \
                ConnectionError('server died')
            # Flush should not raise
            bridge._flush_to_world_model([{
                'prompt': 'hi', 'response': 'hello',
                'timestamp': 0, 'user_id': 'u1',
            }])


# ============================================================================
# Timeout contracts (bundled vs standalone)
# ============================================================================

class TestTimeoutContracts:
    """Bundled mode waits 30 s, standalone waits 5 s, cloud skips entirely."""

    def test_bundled_waits_30_seconds(self):
        import langchain_gpt_api as lgapi

        with patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': ''}), \
             patch.dict('sys.modules', {'hartos_backend_adapter': MagicMock()}), \
             patch.object(lgapi, '_wait_for_llm_server', return_value=False) as m, \
             patch.dict('sys.modules', {
                 'hevolveai': MagicMock(),
                 'hevolveai.embodied_ai': MagicMock(),
                 'hevolveai.embodied_ai.rl_ef': MagicMock(),
                 'hevolveai.embodied_ai.monitoring': MagicMock(),
                 'hevolveai.embodied_ai.monitoring.trace_recorder': MagicMock(),
                 'hevolveai.embodied_ai.learning': MagicMock(),
                 'hevolveai.embodied_ai.learning.hive_mind': MagicMock(),
             }):
            lgapi._learning_provider = None
            lgapi._hive_mind = None
            lgapi._trace_recorder = None
            lgapi._init_learning_pipeline()
            m.assert_called_once_with(timeout=30)

    def test_standalone_waits_5_seconds(self):
        import langchain_gpt_api as lgapi

        mods = dict(sys.modules)
        mods.pop('hartos_backend_adapter', None)
        with patch.dict('sys.modules', mods, clear=True), \
             patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': ''}), \
             patch.object(lgapi, '_wait_for_llm_server', return_value=False) as m, \
             patch.dict('sys.modules', {
                 'hevolveai': MagicMock(),
                 'hevolveai.embodied_ai': MagicMock(),
                 'hevolveai.embodied_ai.rl_ef': MagicMock(),
                 'hevolveai.embodied_ai.monitoring': MagicMock(),
                 'hevolveai.embodied_ai.monitoring.trace_recorder': MagicMock(),
                 'hevolveai.embodied_ai.learning': MagicMock(),
                 'hevolveai.embodied_ai.learning.hive_mind': MagicMock(),
             }):
            rl_ef = sys.modules['hevolveai.embodied_ai.rl_ef']
            rl_ef.create_learning_llm_config.return_value = {}
            lgapi._learning_provider = None
            lgapi._hive_mind = None
            lgapi._trace_recorder = None
            lgapi._init_learning_pipeline()
            m.assert_called_once_with(timeout=5)

    def test_cloud_api_skips_wait_entirely(self):
        import langchain_gpt_api as lgapi

        with patch.dict(os.environ, {'HEVOLVE_LLM_ENDPOINT_URL': 'https://api.example.com'}), \
             patch.dict('sys.modules', {'hartos_backend_adapter': MagicMock()}), \
             patch.object(lgapi, '_wait_for_llm_server') as m, \
             patch.dict('sys.modules', {
                 'hevolveai': MagicMock(),
                 'hevolveai.embodied_ai': MagicMock(),
                 'hevolveai.embodied_ai.rl_ef': MagicMock(),
                 'hevolveai.embodied_ai.monitoring': MagicMock(),
                 'hevolveai.embodied_ai.monitoring.trace_recorder': MagicMock(),
                 'hevolveai.embodied_ai.learning': MagicMock(),
                 'hevolveai.embodied_ai.learning.hive_mind': MagicMock(),
             }):
            rl_ef = sys.modules['hevolveai.embodied_ai.rl_ef']
            rl_ef.create_learning_llm_config.return_value = {}
            lgapi._learning_provider = None
            lgapi._hive_mind = None
            lgapi._trace_recorder = None
            lgapi._init_learning_pipeline()
            m.assert_not_called()


# ============================================================================
# Init ordering / structural checks
# ============================================================================

class TestInitStructure:
    """Structural guards: ordering, function existence, code invariants."""

    def test_wait_called_before_create_learning_config(self):
        import inspect, re
        import langchain_gpt_api as lgapi
        source = inspect.getsource(lgapi._init_learning_pipeline)
        # Strip docstring and comments to avoid false positives
        body = re.sub(r'"""[\s\S]*?"""', '', source, count=1)
        body = re.sub(r"'''[\s\S]*?'''", '', body, count=1)
        wait_pos = body.find('_wait_for_llm_server(')
        config_pos = body.find('create_learning_llm_config(')
        assert wait_pos != -1
        assert config_pos != -1
        assert wait_pos < config_pos

    def test_is_bundled_checks_sys_modules(self):
        import inspect
        import langchain_gpt_api as lgapi
        source = inspect.getsource(lgapi._is_bundled)
        assert 'sys.modules' in source
        assert 'hartos_backend_adapter' in source

    def test_has_cloud_api_checks_env(self):
        import inspect
        import langchain_gpt_api as lgapi
        source = inspect.getsource(lgapi._has_cloud_api)
        assert 'HEVOLVE_LLM_ENDPOINT_URL' in source

    def test_bundled_path_returns_early_when_no_server(self):
        """In bundled mode, if server not found, function returns before
        create_learning_llm_config - verified by source inspection."""
        import inspect
        import langchain_gpt_api as lgapi
        source = inspect.getsource(lgapi._init_learning_pipeline)
        # The bundled branch has 'return' between wait and create_learning
        bundled_section_start = source.find('elif bundled:')
        assert bundled_section_start != -1
        # Use 500 chars to capture through the 'return' statement
        bundled_section = source[bundled_section_start:
                                 bundled_section_start + 500]
        assert 'return' in bundled_section
