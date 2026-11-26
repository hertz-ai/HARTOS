"""Live AvatarForcing stream: a face speaking a reply, generated while it plays.

    POST   /session    JSON {audio_url, image_url} -> {sid, stream: /live.mp4?sid=..., frames, fps}
                       the reply's speech and the avatar's portrait, as file: URLs of files on this
                       machine (HARTOS is the caller). Answers at once; generation starts when the
                       first viewer connects and stops when the last one leaves (or on DELETE).
    GET    /live.mp4   ?sid=... that reply's stream: ONE fragmented MP4, H.264 + AAC, each video
                       frame out about 40 ms after its block exists. A browser plays it through
                       Media Source Extensions; a phone's player (ExoPlayer) plays the URL as is.
    DELETE /live.mp4   ?sid=... stop generating that reply (its viewer left).
    GET    /stats      JSON: per-block generation time vs its 400 ms budget, latency, throughput.
    GET    /health     {ok, ready, running}
    With --demo only:  GET /live.mp4 without sid (the --ref/--avatar_audio demo, with a burned-in
                       timing line) and GET /view (its page).

Loopback only: it binds 127.0.0.1, answers only a Host of 127.0.0.1/localhost on its own port (a
web page cannot reach it by DNS rebinding), sends no CORS headers (no other origin may read it;
Nunba relays the stream to the page and to phones) and takes POST/DELETE only as JSON requests (a
cross-origin page cannot send one without the preflight it never gets).

    .venv/Scripts/python.exe stream_server.py --demo --ref data/teacher63.png --port 18765
"""
import argparse
import hashlib
import json
import os
import queue
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import av
import cv2
import numpy as np
import torch
from omegaconf import OmegaConf

# HERE: the engine (lowvram/, these scripts). ROOT: the AvatarForcing checkout it runs on (models/,
# configs/, pretrained_dir/, data/) -- the same folder in a dev checkout; HARTOS ships the engine and
# names the model folder (AVATARFORCING_MODEL_DIR). The engine is first on the path.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("AVATARFORCING_MODEL_DIR") or HERE
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.chdir(ROOT)

from lowvram.engine import LowVRAMAvatar  # noqa: E402
from lowvram.session import stream_blocks  # noqa: E402
from realtime_lowvram import FPS, SR, SPF, FaceCropper, load_audio, load_video_25fps, to_tensor  # noqa: E402

STATE = {"session": 0, "blocks": [], "running": False, "started": None}
GEN_LOCK = threading.Lock()
GPU_RUN = threading.Lock()  # the engine runs one session at a time


class LiveBuffer:
    """The one running stream: generation appends fMP4 bytes here; every HTTP client reads from it.

    A browser re-requests a <video> source mid-stream (and probes it); each request joins the same
    stream instead of restarting generation, and a client leaving never touches the muxer."""

    def __init__(self):
        self.chunks, self.done, self.cv = [], False, threading.Condition()
        self.viewers, self.cancel = 0, threading.Event()  # nobody left watching -> stop generating
        self.complete = False  # the muxer closed the file: every frame and the audio are in chunks

    def write(self, b):  # PyAV's file-like sink
        if b:
            with self.cv:
                self.chunks.append(bytes(b))
                self.cv.notify_all()
        return len(b)

    def flush(self):
        pass

    def finish(self):
        with self.cv:
            self.done = True
            self.cv.notify_all()


LIVE = {"buf": None, "generating": None}
SESSIONS = {}   # sid -> one reply: its audio, its agent's face, its stream
KEEP_SESSIONS = 16  # a finished reply's stream can be fetched again (a player that reconnects)
MAX_SESSION_S = 300  # the stream is kept in memory (~0.3 MB/s): a reply longer than this is cut
REF_CACHE = OrderedDict()  # (portrait path, mtime, size) -> cropped reference (one face detection each)
#: A finished reply's whole stream, by (portrait version, audio digest): the same words in the same
#: face (a conversation repeats its short phrases: a greeting, praise, "try again") are replayed
#: from memory at once, with no GPU.  Only a complete stream is kept; the oldest goes first.
STREAM_MEMO = OrderedDict()
STREAM_MEMO_BYTES = 64 * 2**20
MEMO_LOCK = threading.Lock()
REF_CACHE_SIZE = 16


