"""R14: a tool schema the fit keeps is one the trim keeps and the server takes.

THE MEASURED FAILURE (2026-10-20, installed Nunba on the owner's desktop,
llama-server "n_slots = 1, n_ctx_slot = 12288").  Daemon goal d2f34440, prompt
53298912627 ("HIVE ECONOMICS AGENT"), source autogen.reuse:

  17:07:34  the Assistant (33 tools) calls request_tools("create a detailed
            recipe for the 'Respond to user' action including steps, fallback
            strategy, and Python code if applicable").  discover_and_attach puts
            46 tools on the Helper (2 of them on the Assistant too) and re-fits
            the Assistant only.
  17:07:35  the Helper's next body carries 93 tools.  The wire counts 3,038
            message + 11,438 schema = 14,476 tokens and sends it; llama-server
            counts 14,798 and answers HTTP 400 "request (14798 tokens) exceeds
            the available context size (12288 tokens)".

Two causes, one in each half of the budget:

  * discover_and_attach (core/agent_tool_menu.py) bounded the seat that
    proposes and never the Helper, which every attach widens.  One call took
    the Helper from 47 tools (5,173 schema tokens, inside its room) to 93
    (11,438).
  * The fit and the trim split the slot by two rules.  The fit's room,
    schema_token_room(), was n_ctx less the messages' floor, 12288 - 3072 =
    9216, with nothing for what llama-server adds to the wire's count (322
    tokens on this body, at most 407 over the 377 bodies of that window that
    pair with a server task).  So a Helper fitted to that room, its messages
    trimmed to the floor, is 12,288 by the wire's count and over n_ctx by the
    server's.  And the trim gave the messages n_ctx - max_tokens - margin -
    schema = 7424 - schema, under the floor the fit leaves for any schema
    from 4353 to 7423: 31 Helper bodies in the window were trimmed to message
    budgets of 316 to 2,730 tokens.

THE ONE RULE (core/llm_outbound_logger.py): a schema may take what the slot
leaves beside the messages' floor and the server's own overhead (_schema_room,
which schema_token_room and so the fit read), and the trim never takes the
messages below that floor for a schema (_message_budget).

The Helper here is built from the 93 tool entries of the refused body exactly
as they went on the wire (tests/fixtures/r14_helper_400_tools.json), so every
size below is the measured one.

    python -m pytest tests/unit/test_the_fit_and_the_trim_share_one_budget.py -q -p no:cacheprovider
"""
import importlib
import json
import logging
import os

import pytest

from core.agent_tool_menu import (
    fit_schema_to_ctx,
    helper_tool_names,
    register_request_tools,
)
from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS
from core.token_utils import _get_encoding, count_tokens_for_messages

_FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'fixtures', 'r14_helper_400_tools.json')

#: llama-server's own start line on that box, 2026-10-20 16:08:58:
#: "load_model: initializing, n_slots = 1, n_ctx_slot = 12288".
N_CTX = 12288
MAX_TOKENS = 2048


def _evidence():
    with open(_FIXTURE, encoding='utf-8') as fh:
        return json.load(fh)


@pytest.fixture
def wire():
    """The module as it is now in sys.modules: another test file deletes and
    re-imports it, and a patch on a stale copy patches nothing."""
    return importlib.import_module('core.llm_outbound_logger')


@pytest.fixture
def box(wire, monkeypatch):
    """That box's geometry, pinned: no llama-server is asked for /props."""
    monkeypatch.setattr(wire, '_get_budget_per_slot', lambda: N_CTX)
    return wire


def _tokens(wire, entries):
    return wire._schema_tokens({'tools': list(entries)})


class _Seat:
    """An autogen-shaped seat (llm_config['tools'], register_for_llm /
    register_for_execution, update_tool_signature for removal).  What
    register_for_llm adds is the entry the live body carried for that name, so
    the schema a seat holds costs what it cost on the wire."""

    def __init__(self, name, entries, catalogue):
        self.name = name
        self.llm_config = {'config_list': [{'model': 'test'}],
                           'tools': list(entries)}
        self._function_map = {}
        self._catalogue = catalogue

    def register_for_llm(self, name=None, description=None):
        def _wrap(func):
            entry = self._catalogue.get(name) or {
                'type': 'function',
                'function': {'name': name, 'description': description or name,
                             'parameters': {'type': 'object',
                                            'properties': {}}}}
            self.llm_config['tools'] = [
                e for e in self.llm_config['tools']
                if e['function']['name'] != name] + [entry]
            return func
        return _wrap

    def register_for_execution(self, name=None):
        def _wrap(func):
            self._function_map[name] = func
            return func
        return _wrap

    def update_tool_signature(self, name, is_remove=False):
        assert is_remove, 'a fit may only ever remove'
        self.llm_config['tools'] = [
            e for e in self.llm_config['tools']
            if e['function']['name'] != name]


