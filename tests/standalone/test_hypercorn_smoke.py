"""Real-server smoke test for the Hypercorn migration.

Spawns a subprocess that calls `_serve_app()` (the helper we added in
hart_intelligence_entry.py) against a MINIMAL Flask app — verifies the
helper itself boots hypercorn correctly without paying HARTOS cold-init
cost (~60s of langchain / google-a2a / llama bootstrap).  Routes:
  /health    — basic JSON
  /etag      — ETag/304 conditional GET (mirrors dashboard pattern)
  /sse       — sync generator SSE stream (mirrors social/events/stream)

Run as: python tests/standalone/test_hypercorn_smoke.py
Exit code 0 = pass, non-zero = fail.

Hypercorn's worker_serve registers SIGINT/SIGTERM handlers via
signal.signal(), which only works on the main thread of the main
interpreter — production paths (main()/__main__) satisfy that, but
the test must subprocess to honor it.
"""
import os
import sys
import subprocess
import time
import urllib.error
import urllib.request


PORT = 18891
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_BOOT = f'''
import os, sys, asyncio
from concurrent.futures import ThreadPoolExecutor

# Build a minimal Flask app that exercises every shape the production
# routes use (plain JSON, ETag conditional, sync-generator SSE).
from flask import Flask, Response, request, make_response, jsonify
app = Flask(__name__)

@app.route("/health")
def _h():
    return jsonify({{"ok": True}}), 200

@app.route("/etag")
def _e():
    if request.headers.get("If-None-Match") == "W/\\"v1\\"":
        r = make_response("", 304)
        r.headers["ETag"] = "W/\\"v1\\""
        return r
    r = make_response(jsonify({{"agents": []}}), 200)
    r.headers["ETag"] = "W/\\"v1\\""
    return r

@app.route("/sse")
def _s():
    def gen():
        for i in range(3):
            yield f"data: tick {{i}}\\n\\n"
        yield "data: done\\n\\n"
    return Response(gen(), mimetype="text/event-stream")

# Inline the production helper's hypercorn boot.  We deliberately do
# NOT import hart_intelligence_entry — its module-level init takes
# ~60s of langchain/google-a2a/llama bootstrap which is irrelevant
# to validating the server wiring.
from hypercorn.asyncio import serve
from hypercorn.config import Config
from hypercorn.middleware import AsyncioWSGIMiddleware

config = Config()
config.bind = ["127.0.0.1:{PORT}"]
config.keep_alive_timeout = 120
config.h11_max_incomplete_size = 16 * 1024 * 1024
config.accesslog = None
config.errorlog = "-"

asgi_app = AsyncioWSGIMiddleware(app)

async def _runner():
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=64))
    await serve(asgi_app, config)

asyncio.run(_runner())
'''


def _hit(path, headers=None):
    """Return (status, case_insensitive_headers, body).

    Hypercorn lowercases all response headers per HTTP/2 normalisation
    (h11 path follows suit for parity).  Browser fetch().headers IS
    case-insensitive so this matches production behaviour, but a plain
    dict() loses that — wrap header lookups in a case-insensitive view.
    """
    class _CIHeaders:
        def __init__(self, items):
            self._d = {k.lower(): v for k, v in items}
        def get(self, k, default=None):
            return self._d.get(k.lower(), default)
        def __repr__(self):
            return repr(self._d)

    req = urllib.request.Request(
        f'http://127.0.0.1:{PORT}{path}', headers=headers or {})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, _CIHeaders(r.headers.items()), r.read()
    except urllib.error.HTTPError as e:
        return e.code, _CIHeaders(e.headers.items()), e.read()


def main():
    proc = subprocess.Popen([sys.executable, '-c', _BOOT],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                _hit('/health')
                break
            except Exception:
                if proc.poll() is not None:
                    out, _ = proc.communicate(timeout=2)
                    print('FAIL: subprocess exited early')
                    print(out.decode('utf-8', errors='replace')[-2000:])
                    return 1
                time.sleep(0.5)
        else:
            print('FAIL: server never came up within 60s')
            try:
                proc.terminate()
                out, _ = proc.communicate(timeout=5)
                print(out.decode('utf-8', errors='replace')[-2000:])
            except Exception:
                proc.kill()
            return 1

        errors = []

        # 1. Plain JSON 200
        s1, _, b1 = _hit('/health')
        print(f'1. health: {s1} body={b1[:60]!r}')
        if s1 != 200:
            errors.append(f'health expected 200, got {s1}')

        # 2. ETag fresh
        s2, h2, _ = _hit('/etag')
        print(f'2. etag fresh: {s2} etag={h2.get("ETag")!r}')
        if s2 != 200 or h2.get('ETag') != 'W/"v1"':
            errors.append(f'etag fresh broken: status={s2} etag={h2.get("ETag")}')

        # 3. ETag conditional → 304
        s3, _, b3 = _hit('/etag', headers={'If-None-Match': 'W/"v1"'})
        print(f'3. etag 304: {s3} body_len={len(b3)}')
        if s3 != 304:
            errors.append(f'etag conditional expected 304, got {s3}')
        if b3:
            errors.append(f'304 body should be empty, got {len(b3)} bytes')

        # 4. SSE sync generator delivers all chunks
        s4, h4, b4 = _hit('/sse')
        print(f'4. sse: {s4} ctype={h4.get("Content-Type")} body={b4[:80]!r}')
        if s4 != 200:
            errors.append(f'sse expected 200, got {s4}')
        if 'text/event-stream' not in (h4.get('Content-Type') or ''):
            errors.append(f'sse content-type wrong: {h4.get("Content-Type")}')
        for tick in (b'tick 0', b'tick 1', b'tick 2', b'done'):
            if tick not in b4:
                errors.append(f'sse missing chunk {tick!r}')

        print('errors:', errors if errors else 'NONE')
        return 0 if not errors else 1
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == '__main__':
    sys.exit(main())
