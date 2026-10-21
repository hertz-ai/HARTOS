"""Reel Studio's desktop pipeline from the command line (plan E2E-1).

Not a test pytest collects (tests/conftest.py ignores tests/standalone) and
not shipped.  It refuses to run unless HARTOS_E2E_BASE names a scratch
directory: work dirs and default outputs go under it, nothing else is
written outside the data dir the app itself uses.

  make    a reel from clip files on disk, through the production pipeline
          (integrations.reel_studio.pipeline.produce_reel), holding the
          machine's one reel claim (jobs.start_job) so it never renders
          beside the app.  --captured-at gives each clip the phone's
          capture time (epoch ms, '-' for none), as a phone upload carries
          it.  Writes the mp4, its EDL (--edl), moments and QA report beside
          it, and prints stage timings, peak RSS (this process and its
          ffmpeg children when psutil is installed) and the work dir's peak
          size.  Exit 0 when every hard QA check passed, 1 when one failed,
          2 when no reel was made (the status is printed), 3 when busy.
  qa      the hard and advisory QA rows of a reel against its EDL
          (media.qa.run_qa).  Exit 0 when it passed, 1 when not.
  sheet   a contact sheet PNG: --cols x --rows frames spread evenly over
          the video, each stamped with its time when a font is found.
  ingest  files into a user's store as her phone would upload them (offer,
          chunks, commit) and bound to an agent turn, through the store API
          (core.user_file_store).  Prints the /chat attachment rows (the U9
          shape) for a loopback chat turn.  Run it while no reel turn is in
          flight: the store has one writer process (the app).

  python tests/standalone/reel_pipeline_cli.py make --clips c1.mp4 c2.mp4 c3.mp4 \\
      --captured-at 1760247082000 1760247382000 - --target 20 --open-with story \\
      --logo logo.png --out out.mp4 --edl out.edl.json --vlm default
  python tests/standalone/reel_pipeline_cli.py qa out.mp4 --edl out.edl.json
  python tests/standalone/reel_pipeline_cli.py sheet out.mp4
  python tests/standalone/reel_pipeline_cli.py ingest --user 10077 --prompt 42 \\
      --request rid-1 c1.mp4 c2.mp4 logo.png

--vlm default rates keyframes on moments.REEL_VLM_DEFAULT (the 0.8B caption
server on this machine; never started here); --vlm off scores without a
model (the fallback path); any other value names a vision backend.
"""
import argparse
import hashlib
import json
import os
import sys
import threading
import time

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)

ENV_BASE = 'HARTOS_E2E_BASE'
EXIT_QA_FAILED, EXIT_NO_REEL, EXIT_BUSY = 1, 2, 3
CLI_USER = 'e2e-cli'


def _base() -> str:
    base = os.environ.get(ENV_BASE, '').strip()
    if not base:
        sys.stderr.write('%s is not set: this E2E driver writes its work dirs and outputs '
                         'under that scratch directory and will not run without it.\n' % ENV_BASE)
        sys.exit(EXIT_NO_REEL)
    base = os.path.abspath(base)
    os.makedirs(base, exist_ok=True)
    return base


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: str, doc) -> None:
    from core.file_cache import atomic_json_write
    atomic_json_write(path, doc)


def _captured(values, count: int):
    """--captured-at values for ``count`` clips: ints, None for '-'."""
    if not values:
        return [None] * count
    if len(values) != count:
        sys.stderr.write('--captured-at needs one value per clip (%d), got %d\n'
                         % (count, len(values)))
        sys.exit(EXIT_NO_REEL)
    out = []
    for v in values:
        out.append(None if v in ('-', '', 'none') else int(v))
    return out


# ── watching the run ──────────────────────────────────────────────────────

class _Watch:
    """Peak work-dir size, and peak RSS of this process and its children
    when psutil is there; sampled every half second."""

    def __init__(self, workdir: str):
        self.workdir = workdir
        self.peak_dir = 0
        self.peak_rss = None
        self._stop = threading.Event()
        try:
            import psutil
            self._proc = psutil.Process()
        except Exception:
            self._proc = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _dir_bytes(self) -> int:
        total = 0
        for root, _dirs, files in os.walk(self.workdir):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
        return total

    def _rss(self):
        if self._proc is None:
            return None
        try:
            procs = [self._proc] + self._proc.children(recursive=True)
            return sum(p.memory_info().rss for p in procs if p.is_running())
        except Exception:
            return None

    def _run(self):
        while not self._stop.wait(0.5):
            self.peak_dir = max(self.peak_dir, self._dir_bytes())
            rss = self._rss()
            if rss is not None:
                self.peak_rss = max(self.peak_rss or 0, rss)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(5)
        if self.peak_rss is None:
            self.peak_rss = _own_peak_rss()


