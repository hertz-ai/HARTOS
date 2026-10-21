"""Reel Studio E2E-2: her phone's side of the device link, driven from a script.

It speaks to a running desktop (dev Nunba on :5000) the way her phone does,
on the paths the phone uses, so a full agent turn is exercised end to end:

* a real PeerLink device link to ws://<desktop>/peer_link, whose HELLO
  carries a device token signed with the phone's own Ed25519 key (as
  PeerLinkHandshake does on the phone);
* the file door, channel 'file' (core.peer_link.file_channel): file_offer,
  file_put in a window of three, file_commit; file_list, file_get,
  file_fetched for the reel coming back;
* the mobile adapter, channel 'dispatch' (integrations.mobile_adapter): an
  api_request for POST https://<cloud>/chat/custom_gpt with the phone's body
  (no intelligence_preference), answered by the desktop's own route;
* pushes on the link's 'events' channel, printed as they arrive (the reel
  push carries ``media``).

The phone's identity is its own key: HEVOLVE_KEY_DIR must name a key dir
under HARTOS_E2E_BASE, never the desktop's.  The first dial is refused with
'pending' and files the owner's device_access ask on the desktop; once the
owner allows it, dial again.

  token   the phone's public key and a device token for --user
  upload  FILES [--captured-at ms ...] -> the attachment rows (U9 shape)
  turn    --text T --prompt P [--create] [--attachments rows.json] [--request R]
          -> the reply (and any push that arrives while it waits)
  list    [--prompt P] -> the outbox items (file_items)
  fetch   XID --out PATH -> downloads it, checks its sha256, sends file_fetched

Not collected by pytest and not shipped; it refuses to run without
HARTOS_E2E_BASE.  Nothing here starts or stops the desktop.
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)

from tests.standalone.reel_pipeline_cli import (  # noqa: E402
    EXIT_NO_REEL, _base, _captured, _kind, _sha256)

DEFAULT_URL = 'ws://127.0.0.1:5000/peer_link'
#: How long one file frame waits for its answer, and how often it is asked.
REQUEST_TIMEOUT_S = 30.0
ATTEMPTS = 5
#: Puts in flight at once (the store tells the phone its max_inflight).
WINDOW = 3
TOKEN_LIFETIME_S = 30 * 24 * 3600


def _phone_key_dir(base: str) -> str:
    key_dir = os.environ.get('HEVOLVE_KEY_DIR', '').strip()
    if not key_dir or not os.path.abspath(key_dir).startswith(base):
        sys.stderr.write('HEVOLVE_KEY_DIR must name the phone\'s own key dir under %s (e.g. '
                         '%s); with the desktop\'s key the "phone" would be the desktop.\n'
                         % (base, os.path.join(base, 'phone_keys')))
        sys.exit(EXIT_NO_REEL)
    os.makedirs(key_dir, exist_ok=True)
    return key_dir


def device_token(user_id: str, username: str = 'e2e-phone') -> str:
    """A device token as her phone makes one: hive scope, the phone's key in
    node_public_key, node_sig over the claims with that key (verify_device_jwt)."""
    import jwt as pyjwt
    from security.node_integrity import get_public_key_hex, sign_json_payload
    now = int(time.time())
    claims = {'user_id': str(user_id), 'username': username, 'jti': uuid.uuid4().hex,
              'iat': now, 'exp': now + TOKEN_LIFETIME_S, 'type': 'access', 'scope': 'hive',
              'iss': 'hive:hevolve', 'node_public_key': get_public_key_hex()}
    claims['node_sig'] = sign_json_payload(claims)
    # The desktop reads the claims and checks node_sig; the HS256 key is the phone's own.
    return pyjwt.encode(claims, uuid.uuid4().hex, algorithm='HS256')


def _phone_link_class():
    from core.peer_link.link import LinkState, PeerLink

    class PhoneLink(PeerLink):
        """The phone's end: its HELLO carries the device token, signed with
        the rest of the HELLO; the desktop's first answer is kept, so a
        refusal ('pending', 'denied', ...) can be shown."""
        token = ''
        first_answer = None

        def _ws_send(self, data):
            if self._state == LinkState.HANDSHAKING:
                hello = json.loads(data)
                if hello.get('type') == 'hello':
                    from security.node_integrity import sign_json_payload
                    hello.pop('signature', None)
                    hello['device_token'] = self.token
                    hello['signature'] = sign_json_payload(hello)
                    data = json.dumps(hello, separators=(',', ':')).encode('utf-8')
            super()._ws_send(data)

        def _ws_recv(self, timeout: float = 30.0):
            data = super()._ws_recv(timeout)
            if self._state == LinkState.HANDSHAKING and self.first_answer is None and data:
                try:
                    self.first_answer = json.loads(data)
                except (ValueError, TypeError):
                    self.first_answer = {'raw': str(data)[:200]}
            return data

    return PhoneLink


class DeviceClient:
    """One device link to a desktop, and the phone's requests over it."""

    def __init__(self, url: str, user_id: str):
        from core.peer_link.link import TrustLevel
        self.user_id = str(user_id)
        self.link = _phone_link_class()(peer_id='desktop', address=url,
                                         trust=TrustLevel.SAME_USER)
        self.link.token = device_token(self.user_id)
        self.pushes = []
        self._push_lock = threading.Lock()

    def connect(self) -> bool:
        if not self.link.connect():
            answer = self.link.first_answer or {}
            sys.stderr.write('the desktop did not take the link: %s\n' % (
                answer.get('reason') or answer.get('type') or 'no answer'))
            if answer.get('reason') == 'pending':
                sys.stderr.write('allow this phone on the desktop (device access ask), '
                                 'then run again.\n')
            return False
        self.link.on_message('events', self._on_event)
        return True

    def close(self) -> None:
        self.link.close()

    def _on_event(self, channel, data, peer_id):
        payload = data.get('data') if isinstance(data, dict) else None
        if isinstance(payload, dict) and payload.get('media'):
            with self._push_lock:
                self.pushes.append(payload)
            print('PUSH request_id=%s media=%s' % (
                payload.get('request_id'), [m.get('xid') for m in payload['media']]), flush=True)

    def ask(self, channel: str, frame: dict, timeout: float = REQUEST_TIMEOUT_S) -> dict:
        """One request, asked again on a lost answer or 'busy' (always safe)."""
        for _ in range(ATTEMPTS):
            reply = self.link.send(channel, frame, wait_response=True, timeout=timeout)
            busy = reply is not None and (reply.get('type') == 'busy' or reply.get('reason') == 'busy')
            if reply is not None and not busy:
                return reply
            time.sleep(1.0)
        raise RuntimeError('%s %s unanswered after %d tries' % (channel, frame.get('type'),
                                                                 ATTEMPTS))

    # ── the file door ──

    def upload(self, path: str, *, captured_at=None) -> dict:
        path = os.path.abspath(path)
        mime, purpose = _kind(path)
        if mime is None:
            raise ValueError('%s is not a clip, logo or song the store takes' % path)
        size, xid = os.path.getsize(path), _sha256(path)
        offer = {'type': 'file_offer', 'name': os.path.basename(path), 'size': size,
                 'sha256': xid, 'mime': mime, 'purpose': purpose}
        if captured_at:
            offer['captured_at'] = int(captured_at)
        answer = self.ask('file', offer)
        if answer.get('type') == 'file_accept':
            cs = answer['chunk_size']
            wanted = [i for a, b in answer['missing'] for i in range(a, b + 1)]

            def put(i):
                with open(path, 'rb') as f:
                    f.seek(i * cs)
                    part = f.read(cs)
                return self.ask('file', {'type': 'file_put', 'xid': xid, 'i': i,
                                         'b64': base64.b64encode(part).decode('ascii'),
                                         'c': hashlib.sha256(part).hexdigest()})
            with ThreadPoolExecutor(max_workers=min(WINDOW, answer.get('max_inflight', WINDOW))) as w:
                acks = list(w.map(put, wanted))
            bad = [a for a in acks if a.get('type') != 'file_ack']
            if bad:
                raise RuntimeError('%s: chunks refused: %s' % (path, bad[:3]))
            done = self.ask('file', {'type': 'file_commit', 'xid': xid})
            if done.get('type') != 'file_done':
                raise RuntimeError('%s not committed: %s' % (path, done))
        elif answer.get('type') != 'file_have':
            raise RuntimeError('%s refused: %s' % (path, answer))
        row = {'file_id': xid, 'sha256': xid, 'name': os.path.basename(path), 'mime': mime,
               'size': size, 'purpose': purpose}
        if captured_at:
            row['captured_at'] = int(captured_at)
        return row

    def file_list(self, prompt_id=None) -> list:
        frame = {'type': 'file_list'}
        if prompt_id is not None:
            frame['prompt_id'] = str(prompt_id)
        answer = self.ask('file', frame)
        return answer.get('items') or []

    def fetch(self, xid: str, out: str) -> str:
        first = self.ask('file', {'type': 'file_get', 'xid': xid, 'i': 0})
        if first.get('type') != 'file_chunk':
            raise RuntimeError('file_get %s refused: %s' % (xid[:12], first))
        chunks = [first]
        for i in range(1, first['n']):
            chunks.append(self.ask('file', {'type': 'file_get', 'xid': xid, 'i': i}))
        digest = hashlib.sha256()
        with open(out, 'wb') as f:
            for chunk in chunks:
                part = base64.b64decode(chunk['b64'], validate=True)
                if hashlib.sha256(part).hexdigest() != chunk['c']:
                    raise RuntimeError('chunk %d of %s is corrupt' % (chunk['i'], xid[:12]))
                digest.update(part)
                f.write(part)
        if digest.hexdigest() != xid:
            raise RuntimeError('%s reassembled to another sha256' % xid[:12])
        self.ask('file', {'type': 'file_fetched', 'xid': xid})
        return out

    # ── the chat turn, through the mobile adapter ──

    def turn(self, body: dict, timeout: float) -> dict:
        from core.peer_link.channels import API_REQUEST
        from integrations.mobile_adapter.aliases import CLOUD
        frame = {'type': API_REQUEST, 'method': 'POST',
                 'url': 'https://%s/chat/custom_gpt' % CLOUD,
                 'headers': {'Content-Type': 'application/json', 'Accept': 'application/json'},
                 'body_b64': base64.b64encode(json.dumps(body).encode('utf-8')).decode('ascii'),
                 'device_token': self.link.token}
        reply = self.link.send('dispatch', frame, wait_response=True, timeout=timeout)
        if reply is None:
            return {'status': None, 'error': 'no answer within %.0f s (the phone shows a pending '
                                             'bubble; the reel comes by push or file_list)'
                                             % timeout}
        if reply.get('not_here'):
            return {'status': None, 'error': 'the desktop does not serve /chat/custom_gpt'}
        raw = base64.b64decode(reply.get('body_b64') or '')
        try:
            body_out = json.loads(raw)
        except ValueError:
            body_out = raw.decode('utf-8', 'replace')[:2000]
        return {'status': reply.get('status'), 'body': body_out}


