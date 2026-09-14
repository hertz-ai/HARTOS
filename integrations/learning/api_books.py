"""Book routes, served by EVERY HARTOS node (registered in init_social).

The same URLs the clients already call -- the web app's BOOK_PARSING_URL is
${API_BASE_URL}/upload/parse_pdf. On loopback nothing changes for a client;
from anywhere else every route, the page images included, needs a user token
(see Auth), which today's web and Android clients do not send yet.

  POST     /upload/parse_pdf           a PDF (multipart `file`), or one already
                                       uploaded (JSON `file_url`). Returns 202
                                       at once; the parse runs in the background.
  GET|POST /upload/parse_pdf/status    a parse's progress (job_id)
  GET      /db/pdf_files               the caller's parsed books
  GET      /db/layouts                 a book's pages (file_id [, page_number])
  GET      /uploads/pdf_parse/<file_id>/page_<n>.jpg   one page's image

Thin routes over integrations.learning.book_pipeline, the one implementation.

Size: a multipart upload is bounded by the node's request-size limit
(HEVOLVE_MAX_PAYLOAD_BYTES, 2 MB by default: hart_intelligence_entry sets
Flask's MAX_CONTENT_LENGTH from it and core.serve the transport's), not by
MAX_PDF_BYTES. A desktop takes larger books through /upload/native, which
sends a path, and any node through a PDF link the agent reads (fetch_pdf).

Auth: require_local_or_auth. The desktop's SPA calls these from 127.0.0.1 with
no session, as it always has; any other caller must present a user token and
is scoped to their own books -- a remote caller never chooses a user_id.

The blueprint is named 'books', NOT 'upload' or 'db': those are Nunba's
blueprint names, and a clash makes Flask reject Nunba's registration, which
Nunba logs only at debug level -- silently removing every one of its upload
and db routes.
"""
import logging

from flask import Blueprint, g, jsonify, request, send_file

from integrations.social.auth import require_local_or_auth
from security.rate_limiter_redis import rate_limit

logger = logging.getLogger(__name__)

books_bp = Blueprint('books', __name__)


def _caller_user_id(named='') -> str:
    """The authenticated user for a remote caller; the named user for a local one."""
    uid = getattr(g, 'user_id', None)
    return str(uid) if uid else str(named or '')


def _visible(record) -> bool:
    """A local caller sees everything; a remote caller only their own."""
    uid = getattr(g, 'user_id', None)
    return record is not None and (uid is None or str(record.get('user_id')) == str(uid))


@books_bp.route('/upload/parse_pdf', methods=['POST'])
@require_local_or_auth
@rate_limit('book_parse')
def parse_pdf():
    from integrations.learning import book_pipeline as bp

    if request.content_type and 'multipart' in request.content_type:
        file_obj = request.files.get('file')
        if not file_obj:
            return jsonify({'error': 'No file provided'}), 400
        if not (file_obj.filename or '').lower().endswith('.pdf'):
            return jsonify({'error': 'Only PDF files accepted'}), 400
        user_id = _caller_user_id(request.form.get('user_id', '0'))
        request_id = request.form.get('request_id', '')
        try:
            path = bp.save_pdf(file_obj.stream.read(bp.MAX_PDF_BYTES + 1),
                               file_obj.filename)
        except bp.BookParseError as e:
            return jsonify({'error': str(e)}), 400
        except OSError as e:
            logger.error(f"could not store an uploaded PDF: {e}")
            return jsonify({'error': 'this node could not store the upload'}), 507
    else:
        body = request.get_json(silent=True) or {}
        user_id = _caller_user_id(body.get('user_id', '0'))
        request_id = body.get('request_id', '')
        file_url = str(body.get('file_url') or '')
        if not file_url.startswith('/uploads/'):
            return jsonify({'error': 'Provide PDF file or file_url'}), 400
        path = bp.resolve_upload_url(file_url)
        if path is None:
            return jsonify({'error': f'File not found: {file_url}'}), 404

    try:
        started = bp.start_parse(path, user_id, request_id)
    except Exception as e:
        logger.error(f"could not start a book parse for {path}: {e}")
        return jsonify({'error': 'could not start parsing this PDF'}), 500
    return jsonify({
        **started,
        'file_url': bp.upload_url_for(path),
        'request_id': request_id,
        'message': 'PDF parsing started. Poll /upload/parse_pdf/status for progress.',
    }), 202


@books_bp.route('/upload/parse_pdf/status', methods=['GET', 'POST'])
@require_local_or_auth
def parse_pdf_status():
    from integrations.learning import book_pipeline as bp

    if request.method == 'GET':
        job_id = request.args.get('job_id', '')
    else:
        job_id = (request.get_json(silent=True) or {}).get('job_id', '')
    job = bp.get_job(job_id) if job_id else None
    if not _visible(job):
        return jsonify({'error': 'Unknown job_id'}), 404

    response = {k: job.get(k) for k in ('job_id', 'status', 'total_pages',
                                        'progress', 'file_id')}
    if job['status'] == 'completed' and job.get('result'):
        response['result'] = job['result']
    elif job['status'] == 'failed':
        response['error'] = job.get('error') or 'Unknown error'
    return jsonify(response)


@books_bp.route('/db/pdf_files', methods=['GET'])
@require_local_or_auth
def list_pdf_files():
    from integrations.learning import book_pipeline as bp
    user_id = _caller_user_id(request.args.get('user_id', ''))
    return jsonify(bp.list_books(user_id or None))


@books_bp.route('/db/layouts', methods=['GET'])
@require_local_or_auth
def list_layouts():
    from integrations.learning import book_pipeline as bp

    file_id = str(request.args.get('file_id', ''))
    if not file_id.isdigit():
        return jsonify({'error': 'file_id required'}), 400
    if not _visible(bp.get_book(int(file_id))):
        return jsonify([])
    page_number = str(request.args.get('page_number', ''))
    return jsonify(bp.get_layouts(int(file_id),
                                  int(page_number) if page_number.isdigit() else None))


@books_bp.route('/uploads/pdf_parse/<int:file_id>/page_<int:page_number>.jpg',
                methods=['GET'])
@require_local_or_auth
def page_image(file_id, page_number):
    from integrations.learning import book_pipeline as bp

    if not _visible(bp.get_book(file_id)):
        return jsonify({'error': 'not found'}), 404
    path = bp.page_image_path(file_id, page_number)
    if not path.is_file():
        return jsonify({'error': 'not found'}), 404
    response = send_file(str(path), mimetype='image/jpeg', max_age=3600)
    # One user's page: their own client may cache it, a shared cache may not.
    response.cache_control.public = False
    response.cache_control.private = True
    return response
