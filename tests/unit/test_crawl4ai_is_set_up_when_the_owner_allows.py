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
  * When the user site's drive has under 5 GiB free and the private D: site
    can be made, both go to D: (the site, and a browsers folder beside it,
    which the crawler then launches from); otherwise to the usual places.
  * After a good install the next crawl imports crawl4ai again.

    python -m pytest tests/unit/test_crawl4ai_is_set_up_when_the_owner_allows.py -q
"""
import sys
import threading
import types
from collections import namedtuple

import pytest

from integrations import web_crawler as wc

_Usage = namedtuple('_Usage', 'total used free')
GIB = 1024 ** 3


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(wc, '_crawler_available', False)
    monkeypatch.setattr(wc, '_setup_thread', None)
    monkeypatch.setattr(wc, '_setup_failed', False)
    monkeypatch.setattr(wc, '_fallback_fetch', lambda url, timeout, log: {
        'success': True, 'url': url, 'markdown': 'static text', 'word_count': 2})
    yield


def _answer(monkeypatch, answer, asked=None):
    from integrations.agent_engine import capability_setup

    def ask(capability, *, reason):
        if asked is not None:
            asked.append((capability, reason))
        return answer
    monkeypatch.setattr(capability_setup, 'ask_owner_for_setup', ask)


def _blocking_install(monkeypatch):
    """An install that runs until released, counting how many started."""
    started, release = [], threading.Event()

    def install():
        started.append(1)
        release.wait(5)
    monkeypatch.setattr(wc, '_install_crawl4ai', install)
    return started, release


def test_a_crawl_with_crawl4ai_missing_asks_the_owner_once_and_answers_now(monkeypatch):
    asked = []
    _answer(monkeypatch, 'asked', asked)
    started, release = _blocking_install(monkeypatch)
    result = wc.crawl_url('https://example.com', timeout=5)
    assert result['success'] and result['markdown'] == 'static text'
    assert [c for c, _ in asked] == ['crawl:crawl4ai']
    assert 'JavaScript' in asked[0][1]
    assert started == [], 'nothing is installed before the owner says yes'


def test_a_batch_crawl_with_crawl4ai_missing_asks_too(monkeypatch):
    asked = []
    _answer(monkeypatch, 'asked', asked)
    results = wc.crawl_urls(['https://example.com', 'https://example.org'], timeout=5)
    assert [r['markdown'] for r in results] == ['static text', 'static text']
    assert [c for c, _ in asked] == ['crawl:crawl4ai']


def test_a_declined_setup_installs_nothing(monkeypatch):
    _answer(monkeypatch, 'declined')
    started, release = _blocking_install(monkeypatch)
    wc.crawl_url('https://example.com', timeout=5)
    wc.crawl_urls(['https://example.com', 'https://example.org'], timeout=5)
    assert started == []


def test_the_owners_yes_starts_one_install_however_many_crawls_ask(monkeypatch):
    _answer(monkeypatch, 'granted')
    started, release = _blocking_install(monkeypatch)
    try:
        first = wc.crawl_url('https://example.com', timeout=5)
        wc.crawl_url('https://example.org', timeout=5)
        wc.crawl_urls(['https://example.net'], timeout=5)
        assert first['success'] and first['markdown'] == 'static text'
        wc._setup_thread.join(0.5)
        assert started == [1]
    finally:
        release.set()
        wc._setup_thread.join(5)


def test_a_failed_install_is_not_retried_in_the_same_run(monkeypatch):
    _answer(monkeypatch, 'granted')
    calls = []
    monkeypatch.setattr(wc, '_setup_target', lambda: (None, None))
    monkeypatch.setattr(wc, '_pip_install_crawl4ai',
                        lambda site: calls.append('pip') or False)
    monkeypatch.setattr(wc, '_install_chromium',
                        lambda browsers: calls.append('chromium') or True)
    wc.crawl_url('https://example.com', timeout=5)
    wc._setup_thread.join(5)
    wc.crawl_url('https://example.com', timeout=5)
    if wc._setup_thread is not None:
        wc._setup_thread.join(5)
    assert calls == ['pip'], calls
    assert wc._crawler_available is False


def test_a_good_install_lets_the_next_crawl_import_crawl4ai(monkeypatch):
    _answer(monkeypatch, 'granted')
    monkeypatch.setattr(wc, '_setup_target', lambda: (None, None))
    monkeypatch.setattr(wc, '_pip_install_crawl4ai', lambda site: True)
    monkeypatch.setattr(wc, '_install_chromium', lambda browsers: True)
    wc.crawl_url('https://example.com', timeout=5)
    wc._setup_thread.join(5)
    assert wc._crawler_available is None, 'the next check imports again'


@pytest.fixture
def host(monkeypatch, tmp_path):
    """A frozen desktop host: Nunba's installer module with a C: user site
    and a D: root, recording what pip and Playwright were run with."""
    runs = []
    c_site = tmp_path / 'c' / '.nunba' / 'site-packages'
    d_root = tmp_path / 'd' / '.nunba'
    pkg = types.ModuleType('tts')
    pi = types.ModuleType('tts.package_installer')
    pi.get_user_site_packages = lambda: str(c_site)
    pi._D_DRIVE_ROOT = str(d_root)

    def make_private():
        (d_root / 'site-packages').mkdir(parents=True, exist_ok=True)
        return pi.private_ok
    pi.private_ok = True
    pi._make_private_d_drive_site = make_private
    pi._run_pip = lambda args, **kw: (runs.append(('pip', list(args))) or (True, 'ok'))
    pi.get_embed_python = lambda: str(tmp_path / 'python-embed' / 'python.exe')
    pkg.package_installer = pi
    monkeypatch.setitem(sys.modules, 'tts', pkg)
    monkeypatch.setitem(sys.modules, 'tts.package_installer', pi)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'platform', 'win32')

    def run_bounded(cmd, timeout=5.0, **kw):
        runs.append(('chromium', list(cmd), (kw.get('env') or {}).get(
            'PLAYWRIGHT_BROWSERS_PATH')))
        return types.SimpleNamespace(returncode=0, timed_out=False, stdout='', stderr='')
    monkeypatch.setattr(wc, 'run_bounded', run_bounded)
    monkeypatch.delenv('PLAYWRIGHT_BROWSERS_PATH', raising=False)
    return pi, runs, d_root


def _free(monkeypatch, free_bytes):
    monkeypatch.setattr(wc.shutil, 'disk_usage',
                        lambda path: _Usage(500 * GIB, 0, free_bytes))


def test_with_c_low_crawl4ai_and_its_browser_go_to_the_private_d_site(host, monkeypatch):
    pi, runs, d_root = host
    _free(monkeypatch, 4 * GIB)
    wc._install_crawl4ai()
    pip = [r for r in runs if r[0] == 'pip']
    chromium = [r for r in runs if r[0] == 'chromium']
    assert len(pip) == 1 and pip[0][1][:2] == ['install', 'crawl4ai']
    args = pip[0][1]
    assert args[args.index('--target') + 1] == str(d_root / 'site-packages')
    assert len(chromium) == 1
    assert chromium[0][1][-4:] == ['-m', 'playwright', 'install', 'chromium']
    assert chromium[0][1][0] == pi.get_embed_python()
    assert chromium[0][2] == str(d_root / 'ms-playwright')
    assert wc._crawler_available is None


def test_with_room_on_c_the_usual_places_are_used(host, monkeypatch):
    pi, runs, d_root = host
    _free(monkeypatch, 40 * GIB)
    wc._install_crawl4ai()
    pip = [r for r in runs if r[0] == 'pip']
    chromium = [r for r in runs if r[0] == 'chromium']
    assert pip and '--target' not in pip[0][1]
    assert chromium and chromium[0][2] is None
    assert not (d_root / 'site-packages').exists()


def test_a_d_site_that_cannot_be_made_private_is_not_used(host, monkeypatch):
    pi, runs, d_root = host
    pi.private_ok = False
    _free(monkeypatch, 4 * GIB)
    wc._install_crawl4ai()
    pip = [r for r in runs if r[0] == 'pip']
    assert pip and '--target' not in pip[0][1]


def test_the_crawler_launches_the_browser_installed_on_d(host, monkeypatch):
    pi, runs, d_root = host
    (d_root / 'ms-playwright' / 'chromium-1').mkdir(parents=True)
    wc._use_installed_browsers()
    import os
    assert os.environ.get('PLAYWRIGHT_BROWSERS_PATH') == str(d_root / 'ms-playwright')


def test_a_browser_folder_already_named_is_kept(host, monkeypatch):
    pi, runs, d_root = host
    (d_root / 'ms-playwright' / 'chromium-1').mkdir(parents=True)
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', 'E:\\browsers')
    wc._use_installed_browsers()
    import os
    assert os.environ['PLAYWRIGHT_BROWSERS_PATH'] == 'E:\\browsers'
