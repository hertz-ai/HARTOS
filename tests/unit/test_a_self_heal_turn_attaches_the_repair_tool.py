"""A self-heal turn attaches the repair tool it names, on both pipelines.

Measured live 13 Nov 2026 (Nunba build 75): a capability-setup consent raised
a self_heal goal for mms_tts whose prompt says "call the repair tool FIRST:
repair_backend_venv(backend_name='mms_tts')".  The agent (28377198566) ran on
CREATE, whose build-time gate resolved goal_tags=[] (18:10:01 IST): that gate
reads the record's stored tags, which only a user-assignable creation hint
writes (amendment A8 c), and it stays as it is.  The action failed with "the
tool 'repair_backend_venv' does not exist" and was retried 148 times.

The door both pipelines open on every turn is
core.agent_tool_menu.attach_for_turn (CREATE's _attach_for_create_turn, REUSE's
get_agent_response).  It resolves the turn's own words, and since 8e944550b
the self-heal prompt resolves to 'backend_repair'.  It then attaches the
ServiceToolRegistry tools whose tags meet the capability tags
goal_manager.get_tool_tags gives that tag.  At 8e944550b the repair tools were
not in the registry and 'backend_repair' unlocked no capability, so the turn
attached 0 tools.

Pinned here with the real attach_for_turn, a real ServiceToolRegistry filled
the way both recipes fill it (BackendRepairTool.register()), and recording
agents:
  * the live self-heal prompt and the live "Execute Action 1" turn attach
    repair_backend_venv and next_tts_venv_to_provision, and the attached
    callable runs BACKEND_REPAIR_TOOLS' own function;
  * a marketing, a plain, a media (text to speech) and a coding turn attach
    neither, and the build-time Tier-1 filter keeps them only for
    'backend_repair', so no other turn pays for their schema;
  * both recipes register the shim beside ExperimentVoteTool (source guard).

Review of ea8e9b3a4 (REJECTED, 14 Nov 2026):
  * this is the ONE attach path.  8e944550b's Tier-2 loader
    (register_backend_repair_tools) put a second copy with a second schema on
    the Helper of a REUSE build whose goal names the tool; it is gone, and
    test_self_heal_gets_backend_repair_tools pins that the Tier-2 block leaves
    them to the registry.  CREATE's own per-turn attach, lifted from
    create_recipe, attaches them on the self-heal turn; REUSE's build keeps
    them through the Tier-1 filter, and its turn runs the same
    attach_for_turn;
  * request_tools' keyword discovery never offers them.  The reviewer's probe
    needs ('text to speech', 'install a package', 'use a model', 'context',
    'set up the next engine') each attached an installer on a turn that never
    resolved 'backend_repair' ('text' is inside 'context.backend' in the
    description).  They are offered by their tag and by their name only;
  * a call without backend_name (or with a name the function does not take)
    is refused by the one refusal every tool call goes through
    (hartos.helper.tool_argument_error, on the patched execute_function),
    not a bare TypeError: the schema marks backend_name required, as the
    function does.  The offered schema types each parameter, and wipe_first
    reaches the function.

Review of 2d48b6fb7 (REJECTED, 14 Nov 2026), the seat that proposes:
  * ONE seat is offered the call: the Helper gets the schema, the seat that
    runs tools (the executor on REUSE, the Assistant on CREATE) gets the
    function, and no other seat gets the schema.  8e944550b's Tier-2 loader
    also put the schema on the ASSISTANT and the function on a separate
    executor; that is RETIRED with the loader, by design: attach_for_tags
    offers a tool's schema to the Helper only, so the model is offered the
    repair call once, from the seat that proposes every tool call;
  * a session that once got the tools adds no second copy when request_tools
    is asked for something else later.
"""
import ast
import json
import os

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
REPAIR_NAMES = {'repair_backend_venv', 'next_tts_venv_to_provision'}

#: The live turn after the first: agent_ledger's action 1 for 28377198566.
LIVE_ACTION_TURN = (
    "Execute Action 1: execute_coding_task: call "
    "repair_backend_venv(backend_name='mms_tts') and record success, message "
    "and log_path from the result")
MARKETING_TURN = 'Run a launch campaign for our new app on social media'
PLAIN_TURN = 'Say good morning to me'
MEDIA_TURN = 'Narrate this story with text to speech and compose a jingle'
CODING_TURN = 'Commit the fix to a branch and open a pull request on github'


