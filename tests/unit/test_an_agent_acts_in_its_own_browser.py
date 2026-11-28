"""An agent opens, reads, clicks and types in its OWN browser profile.

Owner, 2026-11-27 (verbatim): "the llm shd be able to work on or use headless
and with head browser (no-GUI) and (with GUI) actions respectivelu".

integrations.browser_research.scripts.agent_browser drives a page of the
mailbox's own Chrome (one profile per mailbox) through the one dispatch
surface (browser_research.tools.dispatch).  The owner types passwords,
one-time codes and card/PAN/GSTIN/Aadhaar/bank details himself, so such
fields are refused; clicking and typing need the owner's permission for
agents to control this computer.

A fake page stands in for Chrome here; the page-side scripts are checked
against a real headless Chrome by the live probe.

Run:
  pytest tests/unit/test_an_agent_acts_in_its_own_browser.py -q
"""
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.browser_research import driver, tools  # noqa: E402
from integrations.browser_research.scripts import agent_browser as ab  # noqa: E402

X = 'registration@mcgroce.com'
SNAP = {'url': 'https://mail.example/inbox', 'title': 'Inbox', 'text': 'hello',
        'text_length': 5, 'clickable': ['Compose', 'Send'], 'fields': []}


class _Page:
    def __init__(self, found=None):
        self.found = found if found is not None else {'found': False, 'candidates': ['Compose', 'Send']}
        self.sent = []
        self.navigated = []
        self.focused = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def navigate(self, url, wait_s=None):
        self.navigated.append(url)

    def wait_loaded(self, what='', wait_s=None):
        pass

    def evaluate(self, expr):
        if 'window.__hartosTarget = el' in expr:
            return self.found
        if 'e.focus()' in expr:
            self.focused = True
            return None
        if 'text_length' in expr:
            return dict(SNAP)
        if expr == 'location.href':
            return 'about:blank'
        raise AssertionError(f'unexpected script: {expr[:60]}')

    def send(self, method, params=None):
        self.sent.append((method, params))
        return {}


@pytest.fixture
def page(monkeypatch):
    holder = {'page': _Page(), 'opened': []}

    def _open(mailbox, headless=True):
        holder['opened'].append((mailbox, headless))
        return holder['page']

    monkeypatch.setattr(ab, '_page', _open)
    monkeypatch.setattr(ab, '_control_refusal', lambda agent_id: None)
    monkeypatch.setattr(ab.time, 'sleep', lambda s: None)
    return holder


def _methods(p):
    return [m for m, _ in p.sent]


def test_open_page_goes_there_and_reads_it(page):
    out = ab.open_page(X, 'https://mail.example')
    assert page['page'].navigated == ['https://mail.example']
    assert page['opened'] == [(X, True)], 'headless unless a window is already open'
    assert out['success'] and out['title'] == 'Inbox' and out['clickable'] == ['Compose', 'Send']
    assert out['connection_mechanism'] == 'agent_profile_cdp' and out['mailbox'] == X


def test_click_presses_the_mouse_at_the_items_centre(page):
    page['page'].found = {'found': True, 'x': 120.5, 'y': 40.0, 'words': 'compose'}
    out = ab.click(X, 'Compose', agent_id='a1')
    mouse = [p for m, p in page['page'].sent if m == 'Input.dispatchMouseEvent']
    assert [m['type'] for m in mouse] == ['mouseMoved', 'mousePressed', 'mouseReleased']
    assert all((m['x'], m['y']) == (120.5, 40.0) for m in mouse)
    assert out['success'] and out['clicked'] == 'compose'


def test_a_click_that_finds_nothing_names_what_is_there(page):
    out = ab.click(X, 'Inbox zero', agent_id='a1')
    assert not out['success']
    assert 'Compose' in out['error'] and 'Send' in out['error'] and 'Inbox zero' in out['error']
    assert 'Input.dispatchMouseEvent' not in _methods(page['page'])


def test_click_without_the_owners_permission_does_not_open_the_page(page, monkeypatch):
    monkeypatch.setattr(ab, '_control_refusal',
                        lambda agent_id: 'Not run: the owner has not allowed agents to control this computer.')
    out = ab.click(X, 'Send', agent_id='a1')
    assert not out['success'] and 'not allowed' in out['error']
    assert page['opened'] == []


def test_type_text_replaces_the_field_and_presses_enter_only_when_asked(page):
    page['page'].found = {'found': True, 'x': 10, 'y': 20, 'words': 'to', 'type': 'email',
                          'autocomplete': '', 'name': 'to'}
    out = ab.type_text(X, 'To', 'buyer@example.com', agent_id='a1')
    assert page['page'].focused
    assert ('Input.insertText', {'text': 'buyer@example.com'}) in page['page'].sent
    assert 'Input.dispatchKeyEvent' not in _methods(page['page'])
    assert out['success'] and out['typed_into'] == 'to'
    ab.type_text(X, 'To', 'buyer@example.com', submit=True, agent_id='a1')
    keys = [p for m, p in page['page'].sent if m == 'Input.dispatchKeyEvent']
    assert [k['type'] for k in keys] == ['keyDown', 'keyUp'] and keys[0]['key'] == 'Enter'


