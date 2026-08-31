"""
core.hub_allowlist — runtime-editable trusted HuggingFace org allowlist.

WHY THIS EXISTS
───────────────
The trusted-publisher list used to live as a frozenset literal at
main.py:1568 (`_TRUSTED_HF_ORGS`).  Adding a new trusted org (e.g.,
an enterprise tenant's internal HF org `acme-corp`) required a code
edit, a release, and a redeploy — friction high enough that the field
team just told customers to pass `confirm_unverified=True` instead.
That defeated the entire safety gate the list was meant to enforce.

This module persists the list to `~/.nunba/hub_allowlist.json` and
exposes `add(org, reason)` / `remove(org)` / `is_trusted(org)` /
`list()` so the operator can manage it via the admin UI without a
release.

THREAT MODEL
────────────
- Typosquat orgs (Unicode homoglyphs handled separately by
  `_normalize_hf_id` in main.py).
- Drive-by "hot on HF Hub" installs of pickled weights with arbitrary-
  code-execution payloads in `torch.load()`.
- Supply-chain attacks via account-takeover of a previously-trusted org
  (handled via `remove()` — operator can rapidly revoke without a release).

DESIGN NOTES
────────────
- Case-insensitive matching (HF org names are case-preserved but
  case-insensitive at the resolver level; `Qwen` and `qwen` resolve to
  the same repo, so an attacker can't slip in a `Qwen` clone if `qwen`
  is allowed).
- Reasons are persisted alongside each entry so the next operator can
  see WHY an org was added (audit trail without a separate log channel).
- Default-seeded with the 23 orgs that were in the previous frozenset,
  so cold installs preserve the existing trust posture.
- File layout uses indented JSON for git-friendly diffs when an operator
  checks the file into a config repo.
- The path defaults to `~/.nunba/hub_allowlist.json` to follow the same
  pattern as `~/.nunba/mcp.token` — operators learn ONE config location.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# Seeded defaults — extracted verbatim from the previous _TRUSTED_HF_ORGS
# frozenset in main.py.  DO NOT prune entries here without operator
# notice; existing installs that re-seed will lose previously-trusted orgs.
DEFAULT_TRUSTED_ORGS: Dict[str, str] = {
    'google': 'Google AI / Gemma / Gemini ecosystem',
    'microsoft': 'Microsoft (Phi, Bing) — verified publisher',
    'openai': 'OpenAI official org',
    'meta-llama': 'Meta Llama foundation models',
    'mistralai': 'Mistral AI official',
    'Qwen': 'Alibaba Qwen foundation models',
    'ai4bharat': 'AI4Bharat — Indic language ecosystem (T9 cohort)',
    'facebook': 'Meta (legacy facebook namespace)',
    'HuggingFaceTB': 'HuggingFace TB org (small efficient models)',
    'HuggingFaceH4': 'HuggingFace H4 org (instruction-tuned variants)',
    'suno': 'Suno (Bark TTS)',
    'coqui': 'Coqui TTS / XTTS',
    'hexgrad': 'Kokoro TTS publisher',
    'SparkAudio': 'Spark TTS publisher',
    'nvidia': 'NVIDIA official models (NeMo)',
    'NousResearch': 'Nous Research (Hermes, etc.)',
    'pyannote': 'pyannote-audio diarization',
    'openai-community': 'OpenAI community-mirror org',
    'sentence-transformers': 'Sentence Transformers (embeddings)',
    'BAAI': 'Beijing Academy of AI',
    'intfloat': 'intfloat (E5 embedding family)',
    'mixedbread-ai': 'mixedbread-ai (mxbai embeddings)',
    'stabilityai': 'Stability AI (SD, SDXL, Stable Audio)',
    'runwayml': 'RunwayML (legacy SD checkpoints)',
    'CompVis': 'CompVis (legacy SD checkpoints)',
    'hertz-ai': 'Hevolve / HARTOS / Nunba — first-party',
    'HertzAI': 'Hevolve / HARTOS / Nunba — first-party (capitalized)',
}


def _default_path() -> Path:
    """Default config path: ~/.nunba/hub_allowlist.json (Linux/macOS)
    or %USERPROFILE%/.nunba/hub_allowlist.json (Windows).  Mirrors
    the location of mcp.token so operators learn one config root."""
    return Path.home() / '.nunba' / 'hub_allowlist.json'


class HubAllowlist:
    """Persistent, runtime-editable allowlist of trusted HF org publishers.

    Thread-safe (the admin UI can fire concurrent add/remove during a
    multi-tenant tenant-onboarding flow).  Reads are lock-free —
    `is_trusted` is on the chat-install hot path and must be cheap.
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self._path = Path(config_path) if config_path else _default_path()
        self._lock = threading.RLock()
        self._entries: Dict[str, Dict[str, object]] = {}
        self._load_or_seed()

    # ── Persistence ────────────────────────────────────────────────────

    def _load_or_seed(self) -> None:
        """Load from disk, or seed defaults if no config exists yet."""
        if self._path.is_file():
            try:
                with self._path.open(encoding='utf-8') as f:
                    raw = json.load(f)
                # Tolerate legacy formats: list-of-strings → minimal entries.
                if isinstance(raw, list):
                    self._entries = {
                        org: {'reason': '(legacy import)', 'added_at': time.time()}
                        for org in raw if isinstance(org, str)
                    }
                elif isinstance(raw, dict):
                    self._entries = {
                        k: dict(v) if isinstance(v, dict)
                        else {'reason': str(v), 'added_at': time.time()}
                        for k, v in raw.items()
                    }
                else:
                    raise ValueError(f"unexpected JSON root type: {type(raw)}")
                return
            except Exception as e:
                # Don't kill startup over a corrupt config — log loud and
                # fall through to seeding defaults.  Operator sees the
                # warning in /api/admin/diag/degradations-adjacent log.
                logger.warning(
                    "hub_allowlist: failed to load %s (%s) — seeding defaults",
                    self._path, e,
                )
        self._seed_defaults()
        self._save()

    def _seed_defaults(self) -> None:
        now = time.time()
        self._entries = {
            org: {'reason': reason, 'added_at': now}
            for org, reason in DEFAULT_TRUSTED_ORGS.items()
        }

    def _save(self) -> None:
        """Atomic write — never leave a half-written JSON on disk."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix('.json.tmp')
            with tmp.open('w', encoding='utf-8') as f:
                json.dump(self._entries, f, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning("hub_allowlist: save failed (%s)", e)

    # ── Read ───────────────────────────────────────────────────────────

    def is_trusted(self, org: str) -> bool:
        """Case-insensitive trust check.

        HF org names are case-preserved but case-insensitive at resolution
        time — `Qwen` and `qwen` map to the same repo, so an attacker
        can't slip in a `Qwen` clone if only `qwen` is allowed.  We
        normalize both sides to lower for the comparison.
        """
        if not org or not isinstance(org, str):
            return False
        target = org.strip().lower()
        for known in self._entries:
            if known.lower() == target:
                return True
        return False

    def list(self) -> List[Dict[str, object]]:
        """Return all entries with metadata for the admin UI."""
        with self._lock:
            return [
                {
                    'org': org,
                    'reason': info.get('reason', ''),
                    'added_at': info.get('added_at', 0),
                }
                for org, info in sorted(self._entries.items())
            ]

    # ── Write ──────────────────────────────────────────────────────────

    def add(self, org: str, reason: str) -> None:
        """Add an org with a human-readable reason.

        Raises ValueError on invalid input — the admin handler turns this
        into a 400 with the message intact.
        """
        if not org or not isinstance(org, str):
            raise ValueError("org must be a non-empty string")
        org_clean = org.strip()
        # Reject ANY interior whitespace (tab / newline / space), not just the
        # ASCII space — the message and docstring both say "whitespace", and an
        # HF org id is [A-Za-z0-9-] so a tab/newline is always malformed input.
        if '/' in org_clean or any(c.isspace() for c in org_clean):
            raise ValueError("org must not contain '/' or whitespace")
        # ASCII-only — same homoglyph defense as _normalize_hf_id.
        if any(ord(c) > 0x7F for c in org_clean):
            raise ValueError(
                "org must be ASCII-only (Unicode homoglyph defense)"
            )
        if not reason or not isinstance(reason, str):
            raise ValueError("reason must be a non-empty string")
        with self._lock:
            self._entries[org_clean] = {
                'reason': reason.strip(),
                'added_at': time.time(),
            }
            self._save()

    def remove(self, org: str) -> bool:
        """Remove an org by exact-match key.  Returns True if removed,
        False if it wasn't present (idempotent for the operator UI)."""
        if not org or not isinstance(org, str):
            return False
        with self._lock:
            # Case-insensitive remove: find the actual stored key.
            target = org.strip().lower()
            for stored in list(self._entries.keys()):
                if stored.lower() == target:
                    del self._entries[stored]
                    self._save()
                    return True
        return False


# ── Process-wide singleton accessor ──────────────────────────────────────
# Lazy so test code can swap the path via `HubAllowlist(config_path=...)`
# before the singleton is created.  Re-instantiation is cheap.
_INSTANCE: Optional[HubAllowlist] = None
_INSTANCE_LOCK = threading.Lock()


def get_allowlist() -> HubAllowlist:
    """Return the process-wide allowlist singleton."""
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = HubAllowlist()
    return _INSTANCE


def reset_for_tests() -> None:
    """Clear the singleton so a test can swap the config path.  Test-only."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None


__all__ = [
    'DEFAULT_TRUSTED_ORGS',
    'HubAllowlist',
    'get_allowlist',
    'reset_for_tests',
]
