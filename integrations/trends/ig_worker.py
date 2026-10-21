"""integrations/trends/ig_worker.py: the trending-songs worker, run by her
desktop's daily job (integrations.trends.daily_job) as a child process in
the worker's own venv under the data dir:

    <worker venv python> -I ig_worker.py <spec.json> <out.json>

instagrapi lives only in that venv (daily_job.WORKER_PACKAGES, installed by
Nunba's tts.backend_venv): its pydantic and requests pins conflict with
HARTOS's, so it never enters the HARTOS interpreter.  ``-I`` keeps the
PYTHON* environment variables, the user site-packages and the working and
script directories out of the child's sys.path.  It does not keep the app
packages out: tts.backend_venv.ensure_venv writes core.venv_paths'
nunba_parent_packages.pth into every venv it builds, this one included, and
site still reads a .pth under ``-I``, so the directory holding the app's
core and integrations packages (the HARTOS root from source, the install
dir on the frozen build; core.venv_paths.parent_package_roots) is APPENDED
to sys.path, after the venv's own site-packages (the venv's pins win any
module both hold).  The worker imports nothing from there: the one HARTOS
file it uses, sources.py beside it (standard library only), is loaded by
path.  Imported as integrations.trends.ig_worker (the tests), it uses
integrations.trends.sources instead: the same file.

The spec (written by daily_job, no secrets in it):
    {"accounts": [{"handle", "pk"}], "budget": 60, "known_song_keys": [...],
     "session_path": ".../trends/.ig_session.json", "delay_range": [3, 8],
     "deadline_s": 780}
The research account's credentials (a dedicated research account, never
her business account) arrive as ONE JSON line on stdin,
``{"username": ..., "password": ...}``, which daily_job writes from the
desktop's vault and closes.  They are never in argv, the environment, the
spec, the batch or a log line: instagrapi logs through the standard logging
module, and while the worker runs every record reaches its stderr (which
daily_job sends to trends/worker.log) through one handler that blanks both
values out (_logs_without), so no record falls through to logging's
last-resort handler, which would print them as they are.

What it does, read from instagrapi 3.0.24's own source:
* ``Client(with_challenge_flow=False, delay_range=...)``: a challenge
  raises ChallengeRequired instead of prompting for a code, and every
  private request waits 3-8 s.
* The saved session is loaded with ``override_app_version=True`` (a session
  from an older app profile otherwise fails the CAA login), then
  ``login()`` reuses or renews it, and the session is written back with
  mode 0600, atomically.  A refused login keeps no session.
* One ``user_clips_paginated_v1(pk, amount=12)`` page per account (the
  account's pk looked up once with ``user_info_by_username_v1`` unless
  the job cached it), and ``track_info_by_canonical_id`` for each song
  known only by its canonical id that the published list does not already
  know.
* Every instagrapi exception maps to one code (error_code); the run's stop
  rules, budget and deadline are sources.collect's.

Exit codes: 0 a batch was written (its "error" says how the run went),
2 bad arguments, 3 instagrapi is not installed (a "dependency" batch is
still written), 1 an unexpected failure (an "unknown" batch is written).
"""
import contextlib
import importlib.util
import json
import logging
import os
import sys
import time

#: The longest credentials line read from stdin.
MAX_CREDENTIALS_LINE = 4096

#: The worker's own deadline when the spec names none, under daily_job's
#: 900 s kill, so the batch is written before the job gives up on it.
DEFAULT_DEADLINE_S = 780.0
DEFAULT_DELAY_RANGE = [3, 8]
#: The most requests a spec may grant one run.
MAX_BUDGET = 200

