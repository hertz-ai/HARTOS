"""Reel Studio E2E-2 on the desktop: her agent's turns, and what they leave behind.

The plan's E2E-2 (CREATE, then REUSE) needs a running desktop (dev Nunba on
:5000) and her real clips; this script drives the turns and checks the
artifacts.  The preferred transport is her phone's own, the device link
(tests/standalone/reel_device_client.py); the fallback is the one here:
the clips go into her store with ``reel_pipeline_cli.py ingest`` and each
turn is a loopback POST to /chat/custom_gpt with the phone's body (no
intelligence_preference).

  turn   --user U --prompt P --text T [--create] [--attachments rows.json]
         [--request R] [--url http://127.0.0.1:5000] [--bearer TOKEN]
         -> the reply's text and media; the reply is saved under the base
  wait   --user U --prompt P [--request R] [--budget 1800]
         -> polls her outbox until a reel for the turn is there
  check  --user U --prompt P [--xid X]
         -> the banked artifacts, one row each: her agent record exists and
            carries goal_tags reel_studio; no prompts/-1.json was written;
            the reel is in her outbox and hashes to its xid; its media item
            passes contracts.validate_media_item; caption, hashtags and song
            are there; the file itself is 1080x1920 H.264 at 30 fps, silent,
            faststart, as long as the item says.  Exit 0 when every row is ok.

It reads her store and the prompts dir the way the app does, and writes
only under HARTOS_E2E_BASE; it refuses to run without it.  Not collected by
pytest, not shipped.  Nothing here starts or stops the desktop.
"""
import argparse
import json
import os
import sys
import time
import uuid

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)

from tests.standalone.reel_device_client import phone_body  # noqa: E402
from tests.standalone.reel_pipeline_cli import EXIT_NO_REEL, _base, _sha256  # noqa: E402

DEFAULT_URL = 'http://127.0.0.1:5000'
#: The phone waits this long for a chat reply (plan section 11).
TURN_WAIT_S = 150.0
POLL_S = 5.0


def cmd_turn(args) -> int:
    import requests
    base = _base()
    attachments = None
    if args.attachments:
        with open(args.attachments, encoding='utf-8') as f:
            doc = json.load(f)
        attachments = doc.get('attachments') if isinstance(doc, dict) else doc
    rid = args.request or 'e2e%s' % uuid.uuid4().hex[:12]
    body = phone_body(user_id=args.user, prompt_id=args.prompt, text=args.text, request_id=rid,
                      create=args.create, attachments=attachments)
    headers = {'Content-Type': 'application/json'}
    if args.bearer:
        headers['Authorization'] = 'Bearer %s' % args.bearer
    started = time.monotonic()
    try:
        resp = requests.post(args.url.rstrip('/') + '/chat/custom_gpt', json=body,
                             headers=headers, timeout=args.wait)
        status, reply = resp.status_code, resp.json() if resp.content else {}
    except requests.exceptions.Timeout:
        status, reply = None, {'error': 'no reply within %.0f s; wait for the reel' % args.wait}
    except (requests.exceptions.RequestException, ValueError) as e:
        status, reply = None, {'error': str(e)[:300]}
    out = {'request_id': rid, 'status': status, 'seconds': round(time.monotonic() - started, 1),
           'reply': reply}
    folder = os.path.join(base, 'turns')
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, '%s.json' % rid), 'w', encoding='utf-8') as f:
        json.dump({'body': body, 'result': out}, f, indent=2, ensure_ascii=False)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if status == 200 else EXIT_NO_REEL


def _outbox(user, prompt, request=None):
    from core.user_file_store import get_user_file_store
    items = get_user_file_store().list_outbox(user, prompt_id=prompt, unfetched_only=False)
    return [i for i in items if request is None or i['meta'].get('request_id') == str(request)]


def cmd_wait(args) -> int:
    _base()
    deadline = time.monotonic() + args.budget
    while True:
        items = _outbox(args.user, args.prompt, args.request)
        if items:
            print(json.dumps(items[-1], indent=2, ensure_ascii=False))
            return 0
        if time.monotonic() >= deadline:
            print('no reel for prompt %s%s within %.0f s' % (
                args.prompt, ' request %s' % args.request if args.request else '', args.budget))
            return EXIT_NO_REEL
        time.sleep(POLL_S)


def _row(rows, check, ok, value):
    rows.append({'check': check, 'ok': bool(ok), 'value': value})