def phone_body(*, user_id, prompt_id, text, request_id, create, attachments=None,
               goal_tags=('reel_studio',)) -> dict:
    """The body CustomBotsActivity.doAction sends, with Reel Studio's fields
    (plan WS-12) and no intelligence_preference."""
    body = {'text': [text], 'raw_text': text, 'user_id': str(user_id),
            'teacher_avatar_id': 0, 'expression': 'neutral', 'file_id': 0,
            'conversation_id': 'custom_%s' % prompt_id, 'prompt_name': 'Reel Studio',
            'prompt_id': str(prompt_id), 'device_id': 'e2e-phone', 'create_agent': bool(create),
            'request_id': request_id, 'media_mode': 'text'}
    if create and goal_tags:
        body['goal_tags'] = list(goal_tags)
    if attachments:
        body['attachments'] = list(attachments)
    return body


# ── commands ──────────────────────────────────────────────────────────────

def _client(args) -> DeviceClient:
    client = DeviceClient(args.url, args.user)
    if not client.connect():
        sys.exit(EXIT_NO_REEL)
    return client


def cmd_token(args) -> int:
    from security.node_integrity import get_public_key_hex
    print(json.dumps({'phone_public_key': get_public_key_hex(),
                      'device_token': device_token(args.user)}, indent=2))
    return 0


