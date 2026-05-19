"""SEO audit + scoring for blog-post markdown.

Executable subset of the 10-section checklist in
``.claude/agents/seo.md`` (Nunba repo).  That subagent is the
**human-readable spec + code-review reviewer**; this tool is its
**runtime twin** — runs the same checks on a markdown draft BEFORE
publish so the autonomous blog daemon can gate ``direct_push`` on
``score > 90`` (hybrid publish path agreed with the operator).

Scope notes
-----------
The reviewer subagent's checklist covers full HTML page SEO (Core Web
Vitals, mobile-first, hreflang, etc.).  This tool covers only the
subset that's verifiable from a markdown post + frontmatter pre-publish:

  1. Metadata (frontmatter) — title length, description length, slug,
     keywords, author, date, og_image
  2. Heading hierarchy     — exactly one H1, H2/H3 ordered, no skips
  3. Word count            — thin content lower bound, engagement upper
  4. Internal links        — at least N internal /links (topic cluster)
  5. External links        — at least 1 to authoritative source (E-E-A-T)
  6. Image alt text        — every ``![](...)`` has non-empty alt
  7. FAQ section           — ``## FAQ`` (or similar) with >=3 Q&A items
                             (drives FAQPage schema on the rendered page)
  8. Keyword presence      — target keyword in title + first 100 words
                             + at least one H2/H3
  9. Readability           — avg sentence + paragraph length
 10. Live demo embed       — for tutorial posts, an iframe/HTML embed
                             pointing at /agents/<slug>?plugin=1
                             (required by the use-case demo creator goal)

Live-URL checks (Core Web Vitals, mobile-first, hreflang) belong in a
follow-up tool that uses ``crawl4ai`` against the deployed page;
deliberately out of scope here to keep this pure / no-network /
fast-enough for every blog-draft tick.

Reuse — no parallel paths
-------------------------
* Frontmatter parser: reuses
  ``integrations.skills.registry._parse_frontmatter`` (lazy import to
  avoid cycle).
* Service-tool registration: mirrors ``GhPrTool`` / ``Crawl4AITool``
  (same ``ServiceToolInfo`` shape, same ``native_handler`` contract).
* The 10-section spec is owned by ``.claude/agents/seo.md`` — this
  module IS NOT a second source of truth for what "SEO" means.  When
  the human reviewer's checklist evolves, this tool's section weights
  evolve to match (drift-guard test pins the section names).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .registry import ServiceToolInfo, service_tool_registry

logger = logging.getLogger(__name__)


# ── thresholds (single source of truth; tests pin these) ────────────
TITLE_MAX = 60
TITLE_MIN = 20
DESC_MAX = 160
DESC_MIN = 70
WORDS_MIN = 800             # thin-content floor
WORDS_MAX = 3500            # engagement-drop ceiling
INTERNAL_LINKS_MIN = 2
EXTERNAL_LINKS_MIN = 1
FAQ_ITEMS_MIN = 3
AVG_SENT_LEN_MAX = 25       # words; ~9th-grade reading level
AVG_PARA_LINES_MAX = 8      # paragraphs > 8 lines feel like walls of text

# Per-section weights (sum to 100).  Loose alignment with which signals
# Google's helpful-content + E-E-A-T updates weight most heavily today.
SECTION_WEIGHTS: Dict[str, int] = {
    'metadata': 15,
    'heading_hierarchy': 10,
    'word_count': 10,
    'internal_links': 10,
    'external_links': 8,
    'image_alt_text': 7,
    'faq': 10,
    'keyword_presence': 15,
    'readability': 10,
    'live_demo': 5,
}
assert sum(SECTION_WEIGHTS.values()) == 100, 'section weights must sum to 100'

INTERNAL_LINK_HOSTS = ('hevolve.ai', '/')  # /-prefixed paths or hevolve.ai


# ── frontmatter parser (reused from integrations.skills.registry) ──
def _frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Lazy-import the canonical parser so the dependency stays loose."""
    from integrations.skills.registry import _parse_frontmatter
    return _parse_frontmatter(text)


# ── section checkers (each returns (passed_bool, score_0_1, issues)) ──

