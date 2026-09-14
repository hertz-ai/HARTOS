"""Book pipeline: a PDF becomes pages, chapters and page images, on ANY node.

WHY IT LIVES HERE
-----------------
Owner requirement (2026-09-13): any HARTOS instance on the network must be
able to service a book upload, agnostic of OS and platform, with no desktop
client present. The pipeline used to live in Nunba's routes/upload_routes.py
and routes/db_routes.py, reaching the app only through Nunba's consumer-routes
hook. So a HARTOS node without Nunba (the :6777 server, Docker, HART OS,
central) could not parse a book, and HARTOS's own PDF path
(hart_intelligence_entry._parse_pdf_in_process) imported its helpers from
Nunba's routes.

THIS IS THE ONE IMPLEMENTATION. Every entry point calls it:
  * POST /upload/parse_pdf           integrations/learning/api_books.py
  * the parse_book_pdf agent tool    integrations/learning/book_tools.py
  * a PDF link the agent reads       hart_intelligence_entry._parse_pdf_in_process
                                     -> fetch_pdf() + parse_book()
  * Nunba's /upload/file, /upload/native  -> start_parse()

HOW A PAGE IS READ
------------------
One PDF engine, pypdfium2 (PDFium, the engine Chrome uses). It renders the
page image, extracts the text layer and reads the outline, from prebuilt
wheels for Windows, macOS, Linux (glibc and musl, x86 and ARM) and Android,
with no system binary. It replaces the three libraries the old code tried in
turn -- pdf2image+poppler, PyMuPDF, PyPDF2 -- none of which any target
shipped; PyPDF2 3.0.1 in particular loops forever on a crafted content stream
(CVE-2023-36464, reproduced 2026-09-13).

Each page image goes to the vision model (integrations.vision.image_describe):
one VLM call per page does OCR, layout, tables and figures, where the cloud
pipeline used six models. When the model is not there, or returns nothing for
a page, that page falls back to its own text layer, so a node with no vision
model still produces a readable book. Chapters come from the PDF's outline
when it has one (it points at real page indices), else from the
table-of-contents pages the model read.

Progress goes out on the MessageBus (core/peer_link/message_bus.py), topic
'book.parsing': to this node's own subscribers and the desktop's SSE, to the
user's other devices over PeerLink, and over Crossbar on
com.hertzai.bookparsing.{user_id} -- the topic central's pipeline published
on, and the one the web app and Android render.

BOUNDED
-------
The parser now faces the network, not just localhost, so no input may pin a
request thread, exhaust memory, or leave a record 'pending' forever:
  * a parse never runs on a request thread (start_parse returns at once);
  * a page renders at most MAX_RENDER_PX on its long side, whatever size the
    page claims -- a page declared 200 inches wide is an ordinary image, not
    a multi-gigabyte bitmap;
  * MAX_PAGES and MAX_PDF_BYTES bound the work per document -- a download is
    counted in decoded bytes, so a compressed response cannot inflate past it;
  * PDFium is not thread-safe, so every call into it is serialized, while the
    vision model is called outside that lock and parses still run side by side;
  * a parse that finishes no page for STALL_SECONDS is recorded as failed;
  * a write that meets another writer's lock waits it out, briefly, and a read
    never fails because recording a stalled book could not be written.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

#: Page render resolution: what the vision model reads, and what a quoted page
#: image looks like. A letter page at 200 DPI is 1700 x 2200 px.
RENDER_DPI = 200
#: Longest side of any rendered page, whatever size the page CLAIMS to be.
MAX_RENDER_PX = 2400
#: Pages per document. A large textbook is around 1,000.
MAX_PAGES = 2000
#: Bytes per uploaded or downloaded PDF.
MAX_PDF_BYTES = 100 * 1024 * 1024
#: Bytes read per download chunk. A compressed response inflates as it streams,
#: so the MAX_PDF_BYTES cap is checked after every chunk of DECODED bytes.
_DOWNLOAD_CHUNK = 8192
#: A parse that finishes no page for this long is dead: a hang inside native
#: code cannot be interrupted from Python, so it is recorded as failed instead.
STALL_SECONDS = 30 * 60
#: How often a running parse refreshes its row's updated_at.
_TOUCH_EVERY_S = 60
#: In-memory job records kept, oldest dropped first. The durable record is the row.
_JOB_CAP = 256
#: Vision errors, before any success, after which the rest of the book is read
#: from its text layer rather than asking the model again for every page.
_VLM_GIVE_UP_AFTER = 2
#: The MessageBus topic for progress. The bus's TOPIC_MAP carries it to
#: com.hertzai.bookparsing.{user_id}, which the web app (crossbarWorker) and
#: Android (AutobahnConnectionManager) already render: `percentage` drives the
#: "Understanding the Content: N%" bar.
PROGRESS_TOPIC = 'book.parsing'

_LIVE = ('pending', 'processing')


class BookParseError(Exception):
    """A book could not be parsed. The message is written to be shown to a user."""


# ── Where things live ─────────────────────────────────────────────────

def uploads_dir() -> Path:
    from core.platform_paths import get_uploads_dir
    return Path(get_uploads_dir())


def files_dir() -> Path:
    """Where uploaded and downloaded PDFs are kept (served as /uploads/files/...)."""
    d = uploads_dir() / 'files'
    d.mkdir(parents=True, exist_ok=True)
    return d


def page_image_path(file_id, page_number) -> Path:
    return uploads_dir() / 'pdf_parse' / str(int(file_id)) / f'page_{int(page_number)}.jpg'


def page_image_url(file_id, page_number) -> str:
    """The URL a client fetches a page image from: served by api_books on every
    node, and on the desktop Nunba's /uploads/<path> resolves to the same file."""
    return f'/uploads/pdf_parse/{int(file_id)}/page_{int(page_number)}.jpg'


