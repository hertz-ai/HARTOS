"""crawl4ai is set up on this computer once the owner allows it.

WHAT WENT WRONG (owner's desktop, 15 Nov 2026)
  integrations/web_crawler.py reads a page with crawl4ai when it can be
  imported and otherwise with requests+BeautifulSoup.  Nothing ever installed
  crawl4ai on a desktop, and nothing offered to, so every crawl on build 75
  (and on any fresh install) ran the fallback: static pages read, pages that
  build themselves with JavaScript came back empty.  The owner: "we had that
  as auto setup", "restore auto-setup", "install crawl4ai on D: instead"
  (C: had 4.8 GB free).

THE RULE
  * A crawl that finds crawl4ai missing asks the owner once, through the one
    setup consent card (capability_setup.ask_owner_for_setup, scope
    'crawl:crawl4ai').  This crawl is still answered by the fallback; nothing
    waits on the answer or on the install.
  * On the owner's yes, ONE background install runs: crawl4ai with the host's
    pip runner, then Playwright's Chromium.  A crawl during it does not start
    another; a failed install is not retried in the same run of the app.
  * Whenever the private D: site can be made, both go to D: (the site, and a
    browsers folder beside it, which the crawler then launches from);
    otherwise to the usual places.
  * After a good install the next crawl imports crawl4ai again.

ONE INSTALLER (audit row H3, 22 Nov 2026)
  web_crawler carried the install itself, beside RuntimeToolManager, which
  owner ruling A16 names the one place a pip-in-isolated-env setup belongs.
  The steps are now RuntimeToolManager's 'crawl4ai' row (download_type
  'pip'); web_crawler asks it for the consent and the one background setup.
  Every test above the "ONE INSTALLER" block drives the crawl from outside
  (the consent answer, the host's pip runner, the Chromium command), so it
  ran unchanged against web_crawler's own installer and runs against the
  row.

    python -m pytest tests/unit/test_crawl4ai_is_set_up_when_the_owner_allows.py -q
"""
import os
import sys
import threading
import types

import pytest

from integrations import web_crawler as wc
from integrations.service_tools import runtime_manager as rm