class _EmptyRegistry:
    _tools = {}


def _noop(**kwargs):
    return 'ok'


def _the_turn_before_the_request(ev):
    """The Helper (its 47 tools) and the Assistant (its 33) as they stood when
    the Assistant called request_tools, and the closures that call attached."""
    catalogue = {e['function']['name']: e for e in ev['tools']}
    attached = ev['attached_by_request_tools']
    helper = _Seat('Helper', [e for e in ev['tools']
                              if e['function']['name'] not in attached],
                   catalogue)
    assistant = _Seat('Assistant',
                      [catalogue[n] for n in ev['assistant_tool_names']],
                      catalogue)
    core = [(n, catalogue[n]['function'].get('description') or n, _noop)
            for n in attached]
    return helper, assistant, core


def _ready(reply):
    """The names the request_tools reply tells the model it can call now."""
    if 'Attached and ready to call NOW: ' not in reply:
        return []
    head = reply.split('Attached and ready to call NOW: ', 1)[1]
    return head.split('. These execute', 1)[0].split(', ')


# ── the measured request ─────────────────────────────────────────────────────

def test_the_fixture_is_the_refused_body(wire):
    ev = _evidence()
    assert len(ev['tools']) == 93
    assert len(ev['attached_by_request_tools']) == 46
    if _get_encoding() is not None:   # the live counter
        assert _tokens(wire, ev['tools']) == ev['wire_schema_tokens'] == 11438


def test_the_measured_request_leaves_a_helper_body_the_server_takes(box):
    """request_tools, as REUSE and CREATE register it, on the turn that 400'd."""
    wire = box
    ev = _evidence()
    helper, assistant, core = _the_turn_before_the_request(ev)
    room = wire.schema_token_room()
    assert _tokens(wire, helper.llm_config['tools']) <= room, (
        'premise: before the request the Helper fitted its room (5,173 tok)')

    request_tools = register_request_tools(helper, assistant, _EmptyRegistry(),
                                           set())
    assistant._hart_core_tools = core     # read at call time, as both legs set it
    reply = request_tools(ev['need'])

    attached = set(ev['attached_by_request_tools'])
    on_seats = helper_tool_names(helper) | helper_tool_names(assistant)
    assert attached <= on_seats, (
        'the request lost the tools it attached: %s' % sorted(attached - on_seats))
    assert set(ev['attached_to_the_assistant_too']) <= helper_tool_names(assistant)

    schema = _tokens(wire, helper.llm_config['tools'])
    added = (ev['server_prompt_tokens'] - ev['wire_message_tokens']
             - ev['wire_schema_tokens'])           # 14798 - 14476 = 322
    server = schema + ev['wire_message_tokens'] + added
    assert schema <= room and server < ev['n_ctx_per_slot'], (
        'after request_tools the Helper carries %d tools, %d schema tokens '
        'against a room of %d; with the body\'s %d message tokens llama-server '
        'would count %d of %d -- the 400 of 2026-10-20 17:07:35'
        % (len(helper.llm_config['tools']), schema, room,
           ev['wire_message_tokens'], server, ev['n_ctx_per_slot']))
    assert set(_ready(reply)) <= on_seats, (
        'the reply offers tools no seat holds: %s'
        % sorted(set(_ready(reply)) - on_seats))


def test_the_reply_names_only_what_a_seat_still_holds(box, monkeypatch):
    """A window too small for everything the request matched: whatever the fit
    takes off every seat is not offered as callable."""
    wire = box
    ev = _evidence()
    helper, assistant, core = _the_turn_before_the_request(ev)
    catalogue = {e['function']['name']: e for e in ev['tools']}
    keep = ['request_tools'] + ev['attached_by_request_tools'][:10]
    room = _tokens(wire, [catalogue[n] for n in keep]) + 8
    monkeypatch.setattr(wire, 'schema_token_room', lambda: room)
    request_tools = register_request_tools(helper, assistant, _EmptyRegistry(),
                                           set())
    assistant._hart_core_tools = core
    reply = request_tools(ev['need'])
    on_seats = helper_tool_names(helper) | helper_tool_names(assistant)
    assert _tokens(wire, helper.llm_config['tools']) <= room
    ready = set(_ready(reply))
    assert ready and ready <= on_seats, sorted(ready - on_seats)
    gone = set(ev['attached_by_request_tools']) - on_seats
    assert gone, 'premise: the window was meant to defer some of the request'
    for name in gone:
        assert name in reply.split('. These execute', 1)[-1], (
            '%s was deferred and the reply does not say so' % name)


# ── one rule, at its boundary ───────────────────────────────────────────────

def _text_costing(tokens):
    """A text that counts exactly ``tokens`` by the wire's counter."""
    from core.token_utils import count_tokens_for_text
    for unit in (' the', ' x'):
        n = max(0, tokens - 1)
        for _ in range(64):
            text = 'pad' + unit * n
            got = count_tokens_for_text(text)
            if got == tokens:
                return text
            n += tokens - got
            if n < 0:
                break
    pytest.fail('cannot build a text of exactly %d tokens' % tokens)