#: instagrapi exception class names, matched along the exception's MRO so a
#: subclass maps with its family (UserNotFound is a NotFoundError,
#: SignupSpamError a FeedbackRequired).  Matched by name because the
#: classes exist only in the trends venv.  First match on the MRO wins.
_CODE_OF = {
    'ChallengeError': 'challenge', 'ChallengeRequired': 'challenge',
    'CaptchaChallengeRequired': 'challenge', 'AccountSuspended': 'challenge',
    'TwoFactorRequired': 'challenge', 'ProxyAddressIsBlocked': 'challenge',
    'LoginRequired': 'login', 'ClientLoginRequired': 'login', 'BadPassword': 'login',
    'BadCredentials': 'login', 'ReloginAttemptExceeded': 'login',
    'ClientUnauthorizedError': 'login', 'ClientForbiddenError': 'login',
    'PleaseWaitFewMinutes': 'throttled', 'RateLimitError': 'throttled',
    'ClientThrottledError': 'throttled', 'SentryBlock': 'throttled',
    'FeedbackRequired': 'feedback',
    'NotFoundError': 'not_found', 'PrivateAccount': 'not_found',
    'ClientNotFoundError': 'not_found', 'InvalidTargetUser': 'not_found',
    'ClientConnectionError': 'transport', 'ClientRequestTimeout': 'transport',
    'ClientJSONDecodeError': 'transport', 'ClientIncompleteReadError': 'transport',
    'RequestException': 'transport', 'ConnectionError': 'transport',
    'TimeoutError': 'transport',
}


#: What a blanked-out username or password reads as in a log line.
HIDDEN = '[hidden]'


class _Blanking(logging.Formatter):
    """A log line (with its traceback) with each of ``secrets`` replaced by
    HIDDEN, longest first, so a password holding the username is blanked
    whole."""

    def __init__(self, secrets):
        super().__init__('%(levelname)s %(name)s: %(message)s')
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def format(self, record):
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, HIDDEN)
        return text


