"""
test_qwen3vl_computer_use.py - Tests for unified Qwen3-VL Computer Use backend.

Test tiers:
  1. TestCoordinateNormalization — pure math, no model, always runs
  2. TestVLMResponseParsing — string parsing, no model, always runs
  3. TestScreenshotFixtures — real PNG fixtures, no model, always runs
  4. TestQwen3VLBackendUnit — mocked HTTP, always runs
  5. TestQwen3VLIntegration — requires live Qwen3-VL at localhost:8000

Run all unit tests:
    pytest tests/unit/test_qwen3vl_computer_use.py -v --noconftest -k "not integration"

Run integration tests (requires Qwen3-VL server):
    pytest tests/unit/test_qwen3vl_computer_use.py -v --noconftest -k "integration"
"""

import base64
import io
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

import pytest

# Ensure project root is on path
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIXTURES_DIR = os.path.join(ROOT, 'tests', 'fixtures', 'screenshots')


# ============================================================
# 1. Coordinate Normalization Tests (pure math)
# ============================================================

class TestCoordinateNormalization(unittest.TestCase):
    """Pure math tests for Qwen3-VL [0,1000] → pixel coordinate conversion."""

    def setUp(self):
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        self.backend = Qwen3VLBackend.__new__(Qwen3VLBackend)

    def test_1000_to_pixel_center(self):
        """[500, 500, 600, 600] on 1920x1080 → [960, 540, 1152, 648]."""
        result = self.backend._normalize_bbox([500, 500, 600, 600], 1920, 1080)
        self.assertEqual(result, [960, 540, 1152, 648])

    def test_full_screen_bbox(self):
        """[0, 0, 1000, 1000] → [0, 0, width, height]."""
        result = self.backend._normalize_bbox([0, 0, 1000, 1000], 1920, 1080)
        self.assertEqual(result, [0, 0, 1920, 1080])

    def test_origin_bbox(self):
        """[0, 0, 100, 100] on 1920x1080 → [0, 0, 192, 108]."""
        result = self.backend._normalize_bbox([0, 0, 100, 100], 1920, 1080)
        self.assertEqual(result, [0, 0, 192, 108])

    def test_small_screen(self):
        """Works with small screen sizes too."""
        result = self.backend._normalize_bbox([500, 500, 500, 500], 800, 600)
        self.assertEqual(result, [400, 300, 400, 300])

    def test_center_calculation(self):
        """Center of bbox [100, 200, 300, 400] = [200, 300]."""
        bbox = [100, 200, 300, 400]
        center = [int((bbox[0] + bbox[2]) / 2), int((bbox[1] + bbox[3]) / 2)]
        self.assertEqual(center, [200, 300])

    def test_is_normalized_1000_true(self):
        """Detect normalized coords when image is larger than 1000px."""
        self.assertTrue(self.backend._is_normalized_1000([100, 200, 300, 400], 1920, 1080))

    def test_is_normalized_1000_false_for_pixel_coords(self):
        """Pixel coords on large screen should have values > 1000."""
        self.assertFalse(self.backend._is_normalized_1000([960, 540, 1152, 648], 1920, 1080))

    def test_is_normalized_1000_small_screen(self):
        """On 800x600 screen, values ≤1000 could be pixel coords — should return False."""
        self.assertFalse(self.backend._is_normalized_1000([400, 300, 600, 500], 800, 600))

    def test_is_normalized_empty_bbox(self):
        """Empty or invalid bbox returns False."""
        self.assertFalse(self.backend._is_normalized_1000([], 1920, 1080))
        self.assertFalse(self.backend._is_normalized_1000(None, 1920, 1080))


# ============================================================
# 2. VLM Response Parsing Tests (string parsing)
# ============================================================

