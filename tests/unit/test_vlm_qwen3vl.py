"""
Test Suite for VLM Agent Integration with Qwen3-VL

Covers the full VLM pipeline:
  - Qwen3VLBackend: singleton, API calls, response parsing, coordinate normalization
  - VLMAgentContext: availability checks, screen context, action execution, history
  - VLM Adapter: tier selection, circuit breaker, fallback chain
  - Integration: unified pipeline replacement, full action loop

All tests run offline with mocks — no live VLM server required.
"""

import base64
import io
import json
import os
import sys
import time
import pytest
from unittest.mock import patch, MagicMock, PropertyMock, call

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_qwen3vl_singleton():
    """Reset Qwen3VLBackend singleton before/after each test."""
    import integrations.vlm.qwen3vl_backend as mod
    old = mod._instance
    mod._instance = None
    yield
    mod._instance = old


@pytest.fixture(autouse=True)
def reset_vlm_context_singleton():
    """Reset VLMAgentContext singleton before/after each test."""
    import integrations.vlm.vlm_agent_integration as mod
    old = mod._vlm_context
    mod._vlm_context = None
    yield
    mod._vlm_context = old


@pytest.fixture(autouse=True)
def reset_vlm_adapter_state():
    """Reset adapter circuit breakers and probe cache before/after each test."""
    import integrations.vlm.vlm_adapter as mod
    old_t1 = mod._tier1_fail_count
    old_t2 = mod._tier2_fail_count
    old_cache = dict(mod._probe_cache)
    mod._tier1_fail_count = 0
    mod._tier2_fail_count = 0
    mod._probe_cache = {'ts': 0, 'result': None}
    yield
    mod._tier1_fail_count = old_t1
    mod._tier2_fail_count = old_t2
    mod._probe_cache = old_cache


@pytest.fixture
def sample_screenshot_b64():
    """Create a tiny valid PNG encoded as base64 for testing."""
    try:
        from PIL import Image
        img = Image.new('RGB', (1920, 1080), color=(0, 0, 128))
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return base64.b64encode(buf.getvalue()).decode('ascii')
    except ImportError:
        # Fallback: a minimal 1x1 PNG (valid but will trigger dimension fallback)
        # 1x1 transparent PNG bytes
        png_bytes = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
            b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00'
            b'\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00'
            b'\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        return base64.b64encode(png_bytes).decode('ascii')


@pytest.fixture
def mock_api_response_unified():
    """Realistic Qwen3-VL unified response with UI elements and action."""
    return {
        "UI_Elements": [
            {"id": 1, "type": "button", "label": "Save", "bbox": [100, 50, 200, 80]},
            {"id": 2, "type": "textfield", "label": "filename", "bbox": [210, 50, 400, 80]},
            {"id": 3, "type": "button", "label": "Cancel", "bbox": [410, 50, 500, 80]},
            {"id": 4, "type": "menu", "label": "File", "bbox": [10, 0, 50, 25]},
        ],
        "Reasoning": "The save dialog is open with a filename field and Save/Cancel buttons.",
        "Next Action": "left_click",
        "Box ID": 1,
        "coordinate": [150, 65],
        "value": "",
        "Status": "IN_PROGRESS"
    }


@pytest.fixture
def mock_api_response_normalized():
    """Response with coordinates in Qwen3-VL [0,1000] normalized range."""
    return {
        "UI_Elements": [
            {"id": 1, "type": "button", "label": "OK", "bbox": [100, 200, 300, 250]},
            {"id": 2, "type": "button", "label": "Cancel", "bbox": [400, 200, 600, 250]},
        ],
        "Reasoning": "Confirmation dialog shown.",
        "Next Action": "left_click",
        "Box ID": 1,
        "coordinate": [200, 225],
        "value": "",
        "Status": "IN_PROGRESS"
    }


@pytest.fixture
def mock_omniparser_response():
    """Mock OmniParser-compatible parsed screen output."""
    return {
        "screen_info": '1: button "Save"\n2: textfield "filename"',
        "parsed_content_list": [
            {"idx": 1, "type": "button", "content": "Save", "bbox": [100, 50, 200, 80]},
            {"idx": 2, "type": "textfield", "content": "filename", "bbox": [210, 50, 400, 80]},
        ],
        "som_image_base64": "dummybase64",
        "original_screenshot_base64": "dummybase64",
        "width": 1920,
        "height": 1080,
        "latency": 0.5,
    }


def _make_api_success(content_json):
    """Build a mock requests.Response for a successful OpenAI-compatible API call."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "content": json.dumps(content_json)
            }
        }]
    }
    return mock_resp


def _make_health_ok():
    """Build a mock response for a health check."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    return mock_resp


def _make_health_fail():
    """Build a mock response for a failed health check."""
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    return mock_resp


# ═══════════════════════════════════════════════════════════════════════════
# Qwen3VLBackend Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestQwen3VLBackendSingleton:
    """Singleton pattern for Qwen3VLBackend."""

    def test_singleton_pattern(self):
        """get_qwen3vl_backend() returns the same instance on repeated calls."""
        from integrations.vlm.qwen3vl_backend import get_qwen3vl_backend
        a = get_qwen3vl_backend()
        b = get_qwen3vl_backend()
        assert a is b, "Singleton should return the same object"

    def test_singleton_creates_instance(self):
        """First call creates a Qwen3VLBackend instance."""
        from integrations.vlm.qwen3vl_backend import get_qwen3vl_backend, Qwen3VLBackend
        instance = get_qwen3vl_backend()
        assert isinstance(instance, Qwen3VLBackend)


class TestQwen3VLBackendParseAndReason:
    """parse_and_reason() — the unified screen parse + action decision call."""

    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._call_api')
    def test_parse_and_reason_basic(self, mock_api, mock_dims,
                                     sample_screenshot_b64,
                                     mock_api_response_unified):
        """Basic parse_and_reason returns expected structure with UI elements."""
        mock_api.return_value = json.dumps(mock_api_response_unified)
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        backend = Qwen3VLBackend()
        result = backend.parse_and_reason(sample_screenshot_b64, "Click the Save button")

        assert 'screen_info' in result
        assert 'parsed_content_list' in result
        assert 'action_json' in result
        assert 'reasoning' in result
        assert 'latency' in result

        # Check UI elements parsed correctly
        assert len(result['parsed_content_list']) == 4
        assert result['parsed_content_list'][0]['content'] == 'Save'
        assert result['parsed_content_list'][0]['type'] == 'button'

        # Check action JSON
        assert result['action_json']['Next Action'] == 'left_click'
        assert result['action_json']['coordinate'] == [150, 65]
        assert result['action_json']['Status'] == 'IN_PROGRESS'

    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._call_api')
    def test_parse_and_reason_with_normalized_coordinates(
            self, mock_api, mock_dims, sample_screenshot_b64,
            mock_api_response_normalized):
        """Qwen3-VL [0,1000] coordinates are converted to pixel coords."""
        mock_api.return_value = json.dumps(mock_api_response_normalized)
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        backend = Qwen3VLBackend()
        result = backend.parse_and_reason(sample_screenshot_b64, "Click OK")

        elements = result['parsed_content_list']
        assert len(elements) == 2

        # bbox [100, 200, 300, 250] normalized → pixel with 1920x1080
        # x1 = 100*1920/1000 = 192, y1 = 200*1080/1000 = 216
        # x2 = 300*1920/1000 = 576, y2 = 250*1080/1000 = 270
        ok_bbox = elements[0]['bbox']
        assert ok_bbox == [192, 216, 576, 270], f"Expected normalized bbox, got {ok_bbox}"

    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._call_api')
    def test_parse_screen_omniparser_compat(self, mock_api, mock_dims,
                                             sample_screenshot_b64):
        """parse_screen() output matches OmniParser format keys."""
        parse_response = {
            "UI_Elements": [
                {"id": 1, "type": "button", "label": "Save", "bbox": [100, 50, 200, 80]},
            ]
        }
        mock_api.return_value = json.dumps(parse_response)
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        backend = Qwen3VLBackend()
        result = backend.parse_screen(sample_screenshot_b64)

        # Must have OmniParser-compatible keys
        assert 'screen_info' in result
        assert 'parsed_content_list' in result
        assert 'som_image_base64' in result
        assert 'original_screenshot_base64' in result
        assert 'width' in result
        assert 'height' in result
        assert 'latency' in result

        # Check content structure matches OmniParser format
        elem = result['parsed_content_list'][0]
        assert 'idx' in elem
        assert 'type' in elem
        assert 'content' in elem
        assert 'bbox' in elem

    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._call_api')
    def test_describe_scene(self, mock_api, sample_screenshot_b64):
        """describe_scene() returns raw text from API."""
        mock_api.return_value = "A desktop showing a file manager with several folders"
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        backend = Qwen3VLBackend()
        result = backend.describe_scene(sample_screenshot_b64)
        assert result == "A desktop showing a file manager with several folders"
        mock_api.assert_called_once()

    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._call_api')
    def test_describe_scene_custom_prompt(self, mock_api, sample_screenshot_b64):
        """describe_scene() forwards custom prompt."""
        mock_api.return_value = "Two buttons visible"
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        backend = Qwen3VLBackend()
        result = backend.describe_scene(sample_screenshot_b64, prompt="Count the buttons")
        assert result == "Two buttons visible"

        # Verify prompt was passed in messages
        call_args = mock_api.call_args[0][0]
        assert call_args[0]['content'][0]['text'] == "Count the buttons"


