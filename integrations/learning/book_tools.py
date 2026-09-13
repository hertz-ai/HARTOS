"""Book navigation tools — the agent surface over the EXISTING local pipeline.

WHY THIS EXISTS
───────────────
The local PDF pipeline is already built and already better than the cloud one
it replaced: Nunba `routes/upload_routes.py:427 _parse_page_via_vision` does ONE
VLM call per page where the cloud fanned out to docTR + PubLayNet + SegFormer +
Nougat + LLaVA + Detectron across six hostnames.

But it had a ROUTE and no AGENT SURFACE.  In a /chat turn an agent could learn a
file existed (`get_user_uploaded_file`, which returns only a file_id string) and
could read an IMAGE (`get_text_from_image`), yet could not turn a PDF into pages
— `_pdf_to_images` is internal to Nunba's blueprint.  So the agentic loop broke
at exactly one link.  That is task #25's rule ("expose each as an agent-actionable
surface") applied to the book pipeline.

WHAT THIS IS NOT
────────────────
NOT a second pipeline.  Every tool here is a thin call onto an endpoint or table
that already exists:

    parse_book_pdf     -> POST  {local}/upload/parse_pdf      (upload_routes.py:743)
    list_books         -> GET   {local}/db/pdf_files          (db_routes.py:724)
    list_book_chapters -> GET   {local}/db/layouts            (db_routes.py:696)
    read_book_page     -> GET   {local}/db/layouts            (db_routes.py:696)
    read_book_chapter  -> GET   {local}/db/layouts            (db_routes.py:696)

No new endpoint, no new table, no new model.  `goal_manager.py:2164` states the
constraint verbatim: "TOOLS (use existing — DO NOT create new endpoints)".

WHY THE TOOLS RETURN DATA, NOT LESSONS
──────────────────────────────────────
The steward's requirement was that the agent NOT be hardcoded.  So these tools
never decide pedagogy: they return page text, chapter names, page numbers and a
`page_image_url`, and the LLM decides what to teach, which page to quote, and
when to move on.  "Default page-wise learning" is the model reading page N and
choosing to continue — not a for-loop in this file.

`page_image_url` is the field the chat wire schema ALREADY carries
(`crossbar_publish.py`) and that every client ALREADY renders:
  - web + desktop  Demopage.js:2267  setUploadedImage(parsed.page_image_url)
  - Android        CrossbarAnalogyResponse.java:21  @SerializedName("page_image_url")
So quoting a page needs no protocol change and no client change.
"""
from __future__ import annotations

import json
import logging
from typing import Any, List, Tuple

from typing_extensions import Annotated

logger = logging.getLogger(__name__)

#: Bound every HTTP call.  A wedged local backend must degrade this tool, never
#: the turn (feedback_minimise_blast_radius / Gate 7: no unbounded requests).
_HTTP_TIMEOUT = 20

#: Cap the text handed back to the model in one call.  A textbook page can be
#: several KB; a whole chapter is unbounded.  The model can always ask for the
#: next page, so truncate rather than blow the context window (#43 is already
#: an open task on tool-schema context cost).
_MAX_CHARS_PER_PAGE = 4000
_MAX_PAGES_PER_CHAPTER = 12


class _BackendUnreachable(Exception):
    """The local book service did not answer — connection refused, timeout, a
    non-200, or a non-JSON body.  Deliberately DISTINCT from "it answered, and
    there are no books".

    The first live drive (2026-09-11) collapsed the two: with the backend down,
    list_books told the agent "No books parsed yet. Ask the user to upload a
    PDF" — so an agent would ask a user to re-upload a file they had already
    uploaded.  The degraded-mode unit test asserted that exact string, so the
    bug was encoded as expected behaviour until a real backend died mid-drive.
    """


#: What an agent is told when the backend did not answer.  It names the
#: failure AND forbids the misreading, because a model left with an empty
#: result fills the gap with the most plausible story ("you have no books").
_UNREACHABLE_MSG = (
    'The book service did not answer just now (local backend unreachable or '
    'erroring). This does NOT mean the user has no books: do not ask them to '
    're-upload. Tell them the library is temporarily unavailable and try again.'
)


def _get(url, params=None):
    """GET returning parsed JSON.  Raises _BackendUnreachable on ANY failure.

    Raising instead of returning None is the fix: None and [] used to reach the
    same "no books" branch.  Every navigation tool is wrapped by _reachable()
    (see the end of build_book_tools), which turns the exception into
    _UNREACHABLE_MSG — so "never raise into the turn" still holds, at the edge.
    """
    import requests
    try:
        r = requests.get(url, params=params or {}, timeout=_HTTP_TIMEOUT)
    except Exception as e:
        # House rule: no silent gulping — every caught error logs.
        logger.warning("book_tools GET %s failed: %s", url, e)
        raise _BackendUnreachable(str(e)) from e
    if r.status_code != 200:
        logger.warning("book_tools GET %s -> HTTP %s", url, r.status_code)
        raise _BackendUnreachable(f'HTTP {r.status_code}')
    try:
        return r.json()
    except ValueError as e:
        logger.warning("book_tools GET %s returned non-JSON: %s", url, e)
        raise _BackendUnreachable('non-JSON response') from e


def _reachable(fn):
    """Tool-edge guard: convert _BackendUnreachable into _UNREACHABLE_MSG.

    functools.wraps carries __wrapped__ / __annotations__ / __doc__ across, so
    the schema register_for_llm derives via inspect.signature is unchanged.
    """
    import functools

    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _BackendUnreachable:
            return _UNREACHABLE_MSG
    return guarded


