"""Egress has one rule, and every leg that leaves the node asks it.

Owner rulings 2026-09-26: egress is a message that goes to OTHER people's
nodes, and it is scrubbed only there; local records stay raw; security must
not partition the hive.  Review of 19d4c5b02 and bd92deac4 measured four
places where that did not hold:

1. ``MessageBus._route_local`` emits ``bus.<topic>`` on the EventBus, and the
   EventBus WAMP bridge published it verbatim to ``com.hartos.event.bus.<topic>``
   -- a URI naming no user -- while the MessageBus's own Crossbar leg carried
   the scrubbed copy.  (Reviewer probe: the Crossbar leg redacted, the
   EventBus WAMP leg carried the raw email address; a chat.response
   'SECRET-CHAT' left the same way.)
2. The scrub covered a fixed list of eight field names: 'reply', 'caption',
   a nested 'body_text', a tuple and publish_async's {'raw': ...} wrapper
   all went out raw.
3. The "what may leave, and to where" logic had a third copy beside
   security.edge_privacy and secret_redactor.
4. ``hart_intelligence_entry.publish_async`` published straight to Crossbar,
   past the bus scrub.

Plus bd92deac4 review F2 (the ownership rule answered False for every
concrete per-user URI) and F3 (two answers to "who is this event for").

Real MessageBus, real EventBus, real edge_privacy / DLP / secret redactor,
and the real publish_async source.  Mocked boundaries: the two WAMP
sessions (recording fakes), the Crossbar HTTP client, the SSE broker.
"""
import ast
import asyncio
import re
import json
import logging
import os
import sys
import threading
import types
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.peer_link.message_bus import MessageBus, reset_message_bus  # noqa: E402
from core.platform import events as ev  # noqa: E402
from core.platform.events import EventBus  # noqa: E402
from core.platform.registry import get_registry, reset_registry  # noqa: E402

EMAIL = 'a.person@example.com'
PHONE = '415-555-0199'
PROMPT_ID = '9876543210'            # 10 digits: the DLP phone pattern eats it
PEER_URL = 'http://203.0.113.7:6777/api'   # the DLP ip pattern eats its host
API_KEY = 'sk-ant-' + 'A1b2C3d4E5' * 5     # secret_redactor anthropic_key


# ── fakes for the two WAMP legs ────────────────────────────────────────────

class _EventBusSession:
    """The EventBus bridge's WAMP session (async publish on its own loop)."""

    def __init__(self):
        self.published = []

    async def publish(self, uri, payload):
        self.published.append((uri, payload))


class _CrossbarSession:
    """hartos.crossbar_server.wamp_session: MessageBus._route_crossbar hands
    publish()'s return value to asyncio.ensure_future."""

    def __init__(self):
        self.published = []
        self._loop = asyncio.new_event_loop()

    def publish(self, uri, payload):
        self.published.append((uri, payload))
        done = self._loop.create_future()
        done.set_result(None)
        return done


@pytest.fixture()
def legs(monkeypatch, tmp_path):
    """Both WAMP legs recording, the EventBus registered, emits synchronous."""
    monkeypatch.setenv('HEVOLVE_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('HEVOLVE_USER_ID', raising=False)
    reset_registry()
    reset_message_bus()
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    ebus_session = _EventBusSession()
    ebus = EventBus()
    ebus._wamp_session = ebus_session
    ebus._wamp_loop = loop
    ebus._wamp_connected = True
    get_registry().register('events', lambda: ebus, singleton=True)
    cb_session = _CrossbarSession()
    monkeypatch.setitem(sys.modules, 'hartos.crossbar_server',
                        types.SimpleNamespace(wamp_session=cb_session))
    monkeypatch.setattr(ev, 'emit_event',
                        lambda t, d=None, async_=True: ebus.emit(t, d))
    monkeypatch.setattr(ev, 'broadcast_sse_safe', lambda *a, **k: False)
    yield types.SimpleNamespace(ebus=ebus, ebus_session=ebus_session,
                                cb=cb_session, loop=loop)
    loop.call_soon_threadsafe(loop.stop)
    reset_registry()
    reset_message_bus()


def _drain(loop):
    for _ in range(3):
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(5)


def _no_link_manager():
    return patch('core.peer_link.link_manager.get_link_manager',
                 side_effect=RuntimeError('no peerlink in this test'))


# ── 1. the EventBus WAMP bridge never carries the bus echo ─────────────────

def test_a_bus_publish_leaves_only_on_its_own_scrubbed_crossbar_leg(legs):
    local = []
    legs.ebus.on('bus.community.message', lambda t, d: local.append(dict(d)))
    with _no_link_manager():
        MessageBus().publish(
            'community.message',
            {'community_id': 'c1', 'text': f'mail me at {EMAIL} or {PHONE}'},
            user_id='u1')
        MessageBus().publish('chat.response', {'text': 'SECRET-CHAT'},
                             user_id='u-1', skip_crossbar=True)
    _drain(legs.loop)

    bridged = [u for u, _ in legs.ebus_session.published]
    assert not [u for u in bridged if u.startswith('com.hartos.event.bus.')], bridged
    assert 'SECRET-CHAT' not in json.dumps(legs.ebus_session.published)
    assert EMAIL not in json.dumps(legs.ebus_session.published)

    assert len(legs.cb.published) == 1
    uri, payload = legs.cb.published[0]
    assert uri == 'com.hertzai.hevolve.community.c1'
    sent = json.loads(payload)
    assert sent['text'] == 'mail me at [EMAIL_REDACTED] or [PHONE_REDACTED]'
    assert sent['community_id'] == 'c1' and sent['user_id'] == 'u1'
    # the in-process echo is the local record: raw
    assert local and local[0]['text'] == f'mail me at {EMAIL} or {PHONE}'


def test_node_internal_topics_never_bridge_and_everything_else_still_does(legs):
    """bus.* and channel.* are node-internal; peer gossip and theme keep their
    WAMP leg (other HARTOS nodes subscribe to com.hartos.event.peer.*)."""
    legs.ebus.emit('channel.registered', {'name': 'discord'})
    legs.ebus.emit('bus.task.confirmation', {'user_id': 'u1', 'text': EMAIL})
    legs.ebus.emit('peer.capability.announce',
                   {'peer_id': 'p1', 'endpoint': PEER_URL})
    legs.ebus.emit('theme.changed', {'theme': 'aurora'})
    _drain(legs.loop)
    uris = [u for u, _ in legs.ebus_session.published]
    assert uris == ['com.hartos.event.peer.capability.announce',
                    'com.hartos.event.theme.changed']
    # gossip is not scrubbed: the endpoint reaches peers intact
    assert legs.ebus_session.published[0][1]['endpoint'] == PEER_URL


# ── 2. the scrub is structural ─────────────────────────────────────────────

def test_every_content_leaf_is_scrubbed_whatever_its_key():
    from security.edge_privacy import scrub_for_egress
    original = {
        'reply': f'call {PHONE}',
        'caption': EMAIL,
        'message': {'author': 'x', 'body_text': EMAIL},
        'text': (EMAIL,),
        'raw': f'{EMAIL} {API_KEY}',
        'items': [{'note': PHONE}, 7, None, True],
    }
    before = json.dumps(original, sort_keys=True)
    out = scrub_for_egress(original)
    assert out['reply'] == 'call [PHONE_REDACTED]'
    assert out['caption'] == '[EMAIL_REDACTED]'
    assert out['message'] == {'author': 'x', 'body_text': '[EMAIL_REDACTED]'}
    assert out['text'] == ('[EMAIL_REDACTED]',) and isinstance(out['text'], tuple)
    assert EMAIL not in out['raw'] and API_KEY not in out['raw']
    assert out['items'] == [{'note': '[PHONE_REDACTED]'}, 7, None, True]
    assert json.dumps(original, sort_keys=True) == before   # never mutated
    assert scrub_for_egress(f'bare {EMAIL}') == 'bare [EMAIL_REDACTED]'


def test_identifier_and_routing_keys_travel_byte_identical():
    from security.edge_privacy import scrub_for_egress
    ids = {
        'user_id': 'u1', 'prompt_id': PROMPT_ID, 'request_id': PROMPT_ID,
        'uid': PROMPT_ID, 'msg_id': PROMPT_ID, 'peer_url': PEER_URL,
        'endpoint': PEER_URL, 'url': PEER_URL, 'signature': 'ab' * 32,
        'relay_path': ['203.0.113.7'], 'task_type': 'async',
        'timestamp': 1.5, 'created_at': '2026-09-26T10:00:00',
        'nested': {'id': PROMPT_ID, 'text': PHONE},
    }
    out = scrub_for_egress(ids)
    assert out['nested']['text'] == '[PHONE_REDACTED]'
    out['nested']['text'] = PHONE
    assert out == ids


def test_unlisted_keys_are_scrubbed_on_the_bus_crossbar_leg(legs):
    body = {'community_id': 'c1', 'prompt_id': PROMPT_ID,
            'reply': f'call {PHONE}', 'caption': EMAIL,
            'message': {'body_text': EMAIL}}
    with _no_link_manager():
        MessageBus().publish('community.message', body, user_id='u1')
    sent = json.loads(legs.cb.published[0][1])
    assert sent['reply'] == 'call [PHONE_REDACTED]'
    assert sent['caption'] == '[EMAIL_REDACTED]'
    assert sent['message'] == {'body_text': '[EMAIL_REDACTED]'}
    assert sent['prompt_id'] == PROMPT_ID


# ── 4. publish_async goes through the same rule ────────────────────────────

class _Client:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload):
        self.published.append((topic, payload))


