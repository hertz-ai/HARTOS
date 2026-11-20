"""The marketing agent's create_social_post tool posts through PostService.

Live on central 2026-11-20, McGroce goal 09bbc67a paused needs_help at
action 15 ("create_social_post: create platform post for shoppers explaining
how McGroce works"): every call answered {"success": false, "error":
"'media_url' is an invalid keyword argument for Post"}.  The tool built a
Post row by hand with media_url=, a column the model does not have (it keeps
media in media_urls, a JSON list), and passed it even when no media was
given, so no call could ever succeed.  POST /posts makes a post through
PostService.create; the tool now does the same, with that service's own
arguments.

PostService.create takes the author's users row.  The goal's turn ran as
'system_daemon', which has none: measured on a temp SQLite file with the
real models, UserService.get_by_id('system_daemon') is None and the service
cannot make the post; a hand-built row is stored only with foreign keys off
and fails the posts.author_id foreign key with them on.  The tool then
refuses (core.tool_refusal), naming the account and what the owner can do;
on a goal turn the step may finish without the post.

These tests call the real tool against the real service on a SQLite file
and read the rows back.
"""
import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture(scope='module')
def session_factory(tmp_path_factory):
    from integrations.social.models import Base
    path = tmp_path_factory.mktemp('posts') / 'social.db'
    engine = create_engine(f'sqlite:///{path}')
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def social_db(session_factory, monkeypatch):
    """The tool imports get_db from models at call time."""
    import integrations.social.models as models
    monkeypatch.setattr(models, 'get_db', session_factory)
    return session_factory


class _Agents:
    """Stands in for the autogen agents the tool family registers on."""

    def __init__(self):
        self.tools = {}

    def register_for_llm(self, name, description):
        def keep(func):
            self.tools[name] = func
            return func
        return keep

    def register_for_execution(self, name):
        return lambda func: func


def _post_reply(author_id, *args, **kwargs):
    """The tool's reply text, as the agent reads it."""
    from integrations.agent_engine.marketing_tools import register_marketing_tools
    agents = _Agents()
    register_marketing_tools(agents, agents, author_id)
    return agents.tools['create_social_post'](*args, **kwargs)


def _make_person(session_factory):
    from integrations.social.models import User
    db = session_factory()
    try:
        person = User(username=f'poster_{uuid.uuid4().hex[:10]}',
                      user_type='human')
        db.add(person)
        db.commit()
        return person.id
    finally:
        db.close()


def _posts_by(session_factory, author_id):
    """(posts, the author's post_count) stored for ``author_id``."""
    from integrations.social.models import Post, User
    db = session_factory()
    try:
        posts = db.query(Post).filter_by(author_id=author_id).all()
        author = db.query(User).filter_by(id=author_id).first()
        return posts, (author.post_count if author else None)
    finally:
        db.close()


def test_a_post_without_media_is_made_by_the_service(social_db):
    person = _make_person(social_db)
    out = json.loads(_post_reply(person, 'How McGroce works',
                                 'Order, we pick, it comes to you.'))
    assert out['success'] is True
    posts, post_count = _posts_by(social_db, person)
    assert [p.id for p in posts] == [out['post']['id']]
    post = posts[0]
    assert (post.title, post.content) == ('How McGroce works',
                                          'Order, we pick, it comes to you.')
    assert post.media_urls == [] and post.content_type == 'text'
    # The service counted it on the author, as POST /posts does.
    assert post_count == 1


def test_a_post_with_media_keeps_it_where_the_service_keeps_media(social_db):
    person = _make_person(social_db)
    url = 'https://shop.example/mcgroce/how-it-works.png'
    out = json.loads(_post_reply(person, 'How McGroce works', 'See the picture',
                                 media_url=url))
    assert out['success'] is True
    assert out['post']['media_urls'] == [url]
    posts, _ = _posts_by(social_db, person)
    assert len(posts) == 1
    assert posts[0].media_urls == [url] and posts[0].content_type == 'media'


def test_a_community_given_by_its_id_or_its_name_gets_the_post(social_db):
    from integrations.social.models import Community
    person = _make_person(social_db)
    name = f'shoppers_{uuid.uuid4().hex[:8]}'
    db = social_db()
    try:
        community = Community(name=name, display_name='Shoppers',
                              creator_id=person)
        db.add(community)
        db.commit()
        community_id = community.id
    finally:
        db.close()
    by_id = json.loads(_post_reply(person, 'One', 'x', community_id=community_id))
    by_name = json.loads(_post_reply(person, 'Two', 'y', community_id=name))
    assert by_id['post']['community_id'] == community_id
    assert by_name['post']['community_id'] == community_id


