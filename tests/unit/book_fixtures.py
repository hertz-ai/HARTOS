"""Shared fixtures for the book pipeline tests.

PDFs are built from raw PDF syntax -- known text, an optional outline, any page
size, an optional metadata title, or verbatim content streams for malformed
cases -- so the code under test reads bytes no PDF library produced. A few
fixtures that need encryption or a nested outline are post-processed with
pypdf (already a HARTOS dependency); those skip when it is absent.

`book_node` is a node with a REAL SQLite library (the real models, on a file,
NullPool like production) and NO vision model or title LLM -- a CPU-only box --
with a recorder in place of the client publisher. A test module loads the
fixtures with `pytest_plugins = ['tests.unit.book_fixtures']`.
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PAGES = [
    ("Chapter 1: Units and Measurement", "The SI base unit of length is the metre."),
    ("Chapter 1: Units and Measurement", "Dimensional analysis checks equations."),
    ("Chapter 2: Motion", "Acceleration is the rate of change of velocity."),
]
OUTLINE = [("Chapter 1: Units and Measurement", 0), ("Chapter 2: Motion", 2)]


def _esc(s):
    return s.replace('\\', r'\\').replace('(', r'\(').replace(')', r'\)')


def make_pdf(path, pages=PAGES, outline=OUTLINE, media_box=(612, 792),
             title=None, raw_streams=None) -> Path:
    """Write a PDF with one page per (heading, line), drawn as real text.

    outline      [(title, 0-based page index)] bookmarks; None/[] for none
    media_box    (width, height) in points, for every page
    title        a /Title in the document info dictionary
    raw_streams  replaces each page's content stream verbatim
    """
    path = Path(path)
    n = len(pages)
    page_ids = [3 + i for i in range(n)]
    content_ids = [3 + n + i for i in range(n)]
    font_id = 3 + 2 * n
    outline_id = font_id + 1
    item_ids = [outline_id + 1 + i for i in range(len(outline or []))]
    info_id = (item_ids[-1] if item_ids else outline_id) + 1
    width, height = media_box

    objs = {
        1: "<< /Type /Catalog /Pages 2 0 R"
           + (f" /Outlines {outline_id} 0 R" if outline else "") + " >>",
        2: f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {n} >>",
        font_id: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i, (heading, line) in enumerate(pages):
        objs[page_ids[i]] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            f"/Contents {content_ids[i]} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>")
        if raw_streams is not None:
            stream = raw_streams[i]
        else:
            stream = (f"BT\n/F1 18 Tf\n72 {height - 72} Td\n({_esc(heading)}) Tj\n"
                      f"0 -40 Td\n({_esc(line)}) Tj\nET\n")
        # The EOL before `endstream` is outside /Length, so the stream's data
        # is exactly `stream` -- a raw stream can end mid-comment, no newline.
        objs[content_ids[i]] = (f"<< /Length {len(stream.encode('latin-1'))} >>\n"
                                f"stream\n{stream}\nendstream")
    if outline:
        objs[outline_id] = (f"<< /Type /Outlines /First {item_ids[0]} 0 R "
                            f"/Last {item_ids[-1]} 0 R /Count {len(item_ids)} >>")
        for k, (bookmark, page_index) in enumerate(outline):
            links = ((f" /Prev {item_ids[k - 1]} 0 R" if k else "")
                     + (f" /Next {item_ids[k + 1]} 0 R" if k + 1 < len(item_ids) else ""))
            objs[item_ids[k]] = (f"<< /Title ({_esc(bookmark)}) /Parent {outline_id} 0 R"
                                 f"{links} /Dest [{page_ids[page_index]} 0 R /Fit] >>")
    trailer_info = ''
    if title:
        objs[info_id] = f"<< /Title ({_esc(title)}) >>"
        trailer_info = f" /Info {info_id} 0 R"

    out, offsets = bytearray(b"%PDF-1.4\n"), {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n{objs[num]}\nendobj\n".encode('latin-1')
    xref_at, total = len(out), max(objs) + 1
    out += f"xref\n0 {total}\n0000000000 65535 f \n".encode('latin-1')
    for num in range(1, total):
        if num in offsets:
            out += f"{offsets[num]:010d} 00000 n \n".encode('latin-1')
        else:
            out += b"0000000000 65535 f \n"
    out += (f"trailer\n<< /Size {total} /Root 1 0 R{trailer_info} >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode('latin-1')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def make_encrypted_pdf(path, password='secret') -> Path:
    pypdf = pytest.importorskip('pypdf')
    path = Path(path)
    plain = make_pdf(path.with_name(path.stem + '_plain.pdf'))
    writer = pypdf.PdfWriter(clone_from=pypdf.PdfReader(str(plain)))
    writer.encrypt(user_password=password, owner_password=password)
    with open(path, 'wb') as fh:
        writer.write(fh)
    return path


def make_nested_outline_pdf(path) -> Path:
    """Chapters at the outline's top level, sections beneath them."""
    pypdf = pytest.importorskip('pypdf')
    path = Path(path)
    plain = make_pdf(path.with_name(path.stem + '_plain.pdf'), outline=None)
    writer = pypdf.PdfWriter(clone_from=pypdf.PdfReader(str(plain)))
    chapter_one = writer.add_outline_item('Chapter 1: Units and Measurement', 0)
    writer.add_outline_item('1.1 Base units', 0, parent=chapter_one)
    writer.add_outline_item('1.2 Dimensions', 1, parent=chapter_one)
    writer.add_outline_item('Chapter 2: Motion', 2)
    with open(path, 'wb') as fh:
        writer.write(fh)
    return path


@pytest.fixture
def book_db(tmp_path, monkeypatch):
    """The real models on a real SQLite file (NullPool, as in production), so
    a background parse thread and the test each get their own connection."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from integrations.social import models
    engine = create_engine(f"sqlite:///{(tmp_path / 'books.db').as_posix()}",
                           connect_args={'check_same_thread': False},
                           poolclass=NullPool)
    # Only the library's two tables. The shared metadata holds every social
    # table, and creating them all cost seconds and megabytes per test.
    models.Base.metadata.create_all(
        engine, tables=[models.BookFile.__table__, models.BookPageLayout.__table__])
    monkeypatch.setattr(models, '_engine', engine)
    monkeypatch.setattr(models, '_SessionLocal',
                        sessionmaker(bind=engine, expire_on_commit=False))
    yield engine
    engine.dispose()


@pytest.fixture
def book_uploads(tmp_path, monkeypatch):
    root = tmp_path / 'uploads'
    monkeypatch.setattr('core.platform_paths.get_uploads_dir', lambda: str(root))
    return root


class _Node:
    """What a test can see of the stubbed boundaries."""


@pytest.fixture
def book_node(book_db, book_uploads, monkeypatch):
    from integrations.learning import book_pipeline as bp
    node = _Node()
    node.uploads = book_uploads
    node.vision = MagicMock(return_value=None)          # no vision model
    node.published = []
    monkeypatch.setattr('integrations.vision.image_describe.describe_image', node.vision)
    monkeypatch.setattr(bp, '_generate_book_name', lambda text, toc: None)
    monkeypatch.setattr(bp, '_publish',
                        lambda user_id, payload: node.published.append((user_id, payload)) or True)
    return node
