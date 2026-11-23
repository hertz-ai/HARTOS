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
    monkeypatch.setattr(wc, '_setup_failure', '', raising=False)
    # A failed setup's line is masked by the credential vault before an
    # agent reads it: never the real vault here (a test sets its own).
    import core.tool_logging as tool_logging
    monkeypatch.setattr(tool_logging, 'credential_vault', lambda: None)
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


# ── The route itself, through a Flask test client (review of 69178218d) ────
# The route's only test read its source.  These POST to it: the route's own
# function and its own error wrapper, taken from hart_intelligence_entry.py
# and registered under the rule and methods written there, on an app that
# holds nothing else (importing the whole app starts the node).

@pytest.fixture
def tools_client():
    import ast
    import functools
    from pathlib import Path
    import flask
    source = (Path(rm.__file__).resolve().parents[2]
              / 'hart_intelligence_entry.py').read_text(encoding='utf-8')
    defs = {n.name: n for n in ast.parse(source).body
            if isinstance(n, ast.FunctionDef) and n.name in ('_json_endpoint', 'tools_setup')}
    view = defs['tools_setup']
    route = next(d for d in view.decorator_list if isinstance(d, ast.Call))
    assert ast.unparse(route.func) == 'app.route'
    assert [ast.unparse(d) for d in view.decorator_list][1:] == ['_json_endpoint']
    rule = ast.literal_eval(route.args[0])
    options = {k.arg: ast.literal_eval(k.value) for k in route.keywords}
    view.decorator_list = []
    module = ast.Module(body=[defs['_json_endpoint'], view], type_ignores=[])
    names = {'wraps': functools.wraps, 'jsonify': flask.jsonify}
    exec(compile(ast.fix_missing_locations(module), 'hart_intelligence_entry.py', 'exec'), names)
    app = flask.Flask('tools_setup_route')
    app.add_url_rule(rule, 'tools_setup', names['_json_endpoint'](names['tools_setup']),
                     **options)
    # No "with": one test posts from a second thread, and a client held
    # open keeps the first thread's request context.
    client = app.test_client()
    client.rule = rule
    return client


def _count_setups(monkeypatch):
    """How often the manager's two setup entries are called, and for what."""
    manager = rm.runtime_tool_manager
    calls = {'setup_tool_async': [], 'setup_tool': []}
    for name in calls:
        def counted(tool_name, _real=getattr(manager, name), _name=name):
            calls[_name].append(tool_name)
            return _real(tool_name)
        monkeypatch.setattr(manager, name, counted)
    return calls


def test_a_post_to_the_setup_route_sets_the_tool_up_once(monkeypatch, host, tools_client):
    assert tools_client.rule == '/api/tools/<tool_name>/setup'
    _answer(monkeypatch, 'granted')
    _set_up_once_chromium_ran(monkeypatch, host)
    calls = _count_setups(monkeypatch)
    answer = tools_client.post('/api/tools/crawl4ai/setup')
    assert answer.status_code == 200, answer.get_json()
    assert answer.get_json() == {'tool': 'crawl4ai', 'downloaded': True, 'ready': True}
    assert calls == {'setup_tool_async': ['crawl4ai'], 'setup_tool': ['crawl4ai']}
    assert [r[0] for r in host.runs] == ['pip', 'chromium']
    assert tools_client.get('/api/tools/crawl4ai/setup').status_code == 405


def test_a_post_while_a_crawls_setup_runs_is_answered_by_that_setup(monkeypatch, host,
                                                                   tools_client):
    _answer(monkeypatch, 'granted')
    _set_up_once_chromium_ran(monkeypatch, host)
    calls = _count_setups(monkeypatch)
    host.release = threading.Event()
    answers = []
    post = threading.Thread(
        target=lambda: answers.append(tools_client.post('/api/tools/crawl4ai/setup')))
    try:
        wc.crawl_url(URL, timeout=5)            # the owner said yes: its setup starts
        for _ in range(250):
            if host.of('pip'):
                break
            threading.Event().wait(0.02)
        post.start()
        threading.Event().wait(0.5)
        pips_while_the_crawls_runs = len(host.of('pip'))
        still_waiting = post.is_alive()
    finally:
        host.release.set()
        post.join(15)
        _wait()
    assert pips_while_the_crawls_runs == 1 and still_waiting
    assert answers[0].status_code == 200 and answers[0].get_json().get('ready') is True
    assert calls['setup_tool'] == ['crawl4ai'], 'one setup ran for the crawl and the POST'
    assert [r[0] for r in host.runs] == ['pip', 'chromium']


# ── A name that is no tool leaves nothing behind ───────────────────────────
# Every posted name became an entry in the manager's setups in flight and
# stayed: 300 unknown names, 300 entries.

def test_a_post_for_a_name_that_is_no_tool_is_refused_and_keeps_nothing(monkeypatch,
                                                                       tools_client):
    manager = rm.runtime_tool_manager
    before = threading.active_count()
    for i in range(300):
        answer = tools_client.post(f'/api/tools/no-such-tool-{i}/setup')
        assert answer.status_code == 500
        assert answer.get_json() == {'error': f'Unknown tool: no-such-tool-{i}'}
    assert manager._setups == {}
    assert threading.active_count() == before, 'no setup thread for a name that is no tool'
    done = manager.setup_tool_async('no-such-tool')
    assert done.done() and done.result() == {'error': 'Unknown tool: no-such-tool'}


def test_a_known_tools_setup_is_still_the_one_in_flight(monkeypatch, host):
    """Carried over: a tool in the table keeps its one entry."""
    _answer(monkeypatch, 'asked')
    manager = rm.runtime_tool_manager
    manager.setup_tool_async('crawl4ai').result(15)
    assert list(manager._setups) == ['crawl4ai']


# ── A failed setup says why, and what to do (review of 69178218d) ──────────
# pip's and Playwright's words reached only the log; the caller, and the
# route, got the bare "setting crawl4ai up failed".