class TestQwen3VLResponseParsing:
    """_parse_unified_response() — JSON extraction from various formats."""

    def test_parse_response_json_in_markdown(self):
        """Extract JSON from markdown code blocks."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        backend = Qwen3VLBackend()

        text = '```json\n{"UI_Elements": [{"id": 1, "type": "button", "label": "OK", "bbox": [10, 20, 30, 40]}], "Next Action": "left_click"}\n```'
        result = backend._parse_unified_response(text)
        assert result['Next Action'] == 'left_click'
        assert len(result['UI_Elements']) == 1
        assert result['UI_Elements'][0]['label'] == 'OK'

    def test_parse_response_raw_json(self):
        """Parse bare JSON without code block markers."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        backend = Qwen3VLBackend()

        text = '{"UI_Elements": [], "Next Action": "None", "Status": "DONE", "Reasoning": "All done"}'
        result = backend._parse_unified_response(text)
        assert result['Next Action'] == 'None'
        assert result['Status'] == 'DONE'
        assert result['Reasoning'] == 'All done'

    def test_parse_response_malformed(self):
        """Graceful fallback on malformed/non-JSON response."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        backend = Qwen3VLBackend()

        text = "I see a screen with some buttons. I cannot determine the exact layout."
        result = backend._parse_unified_response(text)
        assert result['UI_Elements'] == []
        assert result['Next Action'] == 'None'
        assert result['Status'] == 'DONE'
        # Reasoning should contain the raw text as fallback
        assert 'I see a screen' in result['Reasoning']

    def test_parse_response_json_with_prefix_text(self):
        """JSON embedded after descriptive text."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        backend = Qwen3VLBackend()

        text = 'Here is my analysis:\n{"UI_Elements": [{"id": 1, "type": "link", "label": "Home", "bbox": [5, 5, 50, 20]}], "Next Action": "left_click", "Status": "IN_PROGRESS"}'
        result = backend._parse_unified_response(text)
        assert result['Next Action'] == 'left_click'
        assert len(result['UI_Elements']) == 1

    def test_parse_response_nested_json(self):
        """Deeply nested JSON is extracted via depth-tracking parser."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        backend = Qwen3VLBackend()

        inner = {
            "UI_Elements": [
                {"id": 1, "type": "button", "label": "Submit", "bbox": [100, 200, 300, 250]}
            ],
            "Next Action": "left_click",
            "Box ID": 1,
            "coordinate": [200, 225],
            "value": "",
            "Status": "IN_PROGRESS",
            "Reasoning": "Submit button found"
        }
        text = f"Analysis complete. {json.dumps(inner)}"
        result = backend._parse_unified_response(text)
        assert result['Next Action'] == 'left_click'
        assert result['Box ID'] == 1


class TestQwen3VLCoordinateNormalization:
    """_is_normalized_1000() and _normalize_bbox() coordinate conversion."""

    def test_coordinate_normalization_detected(self):
        """Coordinates in [0,1000] range detected when image > 1000px."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        # bbox values all <= 1000, image is 1920x1080
        assert Qwen3VLBackend._is_normalized_1000([100, 200, 300, 400], 1920, 1080) is True
        assert Qwen3VLBackend._is_normalized_1000([500, 500, 1000, 1000], 1920, 1080) is True

    def test_coordinate_normalization_not_detected_for_small_image(self):
        """Coordinates not flagged as normalized for small images."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        # Image is 800x600 — values <= 1000 are likely real pixel coords
        assert Qwen3VLBackend._is_normalized_1000([100, 200, 300, 400], 800, 600) is False

    def test_coordinate_normalization_not_detected_when_exceeds_1000(self):
        """Coordinates > 1000 are already pixel coordinates."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        # bbox has value > 1000 → already pixel coords
        assert Qwen3VLBackend._is_normalized_1000([100, 200, 1500, 400], 1920, 1080) is False

    def test_normalize_bbox_conversion(self):
        """_normalize_bbox converts [0,1000] to actual pixel coordinates."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        # [500, 500, 1000, 1000] on 1920x1080
        result = Qwen3VLBackend._normalize_bbox([500, 500, 1000, 1000], 1920, 1080)
        assert result == [960, 540, 1920, 1080]

    def test_normalize_bbox_zero_origin(self):
        """Zero coordinates remain zero after normalization."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        result = Qwen3VLBackend._normalize_bbox([0, 0, 500, 500], 1920, 1080)
        assert result == [0, 0, 960, 540]

    def test_is_normalized_1000_empty_bbox(self):
        """Empty bbox returns False."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        assert Qwen3VLBackend._is_normalized_1000([], 1920, 1080) is False

    def test_is_normalized_1000_short_bbox(self):
        """Bbox with fewer than 4 elements returns False."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        assert Qwen3VLBackend._is_normalized_1000([100, 200], 1920, 1080) is False