class _Inline:
    def submit(self, fn, *a, **k):
        fn(*a, **k)


def _publish_async(client):
    """The real publish_async, extracted from hart_intelligence_entry.py
    (that module cannot be imported in a unit test) and bound to fakes for
    its module globals: the Crossbar HTTP client and the executor."""
    src = open(os.path.join(_ROOT, 'hart_intelligence_entry.py'),
               encoding='utf-8').read()
    functions = [n for n in ast.parse(src).body
                 if isinstance(n, ast.FunctionDef)
                 and n.name in ('publish_async', '_http_crossbar_publish')]
    ns = {'json': json, 'os': os, 'client': client, 'logging': logging,
          '_crossbar_client_lock': threading.Lock(),
          'crossbar_executor': _Inline(), 'app': MagicMock()}
    exec(compile(ast.Module(body=functions, type_ignores=[]),
                 'hart_intelligence_entry.py', 'exec'), ns)
    return ns['publish_async']


def test_publish_async_scrubs_a_topic_other_people_subscribe_to(legs):
    client = _Client()
    with _no_link_manager():
        _publish_async(client)('com.hertzai.longrunning.log', {
            'user_id': 'u1', 'uid': PROMPT_ID, 'status': 'INITIALIZED',
            'text': f'mail {EMAIL}'})
    assert len(client.published) == 1
    topic, payload = client.published[0]
    sent = json.loads(payload)
    assert topic == 'com.hertzai.longrunning.log'
    assert sent['text'] == 'mail [EMAIL_REDACTED]'
    assert sent['uid'] == PROMPT_ID and sent['status'] == 'INITIALIZED'


def test_publish_async_keeps_a_non_json_message_a_string(legs):
    client = _Client()
    with _no_link_manager():
        _publish_async(client)('com.hertzai.hevolve.pupitpublish',
                               f'say call {PHONE}')
    assert client.published == [('com.hertzai.hevolve.pupitpublish',
                                  'say call [PHONE_REDACTED]')]


def test_publish_async_sends_the_users_own_topic_byte_identical(legs):
    client = _Client()
    raw = json.dumps({'user_id': 'u-1', 'text': f'mail {EMAIL}'})
    with _no_link_manager():
        _publish_async(client)('com.hertzai.hevolve.chat.u-1', raw)
    assert client.published == [('com.hertzai.hevolve.chat.u-1', raw)]


def test_publish_async_withholds_what_it_could_not_scrub(legs, caplog):
    client = _Client()
    with _no_link_manager(), \
            patch('security.edge_privacy.scrub_for_egress',
                  side_effect=RuntimeError('dlp broken')), \
            caplog.at_level(logging.WARNING, logger='hevolve_security'):
        _publish_async(client)('com.hertzai.longrunning.log',
                               {'user_id': 'u1', 'text': EMAIL})
    assert client.published == []
    assert any('dlp broken' in r.getMessage() for r in caplog.records)


# ── bd92 F2: one ownership rule, templates and concrete URIs ───────────────

