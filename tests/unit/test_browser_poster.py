"""Behavioural tests for Bridge Phase 1c (#62): generic browser posting.

post_to_platform_via_browser must turn (platform, body) into a VLM browser task
using the SINGLE-SOURCE composer URL from marketing/intents + the SINGLE-SOURCE
driver vlm.local_loop.run_local_agentic_loop — generically, for any platform,
not LinkedIn-only.  We mock the browser-loop BOUNDARY (inject a fake
vlm.local_loop module) and assert the orchestration: right URL resolved, body
threaded into the instruction, loop invoked, status mapped.
"""
from __future__ import annotations

import os
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _inject_fake_loop(monkeypatch, captured, status='success'):
    """Replace integrations.vlm.local_loop with a stub so no heavy VLM deps load
    and we can observe the message the poster builds."""
    vlm_pkg = types.ModuleType('integrations.vlm')
    vlm_pkg.__path__ = []  # mark as package
    loop_mod = types.ModuleType('integrations.vlm.local_loop')

    def run_local_agentic_loop(message, tier='inprocess', max_iterations=None):
        captured['message'] = message
        captured['tier'] = tier
        return {'status': status, 'extracted_responses': ['posted ok']}

    loop_mod.run_local_agentic_loop = run_local_agentic_loop
    monkeypatch.setitem(sys.modules, 'integrations.vlm', vlm_pkg)
    monkeypatch.setitem(sys.modules, 'integrations.vlm.local_loop', loop_mod)


def test_browser_post_resolves_composer_and_drives_loop(monkeypatch):
    captured = {}
    _inject_fake_loop(monkeypatch, captured)
    from integrations.marketing.browser_poster import post_to_platform_via_browser

    res = post_to_platform_via_browser('linkedin', body='hello world', user_id='u1')
    assert res['ok'] is True, res
    assert res['platform'] == 'linkedin' and res['status'] == 'success'

    msg = captured['message']
    instr = msg['instruction_to_vlm_agent']
    assert 'linkedin.com' in instr.lower(), "must open the linkedin composer URL from intents"
    assert 'hello world' in instr, "must thread the body into the browser instruction"
    assert msg['os_to_control'] in ('windows', 'macos', 'linux')
    assert msg['user_id'] == 'u1'


def test_browser_post_unknown_platform_does_not_drive_loop(monkeypatch):
    captured = {}
    _inject_fake_loop(monkeypatch, captured)
    from integrations.marketing.browser_poster import post_to_platform_via_browser
    res = post_to_platform_via_browser('myspace')
    assert res['ok'] is False and 'no canonical intent' in res['error']
    assert 'message' not in captured, "must not open a browser for an unknown platform"


def test_browser_post_whatsapp_routes_to_adapter_not_browser(monkeypatch):
    """whatsapp has no web composer (intent_url='') — must point to its adapter,
    not try to drive a browser."""
    captured = {}
    _inject_fake_loop(monkeypatch, captured)
    from integrations.marketing.browser_poster import post_to_platform_via_browser
    res = post_to_platform_via_browser('whatsapp')
    assert res['ok'] is False and 'adapter' in res['error'].lower()
    assert 'message' not in captured


def test_browser_post_maps_non_success_status(monkeypatch):
    captured = {}
    _inject_fake_loop(monkeypatch, captured, status='max_iterations')
    from integrations.marketing.browser_poster import post_to_platform_via_browser
    res = post_to_platform_via_browser('twitter', body='hi')
    assert res['ok'] is False and res['status'] == 'max_iterations'
    assert 'message' in captured  # it DID attempt (loop ran), just didn't finish