def _local_path(url):
    """The file a file: URL names. Only local files: the caller (HARTOS, on this machine) hands
    over the reply's audio and the avatar's portrait; nothing here fetches from a network."""
    if not isinstance(url, str) or not url.startswith("file:"):
        raise ValueError("only file: URLs are accepted")
    path = urllib.request.url2pathname(urllib.parse.urlparse(url).path)
    if os.name == "nt" and path.startswith("\\") and len(path) > 2 and path[2] == ":":
        path = path[1:]  # file:///C:/x -> \C:\x
    if not os.path.isfile(path):
        raise ValueError("no such file")
    return path


def face_reference(ctx, path):
    """The cropped reference of a portrait: the face detection runs once per image version."""
    st = os.stat(path)
    key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    ref = REF_CACHE.get(key)
    if ref is not None:
        REF_CACHE.move_to_end(key)
        return ref
    img = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("unreadable portrait")
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    box = ctx["cropper"].box(img)
    face = (FaceCropper.crop(img, *box) if box else
            cv2.resize(img, (512, 512), interpolation=cv2.INTER_AREA))  # no face found: whole image
    ref = REF_CACHE[key] = to_tensor(face).unsqueeze(0)
    while len(REF_CACHE) > REF_CACHE_SIZE:
        REF_CACHE.popitem(last=False)
    return ref


def _memo_key(image, audio):
    """(portrait version, audio digest): a re-saved portrait or other words never match."""
    face = None
    if image:
        st = os.stat(image)
        face = (os.path.abspath(image), st.st_mtime_ns, st.st_size)
    return face, hashlib.sha1(np.ascontiguousarray(audio, dtype=np.float32).tobytes()).hexdigest()


def remember_stream(key, chunks):
    """Keep a complete stream for replay; one reply never takes more than a quarter of the room."""
    size = sum(len(c) for c in chunks)
    if key is None or not size or size > STREAM_MEMO_BYTES // 4:
        return
    with MEMO_LOCK:
        STREAM_MEMO[key] = (list(chunks), size)
        STREAM_MEMO.move_to_end(key)
        total = sum(s for _, s in STREAM_MEMO.values())
        while total > STREAM_MEMO_BYTES and len(STREAM_MEMO) > 1:
            _, (_, s) = STREAM_MEMO.popitem(last=False)
            total -= s


def remembered_stream(key):
    """A finished buffer holding the remembered stream, or None."""
    with MEMO_LOCK:
        hit = STREAM_MEMO.get(key)
        if hit is None:
            return None
        STREAM_MEMO.move_to_end(key)
    buf = LiveBuffer()
    buf.chunks, buf.complete = list(hit[0]), True
    buf.finish()
    return buf


def new_session(ctx, audio_url, image_url):
    """A spoken reply: its TTS audio (already in the avatar's voice) and the avatar's portrait.

    Answers at once (the audio decode is milliseconds); the portrait's face crop runs when the
    stream starts, in the generation thread, so the caller's reply is never held up by it."""
    audio = load_audio(_local_path(audio_url))
    if audio is None or not len(audio):
        raise ValueError("no audio in the file")
    audio = audio[:MAX_SESSION_S * SR]
    image = _local_path(image_url) if image_url else None
    sid = uuid.uuid4().hex[:12]
    key = _memo_key(image, audio)
    SESSIONS[sid] = {"sid": sid, "audio": audio, "image": image, "memo": key,
                     "T": int(np.ceil(len(audio) * FPS / SR)),
                     "buf": remembered_stream(key)}  # heard before in this face: no generation
    for old in list(SESSIONS)[:-KEEP_SESSIONS]:
        SESSIONS.pop(old, None)
    return SESSIONS[sid]
CRLF = bytes([13, 10])


def overlay(frame, text):
    f = np.ascontiguousarray(frame)
    h = f.shape[0]
    s = h / 512
    cv2.rectangle(f, (0, h - int(30 * s)), (f.shape[1], h), (0, 0, 0), -1)
    cv2.putText(f, text, (int(8 * s), h - int(10 * s)), cv2.FONT_HERSHEY_SIMPLEX, 0.5 * s, (255, 255, 255), max(1, int(s)), cv2.LINE_AA)
    return f


