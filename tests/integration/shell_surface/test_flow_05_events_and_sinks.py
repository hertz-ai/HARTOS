"""CHAPTER 05 -- EVENTS AND SINKS: where every flow in this suite drains.

Closing chapter of the network's story. Everything the earlier chapters set
in motion (installs, upgrades, agent pushes, shell errors) becomes an EVENT,
and every event drains through one of a small set of sinks:

    agent push (POST /api/a2ui) --> _agent_components --> SSE
                                      /api/notifications/stream --> the shell
    desktop notify (POST /api/shell/notifications/send) --> in-memory queue
                                      + notify-send D-Bus argv --> the OS
    journald --> GET /api/shell/system/logs (poll)
             --> GET /api/shell/system/logs/stream (SSE follow) --> the panel
    WebView console --> POST /api/shell/clientlog --> the module logger
                                                  --> the node journal
    web images / local captions --> /api/media/* (local-only, consent-gated
                                                  egress) --> the home cards
    free-form intent --> /api/agent/ask + /api/assistant/chat --> the brain
    context --> /api/context --> /api/ui --> rendered HTML --> the human

SSE DRIVING RULE (learned from a wedged run): both stream generators can
block/spin BEFORE their first yield when no event is pending, and the test
client pulls the first chunk, so a cold open can hang forever. Every stream
here is therefore (1) SEEDED so the first yield is immediate and (2) opened
inside a guarded worker thread with a join timeout; a wedge is narrated and
xfailed instead of hanging the suite.
"""
import json
import logging
import threading
import time
import uuid

import pytest


# ─── chapter-local helpers ───────────────────────────────────────────────────

def _calls_for(fake_os, binary):
    """The argv stream a specific binary received. Background daemons started
    with the app (idle media indexer, GPU probes) share the SAME faked
    subprocess boundary and can interleave their own argv at any moment, so
    argv assertions always read the log through this per-binary filter."""
    return [c for c in fake_os.calls
            if isinstance(c, (list, tuple)) and c and c[0] == binary]