PIP_OUTPUT = (
    'Collecting crawl4ai\n'
    '  Looking in indexes: https://buyer:hunter2secret@index.example.org/simple\n'
    'ERROR: Could not find a version that satisfies the requirement crawl4ai\n'
    'ERROR: No matching distribution found for crawl4ai '
    '(from -r C:\\Users\\somebody\\AppData\\Local\\Temp\\pip-req\\requirements.txt, '
    'index https://buyer:hunter2secret@index.example.org/simple, '
    'key ghp_abcdefghijklmnopqrstuvwxyz0123456789)\n'
    '[notice] A new release of pip is available\n')


def test_a_failed_pip_step_answers_pips_reason_and_what_to_do(monkeypatch, host, tools_client):
    _answer(monkeypatch, 'granted')
    host.pip = (False, PIP_OUTPUT)
    answer = tools_client.post('/api/tools/crawl4ai/setup')
    assert answer.status_code == 500
    error = answer.get_json()['error']
    assert error.startswith('setting crawl4ai up failed: pip could not install crawl4ai: '
                            'ERROR: No matching distribution found for crawl4ai'), error
    assert error.endswith('Check the internet connection and the free disk space, '
                          'then set crawl4ai up again.'), error
    # No secret, and no folder of this computer: the file's own name only.
    assert 'hunter2secret' not in error and 'buyer' not in error
    assert 'ghp_abcdefghijklmnopqrstuvwxyz0123456789' not in error
    assert 'somebody' not in error and 'AppData' not in error and 'C:' not in error
    assert 'requirements.txt' in error and 'https://index.example.org/simple' in error
    assert answer.get_json().get('ready') is None and host.of('chromium') == []


def test_a_failed_browser_step_answers_its_reason(monkeypatch, host):
    _answer(monkeypatch, 'granted')
    host.chromium_rc = 1
    result = rm.runtime_tool_manager.setup_tool('crawl4ai')
    assert result['error'] == (
        "setting crawl4ai up failed: 'playwright install chromium' failed: no browser for "
        "you. Check the internet connection and the free disk space, then set crawl4ai "
        "up again."), result
    assert not result.get('ready')


def test_a_failed_pip_step_from_source_answers_its_reason(monkeypatch, host):
    from integrations.coding_agent import installer
    _answer(monkeypatch, 'granted')
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    monkeypatch.setattr(installer, 'pip_install', lambda packages: {
        'success': False, 'error': 'pip install timed out (120s)'})
    result = rm.runtime_tool_manager.setup_tool('crawl4ai')
    assert result['error'] == (
        'setting crawl4ai up failed: pip could not install crawl4ai: pip install timed out '
        '(120s). Check the internet connection and the free disk space, then set crawl4ai '
        'up again.'), result


@pytest.mark.parametrize('output, reason', [
    ('', ''),
    (None, ''),
    ('one line', 'one line'),
    ('ERROR: first\nplain last line', 'ERROR: first'),
    ('saved to /home/somebody/.cache/pip/wheels/x.whl failed.', 'saved to x.whl failed'),
    ('see https://pypi.org/simple/crawl4ai/ for it', 'see https://pypi.org/simple/crawl4ai/ for it'),
    ('x' * 400, 'x' * 300),
])
def test_a_setup_reason_is_one_safe_line(output, reason):
    assert rm._setup_reason(output) == reason


def test_a_failed_step_with_no_output_still_says_what_to_do(monkeypatch, host):
    _answer(monkeypatch, 'granted')
    host.pip = (False, '')
    result = rm.runtime_tool_manager.setup_tool('crawl4ai')
    assert result['error'] == (
        'setting crawl4ai up failed: pip could not install crawl4ai (its output is in the '
        'log). Check the internet connection and the free disk space, then set crawl4ai up '
        'again.'), result


def test_a_reason_that_cannot_be_made_safe_is_withheld(monkeypatch):
    monkeypatch.setitem(sys.modules, 'security.secret_redactor', None)
    assert rm._setup_reason('ERROR: token ghp_abcdefghijklmnopqrstuvwxyz0123456789') == ''


# ── The agent is told why the setup failed (owner, 22 Nov 2026) ────────────
# "Real failure reasons should be available to the agent so that it can ask
# correct questions".  The reason reached the log and POST
# /api/tools/crawl4ai/setup.  An agent's crawl after a failed setup was read
# without JavaScript and said nothing about why.  It is in the crawl's
# progress now, which each crawl tool hands the agent: crawl_url and
# crawl_urls (the 'progress' key, printed by Data_Extraction_From_URL and
# the search crawl), crawl_url_for_agent and crawl_urls_for_agent (the
# crawl4ai_crawl tool's text).

PIP_SAID = ('pip could not install crawl4ai: ERROR: No matching distribution found '
            'for crawl4ai')
WHAT_TO_DO = ('Check the internet connection and the free disk space, then set '
              'crawl4ai up again.')


def _a_setup_that_failed(monkeypatch, host):
    _answer(monkeypatch, 'granted')
    host.pip = (False, PIP_OUTPUT)
    first = _crawl_and_wait()
    assert wc._setup_failed is True
    return first


def _says_why(text):
    assert 'crawl4ai could not be set up on this computer' in text, text
    assert 'read without JavaScript' in text
    assert PIP_SAID in text and WHAT_TO_DO in text, text
    # the same safe reason the route answers: no secret, no folder
    assert 'hunter2secret' not in text and 'buyer' not in text
    assert 'ghp_abcdefghijklmnopqrstuvwxyz0123456789' not in text
    assert 'somebody' not in text and 'AppData' not in text
    assert 'requirements.txt' in text


def test_the_crawl_that_starts_the_setup_says_nothing_of_a_failure(monkeypatch, host):
    first = _a_setup_that_failed(monkeypatch, host)
    assert 'Setting crawl4ai up in the background' in first['progress']
    assert 'could not be set up' not in first['progress']


def test_a_crawl_after_a_failed_setup_says_why_in_its_progress(monkeypatch, host):
    _a_setup_that_failed(monkeypatch, host)
    result = wc.crawl_url('https://example.org', timeout=5)
    assert result['success'] and result['markdown'] == 'static text'
    _says_why(result['progress'])
    assert len(host.of('pip')) == 1, 'and the failed setup is still not tried again'