URL = 'https://example.com'


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.setattr(wc, '_crawler_available', False)
    monkeypatch.setattr(wc, '_setup_thread', None, raising=False)
    monkeypatch.setattr(wc, '_setup_future', None, raising=False)
    monkeypatch.setattr(wc, '_setup_failed', False)
    monkeypatch.setattr(wc, '_fallback_fetch', lambda url, timeout, log: {
        'success': True, 'url': url, 'markdown': 'static text', 'word_count': 2})
    # RuntimeToolManager's state file and its setups in flight are this
    # test's own; crawl4ai is not importable unless a test says so.
    monkeypatch.setattr(rm, 'STATE_FILE', tmp_path / 'tool_state.json')
    monkeypatch.setattr(rm.runtime_tool_manager, '_setups', {})
    monkeypatch.setitem(sys.modules, 'crawl4ai', None)
    # No test here may start a real install.  Every way out to pip or to
    # Playwright is refused unless a fixture below replaces it (the frozen
    # host's runner exists only as the ``host`` fixture's fake).
    from integrations.coding_agent import installer

    def refused(*args, **kwargs):
        raise AssertionError(f'a real install was about to run: {args!r}')
    monkeypatch.setattr(installer, 'pip_install', refused)
    monkeypatch.setattr(wc, 'run_bounded', refused, raising=False)
    monkeypatch.setattr(rm, 'run_bounded', refused, raising=False)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    # A frozen host installs with Nunba's tts.package_installer._run_pip,
    # which nothing above refuses: where Nunba can be imported, a test
    # without the ``host`` fixture would have run a real pip (review of
    # f9b6424e4).  The module is not importable unless ``host`` fakes it.
    monkeypatch.setitem(sys.modules, 'tts', None)
    monkeypatch.setitem(sys.modules, 'tts.package_installer', None)
    # And no browser this computer really has is found: where Playwright
    # looks is this test's own empty folder.
    monkeypatch.delenv('PLAYWRIGHT_BROWSERS_PATH', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'localappdata'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    yield
    _wait()


def _wait():
    """Until no background setup runs (the thread is named the same by
    web_crawler's own installer and by RuntimeToolManager.setup_tool_async)."""
    for t in threading.enumerate():
        if t.name == 'crawl4ai-setup' and t is not threading.current_thread():
            t.join(10)


def _answer(monkeypatch, answer, asked=None):
    from integrations.agent_engine import capability_setup

    def ask(capability, *, reason):
        if asked is not None:
            asked.append((capability, reason))
        return answer
    monkeypatch.setattr(capability_setup, 'ask_owner_for_setup', ask)


@pytest.fixture
def host(monkeypatch, tmp_path):
    """A frozen desktop host: Nunba's installer module with a D: root,
    recording what pip and Playwright were run with.  ``host.pip`` is what
    its pip runner answers (or raises, or waits on)."""
    d_root = tmp_path / 'd' / '.nunba'
    pkg = types.ModuleType('tts')
    pi = types.ModuleType('tts.package_installer')
    pi._D_DRIVE_ROOT = str(d_root)
    pi.private_ok = True

    def make_private():
        (d_root / 'site-packages').mkdir(parents=True, exist_ok=True)
        return pi.private_ok
    pi._make_private_d_drive_site = make_private
    state = types.SimpleNamespace(
        pi=pi, runs=[], d_root=d_root, pip=(True, 'ok'), chromium_rc=0,
        release=None, embed=str(tmp_path / 'python-embed' / 'python.exe'))

    def run_pip(args, **kw):
        state.runs.append(('pip', list(args), dict(kw)))
        if state.release is not None:
            state.release.wait(10)
        if isinstance(state.pip, BaseException):
            raise state.pip
        return state.pip
    pi._run_pip = run_pip
    pi.get_embed_python = lambda: state.embed
    pkg.package_installer = pi
    monkeypatch.setitem(sys.modules, 'tts', pkg)
    monkeypatch.setitem(sys.modules, 'tts.package_installer', pi)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'platform', 'win32')

    def run_bounded(cmd, timeout=5.0, **kw):
        state.runs.append(('chromium', list(cmd), (kw.get('env') or {}).get(
            'PLAYWRIGHT_BROWSERS_PATH'), timeout))
        return types.SimpleNamespace(returncode=state.chromium_rc, timed_out=False,
                                     stdout='', stderr='no browser for you')
    # Wherever the Chromium step lives.
    monkeypatch.setattr(wc, 'run_bounded', run_bounded, raising=False)
    monkeypatch.setattr(rm, 'run_bounded', run_bounded, raising=False)
    monkeypatch.delenv('PLAYWRIGHT_BROWSERS_PATH', raising=False)
    state.of = lambda kind: [r for r in state.runs if r[0] == kind]
    return state


def _crawl_and_wait(url=URL):
    result = wc.crawl_url(url, timeout=5)
    _wait()
    return result


# ── The offer ──────────────────────────────────────────────────────────────

def test_a_crawl_with_crawl4ai_missing_asks_the_owner_once_and_answers_now(monkeypatch, host):
    asked = []
    _answer(monkeypatch, 'asked', asked)
    result = _crawl_and_wait()
    assert result['success'] and result['markdown'] == 'static text'
    assert [c for c, _ in asked] == ['crawl:crawl4ai']
    assert 'JavaScript' in asked[0][1] and 'Chromium' in asked[0][1]
    assert 'Asked the owner to set crawl4ai up' in result['progress']
    assert host.runs == [], 'nothing is installed before the owner says yes'


def test_a_batch_crawl_with_crawl4ai_missing_asks_too(monkeypatch, host):
    asked = []
    _answer(monkeypatch, 'asked', asked)
    results = wc.crawl_urls([URL, 'https://example.org'], timeout=5)
    assert [r['markdown'] for r in results] == ['static text', 'static text']
    assert [c for c, _ in asked] == ['crawl:crawl4ai']
    assert host.runs == []