def test_the_system_account_with_no_users_row_is_refused_and_may_finish(
        social_db):
    """09bbc67a: the goal's turn ran as 'system_daemon'."""
    from core.constants import tool_reply_failed
    from core.tool_refusal import (RETRY_FINISH_WITHOUT, refusal_has_hint,
                                   refusal_lets_step_finish)
    reply = _post_reply('system_daemon', 'How McGroce works', 'body',
                        media_url='https://shop.example/a.png')
    out = json.loads(reply)
    assert out['success'] is False and out['retry'] == RETRY_FINISH_WITHOUT
    note = out['note']
    assert "'system_daemon'" in note and 'no account' in note
    assert 'owner' in note and 'without the post' in note
    # One option the owner may choose (task #301), named, not wired in.
    assert "'nunba'" in note and 'ensure_system_user' in note
    assert 'media_url' not in out.get('error', '')
    assert refusal_has_hint(reply) and tool_reply_failed(reply)
    assert refusal_lets_step_finish(reply, 'create_social_post') is True
    assert _posts_by(social_db, 'system_daemon') == ([], None)


_DEMO_PATH = (r'C:\Users\someone\Documents\Nunba\data\demo_recordings'
              r'\demo_1700000000.mp4')


def _assert_file_refused(reply, session_factory, person):
    """A file on this node never goes on a platform post: refused with what
    to pass instead, the agent may retry with that, nothing stored."""
    from core.constants import tool_reply_failed
    from core.tool_refusal import (RETRY_AFTER_FIX, refusal_has_hint,
                                   refusal_lets_step_finish)
    out = json.loads(reply)
    assert out['success'] is False and out['retry'] == RETRY_AFTER_FIX
    note = out['note']
    assert 'media_url' in note and 'https://' in note
    assert refusal_has_hint(reply) and tool_reply_failed(reply)
    assert refusal_lets_step_finish(reply, 'create_social_post') is False
    assert _posts_by(session_factory, person) == ([], 0)


def test_a_demo_recording_path_is_refused_with_what_to_pass_instead(social_db):
    """Review of b7e41e7: record_demo_video's path, given as media_url, was
    stored and sent to central and followers as a public post."""
    person = _make_person(social_db)
    reply = _post_reply(person, 'Demo', 'Watch this', media_url=_DEMO_PATH)
    _assert_file_refused(reply, social_db, person)


def test_a_file_this_node_recorded_is_refused_too(social_db):
    """A file under the app's data folder is one post_to_channel can send,
    but a platform post keeps a link, and nobody else can open this one."""
    import os
    from core.platform_paths import get_data_dir
    folder = os.path.join(get_data_dir(), 'demos')
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f'demo_{uuid.uuid4().hex[:8]}.mp4')
    with open(path, 'wb') as f:
        f.write(b'\x00\x00\x00\x18ftypmp42')
    try:
        person = _make_person(social_db)
        reply = _post_reply(person, 'Demo', 'Watch this', media_url=path)
        _assert_file_refused(reply, social_db, person)
        assert 'post_to_channel' in json.loads(reply)['note']
    finally:
        os.remove(path)


def test_a_link_that_names_no_file_type_goes_on_the_post_as_its_link(social_db):
    """The rule post_to_channel keeps (_media_for): a URL whose name says no
    file type is a link, not an attachment."""
    person = _make_person(social_db)
    url = 'https://shop.example/mcgroce/how-it-works'
    out = json.loads(_post_reply(person, 'How McGroce works', 'Read this',
                                 media_url=url))
    assert out['success'] is True
    assert out['post']['link_url'] == url and out['post']['media_urls'] == []


@pytest.mark.parametrize('given', ['McGroce Shoppers', 'no_such_place'])
def test_a_community_that_is_not_found_is_named_in_the_reply(social_db, given):
    """POST /posts also posts with no community then; the tool says so."""
    person = _make_person(social_db)
    out = json.loads(_post_reply(person, 'Title', 'body', community_id=given))
    assert out['success'] is True and out['post']['community_id'] is None
    assert out['community_not_found'] == given
    note = out['note']
    assert repr(given) in note and 'no community' in note
    assert 'do not post it again' in note


def test_on_a_persons_turn_the_same_refusal_says_to_tell_them(social_db,
                                                             monkeypatch):
    import core.agent_tools as agent_tools
    from core.tool_refusal import RETRY_NEVER, refusal_lets_step_finish
    monkeypatch.setattr(agent_tools, 'is_a_persons_request', lambda _id: True)
    reply = _post_reply('nobody_here', 'Title', 'body')
    out = json.loads(reply)
    assert out['success'] is False and out['retry'] == RETRY_NEVER
    assert "'nobody_here'" in out['note'] and 'tell the person' in out['note']
    assert refusal_lets_step_finish(reply, 'create_social_post') is False