def cmd_upload(args) -> int:
    client = _client(args)
    try:
        rows = [client.upload(path, captured_at=at)
                for path, at in zip(args.files, _captured(args.captured_at, len(args.files)))]
    finally:
        client.close()
    print(json.dumps({'attachments': rows}, indent=2))
    return 0


def cmd_turn(args) -> int:
    attachments = None
    if args.attachments:
        with open(args.attachments, encoding='utf-8') as f:
            doc = json.load(f)
        attachments = doc.get('attachments') if isinstance(doc, dict) else doc
    rid = args.request or 'e2e%s' % uuid.uuid4().hex[:12]
    body = phone_body(user_id=args.user, prompt_id=args.prompt, text=args.text, request_id=rid,
                      create=args.create, attachments=attachments)
    client = _client(args)
    try:
        started = time.monotonic()
        reply = client.turn(body, timeout=args.wait)
        reply['request_id'] = rid
        reply['seconds'] = round(time.monotonic() - started, 1)
        deadline = time.monotonic() + args.push_wait
        while args.push_wait and time.monotonic() < deadline and not client.pushes:
            time.sleep(2.0)
        reply['pushes'] = client.pushes
    finally:
        client.close()
    print(json.dumps(reply, indent=2, ensure_ascii=False))
    return 0 if reply.get('status') == 200 else EXIT_NO_REEL