def test_the_ownership_rule_answers_for_concrete_uris():
    from security.edge_privacy import crossbar_uri_is_per_user as own
    assert own('com.hertzai.hevolve.chat.{user_id}') is True
    assert own('com.hertzai.hevolve.community.{community_id}') is False
    assert own('com.hertzai.hevolve.chat.u-1', 'u-1') is True
    assert own('com.hertzai.hevolve.chat.new.u-1', 'u-1') is True
    assert own('com.hertzai.hevolve.chat.u-1', 'u-2') is False
    assert own('com.hertzai.hevolve.chat.u-11', 'u-1') is False
    assert own('com.hertzai.hevolve.community.c1', 'u-1') is False
    assert own('com.hartos.event.agent.ui.update', 'u-1') is False
    # a declared per-user URI is its user's even when the payload names nobody
    assert own('com.hertzai.hevolve.chat.u-1', '') is True
    # review F6: a last segment equal to the user id is not enough -- only a
    # DECLARED per-user template makes a URI one user's
    assert own('com.hertzai.hevolve.community.u-1', 'u-1') is False
    assert own('com.hertzai.hevolve.game.u-1', 'u-1') is False
    assert own('com.hartos.event.tts.speak.u-1', 'u-1') is False
    # a declared SHARED URI is nobody's, though chat.general's template
    # 'com.hertzai.hevolve.{user_id}' would also match it
    assert own('com.hertzai.hevolve.confirmation', '') is False
    assert own('com.hertzai.hevolve.chat.new', '') is False
    # the catch-all chat.general template: its instance for the NAMED user
    # is theirs; it attributes nobody when no user is named
    assert own('com.hertzai.hevolve.u9', 'u9') is True
    assert own('com.hertzai.hevolve.u9', '') is False


def test_a_one_person_event_bridges_only_onto_its_owners_uri(legs, monkeypatch):
    """The rule is unpatched; only its DATA grows: once a per-user bridge URI
    is declared, a card on its own user's URI bridges raw, on anyone else's
    it is withheld.  Undeclared (today), no card bridges at all."""
    from core.peer_link import message_bus as mb
    legs.ebus.emit('agent.ui.update.u-1', {'user_id': 'u-1', 'code': 'K7Q2'})
    _drain(legs.loop)
    assert legs.ebus_session.published == []
    monkeypatch.setattr(mb, 'PER_USER_TOPICS_OUTSIDE_BUS', (
        *mb.PER_USER_TOPICS_OUTSIDE_BUS,
        'com.hartos.event.agent.ui.update.{user_id}'))
    legs.ebus.emit('agent.ui.update.u-1', {'user_id': 'u-1', 'code': 'K7Q2'})
    legs.ebus.emit('agent.ui.update.u-1', {'user_id': 'u-2', 'code': 'K7Q2'})
    legs.ebus.emit('agent.ui.update', {'user_id': 'u-1', 'code': 'K7Q2'})
    _drain(legs.loop)
    assert [(u, p['code']) for u, p in legs.ebus_session.published] == [
        ('com.hartos.event.agent.ui.update.u-1', 'K7Q2')]


# ── bd92 F3: one answer to "who is this event for" ─────────────────────────

def test_one_table_says_who_an_event_is_for():
    assert ev.topic_audience('bus.chat.response') == ev.AUDIENCE_NODE
    assert ev.topic_audience('channel.registered') == ev.AUDIENCE_NODE
    assert ev.topic_audience('agent.ui.update') == ev.AUDIENCE_ONE_PERSON
    assert ev.topic_audience('community.feed') == ev.AUDIENCE_EVERYONE
    assert ev.topic_audience('agent.action.completed') == ev.AUDIENCE_ADDRESSED
    classes = (ev._NODE_INTERNAL_TOPIC_PREFIXES, ev._ONE_PERSON_TOPIC_PREFIXES,
               ev._SSE_GLOBAL_PREFIXES)
    for i, a in enumerate(classes):
        for b in classes[i + 1:]:
            clash = [(x, y) for x in a for y in b
                     if x.startswith(y) or y.startswith(x)]
            assert clash == [], 'a prefix answers two ways: %r' % clash


def test_realtime_never_treats_a_one_person_topic_as_public():
    """realtime listed 'agent.' as public; the one table says 'agent.ui.' is
    one person's, so it must name its publisher like any per-user topic."""
    from integrations.social.realtime import _authorize_topic_for_user_id as ok
    assert ok('agent.ui.update', 'u-1') is False
    assert ok('agent.ui.update.u-1', 'u-1') is True
    # review of a4ea04651 F4: 'agent.' is not everyone's either (no
    # publish_event caller uses it); only the one table decides
    assert ok('agent.lifecycle.started', '') is False
    assert ok('com.hertzai.hevolve.social.u-1', 'u-1') is True
    assert ok('com.hertzai.hevolve.social.u-1', 'u-2') is False


# ── the transit policy lives in one place ──────────────────────────────────

def test_every_crossbar_leg_asks_the_one_policy(legs):
    """Owner delegation 2026-09-27: a per-user topic transiting a router is
    NOT egress.  Were that policy ever flipped, crossbar_leg_is_users_own is
    the one place; all three Crossbar legs follow it."""
    client = _Client()
    with patch('security.edge_privacy.crossbar_leg_is_users_own',
               return_value=False), _no_link_manager():
        MessageBus().publish('chat.social', {'text': EMAIL}, user_id='u-1',
                             skip_peerlink=True)
        _publish_async(client)('com.hertzai.hevolve.chat.u-1',
                               {'user_id': 'u-1', 'text': EMAIL})
        legs.ebus.emit('agent.ui.update.u-1', {'user_id': 'u-1', 'code': 'K'})
    _drain(legs.loop)
    assert json.loads(legs.cb.published[0][1])['text'] == '[EMAIL_REDACTED]'
    assert json.loads(client.published[0][1])['text'] == '[EMAIL_REDACTED]'
    assert legs.ebus_session.published == []


def test_the_users_own_topic_goes_raw_through_the_router(legs):
    with _no_link_manager():
        MessageBus().publish('chat.social', {'text': EMAIL}, user_id='u-1',
                             skip_peerlink=True)
    uri, payload = legs.cb.published[0]
    assert uri == 'com.hertzai.hevolve.social.u-1'
    assert json.loads(payload)['text'] == EMAIL


# ── 3. the other egress sites route through the same scrub ─────────────────

