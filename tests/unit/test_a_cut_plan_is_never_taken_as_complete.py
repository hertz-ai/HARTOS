"""A plan the writer's model cut off is never taken as a complete plan.

RED TESTS, design under peer review (hartos-17).  Each test that pins the
fix is marked xfail(strict=True): main stays green, and the writer-cap commit
flips them (a strict xfail that passes fails the run, so the marks must go
with that commit).  The guards are unmarked and pass today.

THE DEFECT (read 2026-11-24 at HEAD; evidence D:/claude_scratch/66e67cad/
speech_merge/route, the merged speech agent: 10 flows, 51 actions):
  * The plan writer's llm_config (hartos/gather_agentdetails.py,
    create_agents_for_user) sets no max_tokens: it keeps only
    get_llm_config()['config_list'] and drops the 1500 beside it, and
    core/llm_outbound_logger._trim_to_budget then pins every body that
    names none to 2048.
  * The writer's plan for that spec needs about 2,150 to 2,300 tokens, so the
    reply is cut at 2048 with finish_reason 'length'.
  * The cut reply is silently repaired into valid JSON
    (hart_intelligence_entry._parse_plan_reply -> hartos.helper.retrieve_json
    -> json_repair), so a plan missing whole flows is reviewed, approved and
    saved as complete.  The stop reason is never read: the wire logs a
    finish_reason only inside a tool call's record (_response_tool_calls).

THE RULE THESE PIN (the coordinator's design, item 2):
  * A writer reply that ended 'length' is not saved.  The writer is asked
    once more, with the existing wrap-up instruction (_WRAP_UP_PROMPT), the
    one-retry shape of #205 and 7112a12f7.
  * A second 'length' tells the person plainly that the plan was too long,
    and that nothing was saved.
  * A reply that ended 'stop' is saved as today (guard).
  * The refusal is plan parsing's only: helper.retrieve_json and
    _parse_plan_reply's repair stay as they are for every other caller
    (guard).
  * The stop reason travels from the wire to the gather parser PER CALL,
    keyed by the turn's request id, never in one module-level slot.

HOW: everything real down to the HTTP transport.  The real writer
(create_agents_for_user, autogen, the openai SDK, httpx) and the real
core.llm_outbound_logger hook send every chat/completions POST to
httpx.HTTPTransport.handle_request, which ``Wire`` answers with a scripted
completion and finish_reason.  The autonomous creation is the real
_autonomous_gather_info, with the plan reviewer's one model call (pooled_post
to GPT_API on a desktop turn) scripted.  The person's creation is the real
chat() through the gather-mirror harness (test_gather_turn_mirrors_user_side
._drive), its gather_info the real one on the real writer.

The cut is the investigator's: the compact plan of the merged spec, as the
writer writes it, cut at 62% of its text (mid-flow).

    python -m pytest tests/unit/test_a_cut_plan_is_never_taken_as_complete.py -q
"""
import contextlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

import pytest  # noqa: E402

pytest.importorskip('autogen', reason='autogen not installed')
httpx = pytest.importorskip('httpx')

#: The defect every marked test pins; the writer-cap commit removes the marks.
CUT_PLAN = ("a writer reply cut at finish_reason 'length' is repaired and "
            "taken as a complete plan; fixed by the writer-cap commit")

#: The merged speech agent's compact CREATE input: 10 flows, 51 actions,
#: people as blanks (speech_merge/merged_spec.json create_input_text).
SPEC = (Path(__file__).resolve().parents[1] / 'fixtures'
        / 'speech_merge_create_input.txt').read_text(encoding='utf-8')
SLOT = 12288                      # the desktop's n_ctx per slot (Qwen3.5-4B)
LOCAL = 'http://127.0.0.1:8080/v1'
CUT_AT = 0.62                     # where the investigator cut the plan

USER = 'cut_auto_u'
PID = 1794000000101
APPROVED = '{"status": "approved"}'