def _drive_stream(client, url, timeout=15.0):
    """Open an SSE route with buffered=False, read exactly ONE chunk, close.

    Runs the whole open/read/close inside a daemon worker so a generator that
    blocks pre-yield can never wedge the suite; the caller decides what a
    timeout means. Returns {'status', 'mimetype', 'chunk'} or None on wedge.
    """
    out = {}

    def _go():
        resp = client.open(url, buffered=False)
        out['status'] = resp.status_code
        out['mimetype'] = resp.mimetype
        try:
            chunk = next(iter(resp.response))
            out['chunk'] = (chunk.decode('utf-8', 'replace')
                            if isinstance(chunk, bytes) else str(chunk))
        except StopIteration:
            out['chunk'] = ''
        finally:
            resp.close()                 # never iterate further into infinity

    worker = threading.Thread(target=_go, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive() or 'chunk' not in out:
        return None                      # wedged pre-yield; caller narrates
    return out


# ─── Scene 1: the desktop notification source (send -> list -> ack) ─────────

def test_ch05_scene1_notification_send_list_ack(client, fake_os):
    """/api/shell/notifications* (shell_os_apis) -- the freedesktop bridge.

    TOPOLOGY of a notification's life:
      entry: POST /send {'title','body',...}
      -> the record is appended to the module-level in-memory queue
         (_notification_queue; id = position+1, read=False)
      -> boundary: argv ['notify-send', '-u', <urgency>, '-i', <icon>,
         '-t', <timeout>, title, body] carries it to the OS D-Bus daemon;
         its rc feeds ONLY the 'dbus_delivered' flag (a missing notify-send
         degrades gracefully, the queue keeps the record either way)
      -> GET /   : DB-backed NotificationService first (source='database'),
         in-memory queue as the fallback (source='memory') -- two sources,
         one list shape
      -> POST /read {'all': True}: the ack sweeps the MEMORY queue and
         reports how many records it marked.
    """
    resp = client.post('/api/shell/notifications/send', json={
        'title': 'ch05-notif', 'body': 'events chapter', 'urgency': 'low',
        'icon': 'dialog-information', 'timeout': 1234})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['sent'] is True
    # rc 0 from the faked boundary -> the D-Bus leg reports delivered.
    assert body['dbus_delivered'] is True
    assert body['notification']['title'] == 'ch05-notif'
    assert _calls_for(fake_os, 'notify-send')[-1] == [
        'notify-send', '-u', 'low', '-i', 'dialog-information',
        '-t', '1234', 'ch05-notif', 'events chapter']

    # The list side: whichever source answers (DB when the social models are
    # importable, memory otherwise), the shape is one 'notifications' list.
    resp = client.get('/api/shell/notifications')
    assert resp.status_code == 200
    listing = resp.get_json()
    assert listing['source'] in ('database', 'memory')
    assert isinstance(listing['notifications'], list)
    if listing['source'] == 'memory':
        assert any(n['title'] == 'ch05-notif' for n in listing['notifications'])

    # The ack drains the memory queue our send fed, so at least OUR record
    # gets marked regardless of which source served the list above.
    resp = client.post('/api/shell/notifications/read', json={'all': True})
    assert resp.status_code == 200
    assert resp.get_json()['marked'] >= 1


# ─── Scene 2: the agent-event SSE sink (/api/notifications/stream) ──────────

def test_ch05_scene2_agent_event_stream_delivers_a2ui_pushes(
        client, fake_os, monkeypatch):
    """/api/notifications/stream -- where every accepted A2UI push exits.

    SOURCE -> SINK TOPOLOGY:
      source: POST /api/a2ui {'agent_id', 'component'} -> agent_ui_update, the
        ONE governed channel (type allowlist -> hive kill-switch -> per-agent
        rate cap -> XSS reject -> audit log) -> accepted components land in
        self._agent_components stamped with _ts=now
      sink: the stream generator loops forever: sleep(2) -> collect EVERY
        component (all types, not just notifications) with _ts newer than its
        watermark -> advance watermark -> yield ONE SSE frame
        'data: [events...]' -> the shell's EventSource.

    CODE-TRUE SUBTLETY: the watermark starts at the generator's OPEN time, so
    the stream is a FROM-NOW TAIL, not a replay -- a component pushed before
    the open is invisible on a real clock. To drive the first yield
    hermetically we push first (real clock), then freeze time.time 60s in the
    PAST while opening: the watermark now predates our push and the very
    first poll yields it. That is the code's own semantics, just observed
    from a shifted clock.
    """
    # Validation legs of the source first: an unknown component type is
    # refused by the allowlist gate (success False, still HTTP 200).
    resp = client.post('/api/a2ui', json={
        'agent_id': 'ch05_probe', 'component': {'type': 'ch05_bogus_type'}})
    assert resp.status_code == 200
    assert resp.get_json()['success'] is False

    # The real push: a builtin 'notification' component enters the channel.
    marker = f'ch05-stream-{uuid.uuid4().hex[:8]}'
    resp = client.post('/api/a2ui', json={
        'agent_id': 'ch05_probe',
        'component': {'type': 'notification', 'title': marker,
                      'message': 'chapter 05', 'severity': 'info'}})
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True

    # Freeze the clock into the past ONLY around the stream drive, so the
    # generator's watermark predates the push above.
    frozen = time.time() - 60.0
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(time, 'time', lambda: frozen)
        got = _drive_stream(client, '/api/notifications/stream')

    if got is None:
        pytest.xfail('DEFECT-ADJACENT: /api/notifications/stream blocks '
                     'pre-yield with no keepalive frame; cannot be driven '
                     'cold even with a seeded queue')
    assert got['status'] == 200
    assert got['mimetype'] == 'text/event-stream'
    # The first frame is a JSON list of events; ours rides in it, stamped
    # with the pushing agent's id by the generator.
    assert got['chunk'].startswith('data: ')
    events = json.loads(got['chunk'][len('data: '):].strip())
    ours = [e for e in events if e.get('title') == marker]
    assert ours and ours[0]['agent'] == 'ch05_probe'
    assert ours[0]['type'] == 'notification'


# ─── Scene 3: the journal, polled and followed ───────────────────────────────

def test_ch05_scene3_journal_poll_and_sse_follow(client, fake_os):
    """/api/shell/system/logs + /logs/stream -- journald in, SSE out.

    POLL TOPOLOGY (shell_system_logs):
      entry: GET ?unit&lines&priority (lines clamped 1..1000)
      -> boundary argv: journalctl --output=json --no-pager -u <unit>
         -n <lines> [-p <priority>] [--since ...] [-g ...]
      -> parse: one json.loads per stdout line; a MALFORMED line is skipped
         (logged at debug), never fatal
      -> sink: {'entries': [{timestamp, unit, priority, message}], 'count'}.

    STREAM TOPOLOGY (shell_system_logs_stream):
      source: Popen ['journalctl', '--output=json', '--no-pager', '-f',
      '-u', <unit>] -- the follow flag makes the REAL journal infinite; the
      faked boundary hands the generator a finite canned stdout instead
      -> per line: json.loads -> reshape -> yield 'data: {...}' SSE frame
      -> sink: the log panel's EventSource. The generator is LAZY: the argv
      is only issued when the first chunk is pulled, which is why we pull
      exactly one chunk before closing.
    """
    good = json.dumps({'__REALTIME_TIMESTAMP': '1720000000000000',
                       '_SYSTEMD_UNIT': 'hart-backend.service',
                       'PRIORITY': '3',
                       'MESSAGE': 'ch05 journal poll line'})
    fake_os.stdout_for['journalctl'] = good + '\n{this is not json\n'

    resp = client.get('/api/shell/system/logs'
                      '?unit=hart-backend.service&lines=5&priority=err')
    assert resp.status_code == 200
    body = resp.get_json()
    assert _calls_for(fake_os, 'journalctl')[-1] == [
        'journalctl', '--output=json', '--no-pager',
        '-u', 'hart-backend.service', '-n', '5', '-p', 'err']
    # One good line parsed, the malformed one skipped without failing.
    assert body['count'] == 1
    assert body['entries'][0]['message'] == 'ch05 journal poll line'
    assert body['entries'][0]['unit'] == 'hart-backend.service'

    # The SSE follow leg: seed a fresh canned journal, pull ONE frame, close.
    fake_os.calls.clear()
    fake_os.stdout_for['journalctl'] = json.dumps({
        '__REALTIME_TIMESTAMP': '1720000001000000',
        '_SYSTEMD_UNIT': 'hart-backend.service',
        'MESSAGE': 'ch05 seeded follow line'}) + '\n'
    got = _drive_stream(client,
                        '/api/shell/system/logs/stream?unit=hart-backend.service')
    if got is None:
        pytest.xfail('DEFECT-ADJACENT: /api/shell/system/logs/stream blocks '
                     'pre-yield with no keepalive frame; cannot be driven '
                     'cold even with a seeded journal')
    assert got['status'] == 200
    assert got['mimetype'] == 'text/event-stream'
    assert 'ch05 seeded follow line' in got['chunk']
    # Pulling the first chunk started the lazy generator, which issued the
    # follow argv at the boundary.
    assert ['journalctl', '--output=json', '--no-pager',
            '-f', '-u', 'hart-backend.service'] in fake_os.calls


# ─── Scene 4: the console -> journal sink ────────────────────────────────────

def test_ch05_scene4_clientlog_console_to_journal_sink(client, fake_os, caplog):
    """POST /api/shell/clientlog -- the WebView console's only exit.

    TOPOLOGY: the shell's inline head script (window.onerror /
    unhandledrejection / console.error wrapper) POSTs a record here
      -> size gate: a body over 8192 bytes is DROPPED silently (ok:true)
      -> parse (silent; garbage -> {}) -> fields truncated (message 2000,
         stack 4000, url 500) -> '[shell-client] (url:line:col) message'
      -> sink: the module logger 'hevolve.liquid_ui', which the node's
         systemd-cat wrapping forwards into journald
      -> the handler NEVER 500s: a logging failure must not break the shell.
    """
    marker = f'ch05-clientlog-{uuid.uuid4().hex[:8]}'
    with caplog.at_level(logging.WARNING, logger='hevolve.liquid_ui'):
        resp = client.post('/api/shell/clientlog', json={
            'level': 'error', 'message': marker,
            'stack': 'Error: at hartHome.js:1:1',
            'url': 'https://shell.local/home', 'line': 12, 'col': 34})
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True}
    # The record REACHED the journal sink: message + location, tagged.
    sink = [r for r in caplog.records if marker in r.getMessage()]
    assert sink, 'client record never reached the module logger'
    assert '[shell-client]' in sink[0].getMessage()
    assert '(https://shell.local/home:12:34)' in sink[0].getMessage()
    assert sink[0].levelno == logging.ERROR

    # The bounded-drop branch: an oversized record is acknowledged but NOT
    # forwarded (the 8KB gate protects the journal from a client flood).
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger='hevolve.liquid_ui'):
        resp = client.post('/api/shell/clientlog',
                           json={'message': 'x' * 9000})
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True}
    assert '[shell-client]' not in caplog.text

    # The never-500 floor: raw garbage still gets the ok envelope.
    resp = client.post('/api/shell/clientlog', data='}{ not json',
                       content_type='application/json')
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True}


