"""What an agent wants the person to SEE reaches them, and only when it exists.

The owner, 2026-10-25: "if there is an image url to display in frontend and
agent want to set it in response to show to user ... it could be text to image
response, it could also be video url ... all modalities we have a way to set in
response and render play or show it".

One existing path, no new field: the agent names media beside its
message2userfinal, and send_message_to_user1 turns each reference into an item
the file store has vouched for, which publish_agent_message already carries as
``media``.

The guard these tests exist for: THE MODEL INVENTS THINGS. Measured live on
2026-10-25 (request_id t1-retrieval-102136) the agent answered from "The Art of
Computer Programming", a book in no row of pdf_files and no agent-data file. A
model that invents a book will invent a URL, and an invented URL must never
reach a client as a broken frame.
"""
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.peer_link.crossbar_publish import publish_agent_message  # noqa: E402
from core.user_file_store import OUTBOUND_PURPOSE  # noqa: E402
from hartos.helper import MEDIA_REPLY_KEY, media_items_for  # noqa: E402

JPEG = b'\xff\xd8jpegbytes'


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    """A real uploads directory, as resolve_upload_url resolves against."""
    root = tmp_path / 'uploads'
    (root / 'pdf_parse' / '10').mkdir(parents=True)
    monkeypatch.setattr('core.platform_paths.get_uploads_dir', lambda: str(root))
    return root


@pytest.fixture
def offered(monkeypatch):
    """Record what is handed to the store, and answer exactly as it does.

    Mirrored from user_file_store.offer_to_device, because a fake kinder than
    the real store tests nothing: it DEEP-COPIES meta (:1340, so it cannot
    mutate the caller's dict), writes its own facts into meta['media']
    (:1361), sets sha256 to the same digest as xid, and returns them on the
    StoredFile -- which is why reel_studio's _deliver ends
    ``return stored.meta['media']``.
    """
    calls = []

    def fake_offer(user_id, path, *, mime, purpose, prompt_id, request_id,
                   meta=None, name=None):
        if purpose != OUTBOUND_PURPOSE:                 # the real store raises
            raise ValueError('outbound purpose must be %r' % (OUTBOUND_PURPOSE,))
        # Recorded BEFORE the work, so `offered == []` means "the store was
        # never ASKED" and not merely "no ask of it succeeded" -- a spy that
        # records only successes cannot tell a guard that held from a call
        # that failed once it got there.
        call = {'path': str(path), 'mime': mime, 'purpose': purpose, 'name': name}
        calls.append(call)
        meta = json.loads(json.dumps(dict(meta or {})))
        data = Path(path).read_bytes()
        xid = hashlib.sha256(data).hexdigest()
        meta['media'].update(xid=xid, sha256=xid, size=len(data), mime=mime,
                             prompt_id=prompt_id, request_id=request_id)
        call['item'] = meta['media']
        return SimpleNamespace(xid=xid, meta=meta)

    monkeypatch.setattr('core.user_file_store.UserFileStore.offer_to_device',
                        staticmethod(fake_offer), raising=True)
    return calls


def _template_example():
    """The JSON object the reply schema SHOWS the agent, parsed.

    Reading the example as data is what binds the prompt to the code. The
    review of 19836e605 measured three mutants surviving the substring
    version with 24/24 green: a literal 'media_items' key, gif|svg kinds,
    and a "make one up" instruction.
    """
    import hartos.reuse_recipe as reuse
    text = reuse.response_format_template()
    start = text.index('{')
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise AssertionError('no complete JSON object in the template: %r' % text)


def _page(uploads, name='page_2.jpg', data=JPEG):
    path = uploads / 'pdf_parse' / '10' / name
    path.write_bytes(data)
    return '/uploads/pdf_parse/10/' + name


def test_the_two_mime_tables_agree_where_they_overlap():
    """MEDIA_KIND_MIMES is not the tree's only extension-to-MIME table.

    The [[MEDIA:]] marker has _MEDIA_MIME (integrations/channels/registry
    .py:89) for SENDING a file out through a channel. Two tables is a
    decision, not drift: a channel can post a .pdf, .mp3 or .ogg that no
    chat bubble renders, and a reply can show .html and .webm that the
    channel senders do not take. What must never differ is MEANING -- an
    extension both know has to be the same type in both, or one file is
    audio when sent and nothing when shown.
    """
    from integrations.channels.registry import _MEDIA_MIME
    from hartos.helper import MEDIA_KIND_MIMES
    mine = {ext: mime for kind in MEDIA_KIND_MIMES.values()
            for ext, mime in kind.items()}

    shared = set(mine) & set(_MEDIA_MIME)
    assert shared, 'the tables no longer overlap; this guard has gone blind'
    assert {e: mine[e] for e in shared} == {e: _MEDIA_MIME[e] for e in shared}