def upload_url_for(path) -> str:
    """The /uploads/... URL of a file stored under uploads_dir()."""
    rel = Path(path).resolve().relative_to(uploads_dir().resolve())
    return '/uploads/' + rel.as_posix()


def resolve_upload_url(file_url) -> Optional[Path]:
    """'/uploads/files/x.pdf' -> that file under uploads_dir(), or None.

    The path is checked BEFORE any filesystem call: plain names only -- no
    empty, '.' or '..' segment and no drive letter -- so a URL can neither
    climb out of the uploads nor name another machine. On Windows the old
    join made '/uploads///host/share/x' the UNC path \\\\host\\share\\x, and
    touching that goes to the network: measured 2026-09-13, a stat of such a
    path answered WinError 53, "The network path was not found" -- a caller
    could make this node open an SMB connection to a host of its choosing.
    The containment check after resolving stays as a second guard. Nunba's
    route joined the path unchecked: harmless on loopback, not once any node
    serves the route to the network.
    """
    text = str(file_url or '')
    if not text.startswith('/uploads/'):
        return None
    parts = text[len('/uploads/'):].replace('\\', '/').split('/')
    if any(part in ('', '.', '..') or ':' in part for part in parts):
        logger.warning(f"refused upload URL: {text!r}")
        return None
    root = uploads_dir().resolve()
    try:
        target = root.joinpath(*parts).resolve()
    except (OSError, RuntimeError, ValueError) as e:
        logger.debug(f"upload URL {text!r} did not resolve: {e}")
        return None
    if target != root and root not in target.parents:
        logger.warning(f"refused upload URL outside the uploads dir: {text!r}")
        return None
    return target if target.is_file() else None


def save_pdf(data: bytes, original_name: str = '') -> Path:
    """Validate and store an uploaded or downloaded PDF under uploads/files."""
    from werkzeug.utils import secure_filename
    if not data:
        raise BookParseError('the file is empty')
    if len(data) > MAX_PDF_BYTES:
        raise BookParseError(
            f'the file is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB')
    if b'%PDF-' not in data[:1024]:
        raise BookParseError('the file is not a PDF')
    stem = secure_filename(Path(original_name or 'book').stem) or 'book'
    path = files_dir() / f'{uuid.uuid4().hex[:12]}_{stem[:80]}.pdf'
    try:
        path.write_bytes(data)
    except OSError:
        # Out of disk, most likely: keep no partial file behind.
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"could not remove the partial upload {path.name}: {e}")
        raise
    return path


def fetch_pdf(url, timeout=60) -> Path:
    """Download a PDF the agent found by URL into the uploads.

    At most MAX_PDF_BYTES of DECODED bytes are read: requests inflates a
    gzip/deflate body as it streams, so the cap is counted on what comes out,
    chunk by chunk. A raw read(N) counts the compressed bytes: measured
    2026-09-13, read(1 MB + 1) of a 65 KB gzip body handed on 64 MB, where
    this hands on 8 MB and refuses it.
    """
    import requests

    from core.http_pool import pooled_get
    try:
        response = pooled_get(url, timeout=timeout, stream=True)
    except requests.RequestException as e:
        raise BookParseError(f'the PDF could not be downloaded ({e})') from e
    try:
        if response.status_code != 200:
            raise BookParseError(
                f'the PDF could not be downloaded (HTTP {response.status_code})')
        data = bytearray()
        for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK):
            data += chunk
            if len(data) > MAX_PDF_BYTES:
                break            # save_pdf refuses it by size, with its own message
    except requests.RequestException as e:
        raise BookParseError(f'the PDF download failed ({e})') from e
    finally:
        response.close()
    name = str(url).split('?', 1)[0].rstrip('/').rsplit('/', 1)[-1]
    return save_pdf(bytes(data), name or 'book.pdf')


# ── The PDF engine (pypdfium2) ────────────────────────────────────────

def _pdfium():
    try:
        import pypdfium2
    except ImportError as e:
        raise BookParseError(
            'PDF support is not installed on this node (pypdfium2 is missing)') from e
    return pypdfium2


def _open(pdf_path):
    pdfium = _pdfium()
    try:
        return pdfium.PdfDocument(str(pdf_path))
    except pdfium.PdfiumError as e:
        if 'password' in str(e).lower():
            raise BookParseError('this PDF is password-protected') from e
        raise BookParseError(f'this file could not be read as a PDF ({e})') from e
    except OSError as e:
        raise BookParseError(f'the PDF could not be opened ({e})') from e