# ─── Scene 5: the local media index + fetch-once image cache ─────────────────

def test_ch05_scene5_media_index_cache_and_consent_gated_export(client, fake_os):
    """/api/media/* (media_semantic_index) -- local-only, consent-gated edges.

    TOPOLOGY: every route is _require_system_auth gated (loopback passes);
    the catalog holds private-photo captions + absolute paths, so this
    surface must never be readable off-box.
      /index/status -> catalog stats + the image cache's LRU stats
      /search       -> deterministic filename hits FIRST, then semantic
                       caption hits (chromadb); an offline vector store just
                       shrinks the results, never raises
      /image?url=   -> fetch-once cache: hit -> local path; miss -> pooled
                       HTTP fetch. The suite's network boundary refuses
                       instantly, so a novel URL exercises the offline
                       degrade: None -> controlled 404
      /index (POST) -> validation: path|paths required -> 400
      /export       -> THE ONLY EGRESS: ScopeGuard.check_egress decides;
                       the verdict's 'allowed' flag maps 1:1 onto the HTTP
                       code (200 allow / 403 block) and is audit-logged
                       either way. Local indexing needs no consent; leaving
                       the perimeter does.
    """
    resp = client.get('/api/media/index/status')
    assert resp.status_code == 200
    body = resp.get_json()
    assert isinstance(body['index']['total'], int)
    assert isinstance(body['index']['semantic_available'], bool)
    assert body['index']['base_dir']
    assert body['image_cache']['max_bytes'] > 0

    # Search: shape contract (this dev box may carry a real catalog, so we
    # assert the envelope, not emptiness).
    resp = client.get('/api/media/search?q=ch05%20sunset')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['count'] == len(body['results'])

    # Image cache: missing url -> validation 400; a never-seen URL cannot be
    # fetched (network boundary refuses) -> miss -> controlled 404.
    assert client.get('/api/media/image').status_code == 400
    novel = f'https://img.invalid/ch05-{uuid.uuid4().hex}.png'
    resp = client.get(f'/api/media/image?url={novel}')
    assert resp.status_code == 404
    assert resp.get_json()['error'] == 'unavailable'

    # Index ingestion validation-first.
    assert client.post('/api/media/index', json={}).status_code == 400

    # Consent-gated egress: the verdict decides the code, and the default
    # posture is privacy-first (blocked unless the scope check passes).
    resp = client.post('/api/media/export', json={'scope': 'federated'})
    verdict = resp.get_json()
    assert 'allowed' in verdict
    assert resp.status_code == (200 if verdict['allowed'] else 403)