def _self_heal_prompt():
    """The prompt the live goal carried (error_advice's self_heal config for
    a tts.probe failure whose context names the backend)."""
    from integrations.agent_engine.goal_manager import _build_self_heal_prompt
    return _build_self_heal_prompt({
        'id': 'g-live-shape',
        'title': 'Self-heal: tts.probe (RuntimeError)',
        'description': 'A high-severity tts.probe failure escaped the '
                       'deterministic recovery loop.',
        'goal_type': 'self_heal',
        'config': {
            'exc_type': 'RuntimeError',
            'source_module': 'tts/tts_engine.py',
            'source_function': 'synthesize',
            'occurrence_count': 1,
            'sample_traceback': 'RuntimeError: worker did not start in 120.0s',
            'category': 'tts.probe',
            'context': {'backend': 'mms_tts'},
        },
    })


class _Agent:
    """Records what register_dual gives a seat: schema names and callables."""

    def __init__(self):
        self.llm, self.exe = {}, {}

    def register_for_llm(self, name=None, description=None, **_kw):
        def deco(func):
            self.llm[name] = func
            return func
        return deco

    def register_for_execution(self, name=None, **_kw):
        def deco(func):
            self.exe[name] = func
            return func
        return deco


@pytest.fixture
def registry(monkeypatch):
    """A fresh ServiceToolRegistry in the global's place, filled with the
    backend repair shim the way both recipes fill the global.  At a HEAD
    without the shim it stays empty, and the attach finds nothing."""
    from integrations.service_tools import registry as reg_mod
    import integrations.service_tools as st_pkg
    fresh = reg_mod.ServiceToolRegistry(config_file=os.devnull)
    monkeypatch.setattr(reg_mod, 'service_tool_registry', fresh)
    monkeypatch.setattr(st_pkg, 'service_tool_registry', fresh)
    from integrations.coding_agent import backend_repair_tools as brt
    shim = getattr(brt, 'BackendRepairTool', None)
    if shim is not None:
        shim.register()
    return fresh


def _turn(message, registry):
    """One turn through the real attach_for_turn, on a pair armed the way
    both builders arm it (arm_turn_attach: nothing attached, no tags, no
    stored tags)."""
    from core.agent_tool_menu import arm_turn_attach, attach_for_turn
    helper, executor = _Agent(), _Agent()
    arm_turn_attach(executor, set(), [], None)
    new_tags, n = attach_for_turn(message, helper, executor, registry)
    return helper, executor, new_tags, n


@pytest.mark.parametrize('message', ['self_heal_prompt', LIVE_ACTION_TURN],
                         ids=['self_heal_prompt', 'live_action_turn'])
def test_the_self_heal_turn_attaches_the_repair_tool(message, registry):
    if message == 'self_heal_prompt':
        message = _self_heal_prompt()
        assert "repair_backend_venv(backend_name='mms_tts')" in message
    helper, executor, new_tags, n = _turn(message, registry)
    assert 'backend_repair' in new_tags
    assert REPAIR_NAMES <= set(helper.llm), (
        f'the turn tells the agent to call repair_backend_venv; it attached '
        f'{n} tool(s): {sorted(helper.llm)}')
    assert REPAIR_NAMES <= set(executor.exe)


def test_the_attached_tool_runs_the_modules_own_function(monkeypatch):
    """The registry entry calls BACKEND_REPAIR_TOOLS' function with the
    model's arguments (an omitted optional one keeps its default, and one
    the model sets reaches the function)."""
    from integrations.service_tools import registry as reg_mod
    from integrations.coding_agent import backend_repair_tools as brt
    calls = []
    entry = next(t for t in brt.BACKEND_REPAIR_TOOLS
                 if t['name'] == 'repair_backend_venv')

    def _recording(backend_name: str, wipe_first: bool = False) -> str:
        calls.append({'backend_name': backend_name, 'wipe_first': wipe_first})
        return '{"success": true}'

    monkeypatch.setitem(entry, 'func', _recording)
    fresh = reg_mod.ServiceToolRegistry(config_file=os.devnull)
    monkeypatch.setattr(reg_mod, 'service_tool_registry', fresh)
    brt.BackendRepairTool.register()
    helper, executor, _tags, _n = _turn(_self_heal_prompt(), fresh)
    out = executor.exe['repair_backend_venv'](backend_name='mms_tts',
                                              wipe_first=None)
    assert json.loads(out) == {'success': True}
    assert calls == [{'backend_name': 'mms_tts', 'wipe_first': False}]
    executor.exe['repair_backend_venv'](backend_name='mms_tts',
                                        wipe_first=True)
    assert calls[-1] == {'backend_name': 'mms_tts', 'wipe_first': True}, (
        'the model asked for a clean reinstall and the function never saw it')


