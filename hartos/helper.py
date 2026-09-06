from collections import deque
import logging
import sys

_fallback_logger = logging.getLogger(__name__)


def _safe_log(level, msg):
    """Log via Flask current_app if available, else fallback to module logger."""
    try:
        getattr(current_app.logger, level)(msg)
    except (RuntimeError, AttributeError):
        getattr(_fallback_logger, level)(msg)
import requests
import re
import ast
# autogen is imported lazily — it drags google.api_core (~7.6s) + flaml +
# the contrib capabilities chain -> llmlingua -> torch (~4.2s) at import
# time, but every autogen.* / transform_messages.* / transforms.* use in
# this module is INSIDE a function (AST-verified: zero module-level /
# class-base uses; used only in create_visual_agent + the agent builders).
# `import helper` is on the backend-boot critical path (create_recipe /
# reuse_recipe / gather_agentdetails all import it), so deferring autogen
# here is what actually keeps it out of the boot.  Same proxy + test as
# create_recipe.py.  See tests/unit/test_lazy_autogen_import.py.
from core.optional_import import lazy_module
autogen = lazy_module("autogen")
transform_messages = lazy_module(
    "autogen.agentchat.contrib.capabilities.transform_messages")
transforms = lazy_module(
    "autogen.agentchat.contrib.capabilities.transforms")
import json
from flask import current_app
from typing import List, Dict, Tuple, Annotated, Set, FrozenSet, Any
import pickle
from PIL import Image
import uuid
from datetime import datetime, timedelta
import time
import redis
# Lazy-load langchain_classic.schema — every site of use is inside a
# function (HumanMessage/AIMessage at lines 1827/1832; the other 4 names
# are imported-but-unused, so they're dropped here entirely).  Module-
# top import of langchain_classic transitively loads langchain_core
# which uses ``__getattr__`` lazy attribute resolution that cx_Freeze
# can't statically trace.  Result: every `import helper` (and hence
# `import reuse_recipe / gather_agentdetails / create_recipe` which
# import helper) blows up in the frozen-binary validate step with
# `ImportError: cannot import name 'LanguageModelOutput' from
# langchain_core.language_models` (live: build-windows runs
# 25855122044, 26011572288, 26012388043, 26013613058).  Moving these
# to lazy function-scoped imports is the canonical fix — same pattern
# the rest of HARTOS uses for heavy/optional deps (e.g. torch, llama).
# `GoogleSearchAPIWrapper` + `ZepMemory` are likewise lazy below.
import pytz
import aiohttp
import asyncio
import os
from bs4 import BeautifulSoup
from json_repair import repair_json
import traceback

# Performance: cached config loading (single read instead of 3+)
from core.config_cache import get_config as _get_config, get_visual_context_api
# Performance: connection-pooled HTTP sessions
from core.http_pool import get_http_session, pooled_post, pooled_get, pooled_request
# Performance: singleton event loop
from core.event_loop import get_or_create_event_loop
from core.platform_paths import get_coding_workspace_dir

config = _get_config()

# Only set env vars when config actually has non-empty values
# (empty string would clobber a valid env var and fail pydantic validation)
for _key in ('OPENAI_API_KEY', 'GOOGLE_CSE_ID', 'GOOGLE_API_KEY', 'NEWS_API_KEY', 'SERPAPI_API_KEY'):
    _val = config.get(_key, '')
    if _val:
        os.environ[_key] = _val

ACTION_API = config.get('ACTION_API', '')
STUDENT_API = config.get('STUDENT_API', '')
ZEP_API_URL = config.get('ZEP_API_URL', '')
ZEP_API_KEY = config.get('ZEP_API_KEY', '')

try:
    # Lazy-import keeps langchain_classic.utilities (→ langchain_core)
    # out of helper's module-level chain so the frozen-binary validate
    # step doesn't pull the broken langchain_core.language_models lazy
    # __init__ at import time.  Search is optional + only used in a
    # handful of search-tool call paths, so paying the import cost
    # once on first instantiation is cheaper than always-load.
    from langchain_classic.utilities import GoogleSearchAPIWrapper
    search = GoogleSearchAPIWrapper(k=4)
except Exception as _search_err:
    logging.getLogger(__name__).info(f"Google Search unavailable (expected in local mode): {_search_err}")
    search = None


# The control token autogen agents emit to end a round — the same literal the
# UserProxyAgents carry as ``default_auto_reply``.  Named here, next to the
# predicate that consumes it, so the guard below and the check agree by
# construction instead of by two copies of a string.
_TERMINATE_TOKEN = "TERMINATE"


def _is_terminate_msg(msg: dict) -> bool:
    """Null-safe AutoGen termination check.

    AutoGen tool-call messages can have content=None.
    Using ``"TERMINATE" in msg.get("content")`` crashes with TypeError
    when content is None.  This helper guards against that.

    Deliberately a SUBSTRING match: autogen's own convention is that a model
    ends its final answer with "… TERMINATE", so the token has to be honoured
    mid-content.  That is exactly why a CONSUMED token must never be merged
    into unrelated content — see the stale-terminate rule in
    ``validate_messages``.
    """
    content = msg.get("content") if isinstance(msg, dict) else None
    return content is not None and _TERMINATE_TOKEN in content
try:
    redis_client = redis.StrictRedis(
        host=os.environ.get('REDIS_HOST', 'localhost'),
        port=int(os.environ.get('REDIS_PORT', 6379)),
        db=0)
except Exception as _redis_err:
    logging.getLogger(__name__).info(f"Redis unavailable (expected in local mode): {_redis_err}")
    redis_client = None

async def fetch(session, url):
    try:
        async with session.get(url) as response:
            start_time = time.time()
            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')
            end_time = time.time()
            elapsed_time = end_time - start_time
            print(f"time taken to crawl {url} is {elapsed_time}")
            return soup.get_text()
    except Exception as e:
        print(f"An error occurred while fetching {url}: {e}")
        return ""


async def async_main(urls):
    timeout = aiohttp.ClientTimeout(total=30, connect=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [fetch(session, url) for url in urls]
        return await asyncio.gather(*tasks)



# Native web crawler (in-process, no HTTP API needed)

# --- Path traversal protection for prompt file access ---
# Recipe SAVE dir — the SINGLE deployment-aware resolver shared with the REUSE
# read (cache_loaders) and the daemon reuse-CHECK, so a recipe is written, read,
# and checked in the SAME folder in bundled / Docker / dev (no extra env).
try:
    from core.platform_paths import get_recipe_prompts_dir
    PROMPTS_DIR = os.path.abspath(get_recipe_prompts_dir())
except Exception:
    # Fallback mirrors get_recipe_prompts_dir: bundled (read-only install) → user
    # data dir; Docker & dev → code-relative prompts/.
    if getattr(sys, 'frozen', False) or os.environ.get('NUNBA_BUNDLED'):
        PROMPTS_DIR = os.path.abspath(os.path.join(
            os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'prompts'))
    else:
        PROMPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'prompts'))
os.makedirs(PROMPTS_DIR, exist_ok=True)


def sanitize_path_component(value):
    """Reject any value containing path separators or traversal sequences.

    Accepts alphanumeric characters, underscores, and hyphens only.
    Returns the value unchanged if safe, raises ValueError otherwise.
    """
    s = str(value)
    # fullmatch, NOT match: re.match(r'...$', 'foo\n') accepts a trailing newline
    # (the $ anchors before a terminal \n), so a path component 'foo\n' would pass
    # this traversal guard and reach the filesystem. fullmatch rejects it.
    if not re.fullmatch(r'[a-zA-Z0-9_\-]+', s):
        raise ValueError(f"Invalid path component: {s!r}")
    return s


def safe_prompt_path(*parts, ext='.json'):
    """Build a path under prompts/ safely.

    Usage:
        safe_prompt_path(prompt_id)                    -> prompts/{prompt_id}.json
        safe_prompt_path(prompt_id, flow)              -> prompts/{prompt_id}_{flow}.json
        safe_prompt_path(prompt_id, flow, action)      -> prompts/{prompt_id}_{flow}_{action}.json
        safe_prompt_path(prompt_id, flow, 'recipe')    -> prompts/{prompt_id}_{flow}_recipe.json

    Raises ValueError if any component contains path traversal characters.
    """
    sanitized = [sanitize_path_component(p) for p in parts]
    filename = '_'.join(sanitized) + ext
    full = os.path.join(PROMPTS_DIR, filename)
    # Belt-and-suspenders: verify the resolved path is under PROMPTS_DIR
    if not os.path.abspath(full).startswith(PROMPTS_DIR):
        raise ValueError(f"Path escapes prompts directory: {full}")
    return full



def crawl4ai_batch_fetch(urls: List[str], max_concurrent: int = 2) -> List[str]:
    """Fetch multiple URLs using native in-process crawler."""
    try:
        from integrations.web_crawler import crawl_urls
        results = crawl_urls(urls, timeout=30, max_concurrent=max_concurrent)
        extracted = []
        for r in results:
            extracted.append(r['markdown'] if r['success'] else "")
        success_count = sum(1 for r in results if r['success'])
        current_app.logger.info(f"Batch crawl: {success_count}/{len(urls)} succeeded")
        return extracted
    except Exception as e:
        current_app.logger.error(f"Batch crawl error: {e}")
        return [""] * len(urls)