#: PDFium is not thread-safe -- pypdfium2's README says so ("Incompatibility
#: with Threading") -- and its ctypes calls release the GIL. Two parses at once
#: (two uploads, or an upload beside an agent reading a PDF link) kill the
#: process: with this lock removed, the two-parse test died in 4 runs of 4
#: (2026-09-13), a Windows fatal exception inside PDFium. So every PDFium call
#: is made under this one lock; the vision model, which takes seconds a page,
#: is called outside it.
_PDFIUM_LOCK = threading.RLock()


def _page_image(page):
    """The page as a PIL image, never larger than MAX_RENDER_PX on its long side."""
    width, height = page.get_size()
    scale = min(RENDER_DPI / 72.0,
                MAX_RENDER_PX / max(float(width), float(height), 1.0))
    bitmap = page.render(scale=scale)
    try:
        return bitmap.to_pil().convert('RGB')      # a copy, so the bitmap can close
    finally:
        bitmap.close()


def _write_jpeg(image, out_path: Path) -> Path:
    """Write under a temporary name and move it into place, so the page-image
    route never serves a half-written file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_name(out_path.name + '.part')
    image.save(str(partial), 'JPEG', quality=85)
    os.replace(partial, out_path)
    return out_path


def _text(page) -> str:
    textpage = page.get_textpage()
    try:
        text = textpage.get_text_bounded() or ''
    finally:
        textpage.close()
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _outline(pdf) -> List[dict]:
    """The PDF's bookmarks as [{'title', 'page' (1-based), 'level'}]; [] when none."""
    entries = []
    try:
        for bookmark in pdf.get_toc():
            dest = bookmark.get_dest()
            index = dest.get_index() if dest is not None else None
            title = (bookmark.get_title() or '').strip()
            if title and index is not None:
                entries.append({'title': title, 'page': int(index) + 1,
                                'level': int(getattr(bookmark, 'level', 0) or 0)})
    except Exception as e:
        logger.debug(f"PDF outline unreadable: {e}")
    return entries


def _metadata_title(pdf) -> Optional[str]:
    try:
        title = (pdf.get_metadata_dict().get('Title') or '').strip()
    except Exception as e:
        logger.debug(f"PDF metadata unreadable: {e}")
        return None
    return title or None


def _read_page(pdf, index, page_num, file_id):
    """One page's text layer and image, read under the PDFium lock.

    A page PDFium cannot load, or whose text or image it cannot produce, is
    read without it; a native fault (OSError out of the engine) stops the
    book, because PDFium's state after one is not to be trusted.
    """
    with _PDFIUM_LOCK:
        try:
            page = pdf[index]
        except OSError:
            raise
        except Exception as e:
            logger.warning(f"book {file_id}: page {page_num} could not be loaded: {e}")
            return '', None
        try:
            try:
                layer = _text(page)
            except OSError:
                raise
            except Exception as e:
                logger.warning(f"book {file_id}: page {page_num} has no readable text: {e}")
                layer = ''
            try:
                picture = _page_image(page)
            except OSError:
                raise
            except Exception as e:
                logger.warning(f"book {file_id}: page {page_num} did not render: {e}")
                picture = None
        finally:
            page.close()
    return layer, picture


# ── One page through the vision model (moved from Nunba unchanged) ────

#: What the model is asked for each page -- the prompt Nunba's route used.
_PAGE_PROMPT = (
    "You are a document parser. Analyze this page image and extract:\n"
    "1. ALL text content (OCR), preserving paragraph structure\n"
    "2. Page type: 'cover', 'table_of_contents', 'chapter_start', 'content', 'index', 'blank'\n"
    "3. Layout elements found: list of {type, content} where type is one of: "
    "'heading', 'paragraph', 'table', 'figure', 'equation', 'list', 'caption', 'footer', 'header'\n"
    "4. If this is a table of contents, extract chapter/topic names with page numbers\n"
    "5. If there are tables, extract as markdown tables\n"
    "6. If there are figures/images, describe them\n\n"
    "Respond as JSON:\n"
    "{\n"
    '  "page_type": "content",\n'
    '  "text": "full extracted text...",\n'
    '  "elements": [\n'
    '    {"type": "heading", "content": "Chapter 1: Introduction"},\n'
    '    {"type": "paragraph", "content": "Lorem ipsum..."},\n'
    '    {"type": "table", "content": "| Col1 | Col2 |\\n|---|---|\\n| a | b |"},\n'
    '    {"type": "figure", "content": "Diagram showing neural network architecture"}\n'
    '  ],\n'
    '  "toc_entries": [{"title": "Chapter 1", "page": 5}],\n'
    '  "chapter_name": "Introduction",\n'
    '  "has_equations": false,\n'
    '  "has_tables": true,\n'
    '  "has_figures": false\n'
    "}"
)


