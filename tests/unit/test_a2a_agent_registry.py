"""Behavioural tests for integrations.google_a2a.a2a_agent_registry.

register_all_agents() is the A2A discovery wiring: it publishes HART's four
agents (assistant, helper, executor, verify) to the Google A2A server so external
agents can discover and call them. If it silently registered nothing — or raised
and aborted boot — A2A discovery would break invisibly. These tests pin the three
paths: server present -> all four registered with their ids/skills/executors;
server absent -> skipped, no crash; a registration error -> swallowed, not
propagated (the try/except the function wraps everything in).

get_a2a_server is mocked, so no real A2A server / network is needed.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import integrations.google_a2a.a2a_agent_registry as reg

MODEXPECTED_IDS = {'assistant', 'helper', 'executor', 'verify'}


def test_all_four_agents_registered_when_server_present():
    server = MagicMock()
    with patch.object(reg, 'get_a2a_server', return_value=server):
        reg.register_all_agents()
    assert server.register_agent.call_count == 4
    ids = {c.kwargs['agent_id'] for c in server.register_agent.call_args_list}
    assert ids == MODEXPECTED_IDS


def test_each_registration_carries_skills_and_executor():
    server = MagicMock()
    with patch.object(reg, 'get_a2a_server', return_value=server):
        reg.register_all_agents()
    for c in server.register_agent.call_args_list:
        kw = c.kwargs
        assert kw['name'] and kw['description'], kw['agent_id']
        assert kw['skills'], f"{kw['agent_id']} registered with no skills"
        assert callable(kw['executor_func']), kw['agent_id']
        # capabilities is the async/streaming contract the A2A card advertises.
        assert 'async' in kw['capabilities']


def test_no_server_is_skipped_not_fatal():
    # get_a2a_server() returns None before the server is initialised — must be a
    # clean skip, never an AttributeError on None.register_agent.
    with patch.object(reg, 'get_a2a_server', return_value=None):
        reg.register_all_agents()  # must not raise


def test_registration_error_is_swallowed():
    # A2A server present but register_agent raises: the broad guard must catch it
    # so a discovery hiccup cannot abort the caller's boot.
    server = MagicMock()
    server.register_agent.side_effect = RuntimeError("a2a down")
    with patch.object(reg, 'get_a2a_server', return_value=server):
        reg.register_all_agents()  # must not raise


def test_server_getter_raising_is_swallowed():
    # Even get_a2a_server() itself blowing up must not propagate.
    with patch.object(reg, 'get_a2a_server', side_effect=RuntimeError("boom")):
        reg.register_all_agents()  # must not raise