def cmd_list(args) -> int:
    client = _client(args)
    try:
        items = client.file_list(args.prompt)
    finally:
        client.close()
    print(json.dumps(items, indent=2, ensure_ascii=False))
    return 0


def cmd_fetch(args) -> int:
    client = _client(args)
    try:
        out = client.fetch(args.xid, os.path.abspath(args.out))
    finally:
        client.close()
    print(out)
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--url', default=DEFAULT_URL, help='the desktop\'s PeerLink endpoint')
    p.add_argument('--user', required=True, help='her user id (the device token\'s user)')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('token').set_defaults(func=cmd_token)
    u = sub.add_parser('upload')
    u.add_argument('files', nargs='+')
    u.add_argument('--captured-at', nargs='+', default=None)
    u.set_defaults(func=cmd_upload)
    t = sub.add_parser('turn')
    t.add_argument('--text', required=True)
    t.add_argument('--prompt', required=True)
    t.add_argument('--create', action='store_true')
    t.add_argument('--attachments', default=None, help='a JSON file of attachment rows')
    t.add_argument('--request', default=None)
    t.add_argument('--wait', type=float, default=150.0, help='seconds the phone waits')
    t.add_argument('--push-wait', type=float, default=0.0,
                   help='then wait this long for a push with media')
    t.set_defaults(func=cmd_turn)
    li = sub.add_parser('list')
    li.add_argument('--prompt', default=None)
    li.set_defaults(func=cmd_list)
    f = sub.add_parser('fetch')
    f.add_argument('xid')
    f.add_argument('--out', required=True)
    f.set_defaults(func=cmd_fetch)
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    _phone_key_dir(_base())
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