def test_create_hands_media_wherever_it_hands_the_agents_own_answer():
    """A8/A9: mutants dropping media_refs at create_recipe's two call sites
    passed every test, because nothing here drives create's loop.

    Structural, and weaker than driving the path -- it checks the calls,
    not the delivery. Saying so plainly is better than leaving the sites
    unpinned or implying more than it proves. The invariant it does hold
    exactly: wherever CREATE hands the agent's OWN parsed answer to the
    user, it hands the media that agent named along with it. Calls that
    pass raw message content, not json_obj, are correctly untouched.
    """
    import ast
    import inspect
    import hartos.create_recipe as create

    sites = [
        node for node in ast.walk(ast.parse(inspect.getsource(create)))
        if isinstance(node, ast.Call)
        and getattr(node.func, 'id', None) == 'send_message_to_user1'
        and any(isinstance(a, ast.Subscript)
                and getattr(a.value, 'id', None) == 'json_obj'
                for a in node.args)
    ]

    assert len(sites) >= 2, f'expected 2+ json_obj answer sites, found {len(sites)}'
    for call in sites:
        assert 'media_refs' in {k.arg for k in call.keywords}, (
            f'create_recipe.py:{call.lineno} answers from the agent JSON but '
            f'does not pass the media the agent named')


class TestAnInventedUrlNeverShips:
    """The model invents; the store only ever sees real files."""

    def test_a_url_with_no_file_behind_it_is_dropped(self, uploads, offered, caplog):
        """t1-retrieval-102136: the agent named a book that does not exist.
        It will as readily name a page image that does not exist."""
        refs = [{'type': 'image', 'url': '/uploads/pdf_parse/10/page_99.jpg'}]

        with caplog.at_level('WARNING'):
            items = media_items_for('u1', refs, prompt_id='54', request_id='r1')

        assert items == []
        assert offered == []            # the store was never asked
        assert any('page_99' in r.getMessage() for r in caplog.records), \
            [r.getMessage() for r in caplog.records]

    def test_a_url_outside_the_uploads_is_dropped(self, uploads, offered):
        """resolve_upload_url refuses anything that is not under uploads; a
        media reference must not be a way around it."""
        for url in ('/etc/passwd', '/uploads/../secrets.txt',
                    'file:///C:/Windows/win.ini', 'https://example.org/x.jpg'):
            assert media_items_for('u1', [{'type': 'image', 'url': url}],
                                   prompt_id='54', request_id='r1') == []
        assert offered == []

    def test_a_real_file_outside_the_uploads_is_still_refused(self, uploads, offered):
        """A11 of hevolve-cc12's review, and the case that actually matters.

        Every traversal url in the test above names a file that does not
        exist, so a mutant replacing resolve_upload_url with a bare
        "does this path exist?" check passed all of them. The property is
        CONTAINMENT, not existence, and only a real file outside the root
        can tell the two apart.
        """
        outside = uploads.parent / 'secret.jpg'
        outside.write_bytes(JPEG)
        assert outside.is_file(), 'the escape target must really exist'

        for url in ('/uploads/../secret.jpg',
                    '/uploads/pdf_parse/../../secret.jpg',
                    '/uploads/pdf_parse/10/../../../secret.jpg'):
            assert media_items_for('u1', [{'type': 'image', 'url': url}],
                                   prompt_id='54', request_id='r1') == [], url
        assert offered == []

    def test_a_kind_the_file_contradicts_is_dropped(self, uploads, offered, caplog):
        """An agent calling an .mp4 an image is guessing, and the next thing it
        guesses is the path. The file decides; nothing is relabelled."""
        url = _page(uploads, name='clip.mp4', data=b'\x00\x00\x00 ftypmp42')

        with caplog.at_level('WARNING'):
            items = media_items_for('u1', [{'type': 'image', 'url': url}],
                                    prompt_id='54', request_id='r1')

        assert items == [] and offered == []
        assert any('not a image' in r.getMessage() for r in caplog.records)

    def test_one_bad_reference_does_not_lose_the_good_ones(self, uploads, offered):
        real = _page(uploads)
        refs = [{'type': 'image', 'url': '/uploads/pdf_parse/10/ghost.jpg'},
                {'type': 'image', 'url': real}]

        items = media_items_for('u1', refs, prompt_id='54', request_id='r1')

        assert len(items) == 1
        assert items[0]['xid'] == hashlib.sha256(JPEG).hexdigest()