def test_a_declined_setup_installs_nothing(monkeypatch, host):
    _answer(monkeypatch, 'declined')
    wc.crawl_url(URL, timeout=5)
    wc.crawl_urls([URL, 'https://example.org'], timeout=5)
    _wait()
    assert host.runs == []


def test_a_card_that_cannot_be_asked_installs_nothing_and_fails_no_crawl(monkeypatch, host):
    from integrations.agent_engine import capability_setup

    def ask(capability, *, reason):
        raise RuntimeError('no card service')
    monkeypatch.setattr(capability_setup, 'ask_owner_for_setup', ask)
    result = _crawl_and_wait()
    assert result['markdown'] == 'static text' and host.runs == []


# ── The one background install ─────────────────────────────────────────────

def test_the_owners_yes_starts_one_install_however_many_crawls_ask(monkeypatch, host):
    _answer(monkeypatch, 'granted')
    host.release = threading.Event()
    try:
        first = wc.crawl_url(URL, timeout=5)
        second = wc.crawl_url('https://example.org', timeout=5)
        wc.crawl_urls(['https://example.net'], timeout=5)
        assert first['success'] and first['markdown'] == 'static text'
        assert 'Setting crawl4ai up in the background' in first['progress']
        assert 'Setting crawl4ai up' not in second['progress'], 'it is already running'
        for _ in range(100):
            if host.of('pip'):
                break
            threading.Event().wait(0.02)
        assert len(host.of('pip')) == 1
    finally:
        host.release.set()
        _wait()
    assert len(host.of('pip')) == 1 and len(host.of('chromium')) == 1


def test_a_failed_install_is_not_retried_in_the_same_run(monkeypatch, host):
    asked = []
    _answer(monkeypatch, 'granted', asked)
    host.pip = (False, 'ERROR: could not reach the package index')
    _crawl_and_wait()
    asks = len(asked)
    _crawl_and_wait()
    assert len(asked) == asks, 'a failed setup is not offered again in this run'
    assert len(host.of('pip')) == 1, host.runs
    assert host.of('chromium') == [], 'no browser after a failed package'
    assert wc._crawler_available is False


def test_a_pip_runner_that_raises_is_a_failed_install(monkeypatch, host):
    _answer(monkeypatch, 'granted')
    host.pip = OSError('python-embed is missing')
    assert _crawl_and_wait()['markdown'] == 'static text'
    _crawl_and_wait()
    assert len(host.of('pip')) == 1
    assert wc._crawler_available is False


def test_a_failed_browser_install_is_a_failed_install(monkeypatch, host):
    _answer(monkeypatch, 'granted')
    host.chromium_rc = 1
    _crawl_and_wait()
    _crawl_and_wait()
    assert len(host.of('pip')) == 1 and len(host.of('chromium')) == 1
    assert wc._crawler_available is False


def test_a_good_install_lets_the_next_crawl_import_crawl4ai(monkeypatch, host):
    _answer(monkeypatch, 'granted')
    _crawl_and_wait()
    assert wc._crawler_available is None, 'the next check imports again'


# ── Where it goes, and what is run ─────────────────────────────────────────

def test_crawl4ai_and_its_browser_go_to_the_private_d_site(monkeypatch, host):
    """Owner, 2026-11-16: "Always D: when possible"."""
    _answer(monkeypatch, 'granted')
    _crawl_and_wait()
    pip, chromium = host.of('pip'), host.of('chromium')
    assert len(pip) == 1 and pip[0][1] == [
        'install', 'crawl4ai', '--target', str(host.d_root / 'site-packages')]
    assert pip[0][2] == {'timeout': 1800, 'stall_timeout': 600}
    assert len(chromium) == 1
    assert chromium[0][1] == [host.embed, '-m', 'playwright', 'install', 'chromium']
    assert chromium[0][2] == str(host.d_root / 'ms-playwright')
    assert chromium[0][3] == 1800
    assert wc._crawler_available is None


