"""integrations.reel_studio.tools: her seven reel_* tools.

register_reel_studio_tools binds her user id, her agent's prompt id and a
getter for the turn's request id into seven closures, so none of them is a
tool argument.  It registers them in the canonical Tier-2 shape
(news_tools.register_news_tools; commerce, finance and revenue follow it)
on her Reel Studio agent only -- an agent whose record stores reel_studio:
both the helper and the assistant carry the LLM schema, so an
Assistant-proposed reel_make is a structured call, not text autogen never
runs (measured live on the news family 2026-09-03); the assistant and, when
given, the executor run them.  Any other agent gets none, so request_tools
can never offer it one (critic report 2.6).  On the central node it
registers nothing (amendment A8 d).

reel_make turns a chat turn into a reel request: this turn's clips (else her
newest unused batch), her style (tool arguments over her agent record's
words over her voice profile), her logo (the newest she uploaded, else the one
her profile names: her profile is read, never written), the song, a caption
and at most five hashtags; then
pipeline.make_reel delivers it.  A reel_make that made no reel answers in the
executor's failure form ("Error: ..."), so neither the CREATE recipe banker,
the REUSE fabrication gate nor the completion gate can count it as the
action's work (core.constants.tool_reply_failed is the one rule all three
read).

Production (the media work) is replaced by a fake here: the end-to-end path
is tests/unit/test_reel_pipeline.py's.  Everything else is real: the store on
a temporary root, her voice profile, igkit, the skills, and
publish_agent_message with publish_async as the recorded boundary.
"""
import hashlib
import json
import logging
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from integrations.reel_studio import contracts as C
from integrations.trends import trending_songs_client as _trending_client

UID = '10077'
PID = '42'
#: Her Reel Studio agent's stored goal tags (her creation hint stamps them).
HER_TAGS = ('reel_studio',)
#: The real trending read, taken before any fixture replaces it (``env``
#: does: most tests here supply their own list).
REAL_TRENDING = _trending_client.get_trending_songs


class _Agent:
    """Records what autogen's register_for_llm / register_for_execution get."""

    def __init__(self):
        self.llm = {}
        self.exe = {}

    def register_for_llm(self, *, name, description):
        def put(func):
            self.llm[name] = (description, func)
            return func
        return put

    def register_for_execution(self, *, name):
        def put(func):
            self.exe[name] = func
            return func
        return put


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tests.conftest import refuse_all_network
    refuse_all_network(monkeypatch)
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('HEVOLVE_DATA_KEY', raising=False)
    import security.secrets_manager as secrets_manager
    monkeypatch.setattr(secrets_manager, 'get_secret', lambda name, default='': default)
    # Her desktop, whatever this box's env says (a test below makes it central).
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    from core import platform_paths
    platform_paths.reset_cache()
    from core import cache_loaders
    prompts = tmp_path / 'prompts'
    prompts.mkdir()
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(cache_loaders, '_agent_config_cache', {})
    from core import user_file_store as ufs
    store = ufs.UserFileStore(str(tmp_path / 'user_files'), disk_free=lambda _p: 1 << 40)
    monkeypatch.setattr(ufs, '_STORE', store)
    from integrations.reel_studio import jobs, pipeline
    monkeypatch.setattr(jobs, '_ACTIVE', set())
    monkeypatch.setattr(jobs, '_SWEPT', True)
    monkeypatch.setattr(pipeline, 'free_bytes', lambda _p: 1 << 40)
    pushes = []

    def publish_async(topic, message, timeout=2.0, skip_crossbar=False):
        pushes.append((topic, message, {'skip_crossbar': skip_crossbar}))
    monkeypatch.setitem(sys.modules, 'hart_intelligence', SimpleNamespace(publish_async=publish_async))
    produced = []

    def produce(sources, *, style, workdir, out_path, **kw):
        produced.append(dict(kw, sources=list(sources), style=dict(style)))
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, 'wb') as f:
            f.write(b'\x00\x00\x00\x18ftypmp42' + os.urandom(1024))
        qa = {'schema': C.QA_SCHEMA, 'passed': True, 'checks': [], 'advisory': []}
        render = C.RenderResult(path=out_path, duration_s=float(style['target_s']),
                                size_bytes=os.path.getsize(out_path), encoder='libx264')
        return pipeline.Production(
            path=out_path, edl={'notes': ['capture_order:phone']},
            moments={'src': 'vlm', 'final_look_found': True, 'notes': []}, qa=qa, render=render,
            beats_source='local_audio' if kw.get('song_audio') else 'synthetic', notes=[],
            timings={}, analyses=[])
    monkeypatch.setattr(pipeline, 'produce_reel', produce)
    # No list on this desktop: an empty trending list unless a test supplies
    # one (the real read on a fresh data dir: REAL_TRENDING).
    from integrations.trends import trending_songs_client as tsc
    monkeypatch.setattr(tsc, 'get_trending_songs', lambda langs=None, limit=10: {
        'schema': C.TRENDING_CLIENT_SCHEMA, 'songs': [], 'stale': True, 'reason': 'not_run_yet'})
    request = {'id': 'req-1'}
    yield SimpleNamespace(store=store, pushes=pushes, produced=produced, tmp=tmp_path,
                          prompts=prompts, request=request, mp=monkeypatch)
    platform_paths.reset_cache()


