"""generate_receipt core tool — makeup-artist receipt via accepted template (#752).

The only missing leg of the WhatsApp receipt flow: inbound dispatch and
outbound reply already work (integrations/channels: whatsapp_adapter ->
base._dispatch_message -> registry._route_to_agent -> /chat -> reply out
adapter.send_message).  This tool reuses the existing per-prompt KV store
(save_data_in_memory / get_data_by_key) as the "template they accept" and the
existing TemplateEngine to fill it — no new storage, no new formatter, no new
parallel path.  RED before generate_receipt exists.
"""
import json
import re
import unittest
from unittest.mock import Mock

from core.agent_tools import build_core_tool_closures


def _ctx(agent_data):
    """Minimal complete ctx for build_core_tool_closures (all required keys)."""
    return {
        'user_id': '1',
        'prompt_id': 'testp',
        'agent_data': agent_data,
        'helper_fun': Mock(),  # load_agent_data_from_file is a no-op here
        'user_prompt': 'up',
        'request_id_list': {'up': 'rid'},
        'recent_file_id': None,
        'scheduler': None,
        'send_message_to_user1': lambda *a, **k: None,
        'retrieve_json': lambda s: json.loads(s),
        'strip_json_values': lambda d: d,
        'save_conversation_db': lambda *a, **k: None,
    }


def _find(tools, name):
    for n, _desc, func in tools:
        if n == name:
            return func
    return None


class GenerateReceiptTool(unittest.TestCase):

    def test_registered_as_a_core_tool(self):
        tools = build_core_tool_closures(_ctx({}))
        self.assertIn('generate_receipt', [n for n, _, _ in tools])

    def test_uses_the_artists_accepted_stored_template(self):
        # The artist accepted this template earlier (saved via save_data_in_memory).
        agent_data = {'testp': {'receipt_template': 'Bill for {service}: {currency}{amount}'}}
        gen = _find(build_core_tool_closures(_ctx(agent_data)), 'generate_receipt')
        out = gen(service='Bridal makeup', amount='5000', currency='INR')
        self.assertIn('Bill for Bridal makeup: INR5000', out)

    def test_falls_back_to_a_default_template_when_none_accepted(self):
        gen = _find(build_core_tool_closures(_ctx({})), 'generate_receipt')
        out = gen(service='Facial', amount='1200', client_name='Priya')
        self.assertIn('Facial', out)
        self.assertIn('1200', out)
        self.assertIn('Priya', out)

    def test_date_defaults_to_today_when_omitted(self):
        gen = _find(build_core_tool_closures(_ctx({})), 'generate_receipt')
        out = gen(service='Hair', amount='800')
        self.assertRegex(out, r'\d{4}-\d{2}-\d{2}')

    # ── owner-required fields (cost/date/timing/advance every time) ──

    def test_balance_is_computed_not_trusted(self):
        # "₹18k total, 5k advance" → balance 13,000 appears without the
        # model ever doing arithmetic.
        gen = _find(build_core_tool_closures(_ctx({})), 'generate_receipt')
        out = gen(service='Bridal makeup', amount='18k', advance='5k')
        self.assertIn('13,000', out)

    def test_event_timing_renders(self):
        gen = _find(build_core_tool_closures(_ctx({})), 'generate_receipt')
        out = gen(service='Bridal makeup', amount='5000',
                  event_timing='ready by 6am, event at 11am')
        self.assertIn('ready by 6am, event at 11am', out)

    def test_image_render_appends_media_marker(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest('Pillow not installed')
        import os
        import tempfile
        from unittest.mock import patch as _patch
        tmp = tempfile.mkdtemp(prefix='receipt_gen_')
        gen = _find(build_core_tool_closures(_ctx({})), 'generate_receipt')
        with _patch('integrations.channels.response.receipt_image._receipts_dir',
                    return_value=tmp):
            out = gen(service='Party makeup', amount='8000', advance='2000',
                      event_timing='7pm', render='image')
        m = re.search(r'\[\[MEDIA:(.+?)\]\]', out)
        self.assertIsNotNone(m, f'no media marker in: {out[-200:]}')
        self.assertTrue(os.path.isfile(m.group(1)))


class SetReceiptLogoTool(unittest.TestCase):

    def _tools(self, agent_data):
        return build_core_tool_closures(_ctx(agent_data))

    def test_registered_as_a_core_tool(self):
        self.assertIn('set_receipt_logo',
                      [n for n, _, _ in self._tools({})])

    def test_copies_logo_durably_and_stores_path_in_kv(self):
        import os
        import tempfile
        from unittest.mock import patch as _patch
        tmp_src = tempfile.mkdtemp(prefix='logo_src_')
        tmp_data = tempfile.mkdtemp(prefix='logo_data_')
        src = os.path.join(tmp_src, 'brand.png')
        with open(src, 'wb') as f:
            f.write(b'\x89PNG fake')
        agent_data = {}
        set_logo = _find(self._tools(agent_data), 'set_receipt_logo')
        with _patch('core.platform_paths.get_data_dir',
                    return_value=tmp_data):
            out = set_logo(file_path=src)
        self.assertIn('Logo saved', out)
        stored = agent_data['testp']['receipt_logo_path']
        self.assertTrue(os.path.isfile(stored))
        self.assertTrue(os.path.realpath(stored).startswith(
            os.path.realpath(tmp_data)))

    def test_missing_file_asks_for_resend(self):
        set_logo = _find(self._tools({}), 'set_receipt_logo')
        out = set_logo(file_path=r'C:\nope\logo.png')
        self.assertIn('not found', out)

    def test_non_image_extension_rejected(self):
        import os
        import tempfile
        bad = os.path.join(tempfile.mkdtemp(prefix='logo_bad_'), 'x.exe')
        with open(bad, 'wb') as f:
            f.write(b'MZ')
        set_logo = _find(self._tools({}), 'set_receipt_logo')
        out = set_logo(file_path=bad)
        self.assertIn('not an image', out)


if __name__ == '__main__':
    unittest.main()