def _file_rows(rows, path, media):
    """The delivered file's own facts against the hard QA bar's file rows."""
    from integrations.reel_studio.contracts import CANVAS, QUALITY_BAR
    from integrations.reel_studio.media.probe import probe
    from integrations.reel_studio.media.qa import mp4_top_level_boxes
    info = probe(path)
    _row(rows, 'file_size_and_codec', (info.width, info.height) == (CANVAS['w'], CANVAS['h'])
         and info.codec == QUALITY_BAR['codec']['codec']
         and abs(info.fps_avg - QUALITY_BAR['fps']['fps']) < 0.01 and info.rotation == 0,
         {'w': info.width, 'h': info.height, 'codec': info.codec, 'fps': info.fps_avg})
    _row(rows, 'file_silent', not info.has_audio, {'has_audio': info.has_audio})
    _row(rows, 'file_duration', abs(info.duration_s - float(media.get('duration_s') or 0)) <= 0.2,
         {'file_s': info.duration_s, 'media_s': media.get('duration_s')})
    boxes = mp4_top_level_boxes(path)
    _row(rows, 'file_faststart', 'moov' in boxes and 'mdat' in boxes
         and boxes.index('moov') < boxes.index('mdat'), boxes[:6])


def cmd_check(args) -> int:
    _base()
    from core.cache_loaders import load_agent_config
    from core.cache_loaders import PROMPTS_DIR
    from core.user_file_store import get_user_file_store
    from integrations.reel_studio.contracts import validate_media_item
    rows = []
    record = load_agent_config(str(args.prompt)) or {}
    _row(rows, 'agent_record', bool(record), os.path.join(PROMPTS_DIR, '%s.json' % args.prompt))
    tags = record.get('goal_tags') or []
    _row(rows, 'goal_tags_reel_studio', 'reel_studio' in tags, tags)
    _row(rows, 'no_minus_one_record', not os.path.exists(os.path.join(PROMPTS_DIR, '-1.json')),
         os.path.join(PROMPTS_DIR, '-1.json'))
    items = _outbox(args.user, args.prompt)
    item = next((i for i in items if i['xid'] == args.xid), None) if args.xid else \
        (items[-1] if items else None)
    _row(rows, 'outbox_item', item is not None, item and item['xid'])
    if item is not None:
        held = get_user_file_store().resolve(args.user, item['xid'], box='out')
        _row(rows, 'outbox_file_hashes_to_xid', held is not None
             and _sha256(held.path) == item['xid'], held and held.path)
        media = item['meta'].get('media') or {}
        problems = validate_media_item(media)
        _row(rows, 'media_item_valid', not problems, problems[:5])
        _row(rows, 'media_turn_ids', bool(media.get('prompt_id')) and bool(media.get('request_id')),
             {'prompt_id': media.get('prompt_id'), 'request_id': media.get('request_id')})
        _row(rows, 'caption', bool((media.get('caption') or '').strip()), media.get('caption'))
        _row(rows, 'hashtags', bool(media.get('hashtags')), media.get('hashtags'))
        song = media.get('song') or {}
        _row(rows, 'song', bool(song.get('title') or song.get('song_key')), song)
        _row(rows, 'qa_in_item', (media.get('qa') or {}).get('passed') is True, media.get('qa'))
        if held is not None:
            _file_rows(rows, held.path, media)
    ok = all(r['ok'] for r in rows)
    print(json.dumps({'ok': ok, 'rows': rows}, indent=2, ensure_ascii=False, default=str))
    return 0 if ok else EXIT_NO_REEL


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    t = sub.add_parser('turn')
    t.add_argument('--user', required=True)
    t.add_argument('--prompt', required=True)
    t.add_argument('--text', required=True)
    t.add_argument('--create', action='store_true')
    t.add_argument('--attachments', default=None, help='a JSON file of attachment rows')
    t.add_argument('--request', default=None)
    t.add_argument('--url', default=DEFAULT_URL)
    t.add_argument('--bearer', default=None)
    t.add_argument('--wait', type=float, default=TURN_WAIT_S)
    t.set_defaults(func=cmd_turn)
    w = sub.add_parser('wait')
    w.add_argument('--user', required=True)
    w.add_argument('--prompt', required=True)
    w.add_argument('--request', default=None)
    w.add_argument('--budget', type=float, default=1800.0)
    w.set_defaults(func=cmd_wait)
    c = sub.add_parser('check')
    c.add_argument('--user', required=True)
    c.add_argument('--prompt', required=True)
    c.add_argument('--xid', default=None)
    c.set_defaults(func=cmd_check)
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