def _tools(env, user=UID, prompt=PID, stored_tags=HER_TAGS, executor=False):
    """Register the reel tools on recording agents, for an agent whose record
    stores ``stored_tags`` (her Reel Studio agent unless told otherwise);
    ``executor`` adds the group chat's distinct executor (REUSE passes one to
    every Tier-2 family)."""
    from integrations.reel_studio.tools import register_reel_studio_tools
    helper, assistant = _Agent(), _Agent()
    kw = {'executor': _Agent()} if executor else {}
    names = register_reel_studio_tools(helper, assistant, user, prompt_id=prompt,
                                       request_id_getter=lambda: env.request['id'],
                                       stored_tags=stored_tags, **kw)
    return SimpleNamespace(names=names, helper=helper, assistant=assistant,
                           executor=kw.get('executor'), run=assistant.exe)


def _upload(env, data, *, name='VID.mp4', mime='video/mp4', purpose='reel_source',
            captured_at=None, request_id='req-1', prompt_id=PID, user=UID):
    xid = hashlib.sha256(data).hexdigest()
    meta = {'captured_at': captured_at} if captured_at else None
    ans = env.store.offer(user, name, len(data), xid, mime, purpose, meta=meta)
    assert ans['type'] == 'file_accept', ans
    assert env.store.put_chunk(user, xid, 0, data, xid)['type'] == 'file_ack'
    assert env.store.commit(user, xid)['type'] == 'file_done'
    if request_id is not None:
        env.store.bind(user, [xid], prompt_id=prompt_id, request_id=request_id)
    return xid


def _ok(reply):
    from core.constants import tool_reply_failed
    assert not tool_reply_failed(reply), reply
    return json.loads(reply)


def _failed(reply):
    from core.constants import tool_reply_failed
    assert tool_reply_failed(reply), reply
    return json.loads(reply.split('\n', 1)[1])


def _media_pushes(env):
    return [p for p in env.pushes if isinstance(p[1], dict) and p[1].get('media')]


# ── registration ──────────────────────────────────────────────────────────

def test_her_agent_carries_it_on_both_seats_and_the_executor_runs_them(env):
    """The canonical Tier-2 shape (news_tools.register_news_tools): the
    Assistant carries the schema too, or a reel_make it proposes is free-
    formed as text that autogen never runs (measured live on the news family,
    2026-09-03); the executor runs them so the Assistant's own structured
    call is not stranded by autogen's repeat-speaker rule.  The on-demand
    ones (reel_watchlist) run on the same seats with no schema
    (test_reel_watchlist_tool.py)."""
    from integrations.reel_studio.tools import REEL_ON_DEMAND_TOOL_NAMES, REEL_TOOL_NAMES
    every = set(REEL_TOOL_NAMES) | set(REEL_ON_DEMAND_TOOL_NAMES)
    t = _tools(env, executor=True)
    assert len(REEL_TOOL_NAMES) == 7
    assert t.names == list(REEL_TOOL_NAMES) + list(REEL_ON_DEMAND_TOOL_NAMES)
    assert set(t.helper.llm) == set(REEL_TOOL_NAMES) and t.helper.exe == {}
    assert set(t.assistant.llm) == set(REEL_TOOL_NAMES)
    assert set(t.assistant.exe) == every
    assert set(t.executor.exe) == every and t.executor.llm == {}
    for seat in (t.helper, t.assistant):
        for name, (desc, func) in seat.llm.items():
            first = (func.__doc__ or '').strip().split('\n')[0].strip()
            assert desc == first and len(desc) > 20, name
            # One closure per tool, whichever seat proposes or runs it.
            assert func is t.assistant.exe[name] is t.executor.exe[name], name


def test_without_an_executor_the_assistant_runs_them(env):
    from integrations.reel_studio.tools import REEL_ON_DEMAND_TOOL_NAMES, REEL_TOOL_NAMES
    t = _tools(env)
    assert set(t.helper.llm) == set(t.assistant.llm) == set(REEL_TOOL_NAMES)
    assert set(t.assistant.exe) == set(REEL_TOOL_NAMES) | set(REEL_ON_DEMAND_TOOL_NAMES)