def _rows(payload):
    """Normalise the two shapes db_routes returns: [..] or {'layouts': [..]}."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ('layouts', 'files', 'rows', 'data', 'pdf_files'):
            v = payload.get(key)
            if isinstance(v, list):
                return v
    return []


def _page_image_url(book, page_number):
    """URL of the rendered page image, served by Nunba's serve_upload.

    upload_routes.py writes page images to
        PDF_PARSE_DIR/<pdf stem>/page_<N>.jpg      (:388, :413)
    where PDF_PARSE_DIR = UPLOAD_DIR/'pdf_parse'   (:373)
    and UPLOAD_DIR is served at /uploads/<path>    (:1006 serve_upload)

    So the stem comes from the stored filename, NOT from book_name (which the
    LLM generated and may contain spaces the file never had).
    """
    filename = (book or {}).get('filename') or ''
    if not filename or not page_number:
        return ''
    stem = filename.rsplit('.', 1)[0]
    return f'/uploads/pdf_parse/{stem}/page_{int(page_number)}.jpg'


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
        List of (name, description, func) tuples, the shape register_core_tools
        consumes.  Empty list if the local base URL cannot be resolved, so a
        cloud/central node simply does not advertise these.
    """
    user_id = ctx.get('user_id', '')
    tools: List[Tuple[str, str, Any]] = []

    try:
        from core.config_cache import get_book_list_api, get_book_layouts_api, get_book_parsing_api
    except ImportError as e:
        logger.warning("book_tools: config_cache resolvers unavailable: %s", e)
        return tools

    def _books():
        return _rows(_get(get_book_list_api(), {'user_id': user_id}))

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
        return _rows(_get(get_book_layouts_api(), {'file_id': file_id}))

    _has_images = {}

    def _url_exists(rel_url):
        """HEAD a /uploads/... path on the backend the book APIs live on."""
        import requests
        api = get_book_list_api() or ''
        if not rel_url or '/db/' not in api:
            return False                # cannot verify -> omit, never guess
        try:
            r = requests.head(api.split('/db/', 1)[0] + rel_url, timeout=_HTTP_TIMEOUT)
            return r.status_code == 200
        except Exception as e:
            logger.debug("book_tools image check %s failed: %s", rel_url, e)
            return False

    def _image_url(book, page_number):
        """page_image_url ONLY when that book's page images were really rendered.

        A text-only parse (no rasteriser in the build) writes no page images.
        A URL to a missing file renders as a broken image on every client and
        lets the agent claim to show a page it cannot — so verify the artifact
        once per book, and omit the URL when it is not there.
        """
        url = _page_image_url(book, page_number)
        if not url:
            return ''
        fid = book.get('file_id')
        if fid not in _has_images:
            _has_images[fid] = _url_exists(_page_image_url(book, 1))
        return url if _has_images[fid] else ''

    # ── 1. list_books ────────────────────────────────────────────────
    def list_books() -> str:
        """Which books this user has parsed and can be taught from."""
        books = _books()
        if not books:
            return ('No books parsed yet. Ask the user to upload a PDF, then '
                    'call parse_book_pdf.')
        out = []
        for b in books:
            out.append({
                'file_id': b.get('file_id'),
                'book_name': b.get('book_name') or b.get('filename'),
                'total_pages': b.get('total_pages'),
                'status': b.get('status'),
            })
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
            'page_image_url': _image_url(book, want),
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
                'page_image_url': _image_url(book, pg),
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
        import requests
        url = get_book_parsing_api()
        if not url:
            return ('PDF parsing is not available on this node '
                    '(no local backend and no BOOKPARSING_API configured).')
        try:
            r = requests.post(
                url,
                json={'file_url': file_url, 'user_id': str(user_id), 'request_id': ''},
                timeout=_HTTP_TIMEOUT,
            )
        except Exception as e:
            logger.warning("parse_book_pdf POST %s failed: %s", url, e)
            return f'Could not start PDF parsing: {e}'
        if r.status_code == 202:
            # Large PDF — parsing runs async; the agent should not block.
            try:
                job = r.json()
            except ValueError:
                job = {}
            return json.dumps({
                'status': 'parsing',
                'job_id': job.get('job_id'),
                'note': 'Large PDF, parsing in background. Tell the user it is '
                        'being read, then call list_books shortly to see it.',
            }, ensure_ascii=False)
        if r.status_code != 200:
            logger.warning("parse_book_pdf -> HTTP %s", r.status_code)
            return f'PDF parsing failed (HTTP {r.status_code}).'
        try:
            data = r.json()
        except ValueError:
            return 'PDF parsing returned an unreadable response.'
        return json.dumps({
            'status': 'completed',
            'book_name': data.get('book_name'),
            'total_pages': data.get('total_pages') or data.get('pages'),
            'note': 'Parsed. Use list_book_chapters or read_book_page to teach from it.',
        }, ensure_ascii=False)

    tools.append((
        "parse_book_pdf",
        "Parse an uploaded PDF book into pages and chapters using the local "
        "vision model. Call after the user uploads a PDF, before teaching from it.",
        parse_book_pdf,
    ))

    # Guard every tool that reads the backend through _get, so "did not answer"
    # can never read as "no books" / "page not found".  parse_book_pdf is left
    # unwrapped: it POSTs directly and already reports its own failures.
    _NAV = {'list_books', 'list_book_chapters', 'read_book_page', 'read_book_chapter'}
    return [(n, d, _reachable(f) if n in _NAV else f) for n, d, f in tools]