class TestTheStoreVouchesForEveryItem:

    def test_an_image_becomes_an_item_the_store_filled_in(self, uploads, offered):
        url = _page(uploads)

        items = media_items_for('u1', [{'type': 'image', 'url': url}],
                                prompt_id='54', request_id='r1')

        assert len(items) == 1
        item = items[0]
        # what the caller alone knows
        assert item['kind'] == 'image'
        # and what only the store may say
        assert item['xid'] == hashlib.sha256(JPEG).hexdigest()
        assert item['sha256'] == item['xid']    # validate_media_item requires it
        assert item['mime'] == 'image/jpeg'
        assert item['size'] == len(JPEG)
        assert item['prompt_id'] == '54' and item['request_id'] == 'r1'
        assert offered[0]['purpose'] == OUTBOUND_PURPOSE
        assert offered[0]['path'] == str(uploads / 'pdf_parse' / '10' / 'page_2.jpg')

    def test_every_item_carries_both_addresses(self, uploads, offered):
        """The desktop renders ``url`` (same-origin from this Flask); the phone
        fetches ``xid`` over the device link, which works through the relay
        where a LAN URL does not. Traced in both clients 2026-10-25."""
        url = _page(uploads)

        item = media_items_for('u1', [{'type': 'image', 'url': url}],
                               prompt_id='54', request_id='r1')[0]

        assert item['url'] == url
        assert len(item['xid']) == 64

    @pytest.mark.parametrize('name,kind,mime', [
        ('page_2.jpg', 'image', 'image/jpeg'), ('shot.png', 'image', 'image/png'),
        ('clip.mp4', 'video', 'video/mp4'), ('note.html', 'html', 'text/html'),
    ])
    def test_the_mime_follows_the_file(self, uploads, offered, name, kind, mime):
        url = _page(uploads, name=name)

        items = media_items_for('u1', [{'type': kind, 'url': url}],
                                prompt_id='54', request_id='r1')

        assert items[0]['mime'] == mime and items[0]['kind'] == kind
        assert offered[0]['mime'] == mime       # the store is told the same

    def test_a_caption_rides_when_the_agent_wrote_one(self, uploads, offered):
        url = _page(uploads)

        with_caption = media_items_for(
            'u1', [{'type': 'image', 'url': url, 'caption': '  page 2  '}],
            prompt_id='54', request_id='r1')[0]
        bare = media_items_for('u1', [{'type': 'image', 'url': url}],
                               prompt_id='54', request_id='r1')[0]

        assert with_caption['caption'] == 'page 2'
        assert 'caption' not in bare

    def test_nothing_in_means_nothing_out(self, uploads, offered):
        for refs in (None, [], 'not a list', [{}], [{'type': 'image'}],
                     [{'url': '/uploads/pdf_parse/10/page_2.jpg'}]):
            assert media_items_for('u1', refs, prompt_id='54', request_id='r1') == []
        assert offered == []

    def test_a_store_that_refuses_loses_only_that_item(self, uploads, offered,
                                                       monkeypatch, caplog):
        """A full disk must not cost the person their answer."""
        url = _page(uploads)

        def boom(*a, **k):
            raise OSError(28, 'No space left on device', str(uploads / 'x.jpg'))

        monkeypatch.setattr('core.user_file_store.UserFileStore.offer_to_device',
                            staticmethod(boom), raising=True)
        with caplog.at_level('WARNING'):
            items = media_items_for('u1', [{'type': 'image', 'url': url}],
                                    prompt_id='54', request_id='r1')

        assert items == []
        said = ' '.join(r.getMessage() for r in caplog.records)
        assert 'No space left on device' in said
        # the operator's log may name the path; nothing here returns it
        assert json.dumps(items) == '[]'


@pytest.fixture()
def rr():
    pytest.importorskip('autogen', reason='autogen not installed')
    from hartos import reuse_recipe
    return reuse_recipe


@pytest.fixture()
def publisher(monkeypatch):
    """hart_intelligence.publish_async, as safe_hartos_attr resolves it."""
    pub = MagicMock(name='publish_async')
    monkeypatch.setitem(sys.modules, 'hart_intelligence',
                        SimpleNamespace(publish_async=pub))
    return pub


@pytest.fixture()
def real_store(tmp_path, monkeypatch):
    """A REAL UserFileStore on a throwaway root, with a real uploads tree.

    M1 needs the real store, not the fake one above: the reply calls
    chat_attachments.take_outbound, and only the real store records the
    prompt_id and request_id it is then asked for. A fake cannot fail a
    wrong binding, which is one of the three mutants the review measured
    surviving. Patching the _STORE singleton makes media_items_for and
    take_outbound share one instance, as they do in production.
    """
    import core.platform_paths as pp
    import core.user_file_store as ufs
    page_dir = tmp_path / 'uploads' / 'pdf_parse' / '10'
    page_dir.mkdir(parents=True)
    (page_dir / 'page_2.jpg').write_bytes(JPEG)
    monkeypatch.setattr(pp, 'get_uploads_dir', lambda: str(tmp_path / 'uploads'))
    monkeypatch.setattr(ufs, '_STORE', ufs.UserFileStore(root=str(tmp_path / 'store')))
    return ufs._STORE


