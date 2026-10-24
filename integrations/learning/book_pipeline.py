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

Each page image goes to the node's vision backend -- get_vision_backend() in
integrations/vision/lightweight_backend.py, the one camera, screen and media
captions already use -- through its read_document(). One VLM call per page
does OCR, layout, tables and figures, where the cloud pipeline used six
models. Where the Qwen3.5-0.8B caption model is installed that backend is the
caption server, so reading a book does not occupy the main model the user
chats with. A page that backend cannot read, or does not answer for, goes to
the node's own main model, as every page did before
(lightweight_backend.get_document_readers). When neither answers, the page
falls back to its own text layer, so a node with no vision model still
produces a readable book. Chapters come from the PDF's outline when it has
one (it points at real page indices), else from the table-of-contents pages
the model read.

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


#: save_pdf's default cap: MAX_PDF_BYTES as it is when save_pdf is called (a
#: default argument would freeze the value it had at import).
_MAX_PDF_BYTES_AT_CALL = object()


def save_pdf(data: bytes, original_name: str = '',
             max_bytes=_MAX_PDF_BYTES_AT_CALL) -> Path:
    """Validate and store an uploaded or downloaded PDF under uploads/files.

    `max_bytes` None: no size cap -- a local caller's own book (owner
    2026-10-10, "local need not have a cap").  A download (fetch_pdf) and a
    remote upload keep MAX_PDF_BYTES.
    """
    from werkzeug.utils import secure_filename
    if max_bytes is _MAX_PDF_BYTES_AT_CALL:
        max_bytes = MAX_PDF_BYTES
    if not data:
        raise BookParseError('the file is empty')
    if max_bytes is not None and len(data) > max_bytes:
        raise BookParseError(
            f'the file is larger than {max_bytes // (1024 * 1024)} MB')
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

    The link passes security.sanitize.validate_url first: http(s), and not a
    cloud metadata endpoint. A link on the user's own network or this machine
    is an ordinary place for a book and is fetched.
    """
    import requests

    from core.http_pool import pooled_get
    from security.sanitize import validate_url
    try:
        url = validate_url(url)
    except ValueError as e:
        raise BookParseError(f'the PDF link was refused ({e})') from e
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
    try:
        return save_pdf(bytes(data), name or 'book.pdf')
    except OSError as e:
        # The operator's copy: the path and the traceback, on this machine.
        logger.warning("book: a downloaded PDF could not be stored", exc_info=True)
        # Rebuilt rather than blanked: clearing e.filename leaves a trailing
        # ": None" where the path was. Only strerror is replaced, so the errno
        # and the subclass survive -- when errno is set; an errno-less
        # FileNotFoundError(path) comes back as a plain OSError -- and
        # this stays an OSError rather than becoming a BookParseError -- a
        # storage failure is not a bad request.
        #
        # The sentence names THIS node on purpose. Without that, a failure to
        # write here reaches the agent as "[Errno 2] No such file or
        # directory" about the LINK, whose PDF arrived perfectly well.
        reason = ('this node could not store the download '
                  f'({os_error_reason(e, "no reason reported")})')
        if e.errno is None:
            raise OSError(reason) from e
        raise OSError(e.errno, reason) from e


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
        # PDFium's own words carry no path -- measured 2026-10-25 on a non-PDF
        # and on an empty file, both "Failed to load document (PDFium: Data
        # format error)." -- so this one is repeated as it stands.
        raise BookParseError(f'this file could not be read as a PDF ({e})') from e
    except OSError as e:
        # The operator's copy: the path and the traceback, on this machine.
        logger.warning("book: %s could not be opened", pdf_path, exc_info=True)
        # pypdfium2 raises FileNotFoundError(path) with strerror unset, so the
        # default below is what a person actually reads on this branch. It
        # states the fact and gives no advice to upload again, because the same
        # branch serves a link the caller named by URL and never uploaded.
        raise BookParseError(
            'the PDF could not be opened '
            f'({os_error_reason(e, "the file is no longer on this node")})') from e


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


# ── One page through the node's vision backend ───────────────────────

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


def _page_readers() -> list:
    """How this node reads a page, best first
    (integrations.vision.lightweight_backend.get_document_readers). [] when
    the vision package will not load here -- the same as no model: pages are
    read from their text layer, and the book is not failed."""
    try:
        from integrations.vision.lightweight_backend import get_document_readers
        return get_document_readers()
    except Exception as e:
        logger.warning(f"vision unavailable for book pages: {e}")
        return []


def _parse_page_via_vision(page_num: int, image_path: str, readers) -> dict:
    """One VLM read -- OCR + layout + tables + figures -- of one page image,
    from the first reader that answers."""
    unavailable = {"page_number": page_num, "page_type": "unknown", "text": "",
                   "elements": [], "error": "Vision inference unavailable"}
    try:
        image = Path(image_path).read_bytes()
    except OSError as e:
        logger.warning(f"page {page_num}: its image could not be read back: {e}")
        return unavailable
    result = None
    for read in readers:
        try:
            result = read(image, _PAGE_PROMPT)
        except Exception as e:
            logger.warning(f"page {page_num}: a vision reader failed: {e}")
            continue
        if result:
            break
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


def is_lock_error(exc) -> bool:
    """True when ``exc`` is another writer holding the lock, not a bad book.

    One spelling, four callers: ``_retry_on_lock`` reads it to decide whether
    to wait, and the upload route and the worker's two failure paths read it to
    decide what to tell the person. A held lock is transient and the file is
    fine, so those answers must not drift apart.

    Only a database error can be a lock. The worker's handler is given every
    exception a whole parse can raise, and most carry no ``.orig``, so without
    this gate the fallback below reads the *message* -- and
    ``BookParseError('the PDF could not be opened (...\\OS_Deadlocks.pdf)')``
    was classified as lock contention, telling a person to re-upload a file
    that can never parse (measured 2026-10-18, the defect this gate closes).
    That exact text is historical: _open no longer repeats the path (74b39d1a4).
    The gate still earns its place, because a book's own complaint can carry a
    lock word by other routes -- PDFium's wording, or the book's own name.

    Reads the DBAPI error, not the rendered statement. SQLAlchemy's
    ``StatementError.__str__`` appends ``[SQL: ...]`` and ``[parameters: ...]``,
    and for ``_register`` those carry the uploaded filename, the uploads
    directory and the caller-supplied request_id -- so matching the whole
    string let a book called ``deadlock.pdf`` be read as lock contention and
    answered "try again" when the real failure (a missing table, say) was
    permanent. Measured 2026-10-18 on the real route with the library tables
    dropped: physics.pdf answered 500 and deadlock.pdf answered 503.

    ``.orig`` present but None means no driver error, so no lock: keep
    ``getattr(exc, 'orig', exc)`` rather than tidying it to
    ``getattr(exc, 'orig', None) or exc``, which would fall back to the
    rendered statement for that shape and bring the filename back with it.
    A driver error whose own ``__str__`` raises is not a lock either, and
    must not raise from here: this runs inside the route's ``except``, where
    raising would cost the person the JSON answer and the operator the log
    line (SQLAlchemy guards the same call in ``DBAPIError.__init__``).
    """
    from sqlalchemy.exc import SQLAlchemyError
    if not isinstance(exc, SQLAlchemyError):
        return False
    try:
        text = str(getattr(exc, 'orig', exc)).lower()
    except Exception as unreadable:
        logger.debug("book library: a driver error could not be read, so it "
                     "is not treated as a lock (%r)", unreadable)
        return False
    return any(m in text for m in _LOCK_ERRORS)


def os_error_reason(exc, default) -> str:
    """An OS error's own words, with no path among them.

    Reads ``strerror`` and nothing else. ``str(exc)`` is never safe here:
    OSError renders ``filename`` into it through repr, so on Windows the
    backslashes double and a substring check for the real path quietly cannot
    fail; and a hand-raised OSError carries its whole message in ``args[0]``,
    where a path sits just as easily. ``strerror`` is unset for both of those
    shapes, which is what ``default`` is for -- reporting it unguarded tells a
    person "None".

    ``default`` is the caller's sentence because the two callers describe
    different failures: a book that is no longer on this node, and a download
    this node could not store. One rule, two sentences, each chosen at its own
    call site rather than drifting apart.

    driver_text is the companion for a database error. Both exist because
    these messages LEAVE the node: book_tools puts a failed book's error into
    an agent's tool output, which becomes a model's prompt and that model can
    be a cloud one, and api_books hands a job's error to a client that may be
    a phone (H-0715 scrubs what leaves, never the operator's own log).
    """
    return exc.strerror or default


def driver_text(exc) -> str:
    """The driver's own words for ``exc``, safe to repeat to a person.

    is_lock_error's companion: that one reads ``.orig`` to decide whether a
    failure is transient, this one reads it to decide what may be said out
    loud. It is ``StatementError.__str__`` that appends ``[SQL: ...]`` and
    ``[parameters: ...]``, so only that class is unwrapped -- every other
    exception already says only what it was given.

    The lock half of this was closed on 2026-10-18 by routing locks to
    _BUSY_NOT_SAVED (see the comment below for the leak measured then). The
    rest stayed open: classifying by transience left the RENDERING of
    everything else untouched, and a ``disk I/O error`` is not a lock, so it
    took the ``else`` branch and printed the statement in full. That error is
    not hypothetical here -- this node raised it from 16:26 to 17:37 on
    2026-10-22, with the disk at zero bytes free.

    The operator loses nothing -- but ONLY because every caller logs the
    exception itself beside this call, with ``logger.exception`` or
    ``exc_info=True``. Sanitising a person's reason without that leaves the
    operator the sanitised sentence and nothing else: no statement, no
    parameters, no traceback. That is precisely what acd5f5a42 did on the
    agent's synchronous path -- a disk I/O error became four words -- and
    what its review caught. If a new caller is added, log the exception
    there too; this function deliberately cannot do it for you, because it
    does not know whether its answer is headed for a person or a log.

    Public, and deliberately here rather than in a shared module: every
    caller today is inside ``integrations/learning`` (the three boundaries
    below and ``book_tools.parse_book_pdf``), so a shared home would have one
    family of users. If a caller outside books ever needs it, is_lock_error
    and this move together to sit beside ``db_session`` -- in one step, never
    as a second copy (hartos-70, register consult 2026-10-24, H-0715).
    """
    from sqlalchemy.exc import StatementError
    if not isinstance(exc, StatementError):
        return str(exc)
    # .orig is None for a StatementError raised with no driver error under it;
    # falling back to str(exc) there would bring the statement straight back.
    try:
        return str(exc.orig) if exc.orig is not None else exc.__class__.__name__
    except Exception as unreadable:
        logger.debug("book library: a driver error could not be read (%r)", unreadable)
        return exc.__class__.__name__


#: What a person is told when the write lock outlasted the ladder mid-parse.
#: Never the driver's text: SQLAlchemy renders ``[SQL: ...]`` and
#: ``[parameters: ...]`` into it, and for ``_save`` those are the executemany
#: payload -- the book's own page text -- plus the schema, handed to anyone who
#: can poll the status route. Measured 2026-10-18, a status answer read in full:
#: "unexpected error: (sqlite3.OperationalError) database is locked [SQL: DELETE
#: FROM page_layouts WHERE page_layouts.file_id = ?] [parameters: (1,)]".
#: "try again", not "upload again": _run serves a link the agent followed as
#: well as a file a person uploaded, and a link reader has nothing to upload.
_BUSY_NOT_SAVED = ('the book library was busy, so this book did not finish; '
                   'try again in a moment')


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
                if delay is None or not is_lock_error(e):
                    raise
                logger.info(f"{fn.__name__}: the book library is busy; retrying in {delay}s")
                time.sleep(delay)
    return retrying


#: Both pdf_files.user_id and users.id are String(64), and users.id defaults
#: to a uuid4 -- so an id longer than this is not a reader this node can have.
READER_ID_MAX = 64


def _book_key(user_id, filename):
    """How a book's reader and name are stored, in one spelling.

    Asymmetric on purpose, and the asymmetry IS the rule:

    The READER is identity, so it is never shortened. _register refuses an id
    the column cannot hold rather than truncating it, so no stored row carries
    a lossy reader id and every filter can compare the id exactly. Clipping
    identity on the READ side is what made 74b39d1a4 a leak: two readers whose
    ids share the column's first 64 characters collapsed onto one key, so
    list_books merged their libraries -- and get_layouts keys on file_id with
    no owner check, so one reader's pages would have reached the other's agent
    prompt. Measured on review: bob's library showed alice's book. Matching a
    read to a lossy write is never the fix; the write stops losing.

    The NAME is data, so it still is: a 300-character filename is clipped to
    its column exactly as the row has always done, because losing the tail of
    a title harms nobody and refusing the upload would. A job keyed any other
    way cannot be matched to its book, and a long name then slips past the
    match in _job_for_file -- a dead parse reading as live again.

    Pure, and deliberately above the decorator: _retry_on_lock belongs to
    _register, the write. Inserting this helper between the two gave the
    ladder to a function that does no I/O and took it from the insert that
    starts every parse, with every test still green (cc66abd80).
    """
    return str(user_id), str(filename)[:255]


@_retry_on_lock
def _register(user_id, filename, directory, request_id) -> int:
    from integrations.social.models import BookFile, db_session
    key_user, key_name = _book_key(user_id, filename)
    # Refused, not truncated, and before a write transaction is opened. Storing
    # a shortened id is what let two readers share one stored key (the leak in
    # 74b39d1a4, found on review); users.id is String(64), so an id longer than
    # the column is not a reader this node could have in the first place.
    if len(key_user) > READER_ID_MAX:
        raise BookParseError(
            f'this node cannot file a book for that account '
            f'(its id is longer than {READER_ID_MAX} characters)')
    with db_session() as db:
        now = _utcnow()
        row = BookFile(user_id=key_user, filename=key_name,
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

    Two ways a parse dies, and the row can be wrong about both.

    Its job already said so.  The worker records a failure on the job and on
    the row, but on a held lock the row's write is refused by the very lock
    that killed the parse -- so the row keeps 'processing' with a *fresh*
    updated_at and the timeout below cannot see it for STALL_SECONDS.  For
    those 30 minutes _existing read that dead parse as live and refused to
    start another, which made the 503's advice ("try again in a moment")
    false for the one case it is aimed at: the same stored file, retried.
    Measured 2026-10-18; the advice shipped 005372509, this is its other half.

    Or it left no word: a running parse touches its row at least every
    _TOUCH_EVERY_S, so one untouched for STALL_SECONDS hung or died with its
    process -- and would otherwise show 'pending' forever.

    A row with NO job is not read as dead.  parse_book stores a row and never
    makes a job, and start_parse registers the row a moment before the job
    exists, so "no job" is the ordinary shape of a live parse as much as of
    one lost to a restart; only the timeout may call those dead.  Resuming a
    parse across a restart is H-0423's question, not this one's.
    """
    if row.status not in _LIVE:
        return None
    failed = _failed_job_reason(row)
    if failed:
        return failed
    if (row.updated_at is not None
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


def _record_stalled(file_ids) -> None:
    """Record dead parses as failed -- each only if it is STILL dead, so a
    parse that finished a moment ago is never overwritten.

    No retry ladder, deliberately: this is the one write on a READ path, and
    _resolve_stalled below already promises the library answers without it
    ("the next read tries again").  The ladder came off because it made that
    promise false.  _stall_reason calls a row dead as soon as its job has
    failed -- no longer only after STALL_SECONDS -- so the hot window of a
    lock-killed parse now reaches this write, and while it was patient every
    library read sat out the holder with it: 20.48 s for list_books where
    1faa4b6a0's parent answered in 0.00 s, and 40.97 s for a retry through
    start_parse, which meets this ladder in _existing and then _register's
    own (hevolve-cc12's review of 1faa4b6a0, finding A1).

    The patient ladder stays where a write must land: _register, _update_row
    (so _mark_failed keeps it) and _save.
    """
    from integrations.social.models import BookFile, db_session
    with db_session() as db:
        # Best-effort means not waiting, either.  Dropping the ladder left one
        # attempt, but one attempt still sat out the engine's
        # busy_timeout=3000, so a library read inside a lock window paid about
        # 3.4 s -- measured on 179a11815: list_books 3.38 s, down from 20.48 s
        # but not free (hevolve-cc12's review of it, finding 2).  Guarded by
        # dialect because HEVOLVE_DB_URL can point at MySQL, where this is not
        # valid SQL.
        #
        # Put back in the `finally`, and NOT left to the connection's lifetime.
        # c53ad80f9's comment claimed "the pragma dies with it" because file
        # SQLite runs on NullPool; that is true of the file engine and false of
        # the in-memory one, which uses StaticPool and kept the zero for every
        # later session (measured on review).  Either way the containment would
        # have rested on a pool chosen in another module and pinned by no test,
        # and if it ever leaked it would hand the no-wait to _register, _save
        # and _update_row -- the three writes that MUST land.  A pooled probe
        # showed _update_row then giving up after 3.75 s instead of waiting out
        # its ~20.5 s ladder.
        sqlite = db.get_bind().dialect.name == 'sqlite'
        # The DBAPI connection, not the session: the reset below has to work
        # after a refused flush, and by then the session needs a rollback.
        raw = db.connection().connection if sqlite else None
        if sqlite:
            _set_patience(raw, 0)
        try:
            for file_id in file_ids:
                row = db.get(BookFile, int(file_id))
                reason = _stall_reason(row) if row is not None else None
                if reason:
                    row.status, row.error, row.updated_at = 'failed', reason, _utcnow()
                    logger.warning(f"book {file_id}: {reason}")
            # Flushed HERE, while the no-wait still applies: SQLite takes the
            # write lock on the first write, so a flush after the pragma was
            # put back would be the 3 s wait all over again.  The commit that
            # follows holds that lock already and waits for nobody.
            db.flush()
        finally:
            if sqlite:
                _restore_patience(raw)


def _set_patience(raw, milliseconds) -> None:
    """Set busy_timeout on the DBAPI connection itself."""
    cursor = raw.cursor()
    try:
        cursor.execute('PRAGMA busy_timeout=%d' % int(milliseconds))
    finally:
        cursor.close()


def _restore_patience(raw) -> None:
    """Give the connection back the patience the engine configured for it.

    Two faults in abe6cd886, and the second is why the first was permanent
    (hevolve-cc12's review of it).

    It ran through the ORM session, so when the lock refused the flush the
    session needed a rollback first and the reset raised -- swallowed, leaving
    busy_timeout at 0 on a connection the pool hands to the next writer.
    Measured on QueuePool and on StaticPool-on-file: _update_row then gave up
    after 3.77 s instead of waiting out its ~20.5 s ladder, which for a
    lock-killed parse loses the reason entirely. A DBAPI cursor works in a
    failed transaction, so the reset lands.

    And it restored the value it had READ, which on an already-leaked
    connection is 0 -- so every later record faithfully restored 0. The
    engine's configured number is the only correct answer, and models owns it,
    so there is nothing here to drift from it.
    """
    from integrations.social.models import SQLITE_BUSY_TIMEOUT_MS
    try:
        _set_patience(raw, SQLITE_BUSY_TIMEOUT_MS)
    except Exception as unrestored:
        # Said out loud rather than at debug: a reset that fails quietly is how
        # this leak stayed hidden for a whole commit.
        logger.warning("book library: patience not restored (%r)", unrestored)


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
            # Identity, compared exactly, in the one spelling _book_key owns.
            # An id too long for the column matches nothing here, which is
            # correct rather than unhelpful: _register refuses such an id, so
            # no row can exist for it. The previous spelling of this line
            # compared the CLIPPED id and merged two readers' libraries.
            q = q.filter(BookFile.user_id == _book_key(user_id, '')[0])
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
    key_user, key_name = _book_key(user_id, filename)
    with db_session(commit=False) as db:
        books, stalled = _book_dicts(
            db.query(BookFile)
            .filter(BookFile.user_id == key_user,
                    BookFile.filename == key_name)
            .order_by(BookFile.file_id.desc()).all())
    _resolve_stalled(stalled)
    return next((b for b in books
                 if b['status'] in ('pending', 'processing', 'completed')), None)


# ── Jobs (in-memory, bounded) and progress ────────────────────────────

_jobs: 'OrderedDict[str, dict]' = OrderedDict()
_jobs_lock = threading.Lock()


def _new_job(file_id, user_id, request_id, filename) -> str:
    job_id = uuid.uuid4().hex[:12]
    # Keyed as the row keys them, or this job cannot be matched to its book.
    key_user, key_name = _book_key(user_id, filename)
    with _jobs_lock:
        _jobs[job_id] = {
            'job_id': job_id, 'file_id': file_id, 'user_id': key_user,
            'request_id': request_id, 'filename': key_name,
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


def _job_for_file(file_id, filename=None, user_id=None) -> Optional[str]:
    """The most recent job for a book.

    Given a filename and reader as well, only a job for THAT book: file ids
    start over after a database reset (see _clear_page_images), so a stale
    job must never be taken for a fresh row's.
    """
    with _jobs_lock:
        for job in reversed(list(_jobs.values())):
            if (job['file_id'] == file_id
                    and (filename is None or job['filename'] == filename)
                    and (user_id is None or job['user_id'] == str(user_id))):
                return job['job_id']
    return None


def _failed_job_reason(row) -> Optional[str]:
    """Why this book's own parse failed, when the job already knows.

    Read without get_job's resolving, which writes: this runs on the read
    paths, inside an open session.
    """
    job_id = _job_for_file(row.file_id, row.filename, row.user_id)
    if not job_id:
        return None
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is not None and job['status'] == 'failed':
            return job['error'] or 'the parse failed'
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
                # No fields: _update_row bumps updated_at and nothing else,
                # which is all a touch ever needed. Writing status='processing'
                # as well meant the "Done" step -- which runs AFTER _save has
                # written 'completed' -- set a stored book back to processing.
                # _stall_reason then showed it as processing for STALL_SECONDS
                # and finally as "failed: stalled", with its pages on disk the
                # whole time.
                _update_row(file_id)
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

        readers = _page_readers()
        vlm_on, vlm_ok, vlm_errors = bool(readers), 0, 0
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
                data = _parse_page_via_vision(page_num, str(image), readers)
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
            reason = (_BUSY_NOT_SAVED if is_lock_error(e)
                      else f'the book was read but could not be saved: '
                           f'{driver_text(e)}')
            # The operator's only copy on this path. Once the reason is
            # sanitised, nothing else here carries the rendered statement or
            # the traceback -- acd5f5a42 sanitised and did not log, which
            # left a disk I/O error as four words and no stack (its review).
            logger.warning("book %s: read but not saved (%s)", file_id, reason,
                           exc_info=True)
            _mark_failed(file_id, reason)
            log.append(f'Not saved: {reason}')
            result.update(stored=False, store_error=reason, progress_log=log)
            return result
    except Exception as e:
        # A book's own complaint first: this handler sees every exception the
        # parse can raise, and a BookParseError says something true about the
        # file that must not be overwritten by a guess about the database.
        if isinstance(e, BookParseError):
            reason = str(e)
        elif is_lock_error(e):
            reason = _BUSY_NOT_SAVED
        else:
            reason = f'unexpected error: {driver_text(e)}'
        if not isinstance(e, BookParseError):
            # The operator still gets the driver's text and the traceback; it
            # is only the person's copy that drops the SQL and the parameters.
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
        # A book's own complaint first, the same order _run's handler uses: a
        # BookParseError says something TRUE about this request -- a refused
        # account id, say -- and the library is not unavailable merely because
        # it would not file this book (that review's F1).
        if isinstance(e, BookParseError):
            reason = str(e)
        elif isinstance(e, OSError):
            # driver_text falls back to str(e) for anything that is not a
            # SQLAlchemy StatementError, and OSError renders e.filename into
            # str() -- so a disk error here put the uploads path into the
            # agent's answer. The library is not "unavailable" for one either:
            # it is this node that could not keep the file.
            reason = ('this node could not file the book '
                      f'({os_error_reason(e, "no reason reported")})')
        else:
            reason = f'the book library is unavailable ({driver_text(e)})'
        # A traceback for a FAILURE, not for a refusal. Where `reason` is
        # sanitised -- a database error, whose statement the person must not
        # see -- this log is the operator's only copy and needs the stack. A
        # refusal states its own whole reason, so a stack is noise.
        logger.warning(f"reading {pdf_path.name} without storing it: {reason}",
                       exc_info=not isinstance(e, BookParseError))
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
        # This book's own job, never one that merely reused its id: after a
        # database reset the stale job belongs to another book, and handing
        # back its id sends the person to poll a stranger's failure under
        # "Already in your library".
        return {'job_id': _job_for_file(existing['file_id'], existing['filename'],
                                        existing['user_id']),
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
    except BookParseError as e:
        # already recorded on the row and the job, and published
        logger.debug(f"book {file_id}: parse failed (recorded): {e}")
    except Exception:
        logger.exception(f"book {file_id}: background parse crashed")