def _own_peak_rss():
    """This process's own peak RSS from the OS (children not included)."""
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                            ('PeakWorkingSetSize', ctypes.c_size_t),
                            ('WorkingSetSize', ctypes.c_size_t),
                            ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                            ('QuotaPagedPoolUsage', ctypes.c_size_t),
                            ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                            ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                            ('PagefileUsage', ctypes.c_size_t),
                            ('PeakPagefileUsage', ctypes.c_size_t)]
            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            current = ctypes.windll.kernel32.GetCurrentProcess
            current.restype = wintypes.HANDLE          # a 64-bit pseudo-handle, not an int
            info = ctypes.windll.psapi.GetProcessMemoryInfo
            info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            info.restype = wintypes.BOOL
            if info(current(), ctypes.byref(counters), counters.cb):
                return int(counters.PeakWorkingSetSize)
            return None
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except Exception:
        return None


def _mb(n) -> str:
    return 'n/a' if n is None else '%.0f MB' % (n / 1e6)


# ── make ──────────────────────────────────────────────────────────────────

class _NoVision:
    """--vlm off: a backend that never answers, so every keyframe falls back."""
    name = 'off'

    def rate_frame(self, image_bytes, prompt, **_kw):
        return None


def _backend(choice: str):
    if choice == 'default':
        return None
    if choice == 'off':
        return _NoVision()
    from integrations.vision.lightweight_backend import get_vision_backend
    return get_vision_backend(choice)


def _sources(paths, captured):
    from integrations.reel_studio.contracts import ClipSource
    out = []
    for k, (path, at) in enumerate(zip(paths, captured)):
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            sys.stderr.write('no such clip: %s\n' % path)
            sys.exit(EXIT_NO_REEL)
        digest = _sha256(path)
        out.append(ClipSource(xid=digest, path=path, name=os.path.basename(path),
                              mime='video/mp4', size=os.path.getsize(path), sha256=digest,
                              committed_at=float(k), captured_at_ms=at))
    return out


def cmd_make(args) -> int:
    base = _base()
    from integrations.reel_studio import jobs, pipeline
    from integrations.reel_studio.style import build_style
    sources = _sources(args.clips, _captured(args.captured_at, len(args.clips)))
    hook_first = {'final_look': True, 'story': False}.get(args.open_with)
    style = build_style(args={'target_s': args.target, 'hook_first': hook_first,
                              'hook_text': args.hook_text or None,
                              'use_logo': bool(args.logo), 'logo_corner': args.logo_corner})
    out = os.path.abspath(args.out or os.path.join(base, 'out', 'reel_%d.mp4' % time.time()))
    edl_path = os.path.abspath(args.edl or out + '.edl.json')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    job = jobs.start_job(CLI_USER, None, None)
    if job is None:
        print('busy: another reel is being made on this machine')
        return EXIT_BUSY
    workdir = os.path.join(base, 'work', job.reel_id)
    status, code = 'render_failed', EXIT_NO_REEL
    started = time.monotonic()
    try:
        with _Watch(workdir) as watch:
            try:
                made = pipeline.produce_reel(
                    sources, style=style, workdir=workdir, out_path=out,
                    song_audio=os.path.abspath(args.song_audio) if args.song_audio else None,
                    song_start_s=args.song_start, logo_path=args.logo,
                    backend=_backend(args.vlm), encoder=args.encoder,
                    progress=lambda stage, text: print('[%6.1f s] %s' % (
                        time.monotonic() - started, text), flush=True))
            except pipeline.ReelFailed as e:
                status = e.status
                print(json.dumps({'status': e.status, 'message': e.message, **e.data}, indent=2))
                return EXIT_NO_REEL
        from integrations.reel_studio.contracts import qa_summary
        _write_json(edl_path, made.edl)
        _write_json(out + '.moments.json', made.moments)
        _write_json(out + '.qa.json', made.qa)
        summary = qa_summary(made.qa)
        status = 'ok' if summary['passed'] else 'qa_failed'
        code = 0 if summary['passed'] else EXIT_QA_FAILED
        advisory = [r['id'] for r in made.qa.get('advisory', []) if not r.get('ok')]
        print(json.dumps({
            'status': status, 'out': out, 'edl': edl_path, 'qa': summary,
            'advisory_not_ok': advisory, 'duration_s': made.render.duration_s,
            'size_bytes': made.render.size_bytes, 'encoder': made.render.encoder,
            'moments': {'src': made.moments.get('src'), 'model': made.moments.get('model'),
                        'final_look_found': made.moments.get('final_look_found'),
                        'stats': made.moments.get('stats')},
            'beats': made.beats_source,
            'notes': made.edl.get('notes', []) + made.moments.get('notes', []) + made.notes,
            'timings_s': made.timings, 'total_s': round(time.monotonic() - started, 1),
            'peak_rss': _mb(watch.peak_rss) + ('' if watch._proc is not None else
                                               ' (this process only; psutil not installed)'),
            'work_dir_peak': _mb(watch.peak_dir)}, indent=2))
        return code
    finally:
        if not args.keep_work:
            pipeline.remove_work_dir(workdir)
        jobs.finish_job(job.reel_id, status)