def test_redact_for_scope_scrubs_nested_content_and_keeps_ids():
    from security.edge_privacy import PrivacyScope, ScopeGuard
    data = {'_privacy_scope': PrivacyScope.FEDERATED, 'prompt_id': PROMPT_ID,
            'peer_url': PEER_URL, 'meta': {'note': f'mail {EMAIL}'}}
    out = ScopeGuard().redact_for_scope(data, PrivacyScope.FEDERATED)
    assert out['prompt_id'] == PROMPT_ID and out['peer_url'] == PEER_URL
    assert out['meta'] == {'note': 'mail [EMAIL_REDACTED]'}


def test_redact_experience_redacts_secrets_in_every_content_leaf():
    from security.secret_redactor import redact_experience
    exp = {'prompt': 'short', 'response': '', 'model_id': 'm',
           'attribution_chain': [{'observation': f'used key {API_KEY}'}],
           'escalation_reason': f'leaked {API_KEY}'}
    out = redact_experience(exp)
    assert API_KEY not in json.dumps(out)
    assert out['model_id'] == 'm'


# ── the guard that keeps it at one ─────────────────────────────────────────

_SCAN_DIRS = ('core', 'security', 'integrations', 'hartos')
_CANONICAL = os.path.join('security', 'edge_privacy.py')
_AUDIENCE_HOME = os.path.join('core', 'platform', 'events.py')
_DLP_HOME = os.path.join('security', 'dlp_engine.py')
_CLASSIFIER_TABLE = re.compile(
    r'(PUBLIC|GLOBAL|ONE_PERSON|NODE_INTERNAL)\w*(PREFIX|TOPIC)'
    r'|BLOCKLIST|PRIVATE_FIELDS|CONTENT_FIELDS|IDENTIFIER_KEY')
_REDACTORS = {'redact', 'redact_secrets', 'scrub_text', 'scrub_for_egress',
              'redact_fields', 'map_content'}


def _sources():
    yield os.path.join(_ROOT, 'hart_intelligence_entry.py')
    for d in _SCAN_DIRS:
        for base, dirs, files in os.walk(os.path.join(_ROOT, d)):
            dirs[:] = [x for x in dirs if x != '__pycache__']
            for f in files:
                if f.endswith('.py'):
                    yield os.path.join(base, f)


def _called_names(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            yield f.id if isinstance(f, ast.Name) else getattr(f, 'attr', '')


def test_source_guard_the_egress_rule_has_one_home():
    """A second copy of 'what may leave, and to where' fails here:
      * a membership test on the literal '{user_id}' (the template rule),
      * an .endswith(f'.{user_id}') suffix test (the concrete rule),
      * a recursive payload walker that calls a redactor,
      * a scrubber that calls the DLP engine's redact directly (review:
        claude_code_backend's DLP-only copy let sk-ant-... through),
      * a table answering "whose is this topic" or "which fields may go"
        (*PUBLIC*PREFIX*, *GLOBAL*PREFIX*, *BLOCKLIST*, *CONTENT_FIELDS*,
        ...; review: realtime / tenant_acl each had their own),
    anywhere but security/edge_privacy.py (tables: also
    core/platform/events.py, which owns topic_audience)."""
    found = []
    for path in _sources():
        rel = os.path.relpath(path, _ROOT)
        if rel == _CANONICAL:
            continue
        try:
            tree = ast.parse(open(path, encoding='utf-8').read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Compare)
                    and isinstance(node.left, ast.Constant)
                    and node.left.value == '{user_id}'
                    and any(isinstance(o, ast.In) for o in node.ops)):
                found.append((rel, node.lineno, "'{user_id}' in"))
            if (isinstance(node, ast.Call)
                    and getattr(node.func, 'attr', '') == 'endswith'
                    and node.args and isinstance(node.args[0], ast.JoinedStr)):
                parts = node.args[0].values
                if (len(parts) == 2 and isinstance(parts[0], ast.Constant)
                        and parts[0].value in ('.', '/')
                        and isinstance(parts[1], ast.FormattedValue)
                        and 'user' in ast.unparse(parts[1].value)):
                    found.append((rel, node.lineno, 'user-suffix rule'))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls = set(_called_names(node))
                walks_dicts = any(
                    isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'isinstance'
                    and len(n.args) == 2 and 'dict' in ast.unparse(n.args[1])
                    for n in ast.walk(node))
                if node.name in calls and walks_dicts and calls & _REDACTORS:
                    found.append((rel, node.lineno, 'redacting walker ' + node.name))
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and rel != _DLP_HOME):
                calls = set(_called_names(node))
                if {'get_dlp_engine', 'redact'} <= calls:
                    found.append((rel, node.lineno, 'DLP scrubber ' + node.name))
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and rel != _AUDIENCE_HOME:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    name = getattr(t, 'id', '')
                    if _CLASSIFIER_TABLE.search(name):
                        found.append((rel, node.lineno, name))
    assert found == [], 'a second egress rule: %r' % found


# ── review of a4ea04651 ────────────────────────────────────────────────────
#
# F1: the bridge sent every ADDRESSED topic raw on com.hartos.event.<topic>
#     (reviewer probe rv_probe_e: tts.speak, outreach.prospect_replied,
#     perception.watcher.fired).
# F2: the scrub rewrote protocol values (rv_probe_c: addresses, versions,
#     10-digit numbers, base64 keys).
# F3: claude_code_backend had a DLP-only scrubber.
# F4: realtime / tenant_acl had their own public-topic tables.
# F5: publish_async's owner was not pinned by a payload that names nobody.
# F6: any last segment equal to the user id counted as the user's URI.

def test_every_bridged_event_is_scrubbed_unless_the_uri_is_its_users(legs):
    local = []
    legs.ebus.on('tts.speak', lambda t, d: local.append(dict(d)))
    legs.ebus.emit('tts.speak', {'user_id': 'u-1',
                                 'text': f'remind me: call {PHONE}, mail {EMAIL}'})
    legs.ebus.emit('outreach.prospect_replied', {
        'prospect_id': 1, 'email': 'ceo@acme.com',
        'body_preview': f'my cell is {PHONE}'})
    legs.ebus.emit('perception.watcher.fired', {
        'user_id': 'u-1', 'condition': f'see my password {API_KEY}',
        'action': 'x'})
    legs.ebus.emit('system.health.snapshot', {'node_id': PROMPT_ID, 'cpu': 5})
    _drain(legs.loop)

    got = {u: p for u, p in legs.ebus_session.published}
    assert set(got) == {'com.hartos.event.tts.speak',
                        'com.hartos.event.outreach.prospect_replied',
                        'com.hartos.event.perception.watcher.fired',
                        'com.hartos.event.system.health.snapshot'}
    blob = json.dumps(legs.ebus_session.published)
    for raw in (PHONE, EMAIL, 'ceo@acme.com', API_KEY):
        assert raw not in blob, raw
    assert got['com.hartos.event.tts.speak']['text'] == \
        'remind me: call [PHONE_REDACTED], mail [EMAIL_REDACTED]'
    assert got['com.hartos.event.outreach.prospect_replied']['prospect_id'] == 1
    assert got['com.hartos.event.system.health.snapshot']['node_id'] == PROMPT_ID
    # the in-process record is raw
    assert local[0]['text'] == f'remind me: call {PHONE}, mail {EMAIL}'