def speech_plan(status, n_flows=None):
    """The plan the writer writes from SPEC, in its own shape: each numbered
    step one action, the rules paragraph as extra_information (the parse of
    speech_merge/route/measure.py).  ``n_flows`` keeps only the first n."""
    paras = [p for p in SPEC.split('\n\n') if p.strip()]
    rules = next(p for p in paras if p.startswith('Rules for every flow'))
    flows = []
    for p in paras:
        m = re.match(r'Flow (\d+) (.*?) \(persona (\w+)\)\. Sub_goal: (.*?) '
                     r'Steps: (.*)$', p, re.S)
        if not m:
            continue
        steps = [s.strip() for s in re.split(r'\s?\d+\) ', m.group(5)) if s.strip()]
        flows.append({'flow_name': f'Flow {m.group(1)} {m.group(2)}',
                      'persona': m.group(3), 'actions': steps,
                      'sub_goal': m.group(4)})
    assert len(flows) == 10 and sum(len(f['actions']) for f in flows) == 51
    plan = {'status': status, 'name': 'Speech Therapy Agent',
            'agent_name': 'speech.local.name', 'goal': paras[0],
            'broadcast_agent': False,
            'personas': [{'name': n, 'description': f'the {n.lower()}'}
                         for n in ('Child', 'Parent', 'Therapist')],
            'flows': flows[:n_flows] if n_flows else flows,
            'extra_information': rules}
    if status == 'proposed_plan':
        plan['review_required'] = True
    return plan


def cut(text, at=CUT_AT):
    """``text`` as a reply stopped at max_tokens leaves it."""
    return text[:int(len(text) * at)]


def started_flows(text):
    return text.count('"flow_name"')


def flows_of(cfg):
    return len((cfg or {}).get('flows') or [])


# --- the wire ----------------------------------------------------------------

def _pristine(send):
    """httpx's own send under any installed hook (the same unwrap as
    test_a_reply_with_no_completion_is_a_failed_call)."""
    while getattr(send, '__name__', '') in ('_patched_send', '_patched'):
        cells = dict(zip(send.__code__.co_freevars, send.__closure__ or ()))
        send = (cells.get('_orig_send') or cells['_orig']).cell_contents
    return send


def seat_of(body):
    """The autogen seat that sent ``body``: the name on its first user turn
    (the writer of user U is talked to by 'user_proxy_U')."""
    return next((m.get('name') for m in body.get('messages') or []
                 if m.get('role') == 'user'), None)


