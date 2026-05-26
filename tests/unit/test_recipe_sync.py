"""Tests for core.recipe_sync - cross-device cloud push/pull of
recipe-file bundles.  Closes the cross-device gap from the
2026-05-04 Speech Therapy silent-fallback incident."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from core.recipe_sync import (
    _files_for_prompt, _checksum, build_envelope,
    push_recipe, pull_recipe, SCHEMA_VERSION,
    _safe_filename, _load_push_cache, _store_push_cache,
)


class TestSafeFilename(unittest.TestCase):
    """M2 defense: harden against path-traversal / drive-letter /
    NUL-byte / Windows-reserved-name attacks in untrusted cloud
    payloads.  Each rejection class gets a test."""

    def test_normal_filenames_accepted(self):
        for ok in ['12345.json', '12345_personality.json',
                   '12345_0_recipe.json', '12345_0_1.json']:
            self.assertTrue(_safe_filename(ok), msg=ok)

    def test_empty_and_dotfiles_rejected(self):
        for bad in ['', '.', '..', '.hidden', '.gitignore']:
            self.assertFalse(_safe_filename(bad), msg=bad)

    def test_path_separators_rejected(self):
        for bad in ['../etc/passwd', '..\\windows\\system',
                    'sub/file.json', 'sub\\file.json',
                    '../../../etc/passwd']:
            self.assertFalse(_safe_filename(bad), msg=bad)

    def test_nul_byte_rejected(self):
        self.assertFalse(_safe_filename('file\x00.json'))
        self.assertFalse(_safe_filename('\x00bad.json'))

    def test_windows_drive_letter_rejected(self):
        for bad in ['C:foo.json', 'D:bar.json', 'c:relative.json',
                    'Z:\\absolute.json']:
            self.assertFalse(_safe_filename(bad), msg=bad)

    def test_windows_reserved_names_rejected(self):
        for bad in ['CON.json', 'PRN.json', 'AUX.json', 'NUL.json',
                    'COM1.json', 'COM9.json', 'LPT1.json', 'LPT9.json',
                    'con.json', 'nul.json',  # case-insensitive
                    'CON', 'NUL']:           # no extension
            self.assertFalse(_safe_filename(bad), msg=bad)

    def test_basename_mismatch_rejected(self):
        # Anything where os.path.basename(x) != x
        for bad in ['./file.json', 'a/b']:
            self.assertFalse(_safe_filename(bad), msg=bad)


class TestPushCacheIdempotency(unittest.TestCase):
    """M7: push_recipe skips redundant network roundtrips when the
    bundle's checksum matches the last successful push."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, '12345.json'), 'w') as f:
            f.write('{"name":"X"}')
        # Isolate the cache file location for this test
        import core.recipe_sync as _rs
        self._orig_cache_file = _rs._PUSH_CACHE_FILE
        self._test_cache = os.path.join(self.tmp, 'push_cache.json')
        _rs._PUSH_CACHE_FILE = self._test_cache

    def tearDown(self):
        import core.recipe_sync as _rs
        _rs._PUSH_CACHE_FILE = self._orig_cache_file
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_repeat_push_skips_when_unchanged(self):
        """First push hits network + caches checksum.  Second push
        with same content skips network."""
        mock_resp = MagicMock(status_code=200)
        with patch('core.http_pool.pooled_post', return_value=mock_resp) as mock_post:
            # First push: network call
            self.assertTrue(push_recipe(self.tmp, '12345',
                                         central_url='https://x'))
            self.assertEqual(mock_post.call_count, 1)
            # Second push with same content: skipped
            self.assertTrue(push_recipe(self.tmp, '12345',
                                         central_url='https://x'))
            self.assertEqual(mock_post.call_count, 1,
                'second push should have been cached')

    def test_force_overrides_cache(self):
        mock_resp = MagicMock(status_code=200)
        with patch('core.http_pool.pooled_post', return_value=mock_resp) as mock_post:
            push_recipe(self.tmp, '12345', central_url='https://x')
            push_recipe(self.tmp, '12345', central_url='https://x', force=True)
            self.assertEqual(mock_post.call_count, 2,
                'force=True must bypass the checksum cache')

    def test_changed_content_pushes(self):
        mock_resp = MagicMock(status_code=200)
        with patch('core.http_pool.pooled_post', return_value=mock_resp) as mock_post:
            push_recipe(self.tmp, '12345', central_url='https://x')
            # Modify file
            with open(os.path.join(self.tmp, '12345.json'), 'w') as f:
                f.write('{"name":"Y"}')
            push_recipe(self.tmp, '12345', central_url='https://x')
            self.assertEqual(mock_post.call_count, 2,
                'changed content must trigger a fresh push')


