"""Book navigation tools: the agent surface over the ONE book pipeline.

WHY THIS EXISTS
───────────────
An agent in a /chat turn must be able to teach from a book: see what the user
has, navigate by chapter and by page, and quote the image of the exact page it
is teaching from. These five tools are that surface.

They never decide pedagogy. They return page text, chapter names, page numbers
and a `page_image_url`, and the LLM decides what to teach, which page to quote
and when to move on. "Default page-wise learning" is the model reading page N
and choosing to continue, not a for-loop in this file: the steward's
requirement is that the agent is not hardcoded.

WHAT THIS IS NOT
────────────────
NOT a second pipeline. Each tool is a thin in-process call onto
integrations/learning/book_pipeline.py, the one implementation, which every
HARTOS node runs:

    parse_book_pdf     -> book_pipeline.start_parse
    list_books         -> book_pipeline.list_books
    list_book_chapters -> book_pipeline.get_layouts
    read_book_page     -> book_pipeline.get_layouts   (+ that page's image)
    read_book_chapter  -> book_pipeline.get_layouts   (+ each page's image)

They used to reach the same data over HTTP through Nunba's /db routes, which
existed only on a desktop running Nunba, so on any other node they could never
answer.

`page_image_url` is the field the chat wire schema ALREADY carries
(crossbar_publish.py) and that every client ALREADY renders:
  - web + desktop  Demopage.js:2267  setUploadedImage(parsed.page_image_url)
  - Android        CrossbarAnalogyResponse.java:21  @SerializedName("page_image_url")
So quoting a page needs no protocol change and no client change.
"""
from __future__ import annotations

import functools
import json
import logging
from typing import Any, List, Tuple

from typing_extensions import Annotated

logger = logging.getLogger(__name__)

#: Cap the text handed back to the model in one call. A textbook page can be
#: several KB and a whole chapter is unbounded; the model can always ask for
#: the next page, so truncate rather than blow the context window (#43 is an
#: open task on tool-schema context cost).
_MAX_CHARS_PER_PAGE = 4000
_MAX_PAGES_PER_CHAPTER = 12


class _LibraryUnavailable(Exception):
    """The book library could not be read -- a storage error, as opposed to
    "it was read, and there are no books".

    The first live drive (2026-09-11) collapsed the two: with the backend down,
    list_books told the agent "No books parsed yet. Ask the user to upload a
    PDF", so an agent would ask a user to re-upload a file they had already
    uploaded.
    """


#: What an agent is told when the library could not be read. It names the
#: failure AND forbids the misreading, because a model left with an empty
#: result fills the gap with the most plausible story ("you have no books").
_UNREACHABLE_MSG = (
    'The book library could not be read just now (a storage error on this '
    'node). This does NOT mean the user has no books: do not ask them to '
    're-upload. Tell them the library is temporarily unavailable and try again.'
)


def _read(fn, *args):
    """Run one library read; any storage failure becomes _LibraryUnavailable."""
    try:
        return fn(*args)
    except Exception as e:
        # House rule: no silent gulping -- every caught error logs.
        logger.warning("book library read %s failed: %s",
                       getattr(fn, '__name__', fn), e)
        raise _LibraryUnavailable(str(e)) from e


def _reachable(fn):
    """Tool-edge guard: _LibraryUnavailable becomes _UNREACHABLE_MSG.

    functools.wraps carries __wrapped__ / __annotations__ / __doc__ across, so
    the schema register_for_llm derives via inspect.signature is unchanged.
    """
    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _LibraryUnavailable:
            return _UNREACHABLE_MSG
    return guarded


def _page_image_url(book, page_number):
    """page_image_url ONLY when that page's image was really rendered.

    A URL to a missing file renders as a broken image on every client and lets
    the agent claim to show a page it cannot. This node wrote the image, so it
    is checked on disk and the URL omitted when it is not there. Keyed by the
    book's id, never its name: book_name is LLM-generated, and two uploads can
    share a filename.
    """
    from integrations.learning import book_pipeline as bp
    file_id = (book or {}).get('file_id')
    if not file_id or not page_number:
        return ''
    try:
        if bp.page_image_path(file_id, page_number).is_file():
            return bp.page_image_url(file_id, page_number)
    except Exception as e:
        logger.debug("page image check for book %s page %s failed: %s",
                     file_id, page_number, e)
    return ''


def _clip(text, limit=_MAX_CHARS_PER_PAGE):
    text = str(text or '')
    if len(text) <= limit:
        return text
    return text[:limit] + f'\n…[truncated at {limit} chars — ask for the next page]'


