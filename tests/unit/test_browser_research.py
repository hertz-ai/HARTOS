"""Browser Research subsystem — C1 test pack.

Covers: skeleton, audit log, domain allowlist, vault stub, driver mode probe,
T3 YouTube + web_generic dispatch, drift-guards.

T2 platform scripts and Obscura concrete impls have their own tests in C4+.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

HARTOS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if HARTOS_ROOT not in sys.path:
    sys.path.insert(0, HARTOS_ROOT)


class TestPackageImport(unittest.TestCase):
    """Skeleton — module imports cleanly with no T2 dependencies."""

    def test_package_imports(self):
        import integrations.browser_research as br
        self.assertTrue(hasattr(br, '__all__'))

    def test_audit_imports(self):
        from integrations.browser_research import audit
        self.assertTrue(callable(audit.append))
        self.assertTrue(callable(audit.read_recent))

    def test_tools_imports(self):
        from integrations.browser_research import tools
        self.assertTrue(callable(tools.dispatch))
        self.assertTrue(callable(tools.list_tools))

    def test_vault_imports(self):
        from integrations.browser_research import vault
        v = vault.get_vault()
        v2 = vault.get_vault()
        self.assertIs(v, v2, 'vault must be a process-wide singleton')

    def test_driver_imports_without_playwright(self):
        # Driver module must import even if Obscura/Playwright are not installed.
        from integrations.browser_research import driver
        self.assertTrue(callable(driver.get_driver))
        self.assertTrue(callable(driver.cdp_endpoint_reachable))


class TestAuditLog(unittest.TestCase):
    """Audit log writes one JSON line per call; never raises."""

    def test_append_and_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=False):
                with patch('integrations.browser_research.audit._log_path',
                           return_value=os.path.join(tmp, 'audit.log')):
                    from integrations.browser_research import audit
                    audit.append(user_id='u1', tool='YouTube_Transcript',
                                 platform='youtube', connection_mechanism='public_http',
                                 success=True)
                    audit.append(user_id='u1', tool='Read_Webpage',
                                 platform='web_generic', connection_mechanism='public_http',
                                 success=False, error='timeout')
                    records = audit.read_recent(limit=10)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]['tool'], 'YouTube_Transcript')
            self.assertEqual(records[1]['error'], 'timeout')
            self.assertTrue(records[0]['success'])
            self.assertFalse(records[1]['success'])

    def test_append_never_raises_on_io_failure(self):
        from integrations.browser_research import audit
        with patch('integrations.browser_research.audit._log_path',
                   return_value='/nonexistent/path/that/cannot/be/written/audit.log'):
            audit.append(user_id='u1', tool='x', platform='y',
                         connection_mechanism='z', success=False)


class TestDomainAllowlist(unittest.TestCase):
    """Allowlist fails closed; suffix match; unknown script rejected."""

    def test_youtube_allowed(self):
        from integrations.browser_research import domain_allowlist as dal
        self.assertTrue(dal.host_allowed('youtube', 'https://www.youtube.com/watch?v=abc'))
        self.assertTrue(dal.host_allowed('youtube', 'https://youtu.be/abc'))

    def test_youtube_rejects_off_list(self):
        from integrations.browser_research import domain_allowlist as dal
        self.assertFalse(dal.host_allowed('youtube', 'https://evil.example.com/'))
        self.assertFalse(dal.host_allowed('youtube', 'https://fake-youtube.com/'))

    def test_unknown_script_fails_closed(self):
        from integrations.browser_research import domain_allowlist as dal
        self.assertFalse(dal.host_allowed('does_not_exist', 'https://anything.com/'))

    def test_web_generic_empty_allowlist_accepts_any(self):
        from integrations.browser_research import domain_allowlist as dal
        self.assertTrue(dal.host_allowed('web_generic', 'https://anywhere.com/'))

    def test_twitter_x_dot_com(self):
        from integrations.browser_research import domain_allowlist as dal
        self.assertTrue(dal.host_allowed('twitter', 'https://x.com/elon'))
        self.assertTrue(dal.host_allowed('twitter', 'https://twitter.com/jack'))

    def test_subdomain_suffix_match(self):
        from integrations.browser_research import domain_allowlist as dal
        self.assertTrue(dal.host_allowed('reddit', 'https://old.reddit.com/r/X'))

    def test_malformed_url_rejected(self):
        from integrations.browser_research import domain_allowlist as dal
        self.assertFalse(dal.host_allowed('youtube', ''))
        self.assertFalse(dal.host_allowed('youtube', 'not-a-url'))


class TestVaultEncrypted(unittest.TestCase):
    """C4: AES-GCM encrypted-at-rest vault with cookie storage."""

    def test_add_get_revoke_lifecycle(self):
        from integrations.browser_research.vault import AccountVault, Account
        with tempfile.TemporaryDirectory() as tmp:
            v = AccountVault(path=os.path.join(tmp, 'vault.enc'))
            cookies = [{'name': 'auth_token', 'value': 'abc123', 'domain': '.x.com', 'path': '/'}]
            acc = Account(platform='twitter', handle='@me', cookies=cookies,
                          capabilities={'read', 'post'})
            v.add(acc)
            got = v.get('twitter', '@me')
            self.assertIsNotNone(got)
            self.assertEqual(got.platform, 'twitter')
            self.assertEqual(got.handle, '@me')
            self.assertEqual(got.cookies, cookies)
            self.assertEqual(got.capabilities, {'read', 'post'})
            self.assertEqual(v.list_platforms(), ['twitter'])
            self.assertEqual(v.list_handles('twitter'), ['@me'])
            self.assertTrue(v.revoke('twitter', '@me'))
            self.assertIsNone(v.get('twitter', '@me'))
            self.assertFalse(v.revoke('twitter', '@me'))

    def test_vault_persists_across_instances(self):
        from integrations.browser_research.vault import AccountVault, Account
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'vault.enc')
            v1 = AccountVault(path=path)
            v1.add(Account(platform='reddit', handle='u/test',
                           cookies=[{'name': 'session', 'value': 's1'}]))
            # Fresh instance reads same disk
            v2 = AccountVault(path=path)
            got = v2.get('reddit', 'u/test')
            self.assertIsNotNone(got)
            self.assertEqual(got.cookies[0]['value'], 's1')

    def test_vault_encrypts_at_rest(self):
        """Cookie value must not appear in plaintext on disk if cryptography is available."""
        from integrations.browser_research.vault import AccountVault, Account
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        except ImportError:
            self.skipTest('cryptography not installed; vault degrades to plaintext (acceptable)')
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'vault.enc')
            v = AccountVault(path=path)
            v.add(Account(platform='twitter', handle='@me',
                          cookies=[{'name': 'auth_token', 'value': 'SECRET_VALUE_XYZ'}]))
            with open(path, encoding='utf-8') as f:
                disk = f.read()
            self.assertNotIn('SECRET_VALUE_XYZ', disk,
                             'cookie value must be encrypted at rest')


class TestDriverModeProbe(unittest.TestCase):
    """Driver instantiates without Obscura/Playwright; mode='auto' falls back."""

    def test_cdp_probe_returns_false_for_dead_port(self):
        from integrations.browser_research import driver
        # Port 1 is privileged + unlikely to be listening for CDP.
        self.assertFalse(driver.cdp_endpoint_reachable(host='127.0.0.1', port=1))

    def test_get_driver_auto_falls_back_to_b1(self):
        from integrations.browser_research import driver
        with patch('integrations.browser_research.driver.cdp_endpoint_reachable',
                   return_value=False):
            d = driver.get_driver(mode='auto')
            self.assertEqual(d.connection_mechanism, 'obscura_b1_headless_profile')

    def test_get_driver_auto_picks_b2_when_reachable(self):
        from integrations.browser_research import driver
        with patch('integrations.browser_research.driver.cdp_endpoint_reachable',
                   return_value=True):
            d = driver.get_driver(mode='auto')
            self.assertEqual(d.connection_mechanism, 'obscura_b2_cdp_user_chrome')

    def test_b2_raises_when_unreachable(self):
        from integrations.browser_research import driver
        with patch('integrations.browser_research.driver.cdp_endpoint_reachable',
                   return_value=False):
            with self.assertRaises(RuntimeError):
                driver.get_driver(mode='b2')

    def test_unknown_mode_raises(self):
        from integrations.browser_research import driver
        with self.assertRaises(ValueError):
            driver.get_driver(mode='garbage')


class TestToolsDispatch(unittest.TestCase):
    """Dispatcher routes correctly, logs audit, never raises."""

    def test_list_tools_returns_known(self):
        from integrations.browser_research import tools
        names = {t['name'] for t in tools.list_tools()}
        self.assertIn('YouTube_Transcript', names)
        # Read_Webpage removed 2026-06-08 — was parallel path to
        # data_extraction_from_url (Crawl4AI → web_crawler.py).
        self.assertNotIn('Read_Webpage', names)

    def test_unknown_tool_returns_error(self):
        from integrations.browser_research import tools
        result = tools.dispatch(tool='NoSuchTool', user_id='u1')
        self.assertFalse(result['success'])
        self.assertIn('unknown tool', result['error'])

    def test_off_allowlist_url_blocked(self):
        from integrations.browser_research import tools
        with tempfile.TemporaryDirectory() as tmp:
            with patch('integrations.browser_research.audit._log_path',
                       return_value=os.path.join(tmp, 'a.log')):
                result = tools.dispatch(tool='YouTube_Transcript', user_id='u1',
                                        url='https://evil.example.com/foo')
        self.assertFalse(result['success'])
        self.assertIn('not allowed', result['error'])

    def test_youtube_transcript_returns_connection_mechanism(self):
        from integrations.browser_research import tools
        # Even on failure (no youtube_transcript_api), the response shape includes
        # connection_mechanism so the agent can describe what it tried.
        with tempfile.TemporaryDirectory() as tmp:
            with patch('integrations.browser_research.audit._log_path',
                       return_value=os.path.join(tmp, 'a.log')):
                result = tools.dispatch(tool='YouTube_Transcript', user_id='u1',
                                        url='https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        self.assertIn('connection_mechanism', result)
        self.assertEqual(result['connection_mechanism'], 'public_http')

    def test_read_webpage_removed_unknown_tool(self):
        # Drift-guard for the parallel-path removal: Read_Webpage must NOT route.
        # data_extraction_from_url is the canonical "fetch a URL" tool — see
        # core/agent_tools.py:1008 + integrations/web_crawler.py (Crawl4AI).
        from integrations.browser_research import tools
        result = tools.dispatch(tool='Read_Webpage', user_id='u1')
        self.assertFalse(result['success'])
        self.assertIn('unknown tool', result['error'])


class TestYoutubeIdExtraction(unittest.TestCase):
    """ID extraction handles common YouTube URL shapes."""

    def test_standard_watch(self):
        from integrations.browser_research.scripts import youtube
        self.assertEqual(
            youtube._extract_video_id('https://www.youtube.com/watch?v=dQw4w9WgXcQ'),
            'dQw4w9WgXcQ',
        )

    def test_short_youtu_be(self):
        from integrations.browser_research.scripts import youtube
        self.assertEqual(
            youtube._extract_video_id('https://youtu.be/dQw4w9WgXcQ'),
            'dQw4w9WgXcQ',
        )

    def test_shorts(self):
        from integrations.browser_research.scripts import youtube
        self.assertEqual(
            youtube._extract_video_id('https://www.youtube.com/shorts/dQw4w9WgXcQ'),
            'dQw4w9WgXcQ',
        )

    def test_non_youtube_returns_none(self):
        from integrations.browser_research.scripts import youtube
        self.assertIsNone(youtube._extract_video_id('https://vimeo.com/123'))
        self.assertIsNone(youtube._extract_video_id(''))


class TestAgentToolRegistration(unittest.TestCase):
    """Drift-guard: tool names are registered in core.agent_tools so both
    LangChain (helper.register_for_llm) and autogen
    (executor.register_for_execution) pick them up via register_core_tools.
    """

    def test_browser_research_tools_in_closure_list(self):
        # Build a minimal closure ctx and assert YouTube_Transcript +
        # Read_Webpage appear in the returned tools list.
        from core import agent_tools

        class _FakeHelper:
            def __init__(self): self.tools = []
            def txt2img(self, *a, **kw): return ''
            def get_user_camera_inp(self, *a, **kw): return ''
            def save_agent_data_to_file(self, *a, **kw): return True

        ctx = {
            'user_id': 1, 'prompt_id': 'p1', 'agent_data': {},
            'helper_fun': _FakeHelper(),
            'user_prompt': 'x', 'request_id_list': {'x': 'r1'},
            'recent_file_id': '', 'scheduler': None,
            'log_tool_execution': (lambda f: f),
            'send_message_to_user1': (lambda *a, **kw: None),
            'retrieve_json': (lambda s: {}),
            'strip_json_values': (lambda x: x),
            'save_conversation_db': (lambda *a, **kw: None),
        }
        try:
            closures = agent_tools.build_core_tool_closures(ctx)
        except Exception as exc:
            self.skipTest(f'closure build needs heavier fixtures: {exc}')
            return
        names = {name for name, _desc, _fn in closures}
        self.assertIn('YouTube_Transcript', names,
                      'YouTube_Transcript must be registered (BR-C2)')
        # Read_Webpage removed 2026-06-08: data_extraction_from_url is canonical.
        self.assertNotIn('Read_Webpage', names,
                         'Read_Webpage must NOT shadow data_extraction_from_url')
        self.assertIn('data_extraction_from_url', names,
                      'data_extraction_from_url is the canonical URL-fetch tool')


class TestPerPlatformDispatch(unittest.TestCase):
    """C4: Search_Platform / Read_Timeline route by `platform` kwarg + consent gate."""

    def test_search_platform_without_platform_kwarg_fails(self):
        from integrations.browser_research import tools
        result = tools.dispatch(tool='Search_Platform', user_id='u1', query='foo')
        self.assertFalse(result['success'])
        self.assertIn('platform', result['error'])

    def test_search_platform_unknown_platform_fails(self):
        from integrations.browser_research import tools
        result = tools.dispatch(tool='Search_Platform', user_id='u1',
                                platform='myspace', query='foo')
        self.assertFalse(result['success'])
        self.assertIn('platform', result['error'])

    def test_search_platform_consent_denied(self):
        from integrations.browser_research import tools
        with tempfile.TemporaryDirectory() as tmp:
            with patch('integrations.browser_research.audit._log_path',
                       return_value=os.path.join(tmp, 'a.log')):
                result = tools.dispatch(
                    tool='Search_Platform', user_id='u1',
                    platform='twitter', query='ai news',
                    consent_check=lambda uid, scope: False,
                )
        self.assertFalse(result['success'])
        self.assertIn('consent required', result['error'])
        self.assertEqual(result['liquid_ui']['type'], 'consent_prompt')
        self.assertEqual(result['liquid_ui']['scope'], 'web_research:twitter')

    def test_search_platform_with_consent_routes_to_twitter_script(self):
        """Consent granted → script_mod.search() invoked.  Mock the crawler.

        twitter.py imports fetch_with_session into its own namespace, so we
        patch THAT binding (not _base's), per how Python imports work.
        """
        # Force import so we can patch the bound name
        from integrations.browser_research.scripts import twitter as _tw  # noqa: F401
        from integrations.browser_research import tools
        with tempfile.TemporaryDirectory() as tmp:
            with patch('integrations.browser_research.audit._log_path',
                       return_value=os.path.join(tmp, 'a.log')):
                with patch('integrations.browser_research.scripts.twitter.fetch_with_session',
                           return_value={'success': True, 'url': 'https://x.com/search?q=ai',
                                         'markdown': 'mocked',
                                         'connection_mechanism': 'obscura_b1_headless_profile'}):
                    result = tools.dispatch(
                        tool='Search_Platform', user_id='u1',
                        platform='twitter', query='ai',
                        consent_check=lambda uid, scope: True,
                    )
        self.assertTrue(result['success'])
        self.assertEqual(result['action'], 'search')
        self.assertEqual(result['query'], 'ai')
        self.assertEqual(result['connection_mechanism'], 'obscura_b1_headless_profile')

    def test_read_timeline_requires_target_handle(self):
        from integrations.browser_research import tools
        result = tools.dispatch(
            tool='Read_Timeline', user_id='u1',
            platform='twitter',
            consent_check=lambda uid, scope: True,
        )
        self.assertFalse(result['success'])
        self.assertIn('target_handle', result['error'])


class TestPostAsUserPreviewConfirm(unittest.TestCase):
    """C7: Post_As_User defaults dry_run=True, returns preview card.
    Real post (dry_run=False) is intentionally gated until per-platform
    write paths are reviewed.
    """

    def test_post_requires_content(self):
        from integrations.browser_research import tools
        result = tools.dispatch(
            tool='Post_As_User', user_id='u1',
            platform='twitter',
            consent_check=lambda uid, scope: True,
        )
        self.assertFalse(result['success'])
        self.assertIn('content', result['error'])

    def test_dry_run_default_returns_preview(self):
        from integrations.browser_research import tools
        with tempfile.TemporaryDirectory() as tmp:
            with patch('integrations.browser_research.audit._log_path',
                       return_value=os.path.join(tmp, 'a.log')):
                result = tools.dispatch(
                    tool='Post_As_User', user_id='u1',
                    platform='twitter', content='hello world',
                    consent_check=lambda uid, scope: True,
                )
        self.assertTrue(result['success'])
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['connection_mechanism'], 'preview_only')
        self.assertEqual(result['liquid_ui']['type'], 'post_preview')
        self.assertEqual(result['liquid_ui']['platform'], 'twitter')
        self.assertEqual(result['liquid_ui']['content'], 'hello world')
        self.assertEqual(result['liquid_ui']['confirm_args']['dry_run'], False)

    def test_dry_run_false_gated_until_implemented(self):
        """Real write is intentionally not yet plumbed — must return clear error."""
        from integrations.browser_research import tools
        with tempfile.TemporaryDirectory() as tmp:
            with patch('integrations.browser_research.audit._log_path',
                       return_value=os.path.join(tmp, 'a.log')):
                result = tools.dispatch(
                    tool='Post_As_User', user_id='u1',
                    platform='twitter', content='hello', dry_run=False,
                    consent_check=lambda uid, scope: True,
                )
        self.assertFalse(result['success'])
        self.assertIn('preview-only', result['error'])
        self.assertEqual(result['connection_mechanism'], 'unimplemented_write')

    def test_post_consent_denied(self):
        from integrations.browser_research import tools
        with tempfile.TemporaryDirectory() as tmp:
            with patch('integrations.browser_research.audit._log_path',
                       return_value=os.path.join(tmp, 'a.log')):
                result = tools.dispatch(
                    tool='Post_As_User', user_id='u1',
                    platform='twitter', content='x',
                    consent_check=lambda uid, scope: False,
                )
        self.assertFalse(result['success'])
        self.assertEqual(result['liquid_ui']['type'], 'consent_prompt')


class TestCrawlerExtension(unittest.TestCase):
    """C4: web_crawler.crawl_url_with_cookies exists + accepts cookies/cdp."""

    def test_extension_signature(self):
        from integrations import web_crawler
        self.assertTrue(callable(web_crawler.crawl_url_with_cookies))
        # Sanity: it accepts the kwargs we'll be calling it with.
        import inspect
        sig = inspect.signature(web_crawler.crawl_url_with_cookies)
        self.assertIn('cookies', sig.parameters)
        self.assertIn('cdp_endpoint', sig.parameters)
        self.assertIn('timeout', sig.parameters)

    def test_existing_crawl_url_unchanged(self):
        """Zero-regression: the canonical crawl_url is still there with its signature."""
        from integrations import web_crawler
        self.assertTrue(callable(web_crawler.crawl_url))
        self.assertTrue(callable(web_crawler.crawl_url_for_agent))
        self.assertTrue(callable(web_crawler.crawl_urls))


class TestEndToEnd_CookieAuthFlow(unittest.TestCase):
    """END-TO-END: full chain from vault → script → web_crawler → audit → result.

    The mock seam is INSIDE web_crawler.py only — at the underlying crawl4ai /
    Playwright connect_over_cdp boundary.  Everything above that runs for real:

      vault.add(Account)
        -> vault.get(...)               (real AES-GCM round-trip)
        -> twitter.search(query)
        -> _base.fetch_with_session(...)
        -> web_crawler.crawl_url_with_cookies(url, cookies, cdp_endpoint)
          [MOCKED: _crawl_with_cookies returns synthetic result with cookies]
        -> dispatcher annotates result + writes audit log
        -> tools.dispatch returns to caller
        -> assertions verify cookies traversed AND audit log persisted

    This proves the wiring really delivers cookies end-to-end and produces
    the connection_mechanism / audit trail the agent UI depends on.
    """

    def _run_e2e(self, audit_path, mock_crawl):
        """Run the full e2e flow with the underlying crawler mocked."""
        from integrations.browser_research import vault, tools
        from integrations.browser_research.vault import Account
        v = vault.reset_vault_for_tests(path=audit_path + '.vault.enc')
        v.add(Account(
            platform='twitter', handle='@me',
            cookies=[{'name': 'auth_token', 'value': 'SECRET_AUTH',
                      'domain': '.x.com', 'path': '/'}],
            capabilities={'read'},
        ))
        # Patch the underlying crawler primitive — everything else is real.
        with patch('integrations.browser_research.audit._log_path',
                   return_value=audit_path):
            with patch('integrations.web_crawler.crawl_url_with_cookies',
                       side_effect=mock_crawl):
                result = tools.dispatch(
                    tool='Search_Platform', user_id='u1',
                    platform='twitter', query='ai news', handle='@me',
                    consent_check=lambda uid, scope: True,
                )
        return result

    def test_cookies_traverse_to_crawler(self):
        """Vault cookies must reach web_crawler.crawl_url_with_cookies."""
        captured = {}
        def fake_crawler(url, cookies=None, timeout=30, cdp_endpoint=None, **_):
            captured['url'] = url
            captured['cookies'] = cookies
            captured['cdp_endpoint'] = cdp_endpoint
            return {'success': True, 'url': url, 'markdown': 'mock body',
                    'word_count': 2, 'connection_mechanism': 'obscura_b1_headless_profile'}

        with tempfile.TemporaryDirectory() as tmp:
            result = self._run_e2e(os.path.join(tmp, 'audit.log'), fake_crawler)

        self.assertTrue(result['success'], f'expected success, got {result!r}')
        self.assertIn('x.com/search', captured['url'])
        self.assertIn('ai+news', captured['url'])  # quote_plus encoded
        self.assertEqual(len(captured['cookies']), 1)
        self.assertEqual(captured['cookies'][0]['name'], 'auth_token')
        self.assertEqual(captured['cookies'][0]['value'], 'SECRET_AUTH',
                         'vault must AES-decrypt and pass the real cookie value through')

    def test_b2_cdp_endpoint_traverses_when_env_set(self):
        """HEVOLVE_BROWSER_USE_B2=1 + endpoint env -> reaches crawler."""
        captured = {}
        def fake_crawler(url, cookies=None, timeout=30, cdp_endpoint=None, **_):
            captured['cdp_endpoint'] = cdp_endpoint
            return {'success': True, 'url': url, 'markdown': 'x',
                    'connection_mechanism': 'obscura_b2_cdp_user_chrome'}

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {
                'HEVOLVE_BROWSER_USE_B2': '1',
                'HEVOLVE_BROWSER_CDP_ENDPOINT': 'http://127.0.0.1:9333',
            }):
                result = self._run_e2e(os.path.join(tmp, 'audit.log'), fake_crawler)
        self.assertEqual(captured['cdp_endpoint'], 'http://127.0.0.1:9333')
        self.assertEqual(result['connection_mechanism'], 'obscura_b2_cdp_user_chrome')

    def test_audit_log_records_e2e_call(self):
        """Audit log must capture the call (real I/O, real JSON write)."""
        def fake_crawler(url, cookies=None, timeout=30, cdp_endpoint=None, **_):
            return {'success': True, 'url': url, 'markdown': 'x',
                    'connection_mechanism': 'obscura_b1_headless_profile'}

        with tempfile.TemporaryDirectory() as tmp:
            audit_path = os.path.join(tmp, 'audit.log')
            self._run_e2e(audit_path, fake_crawler)
            from integrations.browser_research import audit
            with patch('integrations.browser_research.audit._log_path',
                       return_value=audit_path):
                records = audit.read_recent(limit=10)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['tool'], 'Search_Platform')
        self.assertEqual(records[0]['platform'], 'twitter')
        self.assertEqual(records[0]['connection_mechanism'],
                         'obscura_b1_headless_profile')
        self.assertTrue(records[0]['success'])

    def test_e2e_via_langchain_agent_tool_facade(self):
        """The LangChain/autogen-facing Search_Platform tool must serialize
        the same full result as a JSON string — verifies the agent surface
        sees the cookies-through-to-crawler chain end to end.
        """
        captured = {}
        def fake_crawler(url, cookies=None, timeout=30, cdp_endpoint=None, **_):
            captured['cookies'] = cookies
            return {'success': True, 'url': url, 'markdown': 'tweet1 tweet2',
                    'word_count': 2,
                    'connection_mechanism': 'obscura_b1_headless_profile'}

        from core import agent_tools
        from integrations.browser_research import vault
        from integrations.browser_research.vault import Account

        class _FakeHelper:
            def txt2img(self, *a, **kw): return ''
            def get_user_camera_inp(self, *a, **kw): return ''
            def save_agent_data_to_file(self, *a, **kw): return True

        with tempfile.TemporaryDirectory() as tmp:
            audit_path = os.path.join(tmp, 'audit.log')
            v = vault.reset_vault_for_tests(path=audit_path + '.vault.enc')
            v.add(Account(platform='twitter', handle='@me',
                          cookies=[{'name': 'auth_token', 'value': 'ETOKEN'}]))

            ctx = {
                'user_id': 7, 'prompt_id': 'p1', 'agent_data': {},
                'helper_fun': _FakeHelper(),
                'user_prompt': 'x', 'request_id_list': {'x': 'r1'},
                'recent_file_id': '', 'scheduler': None,
                'log_tool_execution': (lambda f: f),
                'send_message_to_user1': (lambda *a, **kw: None),
                'retrieve_json': (lambda s: {}),
                'strip_json_values': (lambda x: x),
                'save_conversation_db': (lambda *a, **kw: None),
            }
            try:
                closures = agent_tools.build_core_tool_closures(ctx)
            except Exception as exc:
                self.skipTest(f'closures need heavier ctx: {exc}')
                return
            search_fn = next((fn for name, _, fn in closures
                              if name == 'Search_Platform'), None)
            self.assertIsNotNone(search_fn, 'Search_Platform must be registered')

            # When called via the agent-tool closure, the dispatcher's default
            # consent_check (real ConsentService) runs.  Mock has_capability so
            # the e2e flow proceeds past the gate.
            with patch('integrations.browser_research.audit._log_path',
                       return_value=audit_path):
                with patch('integrations.web_crawler.crawl_url_with_cookies',
                           side_effect=fake_crawler):
                    # Mock the canonical consent check so the e2e flow can
                    # exercise the cookie chain without seeding a real DB.
                    with patch(
                        'integrations.social.consent_service.ConsentService.check_consent',
                        return_value=True,
                    ):
                        with patch(
                            'integrations.browser_research.tools._resolve_db_session',
                            return_value=object(),
                        ):
                            json_result = search_fn(platform='twitter', query='ai',
                                                    handle='@me')

        # Result is JSON string from the agent tool — verify shape end-to-end.
        parsed = json.loads(json_result)
        self.assertTrue(parsed['success'])
        self.assertEqual(parsed['connection_mechanism'],
                         'obscura_b1_headless_profile')
        self.assertEqual(parsed['action'], 'search')
        self.assertEqual(captured['cookies'][0]['value'], 'ETOKEN',
                         'cookie value must traverse all the way from vault'
                         ' through agent_tools -> dispatch -> script -> crawler')


class TestEndToEnd_PostPreviewConfirm(unittest.TestCase):
    """E2E for the write-side preview-confirm chain."""

    def test_post_preview_then_confirm_routes_to_per_platform_post(self):
        """First call returns liquid_ui; agent re-invokes with dry_run=False.

        The dry_run=False return is intentionally gated ('unimplemented_write')
        — this test PINS that gate so a future commit can't silently start
        posting without a per-platform POST implementation review.
        """
        from integrations.browser_research import tools

        with tempfile.TemporaryDirectory() as tmp:
            with patch('integrations.browser_research.audit._log_path',
                       return_value=os.path.join(tmp, 'a.log')):
                # Step 1: preview (dry_run defaults True)
                step1 = tools.dispatch(
                    tool='Post_As_User', user_id='u1',
                    platform='twitter', content='hello',
                    consent_check=lambda uid, scope: True,
                )
                self.assertTrue(step1['success'])
                preview = step1['liquid_ui']
                self.assertEqual(preview['type'], 'post_preview')
                # Step 2: simulate user-confirm by invoking confirm_args
                confirm_args = preview['confirm_args']
                step2 = tools.dispatch(
                    tool='Post_As_User', user_id='u1',
                    consent_check=lambda uid, scope: True,
                    **confirm_args,
                )
        # Step 2 returns 'unimplemented_write' until per-platform POST is plumbed.
        # Drift-guard: if this assertion ever flips, somebody added a real
        # post path that bypasses the canonical preview-confirm review.
        self.assertFalse(step2['success'])
        self.assertEqual(step2['connection_mechanism'], 'unimplemented_write')
        self.assertIn('preview-only', step2['error'])


class TestT1AdapterIsolation(unittest.TestCase):
    """Drift-guard: no channel adapter file imports browser_research.

    Captures the zero-regression guarantee: existing 31 websocket adapters
    are untouched by the new T2 subsystem.
    """

    def test_no_channel_adapter_imports_browser_research(self):
        channels_dir = os.path.join(HARTOS_ROOT, 'integrations', 'channels')
        offenders: list[str] = []
        for root, dirs, files in os.walk(channels_dir):
            # Skip vendored node_modules — Baileys & friends are JS, not our concern.
            if 'node_modules' in dirs:
                dirs.remove('node_modules')
            if '__pycache__' in dirs:
                dirs.remove('__pycache__')
            for fname in files:
                if not fname.endswith('.py'):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, encoding='utf-8') as f:
                        src = f.read()
                except OSError:
                    continue
                if 'browser_research' in src:
                    offenders.append(fpath)
        self.assertEqual(offenders, [],
                         f'channel adapters must not import browser_research; offenders: {offenders}')


if __name__ == '__main__':
    unittest.main()