def _parse_page_via_vision(page_num: int, image_path: str) -> dict:
    """One VLM call: OCR + layout + tables + figures for one page image."""
    unavailable = {"page_number": page_num, "page_type": "unknown", "text": "",
                   "elements": [], "error": "Vision inference unavailable"}
    try:
        from integrations.vision.image_describe import describe_image
    except ImportError as e:
        # The vision package would not import on this node. That is the same
        # as having no model -- the page is read from its text layer -- not a
        # reason to fail the book.
        logger.warning(f"vision unavailable for book pages: {e}")
        return unavailable
    result = describe_image(image_path, _PAGE_PROMPT)
    if not result:
        return unavailable

    cleaned = result.strip()
    if cleaned.startswith('```'):
        cleaned = cleaned.split('\n', 1)[1] if '\n' in cleaned else cleaned[3:]
    if cleaned.endswith('```'):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()
    if cleaned.startswith('json'):
        cleaned = cleaned[4:].strip()
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if not isinstance(parsed, dict):
        # The model answered in prose: keep it as the page's text.
        return {"page_number": page_num, "page_type": "content", "text": result,
                "elements": [{"type": "paragraph", "content": result}]}

    parsed["page_number"] = page_num
    if not isinstance(parsed.get("text"), str):
        parsed["text"] = ""
    elements = parsed.get("elements")
    parsed["elements"] = ([e for e in elements if isinstance(e, dict)]
                          if isinstance(elements, list) else [])
    toc = parsed.get("toc_entries")
    parsed["toc_entries"] = ([e for e in toc if isinstance(e, dict)]
                             if isinstance(toc, list) else [])
    return parsed


def _entry_page(entry) -> int:
    try:
        return int(entry.get('page', 0))
    except (ValueError, TypeError, AttributeError):
        return 0


def assign_chapters(pages_data, toc_entries, field='chapter_name', bounds=()):
    """Give each page the name of the ToC entry whose page range it falls in.

    Moved from Nunba (_assign_chapters_to_pages, the pipeline repo's
    db_page_wise_call.py logic). `field` lets the same range logic fill
    topic_name from the outline's sub-headings. `bounds` are entries whose
    start ENDS the range before them without naming a page: given the
    chapters, a chapter's last section stops at the next chapter instead of
    naming every page up to the next section, wherever that is.
    """
    if not toc_entries:
        return pages_data

    # (start page, rank, name). A bound ranks first on a shared page, so a
    # section that opens on its chapter's first page still names that page.
    marks = []
    for entry in toc_entries:
        page = _entry_page(entry)
        title = (entry.get('title', entry.get('chapter_name', ''))
                 if isinstance(entry, dict) else '')
        if page > 0 and title:
            marks.append((page, 1, title))
    if not marks:
        return pages_data
    marks.extend((page, 0, None) for page in map(_entry_page, bounds) if page > 0)
    marks.sort(key=lambda m: (m[0], m[1]))

    for page_data in pages_data:
        page_num = page_data.get('page_number', 0)
        assigned = None
        for i, (start, _rank, name) in enumerate(marks):
            next_start = marks[i + 1][0] if i + 1 < len(marks) else float('inf')
            if start <= page_num < next_start:
                assigned = name
                break
        if assigned and not page_data.get(field):
            page_data[field] = assigned
    return pages_data


def _generate_book_name(first_page_text, toc_entries) -> Optional[str]:
    """A short title from the local LLM (moved from Nunba; thinking now off)."""
    from core.constants import LLM_THINKING_OFF_KWARGS
    from core.http_pool import LLM_COMPLETION_TIMEOUT, pooled_post
    from core.port_registry import get_local_llm_url

    topic_names = [e.get('title', '') for e in (toc_entries or [])[:20]]
    prompt = (
        f"Based on the following information, suggest a short book title (max 10 words).\n"
        f"Topics: {', '.join(str(t) for t in topic_names)}\n"
        f"First page text: {first_page_text[:500]}\n\n"
        f"Respond with ONLY the book title, nothing else."
    )
    try:
        resp = pooled_post(
            get_local_llm_url().rstrip('/') + '/chat/completions',
            json={
                "model": "qwen",
                "messages": [{"role": "user", "content": prompt}],
                "chat_template_kwargs": dict(LLM_THINKING_OFF_KWARGS),
                "max_tokens": 50,
                "temperature": 0.3,
            },
            timeout=LLM_COMPLETION_TIMEOUT,
        )
        if resp.status_code == 200:
            choice = (resp.json().get('choices') or [{}])[0]
            name = ((choice.get('message') or {}).get('content') or '').strip()
            return name.strip('"').strip()[:300] or None
        logger.debug(f"Book name generation returned HTTP {resp.status_code}")
    except Exception as e:
        logger.debug(f"Book name generation failed: {e}")
    return None


# ── Storage (BookFile / BookPageLayout via db_session) ────────────────

def _utcnow() -> datetime:
    return datetime.utcnow()


def _clip(value, limit):
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


#: Waits before each retry of a write that met another writer's lock. The
#: shared engine gives up on a busy database after 3 s on purpose
#: (integrations/social/models.py: daemon ticks must fail fast); a book write
#: is worth more patience, and on a live node the lock is routine.
_LOCK_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0)
#: What lock contention looks like: SQLite, and MySQL/InnoDB behind HEVOLVE_DB_URL.
_LOCK_ERRORS = ('database is locked', 'deadlock', 'lock wait timeout')