def test_the_offered_schema_types_each_parameter(registry):
    """What the model reads: the signature autogen builds the tool schema
    from types backend_name as a string and wipe_first as a boolean."""
    import inspect
    from typing import Optional
    helper, _executor, _tags, _n = _turn(_self_heal_prompt(), registry)
    params = inspect.signature(helper.llm['repair_backend_venv']).parameters
    assert params['backend_name'].annotation in (str, Optional[str])
    assert params['wipe_first'].annotation in (bool, Optional[bool])
    # backend_name is required on the schema the model reads, as on the
    # function; wipe_first may be left out.
    assert params['backend_name'].default is inspect.Parameter.empty
    assert params['wipe_first'].default is None
    # A function with no parameters is offered an empty object, never the
    # bare **kwargs that llama-server refuses (registry.py, the zero-param
    # endpoint note).
    assert not inspect.signature(
        helper.llm['next_tts_venv_to_provision']).parameters


@pytest.fixture
def patched_executor():
    """autogen's execute_function as every live agent runs it: patched by
    hartos.helper.force_apply_autogen_json_fix, so each call is bound to the
    tool's signature (bind_tool_call_arguments -> tool_argument_error)
    before it runs.  Restored afterwards."""
    from autogen.agentchat.conversable_agent import ConversableAgent
    from hartos.helper import force_apply_autogen_json_fix
    saved = (ConversableAgent.execute_function,
             ConversableAgent.a_execute_function)
    assert force_apply_autogen_json_fix()
    yield ConversableAgent
    (ConversableAgent.execute_function,
     ConversableAgent.a_execute_function) = saved


@pytest.mark.parametrize('arguments, wrong', [
    ('{}', 'Missing required argument(s): backend_name'),
    ('{"backend": "mms_tts"}', 'Unknown argument(s): backend'),
    # Strict JSON null for the required name binds; the handler then drops
    # the None and the model read the bare TypeError (review of c58a17460).
    ('{"backend_name": null}', 'Required argument(s) left empty: backend_name'),
], ids=['no_backend_name', 'a_name_it_does_not_take', 'backend_name_null'])
def test_a_call_it_cannot_run_gets_the_one_refusal(
        arguments, wrong, monkeypatch, patched_executor):
    """A call the function cannot take is refused by the refusal every tool
    call goes through, which says what was wrong and how to call again
    (owner rule), and the function does not run.  The model used to read
    the bare TypeError: the flat schema marked backend_name optional, so the
    bind passed and the call reached the function."""
    from integrations.service_tools import registry as reg_mod
    from integrations.coding_agent import backend_repair_tools as brt
    calls = []
    entry = next(t for t in brt.BACKEND_REPAIR_TOOLS
                 if t['name'] == 'repair_backend_venv')

    def _recording(backend_name: str, wipe_first: bool = False) -> str:
        calls.append(backend_name)
        return '{"success": true}'

    monkeypatch.setitem(entry, 'func', _recording)
    fresh = reg_mod.ServiceToolRegistry(config_file=os.devnull)
    for info in brt.BackendRepairTool.create_tool_infos():
        fresh.register_tool(info)
    helper, _executor, _tags, _n = _turn(_self_heal_prompt(), fresh)
    agent = patched_executor('executor', llm_config=False,
                             human_input_mode='NEVER')
    agent.register_function({'repair_backend_venv': agent._wrap_function(
        helper.llm['repair_backend_venv'])})
    ok, reply = agent.execute_function({'name': 'repair_backend_venv',
                                        'arguments': arguments})
    assert ok is False
    assert calls == [], 'the function ran on a call it cannot take'
    assert wrong in reply['content'], reply
    assert 'backend_name (required)' in reply['content']
    assert 'Call repair_backend_venv again' in reply['content']
    ok, reply = agent.execute_function(
        {'name': 'repair_backend_venv',
         'arguments': '{"backend_name": "mms_tts"}'})
    assert calls == ['mms_tts'], reply     # anti-vacuity: a good call runs
    # Null is refused only for a REQUIRED value: an optional one given null
    # still runs (the review of c58a17460 measured wipe_first: null running).
    ok, reply = agent.execute_function(
        {'name': 'repair_backend_venv',
         'arguments': '{"backend_name": "mms_tts", "wipe_first": null}'})
    assert calls == ['mms_tts', 'mms_tts'], reply