@pytest.mark.parametrize('stored', [None, [], ['news'], 'reel_studio', [None, 42]])
def test_an_agent_that_is_not_hers_is_given_no_reel_tool(env, stored):
    """Only an agent whose record STORES reel_studio is hers.  Every other
    agent -- no stored tags, other tags, or a junk value (a bare string is
    not a tag list) -- gets nothing: not the schema, and not execution
    either, because discover_and_attach finds any tool on an agent's
    function map by its docstring, so request_tools would offer it the
    family for an unrelated need (critic report 2.6: "trending news today"
    -> reel_trending_songs, "Immediately CALL")."""
    t = _tools(env, stored_tags=stored, executor=True)
    assert t.names == []
    for seat in (t.helper, t.assistant, t.executor):
        assert seat.llm == {} and seat.exe == {}, stored


def test_every_tool_says_what_it_does_on_its_first_docstring_line(env):
    """discover_and_attach matches, and attaches with, the first docstring
    line of a function-map entry: it is how a deferred tool comes back."""
    for func in _tools(env).run.values():
        assert (func.__doc__ or '').strip().split('\n')[0].strip()


def test_on_the_central_node_no_reel_tool_is_registered(env, monkeypatch):
    """Amendment A8 d: reel_studio is inert on central, so no reel tool sits
    on any of central's agents, not even execution-only for request_tools to
    re-attach ("instagram reels for our product" would match
    reel_make's docstring)."""
    from integrations.reel_studio import jobs
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    monkeypatch.setattr(jobs, '_SWEPT', False)
    swept = []
    monkeypatch.setattr(jobs, 'sweep_interrupted', lambda **k: swept.append(1) or [])
    t = _tools(env, executor=True)              # her stored tag, on central
    assert t.names == []
    for seat in (t.helper, t.assistant, t.executor):
        assert seat.llm == {} and seat.exe == {}
    assert swept == []


def test_real_autogen_agents_take_the_tools(env):
    autogen = pytest.importorskip('autogen')
    from integrations.reel_studio.tools import REEL_TOOL_NAMES, register_reel_studio_tools

    def llm_config():
        return {'config_list': [{'model': 'local', 'api_key': 'none',
                                 'base_url': 'http://127.0.0.1:9'}]}
    helper = autogen.AssistantAgent('helper', llm_config=llm_config())
    assistant = autogen.AssistantAgent('assistant', llm_config=llm_config())
    executor = autogen.UserProxyAgent('executor', human_input_mode='NEVER',
                                      code_execution_config=False)
    register_reel_studio_tools(helper, assistant, UID, prompt_id=PID,
                               request_id_getter=lambda: None, stored_tags=['reel_studio'],
                               executor=executor)
    from core.llm_outbound_logger import _schema_tokens
    for seat in (helper, assistant):
        schema = {t['function']['name']: t['function'] for t in seat.llm_config['tools']}
        assert set(schema) == set(REEL_TOOL_NAMES), seat.name
        props = schema['reel_make']['parameters']['properties']
        assert {'target_seconds', 'open_with', 'hook_text', 'caption', 'hashtags', 'song_key',
                'use_logo', 'clips'} == set(props)
        for bound in ('user_id', 'prompt_id', 'request_id'):
            assert all(bound not in s['parameters'].get('properties', {})
                       for s in schema.values())
        # The family rides each seat next to the core tools: its schema stays
        # within 900 tokens by the counter fit_schema_to_ctx uses.
        assert _schema_tokens({'tools': seat.llm_config['tools']}) <= 900, seat.name
    assert set(REEL_TOOL_NAMES) <= set(assistant.function_map)
    assert set(REEL_TOOL_NAMES) <= set(executor.function_map)