def _drive_reuse_turn(rr, monkeypatch, answer_obj, user, prompt, request_id,
                      then=None):
    """Run the REAL get_agent_response for one turn ending on the agent's
    answer, and return its reply.

    Modelled on the house harness, tests/unit/
    test_reuse_final_answer_is_the_reply.py::_drive_turn, rather than
    invented: fakes only at the boundaries (the autogen agents, the HTTP
    pool, the scheduler), so the function's own path to its answer branch
    is the real one.
    """
    from flask import Flask
    user_prompt = f'{user}_{prompt}'
    action = {'action_id': 1, 'action': 'Show her the page',
              'can_perform_without_user_input': 'no', 'recipe': []}
    monkeypatch.setattr(rr, 'recipes', {user_prompt: {'actions': [action]}})
    monkeypatch.setattr(rr, 'user_tasks', {user_prompt: rr.Action([action])})
    monkeypatch.setattr(rr, 'user_ledgers', {})
    monkeypatch.setattr(rr, 'request_id_list', {user_prompt: request_id})
    monkeypatch.setattr(rr, 'request_id_list_sent_intermediate', {})

    group_chat = SimpleNamespace(messages=[], agents=[])
    manager = SimpleNamespace(_oai_messages={})

    rounds = []

    def initiate_chat(recipient, message=None, **_kw):
        group_chat.messages.append(
            {'role': 'user', 'name': 'ChatInstructor', 'content': message})
        group_chat.messages.append(
            {'role': 'assistant', 'name': 'Assistant',
             'content': json.dumps(answer_obj)})
        # `then` ends the round on the verifier's verdict, so the turn is
        # finalised by the POST-LOOP extractor (site 2) instead of the
        # in-loop branch (site 1). First round only, as the house harness
        # does it.
        if then is not None and not rounds:
            group_chat.messages.append(then)
        rounds.append(message)

    agent = SimpleNamespace(initiate_chat=initiate_chat, name='Assistant')
    with patch.object(rr, 'pooled_post'), \
            patch.object(rr, 'scheduler') as sched, \
            Flask('m1-media').app_context():
        sched.get_job.return_value = None
        return rr.get_agent_response(
            agent, agent, SimpleNamespace(name='helper'), agent, manager,
            group_chat, 'show me page 2', 'user', user, prompt, request_id)


class TestTheMainAnswerDeliversMediaThroughTheReply:
    """M1 of the e0661827a review: nothing drove get_agent_response through
    to the reply, so deleting either call site -- or binding the wrong
    request_id -- left every test green.

    This drives the REAL function and then asks the REAL taker, which is
    what _chat_reply calls (hart_intelligence_entry.py:9024). A wrong
    request_id fails it, because take_replied filters on the turn's own id.
    """

    def test_the_turn_puts_its_media_where_the_reply_takes_it(
            self, real_store, rr, publisher, monkeypatch):
        from core.chat_attachments import take_outbound
        url = '/uploads/pdf_parse/10/page_2.jpg'

        reply = _drive_reuse_turn(
            rr, monkeypatch,
            {'message2userfinal': 'here is page 2',
             MEDIA_REPLY_KEY: [{'type': 'image', 'url': url}]},
            'm1-user', 4242, 'req-m1')

        assert reply == 'here is page 2'
        media = take_outbound('m1-user', 4242, 'req-m1')
        assert [m['url'] for m in media] == [url]
        assert media[0]['xid'] == hashlib.sha256(JPEG).hexdigest()
        assert media[0]['kind'] == 'image'

    def test_a_turn_with_no_media_leaves_the_reply_with_none(
            self, real_store, rr, publisher, monkeypatch):
        from core.chat_attachments import take_outbound

        reply = _drive_reuse_turn(
            rr, monkeypatch, {'message2userfinal': 'just words'},
            'm1-user', 4242, 'req-m1')

        assert reply == 'just words'
        assert take_outbound('m1-user', 4242, 'req-m1') == []

    def test_the_post_loop_answer_delivers_its_media_too(
            self, real_store, rr, publisher, monkeypatch):
        """Site 2, and it was NOT covered until this test existed.

        When the round ends on the verifier's verdict the turn is finalised
        by the post-loop extractor, not the in-loop branch. The mutant that
        deletes THAT call survived all 34 tests while the site-1 mutant
        died -- so "both sites are carried" was a claim one site held up.
        """
        from core.chat_attachments import take_outbound
        url = '/uploads/pdf_parse/10/page_2.jpg'
        verdict = {'role': 'assistant', 'name': 'StatusVerifier',
                   'content': json.dumps({'status': 'completed',
                                          'action_id': 1})}

        reply = _drive_reuse_turn(
            rr, monkeypatch,
            {'message2userfinal': 'here is page 2',
             MEDIA_REPLY_KEY: [{'type': 'image', 'url': url}]},
            'm1-user', 4242, 'req-m1', then=verdict)

        assert reply == 'here is page 2'
        media = take_outbound('m1-user', 4242, 'req-m1')
        assert [m['url'] for m in media] == [url]
        assert media[0]['xid'] == hashlib.sha256(JPEG).hexdigest()

    def test_an_invented_url_reaches_no_reply(
            self, real_store, rr, publisher, monkeypatch):
        """The guard holds on the real path, not only in isolation."""
        from core.chat_attachments import take_outbound

        _drive_reuse_turn(
            rr, monkeypatch,
            {'message2userfinal': 'here',
             MEDIA_REPLY_KEY: [{'type': 'image',
                                'url': '/uploads/pdf_parse/10/ghost.jpg'}]},
            'm1-user', 4242, 'req-m1')

        assert take_outbound('m1-user', 4242, 'req-m1') == []


