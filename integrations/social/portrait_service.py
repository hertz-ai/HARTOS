"""
HevolveSocial - Portrait auto-arrange service (closes #400).

Picks an ordered subset of a user's local gallery for surfaces that
need a small portrait set (BLE encounter post-match profile + map
overlay, social-media share batches, etc.).

Design contract (project_encounter_icebreaker.md §10):

  * INPUT: a list of local gallery references (anything path-shaped
    that supports `Path(p).stat().st_mtime` and `.name`).  No upload,
    no remote.
  * OUTPUT: an ordered subset, length ≤ max_picks.
  * Scoring: pluggable `scorer` callable — when None, falls back to
    a deterministic naive-recency heuristic.  The design doc points
    at a future CLIP-aesthetic plug-in; that plugs in here as the
    `scorer` argument without touching callers.
  * Diversity: avoid consecutive items whose filename ROOT matches
    (e.g., "selfie_001.jpg, selfie_002.jpg" → only the first ranks
    in adjacent positions).  This is a weak proxy for the design
    doc's "don't pick 5 selfies in a row" rule that the CLIP plugin
    will refine.
  * face_visible_consent: when False, drop entries whose filename
    contains face-only hints ("selfie", "face", "portrait").  Weak
    but honors the consent signal even on the no-CLIP path.  When
    True, no filename filter.

The service is pure (no I/O beyond stat()); callers own the gallery
discovery and any post-arrangement upload/copy.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

logger = logging.getLogger('hevolve_social')


# Filename hints we consider face-centric for the no-CLIP fallback.
# Lowercased substring match; intentionally narrow so it doesn't
# accidentally drop landscape shots whose filename happens to contain
# 'face' as part of e.g. 'lacefactory'.
_FACE_HINT_TOKENS: tuple[str, ...] = (
    'selfie', 'portrait', 'mugshot',
)

# Filename ROOT extractor: drop trailing digits + extension so that
# "selfie_001.jpg" and "selfie_002.jpg" share a root.
_DIGIT_TAIL = '0123456789_-'


def _filename_root(name: str) -> str:
    base = os.path.splitext(os.path.basename(name))[0].lower()
    return base.rstrip(_DIGIT_TAIL) or base


def _looks_face_only(name: str) -> bool:
    base = os.path.basename(name).lower()
    return any(tok in base for tok in _FACE_HINT_TOKENS)


@dataclass(frozen=True)
class PortraitChoice:
    """One picked portrait — caller-friendly result row."""
    path: str
    score: float
    reason: str


def _naive_recency_score(path: str) -> float:
    """Default scorer.  Higher mtime → higher score."""
    try:
        return float(os.path.getmtime(path))
    except OSError:
        return 0.0


def arrange_portraits(
    gallery: Sequence[str],
    *,
    face_visible_consent: bool = False,
    max_picks: int = 6,
    scorer: Optional[Callable[[str], float]] = None,
) -> list[PortraitChoice]:
    """Return up to `max_picks` portraits, ordered by descending score
    with adjacent-similar-filename suppression.

    Args:
        gallery: iterable of local paths (anything Path-coercible).
        face_visible_consent: when False, drop face-only filenames.
        max_picks: hard cap on returned portraits.
        scorer: pluggable callable taking a path → float score.
                Defaults to naive-recency (st_mtime).  Higher is
                more preferred.  Errors in scorer fall back to 0.0
                for that path so a single bad file can't poison the
                whole batch.

    Returns:
        list[PortraitChoice], length ≤ max_picks, ordered by
        (score desc, then path asc) with adjacent-root suppression.
    """
    if max_picks <= 0:
        return []
    score_fn = scorer or _naive_recency_score
    candidates: list[tuple[float, str, str]] = []
    for raw in gallery or ():
        # Drop falsy entries BEFORE str() — `str(None)` is the
        # truthy 'None', which would otherwise leak through as a
        # bogus path.
        if not raw:
            continue
        path = str(raw)
        if not face_visible_consent and _looks_face_only(path):
            continue
        try:
            score = float(score_fn(path))
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                'portrait_service: scorer raised on %s: %s', path, exc,
            )
            score = 0.0
        candidates.append((score, path, _filename_root(path)))

    # Stable sort: highest score first, ties broken by path so the
    # output is reproducible across runs / OSes.
    candidates.sort(key=lambda x: (-x[0], x[1]))

    out: list[PortraitChoice] = []
    last_root: Optional[str] = None
    deferred: list[tuple[float, str, str]] = []
    for score, path, root in candidates:
        if root == last_root:
            # Adjacent-similar suppression — push this one to the end
            # of the queue and pick the next non-similar candidate
            # first.  If we run out of variety we fall back to the
            # deferred items (so we don't return fewer than asked
            # when the gallery is largely homogeneous).
            deferred.append((score, path, root))
            continue
        out.append(PortraitChoice(
            path=path, score=score, reason='primary',
        ))
        last_root = root
        if len(out) >= max_picks:
            break

    # Top-up from deferred if we couldn't fill max_picks variety-first.
    if len(out) < max_picks and deferred:
        for score, path, root in deferred:
            out.append(PortraitChoice(
                path=path, score=score, reason='deferred-similar',
            ))
            if len(out) >= max_picks:
                break

    return out


def arrange_portraits_paths(
    gallery: Iterable[str],
    *,
    face_visible_consent: bool = False,
    max_picks: int = 6,
    scorer: Optional[Callable[[str], float]] = None,
) -> list[str]:
    """Convenience wrapper returning just the path strings — for
    callers that don't care about score / reason."""
    return [
        c.path for c in arrange_portraits(
            list(gallery),
            face_visible_consent=face_visible_consent,
            max_picks=max_picks,
            scorer=scorer,
        )
    ]