def fallback_fetch(url: str) -> str:
    """
    Fallback fetch using requests + BeautifulSoup (your original method)
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = pooled_get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')

        # Remove unwanted elements
        for element in soup(["script", "style", "nav", "header", "footer"]):
            element.decompose()

        text = soup.get_text()
        # Clean text
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        cleaned_text = ' '.join(chunk for chunk in chunks if chunk)

        return cleaned_text
    except Exception as e:
        current_app.logger.error(f"Fallback fetch failed for {url}: {e}")
        return ""


def check_crawl4ai_service() -> bool:
    """Check if web crawling is available (in-process, always True)."""
    return True  # Native in-process — no external service to check


# ── Keyless web search (no API key, per-node, zero central rate-limit) ───────
# Default path for google_search/top5_results.  We scrape a SERP's *public HTML*
# (keyless — never the key-gated search APIs), then optionally crawl the result
# links.  Run per node from the user's own IP at chat volume, so per-IP soft
# limits never trip and there is no shared key to exhaust.  Deterministic
# fall-through; never raises.
_URL_RE = re.compile(r'^\s*https?://\S+', re.IGNORECASE)


def _looks_like_url(q: str) -> bool:
    """True when the model handed a direct link to fetch — skip the search step."""
    return bool(_URL_RE.match(q or ''))


def _unwrap_ddg_link(href: str) -> str:
    """DDG html wraps results as //duckduckgo.com/l/?uddg=<encoded>.  Return the
    real target URL."""
    try:
        if not href:
            return href
        if 'uddg=' in href:
            from urllib.parse import urlparse, parse_qs, unquote
            qs = parse_qs(urlparse(href).query)
            if qs.get('uddg'):
                return unquote(qs['uddg'][0])
        if href.startswith('//'):
            return 'https:' + href
        return href
    except Exception:
        return href


_CHALLENGE_MARKERS = ('captcha', 'javascript is required', 'unusual traffic',
                      'are you a robot', 'anomaly', 'enable javascript')


def _log_serp_outcome(tier: str, resp, rows) -> None:
    """Say WHY a tier returned nothing, so a block is distinguishable from a
    genuinely empty query.

    Every keyless tier used to fail silently: a scrape that fetched a perfectly
    good page and parsed zero rows returned [] exactly like a real no-results
    query, and the `except` never fired because nothing raised.  Mojeek is the
    worst case: it serves an anti-bot challenge with **HTTP 200**, so status
    alone says success (measured 2026-08-29: 200, 5,519 bytes, 0 rows, body
    containing 'captcha').  That ambiguity is why the search outage read as a
    code bug for weeks and was filed as "all four keyless tiers are down" when
    two were blocked and two had never been installed.

    Logging only.  Callers and return values are untouched.
    """
    if rows:
        return
    try:
        body = getattr(resp, 'text', '') or ''
        hit = next((m for m in _CHALLENGE_MARKERS if m in body.lower()), None)
        detail = (f"looks BLOCKED (page contains {hit!r})" if hit
                  else "no challenge markers, treat as a genuine empty result")
        current_app.logger.warning(
            "keyless SERP tier %s: HTTP %s, fetched %d bytes, parsed 0 rows: %s",
            tier, getattr(resp, 'status_code', '?'), len(body), detail)
    except Exception:
        pass


def _ddg_html_serp(query: str, max_results: int = 5) -> List[dict]:
    """Keyless DuckDuckGo SERP via the html/ endpoint — no key, no account.
    Returns [{'title','url','snippet'}]; best-effort [] on any failure."""
    try:
        from urllib.parse import quote
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}
        resp = pooled_get(f'https://html.duckduckgo.com/html/?q={quote(query)}',
                          headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        rows = []
        for res in soup.select('div.result'):
            a = res.select_one('a.result__a')
            if not a:
                continue
            url = _unwrap_ddg_link(a.get('href'))
            title = a.get_text(' ', strip=True)
            snip_el = res.select_one('.result__snippet')
            snippet = snip_el.get_text(' ', strip=True) if snip_el else ''
            if url and title:
                rows.append({'title': title, 'url': url, 'snippet': snippet})
            if len(rows) >= max_results:
                break
        _log_serp_outcome('C/ddg-html', resp, rows)
        return rows
    except Exception as e:
        try:
            current_app.logger.warning(f"DDG keyless SERP failed: {e}")
        except Exception:
            pass
        return []


def _mojeek_serp(query: str, max_results: int = 5) -> List[dict]:
    """Keyless Mojeek SERP — independent index, no key, no account.

    WAS documented as "scrape-tolerant, plain GET returns HTTP 200, no anti-bot
    token" and described as the primary keyless backend.  That is NO LONGER TRUE
    and the stale wording actively misled a debugging session on 2026-08-29.
    Measured that day from a residential desktop: HTTP **200**, 5,519 bytes,
    `ul.results-standard > li` -> **0 rows**, body containing 'captcha' and
    'javascript is required'.  The 200 is the trap — nothing raises, so the
    handler below never fires; see _log_serp_outcome.  Treat this tier as
    BLOCKED until re-measured; tier A (ddgs) is the working keyless path.
    Returns [{'title','url','snippet'}]; best-effort [] on any failure."""
    try:
        from urllib.parse import quote
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
                   'Accept-Language': 'en-US,en;q=0.9'}
        resp = pooled_get(f'https://www.mojeek.com/search?q={quote(query)}',
                          headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        rows = []
        for li in soup.select('ul.results-standard > li'):
            a = li.select_one('a.title') or li.select_one('h2 a')
            if not a or not a.get('href'):
                continue
            snip_el = li.select_one('p.s')
            rows.append({'title': a.get_text(' ', strip=True),
                         'url': a.get('href'),
                         'snippet': snip_el.get_text(' ', strip=True) if snip_el else ''})
            if len(rows) >= max_results:
                break
        _log_serp_outcome('B/mojeek', resp, rows)
        return rows
    except Exception as e:
        try:
            current_app.logger.warning(f"Mojeek keyless SERP failed: {e}")
        except Exception:
            pass
        return []


def _keyless_serp(query: str, max_results: int = 5) -> List[dict]:
    """Keyless SERP, tier-ordered.  A: ``ddgs`` lib if installed (multi-source);
    B: Mojeek html scrape (no dep, independent index, returns 200 to a plain GET);
    C: DDG html scrape (no dep, but often 202-bot-blocked → weak fallback);
    D: a self-hosted SearXNG when SEARXNG_URL is set (per-node / OS-node).  [] if
    all empty."""
    try:                                            # Tier A — ddgs (optional)
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS() as ddg:
            rows = [{'title': r.get('title', ''),
                     'url': r.get('href') or r.get('link', ''),
                     'snippet': r.get('body', '')}
                    for r in ddg.text(query, max_results=max_results)]
        rows = [r for r in rows if r['url']]
        if rows:
            return rows
    except Exception:
        pass
    rows = _mojeek_serp(query, max_results)             # Tier B — Mojeek (primary)
    if rows:
        return rows
    rows = _ddg_html_serp(query, max_results)           # Tier C — DDG (weak)
    if rows:
        return rows
    try:                                            # Tier D — self-hosted SearXNG
        import os as _os
        base = (_os.environ.get('SEARXNG_URL') or '').strip()
        if base:
            from urllib.parse import quote
            data = pooled_get(f"{base.rstrip('/')}/search?q={quote(query)}&format=json",
                              timeout=10).json()
            rows = [{'title': r.get('title', ''), 'url': r.get('url', ''),
                     'snippet': r.get('content', '')}
                    for r in (data.get('results') or [])[:max_results]]
            rows = [r for r in rows if r['url']]
            if rows:
                return rows
    except Exception:
        pass
    return []


def _keyless_top5(query: str):
    """Deterministic keyless google_search body.  Same return shape as
    top5_results ([{'text','source',...}]) or [] to fall through to optional
    BYO-key Google CSE.  Fastest path = SERP snippets + their source URLs (often
    enough to answer + cite); crawl only when snippets are thin.  A bare URL from
    the model skips search and crawls directly."""
    query = (query or '').strip()
    if not query:
        return []

    if _looks_like_url(query):       # model gave a link → crawl it, skip search
        url = query.split()[0]
        try:
            parts = crawl4ai_batch_fetch([url], max_concurrent=1)
            text = (parts[0] if parts else '') or fallback_fetch(url)
        except Exception:
            text = fallback_fetch(url)
        text = re.sub(r'\s+', ' ', (text or '').strip())
        return [{'text': text[:4000], 'source': [url],
                 'method': 'direct_url', 'enhanced': bool(text)}]

    rows = _keyless_serp(query, max_results=5)
    if not rows:
        return []
    links = [r['url'] for r in rows if r.get('url')]

    # FAST PATH — snippets usually answer it; return now WITH sources to cite.
    snip = '\n'.join(f"{r['title']} — {r['snippet']} ({r['url']})"
                     for r in rows if r.get('snippet'))
    if len(snip) > 150:
        return [{'text': snip, 'source': links, 'method': 'serp_snippets',
                 'enhanced': True, 'word_count': len(snip.split()),
                 'sources_processed': len(links)}]

    # DEEP — snippets thin → crawl the top links for fuller content.
    try:
        contents = crawl4ai_batch_fetch(links[:2], max_concurrent=2)
        body = ' '.join(re.sub(r'\s+', ' ', c.strip())
                        for c in contents if c and len(c.strip()) > 100)
        if body:
            return [{'text': body[:4000], 'source': links[:2],
                     'method': 'serp_crawl', 'enhanced': True,
                     'word_count': len(body.split()),
                     'sources_processed': min(2, len(links))}]
    except Exception as e:
        try:
            current_app.logger.warning(f"crawl-after-search failed: {e}")
        except Exception:
            pass

    return [{'text': snip or ' '.join(links), 'source': links,
             'method': 'serp_links_only', 'sources_processed': len(links)}]


def top5_results(query):
    """
    Enhanced top5_results using Crawl4AI API service
    Maintains the same interface as your original function
    """
    current_app.logger.info(f"Enhanced search for: {query}")

    # Keyless-first: no API key, per-node, zero central rate-limit.  Returns
    # answers with source links to cite; only if every keyless tier is empty do
    # we fall through to the optional BYO-key Google CSE path below.
    _keyless = _keyless_top5(query)
    if _keyless:
        return _keyless

    final_res = []

    try:
        # Your existing Google search
        if search is None:
            return []
        top_2_search_res = search.results(query, 2)
        top_2_search_res_link = [res['link'] for res in top_2_search_res]

        if not top_2_search_res_link:
            current_app.logger.warning("No search links found")
            return search.results(query, 4)

        current_app.logger.info(f"Processing {len(top_2_search_res_link)} URLs")

        # Check if Crawl4AI service is available
        if check_crawl4ai_service():
            current_app.logger.info("Using Crawl4AI API service")

            # Use batch API for better performance
            extracted_content = crawl4ai_batch_fetch(top_2_search_res_link, max_concurrent=2)

            # Process results
            processed_texts = []
            for i, content in enumerate(extracted_content):
                if content and len(content.strip()) > 100:
                    # Clean and truncate content
                    cleaned_text = re.sub(r'\s+', ' ', content.strip())

                    if len(cleaned_text) > 4000:
                        # Try to break at sentence boundary
                        truncate_pos = cleaned_text.rfind('.', 0, 4000)
                        if truncate_pos > 3000:
                            cleaned_text = cleaned_text[:truncate_pos + 1] + " [Content truncated]"
                        else:
                            cleaned_text = cleaned_text[:4000] + "..."

                    processed_texts.append(cleaned_text)
                    current_app.logger.info(f"Processed {len(cleaned_text)} chars from {top_2_search_res_link[i]}")

            if processed_texts:
                combined_text = " ".join(processed_texts)

                result = {
                    'text': combined_text,
                    'source': top_2_search_res_link,
                    'enhanced': True,
                    'method': 'crawl4ai_api',
                    'word_count': len(combined_text.split()),
                    'sources_processed': len(processed_texts)
                }

                final_res.append(result)
                current_app.logger.info(
                    f"Crawl4AI API success: {len(combined_text)} chars from {len(processed_texts)} sources")
            else:
                raise Exception("No content extracted via Crawl4AI API")

        else:
            current_app.logger.warning("Crawl4AI API service unavailable, using fallback")
            raise Exception("Crawl4AI service unavailable")

    except Exception as e:
        current_app.logger.warning(f"Crawl4AI API method failed: {e}, trying fallback")

        # Fallback to requests + BeautifulSoup
        try:
            processed_texts = []

            for i, url in enumerate(top_2_search_res_link):
                current_app.logger.info(f"Fallback processing URL {i + 1}: {url}")

                content = fallback_fetch(url)

                if content and len(content.strip()) > 100:
                    cleaned_text = re.sub(r'\s+', ' ', content.strip())

                    if len(cleaned_text) > 3000:
                        cleaned_text = cleaned_text[:3000] + "..."

                    processed_texts.append(cleaned_text)
                    current_app.logger.info(f"Fallback extracted {len(cleaned_text)} chars from {url}")

                # Small delay between requests
                time.sleep(0.5)

            if processed_texts:
                combined_text = " ".join(processed_texts)

                result = {
                    'text': combined_text,
                    'source': top_2_search_res_link,
                    'enhanced': True,
                    'method': 'fallback_requests',
                    'word_count': len(combined_text.split()),
                    'sources_processed': len(processed_texts)
                }

                final_res.append(result)
                current_app.logger.info(
                    f"Fallback success: {len(combined_text)} chars from {len(processed_texts)} sources")
            else:
                raise Exception("Fallback method also failed")

        except Exception as fallback_error:
            current_app.logger.error(f"All methods failed: {fallback_error}")

            # Final fallback to your original async method if it exists
            try:
                text = asyncio.run(async_main(top_2_search_res_link))
                cleaned_text = re.sub(r'[^\w\s]', '', text[0] + " " + text[1])
                cleaned_text = re.sub(r'\n+', '\n', cleaned_text).strip()

                if cleaned_text:
                    final_res.append({'text': cleaned_text, 'source': top_2_search_res_link})
                    current_app.logger.info("Original async method fallback successful")

            except Exception as async_error:
                current_app.logger.error(f"Original async method also failed: {async_error}")

    # Your original final fallback
    if len(final_res) == 0:
        current_app.logger.info("All methods failed, using Google API results")
        return search.results(query, 4)

    current_app.logger.info(f"Returning {len(final_res)} results")
    return final_res

def parse_user_id(user_id:int):
    from core.config_cache import get_db_url
    base = get_db_url() or 'https://mailer.hertzai.com'
    url = f'{base}/getstudent_by_user_id'

    headers = {
        'Content-Type': 'application/json'
    }

    payload = json.dumps({
        "user_id": user_id
    })

    response = pooled_request("POST", url, headers=headers, data=payload, timeout=15)
    return response.text

def topological_sort(actions):
    # Create adjacency list and in-degree dictionary
    adj_list = {action["action_id"]: [] for action in actions}
    in_degree = {action["action_id"]: 0 for action in actions}
    action_map = {action["action_id"]: action for action in actions}  # Map ID to full action
    _safe_log('info', f'got the actions in topological function')
    _safe_log('info', f'the actions in topological function: - \n {actions}')
    # Build the graph
    for action in actions:

        if action["actions_this_action_depends_on"]:  # Ensure it's not None
            for dep in action["actions_this_action_depends_on"]:
                if dep != action["action_id"]:  # Ignore self-dependency
                    adj_list[dep].append(action["action_id"])
                    in_degree[action["action_id"]] += 1

    # Initialize queue with actions having in-degree 0 (no dependencies)
    queue = deque([aid for aid in in_degree if in_degree[aid] == 0])

    sorted_actions = []
    processed_count = 0  # Track number of processed actions

    while queue:
        aid = queue.popleft()
        sorted_actions.append(action_map[aid])  # Append action to sorted list
        processed_count += 1

        # Reduce in-degree of dependent actions
        for neighbor in adj_list[aid]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    # If processed actions are less than total actions, a cycle exists
    if processed_count != len(actions):
        # Find the actions still having in-degree > 0 (part of cycle)
        cyclic_actions = [aid for aid in in_degree if in_degree[aid] > 0]
        print("Cyclic dependency detected! The following actions are involved in a cycle:")
        cyclic_ids = []
        for aid in cyclic_actions:
            cyclic_ids.append(action_map[aid]['action_id'])  # Print full action details
        print(cyclic_ids)
        return False, None, cyclic_ids

    return True, sorted_actions, None

# ── Canonical local-LLM completion helper (2026-06-09) ──────────────
# Every LLM-shaped call in this module previously POSTed to
# http://aws_rasa.hertzai.com:5459/gpt3 — a cloud proxy.  That violated
# the on-device promise (chat shows "🔒 On-device" badge), wasted
# every-request 15s on the dead endpoint when offline, and routed
# user data through a third party for tasks the local model already
# handles.  Fix: single canonical helper that hits the llama-server
# OpenAI-compat endpoint on loopback.  Every previous /gpt3 caller
# now goes through this.
#
# If the local llama-server is itself down, return None gracefully
# (callers already handle None — they fall back to no-op / unmodified
# input).  No silent cloud fallback: on-device means on-device.
def _local_llm_port():
    """Resolve the local llama-server port. Defaults to 8080."""
    try:
        from core.port_registry import get_port as _gp
        return _gp('llm')
    except Exception:
        import os as _os
        return int(_os.environ.get('HEVOLVE_LLM_PORT', '8080'))


def _local_llm_complete(prompt, max_tokens=3000, temperature=0):
    """Local-only chat completion via llama-server (OpenAI-compat).

    Returns the completion text (string) on success, None on failure.
    Callers must handle None — never falls back to a cloud endpoint.
    """
    _port = _local_llm_port()
    url = f"http://127.0.0.1:{_port}/v1/chat/completions"
    payload = json.dumps({
        "model": "local",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    })
    headers = {'Content-Type': 'application/json'}
    try:
        response = pooled_post(url, headers=headers, data=payload, timeout=30)
        body = response.json()
        choices = body.get('choices') or []
        if not choices:
            _safe_log('warning',
                      f"_local_llm_complete: no choices in response ({body})")
            return None
        msg = choices[0].get('message') or {}
        return msg.get('content') or ''
    except Exception as exc:
        _safe_log('warning', f"_local_llm_complete: request failed: {exc}")
        return None


def fix_actions(array_of_actions, cyclic_ids):
    """Resolve cyclic action dependencies via local LLM.

    Was previously a cloud /gpt3 POST; rerouted 2026-06-09 to the
    local llama-server (on-device promise).  Returns None if local
    LLM is unavailable — caller already handles None as "skip fix."
    """
    prompt = (
        f"From the Below json array of action we are getting cyclic dependency. "
        f"the action_ids which are creating the cyclic dependecy are {cyclic_ids}.\n"
        f"You can Refer the below array of actions \n{array_of_actions}\n "
        f"and return the corrected action dependency without cyclic dependency.\n"
        f"complete json array without cyclic dependency, "
        f"RESPONSE FORMAT: e.g. "
        f'[{{"action_id":"An integer action_id",'
        f'"actions_this_action_depends_on":[]}}]\n'
        f"IMPORTANT INSTRUCTIONS: Do not add any unnecessary hallucinated dependencies in actions\n"
        f"Output array:"
    )
    text = _local_llm_complete(prompt, max_tokens=3000, temperature=0)
    if text is None:
        return None
    try:
        return ast.literal_eval(text)
    except Exception as e:
        _safe_log('warning', f"fix_actions: literal_eval failed ({e})")
        return None


def strip_json_values(obj: Any) -> Any:
    """
    Recursively walk obj.
    - If dict: recurse on each value, preserving keys.
    - If list/tuple: recurse on each element, preserving order & type.
    - Otherwise (leaf): return redacted marker.
    """
    #current_app.logger.info(f"GOT JSON FOR STRIPPING: {obj}")
    # 1. Dig into dict
    if isinstance(obj, dict):
        return { key: strip_json_values(val) for key, val in obj.items() }

    # 2. Dig into list or tuple
    elif isinstance(obj, list):
        return [ strip_json_values(item) for item in obj ]
    elif isinstance(obj, tuple):
        return tuple(strip_json_values(item) for item in obj)

    # 3. Optional: if you know some strings actually contain JSON and you want to descend into them,
    #    uncomment this block.
    elif isinstance(obj, str):
        try:
            parsed = json.loads(obj)
        except json.JSONDecodeError:
            pass
        else:
            return strip_json_values(parsed)

    # 4. Everything else is a true leaf → redact it
    else:
        return f"redacted {type(obj).__name__}"


def fix_json(json_text):
    """Repair malformed JSON via local LLM.

    Was previously a cloud /gpt3 POST; rerouted 2026-06-09 to the
    local llama-server (on-device promise).  Returns None if local
    LLM is unavailable — caller already handles None as "skip fix."
    """
    prompt = (
        "You are an expert JSON fixer. Your task is to correct a given "
        "JSON string, ensuring it is compatible with Python's `eval()`.\n\n"
        "    ### Instructions:\n"
        "    1. **Fix Formatting Issues:**\n"
        "    - Convert single quotes (`'`) to double quotes (`\"`) where necessary (except inside stringified JSON).\n"
        "    - Ensure correct placement of commas, brackets, and braces.\n"
        "    - Fix missing or extra quotes.\n"
        "    - Properly escape special characters like newlines (`\\n`).\n\n"
        "    2. **Convert JSON to Python-Compatible Format:**\n"
        "    - Ensure `true`, `false`, and `null` are replaced with `True`, `False`, and `None`.\n"
        "    - If the JSON contains a string representation of a dictionary inside a field "
        "(e.g., `'{\"key\": \"value\"}'`), ensure it remains correctly formatted.\n\n"
        "    3. **Preserve Key-Value Data:**\n"
        "    - Do not change any key names or values, only correct formatting.\n\n"
        "    4. **Output Only the Fixed JSON:**\n"
        "    - Provide only the corrected JSON without explanations or extra text.\n\n"
        f"    ### Input JSON: {json_text}\n"
        "    Output Json:\n"
    )
    text = _local_llm_complete(prompt, max_tokens=3000, temperature=0)
    if text is None:
        return None
    try:
        x = ast.literal_eval(text)
        _safe_log('info', 'got json object')
        return x
    except Exception as e:
        _safe_log('info', f'GOT ERROR WHILE JSON FIX:{e}')
        return None


def retrieve_json(json_message):
    json_obj = None

    # First, try to extract just the JSON part (without the @user prefix)
    if '@user' in json_message:
        # Find everything after @user
        prefix_match = re.search(r'@user\s*(.*)', json_message, re.DOTALL)
        if prefix_match:
            json_message = prefix_match.group(1).strip()

    # Normalize Unicode characters BEFORE any parse attempt (local LLMs emit these)
    # U+2018/2019 = curly single quotes -> ASCII apostrophe
    # U+201C/201D = curly double quotes -> ASCII quotation mark
    # U+2014 = em-dash, U+2013 = en-dash -> ASCII hyphen-minus
    json_message = (json_message
        .replace("\u2018", "'").replace("\u2019", "'")
        .replace("\u201c", '"').replace("\u201d", '"')
        .replace("\u2014", "-").replace("\u2013", "-"))

    # Empty / whitespace-only input \u2192 return None immediately.  Without
    # this guard, the downstream fallback chain (repair_json \u2192
    # ast.literal_eval \u2192 regex) all fire on empty input and log
    # `json_repair failed: Expecting value: line 1 column 1 (char 0)`
    # + `ast.literal_eval failed: unmatched ')'` per call.  Production
    # log evidence (2026-05-20 22:22-22:28): 70+ such pairs in a single
    # minute when upstream LLM returned empty due to context overflow.
    if not json_message or not json_message.strip():
        return None

    try:
        return json.loads(repair_json(json_message))
    except Exception as e:
        _safe_log('info', f'json_repair failed: {e}')

    # Try using ast.literal_eval which can handle Python dict syntax with single quotes
    try:
        json_obj = ast.literal_eval(json_message)
        _safe_log('info', 'got json object using ast.literal_eval')
        return json_obj
    except Exception as e:
        _safe_log('info', f'ast.literal_eval failed: {e}')
        json_obj = None

    # Fall back to regex + json.loads approach with more careful quote handling
    try:
        json_match = re.search(r'{[\s\S]*}', json_message)
        if json_match:
            json_part = json_match.group(0)

            # A more careful approach to handle quotes correctly
            # This only replaces outer quotes, not quotes within the content
            processed_json = re.sub(r"'([^']+)':", r'"\1":', json_part)  # Fix keys
            # Now handle the string values, being careful about nested quotes
            processed_json = re.sub(r':\s*\'([^\']*)\'', r': "\1"', processed_json)

            json_obj = json.loads(processed_json)
            _safe_log('info', 'got json object')
            return json_obj
        return None
    except Exception as e:
        _safe_log('info', f'json processing failed: {e}')
        json_obj = fix_json(json_message)
        return json_obj


def ensure_tool_call_arguments_json(messages):
    """Coerce every tool_call / function_call ``arguments`` field to a valid
    JSON-object string, in place, and return the same list.

    Why this exists (measured live 2026-09-05 02:16, Auto Research reuse on the
    installed build): the local model emitted a tool_call whose ``arguments``
    was a natural-language sentence, not JSON.  llama.cpp's OpenAI-compatible
    server is lenient on tool-call OUTPUT (it returns whatever the model put in
    the arguments position) but STRICT on INPUT — when a later request carries
    an assistant message whose tool_call arguments string is not valid JSON, it
    returns HTTP 500 "Failed to parse tool call arguments as JSON" and refuses
    the whole generation.  A single malformed call therefore poisons EVERY
    subsequent request in the group chat, and the reuse turn dies before any
    action completes (a plain completion re-probe returned 200 in the same
    window, proving the server was healthy and the fault was the args).

    This is the tool-call sibling of ``validate_messages``' ROLE-ORDER-GUARD:
    purely defensive, a no-op when the model emits valid JSON args, enforcing
    the OpenAI/autogen contract ("arguments is a JSON string") rather than any
    engine-specific error text — so it stays engine-neutral.

    Coercion per malformed call: keep it if ``json.loads`` already succeeds;
    else ``repair_json`` and keep the repaired text only if it parses to a
    dict; else fall back to ``"{}"`` — a well-formed empty-args call.  The
    executor then reports a missing argument and the model re-steers, which is
    strictly better than a 500 that aborts the entire turn.
    """
    if not messages:
        return messages
    coerced = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        fns = []
        for tc in (msg.get('tool_calls') or []):
            if isinstance(tc, dict) and isinstance(tc.get('function'), dict):
                fns.append(tc['function'])
        if isinstance(msg.get('function_call'), dict):
            fns.append(msg['function_call'])
        for fn in fns:
            args = fn.get('arguments')
            if isinstance(args, dict):
                # Some code paths store the arguments as an object already —
                # the wire wants a string, so serialize (never a 500 risk).
                fn['arguments'] = json.dumps(args)
                continue
            if args is None:
                fn['arguments'] = '{}'
                coerced += 1
                continue
            if not isinstance(args, str):
                args = str(args)
            try:
                json.loads(args)
                continue  # already valid JSON — leave untouched
            except Exception:
                pass
            fixed = '{}'
            try:
                repaired = repair_json(args)
                obj = (repaired if isinstance(repaired, (dict, list))
                       else json.loads(repaired))
                if isinstance(obj, dict):
                    fixed = json.dumps(obj)
            except Exception:
                fixed = '{}'
            fn['arguments'] = fixed
            coerced += 1
    if coerced:
        try:
            current_app.logger.info(
                f"[TOOL-ARGS-GUARD] coerced {coerced} malformed tool_call "
                f"argument(s) to valid JSON — would otherwise cause llama 500 "
                f"'Failed to parse tool call arguments as JSON' on the next "
                f"request and abort the turn.")
        except Exception:
            pass
    return messages


class ToolMessageHandler:
    """Handles tool messages in the conversation history to prevent tool_call_id errors.

    This implementation maintains proper message structure for OpenAI API requirements,
    fixing historical inconsistencies while allowing active tool calls to be processed
    naturally by the framework. handles references between assistant tool calls
    and tool responses to prevent "Invalid parameter: 'tool_call_id' not found" errors.
    It also handles the "only messages with role 'assistant' can have a function call" error.
    """

    def __init__(self, user_tasks=None, user_prompt=None):
        """
        Initialize the ToolMessageHandler.

        Args:
            user_tasks: Global user_tasks dictionary containing session data
            user_prompt: Current session identifier (e.g., "10077_123")
        """
        self.user_tasks = user_tasks
        self.user_prompt = user_prompt

    def get_current_action_id(self):
        """Get current action ID from user_tasks."""
        if not self.user_tasks or not self.user_prompt:
            return None

        try:
            if self.user_prompt in self.user_tasks:
                current_action_id = self.user_tasks[self.user_prompt].current_action
                current_app.logger.info(
                    f"Retrieved current_action_id: {current_action_id} for session: {self.user_prompt}")
                return current_action_id
        except Exception as e:
            current_app.logger.error(f"Error getting current_action_id from user_tasks: {e}")

        return None

    def validate_messages(self, messages: List[Dict]) -> List[Dict]:
        # TOOL-ARGS-GUARD: coerce any malformed tool_call arguments to valid
        # JSON before anything downstream (or the model server) sees them.  A
        # non-JSON arguments string makes llama.cpp 500 "Failed to parse tool
        # call arguments as JSON" on EVERY subsequent request and aborts the
        # turn — sibling of the ROLE-ORDER-GUARD below (see module function).
        messages = ensure_tool_call_arguments_json(messages)
        for i, msg in enumerate(messages):
            if 'content' in msg and msg['content'] is None:
                # Log detailed information about the problematic message
                current_app.logger.warning(f"NULL CONTENT DETECTED: Message at index {i} has null content")
                current_app.logger.warning(
                    f"Message type: {msg.get('role', 'unknown')}, name: {msg.get('name', 'unknown')}")

                # Log additional message properties to help debugging
                tool_calls = "Yes" if "tool_calls" in msg else "No"
                function_call = "Yes" if "function_call" in msg else "No"
                current_app.logger.warning(f"Has tool_calls: {tool_calls}, Has function_call: {function_call}")

                # Log message context (previous message if available)
                if i > 0 and i < len(messages):
                    prev_msg = messages[i - 1]
                    current_app.logger.warning(
                        f"Previous message: role={prev_msg.get('role')}, type={prev_msg.get('type')}")

                # Replace null with empty string
                messages[i]['content'] = ""
                current_app.logger.info(f"FIXED: Replaced null content with empty string in message {i}")

        # OPENAI API ROLE-ORDER GUARD (added 2026-05-08 after live evidence
        # of chat 400 errors: "Cannot have 2 or more assistant messages
        # at the end of the list").  Llama-server's OpenAI-compatible
        # endpoint requires alternating user/assistant messages with no
        # consecutive same-role pairs (especially at the tail).  Autogen's
        # multi-agent loop emits empty assistant placeholders + back-to-back
        # Assistant→Assistant chains during speaker selection (the
        # 2026-05-08 langchain.log showed Message[7]=Assistant,
        # Message[8]={"content":"","role":"assistant"}, Message[10]=Assistant
        # — three consecutive assistants causing the 400).
        #
        # Fix:
        #   1. Drop empty-content assistant messages that have no tool_calls
        #      / function_call (they're autogen placeholders, not real
        #      replies).
        #   2. Coalesce consecutive same-role messages by joining their
        #      content with two newlines, so a {Assistant, Assistant} pair
        #      becomes a single Assistant with merged text.  Tool-call /
        #      function-call carrying messages are preserved as-is so
        #      they don't get silently dropped.
        #
        # This is purely defensive — if the upstream loop emits clean
        # alternating messages, this is a no-op.  Surfaces dropped /
        # merged events at INFO so future diagnoses are visible.
        try:
            cleaned: List[Dict] = []
            # Accumulate and log ONCE per invocation instead of once per
            # message.  The intent above ("surface dropped/merged events at
            # INFO so future diagnoses are visible") is right and is kept —
            # what was wrong was the CARDINALITY.  This fires per message per
            # turn, so on 2026-08-05 it was the single largest consumer of
            # disk on a running desktop: 15,855 lines / 3.45 MB inside one
            # 400k-line sample, with gui_app.log growing 3.4 MB/min and the
            # log dir at 492 MB against 23 GB free.  create_recipe.py:133
            # already records the extreme case — 20,920 of these lines in one
            # session during the livelock.
            #
            # A summary keeps every fact the per-message lines carried (that
            # it happened, how many, which indices, which names) at O(1) lines
            # per call, so the diagnostic survives and the disk does too.
            # Demoting to DEBUG was the obvious alternative and is worse: it
            # would silence the signal precisely when a livelock makes it
            # loudest, which is when you need it.
            _dropped: List[str] = []
            _coalesced: List[str] = []
            _stale_terms: List[str] = []
            _last_idx = len(messages) - 1
            for i, msg in enumerate(messages):
                role = (msg.get('role') or '').lower()
                content = msg.get('content')
                has_calls = bool(msg.get('tool_calls') or msg.get('function_call'))
                # Drop empty assistant placeholders (no content + no tool calls)
                if role == 'assistant' and not has_calls:
                    if content is None or (isinstance(content, str) and content.strip() == ''):
                        _dropped.append(f"{i}({msg.get('name','unknown')})")
                        continue
                # Drop a CONSUMED bare TERMINATE — same class of artifact as the
                # empty placeholder above: a control token, already acted on,
                # carrying no content for the turn being built.
                #
                # It matters because the coalescing below CONCATENATES contents.
                # A stale token merged into a later message makes
                # _is_terminate_msg (a substring match, by design) fire on that
                # message, and autogen applies this transform BEFORE any reply
                # function (conversable_agent.py:2059) — so
                # check_termination_and_human_reply returns (True, None),
                # generate_reply returns None, and run_chat breaks
                # (groupchat.py:1190) without ever calling the model.
                #
                # Measured live 2026-09-05, agent 88601674818 action 3: three
                # attempts in 137 ms, llama-server /slots byte-identical across
                # the turn, retry budget spent, HITL question repeating forever.
                #
                # Only a token that is NOT the last message qualifies.  A live
                # TERMINATE still terminates — dropping that would loop the
                # group chat forever, the opposite failure.
                if (not has_calls and i < _last_idx
                        and isinstance(content, str)
                        and content.strip() == _TERMINATE_TOKEN):
                    _stale_terms.append(f"{i}({msg.get('name','unknown')})")
                    continue
                # Coalesce consecutive same-role messages
                if cleaned:
                    prev = cleaned[-1]
                    prev_role = (prev.get('role') or '').lower()
                    prev_has_calls = bool(prev.get('tool_calls') or prev.get('function_call'))
                    if (prev_role == role
                            and not prev_has_calls
                            and not has_calls
                            and isinstance(prev.get('content'), str)
                            and isinstance(content, str)):
                        merged = prev['content']
                        if content.strip():
                            merged = (merged + '\n\n' + content
                                      if merged.strip() else content)
                        prev['content'] = merged
                        _coalesced.append(f"{i-1}+{i}({role})")
                        continue
                cleaned.append(msg)
            messages = cleaned

            # One line per invocation, only when the guard actually acted.
            # Indices are capped so a pathological turn cannot reintroduce the
            # unbounded growth this replaced — the count stays exact either way.
            if _dropped or _coalesced or _stale_terms:
                _cap = 12
                _d = ', '.join(_dropped[:_cap]) + (
                    f" (+{len(_dropped) - _cap} more)" if len(_dropped) > _cap else '')
                _c = ', '.join(_coalesced[:_cap]) + (
                    f" (+{len(_coalesced) - _cap} more)" if len(_coalesced) > _cap else '')
                _s = ', '.join(_stale_terms[:_cap]) + (
                    f" (+{len(_stale_terms) - _cap} more)" if len(_stale_terms) > _cap else '')
                current_app.logger.info(
                    f"[ROLE-ORDER-GUARD] {len(messages)} msgs out of "
                    f"{len(_dropped) + len(_coalesced) + len(_stale_terms) + len(messages)} in; "
                    f"dropped {len(_dropped)} empty assistant placeholder(s)"
                    f"{' at ' + _d if _dropped else ''}; "
                    f"dropped {len(_stale_terms)} consumed TERMINATE token(s)"
                    f"{' at ' + _s if _stale_terms else ''}; "
                    f"coalesced {len(_coalesced)} consecutive same-role pair(s)"
                    f"{' at ' + _c if _coalesced else ''} "
                    f"— both would cause OpenAI 400 (2+ assistant messages / "
                    f"alternation rule)."
                )

        except Exception as _guard_err:
            # Never break the upstream pipeline — if the guard itself
            # crashes, fall through with the original messages and let
            # the API-level error (if any) surface as before.
            current_app.logger.exception(
                f"[ROLE-ORDER-GUARD] guard raised {type(_guard_err).__name__}: "
                f"{_guard_err!s} — using messages as-is"
            )

        return messages

    def remove_orphan_tool_messages(self, messages):
        # 1. Collect every tool-call id that appears in an assistant message
        valid_tool_call_ids = {
            tc["id"]
            for msg in messages
            if msg.get("role") == "assistant" and "tool_calls" in msg
            for tc in msg["tool_calls"]
            if "id" in tc
        }

        # Helper ── does this consolidated reply reference at least one valid id?
        def consolidated_has_valid_id(msg) -> bool:
            """Return True when a consolidated tool message carries
            a tool_call_id that belongs to some earlier assistant message."""

            nested_ids = self.get_tool_call_ids_from_consolidated(msg)
            return any(tcid in valid_tool_call_ids for tcid in nested_ids)

        cleaned: list[dict] = []
        for msg in messages:
            if msg.get("role") == "tool":
                tcid = msg.get("tool_call_id")

                # ── ordinary single-tool reply ───────────────────────────────
                if tcid is not None:
                    if tcid not in valid_tool_call_ids:
                        current_app.logger.warning(
                            f"Dropping orphan tool message with tool_call_id={tcid}"
                        )
                        continue

                # ── consolidated reply (no top-level tool_call_id) ──────────
                elif self.is_consolidated_response(msg) and not consolidated_has_valid_id(msg):
                    current_app.logger.warning(
                        "Dropping orphan consolidated tool message (no matching IDs)"
                    )
                    continue

            cleaned.append(msg)

        return cleaned

    def is_consolidated_response(self, message):
        """Improved method to detect consolidated tool responses."""
        # Check for standard consolidated response format
        if (message.get('role') == 'tool' and
                'tool_responses' in message and
                isinstance(message['tool_responses'], list) and
                len(message['tool_responses']) > 1):
            return True

        # Also check for multiple tool_call_ids in a single message (alternative format)
        if message.get('role') == 'tool' and 'tool_call_ids' in message and isinstance(message['tool_call_ids'], list):
            return True

        return False

    def get_tool_call_ids_from_consolidated(self, message):
        """Extract all tool call IDs from a consolidated response."""
        if not self.is_consolidated_response(message):
            return []

        tool_call_ids = []

        # Check for direct tool_call_ids array
        if 'tool_call_ids' in message and isinstance(message['tool_call_ids'], list):
            tool_call_ids.extend(message['tool_call_ids'])

        # Check for main message tool_call_id
        if 'tool_call_id' in message:
            tool_call_ids.append(message['tool_call_id'])

        # Extract IDs from each tool response in the array
        if 'tool_responses' in message and isinstance(message['tool_responses'], list):
            for tool_response in message['tool_responses']:
                if 'tool_call_id' in tool_response:
                    tool_call_ids.append(tool_response['tool_call_id'])

        # Ensure unique IDs only
        return list(set(tool_call_ids))

    def find_assistant_for_tool_call_ids(self, messages, tool_call_ids):
        """Find the assistant message that generated all of the specified tool call IDs.

        Returns the index of the assistant message in messages, or None if not found.
        """
        # Reverse the messages to find the most recent matching assistant first
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if msg.get('role') == 'assistant' and 'tool_calls' in msg:
                # Get all tool call IDs from this assistant
                assistant_ids = {tc['id'] for tc in msg['tool_calls'] if 'id' in tc}

                # Check if all of the requested IDs are in this assistant message
                if all(tc_id in assistant_ids for tc_id in tool_call_ids):
                    return i

        return None

    def validate_consolidated_response(self, message):
        """Validate and potentially fix consolidated response structure."""
        if not self.is_consolidated_response(message):
            return message

        tool_call_ids = self.get_tool_call_ids_from_consolidated(message)

        # Ensure we have a valid structure
        fixed_message = message.copy()

        # If using tool_call_ids format, make sure content is appropriate
        if 'tool_call_ids' in fixed_message and isinstance(fixed_message['tool_call_ids'], list):
            if 'content' not in fixed_message or not fixed_message['content']:
                current_app.logger.warning(
                    "Consolidated response with tool_call_ids has no content. Adding placeholder.")
                fixed_message['content'] = json.dumps({"consolidated_result": "Multiple tools executed"})

        # If using tool_responses format, ensure each response has proper structure
        if 'tool_responses' in fixed_message and isinstance(fixed_message['tool_responses'], list):
            for i, response in enumerate(fixed_message['tool_responses']):
                if 'tool_call_id' not in response:
                    current_app.logger.warning(f"Tool response at index {i} missing tool_call_id. Skipping.")
                    continue

                if 'content' not in response or response['content'] is None:
                    current_app.logger.warning(
                        f"Tool response for {response['tool_call_id']} has null content. Adding empty string.")
                    fixed_message['tool_responses'][i]['content'] = ""

        return fixed_message

    def remove_recipe_prompt_messages(self, messages):
        """
        Remove messages starting with 'Focus on the current task at hand and create a detailed recipe'
        if the last message is 'Execute action'. Only removes from older messages, preserving the
        last two messages regardless of content.
        """
        if len(messages) < 3:  # Need at least 3 messages to have something to remove
            return messages

        # Check if the last message contains "Execute action" pattern
        last_message = messages[-1]
        last_content = last_message.get('content', '')

        # Use regex to match "Execute Action" followed by optional number and colon
        execute_action_pattern = r'execute\s+action\s*\d*\s*:?'
        if not re.search(execute_action_pattern, last_content, re.IGNORECASE):
            return messages

        current_app.logger.info(
            "Last message contains 'Execute action' - checking older messages for recipe prompts to remove")

        # Split messages: process older messages, preserve last 2
        messages_to_process = messages[:-2]  # All except last 2
        last_two_messages = messages[-2:]  # Last 2 messages (always preserved)

        cleaned_older_messages = []
        removed_count = 0

        for i, msg in enumerate(messages_to_process):
            should_remove = False

            if 'content' in msg and isinstance(msg['content'], str):
                # Check if message starts with the recipe prompt pattern
                content = msg['content'].strip()
                if content.startswith('Focus on the current task at hand and create a detailed recipe that includes'):
                    should_remove = True
                    removed_count += 1
                    current_app.logger.info(f"Removing recipe prompt message at index {i}: {content[:100]}...")

            if not should_remove:
                cleaned_older_messages.append(msg)

        if removed_count > 0:
            current_app.logger.info(
                f"Removed {removed_count} recipe prompt messages from older conversation history (preserved last 2 messages)")

        # Combine cleaned older messages with preserved last 2 messages
        return cleaned_older_messages + last_two_messages

    def truncate_content(self, content, max_words=10):
        """Truncate content to specified number of words for logging purposes."""
        if not isinstance(content, str):
            return content

        words = content.split()
        if len(words) <= max_words:
            return content

        truncated = ' '.join(words[:max_words])
        return f"{truncated}... [truncated from {len(words)} words]"

    def create_log_safe_message(self, msg, max_words=10):
        """Create a log-safe version of message with truncated content."""
        log_msg = msg.copy()

        # Truncate main content
        if 'content' in log_msg and log_msg['content']:
            log_msg['content'] = self.truncate_content(log_msg['content'], max_words)

        # Truncate tool_responses content if present
        if 'tool_responses' in log_msg and isinstance(log_msg['tool_responses'], list):
            for i, response in enumerate(log_msg['tool_responses']):
                if 'content' in response and response['content']:
                    log_msg['tool_responses'][i] = response.copy()
                    log_msg['tool_responses'][i]['content'] = self.truncate_content(
                        response['content'], max_words
                    )

        # Truncate tool_calls arguments if they're very large
        if 'tool_calls' in log_msg and isinstance(log_msg['tool_calls'], list):
            for i, tool_call in enumerate(log_msg['tool_calls']):
                if ('function' in tool_call and
                        'arguments' in tool_call['function'] and
                        len(str(tool_call['function']['arguments'])) > 200):
                    log_msg['tool_calls'][i] = tool_call.copy()
                    log_msg['tool_calls'][i]['function'] = tool_call['function'].copy()
                    log_msg['tool_calls'][i]['function']['arguments'] = (
                            str(tool_call['function']['arguments'])[:1000] + "... [truncated]"
                    )

        return log_msg

    def compress_action_messages(self, messages, current_action_id=None):
        """
        Compress 'Execute Action X' messages to 'Action X' for older messages.
        Only applies to messages except the last 2, and only for action IDs less than current_action_id.

        Args:
            messages: List of message dictionaries
            current_action_id: Current action ID (int). If None, will try to detect from recent messages.

        Returns:
            List of messages with compressed action references
        """
        if len(messages) <= 2:
            return messages

        # Auto-detect current action ID if not provided
        if current_action_id is None:
            current_action_id = self._detect_current_action_id(messages)

        # Process all messages except last 2
        messages_to_process = messages[:-2]
        recent_messages = messages[-2:]

        compressed_messages = []

        for msg in messages_to_process:
            if 'content' in msg and isinstance(msg['content'], str):
                compressed_content = self._compress_execute_action_text(
                    msg['content'],
                    current_action_id
                )

                if compressed_content != msg['content']:
                    # Create a copy with compressed content
                    compressed_msg = msg.copy()
                    compressed_msg['content'] = compressed_content
                    compressed_messages.append(compressed_msg)
                    current_app.logger.info(
                        f"Compressed action message: '{msg['content'][:50]}...' -> '{compressed_content[:50]}...'")
                else:
                    compressed_messages.append(msg)
            else:
                compressed_messages.append(msg)

        # Combine compressed messages with recent unmodified messages
        return compressed_messages + recent_messages

    def _detect_current_action_id(self, messages):
        """
        Try to detect the current action ID from recent messages.
        Looks for patterns like 'Execute Action X' or 'Action X' in recent messages.
        """
        # Check last few messages for action patterns
        action_pattern = r'(?:Execute\s+)?Action\s+(\d+)'

        for msg in reversed(messages[-5:]):  # Check last 5 messages
            if 'content' in msg and isinstance(msg['content'], str):
                matches = re.findall(action_pattern, msg['content'], re.IGNORECASE)
                if matches:
                    try:
                        return int(matches[-1])  # Return the last (most recent) action ID found
                    except ValueError:
                        continue

        return None  # Couldn't detect current action ID

    def _compress_execute_action_text(self, content, current_action_id):
        """
        Replace 'Execute Action X' with 'Action X' for action IDs less than current_action_id.

        Args:
            content: Message content string
            current_action_id: Current action ID (int or None)

        Returns:
            String with compressed action references
        """
        if not current_action_id:
            return content

        # Pattern to match "Execute Action X" where X is a number
        pattern = r'Execute\s+Action\s+(\d+)'

        def replace_if_older(match):
            action_id_str = match.group(1)
            try:
                action_id = int(action_id_str)
                if action_id < current_action_id:
                    return f"Action {action_id_str}"
                else:
                    return match.group(0)  # Keep original if not older
            except ValueError:
                return match.group(0)  # Keep original if not a valid number

        return re.sub(pattern, replace_if_older, content, flags=re.IGNORECASE)

    def apply_transform(self, messages: List[Dict]) -> List[Dict]:
        """Applies the tool message handling transformation to ensure valid tool call/response pairings."""
        if not messages:
            current_app.logger.info("ToolMessageHandler: No messages to process")
            return messages
        # Get current action ID from user_tasks
        current_action_id = self.get_current_action_id()

        """Removes the done status to remove ambiguity for agent to reinforce current action completion without just giving status done"""
        messages = self.remove_recipe_prompt_messages(messages)

        """Removes the word Execute for historical actions and not for current action"""
        messages = self.compress_action_messages(messages, current_action_id)

        current_app.logger.info(f"ToolMessageHandler: Processing {len(messages)} messages")
        # DEBUGGING: Print the entire conversation structure with full message details
        current_app.logger.info(f"=== FULL INPUT MESSAGES DEBUG ===")
        for i, msg in enumerate(messages):
            log_safe_msg = self.create_log_safe_message(msg, max_words=70)
            current_app.logger.info(f"Message[{i}]: {json.dumps(log_safe_msg, indent=2)}")
        current_app.logger.info(f"=== END FULL INPUT MESSAGES DEBUG ===")

        # DEBUGGING: Print the entire conversation structure
        current_app.logger.info(f"=== CONVERSATION STRUCTURE DEBUG ===")
        for i, msg in enumerate(messages):
            role = msg.get('role', 'unknown')
            name = msg.get('name', 'unknown')
            tool_calls_info = f", tool_calls=[{','.join([tc.get('id') for tc in msg.get('tool_calls', []) if 'id' in tc])}]" if 'tool_calls' in msg else ""
            tool_call_id_info = f", tool_call_id={msg.get('tool_call_id')}" if 'tool_call_id' in msg else ""

            debug_info = f"Message[{i}]: role={role}, name={name}{tool_calls_info}{tool_call_id_info}"
            current_app.logger.info(debug_info)
        current_app.logger.info(f"=== END CONVERSATION STRUCTURE ===")

        processed_messages = messages.copy()

        # STEP 1: Handle first message if it's a tool message (special case).
        #
        # A LEADING role=tool is an orphan: nothing precedes it to answer, so
        # it is invalid in the OpenAI schema.  It becomes one when the wire
        # left-trim eats the conversation from the front and splits a
        # tool_call/tool-response pair.  Measured live 2026-09-06 (agent
        # 89555447799, action 1 of 24) — the response starts correctly paired
        # and migrates as the trim advances:
        #
        #   10:58:33  [3] assistant Helper tool_calls=[Tqon4dDj]
        #             [4] role=tool  name=Assistant      <- paired, valid
        #   10:58:57  index 1
        #   10:59:16  index 0   <- orphan; its tool_call has been trimmed away
        #   10:59:35  index 0
        #   10:59:40  index 0
        #
        # The three lines below are the right repair: demote to user text,
        # rename, drop the now-dangling tool_call_id.  They were followed by
        # `processed_messages = processed_messages[1:]`, which discarded the
        # message they had just made valid — so the repair was dead code and
        # the tool's OUTPUT was destroyed.
        #
        # google_search really ran (INSIDE google search 10:58:28,044, then
        # five HTTP 200s via primp).  The model never saw the result, replied
        # "I apologize for the error in my previous response...", re-issued the
        # same call, and the unanswered tool_call accumulated to 7 byte-identical
        # copies ("Detected 7 active tool calls").  Action 1 never completed.
        # 3 STEP-1 firings in that window == the 3 snapshots with role=tool at
        # index 0.
        #
        # Keeping the repaired message preserves the tool result as ordinary
        # user-visible context.  It deliberately does NOT try to re-pair the
        # orphan (the assistant tool_call is gone — there is nothing to pair
        # with) and does NOT touch the trimmer; that split is a separate
        # upstream concern.
        if processed_messages and processed_messages[0].get('role') == 'tool':
            current_app.logger.info('GOT TOOL AS FIRST MESSAGE CHANGING IT')
            processed_messages[0]['role'] = 'user'
            processed_messages[0]['name'] = 'Helper'
            if 'tool_call_id' in processed_messages[0]:
                del processed_messages[0]['tool_call_id']

        # STEP 2: Pre-identify consolidated responses and assistants with tool calls
        final_messages = []
        tool_call_mapping = {}  # Maps tool_call_id -> assistant_idx
        pending_tool_calls = []  # Track tool calls that need responses
        assistant_tool_calls = {}  # Track tool calls grouped by assistant message index
        consolidated_responses = []  # Track consolidated responses for later processing

        # First sweep: Identify consolidated responses to prevent them from being processed as regular tool messages
        for i, msg in enumerate(processed_messages):
            if msg.get('role') == 'tool' and self.is_consolidated_response(msg):
                consolidated_responses.append((i, msg))
                # Mark this message to be skipped in the main processing
                processed_messages[i] = {"__skip__": True, "original_index": i}
                current_app.logger.info(f"Marked consolidated response at index {i} for special handling")
            # Also identify all assistant messages with tool calls for later reference
            elif msg.get('role') == 'assistant' and 'tool_calls' in msg:
                for tool_call in msg.get('tool_calls', []):
                    if 'id' in tool_call:
                        tool_call_id = tool_call['id']
                        tool_call_mapping[tool_call_id] = i
                        # We'll populate assistant_tool_calls in the main pass

        # Main pass: Process regular messages, skipping marked consolidated responses
        for i, msg in enumerate(processed_messages):
            # Skip messages marked for special handling
            if isinstance(msg, dict) and "__skip__" in msg:
                continue

            # Track assistant messages with tool calls
            if msg.get('role') == 'assistant' and 'tool_calls' in msg:
                assistant_idx = len(final_messages)
                assistant_tool_calls[assistant_idx] = []

                # Register all tool call IDs from this assistant message
                for tool_call in msg.get('tool_calls', []):
                    if 'id' in tool_call:
                        tool_call_id = tool_call['id']
                        tool_call_mapping[tool_call_id] = assistant_idx
                        pending_tool_calls.append(tool_call_id)  # Add to pending list
                        assistant_tool_calls[assistant_idx].append(tool_call_id)
                        current_app.logger.info(
                            f"Registered tool_call_id {tool_call_id} at assistant index {assistant_idx}")

                final_messages.append(msg)

            # Handle tool messages - ensure they have proper tool_call_id
            elif msg.get('role') == 'tool':
                # If this tool message has a tool_call_id
                if 'tool_call_id' in msg:
                    tool_call_id = msg.get('tool_call_id')

                    # Check if this tool_call_id exists in our mapping
                    if tool_call_id in tool_call_mapping:
                        # If this tool call ID is in our pending list, remove it
                        if tool_call_id in pending_tool_calls:
                            pending_tool_calls.remove(tool_call_id)  # Mark as responded

                        current_app.logger.info(f"Valid tool message at index {i} with tool_call_id {tool_call_id}")
                        final_messages.append(msg)
                    else:
                        # No matching tool_call_id found - convert to user message
                        current_app.logger.warning(f"Tool message with invalid tool_call_id - converting to user")
                        final_messages.append({
                            'role': 'user',
                            'name': 'Helper',
                            'content': msg.get('content', '')
                        })
                else:
                    # Tool message without tool_call_id
                    # Check if it directly follows an assistant message with tool calls
                    if len(final_messages) > 0 and final_messages[-1].get('role') == 'assistant' and 'tool_calls' in \
                            final_messages[-1]:
                        last_assistant_idx = len(final_messages) - 1

                        # Get all pending tool calls from the previous assistant message
                        tool_calls_for_assistant = [tc_id for tc_id in assistant_tool_calls.get(last_assistant_idx, [])
                                                    if tc_id in pending_tool_calls]

                        if len(tool_calls_for_assistant) == 1:
                            # If only one pending tool call, assign it directly
                            tool_call_id = tool_calls_for_assistant[0]
                            current_app.logger.info(f"Adding missing tool_call_id {tool_call_id} to tool message")

                            tool_msg = msg.copy()
                            tool_msg['tool_call_id'] = tool_call_id
                            pending_tool_calls.remove(tool_call_id)  # Mark as responded
                            final_messages.append(tool_msg)

                        elif len(tool_calls_for_assistant) > 1:
                            # Avoid adding duplicate tool responses for the same call IDs
                            # Insert each tool message directly after its matching assistant
                            inserted_count = 0
                            for tool_call_id in tool_calls_for_assistant:
                                if any(m.get("tool_call_id") == tool_call_id and m.get("role") == "tool" for m in
                                       final_messages):
                                    current_app.logger.info(
                                        f"Tool response for {tool_call_id} already exists. Skipping.")
                                    continue

                                assistant_idx = tool_call_mapping.get(tool_call_id)
                                if assistant_idx is None:
                                    current_app.logger.warning(
                                        f"No assistant found for tool_call_id {tool_call_id}. Skipping.")
                                    continue

                                # Find the real index of assistant in final_messages
                                actual_assistant_index = None
                                for j in range(len(final_messages) - 1, -1, -1):
                                    if final_messages[j].get("role") == "assistant" and tool_call_id in [
                                        tc["id"] for tc in final_messages[j].get("tool_calls", []) if "id" in tc
                                    ]:
                                        actual_assistant_index = j
                                        break

                                if actual_assistant_index is None:
                                    current_app.logger.warning(
                                        f"Could not locate assistant message for tool_call_id {tool_call_id}. Skipping.")
                                    continue

                                tool_msg = msg.copy()
                                tool_msg["tool_call_id"] = tool_call_id
                                final_messages.insert(actual_assistant_index + 1 + inserted_count, tool_msg)
                                inserted_count += 1
                                pending_tool_calls.remove(tool_call_id)
                                current_app.logger.info(
                                    f"Inserted tool response for {tool_call_id} after assistant[{actual_assistant_index}]")
                        else:
                            # No pending tool calls for this assistant message
                            current_app.logger.warning(
                                f"Tool message without tool_call_id and no pending calls - converting to user")
                            final_messages.append({
                                'role': 'user',
                                'name': 'Helper',
                                'content': msg.get('content', '')
                            })
                    else:
                        # Tool message without tool_call_id and not following an assistant with tool calls
                        current_app.logger.warning(
                            f"Tool message without tool_call_id and no preceding assistant - converting to user")
                        final_messages.append({
                            'role': 'user',
                            'name': 'Helper',
                            'content': msg.get('content', '')
                        })
            else:
                # For all other message types
                final_messages.append(msg)

        # STEP 3: Process consolidated responses with improved handling
        current_app.logger.info(f"Processing {len(consolidated_responses)} consolidated responses")

        # ────────────────────────────────────────────────────────────────
        #  remember which consolidated-ID sets we have already accepted
        # ────────────────────────────────────────────────────────────────
        seen_consolidated_id_sets: set[frozenset[str]] = set()

        for orig_idx, consolidated_msg in consolidated_responses:
            # Validate and fix the consolidated response structure
            fixed_consolidated = self.validate_consolidated_response(consolidated_msg)

            # Get all tool call IDs from this consolidated response
            tool_call_ids = self.get_tool_call_ids_from_consolidated(fixed_consolidated)

            if not tool_call_ids:
                current_app.logger.warning(
                    f"Consolidated response at original index {orig_idx} "
                    f"has no valid tool_call_ids. Converting to user message.")
                final_messages.append({
                    'role': 'user',
                    'name': 'Helper',
                    'content': fixed_consolidated.get('content', '')
                })
                continue

            # ─── Duplicate guard ───────────────────────────────────────
            id_set = frozenset(tool_call_ids)
            if id_set in seen_consolidated_id_sets:
                current_app.logger.info(
                    "Duplicate consolidated response detected – skipping second copy"
                )
                continue
            seen_consolidated_id_sets.add(id_set)
            # ───────────────────────────────────────────────────────────

            current_app.logger.info(f"Processing consolidated response with tool_call_ids: {tool_call_ids}")

            # First try to find the most likely assistant index from our tool_call_mapping
            most_likely_assistant_idx = None

            # Map each tool_call_id to its assistant original index
            assistant_indices = []
            for tc_id in tool_call_ids:
                if tc_id in tool_call_mapping:
                    assistant_indices.append(tool_call_mapping[tc_id])

            # If we have assistant indices, find the most common one (mode)
            if assistant_indices:
                # Simple mode calculation (most frequent value)
                index_counts = {}
                for idx in assistant_indices:
                    if idx not in index_counts:
                        index_counts[idx] = 0
                    index_counts[idx] += 1

                most_likely_assistant_orig_idx = max(index_counts, key=index_counts.get)

                # Now find this assistant in our final_messages
                for i, msg in enumerate(final_messages):
                    if (msg.get('role') == 'assistant' and
                            'tool_calls' in msg and
                            any(tc.get('id') in tool_call_ids for tc in msg.get('tool_calls', []) if 'id' in tc)):
                        most_likely_assistant_idx = i
                        break

            # If we couldn't find it by mapping, try the usual method
            if most_likely_assistant_idx is None:
                most_likely_assistant_idx = self.find_assistant_for_tool_call_ids(final_messages, tool_call_ids)

            if most_likely_assistant_idx is None:
                current_app.logger.warning(
                    f"Could not find corresponding assistant for consolidated response with tool_call_ids: {tool_call_ids}. Converting to user message.")
                final_messages.append({
                    'role': 'user',
                    'name': 'Helper',
                    'content': fixed_consolidated.get('content', '')
                })
                continue

            current_app.logger.info(
                f"Found corresponding assistant at index {most_likely_assistant_idx} for consolidated response")

            # Insert the consolidated response right after the assistant message
            # Add 1 to position it after the assistant message
            insert_position = most_likely_assistant_idx + 1

            # If we already have tool responses after this assistant,
            # insert after the last one to maintain proper sequence
            for j in range(insert_position, len(final_messages)):
                if final_messages[j].get('role') != 'tool':
                    break
                insert_position = j + 1

            # Insert the consolidated response
            final_messages.insert(insert_position, fixed_consolidated)
            current_app.logger.info(
                f"Inserted consolidated response with {len(tool_call_ids)} tool_call_ids after assistant message at index {most_likely_assistant_idx}")

            # Mark these tool calls as responded
            for tool_call_id in tool_call_ids:
                if tool_call_id in pending_tool_calls:
                    pending_tool_calls.remove(tool_call_id)
                    current_app.logger.info(
                        f"Marked tool_call_id {tool_call_id} as responded via consolidated response")

        # STEP 4: Check for active vs. historical pending tool calls
        if pending_tool_calls:
            # Identify active tool calls from the most recent assistant message
            active_tool_call_ids = set()
            most_recent_assistant_idx = None

            # Find the most recent assistant message with tool calls
            for i in range(len(final_messages) - 1, -1, -1):
                if final_messages[i].get('role') == 'assistant' and 'tool_calls' in final_messages[i]:
                    most_recent_assistant_idx = i
                    break

            if most_recent_assistant_idx is not None:
                # Get tool calls from most recent assistant message
                last_assistant_msg = final_messages[most_recent_assistant_idx]
                active_tool_call_ids = {
                    tc.get('id') for tc in last_assistant_msg.get('tool_calls', [])
                    if 'id' in tc
                }

            # Distinguish between active and historical pending tool calls
            historical_pending_calls = [
                tc_id for tc_id in pending_tool_calls
                if tc_id not in active_tool_call_ids
            ]

            active_pending_calls = [
                tc_id for tc_id in pending_tool_calls
                if tc_id in active_tool_call_ids
            ]

            # Log but don't interfere with active tool calls
            if active_pending_calls:
                current_app.logger.info(
                    f"Detected {len(active_pending_calls)} active tool calls - letting framework handle execution"
                )

            # Only fix historical tool calls with missing responses
            if historical_pending_calls:
                current_app.logger.warning(
                    f"Found {len(historical_pending_calls)} historical tool calls with missing responses"
                )

                # Add placeholders only for historical pending tool calls
                for tool_call_id in historical_pending_calls:
                    if tool_call_id in tool_call_mapping:
                        assistant_idx = tool_call_mapping[tool_call_id]

                        # Only add placeholder if the assistant message still exists
                        if assistant_idx < len(final_messages) and final_messages[assistant_idx].get(
                                'role') == 'assistant':
                            assistant_msg = final_messages[assistant_idx]

                            # Find the function name for this tool call
                            function_name = None
                            for tc in assistant_msg.get('tool_calls', []):
                                if tc.get('id') == tool_call_id and tc.get('type') == 'function':
                                    function_name = tc.get('function', {}).get('name')
                                    break

                            placeholder = {
                                'role': 'tool',
                                'name': function_name or assistant_msg.get('name', 'Assistant'),
                                'tool_call_id': tool_call_id,
                                'content': "Placeholder response for historical tool call"
                            }

                            # Insert the placeholder right after the assistant message
                            insert_position = assistant_idx + 1

                            # If we already have tool responses after this assistant,
                            # insert after the last one to maintain proper sequence
                            for j in range(insert_position, len(final_messages)):
                                if final_messages[j].get('role') != 'tool':
                                    break
                                insert_position = j + 1

                            final_messages.insert(insert_position, placeholder)
                            current_app.logger.info(
                                f"Added placeholder for historical tool_call_id {tool_call_id}"
                            )



        final_messages = self.remove_orphan_tool_messages(final_messages)

        current_app.logger.info(f"Processed {len(messages)} messages into {len(final_messages)} validated messages")
        return self.validate_messages(final_messages)

    def get_logs(self, pre_transform_messages: List[Dict], post_transform_messages: List[Dict]) -> Tuple[str, bool]:
        """Generates logs about the transformation.

        Args:
            pre_transform_messages (List[Dict]): Messages before transformation
            post_transform_messages (List[Dict]): Messages after transformation

        Returns:
            Tuple[str, bool]: A tuple containing the log message and whether a transformation occurred
        """
        if len(pre_transform_messages) != len(post_transform_messages):
            return f"Message count changed: {len(pre_transform_messages)} → {len(post_transform_messages)}", True

        # Count role changes
        changes = 0
        for i in range(min(len(pre_transform_messages), len(post_transform_messages))):
            if pre_transform_messages[i].get('role') != post_transform_messages[i].get('role'):
                changes += 1

        if changes > 0:
            return f"Modified {changes} message roles", True

        return "No message transformations needed", False

class Action:
    def __init__(self,actions):
        self.actions = actions
        self.current_action = 1
        self.fallback = False
        self.new_json = []
        self.recipe = False
        self.ledger = None  # Smart Ledger for persistent task tracking

    def get_action(self, array_index):
        if array_index < 0 or array_index >= len(self.actions):
            raise IndexError(f"Array index {array_index} out of range")

        return self.actions[array_index]

    def get_action_byaction_id(self,action_id):
        for i in self.actions:
            if i['action_id'] == action_id:
                return i
        return None

    def set_ledger(self, ledger):
        """Attach Smart Ledger to this Action instance"""
        self.ledger = ledger
        current_app.logger.info(f"Smart Ledger attached with {len(ledger.tasks)} tasks")

# ── txt2img circuit breaker (T3 — 2026-06-09) ────────────────────────
# Background: aws_rasa.hertzai.com:5459 is a cloud endpoint that may be
# unreachable from local-only installs.  The previous implementation
# called pooled_post() with no timeout, no exception handling, and no
# rate limit — agent_system.log on the installed Nunba flooded with
# urllib3 ConnectionError + 15s ReadTimeout tracebacks every time an
# agent triggered txt2img while offline.  Wasted CPU + 50+ MB of log
# noise per day + every blocked dispatch.
#
# Fix: classic in-memory circuit breaker.  After N consecutive failures
# the breaker OPENS for ``_TXT2IMG_OPEN_SECONDS`` and every call inside
# that window returns immediately without touching the network.  First
# failure of each open-cycle logs as ERROR; subsequent suppressed calls
# log once-per-minute as INFO.  Successful call closes the breaker.
_TXT2IMG_BREAKER = {
    'consecutive_failures': 0,
    'open_until': 0.0,
    'last_suppress_log_at': 0.0,
}
_TXT2IMG_OPEN_AFTER = 3            # fails before opening
_TXT2IMG_OPEN_SECONDS = 300        # 5 min open window
_TXT2IMG_REQUEST_TIMEOUT = 10      # per-request hard cap


def txt2img(text: Annotated[str, "Text to create image"]) -> str:
    import time as _t2i_time
    now = _t2i_time.time()

    # ── Breaker open?  Skip the network call. ─────────────────────────
    if now < _TXT2IMG_BREAKER['open_until']:
        # Rate-limit the suppression log to once per 60s to avoid
        # replacing one flood with another, smaller flood.
        if now - _TXT2IMG_BREAKER['last_suppress_log_at'] > 60:
            _safe_log(
                'info',
                "txt2img: circuit breaker OPEN "
                f"(re-tries at {int(_TXT2IMG_BREAKER['open_until'])}); "
                "returning empty result.",
            )
            _TXT2IMG_BREAKER['last_suppress_log_at'] = now
        return ''  # downstream code handles empty url gracefully

    current_app.logger.info('INSIDE txt2img')
    url = f"http://aws_rasa.hertzai.com:5459/txt2img?prompt={text}"
    payload = ""
    headers = {}

    try:
        response = pooled_post(
            url, headers=headers, data=payload,
            timeout=_TXT2IMG_REQUEST_TIMEOUT,
        )
        result = response.json().get('img_url', '')
    except Exception as exc:
        _TXT2IMG_BREAKER['consecutive_failures'] += 1
        n = _TXT2IMG_BREAKER['consecutive_failures']
        # Log only the FIRST failure of a streak at ERROR; subsequent
        # in-streak failures at DEBUG to avoid log flood.
        if n == 1:
            _safe_log('error', f"txt2img: request failed ({exc})")
        else:
            _safe_log('debug', f"txt2img: request failed (#{n}): {exc}")
        # Open the breaker once the failure threshold is hit.
        if n >= _TXT2IMG_OPEN_AFTER:
            _TXT2IMG_BREAKER['open_until'] = now + _TXT2IMG_OPEN_SECONDS
            _safe_log(
                'warning',
                f"txt2img: circuit breaker OPENED after {n} consecutive "
                f"failures — suppressing for {_TXT2IMG_OPEN_SECONDS}s.",
            )
        return ''

    # ── Success: close the breaker. ──────────────────────────────────
    if _TXT2IMG_BREAKER['consecutive_failures'] > 0:
        _safe_log(
            'info',
            f"txt2img: recovered after "
            f"{_TXT2IMG_BREAKER['consecutive_failures']} failure(s); "
            "circuit breaker CLOSED.",
        )
        _TXT2IMG_BREAKER['consecutive_failures'] = 0
        _TXT2IMG_BREAKER['open_until'] = 0.0
    return result


def get_frame(user_id, frame_store=None):
    """Get latest camera frame - FrameStore first, Redis fallback.

    Args:
        user_id: User/device ID.
        frame_store: Optional FrameStore instance for direct injection.
            Used by embedded devices running headless (no Flask app).
    """
    current_app.logger.info('inside get_frame')

    # Direct FrameStore injection (embedded headless mode)
    if frame_store is not None:
        frame_bytes = frame_store.get_frame(str(user_id))
        if frame_bytes is not None:
            import cv2
            frame = cv2.imdecode(
                np.frombuffer(frame_bytes, np.uint8), cv2.IMREAD_COLOR,
            )
            if frame is not None:
                current_app.logger.info(
                    f"Frame for user_id {user_id} from injected FrameStore")
                return frame[:, :, ::-1]  # BGR → RGB

    # Primary: FrameStore via get_frame_store (in-process, zero latency).
    # Go through the helper so there's one accessor for the store, not
    # `get_vision_service().store` reach-ins scattered across files.
    try:
        from core.safe_hartos_attr import safe_hartos_attr
        get_frame_store = safe_hartos_attr('get_frame_store')
        fs = get_frame_store() if get_frame_store is not None else None
        if fs is not None:
            frame_bytes = fs.get_frame(str(user_id))
            if frame_bytes is not None:
                import cv2
                frame = cv2.imdecode(
                    np.frombuffer(frame_bytes, np.uint8), cv2.IMREAD_COLOR,
                )
                if frame is not None:
                    current_app.logger.info(
                        f"Frame for user_id {user_id} from FrameStore")
                    return frame[:, :, ::-1]  # BGR → RGB
    except Exception:
        pass

    # Fallback: Redis (legacy path)
    serialized_frame = redis_client.get(user_id)
    current_app.logger.info('after redis client')
    try:
        if serialized_frame is not None:
            from security.safe_deserialize import safe_load_frame
            frame_bgr = safe_load_frame(serialized_frame)
            current_app.logger.info(
                f"Frame for user_id {user_id} from Redis")
            frame = frame_bgr[:, :, ::-1]
            return frame
        else:
            current_app.logger.info(f"No frame found for user_id {user_id}.")
            return None
    except ModuleNotFoundError as e:
        raise e

def get_user_camera_inp(inp: Annotated[str, "The Question to check from visual context"],user_id:int,request_id:str) -> str:
    current_app.logger.info('Using Vision to answer question')
    frame = get_frame(str(user_id))
    if frame is not None:
        image_path = f"output_images/{user_id}_{request_id}_call.jpg"
        # Ensure the directory exists
        directory = os.path.dirname(image_path)
        if not os.path.exists(directory):
            os.makedirs(directory)
        # Convert the frame (which is a NumPy array) to a PIL image
        image = Image.fromarray(frame)
        # Save the image
        image.save(image_path)
        # Tier 0: Try Qwen+mmproj on local llama-server (already running)
        _llm_port = int(os.environ.get('HEVOLVE_LLM_PORT', 8080))
        try:
            import base64 as _b64
            with open(image_path, 'rb') as _imgf:
                _img_b64 = _b64.b64encode(_imgf.read()).decode('ascii')
            _prompt_text = f'Instruction: Respond in second person point of view\ninput:-{inp}'
            _vlm_r = requests.post(
                f'http://127.0.0.1:{_llm_port}/v1/chat/completions',
                json={'model': 'local', 'messages': [{'role': 'user', 'content': [
                    {'type': 'text', 'text': _prompt_text},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{_img_b64}'}}
                ]}], 'max_tokens': 300},
                timeout=15,
            )
            if _vlm_r.status_code == 200:
                _c = _vlm_r.json().get('choices', [{}])[0].get('message', {}).get('content', '')
                if _c:
                    return _c
        except Exception:
            pass  # Fall through to MiniCPM/cloud

        from core.config_cache import get_vision_api
        url = get_vision_api() or "http://azurekong.hertzai.com:8000/minicpm/upload"
        payload = {
            'prompt': f'Instruction: Respond in second person point of view\ninput:-{inp}'}
        files = [
            ('file', ('call.jpg', open(image_path, 'rb'), 'image/jpeg'))
        ]
        headers = {}
        try:
            response = pooled_post(
                url, headers=headers, data=payload, files=files, timeout=30)
            current_app.logger.info(response.text)
            response = response.text

            return response
        except Exception as e:
            current_app.logger.info('ERROR: Got error in visual QA: %s', e)
            return 'failed to get visual context ask user to check if the camera is turned on'
    else:
        return 'failed to get visual context ask user to check if the camera is turned on'



# ── Deterministic recall-window resolution (#121 follow-up) ──────────────────
# A vague human recall ("what did we discuss 15 days back") AND a small model's
# fuzzy date arithmetic both make the exact day unreliable. resolve_recall_window
# turns the model's start/end into a forgiving [lo, hi] window so a conversation
# a day or two off the target is still caught — deterministically, with no model
# involvement (the model just supplies its best-guess date).
RECALL_WINDOW_PAD_DAYS = 2  # ± padding (days) applied to a single-date recall


def _parse_recall_date(s):
    """Parse an ISO-8601 / bare-date string. Returns (datetime, is_bare_date)
    or (None, False). is_bare_date is True for 'YYYY-MM-DD' (no time component)
    so callers can expand it to full-day bounds instead of the midnight instant.
    """
    from datetime import datetime as _dt
    if not s or not isinstance(s, str):
        return None, False
    s2 = s.strip().rstrip('Z').rstrip('z')
    if not s2 or s2.lower() in ('none', 'null', 'na'):
        return None, False
    for fmt, bare in (('%Y-%m-%dT%H:%M:%S.%f', False),
                      ('%Y-%m-%dT%H:%M:%S', False),
                      ('%Y-%m-%d %H:%M:%S', False),
                      ('%Y-%m-%d', True)):
        try:
            return _dt.strptime(s2, fmt), bare
        except ValueError:
            continue
    return None, False


def resolve_recall_window(start_date, end_date, pad_days=RECALL_WINDOW_PAD_DAYS):
    """Deterministically resolve (start_date, end_date) into a [lo, hi] datetime
    window for ConversationEntry filtering, or None when neither parses (caller
    falls back to semantic search). Rules — no model involvement:

      * neither parses              -> None (semantic fallback)
      * a single point (one side
        given, or both equal)       -> that calendar day, padded by ±pad_days,
                                       so an approximate "N days back" still
                                       catches conversations a day or two off
      * an explicit two-sided range -> honoured as given; a BARE date expands to
                                       full-day bounds so a 1-day range covers the
                                       whole day, not a single instant (midnight)
    """
    from datetime import timedelta
    s, s_bare = _parse_recall_date(start_date)
    e, e_bare = _parse_recall_date(end_date)
    if s is None and e is None:
        return None
    pad = timedelta(days=max(0, int(pad_days)))
    if s is None or e is None or s == e:            # single point -> padded window
        anchor = s if s is not None else e
        day_lo = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
        day_hi = anchor.replace(hour=23, minute=59, second=59, microsecond=999999)
        return day_lo - pad, day_hi + pad
    lo = s.replace(hour=0, minute=0, second=0, microsecond=0) if s_bare else s
    hi = e.replace(hour=23, minute=59, second=59, microsecond=999999) if e_bare else e
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def get_time_based_history(prompt: str, session_id: str, start_date: str, end_date: str):
    '''
    Time-filtered + semantic conversation history retrieval (CANONICAL impl).

    Replaces the removed Zep backend (#121). If start_date/end_date parse as
    ISO-8601, queries ConversationEntry with a created_at BETWEEN range;
    otherwise falls back to SimpleMem semantic search. The autogen
    get_chat_history tool (core/agent_tools.py) + reuse_recipe call this;
    hart_intelligence_entry's same-named wrapper delegates here. Ported verbatim
    from the working hart_intelligence_entry implementation so there is ONE
    date-recall impl, not a langchain-vs-autogen fork.

    inputs:
        prompt: text to semantically search (empty for a pure time-range pull)
        session_id: 'user_{user_id}'
        start_date / end_date: ISO-8601 (or empty / sentinel = no bound)
    '''
    import json as _json
    start_time = time.time()
    try:
        user_id = int(session_id.replace("user_", ""))
    except Exception as e:
        try:
            current_app.logger.warning(f"get_time_based_history: bad session_id {session_id}: {e}")
        except Exception:
            pass
        return _json.dumps({'res': []})

    window = resolve_recall_window(start_date, end_date)

    if window is not None:
        win_lo, win_hi = window
        try:
            from integrations.social._models_local import ConversationEntry
            from integrations.social.models import get_db
            results = []
            db = get_db()
            try:
                q = db.query(ConversationEntry).filter(
                    ConversationEntry.user_id == str(user_id),
                    ConversationEntry.created_at >= win_lo,
                    ConversationEntry.created_at <= win_hi,
                )
                rows = q.order_by(
                    ConversationEntry.created_at.desc()
                ).limit(50).all()
                for r in rows:
                    results.append({
                        'message': {
                            'content': getattr(r, 'content', '') or '',
                            'role': getattr(r, 'role', 'assistant'),
                        },
                        'created_at': (r.created_at.isoformat()
                                       if r.created_at else ''),
                        'channel_type': getattr(r, 'channel_type', ''),
                    })
            finally:
                try:
                    db.close()
                except Exception:
                    pass
            try:
                current_app.logger.info(
                    f"Time-filtered history: {len(results)} rows in "
                    f"{time.time() - start_time:.3f}s (window={win_lo}..{win_hi})"
                )
            except Exception:
                pass
            return _json.dumps({'res_in_filter': results})
        except Exception as e:
            try:
                current_app.logger.warning(
                    f"Time-filtered ConversationEntry query failed, "
                    f"falling back to semantic: {e}"
                )
            except Exception:
                pass

    try:
        from integrations.channels.memory.simplemem_langchain import SimpleMemChatMemory
        memory = SimpleMemChatMemory.load_or_create(user_id)
        results = memory.semantic_search(prompt)
        if results:
            serialized = []
            for r in results:
                item = {'message': {'content': r.get('content', ''),
                                    'role': r.get('role', 'assistant')}}
                # Attach the timestamp so the model can date each memory it gets
                # back. SimpleMem stores it under varying keys across versions.
                ts = (r.get('created_at') or r.get('timestamp') or r.get('ts')
                      or (r.get('metadata') or {}).get('created_at'))
                if ts:
                    item['created_at'] = ts
                serialized.append(item)
            final_res = {'res_in_filter': serialized}
        else:
            final_res = {'res_in_filter': []}
        try:
            current_app.logger.info(
                f"SimpleMem search took {time.time() - start_time:.3f}s, "
                f"{len(results)} results"
            )
        except Exception:
            pass
        return _json.dumps(final_res)
    except Exception as e:
        try:
            current_app.logger.warning(f"SimpleMem search failed: {e}")
        except Exception:
            pass
        return _json.dumps({'res': []})

def parse_date(date_str):
    return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")

def get_visual_context(user_id,mins=5):
    '''
        This function help to extract action that user have perfomed till time
    '''
    # action_url = f"{ACTION_API}?user_id={user_id}"
    action_url = get_visual_context_api(user_id, mins)
    # Todo: get, and populate timezone from client
    time_zone = "Asia/Kolkata"

    india_tz = pytz.timezone(time_zone)

    payload = {}
    headers = {}

    response = pooled_request(
        "GET", action_url, headers=headers, data=payload)

    if response.status_code == 200:
        data = response.json()
        filtered_data_video = [
            obj for obj in data if obj["zeroshot_label"] == 'Video Reasoning']
        # Process video data
        video_context_texts = []
        for obj in filtered_data_video:
            action = obj["action"]
            date = parse_date(obj["created_date"])
            gpt3_label = obj["gpt3_label"]
            if gpt3_label == 'Visual Context':
                now = datetime.now()
                # Check if the action is older than 5 minutes
                if (now - date) > timedelta(minutes=mins):
                    continue
            first_action_text = f"{action} on {date.astimezone(india_tz).strftime('%Y-%m-%dT%H:%M:%S')}"

            video_context_texts.append(first_action_text)
        if video_context_texts:
            return video_context_texts[:10]
        else:
            return None
    else:
        return None


def get_screen_context(user_id, mins=2):
    '''
        Get recent screen understanding descriptions (shorter window than visual).
        Screen context goes stale faster - default 2 minute window.
    '''
    action_url = get_visual_context_api(user_id, mins)
    time_zone = "Asia/Kolkata"
    india_tz = pytz.timezone(time_zone)

    try:
        response = pooled_request("GET", action_url, headers={}, data={})
    except Exception:
        return None

    if response.status_code == 200:
        data = response.json()
        filtered_data_screen = [
            obj for obj in data if obj["zeroshot_label"] == 'Screen Reasoning']
        screen_context_texts = []
        for obj in filtered_data_screen:
            action = obj["action"]
            date = parse_date(obj["created_date"])
            now = datetime.now()
            if (now - date) > timedelta(minutes=mins):
                continue
            screen_text = f"{action} on {date.astimezone(india_tz).strftime('%Y-%m-%dT%H:%M:%S')}"
            screen_context_texts.append(screen_text)
        if screen_context_texts:
            return screen_context_texts[:10]
        else:
            return None
    else:
        return None

def search_visual_history(user_id, query, mins=30, channel='both'):
    '''
        Search past camera/screen descriptions by substring match within a time window.
        Reuses the same DB endpoint as get_visual_context/get_screen_context.
        channel: 'camera', 'screen', or 'both'
    '''
    action_url = get_visual_context_api(user_id, mins)
    time_zone = "Asia/Kolkata"
    india_tz = pytz.timezone(time_zone)

    try:
        response = pooled_request("GET", action_url, headers={}, data={})
    except Exception:
        return None

    if response.status_code != 200:
        return None

    data = response.json()
    query_lower = query.lower()
    results = []

    for obj in data:
        label = obj.get("zeroshot_label", "")
        # Filter by channel
        if channel == 'camera' and label != 'Video Reasoning':
            continue
        if channel == 'screen' and label != 'Screen Reasoning':
            continue
        if channel == 'both' and label not in ('Video Reasoning', 'Screen Reasoning'):
            continue

        action = obj.get("action", "")
        # Substring match on query
        if query_lower and query_lower not in action.lower():
            continue

        date = parse_date(obj["created_date"])
        now = datetime.now()
        if (now - date) > timedelta(minutes=mins):
            continue

        ch = 'camera' if label == 'Video Reasoning' else 'screen'
        results.append(f"[{ch}] {action} at {date.astimezone(india_tz).strftime('%Y-%m-%dT%H:%M:%S')}")

    return results[:20] if results else None


def get_memory(user_id: int):
    '''
        Get memory object from zep
    '''
    from langchain_classic.memory import ZepMemory  # lazy (see helper.py:32)
    session_id = "user_"+str(user_id)
    memory = ZepMemory(
        session_id=session_id,
        url=ZEP_API_URL,
        memory_key="chat_history",
        api_key=ZEP_API_KEY,
        return_messages=True,
        input_key="input"
    )
    return memory

def history(user_id,prompt_id,role,message):
    # lazy import: langchain_classic.schema (see helper.py:32)
    from langchain_classic.schema import HumanMessage, AIMessage
    try:
        memory = get_memory(user_id=int(user_id))
    except Exception:
        return "Invalid user ID"
    if memory:
        if role == 'user':
            memory.chat_memory.add_message(
                HumanMessage(content=message),
                metadata={'prompt_id': prompt_id}
            )
        else:
            memory.chat_memory.add_message(
                AIMessage(content=message),
                metadata={'prompt_id': prompt_id}
            )
        return "Messages are saved!!!"
    else:
        return "Memory object not found"


# The autogen config_list comes from core.autogen_config, the ONE place that
# decides which LLM this node talks to (the configured endpoint, or the local
# llama-server when none is configured).  This module used to rebuild it
# inline with its own model names ('gpt-4.1-mini', 'Qwen3-VL-4B-Instruct'),
# which is how a node could run a model nobody configured (#69).
from core.autogen_config import get_autogen_config_list
config_list = get_autogen_config_list()

llm_config = {
    "config_list": config_list,
    "cache_seed": None
}


def get_llm_config(fallback_config_list=None):
    """Get LLM config — checks thread-local override before falling back to given config_list.
    This enables per-dispatch model routing for speculative execution.

    Args:
        fallback_config_list: config_list to use when no thread-local override is set.
                              Defaults to this module's config_list.
    """
    from hartos.threadlocal import thread_local_data
    override = thread_local_data.get_model_config_override()
    return {"cache_seed": None, "config_list": override or (fallback_config_list if fallback_config_list is not None else config_list), "max_tokens": 1500}


def format_action_text(text):
    """Format VLM action JSON into human-readable step description.

    Canonical implementation — create_recipe.py and reuse_recipe.py delegate here.
    Handles JSON dict, ast.literal_eval fallback, regex fallback.
    """
    if text.strip().startswith("{") and "action" in text:
        try:
            try:
                action_data = json.loads(text.strip())
            except (json.JSONDecodeError, ValueError):
                action_data = ast.literal_eval(text.strip())
            action_type = action_data.get("action", "")

            if action_type == "mouse_move":
                return "Move mouse"
            elif action_type == "left_click":
                return "Perform left click"
            elif action_type == "right_click":
                return "Perform right click"
            elif action_type == "double_click":
                return "Perform double click"
            elif action_type == "type" and "text" in action_data:
                return f"Type '{action_data['text']}'"
            elif action_type == "drag":
                return "Perform drag action"
            else:
                return f"Perform {action_type} action"
        except Exception:
            action_match = re.search(r"'action':\s*'([^']+)'", text)
            text_match = re.search(r"'text':\s*'([^']+)'", text)
            if action_match:
                action_type = action_match.group(1)
                if action_type == "type" and text_match:
                    return f"Type '{text_match.group(1)}'"
                elif action_type == "mouse_move":
                    return "Move mouse"
                elif action_type == "left_click":
                    return "Perform left click"
                elif action_type == "right_click":
                    return "Perform right click"
                elif action_type == "double_click":
                    return "Perform double click"
                else:
                    return f"Perform {action_type} action"
            else:
                return "Perform action"
    elif "Perform" in text and "action" in text:
        return text
    return text


def save_conversation_db(text, user_id, prompt_id, database_url, request_id):
    """Save a conversation turn to the database via the conversation API.

    Canonical implementation — create_recipe.py and reuse_recipe.py delegate here.
    """
    headers = {'Content-Type': 'application/json'}
    data = {
        "request": 'VIDEO GENERATION FROM GENERATE_VIDEO',
        "response": text.strip(),
        "user_id": int(user_id),
        "conv_bot_name": 'GPT-4o',
        "topic": f'{prompt_id}',
        "revision": False,
        "dialogue_id": None,
        "card_type": 'Custom GPT',
        "qid": None,
        "layout_id": None,
        "layout_list": '[]',
        "request_token": 0,
        "response_token": 0,
        "request_id": request_id,
        "historical_request_id": str('[]')
    }
    res = pooled_post("{}/conversation".format(database_url),
                        data=json.dumps(data), headers=headers).json()
    conv_id = res['conv_id']
    return conv_id


def create_visual_agent(user_id,prompt_id):
    visual_agent = autogen.AssistantAgent(
        name='visual_agent',
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=_is_terminate_msg,
        code_execution_config={"work_dir": get_coding_workspace_dir(), "use_docker": False},
        system_message="You are an helpful AI assistant used to perform visual based tasks given to you. "
    )

    visual_user = autogen.UserProxyAgent(
        name=f"UserProxy",
        human_input_mode="NEVER",
        llm_config=False,
        is_termination_msg=_is_terminate_msg,
        max_consecutive_auto_reply=0,
        code_execution_config=False,
    )
    helper2 = autogen.AssistantAgent(
        name="Helper",
        llm_config=llm_config,
        code_execution_config={"work_dir": get_coding_workspace_dir(), "use_docker": False},
        system_message=f"""You are Helper Agent. Help the visual_agent to complete the task:
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than ,Executor,multi_role_agent.
            4. Tools you have [txt2img, img2txt, save_data_in_memory, get_data_from_memory, get_user_id, get_prompt_id, Generate_video, get_user_uploaded_file, get_user_camera_inp, get_chat_history, create_scheduled_jobs] if you have any task which is not doable by these tool check recipe first else create python code to do so
            5. Keep track of action and only go to next action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the, always use this format: @user {{'message_2_user':'message here'}}
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
            When writing code, always print the final response just before returning it.
        """,
        is_termination_msg=_is_terminate_msg,
    )
    executor2 = autogen.AssistantAgent(
        name="Executor",
        llm_config=llm_config,
        code_execution_config={"last_n_messages":2,"work_dir": get_coding_workspace_dir(), "use_docker": False},
        system_message=f'''You are a executor agent. focused solely on creating, running & debugging code.
            Your responsibilities:
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than,Executor,multi_role_agent.
            4. Tools Helper Agent can use [send_message_in_seconds,send_message_to_user,send_presynthesized_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory]
            5. Keep track of action and only go to next action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continuously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the user, always use this format: @user {{'message_2_user':'message here'}}
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.

            Note: Your Working Directory is "{os.getcwd()}" - CRITICAL: When writing code, ALWAYS use os.path.join(os.getcwd(), filename) for file paths. NEVER hardcode paths like '/home/user/path'.
            Add proper error handling, logging.
            Always provide clear execution results or error messages to the assistant.
            if you get any conversation which is not related to coding ask the manager to route this conversation to user
            When writing code, always print the final response just before returning it.
        ''',
        is_termination_msg=_is_terminate_msg,
    )
    multi_role_agent2 = autogen.AssistantAgent(
        name="multi_role_agent",
        llm_config=llm_config,
        code_execution_config=False,
        system_message="""You will send message from multiple different personas your, job is to ask those question to assistant agent
        if you think some text was intent to give to some other agent but i came to you send the same message to user""",
    )
    verify2 = autogen.AssistantAgent(
        name="StatusVerifier",
        llm_config=llm_config,
        code_execution_config=False,
        system_message=""""You are an Status verification agent.
        Role: Track and verify the status of actions. Provide updates strictly in JSON format only when status is completed.
        Response formats:
            1. Action Completed Successfully: {"status": "completed","action": "current action","action_id": 1/2/3...,"message": "message here"}
            2. Action Error: {"status": "error","action": "current action","action_id": 1/2/3...,"message": "message here"}
            3. Action Pending: {"status": "pending","action": "current action","action_id": 1/2/3...,"message": "pending actions here"}
            4. Action Requires Breakdown: {"status": "requires_breakdown","action": "current action","action_id": 1/2/3...,"reason": "Why this action needs to be broken down","subtasks": [{"subtask_id": "1.1","description": "First subtask description","depends_on": [],"can_perform_autonomously": true},{"subtask_id": "1.2","description": "Second subtask","depends_on": ["1.1"],"can_perform_autonomously": true}]}
        Important Instructions:
            Only mark an action as "Completed" if the Assistant Agent confirms successful completion.
            For pending tasks or ongoing actions, respond to helper to complete the task.
            Verify the action performed by assistant and make sure the action is performed correctly as per instructions. if action performed was not as per instructions give the pending actions to the helper agent.
            Report status only-do not perform actions yourself.
            Use "requires_breakdown" when an action is too complex and needs to be split into smaller subtasks.

        """,
        is_termination_msg=_is_terminate_msg,
    )

    chat_instructor2 = autogen.UserProxyAgent(
        name="ChatInstructor",
        human_input_mode="NEVER",
        max_consecutive_auto_reply=10,
        default_auto_reply="TERMINATE",
        code_execution_config=False,
        is_termination_msg=_is_terminate_msg,
    )

    context_handling = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=50,keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=3500, max_tokens_per_message=1000, min_tokens=0),
            ToolMessageHandler(),
        ]
    )
    context_handling.add_to_agent(visual_agent)
    context_handling.add_to_agent(helper2)
    context_handling.add_to_agent(executor2)
    context_handling.add_to_agent(multi_role_agent2)
    context_handling.add_to_agent(verify2)
    # See chat_instructor rationale at create_recipe.py:903 — visual_agent
    # path uses chat_instructor2 (UserProxyAgent line 2047) the same way;
    # it needs the same buffer cap to avoid llama.cpp n_ctx overflow.
    context_handling.add_to_agent(chat_instructor2)

    return visual_agent, visual_user, helper2, executor2, multi_role_agent2, verify2, chat_instructor2


# Create agent_data directory if it doesn't exist.
# When running from a read-only install dir (e.g. C:\Program Files on Windows),
# derive the path from HEVOLVE_DB_PATH so writes go to a writable user directory.
def _resolve_agent_data_dir():
    """Resolve agent_data directory, preferring the DB path's parent for bundled apps."""
    db_path = os.environ.get('HEVOLVE_DB_PATH', '')
    if db_path and db_path != ':memory:' and os.path.isabs(db_path):
        # Use sibling directory to the database file
        return os.path.join(os.path.dirname(db_path), 'agent_data')
    # Bundled/frozen mode: use writable user directory (Program Files is read-only)
    from core.config_cache import is_bundled as _is_bundled_check
    if _is_bundled_check():
        try:
            from core.platform_paths import get_agent_data_dir
            return get_agent_data_dir()
        except ImportError:
            return os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'agent_data')
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'agent_data')