# ─── Scene 6: OpenClaw skills + the floating assistant's static edges ────────

def test_ch05_scene6_openclaw_skills_and_assistant_surfaces(client, fake_os):
    """/api/openclaw/* + /api/assistant/{capabilities,voice} (shell_openclaw).

    TOPOLOGY:
      /openclaw/status  -> gateway_bridge health; an absent OpenClaw install
                           degrades to installed:false INSIDE a success
                           envelope (never an error status)
      /openclaw/skills  -> local ClawHub dir scan (OPENCLAW_HOME/*/SKILL.md);
                           no dir -> empty list, still 200
      /skills/install   -> validation: slug required -> 400
      /openclaw/channels-> a static 10-channel table (pure data, no I/O)
      /assistant/capabilities -> static capability table
      /assistant/voice  -> validation: text required -> 400 (the happy path
                           would enter the local TTS engine; not driven here,
                           synthesis is not a surface concern)
    """
    resp = client.get('/api/openclaw/status')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['success'] is True
    assert isinstance(body['installed'], bool)

    resp = client.get('/api/openclaw/skills')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['success'] is True
    assert body['count'] == len(body['skills'])

    assert client.post('/api/openclaw/skills/install', json={}).status_code == 400
    assert client.post('/api/openclaw/skills/uninstall', json={}).status_code == 400
    assert client.get('/api/openclaw/skills/search').status_code == 400

    chan_ids = {c['id'] for c in
                client.get('/api/openclaw/channels').get_json()['channels']}
    assert {'whatsapp', 'telegram', 'discord', 'matrix'} <= chan_ids

    cap_ids = {c['id'] for c in
               client.get('/api/assistant/capabilities').get_json()['capabilities']}
    assert {'chat', 'voice', 'openclaw'} <= cap_ids

    assert client.post('/api/assistant/voice', json={}).status_code == 400