class TestQwen3VLBoxIdResolution:
    """When coordinate is None, resolve from Box ID center."""

    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._call_api')
    def test_box_id_to_coordinate_resolution(self, mock_api, mock_dims,
                                              sample_screenshot_b64):
        """When API returns Box ID but no coordinate, resolve from element bbox center."""
        response_no_coord = {
            "UI_Elements": [
                {"id": 1, "type": "button", "label": "OK", "bbox": [1100, 500, 1300, 550]},
            ],
            "Reasoning": "Click OK to confirm",
            "Next Action": "left_click",
            "Box ID": 1,
            "coordinate": None,
            "value": "",
            "Status": "IN_PROGRESS"
        }
        mock_api.return_value = json.dumps(response_no_coord)
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        backend = Qwen3VLBackend()
        result = backend.parse_and_reason(sample_screenshot_b64, "Click OK")

        # coordinate should be resolved to center of bbox [1100, 500, 1300, 550]
        # center = ((1100+1300)/2, (500+550)/2) = (1200, 525)
        assert result['action_json']['coordinate'] == [1200, 525]

    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._call_api')
    def test_box_id_no_match_leaves_coordinate_none(self, mock_api, mock_dims,
                                                     sample_screenshot_b64):
        """When Box ID doesn't match any element, coordinate stays None."""
        response = {
            "UI_Elements": [
                {"id": 1, "type": "button", "label": "OK", "bbox": [100, 50, 200, 80]},
            ],
            "Reasoning": "test",
            "Next Action": "left_click",
            "Box ID": 99,  # doesn't match any element
            "coordinate": None,
            "value": "",
            "Status": "IN_PROGRESS"
        }
        mock_api.return_value = json.dumps(response)
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        backend = Qwen3VLBackend()
        result = backend.parse_and_reason(sample_screenshot_b64, "Click something")
        assert result['action_json']['coordinate'] is None