class Wire:
    """The local llama-server at its HTTP transport.  ``sent`` holds every
    chat/completions body exactly as the real stack sent it (after the
    hook's trim and max_tokens pin); ``script(seat, *answers)`` sets that
    seat's (content, finish_reason) answers in order, the last repeating."""

    def __init__(self):
        self.sent = []
        self.scripts = {}
        self.lock = threading.Lock()

    def script(self, seat, *answers):
        self.scripts[seat] = list(answers)

    def bodies(self, seat):
        return [b for b in self.sent if seat_of(b) == seat]

    def asks(self, seat):
        """What each of ``seat``'s writer calls was asked: its last message."""
        return [b['messages'][-1].get('content') for b in self.bodies(seat)]

    def handle(self, request):
        body = json.loads(bytes(request.content).decode('utf-8'))
        seat = seat_of(body)
        with self.lock:
            self.sent.append(body)
            n = len(self.bodies(seat))
            answers = self.scripts.get(seat)
        assert answers, f'no answer scripted for {seat}'
        content, finish = answers[min(n, len(answers)) - 1]
        out = int(body.get('max_tokens') or 0) if finish == 'length' else 900
        return httpx.Response(200, json={
            'id': f'cmpl-{n}', 'object': 'chat.completion',
            'created': int(time.time()), 'model': body.get('model') or 'local',
            'choices': [{'index': 0, 'finish_reason': finish,
                         'message': {'role': 'assistant', 'content': content}}],
            'usage': {'prompt_tokens': 5700, 'completion_tokens': out,
                      'total_tokens': 5700 + out}}, request=request)


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """The node's own model is the local llama-server at LOCAL, one slot of
    SLOT tokens; the real hook is on httpx and the transport is ``Wire``."""
    import core.llm_outbound_logger as hook
    import core.platform_paths as platform_paths
    import core.port_registry as port_registry
    from hartos.threadlocal import thread_local_data
    monkeypatch.setenv('HEVOLVE_LLAMA_CTX_SIZE', str(SLOT))
    monkeypatch.setenv('HEVOLVE_LLAMA_SLOTS', '1')
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    for var in ('HEVOLVE_LLM_ENDPOINT_URL', 'HEVOLVE_LLM_API_KEY',
                'HEVOLVE_LLM_MODEL_NAME', 'HEVOLVE_ACTIVE_CLOUD_PROVIDER',
                'HEVOLVE_LLM_OUTBOUND_DISABLE'):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / 'logs').mkdir(exist_ok=True)
    (tmp_path / 'data').mkdir(exist_ok=True)
    monkeypatch.setenv('NUNBA_LOG_DIR', str(tmp_path / 'logs'))
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path / 'data'),
                        raising=False)
    monkeypatch.setattr(port_registry, 'get_local_llm_url', lambda *a, **k: LOCAL)
    monkeypatch.setattr(httpx.Client, 'send', _pristine(httpx.Client.send))
    hook._install_sync_patch(httpx)
    monkeypatch.setattr(hook, '_target_ports_cache', ({8080}, time.time() + 3600))
    w = Wire()
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request',
                        lambda self, request: w.handle(request))
    hook._close_handle()
    thread_local_data.clear_model_config_override()
    # retrieve_json's last fallback (fix_json) asks a model; never here.
    import hartos.helper as helper
    w.fix_json_calls = []
    monkeypatch.setattr(helper, '_local_llm_complete',
                        lambda *a, **k: w.fix_json_calls.append(a))
    # The writer pushes a thinking line to the UI; not here.
    monkeypatch.setitem(sys.modules, 'hartos.create_recipe',
                        SimpleNamespace(_push_thinking=lambda *a, **k: None))
    yield w
    hook._close_handle()


@contextlib.contextmanager
def turn(request_id):
    """A /chat turn's request id, bound both ways a turn binds it: the
    thread-local chat() sets and the contextvar with_llm_context sets."""
    from core.llm_outbound_logger import request_id_context
    from hartos.threadlocal import thread_local_data
    before = thread_local_data.get_request_id()
    thread_local_data.set_request_id(request_id=request_id)
    try:
        with request_id_context(request_id):
            yield
    finally:
        thread_local_data.set_request_id(request_id=before)


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


@pytest.fixture
def auto(hie, wire, tmp_path, monkeypatch):
    """The real _autonomous_gather_info on the real writer.  The plan
    reviewer's model call is the one other boundary: ``reviews`` keeps each
    request it was sent, ``verdicts`` are its answers (approved when empty)."""
    from hartos import gather_agentdetails as gad
    state = SimpleNamespace(wire=wire, reviews=[], verdicts=[],
                            seat=f'user_proxy_{USER}',
                            saved_path=tmp_path / f'{PID}.json')
    monkeypatch.setattr(hie, 'PROMPTS_DIR', str(tmp_path))

    def pooled_post(url, json=None, **kw):
        if url == hie.GPT_API:
            state.reviews.append(json)
            text = state.verdicts.pop(0) if state.verdicts else APPROVED
            return SimpleNamespace(status_code=200, json=lambda: {
                'choices': [{'message': {'content': text}}]})
        return MagicMock(name='pooled_post')   # the cloud prompt-list sync
    monkeypatch.setattr(hie, 'pooled_post', pooled_post)
    from integrations.agent_engine import person_ask
    monkeypatch.setattr(person_ask, 'ask', lambda *a, **k: {
        'ok': True, 'ask_id': 'ask-1', 'refused': None, 'card': True})
    monkeypatch.setattr(person_ask, 'ask_state',
                        lambda ask_id, user_id: {'status': 'open'})
    monkeypatch.setattr(person_ask, 'person_to_ask',
                        lambda user_id, request_id=None: str(user_id))
    gad.user_agents.pop(f'{USER}_{PID}', None)

    def call(description=SPEC, rid='rq-cut-auto-1'):
        with hie.app.app_context(), turn(rid):
            return hie._autonomous_gather_info(USER, description, PID, request_id=rid)
    state.call = call
    state.saved = lambda: (json.loads(state.saved_path.read_text(encoding='utf-8'))
                           if state.saved_path.exists() else None)
    yield state
    gad.user_agents.pop(f'{USER}_{PID}', None)