class TestVLMResponseParsing(unittest.TestCase):
    """JSON parsing from Qwen3-VL output — no model needed."""

    def setUp(self):
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        self.backend = Qwen3VLBackend.__new__(Qwen3VLBackend)

    def test_parse_clean_json(self):
        """Clean JSON object parses correctly."""
        raw = '{"UI_Elements": [], "Next Action": "None", "Status": "DONE", "Reasoning": "done"}'
        result = self.backend._parse_unified_response(raw)
        self.assertEqual(result['Status'], 'DONE')
        self.assertEqual(result['Next Action'], 'None')

    def test_parse_json_in_markdown_block(self):
        """JSON wrapped in markdown code block."""
        raw = '''Here's the analysis:
```json
{"UI_Elements": [{"id": 1, "type": "button", "label": "OK", "bbox": [100, 50, 200, 80]}], "Next Action": "left_click", "Box ID": 1, "Status": "IN_PROGRESS"}
```'''
        result = self.backend._parse_unified_response(raw)
        self.assertEqual(result['Next Action'], 'left_click')
        self.assertEqual(len(result['UI_Elements']), 1)
        self.assertEqual(result['UI_Elements'][0]['label'], 'OK')

    def test_parse_json_with_surrounding_text(self):
        """JSON with text before/after."""
        raw = 'I see a desktop with: {"UI_Elements": [], "Next Action": "None", "Status": "DONE", "Reasoning": "empty"} That is all.'
        result = self.backend._parse_unified_response(raw)
        self.assertEqual(result['Status'], 'DONE')

    def test_parse_malformed_json_fallback(self):
        """Unparseable text falls back to DONE with text as reasoning."""
        raw = 'This is not JSON at all, just random text about the screen'
        result = self.backend._parse_unified_response(raw)
        self.assertEqual(result['Next Action'], 'None')
        self.assertEqual(result['Status'], 'DONE')
        self.assertIn('random text', result['Reasoning'])

    def test_parse_with_ui_elements_and_action(self):
        """Full response with UI elements, action, and coordinates."""
        raw = json.dumps({
            "UI_Elements": [
                {"id": 1, "type": "button", "label": "Save", "bbox": [100, 50, 200, 80]},
                {"id": 2, "type": "textfield", "label": "filename", "bbox": [210, 50, 400, 80]},
            ],
            "Reasoning": "I see a save dialog. Clicking Save.",
            "Next Action": "left_click",
            "Box ID": 1,
            "coordinate": [150, 65],
            "value": "",
            "Status": "IN_PROGRESS"
        })
        result = self.backend._parse_unified_response(raw)
        self.assertEqual(len(result['UI_Elements']), 2)
        self.assertEqual(result['coordinate'], [150, 65])
        self.assertEqual(result['Box ID'], 1)

    def test_parse_nested_json(self):
        """Deeply nested JSON with UI elements containing nested objects."""
        raw = '{"UI_Elements": [{"id": 1, "type": "menu", "label": "File", "bbox": [0, 0, 50, 25], "children": []}], "Next Action": "left_click", "Box ID": 1, "coordinate": [25, 12], "value": "", "Status": "IN_PROGRESS", "Reasoning": "Opening File menu"}'
        result = self.backend._parse_unified_response(raw)
        self.assertEqual(result['Next Action'], 'left_click')


# ============================================================
# 3. Screenshot Fixture Tests (real PNG files)
# ============================================================

