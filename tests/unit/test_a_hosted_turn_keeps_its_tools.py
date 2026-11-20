"""A turn on a hosted model is not bounded by the local llama-server's window.

MEASURED LIVE on central (build e288d4cd, 2026-11-20 ~04:00Z), goal 5ba50f98
(rice order desk, sales).  Every goal turn ran on Claude Haiku 5.5 (228
Anthropic 200s in 30 minutes), yet the log said::

    tool schema bounded to the live n_ctx: kept ~8614 tok of 8704 available,
    deferred 11 tool(s) -- still executable and re-attachable via
    request_tools: advance_prospect_stage, bulk_import_prospects,
    check_pending_followups, create_followup_sequence, create_prospect,
    get_pipeline_status, move_prospect_stage, post_to_channel_via_browser,
    run_journey_tick, send_prospect_message, view_journey_p...

The sales agent lost its whole CRM family, tried to draft outreach through
execute_coding_task and save_data_in_memory, failed three times, and the goal
paused.  8704 is the LOCAL llama-server's schema room (LLAMA_CTX_SIZE_DEFAULT
12288 less the message floor and the template overhead), read by
fit_schema_to_ctx whatever model the agent calls.  The wire trim that room
belongs to applies only to the local llama-server (_is_target_request); a
hosted provider's call passes through untrimmed.

These tests build the agent's llm_config the way the turn does: central's
turn list is with_local_fallback([resolve_expert_backend()]), the local one is
resolve_llm_backend()'s llama entry.

    python -m pytest tests/unit/test_a_hosted_turn_keeps_its_tools.py -q
"""
import logging
import os
import sys

import pytest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if _HARTOS not in sys.path:
    sys.path.insert(0, _HARTOS)

import core.llm_outbound_logger as wire  # noqa: E402
from core.agent_tool_menu import fit_schema_to_ctx  # noqa: E402

# A slot whose room (8192 - 2048 - 512 = 5632) the 60-tool block overflows.
_SMALL_SLOT = 8192
_ENV_KEYS = ('HEVOLVE_NODE_TIER', 'HEVOLVE_ACTIVE_CLOUD_PROVIDER',
             'HEVOLVE_LLM_ENDPOINT_URL', 'HEVOLVE_LLM_MODEL_NAME',
             'HEVOLVE_LLM_API_KEY', 'HEVOLVE_EXPERT_LLM_ENDPOINT_URL',
             'HEVOLVE_EXPERT_LLM_MODEL_NAME', 'HEVOLVE_EXPERT_LLM_API_KEY')


def _entry(name):
    """A schema entry the size of a live helper-seat tool (~128 tok)."""
    props = {f'arg{i}': {'type': 'string',
                         'description': f'the {i}th argument of {name}, which '
                                        f'the model must supply as plain text'}
             for i in range(2)}
    return {'type': 'function',
            'function': {'name': name,
                         'description': f'{name} performs the {name} operation '
                                        f'on behalf of the calling agent',
                         'parameters': {'type': 'object', 'properties': props,
                                        'required': ['arg0']}}}


class _Seat:
    """Autogen-shaped seat: llm_config with the turn's config_list and tools,
    and the remove API defer_helper_schema goes through."""

    def __init__(self, config_list, n_tools=60):
        self.llm_config = {'config_list': config_list, 'cache_seed': None,
                           'tools': [_entry(f'crm_tool_{i:02d}')
                                     for i in range(n_tools)]}

    def update_tool_signature(self, name, is_remove=False):
        assert is_remove, 'the fit may only ever remove'
        self.llm_config['tools'] = [
            e for e in self.llm_config['tools']
            if (e.get('function') or {}).get('name') != name]


def _names(seat):
    return {(e.get('function') or {}).get('name')
            for e in seat.llm_config['tools']}


@pytest.fixture
def small_llama_slot(monkeypatch):
    monkeypatch.setattr(wire, '_get_budget_per_slot', lambda: _SMALL_SLOT)
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _central_claude_turn(monkeypatch):
    """The config_list central's goal turn carries: the configured expert
    (Claude on Anthropic's compatibility layer, the host core.claude_native
    serves) first, the node's own gateway as with_local_fallback's last
    entry."""
    from core.autogen_config import resolve_expert_backend, with_local_fallback
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    monkeypatch.setenv('HEVOLVE_LLM_ENDPOINT_URL', 'https://gateway.example/llm/v1')
    monkeypatch.setenv('HEVOLVE_LLM_MODEL_NAME', 'openrouter/free')
    monkeypatch.setenv('HEVOLVE_LLM_API_KEY', 'placeholder')
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_ENDPOINT_URL', 'https://api.anthropic.com/v1')
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_MODEL_NAME', 'claude-haiku-5-5')
    turn = with_local_fallback([resolve_expert_backend()])
    assert [e['model'] for e in turn] == ['claude-haiku-5-5', 'openrouter/free']
    return turn


def _local_llama_entry():
    """A flat node's own backend: resolve_llm_backend()'s llama entry."""
    from core.autogen_config import resolve_llm_backend
    kind, entry = resolve_llm_backend()
    assert kind == 'local', entry.get('base_url')
    return entry


def test_a_claude_turn_keeps_all_sixty_tools(small_llama_slot):
    seat = _Seat(_central_claude_turn(small_llama_slot))
    assert wire._schema_tokens({'tools': seat.llm_config['tools']}) \
        > wire.schema_token_room(), 'the fixture must overflow the llama room'
    before = _names(seat)

    deferred = fit_schema_to_ctx(seat)

    assert deferred == set(), (
        'a turn on Claude was bounded by the local llama-server window: '
        'deferred %s' % sorted(deferred))
    assert _names(seat) == before


def test_the_skip_says_which_model_once(small_llama_slot, caplog):
    seat = _Seat(_central_claude_turn(small_llama_slot))
    with caplog.at_level(logging.INFO, logger='tool_execution'):
        fit_schema_to_ctx(seat)
        fit_schema_to_ctx(seat)
    lines = [r.getMessage() for r in caplog.records
             if 'tool schema not bounded' in r.getMessage()]
    assert len(lines) == 1, lines
    assert 'claude-haiku-5-5' in lines[0], lines[0]
    assert 'not the local llama' in lines[0], lines[0]


def test_the_same_tools_on_the_local_llama_are_still_bounded(small_llama_slot):
    seat = _Seat([_local_llama_entry()])
    room = wire.schema_token_room()

    deferred = fit_schema_to_ctx(seat)

    assert deferred, 'a llama-server agent over its room must be bounded'
    assert wire._schema_tokens({'tools': seat.llm_config['tools']}) <= room


def test_a_hosted_turn_that_falls_back_to_the_llama_is_still_bounded(
        small_llama_slot):
    """with_local_fallback ends a flat node's turn list at its own llama, and
    autogen hands that entry the same tools[]: the wire can cut messages but
    never a schema, so an unbounded set would 400 there."""
    seat = _Seat([{'model': 'claude-haiku-5-5', 'api_key': 'placeholder',
                   'base_url': 'https://api.anthropic.com/v1'},
                  _local_llama_entry()])

    assert fit_schema_to_ctx(seat), 'the llama fallback must stay bounded'