def test_a_batch_crawl_after_a_failed_setup_says_why(monkeypatch, host):
    _a_setup_that_failed(monkeypatch, host)
    results = wc.crawl_urls([URL, 'https://example.org'], timeout=5)
    assert [r['markdown'] for r in results] == ['static text', 'static text']
    for r in results:
        _says_why(r['progress'])


def test_the_crawl_tools_answer_for_one_page_says_why(monkeypatch, host):
    _a_setup_that_failed(monkeypatch, host)
    answer = wc.crawl_url_for_agent(URL, timeout=5)
    _says_why(answer)
    assert 'static text' in answer, 'and the page is still answered'


def test_the_crawl_tools_answer_for_several_pages_says_why(monkeypatch, host):
    _a_setup_that_failed(monkeypatch, host)
    answer = wc.crawl_urls_for_agent([URL, 'https://example.org'], timeout=5)
    _says_why(answer)
    assert answer.count('static text') == 2


def test_a_failed_browser_step_is_said_to_the_agent_too(monkeypatch, host):
    _answer(monkeypatch, 'granted')
    host.chromium_rc = 1
    _crawl_and_wait()
    progress = wc.crawl_url(URL, timeout=5)['progress']
    assert "'playwright install chromium' failed: no browser for you. " + WHAT_TO_DO in progress


def test_a_setup_that_raised_is_said_safely(monkeypatch, host):
    """A bug in the setup: its words pass the same rule before an agent reads them."""
    _answer(monkeypatch, 'granted')

    def broken(tool_name):
        raise RuntimeError('no such file C:\\Users\\somebody\\AppData\\Local\\x\\pins.txt '
                           '(key ghp_abcdefghijklmnopqrstuvwxyz0123456789)')
    monkeypatch.setattr(rm.runtime_tool_manager, 'setup_tool', broken)
    _crawl_and_wait()
    progress = wc.crawl_url(URL, timeout=5)['progress']
    assert 'setting crawl4ai up failed: RuntimeError: no such file pins.txt' in progress, progress
    assert 'somebody' not in progress and 'ghp_abcdefghijklmnopqrstuvwxyz0123456789' not in progress


def test_a_reason_that_cannot_be_made_safe_is_not_said_to_the_agent(monkeypatch, host):
    _a_setup_that_failed(monkeypatch, host)
    monkeypatch.setitem(sys.modules, 'security.secret_redactor', None)
    assert wc._safe_setup_failure('pip said ghp_abcdefghijklmnopqrstuvwxyz0123456789') == \
        "Why is in this computer's log."
    assert wc._safe_setup_failure('') == "Why is in this computer's log."
    assert wc._safe_setup_failure(None) == "Why is in this computer's log."


def test_a_good_setup_leaves_no_failure_to_say(monkeypatch, host):
    _answer(monkeypatch, 'granted')
    _crawl_and_wait()
    assert wc._setup_failed is False and wc._setup_failure == ''


def test_the_agents_line_keeps_the_whole_answer_the_reason_capped(monkeypatch, host):
    """The reason is cut at 300 characters where it is made; the sentence
    saying what to do is not cut off after it."""
    _answer(monkeypatch, 'granted')
    host.pip = (False, 'ERROR: ' + 'x' * 500)
    _crawl_and_wait()
    progress = wc.crawl_url(URL, timeout=5)['progress']
    assert ('pip could not install crawl4ai: ERROR: ' + 'x' * 293 + '. ' + WHAT_TO_DO) in progress
    assert 'x' * 294 not in progress
    assert rm._setup_reason('y' * 400) == 'y' * 300
    assert rm._setup_reason('y' * 400, limit=None) == 'y' * 400


def test_a_crawl_while_the_setup_still_runs_says_nothing_of_a_failure(monkeypatch, host):
    """Carried over: only a setup that ended badly is called failed."""
    _answer(monkeypatch, 'granted')
    host.release = threading.Event()
    try:
        wc.crawl_url(URL, timeout=5)
        second = wc.crawl_url('https://example.org', timeout=5)
    finally:
        host.release.set()
        _wait()
    assert 'could not be set up' not in second['progress'], second['progress']
    after = wc.crawl_url('https://example.net', timeout=5)
    assert 'could not be set up' not in after['progress'], 'nor after a good setup'


# ── Every absolute path is cut to its file name (review of 2e0b388fd) ──────
# The first pattern stopped at a space, so a Windows user folder with one
# ("Jane Doe") stayed in the answer with everything after it; file: URLs
# and network paths were left whole.

@pytest.mark.parametrize('output, reason', [
    # a folder name with a space
    ('ERROR: Could not open C:\\Users\\Jane Doe\\AppData\\Local\\Temp\\pip-req\\requirements.txt',
     'ERROR: Could not open requirements.txt'),
    ('ERROR: C:/Users/Jane Doe/AppData/Local/pip/cache/x.whl is not a wheel',
     'ERROR: x.whl is not a wheel'),
    ('ERROR: no space left in /home/jane doe/.cache/pip/wheels/x.whl',
     'ERROR: no space left in x.whl'),
    # file: URLs
    ('ERROR: bad wheel file:///C:/Users/Jane%20Doe/Downloads/crawl4ai-0.9.5-py3-none-any.whl',
     'ERROR: bad wheel crawl4ai-0.9.5-py3-none-any.whl'),
    ('ERROR: bad wheel file:///home/jane/wheels/crawl4ai-0.9.5-py3-none-any.whl (corrupt)',
     'ERROR: bad wheel crawl4ai-0.9.5-py3-none-any.whl (corrupt)'),
    ('ERROR: bad index file://buildhost/share/simple/crawl4ai/index.html',
     'ERROR: bad index index.html'),
    # network paths
    ('ERROR: cannot read \\\\fileserver\\builds\\Jane Doe\\wheels\\x.whl',
     'ERROR: cannot read x.whl'),
    ('ERROR: cannot read //fileserver/builds/wheels/x.whl', 'ERROR: cannot read x.whl'),
    # the home mark
    ('ERROR: cannot write ~/.cache/pip/x.whl', 'ERROR: cannot write x.whl'),
    # two paths in one line: no folder of either stays
    ('ERROR: cannot copy C:\\Users\\Jane Doe\\a\\one.whl to D:\\pip cache\\b\\two.whl',
     'ERROR: cannot copy one.whl to two.whl'),
    # carried over: what is no path on this computer stays whole
    ('ERROR: 404 for https://pypi.org/simple/crawl4ai/', 'ERROR: 404 for https://pypi.org/simple/crawl4ai/'),
    ('ERROR: needs crawl4ai>=0.9/1.0 and/or playwright', 'ERROR: needs crawl4ai>=0.9/1.0 and/or playwright'),
    ('ERROR: No matching distribution found for crawl4ai',
     'ERROR: No matching distribution found for crawl4ai'),
], ids=['space-backslash', 'space-slash', 'space-posix', 'file-url-drive', 'file-url-posix',
        'file-url-host', 'unc', 'unc-slashes', 'home-mark', 'two-paths', 'web-url',
        'words-with-slashes', 'plain'])