@pytest.mark.parametrize('found', [
    {'type': 'password', 'words': 'password', 'name': 'pw'},
    {'type': 'text', 'autocomplete': 'one-time-code', 'words': 'code', 'name': 'c'},
    {'type': 'text', 'words': 'enter the otp sent to you', 'name': 'x'},
    {'type': 'text', 'words': 'pan number', 'name': 'pan'},
    {'type': 'text', 'words': 'company tax id', 'name': 'gstin'},
    {'type': 'text', 'words': 'card number', 'name': 'cc'},
])
def test_fields_the_owner_fills_in_himself_are_refused(page, found):
    page['page'].found = dict({'found': True, 'x': 1, 'y': 1, 'autocomplete': ''}, **found)
    out = ab.type_text(X, 'field', 'whatever', agent_id='a1')
    assert not out['success'] and 'open_window' in out['error']
    assert 'Input.insertText' not in _methods(page['page'])
    assert 'Input.dispatchMouseEvent' not in _methods(page['page'])


@pytest.mark.parametrize('words,name', [
    # rv_br2 on ecbb9d03d, typed live though the owner fills them in:
    ('code', 'otp_code'), ('code', 'otpInput'), ('pan', 'pan_no'),
    ('cvv', 'cvv_code'), ('pin', 'upi_pin'), ('ifsc', 'ifsc_code'),
    ('Card No.', ''), ('GST Number', 'gst_no'), ('MPIN', ''),
    ('Security code', ''), ('A/C No', ''), ('One time password', ''),
    # rv_br3 on 506273cc7: PAN that is not the first word (typed live), a
    # name written as one word, a digit after the word:
    ('Enter PAN', 'txtPan'), ('Your PAN', ''), ('Applicant PAN', ''),
    ('Taxpayer PAN', ''), ('New', 'newpassword'), ('Confirm', 'confirmpassword'),
    ('Card', 'cardnumber'), ('Account', 'accountnumber'),
    ('Aadhaar', ''), ('ID', 'aadhaarnumber'), ('Code', 'verificationcode'),
    ('Code', 'onetimepassword'), ('Code', 'otp1'),
])
def test_owner_only_fields_are_refused_by_name_token(page, words, name):
    page['page'].found = {'found': True, 'x': 1, 'y': 1, 'type': 'text',
                          'autocomplete': '', 'words': words, 'name': name}
    out = ab.type_text(X, words, 'whatever', agent_id='a1')
    assert not out['success'] and 'open_window' in out['error']
    assert 'Input.insertText' not in _methods(page['page'])


@pytest.mark.parametrize('words,name', [
    ('company name', ''), ('subject', ''), ('japan office address', ''),
    # rv_br2: refused though ordinary, or must stay typeable:
    ('Company (as on PAN)', 'company'), ('One time setup fee', 'fee'),
    ('Email', 'passwordless-email'), ('Pincode', 'pincode'),
    ('PIN code', 'pin_code'), ('Account nickname', ''), ('account notes', ''),
    # rv_br3: a name that only says what is written as on the PAN card
    ('Name as per PAN', 'name'), ('Company name as on PAN', 'company'),
    ('Notes', 'cardnotes'), ('Fee', 'onetimefee'),
])
def test_ordinary_fields_are_not_mistaken_for_owner_only_ones(page, words, name):
    page['page'].found = {'found': True, 'x': 1, 'y': 1, 'type': 'text',
                          'autocomplete': '', 'words': words, 'name': name}
    assert ab.type_text(X, words, 'Hevolve', agent_id='a1')['success']


def test_the_card_refusal_names_the_window_action(page):
    out = ab.type_text(X, 'Notes', '4111 1111 1111 1111', agent_id='a1')
    assert 'action "open_window"' in out['error']


@pytest.mark.parametrize('url', [
    'file://localhost/C:/Windows/win.ini', 'file://evil.example/share/x',
    'chrome://settings/passwords', 'javascript://example.com/%0Aalert(1)',
    'ftp://example.com/x', 'http://169.254.169.254/latest/meta-data/',
])
def test_the_browser_itself_refuses_a_non_web_address(page, url):
    """The second layer: the script refuses even when called past dispatch."""
    out = ab.open_page(X, url)
    assert not out['success'] and 'http' in out['error']
    assert page['opened'] == []
    out = ab.open_window(X, url)
    assert not out['success']