class TestQwen3VLApiCall:
    """_call_api() — HTTP calls, timeout, error handling."""

    def test_api_call_timeout(self):
        """API call uses configured timeout."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "test response"}}]
        }

        backend = Qwen3VLBackend()
        backend.timeout = 42
        messages = [{"role": "user", "content": "test"}]

        # requests is imported inside _call_api, so patch at the top-level module
        with patch('requests.post', return_value=mock_resp) as mock_post:
            backend._call_api(messages)
            _, kwargs = mock_post.call_args
            assert kwargs['timeout'] == 42

    def test_api_call_error_handling(self):
        """API call raises on HTTP error."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        backend = Qwen3VLBackend()
        messages = [{"role": "user", "content": "test"}]

        with patch('requests.post', side_effect=ConnectionError("Connection refused")):
            with pytest.raises(ConnectionError):
                backend._call_api(messages)

    def test_api_call_http_500(self):
        """API call raises on HTTP 500."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = Exception("500 Server Error")

        backend = Qwen3VLBackend()
        messages = [{"role": "user", "content": "test"}]

        with patch('requests.post', return_value=mock_resp):
            with pytest.raises(Exception, match="500 Server Error"):
                backend._call_api(messages)


class TestQwen3VLConfiguration:
    """Environment variable configuration."""

    def test_env_var_configuration(self, monkeypatch):
        """Backend reads HEVOLVE_VLM_ENDPOINT_URL, MODEL_NAME, API_KEY."""
        monkeypatch.setenv('HEVOLVE_VLM_ENDPOINT_URL', 'http://custom:9999/v1')
        monkeypatch.setenv('HEVOLVE_VLM_MODEL_NAME', 'Qwen3-VL-7B')
        monkeypatch.setenv('HEVOLVE_VLM_API_KEY', 'secret-key-123')
        monkeypatch.setenv('HEVOLVE_VLM_TIMEOUT', '120')

        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        backend = Qwen3VLBackend()

        assert backend.base_url == 'http://custom:9999/v1'
        assert backend.model_name == 'Qwen3-VL-7B'
        assert backend.api_key == 'secret-key-123'
        assert backend.timeout == 120

    def test_env_var_fallback_to_llm_config(self, monkeypatch):
        """Without VLM-specific vars, falls back to HEVOLVE_LLM_* vars."""
        monkeypatch.delenv('HEVOLVE_VLM_ENDPOINT_URL', raising=False)
        monkeypatch.delenv('HEVOLVE_VLM_MODEL_NAME', raising=False)
        monkeypatch.delenv('HEVOLVE_VLM_API_KEY', raising=False)
        monkeypatch.setenv('HEVOLVE_LLM_ENDPOINT_URL', 'http://llm-server:8000/v1')
        monkeypatch.setenv('HEVOLVE_LLM_MODEL_NAME', 'gpt-4.1-mini')
        monkeypatch.setenv('HEVOLVE_LLM_API_KEY', 'llm-key')

        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        backend = Qwen3VLBackend()

        assert backend.base_url == 'http://llm-server:8000/v1'
        assert backend.model_name == 'gpt-4.1-mini'
        assert backend.api_key == 'llm-key'

    def test_env_var_defaults(self, monkeypatch):
        """Without any env vars, defaults are used."""
        for var in ['HEVOLVE_VLM_ENDPOINT_URL', 'HEVOLVE_VLM_MODEL_NAME',
                    'HEVOLVE_VLM_API_KEY', 'HEVOLVE_VLM_TIMEOUT',
                    'HEVOLVE_LLM_ENDPOINT_URL', 'HEVOLVE_LLM_MODEL_NAME',
                    'HEVOLVE_LLM_API_KEY']:
            monkeypatch.delenv(var, raising=False)

        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        backend = Qwen3VLBackend()

        assert backend.base_url == 'http://localhost:8000/v1'
        assert backend.model_name == 'Qwen3-VL-2B-Instruct'
        assert backend.api_key == 'dummy'
        assert backend.timeout == 60

    def test_constructor_overrides_env(self, monkeypatch):
        """Explicit constructor arguments override env vars."""
        monkeypatch.setenv('HEVOLVE_VLM_ENDPOINT_URL', 'http://env:1111/v1')

        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        backend = Qwen3VLBackend(
            base_url='http://explicit:2222/v1',
            model_name='ExplicitModel'
        )

        assert backend.base_url == 'http://explicit:2222/v1'
        assert backend.model_name == 'ExplicitModel'


class TestQwen3VLImageDimensions:
    """_get_image_dimensions() — PIL path and fallback."""

    def test_image_dimensions_from_pil(self, sample_screenshot_b64):
        """With PIL available, actual image dimensions are returned."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        try:
            from PIL import Image
            w, h = Qwen3VLBackend._get_image_dimensions(sample_screenshot_b64)
            # Our fixture creates 1920x1080 if PIL is available
            assert w == 1920
            assert h == 1080
        except ImportError:
            pytest.skip("PIL not available")

    def test_image_dimensions_fallback(self):
        """Without PIL, defaults to 1920x1080."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        with patch.dict('sys.modules', {'PIL': None, 'PIL.Image': None}):
            # Force ImportError path by passing invalid base64
            w, h = Qwen3VLBackend._get_image_dimensions("not-valid-base64")
            assert w == 1920
            assert h == 1080


class TestQwen3VLHistory:
    """Conversation history passed to API."""

    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._call_api')
    def test_history_passed_to_api(self, mock_api, mock_dims, sample_screenshot_b64):
        """Conversation history is prepended to messages sent to API."""
        done_response = {
            "UI_Elements": [],
            "Next Action": "None",
            "Status": "DONE",
            "Reasoning": "Done"
        }
        mock_api.return_value = json.dumps(done_response)
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        backend = Qwen3VLBackend()
        history = [
            {"role": "system", "content": "You are a helpful agent."},
            {"role": "user", "content": "Open the settings menu"},
            {"role": "assistant", "content": '{"Next Action": "left_click", "Status": "IN_PROGRESS"}'},
        ]

        backend.parse_and_reason(sample_screenshot_b64, "Now click Save", history=history)

        # Verify the messages list passed to _call_api includes history
        call_args = mock_api.call_args[0][0]
        assert len(call_args) == 4  # 3 history + 1 new user message
        assert call_args[0]['role'] == 'system'
        assert call_args[1]['role'] == 'user'
        assert call_args[2]['role'] == 'assistant'
        assert call_args[3]['role'] == 'user'

    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._call_api')
    def test_no_history_works(self, mock_api, mock_dims, sample_screenshot_b64):
        """parse_and_reason without history only sends one user message."""
        done_response = {
            "UI_Elements": [],
            "Next Action": "None",
            "Status": "DONE",
            "Reasoning": "Done"
        }
        mock_api.return_value = json.dumps(done_response)
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend

        backend = Qwen3VLBackend()
        backend.parse_and_reason(sample_screenshot_b64, "Do something")

        call_args = mock_api.call_args[0][0]
        assert len(call_args) == 1
        assert call_args[0]['role'] == 'user'


# ═══════════════════════════════════════════════════════════════════════════
# VLMAgentContext Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestVLMAgentContextSingleton:
    """Singleton pattern for VLMAgentContext."""

    def test_vlm_context_singleton(self):
        """get_vlm_context() returns same instance on repeated calls."""
        from integrations.vlm.vlm_agent_integration import get_vlm_context
        a = get_vlm_context()
        b = get_vlm_context()
        assert a is b


class TestVLMAgentContextAvailability:
    """Availability checks for VLM and OmniParser servers."""

    @patch('integrations.vlm.vlm_agent_integration.pooled_get')
    def test_is_vlm_available_true(self, mock_get):
        """is_vlm_available() returns True when health endpoint is 200."""
        mock_get.return_value = _make_health_ok()
        from integrations.vlm.vlm_agent_integration import VLMAgentContext

        ctx = VLMAgentContext()
        assert ctx.is_vlm_available() is True
        mock_get.assert_called_once_with("http://localhost:5001/health", timeout=2)

    @patch('integrations.vlm.vlm_agent_integration.pooled_get')
    def test_is_vlm_available_false_on_error(self, mock_get):
        """is_vlm_available() returns False on connection error."""
        mock_get.side_effect = ConnectionError("Connection refused")
        from integrations.vlm.vlm_agent_integration import VLMAgentContext

        ctx = VLMAgentContext()
        assert ctx.is_vlm_available() is False

    @patch('integrations.vlm.vlm_agent_integration.pooled_get')
    def test_is_omniparser_available_true(self, mock_get):
        """is_omniparser_available() returns True when probe is 200."""
        mock_get.return_value = _make_health_ok()
        from integrations.vlm.vlm_agent_integration import VLMAgentContext

        ctx = VLMAgentContext()
        assert ctx.is_omniparser_available() is True
        mock_get.assert_called_once_with("http://localhost:8080/probe", timeout=2)

    @patch('integrations.vlm.vlm_agent_integration.pooled_get')
    def test_is_omniparser_available_false(self, mock_get):
        """is_omniparser_available() returns False on timeout."""
        mock_get.side_effect = TimeoutError("Timed out")
        from integrations.vlm.vlm_agent_integration import VLMAgentContext

        ctx = VLMAgentContext()
        assert ctx.is_omniparser_available() is False


class TestVLMAgentContextScreenContext:
    """get_screen_context() — OmniParser screen parsing."""

    @patch('integrations.vlm.vlm_agent_integration.pooled_post')
    @patch('integrations.vlm.vlm_agent_integration.pooled_get')
    def test_get_screen_context(self, mock_get, mock_post, mock_omniparser_response):
        """get_screen_context() returns parsed OmniParser response."""
        # Mock probe check
        mock_get.return_value = _make_health_ok()

        # Mock parse_screen POST
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = mock_omniparser_response
        mock_post.return_value = mock_post_resp

        from integrations.vlm.vlm_agent_integration import VLMAgentContext
        ctx = VLMAgentContext()
        result = ctx.get_screen_context()

        assert result is not None
        assert result['screen_info'] == mock_omniparser_response['screen_info']
        assert len(result['parsed_content_list']) == 2
        assert result['width'] == 1920

    @patch('integrations.vlm.vlm_agent_integration.pooled_get')
    def test_get_screen_context_unavailable(self, mock_get):
        """get_screen_context() returns None when OmniParser is unavailable."""
        mock_get.side_effect = ConnectionError("Not reachable")
        from integrations.vlm.vlm_agent_integration import VLMAgentContext

        ctx = VLMAgentContext()
        result = ctx.get_screen_context()
        assert result is None


class TestVLMAgentContextActions:
    """Context injection and action execution."""

    @patch('integrations.vlm.vlm_agent_integration.pooled_post')
    @patch('integrations.vlm.vlm_agent_integration.pooled_get')
    def test_inject_visual_context(self, mock_get, mock_post, mock_omniparser_response):
        """inject_visual_context_into_ledger_task() adds visual_context key."""
        mock_get.return_value = _make_health_ok()

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = mock_omniparser_response
        mock_post.return_value = mock_post_resp

        from integrations.vlm.vlm_agent_integration import VLMAgentContext
        ctx = VLMAgentContext()

        task = {"task_id": "t1", "description": "Test task"}
        enhanced = ctx.inject_visual_context_into_ledger_task(task)

        assert "visual_context" in enhanced
        assert enhanced["visual_context"]["has_screen_info"] is True
        assert enhanced["visual_context"]["visible_elements"] == 2
        assert enhanced["visual_context"]["screen_dimensions"]["width"] == 1920

    @patch('integrations.vlm.vlm_agent_integration.pooled_get')
    def test_inject_visual_context_unavailable(self, mock_get):
        """When VLM is unavailable, context has has_screen_info=False."""
        mock_get.side_effect = ConnectionError("Not available")
        from integrations.vlm.vlm_agent_integration import VLMAgentContext

        ctx = VLMAgentContext()
        task = {"task_id": "t1"}
        enhanced = ctx.inject_visual_context_into_ledger_task(task)

        assert enhanced["visual_context"]["has_screen_info"] is False
        assert "not available" in enhanced["visual_context"]["note"].lower()

    @patch('integrations.vlm.vlm_agent_integration.pooled_post')
    @patch('integrations.vlm.vlm_agent_integration.pooled_get')
    def test_execute_vlm_action(self, mock_get, mock_post):
        """execute_vlm_action() sends action to VLM server and tracks history."""
        mock_get.return_value = _make_health_ok()

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {"status": "success", "output": "Clicked at [150, 65]"}
        mock_post.return_value = mock_post_resp

        from integrations.vlm.vlm_agent_integration import VLMAgentContext
        ctx = VLMAgentContext()

        result = ctx.execute_vlm_action(
            action="left_click",
            parameters={"coordinate": [150, 65]},
            user_id="user1",
            prompt_id="task1"
        )

        assert result['status'] == 'success'
        assert len(ctx.action_history) == 1
        assert ctx.action_history[0]['action'] == 'left_click'

    @patch('integrations.vlm.vlm_agent_integration.pooled_get')
    def test_execute_vlm_action_unavailable(self, mock_get):
        """execute_vlm_action() returns error when VLM server is down."""
        mock_get.side_effect = ConnectionError("Not available")
        from integrations.vlm.vlm_agent_integration import VLMAgentContext

        ctx = VLMAgentContext()
        result = ctx.execute_vlm_action("left_click", {"coordinate": [100, 100]})

        assert result['status'] == 'error'
        assert 'not available' in result['message'].lower()


class TestVLMAgentContextWindowsCommand:
    """execute_windows_command() — multi-step Win+R flow."""

    @patch('integrations.vlm.vlm_agent_integration.pooled_post')
    @patch('integrations.vlm.vlm_agent_integration.pooled_get')
    def test_execute_windows_command(self, mock_get, mock_post):
        """Windows command executes 4 steps: Win+R, wait, type, Return."""
        mock_get.return_value = _make_health_ok()

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {"status": "success", "output": "done"}
        mock_post.return_value = mock_post_resp

        from integrations.vlm.vlm_agent_integration import VLMAgentContext
        ctx = VLMAgentContext()

        result = ctx.execute_windows_command("notepad")

        assert result['status'] == 'success'
        assert result['command'] == 'notepad'
        assert len(result['results']) == 4  # hotkey, wait, type, hotkey

        # 4 actions tracked in history
        assert len(ctx.action_history) == 4

    @patch('integrations.vlm.vlm_agent_integration.pooled_post')
    @patch('integrations.vlm.vlm_agent_integration.pooled_get')
    def test_execute_windows_command_step_failure(self, mock_get, mock_post):
        """If any step fails, command returns error with partial results."""
        mock_get.return_value = _make_health_ok()

        # First call succeeds, second fails
        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.json.return_value = {"status": "success"}

        fail_resp = MagicMock()
        fail_resp.status_code = 200
        fail_resp.json.return_value = {"status": "error", "message": "action failed"}

        mock_post.side_effect = [success_resp, fail_resp]

        from integrations.vlm.vlm_agent_integration import VLMAgentContext
        ctx = VLMAgentContext()

        result = ctx.execute_windows_command("calc")
        assert result['status'] == 'error'
        assert 'Failed at step' in result['message']


class TestVLMAgentContextHistory:
    """History limits for actions and screens."""

    def test_action_history_limit(self):
        """Action history is capped at 50 entries."""
        from integrations.vlm.vlm_agent_integration import VLMAgentContext
        ctx = VLMAgentContext()

        # Manually populate 55 entries
        for i in range(55):
            ctx.action_history.append({
                "timestamp": f"2026-03-09T00:00:{i:02d}",
                "action": f"action_{i}",
                "parameters": {},
                "result": "success"
            })
            if len(ctx.action_history) > 50:
                ctx.action_history.pop(0)

        assert len(ctx.action_history) == 50
        # Oldest entries removed, newest kept
        assert ctx.action_history[0]['action'] == 'action_5'
        assert ctx.action_history[-1]['action'] == 'action_54'

    @patch('integrations.vlm.vlm_agent_integration.pooled_post')
    @patch('integrations.vlm.vlm_agent_integration.pooled_get')
    def test_screen_history_limit(self, mock_get, mock_post, mock_omniparser_response):
        """Screen history is capped at 10 entries."""
        mock_get.return_value = _make_health_ok()

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = mock_omniparser_response
        mock_post.return_value = mock_post_resp

        from integrations.vlm.vlm_agent_integration import VLMAgentContext
        ctx = VLMAgentContext()

        # Call get_screen_context 15 times
        for _ in range(15):
            ctx.get_screen_context()

        assert len(ctx.screen_history) == 10


class TestVLMAgentContextFeedbackAndTools:
    """get_visual_feedback_for_task() and create_vlm_enabled_tool()."""

    @patch('integrations.vlm.vlm_agent_integration.pooled_post')
    @patch('integrations.vlm.vlm_agent_integration.pooled_get')
    def test_get_visual_feedback(self, mock_get, mock_post, mock_omniparser_response):
        """Visual feedback generates descriptive text."""
        mock_get.return_value = _make_health_ok()

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = mock_omniparser_response
        mock_post.return_value = mock_post_resp

        from integrations.vlm.vlm_agent_integration import VLMAgentContext
        ctx = VLMAgentContext()

        # Add some action history
        ctx.action_history.append({
            "timestamp": "2026-03-09T12:00:00",
            "action": "left_click",
            "parameters": {"coordinate": [100, 200]},
            "result": "success"
        })

        feedback = ctx.get_visual_feedback_for_task("Save the document")

        assert "Save the document" in feedback
        assert "2 UI elements" in feedback
        assert "1920x1080" in feedback
        assert "left_click" in feedback

    @patch('integrations.vlm.vlm_agent_integration.pooled_get')
    def test_get_visual_feedback_unavailable(self, mock_get):
        """Feedback returns unavailable message when VLM is down."""
        mock_get.side_effect = ConnectionError("down")
        from integrations.vlm.vlm_agent_integration import VLMAgentContext

        ctx = VLMAgentContext()
        feedback = ctx.get_visual_feedback_for_task("anything")
        assert "unavailable" in feedback.lower()

    def test_create_vlm_enabled_tool(self):
        """Tool definition has correct OpenAI function calling structure."""
        from integrations.vlm.vlm_agent_integration import VLMAgentContext
        ctx = VLMAgentContext()

        tool = ctx.create_vlm_enabled_tool("screen_interact", "Interact with the screen")

        assert tool['type'] == 'function'
        assert tool['function']['name'] == 'screen_interact'
        assert tool['function']['description'] == 'Interact with the screen'
        assert 'parameters' in tool['function']
        assert 'action' in tool['function']['parameters']['properties']

        # Check action enum includes expected values
        action_enum = tool['function']['parameters']['properties']['action']['enum']
        assert 'left_click' in action_enum
        assert 'type' in action_enum
        assert 'hotkey' in action_enum
        assert 'scroll_up' in action_enum

    @patch('integrations.vlm.vlm_agent_integration.pooled_get')
    def test_status_summary(self, mock_get):
        """get_status_summary() includes all expected keys."""
        mock_get.return_value = _make_health_ok()
        from integrations.vlm.vlm_agent_integration import VLMAgentContext

        ctx = VLMAgentContext()
        status = ctx.get_status_summary()

        assert 'vlm_available' in status
        assert 'omniparser_available' in status
        assert 'screen_history_count' in status
        assert 'action_history_count' in status
        assert 'last_screen_capture' in status
        assert 'last_action' in status
        assert status['screen_history_count'] == 0
        assert status['action_history_count'] == 0
        assert status['last_screen_capture'] is None
        assert status['last_action'] is None


# ═══════════════════════════════════════════════════════════════════════════
# VLM Adapter Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestVLMAdapterTierSelection:
    """Tier routing: Qwen3VL > OmniParser > lightweight."""

    @patch('integrations.vlm.vlm_adapter._HAS_PYAUTOGUI', True)
    @patch('integrations.vlm.local_loop.run_local_agentic_loop')
    def test_adapter_tier1_when_pyautogui_available(self, mock_loop):
        """Tier 1 (in-process) used when pyautogui is available."""
        mock_loop.return_value = {
            "status": "success",
            "extracted_responses": [],
            "execution_time_seconds": 1.0
        }

        import integrations.vlm.vlm_adapter as mod
        mod._tier1_fail_count = 0

        result = mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})

        assert result is not None
        assert result['status'] == 'success'
        mock_loop.assert_called_once()
        # Verify tier='inprocess' was passed
        _, kwargs = mock_loop.call_args
        assert kwargs.get('tier', mock_loop.call_args[0][1] if len(mock_loop.call_args[0]) > 1 else None) == 'inprocess'

    @patch('integrations.vlm.vlm_adapter._node_tier', 'flat')
    @patch('integrations.vlm.vlm_adapter._HAS_PYAUTOGUI', False)
    @patch('integrations.vlm.local_loop.run_local_agentic_loop')
    def test_adapter_tier2_flat_mode(self, mock_loop):
        """Tier 2 (HTTP local) used when pyautogui unavailable and flat mode."""
        mock_loop.return_value = {
            "status": "success",
            "extracted_responses": [],
            "execution_time_seconds": 2.0
        }

        import integrations.vlm.vlm_adapter as mod
        mod._tier2_fail_count = 0

        result = mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})

        assert result is not None
        assert result['status'] == 'success'

    @patch('integrations.vlm.vlm_adapter._node_tier', 'central')
    @patch('integrations.vlm.vlm_adapter._HAS_PYAUTOGUI', False)
    def test_adapter_tier3_returns_none(self):
        """Tier 3 (Crossbar) returns None to signal caller should use WAMP."""
        import integrations.vlm.vlm_adapter as mod
        result = mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})
        assert result is None


class TestVLMAdapterCircuitBreaker:
    """Circuit breaker opens after 2 consecutive failures."""

    @patch('integrations.vlm.vlm_adapter._node_tier', 'central')
    @patch('integrations.vlm.vlm_adapter._HAS_PYAUTOGUI', True)
    @patch('integrations.vlm.local_loop.run_local_agentic_loop',
           side_effect=RuntimeError("GPU error"))
    def test_adapter_circuit_breaker(self, mock_loop):
        """After 2 failures, tier 1 is skipped (circuit opens)."""
        import integrations.vlm.vlm_adapter as mod
        mod._tier1_fail_count = 0

        # First call — fails, count=1
        mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test1"})
        assert mod._tier1_fail_count == 1

        # Second call — fails, count=2
        mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test2"})
        assert mod._tier1_fail_count == 2

        # Third call — tier 1 is skipped (circuit open), returns None (tier 3)
        mock_loop.reset_mock()
        result = mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test3"})
        # With node_tier='central' and tier1 circuit open, should fall through to tier 3
        assert result is None
        # run_local_agentic_loop should NOT have been called for tier 1
        # (it was called 0 times because circuit breaker skipped it)
        mock_loop.assert_not_called()

    @patch('integrations.vlm.vlm_adapter._HAS_PYAUTOGUI', True)
    @patch('integrations.vlm.local_loop.run_local_agentic_loop')
    def test_adapter_circuit_breaker_resets_on_success(self, mock_loop):
        """Successful call resets the failure counter."""
        import integrations.vlm.vlm_adapter as mod
        mod._tier1_fail_count = 1  # Already had one failure

        mock_loop.return_value = {"status": "success", "extracted_responses": [], "execution_time_seconds": 1.0}
        mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})
        assert mod._tier1_fail_count == 0  # Reset after success

    def test_reset_circuit_breakers(self):
        """reset_circuit_breakers() clears all state."""
        import integrations.vlm.vlm_adapter as mod
        mod._tier1_fail_count = 5
        mod._tier2_fail_count = 5
        mod._probe_cache = {'ts': 99999, 'result': True}

        mod.reset_circuit_breakers()

        assert mod._tier1_fail_count == 0
        assert mod._tier2_fail_count == 0
        assert mod._probe_cache['ts'] == 0
        assert mod._probe_cache['result'] is None


class TestVLMAdapterFallbackChain:
    """When primary tier fails, falls back to next tier."""

    @patch('integrations.vlm.vlm_adapter._node_tier', 'flat')
    @patch('integrations.vlm.vlm_adapter._HAS_PYAUTOGUI', True)
    @patch('integrations.vlm.local_loop.run_local_agentic_loop')
    def test_adapter_fallback_chain(self, mock_loop):
        """Tier 1 failure falls through to tier 2 in flat mode."""
        import integrations.vlm.vlm_adapter as mod
        mod._tier1_fail_count = 0
        mod._tier2_fail_count = 0

        # Tier 1 fails, tier 2 succeeds
        call_count = [0]
        def side_effect(msg, tier):
            call_count[0] += 1
            if tier == 'inprocess':
                raise RuntimeError("Tier 1 failed")
            return {"status": "success", "extracted_responses": [], "execution_time_seconds": 1.0}

        mock_loop.side_effect = side_effect

        result = mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})

        assert result is not None
        assert result['status'] == 'success'
        assert call_count[0] == 2  # Called for both tiers

    @patch('integrations.vlm.vlm_adapter._node_tier', 'flat')
    @patch('integrations.vlm.vlm_adapter._HAS_PYAUTOGUI', True)
    @patch('integrations.vlm.local_loop.run_local_agentic_loop',
           side_effect=RuntimeError("All failed"))
    def test_adapter_all_tiers_fail(self, mock_loop):
        """When both tier 1 and 2 fail, returns None for tier 3."""
        import integrations.vlm.vlm_adapter as mod
        mod._tier1_fail_count = 0
        mod._tier2_fail_count = 0

        result = mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})
        assert result is None


class TestVLMAdapterAvailabilityCheck:
    """check_vlm_available() — quick availability check."""

    @patch('integrations.vlm.vlm_adapter._HAS_PYAUTOGUI', True)
    def test_available_with_pyautogui(self):
        """Available when pyautogui is installed."""
        from integrations.vlm.vlm_adapter import check_vlm_available
        assert check_vlm_available() is True

    @patch('integrations.vlm.vlm_adapter._HAS_PYAUTOGUI', False)
    @patch('integrations.vlm.vlm_adapter._node_tier', 'flat')
    @patch('integrations.vlm.vlm_adapter._probe_local_services', return_value=True)
    def test_available_flat_mode_services_up(self, mock_probe):
        """Available in flat mode when local services respond."""
        from integrations.vlm.vlm_adapter import check_vlm_available
        assert check_vlm_available() is True

    @patch('integrations.vlm.vlm_adapter._HAS_PYAUTOGUI', False)
    @patch('integrations.vlm.vlm_adapter._node_tier', 'central')
    def test_available_central_mode_always_true(self):
        """Central mode assumes Crossbar is available (tier 3)."""
        from integrations.vlm.vlm_adapter import check_vlm_available
        assert check_vlm_available() is True


class TestVLMAdapterProbeCache:
    """_probe_local_services() caching behavior."""

    @patch('requests.get')
    def test_probe_cache_ttl(self, mock_get):
        """Probe result is cached for 60 seconds."""
        import integrations.vlm.vlm_adapter as mod

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        # First call — hits network
        mod._probe_cache = {'ts': 0, 'result': None}
        result1 = mod._probe_local_services()
        assert result1 is True
        first_call_count = mock_get.call_count

        # Second call within TTL — uses cache
        result2 = mod._probe_local_services()
        assert result2 is True
        assert mock_get.call_count == first_call_count  # No new calls

    def test_probe_cache_expired(self):
        """Expired cache triggers new probe."""
        import integrations.vlm.vlm_adapter as mod
        mod._probe_cache = {'ts': time.time() - 120, 'result': True}  # Expired

        with patch('requests.get', side_effect=ConnectionError("down")):
            result = mod._probe_local_services()
            assert result is False


# ═══════════════════════════════════════════════════════════════════════════
# Integration Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestQwen3VLReplacesOmniParserPipeline:
    """Verify single Qwen3-VL call replaces the 3-model OmniParser pipeline."""

    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._call_api')
    def test_qwen3vl_replaces_omniparser_pipeline(self, mock_api, mock_dims,
                                                    sample_screenshot_b64):
        """Single Qwen3-VL call produces both parsing and action — replacing
        the OmniParser (YOLO+Florence) + MiniCPM + separate LLM pipeline."""
        unified_response = {
            "UI_Elements": [
                {"id": 1, "type": "button", "label": "Save", "bbox": [100, 50, 200, 80]},
                {"id": 2, "type": "textfield", "label": "filename", "bbox": [210, 50, 400, 80]},
            ],
            "Reasoning": "Save dialog is open. Clicking Save to complete.",
            "Next Action": "left_click",
            "Box ID": 1,
            "coordinate": [150, 65],
            "value": "",
            "Status": "IN_PROGRESS"
        }
        mock_api.return_value = json.dumps(unified_response)

        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        backend = Qwen3VLBackend()

        result = backend.parse_and_reason(sample_screenshot_b64, "Save the file")

        # Single call produced both parsing output AND action decision
        assert len(result['parsed_content_list']) == 2
        assert result['action_json']['Next Action'] == 'left_click'
        assert result['action_json']['coordinate'] == [150, 65]

        # Only one API call was made (not 3 separate calls)
        assert mock_api.call_count == 1

    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._call_api')
    def test_parse_screen_also_single_call(self, mock_api, mock_dims,
                                            sample_screenshot_b64):
        """parse_screen() replaces OmniParser YOLO+Florence with one call."""
        parse_resp = {
            "UI_Elements": [
                {"id": 1, "type": "button", "label": "OK", "bbox": [500, 400, 600, 430]},
                {"id": 2, "type": "icon", "label": "close", "bbox": [1900, 0, 1920, 20]},
            ]
        }
        mock_api.return_value = json.dumps(parse_resp)

        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        backend = Qwen3VLBackend()

        result = backend.parse_screen(sample_screenshot_b64)

        assert len(result['parsed_content_list']) == 2
        assert result['parsed_content_list'][0]['content'] == 'OK'
        assert mock_api.call_count == 1


class TestFullActionLoop:
    """Full pipeline: screenshot -> parse -> reason -> action -> verify."""

    @patch('integrations.vlm.local_loop.time.sleep')
    @patch('integrations.vlm.local_computer_tool.execute_action')
    @patch('integrations.vlm.local_computer_tool.take_screenshot')
    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._call_api')
    def test_full_action_loop(self, mock_api, mock_dims, mock_screenshot,
                               mock_execute, mock_sleep, monkeypatch):
        """End-to-end: screenshot -> Qwen3-VL parse+reason -> execute -> done."""
        monkeypatch.setenv('HEVOLVE_VLM_UNIFIED', 'true')

        # Screenshot returns dummy base64
        mock_screenshot.return_value = "dummyscreenshot"

        # First iteration: click Save
        first_response = json.dumps({
            "UI_Elements": [
                {"id": 1, "type": "button", "label": "Save", "bbox": [100, 50, 200, 80]},
            ],
            "Reasoning": "Save button found, clicking it.",
            "Next Action": "left_click",
            "Box ID": 1,
            "coordinate": [150, 65],
            "value": "",
            "Status": "IN_PROGRESS"
        })

        # Second iteration: task is done
        second_response = json.dumps({
            "UI_Elements": [],
            "Reasoning": "File has been saved. Task complete.",
            "Next Action": "None",
            "coordinate": None,
            "value": "",
            "Status": "DONE"
        })

        mock_api.side_effect = [first_response, second_response]
        mock_execute.return_value = {"output": "Clicked at [150, 65]"}

        from integrations.vlm.local_loop import run_local_agentic_loop

        message = {
            "instruction_to_vlm_agent": "Save the file",
            "user_id": "test_user",
            "prompt_id": "test_prompt",
            "max_ETA_in_seconds": 300,
        }

        result = run_local_agentic_loop(message, tier='inprocess', max_iterations=10)

        assert result['status'] == 'success'
        assert len(result['extracted_responses']) == 2

        # First response should be the action
        assert result['extracted_responses'][0]['type'] == 'action'
        assert result['extracted_responses'][0]['content']['action'] == 'left_click'

        # Second response should be completion
        assert result['extracted_responses'][1]['type'] == 'completion'

        # execute_action called once (for the click), not called for DONE
        assert mock_execute.call_count == 1

    @patch('integrations.vlm.local_loop.time.sleep')
    @patch('integrations.vlm.local_computer_tool.execute_action')
    @patch('integrations.vlm.local_computer_tool.take_screenshot')
    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._call_api')
    def test_full_loop_eta_timeout(self, mock_api, mock_dims, mock_screenshot,
                                    mock_execute, mock_sleep, monkeypatch):
        """Loop respects max_ETA_in_seconds timeout."""
        monkeypatch.setenv('HEVOLVE_VLM_UNIFIED', 'true')

        mock_screenshot.return_value = "dummyscreenshot"

        # Always return IN_PROGRESS to force timeout
        ongoing = json.dumps({
            "UI_Elements": [],
            "Reasoning": "Still working...",
            "Next Action": "wait",
            "coordinate": None,
            "value": "",
            "Status": "IN_PROGRESS"
        })
        mock_api.return_value = ongoing
        mock_execute.return_value = {"output": "waited"}

        from integrations.vlm.local_loop import run_local_agentic_loop

        message = {
            "instruction_to_vlm_agent": "Test",
            "user_id": "u1",
            "prompt_id": "p1",
            "max_ETA_in_seconds": 0,  # Immediate timeout
        }

        result = run_local_agentic_loop(message, tier='inprocess', max_iterations=100)
        assert result['status'] == 'success'
        # Should have stopped quickly due to ETA
        assert len(result['extracted_responses']) <= 1

    @patch('integrations.vlm.local_loop.time.sleep')
    @patch('integrations.vlm.local_computer_tool.execute_action')
    @patch('integrations.vlm.local_computer_tool.take_screenshot')
    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._call_api')
    def test_full_loop_error_recovery(self, mock_api, mock_dims, mock_screenshot,
                                       mock_execute, mock_sleep, monkeypatch):
        """Loop continues after an iteration error instead of aborting."""
        monkeypatch.setenv('HEVOLVE_VLM_UNIFIED', 'true')

        # First screenshot fails, second succeeds
        mock_screenshot.side_effect = [
            RuntimeError("Screenshot failed"),
            "dummyscreenshot"
        ]

        done_response = json.dumps({
            "UI_Elements": [],
            "Reasoning": "Task done.",
            "Next Action": "None",
            "Status": "DONE"
        })
        mock_api.return_value = done_response

        from integrations.vlm.local_loop import run_local_agentic_loop

        message = {
            "instruction_to_vlm_agent": "Test",
            "user_id": "u1",
            "prompt_id": "p1",
            "max_ETA_in_seconds": 300,
        }

        result = run_local_agentic_loop(message, tier='inprocess', max_iterations=5)
        assert result['status'] == 'success'
        # Should have error + completion
        assert len(result['extracted_responses']) == 2
        assert result['extracted_responses'][0]['type'] == 'error'
        assert result['extracted_responses'][1]['type'] == 'completion'


# ═══════════════════════════════════════════════════════════════════════════
# Local Computer Tool Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestLocalComputerTool:
    """Basic tests for local_computer_tool actions."""

    def test_supported_actions_set(self):
        """SUPPORTED_ACTIONS contains all expected action types."""
        from integrations.vlm.local_computer_tool import SUPPORTED_ACTIONS

        expected = {
            'key', 'type', 'left_click', 'right_click', 'double_click',
            'hover', 'hotkey', 'wait', 'screenshot', 'mouse_move',
            'list_folders_and_files', 'write_file', 'read_file_and_understand',
        }
        for action in expected:
            assert action in SUPPORTED_ACTIONS, f"Missing action: {action}"

    @patch('integrations.vlm.local_computer_tool.time.sleep')
    def test_wait_action(self, mock_sleep):
        """Wait action sleeps for specified duration."""
        from integrations.vlm.local_computer_tool import _execute_inprocess

        result = _execute_inprocess({'action': 'wait', 'duration': 3})
        assert 'Waited 3s' in result['output']
        mock_sleep.assert_called_once_with(3)

    def test_list_files_action(self, tmp_path):
        """list_folders_and_files action returns directory listing."""
        # Create some test files
        (tmp_path / "file1.txt").write_text("hello")
        (tmp_path / "file2.py").write_text("world")

        from integrations.vlm.local_computer_tool import _execute_inprocess

        result = _execute_inprocess({'action': 'list_folders_and_files', 'path': str(tmp_path)})
        assert 'file1.txt' in result['output']
        assert 'file2.py' in result['output']

    def test_read_file_action(self, tmp_path):
        """read_file_and_understand action reads file content."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello World")

        from integrations.vlm.local_computer_tool import _execute_inprocess

        result = _execute_inprocess({
            'action': 'read_file_and_understand',
            'path': str(test_file)
        })
        assert result['output'] == "Hello World"

    def test_write_file_action(self, tmp_path):
        """write_file action creates file with content."""
        target = tmp_path / "output.txt"

        from integrations.vlm.local_computer_tool import _execute_inprocess

        result = _execute_inprocess({
            'action': 'write_file',
            'path': str(target),
            'content': 'Test content here'
        })
        assert 'Written to' in result['output']
        assert target.read_text() == 'Test content here'

    def test_unknown_action(self):
        """Unknown action type returns error."""
        from integrations.vlm.local_computer_tool import _execute_inprocess

        result = _execute_inprocess({'action': 'teleport'})
        assert 'error' in result
        assert 'Unknown action' in result['error']

    @patch('integrations.vlm.local_computer_tool.pooled_get')
    def test_take_screenshot_http(self, mock_get):
        """HTTP tier screenshot fetches from localhost:5001."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"base64_image": "abc123"}
        mock_get.return_value = mock_resp

        from integrations.vlm.local_computer_tool import take_screenshot
        result = take_screenshot('http')
        assert result == "abc123"

    @patch('integrations.vlm.local_computer_tool.pooled_post')
    def test_execute_action_http(self, mock_post):
        """HTTP tier execute sends POST to localhost:5001/execute."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"output": "Clicked"}
        mock_post.return_value = mock_resp

        from integrations.vlm.local_computer_tool import execute_action
        result = execute_action({"action": "left_click", "coordinate": [100, 200]}, 'http')
        assert result['output'] == 'Clicked'