def test_every_absolute_path_in_a_reason_is_cut_to_its_file_name(output, reason):
    answered = rm._setup_reason(output)
    assert answered == reason
    assert 'Jane' not in answered and 'Doe' not in answered and 'fileserver' not in answered


def test_a_path_that_ends_at_the_home_folder_does_not_keep_the_persons_name(monkeypatch):
    """No file name follows the home folder, so cutting to the last part
    would leave the person's name: the home folder is written ~ first."""
    monkeypatch.setattr(os.path, 'expanduser', lambda path: 'C:\\Users\\Jane Doe')
    monkeypatch.setattr(os, 'sep', '\\')
    for written in ('C:\\Users\\Jane Doe', 'c:/users/jane doe', 'C:\\Users\\Jane Doe\\'):
        answered = rm._setup_reason('ERROR: no write access to ' + written)
        assert answered in ('ERROR: no write access to ~', 'ERROR: no write access to'), answered
        assert 'jane' not in answered.lower() and 'doe' not in answered.lower()
    assert rm._setup_reason('ERROR: bad C:\\Users\\Jane Doe\\pip\\pip.ini here') == \
        'ERROR: bad pip.ini here'


def test_the_route_answers_no_folder_of_a_user_with_a_space_in_the_name(monkeypatch, host,
                                                                        tools_client):
    _answer(monkeypatch, 'granted')
    host.pip = (False, 'ERROR: Could not install packages due to an OSError: [Errno 13] '
                       'Permission denied: C:\\Users\\Jane Doe\\AppData\\Roaming\\Python\\x.pyd')
    error = tools_client.post('/api/tools/crawl4ai/setup').get_json()['error']
    assert 'Permission denied' in error and 'x.pyd' in error
    assert 'Jane' not in error and 'Doe' not in error and 'AppData' not in error


@pytest.mark.parametrize('output, reason', [
    ('ERROR: 401 for https://alice:p@ssw0rd@pypi.internal.example/simple',
     'ERROR: 401 for https://pypi.internal.example/simple'),
    ('ERROR: 401 for https://alice:tok3n@pypi.internal.example/simple/crawl4ai/',
     'ERROR: 401 for https://pypi.internal.example/simple/crawl4ai/'),
    ('ERROR: 401 for https://token-only@pypi.internal.example/simple',
     'ERROR: 401 for https://pypi.internal.example/simple'),
    # carried over: an @ after the host is no login
    ('ERROR: no tag at https://github.com/x/y@v1.2 for it',
     'ERROR: no tag at https://github.com/x/y@v1.2 for it'),
], ids=['at-in-password', 'plain-login', 'token-only', 'at-after-host'])
def test_an_index_login_is_removed_whole(output, reason):
    """Review probe on 2e0b388fd: name:p@ssw0rd@host kept "ssw0rd@host"."""
    assert rm._setup_reason(output) == reason


# ── The reason is bounded, and reaches the tools that show no progress ─────
# Review of 42caf1a6f: (1) the LangChain Data_Extraction_From_URL's failed
# page and core.agent_tools.data_extraction_from_url answered without the
# crawl's progress, so their agent still did not read why; (2) a setup that
# RAISED was quoted whole: 2,138 characters reached the agent's crawl.

NOTE_HEAD = ('crawl4ai could not be set up on this computer, so this page is read '
             'without JavaScript. ')
ITS_OWN_ERROR = ('The setup stopped on an error of its own. Set crawl4ai up again; if it '
                 "stops the same way, the whole error is in this computer's log.")


def _a_setup_that_raised(monkeypatch, error):
    _answer(monkeypatch, 'granted')

    def broken(tool_name):
        raise error
    monkeypatch.setattr(rm.runtime_tool_manager, 'setup_tool', broken)
    _crawl_and_wait()
    assert wc._setup_failed is True
    return wc.crawl_url(URL, timeout=5)['progress']


def test_a_setup_that_raised_2000_characters_is_cut_to_300_of_them(monkeypatch, host):
    progress = _a_setup_that_raised(monkeypatch, RuntimeError('y' * 2000))
    line = next(ln for ln in progress.splitlines() if 'could not be set up' in ln)
    reason = 'RuntimeError: ' + 'y' * 286                       # 300 characters
    assert (NOTE_HEAD + 'setting crawl4ai up failed: ' + reason + '. ' + ITS_OWN_ERROR) in line
    assert 'y' * 287 not in line
    assert len(wc.setup_failure_note()) < 600, len(wc.setup_failure_note())


def test_a_raised_error_of_several_lines_keeps_its_first_words(monkeypatch, host):
    """The cause is where an error starts; the last line alone lost it."""
    progress = _a_setup_that_raised(monkeypatch, RuntimeError(
        'no such file C:\\Users\\somebody\\AppData\\Local\\x\\pins.txt '
        '(key ghp_abcdefghijklmnopqrstuvwxyz0123456789)\n'
        'second line https://buyer:hunter2secret@index.example.org/simple'))
    assert ('setting crawl4ai up failed: RuntimeError: no such file pins.txt (key [REDACTED:'
            'github_token]) second line https://index.example.org/simple. '
            + ITS_OWN_ERROR) in progress, progress
    for secret in ('somebody', 'AppData', 'hunter2secret', 'buyer',
                   'ghp_abcdefghijklmnopqrstuvwxyz0123456789'):
        assert secret not in progress


