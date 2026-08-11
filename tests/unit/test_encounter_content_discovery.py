"""Content-based people discovery.

get_suggestions ranks by ENCOUNTERS and needs >= 2 co-occurrences, so a user
who has only read things they liked is shown nobody, and a brand-new account
is shown nobody at all. Search does not fix that: it asks a newcomer to
already know who they are looking for. suggest_by_content fills the gap from
what they engaged with, then from what is widely read.

Every query here is best-effort: discovery is a nice-to-have surface and must
never take down the endpoint that renders it.
"""
from unittest.mock import MagicMock

from integrations.social.encounter_service import EncounterService


def _db_that_raises():
    db = MagicMock()
    db.query.side_effect = RuntimeError('database on fire')
    return db


def test_already_connected_always_excludes_self():
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    assert 'me' in EncounterService._already_connected(db, 'me')


def test_already_connected_excludes_people_already_followed():
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [('alice',), ('bob',)]
    out = EncounterService._already_connected(db, 'me')
    assert {'me', 'alice', 'bob'} <= out


def test_already_connected_survives_a_broken_follow_table():
    """A failed lookup must not mean 'suggest everyone including yourself'."""
    out = EncounterService._already_connected(_db_that_raises(), 'me')
    assert out == {'me'}


def test_engaged_content_authors_survives_query_failure():
    out = EncounterService._authors_of_engaged_content(
        _db_that_raises(), 'me', set(), 5)
    assert out == []


def test_authors_worth_reading_survives_query_failure():
    assert EncounterService._authors_worth_reading(_db_that_raises(), set(), 5) == []


def test_suggest_by_content_never_raises_into_the_caller():
    """The endpoint behind this renders a page; a discovery miss is an empty
    list, not a 500."""
    assert EncounterService.suggest_by_content(_db_that_raises(), 'me', limit=5) == []


def test_hydrate_marks_why_someone_was_suggested():
    """The reason is shown to the user, so it has to survive hydration."""
    user = MagicMock()
    user.id, user.username = 'u1', 'alice'
    user.display_name, user.avatar_url, user.user_type = 'Alice', '', 'human'
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = user
    out = EncounterService._hydrate(db, ['u1'], 'you engaged with their posts')
    assert out[0]['username'] == 'alice'
    assert out[0]['reason'] == 'you engaged with their posts'


def test_hydrate_skips_ids_with_no_user_row():
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    assert EncounterService._hydrate(db, ['ghost'], 'whatever') == []