class TestTheTwoSynthesisSteersDiffer:
    """Only the branch that knows the tools did not run may forbid media.

    e0661827a put "Attach no media here" in _REUSE_SYNTHESIS_ANSWER_SHAPE,
    the text BOTH steers end with -- so it also forbade media on
    _REUSE_SYNTHESIS_STEER, which fires when the tools DID all run. That is
    the one path where an agent's file is grounded, and it was the only
    path the feature exists for.
    """

    def test_the_grounded_steer_says_nothing_about_media_at_all(self):
        """Pinned on the WORD, not on a phrase.

        The first version asserted 'no media' was absent, which a reworded
        prohibition walks straight past -- hevolve-cc12 measured exactly
        that mutant surviving 31/31. Any mention of media in the grounded
        steer is the defect, however it is phrased, because that branch is
        where an agent's file is real.
        """
        import hartos.reuse_recipe as reuse

        assert 'media' not in reuse._REUSE_SYNTHESIS_STEER.lower()

    def test_the_incomplete_steer_does_forbid_media(self):
        """The turn did not finish, so no file in it is vouched for, and a
        picture beside an ungrounded answer is read as evidence for it.

        Also pinned on the word: a correct REWORDING of the prohibition
        must not be failed by this test, which the phrase version did.
        """
        import hartos.reuse_recipe as reuse

        assert 'media' in reuse._REUSE_SYNTHESIS_STEER_INCOMPLETE.lower()

    def test_the_incomplete_steer_claims_only_what_its_gate_knows(self):
        """It fires on `_unrun OR _unfinished`, so it also reaches a turn
        whose EARLIER actions ran their tools. Saying "nothing ran" there
        contradicts the same message's "report what WAS actually done"."""
        import hartos.reuse_recipe as reuse
        text = reuse._REUSE_SYNTHESIS_STEER_INCOMPLETE.lower()

        assert 'nothing ran' not in text

    def test_the_incomplete_steer_still_formats_its_one_slot(self):
        """It carries {unrun} and is .format()ed at the call site, so a
        stray brace in the media rule would raise exactly when a turn had
        already gone wrong. The original comment warned of this."""
        import hartos.reuse_recipe as reuse

        out = reuse._REUSE_SYNTHESIS_STEER_INCOMPLETE.format(
            unrun='read_book_page did not run for action 1')

        assert 'read_book_page did not run for action 1' in out
        assert '{' not in out and '}' not in out


@pytest.fixture
def published(monkeypatch):
    """What reached publish_async: (topic, payload, flags)."""
    sent = []

    def resolve(name):
        if name != 'publish_async':
            return None
        return lambda topic, payload, **kw: sent.append((topic, payload, kw))

    monkeypatch.setattr('core.safe_hartos_attr.safe_hartos_attr', resolve)
    return sent