# ── Offered by their tag only (review of ea8e9b3a4, MUST-FIX 2) ─────────
#: The reviewer's probe needs: with the real discover_and_attach each one
#: attached repair_backend_venv or next_tts_venv_to_provision on a turn that
#: never resolved 'backend_repair'.  'set up the next engine' is the second
#: tool's own description ("Which TTS engine to set up next").
DISCOVERY_NEEDS = ['text to speech', 'install a package', 'use a model',
                   'context', 'set up the next engine']


def _discover(need, registry):
    """One request_tools call: the real discover_and_attach on a fresh pair."""
    from core.agent_tool_menu import discover_and_attach
    helper, executor, attached = _Agent(), _Agent(), set()
    reply = discover_and_attach(need, helper, executor, registry, attached)
    return helper, executor, attached, reply


@pytest.mark.parametrize('need', DISCOVERY_NEEDS)
def test_request_tools_never_offers_an_installer(need, registry):
    helper, executor, attached, reply = _discover(need, registry)
    got = REPAIR_NAMES & (set(helper.llm) | set(executor.exe) | attached)
    assert not got, (
        f'request_tools({need!r}) attached {sorted(got)} on a turn that '
        f'never resolved backend_repair: {reply}')


def test_request_tools_still_finds_the_media_tool(registry):
    """Anti-vacuity: the same discovery still attaches what the need asks
    for, so the repair tools stay off it by their own entry, not because
    discovery attached nothing."""
    from integrations.service_tools.registry import ServiceToolInfo
    registry.register_tool(ServiceToolInfo(
        name='pocket_tts', description='offline speech synthesis',
        base_url='native://in-process',
        endpoints={'synthesize': {'path': '/synthesize',
                                  'description': 'Text to speech',
                                  'native_handler': lambda p: 'ok'}},
        health_endpoint=None, tags=['tts', 'speech']))
    helper, _executor, _attached, reply = _discover('text to speech', registry)
    assert 'pocket_tts_synthesize' in helper.llm, reply
    assert not (REPAIR_NAMES & set(helper.llm)), reply