def test_a_d_site_that_cannot_be_made_private_is_not_used(monkeypatch, host):
    _answer(monkeypatch, 'granted')
    host.pi.private_ok = False
    _crawl_and_wait()
    pip, chromium = host.of('pip'), host.of('chromium')
    assert pip and pip[0][1] == ['install', 'crawl4ai']
    assert chromium and chromium[0][2] is None, "Playwright's own folder"


def test_off_windows_the_usual_places_are_used(monkeypatch, host):
    _answer(monkeypatch, 'granted')
    monkeypatch.setattr(sys, 'platform', 'linux')
    _crawl_and_wait()
    assert host.of('pip')[0][1] == ['install', 'crawl4ai']
    assert host.of('chromium')[0][2] is None


def test_with_the_package_present_the_setup_installs_only_the_browser(monkeypatch, host):
    """Live on build 75: the pip step landed and the browser did not."""
    _answer(monkeypatch, 'granted')
    monkeypatch.setitem(sys.modules, 'crawl4ai', types.ModuleType('crawl4ai'))
    _crawl_and_wait()
    assert host.of('pip') == []
    chromium = host.of('chromium')
    assert len(chromium) == 1 and chromium[0][2] == str(host.d_root / 'ms-playwright')
    assert wc._crawler_available is None


def test_from_source_the_package_goes_through_the_one_pip_install(monkeypatch, host):
    """Not frozen: integrations.coding_agent.installer.pip_install and this
    interpreter."""
    from integrations.coding_agent import installer
    _answer(monkeypatch, 'granted')
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    asked = []
    monkeypatch.setattr(installer, 'pip_install',
                        lambda packages: asked.append(packages) or {'success': True})
    _crawl_and_wait()
    assert asked == ['crawl4ai'] and host.of('pip') == []
    assert host.of('chromium')[0][1][0] == sys.executable
    assert wc._crawler_available is None


def test_the_crawler_launches_the_browser_installed_on_d(host, monkeypatch):
    (host.d_root / 'ms-playwright' / 'chromium-1').mkdir(parents=True)
    wc._use_installed_browsers()
    assert os.environ.get('PLAYWRIGHT_BROWSERS_PATH') == str(host.d_root / 'ms-playwright')
    monkeypatch.delenv('PLAYWRIGHT_BROWSERS_PATH', raising=False)


def test_a_browser_folder_already_named_is_kept(host, monkeypatch):
    (host.d_root / 'ms-playwright' / 'chromium-1').mkdir(parents=True)
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', 'E:\\browsers')
    wc._use_installed_browsers()
    assert os.environ['PLAYWRIGHT_BROWSERS_PATH'] == 'E:\\browsers'


# ── Ready means the package AND its browser (forward fix, 2026-11-16) ──────
# Live on build 75, 06:28-06:33: the setup's pip step landed crawl4ai 0.9.5
# but was recorded failed before Chromium (a false import-check failure,
# Nunba dbe17a7f).  An import-only availability check would have called
# crawl4ai ready after the next restart and never offered the setup again,
# while every crawl failed for want of a browser.

def test_crawl4ai_without_its_browser_is_not_ready_and_is_offered(monkeypatch):
    asked = []
    _answer(monkeypatch, 'asked', asked)
    monkeypatch.setattr(wc, '_crawler_available', None)
    monkeypatch.setattr(wc, '_crawl4ai_importable', lambda: True)
    monkeypatch.setattr(wc, '_browser_installed', lambda: False)
    result = wc.crawl_url(URL, timeout=5)
    assert result['markdown'] == 'static text', 'a crawl was tried with no browser'
    assert [c for c, _ in asked] == ['crawl:crawl4ai']


