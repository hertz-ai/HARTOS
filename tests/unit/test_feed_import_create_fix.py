"""Behavioural test for the feed_import inbound-ingestion fix (task #62,
2026-05-30).

ROOT: FeedImporter.import_items called PostService.create_post(author_id=...,
post_type=..., tags=..., community_id=...) — a method that DOES NOT EXIST on
PostService (the real API is .create(db, author, title, content, ...)). Every
feed item therefore raised AttributeError, rolled back, and logged an error —
so RSS/Atom/JSON feeds never produced a single Nunba post (one of the two dead
inbound-ingestion paths in the omni-channel bridge map).

This test pins that import_items now drives the REAL PostService.create with
the author User object (not an id), title + body separate, the link-aware
content_type, and the canonical source_message_id dedup stamp.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.social.feed_import import FeedImporter, FeedItem  # noqa: E402
# NOTE: these are aliased classes — `User` is really `SocialUser`, `Post` is
# `SocialPost` — so the mock must match by class IDENTITY, not __name__.
from integrations.social.models import User as _User, Post as _Post  # noqa: E402


def _db_with_author(author):
    """DB stub: User lookup → author; Post (dedup) / Community lookup → None."""
    def _query(model):
        q = MagicMock()
        q.filter.return_value.first.return_value = (
            author if model is _User else None)
        return q
    db = MagicMock()
    db.query.side_effect = _query
    return db


def test_import_items_calls_real_create_not_phantom_create_post():
    item = FeedItem(
        id='guid-1', title='Local-first agents', content='Body of the item.',
        link='https://example.com/post', source_feed='https://example.com/feed.xml',
        media_urls=['https://example.com/a.png'])
    author = MagicMock()
    author.id = 'user-1'
    importer = FeedImporter(_db_with_author(author))

    fake_post = MagicMock()
    fake_post.id = 'post-123'

    # PostService.create_post must NOT exist / must NOT be used; we assert the
    # real .create is what gets driven.
    with patch('integrations.social.services.PostService.create',
               return_value=fake_post) as mk_create:
        created = importer.import_items([item], user_id='user-1')

    assert created == ['post-123'], "the created post id must be collected"
    assert mk_create.called, "import_items must call the REAL PostService.create"

    args, kwargs = mk_create.call_args
    # create(db, author, title, body, ...) — author is the User OBJECT, not an id
    assert args[1] is author, "author must be the User object, not author_id"
    assert args[2] == 'Local-first agents', "title is its own positional arg"
    assert 'Body of the item.' in args[3], "body content is passed"
    # link-aware content_type + canonical dedup stamp
    assert kwargs.get('content_type') == 'link'
    assert kwargs.get('link_url') == 'https://example.com/post'
    assert kwargs.get('source_message_id') == item.content_hash
    assert kwargs.get('source_channel', '').startswith('feed:')


def test_import_items_skips_when_author_missing():
    """If the attributed user doesn't exist, skip the item (don't crash, don't
    pass a None author into create)."""
    item = FeedItem(id='g2', title='T', content='C', link='', source_feed='')
    importer = FeedImporter(_db_with_author(None))  # User lookup → None
    with patch('integrations.social.services.PostService.create') as mk_create:
        created = importer.import_items([item], user_id='ghost')
    assert created == []
    assert not mk_create.called, "must not call create with a missing author"


def test_import_items_dedup_skips_existing():
    """A feed item whose content_hash already exists as a post is skipped."""
    item = FeedItem(id='g3', title='Dup', content='C', link='', source_feed='')
    author = MagicMock()
    author.id = 'user-1'

    def _query(model):
        q = MagicMock()
        if model is _Post:
            q.filter.return_value.first.return_value = MagicMock()  # existing dup
        elif model is _User:
            q.filter.return_value.first.return_value = author
        else:
            q.filter.return_value.first.return_value = None
        return q
    db = MagicMock()
    db.query.side_effect = _query
    importer = FeedImporter(db)

    with patch('integrations.social.services.PostService.create') as mk_create:
        created = importer.import_items([item], user_id='user-1')
    assert created == []
    assert not mk_create.called, "duplicate item must not be re-created"
