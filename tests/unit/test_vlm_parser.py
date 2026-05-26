"""Tests for integrations.vlm.parser — Phase 5 of the VLM plan §4.

Covers extract_json (3 fallback paths), parse_vlm_action for each
of the three expected_shape values, and back-compat conversion to
the legacy dict shapes the shimmed parsers must keep returning.
"""
import unittest

from integrations.vlm.parser import (
    extract_json, parse_vlm_action, ParsedAction,
)


class TestExtractJson(unittest.TestCase):
    """Single canonical JSON extractor — was duplicated across
    _parse_vlm_response and _parse_unified_response."""

    def test_code_block_json(self):
        text = '```json\n{"foo": "bar"}\n```'
        self.assertEqual(extract_json(text), {'foo': 'bar'})

    def test_code_block_no_lang_tag(self):
        text = '```\n{"foo": 1}\n```'
        self.assertEqual(extract_json(text), {'foo': 1})

    def test_raw_brace_match(self):
        text = 'Here is the action: {"Next Action": "left_click"}'
        self.assertEqual(extract_json(text),
                         {'Next Action': 'left_click'})

    def test_depth_counted_nested_objects(self):
        """Nested objects the simple raw-brace regex can't match."""
        text = 'before {"outer": {"inner": [{"a": 1}]}, "x": 2} after'
        result = extract_json(text)
        self.assertEqual(result['outer']['inner'][0]['a'], 1)
        self.assertEqual(result['x'], 2)

    def test_empty_returns_none(self):
        self.assertIsNone(extract_json(''))
        self.assertIsNone(extract_json(None))

    def test_unparseable_returns_none(self):
        self.assertIsNone(extract_json('I have no idea what to do'))

    def test_malformed_json_returns_none(self):
        # Looks like JSON but missing comma after first key.
        self.assertIsNone(extract_json('{"a": 1 "b": 2}'))


class TestParseVlmActionJsonShape(unittest.TestCase):
    """expected_shape='action_json' — local_loop's inline branch."""

    def test_well_formed_json_parsed(self):
        raw = '{"Next Action": "left_click", "Status": "IN_PROGRESS", '\
              '"Reasoning": "clicking save", "coordinate": [100, 200]}'
        pa = parse_vlm_action(raw, expected_shape='action_json')
        self.assertEqual(pa.next_action, 'left_click')
        self.assertEqual(pa.action, 'left_click')
        self.assertEqual(pa.status, 'IN_PROGRESS')
        self.assertEqual(pa.reasoning, 'clicking save')
        self.assertEqual(pa.coordinate, [100, 200])
        self.assertFalse(pa.done)

    def test_done_status_sets_done_flag(self):
        raw = '{"Next Action": "None", "Status": "DONE", "Reasoning": "task complete"}'
        pa = parse_vlm_action(raw, expected_shape='action_json')
        self.assertTrue(pa.done)

    def test_box_id_preserved(self):
        raw = '{"Next Action": "left_click", "Status": "IN_PROGRESS", '\
              '"Reasoning": "click button 3", "Box ID": 3}'
        pa = parse_vlm_action(raw, expected_shape='action_json')
        self.assertEqual(pa.box_id, 3)

    def test_value_field_becomes_text(self):
        raw = '{"Next Action": "type", "Status": "IN_PROGRESS", '\
              '"Reasoning": "typing", "value": "hello world"}'
        pa = parse_vlm_action(raw, expected_shape='action_json')
        self.assertEqual(pa.text, 'hello world')

    def test_empty_input_falls_back_to_done(self):
        pa = parse_vlm_action('', expected_shape='action_json')
        self.assertTrue(pa.done)
        self.assertEqual(pa.status, 'DONE')
        self.assertEqual(pa.next_action, 'None')

    def test_unparseable_input_falls_back_to_done(self):
        pa = parse_vlm_action('I dunno', expected_shape='action_json')
        self.assertTrue(pa.done)
        self.assertIn('I dunno', pa.reasoning)

    def test_action_normalized_to_lowercase_underscore(self):
        raw = '{"Next Action": "Left Click", "Status": "IN_PROGRESS", "Reasoning": "x"}'
        pa = parse_vlm_action(raw, expected_shape='action_json')
        self.assertEqual(pa.action, 'left_click')


class TestParseVlmActionSomShape(unittest.TestCase):
    """expected_shape='som_bbox' — parse_and_reason / parse_screen."""

    def test_ui_elements_extracted(self):
        raw = ('{"Next Action": "left_click", "Status": "IN_PROGRESS", '
               '"Reasoning": "x", "UI_Elements": '
               '[{"id": 1, "label": "OK"}], '
               '"parsed_content_list": [{"idx": 1, "type": "button"}]}')
        pa = parse_vlm_action(raw, expected_shape='som_bbox')
        self.assertEqual(len(pa.ui_elements), 1)
        self.assertEqual(pa.ui_elements[0]['label'], 'OK')
        self.assertEqual(pa.parsed_content_list[0]['type'], 'button')

    def test_action_json_shape_does_not_extract_ui_elements(self):
        """action_json shape ignores UI_Elements even if present."""
        raw = '{"Next Action": "x", "Status": "IN_PROGRESS", '\
              '"Reasoning": "x", "UI_Elements": [1, 2, 3]}'
        pa = parse_vlm_action(raw, expected_shape='action_json')
        self.assertEqual(pa.ui_elements, [])

    def test_unparseable_som_falls_back(self):
        pa = parse_vlm_action('not json', expected_shape='som_bbox')
        self.assertTrue(pa.done)
        self.assertEqual(pa.ui_elements, [])