def test_creates_own_turn_attach_gives_the_self_heal_turn_the_tool(registry):
    """CREATE, behaviourally: its per-turn attach, lifted from
    create_recipe.py, on the self-heal prompt attaches both tools on the
    pair create_agents builds (Helper proposes, Assistant executes).  This
    is the turn whose build-time gate resolved goal_tags=[] on build 75."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from core.agent_tool_menu import arm_turn_attach
    with open(os.path.join(REPO, 'hartos', 'create_recipe.py'),
              encoding='utf-8') as f:
        src = f.read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef)
              and n.name == '_attach_for_create_turn')
    ns = {'current_app': SimpleNamespace(logger=MagicMock())}
    exec(ast.get_source_segment(src, fn), ns)
    helper, assistant = _Agent(), _Agent()
    arm_turn_attach(assistant, set(), [], None)
    ns['_attach_for_create_turn'](
        {'helper': helper, 'assistant': assistant}, _self_heal_prompt(), 'u1')
    assert REPAIR_NAMES <= set(helper.llm)
    assert REPAIR_NAMES <= set(assistant.exe)
    # The Helper proposes; the Assistant runs and is not offered the schema
    # (review of 2d48b6fb7: the Tier-2 Assistant-seat schema is retired).
    assert not (REPAIR_NAMES & set(assistant.llm)), sorted(assistant.llm)


def test_only_the_helper_is_offered_the_repair_call(registry):
    """REUSE's turn: the schema on the Helper, the function on the executor,
    no schema on the executor (review of 2d48b6fb7)."""
    helper, executor, _tags, _n = _turn(_self_heal_prompt(), registry)
    assert REPAIR_NAMES <= set(helper.llm)
    assert REPAIR_NAMES <= set(executor.exe)
    assert not (REPAIR_NAMES & set(executor.llm)), sorted(executor.llm)
    assert not (REPAIR_NAMES & set(helper.exe)), sorted(helper.exe)


def test_request_tools_after_a_self_heal_turn_adds_no_second_copy(registry):
    """A session that once got the repair tools asks request_tools for
    something else: the tools it holds are not attached again (the review of
    2d48b6fb7 measured a re-attach on the parent and could not run this on
    the commit)."""
    from core.agent_tool_menu import discover_and_attach
    helper, executor, _tags, _n = _turn(_self_heal_prompt(), registry)
    held_llm = {n: helper.llm[n] for n in REPAIR_NAMES}
    held_exe = {n: executor.exe[n] for n in REPAIR_NAMES}
    attached = set()
    reply = discover_and_attach('text to speech', helper, executor, registry,
                                attached)
    assert not (REPAIR_NAMES & attached), reply
    for n in REPAIR_NAMES:
        assert helper.llm[n] is held_llm[n], f'{n} schema attached again'
        assert executor.exe[n] is held_exe[n], f'{n} function attached again'


def test_a_second_self_heal_turn_on_the_session_adds_no_second_copy(registry):
    """The next self-heal turn on the same session finds the repair tools
    already held (the turn's attach ledger, armed once per session) and
    attaches nothing again."""
    from core.agent_tool_menu import attach_for_turn
    helper, executor, _tags, _n = _turn(_self_heal_prompt(), registry)
    held_llm = {n: helper.llm[n] for n in REPAIR_NAMES}
    held_exe = {n: executor.exe[n] for n in REPAIR_NAMES}
    attach_for_turn(_self_heal_prompt(), helper, executor, registry)
    for n in REPAIR_NAMES:
        assert helper.llm[n] is held_llm[n], f'{n} schema attached again'
        assert executor.exe[n] is held_exe[n], f'{n} function attached again'


@pytest.mark.parametrize('message', [
    MARKETING_TURN, PLAIN_TURN, MEDIA_TURN, CODING_TURN])
def test_other_turns_attach_no_repair_tool(message, registry):
    helper, executor, _new_tags, _n = _turn(message, registry)
    attached = set(helper.llm) | set(executor.exe)
    assert not (REPAIR_NAMES & attached), (
        f'{message!r} attached {sorted(REPAIR_NAMES & attached)}')


def test_the_media_turn_still_attaches_its_own_family(registry):
    """Anti-vacuity for the media case: a registry tool tagged 'tts' does
    attach on that turn, so the repair tools stay off it by their own tags,
    not because the attach did nothing."""
    from integrations.service_tools.registry import ServiceToolInfo
    registry.register_tool(ServiceToolInfo(
        name='pocket_tts', description='offline speech synthesis',
        base_url='native://in-process',
        endpoints={'synthesize': {'path': '/synthesize',
                                  'description': 'Text to speech',
                                  'native_handler': lambda p: 'ok'}},
        health_endpoint=None, tags=['tts', 'speech']))
    helper, _executor, new_tags, _n = _turn(MEDIA_TURN, registry)
    assert 'media' in new_tags
    assert 'pocket_tts_synthesize' in helper.llm
    assert not (REPAIR_NAMES & set(helper.llm))


@pytest.mark.parametrize('tags, kept', [
    (['backend_repair'], True),
    (['marketing'], False), (['media'], False), (['coding'], False),
    ([], False),
])
def test_the_build_time_filter_keeps_them_only_for_backend_repair(
        tags, kept, registry):
    from core.agent_tools import filter_service_tools
    svc = filter_service_tools(tags, registry.get_all_tool_functions(),
                               registry.get_tool_definitions(), registry)
    assert (REPAIR_NAMES <= set(svc)) is kept
    if not kept:
        assert not (REPAIR_NAMES & set(svc))


def _native_register_calls(leg):
    """Names X with X.register() in the try block that registers
    ExperimentVoteTool in ``leg``."""
    with open(os.path.join(REPO, leg), encoding='utf-8') as f:
        tree = ast.parse(f.read())

    def _registers(node):
        return {sub.func.value.id for sub in ast.walk(node)
                if isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == 'register'
                and isinstance(sub.func.value, ast.Name)}

    blocks = [n for n in ast.walk(tree) if isinstance(n, ast.Try)
              and any('ExperimentVoteTool' in _registers(b) for b in n.body)]
    assert len(blocks) == 1, f'{leg}: expected one service-tools try block'
    return set().union(*(_registers(b) for b in blocks[0].body))


@pytest.mark.parametrize('leg', ['hartos/create_recipe.py',
                                 'hartos/reuse_recipe.py'])
def test_source_guard_both_recipes_register_the_shim(leg):
    """Source guard: the recipe constructors cannot be driven in a unit test,
    so this pins that each registers the shim in the same block as the other
    native tools, before the registry is read."""
    assert 'BackendRepairTool' in _native_register_calls(leg)