def _says_too_long_and_nothing_saved(text):
    low = str(text).lower()
    return 'too long' in low and bool(re.search(r'nothing\b.*\bsaved|not saved', low))


# --- the autonomous creation (_autonomous_gather_info) -----------------------

@pytest.mark.xfail(strict=True, reason=CUT_PLAN)
def test_a_cut_plan_is_asked_for_once_more_and_the_whole_plan_is_saved(auto, hie):
    """The writer's first reply stops at max_tokens mid-flow; asked once
    more with the wrap-up, it answers the whole plan.  The 10-flow plan is
    saved, never the cut one."""
    whole = json.dumps(speech_plan('proposed_plan'))
    auto.wire.script(auto.seat, (cut(whole), 'length'), (whole, 'stop'))
    reply = auto.call()
    saved = auto.saved()
    assert saved is not None and flows_of(saved) == 10, (
        f'saved {flows_of(saved)} flows of the 10 the task names (the cut '
        f'reply started {started_flows(cut(whole))}): reply {str(reply)[:120]!r}')
    asks = auto.wire.asks(auto.seat)
    assert len(asks) == 2, f'the writer was asked {len(asks)} time(s), not twice'
    assert hie._WRAP_UP_PROMPT in str(asks[1]), asks[1][:200]
    assert auto.wire.fix_json_calls == []


@pytest.mark.xfail(strict=True, reason=CUT_PLAN)
def test_a_plan_cut_twice_saves_nothing_and_says_it_was_too_long(auto):
    whole = json.dumps(speech_plan('proposed_plan'))
    auto.wire.script(auto.seat, (cut(whole), 'length'), (cut(whole, 0.7), 'length'))
    reply = auto.call()
    assert not auto.saved_path.exists(), (
        f'a cut plan of {flows_of(auto.saved())} flows was saved as the agent '
        f'(the task names 10): reply {str(reply)[:120]!r}')
    assert len(auto.wire.asks(auto.seat)) == 2, 'the writer was not asked exactly once more'
    assert 'Agent details gathered' not in str(reply), reply
    assert _says_too_long_and_nothing_saved(reply), reply


def test_a_whole_plan_is_saved_as_it_came(auto):
    """Guard: a reply that ended 'stop' is reviewed and saved unchanged, on
    one writer call (passes today)."""
    plan = speech_plan('proposed_plan')
    auto.wire.script(auto.seat, (json.dumps(plan), 'stop'))
    reply = auto.call()
    saved = auto.saved()
    assert saved is not None, reply
    assert saved['flows'] == plan['flows']
    assert len(auto.wire.asks(auto.seat)) == 1
    assert len(auto.reviews) == 1


# --- a person's creation (chat(), the interview's gather) --------------------