class TestAnAgentsPictureRidesTheSlotABookPageUses:
    """An agent's image goes on ``page_image_url``, the way a book page
    always has.

    Owner ruling 2026-10-25, relayed verbatim through two sessions: "we
    show page image url and analogy images", "it shd be the same exact way
    we used t show earlier", "what filter per agent when did we write one
    agent specific contract".

    This class previously asserted the OPPOSITE, with a confident rationale:
    that the slot is a book-page MODE, that filling it draws the picture
    twice, and that the phone cannot resolve a relative url. The first is
    the behaviour he wants; the second was true only beside a second
    renderer now being withdrawn; the third was a code reading nobody had
    measured, since withdrawn by the lane that made it. Kept in the record
    because the reasoning read just as well when it was wrong.
    """

    def test_the_first_image_fills_the_slot_a_book_page_uses(self, published):
        media = [{'kind': 'image', 'url': '/uploads/pdf_parse/10/page_2.jpg',
                  'xid': 'a' * 64}]

        assert publish_agent_message(text='here is page 2', user_id='u1',
                                     request_id='r1', prompt_id='54', media=media)

        payload = published[0][1]
        assert payload['page_image_url'] == '/uploads/pdf_parse/10/page_2.jpg'
        assert payload['media'] == media        # and the items still ride

    def test_the_slot_takes_the_first_image_past_anything_else(self, published):
        """It holds one url, so which one is a real choice: the first image,
        past a non-image before it, and not the last."""
        publish_agent_message(
            text='t', user_id='u1', request_id='r1', prompt_id='54',
            media=[{'kind': 'video', 'url': '/uploads/a/clip.mp4'},
                   {'kind': 'image', 'url': '/uploads/a/first.jpg'},
                   {'kind': 'image', 'url': '/uploads/a/second.jpg'}])

        assert published[0][1]['page_image_url'] == '/uploads/a/first.jpg'

    def test_the_kinds_the_slot_could_not_take_are_named(self, published, caplog):
        """A kind must not vanish from the record in silence -- but the line
        says only what THIS function knows."""
        with caplog.at_level('INFO'):
            publish_agent_message(
                text='t', user_id='u1', request_id='r1', prompt_id='54',
                media=[{'kind': 'video', 'url': '/uploads/a/clip.mp4'},
                       {'kind': 'html', 'url': '/uploads/a/note.html'}])

        said = ' '.join(r.getMessage() for r in caplog.records)
        assert 'video' in said and 'html' in said
        assert published[0][1]['page_image_url'] == ''
        assert len(published[0][1]['media']) == 2      # still carried

    def test_a_reel_is_never_described_as_unrendered(self, published, caplog):
        """f92d0846f's F1, measured by hevolve-cc12 and correct.

        Reel Studio publishes EVERY reel through this function
        (reel_studio/pipeline.py:569, kind 'video'), and the phone renders
        exactly that item -- ReelInbox.onPush via ReelMedia.isDeliverable.
        The warning I had written fired on all of them and said no client
        renders them, which is false, and the test I wrote pinned the
        falsehood. The publisher cannot know what a client draws, so it may
        not say.
        """
        reel = {'kind': 'video', 'xid': 'a' * 64, 'sha256': 'a' * 64,
                'mime': 'video/mp4', 'url': '/uploads/a/reel.mp4'}

        with caplog.at_level('DEBUG'):
            publish_agent_message(text='your reel is ready', user_id='u1',
                                  request_id='r1', prompt_id='54', media=[reel])

        # The WHOLE line, not a list of banned phrases. Banning phrases is
        # what let the earlier false claims through: a rewording walks past
        # a blocklist, and this test is the one standing between a reel and
        # being described as unrendered again. Pinning the rendered message
        # means any added claim fails, however it is worded.
        assert [r.getMessage() for r in caplog.records
                if r.name == 'core.peer_link.crossbar_publish'] == [
            'publish_agent_message: page_image_url is empty; video item(s) '
            'ride `media` (user u1)']
        assert not [r for r in caplog.records if r.levelname == 'WARNING']
        assert published[0][1]['media'] == [reel]      # delivered untouched

    def test_the_line_says_the_slot_took_an_image_when_it_did(self, published, caplog):
        """The other half of the branch, and nothing pinned it: a mutant
        that ALWAYS logged "is empty" survived, because every existing test
        exercised only the no-image side."""
        with caplog.at_level('INFO'):
            publish_agent_message(
                text='t', user_id='u1', request_id='r1', prompt_id='54',
                media=[{'kind': 'image', 'url': '/uploads/a/p.jpg'},
                       {'kind': 'video', 'url': '/uploads/a/clip.mp4'}])

        said = ' '.join(r.getMessage() for r in caplog.records)
        assert 'carries the first image' in said
        assert 'is empty' not in said
        assert published[0][1]['page_image_url'] == '/uploads/a/p.jpg'

    def test_a_malformed_kind_cannot_break_the_publish(self, published):
        """The kind collection sits OUTSIDE the try that guards publishing,
        so a sort over mixed types raised TypeError and lost the whole
        message -- the person's answer destroyed for the sake of a log
        line.

        Named for what it actually holds. fc47e967a called this "a bad kind
        costs itself", which the reviewer correctly refused: the malformed
        item still rides `media` and reaches the client untouched, as the
        last assertion here shows. It costs nothing; it simply cannot break
        the publish any more.
        """
        assert publish_agent_message(
            text='here is your answer', user_id='u1', request_id='r1',
            prompt_id='54',
            media=[{'kind': 7, 'url': '/uploads/a/x.bin'},
                   {'kind': ['nested'], 'url': '/uploads/a/y.bin'},
                   {'kind': 'image', 'url': '/uploads/a/p.jpg'}])

        payload = published[0][1]
        assert payload['text'] == ['here is your answer']
        assert payload['page_image_url'] == '/uploads/a/p.jpg'
        assert len(payload['media']) == 3

    def test_a_message_with_no_image_leaves_the_slot_empty(self, published):
        for media in (None, [], [{'kind': 'video', 'url': '/uploads/a/clip.mp4'}],
                      [{'kind': 'image', 'xid': 'a' * 64}]):   # an item with no url
            published.clear()
            publish_agent_message(text='t', user_id='u1', request_id='r1',
                                  prompt_id='54', media=media)
            assert published[0][1]['page_image_url'] == '', media

    def test_the_items_ride_in_order_and_unaltered(self, published):
        """Several kinds at once reach the client as sent: the publisher
        chooses no favourite among them and rewrites none of them."""
        media = [{'kind': 'video', 'url': '/uploads/a/clip.mp4', 'xid': 'b' * 64},
                 {'kind': 'image', 'url': '/uploads/a/first.jpg', 'xid': 'c' * 64},
                 {'kind': 'html', 'url': '/uploads/a/note.html', 'xid': 'd' * 64}]

        publish_agent_message(text='t', user_id='u1', request_id='r1',
                              prompt_id='54', media=media)

        payload = published[0][1]
        assert payload['media'] == media
        # the slot is filled from the items, never at their expense: the
        # list the client receives is still every item, in order, unaltered
        assert payload['page_image_url'] == '/uploads/a/first.jpg'

    def test_a_text_only_message_is_the_envelope_it_always_was(self, published):
        """Without media the envelope is exactly the one subscribers have
        parsed since 2026-06-09; this change adds nothing to it."""
        publish_agent_message(text='just words', user_id='u1', request_id='r1',
                              prompt_id='54')

        payload = published[0][1]
        assert 'media' not in payload
        assert payload['page_image_url'] == '' and payload['text'] == ['just words']