# One fragmented MP4 carrying video AND audio, so one URL plays anywhere (a browser's MSE, Android's
# ExoPlayer). Fragments are cut by duration, not per frame: with audio in the file, frag_every_frame
# gave duplicate and skipped video timestamps (186 of 250 steps wrong), which stalls a player.
# frag_duration 40 ms + max_interleave_delta 1 + flush_packets: 0 of 250 video steps wrong, audio
# continuous, each frame readable one frame after it is encoded (tests_lowvram/fmp4_timestamps.py).
MUX = {"movflags": "empty_moov+default_base_moof", "frag_duration": "40000",
       "max_interleave_delta": "1", "flush_packets": "1"}


def generate(ctx, buf, session, t_req, sess=None):
    """Run one session; every frame and its audio go into buf (one fMP4) as soon as they exist.

    pace=False (default): the speech audio already exists (a TTS reply), so generation runs ahead of
    playback. pace=True: inputs appear at wall-clock speed, as from a live microphone and camera."""
    GPU_RUN.acquire()
    try:  # everything that holds the GPU lock, so nothing can leave it held or a viewer waiting
        if buf.cancel.is_set():  # its viewers left while it waited for the GPU
            return
        LIVE["generating"] = buf
        if sess is not None:  # a reply: the avatar's face speaking its TTS audio, nobody on camera
            ref = face_reference(ctx, sess["image"]) if sess["image"] else ctx["ref"]
            audio, user_audio, user_frame, T = sess["audio"], np.zeros_like(sess["audio"]), None, sess["T"]
        else:                 # the /view demo
            audio, user_audio, user_frame, T, ref = ctx["avatar_audio"], ctx["user_audio"], ctx["user_frame"], ctx["T"], ctx["ref"]
        if ctx.get("cur_ref") is not ref:  # switch the engine to this session's face
            ctx["cur_ref"] = None  # a switch that fails half way is redone next time
            ctx["eng"].set_reference(ref)
            ctx["cur_ref"] = ref
        STATE.update(running=True, blocks=[], started=time.time(), ttff_ms=None, total_frames=T,
                     t0=time.perf_counter())
        demo = sess is None  # only the demo burns its timing line into the picture
        _generate_blocks(ctx, buf, session, t_req, audio, user_audio, user_frame, T, demo)
        if (sess is not None and buf.complete and not buf.cancel.is_set()
                and STATE.get("frames_sent", 0) >= T):
            remember_stream(sess.get("memo"), buf.chunks)
    except Exception as e:  # never let a generation error take the server down
        sys.stderr.write(f"[live] s{session} generation stopped: {type(e).__name__}: {e}" + chr(10))
    finally:
        buf.finish()  # a viewer still waiting ends its stream
        STATE["running"] = False
        if LIVE.get("generating") is buf:
            LIVE["generating"] = None
        GPU_RUN.release()


