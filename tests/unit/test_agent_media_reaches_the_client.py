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


def _page(uploads, name='page_2.jpg', data=JPEG):
    path = uploads / 'pdf_parse' / '10' / name
    path.write_bytes(data)
    return '/uploads/pdf_parse/10/' + name


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


class TestTheLegacyImageSlotIsLeftAlone:
    """``media`` is the ONE path; the legacy slot is not a spare image field.

    ad729fcc9 derived page_image_url from the first image item, reasoning
    that it was the only slot the desktop read. That was wrong, and this is
    the guard against reinstating it.

    ``page_image_url`` is not "an image": on the desktop
    ``setUploadedImage(parsed.page_image_url)`` (Demopage.js:2477, :2558)
    switches the left panel to the PdfViewer book-page view, and the gate at
    :5179 forces that panel w-full whenever uploadedImage is set -- in text
    mode too. Beside a client that renders ``media`` (Nunba 523b8b71), a
    derived slot draws the same picture twice AND rearranges the screen. On
    the phone the slot is loaded by Glide with no base URL, so a
    desktop-relative '/uploads/...' resolves to nothing and the card comes
    up empty.

    Each client fetches the way it can -- the desktop by ``url``, the phone
    by ``xid`` -- and every item carries both, which is what makes the
    second path unnecessary rather than merely redundant.
    """

    def test_media_never_fills_the_legacy_slot(self, published):
        """The regression guard. A reply's media must not reach the slot
        that drives the book-page panel and the phone's Glide card."""
        media = [{'kind': 'image', 'url': '/uploads/pdf_parse/10/page_2.jpg',
                  'xid': 'a' * 64}]

        assert publish_agent_message(text='here is page 2', user_id='u1',
                                     request_id='r1', prompt_id='54', media=media)

        payload = published[0][1]
        assert payload['page_image_url'] == ''
        assert payload['media'] == media        # and the one real path is intact

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
        assert payload['page_image_url'] == ''

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

    def test_the_schema_offers_the_key_the_door_honours(self):
        """An agent can only send what the reply schema offers it.

        Pins the COUPLING, not the wording: response_format_template is the
        ONE source interpolated into six prompt sites as
        `@user {response_format}`, and the door test above builds its JSON
        with this same constant. Rename the key in either place and one of
        the two fails -- where a prompt string-match would stay green while
        the agent's media silently stopped arriving. That is exactly how
        page_image_url went dead.
        """
        import hartos.reuse_recipe as reuse

        assert MEDIA_REPLY_KEY in reuse.response_format_template()

    def test_the_schema_advertises_exactly_the_kinds_the_producer_accepts(self):
        """A kind the prompt names but the producer drops is an agent told
        to do something that silently fails; a kind the producer accepts but
        the prompt hides is dead capability. They must be the same set."""
        from hartos.helper import MEDIA_KIND_MIMES
        import hartos.reuse_recipe as reuse
        advertised = reuse.response_format_template()

        for kind in MEDIA_KIND_MIMES:
            assert kind in advertised, f'{kind} is accepted but never offered'
        for word in ('audio', 'pdf', 'document'):      # not accepted today
            assert word not in advertised, f'{word} is offered but would be dropped'

    def test_the_schema_tells_the_agent_not_to_invent_a_url(self):
        """The one instruction that matters, given 'The Art of Computer
        Programming' at t1-retrieval-102136: a url the model did not get
        from a tool is dropped by media_items_for, so the agent is told
        that rather than left to discover it as a silent failure."""
        advertised = __import__('hartos.reuse_recipe', fromlist=['x']) \
            .response_format_template().lower()

        assert 'tool' in advertised
        assert '/uploads/' in advertised

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
