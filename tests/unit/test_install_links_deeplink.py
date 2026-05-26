"""Unit tests for ``core.install_links`` deep-link helpers (UNIF-G4).

Verifies the custom-scheme validator + URL builders.  Pure shape /
contract — no DB, no network.
"""
from __future__ import annotations

import unittest


class IsAllowedDeeplinkUriTest(unittest.TestCase):

    def test_accepts_canonical_invite_uri(self):
        from core.install_links import is_allowed_deeplink_uri
        self.assertTrue(is_allowed_deeplink_uri('hevolveai://invite/abc123'))
        self.assertTrue(is_allowed_deeplink_uri('nunba://invite/abc123'))

    def test_accepts_meet_with_platform_and_room(self):
        from core.install_links import is_allowed_deeplink_uri
        self.assertTrue(is_allowed_deeplink_uri(
            'hevolveai://meet/discord/voice-room-7'))
        self.assertTrue(is_allowed_deeplink_uri(
            'nunba://meet/teams/abc-def-ghi'))

    def test_accepts_group_with_platform_and_id(self):
        from core.install_links import is_allowed_deeplink_uri
        self.assertTrue(is_allowed_deeplink_uri(
            'hevolveai://group/whatsapp/120363145'))

    def test_rejects_https_scheme(self):
        # HTTPS URIs go through is_allowed_install_link, NOT this helper.
        from core.install_links import is_allowed_deeplink_uri
        self.assertFalse(is_allowed_deeplink_uri(
            'https://hevolve.ai/invite/abc'))

    def test_rejects_unknown_scheme(self):
        from core.install_links import is_allowed_deeplink_uri
        self.assertFalse(is_allowed_deeplink_uri(
            'evil://invite/abc'))
        self.assertFalse(is_allowed_deeplink_uri(
            'javascript://invite/abc'))

    def test_rejects_unknown_verb(self):
        from core.install_links import is_allowed_deeplink_uri
        self.assertFalse(is_allowed_deeplink_uri(
            'hevolveai://login/abc'))
        self.assertFalse(is_allowed_deeplink_uri(
            'nunba://exec/rm-rf'))

    def test_rejects_missing_trailing_segment(self):
        from core.install_links import is_allowed_deeplink_uri
        self.assertFalse(is_allowed_deeplink_uri('hevolveai://invite'))
        self.assertFalse(is_allowed_deeplink_uri('hevolveai://invite/'))
        # meet needs platform + room — only platform isn't enough
        self.assertFalse(is_allowed_deeplink_uri('hevolveai://meet/discord'))

    def test_rejects_empty_or_garbage(self):
        from core.install_links import is_allowed_deeplink_uri
        self.assertFalse(is_allowed_deeplink_uri(''))
        self.assertFalse(is_allowed_deeplink_uri(None))
        self.assertFalse(is_allowed_deeplink_uri(123))


class BuilderHelpersTest(unittest.TestCase):

    def test_invite_link_default_scheme(self):
        from core.install_links import invite_link
        self.assertEqual(invite_link('abc123'),
                         'hevolveai://invite/abc123')

    def test_invite_link_nunba_scheme(self):
        from core.install_links import invite_link
        self.assertEqual(invite_link('abc', scheme='nunba'),
                         'nunba://invite/abc')

    def test_invite_link_rejects_empty_code(self):
        from core.install_links import invite_link
        with self.assertRaises(ValueError):
            invite_link('')

    def test_invite_link_rejects_unknown_scheme(self):
        from core.install_links import invite_link
        with self.assertRaises(ValueError):
            invite_link('abc', scheme='evil')

    def test_meet_link(self):
        from core.install_links import meet_link
        self.assertEqual(meet_link('Discord', 'room-7'),
                         'hevolveai://meet/discord/room-7')
        # Platform name lowercased
        self.assertEqual(meet_link('TEAMS', 'abc'),
                         'hevolveai://meet/teams/abc')

    def test_meet_link_rejects_missing_args(self):
        from core.install_links import meet_link
        with self.assertRaises(ValueError):
            meet_link('', 'room-7')
        with self.assertRaises(ValueError):
            meet_link('discord', '')

    def test_group_link(self):
        from core.install_links import group_link
        self.assertEqual(group_link('whatsapp', '120363'),
                         'hevolveai://group/whatsapp/120363')

    def test_round_trip_builder_validator(self):
        # Anything we build must pass our own allowlist.
        from core.install_links import (
            group_link, invite_link, is_allowed_deeplink_uri, meet_link,
        )
        self.assertTrue(is_allowed_deeplink_uri(invite_link('x')))
        self.assertTrue(is_allowed_deeplink_uri(meet_link('discord', 'r1')))
        self.assertTrue(is_allowed_deeplink_uri(group_link('slack', 'C1')))


if __name__ == '__main__':
    unittest.main()