# ── qa ────────────────────────────────────────────────────────────────────

def cmd_qa(args) -> int:
    _base()
    from integrations.reel_studio.contracts import qa_summary
    from integrations.reel_studio.media.qa import run_qa
    with open(args.edl, encoding='utf-8') as f:
        edl = json.load(f)
    report = run_qa(os.path.abspath(args.path), edl)
    if args.report:
        _write_json(os.path.abspath(args.report), report)
    for row in report['checks']:
        if not row['ok']:
            print('HARD FAIL  %-18s %s' % (row['id'], json.dumps(row['value'])[:300]))
    for row in report['advisory']:
        print('advisory   %-18s %-4s %s' % (row['id'], 'ok' if row['ok'] else 'NOT',
                                            json.dumps(row['value'])[:200]))
    summary = qa_summary(report)
    print(json.dumps(summary))
    return 0 if summary['passed'] else EXIT_QA_FAILED


# ── sheet ─────────────────────────────────────────────────────────────────

def cmd_sheet(args) -> int:
    _base()
    from core import media_bin
    from integrations.reel_studio.media import fonts
    from integrations.reel_studio.media.probe import probe
    from integrations.reel_studio.media.render import escape_filter_path
    path = os.path.abspath(args.path)
    info = probe(path)
    count = args.cols * args.rows
    out = os.path.abspath(args.out or path + '.sheet.png')
    chain = ['fps=%d/%.3f' % (count, max(0.1, info.duration_s)),
             'scale=%d:-2' % args.width]
    font = fonts.resolve_font('0:00')
    if font:
        chain.append("drawtext=fontfile=%s:text='%%{pts\\:hms}':x=6:y=6:fontsize=%d:"
                     'fontcolor=white:box=1:boxcolor=black@0.6'
                     % (escape_filter_path(font), max(12, args.width // 12)))
    chain.append('tile=%dx%d:padding=4:color=gray' % (args.cols, args.rows))
    result = media_bin.run_ffmpeg(['-y', '-loglevel', 'error', '-i', path, '-vf',
                                   ','.join(chain), '-frames:v', '1', '-update', '1', out],
                                  timeout_s=120)
    if result.returncode != 0 or result.timed_out:
        sys.stderr.write('ffmpeg failed: %s\n' % (result.stderr or '')[-800:])
        return EXIT_NO_REEL
    print(out)
    return 0


# ── ingest ────────────────────────────────────────────────────────────────

def _kind(path: str):
    """(mime, purpose) of a file by its extension, from the store's own table."""
    from core import user_file_store as ufs
    ext = os.path.splitext(path)[1].lower()
    ext = '.jpg' if ext == '.jpeg' else ext
    mime = next((m for m, e in ufs._EXT.items() if e == ext), None)
    purpose = next((p for p, mimes in ufs.INBOUND_MIMES.items() if mime in mimes), None)
    return mime, purpose


def cmd_ingest(args) -> int:
    _base()
    from core import user_file_store as ufs
    store = ufs.UserFileStore(args.root) if args.root else ufs.get_user_file_store()
    captured = _captured(args.captured_at, len(args.files))
    print('store root: %s (the app must not be making a reel while this writes)' % store.root,
          file=sys.stderr)
    rows = []
    for path, at in zip(args.files, captured):
        path = os.path.abspath(path)
        mime, purpose = _kind(path)
        purpose = args.purpose or purpose
        if mime is None or purpose is None:
            sys.stderr.write('%s: not a clip, logo or song the store takes\n' % path)
            return EXIT_NO_REEL
        if at is None and purpose == 'reel_source':
            at = int(os.path.getmtime(path) * 1000)   # the phone's DATE_MODIFIED fallback
        xid, size = _sha256(path), os.path.getsize(path)
        meta = {'captured_at': at} if at else None
        offer = store.offer(args.user, os.path.basename(path), size, xid, mime, purpose,
                            meta=meta)
        if offer['type'] == 'file_accept':
            cs = offer['chunk_size']
            with open(path, 'rb') as f:
                for first, last in offer['missing']:
                    for i in range(first, last + 1):
                        f.seek(i * cs)
                        data = f.read(cs)
                        ack = store.put_chunk(args.user, xid, i, data,
                                              hashlib.sha256(data).hexdigest())
                        if ack['type'] != 'file_ack':
                            sys.stderr.write('%s chunk %d refused: %s\n' % (path, i, ack))
                            return EXIT_NO_REEL
            done = store.commit(args.user, xid)
            if done['type'] != 'file_done':
                sys.stderr.write('%s not committed: %s\n' % (path, done))
                return EXIT_NO_REEL
        elif offer['type'] != 'file_have':
            sys.stderr.write('%s refused: %s\n' % (path, offer))
            return EXIT_NO_REEL
        # By purpose, as the phone's turn binds it: a file held from an
        # earlier ingest moves to this turn (a bare reference would not).
        store.bind(args.user, [xid], prompt_id=args.prompt, request_id=args.request,
                   purpose=purpose)
        row = {'file_id': xid, 'sha256': xid, 'name': os.path.basename(path), 'mime': mime,
               'size': size, 'purpose': purpose}
        if at:
            row['captured_at'] = at
        rows.append(row)
    print(json.dumps({'attachments': rows}, indent=2))
    return 0


# ── main ──────────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)

    m = sub.add_parser('make', help='make a reel from clip files')
    m.add_argument('--clips', nargs='+', required=True)
    m.add_argument('--captured-at', nargs='+', default=None,
                   help="epoch ms per clip, '-' for none (as a phone upload carries it)")
    m.add_argument('--target', type=float, default=20)
    m.add_argument('--open-with', choices=('final_look', 'story', ''), default='')
    m.add_argument('--hook-text', default='')
    m.add_argument('--logo', default=None)
    m.add_argument('--logo-corner', choices=('top_left', 'top_right', 'bottom_left'),
                   default=None)
    m.add_argument('--song-audio', default=None)
    m.add_argument('--song-start', type=float, default=0.0)
    m.add_argument('--out', default=None)
    m.add_argument('--edl', default=None)
    m.add_argument('--vlm', default='default', help="default | off | a backend name")
    m.add_argument('--encoder', choices=('auto', 'h264_nvenc', 'libx264'), default='auto')
    m.add_argument('--keep-work', action='store_true')
    m.set_defaults(func=cmd_make)

    q = sub.add_parser('qa', help='QA a rendered reel against its EDL')
    q.add_argument('path')
    q.add_argument('--edl', required=True)
    q.add_argument('--report', default=None)
    q.set_defaults(func=cmd_qa)

    s = sub.add_parser('sheet', help='a contact sheet PNG of a video')
    s.add_argument('path')
    s.add_argument('--out', default=None)
    s.add_argument('--cols', type=int, default=4)
    s.add_argument('--rows', type=int, default=3)
    s.add_argument('--width', type=int, default=240)
    s.set_defaults(func=cmd_sheet)

    i = sub.add_parser('ingest', help="put files in a user's store, bound to a turn")
    i.add_argument('files', nargs='+')
    i.add_argument('--user', required=True)
    i.add_argument('--prompt', required=True)
    i.add_argument('--request', default=None)
    i.add_argument('--purpose', choices=('reel_source', 'logo', 'song_audio'), default=None)
    i.add_argument('--captured-at', nargs='+', default=None)
    i.add_argument('--root', default=None, help='another store root (default: the app\'s)')
    i.set_defaults(func=cmd_ingest)
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