def test_importing_the_tools_is_cheap():
    code = ('import sys, integrations.reel_studio\n'
            'before = set(sys.modules)\n'
            'import integrations.reel_studio.tools\n'
            'new = sorted(set(sys.modules) - before)\n'
            'print("\\n".join(new))\n')
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = subprocess.run([sys.executable, '-c', code], cwd=here, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    loaded = out.stdout.split()
    heavy = [m for m in loaded if m.split('.')[0] in ('cv2', 'numpy', 'autogen', 'langchain')
             or m.startswith(('integrations.reel_studio.media', 'integrations.reel_studio.pipeline',
                              'integrations.vision'))]
    assert heavy == []


def test_the_closures_answer_for_the_user_they_were_built_for(env):
    a, b = _tools(env, user='10077'), _tools(env, user='20088', prompt='7')
    _ok(a.run['reel_voice'](action='set', field='city', value='Madurai'))
    assert _ok(a.run['reel_voice'](action='get'))['profile']['city'] == 'Madurai'
    assert _ok(b.run['reel_voice'](action='get'))['profile'] == {}


def test_the_first_registration_sweeps_interrupted_jobs(env, monkeypatch):
    from integrations.reel_studio import jobs
    monkeypatch.setattr(jobs, '_SWEPT', False)
    calls = []
    monkeypatch.setattr(jobs, 'sweep_interrupted', lambda **k: calls.append(1) or [])
    _tools(env, stored_tags=None)       # any agent built here: not her agent's alone
    _tools(env)
    assert calls == [1]


def test_each_reel_runs_the_stores_retention_pass(env, monkeypatch):
    gcs = []
    monkeypatch.setattr(env.store, 'gc', lambda **k: gcs.append(1) or 0)
    _upload(env, b'clip' * 500)
    _ok(_tools(env).run['reel_make']())
    assert gcs == [1]


# ── reel_make ─────────────────────────────────────────────────────────────

def test_no_clips_asks_her_for_clips_and_is_not_the_actions_work(env):
    t = _tools(env)
    reply = t.run['reel_make']()
    body = _failed(reply)
    assert body['status'] == 'needs_clips' and body['message'].strip()
    assert env.produced == [] and _media_pushes(env) == []


REEL_ACTION = ('Call reel_make to make the reel from the clips she attached and send it '
               'to her phone.')


@pytest.fixture
def create_action(env, monkeypatch):
    """One CREATE action, "make the reel", as create_recipe runs it: the
    session's group log and ledger registered the way create_recipe
    registers them, the action in progress, its Assistant serving the real
    reel tools.  ``verdict(reply)`` puts the dispatch, the Assistant's
    reel_make call and ``reply`` in the log, then hands the StatusVerifier's
    "completed" verdict to the real verifier hook, as CREATE's loop does."""
    lh = pytest.importorskip('hartos.lifecycle_hooks')
    up = 'reelcreatesession'        # no '_': the ledger registry makes nothing on disk
    t = _tools(env)
    serving = SimpleNamespace(name='Assistant', _function_map=dict(t.run),
                              llm_config={'tools': []})
    group = SimpleNamespace(messages=[], agents=[serving])
    ledger = SimpleNamespace(
        tasks={'action_1': SimpleNamespace(description=REEL_ACTION, context={})},
        save=lambda: True)
    monkeypatch.setattr(lh, 'get_registered_groupchat', lambda _up: group)
    monkeypatch.setattr(lh, 'get_registered_ledger', lambda _up: ledger)
    monkeypatch.setattr(lh, '_promote_verified_outcome', lambda *a, **k: None)
    lh.action_states.pop(up, None)
    lh.retry_tracker.reset_count(up, 1)
    lh.set_action_state(up, 1, lh.ActionState.IN_PROGRESS, 'test')

    def verdict(reply):
        group.messages[:] = [
            {'role': 'user', 'name': 'ChatInstructor',
             'content': 'Execute Action 1: %s ,Latest User message: make my reel' % REEL_ACTION},
            {'role': 'assistant', 'name': 'Assistant', 'content': None,
             'tool_calls': [{'id': 'c1', 'type': 'function',
                             'function': {'name': 'reel_make', 'arguments': '{}'}}]},
            {'role': 'tool', 'name': 'Assistant', 'content': reply,
             'tool_responses': [{'tool_call_id': 'c1', 'role': 'tool', 'content': reply}]}]
        answer = lh.lifecycle_hook_process_verifier_response(
            up, {'status': 'completed', 'action_id': 1,
                 'evidence': {'message_index': 2, 'kind': 'tool_receipt'}},
            SimpleNamespace(current_action=1))
        return answer, lh.get_action_state(up, 1)
    yield SimpleNamespace(run=t.run, verdict=verdict, lh=lh, up=up)
    lh.action_states.pop(up, None)
    lh.retry_tracker.reset_count(up, 1)


def test_a_create_action_whose_reel_make_needs_clips_is_not_completed(create_action):
    """plan section 5: the needs_clips answer is never banked as the action's
    work -- the verifier's "completed" is refused and the action steered."""
    lh = create_action.lh
    reply = create_action.run['reel_make']()                 # no clips attached
    assert _failed(reply)['status'] == 'needs_clips'
    answer, state = create_action.verdict(reply)
    assert answer['action'] == 'force_completion', answer
    assert state is not lh.ActionState.COMPLETED
    assert not lh.tool_result_is_action_work(create_action.up, 1, 2)


def test_a_create_action_whose_reel_make_made_the_reel_is_completed(env, create_action):
    """Control: the same action with her clips attached completes on the reel."""
    lh = create_action.lh
    _upload(env, b'clip' * 500)
    reply = create_action.run['reel_make']()
    assert _ok(reply)['status'] == 'ok'
    answer, state = create_action.verdict(reply)
    assert answer['action'] == 'force_fallback', answer
    assert state is lh.ActionState.COMPLETED


def test_this_turns_clips_make_a_reel_that_reaches_her_with_the_turns_request_id(env):
    a = _upload(env, b'clip-a' * 500, captured_at=1_760_247_082_000)
    b = _upload(env, b'clip-b' * 500, captured_at=1_760_247_382_000)
    _upload(env, b'old-batch' * 500, request_id='req-0')
    env.request['id'] = 'req-at-registration'
    t = _tools(env)
    env.request['id'] = 'req-1'                 # read at call time, not at registration
    body = _ok(t.run['reel_make'](target_seconds=15))
    assert body['status'] == 'ok'
    media = body['media']
    assert C.validate_media_item(media) == []
    assert [s.xid for s in env.produced[0]['sources']] == [a, b]
    assert env.store.resolve(UID, media['xid'], box='out') is not None
    (topic, payload, flags), = _media_pushes(env)
    assert payload['request_id'] == 'req-1' and payload['prompt_id'] == PID
    assert payload['text'][0].strip() and flags == {'skip_crossbar': True}
    assert env.store.query_inbox(UID, request_id='req-1') == []        # consumed
    assert env.produced[0]['style']['target_s'] == 15


def test_with_none_attached_this_turn_the_newest_unused_batch_is_used(env):
    _upload(env, b'first' * 500, request_id='req-a')
    newest = _upload(env, b'second' * 500, request_id='req-b')
    env.request['id'] = 'req-c'
    t = _tools(env)
    _ok(t.run['reel_make']())
    assert [s.xid for s in env.produced[0]['sources']] == [newest]


def test_named_clips_are_used_and_anything_else_is_ignored(env):
    a = _upload(env, b'one' * 500)
    b = _upload(env, b'two' * 500)
    logo = _upload(env, b'\x89PNG' + b'0' * 64, name='logo.png', mime='image/png', purpose='logo')
    t = _tools(env)
    _ok(t.run['reel_make'](clips='%s, %s, nonsense, %s' % (b, logo, 'f' * 64)))
    assert [s.xid for s in env.produced[0]['sources']] == [b]
    assert a  # the other clip stays unused
    assert [s.xid for s in env.store.query_inbox(UID, purpose='reel_source')] == [a]


def test_another_users_clip_is_never_used(env):
    theirs = _upload(env, b'theirs' * 500, user='20088', prompt_id='7')
    t = _tools(env)
    body = _failed(t.run['reel_make'](clips=theirs))
    assert body['status'] == 'needs_clips'


def _voice_file():
    """Her voice profile on disk (voice_profile.user_dir: 'voice.json' in it)."""
    from integrations.reel_studio import voice_profile
    return os.path.join(voice_profile.user_dir(UID), 'voice.json')


def test_her_newest_logo_is_used_and_her_profile_is_not_written(env):
    """Amendment A11b: making a reel reads her profile and never writes it;
    the logo she uploaded last is used as it is."""
    from integrations.reel_studio import voice_profile
    voice_profile.update_field(UID, 'city', 'Madurai')
    with open(_voice_file(), 'rb') as f:
        before = f.read()
    _upload(env, b'clip' * 500)
    logo = _upload(env, b'\x89PNG-new' + b'1' * 64, name='logo.png', mime='image/png',
                   purpose='logo', request_id=None)
    t = _tools(env)
    _ok(t.run['reel_make']())
    held = env.store.resolve(UID, logo, box='in')
    assert env.produced[0]['logo_path'] == held.path
    with open(_voice_file(), 'rb') as f:
        assert f.read() == before
    assert 'logo_xid' not in voice_profile.load_voice(UID)


def test_a_reel_for_her_with_no_profile_creates_none(env):
    _upload(env, b'clip' * 500)
    logo = _upload(env, b'\x89PNG-new' + b'6' * 64, name='logo.png', mime='image/png',
                   purpose='logo', request_id=None)
    _ok(_tools(env).run['reel_make']())
    assert env.produced[0]['logo_path'] == env.store.resolve(UID, logo, box='in').path
    assert not os.path.exists(_voice_file())


def test_the_logo_in_her_profile_is_used_when_nothing_newer_was_uploaded(env):
    from integrations.reel_studio import voice_profile
    logo = _upload(env, b'\x89PNG-saved' + b'2' * 64, name='logo.png', mime='image/png',
                   purpose='logo', request_id=None)
    voice_profile.update_field(UID, 'logo_xid', logo)
    _upload(env, b'clip' * 500)
    t = _tools(env)
    _ok(t.run['reel_make']())
    assert env.produced[0]['logo_path'] == env.store.resolve(UID, logo).path


def test_no_logo_when_she_says_so(env):
    _upload(env, b'\x89PNG' + b'3' * 64, name='logo.png', mime='image/png', purpose='logo')
    _upload(env, b'clip' * 500)
    t = _tools(env)
    _ok(t.run['reel_make'](use_logo='no'))
    assert env.produced[0]['logo_path'] is None


@pytest.mark.parametrize('junk', ['nah', 'maybe', 'the usual'])
def test_an_unclear_use_logo_leaves_her_own_no_logo_standing(env, junk):
    """An argument the one flag reading does not recognise is "not given":
    it never overrides what she said ("no logo")."""
    (env.prompts / ('%s.json' % PID)).write_text(json.dumps(
        {'goal': 'Bridal reels in 20s, no logo please'}), encoding='utf-8')
    _upload(env, b'\x89PNG' + b'4' * 64, name='logo.png', mime='image/png', purpose='logo')
    _upload(env, b'clip' * 500)
    _ok(_tools(env).run['reel_make'](use_logo=junk))
    assert env.produced[0]['style']['use_logo'] is False
    assert env.produced[0]['logo_path'] is None


def test_a_profile_with_only_her_city_invents_no_caption(env):
    from integrations.reel_studio import voice_profile
    voice_profile.update_field(UID, 'city', 'Madurai')
    _upload(env, b'clip' * 500)
    media = _ok(_tools(env).run['reel_make']())['media']
    assert media['caption'] == ''


def test_a_busy_desktop_answers_before_touching_her_profile_or_the_trending_list(
        env, monkeypatch):
    from integrations.reel_studio import jobs, voice_profile
    from integrations.trends import trending_songs_client as tsc
    asked = []
    monkeypatch.setattr(tsc, 'get_trending_songs', lambda langs=None, limit=10: asked.append(
        langs) or {'schema': C.TRENDING_CLIENT_SCHEMA, 'songs': [], 'stale': True,
                   'reason': 'no_central'})
    _upload(env, b'clip' * 500)
    _upload(env, b'\x89PNG-new' + b'5' * 64, name='logo.png', mime='image/png',
            purpose='logo', request_id=None)
    other = jobs.start_job('20088', '7', 'x')
    try:
        body = _failed(_tools(env).run['reel_make']())
    finally:
        jobs.finish_job(other.reel_id, 'ok')
    assert body['status'] == 'busy' and body['message']
    assert asked == [] and env.produced == []
    assert 'logo_xid' not in voice_profile.load_voice(UID)


def test_a_saved_logo_the_store_cannot_read_is_logged_and_left_out(env, monkeypatch, caplog):
    from integrations.reel_studio import voice_profile
    saved = hashlib.sha256(b'gone').hexdigest()
    voice_profile.update_field(UID, 'logo_xid', saved)
    _upload(env, b'clip' * 500)
    real = env.store.resolve

    def resolve(user_id, xid, *a, **k):
        if xid == saved:
            raise OSError(5, 'Input/output error')
        return real(user_id, xid, *a, **k)
    monkeypatch.setattr(env.store, 'resolve', resolve)
    with caplog.at_level(logging.WARNING, logger='integrations.reel_studio.tools'):
        _ok(_tools(env).run['reel_make']())
    assert env.produced[0]['logo_path'] is None
    assert any(saved[:12] in r.getMessage() and r.levelno >= logging.WARNING
               for r in caplog.records), [r.getMessage() for r in caplog.records]


def test_her_agent_record_sets_the_style_and_arguments_override_it(env):
    (env.prompts / ('%s.json' % PID)).write_text(json.dumps(
        {'goal': 'Bridal reels: open with the final look, 25s, Tamil songs, logo top right',
         'flows': []}), encoding='utf-8')
    _upload(env, b'clip' * 500)
    t = _tools(env)
    _ok(t.run['reel_make'](target_seconds=12))
    style = env.produced[0]['style']
    assert style['target_s'] == 12                       # the argument
    assert style['hook_first'] is True and style['language'] == 'ta'
    assert style['logo_corner'] == 'top_right'
    assert C.validate_style(style) == []


@pytest.mark.parametrize('open_with,want', [('final_look', True), ('story', False),
                                            ('bare_face', False)])
def test_open_with_sets_the_teaser(env, open_with, want):
    (env.prompts / ('%s.json' % PID)).write_text(json.dumps(
        {'goal': 'start with the result'}), encoding='utf-8')
    _upload(env, b'clip' * 500)
    t = _tools(env)
    _ok(t.run['reel_make'](open_with=open_with))
    assert env.produced[0]['style']['hook_first'] is want


def test_hashtags_are_cleaned_and_capped_at_five(env):
    _upload(env, b'clip' * 500)
    t = _tools(env)
    body = _ok(t.run['reel_make'](hashtags='#bridalmakeup, chennaimua #mua #mua #a #b #c #d'))
    assert body['media']['hashtags'] == ['#bridalmakeup', '#chennaimua', '#mua', '#a', '#b']


def test_hashtags_and_caption_come_from_her_profile_when_not_given(env):
    from integrations.reel_studio import voice_profile
    voice_profile.update_field(UID, 'hashtag_bank', '#a #b #c #d #e #f #g')
    voice_profile.update_field(UID, 'services', 'Bridal and reception makeup')
    voice_profile.update_field(UID, 'city', 'Madurai')
    voice_profile.update_field(UID, 'cta', 'DM for bookings')
    _upload(env, b'clip' * 500)
    t = _tools(env)
    media = _ok(t.run['reel_make']())['media']
    assert media['hashtags'] == ['#a', '#b', '#c', '#d', '#e']
    assert 'Madurai' in media['caption'] and 'DM for bookings' in media['caption']


def test_a_trending_song_named_by_key_rides_the_reel(env, monkeypatch):
    from integrations.trends import trending_songs_client as tsc
    songs = {'schema': C.TRENDING_CLIENT_SCHEMA, 'fetched_at': '2026-10-14T00:00:00Z',
             'stale': False, 'reason': None,
             'songs': [{'song_key': 'cluster:9', 'title': 'Vaathi', 'artist': 'Anirudh',
                        'lang': 'ta', 'suggested_start_ms': 38_500, 'start_label': '0:38',
                        'score': 3.2}]}
    monkeypatch.setattr(tsc, 'get_trending_songs', lambda langs=None, limit=10: songs)
    _upload(env, b'clip' * 500)
    t = _tools(env)
    song = _ok(t.run['reel_make'](song_key='cluster:9'))['media']['song']
    assert song == {'song_key': 'cluster:9', 'title': 'Vaathi', 'artist': 'Anirudh', 'lang': 'ta',
                    'start_ms': 38_500, 'start_label': '0:38', 'beats': 'synthetic'}


def test_without_a_song_key_the_top_song_in_her_language_is_suggested(env, monkeypatch):
    from integrations.trends import trending_songs_client as tsc
    asked = []

    def trending(langs=None, limit=10):
        asked.append(langs)
        return {'songs': [{'song_key': 'cluster:1', 'title': 'Top', 'artist': 'X', 'lang': 'hi',
                           'suggested_start_ms': 0, 'start_label': '0:00', 'score': 1.0}]}
    monkeypatch.setattr(tsc, 'get_trending_songs', trending)
    (env.prompts / ('%s.json' % PID)).write_text(json.dumps({'goal': 'Hindi songs'}),
                                                 encoding='utf-8')
    _upload(env, b'clip' * 500)
    t = _tools(env)
    song = _ok(t.run['reel_make']())['media']['song']
    assert song['song_key'] == 'cluster:1' and asked[-1] == 'hi'


def test_her_song_audio_gives_the_beats(env):
    _upload(env, b'clip' * 500)
    audio = _upload(env, b'ID3' + b'9' * 500, name='Vaathi Coming.mp3', mime='audio/mpeg',
                    purpose='song_audio')
    t = _tools(env)
    media = _ok(t.run['reel_make']())['media']
    assert env.produced[0]['song_audio'] == env.store.resolve(UID, audio).path
    assert media['song']['beats'] == 'local_audio'
    assert media['song']['title'] == 'Vaathi Coming'


def test_on_a_default_install_the_reel_says_why_and_names_no_song(env, monkeypatch):
    """Reviewer's measurement on c17513ec4, a fresh data dir with no owner
    credentials: reel_trending_songs said why there was no list, but
    reel_make dropped the reason, pushed no word about songs, and still told
    her agent to give her "the song to add in Instagram".  And on
    d73704a5e: every install's default also has no watchlist (nothing writes
    one), so that is the reason she is told, in words she can act on, and no
    research account is asked for.  Here the real trending read and the real
    job, a fresh vault, no watchlist and nobody on this computer to ask."""
    from hartos import ai_key_vault
    from integrations.trends import trending_songs_client as tsc
    monkeypatch.setattr(tsc, 'get_trending_songs', REAL_TRENDING)
    vault = ai_key_vault.AIKeyVault()
    monkeypatch.setattr(ai_key_vault, 'get_ai_key_vault', lambda: vault)
    for name in ('HEVOLVE_OWNER_USER_ID', 'IG_RESEARCH_USERNAME', 'IG_RESEARCH_PASSWORD'):
        monkeypatch.delenv(name, raising=False)
    _upload(env, b'clip' * 500)
    t = _tools(env)
    listed = _ok(t.run['reel_trending_songs']())
    assert listed['songs'] == [] and listed['reason'] == 'no_watchlist'
    assert listed['note'] == tsc.note_for('no_watchlist')
    body = _ok(t.run['reel_make']())
    assert body['media']['song'] is None
    assert listed['note'] in body['notes']
    assert 'song to add' not in body['message'] and listed['note'] in body['message']
    (_topic, payload, _flags), = _media_pushes(env)
    assert 'No song is suggested' in payload['text'][0]
    assert listed['note'] in payload['text'][0]


def test_a_song_key_the_list_does_not_hold_is_not_offered_as_a_song(env):
    from integrations.reel_studio.tools import UNLISTED_SONG_NOTE
    _upload(env, b'clip' * 500)
    body = _ok(_tools(env).run['reel_make'](song_key='cluster:404'))
    assert body['media']['song']['song_key'] == 'cluster:404'
    assert body['media']['song']['title'] == ''
    assert UNLISTED_SONG_NOTE in body['notes'] and UNLISTED_SONG_NOTE in body['message']
    assert 'song to add' not in body['message']


def test_a_busy_desktop_says_so_in_the_failure_form(env):
    from integrations.reel_studio import jobs
    _upload(env, b'clip' * 500)
    other = jobs.start_job('20088', '7', 'x')
    try:
        body = _failed(_tools(env).run['reel_make']())
    finally:
        jobs.finish_job(other.reel_id, 'ok')
    assert body['status'] == 'busy' and body['message']


# ── the writing tools ─────────────────────────────────────────────────────

def test_check_caption_lints_detects_and_humanizes_in_her_voice(env):
    from integrations.reel_studio import voice_profile
    caption = 'Meena did this bride in Madurai ✨ DM for bookings #bridalmakeup'
    t = _tools(env)

    def structures(body):
        return [s['id'] for s in body['humanized']['report']['structures']]
    before = _ok(t.run['reel_check_caption'](caption=caption, keywords='bridal makeup Madurai'))
    assert 'rocket' in structures(before)        # a sparkle reads as a machine tell ...
    voice_profile.update_field(UID, 'emoji', '✨')
    body = _ok(t.run['reel_check_caption'](caption=caption, keywords='bridal makeup Madurai'))
    assert 'rocket' not in structures(body)      # ... unless it is one of hers
    assert body['lint']['verdict'] in ('READY', 'REVIEW', 'FIX')
    assert any(c['check'] == 'SEARCH TERMS' for c in body['lint']['checks'])
    assert body['detection']['verdict'] in ('PASS', 'REVIEW', 'FLAGGED')
    assert body['humanized']['text'].strip()


def test_score_hooks_gives_verdicts_and_never_a_number(env):
    t = _tools(env)
    body = _ok(t.run['reel_score_hooks'](hooks='Hi guys welcome back\n₹15k bridal look in 20s'))
    assert [h['verdict'] for h in body['hooks']] == ['WEAK', body['hooks'][1]['verdict']]
    assert body['note']
    for h in body['hooks']:
        assert set(h) >= {'hook', 'verdict', 'weakest'} and 'score' not in h


def test_beat_sheet_times_at_her_own_rate(env):
    from integrations.reel_studio import voice_profile
    voice_profile.update_field(UID, 'wpm', 120)
    t = _tools(env)
    body = _ok(t.run['reel_beat_sheet'](script='Bare face to bride in twenty seconds.\nDM me.',
                                        target_seconds=20))
    assert body['wpm'] == 120 and body['target'] == 20 and body['beats']


def test_skill_text_comes_with_her_voice_brief_in_front(env):
    from integrations.reel_studio import voice_profile
    from integrations.reel_studio.igkit.skills import get_skill_text
    voice_profile.update_field(UID, 'city', 'Madurai')
    t = _tools(env)
    text = t.run['reel_skill'](name='ig-caption')
    assert text.startswith(voice_profile.voice_brief(UID))
    assert text.endswith(get_skill_text('ig_caption'))


def test_an_unknown_skill_names_the_ones_there_are(env):
    from core.constants import tool_reply_failed
    t = _tools(env)
    reply = t.run['reel_skill'](name='ig_viral')
    assert tool_reply_failed(reply) and 'ig_reel' in reply


def test_voice_set_get_clear_and_fields(env):
    t = _tools(env)
    profile = _ok(t.run['reel_voice'](action='set', field='hashtag_bank', value='#a, #b'))['profile']
    assert profile['hashtag_bank'] == ['#a', '#b']
    _ok(t.run['reel_voice'](action='clear', field='hashtag_bank'))
    assert 'hashtag_bank' not in _ok(t.run['reel_voice']())['profile']
    fields = _ok(t.run['reel_voice'](action='fields'))['fields']
    assert 'caption_language' in fields and fields['caption_language']['help']


def test_a_value_her_profile_refuses_is_reported_not_raised(env):
    from core.constants import tool_reply_failed
    t = _tools(env)
    reply = t.run['reel_voice'](action='set', field='wpm', value='9000')
    assert tool_reply_failed(reply) and 'wpm' in reply
    assert tool_reply_failed(t.run['reel_voice'](action='set', field='colour', value='pink'))
    assert tool_reply_failed(t.run['reel_voice'](action='dance'))


def test_trending_songs_default_to_her_caption_language(env, monkeypatch):
    from integrations.reel_studio import voice_profile
    from integrations.trends import trending_songs_client as tsc
    voice_profile.update_field(UID, 'caption_language', 'pa')
    asked = []
    monkeypatch.setattr(tsc, 'get_trending_songs',
                        lambda langs=None, limit=10: asked.append((langs, limit)) or
                        {'schema': C.TRENDING_CLIENT_SCHEMA, 'songs': [], 'stale': True,
                         'reason': 'no_central'})
    t = _tools(env)
    body = _ok(t.run['reel_trending_songs']())
    assert asked == [('pa', 5)] and body['reason'] == 'no_central'
    _ok(t.run['reel_trending_songs'](language='ta', limit=3))
    assert asked[-1] == ('ta', 3)