def test_peer_gossip_crosses_the_bridge_byte_identical(legs):
    """Scrubbing the bridge must not partition the hive: the capability
    advert's endpoint, attestation and model facts survive untouched."""
    advert = {
        'peer_id': '8f3a1c9e2b7d4f60', 'endpoint': PEER_URL,
        'auth_token': 'c0ffee' * 8,
        'origin_attestation': {'signature': 'ab' * 32,
                               'x25519_public': 'q2+/4155550199/Zx9yA=',
                               'address': '203.0.113.7:6777',
                               'hart_version': '2026.9.27.1'},
        'models': [{'name': 'qwen3.5-4b', 'build': '1.4.0.12',
                    'vram_bytes': '8589934592'}],
        'announced_at': 1727430000.5,
    }
    legs.ebus.emit('peer.capability.announce', json.loads(json.dumps(advert)))
    _drain(legs.loop)
    (uri, sent), = legs.ebus_session.published
    assert uri == 'com.hartos.event.peer.capability.announce'
    sent = dict(sent)
    sent.pop('msg_id')
    assert sent == advert


def test_protocol_values_survive_and_person_values_do_not():
    from security.edge_privacy import scrub_for_egress
    protocol = {
        'type': 'announce', 'node_id': '8f3a1c9e2b7d4f60', 'url': PEER_URL,
        'public_key': '3fa91234567890' + 'ab' * 25,
        'x25519_public': 'q2+/4155550199/Zx9yA=', 'address': '203.0.113.7:6777',
        'lan_ip': '192.168.1.42', 'peer_urls': ['http://198.51.100.9:6777'],
        'hart_version': '2026.9.27.1', 'build': '1.4.0.12',
        'sig': 'MEUCIQ/4155550199/+x', 'nonce': '4155550199',
        'hostname': 'msi-203-0-113-7',
        'vram_bytes': '8589934592', 'phone_like_count': '1234567890',
        'artifact': 'sha256:' + '12' * 32, 'date': '2026-09-27',
    }
    assert scrub_for_egress(protocol) == protocol
    person = scrub_for_egress({
        'api_key': API_KEY, 'private_key': API_KEY,
        'contact_email': EMAIL, 'phone': '4155550199', 'mobile': 'call me',
        'note': PHONE, 'reply': f'my number is 4155550199 {EMAIL}',
        # a bare ip in content is personal data (review of d89d50223 F1)
        'q': '10.1.2.3',
        'author': {'name': 'a', 'email': EMAIL, 'voice_profile': 'v1'},
        # an identifier SUFFIX never exempts a contact or secret key
        'email_address': EMAIL, 'recovery_token_hash': API_KEY,
    })
    blob = json.dumps(person)
    for raw in (API_KEY, EMAIL, PHONE, '4155550199', 'call me', 'v1',
                '10.1.2.3'):
        assert raw not in blob, raw
    assert person['author'] == {'name': 'a'}


def test_the_copilot_prompt_uses_the_one_scrub():
    from integrations.coding_agent.claude_code_backend import _scrub_for_egress
    out = _scrub_for_egress(
        f'use {API_KEY} with password=hunter2secret and mail {EMAIL}')
    for raw in (API_KEY, 'hunter2secret', EMAIL):
        assert raw not in out, raw
    assert _scrub_for_egress('') == ''


def test_publish_and_subscribe_gates_ask_the_one_classifier():
    from integrations.social.realtime import _authorize_topic_for_user_id as pub
    from integrations.social.tenant_acl import authorize_subscribe as sub
    # once "public", now per-user like any addressed topic
    for topic in ('tts.audio_ready', 'game.s1', 'admin.broadcast',
                  'presence.online', 'dm.c1', 'agent.lifecycle.a1'):
        assert pub(topic, 'u-1') is False, topic
        assert sub(topic, {'user_id': 'u-1'}) is False, topic
    # everyone's (topic_audience EVERYONE): the measured callers
    assert pub('setup_progress', '') is True
    assert pub('social.post.p1.vote', '') is True
    assert pub('community.feed', '') is True
    assert sub('community.feed', {'user_id': 'u-1'}) is True
    assert sub('community.feed', {}) is False
    # the publisher's own per-user bus topic
    assert pub('chat.social', 'u-1') is True
    assert pub('chat.social', '') is False
    assert sub('chat.social', {'user_id': 'u-1'}) is True
    # a concrete topic naming the user
    assert sub('com.hertzai.hevolve.social.u-1', {'user_id': 'u-1'}) is True
    assert sub('com.hertzai.hevolve.social.u-2', {'user_id': 'u-1'}) is False


def test_the_real_thinking_envelope_reaches_its_own_user_raw(legs):
    """crossbar_publish.publish_thinking_trace sends an envelope that names
    no user; on the user's own chat URI it goes byte-identical."""
    from core.peer_link.crossbar_publish import publish_thinking_trace
    client = _Client()
    with _no_link_manager(), patch('core.safe_hartos_attr.safe_hartos_attr',
                                   return_value=_publish_async(client)):
        assert publish_thinking_trace(text=f'mail {EMAIL}', user_id='u-1',
                                      request_id=PROMPT_ID) is True
    (topic, payload), = client.published
    assert topic == 'com.hertzai.hevolve.chat.u-1'
    sent = json.loads(payload)
    assert 'user_id' not in sent and sent['text'] == [f'mail {EMAIL}']