class TestParseVlmActionPointShape(unittest.TestCase):
    """expected_shape='point_only' — point_and_act / taskbar shortcut."""

    def test_done_marker_returns_done_action(self):
        pa = parse_vlm_action('Task complete. DONE.',
                              expected_shape='point_only')
        self.assertEqual(pa.action, 'done')
        self.assertTrue(pa.done)

    def test_type_prefix_extracts_text(self):
        pa = parse_vlm_action('TYPE: hello world',
                              expected_shape='point_only')
        self.assertEqual(pa.action, 'type')
        self.assertEqual(pa.text, 'hello world')

    def test_type_freetext_extracts_text(self):
        pa = parse_vlm_action('I should type "search query"',
                              expected_shape='point_only')
        self.assertEqual(pa.action, 'type')
        self.assertEqual(pa.text, 'search query')

    def test_type_freetext_skipped_when_point_present(self):
        """If both 'type' and <point> are in the text, the point wins
        (the model is grounding, not typing)."""
        pa = parse_vlm_action(
            'Type the text then click <point>500,500</point>',
            expected_shape='point_only',
            screen_w=2560, screen_h=1440,
            detect_action_type=lambda t, r: 'left_click',
        )
        self.assertEqual(pa.action, 'left_click')
        self.assertEqual(pa.norm_x, 500)
        self.assertEqual(pa.norm_y, 500)

    def test_point_marker_extracts_coords(self):
        pa = parse_vlm_action(
            'The button is at <point>500,300</point>',
            expected_shape='point_only',
            screen_w=2560, screen_h=1440,
            detect_action_type=lambda t, r: 'left_click',
        )
        self.assertEqual(pa.action, 'left_click')
        self.assertEqual(pa.norm_x, 500)
        self.assertEqual(pa.norm_y, 300)
        # 500 * 2560 / 1000 = 1280, 300 * 1440 / 1000 = 432
        self.assertEqual(pa.screen_x, 1280)
        self.assertEqual(pa.screen_y, 432)

    def test_number_pair_fallback(self):
        """When no <point>, two numbers in 0-1000 range work."""
        pa = parse_vlm_action(
            '400 600',
            expected_shape='point_only',
            screen_w=2560, screen_h=1440,
            detect_action_type=lambda t, r: 'left_click',
        )
        self.assertEqual(pa.norm_x, 400)
        self.assertEqual(pa.norm_y, 600)

    def test_scroll_down_from_task_keyword(self):
        pa = parse_vlm_action(
            'I should look further',
            task='please scroll down to see more',
            expected_shape='point_only',
            scroll_down_keywords=('scroll down', 'scroll_down'),
        )
        self.assertEqual(pa.action, 'scroll_down')

    def test_unparseable_point_returns_none_action(self):
        pa = parse_vlm_action(
            'I do not understand',
            expected_shape='point_only',
        )
        self.assertEqual(pa.action, 'none')

    def test_unknown_shape_raises(self):
        with self.assertRaises(ValueError):
            parse_vlm_action('foo', expected_shape='nonsense')


class TestParsedActionBackCompat(unittest.TestCase):
    """The two to_*_dict() conversion methods reproduce the historical
    dict shapes legacy callers consume.  This is what makes the shims
    truly drop-in."""

    def test_to_action_json_dict_required_keys(self):
        pa = parse_vlm_action(
            '{"Next Action": "left_click", "Status": "IN_PROGRESS", '
            '"Reasoning": "click here", "coordinate": [10, 20]}',
            expected_shape='action_json')
        d = pa.to_action_json_dict()
        self.assertEqual(d['Next Action'], 'left_click')
        self.assertEqual(d['Status'], 'IN_PROGRESS')
        self.assertEqual(d['Reasoning'], 'click here')
        self.assertEqual(d['coordinate'], [10, 20])

    def test_to_action_json_dict_empty_input(self):
        """Empty input must still produce the {Next Action, Status, Reasoning}
        keys legacy fallback callers depend on."""
        pa = parse_vlm_action('', expected_shape='action_json')
        d = pa.to_action_json_dict()
        self.assertEqual(d['Next Action'], 'None')
        self.assertEqual(d['Status'], 'DONE')
        self.assertIn('Reasoning', d)

    def test_to_point_action_dict_required_keys(self):
        pa = parse_vlm_action(
            'click <point>500,500</point>',
            expected_shape='point_only',
            screen_w=1000, screen_h=1000,
            detect_action_type=lambda t, r: 'left_click',
        )
        d = pa.to_point_action_dict()
        for k in ('action', 'screen_x', 'screen_y', 'text', 'done',
                  'reasoning', 'raw'):
            self.assertIn(k, d)
        self.assertEqual(d['norm_x'], 500)
        self.assertEqual(d['norm_y'], 500)
        self.assertEqual(d['screen_x'], 500)
        self.assertEqual(d['screen_y'], 500)

    def test_to_point_action_dict_done(self):
        pa = parse_vlm_action('DONE', expected_shape='point_only')
        d = pa.to_point_action_dict()
        self.assertEqual(d['action'], 'done')
        self.assertTrue(d['done'])
        # norm_x/norm_y not populated for done — must NOT be in dict
        # (the legacy code returned without setting them).
        self.assertNotIn('norm_x', d)


if __name__ == '__main__':
    unittest.main()