def test_a_step_that_raised_is_answered_bounded_on_the_route_too(monkeypatch, host, tools_client):
    """setup_tool's own catch-all quoted the error whole and unredacted."""
    _answer(monkeypatch, 'granted')
    host.pip = OSError('python-embed missing at C:\\Users\\somebody\\AppData\\Local\\Nunba\\'
                       'python.exe token=ghp_abcdefghijklmnopqrstuvwxyz0123456789 ' + 'z' * 2000)
    answer = tools_client.post('/api/tools/crawl4ai/setup')
    error = answer.get_json()['error']
    assert answer.status_code == 500
    assert error.startswith('setting crawl4ai up failed: OSError: python-embed missing at '
                            'python.exe '), error
    assert error.endswith(ITS_OWN_ERROR) and len(error) < 500, len(error)
    assert 'somebody' not in error and 'ghp_abcdefghijklmnopqrstuvwxyz0123456789' not in error


def test_any_failed_setups_answer_is_bounded_before_an_agent_reads_it(monkeypatch):
    """Whatever answered (another row, a later change): 500 characters of
    it, its beginning, and where the rest is."""
    said = wc._safe_setup_failure('setting crawl4ai up failed: ' + 'word ' * 1000)
    assert said.startswith('setting crawl4ai up failed: word word')
    assert said.endswith("... (cut here; the whole error is in this computer's log).")
    assert 500 < len(said) < 570, len(said)
    short = 'setting crawl4ai up failed: pip could not install crawl4ai: ERROR: no. Try again'
    assert wc._safe_setup_failure(short) == short + '.', 'an answer that fits is said whole'


def test_a_stored_credentials_value_is_said_as_its_alias(monkeypatch, host):
    import core.tool_logging as tool_logging
    vault = types.SimpleNamespace(
        mask_secrets=lambda text: text.replace('Hx7vaultRAWvalue', '{{secret:PIP_TOKEN}}'))
    monkeypatch.setattr(tool_logging, 'credential_vault', lambda: vault)
    _answer(monkeypatch, 'granted')
    host.pip = (False, 'ERROR: index refused the login Hx7vaultRAWvalue for crawl4ai')
    _crawl_and_wait()
    note = wc.setup_failure_note()
    assert 'refused the login {{secret:PIP_TOKEN}} for crawl4ai' in note, note
    assert 'Hx7vaultRAWvalue' not in note


def test_a_vault_that_cannot_mask_leaves_no_reason_said(monkeypatch, host):
    import core.tool_logging as tool_logging

    def broken():
        raise RuntimeError('vault store unreadable')
    monkeypatch.setattr(tool_logging, 'credential_vault', broken)
    _a_setup_that_failed(monkeypatch, host)
    assert wc.setup_failure_note() == NOTE_HEAD + "Why is in this computer's log."


def test_the_note_is_empty_until_a_setup_has_failed(monkeypatch, host):
    assert wc.setup_failure_note() == ''
    _answer(monkeypatch, 'granted')
    _crawl_and_wait()                                   # a good setup
    assert wc.setup_failure_note() == ''


def test_the_note_is_the_line_the_crawls_progress_carries(monkeypatch, host):
    _a_setup_that_failed(monkeypatch, host)
    note = wc.setup_failure_note()
    assert note.startswith(NOTE_HEAD + 'setting crawl4ai up failed: ' + PIP_SAID)
    assert note.endswith(WHAT_TO_DO)
    assert note in wc.crawl_url(URL, timeout=5)['progress']


# -- the LangChain Data_Extraction_From_URL (hart_intelligence_entry) -------

@pytest.fixture
def langchain_tool(monkeypatch):
    """parse_link_for_crwalab, the tool's own function, taken from the
    source and run with stand-ins for the app it lives in."""
    import ast
    import logging
    from pathlib import Path
    source = (Path(rm.__file__).resolve().parents[2]
              / 'hart_intelligence_entry.py').read_text(encoding='utf-8')
    fn = next(n for n in ast.parse(source).body
              if isinstance(n, ast.FunctionDef) and n.name == 'parse_link_for_crwalab')
    names = {
        'thread_local_data': types.SimpleNamespace(get_user_id=lambda: 'u1',
                                                   get_request_id=lambda: 'r1'),
        'app': types.SimpleNamespace(logger=logging.getLogger('test.app')),
        'publish_async': lambda *a, **k: None,
        'TaskStatus': types.SimpleNamespace(EXECUTING=types.SimpleNamespace(value='x'),
                                            ERROR=types.SimpleNamespace(value='e')),
        'TaskNames': types.SimpleNamespace(CRAWLAB=types.SimpleNamespace(value='crawlab')),
        '_parse_pdf_in_process': lambda *a: 'pdf',
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                 'hart_intelligence_entry.py', 'exec'), names)
    publish = types.ModuleType('core.peer_link.crossbar_publish')
    publish.publish_thinking_trace = lambda **k: None
    monkeypatch.setitem(sys.modules, 'core.peer_link.crossbar_publish', publish)
    return names['parse_link_for_crwalab']


def _the_page_fails(monkeypatch):
    monkeypatch.setattr(wc, '_fallback_fetch', lambda url, timeout, log: {
        'success': False, 'url': url, 'error': 'Too little content extracted'})


def test_the_langchain_tools_failed_page_says_why_the_setup_failed(monkeypatch, host,
                                                                  langchain_tool):
    _a_setup_that_failed(monkeypatch, host)
    _the_page_fails(monkeypatch)
    answer = langchain_tool(f'{URL}, website')
    assert answer.startswith(f'Failed to crawl {URL}: Too little content extracted\n[')
    _says_why(answer)


def test_the_langchain_tools_failed_page_is_as_before_with_no_failed_setup(monkeypatch, host,
                                                                          langchain_tool):
    """Carried over: the answer's one line, unchanged."""
    _answer(monkeypatch, 'asked')
    _the_page_fails(monkeypatch)
    assert langchain_tool(f'{URL}, website') == \
        f'Failed to crawl {URL}: Too little content extracted'