@pytest.fixture
def interview(wire, tmp_path, monkeypatch):
    """One gather turn of a person's creation through the real chat() (the
    gather-mirror harness), its gather_info the real one on the real
    interactive writer, on the wire above.  chat() is given the real
    hartos.helper.retrieve_json (the harness's json.loads stand-in would
    refuse the cut reply that production repairs) and the real thread-local,
    so the turn's request id is the one the writer's call carries."""
    from tests.unit import test_gather_turn_mirrors_user_side as H
    from hartos import gather_agentdetails as gad
    from hartos.helper import retrieve_json
    from hartos.threadlocal import thread_local_data
    real = dict(sys.modules)
    key = f'{H.USER_ID}_{H.PROMPT_ID}'
    gad.user_agents.pop(key, None)

    class Live(list):
        """The harness's gather replies: each is the real writer's answer to
        the message just sent, run on the real modules (the harness stubs
        some, core.constants among them, while chat() runs)."""

        def __init__(self, history):
            super().__init__()
            self.history = history

        def __len__(self):
            return 10 ** 6

        def __getitem__(self, i):
            stubbed = {k: v for k, v in sys.modules.items()
                       if k in real and v is not real[k]}
            sys.modules.update({k: real[k] for k in stubbed})
            try:
                return gad.gather_info(H.USER_ID, self.history[-1], H.PROMPT_ID)
            finally:
                sys.modules.update(stubbed)

    def go(text, turn_before=4):
        # On a thread of its own, as a /chat request runs: what chat() sets
        # on the real thread-local ends with it.
        history, result = [], {}

        def _turn():
            try:
                with turn('rq-gather-1'):
                    result['out'] = H._drive(
                        tmp_path, Live(history), turn_before=turn_before,
                        user_text=text, writer_history=history,
                        extra_ns={'retrieve_json': retrieve_json,
                                  'thread_local_data': thread_local_data,
                                  '_gather_failures': {}})
            except BaseException as e:  # re-raised on the test's thread
                result['error'] = e
        worker = threading.Thread(target=_turn)
        worker.start()
        worker.join(300)
        if 'error' in result:
            raise result['error']
        payload, _persist, _memory, sent = result['out']
        path = tmp_path / f'{H.PROMPT_ID}.json'
        saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        return payload, sent, saved
    state = SimpleNamespace(go=go, seat=f'user_proxy_{H.USER_ID}', wire=wire)
    yield state
    gad.user_agents.pop(key, None)


TEXT = 'Yes, that is all of it. Build it with all ten flows.'


@pytest.mark.xfail(strict=True, reason=CUT_PLAN)
def test_a_persons_cut_config_is_asked_for_once_more_and_the_whole_one_saved(
        interview, hie):
    whole = json.dumps(speech_plan('completed'))
    interview.wire.script(interview.seat, (cut(whole), 'length'), (whole, 'stop'))
    payload, sent, saved = interview.go(TEXT)
    assert saved is None or flows_of(saved) == 10, (
        f'a cut config of {flows_of(saved)} flows was saved (the task names '
        f'10): {payload.get("Agent_status")} {str(payload.get("response"))[:100]!r}')
    assert len(sent) == 2 and hie._WRAP_UP_PROMPT in sent[1], sent
    assert saved is not None and flows_of(saved) == 10, payload
    assert payload['Agent_status'] == 'Review Mode', payload


@pytest.mark.xfail(strict=True, reason=CUT_PLAN)
def test_a_persons_config_cut_twice_saves_nothing_and_says_it_was_too_long(interview):
    whole = json.dumps(speech_plan('completed'))
    interview.wire.script(interview.seat, (cut(whole), 'length'),
                          (cut(whole, 0.7), 'length'))
    payload, sent, saved = interview.go(TEXT)
    assert saved is None, (
        f'a cut config of {flows_of(saved)} flows was saved (the task names '
        f'10): {payload.get("Agent_status")} {str(payload.get("response"))[:100]!r}')
    assert len(sent) == 2, sent
    assert payload['Agent_status'] == 'Creation Mode', payload
    assert _says_too_long_and_nothing_saved(payload.get('response')), payload


def test_a_persons_whole_config_is_saved_as_it_came(interview):
    """Guard (passes today): one call, 'stop', saved with all ten flows."""
    plan = speech_plan('completed')
    interview.wire.script(interview.seat, (json.dumps(plan), 'stop'))
    payload, sent, saved = interview.go(TEXT)
    assert saved is not None and saved['flows'] == plan['flows'], payload
    assert sent == [TEXT]
    assert payload['Agent_status'] == 'Review Mode', payload