def _retry_on_lock(fn):
    """Retry a write that met another writer's lock; any other error raises at once.

    The pattern of security/immutable_audit_log.py's own-session retry: the
    shared engine stays fast-fail for everyone else, only this writer is more
    patient. Measured 2026-09-13 on a node booted through the real
    init_social: the social daemons held SQLite's write lock past the 3 s busy
    timeout, the book's final save raised "database is locked", and the
    failure could not be recorded either -- the row sat in 'processing'.
    """
    @functools.wraps(fn)
    def retrying(*args, **kwargs):
        from sqlalchemy.exc import OperationalError
        for delay in (*_LOCK_RETRY_DELAYS, None):
            try:
                return fn(*args, **kwargs)
            except OperationalError as e:
                if delay is None or not any(m in str(e).lower() for m in _LOCK_ERRORS):
                    raise
                logger.info(f"{fn.__name__}: the book library is busy; retrying in {delay}s")
                time.sleep(delay)
    return retrying


@_retry_on_lock
def _register(user_id, filename, directory, request_id) -> int:
    from integrations.social.models import BookFile, db_session
    with db_session() as db:
        now = _utcnow()
        row = BookFile(user_id=str(user_id)[:64], filename=str(filename)[:255],
                       directory=str(directory)[:1024],
                       request_id=str(request_id or '')[:128],
                       status='pending', created_date=now, updated_at=now)
        db.add(row)
        db.flush()
        return int(row.file_id)


@_retry_on_lock
def _update_row(file_id, **fields) -> bool:
    from integrations.social.models import BookFile, db_session
    with db_session() as db:
        row = db.get(BookFile, int(file_id))
        if row is None:
            return False
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = _utcnow()
        return True


def _mark_failed(file_id, reason) -> None:
    """Record a failed parse on the DURABLE row; a job record is gone on restart."""
    if not file_id:
        return
    try:
        _update_row(file_id, status='failed', error=str(reason)[:2000])
    except Exception as e:
        logger.warning(f"book {file_id}: could not record the failure ({reason}): {e}")
        return
    logger.warning(f"book {file_id} failed: {reason}")


@_retry_on_lock
def _save(file_id, pages, whole_text, book_name, total_pages) -> None:
    """Store the parsed pages (replacing any from an earlier attempt) and
    complete the row, in one SHORT transaction.

    The rows are built first and inserted in one executemany, so the write
    lock is held for milliseconds even for a long book and the daemons writing
    beside it are not starved (integrity_service.py's lesson: a smaller write
    interleaves with the other writers instead of blocking them).
    """
    from sqlalchemy import insert

    from integrations.social.models import BookFile, BookPageLayout, db_session
    now = _utcnow()
    rows = []
    for page in pages:
        common = {'file_id': int(file_id),
                  'page_number': int(page.get('page_number') or 0),
                  'chapter_name': _clip(page.get('chapter_name'), 300),
                  'topic_name': _clip(page.get('topic_name'), 300),
                  'page_type': _clip(page.get('page_type') or 'content', 64),
                  'created_date': now}
        elements = [e for e in (page.get('elements') or []) if isinstance(e, dict)]
        if not elements:
            rows.append({**common, 'layout_number': 1, 'num_layouts_per_page': 1,
                         'passage': str(page.get('text') or ''),
                         'element_type': 'full_page', 'label': None})
            continue
        for idx, elem in enumerate(elements, 1):
            rows.append({**common, 'layout_number': idx,
                         'num_layouts_per_page': len(elements),
                         'passage': str(elem.get('content') or ''),
                         'element_type': _clip(elem.get('type') or 'paragraph', 64),
                         'label': _clip(elem.get('type'), 128)})
    with db_session() as db:
        row = db.get(BookFile, int(file_id))
        if row is None:
            raise BookParseError('the book record disappeared while it was being parsed')
        db.query(BookPageLayout).filter(
            BookPageLayout.file_id == int(file_id)).delete(synchronize_session=False)
        if rows:
            db.execute(insert(BookPageLayout), rows)
        row.text_response = whole_text
        row.book_name = _clip(book_name, 300)
        row.total_pages = int(total_pages)
        row.status = 'completed'
        row.error = None
        row.updated_at = _utcnow()


def _stall_reason(row) -> Optional[str]:
    """Why a 'pending'/'processing' row is dead, or None while it is alive.

    A running parse touches its row at least every _TOUCH_EVERY_S, so one
    untouched for STALL_SECONDS belongs to a parse that hung or died with its
    process -- and would otherwise show 'pending' forever.
    """
    if (row.status in _LIVE and row.updated_at is not None
            and (_utcnow() - row.updated_at).total_seconds() > STALL_SECONDS):
        return f'stalled: no page finished for {STALL_SECONDS // 60} minutes'
    return None


def _book_dicts(rows):
    """Rows as dicts, a dead parse shown as 'failed'; plus the dead ones' ids."""
    books, stalled = [], []
    for row in rows:
        d = row.to_dict()
        reason = _stall_reason(row)
        if reason:
            d.update(status='failed', error=reason)
            stalled.append(row.file_id)
        books.append(d)
    return books, stalled


