"""Edge-case tests for security.sanitize — the input-sanitization boundary.

validate_url (SSRF) is already covered in tests/unit/test_security_p0.py; this
file complements it by covering the UNCOVERED functions surfaced by the coverage
baseline: sanitize_path (path traversal — was entirely untested), sanitize_html,
validate_input's error branches, and validate_prompt_id / validate_user_id. These
are security boundaries, so the point is the rejection/neutralisation paths, not
just the happy path.
"""
from __future__ import annotations

import os

import pytest

from security.sanitize import (
    escape_like, sanitize_path, sanitize_html, validate_input,
    validate_prompt_id, validate_user_id, validate_username, validate_password,
    validate_post_content, validate_comment,
)


# ── escape_like: LIKE-injection wildcards are neutralised ────────────────────
def test_escape_like_neutralises_wildcards():
    # A user searching for '%' must not match everything; backslash first so the
    # escapes themselves aren't doubled wrong.
    assert escape_like('100%_off') == '100\\%\\_off'
    assert escape_like('a\\b') == 'a\\\\b'


# ── sanitize_path: traversal is neutralised, result stays under base ─────────
def test_sanitize_path_keeps_plain_name_under_base(tmp_path):
    base = str(tmp_path)
    result = sanitize_path('42.json', base)
    assert result.startswith(os.path.realpath(base))
    assert result.endswith('42.json')


def test_sanitize_path_neutralises_traversal(tmp_path):
    # The separators and '..' are stripped, so a classic traversal cannot climb
    # out of base — the resolved target stays inside it.
    base = str(tmp_path)
    result = sanitize_path('../../../etc/passwd', base)
    assert result.startswith(os.path.realpath(base))
    # '..' and separators gone -> collapses to 'etcpasswd' under base.
    assert 'etcpasswd' in result
    assert '..' not in result


def test_sanitize_path_strips_backslash_and_slash(tmp_path):
    base = str(tmp_path)
    result = sanitize_path('sub\\dir/file', base)
    assert result.startswith(os.path.realpath(base))
    assert '/' not in os.path.relpath(result, base).replace(os.sep, '/').lstrip('.')


# ── sanitize_html: escapes str, passes non-str through unchanged ─────────────
def test_sanitize_html_escapes_xss():
    out = sanitize_html('<script>alert(1)</script>')
    assert '<script>' not in out and '&lt;script&gt;' in out
    # quote=True -> quotes escaped too (stored-XSS in attributes).
    assert sanitize_html('"x"').count('&quot;') == 2


def test_sanitize_html_non_str_passthrough():
    assert sanitize_html(123) == 123
    assert sanitize_html(None) is None


# ── validate_input: length + pattern + type gates ───────────────────────────
def test_validate_input_happy_path_strips():
    assert validate_input('  hello  ') == 'hello'


def test_validate_input_rejects_non_str():
    with pytest.raises(ValueError, match='must be a string'):
        validate_input(123)


def test_validate_input_min_length():
    with pytest.raises(ValueError, match='at least 3'):
        validate_input('ab', min_length=3)


def test_validate_input_max_length():
    with pytest.raises(ValueError, match='maximum length'):
        validate_input('abcdef', max_length=3)


def test_validate_input_pattern_mismatch():
    with pytest.raises(ValueError, match='invalid characters'):
        validate_input('has spaces', pattern=r'^\w+$')


# ── validate_prompt_id / user_id: the input gates (#36 trailing-newline safe) ─
def test_validate_prompt_id_numeric_ok():
    assert validate_prompt_id(42) == '42'
    assert validate_prompt_id('  7 ') == '7'


def test_validate_prompt_id_trailing_newline_is_stripped_not_bypassed():
    # .strip() runs before the ^\d+$ check, so "12\n" normalises to "12"
    # rather than sneaking past the anchor (the #36 bypass class).
    assert validate_prompt_id('12\n') == '12'


def test_validate_prompt_id_rejects_non_numeric():
    with pytest.raises(ValueError, match='must be numeric'):
        validate_prompt_id('12a')


def test_validate_prompt_id_rejects_embedded_newline():
    # An embedded newline is NOT stripped and must be rejected (no multiline
    # match past the anchor).
    with pytest.raises(ValueError):
        validate_prompt_id('1\n2')


def test_validate_user_id_alphanumeric_ok():
    assert validate_user_id('user_42-x') == 'user_42-x'


def test_validate_user_id_rejects_symbols():
    with pytest.raises(ValueError, match='alphanumeric'):
        validate_user_id('bad id!')


# ── the thin validate_* wrappers delegate to validate_input correctly ────────
def test_validate_username_pattern_and_bounds():
    assert validate_username('good.name-1') == 'good.name-1'
    with pytest.raises(ValueError):
        validate_username('x')          # below min_length 2
    with pytest.raises(ValueError):
        validate_username('bad name')   # space fails the pattern


def test_validate_password_min_length():
    assert validate_password('longenough1') == 'longenough1'
    with pytest.raises(ValueError):
        validate_password('short')


def test_validate_post_and_comment_length_bounds():
    assert validate_post_content('a post') == 'a post'
    assert validate_comment('a comment') == 'a comment'
    with pytest.raises(ValueError):
        validate_post_content('')       # min_length 1 after strip
    with pytest.raises(ValueError):
        validate_comment('   ')         # strips to empty
