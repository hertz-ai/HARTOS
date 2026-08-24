"""
Grounding gate for published facts.

An agent asked for "fascinating, little-known facts" optimised for sharing
will produce fabrications, confidently, because plausibility is exactly what
it is good at. This module exists so that cannot reach an audience.

The rule is structural, not advisory: a claim becomes publishable ONLY by
passing through `verify_claim`, which fetches the cited source and checks the
claim against what actually came back. There is no constructor for a verified
fact that does not go through a fetch. A caller cannot assemble one by hand,
forget a check, or pass a dict that looks close enough.

This matters here specifically. This codebase has already published 416
fabricated "PROOF: 0.0%" posts, for four months, because a number was
generated and nothing downstream asked where it came from. The lesson was not
"add a warning". It was that an unverified value must not be representable in
the type the publisher accepts.

WHAT THIS PROVES, precisely:
  * the cited URL resolves and returns readable text
  * every number in the claim appears verbatim in that text
  * the claim's content words substantially occur in that text

WHAT IT DOES NOT PROVE:
  * that the source is authoritative or peer-reviewed
  * that the claim is the source's actual conclusion rather than something it
    merely mentions, quotes, or refutes
  * that the source is not itself wrong

So this stops fabrication, not misreading. A human still owns the decision to
publish. Overstating what an automated check delivers is the same failure as
the fabricated proofs, one level up.
"""
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Fraction of a claim's content words that must occur in the fetched source.
# Not 1.0: sources paraphrase, and a claim legitimately rewords its source.
# High enough that an invented claim pointing at a real page fails.
_MIN_TERM_OVERLAP = 0.6

# Numbers are the sharpest signal available. Viral "facts" are overwhelmingly
# numeric, a fabricated statistic almost never appears in the page it cites,
# and unlike prose a number cannot be paraphrased. So numbers are matched
# EXACTLY and a single miss fails the claim, regardless of term overlap.
_NUMBER_RE = re.compile(r'\d[\d,]*\.?\d*')

# Spelled-out numbers must be checked too, or the gate has a hole exactly
# where viral copy lives: "the octopus has fourteen brains" carries no digits,
# so a digit-only check waves it through on term overlap alone. Both the claim
# and the source are normalised to digit strings before comparison, so "nine"
# in a claim matches either "nine" or "9" in the source.
_NUMBER_WORDS = {
    'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
    'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
    'ten': '10', 'eleven': '11', 'twelve': '12', 'thirteen': '13',
    'fourteen': '14', 'fifteen': '15', 'sixteen': '16', 'seventeen': '17',
    'eighteen': '18', 'nineteen': '19', 'twenty': '20', 'thirty': '30',
    'forty': '40', 'fifty': '50', 'sixty': '60', 'seventy': '70',
    'eighty': '80', 'ninety': '90', 'hundred': '100', 'thousand': '1000',
    'million': '1000000', 'billion': '1000000000', 'trillion': '1000000000000',
}
_NUMBER_WORD_RE = re.compile(
    r'\b(' + '|'.join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r')\b')

_STOPWORDS = frozenset("""
a an and are as at be been but by can could did do does for from had has have
he her his how i if in into is it its may might more most no not of on once
one only or other our out over own said same she should since so some such
than that the their them then there these they this those through to too
under until up very was we were what when where which while who whom why will
with would you your
""".split())


@dataclass(frozen=True)
class GroundedFact:
    """A claim that was checked against text actually fetched from its source.

    Frozen, and deliberately only produced by `verify_claim`. If you are
    holding one of these, a fetch happened and the checks below passed.
    """
    claim: str
    source_url: str
    evidence: str          # the snippet from the source the claim matched against
    matched_numbers: Tuple[str, ...]
    term_overlap: float
    verified_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'claim': self.claim,
            'source_url': self.source_url,
            'evidence': self.evidence,
            'matched_numbers': list(self.matched_numbers),
            'term_overlap': round(self.term_overlap, 3),
            'verified_at': self.verified_at,
        }


class GroundingError(Exception):
    """Raised when something unverified tries to reach a publish path."""


def _content_words(text: str) -> List[str]:
    words = re.findall(r"[a-z][a-z'-]+", text.lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 2]


def _numbers(text: str) -> List[str]:
    """Every quantity in the text, as canonical digit strings.

    Digits are normalised only for thousands separators: '3,200' and '3200'
    are the same number and sources differ on the comma. Everything else stays
    literal, so 40 must not satisfy a claim of 400.

    Number WORDS are folded to the same representation, so a claim of "nine"
    is checked against a source saying either "nine" or "9". Without this the
    gate misses fabricated spelled-out quantities, which is most of them in
    social copy.
    """
    lowered = text.lower()
    found = [m.group(0).replace(',', '') for m in _NUMBER_RE.finditer(lowered)]
    found += [_NUMBER_WORDS[m.group(1)]
              for m in _NUMBER_WORD_RE.finditer(lowered)]
    return found


