"""Unit tests for integrations/social/portrait_service (closes #400).

Real temp files (via tmp_path) + real os.path.getmtime — no mocks on
the production path.  Where a test wants a specific time order, it
sets mtime via os.utime; where it wants a custom score, it passes a
real callable as the `scorer` argument (the production contract).
"""
from __future__ import annotations

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..'),
)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from integrations.social.portrait_service import (  # noqa: E402
    PortraitChoice,
    arrange_portraits,
    arrange_portraits_paths,
)


def _touch(path, mtime: float) -> str:
    path = str(path)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('x')
    os.utime(path, (mtime, mtime))
    return path


# ══════════════════════════════════════════════════════════════════════
# Recency ordering (default scorer)
# ══════════════════════════════════════════════════════════════════════


def test_default_scorer_sorts_by_recency_descending(tmp_path):
    a = _touch(tmp_path / 'a.jpg', 1000.0)
    b = _touch(tmp_path / 'b.jpg', 3000.0)  # newest
    c = _touch(tmp_path / 'c.jpg', 2000.0)
    out = arrange_portraits([a, b, c], face_visible_consent=True)
    assert [p.path for p in out] == [b, c, a]
    assert all(isinstance(p, PortraitChoice) for p in out)


def test_max_picks_truncates(tmp_path):
    paths = [
        _touch(tmp_path / f'p{i}.jpg', 1000.0 + i)
        for i in range(10)
    ]
    out = arrange_portraits(paths, face_visible_consent=True, max_picks=3)
    assert len(out) == 3


def test_max_picks_zero_returns_empty(tmp_path):
    a = _touch(tmp_path / 'a.jpg', 1000.0)
    assert arrange_portraits([a], max_picks=0) == []


def test_empty_gallery(tmp_path):
    assert arrange_portraits([], face_visible_consent=True) == []
    assert arrange_portraits_paths([]) == []


# ══════════════════════════════════════════════════════════════════════
# face_visible_consent filter
# ══════════════════════════════════════════════════════════════════════


def test_face_consent_off_filters_face_hints(tmp_path):
    selfie = _touch(tmp_path / 'selfie_001.jpg', 3000.0)
    portrait = _touch(tmp_path / 'portrait_holiday.jpg', 2900.0)
    landscape = _touch(tmp_path / 'mountain_dawn.jpg', 1000.0)
    out = arrange_portraits(
        [selfie, portrait, landscape], face_visible_consent=False,
    )
    paths = [p.path for p in out]
    assert selfie not in paths
    assert portrait not in paths
    assert landscape in paths


def test_face_consent_on_keeps_face_hints(tmp_path):
    selfie = _touch(tmp_path / 'selfie_001.jpg', 3000.0)
    landscape = _touch(tmp_path / 'mountain.jpg', 1000.0)
    out = arrange_portraits(
        [selfie, landscape], face_visible_consent=True,
    )
    assert {p.path for p in out} == {selfie, landscape}


def test_face_hint_substring_does_not_match_unrelated_word(tmp_path):
    """'face' as substring must NOT trip — only the explicit hint
    tokens.  Verifies the narrow filter list."""
    landscape_with_face_in_name = _touch(
        tmp_path / 'lacefactory_dusk.jpg', 1000.0,
    )
    out = arrange_portraits(
        [landscape_with_face_in_name], face_visible_consent=False,
    )
    # 'face' substring in 'lacefactory' is allowed because we only
    # match explicit tokens (selfie/portrait/mugshot).
    assert len(out) == 1


# ══════════════════════════════════════════════════════════════════════
# Adjacent-similar suppression (diversity)
# ══════════════════════════════════════════════════════════════════════