def _check_metadata(meta: Dict[str, Any]) -> Tuple[bool, float, List[str]]:
    """Title + description are CRITICAL (correct length, present).  Any
    critical failure → section fails outright regardless of soft-field
    score.  Soft fields (slug/author/date/keywords/og_image) still
    contribute to score granularity but cannot rescue a critical fail.
    """
    issues: List[str] = []
    score_parts: List[float] = []
    critical_ok = True

    title = (meta.get('title') or '').strip()
    if not title:
        issues.append('missing frontmatter: title')
        score_parts.append(0.0)
        critical_ok = False
    elif len(title) > TITLE_MAX:
        issues.append(f'title too long ({len(title)} > {TITLE_MAX})')
        score_parts.append(0.4)
        critical_ok = False
    elif len(title) < TITLE_MIN:
        issues.append(f'title too short ({len(title)} < {TITLE_MIN})')
        score_parts.append(0.6)
        critical_ok = False
    else:
        score_parts.append(1.0)

    desc = (meta.get('description') or '').strip()
    if not desc:
        issues.append('missing frontmatter: description')
        score_parts.append(0.0)
        critical_ok = False
    elif len(desc) > DESC_MAX:
        issues.append(f'description too long ({len(desc)} > {DESC_MAX})')
        score_parts.append(0.4)
        critical_ok = False
    elif len(desc) < DESC_MIN:
        issues.append(f'description too short ({len(desc)} < {DESC_MIN})')
        score_parts.append(0.6)
        critical_ok = False
    else:
        score_parts.append(1.0)

    for required in ('slug', 'author', 'date'):
        v = meta.get(required)
        empty = (not v) if not isinstance(v, str) else not v.strip()
        if empty:
            issues.append(f'missing frontmatter: {required}')
            score_parts.append(0.0)
        else:
            score_parts.append(1.0)

    # keywords + og_image are softer requirements
    if not meta.get('keywords'):
        issues.append('missing frontmatter: keywords (list)')
        score_parts.append(0.5)
    else:
        score_parts.append(1.0)
    if not (meta.get('og_image') or meta.get('og_image_prompt')):
        issues.append('missing frontmatter: og_image (or og_image_prompt)')
        score_parts.append(0.5)
    else:
        score_parts.append(1.0)

    avg = sum(score_parts) / len(score_parts)
    return (critical_ok and avg >= 0.8), avg, issues


def _heading_lines(body: str) -> List[Tuple[int, str]]:
    """Return [(level, text), ...] for every ATX heading in order."""
    out: List[Tuple[int, str]] = []
    in_code = False
    for ln in body.splitlines():
        if ln.strip().startswith('```'):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = re.match(r'^(#{1,6})\s+(.+?)\s*#*\s*$', ln)
        if m:
            out.append((len(m.group(1)), m.group(2).strip()))
    return out


def _check_heading_hierarchy(body: str) -> Tuple[bool, float, List[str]]:
    issues: List[str] = []
    headings = _heading_lines(body)
    h1s = [h for h in headings if h[0] == 1]
    if len(h1s) == 0:
        issues.append('no H1 found')
        return False, 0.0, issues
    if len(h1s) > 1:
        issues.append(f'multiple H1s ({len(h1s)}) — should be exactly 1')
    # Detect level skips (H1 → H3 with no H2)
    prev_level = 0
    skips = 0
    for lvl, _ in headings:
        if prev_level and lvl > prev_level + 1:
            skips += 1
        prev_level = lvl
    if skips:
        issues.append(f'{skips} heading-level skip(s) (H1→H3 etc.)')
    score = 1.0 - 0.3 * min(1, len(h1s) - 1) - 0.2 * min(2, skips)
    score = max(0.0, score)
    return score >= 0.8, score, issues


def _strip_code_blocks(body: str) -> str:
    """Remove fenced code blocks so they don't pollute word/link counts."""
    return re.sub(r'```.*?```', '', body, flags=re.DOTALL)


def _check_word_count(body: str) -> Tuple[bool, float, List[str]]:
    text = _strip_code_blocks(body)
    # Strip markdown syntax cheaply for word count
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', text)        # images
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)      # links
    text = re.sub(r'[#*_>`~|-]+', ' ', text)
    words = [w for w in re.findall(r'\b[\w\-]+\b', text) if any(ch.isalpha() for ch in w)]
    n = len(words)
    issues: List[str] = []
    if n < WORDS_MIN:
        issues.append(f'thin content: {n} words (< {WORDS_MIN})')
        return False, max(0.0, n / WORDS_MIN), issues
    if n > WORDS_MAX:
        issues.append(f'too long: {n} words (> {WORDS_MAX})')
        return False, 0.7, issues
    return True, 1.0, issues


_LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')


def _is_internal(url: str) -> bool:
    u = url.strip().lower()
    if u.startswith('#'):
        return False  # in-page anchor isn't a topic-cluster link
    if u.startswith('/'):
        return True
    return any(h in u for h in INTERNAL_LINK_HOSTS if not h.startswith('/'))


def _check_internal_links(body: str) -> Tuple[bool, float, List[str]]:
    text = _strip_code_blocks(body)
    internal = [m for m in _LINK_RE.findall(text) if _is_internal(m[1])]
    issues: List[str] = []
    if len(internal) < INTERNAL_LINKS_MIN:
        issues.append(f'only {len(internal)} internal link(s) '
                      f'(need ≥ {INTERNAL_LINKS_MIN} for topic cluster)')
        return False, len(internal) / INTERNAL_LINKS_MIN, issues
    return True, 1.0, issues


def _check_external_links(body: str) -> Tuple[bool, float, List[str]]:
    text = _strip_code_blocks(body)
    external = [m for m in _LINK_RE.findall(text)
                if m[1].strip().lower().startswith(('http://', 'https://'))
                and not _is_internal(m[1])]
    issues: List[str] = []
    if len(external) < EXTERNAL_LINKS_MIN:
        issues.append(f'{len(external)} external link(s) — E-E-A-T '
                      f'needs ≥ {EXTERNAL_LINKS_MIN} authoritative source')
        return False, 0.0, issues
    return True, 1.0, issues


_IMG_RE = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')


def _check_image_alt_text(body: str) -> Tuple[bool, float, List[str]]:
    text = _strip_code_blocks(body)
    issues: List[str] = []
    total = 0
    missing = 0
    for alt, _src in _IMG_RE.findall(text):
        total += 1
        if not alt.strip():
            missing += 1
    if total == 0:
        # No images is acceptable — but feature image is recommended
        return True, 0.7, ['no images — feature image recommended for OG card']
    if missing:
        issues.append(f'{missing}/{total} images missing alt text')
        return False, 1.0 - missing / total, issues
    return True, 1.0, issues


_FAQ_HEADING_RE = re.compile(r'^(#{1,3})\s*(faq|frequently asked questions?|q&a)\s*$',
                             re.IGNORECASE | re.MULTILINE)
_FAQ_ITEM_RE = re.compile(r'^(#{2,4}\s+|\*\*Q[:\.]|Q\d*[:\.]\s+|\*\s+\*\*Q)',
                          re.IGNORECASE | re.MULTILINE)


def _check_faq(body: str) -> Tuple[bool, float, List[str]]:
    text = _strip_code_blocks(body)
    issues: List[str] = []
    m = _FAQ_HEADING_RE.search(text)
    if not m:
        issues.append('no FAQ section — recommended (drives FAQPage schema)')
        return False, 0.0, issues
    # Count Q-style sub-items inside the FAQ section (until next H1/H2)
    faq_body = text[m.end():]
    next_h = re.search(r'^#{1,2}\s+', faq_body, re.MULTILINE)
    if next_h:
        faq_body = faq_body[:next_h.start()]
    items = _FAQ_ITEM_RE.findall(faq_body)
    if len(items) < FAQ_ITEMS_MIN:
        issues.append(f'FAQ has {len(items)} item(s) — need ≥ {FAQ_ITEMS_MIN}')
        return False, len(items) / FAQ_ITEMS_MIN, issues
    return True, 1.0, issues


def _check_keyword_presence(
    body: str,
    meta: Dict[str, Any],
) -> Tuple[bool, float, List[str]]:
    issues: List[str] = []
    keywords = meta.get('keywords') or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(',') if k.strip()]
    if not keywords:
        issues.append('no target keywords declared in frontmatter')
        return False, 0.0, issues
    primary = keywords[0].lower()

    title = (meta.get('title') or '').lower()
    headings = ' '.join(t for _, t in _heading_lines(body)).lower()
    # First-100-words: word-tokenize, then check primary keyword presence
    text_norm = _strip_code_blocks(body).lower()
    first_100 = ' '.join(re.findall(r'\b[\w\-]+\b', text_norm)[:100])

    parts: List[float] = []
    if primary in title:
        parts.append(1.0)
    else:
        parts.append(0.0)
        issues.append(f'primary keyword "{primary}" not in title')
    if primary in first_100:
        parts.append(1.0)
    else:
        parts.append(0.0)
        issues.append(f'primary keyword "{primary}" not in first 100 words')
    if primary in headings:
        parts.append(1.0)
    else:
        parts.append(0.0)
        issues.append(f'primary keyword "{primary}" not in any heading')
    score = sum(parts) / len(parts)
    return score >= 0.66, score, issues