class TestScreenshotFixtures(unittest.TestCase):
    """Tests using real screenshot PNG fixtures."""

    def _get_fixture(self, name):
        path = os.path.join(FIXTURES_DIR, name)
        if not os.path.exists(path):
            self.skipTest(f"Fixture not found: {path}")
        return path

    def test_desktop_current_exists(self):
        """Real desktop screenshot fixture exists."""
        path = self._get_fixture('desktop_current.png')
        self.assertTrue(os.path.getsize(path) > 1000, "Screenshot too small")

    def test_desktop_region_exists(self):
        """Cropped region fixture exists."""
        path = self._get_fixture('desktop_region.png')
        self.assertTrue(os.path.getsize(path) > 1000, "Screenshot too small")

    def test_fixture_loads_as_valid_base64(self):
        """Fixture can be loaded and encoded as base64."""
        path = self._get_fixture('desktop_current.png')
        with open(path, 'rb') as f:
            raw_bytes = f.read()
        b64 = base64.b64encode(raw_bytes).decode('ascii')
        self.assertTrue(len(b64) > 100)
        # Verify round-trip
        decoded = base64.b64decode(b64)
        self.assertEqual(decoded[:8], raw_bytes[:8])  # PNG header

    def test_fixture_dimensions_detected(self):
        """Image dimensions can be extracted from fixture."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        path = self._get_fixture('desktop_current.png')
        with open(path, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('ascii')
        w, h = Qwen3VLBackend._get_image_dimensions(b64)
        self.assertGreater(w, 0)
        self.assertGreater(h, 0)
        self.assertGreaterEqual(w, 800)  # At least 800px wide

    def test_fixture_is_valid_png(self):
        """Fixture has valid PNG header."""
        path = self._get_fixture('desktop_current.png')
        with open(path, 'rb') as f:
            header = f.read(8)
        # PNG magic bytes
        self.assertEqual(header[:4], b'\x89PNG')

    def test_region_fixture_smaller_than_full(self):
        """Cropped region is smaller than full desktop."""
        full_path = self._get_fixture('desktop_current.png')
        region_path = self._get_fixture('desktop_region.png')
        self.assertLess(os.path.getsize(region_path), os.path.getsize(full_path))


# ============================================================
# 4. Qwen3VL Backend Unit Tests (mocked HTTP)
# ============================================================

class TestQwen3VLBackendUnit(unittest.TestCase):
    """Backend tests with mocked HTTP calls — no server needed."""

    def setUp(self):
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        self.backend = Qwen3VLBackend(
            base_url='http://localhost:8000/v1',
            model_name='Qwen3-VL-2B-Instruct'
        )

    def _mock_api_response(self, content):
        """Create a mock requests.post response."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'choices': [{'message': {'content': content}}]
        }
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    def test_parse_screen_returns_expected_format(self, mock_dims):
        """parse_screen() returns OmniParser-compatible dict."""
        api_content = json.dumps({
            "UI_Elements": [
                {"id": 1, "type": "button", "label": "Close", "bbox": [900, 10, 950, 30]},
            ]
        })

        with patch('core.http_pool.pooled_post', return_value=self._mock_api_response(api_content)):
            result = self.backend.parse_screen("fake_base64")

        self.assertIn('screen_info', result)
        self.assertIn('parsed_content_list', result)
        self.assertIn('latency', result)
        self.assertEqual(len(result['parsed_content_list']), 1)
        self.assertEqual(result['parsed_content_list'][0]['content'], 'Close')

    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    def test_describe_scene_returns_text(self, mock_dims):
        """describe_scene() returns a text description."""
        with patch('core.http_pool.pooled_post',
                   return_value=self._mock_api_response("A desktop with a code editor open")):
            result = self.backend.describe_scene("fake_base64")

        self.assertIn('desktop', result.lower())

    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    def test_parse_and_reason_unified(self, mock_dims):
        """parse_and_reason() returns both UI elements and action."""
        api_content = json.dumps({
            "UI_Elements": [
                {"id": 1, "type": "button", "label": "Save", "bbox": [100, 50, 200, 80]},
            ],
            "Reasoning": "Clicking Save button",
            "Next Action": "left_click",
            "Box ID": 1,
            "coordinate": None,
            "value": "",
            "Status": "IN_PROGRESS"
        })

        with patch('core.http_pool.pooled_post', return_value=self._mock_api_response(api_content)):
            result = self.backend.parse_and_reason("fake_base64", "Save the file")

        self.assertIn('screen_info', result)
        self.assertIn('parsed_content_list', result)
        self.assertIn('action_json', result)
        self.assertEqual(result['action_json']['Next Action'], 'left_click')
        # bbox [100, 50, 200, 80] normalized from [0,1000] to 1920x1080:
        # [192, 54, 384, 86] → center [288, 70]
        self.assertEqual(result['action_json']['coordinate'], [288, 70])

    def test_connection_failure_raises(self):
        """API connection failure raises an exception."""
        import requests
        with patch('core.http_pool.pooled_post', side_effect=requests.ConnectionError("refused")):
            with self.assertRaises(requests.ConnectionError):
                self.backend.describe_scene("fake_base64")

    def test_timeout_handling(self):
        """API timeout raises an exception."""
        import requests
        with patch('core.http_pool.pooled_post', side_effect=requests.Timeout("timed out")):
            with self.assertRaises(requests.Timeout):
                self.backend.describe_scene("fake_base64")

    def test_singleton_pattern(self):
        """get_qwen3vl_backend() returns same instance."""
        import integrations.vlm.qwen3vl_backend as mod
        old_instance = mod._instance
        mod._instance = None  # Reset
        try:
            a = mod.get_qwen3vl_backend()
            b = mod.get_qwen3vl_backend()
            self.assertIs(a, b)
        finally:
            mod._instance = old_instance


# ============================================================
# 5. Box ID → Coordinate Resolution Tests
# ============================================================