AGENT_DATA_DIR = _resolve_agent_data_dir()
try:
    if not os.path.exists(AGENT_DATA_DIR):
        os.makedirs(AGENT_DATA_DIR, exist_ok=True)
except PermissionError:
    # Fallback: user home directory (e.g. bundled app in Program Files)
    try:
        from core.platform_paths import get_agent_data_dir as _get_agent_fallback
        AGENT_DATA_DIR = _get_agent_fallback()
    except ImportError:
        AGENT_DATA_DIR = os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'agent_data')
    os.makedirs(AGENT_DATA_DIR, exist_ok=True)
    logging.getLogger(__name__).warning(f"agent_data dir redirected to {AGENT_DATA_DIR} (install dir not writable)")


def get_agent_data_file_path(prompt_id: int) -> str:
    """Get the file path for storing agent data for a specific prompt_id"""
    return os.path.join(AGENT_DATA_DIR, f"{prompt_id}_agent_data.json")


def save_agent_data_to_file(prompt_id: int, agent_data: Dict) -> bool:
    """
    Save current agent_data[prompt_id] to a JSON file

    Args:
        prompt_id: The prompt ID to save data for
        agent_data: The agent data dictionary
    Returns:
        bool: True if saved successfully, False otherwise
    """
    try:
        file_path = get_agent_data_file_path(prompt_id)

        # Get current agent data for this prompt_id
        data_to_save = agent_data.get(prompt_id, {})

        # Add metadata about when this was saved
        save_metadata = {
            "prompt_id": prompt_id,
            "saved_at": datetime.now().isoformat(),
            "data": data_to_save
        }

        # Write to file with encryption (falls back to plaintext if no key configured)
        try:
            from security.crypto import encrypt_json_file
            encrypt_json_file(file_path, save_metadata)
        except ImportError:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(save_metadata, f, indent=2, ensure_ascii=False)

        current_app.logger.info(f" Saved agent data to: {file_path}")
        return True

    except Exception as e:
        current_app.logger.error(f" Error saving agent data for prompt_id {prompt_id}: {e}")
        return False