def _generate_blocks(ctx, buf, session, t_req, audio, user_audio, user_frame, T, demo):
    """Encode on a thread of its own while the GPU makes the next block; stop when the viewers
    leave or the encoder dies."""
    work = queue.Queue()

    def encoder():
        """H.264/AAC encoding and muxing on a CPU thread of its own, overlapping the GPU work of the
        next block instead of adding to it."""
        try:
            R = ctx["eng"].out_res
            out = av.open(buf, mode="w", format="mp4", options=MUX)
            vs = out.add_stream("libx264", rate=FPS)
            vs.width = vs.height = R
            vs.pix_fmt = "yuv420p"
            vs.options = {"preset": "ultrafast", "tune": "zerolatency", "g": "250", "crf": "18"}  # no keyframe "pulse" per block
            aus = out.add_stream("aac", rate=SR, layout="mono")
            a_pts = 0
            while True:
                item = work.get()
                if item is None:
                    break
                blk, frames = item
                note = (f"frames {blk['t']}-{blk['t_end']}: made in {blk['after_last_input_ms']:.0f} ms "
                        f"(real time = {blk['budget_ms']:.0f})")
                for i, f in enumerate(frames):
                    vf = av.VideoFrame.from_ndarray(overlay(f, note) if demo else np.ascontiguousarray(f), format="rgb24")
                    vf.pts, vf.time_base = blk["t"] + i, Fraction(1, FPS)
                    for p in vs.encode(vf):
                        p.duration = 1
                        out.mux(p)
                        if STATE["ttff_ms"] is None:  # first video bytes handed to the viewers
                            STATE["ttff_ms"] = (time.perf_counter() - t_req) * 1e3
                            sys.stderr.write(f"[live] s{session} time to first frame {STATE['ttff_ms']:.0f} ms" + chr(10))
                a = audio[blk["t"] * SPF: blk["t_end"] * SPF].astype(np.float32)
                a = np.pad(a, (0, (blk["t_end"] - blk["t"]) * SPF - len(a)))
                af = av.AudioFrame.from_ndarray(a.reshape(1, -1), format="flt", layout="mono")
                af.sample_rate, af.pts, af.time_base = SR, a_pts, Fraction(1, SR)
                a_pts += len(a)
                for p in aus.encode(af):
                    out.mux(p)
                STATE["frames_sent"] = blk["t_end"]
            for p in vs.encode():
                p.duration = 1
                out.mux(p)
            for p in aus.encode(None):
                out.mux(p)
            out.close()
            buf.complete = True
        except Exception as e:
            sys.stderr.write(f"[live] s{session} encoder stopped: {type(e).__name__}: {e}" + chr(10))
        finally:
            buf.finish()

    enc = threading.Thread(target=encoder, daemon=True)
    enc.start()
    STATE["frames_sent"] = 0
    blocks = stream_blocks(ctx["eng"], audio, user_audio, user_frame, T, pace=ctx["pace"] and demo,
                           audio_window=50, guard=True, seed=ctx["seed"])
    try:
        for blk in blocks:
            frames = blk.pop("frames")
            blk.pop("prep_ms")
            STATE["blocks"].append(blk)
            sys.stderr.write(f"[live] s{session} frames {blk['t']}-{blk['t_end']} gen {blk['after_last_input_ms']:.0f} ms "
                             f"latency {blk['latency_ms']:.0f} ms" + chr(10))
            if buf.done:  # the encoder died: nobody would drain the frames
                sys.stderr.write(f"[live] s{session} encoder gone; stopped at frame {blk['t']} of {T}" + chr(10))
                break
            work.put((blk, frames))
            if buf.cancel.is_set():  # every viewer left: free the GPU for the next reply
                sys.stderr.write(f"[live] s{session} nobody watching; stopped at frame {blk['t_end']} of {T}" + chr(10))
                break
    finally:
        blocks.close()  # its cleanup (camera thread) runs now, before the next session takes the GPU
        work.put(None)
        enc.join()


