"""A node that silently starts no gossip must not look like a healthy one.

init_social's gossip block logged INFO on success and WARNING on failure, but
NOTHING on skip. So a node where _boot_verified came out False started no
gossip, no LAN auto-discovery and no integrity round -- and said nothing at any
level. From outside, "gossip running fine" and "gossip never started" were
indistinguishable, because the hevolve_social logger runs at WARNING in
production and the success line is INFO.

Measured on central 2026-09-01 (langchain_gpt:12db230c): the retention sweep
removed 0 rows across ~24 minutes of a fresh container with 135,869 rows still
eligible, and the logs contained ZERO lines of any kind -- no sweep failure, no
CRITICAL, no gossip line either way, and every env knob at its default. Every
visible branch was eliminated, which left the branch that is invisible by
construction.

Runs standalone (`python tests/unit/test_gossip_skip_is_visible.py`).
"""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'


def _init_social_source():
    from integrations import social
    return inspect.getsource(social.init_social)


class GossipSkipVisibilityTest(unittest.TestCase):

    def test_skip_path_logs_at_warning(self):
        src = _init_social_source()
        self.assertIn('if not _boot_verified:', src,
                      'the gossip skip has no branch of its own to log from')
        idx = src.index('if not _boot_verified:')
        window = src[idx:idx + 700]
        self.assertIn('logger.warning', window,
                      'a node that starts no gossip must say so at WARNING')

    def test_skip_message_names_what_is_disabled(self):
        """An operator reading it should not have to guess the blast radius."""
        src = _init_social_source()
        idx = src.index('if not _boot_verified:')
        window = src[idx:idx + 700].lower()
        for term in ('gossip', 'discovery', 'integrity'):
            self.assertIn(term, window,
                          'skip warning should name ' + term + ' as disabled')

    def test_skip_precedes_the_start_block(self):
        """The warning must fire on the same pass, not after an early return."""
        # Match CODE lines only: an existing comment at the top of the block
        # quotes "gossip.start()" in prose, so a plain str.index finds that
        # first and the assertion becomes meaningless.
        lines = [l.strip() for l in _init_social_source().splitlines()]
        skip = next(i for i, l in enumerate(lines)
                    if l.startswith('if not _boot_verified:'))
        start = next(i for i, l in enumerate(lines)
                     if l == 'gossip.start()')
        self.assertLess(skip, start)

    def test_success_and_failure_paths_still_exist(self):
        """Do not trade one blind spot for another."""
        src = _init_social_source()
        self.assertIn('gossip.start()', src)
        self.assertIn('gossip start FAILED', src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