def load_agent_data_from_file(prompt_id: int, agent_data: Dict) -> bool:
    """
    Load agent_data[prompt_id] from JSON file

    Args:
        prompt_id: The prompt ID to load data for
        agent_data: The agent data dictionary

    Returns:
        bool: True if loaded successfully, False otherwise
    """
    try:
        file_path = get_agent_data_file_path(prompt_id)

        # Check if file exists
        if not os.path.exists(file_path):
            current_app.logger.info(f"[FILE] No saved agent data found for prompt_id {prompt_id}")
            # Initialize with default data
            agent_data[prompt_id] = {}
            return False

        # Load from file (supports encrypted and plaintext)
        try:
            from security.crypto import decrypt_json_file
            loaded_data = decrypt_json_file(file_path)
            if loaded_data is None:
                current_app.logger.warning(f"Failed to decrypt/load: {file_path}")
                agent_data[prompt_id] = {}
                return False
        except ImportError:
            with open(file_path, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)

        # Extract the actual data (skip metadata)
        if 'data' in loaded_data:
            agent_data[prompt_id] = loaded_data['data']
            current_app.logger.info(f" Loaded agent data from: {file_path}")
            # Guard the diagnostic: a non-dict payload (e.g. a list) has no
            # .keys(), and letting that AttributeError propagate would discard
            # data that was already extracted cleanly, dropping the load into
            # the error path (return False, agent_data reset to {}). A logging
            # line must never corrupt a successful load.
            _loaded = agent_data[prompt_id]
            current_app.logger.info(
                f" Loaded data keys: "
                f"{list(_loaded.keys()) if isinstance(_loaded, dict) else type(_loaded).__name__}")
            return True
        else:
            # Handle old format (direct data)
            agent_data[prompt_id] = loaded_data
            current_app.logger.info(f" Loaded agent data (old format) from: {file_path}")
            return True

    except Exception as e:
        current_app.logger.error(f" Error loading agent data for prompt_id {prompt_id}: {e}")
        # Initialize with default data on error
        agent_data[prompt_id] = {}
        return False