@_retry_on_lock
def _record_stalled(file_ids) -> None:
    """Record dead parses as failed -- each only if it is STILL dead, so a
    parse that finished a moment ago is never overwritten."""
    from integrations.social.models import BookFile, db_session
    with db_session() as db:
        for file_id in file_ids:
            row = db.get(BookFile, int(file_id))
            reason = _stall_reason(row) if row is not None else None
            if reason:
                row.status, row.error, row.updated_at = 'failed', reason, _utcnow()
                logger.warning(f"book {file_id}: {reason}")


def _resolve_stalled(file_ids) -> None:
    """A write on a read path is best-effort (api.py's view-counter lesson):
    the library still answers, stalled books shown as failed, when the record
    cannot be written just now; the next read tries again."""
    if not file_ids:
        return
    try:
        _record_stalled(file_ids)
    except Exception as e:
        logger.warning(f"could not record stalled book(s) {file_ids} as failed: {e}")


def list_books(user_id=None, limit=100) -> List[dict]:
    """A user's books, newest first (every user's when user_id is None/'')."""
    from integrations.social.models import BookFile, db_session
    with db_session(commit=False) as db:
        q = db.query(BookFile)
        if user_id not in (None, ''):
            q = q.filter(BookFile.user_id == str(user_id))
        books, stalled = _book_dicts(
            q.order_by(BookFile.file_id.desc()).limit(int(limit)).all())
    _resolve_stalled(stalled)
    return books


def get_book(file_id) -> Optional[dict]:
    from integrations.social.models import BookFile, db_session
    with db_session(commit=False) as db:
        row = db.get(BookFile, int(file_id))
        books, stalled = _book_dicts([row] if row is not None else [])
    _resolve_stalled(stalled)
    return books[0] if books else None


def get_layouts(file_id, page_number=None) -> List[dict]:
    """A book's stored page elements in reading order (optionally one page)."""
    from integrations.social.models import BookPageLayout, db_session
    with db_session(commit=False) as db:
        q = db.query(BookPageLayout).filter(BookPageLayout.file_id == int(file_id))
        if page_number is not None:
            q = q.filter(BookPageLayout.page_number == int(page_number))
        rows = q.order_by(BookPageLayout.page_number, BookPageLayout.layout_number).all()
        return [r.to_dict() for r in rows]


def _existing(user_id, filename) -> Optional[dict]:
    """A live or completed book already made from this stored file, if any."""
    from integrations.social.models import BookFile, db_session
    with db_session(commit=False) as db:
        books, stalled = _book_dicts(
            db.query(BookFile)
            .filter(BookFile.user_id == str(user_id)[:64],
                    BookFile.filename == str(filename)[:255])
            .order_by(BookFile.file_id.desc()).all())
    _resolve_stalled(stalled)
    return next((b for b in books
                 if b['status'] in ('pending', 'processing', 'completed')), None)


# ── Jobs (in-memory, bounded) and progress ────────────────────────────

_jobs: 'OrderedDict[str, dict]' = OrderedDict()
_jobs_lock = threading.Lock()


def _new_job(file_id, user_id, request_id, filename) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            'job_id': job_id, 'file_id': file_id, 'user_id': str(user_id),
            'request_id': request_id, 'filename': filename,
            'status': 'queued', 'total_pages': 0, 'progress': 0,
            'error': None, 'result': None, 'updated': time.time(),
        }
        while len(_jobs) > _JOB_CAP:
            _jobs.popitem(last=False)
    return job_id


def _update_job(job_id, **fields) -> None:
    if not job_id:
        return
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)
            job['updated'] = time.time()


def _job_for_file(file_id) -> Optional[str]:
    with _jobs_lock:
        for job in reversed(list(_jobs.values())):
            if job['file_id'] == file_id:
                return job['job_id']
    return None


def get_job(job_id) -> Optional[dict]:
    """A copy of the job record, with a stalled job resolved to 'failed'."""
    with _jobs_lock:
        job = dict(_jobs[job_id]) if job_id in _jobs else None
    if (job is not None and job['status'] in ('queued', 'processing')
            and time.time() - job['updated'] > STALL_SECONDS):
        reason = f'stalled: no page finished for {STALL_SECONDS // 60} minutes'
        _update_job(job_id, status='failed', error=reason)
        _mark_failed(job['file_id'], reason)
        job.update(status='failed', error=reason)
    return job


def _progress(request_id, filename, file_id, message, percentage=None,
              page_number=None) -> dict:
    """One progress message, as the clients read it off the book-parsing topic."""
    payload = {'request_id': request_id, 'bot_type': 'Agent',
               'filename': filename, 'file_id': file_id, 'text': [message]}
    if percentage is not None:
        payload['percentage'] = int(percentage)
    if page_number is not None:
        payload['page_number'] = page_number
    return payload