# ═══════════════════════════════════════════════════════════════════════════
# Local OmniParser Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestLocalOmniParser:
    """local_omniparser.py — HTTP parsing fallback."""

    def test_parse_http_success(self):
        """HTTP parse returns structured result from OmniParser."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "screen_info": "1: button Save",
            "parsed_content_list": [
                {"idx": 1, "type": "button", "content": "Save", "bbox": [100, 50, 200, 80]}
            ],
            "som_image_base64": "labeled_image",
        }

        # pooled_post is lazy-imported inside _parse_http, mock at source
        with patch('core.http_pool.pooled_post', return_value=mock_resp):
            from integrations.vlm.local_omniparser import _parse_http
            result = _parse_http("dummyb64")

        assert result['screen_info'] == "1: button Save"
        assert len(result['parsed_content_list']) == 1
        assert 'latency' in result
        assert 'original_screenshot_base64' in result

    def test_parse_http_fallback(self):
        """HTTP parse falls back gracefully when OmniParser is unavailable."""
        import requests as real_requests
        # Use requests.ConnectionError (subclass of RequestException) so the
        # except (requests.RequestException, ValueError) clause catches it.
        with patch('requests.post', side_effect=real_requests.ConnectionError("refused")):
            from integrations.vlm.local_omniparser import _parse_http
            result = _parse_http("dummyb64")

        assert result['screen_info'] == ''
        assert result['parsed_content_list'] == []
        assert 'latency' in result

    def test_parse_screen_routes_by_tier(self):
        """parse_screen() routes to correct implementation based on tier."""
        from integrations.vlm.local_omniparser import parse_screen

        with patch('integrations.vlm.local_omniparser._parse_http') as mock_http:
            mock_http.return_value = {"screen_info": "", "parsed_content_list": []}
            parse_screen("b64data", 'http')
            mock_http.assert_called_once_with("b64data")


# ═══════════════════════════════════════════════════════════════════════════
# Local Loop Helper Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestLocalLoopHelpers:
    """Helper functions in local_loop.py."""

    def test_parse_vlm_response_json(self):
        """_parse_vlm_response extracts JSON from response text."""
        from integrations.vlm.local_loop import _parse_vlm_response

        text = '{"Next Action": "left_click", "Status": "IN_PROGRESS", "Reasoning": "clicking"}'
        result = _parse_vlm_response(text)
        assert result['Next Action'] == 'left_click'

    def test_parse_vlm_response_code_block(self):
        """_parse_vlm_response handles markdown code blocks."""
        from integrations.vlm.local_loop import _parse_vlm_response

        text = '```json\n{"Next Action": "type", "value": "hello", "Status": "IN_PROGRESS"}\n```'
        result = _parse_vlm_response(text)
        assert result['Next Action'] == 'type'
        assert result['value'] == 'hello'

    def test_parse_vlm_response_malformed_fallback(self):
        """Malformed response falls back to DONE status."""
        from integrations.vlm.local_loop import _parse_vlm_response

        text = "I don't understand what to do next."
        result = _parse_vlm_response(text)
        assert result['Next Action'] == 'None'
        assert result['Status'] == 'DONE'

    def test_build_action_payload_with_coordinate(self):
        """_build_action_payload builds correct payload with explicit coordinate."""
        from integrations.vlm.local_loop import _build_action_payload

        action_json = {
            "Next Action": "left_click",
            "coordinate": [150, 65],
            "value": "",
            "Box ID": None,
        }
        parsed = {"parsed_content_list": []}
        payload = _build_action_payload(action_json, parsed)

        assert payload['action'] == 'left_click'
        assert payload['coordinate'] == [150, 65]

    def test_build_action_payload_resolves_box_id(self):
        """_build_action_payload resolves Box ID to coordinate from parsed screen."""
        from integrations.vlm.local_loop import _build_action_payload

        action_json = {
            "Next Action": "left_click",
            "coordinate": None,
            "value": "",
            "Box ID": 2,
        }
        parsed = {
            "parsed_content_list": [
                {"idx": 1, "type": "button", "content": "Cancel", "bbox": [400, 50, 500, 80]},
                {"idx": 2, "type": "button", "content": "Save", "bbox": [100, 50, 200, 80]},
            ]
        }
        payload = _build_action_payload(action_json, parsed)

        # Center of bbox [100, 50, 200, 80] = (150, 65)
        assert payload['coordinate'] == [150, 65]

    def test_build_action_payload_passes_file_keys(self):
        """_build_action_payload passes through file operation keys."""
        from integrations.vlm.local_loop import _build_action_payload

        action_json = {
            "Next Action": "write_file",
            "coordinate": None,
            "value": "",
            "Box ID": None,
            "path": "/tmp/test.txt",
            "content": "Hello World",
        }
        parsed = {"parsed_content_list": []}
        payload = _build_action_payload(action_json, parsed)

        assert payload['action'] == 'write_file'
        assert payload['path'] == '/tmp/test.txt'
        assert payload['content'] == 'Hello World'

    def test_build_vision_prompt_first_iteration(self):
        """First iteration prompt says 'current screen state'."""
        from integrations.vlm.local_loop import _build_vision_prompt

        content = _build_vision_prompt("1: button Save", "b64data", iteration=0)
        assert any("current screen state" in c.get('text', '') for c in content if isinstance(c, dict) and 'text' in c)

    def test_build_vision_prompt_subsequent_iteration(self):
        """Subsequent iterations prompt says 'updated screen'."""
        from integrations.vlm.local_loop import _build_vision_prompt

        content = _build_vision_prompt("1: button Save", "b64data", iteration=3)
        assert any("updated screen" in c.get('text', '') for c in content if isinstance(c, dict) and 'text' in c)