def test_publish_async_asks_with_the_user_the_payload_names(legs):
    """The bus leg stamps the topic suffix into the payload; for
    channel.response.<uid> that suffix is 'channel.response.<uid>', not a
    user.  The egress owner is the user the PAYLOAD names: nobody -> the
    URI's own user (raw); someone else -> scrubbed."""
    client = _Client()
    reply = json.dumps({'text': [f'mail {EMAIL}'], 'action': 'ChannelResponse'})
    with _no_link_manager():
        pa = _publish_async(client)
        pa('com.hertzai.hevolve.channel.response.u-1', reply)
        pa('com.hertzai.hevolve.chat.u-1', {'user_id': 'u-2', 'text': EMAIL})
        pa('com.hertzai.hevolve.chat.10077', {'user_id': 10077, 'text': EMAIL})
    assert client.published[0] == (
        'com.hertzai.hevolve.channel.response.u-1', reply)
    assert json.loads(client.published[1][1])['text'] == '[EMAIL_REDACTED]'
    assert json.loads(client.published[2][1])['text'] == EMAIL


def test_a_community_named_like_its_user_is_still_other_peoples(legs):
    with _no_link_manager():
        MessageBus().publish('community.message',
                             {'community_id': 'u1', 'text': EMAIL},
                             user_id='u1', skip_peerlink=True)
    uri, payload = legs.cb.published[0]
    assert uri == 'com.hertzai.hevolve.community.u1'
    assert json.loads(payload)['text'] == '[EMAIL_REDACTED]'


# ── review of d89d50223 (egress half rejected) ────────────────────────────
#
# F1: the shape exemption let whole phone numbers / ips through as content
#     ({'text': '4155550199'}) -- shapes exempt ONLY under identifier keys.
# F2: identifier keys exempted any value ('ip': an email, 'commit': an
#     email, 'home_address': a street) -- the value's shape is checked too.
# F3: a secret-keyed value no pattern recognised went raw.
# F4: a failed scrub on the bridge must withhold, never publish {}.
# F5: EVERYONE-class topics became publishable by anyone.
# F7: an undeclared com.hertzai.hevolve.<x> was user <x>'s (catch-all).

@pytest.mark.parametrize('value', [
    '4155550199', '+14155550199', '415.555.0199', '(415)555-0199',
    '415-555-0199', '73.22.101.5', 'call:4155550199',
    'user:john.doe@example.com', '4111111111111111',
])
def test_a_whole_token_of_personal_data_in_content_is_scrubbed(value):
    from security.edge_privacy import scrub_for_egress
    for payload in ({'text': value}, {'reply': [value]}, value):
        out = json.dumps(scrub_for_egress(payload))
        assert value not in out, (value, out)


def test_identifier_keys_exempt_only_values_shaped_like_their_key():
    from security.edge_privacy import scrub_for_egress
    raw = {
        'ip': 'jane@acme.io', 'lan_ip': 'call 4155550199',
        'commit': 'jane@acme.io', 'build': 'my email is jane@acme.io',
        'version': 'call 4155550199', 'host': 'john@example.com',
        'status': 'jane@acme.io phone 4155550199',
        'reply_to_address': 'jane@acme.io',
        'home_address': '221B Baker Street, j@x.io',
        'address': '1 Main St, Springfield',
    }
    blob = json.dumps(scrub_for_egress(raw))
    for v in ('jane@acme.io', 'john@example.com', '4155550199',
              '221B Baker Street', '1 Main St'):
        assert v not in blob, v
    shaped = {
        'ip': '73.22.101.5', 'lan_ip': '192.168.1.42',
        'peer_ip': 'fe80::1ff:fe23:4567:890a', 'address': '203.0.113.7:6777',
        'commit': 'a4ea046510b898eb26d9337dd0e6b145be031a6f',
        'build': '1.4.0.12', 'hart_version': '2026.9.27.1',
        'guardrail_hash': 'c0ffee' * 10, 'checksum': 'sha256:' + 'bb' * 32,
        'prompt_id': PROMPT_ID, 'request_id': PROMPT_ID, 'nonce': '4155550199',
        'status': 'INITIALIZED', 'url': PEER_URL, 'hostname': 'msi-203-0-113-7',
        'vram_bytes': '8589934592',
    }
    assert scrub_for_egress(shaped) == shaped


def test_a_secret_keyed_value_no_pattern_knows_is_withheld():
    """M6: secret-named keys win over identifier suffixes, and a value no
    secret pattern recognises is withheld whole, not sent raw."""
    from security.edge_privacy import scrub_for_egress
    out = scrub_for_egress({
        'api_key': 'AIzaSyShort123', 'auth_token': 'abc123def456',
        'db_secret': 'hunter2hunter2', 'password': 'correcthorse',
        'token': 'none',
    })
    blob = json.dumps(out)
    for v in ('AIzaSyShort123', 'abc123def456', 'hunter2hunter2',
              'correcthorse'):
        assert v not in blob, v
    assert out['token'] == 'none'          # a status word, not a secret


def test_the_capability_advert_keeps_its_auth_token_for_peers():
    """The one explicit exception: the gossip advert's auth_token is FOR
    peers (they call the endpoint with it); it travels beside the signed
    origin_attestation, and only there."""
    from security.edge_privacy import scrub_for_egress
    advert = {'peer_id': 'p1', 'endpoint': PEER_URL,
              'auth_token': 'Zx9-token-abc123',
              'origin_attestation': {'node_signature': 'ab' * 32}}
    assert scrub_for_egress(advert) == advert
    assert scrub_for_egress({'auth_token': 'Zx9-token-abc123'}) != {
        'auth_token': 'Zx9-token-abc123'}


def test_a_failed_scrub_on_the_bridge_publishes_nothing(legs):
    """M4: a failed scrub is withheld; the bridge never publishes {}."""
    with patch('security.edge_privacy.scrub_for_egress',
               side_effect=RuntimeError('dlp broken')):
        legs.ebus.emit('tts.speak', {'user_id': 'u-1', 'text': EMAIL})
        legs.ebus.emit('theme.changed', {'theme': 'aurora'})
    _drain(legs.loop)
    assert legs.ebus_session.published == []