def _publish(user_id, payload) -> bool:
    """Progress to the user's clients, through the MessageBus: this node's
    own subscribers and the desktop's SSE, the user's other devices over
    PeerLink, and Crossbar on com.hertzai.bookparsing.{user_id}.

    Not through safe_hartos_attr('publish_async'): that finds the entry
    module only under its own name, and `python hart_intelligence_entry.py`
    (the Docker CMD) runs it as __main__.

    Each message carries its own msg_id. The web client drops a repeat of a
    message id and, lacking one, keys on request_id -- which every message of
    one book shares -- so every page after the first would be dropped.
    """
    try:
        from core.peer_link.message_bus import get_message_bus
        get_message_bus().publish(PROGRESS_TOPIC,
                                  dict(payload, msg_id=uuid.uuid4().hex[:16]),
                                  user_id=str(user_id))
        return True
    except Exception as e:
        logger.warning(f"book progress not published: {e}")
        return False


def _make_step(job_id, file_id, user_id, request_id, filename, log) -> Callable:
    """The one progress callback: log line, job record, client event, row touch."""
    last_touch = [time.monotonic()]

    def step(message, percentage=None, page_number=None, total_pages=None):
        log.append(message)
        logger.info(f"book {file_id}: {message}")
        fields = {}
        if page_number is not None:
            fields['progress'] = page_number
        if total_pages is not None:
            fields['total_pages'] = total_pages
        _update_job(job_id, **fields)
        _publish(user_id, _progress(request_id, filename, file_id, message,
                                    percentage, page_number))
        now = time.monotonic()
        if file_id is not None and now - last_touch[0] >= _TOUCH_EVERY_S:
            last_touch[0] = now
            try:
                _update_row(file_id, status='processing')
            except Exception as e:
                logger.warning(f"book {file_id}: could not refresh its record: {e}")

    return step


# ── The one sequence ──────────────────────────────────────────────────

def _clear_page_images(file_id) -> None:
    """Drop images left under this book's id by an earlier parse or an earlier
    database (ids start over after a reset), so no other book's page is served."""
    for old in page_image_path(file_id, 1).parent.glob('page_*'):
        try:
            old.unlink()
        except OSError as e:
            logger.warning(f"book {file_id}: could not remove the stale {old.name}: {e}")


def _parse(file_id, pdf_path, step, image_path=None) -> dict:
    """Read every page: its text layer, its image, and the vision model's
    reading when it answers. Stores nothing -- _run does that."""
    image_path = image_path or (lambda n: page_image_path(file_id, n))
    step('Opening the PDF…', 2)
    with _PDFIUM_LOCK:
        pdf = _open(pdf_path)
        try:
            n = len(pdf)
            outline = _outline(pdf)
            meta_title = _metadata_title(pdf)
        except BaseException:
            pdf.close()
            raise
    results, texts, vlm_toc = [], [], []
    try:
        if n == 0:
            raise BookParseError('the PDF has no pages')
        if n > MAX_PAGES:
            raise BookParseError(f'the PDF has {n} pages; at most {MAX_PAGES} can be parsed')
        step(f'{n} pages found', 5, total_pages=n)
        if file_id is not None:
            _clear_page_images(file_id)

        vlm_on, vlm_ok, vlm_errors = True, 0, 0
        for i in range(n):
            page_num = i + 1
            layer, picture = _read_page(pdf, i, page_num, file_id)
            image = None
            if picture is not None:
                try:
                    image = _write_jpeg(picture, image_path(page_num))
                except OSError as e:
                    logger.warning(f"book {file_id}: page {page_num} image not written: {e}")

            data = None
            if vlm_on and image is not None:
                data = _parse_page_via_vision(page_num, str(image))
                if data.get('error'):
                    vlm_errors += 1
                    if vlm_ok == 0 and vlm_errors >= _VLM_GIVE_UP_AFTER:
                        vlm_on = False
                        step('The vision model did not answer; reading the rest '
                             'of the book from its text layer')
                else:
                    vlm_ok += 1
            if not data or data.get('error') or not str(data.get('text') or '').strip():
                data = {'page_number': page_num, 'page_type': 'content',
                        'text': layer, 'elements': []}
            results.append(data)
            texts.append(str(data.get('text') or ''))
            vlm_toc.extend(data.get('toc_entries') or [])
            step(f'Page {page_num}/{n} read', 5 + page_num / n * 85, page_num)
    finally:
        with _PDFIUM_LOCK:
            pdf.close()

    if not any(t.strip() for t in texts):
        raise BookParseError(
            'no text could be read from this PDF: it looks scanned, and the '
            'vision model did not answer')

    step('Finding chapters…', 92)
    chapters = [e for e in outline if e['level'] == 0] or vlm_toc
    topics = [e for e in outline if e['level'] == 1]
    results = assign_chapters(results, chapters)
    if topics:
        results = assign_chapters(results, topics, field='topic_name', bounds=chapters)

    step('Naming the book…', 95)
    book_name = _generate_book_name(texts[0][:500], chapters) or meta_title
    return {'file_id': file_id, 'book_name': book_name, 'total_pages': n,
            'chapters': len(chapters), 'toc': chapters,
            'pages': results, 'whole_text': '\n\n'.join(texts)}