def schedule_periodic_backups(agent_data, scheduler):
    """Schedule periodic backups of agent data"""

    def backup_all_agent_data():
        """Backup all active agent data"""
        backup_count = 0
        for prompt_id in agent_data.keys():
            if agent_data[prompt_id]:  # Only backup if there's data
                if backup_agent_data_file(prompt_id):
                    backup_count += 1

    # Schedule daily backups at 2 AM
    if scheduler.running:
        scheduler.add_job(
            backup_all_agent_data,
            'cron',
            hour=2,
            minute=0,
            id='periodic_agent_data_backup'
        )


def initialize_persistent_storage(agent_data: Dict):
    """
    Initialize persistent storage and migrate existing data
    Call this during application startup
        Args:
        agent_data: The agent data dictionary

    """
    try:
        # Create agent_data directory if it doesn't exist
        if not os.path.exists(AGENT_DATA_DIR):
            os.makedirs(AGENT_DATA_DIR)

        return True

    except Exception as e:
        return False


def backup_agent_data_file(prompt_id: int, keep_count: int = 5) -> bool:
    """Create a timestamped backup of the agent data file and prune old ones.

    Two responsibilities bundled because a "maintain the backup set for
    prompt_id" operation is conceptually one thing — every caller that
    wants a new backup also wants the backup directory bounded, otherwise
    copies accumulate forever (the exact bug that left `cleanup_old_backups`
    orphaned for months).

    Args:
        prompt_id: The prompt ID to backup data for.
        keep_count: How many most-recent backups to retain. Older ones
            are deleted by cleanup_old_backups() after the new backup
            is written. Defaults to 5.

    Returns:
        bool: True if the NEW backup was written successfully. Cleanup
        failures are non-fatal (logged inside cleanup_old_backups) so
        a failing prune doesn't mask a successful backup.
    """
    try:
        file_path = get_agent_data_file_path(prompt_id)

        if not os.path.exists(file_path):
            return False

        # Create backup with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = file_path.replace('.json', f'_backup_{timestamp}.json')

        # Copy file
        import shutil
        shutil.copy2(file_path, backup_path)

        current_app.logger.info(f" Created backup: {backup_path}")

        # Rotation: prune older backups beyond keep_count. Non-fatal —
        # cleanup_old_backups swallows its own exceptions and returns 0
        # on failure so this call can't undo the successful backup above.
        cleanup_old_backups(prompt_id, keep_count=keep_count)
        return True

    except Exception as e:
        current_app.logger.error(f" Error creating backup for prompt_id {prompt_id}: {e}")
        return False


