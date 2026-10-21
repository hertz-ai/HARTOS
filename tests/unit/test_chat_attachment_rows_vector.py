"""The one cross-language vector for a /chat turn's ``attachments`` rows:
tests/fixtures/chat_attachment_rows.json.

The phone keeps a copy of the row rule in its own language because it must
refuse a row before it sends one (Hevolve_React_Native
DesktopFileWorker.attachmentProblems; ReelInbox carries only the rows it
accepts).  The rule itself is HARTOS's one rule,
core.chat_attachments.validate_attachment, and this vector is what keeps the
two copies one rule (amendment A20, D23p1).  Each ``rows`` entry is a row with
the problems validate_attachment finds in it; each ``mirror`` entry is a reel
media item with the chat mirror rows media_to_mirror writes for it.  Here
HARTOS replays every entry through those functions and must give exactly
what the vector records.  The phone's ChatAttachmentRowsTest reads a
byte-for-byte copy (android/app/src/test/resources/chat_attachment_rows.json)
and must name the same fields.  Both tests pin the file's sha256, so neither
side can change it alone (the tests/fixtures/peerlink_file_vector.json
pattern).

When the rule or its words change, this test fails first: record the
vector again from the new answers, copy it to the phone byte for byte, and
move both pins.

The file is one line with no line break: both repos check out with
core.autocrlf=true, which would otherwise rewrite it, and its hash, on
Windows.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.chat_attachments import media_to_mirror, parse_inbound, validate_attachment  # noqa: E402
from core.user_file_store import INBOUND_META_KEYS  # noqa: E402

VECTOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      'fixtures', 'chat_attachment_rows.json')
#: The phone's ChatAttachmentRowsTest pins this same value.
PINNED_SHA256 = '1355c9e8b259b625d45491d7a8b7aa17820779b0d3b4f30717d4d81733f3c58c'

#: Every field a row is judged on: the documented U9 row, its optional
#: purpose, then the offer's meta (the store's own list of meta keys).
_FIELDS = ('file_id', 'sha256', 'name', 'mime', 'size', 'purpose') + tuple(INBOUND_META_KEYS)


def _raw() -> bytes:
    with open(VECTOR, 'rb') as f:
        return f.read()


def _vector() -> dict:
    return json.loads(_raw())


def test_the_vector_is_the_pinned_one_and_one_line():
    raw = _raw()
    assert hashlib.sha256(raw).hexdigest() == PINNED_SHA256
    assert b'\n' not in raw and b'\r' not in raw
    vector = _vector()
    assert vector['schema'] == 'hart.chat.attachment_rows.v1'
    assert vector['recorded_from'].startswith('HARTOS ')


def test_every_row_gets_exactly_the_problems_the_vector_records():
    rows = _vector()['rows']
    assert len(rows) >= 50
    for e in rows:
        assert validate_attachment(e['row']) == e['problems'], e['note']


def test_the_vector_refuses_a_row_for_each_field_the_rule_judges():
    # A field the rule judges with no row refusing it would be a rule the
    # phone never checks against HARTOS (a new meta key, say).
    problems = [p for e in _vector()['rows'] for p in e['problems']]
    for field in _FIELDS:
        assert any(p.startswith('attachment.%s ' % field) for p in problems), field
    assert 'attachment is not an object' in problems


def test_hartos_reads_a_row_as_a_reference_exactly_when_the_vector_says_it_binds():
    # What the phone relies on: a row the rule accepts is bound by its
    # file_id (and purpose, when it names one); any other is dropped whole.
    for e in _vector()['rows']:
        row = e['row']
        if e['problems']:
            assert parse_inbound([row]) == [], e['note']
        else:
            ref = {'file_id': row['file_id']}
            if 'purpose' in row:
                ref['purpose'] = row['purpose']
            assert parse_inbound([row]) == [ref], e['note']


def test_every_reel_item_mirrors_to_exactly_the_rows_the_vector_records():
    mirror = _vector()['mirror']
    assert mirror
    for e in mirror:
        rows = media_to_mirror([e['media']])
        assert rows == e['rows'], e['note']
        for row in rows:
            assert validate_attachment(row) == [], e['note']
