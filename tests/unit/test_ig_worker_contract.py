"""integrations/trends/ig_worker.py: the child central's job starts in its
own venv (``HEVOLVE_TRENDS_PYTHON -I ig_worker.py <spec> <out>``).

instagrapi is never installed into the HARTOS venv (its pydantic and
requests pins conflict, goals-trends 1.7), so these tests stand a fake
``instagrapi`` in through tests.unit.module_swap.swap_modules and drive the
worker's main() in-process.  They pin the contract read from instagrapi
3.0.24's own source: Client(with_challenge_flow=False, delay_range=[3, 8])
so a challenge raises instead of reading stdin; the saved session is
loaded, login() reuses or renews it, and the session is written back with
mode 0600; credentials come from the environment only; one
user_clips_paginated_v1(pk, amount=12) page per account; each exception
class maps to one error code.  One more test runs the real file with
``python -I`` and no instagrapi, which proves it imports nothing from
HARTOS.
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import types

import pytest

from tests.fixtures.trends import build as fx

NOW = 1792000000

# ── a fake instagrapi ────────────────────────────────────────────────────


def _exceptions_module():
    mod = types.ModuleType('instagrapi.exceptions')

    def make(name, *bases):
        cls = type(name, bases or (Exception,), {})
        setattr(mod, name, cls)
        return cls
    ClientError = make('ClientError')
    PrivateError = make('PrivateError', ClientError)
    ChallengeError = make('ChallengeError', PrivateError)
    make('ChallengeRequired', ChallengeError)
    make('CaptchaChallengeRequired', ClientError)
    make('AccountSuspended', ClientError)
    make('TwoFactorRequired', PrivateError)
    make('LoginRequired', PrivateError)
    make('BadPassword', PrivateError)
    make('BadCredentials', PrivateError)
    make('ReloginAttemptExceeded', ClientError)
    make('PleaseWaitFewMinutes', PrivateError)
    make('RateLimitError', PrivateError)
    make('ClientThrottledError', ClientError)
    make('SentryBlock', PrivateError)
    FeedbackRequired = make('FeedbackRequired', PrivateError)
    make('SignupSpamError', FeedbackRequired)
    NotFoundError = make('NotFoundError', PrivateError)
    make('UserNotFound', NotFoundError)
    make('TrackNotFound', NotFoundError)
    make('PrivateAccount', PrivateError)
    make('ClientNotFoundError', ClientError)
    make('ClientConnectionError', ClientError)
    make('ClientRequestTimeout', ClientError)
    make('ClientJSONDecodeError', ClientError)
    make('ClientIncompleteReadError', ClientError)
    make('ClientLoginRequired', ClientError)
    make('ClientUnauthorizedError', ClientError)
    make('ClientForbiddenError', ClientError)
    make('ProxyAddressIsBlocked', PrivateError)
    make('ChallengeSelfieCaptcha', ChallengeError)
    make('UnknownError', PrivateError)
    return mod


class _Media:
    """instagrapi.types.Media as far as the worker uses it."""

    def __init__(self, data):
        self._data = data

    def model_dump(self, mode='python', **_kw):
        return json.loads(json.dumps(self._data))


def _fake_instagrapi(script):
    """``script``: what each call does.  ``login`` / ``clips`` / ``user`` /
    ``track`` map to callables or exceptions; every call is recorded."""
    exc = _exceptions_module()
    calls = {'init': [], 'load_settings': [], 'login': [], 'dump_settings': [],
             'user': [], 'clips': [], 'track': []}

    class Client:
        private_requests_count = 0

        def __init__(self, settings=None, proxy=None, delay_range=None, **kw):
            calls['init'].append(dict(kw, delay_range=delay_range))

        def _spend(self):
            type(self).private_requests_count += 1

        def load_settings(self, path, override_app_version=False):
            calls['load_settings'].append((path, override_app_version))
            with open(path, encoding='utf-8') as fh:
                return json.load(fh)

        def login(self, username=None, password=None, relogin=False, verification_code=''):
            calls['login'].append((username, password))
            self._spend()
            hit = script.get('login')
            if isinstance(hit, BaseException) or (isinstance(hit, type) and issubclass(hit, BaseException)):
                raise hit
            return True

        def dump_settings(self, path):
            calls['dump_settings'].append(path)
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump({'cookies': {'sessionid': 'fixture-session'}}, fh)
            return True

        def user_info_by_username_v1(self, username):
            calls['user'].append(username)
            self._spend()
            hit = script.get('user', {}).get(username)
            if hit is not None:
                raise hit
            return types.SimpleNamespace(pk=str(fx.user_pk(username)))

        def user_clips_paginated_v1(self, user_id, amount=50, end_cursor=''):
            calls['clips'].append((str(user_id), amount))
            self._spend()
            hit = script.get('clips', {}).get(str(user_id))
            if isinstance(hit, BaseException):
                raise hit
            if hit == 'swallowed':                 # instagrapi's own fallback
                return [], None
            return [_Media(r) for r in script.get('reels', {}).get(str(user_id), [])], ''

        def track_info_by_canonical_id(self, music_canonical_id):
            calls['track'].append(music_canonical_id)
            self._spend()
            hit = script.get('track', {}).get(str(music_canonical_id))
            if isinstance(hit, BaseException):
                raise hit
            return _Media(hit)

    mod = types.ModuleType('instagrapi')
    mod.Client = Client
    mod.exceptions = exc
    Client.private_requests_count = 0
    return mod, exc, calls


# ── harness ──────────────────────────────────────────────────────────────

@pytest.fixture
def run(tmp_path, monkeypatch):
    from tests.conftest import refuse_all_network
    refuse_all_network(monkeypatch)
    monkeypatch.setenv('HEVOLVE_IG_RESEARCH_USERNAME', 'research_fixture')
    monkeypatch.setenv('HEVOLVE_IG_RESEARCH_PASSWORD', 'pw-fixture')
    from tests.unit.module_swap import swap_modules
    from integrations.trends import ig_worker

    def _run(script, accounts=('mua_a',), *, budget=60, known=(), session=None):
        mod, exc, calls = _fake_instagrapi(script)
        session_path = str(tmp_path / '.ig_session.json')
        if session is not None:
            with open(session_path, 'w', encoding='utf-8') as fh:
                json.dump(session, fh)
        spec = {'accounts': [a if isinstance(a, dict) else {'handle': a, 'pk': None}
                             for a in accounts],
                'budget': budget, 'known_song_keys': list(known),
                'session_path': session_path, 'delay_range': [3, 8]}
        spec_path, out_path = str(tmp_path / 'spec.json'), str(tmp_path / 'out.json')
        with open(spec_path, 'w', encoding='utf-8') as fh:
            json.dump(spec, fh)
        with swap_modules({'instagrapi': mod, 'instagrapi.exceptions': exc}):
            rc = ig_worker.main([spec_path, out_path])
        out = None
        if os.path.exists(out_path):
            with open(out_path, encoding='utf-8') as fh:
                out = json.load(fh)
        return rc, out, calls, exc, session_path
    return _run


def _reels_for(handle, n=2):
    return [fx.reel('licensed', pk=100 + i, handle=handle, plays=1000 + i, taken_at=NOW)
            for i in range(n)]


def test_the_client_never_resolves_a_challenge_and_paces_itself(run):
    rc, out, calls, _, _ = run({})
    assert rc == 0
    assert calls['init'] == [{'with_challenge_flow': False, 'delay_range': [3, 8]}]
    assert calls['login'] == [('research_fixture', 'pw-fixture')]
    assert out['schema'] == 'hart.trends.batch.v1' and out['error'] is None


def test_the_session_is_reused_and_written_back_private(run):
    rc, out, calls, _, session_path = run({}, session={'cookies': {'sessionid': 'old'}})
    assert calls['load_settings'] == [(session_path, True)]
    assert calls['dump_settings'], 'the renewed session was not saved'
    with open(session_path, encoding='utf-8') as fh:
        assert json.load(fh)['cookies']['sessionid'] == 'fixture-session'
    if os.name == 'posix':
        assert stat.S_IMODE(os.stat(session_path).st_mode) == 0o600
    assert not [n for n in os.listdir(os.path.dirname(session_path)) if n.endswith('.tmp')]


def test_a_damaged_session_file_falls_back_to_a_fresh_login(run, tmp_path):
    path = tmp_path / '.ig_session.json'
    path.write_text('{damaged', encoding='utf-8')
    mod_calls = run({})[2]
    assert mod_calls['login'] == [('research_fixture', 'pw-fixture')]


def test_one_page_of_reels_per_account_slimmed(run):
    pk_a = str(fx.user_pk('mua_a'))
    rc, out, calls, _, _ = run({'reels': {pk_a: _reels_for('mua_a'), '555': _reels_for('mua_b')}},
                               accounts=('mua_a', {'handle': 'mua_b', 'pk': '555'}))
    assert calls['user'] == ['mua_a']                    # mua_b's pk was cached
    assert calls['clips'] == [(pk_a, 12), ('555', 12)]
    rows = {r['handle']: r for r in out['accounts']}
    assert rows['mua_a']['pk'] == pk_a and rows['mua_a']['ok'] is True
    assert len(rows['mua_b']['reels']) == 2
    text = json.dumps(out)
    assert 'example.invalid' not in text and 'MPD' not in text
    assert 'pw-fixture' not in text and 'fixture-session' not in text
    assert (out['accounts_ok'], out['accounts_total']) == (2, 2)
    assert out['requests'] == 1 + 1 + 2                  # login, one pk, two pages


@pytest.mark.parametrize('name, code', [
    ('ChallengeRequired', 'challenge'), ('CaptchaChallengeRequired', 'challenge'),
    ('AccountSuspended', 'challenge'), ('TwoFactorRequired', 'challenge'),
    ('LoginRequired', 'login'), ('BadPassword', 'login'), ('BadCredentials', 'login'),
    ('ReloginAttemptExceeded', 'login'),
    ('PleaseWaitFewMinutes', 'throttled'), ('RateLimitError', 'throttled'),
    ('ClientThrottledError', 'throttled'), ('SentryBlock', 'throttled'),
    ('FeedbackRequired', 'feedback'), ('SignupSpamError', 'feedback'),
    ('UserNotFound', 'not_found'), ('PrivateAccount', 'not_found'),
    ('ClientNotFoundError', 'not_found'),
    ('ClientConnectionError', 'transport'), ('ClientRequestTimeout', 'transport'),
    ('ClientJSONDecodeError', 'transport'), ('ClientIncompleteReadError', 'transport'),
    ('ClientLoginRequired', 'login'), ('ClientUnauthorizedError', 'login'),
    ('ClientForbiddenError', 'login'), ('ProxyAddressIsBlocked', 'challenge'),
    ('ChallengeSelfieCaptcha', 'challenge'), ('TrackNotFound', 'not_found'),
    ('UnknownError', 'unknown'), ('ClientError', 'unknown'),
])
def test_each_instagrapi_error_maps_to_one_code(name, code):
    """Classes are matched by name along the MRO (the real module lives only
    in central's own venv), so a subclass maps with its family."""
    from integrations.trends.ig_worker import error_code
    exc = _exceptions_module()
    assert error_code(getattr(exc, name)('x')) == code
    assert error_code(ConnectionError('reset')) == 'transport'
    assert error_code(TimeoutError('slow')) == 'transport'
    assert error_code(ValueError('bug')) == 'unknown'


def test_a_challenge_at_login_ends_the_run_before_any_account(run):
    exc = _exceptions_module()
    rc, out, calls, _, _ = run({'login': exc.ChallengeRequired('challenge_required')})
    assert rc == 0
    assert out['error'] == 'challenge' and out['error_detail'] == 'ChallengeRequired'
    assert calls['clips'] == [] and calls['user'] == []
    assert out['accounts_ok'] == 0
    assert calls['dump_settings'] == []        # a refused session is not kept


def test_an_unknown_account_is_skipped(run):
    exc = _exceptions_module()
    rc, out, calls, _, _ = run({'user': {'mua_gone': exc.UserNotFound('gone')}},
                               accounts=('mua_gone', 'mua_a'))
    rows = {r['handle']: r for r in out['accounts']}
    assert rows['mua_gone']['error'] == 'not_found'
    assert rows['mua_a']['ok'] is True and out['error'] is None


def test_a_page_instagrapi_swallowed_is_a_transport_failure(run):
    """user_clips_paginated_v1 logs any non-private error and returns
    ([], None); an empty page with no cursor is not 'no reels'."""
    pk_a = str(fx.user_pk('mua_a'))
    rc, out, calls, _, _ = run({'clips': {pk_a: 'swallowed'}})
    assert out['accounts'][0]['error'] == 'transport'


def test_throttling_mid_run_stops_it(run):
    exc = _exceptions_module()
    pk_a = str(fx.user_pk('mua_a'))
    rc, out, calls, _, _ = run({'clips': {pk_a: exc.PleaseWaitFewMinutes('wait')}},
                               accounts=('mua_a', 'mua_b'))
    assert out['error'] == 'throttled'
    assert calls['user'] == ['mua_a']


def test_unknown_canonical_songs_are_looked_up_once(run):
    pk_a = str(fx.user_pk('mua_a'))
    canon = fx.reel('canonical_only', pk=7, handle='mua_a', plays=10, taken_at=NOW)
    track = fx.load()['track']
    rc, out, calls, _, _ = run({'reels': {pk_a: [canon, dict(canon, pk=8)]},
                                'track': {'18200000000000044': track}})
    assert calls['track'] == ['18200000000000044']
    got = out['tracks']['canonical:18200000000000044']
    assert got['title'] == 'Mallige Song' and got['highlight_start_times_in_ms'] == [45000]
    assert 'example.invalid' not in json.dumps(got)


def test_credentials_come_only_from_the_environment(run, monkeypatch):
    monkeypatch.delenv('HEVOLVE_IG_RESEARCH_PASSWORD')
    rc, out, calls, _, _ = run({})
    assert out['error'] == 'login' and out['error_detail'] == 'no_credentials'
    assert calls['init'] == []


def test_bad_arguments_exit_2(tmp_path):
    from integrations.trends import ig_worker
    assert ig_worker.main([]) == 2
    assert ig_worker.main([str(tmp_path / 'missing.json'), str(tmp_path / 'out.json')]) == 2


def test_the_worker_runs_isolated_with_nothing_from_hartos(tmp_path):
    """The real file under ``python -I`` (no PYTHONPATH, no script dir on
    sys.path), with no instagrapi installed: it still starts, writes its
    batch and exits 3 (dependency), so it imports nothing from HARTOS."""
    if importlib.util.find_spec('instagrapi') is not None:
        pytest.skip('instagrapi is installed here; the HARTOS venv must not carry it')
    from integrations.trends import ig_worker
    spec = tmp_path / 'spec.json'
    spec.write_text(json.dumps({'accounts': [{'handle': 'mua_a', 'pk': None}],
                                'budget': 5, 'known_song_keys': [],
                                'session_path': str(tmp_path / 's.json'),
                                'delay_range': [3, 8]}), encoding='utf-8')
    out = tmp_path / 'out.json'
    env = {k: v for k, v in os.environ.items()
           if k in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'HOME')}
    env.update({'HEVOLVE_IG_RESEARCH_USERNAME': 'research_fixture',
                'HEVOLVE_IG_RESEARCH_PASSWORD': 'pw-fixture'})
    proc = subprocess.run([sys.executable, '-I', ig_worker.__file__, str(spec), str(out)],
                          cwd=str(tmp_path), env=env, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 3, proc.stderr[-2000:]
    batch = json.loads(out.read_text(encoding='utf-8'))
    assert batch['error'] == 'dependency' and batch['schema'] == 'hart.trends.batch.v1'
    assert 'pw-fixture' not in proc.stdout + proc.stderr