def test_the_langchain_tools_read_page_still_shows_the_progress(monkeypatch, host,
                                                               langchain_tool):
    """Carried over: a page that was read carries the crawl's progress, the reason in it."""
    _a_setup_that_failed(monkeypatch, host)
    answer = langchain_tool(f'{URL}, website')
    assert answer.startswith('--- Progress ---') and 'Content:\nstatic text' in answer
    _says_why(answer)
    assert answer.count('could not be set up') == 1


# -- the search crawl (hartos.helper.crawl4ai_batch_fetch) -------------------

@pytest.fixture
def batch_fetch():
    """crawl4ai_batch_fetch, taken from hartos/helper.py (importing the
    module brings the whole agent runtime with it)."""
    import ast
    import logging
    from pathlib import Path
    from typing import List
    source = (Path(rm.__file__).resolve().parents[2] / 'hartos' / 'helper.py').read_text(
        encoding='utf-8')
    fn = next(n for n in ast.parse(source).body
              if isinstance(n, ast.FunctionDef) and n.name == 'crawl4ai_batch_fetch')
    names = {'List': List,
             'current_app': types.SimpleNamespace(logger=logging.getLogger('test.app'))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                 'helper.py', 'exec'), names)
    return names['crawl4ai_batch_fetch']


def _pages(monkeypatch, answers):
    """What the plain read gives for each URL, in turn."""
    monkeypatch.setattr(wc, '_fallback_fetch', lambda url, timeout, log: dict(
        answers[url], url=url))


OTHER = 'https://example.org'


def test_the_search_crawl_carries_the_reason_at_the_head_of_the_first_page_read(
        monkeypatch, host, batch_fetch):
    _a_setup_that_failed(monkeypatch, host)
    _pages(monkeypatch, {URL: {'success': False, 'error': 'HTTP 404'},
                         OTHER: {'success': True, 'markdown': 'page two', 'word_count': 2},
                         'https://example.net': {'success': True, 'markdown': 'page three',
                                                 'word_count': 2}})
    texts = batch_fetch([URL, OTHER, 'https://example.net'])
    assert isinstance(texts, list) and len(texts) == 3 and all(isinstance(t, str) for t in texts)
    assert texts[0] == '', 'a page that failed stays empty, as its callers test it'
    assert texts[1] == '[' + wc.setup_failure_note() + ']\npage two'
    assert texts[2] == 'page three', 'said once'
    _says_why(texts[1])


def test_the_search_crawl_is_as_before_with_no_failed_setup(monkeypatch, host, batch_fetch):
    """Carried over: the page texts and nothing else."""
    _answer(monkeypatch, 'asked')
    _pages(monkeypatch, {URL: {'success': True, 'markdown': 'page one', 'word_count': 2},
                         OTHER: {'success': False, 'error': 'HTTP 404'}})
    assert batch_fetch([URL, OTHER]) == ['page one', '']


def test_a_search_crawl_that_breaks_still_answers_one_empty_text_per_link(monkeypatch,
                                                                         batch_fetch):
    """Carried over."""
    def broken(urls, timeout=30, max_concurrent=3):
        raise RuntimeError('crawler gone')
    monkeypatch.setattr(wc, 'crawl_urls', broken)
    assert batch_fetch([URL, OTHER]) == ['', '']


# ── pip's own line, the home folder matched whole (review of f2683dc2f) ────
# (1) pip prints an OSError with str(), which quotes the path and doubles
# every backslash; read as single separators that line came back whole, the
# person's name and every folder in it.  (2) The home folder was matched as
# a prefix: with home C:\Users\Jane, C:\Users\Janet\AppData\Local\x.pyd
# became "~t\AppData\Local\x.pyd".  (3) "1 / 2 / 3" lost its middle.

BS = chr(92)


def _win(*parts):
    return BS.join(parts)


def test_pips_own_oserror_line_keeps_no_folder():
    """The line as pip prints it: str() of the error it caught."""
    path = _win('C:', 'Users', 'Jane Doe', 'AppData', 'Roaming', 'Python', 'x.pyd')
    error = PermissionError(13, 'Permission denied', path)
    assert BS * 2 in str(error), 'str() doubles the backslashes'
    line = 'ERROR: Could not install packages due to an OSError: ' + str(error)
    assert rm._setup_reason(line) == (
        "ERROR: Could not install packages due to an OSError: [Errno 13] Permission "
        "denied: 'x.pyd'")


def _home(monkeypatch, *parts):
    monkeypatch.setattr(os.path, 'expanduser', lambda path: _win(*parts))


@pytest.mark.parametrize('path, left', [
    # doubled separators, as str(OSError) and a repr write them
    ((BS * 2).join(['C:', 'Users', 'Jane Doe', 'AppData', 'x.pyd']), 'x.pyd'),
    ('C://Users//Jane Doe//AppData//x.pyd', 'x.pyd'),
    ((BS * 2).join(['', '', 'fileserver', 'builds', 'Jane Doe', 'x.whl']), 'x.whl'),
    # a folder name with an apostrophe or brackets
    (_win('C:', 'Users', "Sean O'Brien", 'AppData', 'Local', 'Temp', 'pip-x', 'setup.py'),
     'setup.py'),
    (_win('C:', 'Users', 'Kim (Work)', 'AppData', 'Local', 'x.pyd'), 'x.pyd'),
    (_win('C:', 'Program Files (x86)', 'Python', 'Lib', 'x.pyd'), 'x.pyd'),
    # another person whose name starts with this home's, and an 8.3 short name
    (_win('C:', 'Users', 'Janet', 'AppData', 'Local', 'x.pyd'), 'x.pyd'),
    (_win('C:', 'Users', 'JANEDO~1', 'AppData', 'Local', 'Temp', 'pip-unpack-1', 'x.whl'),
     'x.whl'),
    (_win('C:', 'Users', 'Jane.Doe', 'AppData', 'x.pyd'), 'x.pyd'),
    # the home folder itself, either separator, once or doubled
    (_win('C:', 'Users', 'Jane', 'x.whl'), 'x.whl'),
    ('c:/users/jane/AppData/x.whl', 'x.whl'),
    ((BS * 2).join(['C:', 'Users', 'Jane', 'AppData', 'x.whl']), 'x.whl'),
], ids=['doubled-backslash', 'doubled-slash', 'doubled-unc', 'apostrophe', 'brackets',
        'program-files-x86', 'home-is-a-prefix', 'short-name', 'dotted-name', 'home-file',
        'home-slashes', 'home-doubled'])