def test_text_holding_a_card_number_is_refused_before_anything_runs(page):
    out = ab.type_text(X, 'Notes', 'pay with 4111 1111 1111 1111 please', agent_id='a1')
    assert not out['success'] and 'card number' in out['error']
    assert page['opened'] == []


def test_a_browser_that_cannot_start_is_an_answer_not_a_crash(page, monkeypatch):
    def _no_browser(mailbox, headless=True):
        raise driver.AgentBrowserUnavailable('no Chrome was found; set HEVOLVE_CHROME_PATH')
    monkeypatch.setattr(ab, '_page', _no_browser)
    out = ab.read_page(X)
    assert out == {'success': False, 'mailbox': X, 'connection_mechanism': 'agent_profile_cdp',
                   'error': 'no Chrome was found; set HEVOLVE_CHROME_PATH'}


def test_open_window_starts_the_profile_with_a_window_at_the_address(page, monkeypatch):
    calls = []
    monkeypatch.setattr(driver, 'ensure_agent_browser',
                        lambda m, headless=False, url=None: calls.append((m, headless, url)) or 'http://127.0.0.1:9300')
    monkeypatch.setattr(ab.cdp.CDPPage, 'attach', classmethod(lambda cls, endpoint: page['page']))
    out = ab.open_window(X, 'https://email.ionos.com')
    assert calls == [(X, False, 'https://email.ionos.com')]
    assert page['page'].navigated == ['https://email.ionos.com'], 'goes there when it is elsewhere'
    assert out['success'] and 'Computer_Action' in out['message'] and 'signs in' in out['message']


# ── Through the one dispatch surface ──

def test_dispatch_routes_each_action_and_audits_it(monkeypatch):
    seen, audited = [], []
    monkeypatch.setattr(ab, 'open_page', lambda **kw: seen.append(kw) or
                        {'success': True, 'connection_mechanism': 'agent_profile_cdp'})
    monkeypatch.setattr(tools.audit, 'append', lambda **kw: audited.append(kw))
    out = tools.dispatch('Agent_Browser_Open', 'u1', url='https://mail.example',
                         mailbox=X, headless=True)
    assert out['success']
    assert seen == [{'url': 'https://mail.example', 'mailbox': X, 'headless': True}]
    assert audited[-1]['connection_mechanism'] == 'agent_profile_cdp'
    assert {t['name'] for t in tools.list_tools()} >= {
        'Agent_Browser_Open', 'Agent_Browser_Read', 'Agent_Browser_Click',
        'Agent_Browser_Type', 'Agent_Browser_Window'}


@pytest.mark.parametrize('url', [
    'file:///C:/Users/secret.txt',
    # rv_br2 on ecbb9d03d: these have a host, and the allowlist let them in;
    # the first one read a local file live.
    'file://localhost/C:/Windows/win.ini', 'file://evil.example/share/x',
    'chrome://settings/passwords', 'javascript://example.com/%0Aalert(1)',
    'ftp://example.com/x', 'http://169.254.169.254/latest/meta-data/',
])
def test_dispatch_refuses_a_non_web_address(monkeypatch, url):
    monkeypatch.setattr(tools.audit, 'append', lambda **kw: None)
    monkeypatch.setattr(ab, 'open_page', lambda **kw: pytest.fail('must not run'))
    out = tools.dispatch('Agent_Browser_Open', 'u1', url=url, mailbox=X)
    assert not out['success'] and 'not allowed' in out['error']


@pytest.mark.parametrize('url, why', [
    ('file://localhost/C:/Windows/win.ini', 'http or https'),
    ('http://169.254.169.254/latest/meta-data/', 'metadata'),
])
def test_dispatch_says_why_an_address_was_refused(monkeypatch, url, why):
    """rv_br3 on 506273cc7: the refusal named only the URL; the owner's rule
    (2026-11-09) is that it says what was wrong and what to give instead."""
    audited = []
    monkeypatch.setattr(tools.audit, 'append', lambda **kw: audited.append(kw))
    out = tools.dispatch('Agent_Browser_Open', 'u1', url=url, mailbox=X)
    assert why in out['error']
    assert why in audited[-1]['error']


def test_dispatch_names_the_sites_a_platform_script_may_open(monkeypatch):
    monkeypatch.setattr(tools.audit, 'append', lambda **kw: None)
    out = tools.dispatch('YouTube_Transcript', 'u1', url='https://example.com/v')
    assert 'youtube.com' in out['error'] and 'example.com' in out['error']


def test_dispatch_still_takes_a_lan_address(monkeypatch):
    """Owner 2026-09-14: LAN and loopback are allowed by default."""
    monkeypatch.setattr(tools.audit, 'append', lambda **kw: None)
    monkeypatch.setattr(ab, 'open_page', lambda **kw: {'success': True})
    assert tools.dispatch('Agent_Browser_Open', 'u1', url='http://192.168.0.9:6777/',
                          mailbox=X)['success']