def test_a_browser_in_playwrights_default_folder_is_found(monkeypatch, tmp_path):
    monkeypatch.delenv('PLAYWRIGHT_BROWSERS_PATH', raising=False)
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setitem(sys.modules, 'tts.package_installer',
                        types.SimpleNamespace(_D_DRIVE_ROOT=str(tmp_path / 'nod')))
    assert wc._browser_installed() is False
    (tmp_path / 'ms-playwright' / 'chromium-1200').mkdir(parents=True)
    assert wc._browser_installed() is True


def test_ready_needs_both_the_package_and_the_browser(monkeypatch):
    for importable, browser, ready in ((False, True, False), (True, False, False),
                                       (True, True, True)):
        monkeypatch.setattr(wc, '_crawler_available', None)
        monkeypatch.setattr(wc, '_crawl4ai_importable', lambda v=importable: v)
        monkeypatch.setattr(wc, '_browser_installed', lambda v=browser: v)
        assert wc._check_available() is ready, (importable, browser)


# ── ONE INSTALLER: the steps are RuntimeToolManager's 'crawl4ai' row ───────

def test_the_setup_is_a_runtime_tool_manager_row():
    row = rm.TOOL_CONFIGS['crawl4ai']
    assert row['download_type'] == 'pip' and row['pip_package'] == 'crawl4ai'
    assert row['consent'] == 'crawl:crawl4ai' and 'JavaScript' in row['consent_reason']
    assert row['on_demand'] is True, 'set up only when a crawl asks'


def test_web_crawler_keeps_no_installer_of_its_own():
    for name in ('_install_crawl4ai', '_pip_install_crawl4ai', '_install_chromium',
                 '_setup_target'):
        assert not hasattr(wc, name), name


def test_setup_tool_runs_the_same_steps_for_any_caller(monkeypatch, host):
    """POST /api/tools/crawl4ai/setup and an agent's tool reach setup_tool."""
    _answer(monkeypatch, 'granted')
    result = rm.runtime_tool_manager.setup_tool('crawl4ai')
    assert result.get('ready') is True and not result.get('error'), result
    assert [r[0] for r in host.runs] == ['pip', 'chromium']
    assert host.of('pip')[0][1] == [
        'install', 'crawl4ai', '--target', str(host.d_root / 'site-packages')]


def test_setup_tool_installs_nothing_without_the_owners_yes(monkeypatch, host):
    asked = []
    _answer(monkeypatch, 'asked', asked)
    result = rm.runtime_tool_manager.setup_tool('crawl4ai')
    assert result.get('consent') == 'asked' and result.get('error')
    assert host.runs == [] and [c for c, _ in asked] == ['crawl:crawl4ai']


def test_a_failed_step_is_answered_as_a_failed_setup(monkeypatch, host):
    _answer(monkeypatch, 'granted')
    host.pip = (False, 'ERROR: no index')
    result = rm.runtime_tool_manager.setup_tool('crawl4ai')
    assert result.get('error') and not result.get('ready')
    host.pip = OSError('python-embed is missing')
    result = rm.runtime_tool_manager.setup_tool('crawl4ai')
    assert 'python-embed is missing' in result.get('error', '') and not result.get('ready')


def test_the_row_is_ready_when_the_crawler_says_so(monkeypatch):
    for importable, browser, ready in ((False, True, False), (True, False, False),
                                       (True, True, True)):
        monkeypatch.setattr(wc, '_crawl4ai_importable', lambda v=importable: v)
        monkeypatch.setattr(wc, '_browser_installed', lambda v=browser: v)
        assert rm.runtime_tool_manager.tool_ready('crawl4ai') is ready
    status = rm.runtime_tool_manager.get_tool_status('crawl4ai')
    assert status['on_demand'] is True and status['ready'] is True


# ── One setup of a tool at a time, whoever asks (forward fix of f9b6424e4) ──
# Review of f9b6424e4, measured: POST /api/tools/crawl4ai/setup called
# setup_tool beside the crawl's background setup, and install_pip_tool had no
# lock, so two pip installs and then two Chromium downloads ran at once into
# the same D: folders.