@contextlib.contextmanager
def _logs_without(*secrets):
    """While inside: WARNING and above from any logger (instagrapi's
    included) go to stderr with ``secrets`` blanked out.  The handler is
    removed on the way out, so the process's logging is left as found."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(_Blanking(secrets))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)


def error_code(exc: BaseException) -> str:
    """The run's code for an exception: 'challenge', 'login', 'throttled',
    'feedback', 'not_found', 'transport' or 'unknown'."""
    for cls in type(exc).__mro__:
        code = _CODE_OF.get(cls.__name__)
        if code:
            return code
    return 'unknown'


def _load_sources():
    """sources.py: the package module when imported as part of HARTOS, else
    the file beside this one, loaded by path (the worker never relies on
    the app's packages being on its sys.path: only the venv's appended .pth
    puts them there)."""
    if __package__:
        from . import sources
        return sources
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sources.py')
    spec = importlib.util.spec_from_file_location('hart_trends_sources', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules['hart_trends_sources'] = module
    spec.loader.exec_module(module)
    return module


def _dump(obj):
    """A model as plain JSON-ready data, or the mapping itself."""
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, 'model_dump', None)
    if callable(dump):
        try:
            return dump(mode='json')
        except Exception:
            return None
    return None


class InstagrapiSource:
    """sources.TrendSource over a logged-in instagrapi Client."""

    name = 'instagrapi'

    def __init__(self, client, sources):
        self.client = client
        self.sources = sources

    def requests_used(self) -> int:
        return int(getattr(self.client, 'private_requests_count', 0) or 0)

    def _fail(self, exc, pk=None):
        return self.sources.TrendSourceError(error_code(exc), type(exc).__name__, pk=pk)

    def fetch_reels(self, handle, pk_hint):
        pk = pk_hint
        try:
            if not pk:
                pk = str(self.client.user_info_by_username_v1(handle).pk)
            medias, cursor = self.client.user_clips_paginated_v1(
                pk, amount=self.sources.PAGE_SIZE)
        except Exception as e:
            raise self._fail(e, pk) from None
        if cursor is None and not medias:
            # user_clips_paginated_v1 logs any non-private error and returns
            # ([], None); an account with no reels returns ([], '').
            raise self.sources.TrendSourceError('transport', 'empty_page', pk=pk)
        reels = [r for r in (_dump(m) for m in medias or []) if r is not None]
        return {'pk': pk, 'reels': reels}

    def lookup_track(self, song_key):
        if not isinstance(song_key, str) or not song_key.startswith('canonical:'):
            return None
        try:
            track = self.client.track_info_by_canonical_id(song_key.split(':', 1)[1])
        except Exception as e:
            raise self._fail(e) from None
        return _dump(track)


def _read_spec(path):
    try:
        with open(path, encoding='utf-8') as fh:
            spec = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(spec, dict) or not isinstance(spec.get('session_path'), str):
        return None
    budget = spec.get('budget')
    spec['budget'] = (budget if isinstance(budget, int) and not isinstance(budget, bool)
                      and 0 < budget <= MAX_BUDGET else 60)
    delay = spec.get('delay_range')
    if not (isinstance(delay, list) and len(delay) == 2
            and all(isinstance(d, (int, float)) and not isinstance(d, bool) and d >= 0
                    for d in delay) and delay[0] <= delay[1]):
        spec['delay_range'] = list(DEFAULT_DELAY_RANGE)
    deadline = spec.get('deadline_s')
    spec['deadline_s'] = (float(deadline) if isinstance(deadline, (int, float))
                          and not isinstance(deadline, bool) and 0 < deadline <= 3600
                          else DEFAULT_DEADLINE_S)
    known = spec.get('known_song_keys')
    spec['known_song_keys'] = [k for k in known if isinstance(k, str)] if isinstance(
        known, list) else []
    return spec


def _read_credentials():
    """``(username, password)`` from the one JSON line daily_job wrote to
    stdin; ``('', '')`` for no line, a line that is not that object, or a
    blank field."""
    stream = sys.stdin
    if stream is None:
        return '', ''
    try:
        line = stream.readline(MAX_CREDENTIALS_LINE)
        data = json.loads(line) if line and line.strip() else None
    except (OSError, ValueError):
        return '', ''
    if not isinstance(data, dict):
        return '', ''
    username, password = data.get('username'), data.get('password')
    if not isinstance(username, str) or not isinstance(password, str):
        return '', ''
    username = username.strip()
    if not username or not password:
        return '', ''
    return username, password


def _write_json(path, data, mode=0o644):
    """Write ``data`` to a temp file beside ``path``, then os.replace."""
    tmp = '%s.%d.tmp' % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_session(client, path):
    """Load the saved session, if one is readable; a damaged file is left
    for the next dump to replace, and the login starts fresh."""
    if not os.path.isfile(path):
        return False
    try:
        client.load_settings(path, override_app_version=True)
        return True
    except Exception as e:
        sys.stderr.write('saved session not loaded: %s\n' % type(e).__name__)
        return False


def _save_session(client, path):
    """Write the client's session to ``path`` with mode 0600: dumped into a
    fresh 0600 temp file (dump_settings opens with "w", which keeps the
    file's mode), then moved over the old one."""
    tmp = '%s.%d.tmp' % (path, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        client.dump_settings(tmp)
        os.replace(tmp, path)
        if os.name == 'posix':
            os.chmod(path, 0o600)
    except Exception as e:
        sys.stderr.write('session not saved: %s\n' % type(e).__name__)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        sys.stderr.write('usage: ig_worker.py <spec.json> <out.json>\n')
        return 2
    spec_path, out_path = argv
    spec = _read_spec(spec_path)
    if spec is None:
        sys.stderr.write('spec not readable\n')
        return 2
    sources = _load_sources()
    started = sources.iso_now()

    def _finish(batch, rc):
        batch.update(source=InstagrapiSource.name, started_at=started,
                     finished_at=sources.iso_now())
        _write_json(out_path, batch)
        return rc

    def _failed(code, detail, client=None, rc=0):
        used = int(getattr(client, 'private_requests_count', 0) or 0) if client else 0
        return _finish(sources.failed_batch(code, detail, spec.get('accounts'),
                                            requests=used, budget=spec['budget']), rc)

    username, password = _read_credentials()
    try:
        import instagrapi
    except ImportError:
        return _failed('dependency', 'instagrapi is not installed', rc=3)
    if not username or not password:
        return _failed('login', 'no_credentials')
    client = None
    with _logs_without(username, password):
        try:
            client = instagrapi.Client(with_challenge_flow=False,
                                       delay_range=list(spec['delay_range']))
            _load_session(client, spec['session_path'])
            try:
                client.login(username, password)
            except Exception as e:
                return _failed(error_code(e), type(e).__name__, client)
            _save_session(client, spec['session_path'])
            batch = sources.collect(InstagrapiSource(client, sources), spec.get('accounts'),
                                    budget=spec['budget'],
                                    known_song_keys=spec['known_song_keys'],
                                    deadline=time.monotonic() + spec['deadline_s'])
            _save_session(client, spec['session_path'])
            return _finish(batch, 0)
        except Exception as e:
            sys.stderr.write('trends worker failed: %s\n' % type(e).__name__)
            return _failed('unknown', type(e).__name__, client, rc=1)


if __name__ == '__main__':
    sys.exit(main())