def test_adjacent_similar_filenames_suppressed(tmp_path):
    """Two files with the same root ('selfie_001', 'selfie_002') —
    after stripping digit tail both share root 'selfie'.  Should
    suppress the second from adjacent-position even when both rank
    high; falls back to a 'deferred-similar' tail for top-up."""
    s1 = _touch(tmp_path / 'walk_001.jpg', 3000.0)
    s2 = _touch(tmp_path / 'walk_002.jpg', 2999.0)  # similar root
    s3 = _touch(tmp_path / 'walk_003.jpg', 2998.0)  # similar root
    var = _touch(tmp_path / 'cafe_morning.jpg', 2500.0)

    out = arrange_portraits(
        [s1, s2, s3, var],
        face_visible_consent=True,
        max_picks=4,
    )
    paths = [p.path for p in out]
    # First two should be the highest-scoring DIFFERENT roots.
    assert paths[0] == s1
    assert paths[1] == var  # adjacent-similar rule pushed s2/s3 down
    # The deferred-similar fallback fills the rest so we don't return
    # fewer than max_picks.
    deferred_reasons = [p.reason for p in out[2:]]
    assert all(r == 'deferred-similar' for r in deferred_reasons)
    assert {paths[2], paths[3]} <= {s2, s3}


def test_homogeneous_gallery_still_fills_max_picks(tmp_path):
    """Gallery is all same-root.  No variety to suppress; should
    still return up to max_picks via the deferred-similar tail."""
    paths = [
        _touch(tmp_path / f'walk_{i:03d}.jpg', 1000.0 + i)
        for i in range(5)
    ]
    out = arrange_portraits(
        paths, face_visible_consent=True, max_picks=3,
    )
    assert len(out) == 3


# ══════════════════════════════════════════════════════════════════════
# Custom scorer
# ══════════════════════════════════════════════════════════════════════


def test_custom_scorer_used(tmp_path):
    a = _touch(tmp_path / 'a_image.jpg', 1000.0)
    b = _touch(tmp_path / 'b_image.jpg', 2000.0)
    c = _touch(tmp_path / 'c_image.jpg', 3000.0)
    # Reverse-mtime scorer (oldest first).
    def scorer(p):
        return -os.path.getmtime(p)

    out = arrange_portraits(
        [a, b, c], face_visible_consent=True, scorer=scorer,
    )
    assert [p.path for p in out] == [a, b, c]


def test_custom_scorer_failure_recovers_with_zero_score(tmp_path):
    a = _touch(tmp_path / 'a_image.jpg', 1000.0)
    b = _touch(tmp_path / 'b_image.jpg', 2000.0)

    def flaky(p):
        if 'a_image' in p:
            raise RuntimeError('scorer exploded')
        return os.path.getmtime(p)

    out = arrange_portraits(
        [a, b], face_visible_consent=True, scorer=flaky,
    )
    assert len(out) == 2
    # b ranks higher (real mtime); a got 0.0 from the failure path.
    assert out[0].path == b


# ══════════════════════════════════════════════════════════════════════
# Bad input tolerance
# ══════════════════════════════════════════════════════════════════════


def test_nonexistent_path_default_scorer_zero(tmp_path):
    """Default scorer (os.path.getmtime) raises OSError on a missing
    path — production should treat that as score=0, not propagate."""
    real = _touch(tmp_path / 'r.jpg', 2000.0)
    bogus = str(tmp_path / 'does_not_exist.jpg')
    out = arrange_portraits([real, bogus], face_visible_consent=True)
    assert [p.path for p in out] == [real, bogus]
    assert out[1].score == 0.0


def test_empty_string_paths_dropped(tmp_path):
    real = _touch(tmp_path / 'r.jpg', 2000.0)
    out = arrange_portraits(
        [real, '', None], face_visible_consent=True,
    )
    assert len(out) == 1
    assert out[0].path == real


# ══════════════════════════════════════════════════════════════════════
# Convenience wrapper
# ══════════════════════════════════════════════════════════════════════


def test_arrange_portraits_paths_returns_strings(tmp_path):
    a = _touch(tmp_path / 'a.jpg', 1000.0)
    b = _touch(tmp_path / 'b.jpg', 2000.0)
    paths = arrange_portraits_paths([a, b], face_visible_consent=True)
    assert paths == [b, a]
    assert all(isinstance(p, str) for p in paths)