_SENT_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')


def _check_readability(body: str) -> Tuple[bool, float, List[str]]:
    text = _strip_code_blocks(body)
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    issues: List[str] = []
    sentences = [s for s in _SENT_SPLIT_RE.split(text) if s.strip()]
    if not sentences:
        return False, 0.0, ['no prose detected']
    avg_words = sum(len(re.findall(r'\b\w+\b', s)) for s in sentences) / len(sentences)
    paragraphs = [p for p in text.split('\n\n') if p.strip()]
    avg_lines = (sum(p.count('\n') + 1 for p in paragraphs) / max(1, len(paragraphs)))
    parts: List[float] = []
    if avg_words > AVG_SENT_LEN_MAX:
        issues.append(f'avg sentence length {avg_words:.1f} > {AVG_SENT_LEN_MAX} words')
        parts.append(max(0.0, 1.0 - (avg_words - AVG_SENT_LEN_MAX) / 15))
    else:
        parts.append(1.0)
    if avg_lines > AVG_PARA_LINES_MAX:
        issues.append(f'avg paragraph {avg_lines:.1f} lines > {AVG_PARA_LINES_MAX}')
        parts.append(max(0.0, 1.0 - (avg_lines - AVG_PARA_LINES_MAX) / 8))
    else:
        parts.append(1.0)
    score = sum(parts) / len(parts)
    return score >= 0.8, score, issues


_IFRAME_RE = re.compile(r'<iframe[^>]+src\s*=', re.IGNORECASE)
_EMBED_AGENT_RE = re.compile(r'/agents/[A-Za-z0-9_-]+\?(?:[^"\'\s]*plugin=1|[^"\'\s]*audio_only=1)',
                             re.IGNORECASE)


def _check_live_demo(
    body: str,
    meta: Dict[str, Any],
) -> Tuple[bool, float, List[str]]:
    """Live demo is REQUIRED for posts where ``meta.post_type ==
    'tutorial'`` or ``meta.post_type == 'use_case'``.  Optional for news
    digests and pillar posts.  Score is 1.0 if either (a) post_type
    doesn't require a demo OR (b) a live demo embed is present."""
    post_type = (meta.get('post_type') or '').lower()
    requires_demo = post_type in ('tutorial', 'use_case', 'demo')
    if not requires_demo:
        return True, 1.0, []
    has_iframe = bool(_IFRAME_RE.search(body))
    has_agent_url = bool(_EMBED_AGENT_RE.search(body))
    if has_iframe and has_agent_url:
        return True, 1.0, []
    if has_iframe or has_agent_url:
        return True, 0.7, ['live demo embed present but missing iframe + ?plugin=1 query']
    return False, 0.0, [
        f'post_type={post_type!r} requires a live demo embed '
        '(iframe src="/agents/<slug>?plugin=1")',
    ]


# ── public API ──────────────────────────────────────────────────────

CHECKERS = (
    ('metadata',           _check_metadata),
    ('heading_hierarchy',  _check_heading_hierarchy),
    ('word_count',         _check_word_count),
    ('internal_links',     _check_internal_links),
    ('external_links',     _check_external_links),
    ('image_alt_text',     _check_image_alt_text),
    ('faq',                _check_faq),
    ('keyword_presence',   _check_keyword_presence),
    ('readability',        _check_readability),
    ('live_demo',          _check_live_demo),
)


def _err(reason_code: str, message: str) -> str:
    return json.dumps({
        'ok': False,
        'reason_code': reason_code,
        'error': message,
    })