def _counted(live, run):
    """``run``, counting how many calls are inside it at once."""
    def step(*args, **kwargs):
        with live['gate']:
            live['now'] += 1
            live['most'] = max(live['most'], live['now'])
        try:
            return run(*args, **kwargs)
        finally:
            with live['gate']:
                live['now'] -= 1
    return step


def _set_up_once_chromium_ran(monkeypatch, host):
    """The row is ready as soon as a Chromium step has run."""
    monkeypatch.setitem(rm.TOOL_CONFIGS['crawl4ai'], 'pip_ready',
                        lambda: bool(host.of('chromium')))


def test_two_setups_of_one_tool_never_install_at_once(monkeypatch, host):
    _answer(monkeypatch, 'granted')
    _set_up_once_chromium_ran(monkeypatch, host)
    live = {'gate': threading.Lock(), 'now': 0, 'most': 0}
    host.pi._run_pip = _counted(live, host.pi._run_pip)
    monkeypatch.setattr(rm, 'run_bounded', _counted(live, rm.run_bounded))
    host.release = threading.Event()
    answers = []
    callers = [threading.Thread(
        target=lambda: answers.append(rm.runtime_tool_manager.setup_tool('crawl4ai')))
        for _ in range(2)]
    try:
        for t in callers:
            t.start()
        for _ in range(250):
            if host.of('pip'):
                break
            threading.Event().wait(0.02)
        threading.Event().wait(0.5)     # time for the second caller to arrive
        pips_while_the_first_runs = len(host.of('pip'))
    finally:
        host.release.set()
        for t in callers:
            t.join(15)
    assert pips_while_the_first_runs == 1, 'a second pip started beside the first'
    assert live['most'] == 1, 'two install steps ran at once'
    # The caller that waited finds the tool set up and installs nothing.
    assert [r[0] for r in host.runs] == ['pip', 'chromium'], host.runs
    assert len(answers) == 2 and all(a.get('ready') is True and not a.get('error')
                                     for a in answers), answers


def test_a_tool_that_is_set_up_installs_nothing_again(monkeypatch, host):
    """setup_tool is idempotent: a set-up tool answers at once."""
    _answer(monkeypatch, 'granted')
    monkeypatch.setitem(rm.TOOL_CONFIGS['crawl4ai'], 'pip_ready', lambda: True)
    result = rm.runtime_tool_manager.setup_tool('crawl4ai')
    assert result.get('ready') is True and result.get('downloaded') is True, result
    assert host.runs == []


def test_one_tools_setup_does_not_wait_for_anothers(monkeypatch, host):
    """The lock is per tool."""
    _answer(monkeypatch, 'granted')
    _set_up_once_chromium_ran(monkeypatch, host)
    held = threading.Event()
    plain_pip = host.pi._run_pip

    def run_pip(args, **kw):
        if 'crawl4ai' in args:
            host.runs.append(('pip', list(args), dict(kw)))
            held.wait(10)
            return True, 'ok'
        return plain_pip(args, **kw)
    host.pi._run_pip = run_pip
    first = threading.Thread(target=rm.runtime_tool_manager.setup_tool, args=('crawl4ai',))
    other = {}
    second = threading.Thread(target=lambda: other.update(done=rm.install_pip_tool(
        'another-tool', {'pip_package': 'no_such_package_for_this_test'})))
    try:
        first.start()
        for _ in range(250):
            if host.of('pip'):
                break
            threading.Event().wait(0.02)
        second.start()
        second.join(5)
        finished_while_the_first_ran = other.get('done')
    finally:
        held.set()
        first.join(15)
        second.join(15)
    assert finished_while_the_first_ran is True
    assert [r[1][1] for r in host.of('pip')] == ['crawl4ai', 'no_such_package_for_this_test']