class TestFilesForPrompt(unittest.TestCase):
    """Filename matching for a given prompt_id."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Mix of files to test the matcher.
        for name in [
            '12345.json',
            '12345_personality.json',
            '12345_0_recipe.json',
            '12345_0_1.json',
            '12345_1_recipe.json',
            # Should NOT match:
            '54321.json',
            '12345.txt',
            '123456.json',  # different prompt_id
            'random.json',
        ]:
            with open(os.path.join(self.tmp, name), 'w') as f:
                f.write('{}')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_matches_exact_and_prefix(self):
        names = _files_for_prompt(self.tmp, 12345)
        self.assertIn('12345.json', names)
        self.assertIn('12345_personality.json', names)
        self.assertIn('12345_0_recipe.json', names)
        self.assertIn('12345_0_1.json', names)
        self.assertIn('12345_1_recipe.json', names)
        # Excludes:
        self.assertNotIn('54321.json', names)
        self.assertNotIn('12345.txt', names)
        self.assertNotIn('123456.json', names,
            'must not match a longer-id prefix overlap')
        self.assertNotIn('random.json', names)

    def test_returns_sorted(self):
        names = _files_for_prompt(self.tmp, 12345)
        self.assertEqual(names, sorted(names))

    def test_missing_dir_returns_empty(self):
        self.assertEqual(_files_for_prompt('/nope/missing/dir/xyz', 1), [])

    def test_string_prompt_id(self):
        names = _files_for_prompt(self.tmp, '12345')
        self.assertIn('12345.json', names)


class TestChecksum(unittest.TestCase):
    """Stable hash for cloud dedup + skip-rewrite-when-equal."""

    def test_same_files_same_checksum(self):
        a = {'a.json': '{"x":1}', 'b.json': '{"y":2}'}
        b = {'b.json': '{"y":2}', 'a.json': '{"x":1}'}  # different insert order
        self.assertEqual(_checksum(a), _checksum(b))

    def test_different_content_different_checksum(self):
        a = {'a.json': '{"x":1}'}
        b = {'a.json': '{"x":2}'}
        self.assertNotEqual(_checksum(a), _checksum(b))

    def test_empty_dict_stable(self):
        self.assertEqual(_checksum({}), _checksum({}))


class TestBuildEnvelope(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_files_returns_none(self):
        self.assertIsNone(build_envelope(self.tmp, '12345'))

    def test_envelope_shape(self):
        with open(os.path.join(self.tmp, '12345.json'), 'w') as f:
            f.write('{"name":"Speech Therapy"}')
        with open(os.path.join(self.tmp, '12345_personality.json'), 'w') as f:
            f.write('{"warmth":0.7}')
        env = build_envelope(self.tmp, 12345, user_id='u1')
        self.assertEqual(env['schema_version'], SCHEMA_VERSION)
        self.assertEqual(env['prompt_id'], '12345')
        self.assertEqual(env['user_id'], 'u1')
        self.assertEqual(set(env['files'].keys()),
                         {'12345.json', '12345_personality.json'})
        self.assertEqual(env['files']['12345.json'], '{"name":"Speech Therapy"}')
        self.assertIn('checksum', env)
        self.assertIn('uploaded_at', env)


class TestPushRecipe(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, '12345.json'), 'w') as f:
            f.write('{"name":"X"}')
        # Isolate the M7 push cache so other tests' successful pushes
        # don't make us short-circuit a network call we want to verify.
        import core.recipe_sync as _rs
        self._orig_cache_file = _rs._PUSH_CACHE_FILE
        _rs._PUSH_CACHE_FILE = os.path.join(self.tmp, '_isolated_cache.json')

    def tearDown(self):
        import core.recipe_sync as _rs
        _rs._PUSH_CACHE_FILE = self._orig_cache_file
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_central_url_returns_false(self):
        with patch('core.config_cache.get_central_db_url', return_value=''):
            self.assertFalse(push_recipe(self.tmp, '12345', central_url=''))

    def test_no_local_files_returns_false(self):
        empty = tempfile.mkdtemp()
        try:
            self.assertFalse(push_recipe(empty, '99999',
                                          central_url='https://x'))
        finally:
            import shutil
            shutil.rmtree(empty, ignore_errors=True)

    def test_2xx_returns_true(self):
        mock_resp = MagicMock(status_code=200)
        with patch('core.http_pool.pooled_post', return_value=mock_resp):
            self.assertTrue(push_recipe(self.tmp, '12345',
                                         central_url='https://x'))

    def test_5xx_returns_false(self):
        mock_resp = MagicMock(status_code=503)
        with patch('core.http_pool.pooled_post', return_value=mock_resp):
            self.assertFalse(push_recipe(self.tmp, '12345',
                                          central_url='https://x'))

    def test_exception_returns_false_no_raise(self):
        with patch('core.http_pool.pooled_post',
                   side_effect=Exception('network down')):
            self.assertFalse(push_recipe(self.tmp, '12345',
                                          central_url='https://x'))


class TestPullRecipe(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mock_session(self, status_code, json_body=None):
        sess = MagicMock()
        resp = MagicMock(status_code=status_code)
        resp.json.return_value = json_body or {}
        sess.get.return_value = resp
        return sess

    def test_404_returns_false(self):
        with patch('core.http_pool.get_http_session',
                   return_value=self._mock_session(404)):
            self.assertFalse(pull_recipe(self.tmp, '12345',
                                          central_url='https://x'))

    def test_schema_mismatch_returns_false(self):
        with patch('core.http_pool.get_http_session',
                   return_value=self._mock_session(200, {
                       'schema_version': SCHEMA_VERSION + 99,
                       'files': {'a.json': '{}'},
                   })):
            self.assertFalse(pull_recipe(self.tmp, '12345',
                                          central_url='https://x'))

    def test_writes_files_to_dir(self):
        envelope = {
            'schema_version': SCHEMA_VERSION,
            'prompt_id': '12345',
            'files': {
                '12345.json': '{"name":"Speech Therapy"}',
                '12345_0_recipe.json': '{"actions":[]}',
            },
            'checksum': 'doesntmatter',
        }
        with patch('core.http_pool.get_http_session',
                   return_value=self._mock_session(200, envelope)):
            self.assertTrue(pull_recipe(self.tmp, '12345',
                                         central_url='https://x'))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, '12345.json')))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, '12345_0_recipe.json')))
        with open(os.path.join(self.tmp, '12345.json')) as f:
            self.assertEqual(f.read(), '{"name":"Speech Therapy"}')

    def test_path_traversal_filenames_rejected(self):
        envelope = {
            'schema_version': SCHEMA_VERSION,
            'prompt_id': '12345',
            'files': {
                '../../../etc/passwd': 'pwned',
                'normal.json': '{}',
            },
            'checksum': '',
        }
        with patch('core.http_pool.get_http_session',
                   return_value=self._mock_session(200, envelope)):
            pull_recipe(self.tmp, '12345', central_url='https://x')
        # The path-traversal file must NOT have been written anywhere.
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, '..', '..', '..', 'etc', 'passwd')))

    def test_no_central_url_returns_false(self):
        with patch('core.config_cache.get_central_db_url', return_value=''):
            self.assertFalse(pull_recipe(self.tmp, '12345', central_url=''))


if __name__ == '__main__':
    unittest.main()
