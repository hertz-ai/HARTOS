"""The .social_secret_key has ONE discovery source shared by writer + reader (#98e).

`integrations.social.auth` WRITES/creates the persisted social secret key;
`security.jwt_manager` READS it. If they disagree on WHERE the key lives, a
token minted by one fails verification by the other. Before this change each
file built its own candidate-path list and they had already drifted —
jwt_manager used `os.path.join(HEVOLVE_DB_PATH, '..', ...)` (and probed a junk
`../.social_secret_key` when the env was unset) while auth used
`os.path.dirname(db_path)`.

These behavioural tests drive the REAL helpers + the REAL loaders against real
temp files and a controlled environment:

  * candidate ordering: explicit HEVOLVE_DB_PATH wins; junk paths never appear;
  * the writer's create-target is always the reader's top candidate;
  * ROUND-TRIP: a key created by auth is read back byte-identical by
    jwt_manager — the core anti-drift guard.

No grep tests.
"""
from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _clean_env(**overrides):
    """A minimal env with every secret-key-affecting var cleared, then overrides
    applied. Prevents the dev machine's real key locations from leaking in."""
    env = dict(os.environ)
    for k in ('SOCIAL_SECRET_KEY', 'HEVOLVE_DB_PATH', 'NUNBA_BUNDLED',
              'NUNBA_DATA_DIR', 'HARTOS_DATA_DIR'):
        env.pop(k, None)
    env.update(overrides)
    return env


def test_explicit_db_path_is_top_candidate_and_write_target():
    from core.platform_paths import (
        social_secret_key_candidates, social_secret_key_write_target)
    with tempfile.TemporaryDirectory() as tmp:
        fake_db = os.path.join(tmp, 'hevolve.db')
        with patch.dict(os.environ, _clean_env(HEVOLVE_DB_PATH=fake_db), clear=True):
            cands = social_secret_key_candidates()
            top = os.path.normpath(cands[0])
            assert top == os.path.normpath(os.path.join(tmp, '.social_secret_key'))
            # The writer must create exactly where the reader looks first.
            assert os.path.normpath(social_secret_key_write_target()) == top


def test_no_junk_candidate_when_db_path_unset():
    from core.platform_paths import social_secret_key_candidates
    with patch.dict(os.environ, _clean_env(), clear=True):
        cands = [os.path.normpath(c) for c in social_secret_key_candidates()]
        # The old jwt_manager probed '../.social_secret_key' from joining ''.
        assert all(not c.endswith(os.path.normpath('../.social_secret_key'))
                   or os.path.isabs(c) for c in cands)
        # Every candidate is a real .social_secret_key leaf.
        assert cands, "must always offer at least the data-dir + agent_data fallbacks"
        for c in cands:
            assert os.path.basename(c) == '.social_secret_key'


def test_write_target_is_always_a_reader_candidate():
    """Whatever location the writer picks, the reader's scan includes it."""
    from core.platform_paths import (
        social_secret_key_candidates, social_secret_key_write_target)
    scenarios = [
        _clean_env(),                       # plain source tree
        _clean_env(NUNBA_BUNDLED='1'),      # bundled desktop
    ]
    with tempfile.TemporaryDirectory() as tmp:
        scenarios.append(_clean_env(HEVOLVE_DB_PATH=os.path.join(tmp, 'x.db')))
        for env in scenarios:
            with patch.dict(os.environ, env, clear=True):
                target = os.path.normpath(social_secret_key_write_target())
                cands = [os.path.normpath(c) for c in social_secret_key_candidates()]
                assert target in cands, (target, cands, env.get('NUNBA_BUNDLED'),
                                         env.get('HEVOLVE_DB_PATH'))


def test_roundtrip_auth_writes_jwt_reads_same_key():
    """The anti-drift invariant: auth creates a key, jwt_manager reads it back."""
    from integrations.social.auth import _load_or_create_secret_key
    with tempfile.TemporaryDirectory() as tmp:
        fake_db = os.path.join(tmp, 'hevolve.db')
        with patch.dict(os.environ, _clean_env(HEVOLVE_DB_PATH=fake_db), clear=True):
            written = _load_or_create_secret_key()
            assert len(written) >= 32
            # File landed next to the DB (preserved behaviour).
            assert os.path.exists(os.path.join(tmp, '.social_secret_key'))

            # jwt_manager's reader, with no env key, must discover the SAME file.
            from core.platform_paths import read_social_secret_key
            assert read_social_secret_key() == written

            # And a fresh JWTManager picks it up (no SOCIAL_SECRET_KEY in env).
            from security.jwt_manager import JWTManager
            mgr = JWTManager()
            assert mgr._secret_key == written


def test_reader_returns_empty_when_no_key_anywhere():
    import core.platform_paths as pp
    with tempfile.TemporaryDirectory() as tmp:
        empty = os.path.join(tmp, 'data')
        os.makedirs(empty, exist_ok=True)
        # get_data_dir caches its result, so NUNBA_DATA_DIR won't take effect
        # mid-session — patch get_db_dir directly to a guaranteed-empty dir.
        # HEVOLVE_DB_PATH points into the same empty tree; CWD is the empty dir
        # so the relative agent_data candidate also resolves to nothing.
        with patch.dict(os.environ, _clean_env(
                HEVOLVE_DB_PATH=os.path.join(empty, 'none.db')), clear=True), \
                patch.object(pp, 'get_db_dir', return_value=empty):
            cwd = os.getcwd()
            try:
                os.chdir(tmp)   # ./agent_data does not exist under tmp
                assert pp.read_social_secret_key() == ''
            finally:
                os.chdir(cwd)