def test_a_path_keeps_no_folder_whatever_the_home_folder_is(monkeypatch, path, left):
    _home(monkeypatch, 'C:', 'Users', 'Jane')
    for line in ('ERROR: [Errno 13] Permission denied: ' + path,
                 "ERROR: [Errno 13] Permission denied: '" + path + "'"):
        answered = rm._setup_reason(line)
        assert answered.replace("'", '') == 'ERROR: [Errno 13] Permission denied: ' + left, answered
        for part in ('Users', 'users', 'Jane', 'jane', 'JANEDO', 'AppData', 'Sean', 'Kim',
                     'Program', 'fileserver', '~t', '~DO'):
            assert part not in answered, (part, answered)


@pytest.mark.parametrize('ending', ['', ' ', "'", '"', ')', ',', ';', '. Try again', BS, '/'])
def test_the_home_folder_alone_is_written_as_the_home_mark(monkeypatch, ending):
    _home(monkeypatch, 'C:', 'Users', 'Jane')
    answered = rm._setup_reason('ERROR: no write access to ' + _win('C:', 'Users', 'Jane') + ending)
    assert answered.startswith('ERROR: no write access to'), answered
    assert 'Jane' not in answered and 'Users' not in answered, answered
    if ending not in (BS, '/'):
        assert answered.startswith('ERROR: no write access to ~'), answered


def test_a_home_folder_whose_name_starts_a_longer_name_with_a_space(monkeypatch):
    """Home ...\\Jane is not the start of ...\\Jane Doe\\: the longer folder
    is cut like any other, and nothing of either name stays."""
    _home(monkeypatch, 'C:', 'Users', 'Jane')
    for path in (_win('C:', 'Users', 'Jane Doe', 'AppData', 'x.pyd'),
                 'C:/Users/Jane Doe/AppData/x.pyd'):
        answered = rm._setup_reason('ERROR: denied: ' + path)
        assert answered == 'ERROR: denied: x.pyd', answered
    assert rm._setup_reason('ERROR: no room in ' + _win('C:', 'Users', 'Jane')
                            + ' and none on D') == 'ERROR: no room in ~ and none on D'


def test_a_name_that_only_starts_like_the_home_folder_is_not_the_home_mark(monkeypatch):
    """Carried over from before f2683dc2f: such a path is cut like any other."""
    _home(monkeypatch, 'C:', 'Users', 'Jane')
    assert rm._home_marked('at ' + _win('C:', 'Users', 'Janet')) == 'at ' + _win('C:', 'Users', 'Janet')
    assert rm._home_marked('at ' + _win('C:', 'Users', 'Jane')) == 'at ~'
    assert rm._home_marked('at ' + _win('XC:', 'Users', 'Jane')) == 'at ' + _win('XC:', 'Users', 'Jane')


def test_a_posix_home_folder_is_matched_whole_too(monkeypatch):
    monkeypatch.setattr(os.path, 'expanduser', lambda path: '/home/jane')
    assert rm._setup_reason('ERROR: cannot write /home/jane/.cache/pip/x.whl') == \
        'ERROR: cannot write x.whl'
    assert rm._setup_reason('ERROR: cannot write /home/janet/.cache/pip/x.whl') == \
        'ERROR: cannot write x.whl'
    assert rm._setup_reason('ERROR: cannot write to /home/jane') == 'ERROR: cannot write to ~'
    assert rm._home_marked('see /home/janet') == 'see /home/janet'


@pytest.mark.parametrize('text', [
    'ERROR: Check the read/write permission and/or the disk, 1 / 2 / 3 done',
    'ERROR: HTTP error 404 while getting https://pypi.org/simple/crawl4ai/ (from https://x.example/a/b)',
    'ERROR: 404 for https://pypi.example:8443?u=a@b/simple',
    'ERROR: git+https://github.com/x/y.git@v1.2 failed',
    'ERROR: needs crawl4ai>=0.9/1.0 and/or playwright',
    "ERROR: 'playwright install chromium' failed",
    'ERROR: a / b and c/d are not paths',
])
def test_what_is_no_path_on_this_computer_is_said_as_it_came(text):
    assert rm._setup_reason(text) == text


def test_the_home_folder_alone_with_doubled_separators_is_the_home_mark(monkeypatch):
    """As str(OSError) writes a path that ends at the home folder."""
    _home(monkeypatch, 'C:', 'Users', 'Jane')
    error = PermissionError(13, 'Permission denied', _win('C:', 'Users', 'Jane'))
    assert rm._setup_reason('ERROR: ' + str(error)) == "ERROR: [Errno 13] Permission denied: '~'"
    assert rm._setup_reason('ERROR: denied: C://Users//Jane') == 'ERROR: denied: ~'


# ── A raised OSError that carries its file (review of d057b3782) ───────────
# setup_error's own test raised a one-argument OSError, whose text holds no
# path.  A real one carries the file: str(FileNotFoundError(2, 'No such file
# or directory', path)) quotes the path and doubles every backslash, and the
# person's name and every folder reached the agent's note and the route.

EMBED = _win('C:', 'Users', 'Jane Doe', 'AppData', 'Local', 'Nunba', 'python-embed', 'python.exe')
EMBED_SAID = ("setting crawl4ai up failed: FileNotFoundError: [Errno 2] No such file or "
              "directory: 'python.exe'. " + ITS_OWN_ERROR)


def _no_folder_of_jane(text):
    for part in ('Jane', 'Doe', 'Users', 'AppData', 'Nunba', 'python-embed', BS + BS):
        assert part not in text, (part, text)