def cleanup_old_backups(prompt_id: int, keep_count: int = 5) -> int:
    """
    Clean up old backup files, keeping only the most recent ones

    Args:
        prompt_id: The prompt ID to clean backups for
        keep_count: Number of backup files to keep

    Returns:
        int: Number of files deleted
    """
    try:
        backup_pattern = f"{prompt_id}_agent_data_backup_"
        backup_files = []

        # Find all backup files for this prompt_id
        for filename in os.listdir(AGENT_DATA_DIR):
            if filename.startswith(backup_pattern) and filename.endswith('.json'):
                file_path = os.path.join(AGENT_DATA_DIR, filename)
                # Get file modification time
                mtime = os.path.getmtime(file_path)
                backup_files.append((mtime, file_path))

        # Sort by modification time (newest first)
        backup_files.sort(reverse=True)

        # Delete old backups
        deleted_count = 0
        for i, (mtime, file_path) in enumerate(backup_files):
            if i >= keep_count:  # Keep only the newest keep_count files
                os.remove(file_path)
                deleted_count += 1
                current_app.logger.info(f" Deleted old backup: {file_path}")

        return deleted_count

    except Exception as e:
        current_app.logger.error(f" Error cleaning up backups for prompt_id {prompt_id}: {e}")
        return 0