VIEW_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Live avatar</title>
<style>body{margin:0;background:#111;color:#eee;font:14px system-ui,sans-serif;display:flex;flex-wrap:wrap;gap:16px;padding:16px}
video{width:512px;max-width:100%;background:#000;border-radius:8px}#s{min-width:280px}td{padding:2px 8px}</style></head>
<body><div><button id="go" style="font-size:18px;padding:10px 18px;margin-bottom:10px">Start live avatar (with sound)</button><br>
<video id="v" controls playsinline></video></div><div id="s">press Start: generation begins on click</div>
<script>
// Live playback through Media Source Extensions: the fragmented MP4 is appended as it arrives.
// The click is the user gesture browsers require before playing sound.
document.getElementById('go').onclick = async () => {
  const v = document.getElementById('v');
  const t0 = performance.now(); window.ttffClient = null;
  if (v.requestVideoFrameCallback) v.requestVideoFrameCallback(() => { window.ttffClient = performance.now() - t0; });
  else v.addEventListener('playing', () => { window.ttffClient = performance.now() - t0; }, {once: true});
  const types = ['video/mp4; codecs="avc1.42C01E, mp4a.40.2"', 'video/mp4; codecs="avc1.42E01E, mp4a.40.2"', 'video/mp4; codecs="avc1.42001E, mp4a.40.2"'];
  const mime = types.find(t => window.MediaSource && MediaSource.isTypeSupported(t));
  if (!mime) { document.getElementById('s').textContent = 'This browser has no MSE support for H.264+AAC'; return; }
  const ms = new MediaSource(); v.src = URL.createObjectURL(ms);
  // play() inside the click itself (before any await): that is what lets sound play. It resolves
  // once data arrives; a later play() after a stall is then allowed too.
  v.play().catch(e => console.warn('play()', e));
  await new Promise(r => ms.addEventListener('sourceopen', r, {once: true}));
  const sb = ms.addSourceBuffer(mime);
  const q = []; let ended = false;
  const resume = () => {  // after a stall, resume once 0.3 s is buffered ahead
    if (v.paused && v.buffered.length && (v.buffered.end(v.buffered.length - 1) - v.currentTime >= 0.3 || ended))
      v.play().catch(() => {});
  };
  const pump = () => {
    resume();
    if (sb.updating) return;
    if (q.length) { sb.appendBuffer(q.shift()); return; }
    if (ended && ms.readyState === 'open') ms.endOfStream();
  };
  sb.addEventListener('updateend', pump);
  const rd = (await fetch('/live.mp4?t=' + Date.now(), {cache: 'no-store'})).body.getReader();
  // playback was started in the click; resume() handles stalls
  for (;;) { const {done, value} = await rd.read(); if (done) { ended = true; pump(); break; } q.push(value); pump(); }
};
async function tick(){try{const s=await (await fetch('/stats')).json();const b=s.per_block||[];
const rows=b.slice(-12).map(x=>`<tr><td>${x.t}-${x.t_end}</td><td>${x.after_last_input_ms.toFixed(0)} ms</td><td>${(x.latency_ms/1000).toFixed(1)} s</td></tr>`).join('');
document.getElementById('s').innerHTML=`<div style="font-size:18px;margin-bottom:8px">time to first frame: <b>${window.ttffClient!=null?window.ttffClient.toFixed(0)+' ms':'-'}</b> on screen (click to first painted frame), server ${s.ttff_ms!=null?s.ttff_ms.toFixed(0)+' ms':'-'}</div><b>${s.running?'generating':'done'}</b>:${s.blocks} blocks, made in median ${s.gpu_ms_p50?s.gpu_ms_p50.toFixed(0):'-'} ms per 400 ms block, ${s.blocks_over_400ms} slower than real time<table><tr><th>frames</th><th>made in</th><th>delay</th></tr>${rows}</table>`;}catch(e){}}
setInterval(tick,1000);</script></body></html>"""


def make_handler(ctx):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            sys.stderr.write("[http] " + (fmt % a) + "\n")

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            if code >= 400:  # an early answer may leave the request body unread: never reuse
                self.close_connection = True
                self.send_header("Connection", "close")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self):
            """Only this machine's own name for this server: a web page that rebinds its DNS
            name to 127.0.0.1 still sends its own name as Host."""
            port = self.server.server_address[1]
            if self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return True
            self.send_error(403)
            return False

        def _json_request(self):
            """POST/DELETE come as JSON: a cross-origin page cannot send that without a CORS
            preflight, which this server never answers."""
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() == "application/json":
                return True
            self.send_error(415)
            return False

        def _query(self):
            return (dict(x.split("=", 1) for x in self.path.split("?", 1)[1].split("&") if "=" in x)
                    if "?" in self.path else {})

        def do_GET(self):
            if not self._host_ok():
                return
            path = self.path.split("?")[0]
            if path == "/health":
                return self._json({"ok": True, "ready": bool(ctx.get("ready")), "running": STATE["running"]})
            if path == "/stats":
                b = STATE["blocks"]
                steady = [x for x in b if x["t"] > 0]
                gm = [x["gpu_ms"] for x in steady]
                el = time.perf_counter() - STATE.get("t0", time.perf_counter())
                sent = STATE.get("frames_sent", 0)
                return self._json({"session": STATE["session"], "running": STATE["running"], "blocks": len(b),
                                   "ttff_ms": STATE.get("ttff_ms"), "total_frames": STATE.get("total_frames"),
                                   "frames_sent": sent, "elapsed_s": el,
                                   "rate_fps": (b[-1]["t_end"] - b[-6]["t_end"]) / max(1e-3, b[-1]["wall_s"] - b[-6]["wall_s"]) if len(b) > 6 else None,
                                   "gpu_ms_p50": float(np.median(gm)) if gm else None,
                                   "blocks_over_400ms": int(sum(g > 400 for g in gm)),
                                   "latency_ms_last": b[-1]["latency_ms"] if b else None, "per_block": b[-40:]})
            if path == "/live.mp4":
                if not ctx.get("ready"):
                    return self._json({"error": "loading"}, 503)
                sid = self._query().get("sid")
                if not sid and not ctx.get("demo"):
                    return self.send_error(404)
                return self._live(sid)
            if path in ("/", "/view") and ctx.get("demo"):
                return self._view()
            self.send_error(404)

        def do_POST(self):
            if not self._host_ok():
                return
            if self.path.split("?")[0] != "/session":
                return self.send_error(404)
            if not self._json_request():
                return
            if not ctx.get("ready"):
                return self._json({"error": "loading"}, 503)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                sess = new_session(ctx, req.get("audio_url"), req.get("image_url"))
                return self._json({"sid": sess["sid"], "stream": f"/live.mp4?sid={sess['sid']}",
                                   "frames": sess["T"], "fps": FPS})
            except Exception as e:
                sys.stderr.write(f"[session] refused: {type(e).__name__}: {e}" + chr(10))
                return self._json({"error": f"{type(e).__name__}: {e}"}, 400)

        def do_DELETE(self):
            """Stop generating a reply: its viewer left (a relay in between may never see that)."""
            if not self._host_ok():
                return
            if self.path.split("?")[0] != "/live.mp4":
                return self.send_error(404)
            if not self._json_request():
                return
            sess = SESSIONS.get(self._query().get("sid") or "")
            if sess is None:
                return self.send_error(404)
            with GEN_LOCK:
                if sess.get("buf") is None:
                    sess["buf"] = LiveBuffer()  # a stream that never started never will
                    sess["buf"].finish()
                    sess["buf"].cancel.set()
                elif sess["buf"].viewers <= 1:  # the one leaving is the last: stop generating
                    sess["buf"].cancel.set()
                else:  # others still watch (another tab, the same user's other device)
                    return self._json({"stopped": False, "viewers": sess["buf"].viewers})
            return self._json({"stopped": True})

        def _live(self, sid=None):
            t_req = time.perf_counter()
            with GEN_LOCK:  # join the running session, or start one
                if sid:
                    sess = SESSIONS.get(sid)
                    if sess is None:
                        return self.send_error(404)
                    buf = sess.get("buf")
                    if buf is None:
                        old = LIVE.get("generating")
                        if old is not None and old.viewers == 0 and not old.done:
                            old.cancel.set()  # an abandoned reply yields the GPU to this one now
                        buf = sess["buf"] = LiveBuffer()
                        STATE["session"] += 1
                        threading.Thread(target=generate, args=(ctx, buf, STATE["session"], t_req, sess),
                                         daemon=True).start()
                else:  # the demo: a new viewer after the last one finished starts it again
                    buf = LIVE["buf"]
                    if buf is None or buf.done or buf.cancel.is_set():
                        buf = LIVE["buf"] = LiveBuffer()
                        STATE["session"] += 1
                        threading.Thread(target=generate, args=(ctx, buf, STATE["session"], t_req), daemon=True).start()
                buf.viewers += 1
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            i = 0
            try:
                while True:
                    with buf.cv:
                        while i >= len(buf.chunks) and not buf.done:
                            buf.cv.wait(timeout=1.0)
                        new, finished = buf.chunks[i:], buf.done and i >= len(buf.chunks)
                    if finished:
                        break
                    for c in new:
                        self.wfile.write(b"%x" % len(c) + CRLF + c + CRLF)
                    self.wfile.flush()
                    i += len(new)
                self.wfile.write(b"0" + CRLF + CRLF)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                sys.stderr.write("[live] a viewer left before the end" + chr(10))
            finally:
                with GEN_LOCK:
                    buf.viewers -= 1
                    if buf.viewers == 0 and not buf.done:
                        # A player may drop and reconnect; stop only if nobody is back in 2 s.
                        threading.Timer(2.0, lambda: buf.viewers == 0 and not buf.done and buf.cancel.set()).start()

        def _view(self):
            f = os.path.join(HERE, "stream_view.html")  # read per request: page fixes need no restart
            body = (open(f, encoding="utf-8").read() if os.path.exists(f) else VIEW_HTML).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="data/rumi.jpg")  # the checkout's sample face; sessions bring their own
    ap.add_argument("--avatar_audio", default="data/avatar.wav")
    ap.add_argument("--user_video", default="data/user.mp4")
    ap.add_argument("--res", type=int, default=512, choices=[256, 512])
    ap.add_argument("--port", type=int, default=18765)
    ap.add_argument("--max_seconds", type=float, default=60)
    ap.add_argument("--seed", type=int, default=25)
    ap.add_argument("--pace", action="store_true", help="feed inputs at wall-clock speed (live mic/camera); "
                    "default: the speech audio already exists (a TTS reply), generation runs ahead of playback")
    ap.add_argument("--vram_mb", type=int, default=1024)
    ap.add_argument("--ctx_reserve_mb", type=int, default=260)
    ap.add_argument("--demo", action="store_true", help="also serve the --ref/--avatar_audio demo "
                    "(GET /live.mp4 without sid, GET /view); off for the HARTOS sidecar")
    args = ap.parse_args(argv)

    ctx = {"demo": args.demo}
    # Claim the port and start answering before the slow model load. --port 0 takes a free port and
    # prints PORT=N: the contract of HARTOS's RuntimeToolManager sidecars (it waits for that line, then
    # TCP-probes). Until the model is loaded, /health says ready=false and streams answer 503.
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(ctx))
    port = srv.server_address[1]
    print(f"PORT={port}", flush=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    dev = torch.device("cuda:0")
    torch.zeros(1, device=dev)
    total = torch.cuda.get_device_properties(dev).total_memory
    torch.cuda.set_per_process_memory_fraction((args.vram_mb - args.ctx_reserve_mb) * 2**20 / total, dev)

    fc = FaceCropper()
    img = cv2.cvtColor(cv2.imread(args.ref), cv2.COLOR_BGR2RGB)
    ref = to_tensor(fc.crop(img, *fc.box(img))).unsqueeze(0)
    avatar_audio = load_audio(args.avatar_audio)
    user_frames = load_video_25fps(args.user_video) if args.user_video else []
    user_audio = load_audio(args.user_video) if args.user_video else None
    if user_audio is None:
        user_audio = np.zeros_like(avatar_audio)
    boxes = [b for b in (fc.box(f) for f in user_frames[:FPS]) if b is not None]
    ub = tuple(float(np.mean([b[i] for b in boxes])) for i in range(4)) + (boxes[0][4],) if boxes else None

    def user_frame(i):
        f = user_frames[min(i, len(user_frames) - 1)]
        return to_tensor(FaceCropper.crop(f, *ub)) if ub else to_tensor(cv2.resize(f, (512, 512), interpolation=cv2.INTER_AREA))

    T = min(int(np.ceil(max(len(avatar_audio), len(user_audio)) * FPS / SR)), int(args.max_seconds * FPS))
    cfg = OmegaConf.load("configs/inference.yaml")
    cfg.rank, cfg.ngpus = 0, 1
    eng = LowVRAMAvatar(cfg, device="cuda:0", out_res=args.res, audio_device="cuda:0", graph_frames=1)
    eng.set_reference(ref)
    eng.reset()
    eng.audio_features(torch.zeros(2, 50 * SPF), 50)
    eng.prepare_graphs(audio_frames=50)
    ctx.update({"eng": eng, "ref": ref, "cropper": fc, "cur_ref": ref, "avatar_audio": avatar_audio, "user_audio": user_audio,
                "user_frame": user_frame if user_frames else None, "T": T, "seed": args.seed,
                "pace": args.pace, "ready": True})
    print(f"READY http://127.0.0.1:{port}/view", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