def test_setup_error_keeps_no_folder_of_a_two_argument_oserror():
    error = FileNotFoundError(2, 'No such file or directory', EMBED)
    assert BS * 2 in str(error) and 'Jane Doe' in str(error)
    answer = rm.setup_error('crawl4ai', error)
    assert answer == EMBED_SAID
    _no_folder_of_jane(answer)


def test_the_route_answers_no_folder_of_a_raised_oserror(monkeypatch, host, tools_client):
    _answer(monkeypatch, 'granted')
    host.pip = FileNotFoundError(2, 'No such file or directory', EMBED)
    answer = tools_client.post('/api/tools/crawl4ai/setup')
    assert answer.status_code == 500 and answer.get_json()['error'] == EMBED_SAID
    _no_folder_of_jane(answer.get_data(as_text=True))


def test_the_agents_note_holds_no_folder_of_a_raised_oserror(monkeypatch, host):
    progress = _a_setup_that_raised(
        monkeypatch, FileNotFoundError(2, 'No such file or directory', EMBED))
    assert NOTE_HEAD + EMBED_SAID in progress, progress
    _no_folder_of_jane(progress)
    _no_folder_of_jane(wc.crawl_url_for_agent(URL, timeout=5))


# ── Windows' long-path prefix (review of b1613c373) ────────────────────────
# Windows writes a path past 260 characters as \\?\C:\... (a device path as
# \\.\C:\...), and pip's errors carry it.  b1613c373's guards refused the
# backslash right before the drive letter, so neither the home rule nor the
# folder rule fired: with home C:\Users\Jane Doe the reviewer's line came
# back whole, the person's name in it, where the commit before answered
# "ERROR: x.pyd".

LONG_PATH = BS * 2 + '?' + BS
DEVICE = BS * 2 + '.' + BS
PIP_TEMP = _win('C:', 'Users', 'Jane Doe', 'AppData', 'Local', 'Temp', 'pip-install-ab', 'x.pyd')
HOMES = [('C:', 'Users', 'Jane Doe'), ('C:', 'Users', 'Jane'), ('C:', 'Users', 'Bob')]
HOME_IDS = ['home-is-the-paths', 'home-starts-the-name', 'another-home']


def _no_folder_in(text):
    for part in ('Jane', 'jane', 'Doe', 'Users', 'AppData', 'Temp', 'pip-install', 'C:', BS):
        assert part not in text, (part, text)


@pytest.mark.parametrize('prefix', [LONG_PATH, DEVICE], ids=['long-path', 'device'])
@pytest.mark.parametrize('home', HOMES, ids=HOME_IDS)
def test_a_long_path_prefix_keeps_no_folder(monkeypatch, prefix, home):
    """The reviewer's line as it came, one backslash between folders."""
    _home(monkeypatch, *home)
    answered = rm._setup_reason('ERROR: ' + prefix + PIP_TEMP)
    assert answered == 'ERROR: x.pyd', answered
    _no_folder_in(answered)


@pytest.mark.parametrize('prefix', [LONG_PATH, DEVICE], ids=['long-path', 'device'])
@pytest.mark.parametrize('home', HOMES, ids=HOME_IDS)
def test_pips_own_line_of_a_long_path_keeps_no_folder(monkeypatch, prefix, home):
    """As pip prints it: str() doubles every backslash, the prefix's too."""
    _home(monkeypatch, *home)
    error = PermissionError(13, 'Permission denied', prefix + PIP_TEMP)
    assert BS * 4 + prefix[2] + BS * 2 + 'C:' + BS * 2 + 'Users' in str(error), str(error)
    line = 'ERROR: Could not install packages due to an OSError: ' + str(error)
    answered = rm._setup_reason(line)
    assert answered == ("ERROR: Could not install packages due to an OSError: [Errno 13] "
                        "Permission denied: 'x.pyd'"), answered
    _no_folder_in(answered)


@pytest.mark.parametrize('prefix', [LONG_PATH, DEVICE], ids=['long-path', 'device'])
def test_the_home_folder_with_a_long_path_prefix_is_the_home_mark(monkeypatch, prefix):
    """A path that ends at the home folder: no file name after the person's
    name, so only the home rule keeps the name out."""
    _home(monkeypatch, 'C:', 'Users', 'Jane Doe')
    home = prefix + _win('C:', 'Users', 'Jane Doe')
    assert rm._home_marked('at ' + home) == 'at ~'
    assert rm._setup_reason('ERROR: no write access to ' + home) == 'ERROR: no write access to ~'
    error = PermissionError(13, 'Permission denied', home)
    assert rm._setup_reason('ERROR: ' + str(error)) == "ERROR: [Errno 13] Permission denied: '~'"
    assert rm._setup_reason("ERROR: could not create '" + home + BS + ".cache': denied") == \
        "ERROR: could not create '.cache': denied"
    assert rm.setup_error('crawl4ai', error).startswith(
        "setting crawl4ai up failed: PermissionError: [Errno 13] Permission denied: '~'. ")


@pytest.mark.parametrize('text, said', [
    ('ERROR: cannot read ' + LONG_PATH + _win('UNC', 'fileserver', 'builds', 'Jane Doe', 'x.whl'),
     'ERROR: cannot read x.whl'),
    ('ERROR: ' + str(FileNotFoundError(2, 'No such file or directory', LONG_PATH
                                       + _win('UNC', 'fileserver', 'Jane Doe', 'x.whl'))),
     "ERROR: [Errno 2] No such file or directory: 'x.whl'"),
    ('ERROR: no answer on ' + DEVICE + _win('pipe', 'hartos-tool'), 'ERROR: no answer on hartos-tool'),
    ('ERROR: cannot open ' + DEVICE + 'PhysicalDrive0', 'ERROR: cannot open ' + DEVICE + 'PhysicalDrive0'),
], ids=['unc-long-path', 'unc-long-path-doubled', 'pipe', 'device-no-folder'])
def test_a_prefix_before_no_drive_is_read_as_before(monkeypatch, text, said):
    """Carried over: a prefix before a network path or a device that is no
    drive is cut (or kept) as b1613c373 did."""
    _home(monkeypatch, 'C:', 'Users', 'Jane Doe')
    assert rm._setup_reason(text) == said