def get_agent_data_info(prompt_id: int) -> Dict[str, Any]:
    """
    Get information about saved agent data file

    Args:
        prompt_id: The prompt ID to get info for

    Returns:
        dict: Information about the file
    """
    try:
        file_path = get_agent_data_file_path(prompt_id)

        if not os.path.exists(file_path):
            return {"exists": False, "path": file_path}

        # Get file stats
        stat = os.stat(file_path)

        # Try to get save metadata
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            saved_at = data.get('saved_at', 'unknown')
            data_keys = list(data.get('data', {}).keys()) if 'data' in data else list(data.keys())
        except Exception:
            saved_at = 'unknown'
            data_keys = []

        return {
            "exists": True,
            "path": file_path,
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "saved_at": saved_at,
            "data_keys": data_keys
        }

    except Exception as e:
        current_app.logger.error(f" Error getting agent data info for prompt_id {prompt_id}: {e}")
        return {"exists": False, "error": str(e)}


# ========================================================================================
# AUTOGEN JSON HANDLING ENHANCEMENT
# ========================================================================================
def safe_function_call(func, arguments):
    """Fixed version that handles list with dict properly"""
    import logging

    logger = logging.getLogger("safe_function_call")

    logger.info(" SAFE_FUNCTION_CALL DEBUG:")
    logger.info(f"   Function: {func.__name__ if hasattr(func, '__name__') else func}")
    logger.info(f"   Arguments type: {type(arguments)}")
    logger.info(f"   Arguments content: {arguments}")

    try:
        # Try original AutoGen approach first
        if isinstance(arguments, dict):
            logger.info("   → Using **kwargs approach")
            result = func(**arguments)
            logger.info("    Success with **kwargs")
            return result

        # Handle list case - FIXED LOGIC
        elif isinstance(arguments, list):
            logger.info("   → Analyzing list content")

            # Check if first item is a dict (common pattern from retrieve_json)
            if len(arguments) >= 1 and isinstance(arguments[0], dict):
                # The first item is the actual arguments dict
                actual_args = arguments[0]
                logger.info(f"   → Found dict in list[0]: {actual_args}")
                logger.info("   → Using **kwargs approach on extracted dict")
                result = func(**actual_args)
                logger.info("    Success with **kwargs from list")
                return result
            else:
                # Fallback to treating as positional args
                logger.info("   → Using *args approach")
                result = func(*arguments)
                logger.info("    Success with *args")
                return result

        # Handle single argument case
        else:
            logger.info("   → Using single argument approach")
            result = func(arguments)
            logger.info("    Success with single arg")
            return result

    except TypeError as e:
        logger.error(f"    TypeError: {e}")
        logger.error(f"   TypeError traceback:\n{traceback.format_exc()}")

        # Enhanced intelligent mapping for lists
        if isinstance(arguments, list):
            logger.info("   → Trying enhanced list handling")

            try:
                # If it's a list with a dict, extract the dict
                if len(arguments) >= 1 and isinstance(arguments[0], dict):
                    logger.info("   → Extracting dict from list and retrying")
                    result = func(**arguments[0])
                    logger.info("    Success with extracted dict")
                    return result

                # If it's a simple list, try intelligent parameter mapping
                elif hasattr(func, '__annotations__'):
                    import inspect
                    sig = inspect.signature(func)
                    param_names = list(sig.parameters.keys())
                    logger.info(f"   → Function expects parameters: {param_names}")

                    # Filter out truncation indicators
                    clean_args = [arg for arg in arguments if
                                  not (isinstance(arg, list) and len(arg) == 1 and arg[0] == 'truncated')]

                    if len(clean_args) <= len(param_names):
                        kwargs = dict(zip(param_names, clean_args))
                        logger.info(f"   → Mapped to kwargs: {kwargs}")
                        result = func(**kwargs)
                        logger.info("    Success with intelligent mapping")
                        return result

            except Exception as mapping_error:
                logger.error(f"    Enhanced list handling failed: {mapping_error}")
                logger.error(f"   Mapping traceback:\n{traceback.format_exc()}")

        # Re-raise if we can't handle it
        logger.error("    Cannot handle - re-raising original TypeError")
        raise e

    except Exception as e:
        logger.error(f"    Unexpected error: {e}")
        logger.error(f"   Unexpected error traceback:\n{traceback.format_exc()}")
        raise e