def test_the_setup_route_joins_the_one_setup_per_tool():
    """POST /api/tools/<name>/setup takes the manager's setup in flight
    (setup_tool_async), as the crawl does; it called setup_tool itself and
    ran a second setup beside the crawl's."""
    import ast
    from pathlib import Path
    source = (Path(rm.__file__).resolve().parents[2]
              / 'hart_intelligence_entry.py').read_text(encoding='utf-8')
    route = next(n for n in ast.walk(ast.parse(source))
                 if isinstance(n, ast.FunctionDef) and n.name == 'tools_setup')
    called = [n.func.attr for n in ast.walk(route)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert 'setup_tool_async' in called and 'setup_tool' not in called, called
    # The answer is the setup's own dict: the Future's result.
    assert 'result' in called, called


def test_a_setup_asked_for_while_one_runs_is_answered_by_that_one(monkeypatch, host):
    """What the route now gets: one setup, one answer for both askers."""
    _answer(monkeypatch, 'granted')
    _set_up_once_chromium_ran(monkeypatch, host)
    host.release = threading.Event()
    try:
        crawl = rm.runtime_tool_manager.setup_tool_async('crawl4ai')
        route = rm.runtime_tool_manager.setup_tool_async('crawl4ai')
        assert route is crawl
    finally:
        host.release.set()
    assert route.result(15).get('ready') is True
    assert [r[0] for r in host.runs] == ['pip', 'chromium']


# ── The status says what the setup said ────────────────────────────────────
# GET /api/tools/status answered "downloaded": false for crawl4ai right
# after a good setup: it asked the models folder, where a pip row puts
# nothing.

def test_the_status_says_downloaded_when_the_row_is_ready(monkeypatch):
    manager = rm.runtime_tool_manager
    for importable, browser in ((True, True), (True, False), (False, True)):
        monkeypatch.setattr(wc, '_crawl4ai_importable', lambda v=importable: v)
        monkeypatch.setattr(wc, '_browser_installed', lambda v=browser: v)
        status = manager.get_tool_status('crawl4ai')
        assert status['downloaded'] is (importable and browser), (importable, browser, status)
        assert status['ready'] is status['downloaded']


def test_a_pip_rows_status_does_not_ask_the_models_folder(monkeypatch):
    """And every other row's still does."""
    manager = rm.runtime_tool_manager
    asked = []
    monkeypatch.setattr(manager, 'storage', types.SimpleNamespace(
        is_downloaded=lambda name: asked.append(name) or True,
        npm_install_is_complete=lambda name, project: False))
    monkeypatch.setattr(wc, '_crawl4ai_importable', lambda: False)
    assert manager.get_tool_status('crawl4ai')['downloaded'] is False
    assert asked == []
    npm_row = next(n for n, c in rm.TOOL_CONFIGS.items() if c.get('download_type') == 'npm')
    assert manager.get_tool_status(npm_row)['downloaded'] is True
    assert asked == [npm_row]


def test_the_status_after_a_good_setup_is_downloaded(monkeypatch, host):
    _answer(monkeypatch, 'granted')
    monkeypatch.setattr(wc, '_crawl4ai_importable', lambda: bool(host.of('pip')))
    monkeypatch.setattr(wc, '_browser_installed', lambda: bool(host.of('chromium')))
    manager = rm.runtime_tool_manager
    assert manager.get_tool_status('crawl4ai')['downloaded'] is False
    result = manager.setup_tool('crawl4ai')
    assert result.get('downloaded') is True and result.get('ready') is True, result
    assert manager.get_tool_status('crawl4ai')['downloaded'] is True


# ── No test here can reach a real installer ────────────────────────────────

def test_without_the_host_fixture_no_installer_can_be_reached(monkeypatch):
    """A real ``pip install`` ran from a test once (21 Nov 2026).  With the
    owner's yes and no fake host, every way to an installer is shut: the
    setup fails and nothing is run."""
    assert sys.modules.get('tts', 'absent') is None
    assert sys.modules.get('tts.package_installer', 'absent') is None
    _answer(monkeypatch, 'granted')
    result = rm.runtime_tool_manager.setup_tool('crawl4ai')
    assert result.get('error') and not result.get('ready'), result