def _entry_costing(wire, tokens, name='pad_tool'):
    """One tool entry whose wire charge is exactly ``tokens``."""
    entry = {'type': 'function',
             'function': {'name': name, 'description': 'pad',
                          'parameters': {'type': 'object', 'properties': {}}}}
    base = _tokens(wire, [entry])
    for unit in (' the', ' x'):
        n = max(0, tokens - base)
        for _ in range(64):
            entry['function']['description'] = 'pad' + unit * n
            got = _tokens(wire, [entry])
            if got == tokens:
                return entry
            n += tokens - got
            if n < 0:
                break
    pytest.fail('cannot build a tool entry of exactly %d tokens' % tokens)


def test_the_room_leaves_the_floor_and_what_the_server_added(box):
    """At the room, with the messages at their floor, the measured body's own
    overhead (322) still fits the slot."""
    wire = box
    ev = _evidence()
    floor = wire._min_message_budget(N_CTX)
    added = (ev['server_prompt_tokens'] - ev['wire_message_tokens']
             - ev['wire_schema_tokens'])
    room = wire.schema_token_room()
    assert room + floor + added < N_CTX, (
        'a schema at the room (%d) beside the messages\' floor (%d) is %d by '
        'the wire\'s count and %d by the server\'s, against n_ctx %d'
        % (room, floor, room + floor, room + floor + added, N_CTX))


def test_the_fit_keeps_exactly_the_room_and_not_one_token_more(box):
    wire = box
    room = wire.schema_token_room()
    at = _Seat('Helper', [_entry_costing(wire, room)], {})
    assert fit_schema_to_ctx(at) == set()
    over = _Seat('Helper', [_entry_costing(wire, room + 1)], {})
    assert fit_schema_to_ctx(over) == {'pad_tool'}


def _errors(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


def test_the_trim_takes_a_body_at_the_room_and_reports_one_token_more(
        box, caplog):
    """The schema at exactly the room and the messages at exactly their floor:
    the trim keeps the body as it is and has nothing to report.  One token
    more of schema and it says so."""
    wire = box
    room = wire.schema_token_room()
    floor = wire._min_message_budget(N_CTX)
    msgs = [{'role': 'user', 'content': _text_costing(floor - 4)}]
    assert count_tokens_for_messages(msgs) == floor
    body = {'model': 'llama', 'max_tokens': MAX_TOKENS, 'messages': msgs,
            'tools': [_entry_costing(wire, room)]}
    with caplog.at_level(logging.ERROR, logger=wire.logger.name):
        _out, dropped, cut, _before, after, budget = wire._trim_to_budget(body)
    assert budget == floor and after == floor and (dropped, cut) == (0, 0)
    assert not _errors(caplog), _errors(caplog)

    caplog.clear()
    over = dict(body, tools=[_entry_costing(wire, room + 1)])
    with caplog.at_level(logging.ERROR, logger=wire.logger.name):
        wire._trim_to_budget(over)
    assert any('TOOL SCHEMA' in m for m in _errors(caplog)), _errors(caplog)


@pytest.mark.parametrize('schema', [4353, 6000, 7423, 'room'])
def test_a_schema_the_fit_keeps_leaves_the_messages_their_floor(box, schema):
    """7424 - schema fell under the floor for every schema from 4353 to 7423
    (n_ctx 12288, max_tokens 2048): the trim kept less than the fit left."""
    wire = box
    room = wire.schema_token_room()
    schema = room if schema == 'room' else schema
    assert schema <= room
    floor = wire._min_message_budget(N_CTX)
    body = {'model': 'llama', 'max_tokens': MAX_TOKENS,
            'messages': [{'role': 'user', 'content': 'hello'}],
            'tools': [_entry_costing(wire, schema)]}
    budget = wire._trim_to_budget(body)[5]
    assert budget >= floor, (
        'a %d-token schema the fit keeps left the messages %d tokens, under '
        'the floor of %d' % (schema, budget, floor))


def test_a_body_without_a_schema_keeps_the_reserves_rule(wire, monkeypatch):
    """Scope: only a schema is held to the floor.  Without one the budget is
    n_ctx - max_tokens - margin exactly as before, even where that is under
    the floor (the wire-trim tests size their windows this way)."""
    monkeypatch.setattr(wire, '_get_budget_per_slot',
                        lambda: 900 + MAX_TOKENS + WIRE_TRIM_SAFETY_MARGIN_TOKENS)
    body = {'model': 'llama', 'max_tokens': MAX_TOKENS,
            'messages': [{'role': 'user', 'content': 'hello'}]}
    assert wire._trim_to_budget(body)[5] == 900