def force_apply_autogen_json_fix():
    """Force apply the autogen JSON fix with robust error handling."""

    def enhanced_execute_function(self, func_call, verbose: bool = False):
        """Enhanced execute_function that falls back to retrieve_json only when original fails."""
        try:
            from autogen.io.base import IOStream
            iostream = IOStream.get_default()
        except Exception:
            class MockIOStream:
                def print(self, *args, **kwargs):
                    print(*args)

            iostream = MockIOStream()

        func_name = func_call.get("name", "")
        func = self._function_map.get(func_name, None)

        is_exec_success = False
        if func is not None:
            # ========== PRESERVE ORIGINAL AUTOGEN LOGIC ==========
            # Extract arguments from a json-like string and put it into a dict.
            input_string = func_call.get("arguments", "{}")

            try:
                # Try original autogen approach first
                formatted_string = self._format_json_str(input_string)
                arguments = json.loads(formatted_string)
                print(f" ORIGINAL AUTOGEN: Successfully parsed arguments for {func_name}")
            except (json.JSONDecodeError, Exception) as e:
                # Only if original fails, fall back to our enhanced parsing
                print(f" ORIGINAL AUTOGEN FAILED: {e} - falling back to enhanced parsing for {func_name}")
                try:
                    arguments = retrieve_json(input_string)
                    if arguments is None:
                        arguments = {}
                    elif isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    print(f" FALLBACK SUCCESSFUL: Enhanced parsing worked for {func_name}")
                except Exception as fallback_error:
                    print(f" FALLBACK FAILED: {fallback_error}")
                    arguments = None
                    content = f"Error: {e}\n The argument must be in JSON format."

            # ========== PRESERVE ORIGINAL EXECUTION LOGIC ==========
            if arguments is not None:
                iostream.print(f"\n>>>>>>>> EXECUTING FUNCTION {func_name}...", flush=True)
                try:
                    print(" Function being called details:")
                    print(f"   Function: {func}")
                    print(f"   Function name: {getattr(func, '__name__', 'NO_NAME')}")
                    print(" Parsed arguments analysis:")
                    print(f"   Arguments type: {type(arguments)}")
                    print(f"   Arguments content: {arguments}")
                    content = safe_function_call(func, arguments)  # Original autogen always uses **kwargs
                    is_exec_success = True
                    print(f" EXECUTED: Successfully executed {func_name}")
                except Exception as e:
                    content = f"Error: {e}"
                    print(f" EXECUTION FAILED: {func_name}: {e}")
        else:
            content = f"Error: Function {func_name} not found."

        if verbose:
            iostream.print(f"\nInput arguments: {arguments}\nOutput:\n{content}", flush=True)

        return is_exec_success, {
            "name": func_name,
            "role": "function",
            "content": str(content),
        }

    async def enhanced_a_execute_function(self, func_call):
        """Enhanced async execute_function that falls back to retrieve_json only when original fails."""
        try:
            from autogen.io.base import IOStream
            iostream = IOStream.get_default()
        except Exception:
            class MockIOStream:
                def print(self, *args, **kwargs):
                    print(*args)

            iostream = MockIOStream()

        func_name = func_call.get("name", "")
        func = self._function_map.get(func_name, None)

        is_exec_success = False
        if func is not None:
            input_string = func_call.get("arguments", "{}")

            try:
                # Try original autogen approach first
                formatted_string = self._format_json_str(input_string)
                arguments = json.loads(formatted_string)
                print(f" ORIGINAL AUTOGEN ASYNC: Successfully parsed arguments for {func_name}")
            except (json.JSONDecodeError, Exception) as e:
                # Only if original fails, fall back to our enhanced parsing
                print(f" ORIGINAL AUTOGEN ASYNC FAILED: {e} - falling back to enhanced parsing for {func_name}")
                try:
                    arguments = retrieve_json(input_string)
                    if arguments is None:
                        arguments = {}
                    elif isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    print(f" FALLBACK ASYNC SUCCESSFUL: Enhanced parsing worked for {func_name}")
                except Exception as fallback_error:
                    print(f" FALLBACK ASYNC FAILED: {fallback_error}")
                    arguments = None
                    content = f"Error: {e}\n The argument must be in JSON format."

            if arguments is not None:
                iostream.print(f"\n>>>>>>>> EXECUTING ASYNC FUNCTION {func_name}...", flush=True)
                try:
                    print(" Function being called details:")
                    print(f"   Function: {func}")
                    print(f"   Function name: {getattr(func, '__name__', 'NO_NAME')}")
                    print(" Parsed arguments analysis:")
                    print(f"   Arguments type: {type(arguments)}")
                    print(f"   Arguments content: {arguments}")
                    import inspect
                    if inspect.iscoroutinefunction(func):
                        if isinstance(arguments, dict):
                            content = await func(**arguments)  # Original autogen always uses **kwargs
                        # Handle list case - convert to positional arguments
                        elif isinstance(arguments, list):
                            content = await func(*arguments)  # Original autogen always uses **kwargs
                        # Handle single argument case
                        else:
                            content = await func(arguments)  # Original autogen always uses **kwargs
                    else:
                        content = safe_function_call(func, arguments)
                    is_exec_success = True
                    print(f" EXECUTED ASYNC: Successfully executed {func_name}")
                except Exception as e:
                    content = f"Error: {e}"
                    print(f" EXECUTION ASYNC FAILED: {func_name}: {e}")
        else:
            content = f"Error: Function {func_name} not found."

        return is_exec_success, {
            "name": func_name,
            "role": "function",
            "content": str(content),
        }

    # Force import autogen and apply patches
    try:
        import autogen
        from autogen.agentchat.conversable_agent import ConversableAgent

        # Store original methods for verification
        original_execute = getattr(ConversableAgent, 'execute_function', None)
        original_a_execute = getattr(ConversableAgent, 'a_execute_function', None)

        # Apply patches
        ConversableAgent.execute_function = enhanced_execute_function
        ConversableAgent.a_execute_function = enhanced_a_execute_function

        # Verify patches were applied
        new_execute = getattr(ConversableAgent, 'execute_function', None)
        new_a_execute = getattr(ConversableAgent, 'a_execute_function', None)

        if new_execute is not original_execute:
            print(" SUCCESS: Autogen sync execute_function has been patched!")
        else:
            print(" FAILED: Autogen sync execute_function patch was not applied")

        if new_a_execute is not original_a_execute:
            print(" SUCCESS: Autogen async execute_function has been patched!")
        else:
            print(" FAILED: Autogen async execute_function patch was not applied")

        print(" Autogen JSON handling enhanced - tool calls can now handle unlimited length!")
        return True

    except ImportError as e:
        print(f" Could not import autogen for patching: {e}")
        return False
    except Exception as e:
        print(f" Error applying autogen patches: {e}")
        import traceback
        traceback.print_exc()
        return False



# Also provide a manual trigger function for Flask startup
def apply_autogen_fix_on_startup():
    """Manual function to call during Flask app startup if automatic patch fails."""
    print("[INIT] Manually applying autogen JSON fix...")
    return force_apply_autogen_json_fix()

# ========================================================================================
# END AUTOGEN JSON HANDLING ENHANCEMENT
# ========================================================================================
def load_vlm_agent_files(prompt_id, role_number):
    """Loads any VLM agent JSON files for the given prompt_id and role_number and integrates them with existing recipes."""
    vlm_actions = []

    # Look for existing VLM agent files
    try:
        for file in os.listdir("prompts"):
            if file.startswith(f"{prompt_id}_{role_number}_") and file.endswith("_vlm_agent.json"):
                file_path = os.path.join("prompts", file)
                try:
                    with open(file_path, 'r') as f:
                        recipe_data = json.load(f)
                        current_app.logger.info(f"Found VLM agent recipe: {file_path}")

                        # Extract the action ID from the filename (assuming format: prompt_id_role_number_action_id_vlm_agent.json)
                        parts = file.split('_')
                        if len(parts) >= 4:
                            try:
                                action_id = int(parts[2]) # Get the action ID
                                # Add or replace action in the actions list
                                recipe_data["action_id"] = action_id
                                vlm_actions.append(recipe_data)
                            except (ValueError, IndexError):
                                current_app.logger.error(f"Couldn't parse action ID from filename {file}")
                except Exception as e:
                    current_app.logger.error(f"Error reading VLM agent file {file_path}: {e}")
    except Exception as e:
        current_app.logger.error(f"Error listing files in prompts directory: {e}")

    return vlm_actions


# ── Canonical WAMP RPC helper — ONE implementation ──────────────────────────
# Was duplicated VERBATIM in create_recipe.py:815 and reuse_recipe.py:338 (88
# lines each) and had already DRIFTED: reuse_recipe computed `actual_timeout`
# INSIDE the try, so a non-numeric `time` raised TypeError, hit the broad
# `except Exception -> return None`, and the caller could not tell a bad argument
# from a failed RPC. create_recipe computed it BEFORE the try, letting a
# programming error surface loudly. THIS COPY KEEPS create_recipe's placement —
# the drift was an instance of the silent-failure class, and the loud variant is
# the correct one.
#
# Both call sites already delegate ~64 other helpers here via `helper_fun.`, so
# this is the established home, not a new one.
#
# KNOWN, NOT FIXED HERE: the transport URL below is hardcoded, and the same
# literal appears 21x across the repo. Routing it through core.port_registry /
# a config seam is a separate change with 21 call sites and a behaviour risk;
# consolidating first means that fix lands in ONE place instead of two.
async def subscribe_and_return(message, topic, time=1800000):
    """
    Makes an RPC call to the specified topic using a component.
    Waits for the full duration of the specified timeout for a response.

    Args:
        message: The message payload to send
        topic: The topic to call
        time: Timeout in milliseconds (default: 8000)

    Returns:
        The response from the RPC call, or None if there was an error or timeout
    """
    from autobahn.asyncio.component import Component
    import asyncio
    current_app.logger.info(f"Making RPC Call to {topic}...")

    # Create a new component for this call
    # The relay/federation router, RESOLVED not hardcoded. WAMP carries central
    # relay AND federation, so a fixed literal pinned every node to one box — it
    # could not use a regional host, a LAN peer, or the router Nunba already ships
    # locally on :8088. core.wamp_url is the single place that knows both WAMP_URL
    # dialects (ws router vs http publish bridge).
    #
    # The default also corrects the NAME: this used to read aws_rasa while the run
    # scripts export azurekong. Verified 2026-08-18 that both resolve to
    # 106.51.181.24 and serve identical /ws and /publish responses, so this is a
    # rename, not a redirect — and it ends the split where a node's RPC and its
    # publish bridge could disagree about which host they mean.
    from core.wamp_url import resolve_router_url
    component = Component(
        transports=resolve_router_url(),
        realm="realm1",
    )

    response_future = asyncio.Future()

    @component.on_join
    async def join(session, details):
        current_app.logger.info("Session joined, making RPC call...")
        try:
            # Convert time from milliseconds to seconds
            timeout_seconds = time / 1000
            current_app.logger.info(f"Using timeout of {timeout_seconds} seconds")

            # Set actual timeout
            try:
                result = await asyncio.wait_for(
                    session.call(topic, message),
                    timeout=timeout_seconds
                )

                if not response_future.done():
                    response_future.set_result(result)

            except asyncio.TimeoutError:
                if not response_future.done():
                    response_future.set_exception(
                        Exception(f"RPC call timed out after {timeout_seconds} seconds")
                    )
            except Exception as e:
                if not response_future.done():
                    response_future.set_exception(e)

        finally:
            # Stop the component regardless of success / failure
            try:
                await component.stop()
            except Exception as e:
                current_app.logger.error(f"Error stopping component: {e}")

    # Calculate timeout with a small buffer
    actual_timeout = (time / 1000) + 5  # Add 5 second buffer
    try:
        # Start the component
        await component.start()

        # Wait for the response or timeout
        result = await asyncio.wait_for(response_future, timeout=actual_timeout)

        # Return the result
        return result

    except asyncio.TimeoutError:
        current_app.logger.error(f"Timed out waiting for response after {actual_timeout} seconds")
        # Explicitlt cancel the future if it's still pending
        if not response_future.done():
            response_future.cancel()
        return None
    except Exception as e:
        current_app.logger.error(f"Error in subscribe_and_return: {e}")
        # Explicitly cancel the future if it's still pending
        if not response_future.done():
            response_future.cancel()
        return None
    finally:
        # Ensure component is stopped
        if hasattr(component, 'session') and component.session:
            try:
                await component.stop()
            except Exception as e:
                current_app.logger.error(f"Error stopping component in finally: {e}")