def build_book_tools(ctx) -> List[Tuple[str, str, Any]]:
    """Build book-navigation closures.

    Args:
        ctx: dict with session variables (user_id, …) — same shape
             build_core_tool_closures uses.

    Returns:
        List of (name, description, func) tuples, the shape
        register_core_tools consumes. Every node serves its own library
        in-process, so the tools are always built.
    """
    from integrations.learning import book_pipeline as bp

    user_id = ctx.get('user_id', '')
    tools: List[Tuple[str, str, Any]] = []

    def _books():
        # A turn with no user has no library -- never every user's books,
        # which is what list_books answers for an empty id.
        if user_id in (None, ''):
            return []
        return _read(bp.list_books, user_id)

    def _find_book(book_name=''):
        """Resolve a book by fuzzy name, else the most recent one."""
        books = _books()
        if not books:
            return None
        want = str(book_name or '').strip().lower()
        if want:
            for b in books:
                if want in str(b.get('book_name') or '').lower():
                    return b
            for b in books:
                if want in str(b.get('filename') or '').lower():
                    return b
        return books[0]

    def _layouts(file_id):
        return _read(bp.get_layouts, file_id)

    # ── 1. list_books ────────────────────────────────────────────────
    def list_books() -> str:
        """Which books this user has parsed and can be taught from."""
        books = _books()
        if not books:
            return ('No books parsed yet. Ask the user to upload a PDF, then '
                    'call parse_book_pdf.')
        out = []
        for b in books:
            entry = {
                'file_id': b.get('file_id'),
                'book_name': b.get('book_name') or b.get('filename'),
                'total_pages': b.get('total_pages'),
                'status': b.get('status'),
            }
            if b.get('status') == 'failed' and b.get('error'):
                # Let the agent tell the user WHY (password-protected, scanned
                # with no vision model, ...) instead of guessing.
                entry['error'] = b.get('error')
            out.append(entry)
        return json.dumps({'books': out}, ensure_ascii=False)

    tools.append((
        "list_books",
        "List the books/PDFs this user has already parsed, with page counts. "
        "Call this first when the user asks to study, read or revise a book.",
        list_books,
    ))

    # ── 2. list_book_chapters ────────────────────────────────────────
    def list_book_chapters(
        book_name: Annotated[str, "Book name or part of it. Empty = most recent book."] = '',
    ) -> str:
        """Chapters in a book, in page order, so the agent can navigate."""
        book = _find_book(book_name)
        if not book:
            return 'No matching book. Call list_books to see what is available.'
        rows = _layouts(book.get('file_id'))
        if not rows:
            return f"No parsed pages for '{book.get('book_name')}'. It may still be parsing."
        seen = {}
        for r in rows:
            ch = (r.get('chapter_name') or '').strip()
            if not ch:
                continue
            pg = r.get('page_number') or 0
            if ch not in seen:
                seen[ch] = {'chapter': ch, 'first_page': pg, 'last_page': pg}
            else:
                seen[ch]['first_page'] = min(seen[ch]['first_page'], pg)
                seen[ch]['last_page'] = max(seen[ch]['last_page'], pg)
        chapters = sorted(seen.values(), key=lambda c: c['first_page'])
        if not chapters:
            return json.dumps({
                'book': book.get('book_name'),
                'chapters': [],
                'note': 'Parsed, but no chapter names were detected. '
                        'Navigate by page with read_book_page.',
                'total_pages': book.get('total_pages'),
            }, ensure_ascii=False)
        return json.dumps({
            'book': book.get('book_name'),
            'total_pages': book.get('total_pages'),
            'chapters': chapters,
        }, ensure_ascii=False)

    tools.append((
        "list_book_chapters",
        "List the chapters of a parsed book with their page ranges. Use to "
        "navigate chapter-by-chapter before reading pages.",
        list_book_chapters,
    ))

    # ── 3. read_book_page ────────────────────────────────────────────
    def read_book_page(
        page_number: Annotated[int, "1-based page number to read"],
        book_name: Annotated[str, "Book name or part of it. Empty = most recent."] = '',
    ) -> str:
        """One page's text plus the URL of that page's image, for quoting."""
        book = _find_book(book_name)
        if not book:
            return 'No matching book. Call list_books first.'
        rows = _layouts(book.get('file_id'))
        want = int(page_number or 0)
        page_rows = [r for r in rows if int(r.get('page_number') or 0) == want]
        if not page_rows:
            pages = sorted({int(r.get('page_number') or 0) for r in rows})
            return (f'Page {want} not found. Available pages: '
                    f'{pages[:1]}..{pages[-1:]} ({len(pages)} parsed).')
        page_rows.sort(key=lambda r: int(r.get('layout_number') or 1))
        text = '\n'.join(str(r.get('passage') or '') for r in page_rows).strip()
        chapter = next((r.get('chapter_name') for r in page_rows if r.get('chapter_name')), '')
        topic = next((r.get('topic_name') for r in page_rows if r.get('topic_name')), '')
        total = int(book.get('total_pages') or 0)
        return json.dumps({
            'book': book.get('book_name'),
            'page_number': want,
            'chapter': chapter,
            'topic': topic,
            'text': _clip(text),
            'page_image_url': _page_image_url(book, want),
            'has_next': bool(total and want < total),
            'next_page': want + 1 if (total and want < total) else None,
        }, ensure_ascii=False)

    tools.append((
        "read_book_page",
        "Read ONE page of a parsed book: its text, its chapter, and "
        "page_image_url — the image of that exact page, which you can quote to "
        "the user. Use for page-wise learning; call again with next_page to go on.",
        read_book_page,
    ))

    # ── 4. read_book_chapter ─────────────────────────────────────────
    def read_book_chapter(
        chapter: Annotated[str, "Chapter name (or part of it) to read"],
        book_name: Annotated[str, "Book name or part of it. Empty = most recent."] = '',
        from_page: Annotated[int, "Resume from this page inside the chapter. 0 = start."] = 0,
    ) -> str:
        """A chapter's pages, each with its own image URL for quoting."""
        book = _find_book(book_name)
        if not book:
            return 'No matching book. Call list_books first.'
        rows = _layouts(book.get('file_id'))
        want = str(chapter or '').strip().lower()
        hit = [r for r in rows if want and want in str(r.get('chapter_name') or '').lower()]
        if not hit:
            names = sorted({str(r.get('chapter_name') or '') for r in rows if r.get('chapter_name')})
            return (f"Chapter '{chapter}' not found. Chapters: {names[:20]}"
                    if names else
                    f"Chapter '{chapter}' not found and no chapter names were "
                    f"detected — navigate by page with read_book_page.")
        by_page = {}
        for r in hit:
            pg = int(r.get('page_number') or 0)
            if from_page and pg < int(from_page):
                continue
            by_page.setdefault(pg, []).append(r)
        ordered = sorted(by_page.items())
        clipped = ordered[:_MAX_PAGES_PER_CHAPTER]
        pages = []
        for pg, rs in clipped:
            rs.sort(key=lambda r: int(r.get('layout_number') or 1))
            pages.append({
                'page_number': pg,
                'text': _clip(('\n'.join(str(r.get('passage') or '') for r in rs)).strip()),
                'page_image_url': _page_image_url(book, pg),
            })
        more = len(ordered) > len(clipped)
        return json.dumps({
            'book': book.get('book_name'),
            'chapter': hit[0].get('chapter_name'),
            'pages': pages,
            'has_more': more,
            'resume_from_page': (clipped[-1][0] + 1) if (more and clipped) else None,
        }, ensure_ascii=False)

    tools.append((
        "read_book_chapter",
        "Read a chapter of a parsed book. Returns its pages in order, each with "
        "page_image_url so you can show the user the exact page you are "
        "teaching from. Use resume_from_page to continue a long chapter.",
        read_book_chapter,
    ))

    # ── 5. parse_book_pdf ────────────────────────────────────────────
    def parse_book_pdf(
        file_url: Annotated[str, "Uploaded PDF URL, e.g. /uploads/files/<name>.pdf"],
    ) -> str:
        """Parse an uploaded PDF into pages+chapters via the local VLM."""
        path = bp.resolve_upload_url(file_url)
        if path is None:
            return (f'No uploaded PDF was found at {file_url!r} on this node. '
                    'Ask the user to upload the PDF.')
        try:
            started = bp.start_parse(path, user_id)
        except Exception as e:
            logger.warning("parse_book_pdf could not start for %s: %s", file_url, e)
            return f'Could not start PDF parsing: {e}'
        if started.get('status') == 'completed':
            return json.dumps({
                'status': 'completed',
                'file_id': started.get('file_id'),
                'note': 'Already parsed. Use list_book_chapters or read_book_page '
                        'to teach from it.',
            }, ensure_ascii=False)
        return json.dumps({
            'status': 'parsing',
            'job_id': started.get('job_id'),
            'file_id': started.get('file_id'),
            'note': 'Parsing in the background. Tell the user it is being read, '
                    'then call list_books shortly to see it.',
        }, ensure_ascii=False)

    tools.append((
        "parse_book_pdf",
        "Parse an uploaded PDF book into pages and chapters using the local "
        "vision model. Call after the user uploads a PDF, before teaching from it.",
        parse_book_pdf,
    ))

    # Guard every tool that reads the library, so "could not be read" can
    # never read as "no books" / "page not found". parse_book_pdf reports its
    # own failures.
    _NAV = {'list_books', 'list_book_chapters', 'read_book_page', 'read_book_chapter'}
    return [(n, d, _reachable(f) if n in _NAV else f) for n, d, f in tools]
