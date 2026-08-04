"""Site pages: draft -> in_review -> published, driven against real sqlite.

The service is the unit under test, the way thought_experiment_service is
tested: routes stay thin, the service takes a db session, and here that
session is an in-memory sqlite with the real models. No auth mocking needed
because auth lives in the route decorators, not the service.

    python -m pytest tests/unit/test_site_pages.py --noconftest -q
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from integrations.social.models import Base, SitePage, User  # noqa: F401 — model registration
# Imported from the FACADE, never from _models_local. The fallback module
# re-declares `users`, so importing it directly while Hevolve_Database is
# installed raises InvalidRequestError at COLLECTION — which is exactly how
# this defect was found (it aborted the repo-wide coverage sweep).
from integrations.social.api_pages import PagesService


@pytest.fixture()
def db():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(User(id='admin-1', username='admin', is_admin=True,
                     user_type='human'))
    session.commit()
    yield session
    session.close()


def test_upsert_creates_a_draft(db):
    page = PagesService.upsert(db, 'first-post', 'admin-1',
                               title='First Post', content='# hi')
    assert page['status'] == 'draft'
    assert page['slug'] == 'first-post'
    assert page['published_at'] is None


def test_slug_is_normalised_and_validated(db):
    page = PagesService.upsert(db, '  My-Slug ', 'admin-1', title='T')
    assert page['slug'] == 'my-slug'
    with pytest.raises(ValueError):
        PagesService.upsert(db, 'bad slug!', 'admin-1', title='T')
    with pytest.raises(ValueError):
        PagesService.upsert(db, '', 'admin-1', title='T')


def test_title_required_on_create(db):
    with pytest.raises(ValueError):
        PagesService.upsert(db, 'no-title', 'admin-1', content='body only')


def test_draft_is_invisible_until_published(db):
    PagesService.upsert(db, 'hidden', 'admin-1', title='Hidden')
    assert PagesService.get(db, 'hidden') is None
    assert PagesService.get(db, 'hidden', include_unpublished=True) is not None
    assert PagesService.list(db, status='published') == []


def test_publish_flow_sets_published_at_once(db):
    PagesService.upsert(db, 'story', 'admin-1', title='Story', content='x')
    moved = PagesService.set_status(db, 'story', 'in_review')
    assert moved['status'] == 'in_review'
    published = PagesService.set_status(db, 'story', 'published')
    assert published['published_at'] is not None
    first_publish = published['published_at']

    # unpublish then republish: the original date is kept, not rewritten
    PagesService.set_status(db, 'story', 'draft')
    again = PagesService.set_status(db, 'story', 'published')
    assert again['published_at'] == first_publish

    assert PagesService.get(db, 'story')['content'] == 'x'
    listed = PagesService.list(db)
    assert [p['slug'] for p in listed] == ['story']
    assert 'content' not in listed[0]


def test_update_keeps_state_and_changes_content(db):
    PagesService.upsert(db, 'evolving', 'admin-1', title='V1', content='one')
    PagesService.set_status(db, 'evolving', 'published')
    updated = PagesService.upsert(db, 'evolving', 'admin-1', content='two')
    assert updated['status'] == 'published'
    assert PagesService.get(db, 'evolving')['content'] == 'two'
    assert updated['title'] == 'V1'


def test_invalid_status_rejected_and_unknown_slug_is_none(db):
    PagesService.upsert(db, 'p', 'admin-1', title='P')
    with pytest.raises(ValueError):
        PagesService.set_status(db, 'p', 'live')
    assert PagesService.set_status(db, 'ghost', 'published') is None
    assert PagesService.get(db, 'ghost') is None