def test_only_the_measured_server_broadcasts_are_publishable_without_a_user():
    """F5: EVERYONE-class topics are subscribable by any signed-in user, but
    the realtime publish gate takes only what server code publishes with no
    user (measured publish_event callers): community.*, vote scores,
    setup_progress, and the node-infra feeds that were public before."""
    from integrations.social.realtime import _authorize_topic_for_user_id as pub
    from integrations.social.tenant_acl import authorize_subscribe as sub
    for topic in ('hive.x', 'public.x', 'federation.x', 'app.x',
                  'resource.x'):
        assert pub(topic, '') is False, topic
        assert pub(topic, 'u-1') is False, topic
        assert sub(topic, {'user_id': 'u-1'}) is True, topic
        assert sub(topic, {}) is False, topic
    for topic in ('community.feed', 'community.message', 'setup_progress',
                  'social.post.p1.vote', 'system.health', 'model.loaded',
                  'catalog.updated'):
        assert pub(topic, '') is True, topic


def test_an_undeclared_uri_under_the_catch_all_is_nobodys():
    """F7: 'com.hertzai.hevolve.{user_id}' is the catch-all; a URI only it
    matches is not attributed to a user, so it is scrubbed as shared."""
    from security.edge_privacy import crossbar_uri_is_per_user as own
    from security.edge_privacy import per_user_uri_owner
    assert per_user_uri_owner('com.hertzai.hevolve.intermediate2') == ''
    assert own('com.hertzai.hevolve.somethingnew', '') is False
    assert own('com.hertzai.hevolve.somethingnew', 'u1') is False
    # declared, more specific templates still name their user
    assert per_user_uri_owner('com.hertzai.hevolve.chat.u-1') == 'u-1'
    assert per_user_uri_owner('com.hertzai.hevolve.social.u-1') == 'u-1'


def test_publish_async_scrubs_an_undeclared_catch_all_uri(legs):
    client = _Client()
    with _no_link_manager():
        _publish_async(client)('com.hertzai.hevolve.somethingnew',
                               {'text': f'mail {EMAIL}'})
    assert json.loads(client.published[0][1])['text'] == 'mail [EMAIL_REDACTED]'


# ── the remaining raw leaks, closed in the ONE pattern home ───────────────
# (dlp_engine.PII_PATTERNS / secret_redactor._SECRET_PATTERNS; scrub_text
# only calls them)

@pytest.mark.parametrize('text, raw', [
    ('my box is at 2001:db8::8a2e:370:7334 today', '2001:db8::8a2e:370:7334'),
    ('fe80::1ff:fe23:4567:890a', 'fe80::1ff:fe23:4567:890a'),
    ('full 2001:0db8:85a3:0000:0000:8a2e:0370:7334 here',
     '2001:0db8:85a3:0000:0000:8a2e:0370:7334'),
    ('write john.doe%40example.com', 'john.doe%40example.com'),
    ('https://x.io/u?email=john.doe%40example.com', 'john.doe%40example.com'),
    ('reach john.doe(at)example.com', 'john.doe(at)example.com'),
    ('reach john.doe [at] example.com', 'example.com'),
    ('password=hunter2', 'hunter2'),
    ('pwd=abc', '=abc'),
    ('pass:xyz9', 'xyz9'),
    ('my password: hunter2 ok', 'hunter2'),
    ('DB_PASSWORD=s3cr3t', 's3cr3t'),
    ('https://api.example.com/v1/data?key=AIzaSyShort123', 'AIzaSyShort123'),
    ('https://api.example.com/v1/data?x=1&api_key=abc123', 'abc123'),
    ('phone_4155550199', '4155550199'),
    ('node-4155550199.hive.local', '4155550199'),
])
def test_the_one_scrub_closes_the_remaining_raw_leaks(text, raw):
    from security.edge_privacy import scrub_for_egress, scrub_text
    assert raw not in scrub_text(text), text
    assert raw not in json.dumps(scrub_for_egress({'reply': text})), text


def test_the_new_patterns_keep_protocol_and_plain_text():
    from security.edge_privacy import scrub_for_egress, scrub_text
    kept = {'version': '1.4.0.12', 'build': '2026.9.27.1',
            'hart_version': '3.5.4.1', 'peer_ip': 'fe80::1ff:fe23:4567:890a',
            'request_id': 'task_1234567890'}
    assert scrub_for_egress(kept) == kept
    for plain in ('meet at 12:30:45 today', 'std::vector<int> works',
                  'the pass rate was high', 'password=none',
                  'you pass: nothing', 'hash deadbeefcafe'):
        assert scrub_text(plain) == plain, plain


# ── review of the c072913e4 / d2c9f6b59 / df0e35e0a egress set ───────────
#
# 1 (CRITICAL): *_id keys exempted spaceless values, so hive.signal.* with
#   sender_id = a phone number went raw to every node.
# 2: chat.general's own instance for its user was scrubbed (two answers).
# 3: a phone / email / ip span inside a spaceless token was kept whole.
# 4: the email pattern was quadratic.
# 5: secret words matched as substrings (tokenizer, max_tokens withheld).

@pytest.mark.parametrize('key, value', [
    ('sender_id', '+14155550199'), ('wa_id', '14155550199'),
    ('caller_id', '+14155550199'), ('contact_id', '4155550199'),
    ('recipient_id', 'jane@icloud.com'), ('client_ip', '73.22.101.5'),
    ('remote_ip', '73.22.101.5'), ('mobile_ip', '73.22.101.5'),
    ('status', '4155550199'),
    ('url', 'https://x.io/call?to=+14155550199'),
    ('phone_hash', '4155550199'), ('address', '4155550199'),
    ('home_address', 'Springfield'),
    ('recovery_token_hash', API_KEY),
])
def test_a_person_handle_never_rides_an_identifier_key(key, value):
    from security.edge_privacy import scrub_for_egress
    out = scrub_for_egress({key: value})[key]
    assert out != value, (key, value)
    for part in ('4155550199', 'jane@icloud.com', '73.22.101.5',
                 'Springfield', API_KEY):
        if part in value:
            assert part not in out, (key, out)