class TestTheAgentsRefsReachThePublisher:
    """The agent names media beside its message2userfinal, and the existing
    door carries it. Nothing between has to learn a new shape."""

    def test_the_speaker_door_passes_what_the_agent_wrote(self, monkeypatch):
        """_reuse_speaker_says_to_user already parses the agent's JSON for
        message2userfinal; the media it wrote beside it travels the same
        way, so there is no second door to keep in step."""
        import hartos.reuse_recipe as reuse
        refs = [{'type': 'image', 'url': '/uploads/pdf_parse/10/page_2.jpg'}]
        seen = {}
        monkeypatch.setattr(reuse, '_reuse_message_is_user_answer', lambda m: True)
        monkeypatch.setattr(reuse, 'send_message_to_user1',
                            lambda *a, **kw: seen.update(args=a, kwargs=kw))
        message = {'name': 'Answerer', 'content': json.dumps(
            {'message2userfinal': 'here is page 2', MEDIA_REPLY_KEY: refs})}

        assert reuse._reuse_speaker_says_to_user('u1', message, '54', inp='q') is True
        assert seen['kwargs']['media_refs'] == refs

    def test_an_agent_that_sent_no_media_is_unchanged(self, monkeypatch):
        import hartos.reuse_recipe as reuse
        seen = {}
        monkeypatch.setattr(reuse, '_reuse_message_is_user_answer', lambda m: True)
        monkeypatch.setattr(reuse, 'send_message_to_user1',
                            lambda *a, **kw: seen.update(args=a, kwargs=kw))
        message = {'name': 'Answerer',
                   'content': json.dumps({'message2userfinal': 'just words'})}

        assert reuse._reuse_speaker_says_to_user('u1', message, '54', inp='q') is True
        assert seen['kwargs']['media_refs'] is None

    def test_the_refs_become_items_on_the_published_message(self, uploads, offered,
                                                            monkeypatch):
        """End of the server half: a url the agent wrote comes out the other
        side as an item the store vouched for, on the message the user gets."""
        import core.peer_link.crossbar_publish as cp
        import hartos.reuse_recipe as reuse
        url = _page(uploads)
        seen = {}
        monkeypatch.setattr(reuse, 'is_bundled', lambda: True)
        monkeypatch.setattr(cp, 'publish_agent_message',
                            lambda **kw: (seen.update(kw), True)[1])

        reuse.send_message_to_user1('u1', 'here is page 2', '', '54',
                                    media_refs=[{'type': 'image', 'url': url}])

        assert seen['text'] == 'here is page 2'
        assert [i['kind'] for i in seen['media']] == ['image']
        assert seen['media'][0]['xid'] == hashlib.sha256(JPEG).hexdigest()
        assert seen['media'][0]['url'] == url

    def test_a_message_with_no_refs_publishes_no_media(self, uploads, offered,
                                                       monkeypatch):
        import core.peer_link.crossbar_publish as cp
        import hartos.reuse_recipe as reuse
        seen = {}
        monkeypatch.setattr(reuse, 'is_bundled', lambda: True)
        monkeypatch.setattr(cp, 'publish_agent_message',
                            lambda **kw: (seen.update(kw), True)[1])

        reuse.send_message_to_user1('u1', 'just words', '', '54')

        assert seen['media'] == [] and offered == []

    def test_the_example_is_real_json_carrying_exactly_the_reply_key(self):
        """The schema must OFFER the key the readers honour, by that exact
        name.

        Parsed, not matched. The review of 19836e605 killed the substring
        version: a literal 'media_items' satisfies `MEDIA_REPLY_KEY in
        template` while no reader consumes it, and 24/24 stayed green.
        """
        example = _template_example()

        assert 'message2userfinal' in example
        assert MEDIA_REPLY_KEY in example

    def test_the_example_offers_exactly_the_kinds_the_producer_accepts(self):
        """Equality, not membership. A kind named but dropped is an agent
        told to do something that silently fails; a kind accepted but hidden
        is dead capability. The gif|svg mutant passed the old version."""
        from hartos.helper import MEDIA_KIND_MIMES
        example = _template_example()

        offered = set(example[MEDIA_REPLY_KEY][0]['type'].split('|'))
        assert offered == set(MEDIA_KIND_MIMES)

    def test_the_example_url_is_the_shape_the_producer_will_resolve(self):
        """An example the producer would itself drop teaches the agent to
        fail."""
        example = _template_example()

        assert example[MEDIA_REPLY_KEY][0]['url'].startswith('/uploads/')

    def test_the_rule_names_uploads_not_merely_a_tool(self):
        """19836e605 said "ONLY with a url a tool gave you". That was NOT
        the rule the code enforces, and the difference matters: txt2img
        returns a remote aws_rasa url and Generate_video returns a conv_id,
        so under the old wording the owner's own first example -- text to
        image -- would be dropped by media_items_for in silence. The
        enforced rule is a /uploads path on THIS node.

        Prose can only be pinned weakly; this says so rather than pretending
        otherwise. The enforcement itself is pinned by the drop tests above.
        """
        import hartos.reuse_recipe as reuse
        text = reuse.response_format_template().lower()

        assert 'uploads' in text
        for invitation in ('make one up', 'invent', 'guess'):
            assert invitation not in text, invitation

    def test_the_main_chat_answer_offers_its_media_to_the_store(self, uploads, offered):
        """F1 of the 19836e605 review, and the defect that mattered most.

        Six prompt sites invited media and ONE reader honoured it: the
        speaker door. get_agent_response, which produces the MAIN /chat
        answer -- the book-page case -- returned message2userfinal alone and
        dropped every item, with media_items_for called 0 times.

        Putting the files in her outbox here is all that is needed: the
        reply already takes them, because _chat_reply calls
        chat_attachments.take_outbound for any turn with a prompt_id
        (hart_intelligence_entry.py:9024) and puts them on the reply as
        `media`. One existing path, no new field on the reply.
        """
        import hartos.reuse_recipe as reuse
        url = _page(uploads)

        reuse._carry_reply_media('u1', {'message2userfinal': 'here is page 2',
                                        MEDIA_REPLY_KEY: [{'type': 'image',
                                                           'url': url}]},
                                 '54', 'r1')

        assert [c['path'] for c in offered] == \
            [str(uploads / 'pdf_parse' / '10' / 'page_2.jpg')]

    def test_an_answer_with_no_media_never_touches_the_store(self, uploads, offered):
        import hartos.reuse_recipe as reuse

        for obj in ({'message2userfinal': 'just words'},
                    {'message2userfinal': 'x', MEDIA_REPLY_KEY: []},
                    {'message2userfinal': 'x', MEDIA_REPLY_KEY: None},
                    {}):
            reuse._carry_reply_media('u1', obj, '54', 'r1')

        assert offered == []

    def test_a_main_answer_with_an_invented_url_offers_nothing(self, uploads, offered):
        """The guard holds on this path too, not only the speaker door."""
        import hartos.reuse_recipe as reuse

        reuse._carry_reply_media(
            'u1', {'message2userfinal': 'here',
                   MEDIA_REPLY_KEY: [{'type': 'image',
                                      'url': '/uploads/pdf_parse/10/ghost.jpg'}]},
            '54', 'r1')

        assert offered == []

    def test_create_carries_media_the_same_way(self, uploads, offered, monkeypatch):
        """publish_agent_message calls itself "the one on-device path for
        send_message_to_user1 in BOTH create_recipe and reuse_recipe". One
        half wired and the other not is how the two drift apart."""
        import core.peer_link.crossbar_publish as cp
        import hartos.create_recipe as create
        url = _page(uploads)
        seen = {}
        monkeypatch.setattr(create, 'is_bundled', lambda: True)
        monkeypatch.setattr(cp, 'publish_agent_message',
                            lambda **kw: (seen.update(kw), True)[1])

        create.send_message_to_user1('u1', 'here is page 2', '', '54',
                                     media_refs=[{'type': 'image', 'url': url}])

        assert [i['kind'] for i in seen['media']] == ['image']
        assert seen['media'][0]['xid'] == hashlib.sha256(JPEG).hexdigest()