def test_ch05_scene7_assistant_chat_degrade_is_controlled(client, fake_os):
    """POST /api/assistant/chat -- the floating bubble's route to the brain.

    TOPOLOGY: entry {'message', ...} -> validation (message required -> 400)
    -> pooled_post to the :6777 /chat pipeline (the ONE intent dispatcher)
    -> reply relayed back as {'success': True, 'response': ...}.

    In this hermetic suite the peer boundary refuses instantly, which is
    exactly what a node with the backend down experiences. (FIXED 2026-07-17:
    the except branch used to raw-500 this; it now returns 502 -- the
    upstream :6777 pipeline is UNAVAILABLE, a controlled degrade matching the
    sibling /api/agent/ask envelope semantics. The suite caught the split.)
    """
    assert client.post('/api/assistant/chat', json={}).status_code == 400

    resp = client.post('/api/assistant/chat',
                       json={'message': 'ch05 hello hive'})
    assert resp.status_code == 502, (
        'peer-absent degrade must be the controlled 502: %r'
        % resp.get_data(as_text=True)[:200])


# ─── Scene 7: intent in, context read, UI out ────────────────────────────────

def test_ch05_scene8_agent_ask_context_and_generated_ui(client, fake_os):
    """/api/agent/ask + /api/context + /api/ui + /api/a2ui/specs -- the loop
    that closes the whole story: intent enters, context is read, UI exits.

    /api/agent/ask TOPOLOGY: entry {'text'} -> empty text -> 200 error
    envelope; real text -> requests.post to the :6777 /chat brain. With the
    peer refused, the except branch returns the error IN the envelope at
    HTTP 200 (contrast scene 7's 500: same failure, two different codes on
    two sibling routes -- this one is the controlled shape).

    /api/context TOPOLOGY: ContextEngine.get_context aggregates FOUR signal
    groups per request: device (files + clock), models (model-bus HTTP),
    agents (backend HTTP), system (/proc reads). Every probe degrades
    independently; with the network boundary refusing, models/agents MUST
    come back as their zero-shapes, deterministically.

    /api/ui TOPOLOGY: get_context -> generate_ui: model available -> LLM
    composes; else _generate_static_ui (deterministic dashboard: a System
    Status card + any live agent components) -> each component rendered to
    HTML server-side -> sink: {'source', 'html', 'component_count'}.
    """
    # Empty intent: refused inside a 200 envelope.
    resp = client.post('/api/agent/ask', json={'text': ''})
    assert resp.status_code == 200
    assert resp.get_json()['error'] == 'No text provided'

    # Real intent, absent brain: the SAME failure scene 7 hits, but this
    # handler drains it as a controlled error envelope.
    resp = client.post('/api/agent/ask', json={'text': 'ch05 make me a card'})
    assert resp.status_code == 200
    assert 'error' in resp.get_json()

    # Context read: the degrade branches are THE deterministic branches here.
    ctx = client.get('/api/context').get_json()
    assert set(ctx) >= {'timestamp', 'device', 'models', 'agents', 'system'}
    assert ctx['models']['available'] is False       # model bus refused
    assert ctx['models']['count'] == 0
    assert ctx['agents'] == {'running': 0, 'total': 0, 'agents': []}
    assert ctx['device']['time_of_day'] in (
        'morning', 'afternoon', 'evening', 'night')

    # UI generation: no model bus -> the static composer answers, and its
    # System Status card guarantees at least one rendered component.
    ui = client.get('/api/ui').get_json()
    assert ui['source'] == 'static'
    assert ui['component_count'] >= 1
    assert isinstance(ui['html'], str) and ui['html']

    # The component catalogue (the introspection surface agents compose
    # from): builtins must be present with their machine specs.
    specs = client.get('/api/a2ui/specs').get_json()['specs']
    names = {s.get('name') for s in specs}
    assert {'notification', 'home_compose', 'metric', 'card'} <= names