def _run(file_id, pdf_path, user_id, request_id, job_id=None, log=None,
         keep_read=False) -> dict:
    """Read the book, then store it.

    keep_read is the agent's synchronous read: the text comes back even when
    the library cannot store it ('stored': False, with 'store_error'), as the
    agent's PDF reader always did, and the row is recorded as failed. A
    background parse fails instead.
    """
    log = log if log is not None else []
    name = Path(pdf_path).name
    step = _make_step(job_id, file_id, str(user_id), request_id, name, log)
    _update_job(job_id, status='processing')
    try:
        _update_row(file_id, status='processing')
    except Exception as e:
        logger.warning(f"book {file_id}: could not mark it processing: {e}")
    try:
        result = _parse(file_id, pdf_path, step)
        step('Saving…', 98)
        try:
            _save(file_id, result['pages'], result['whole_text'],
                  result['book_name'], result['total_pages'])
        except Exception as e:
            if not keep_read:
                raise
            reason = f'the book was read but could not be saved: {e}'
            _mark_failed(file_id, reason)
            log.append(f'Not saved: {reason}')
            result.update(stored=False, store_error=reason, progress_log=log)
            return result
    except Exception as e:
        reason = str(e) if isinstance(e, BookParseError) else f'unexpected error: {e}'
        if not isinstance(e, BookParseError):
            logger.exception(f"book {file_id}: parse crashed")
        _mark_failed(file_id, reason)
        _update_job(job_id, status='failed', error=reason)
        log.append(f'Failed: {reason}')
        _publish(user_id, _progress(request_id, name, file_id,
                                    f'Could not read this book: {reason}'))
        if isinstance(e, BookParseError):
            raise
        raise BookParseError(reason) from e
    step(f"Done: {result['total_pages']} pages, {result['chapters']} chapters", 100)
    _update_job(job_id, status='completed', total_pages=result['total_pages'],
                progress=result['total_pages'],
                result={'file_id': file_id, 'book_name': result['book_name'],
                        'total_pages': result['total_pages']})
    result.update(stored=True, progress_log=log)
    return result


def parse_book(pdf_path, user_id, request_id='', log=None) -> dict:
    """Parse synchronously, on the caller's thread, for an agent that waits for
    the book's text; a request handler must use start_parse().

    The text comes back even when the library cannot store the book
    (result['stored'] is False, with 'store_error').
    """
    pdf_path = Path(pdf_path)
    try:
        file_id = _register(user_id, pdf_path.name, pdf_path.parent, request_id)
    except Exception as e:
        reason = f'the book library is unavailable ({e})'
        logger.warning(f"reading {pdf_path.name} without storing it: {reason}")
        return _read_unstored(pdf_path, user_id, request_id, log, reason)
    return _run(file_id, pdf_path, user_id, request_id, log=log, keep_read=True)


def _read_unstored(pdf_path, user_id, request_id, log, reason) -> dict:
    """Read a book with nowhere to store it. Its page images go to a scratch
    dir, for the vision model only, and are removed after."""
    log = log if log is not None else []
    scratch = Path(tempfile.mkdtemp(prefix='book_read_'))
    try:
        step = _make_step(None, None, str(user_id), request_id, pdf_path.name, log)
        result = _parse(None, pdf_path, step,
                        image_path=lambda n: scratch / f'page_{n}.jpg')
    finally:
        for leftover in scratch.glob('*'):
            try:
                leftover.unlink()
            except OSError as e:
                logger.warning(f"could not remove the scratch page {leftover}: {e}")
        try:
            scratch.rmdir()
        except OSError as e:
            logger.warning(f"could not remove the scratch dir {scratch}: {e}")
    log.append(f'Not saved: {reason}')
    result.update(stored=False, store_error=reason, progress_log=log)
    return result


def start_parse(pdf_path, user_id, request_id='') -> dict:
    """Start a parse in the background and return at once.

    A file already parsed, or being parsed, for this user is not parsed twice:
    an upload route and the parse_book_pdf tool both start parses, often for
    the same upload. One already read goes straight to 100% on the book the
    user already has, as central answered a repeat upload
    (pipeline/upload_api.py); one still being read reports its own progress.
    """
    pdf_path = Path(pdf_path)
    existing = _existing(user_id, pdf_path.name)
    if existing is not None:
        if existing['status'] == 'completed':
            _publish(user_id, _progress(request_id, pdf_path.name, existing['file_id'],
                                        'Already in your library', 100))
        return {'job_id': _job_for_file(existing['file_id']),
                'file_id': existing['file_id'], 'status': existing['status'],
                'existing': True}
    file_id = _register(user_id, pdf_path.name, pdf_path.parent, request_id)
    job_id = _new_job(file_id, user_id, request_id, pdf_path.name)
    threading.Thread(target=_worker,
                     args=(job_id, file_id, str(pdf_path), str(user_id), request_id),
                     daemon=True, name=f'book-parse-{file_id}').start()
    return {'job_id': job_id, 'file_id': file_id, 'status': 'queued'}


def _worker(job_id, file_id, pdf_path, user_id, request_id) -> None:
    try:
        _run(file_id, pdf_path, user_id, request_id, job_id=job_id)
    except BookParseError:
        pass  # already recorded on the row and the job, and published
    except Exception:
        logger.exception(f"book {file_id}: background parse crashed")