# --- the stop reason is per call ---------------------------------------------

PAR = {'a': ('cut_par_a', 1794000000201), 'b': ('cut_par_b', 1794000000202)}


@pytest.mark.xfail(strict=True, reason=CUT_PLAN)
@pytest.mark.parametrize('first', ['length', 'stop'])
def test_each_turn_reads_the_stop_reason_of_its_own_writer_call(hie, wire, first):
    """Two turns, two writers, their calls and parses interleaved: A's call
    ends 'length', B's 'stop'.  The first caller parses only after the second
    call returned, so one module-level "last stop reason" gives the first
    parse the other turn's: A read as whole when B's 'stop' came last, B
    refused when A's 'length' came last.  Each must read its own."""
    from hartos import gather_agentdetails as gad
    whole = json.dumps(speech_plan('completed'))
    wire.script('user_proxy_cut_par_a', (cut(whole), 'length'))
    wire.script('user_proxy_cut_par_b', (whole, 'stop'))
    order = ['a', 'b'] if first == 'length' else ['b', 'a']
    called_1, called_2, parsed_1 = (threading.Event() for _ in range(3))
    out, errors = {}, []

    def run(side):
        user, pid = PAR[side]
        is_first = side == order[0]
        try:
            with hie.app.app_context(), turn(f'rq-cut-par-{side}'):
                if not is_first:
                    assert called_1.wait(120)
                reply = gad.gather_info(user, 'Write the complete configuration now.', pid)
                (called_1 if is_first else called_2).set()
                assert (called_2 if is_first else parsed_1).wait(120)
                try:
                    out[side] = ('read', flows_of(hie._read_gather_config(reply, pid)))
                except Exception as e:  # the refusal
                    out[side] = ('refused', type(e).__name__)
                if is_first:
                    parsed_1.set()
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(f'{side}: {type(e).__name__}: {e}')
            called_1.set(), called_2.set(), parsed_1.set()

    for side in PAR:
        gad.user_agents.pop(f'{PAR[side][0]}_{PAR[side][1]}', None)
    threads = [threading.Thread(target=run, args=(s,)) for s in order]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(300)
    finally:
        for side in PAR:
            gad.user_agents.pop(f'{PAR[side][0]}_{PAR[side][1]}', None)
    assert not errors, errors
    assert out.get('a', ('?',))[0] == 'refused', (
        f"the turn whose writer call ended 'length' read its cut reply as a "
        f"plan of {out.get('a')} flows (the cut started "
        f"{started_flows(cut(whole))})")
    assert out.get('b') == ('read', 10), (
        f"the turn whose writer call ended 'stop' got {out.get('b')}")


def test_the_json_repair_still_reads_a_cut_reply_for_its_other_callers(auto, hie):
    """Guard (passes today): the refusal is plan parsing's only.  In a turn
    whose writer call ended 'length', the plan reviewer's verdict cut before
    its closing brace is still read (helper.retrieve_json's repair), and so
    is any other cut JSON retrieve_json is handed."""
    from hartos import gather_agentdetails as gad
    from hartos.helper import retrieve_json
    auto.wire.script(auto.seat, (cut(json.dumps(speech_plan('proposed_plan'))), 'length'))
    auto.verdicts.append('{"status": "approved"')
    with hie.app.app_context(), turn('rq-cut-guard'):
        gad.gather_info(USER, SPEC, PID, autonomous=True)
        verdict = hie._review_proposed_plan(speech_plan('proposed_plan'), 3)
        repaired = retrieve_json('{"status": "needs_refinement", "feedback": "add a step"')
    assert len(auto.wire.sent) == 1 and len(auto.reviews) == 1
    assert verdict == ('approved', None)
    assert repaired == {'status': 'needs_refinement', 'feedback': 'add a step'}