def audit_markdown_post(text: str) -> Dict[str, Any]:
    """Run all 10 checks on a markdown post string. Returns dict
    (not JSON) so internal callers (gh_pr_open gating, daemon) can
    branch on fields directly without re-parsing.
    """
    meta, body = _frontmatter(text)
    sections: Dict[str, Dict[str, Any]] = {}
    issues_total: List[str] = []
    weighted_score = 0.0
    for name, fn in CHECKERS:
        if name == 'metadata':
            passed, sec_score, issues = fn(meta)
        elif name == 'keyword_presence':
            passed, sec_score, issues = fn(body, meta)
        elif name == 'live_demo':
            passed, sec_score, issues = fn(body, meta)
        else:
            passed, sec_score, issues = fn(body)
        sec_score = max(0.0, min(1.0, sec_score))
        weight = SECTION_WEIGHTS[name]
        weighted_score += sec_score * weight
        sections[name] = {
            'passed': bool(passed),
            'score': round(sec_score * 100, 1),
            'weight': weight,
            'issues': issues,
        }
        if issues:
            issues_total.extend(f'[{name}] {i}' for i in issues)
    score = round(weighted_score, 1)
    verdict = (
        'SHIP' if score >= 90 else
        'REVIEW' if score >= 70 else
        'REWORK'
    )
    return {
        'ok': True,
        'score': score,
        'verdict': verdict,
        'sections': sections,
        'issues': issues_total,
        'meta': meta,
    }


def seo_audit_score(params_json: str) -> str:
    """Service-tool entrypoint.  Accepts JSON with either ``markdown``
    (the post body string) or ``path`` (a file path within the runtime
    data dir).  Returns JSON with ``score`` (0-100), ``verdict``
    (SHIP/REVIEW/REWORK), per-section breakdown, and full issue list.
    """
    try:
        params = (json.loads(params_json)
                  if isinstance(params_json, str) else params_json) or {}
    except (json.JSONDecodeError, TypeError) as exc:
        return _err('bad_input', f'params must be JSON: {exc}')

    markdown = params.get('markdown')
    path = params.get('path')

    if markdown is None and not path:
        return _err('bad_input',
                    'provide either "markdown" (string) or "path" (file path)')

    if markdown is not None:
        if not isinstance(markdown, str):
            return _err('bad_input', '"markdown" must be a string')
        text = markdown
    else:
        # Reading from disk — confine to the canonical data dir to
        # avoid an agent prompting this tool to read arbitrary files.
        if not isinstance(path, str) or '..' in path:
            return _err('bad_input', 'invalid path')
        try:
            from pathlib import Path
            try:
                from core.platform_paths import get_data_dir
                root = Path(get_data_dir()).resolve()
            except Exception:
                # Test harness / standalone: fall back to current dir
                root = Path('.').resolve()
            p = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
            try:
                p.relative_to(root)
            except ValueError:
                return _err('bad_input', 'path escapes data root')
            text = p.read_text(encoding='utf-8')
        except FileNotFoundError:
            return _err('not_found', f'no such file: {path}')
        except OSError as exc:
            return _err('io_error', str(exc))

    return json.dumps(audit_markdown_post(text))


# ── registration ────────────────────────────────────────────────────
class SeoAuditTool:
    """Service-tool registration shim (mirrors GhPrTool / Crawl4AITool)."""

    NAME = 'seo_audit_score'

    @classmethod
    def create_tool_info(cls, base_url: Optional[str] = None) -> ServiceToolInfo:
        return ServiceToolInfo(
            name=cls.NAME,
            description=(
                'Score a markdown blog post against the 10-section SEO '
                'checklist (executable subset of .claude/agents/seo.md). '
                'Use BEFORE publishing to gate auto-direct-push on '
                'score >= 90 (SHIP); 70-89 routes to PR for human review '
                '(REVIEW); < 70 needs REWORK. Returns per-section '
                'breakdown so the agent can fix specific failures and '
                're-score.'
            ),
            base_url=base_url or 'native://in-process',
            endpoints={
                'seo_audit_score': {
                    'path': '/seo_audit_score',
                    'method': 'POST',
                    'description': (
                        'Audit a markdown post. Pass either "markdown" '
                        '(string) or "path" (file under data_dir). '
                        'Returns {ok, score, verdict, sections, issues}.'
                    ),
                    'params_schema': {
                        'markdown': {
                            'type': 'string',
                            'description': 'Markdown body with frontmatter',
                        },
                        'path': {
                            'type': 'string',
                            'description': 'Relative path under runtime data_dir',
                        },
                    },
                    'native_handler': seo_audit_score,
                },
            },
            health_endpoint=None,
            tags=['seo', 'blog', 'audit', 'gate'],
            timeout=30,
        )

    @classmethod
    def register(cls, base_url: Optional[str] = None) -> bool:
        try:
            return service_tool_registry.register_tool(
                cls.create_tool_info(base_url),
            )
        except Exception as exc:
            logger.warning(f'seo_audit_score registration skipped: {exc}')
            return False