def _fetch(url: str, timeout: int = 30) -> Optional[str]:
    """Fetch readable text for a URL using the crawler the project already has.

    Returns None on any failure. A source that cannot be fetched is not a
    source, so the claim it backs does not get published. That is the
    fail-closed direction.
    """
    try:
        from integrations.web_crawler import crawl_url
    except ImportError as exc:
        logger.warning("web_crawler unavailable, cannot ground claims: %s", exc)
        return None

    try:
        result = crawl_url(url, timeout=timeout) or {}
    except Exception as exc:
        logger.warning("fetch failed for %s: %s", url, exc)
        return None

    if not result.get('success', True):
        logger.info("crawler reported failure for %s: %s",
                    url, result.get('error'))
        return None

    for key in ('markdown', 'content', 'text', 'cleaned_html', 'html'):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _evidence_window(source_text: str, claim: str, width: int = 320) -> str:
    """The span of source text where the claim's terms cluster most densely.

    Stored on the fact so a human reviewing a post can see WHY it passed
    without re-fetching. A gate whose reasoning cannot be inspected gets
    trusted blindly, which is how the fabricated proofs survived.
    """
    lowered = source_text.lower()
    terms = _content_words(claim)
    if not terms:
        return source_text[:width].strip()

    best_pos, best_hits = 0, -1
    step = max(1, width // 4)
    for start in range(0, max(1, len(lowered) - width + 1), step):
        window = lowered[start:start + width]
        hits = sum(1 for t in set(terms) if t in window)
        if hits > best_hits:
            best_pos, best_hits = start, hits
    return source_text[best_pos:best_pos + width].strip()


def verify_claim(claim: str, source_url: str,
                 timeout: int = 30) -> Tuple[Optional[GroundedFact], str]:
    """THE only way to make a GroundedFact. Returns (fact, reason).

    On failure the fact is None and the reason says which check failed, so a
    flow can log precisely why a claim was dropped rather than reporting a
    silent zero.
    """
    claim = (claim or '').strip()
    source_url = (source_url or '').strip()

    if not claim:
        return None, 'empty claim'
    if not source_url.lower().startswith(('http://', 'https://')):
        return None, f'no usable source url: {source_url!r}'

    source_text = _fetch(source_url, timeout=timeout)
    if not source_text:
        return None, f'source could not be fetched: {source_url}'

    haystack_numbers = set(_numbers(source_text))
    claim_numbers = _numbers(claim)
    missing = [n for n in claim_numbers if n not in haystack_numbers]
    if missing:
        return None, (f'number(s) {missing} in the claim do not appear in '
                      f'{source_url}')

    lowered = source_text.lower()
    terms = _content_words(claim)
    if not terms:
        return None, 'claim has no checkable content words'
    present = [t for t in terms if t in lowered]
    overlap = len(present) / float(len(terms))
    if overlap < _MIN_TERM_OVERLAP:
        return None, (f'only {overlap:.0%} of the claim\'s terms occur in '
                      f'{source_url} (need {_MIN_TERM_OVERLAP:.0%})')

    return GroundedFact(
        claim=claim,
        source_url=source_url,
        evidence=_evidence_window(source_text, claim),
        matched_numbers=tuple(claim_numbers),
        term_overlap=overlap,
        verified_at=time.time(),
    ), 'verified'


def verify_all(candidates: Sequence[Dict[str, str]],
               timeout: int = 30) -> Tuple[List[GroundedFact], List[Dict[str, str]]]:
    """Verify a batch. Returns (grounded, rejected-with-reasons).

    Rejections are RETURNED, not swallowed. A run that grounds 2 of 9 claims
    should say so loudly: silently publishing the 2 hides that the research
    step is mostly inventing things.
    """
    grounded: List[GroundedFact] = []
    rejected: List[Dict[str, str]] = []
    for item in candidates:
        fact, reason = verify_claim(
            (item or {}).get('claim', ''),
            (item or {}).get('source_url', ''),
            timeout=timeout,
        )
        if fact:
            grounded.append(fact)
        else:
            rejected.append({
                'claim': (item or {}).get('claim', ''),
                'source_url': (item or {}).get('source_url', ''),
                'reason': reason,
            })
    if rejected:
        logger.info("grounding rejected %d of %d claims",
                    len(rejected), len(candidates))
    return grounded, rejected


def assert_publishable(facts: Sequence[Any]) -> List[GroundedFact]:
    """Raise unless every item is a GroundedFact. NOT WIRED as of 2026-08-19.

    ZERO production callers. It was written to be the last gate before
    anything leaves the machine, and it raises rather than filters so a
    publisher cannot quietly drop unverified items while letting its caller
    believe it published what it was given. None of that is in force today:
    nothing calls it, so publishing is NOT grounded-gated.

    It cannot simply be called at the publish boundary, because GroundedFact
    does not survive the trip. verify_facts (marketing_tools.py:390) runs
    verify_all and returns ``[f.to_dict() for f in grounded]`` as JSON TO THE
    LLM; the LLM then composes free text and calls create_social_post(
    content: str) / post_to_channel. By the boundary there are no
    GroundedFact objects left to assert on, only prose. Wiring this needs
    fact-ids carried through the tool round trip and re-hydrated at publish
    time, or re-verification at the boundary -- new machinery, not a call
    site.

    Until then treat "the publish path is grounded" as FALSE. The grounding
    contract lives only in verify_facts' docstring, which instructs the
    model; it is not enforced in code.
    """
    bad = [f for f in facts if not isinstance(f, GroundedFact)]
    if bad:
        raise GroundingError(
            f"{len(bad)} of {len(facts)} items are not GroundedFact. Every "
            f"published claim must come from verify_claim(). Offending: "
            f"{[str(b)[:80] for b in bad[:3]]}"
        )
    if not facts:
        raise GroundingError(
            "nothing to publish: no claim survived grounding. Publishing "
            "nothing is the correct outcome here, not a fallback to unsourced "
            "content."
        )
    return list(facts)