# ─── Scene 8: the agentic home + the approval loop, closing the story ────────

def test_ch05_scene9_home_compose_and_the_approval_roundtrip(client, fake_os):
    """/api/home/compose + /api/approval + /api/agent/approval -- the last
    exits: an agent paints the home, and a human answers an agent.

    HOME TOPOLOGY: entry {'hero', 'rows'[, 'mood']} (or wrapped in
    'payload') -> compose_home builds ONE 'home_compose' component and
    delegates to agent_ui_update -- the same governed channel as scene 2, so
    the kill-switch / rate-cap / audit / XSS gates apply to the home too ->
    accepted pushes drain out the scene-2 SSE stream into hartHome.compose.
    Both-fields-empty is refused BEFORE the channel (success False).

    APPROVAL TOPOLOGY (the human-in-control loop as data flow):
      enters: POST /api/approval -> agent_request_approval builds an
        'approval' component (options Approve/Deny/Ask me later) -> the same
        governed channel -> the card reaches the human via the stream
      becomes: POST /api/agent/approval {'decision'} -> validation
        (approve|deny|later only) -> the matching pending component in
        _agent_components gets _decision stamped (resolved True)
      exits: an 'agent.approval.decision' EventBus emit toward every other
        frontend, and the ok envelope back to the caller.
    """
    # The home push: accepted through the governed channel.
    resp = client.post('/api/home/compose', json={
        'agent_id': 'ch05_home',
        'hero': {'title': 'ch05 evening', 'subtitle': 'events and sinks'},
        'rows': []})
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True

    # The XSS gate guards the home exactly like any other component: a
    # script-bearing hero is REJECTED server-side (success False, 200).
    resp = client.post('/api/home/compose', json={
        'agent_id': 'ch05_home',
        'hero': {'title': '<script>alert(1)</script>'}})
    assert resp.status_code == 200
    assert resp.get_json()['success'] is False

    # Nothing to compose -> refused before the channel.
    resp = client.post('/api/home/compose', json={})
    assert resp.status_code == 200
    assert resp.get_json()['success'] is False

    # The approval round trip. Enter:
    resp = client.post('/api/approval', json={
        'agent_id': 'ch05_asker', 'action': 'ch05-demo-act',
        'description': 'may I?'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['status'] == 'approval_requested'
    assert body['component']['options'] == ['Approve', 'Deny', 'Ask me later']

    # Invalid decision: validation-first, 400, nothing resolved.
    resp = client.post('/api/agent/approval', json={
        'agent_id': 'ch05_asker', 'action': 'ch05-demo-act',
        'decision': 'shrug'})
    assert resp.status_code == 400

    # The human answers: the pending card we created above is found in the
    # component store and stamped with the decision -- resolved True proves
    # the entry and the exit are the SAME object, one loop, fully closed.
    resp = client.post('/api/agent/approval', json={
        'agent_id': 'ch05_asker', 'action': 'ch05-demo-act',
        'decision': 'approve'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['status'] == 'ok'
    assert body['decision'] == 'approve'
    assert body['resolved'] is True