def test_hive_signal_events_carry_a_pseudonym_not_the_sender():
    """The emitter pseudonymises (salted per node); the egress scrub then
    withholds even that on legs to other nodes."""
    from integrations.channels.hive_signal_bridge import HiveSignalBridge
    import types as _t
    msg = _t.SimpleNamespace(id='m1', sender_id='+14155550199',
                             sender_name='Jane', is_group=False,
                             channel='signal', content='hello')
    seen = []
    with patch('core.platform.events.emit_event',
               side_effect=lambda t, d=None, **k: seen.append((t, d))):
        b = HiveSignalBridge()
        b._emit_signal_event(msg, ['SENTIMENT'], 'signal')
        b._emit_spark_event(msg, ['SENTIMENT'], 'signal')
    assert [t for t, _ in seen] == ['hive.signal.received', 'hive.signal.spark']
    ids = {d['sender_id'] for _, d in seen}
    assert len(ids) == 1                       # stable: one sender, one ref
    (ref,) = ids
    assert ref and '4155550199' not in ref and '+1' not in ref
    other = types.SimpleNamespace(**dict(vars(msg), sender_id='+14155550100'))
    with patch('core.platform.events.emit_event',
               side_effect=lambda t, d=None, **k: seen.append((t, d))):
        HiveSignalBridge()._emit_signal_event(other, ['SENTIMENT'], 'signal')
    assert seen[-1][1]['sender_id'] != ref     # different sender, different ref


def test_the_hive_signal_bridge_payload_never_carries_the_phone(legs):
    legs.ebus.emit('hive.signal.received', {
        'message_id': 'm1', 'channel': 'signal',
        'sender_id': '+14155550199', 'signals': ['x'], 'is_group': False,
        'timestamp': 1.0})
    _drain(legs.loop)
    assert '4155550199' not in json.dumps(legs.ebus_session.published)


def test_chat_general_is_its_users_own_and_nobody_elses(legs):
    """One rule, one answer: the template is per-user, so its instance for
    the message's user is that user's (raw); an undeclared URI with no user
    named is nobody's (scrubbed)."""
    from core.peer_link.message_bus import crossbar_topic_is_per_user
    from security.edge_privacy import crossbar_leg_is_users_own as leg
    assert crossbar_topic_is_per_user('chat.general') is True
    assert leg('com.hertzai.hevolve.u1', 'u1') is True
    assert leg('com.hertzai.hevolve.u1', 'u2') is False
    assert leg('com.hertzai.hevolve.u1', '') is False
    assert leg('com.hertzai.hevolve.confirmation', 'confirmation') is False
    with _no_link_manager():
        MessageBus().publish('chat.general', {'text': f'mail {EMAIL}'},
                             user_id='u1', skip_peerlink=True)
    uri, payload = legs.cb.published[0]
    assert uri == 'com.hertzai.hevolve.u1'
    assert json.loads(payload)['text'] == f'mail {EMAIL}'


@pytest.mark.parametrize('value, raw', [
    ('https://wa.me/14155550199', '14155550199'),
    ('{"phone":"4155550199","n":1}', '4155550199'),
    ('ip=73.22.101.5;port=6777', '73.22.101.5'),
    ('my-number-is-4155550199', '4155550199'),
    ('call_me_at_4155550199_tonight', '4155550199'),
    ('contact:+14155550199,x', '4155550199'),
    ('https://example.org/a/b/c/d/e/f/g/h?contact=jo@x.io', 'jo@x.io'),
])
def test_a_span_of_personal_data_inside_one_token_is_redacted(value, raw):
    """M21 / half-coverage: no opaque-token allowance in content -- the
    matched span is redacted, whatever the rest of the token is."""
    from security.edge_privacy import scrub_text
    assert raw not in scrub_text(value), value


def test_the_email_pattern_is_linear():
    import time
    from security.dlp_engine import get_dlp_engine
    dlp = get_dlp_engine()
    for text in ('1.2.' * 25000, 'a' * 100000, 'a.' * 50000 + '@'):
        t0 = time.perf_counter()
        dlp.redact(text)
        assert time.perf_counter() - t0 < 2.0, text[:10]


@pytest.mark.parametrize('key, value', [
    ('tokenizer', 'llama-bpe'), ('token_type', 'Bearer'),
    ('tokens_used', '512'), ('token_count', '12'),
    ('credential_type', 'oauth'), ('cookie_policy', 'strict'),
    ('secret_name', 'HF_TOKEN'), ('private_mode', 'on'),
    ('max_tokens', '4096'),
])
def test_secret_words_match_whole_words_not_substrings(key, value):
    from security.edge_privacy import scrub_for_egress
    assert scrub_for_egress({key: value}) == {key: value}


def test_the_pseudonym_is_salted_per_node():
    """Two nodes (two social secret keys) give one sender two references,
    so no node can correlate another's; one node gives it one."""
    from security.edge_privacy import pseudonym
    with patch('core.platform_paths.read_social_secret_key',
               return_value='A' * 40):
        a1 = pseudonym('+14155550199', 'hive.signal.sender')
        a2 = pseudonym('+14155550199', 'hive.signal.sender')
        other_purpose = pseudonym('+14155550199', 'something.else')
    with patch('core.platform_paths.read_social_secret_key',
               return_value='B' * 40):
        b1 = pseudonym('+14155550199', 'hive.signal.sender')
    assert a1 == a2 and a1 != b1 and a1 != other_purpose
    assert pseudonym('', 'hive.signal.sender') == ''


def test_a_scrubbed_routing_key_withholds_the_leg_loudly(legs, monkeypatch, caplog):
    """A URI built from an UNCLASSIFIED routing key whose value the scrub
    alters would route nowhere; the leg is withheld with a WARNING, never
    sent silently broken (and never sent raw)."""
    from core.peer_link import message_bus as mb
    monkeypatch.setitem(mb.TOPIC_MAP, 'test.room', 'com.hertzai.hevolve.room.{room}')
    bus = MessageBus()
    with _no_link_manager(), caplog.at_level(logging.WARNING,
                                             logger='hevolve_security'):
        bus.publish('test.room', {'room': '4155550199', 'text': 'hi'},
                    user_id='u1', skip_peerlink=True)
        bus.publish('test.room', {'room': 'lobby', 'text': f'mail {EMAIL}'},
                    user_id='u1', skip_peerlink=True)
    assert [u for u, _ in legs.cb.published] == ['com.hertzai.hevolve.room.lobby']
    assert json.loads(legs.cb.published[0][1])['text'] == 'mail [EMAIL_REDACTED]'
    assert bus.get_stats()['egress_withheld'] == 1
    assert any('routing key(s) room' in r.getMessage() for r in caplog.records)


def test_a_secret_never_rides_an_id_or_a_password_hash():
    from security.edge_privacy import scrub_for_egress
    out = scrub_for_egress({'session_id': API_KEY,
                            'password_hash': 'a1b2c3d4e5f6a7b8c9d0'})
    assert API_KEY not in json.dumps(out)
    assert out['password_hash'] == '[SECRET_REDACTED]'