class TestBoxIDResolution(unittest.TestCase):
    """Test that Box ID is correctly resolved to pixel coordinates."""

    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    def test_box_id_resolves_to_center(self, mock_dims):
        """Box ID 2 with bbox [210, 50, 400, 80] normalized on 1920x1080 → center [585, 70]."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        backend = Qwen3VLBackend(base_url='http://localhost:8000/v1')

        api_content = json.dumps({
            "UI_Elements": [
                {"id": 1, "type": "button", "label": "OK", "bbox": [100, 50, 200, 80]},
                {"id": 2, "type": "textfield", "label": "name", "bbox": [210, 50, 400, 80]},
            ],
            "Next Action": "left_click",
            "Box ID": 2,
            "coordinate": None,
            "value": "",
            "Status": "IN_PROGRESS",
            "Reasoning": "Clicking text field"
        })

        mock_resp = MagicMock()
        mock_resp.json.return_value = {'choices': [{'message': {'content': api_content}}]}
        mock_resp.raise_for_status = MagicMock()

        with patch('core.http_pool.pooled_post', return_value=mock_resp):
            result = backend.parse_and_reason("fake_b64", "Enter name")

        # bbox [210, 50, 400, 80] normalized from [0,1000] to 1920x1080:
        # [403, 54, 768, 86] → center [585, 70]
        self.assertEqual(result['action_json']['coordinate'], [585, 70])

    @patch('integrations.vlm.qwen3vl_backend.Qwen3VLBackend._get_image_dimensions',
           return_value=(1920, 1080))
    def test_explicit_coordinate_overrides_box_id(self, mock_dims):
        """When coordinate is explicitly provided, Box ID resolution is skipped."""
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        backend = Qwen3VLBackend(base_url='http://localhost:8000/v1')

        api_content = json.dumps({
            "UI_Elements": [{"id": 1, "type": "button", "label": "X", "bbox": [0, 0, 50, 50]}],
            "Next Action": "left_click",
            "Box ID": 1,
            "coordinate": [999, 888],
            "value": "",
            "Status": "IN_PROGRESS",
            "Reasoning": "Custom coordinate"
        })

        mock_resp = MagicMock()
        mock_resp.json.return_value = {'choices': [{'message': {'content': api_content}}]}
        mock_resp.raise_for_status = MagicMock()

        with patch('core.http_pool.pooled_post', return_value=mock_resp):
            result = backend.parse_and_reason("fake_b64", "Click")

        # Explicit coordinate should be preserved, not overwritten by Box ID center
        self.assertEqual(result['action_json']['coordinate'], [999, 888])


# ============================================================
# 6. Integration Tests (require live Qwen3-VL server)
# ============================================================

def _qwen3vl_available():
    """Check if Qwen3-VL server is running at localhost:8000."""
    try:
        import requests
        resp = requests.get('http://localhost:8000/health', timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


@pytest.mark.integration
@unittest.skipUnless(_qwen3vl_available(), "Qwen3-VL server not available at localhost:8000")
class TestQwen3VLIntegration(unittest.TestCase):
    """Integration tests that call the real Qwen3-VL server with real screenshots."""

    def setUp(self):
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        self.backend = Qwen3VLBackend()

    def _load_fixture(self, name):
        path = os.path.join(FIXTURES_DIR, name)
        if not os.path.exists(path):
            self.skipTest(f"Fixture not found: {path}")
        with open(path, 'rb') as f:
            return base64.b64encode(f.read()).decode('ascii')

    def test_real_scene_description(self):
        """Send real screenshot to Qwen3-VL, verify it returns a meaningful description."""
        b64 = self._load_fixture('desktop_current.png')
        description = self.backend.describe_scene(b64)
        self.assertIsInstance(description, str)
        self.assertGreater(len(description), 20, "Description too short")

    def test_real_screenshot_parsing(self):
        """Parse real screenshot with Qwen3-VL, verify UI elements returned."""
        b64 = self._load_fixture('desktop_current.png')
        result = self.backend.parse_screen(b64)
        self.assertIn('screen_info', result)
        self.assertIn('parsed_content_list', result)
        self.assertIsInstance(result['parsed_content_list'], list)
        self.assertIn('latency', result)
        self.assertGreater(result['latency'], 0)

    def test_real_action_generation(self):
        """Send screenshot + instruction to Qwen3-VL, verify action JSON."""
        b64 = self._load_fixture('desktop_current.png')
        result = self.backend.parse_and_reason(
            b64, "Describe what you see on the screen, then set status to DONE"
        )
        self.assertIn('action_json', result)
        self.assertIn('Status', result['action_json'])
        self.assertIn(result['action_json']['Status'], ['IN_PROGRESS', 'DONE'])

    def test_real_region_parsing(self):
        """Parse cropped region fixture."""
        b64 = self._load_fixture('desktop_region.png')
        result = self.backend.parse_screen(b64)
        self.assertIn('parsed_content_list', result)
        self.assertIn('width', result)
        self.assertEqual(result['width'], 800)


if __name__ == '__main__':
    unittest.main()
