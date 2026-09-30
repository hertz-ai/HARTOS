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
# this module is INSIDE a function (AST-verified: zero module-level uses;
# used only in create_visual_agent, the agent builders, and
# _context_limiter_classes, whose two class bases resolve on its first call).
# `import helper` is on the backend-boot critical path (create_recipe /
# reuse_recipe / gather_agentdetails all import it), so deferring autogen
# here is what actually keeps it out of the boot.  Same proxy + test as
# create_recipe.py.  See tests/unit/test_lazy_autogen_import.py.
from core.optional_import import lazy_module
# The fabrication gate in reuse_recipe keys on this EXACT string to tell a
# back-filled stand-in from a real tool result — one definition, no drift.
from core.constants import HISTORICAL_TOOL_PLACEHOLDER
autogen = lazy_module("autogen")
transform_messages = lazy_module(
    "autogen.agentchat.contrib.capabilities.transform_messages")
transforms = lazy_module(
    "autogen.agentchat.contrib.capabilities.transforms")
import json
import math
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
import threading
from core.file_cache import atomic_json_write
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

# get_frame's legacy Redis read: one refused connect opens the breaker for
# that client, then one probe per cooldown.  Keyed by the client object, so a
# replaced client (tests patch hartos.helper.redis_client) starts closed
# instead of inheriting another client's open breaker.  See get_frame.
from core.circuit_breaker import KeyedCircuitBreaker
_REDIS_FRAME_BREAKER = KeyedCircuitBreaker(threshold=1, cooldown=300,
                                           name='redis_frame')

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
from core.platform_paths import get_recipe_prompts_dir
PROMPTS_DIR = os.path.abspath(get_recipe_prompts_dir())
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


# A registry tool name as `attach_for_names` compares it: the registry key, or
# `{tool}_{endpoint}`.  Dots are legal (`tts.package_installer` is real, 5 uses
# in the banked corpus).  The >=3-char floor is what stops a Windows drive
# letter surviving as the candidate `C` when a path is split on ':'.
#
# Lives here, not in reuse_recipe, because BOTH sides of the authoring
# convention read it: `_tool_name_candidates` (reuse_recipe) takes the NAME
# half, `strip_authored_tool_prefix` below takes the ARGUMENT half, and
# create_recipe needs the second one too.  One pattern, one home.
TOOL_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]{2,}$')


def strip_authored_tool_prefix(raw):
    """The ARGUMENT half of an authored ``<tool>: '<argument>'`` action text.

    Complement of ``_tool_name_candidates`` (reuse_recipe.py), which takes the
    NAME half of the same convention and exists because "the authoring model
    routinely writes the tool AND its argument into the single field".  Nothing
    took the other half, so every consumer that wanted the human-readable
    instruction was comparing against the tool name as well.

    WHAT THAT COST (drive d69-, 2026-09-11 04:11:12, agent 88719487304
    action 2).  ``similar_instructions`` scored an action against ITS OWN
    banked recipe:

        live   'Open a web browser and navigate to the top result URL for HART OS documentation'
        stored "execute_windows_or_android_command: 'Open default web browser and
                navigate to the top result URL for HART OS documentation'"

        words1=15  words2=16  overlap=12  ->  12/16 = 0.75

    against a 0.8 threshold — the log recorded exactly 0.7500.  The prefix
    inflates the denominator and dilutes the overlap, so the action missed its
    own recipe by 0.05.  ``matching_recipe`` stayed None, ``REUSING command``
    logged ZERO times, no "Follow these steps from a previous successful
    execution" block was built, and the VLM loop started from nothing: 30
    iterations in 114.6s, an invented https://www.hartos.com/documentation, and
    exit_reason=max_iterations.  Stripped, the same pair scores 0.9333 — the
    two texts then differ by one word, 'a' vs 'default'.

    ONLY an identifier-shaped prefix is removed, and only before the FIRST
    colon.  'Ratio 3:2 matters here' keeps its colon because 'Ratio 3' is not
    an identifier; a bare 'C:\\path' keeps its because of the >=3-char floor.
    That matters: over-stripping would make unrelated actions match, and
    injecting the WRONG action's steps is a worse failure than injecting none.

    Returns the text unchanged (whitespace- and quote-trimmed) when there is no
    such prefix.  Never raises — it runs inside the reuse dispatch path.
    """
    try:
        text = str(raw if raw is not None else '').strip()
    except Exception:
        return ''
    head, sep, tail = text.partition(':')
    if sep and TOOL_IDENT_RE.match(head.strip()):
        text = tail.strip()
    return text.strip('\'"')


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


# ─── Wire-strict JSON ────────────────────────────────────────────────────
# Python's json.loads is laxer than llama.cpp's parser (nlohmann): it reads
# NaN / Infinity / -Infinity, and turns a number too big for a double into
# inf (json.loads('{"v":620e51403072992921}') == {'v': inf}).  nlohmann
# refuses all of these ("out_of_range.406 number overflow").  Live 2026-09-25:
# a banked step's unquoted hex user id passed the TOOL-ARGS-GUARD for that
# reason and llama.cpp answered 500 on every later request of the chat.
# The same holds for a lone UTF-16 surrogate escape ("\ud800" with no low
# half): json.loads keeps it as a character, nlohmann answers 500 "invalid
# string: surrogate U+D800..U+DBFF must be followed by U+DC00..U+DFFF"
# (measured on :8080 by the review of bb809af28).  json.loads joins an
# escaped PAIR into one character, so what this pattern finds after a parse
# is lone; the lookarounds leave a pair of raw surrogate characters alone.
_LONE_SURROGATE = re.compile(
    r'[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]')


class WireKeyCollision(ValueError):
    """Two keys of one object would become the same key once each lone
    surrogate is replaced by U+FFFD (see _wire_json_loads)."""


def _wire_json_loads(text, on_refused):
    """json.loads where every token a strict parser refuses goes to on_refused:
    a non-finite number or NaN/Infinity constant as its text, a lone
    surrogate as its one character."""
    def number(parse):
        def hook(token):
            return parse(token) if math.isfinite(float(token)) else on_refused(token)
        return hook

    def strings(value):
        if isinstance(value, str):
            return _LONE_SURROGATE.sub(lambda m: on_refused(m.group()), value)
        if isinstance(value, list):
            return [strings(v) for v in value]
        if isinstance(value, dict):
            out = {strings(k): strings(v) for k, v in value.items()}
            if len(out) < len(value):
                # Two keys became one: a lone surrogate replaced by U+FFFD
                # met another key that now reads the same, and the dict kept
                # only one of the two arguments.  Review of b0fa4989e, probed:
                # {"\ud800":1,"\udc00":2} came back as one key, U+FFFD,
                # holding 2.
                raise WireKeyCollision("keys collide once lone surrogates "
                                       "are replaced: " + repr(list(value)))
            return out
        return value

    return strings(json.loads(text, parse_float=number(float),
                              parse_int=number(int), parse_constant=on_refused))


def _refuse_token(token):
    raise ValueError(f"not valid for a strict JSON parser: {token!r}")


def _keep_refused_token(token):
    """A refused number or constant keeps its text; a lone surrogate, which
    has no text a strict parser accepts, becomes U+FFFD."""
    return '\ufffd' if _LONE_SURROGATE.fullmatch(token) else token


def is_wire_json(text):
    """True when ``text`` parses as JSON and holds none of the tokens that
    Python's ``json.loads`` accepts but llama.cpp's parser (nlohmann) refuses:
    NaN / Infinity / -Infinity, a number that overflows a double, a lone
    UTF-16 surrogate.  It is not a full nlohmann conformance check: it covers
    the laxities of json.loads that are known to reach the wire."""
    try:
        _wire_json_loads(text, _refuse_token)
        return True
    except Exception:
        return False


def load_wire_json(text):
    """Parse ``text``, keeping each token a strict parser refuses as its own
    string: an unquoted id ``620e51403072992921`` comes back as
    ``"620e51403072992921"``, never ``inf``; a lone surrogate becomes U+FFFD.
    Raises like ``json.loads``, and ValueError when that replacement would
    turn two keys of one object into the same key (one argument would be
    lost without a trace)."""
    return _wire_json_loads(text, _keep_refused_token)


# A bare number token, and the string delimiters json_repair reads (its
# constants.STRING_DELIMITERS: " ' and the curly pair), each with its closer.
_BARE_NUMBER = re.compile(r'-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?')
_STRING_CLOSER = {'"': '"', "'": "'", '“': '”', '”': '”'}
# What a bare token is made of: a number with one of these next to it is
# part of a longer token (a UUID, a word, a path), never a number of its own.
# '/ * $ % !' too: 1e999/2 is an expression, not a number followed by text
# (review of e1a1aa233: the /2 was dropped).
_TOKEN_CHARS = '_.-+/*$%!'
# Where a comment may open: after whitespace or ``{ [ ,`` -- not inside an
# unquoted value, and not after ':', which the '//' of a URL follows.
_BEFORE_A_COMMENT = '{[,'


def parse_tool_arguments(text):
    """``(value, repaired)`` for a tool call's arguments text: THE one reader,
    for both the history guard (ensure_tool_call_arguments_json) and the
    executor (bind_tool_call_arguments).  Raises ValueError when the text
    cannot be read without inventing a value.

    The text as written first, with load_wire_json (an overflowing number
    comes back as the token the model wrote, never inf); else its
    repair_json repair, with each overflowing number quoted first because
    repair_json itself would write Infinity for it.  A repair that still
    yields Infinity / NaN the model never wrote is refused.

    Review of c21b5e6e2 / 7d07c0a2d: the executor parsed with plain
    json.loads and retrieve_json, so ``{"id": 620e51403072992921}`` ran the
    tool with id=inf while the guard showed the model the id it wrote."""
    try:
        return load_wire_json(text), False
    except ValueError as e:
        _fallback_logger.debug(f"tool arguments not strict JSON, repairing: {e}")
    value = load_wire_json(repair_json(_quote_overflowing_numbers(text)))
    if _invents_a_constant(value, text):
        raise ValueError('repair would send a value the model never wrote')
    return value, True


_NON_FINITE_WORDS = ('Infinity', '-Infinity', 'NaN')


def _written_value_counts(original):
    """How many times the model wrote each of Infinity, -Infinity, NaN as a
    WHOLE value in ``original``: a bare token, or the whole of a quoted
    string.  Read with the one scanner (_scan_segments), so a word inside a
    longer string ("to Infinity, and beyond", "say 'Infinity' now") or a
    comment is never a value the model wrote (review of dbfef4360: a regex
    blind to string boundaries let an invented Infinity through)."""
    segments = [s for s in _scan_segments(str(original))
                if not (s[0] == "char" and s[1].isspace())
                and s[0] != "comment"]
    counts = {}
    for k, (kind, piece) in enumerate(segments):
        if kind == "string":
            word = (piece[1:-1] if len(piece) > 1 else "").strip()
        elif kind == "run":
            # A bare word is a whole value only between value delimiters:
            # in {"q": Infinity war} it is the start of an unquoted string.
            before = segments[k - 1] if k else None
            after = segments[k + 1] if k + 1 < len(segments) else None
            if before is not None and (before[0] != "char"
                                       or before[1] not in "{[,:"):
                continue
            if after is not None and (after[0] != "char"
                                      or after[1] not in ",}]"):
                continue
            word = piece
        else:
            continue
        if word in _NON_FINITE_WORDS:
            counts[word] = counts.get(word, 0) + 1
    return counts


class _WrittenEntry:
    """One ``key: value`` the model wrote in the outermost object (see
    _outermost_entries)."""
    __slots__ = ('key', 'quoted', 'doubtful', 'empty')

    def __init__(self, key, quoted, doubtful):
        self.key, self.quoted, self.doubtful = key, quoted, doubtful
        self.empty = True


# A bare word that is a whole JSON value on its own.
_WHOLE_VALUE_WORDS = ('true', 'false', 'null')


def _written_key(kind, piece):
    """A key token as the reader names it: a double-quoted key decoded (a
    lone surrogate as U+FFFD, like load_wire_json; an invalid escape kept
    as written, like json_repair), another quoted key without its quotes,
    a bare key as it is."""
    if kind != "string":
        return piece
    try:
        key = json.loads(piece) if piece[0] == '"' else piece[1:-1]
    except ValueError:
        key = piece[1:-1]
    return _LONE_SURROGATE.sub(chr(0xFFFD), key)


def _outermost_entries(original):
    """What the model wrote in the outermost object of ``original``: THE
    one reader of it, with the one scanner (_scan_segments), for both rules
    tool_argument_error applies after a repair.  One _WrittenEntry per key,
    in order.  A key is a quoted string, word or number where a key starts
    (right after ``{`` or ``,``) followed by ``:``; a quoted word inside an
    unquoted value (``{"command": echo the "status": ok}``) is not one
    (review of 30042a2b6, problem 6), nor a split word (``..., then report
    status: ok``).

    ``quoted``: the key is in quotes.  ``doubtful``: a bare key after a
    value that was not one whole value (a quoted string, a number, true /
    false / null, or one [...] / {...}): the comma before it may be part of
    that unquoted value, which json_repair cut there (``{"text": Hi there,
    text: again}`` sent 'again', review of 30042a2b6, problem 1).
    ``empty``: the model left the value empty: a quoted string of nothing
    but whitespace, ``null``, or no value (``"a": ,`` or the text ending);
    review of e9daad6c5."""
    entries = []
    depth, key_start, loose = 0, False, False
    candidate, entry, items = None, None, None

    def close():
        # The value of ``entry`` ends: was it one whole value, and empty?
        nonlocal loose, entry, items
        if entry is not None:
            whole = len(items) <= 1 and all(
                kind in ("string", "number", "group")
                or kind == "run" and piece in _WHOLE_VALUE_WORDS
                for kind, piece in items)
            loose = loose or not whole
            entry.empty = not items or items == [("run", "null")] or (
                items[0][0] == "string" and len(items) == 1
                and not items[0][1][1:-1].strip())
        entry, items = None, None

    for kind, piece in _scan_segments(str(original)):
        if kind == "char" and piece.isspace() or kind == "comment":
            continue
        opens = kind == "char" and piece in "{["
        shuts = kind == "char" and piece in "}]"
        if depth == 1:
            if candidate is not None and kind == "char" and piece == ":":
                entry = _WrittenEntry(_written_key(*candidate),
                                      candidate[0] == "string",
                                      candidate[0] != "string" and loose)
                entries.append(entry)
                candidate, items = None, []
                continue
            candidate = None
            if kind == "char" and piece == ",":
                close()
                key_start = True
                continue
            if shuts:
                close()
            elif key_start and kind in ("string", "run", "number"):
                candidate = (kind, piece)
            elif items is not None:
                items.append(("group", piece) if opens else (kind, piece))
            key_start = False
        if opens:
            depth += 1
            key_start = depth == 1
        elif shuts:
            depth -= 1
    close()
    return entries


def _invents_a_constant(value, original):
    """True when ``value`` carries Infinity / NaN the model never wrote as a
    value: a float inf/nan (json.loads of an overflowing number -- the model
    wrote a number, never inf), or more "Infinity" / "NaN" string values than
    the model wrote as whole values.  Such a repair is refused, never sent.

    Counted per value, not looked up anywhere in the text: review of
    3a7abe540 -- with "Infinity war" in the query, the Infinity json_repair
    invented for 1e999e was let through and search('Infinity war',
    'Infinity') ran and was banked."""
    counts = {}

    def walk(v):
        if isinstance(v, dict):
            return any(walk(k) or walk(x) for k, x in v.items())
        if isinstance(v, list):
            return any(walk(x) for x in v)
        if isinstance(v, float) and not math.isfinite(v):
            return True
        if isinstance(v, str) and v in _NON_FINITE_WORDS:
            counts[v] = counts.get(v, 0) + 1
        return False

    if walk(value):
        return True
    written = _written_value_counts(original)
    return any(n > written.get(word, 0) for word, n in counts.items())


def _joins_a_token(ch):
    """True when ``ch`` next to a number makes it part of a longer token."""
    return ch.isalnum() or ch in _TOKEN_CHARS


def _scan_segments(text):
    """Split ``text`` into ``(kind, piece)`` the way the tool-argument reader
    sees it, so the quoting and the counting of written values read ONE
    tokenisation: "string" (a quoted string, quotes included; one that never
    closes runs to the end), "comment" (/* */ or // to the line end, opened
    only where a comment can start: after whitespace or { [ , -- not inside
    an unquoted value such as a URL), "number" (a whole run that is a bare
    number), "run" (any other whole run of letters, digits and
    _TOKEN_CHARS: a word, a UUID, a path, an expression), or "char"."""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in _STRING_CLOSER:
            closer, j = _STRING_CLOSER[ch], i + 1
            while j < n and text[j] != closer:
                j += 2 if text[j] == "\\" else 1
            j = min(n, j + 1)
            yield "string", text[i:j]
            i = j
            continue
        if ((text.startswith("/*", i) or text.startswith("//", i))
                and (i == 0 or text[i - 1].isspace()
                     or text[i - 1] in _BEFORE_A_COMMENT)):
            closer = "*/" if text[i + 1] == "*" else "\n"
            end = text.find(closer, i + 2)
            end = n if end < 0 else end + len(closer)
            yield "comment", text[i:end]
            i = end
            continue
        if _joins_a_token(ch):
            j = i + 1
            while j < n and _joins_a_token(text[j]):
                j += 1
            run = text[i:j]
            yield ("number" if _BARE_NUMBER.fullmatch(run) else "run"), run
            i = j
            continue
        yield "char", ch
        i += 1


def _quote_overflowing_numbers(text):
    """``text`` with every bare number a double cannot hold put in double
    quotes, for ``repair_json``: it reads such a number as inf and writes
    Infinity, so the token the model wrote (an unquoted id like
    ``620e51403072992921``) would be lost before ``load_wire_json`` sees it.
    Text inside a string literal or a comment, and finite numbers, are left
    as they are; a string or comment that never closes swallows the rest,
    which is then left as it was.

    Only a WHOLE token is quoted: one with no letter, digit or ``_ . - +``
    on either side (_joins_a_token).  Review of ed31c7c53, probed: reading
    '-' and '+' as delimiters turned an unquoted UUID ``550e8400-e29b-...``
    into ``"550e8400"`` and lost the rest.  Review of 3ea611862: a list of
    allowed neighbours (``, } ] :``) left a number before a quote, ``)`` or
    ``;`` unquoted, and repair_json then wrote Infinity; and a comment is
    opened only where one can start (_BEFORE_A_COMMENT), since the ``//``
    of an unquoted URL is not one."""
    # A number is quoted with the quote character the document itself uses
    # (the one its last string opened with): a '"' put into a document
    # written with "'" made json_repair read the next key into the value
    # before it (review of e1a1aa233: {'u': http://h/x, 'id': 620e...} lost
    # the id).
    out, quote = [], '"'
    for kind, piece in _scan_segments(text):
        if kind == "string":
            quote = "'" if piece[0] == "'" else '"'
        elif kind == "number" and not math.isfinite(float(piece)):
            piece = quote + piece + quote
        out.append(piece)
    return "".join(out)


# The two keys of a refused call's stand-in arguments (see
# refused_arguments_json).  A stand-in is never run: tool_argument_error
# refuses any arguments carrying REFUSED_ARGUMENTS_KEY, including for a tool
# that takes **kwargs and would otherwise bind any names (review of
# cd8d9154d, M3: clawhub_adapter, MCP tool_executor).
REFUSED_ARGUMENTS_KEY = 'refused_arguments'
REFUSED_BECAUSE_KEY = 'refused_because'


def refused_arguments_reason(text):
    """Why ``text`` cannot be sent as a call's arguments, in one sentence.

    For text the guard could not turn into an object: it is invalid JSON,
    two of its keys collide, or it parses to something that is not an
    object (an object would have been kept, so that is the only other
    case)."""
    try:
        load_wire_json(text)
    except WireKeyCollision:
        return ('two of its keys differ only in an invalid character and '
                'would become one key')
    except Exception:
        return 'it is not valid JSON'
    return NOT_AN_OBJECT_REASON


def refused_arguments_json(text):
    """A strict JSON object standing in for arguments that cannot be sent:
    the text the model wrote, marked refused, and why.

    The wire needs an object (llama.cpp 500s on anything else), but ``{}``
    erased what the model sent, so its next turn could neither see nor fix
    its own call (review of 86e580b99).  A lone surrogate in the text becomes
    U+FFFD, the one change needed for llama.cpp to accept it."""
    shown = _LONE_SURROGATE.sub(chr(0xFFFD), text)
    return json.dumps({
        REFUSED_ARGUMENTS_KEY: shown,
        REFUSED_BECAUSE_KEY: ('these arguments were refused and the call was '
                              'not run: '
                              + refusal_sentence(refused_arguments_reason(text))),
    })


# The reason, and what to do instead, for arguments that are not one JSON
# object: ONE wording for the history's stand-in (refused_arguments_json)
# and the executor's reply (tool_argument_error), so the model reads the
# same refusal in both (review of 1bf298f5b).
NOT_AN_OBJECT_REASON = 'it is not one JSON object of named values'


def refusal_sentence(reason):
    """``reason`` and the one instruction that follows every refusal."""
    return reason + '. Call it again with one JSON object of named values.'


def call_functions(msg):
    """The function dicts a message's tool calls carry: each
    ``tool_calls[].function`` and a legacy ``function_call``, in order.  The
    dicts themselves, not copies, so a caller may write into them."""
    if not isinstance(msg, dict):
        return []
    fns = [tc['function'] for tc in (msg.get('tool_calls') or [])
           if isinstance(tc, dict) and isinstance(tc.get('function'), dict)]
    if isinstance(msg.get('function_call'), dict):
        fns.append(msg['function_call'])
    return fns


def wire_tool_arguments(args):
    """``(text, coerced)``: the arguments text the TOOL-ARGS-GUARD sends for
    one call written with ``args`` (see ensure_tool_call_arguments_json), and
    whether it had to change what was written to get it.  Pure: the guard
    writes it, and the executor uses it to find the record a guarded copy
    came from (stored_call_records)."""
    if isinstance(args, dict):
        # Some code paths store the arguments as an object already — the
        # wire wants a string, so serialize.  A float inf / nan in it
        # serializes as Infinity / NaN, so it is checked below like any
        # other string.
        args = json.dumps(args)
        if is_wire_json(args):
            return args, False
    if args is None or (isinstance(args, str) and not args.strip()):
        # Nothing written is no arguments: {} -- what the executor runs a
        # zero-parameter tool with, so history and execution agree (review
        # of 1bf298f5b: "" became the refused stand-in while the tool ran).
        return '{}', True
    if not isinstance(args, str):
        args = str(args)
    try:
        is_object = isinstance(_wire_json_loads(args, _refuse_token), dict)
    except Exception:
        is_object = False
    if is_object:
        # Already a strict JSON object: leave untouched.  Strict JSON that is
        # not an object ('[1,2]', '"hello"') is not arguments: the llama.cpp
        # template reads arguments only as a mapping, so a prior call with
        # '[{"url": ...}]' rendered with no parameters at all (measured on
        # :8080, review of b0fa4989e).
        return args, False
    try:
        obj, _ = parse_tool_arguments(args)
        if isinstance(obj, dict):
            return json.dumps(obj), True
    except ValueError as e:
        _fallback_logger.debug(f"TOOL-ARGS-GUARD: arguments refused: {e}")
    return refused_arguments_json(args), True


class _CallArguments(str):
    """Wire-compatible arguments retaining this call's source through transforms."""
    def __new__(cls, wire, as_written, record):
        obj = super().__new__(cls, wire)
        obj.as_written = as_written
        obj.record = record
        return obj

    def __deepcopy__(self, memo):
        # TransformMessages copies history, not the identity of its source call.
        return type(self)(str(self), self.as_written, self.record)


def _bind_argument_sources(messages):
    for msg in messages or ():
        for fn in call_functions(msg):
            raw = fn.get('arguments')
            if isinstance(raw, str) and not isinstance(raw, _CallArguments):
                fn['arguments'] = _CallArguments(raw, raw, fn)
    return messages


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

    Coercion per malformed call: keep it if it is already a JSON OBJECT a
    STRICT parser accepts (``is_wire_json``'s test -- Python's ``json.loads`` alone is not
    that test: it reads an overflowing number as inf and accepts NaN and a
    lone surrogate escape, all of which llama.cpp refuses with a 500); else
    parse it, or its ``repair_json`` repair, with ``load_wire_json``, which
    keeps each refused number as its own string and turns a lone surrogate
    into U+FFFD (refusing, rather than merging, two keys that replacement
    would make one), and keep the result only if it is a dict; else replace
    it with :func:`refused_arguments_json` -- a well-formed object that keeps
    what the model wrote, marked refused, with the reason, so its next turn
    can see and correct its own call (it used to be ``"{}"``, which erased
    it: review of 86e580b99).  ``None`` arguments, where nothing was written,
    still become ``"{}"``.

    The per-call rule is :func:`wire_tool_arguments`.

    This guard has no tool signatures, so its repair cannot tell a benign fix
    (a trailing comma) from json_repair splitting an unquoted value into
    keys.  The executor can, and it reads the call as the model wrote it:
    every production seat runs this guard in its TransformMessages chain
    BEFORE any reply function, so the executor is handed this guard's
    repair, not the model's text; it finds the conversation's stored record
    of the call (stored_call_records) and parses that.  A call it refused
    as broken JSON is marked refused in that record, and a strict JSON
    object is kept here as it is.
    """
    if not messages:
        return messages
    coerced = 0
    for msg in messages:
        for fn in call_functions(msg):
            args = fn.get('arguments')
            fixed, changed = wire_tool_arguments(args)
            if isinstance(args, _CallArguments):
                fn['arguments'] = _CallArguments(fixed, args.as_written, args.record)
            elif fixed != args:
                fn['arguments'] = _CallArguments(fixed, args, fn)
            coerced += changed
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


def answered_call_ids(m):
    """Every tool_call_id a single message answers.

    autogen returns tool results in TWO shapes and a reader that knows only the
    first sees nothing on real data.  From ``generate_tool_calls_reply``
    (autogen/agentchat/conversable_agent.py): each executed call becomes
    ``{"tool_call_id": ..., "role": "tool", "content": ...}``, and when the turn
    finishes those are wrapped and returned as ONE message —

        {"role": "tool", "tool_responses": [ ...those... ],
         "content": "\\n\\n".join(...)}

    — whose OUTER dict has no ``tool_call_id`` at all.  ``is_consolidated_response``
    below keys on ``'tool_responses'`` for exactly this reason (it additionally
    requires len > 1; this reader deliberately does not, because a single-entry
    reply hides its id in the same place).

    CANONICAL: reuse_recipe imports this rather than keeping its own copy —
    the two sides must not drift, since one finds answers and the other
    decides whether a slot gets a real result or a manufactured one.
    """
    ids = set()
    if not isinstance(m, dict) or m.get('role') != 'tool':
        return ids
    if m.get('tool_call_id'):
        ids.add(m['tool_call_id'])
    for r in (m.get('tool_responses') if isinstance(m.get('tool_responses'), list) else []):
        if isinstance(r, dict) and r.get('tool_call_id'):
            ids.add(r['tool_call_id'])
    return ids


# ─── Context-window limiters ─────────────────────────────────────────────
# autogen 0.2.37's MessageHistoryLimiter and MessageTokenLimiter both end the
# same way (transforms.py:105-106 and :235-236):
#
#     if not transforms_util.is_tool_call_valid(truncated_messages):
#         truncated_messages.pop()
#
# is_tool_call_valid() looks only at the FIRST message (role == 'tool',
# transforms_util.py:117-118), and pop() removes the LAST.  So whenever the
# window opens on a tool result, the limiter keeps that orphan and throws away
# the newest message -- usually the instruction the agent is being asked to
# answer.
#
# Live 2026-09-13 11:27:21, CREATE 87400889007 flow 1 action 3: the newest
# message was ChatInstructor's recipe request.  The StatusVerifier's window
# opened on two tool results, and the verifier got everything except the
# request.  09:15-12:35 the same day, 255 of 1,549 ToolMessageHandler inputs
# opened on a tool result; its own pre-steps explain at most 11 of them.
#
# The orphan is not handled here.  ToolMessageHandler comes next in every
# chain that reaches a model and already converts or drops a leading tool
# message (and any other orphan).  So the one correction is to put the newest
# message back; the window is otherwise autogen's own.
_CONTEXT_LIMITER_CLASSES = None


def _context_limiter_classes():
    """autogen's two limiters with the newest message kept (see above).

    Built on first use, not at module level, so `import helper` still does not
    import autogen (see the note at the top of this module and
    tests/unit/test_lazy_autogen_import.py).
    """
    global _CONTEXT_LIMITER_CLASSES
    if _CONTEXT_LIMITER_CLASSES is not None:
        return _CONTEXT_LIMITER_CLASSES

    def dropped_newest(messages, kept):
        # autogen pops only when the window's first message is a tool result,
        # and after the pop that message is still first -- or nothing is left.
        return (kept is not messages and bool(messages)
                and (not kept or kept[0].get('role') == 'tool'))

    def note(which, newest):
        _safe_log('info',
                  f"[NEWEST-KEPT] autogen {which} opened its window on a tool "
                  f"result and would have dropped the newest message "
                  f"(role={newest.get('role')}, name={newest.get('name')}); kept it")

    def same_message(kept_msg, original):
        # The history limiter keeps the caller's dicts; the token limiter
        # keeps deep copies whose content it may have cut from the tail
        # (autogen's cut keeps the head).  So: the same dict, or the same
        # speaker and call id with the kept content a head of the original.
        if kept_msg is original:
            return True
        if not isinstance(kept_msg, dict) or not isinstance(original, dict):
            return False
        if any(kept_msg.get(k) != original.get(k)
               for k in ('role', 'name', 'tool_call_id')):
            return False
        a, b = kept_msg.get('content'), original.get('content')
        if isinstance(a, str) and isinstance(b, str):
            return b.startswith(a[:200])
        return a == b

    def restore_protected(which, messages, kept, bound=None):
        # Put back every message of the ONE protected set
        # (core.llm_outbound_logger.protected_messages, which the wire trim
        # never drops either) that autogen's window left out.  Live
        # 2026-09-27, REUSE probe liveprobe_reuse_1: 99 of 113
        # ToolMessageHandler inputs held no message from User -- the token
        # limiter keeps the newest ~2500 tokens and the task turn is the
        # oldest message -- so the user's words reached 4 of 77 LLM calls.
        # Each goes back in its place (before the first kept message that is
        # newer), cut by ``bound`` like any other message.
        from core.llm_outbound_logger import protected_messages
        if kept is messages:
            return kept
        missing = [p for p in protected_messages(messages)
                   if not any(same_message(k, p) for k in kept)]
        if not missing:
            return kept
        kept = list(kept)

        def original_index(k):
            return next((i for i in range(len(messages) - 1, -1, -1)
                         if same_message(k, messages[i])), len(messages))

        for p in sorted(missing, key=lambda m: next(
                i for i, x in enumerate(messages) if x is m)):
            p_idx = next(i for i, x in enumerate(messages) if x is p)
            at = next((j for j, k in enumerate(kept)
                       if original_index(k) > p_idx), len(kept))
            kept.insert(at, bound(p) if bound else p)
            _safe_log('info',
                      f"[PROTECTED-KEPT] autogen {which} dropped a protected "
                      f"message (role={p.get('role')}, name={p.get('name')}); "
                      f"put it back at {at}")
        return kept

    class HistoryLimiter(transforms.MessageHistoryLimiter):
        def apply_transform(self, messages):
            kept = super().apply_transform(messages)
            newest = messages[-1] if messages else None
            if dropped_newest(messages, kept) and all(m is not newest for m in kept):
                # The window holds the caller's own dicts, and with room for
                # two or more messages the newest is the last one autogen put
                # in -- so the message it popped is exactly this one.
                kept.append(newest)
                note('MessageHistoryLimiter', newest)
            return restore_protected('MessageHistoryLimiter', messages, kept)

    class TokenLimiter(transforms.MessageTokenLimiter):
        def apply_transform(self, messages):
            # autogen's per-message cut (_truncate_tokens below) is handed
            # the text alone, never the role.  The texts of user turns --
            # the only turns that keep their head (must_keep_head) -- are
            # noted here so the cut can tell them apart.  Review of
            # e1a1aa233: an assistant or tool message holding the separator
            # kept its head too (~6,000 tokens against 2,500).
            self._user_texts = {
                m['content'] for m in messages
                if isinstance(m, dict) and m.get('role') == 'user'
                and isinstance(m.get('content'), str)}
            kept = super().apply_transform(messages)
            if dropped_newest(messages, kept):
                # autogen cuts the newest message first, with nothing yet
                # counted against the budget: to max_tokens_per_message, or to
                # max_tokens when that is smaller.  Give it the same cut.
                newest = self._bound_message(messages[-1])
                kept.append(newest)
                note('MessageTokenLimiter', newest)
            kept = restore_protected('MessageTokenLimiter', messages, kept,
                                     bound=self._bound_message)
            return [self._bound_tool_responses(m) for m in kept]

        def _truncate_tokens(self, text, n_tokens):
            # autogen keeps a message's first n tokens.  A REUSE dispatch
            # turn keeps its marker and the user's words whole instead and
            # loses only steps -- the rule the wire trim applies
            # (core.llm_outbound_logger.must_keep_head).  Review of
            # f97b6bed8: words over the 1,000-token per-message cap were cut
            # here first, the "follow these steps:" boundary with them.
            from core.llm_outbound_logger import keep_head_cut, must_keep_head
            from core.constants import WIRE_TRIM_MARKER
            role = ('user' if text in getattr(self, '_user_texts', ())
                    else None)
            keep = must_keep_head(text, role)
            if not keep:
                return super()._truncate_tokens(text, n_tokens)
            util = transforms.transforms_util
            if util.count_text_tokens(text) <= n_tokens:
                return text
            room = (n_tokens - util.count_text_tokens(text[:keep])
                    - util.count_text_tokens(WIRE_TRIM_MARKER))
            rest = text[keep:]
            tail = ''
            if room > 0 and rest:
                tail = rest[-max(1, int(len(rest) * room
                                        / max(1, util.count_text_tokens(rest))
                                        * 0.9)):]
            return keep_head_cut(text, keep, len(tail), WIRE_TRIM_MARKER)

        def _bound_message(self, msg):
            # The per-message cut autogen gives every message it keeps (head
            # kept), for a message put back by restore_protected.  A copy:
            # the original is the group chat's own.
            msg = dict(msg)
            util = transforms.transforms_util
            if (util.is_content_right_type(msg.get('content'))
                    and util.should_transform_message(
                        msg, self._filter_dict, self._exclude_filter)):
                msg['content'] = self._truncate_str_to_tokens(
                    msg['content'],
                    min(self._max_tokens, self._max_tokens_per_message))
            return msg

        def _bound_tool_responses(self, msg):
            # autogen cuts a message's 'content' and never reads
            # 'tool_responses'. A bundled tool reply carries every call's
            # result there too, and ToolMessageHandler's split (#89) rebuilds
            # one tool message per call from that list, so each cut above was
            # undone before the request left. Live 2026-09-14 (#104),
            # Guardian Convergence action 9: two search_long_term_memory
            # results, 3,386,616 chars together, passed a 1000-token limit and
            # every call to the hosted model was a bare 400. The bundle is one
            # message, so its calls share that message's allowance, and only
            # when they do not fit (fit_texts_to_token_budget). New dicts only:
            # the list may be the group chat's own.
            from core.token_utils import fit_texts_to_token_budget
            responses = msg.get('tool_responses') if isinstance(msg, dict) else None
            if not isinstance(responses, list) or not responses:
                return msg
            slots = [i for i, r in enumerate(responses)
                     if isinstance(r, dict) and isinstance(r.get('content'), str)]
            if not slots:
                return msg
            fitted = fit_texts_to_token_budget(
                [responses[i]['content'] for i in slots],
                min(self._max_tokens, self._max_tokens_per_message))
            bounded, cut = list(responses), False
            for i, text in zip(slots, fitted):
                if text != responses[i]['content']:
                    bounded[i] = {**responses[i], 'content': text}
                    cut = True
            return {**msg, 'tool_responses': bounded} if cut else msg

    _CONTEXT_LIMITER_CLASSES = (HistoryLimiter, TokenLimiter)
    return _CONTEXT_LIMITER_CLASSES


def history_limiter(max_messages, keep_first_message=False):
    """autogen's MessageHistoryLimiter, minus its newest-message pop (above)."""
    return _context_limiter_classes()[0](
        max_messages=max_messages, keep_first_message=keep_first_message)


def token_limiter(max_tokens, max_tokens_per_message, min_tokens=0):
    """autogen's MessageTokenLimiter, minus its newest-message pop (above)."""
    return _context_limiter_classes()[1](
        max_tokens=max_tokens, max_tokens_per_message=max_tokens_per_message,
        min_tokens=min_tokens)


class ToolMessageHandler:
    """Handles tool messages in the conversation history to prevent tool_call_id errors.

    This implementation maintains proper message structure for OpenAI API requirements,
    fixing historical inconsistencies while allowing active tool calls to be processed
    naturally by the framework. handles references between assistant tool calls
    and tool responses to prevent "Invalid parameter: 'tool_call_id' not found" errors.
    It also handles the "only messages with role 'assistant' can have a function call" error.
    """

    def __init__(self, user_tasks=None, user_prompt=None, peer_agents=None):
        """
        Initialize the ToolMessageHandler.

        Args:
            user_tasks: Global user_tasks dictionary containing session data
            user_prompt: Current session identifier (e.g., "10077_123")
            peer_agents: The other agents in THIS conversation.  Tools execute
                in a pairwise Assistant<->Executor exchange, so the seat whose
                request we are building often never saw the result and the
                repair below would mint a placeholder over a real answer.
                Given the peers, we can read the answer they already hold.
                A live list of agent objects (their _oai_messages fill in
                later); omit it and behaviour is exactly as before.
        """
        self.user_tasks = user_tasks
        self.user_prompt = user_prompt
        self._peer_agents = list(peer_agents or [])

    def real_tool_answer(self, tool_call_id):
        """The REAL content answering this call, from a peer agent's buffer.

        WHY THIS EXISTS.  Measured live 2026-09-07 (agent 18088688973):
        google_search really ran and really fetched five engines, yet the brief
        cited nothing.  On the wire the median tool result was 45 chars —
        exactly HISTORICAL_TOOL_PLACEHOLDER — 116/119 under 120 chars, 1/119
        carrying a URL.  The model cannot cite what it never received.

        The answers are not lost: they sit in the executing seat's own
        ``_oai_messages`` buffer (measured: Assistant n=14 calls=10 answers=4
        beside six broadcast copies at answers=0).  Same process, same turn,
        same conversation — so this reads them directly rather than caching or
        reconstructing anything.

        Returns None when no peer holds a real answer, so the caller keeps the
        placeholder and the fabrication gate still sees the truth.  Never
        returns the placeholder itself as if it were real, and never raises —
        it runs inside the transform on every LLM call.
        """
        try:
            for agent in self._peer_agents:
                buffers = getattr(agent, '_oai_messages', None)
                if not buffers:
                    continue
                for conv in list(buffers.values()):
                    for m in (conv or []):
                        if tool_call_id not in answered_call_ids(m):
                            continue
                        # Consolidated reply: take THIS call's own entry, not
                        # the joined blob of every call in the batch.
                        for r in (m.get('tool_responses') or []):
                            if isinstance(r, dict) and r.get('tool_call_id') == tool_call_id:
                                c = r.get('content')
                                if c and c != HISTORICAL_TOOL_PLACEHOLDER:
                                    return c
                        c = m.get('content')
                        if c and c != HISTORICAL_TOOL_PLACEHOLDER:
                            return c
        except Exception as e:
            try:
                current_app.logger.debug(f"real_tool_answer lookup skipped: {e}")
            except Exception:
                pass
        return None

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
            _dropped_user: List[str] = []
            _coalesced: List[str] = []
            _stale_terms: List[str] = []
            _last_idx = len(messages) - 1
            for i, msg in enumerate(messages):
                role = (msg.get('role') or '').lower()
                content = msg.get('content')
                has_calls = bool(msg.get('tool_calls') or msg.get('function_call'))
                _empty = content is None or (isinstance(content, str) and content.strip() == '')
                # Drop empty assistant placeholders (no content + no tool calls)
                if role == 'assistant' and not has_calls and _empty:
                    _dropped.append(f"{i}({msg.get('name','unknown')})")
                    continue
                # Drop an empty USER message too: the same placeholder seen
                # from the other side.  In a group chat another agent's turn
                # reaches the speaker as role=user, so an agent that said
                # nothing arrives as {"role": "user", "content": ""}.  The
                # hosted Qwen endpoint answers any request holding one with a
                # bare 400 "invalid request".  Measured on central 2026-09-13:
                # a recipe request opening with Message[0] user/Assistant ""
                # failed, and the same conversation passed once that message
                # had text or was removed, with or without its name field.
                if role == 'user' and not has_calls and _empty:
                    _dropped_user.append(f"{i}({msg.get('name','unknown')})")
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
                #
                # role='tool' is excluded here for the same reason it is
                # excluded from the coalescing below: a tool message is an
                # answer slot keyed by tool_call_id, so dropping one always
                # orphans its call and mints a placeholder over the real
                # output.  A result that happens to read TERMINATE is a
                # RESULT, never a control token.  Unlike the coalescing case
                # this one has not been observed in production — it is the
                # same invariant applied at the sibling site, pinned by test.
                if (not has_calls and i < _last_idx
                        and role != 'tool'
                        and isinstance(content, str)
                        and content.strip() == _TERMINATE_TOKEN):
                    _stale_terms.append(f"{i}({msg.get('name','unknown')})")
                    continue
                # Coalesce consecutive same-role messages.
                #
                # NOT role='tool'.  A tool message is an ANSWER SLOT addressed
                # by tool_call_id, not prose: merging two of them keeps only
                # the first id, so every other call is left unanswered and
                # :1889 stamps HISTORICAL_TOOL_PLACEHOLDER over output that
                # really was produced.  The guard already refuses to merge a
                # message that CARRIES tool_calls for exactly this reason
                # ("would silently drop the call") — that protected the
                # question and never the answer, because a tool message has
                # no 'tool_calls' key and so has_calls is False here.
                #
                # Measured live 2026-09-07 on the installed build: 104
                # occurrences across two log rotations, e.g. 03:47:27 merged
                # indices 10..16 — seven results into one message, six answers
                # destroyed in a single call.
                #
                # Nothing is lost by excluding them.  The 400 this guard
                # exists to prevent is the user/assistant alternation rule;
                # consecutive tool messages are REQUIRED by that same API,
                # one per tool_call in a parallel-call assistant message.
                if cleaned:
                    prev = cleaned[-1]
                    prev_role = (prev.get('role') or '').lower()
                    prev_has_calls = bool(prev.get('tool_calls') or prev.get('function_call'))
                    if (prev_role == role
                            and role != 'tool'
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
            if _dropped or _dropped_user or _coalesced or _stale_terms:
                _cap = 12
                _d = ', '.join(_dropped[:_cap]) + (
                    f" (+{len(_dropped) - _cap} more)" if len(_dropped) > _cap else '')
                _u = ', '.join(_dropped_user[:_cap]) + (
                    f" (+{len(_dropped_user) - _cap} more)" if len(_dropped_user) > _cap else '')
                _c = ', '.join(_coalesced[:_cap]) + (
                    f" (+{len(_coalesced) - _cap} more)" if len(_coalesced) > _cap else '')
                _s = ', '.join(_stale_terms[:_cap]) + (
                    f" (+{len(_stale_terms) - _cap} more)" if len(_stale_terms) > _cap else '')
                current_app.logger.info(
                    f"[ROLE-ORDER-GUARD] {len(messages)} msgs out of "
                    f"{len(_dropped) + len(_dropped_user) + len(_coalesced) + len(_stale_terms) + len(messages)} in; "
                    f"dropped {len(_dropped)} empty assistant placeholder(s)"
                    f"{' at ' + _d if _dropped else ''}; "
                    f"{'dropped %d empty user message(s) at %s; ' % (len(_dropped_user), _u) if _dropped_user else ''}"
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

        # A conversation with no user turn is refused by both model servers:
        # llama-server's Qwen3 template 500s, and central's hosted endpoint
        # answers a bare 400 (measured 2026-09-13, task #89).  The wire trim
        # applies the same rule but only sees local llama-server traffic, so
        # this last step of the agent path applies it too.  Dropping empty
        # user messages above can itself be what leaves none.
        from core.llm_outbound_logger import ensure_user_turn
        if ensure_user_turn(messages):
            current_app.logger.info(
                "[ROLE-ORDER-GUARD] no user turn left; seeded one "
                "(WIRE_USER_SEED_TEXT)")
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
        """Create a log-safe version of message with truncated content.

        ``msg.copy()`` is SHALLOW, so ``log_msg['tool_calls']`` is the caller's
        own list object.  Assigning into it (``log_msg['tool_calls'][i] = ...``)
        is ``list.__setitem__`` on that shared list and writes the truncated
        copy straight back into the live message — the per-entry ``.copy()``
        calls below protect the dicts but not the list holding them.

        Measured cost of that, live 2026-09-06 17:08-17:55 (agent 89555447799):
        every tool_call with arguments over 200 chars reached the executor cut
        to ``[:1000] + "... [truncated]"``, i.e. no longer valid JSON, so
        ``ensure_tool_call_arguments_json`` repaired it into a wrong dict or
        ``'{}'``.  The turn ended with the agent telling the user "the previous
        attempts to open LinkedIn failed because I didn't have the correct
        parameters", and one executor error was literally
        ``send_message_to_user() got an unexpected keyword argument 'remains'``
        — 'remains' being a word from inside the article draft the model had
        correctly placed in ``text``.  Short arguments were unaffected, which
        is why execute_windows_or_android_command survived 14/15 and
        send_message_to_user 0/17.

        Owning each list before writing into it keeps the truncation (the log
        line stays small) while confining it to the copy.
        """
        log_msg = msg.copy()

        # Truncate main content
        if 'content' in log_msg and log_msg['content']:
            log_msg['content'] = self.truncate_content(log_msg['content'], max_words)

        # Truncate tool_responses content if present
        if 'tool_responses' in log_msg and isinstance(log_msg['tool_responses'], list):
            log_msg['tool_responses'] = list(log_msg['tool_responses'])
            for i, response in enumerate(log_msg['tool_responses']):
                if 'content' in response and response['content']:
                    log_msg['tool_responses'][i] = response.copy()
                    log_msg['tool_responses'][i]['content'] = self.truncate_content(
                        response['content'], max_words
                    )

        # Truncate tool_calls arguments if they're very large
        if 'tool_calls' in log_msg and isinstance(log_msg['tool_calls'], list):
            log_msg['tool_calls'] = list(log_msg['tool_calls'])
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

    def stale_phantom_call_ids(self, messages):
        """Tool calls announced by a FINISHED action that never produced a result.

        MEASURED LIVE 2026-09-09 06:46:23-06:46:34 (agent 33323830039, action
        2 of 2, installed build).  Action 1 had completed honestly.  Action 2
        then had its own full round allowance and still reported
        ``{'status': 'pending', ...}``.  In its 10.4-second window every one of
        4 LLM calls was over budget and left-trimmed (est 6291 -> 6519 tokens
        against a 5484 budget — the body GREW), and the model was shown 7
        placeholder tool answers against 3 real ones.  It said `pending`
        because 70% of the results in its view were
        HISTORICAL_TOOL_PLACEHOLDER: it could not see its own work.

        Those 7 were not action 2's calls.  STEP 4 below treats EVERY
        unanswered tool_call id anywhere in the accumulated conversation as
        "historical pending" and answers it with a manufactured string, and
        under ``clear_history=False`` nothing ever ages one out.  So a call the
        model announced during action 1 but never executed is re-answered with
        a placeholder on every later request, forever — costing budget and
        reporting "your tools produced nothing" at the same time.

        WHOSE CALL IS IT.  ``evidence_seen_call_ids`` already answers exactly
        that: ``_stamp_action_evidence_watermark`` records, at each dispatch,
        the tool calls that already existed when THIS action started — someone
        else's work.  The fabrication gate reads the same set to refuse
        crediting an earlier action's result.  Reusing it keeps ONE notion of
        ownership; a second rule here would be free to drift from the gate's.

        Deliberately conservative, in this order:
          - no watermark recorded -> nothing is stale (behaviour unchanged);
          - answered anywhere in this list -> real work, keep it;
          - a peer agent holds the answer -> keep it, so the fill below can
            put the REAL result in the slot (the 2026-09-07 repair);
          - only then is it a phantom.

        Never raises: this runs inside the transform on every LLM call.
        """
        try:
            session = (self.user_tasks or {}).get(self.user_prompt)
            seen = getattr(session, 'evidence_seen_call_ids', None)
        except Exception:
            seen = None
        if not isinstance(seen, (set, frozenset)) or not seen:
            return set()

        answered = set()
        announced = set()
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            answered |= answered_call_ids(m)
            for tc in (m.get('tool_calls') or []):
                if isinstance(tc, dict) and tc.get('id'):
                    announced.add(tc['id'])

        stale = set()
        for call_id in (announced & set(seen)):
            if call_id in answered:
                continue
            if self.real_tool_answer(call_id):
                continue
            stale.add(call_id)
        return stale

    def drop_stale_phantom_tool_calls(self, messages):
        """Remove the announcements identified by stale_phantom_call_ids.

        Done BEFORE the pending-call bookkeeping so the phantom never enters
        ``pending_tool_calls`` and no placeholder is minted for it — rather
        than deleting a placeholder after the fact, which would leave the
        assistant announcement behind for the next transform to re-answer.

        Copies any message it edits.  These dicts are the agents' own
        ``_oai_messages`` entries; mutating one would rewrite the
        conversation's real history, not just this request body.
        """
        stale = self.stale_phantom_call_ids(messages)
        if not stale:
            return messages

        out = []
        for m in messages:
            if not isinstance(m, dict) or not m.get('tool_calls'):
                out.append(m)
                continue
            kept = [tc for tc in m['tool_calls']
                    if not (isinstance(tc, dict) and tc.get('id') in stale)]
            if len(kept) == len(m['tool_calls']):
                out.append(m)
                continue
            m = dict(m)
            if kept:
                m['tool_calls'] = kept
            else:
                m.pop('tool_calls', None)
                if not str(m.get('content') or '').strip():
                    continue  # the announcement was all the message carried
            out.append(m)

        current_app.logger.info(
            f"[PHANTOM-DROP] {len(stale)} tool call(s) announced by a finished "
            f"action produced no result and were dropped instead of being "
            f"answered with a placeholder: {sorted(stale)} "
            f"({len(messages)} msgs -> {len(out)})")
        return out

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

        # Drop announcements left behind by actions that are already finished,
        # before anything downstream counts them as needing an answer.
        messages = self.drop_stale_phantom_tool_calls(messages)

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

        # STEP 1: Handle first message if it's a tool message (special case)
        if processed_messages and processed_messages[0].get('role') == 'tool':
            current_app.logger.info('GOT TOOL AS FIRST MESSAGE CHANGING IT')
            processed_messages[0]['role'] = 'user'
            processed_messages[0]['name'] = 'Helper'
            if 'tool_call_id' in processed_messages[0]:
                del processed_messages[0]['tool_call_id']
            processed_messages = processed_messages[1:]

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

            # Insert the consolidated response as ONE tool message per call.
            # The bundled shape (role=tool, tool_responses=[...], no top-level
            # tool_call_id) is autogen's internal form, not the API's: the
            # hosted Qwen endpoint answers any request holding it with a bare
            # 400 "invalid request", with or without a user turn (measured on
            # central 2026-09-13, task #89), while the same results as one
            # tool message per tool_call_id pass.  A bundle without per-call
            # entries (the tool_call_ids form) has nothing to split and goes
            # in as it did.
            _per_call = [
                {'role': 'tool', 'tool_call_id': r['tool_call_id'],
                 'content': r.get('content') if r.get('content') is not None else ''}
                for r in (fixed_consolidated.get('tool_responses') or [])
                if isinstance(r, dict) and r.get('tool_call_id')
            ]
            final_messages[insert_position:insert_position] = (
                _per_call or [fixed_consolidated])
            current_app.logger.info(
                f"Inserted consolidated response with {len(tool_call_ids)} tool_call_ids after assistant message at index {most_likely_assistant_idx}"
                + (f" as {len(_per_call)} tool message(s)" if _per_call else ""))

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

                            # Fill the slot with the REAL result when a peer
                            # agent already holds it.  This is the same "fill
                            # the answer slot" repair as before — only the
                            # content changes, from a manufactured string to
                            # what the tool actually returned.  Falls back to
                            # the placeholder when nothing real exists, so a
                            # genuinely unanswered call still looks unanswered.
                            _real = self.real_tool_answer(tool_call_id)
                            if _real:
                                # A peer's buffer holds the result as it ran,
                                # and this runs after the context limiter, so
                                # bound it the way the limiter bounds a tool
                                # result (#104 review: a peer's whole answer
                                # reached the model uncut).
                                from core.constants import AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE
                                from core.token_utils import fit_texts_to_token_budget
                                _real = fit_texts_to_token_budget(
                                    [_real], AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE)[0]
                            placeholder = {
                                'role': 'tool',
                                'name': function_name or assistant_msg.get('name', 'Assistant'),
                                'tool_call_id': tool_call_id,
                                'content': _real or HISTORICAL_TOOL_PLACEHOLDER
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
                            # peers=N is the DISCRIMINATOR, not decoration.
                            # ToolMessageHandler is constructed at 9 sites and
                            # only 2 pass peer_agents (reuse_recipe :1633,
                            # :2311); the other 7 -- create_recipe :1135/:2891/
                            # :3571/:3682 and reuse_recipe :1010/:3201 -- leave
                            # it empty, and real_tool_answer iterates exactly
                            # that list, so with peers=0 it can ONLY ever
                            # return None and "no peer holds it" is vacuous.
                            # Live 2026-09-11 07:29-07:32 (agent 89091774807):
                            # 39 placeholders, every one "no peer holds it",
                            # and the model then invented "38.4 GB" against a
                            # real 9.17 GB.  Without this count the log cannot
                            # say whether the answer was genuinely absent
                            # (peers>0, a real defect upstream) or was never
                            # looked for (peers=0, a wiring gap here).
                            current_app.logger.info(
                                f"[TOOL-ANSWER-FILL] {tool_call_id} <- "
                                f"{'REAL result %d chars' % len(_real) if _real else 'placeholder (no peer holds it)'}"
                                f" peers={len(self._peer_agents)}"
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


class ToolActivityAsEvidence:
    """Show a judging seat the other seats' tool calls as a report.

    autogen 0.2.37 ``_append_oai_message`` (conversable_agent.py:667-668)
    gives role="assistant" to every message carrying tool_calls, whoever
    sent it.  So the StatusVerifier, which holds no tools, receives the
    Assistant's call as its own turn, and the model continues that turn
    instead of judging it.

    Live 2026-09-14, two agents walked as their owners: the verifier
    answered with the Assistant's call written out as <tool_call> text
    (20260824301, 3 of 3 rounds) or with the Assistant's next step
    (12165936867).  Neither action got a verdict, both turns spent their 12
    rounds, and the user was handed the leftover text.  The verifier's own
    logged requests, replayed against the live llama-server: 4/4 answered
    with tool-call text as logged, 4/4 with a JSON verdict once the calls
    and results were told as a report from the seat that made them.

    This runs after the shared ToolMessageHandler, so a result held only by
    a peer seat has already been filled in.  A seat that can run a call or a
    code block keeps the raw structure: generate_reply hands the transformed
    list to every reply function, tool and code execution included.
    """

    def __init__(self, seat):
        self._seat = seat

    def _acts_on_calls(self):
        seat = self._seat
        return bool(getattr(seat, '_function_map', None)
                    or getattr(seat, '_code_execution_config', False)
                    or (getattr(seat, 'llm_config', None) or {}).get('tools'))

    def apply_transform(self, messages: List[Dict]) -> List[Dict]:
        if self._acts_on_calls() or not any(
                m.get('tool_calls') or m.get('role') == 'tool' for m in messages):
            return messages
        answered = set()
        for m in messages:
            if m.get('role') == 'tool':
                answered.add(m.get('tool_call_id'))
                answered.update(r.get('tool_call_id') for r in (m.get('tool_responses') or [])
                                if isinstance(r, dict))
        calls, out, n_calls, n_results = {}, [], 0, 0
        for m in messages:
            if m.get('tool_calls'):
                who = m.get('name') or 'Assistant'
                if str(m.get('content') or '').strip():
                    out.append({'role': 'user', 'name': who, 'content': m['content']})
                for tc in m['tool_calls']:
                    fn = tc.get('function') or {}
                    line = f"{who} called {fn.get('name')}({fn.get('arguments') or ''})"
                    n_calls += 1
                    if tc.get('id') in answered:
                        calls[tc.get('id')] = (who, line)
                    else:
                        out.append({'role': 'user', 'name': who,
                                    'content': f"{line}\nTool result: (none recorded)"})
            elif m.get('role') == 'tool':
                for r in (m.get('tool_responses') or [m]):
                    who, line = calls.pop(r.get('tool_call_id'),
                                          (m.get('name') or 'Tool', 'A tool call'))
                    out.append({'role': 'user', 'name': who,
                                'content': f"{line}\nTool result: {r.get('content')}"})
                    n_results += 1
            else:
                out.append(m)
        _safe_log('info',
                  f"[JUDGE-VIEW] {getattr(self._seat, 'name', '?')}: {n_calls} tool "
                  f"call(s) and {n_results} result(s) shown as a report")
        # The same role-order guard the shared chain ends with: the report
        # lines are user turns, and consecutive ones are merged the same way.
        return ToolMessageHandler().validate_messages(out)

    def get_logs(self, pre_transform_messages: List[Dict],
                 post_transform_messages: List[Dict]) -> Tuple[str, bool]:
        changed = any(m.get('tool_calls') or m.get('role') == 'tool'
                      for m in pre_transform_messages)
        return ("tool activity shown as a report" if changed else "no tool activity",
                changed)


def give_judge_view(seat):
    """Give a judging seat ToolActivityAsEvidence.

    Call it after the seat's shared TransformMessages: hooks run in the order
    they were registered, and the shared chain fills the real tool answers
    this view reports.
    """
    transform_messages.TransformMessages(
        transforms=[ToolActivityAsEvidence(seat)], verbose=False).add_to_agent(seat)


class Action:
    def __init__(self,actions):
        self.actions = actions
        self.current_action = 1
        self.fallback = False
        self.new_json = []
        self.recipe = False
        self.ledger = None  # Smart Ledger for persistent task tracking
        # One id per execution of these actions.  bank_vlm_learning keys on
        # it: computer-use calls inside one execution of an action extend
        # that action's learning; a later execution re-learns it.
        self.run_id = uuid.uuid4().hex

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

    # Fallback: Redis (legacy path).  Only a cloud camera pipeline writes
    # frames there; a desktop runs no Redis, and redis_client is built lazily
    # so it is never None.  A refused connect means "no frame" -- the
    # callers' None branch already tells the user the camera is off -- and
    # costs 4.07 s on Windows (measured 2026-09-13), so after one refusal the
    # breaker skips Redis for its cooldown.  Unguarded, the ConnectionError
    # escaped get_user_camera_inp 246 times on 2026-09-13 and agents went on
    # to drive the desktop trying to start Redis.
    if redis_client is None or _REDIS_FRAME_BREAKER.is_open(redis_client):
        return None
    try:
        serialized_frame = redis_client.get(user_id)
    except redis.RedisError as e:
        _REDIS_FRAME_BREAKER.record_failure(redis_client)
        current_app.logger.info(
            f"No frame for user_id {user_id}: Redis unavailable ({e})")
        return None
    _REDIS_FRAME_BREAKER.record_success(redis_client)
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
    """Answer ``inp`` from the user's latest camera frame.

    Raises RuntimeError when there is nothing to answer from: no frame for
    this user, or a frame no vision model could read.  Both used to RETURN
    'failed to get visual context ask user to check if the camera is turned
    on', so core.tool_logging logged TOOL EXECUTION SUCCESS and
    core.constants.tool_reply_failed (CREATE's trace banker, REUSE's
    fabrication gate) counted the call as done work.  Measured on the MSI
    desktop, agent_system.log + .1, 2026-09-22 to 09-29: 732 calls, all 732
    that sentence, all 732 logged as a success, 530 of them from the daemon's
    own account (hevolve_system_agent), which no camera is ever keyed to.
    Raised, the call reaches the model as the canonical failure envelope.
    The text names the camera, never the Redis leg of get_frame: naming Redis
    sent agents off to start it (tests/unit/test_get_frame_redis_down.py).
    """
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
        except Exception as e:
            # Fall through to MiniCPM/cloud, saying why.
            current_app.logger.info('Local VLM did not answer the camera question: %s', e)

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
            raise RuntimeError(
                "The user's camera frame was captured, but no vision model "
                "could read it, so this question about the camera was not "
                "answered.") from e
    else:
        raise RuntimeError(
            "No camera frame is shared for this user right now, so there is "
            "nothing to look at. get_user_camera_inp only answers questions "
            "about what the user's live camera shows; it cannot answer "
            "anything else.")



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
    # THE ID STAYS A STRING.  Both consumers below already take one —
    # `ConversationEntry.user_id == str(user_id)` converts straight back, and
    # SimpleMemChatMemory.load_or_create takes it as-is — so int() never did
    # anything except narrow the accepted id space.  Nunba's guest ids are
    # UUIDs, so that narrowing made this function return EMPTY for them
    # before querying any store.
    #
    # Measured live 2026-09-10, agent 92583386981, 50 ms apart:
    #   14:58:55,851 WARNING bad session_id user_3e2908ac-3ff6-4198-bc46-
    #                9ec43a2aac9a: invalid literal for int() with base 10
    #   14:58:55,901 tool    {"res": []}   (= get_chat_history)
    # The caller cannot tell that from "you have no history", and the model
    # filled the gap by inventing a CEFR level for the user (#817/D52).
    #
    # NO REGRESSION for numeric ids: str(int('123')) == '123' == str('123'),
    # and the DB filter is the only place the value is used, so what gets
    # queried is unchanged for every integer user.
    user_id = str(session_id or '')
    if user_id.startswith('user_'):
        user_id = user_id[len('user_'):]
    if not user_id.strip():
        # Still refused — an empty id is not a user, and querying on it would
        # match whatever rows carry an empty user_id.
        try:
            current_app.logger.warning(
                f"get_time_based_history: no user id in session_id "
                f"{session_id!r}")
        except Exception:
            pass
        return _json.dumps({'res': []})

    window = resolve_recall_window(start_date, end_date)

    if window is not None:
        win_lo, win_hi = window
        try:
            # From the facade, never from _models_local: on an install with
            # sql.models, executing the fallback module re-registers every
            # table on the shared Base and every later query fails with
            # "Multiple classes found for path" (live 2026-09-15 12:21:32,
            # this function; the owner's consent clicks failed for the
            # rest of the process).
            from integrations.social.models import ConversationEntry, get_db
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

    user_id goes out as given: a desktop user's id is a UUID string (the
    bundled /conversation route stores it as is), and a cloud user's integer
    id stays an integer.  int(user_id) here failed every Generate_video
    avatar call for UUID users before anything was sent.
    """
    headers = {'Content-Type': 'application/json'}
    data = {
        "request": 'VIDEO GENERATION FROM GENERATE_VIDEO',
        "response": text.strip(),
        "user_id": user_id,
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
            history_limiter(max_messages=50, keep_first_message=True),
            token_limiter(max_tokens=3500, max_tokens_per_message=1000, min_tokens=0),
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
    give_judge_view(verify2)

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
        from core.platform_paths import get_agent_data_dir
        return get_agent_data_dir()
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'agent_data')

AGENT_DATA_DIR = _resolve_agent_data_dir()
try:
    if not os.path.exists(AGENT_DATA_DIR):
        os.makedirs(AGENT_DATA_DIR, exist_ok=True)
except PermissionError:
    # Fallback: user home directory (e.g. bundled app in Program Files)
    from core.platform_paths import get_agent_data_dir as _get_agent_fallback
    AGENT_DATA_DIR = _get_agent_fallback()
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
def tool_call_shape(arguments):
    """``(args, kwargs)`` a tool is called with for parsed ``arguments``.

    THE one rule for turning parsed tool-call arguments into a call, read by
    safe_function_call (which makes the call), the async executor (which
    awaits a coroutine tool with it) and tool_argument_error (which binds it
    to the signature first), so the checked call is the call that runs:

      * a dict                      -> ``func(**dict)``
      * a list whose head is a dict -> ``func(**list[0])`` (retrieve_json
        often wraps the object in a list; the rest of the list is ignored)
      * any other list              -> ``func(*list)``
      * anything else               -> ``func(arguments)``
    """
    if isinstance(arguments, dict):
        return (), arguments
    if isinstance(arguments, list):
        if arguments and isinstance(arguments[0], dict):
            return (), arguments[0]
        return tuple(arguments), {}
    return (arguments,), {}


def remapped_positional_kwargs(func, arguments):
    """safe_function_call's recovery for a positional list that did not bind:
    drop ``['truncated']`` sentinels and name the rest after the signature's
    parameters, in order.  The kwargs, or None when that does not apply."""
    if not isinstance(arguments, list) or not hasattr(func, '__annotations__'):
        return None
    import inspect
    param_names = list(inspect.signature(func).parameters.keys())
    clean_args = [arg for arg in arguments if
                  not (isinstance(arg, list) and len(arg) == 1 and arg[0] == 'truncated')]
    if len(clean_args) > len(param_names):
        return None
    return dict(zip(param_names, clean_args))


def safe_function_call(func, arguments):
    """Call a tool with parsed arguments, shaped by :func:`tool_call_shape`."""
    import logging

    logger = logging.getLogger("safe_function_call")

    logger.info(" SAFE_FUNCTION_CALL DEBUG:")
    logger.info(f"   Function: {func.__name__ if hasattr(func, '__name__') else func}")
    logger.info(f"   Arguments type: {type(arguments)}")
    logger.info(f"   Arguments content: {arguments}")

    try:
        args, kwargs = tool_call_shape(arguments)
        logger.info(f"   → Calling with {len(args)} positional, keywords {list(kwargs)}")
        result = func(*args, **kwargs)
        logger.info("    Success")
        return result

    except TypeError as e:
        logger.error(f"    TypeError: {e}")
        logger.error(f"   TypeError traceback:\n{traceback.format_exc()}")

        # A positional list that did not bind: try the sentinel-free list
        # named after the signature (a list headed by a dict already WAS a
        # keyword call, so retrying it would repeat the same TypeError).
        if isinstance(arguments, list) and not (
                arguments and isinstance(arguments[0], dict)):
            logger.info("   → Trying enhanced list handling")

            try:
                kwargs = remapped_positional_kwargs(func, arguments)
                if kwargs is not None:
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


def _is_numeric_annotation(annotation):
    """True for int / float, also inside Optional / Union / Annotated."""
    return any(t in (int, float) for t in _annotation_types(annotation))


def _annotation_types(annotation):
    """The plain types an annotation admits: Annotated unwrapped, Optional /
    Union flattened (None dropped)."""
    import typing
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _annotation_types(typing.get_args(annotation)[0])
    if origin is typing.Union:
        return [t for a in typing.get_args(annotation) if a is not type(None)
                for t in _annotation_types(a)]
    return [annotation]


def _list_element_annotation(annotation):
    """The element annotation of a List[X] / list[X] parameter, else None."""
    import typing
    for t in _annotation_types(annotation):
        if typing.get_origin(t) is list and typing.get_args(t):
            return typing.get_args(t)[0]
    return None


def _unholdable_token(value):
    """True for a string the reader kept for a number no double can hold, or
    for NaN / Infinity."""
    if not isinstance(value, str):
        return False
    token = value.strip()
    return token in _NON_FINITE_WORDS or bool(
        _BARE_NUMBER.fullmatch(token) and not math.isfinite(float(token)))


def _exact_int_token(value):
    """The exact int for a whole-number token a double cannot hold (Python
    ints are exact), else None."""
    if isinstance(value, str) and re.fullmatch(r'-?\d+', value.strip()):
        try:
            return int(value.strip())
        except ValueError as e:
            # Past Python's int-from-text digit limit (4300): not a value the
            # tool can be given, so it is refused like any unholdable number
            # (review of 1bf298f5b: uncaught, the turn died).
            _fallback_logger.debug(f"whole number too long to read: {e}")
            return None
    return None


def _number_for(value, types):
    """What a number-typed parameter admitting ``types`` gets for ``value``:
    ``(True, value)`` to pass it as it is, ``(True, exact_int)`` for an exact
    whole number an int parameter can hold, ``(False, None)`` to refuse a
    number no double can hold, or NaN / Infinity.  THE one rule, for a
    single value and for each element of a list (review of 1bf298f5b:
    List[int] refused what int took)."""
    if str in types or not any(t in (int, float) for t in types):
        return True, value
    if not _unholdable_token(value):
        return True, value
    if int in types:
        exact = _exact_int_token(value)
        if exact is not None:
            return True, exact
    return False, None


def _numbers_out_of_range(params, bound):
    """Names of number-typed parameters bound to the token of a number no
    double can hold, or to NaN / Infinity, which the reader keeps as text
    (_number_for decides each value)."""
    out = []
    for p in params:
        value = bound.get(p.name)
        element = _list_element_annotation(p.annotation)
        if isinstance(value, list) and element is not None:
            types = _annotation_types(element)
            if not all(_number_for(v, types)[0] for v in value):
                out.append(p.name)
            continue
        if not _number_for(value, _annotation_types(p.annotation))[0]:
            out.append(p.name)
    return out


def _exact_int_arguments(func, arguments):
    """``arguments`` with each value _number_for turns into an exact int --
    a whole number a double could not hold, for an int parameter or an
    element of a List[int] -- replaced by that int.  Anything else, and
    arguments that are not a dict, as they are."""
    import inspect
    if not isinstance(arguments, dict):
        return arguments
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return arguments
    out = dict(arguments)
    for name, value in arguments.items():
        p = params.get(name)
        if p is None:
            continue
        element = _list_element_annotation(p.annotation)
        if isinstance(value, list) and element is not None:
            types = _annotation_types(element)
            out[name] = [_number_for(v, types)[1] if _number_for(v, types)[0]
                         else v for v in value]
        else:
            ok, got = _number_for(value, _annotation_types(p.annotation))
            if ok:
                out[name] = got
    return out


def tool_argument_error(func, func_name, arguments, repaired, as_written=None):
    """Why ``func`` cannot be called with ``arguments``, or None.

    Binds the call the arguments actually become (:func:`tool_call_shape`,
    the same rule safe_function_call and the async executor call with) to the
    tool's own signature (``inspect.signature`` follows the ``__wrapped__``
    chain of autogen's and core.tool_logging's wrappers to the real closure).
    Until the review of 68377afd2 only a dict was checked, so ``[{...}]`` --
    run as ``func(**list[0])`` -- reached the tool unchecked.  A positional
    list is bound as the positional call it becomes; safe_function_call's
    ``['truncated']`` recovery is not credited, because a live tool is wrapped
    by core.tool_logging, which answers the first call's TypeError itself, so
    that recovery never runs for it.  A tool whose signature cannot be read
    is not checked.

    ``repaired`` says the arguments did not parse as JSON and were recovered by
    retrieve_json.  json_repair reads an unquoted string value as a run of
    bare ``key: value`` pairs: live 2026-09-22 08:23:01, ``{"text": Financial
    Dashboard ... - Consulting: $5,000 ...}`` became ``{"text": "...$10",
    "Consulting": "5,000", ...}`` and the tool raised "unexpected keyword
    argument 'Consulting'", which reads as a naming mistake.  So a repaired
    call that does not bind is reported as broken JSON, with the keys that
    could be read shown as what was received, not as names to fix.  A
    repaired call that binds but leaves a required value empty (None, or a
    blank string json_repair filled in) is refused the same way.

    ``as_written`` is the text the model wrote.  After a repair, a key that
    the tool does not declare and the model did not write in quotes is
    refused the same way, whatever the signature.  Such a key is either one
    the model wrote bare (``cwd: "/tmp"``) or one json_repair split out of
    an unquoted value (``{"command": deploy the app, then report status:
    ok}`` -> ``status``); the two cannot be told apart, and a tool that
    takes **kwargs binds any name, so the split used to run it with the
    value cut short (review of the defect-14 fix; MCP tool_executor and
    service_tools endpoint_executor keep a **kwargs signature when a tool
    has no schema).  For such a tool the refusal asks for every key in
    double quotes and does not present the declared names as the only ones
    (review of 30042a2b6: that steered the model into dropping ``cwd``).
    Strict JSON quotes every key, so it is not scanned.  After a repair, a
    value left empty that the model did not write empty (``"cwd": /tmp``
    -> ``""``) is refused like a required one.

    A declared name is no safer (review of 30042a2b6, problem 1): the split
    can make a key the tool declares, and the dict keeps only the last
    value, so ``{"command": ls -la, command: rm -rf /tmp/x}`` ran
    ``rm -rf /tmp/x``.  So after a repair every key must be one the model
    wrote where a key starts, and a key written twice, or written bare after
    a value that was not one whole value, is refused.  All of it is read by
    the one reader, once (_outermost_entries).
    """
    import inspect
    if not isinstance(arguments, dict):
        # One JSON object of named values, or not run -- the rule the history
        # guard (ensure_tool_call_arguments_json) applies to the same call.
        # The executor used to run '[1, 2]' positionally and '"hello"' as one
        # argument while the guard rewrote the call in the history to the
        # refused stand-in ("the call was not run"); the two now agree
        # (coordinator's default, review of dbfef4360).  Measured before:
        # all 854 tool calls models made in llm_outbound.jsonl + .old carry
        # an object.
        return (f"Error: {func_name} was not run: "
                + refusal_sentence(NOT_AN_OBJECT_REASON))
    args, kwargs = tool_call_shape(arguments)
    if REFUSED_ARGUMENTS_KEY in kwargs:
        # A refused call's stand-in (refused_arguments_json) is never run,
        # whatever the tool accepts: a **kwargs tool binds any names, so the
        # signature check alone let it through (review of cd8d9154d, M3).
        return (f"Error: {func_name} was not run: these are the stand-in for "
                f"arguments that were refused earlier "
                f"({kwargs.get(REFUSED_BECAUSE_KEY, 'refused')}). Call "
                f"{func_name} again with one JSON object of named values.")
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return None
    params =[p for p in sig.parameters.values()
              if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
    try:
        bound = sig.bind(*args, **kwargs).arguments
    except TypeError:
        bound = None
    # A value json_repair filled in, not one the model wrote: a cut-off call
    # '{"text":' repairs to {"text": ""}, which binds, and the tool ran with
    # nothing (log RCA defect 14, leftover).  So after a repair, a REQUIRED
    # value that is empty is refused like a missing one.  Strict JSON with
    # "" is the model's own choice and runs.
    def blank(value):
        return value is None or (isinstance(value, str) and not value.strip())

    emptied = ([p.name for p in params
                if p.default is p.empty and blank(bound.get(p.name))]
               if repaired and bound is not None else [])
    # Any value, required or not, that the repair emptied: empty now, but
    # not left empty by the model (review of 30042a2b6: "cwd": /tmp ran a
    # **kwargs tool with cwd='').
    blanks = [k for k, v in kwargs.items() if blank(v) and k not in emptied]
    # What the model wrote, read once and only after a repair: strict JSON
    # quotes every key and cannot be split (review of 30042a2b6: a scan per
    # extra key took 0.42 s on 100 KB).
    written = (_outermost_entries(as_written)
               if repaired and as_written is not None else None)
    emptied_written = ([k for k in blanks
                        if k not in {e.key for e in written if e.empty}]
                       if written is not None else [])
    unholdable = (_numbers_out_of_range(params, bound)
                  if bound is not None else [])
    if unholdable:
        # The reader keeps a number a double cannot hold (or NaN/Infinity
        # the model wrote) as the token it wrote, a string.  For a parameter
        # typed as a number that string is not a value the tool can take:
        # refused, never passed on as text (review of 3a7abe540, item 6).
        return (f"Error: {func_name} was not run: "
                f"{', '.join(unholdable)} must be a finite number, and the "
                f"value given cannot be held as one. Call {func_name} again "
                f"with a number in range.")
    # After a repair, every key must be one the model wrote as a key: in
    # quotes when the tool does not declare it; and none written twice or
    # bare after an unquoted value, where the dict would keep a value cut
    # out of the one before (review of 30042a2b6, problem 1).
    declared = {p.name for p in params}
    invented, suspect = [], []
    if written is not None:
        as_keys = [e.key for e in written]
        quoted = {e.key for e in written if e.quoted}
        invented = [k for k in kwargs
                    if k not in (as_keys if k in declared else quoted)]
        seen, twice = set(), []
        for k in as_keys:
            if k in seen:
                twice.append(k)
            seen.add(k)
        suspect = list(dict.fromkeys(
            [e.key for e in written if e.doubtful] + twice))
    if (bound is not None and not emptied and not emptied_written
            and not invented and not suspect):
        return None
    takes_any = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
    names = {p.name for p in params}
    expected = ', '.join(p.name + (' (required)' if p.default is p.empty else '')
                         for p in params) or 'none'
    if args and bound is None:
        # Positional values: there are no names to report as unknown.
        return (f"Error: {func_name} was not run: its arguments were not one "
                f"JSON object of named values. Expected parameters: "
                f"{expected}. Call {func_name} again with one JSON object "
                f"using these names.")
    arguments = kwargs
    missing = [p.name for p in params
               if bound is None and p.default is p.empty
               and p.name not in arguments]
    unknown = [] if takes_any else [k for k in arguments if k not in names]
    if repaired:
        text = (f"Error: the arguments for {func_name} were not valid JSON, so "
                f"{func_name} was not run. Every string value must be in "
                f"double quotes, with any double quote inside it written as "
                f"\\\" and any line break as \\n. What could be read from "
                f"them had the keys: {', '.join(map(str, arguments)) or 'none'}.")
    else:
        text = f"Error: {func_name} was not run."
        if unknown:
            text += f" Unknown argument(s): {', '.join(map(str, unknown))}."
    if missing:
        text += f" Missing required argument(s): {', '.join(missing)}."
    if emptied:
        text += f" Required argument(s) left empty: {', '.join(emptied)}."
    if emptied_written:
        text += (f" Value(s) that could not be read and came out empty: "
                 f"{', '.join(map(str, emptied_written))}.")
    if suspect:
        text += (f" Key(s) written twice, or bare after a value not in double "
                 f"quotes, so they may be part of the value before them: "
                 f"{', '.join(map(str, suspect))}.")
    if takes_any:
        # Any name is accepted: the declared ones are not the only ones, so
        # they are not offered as the fix -- quoting is (review of 30042a2b6).
        if invented:
            text += (f" Key(s) not written as a key in double quotes: "
                     f"{', '.join(map(str, invented))}.")
        return (text + f" Declared parameters: {expected}; {func_name} also "
                f"takes other named values. Call {func_name} again with one "
                f"JSON object, every key and every string value in double "
                f"quotes.")
    return (text + f" Expected parameters: {expected}. Call {func_name} again "
            f"with one JSON object using these names.")


def stored_call_records(agent, func_call):
    """Recover only this call's source; never infer it from repaired arguments."""
    handed = func_call.get('arguments', '{}')
    records = [fn
               for conversation in (getattr(agent, '_oai_messages', None) or {}).values()
               for msg in conversation or () for fn in call_functions(msg)]
    if isinstance(handed, _CallArguments):
        if any(fn is handed.record for fn in records):
            return handed.as_written, [handed.record]
        # A guard called directly on a copy has no source identity. An exact,
        # unique raw match can mark history, but cannot change what is parsed.
        matches = [fn for fn in records
                   if fn.get('name') == func_call.get('name')
                   and fn.get('arguments') == handed.as_written]
        return handed.as_written, matches if len(matches) == 1 else []
    return handed, [fn for fn in records if fn is func_call]


def mark_call_refused(records, input_string):
    """Write the refused stand-in (refused_arguments_json: what the model
    wrote, marked refused, and why) into each of ``records``, the
    conversation's own records of a tool call whose arguments were not valid
    JSON and were refused (stored_call_records).  A no-op when there is no
    record to mark."""
    text = input_string if isinstance(input_string, str) else str(input_string)
    for record in records or ():
        if isinstance(record, dict):
            record['arguments'] = refused_arguments_json(text)


def bind_tool_call_arguments(func, func_name, input_string, format_json_str,
                             where='', records=()):
    """Parse a tool call's arguments and check them against the tool.

    Returns ``(arguments, None)`` when the tool may be called with them and
    ``(None, error_text)`` when it may not.  The one parse-and-bind step for
    the patched ``execute_function`` and ``a_execute_function``: autogen's
    strict parse first, retrieve_json only when that fails, then
    ``tool_argument_error`` on whatever came back, including the ``{}``
    used when nothing could be recovered.

    ``input_string`` is the call as the model wrote it and ``records`` the
    conversation's own records of it (stored_call_records: with the
    production TransformMessages the executor is handed a guarded deep copy,
    so neither the handed text nor the handed dict will do).  The seats of
    a conversation share those function dicts (autogen broadcasts the one
    message object), so when arguments that were not valid JSON are
    refused, marking the records (mark_call_refused) marks every seat's
    history.  Log RCA defect 14, leftover: the TOOL-ARGS-GUARD
    (ensure_tool_call_arguments_json), which has no tool signatures, repaired
    the same text on its own and wrote json_repair's split dict back into
    every later request, so the model saw its broken call as a well-formed
    one next to a reply saying it was not valid JSON.  The guard keeps a
    strict JSON object as it is, so the stand-in is what the model sees.
    Refused well-formed JSON (a wrong name) stays as the model wrote it:
    that is what the reply names.
    """
    # ONE reader for tool arguments (parse_tool_arguments), the one the
    # history guard uses, so the tool gets what the model is shown -- never
    # json.loads here: it read an overflowing number as inf and ran the tool
    # with it (review of c21b5e6e2 / 7d07c0a2d).  retrieve_json stays the
    # last resort for shapes the reader cannot repair (an "@user" prefix,
    # Python-literal syntax), and what it returns is refused if it carries
    # Infinity / NaN the model never wrote.
    if not isinstance(input_string, str):
        # Arguments handed as an object or None, not text: read by the
        # history guard's own rule (wire_tool_arguments: a dict serialized,
        # None as {}), so the executor runs what the history shows.  Before,
        # _format_json_str raised on them and the call was refused and
        # marked with the Python repr (review of the defect-14 fix).
        input_string, _ = wire_tool_arguments(input_string)
    try:
        # A strict read of autogen's formatted text first; on failure the
        # reader repairs the RAW text, the text the history guard reads.
        # format_json_str drops newlines, so repairing ITS output let a '//'
        # comment swallow the rest of the arguments (review of 3a7abe540:
        # '{\n "id": "x", // c\n "n": 2\n}' ran as ('x', 2) before, and was
        # refused there).
        try:
            arguments, repaired = load_wire_json(format_json_str(input_string)), False
        except ValueError:
            arguments, repaired = parse_tool_arguments(input_string)
        print(f" ORIGINAL AUTOGEN{where}: parsed arguments for {func_name}"
              f"{' (repaired)' if repaired else ''}")
    except Exception as e:
        print(f" ORIGINAL AUTOGEN{where} FAILED: {e} - falling back to enhanced parsing for {func_name}")
        try:
            arguments = retrieve_json(input_string)
            if arguments is None:
                arguments = {}
            elif isinstance(arguments, str):
                arguments, _ = parse_tool_arguments(arguments)
        except Exception as fallback_error:
            print(f" FALLBACK{where} FAILED: {fallback_error}")
            mark_call_refused(records, input_string)
            return None, f"Error: {e}\n The argument must be in JSON format."
        print(f" FALLBACK{where} PARSED: arguments for {func_name}: {arguments}")
        repaired = True
    if _invents_a_constant(arguments, input_string):
        print(f" ARGUMENTS REFUSED{where}: {func_name} not run: a value the "
              f"model never wrote (Infinity/NaN)")
        mark_call_refused(records, input_string)
        return None, (f"Error: {func_name} was not run: its arguments could "
                      f"not be read without turning a number into Infinity. "
                      f"Write a long number or id as a string in double "
                      f"quotes.")
    error = tool_argument_error(func, func_name, arguments, repaired,
                                as_written=input_string)
    if error is not None:
        print(f" ARGUMENTS REFUSED{where}: {func_name} not run: {error}")
        if repaired:
            mark_call_refused(records, input_string)
        return None, error
    # An int parameter gets an exact whole number a double could not hold
    # as the int it is, not the token text (review of dbfef4360).
    return _exact_int_arguments(func, arguments), None


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
            # The call as the model wrote it, from the conversation's own
            # record: the handed func_call is the guard's copy under the
            # production TransformMessages (stored_call_records).
            input_string, records = stored_call_records(self, func_call)
            arguments, content = bind_tool_call_arguments(
                func, func_name, input_string, self._format_json_str,
                records=records)

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
            input_string, records = stored_call_records(self, func_call)
            arguments, content = bind_tool_call_arguments(
                func, func_name, input_string, self._format_json_str,
                where=' ASYNC', records=records)

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
                        # The call bind_tool_call_arguments checked, not a
                        # second rule: [{...}] used to be awaited as
                        # func({...}), the whole object as one positional.
                        args, kwargs = tool_call_shape(arguments)
                        content = await func(*args, **kwargs)
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

        # Bind before existing transforms deepcopy history. Both reply modes
        # retain the same call identity, without adding another reply runner.
        if not getattr(ConversableAgent, '_hart_argument_sources_bound', False):
            sync_process = ConversableAgent.process_all_messages_before_reply
            async_process = ConversableAgent.a_process_all_messages_before_reply

            def process_with_sources(self, messages):
                return sync_process(self, _bind_argument_sources(messages))

            async def async_process_with_sources(self, messages):
                return await async_process(self, _bind_argument_sources(messages))

            ConversableAgent.process_all_messages_before_reply = process_with_sources
            ConversableAgent.a_process_all_messages_before_reply = async_process_with_sources
            ConversableAgent._hart_argument_sources_bound = True

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
# ── VLM learnings: a file must prove which action it belongs to ─────────────
#
# A ``<agent>_<flow>_<action>_vlm_agent.json`` file is a computer-use run's
# learned steps, and _vlm_merged_actions (reuse_recipe) REPLACES the steps of
# the action whose id the filename names.  Until 5d6343409 the CREATE writer
# (and the REUSE writer until dcf4c6b4b) walked to the next FREE number, so
# the filename number was a free slot, not the action that ran.  MEASURED
# 2026-09-28 on the live prompts dir, through the real loader and merge: 96
# in-range files, all 96 replacing their action's steps, 77 of them below
# _RELEARN_IDENTITY_THRESHOLD against that action.  Agent 18088688973's action
# 3 replayed 'ls -la /data', action 6 'Create a temporary directory at
# C:\Users\testuser\...', action 4 a click on 'Allow' in a Windows Security
# network-access dialog.  Nothing in such a file says which action it ran
# under, and similarity cannot tell (the 'Allow' file scores 0.5, above 0.45).
#
# So the one writer stamps VLM_PROVENANCE_KEY = {prompt_id, flow, action_id}
# from the action that was running, and the one reader accepts a file only
# when that stamp names the file's own coordinates.  A refused file costs the
# action nothing it needs: it keeps its CREATE-authored steps.
VLM_PROVENANCE_KEY = 'learned_for'
VLM_QUARANTINE_DIRNAME = '_quarantine_vlm_unproven'
VLM_QUARANTINE_MARKER = 'quarantine.done.json'
_VLM_FILE_RE = re.compile(
    r'^(?P<prompt_id>.+)_(?P<flow>\d+)_(?P<action_id>\d+)_vlm_agent\.json$')
_vlm_warned = set()
_vlm_warned_lock = threading.Lock()


def vlm_warn_once(logger, key, msg, *args):
    """``logger.warning(msg, *args)`` the first time ``key`` is seen in this
    process.  A refused or orphaned learning is re-read on every agent build;
    one line per file keeps it countable without flooding the log (23 orphan
    warnings per build, measured 2026-09-28)."""
    with _vlm_warned_lock:
        if key in _vlm_warned:
            return False
        _vlm_warned.add(key)
    logger.warning(msg, *args)
    return True


def vlm_learning_refusal(record, prompt_id, flow, action_id):
    """None when ``record`` proves it was learned for exactly this action of
    this flow of this agent; otherwise the reason it cannot be applied there.

    Pure, never raises: it runs while an agent is being built."""
    if not isinstance(record, dict):
        return 'not a JSON object'
    stamp = record.get(VLM_PROVENANCE_KEY)
    if not isinstance(stamp, dict):
        return ('no %s: written before a learning recorded the action it ran '
                'under (a next-free-slot walker filed those under other '
                "actions' ids)" % VLM_PROVENANCE_KEY)
    try:
        same = (str(stamp.get('prompt_id')) == str(prompt_id)
                and int(stamp.get('flow')) == int(flow)
                and int(stamp.get('action_id')) == int(action_id))
    except (TypeError, ValueError):
        same = False
    if not same:
        return '%s %r names another action than prompt %s flow %s action %s' % (
            VLM_PROVENANCE_KEY, stamp, prompt_id, flow, action_id)
    return None


def _read_vlm_file(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def read_vlm_learning(prompt_id, flow, action_id):
    """The proven learning of ONE action, or None (absent, unreadable, or not
    this action's).  What both tools' direct read injects as "steps from a
    previous successful execution", under the loader's rule."""
    file_path = safe_prompt_path(prompt_id, flow, action_id, 'vlm_agent')
    if not os.path.exists(file_path):
        return None
    try:
        record = _read_vlm_file(file_path)
    except Exception as e:
        vlm_warn_once(current_app.logger, ('unreadable', file_path),
                      '[VLM-UNPROVEN] ignoring %s: unreadable (%s)', file_path, e)
        return None
    why = vlm_learning_refusal(record, prompt_id, flow, action_id)
    if why:
        vlm_warn_once(current_app.logger, ('unproven', file_path),
                      '[VLM-UNPROVEN] ignoring %s: %s', file_path, why)
        return None
    record['action_id'] = int(action_id)
    return record


def bank_vlm_learning(prompt_id, flow, action_id, run_id, record):
    """Save a computer-use run's learning as THIS action's, and return the path.

    The ONE writer (CREATE and REUSE both call it).  The filename and the
    stamp both come from ``action_id``, the action that was running, so the
    reader's rule holds by construction.  Calls inside one execution of the
    action (same ``run_id``, helper.Action.run_id) extend its steps in order;
    a new execution, or an unknown one (``run_id`` None), replaces them, so a
    re-learning never piles steps up across runs.  Atomic write.
    """
    file_path = safe_prompt_path(prompt_id, flow, action_id, 'vlm_agent')
    stamp = {'prompt_id': str(prompt_id), 'flow': int(flow),
             'action_id': int(action_id), 'run': run_id}
    out = dict(record)
    out['action_id'] = int(action_id)
    out[VLM_PROVENANCE_KEY] = stamp
    out['recipe'] = list(out.get('recipe') or [])
    if run_id and os.path.exists(file_path):
        try:
            prior = _read_vlm_file(file_path)
        except Exception:
            prior = None
        if (isinstance(prior, dict)
                and vlm_learning_refusal(prior, prompt_id, flow, action_id) is None
                and prior[VLM_PROVENANCE_KEY].get('run') == run_id):
            out['recipe'] = list(prior.get('recipe') or []) + out['recipe']
    atomic_json_write(file_path, out, indent=4)
    return file_path


def load_vlm_agent_files(prompt_id, role_number):
    """Every PROVEN VLM learning of one flow, each with ``action_id`` set to
    the action it was learned for.  A file that cannot prove it (see
    vlm_learning_refusal) is left out and logged once: it would otherwise
    replace a real action's steps with another run's job."""
    vlm_actions = []

    # Look for existing VLM agent files.  PROMPTS_DIR (module scope, above) is
    # the canonical store and is created at import; the CWD-relative "prompts"
    # this used to read resolved to Program Files in the frozen install, so the
    # listdir raised and the broad except below SWALLOWED it — measured live
    # 2026-09-06: 45 "Error listing files in prompts directory" and ZERO
    # "Found VLM agent recipe", i.e. this function had never once returned a
    # loaded agent on an installed build.
    try:
        for file in os.listdir(PROMPTS_DIR):
            m = _VLM_FILE_RE.match(file)
            if not m or m.group('prompt_id') != str(prompt_id) \
                    or m.group('flow') != str(role_number):
                continue
            file_path = os.path.join(PROMPTS_DIR, file)
            action_id = int(m.group('action_id'))
            try:
                recipe_data = _read_vlm_file(file_path)
            except Exception as e:
                vlm_warn_once(current_app.logger, ('unreadable', file_path),
                              '[VLM-UNPROVEN] ignoring %s: unreadable (%s)',
                              file_path, e)
                continue
            why = vlm_learning_refusal(recipe_data, prompt_id, role_number,
                                       action_id)
            if why:
                vlm_warn_once(current_app.logger, ('unproven', file_path),
                              '[VLM-UNPROVEN] ignoring %s: %s', file_path, why)
                continue
            current_app.logger.info(f"Found VLM agent recipe: {file_path}")
            recipe_data["action_id"] = action_id
            vlm_actions.append(recipe_data)
    except Exception as e:
        current_app.logger.error(f"Error listing files in prompts directory: {e}")

    return vlm_actions


def unproven_vlm_learnings(prompts_dir):
    """``{filename: reason}`` for every VLM learning in ``prompts_dir`` that
    the loader would refuse.  Read-only."""
    found = {}
    for name in sorted(os.listdir(prompts_dir)):
        m = _VLM_FILE_RE.match(name)
        if not m:
            continue
        try:
            record = _read_vlm_file(os.path.join(prompts_dir, name))
        except Exception as e:
            found[name] = 'unreadable (%s)' % e
            continue
        why = vlm_learning_refusal(record, m.group('prompt_id'),
                                   m.group('flow'), m.group('action_id'))
        if why:
            found[name] = why
    return found


def quarantine_unproven_vlm_learnings_once(prompts_dir=None):
    """Move every unproven VLM learning into ``<prompts>/VLM_QUARANTINE_DIRNAME``,
    once.  Never deletes: a moved file is the same bytes under the same name.

    The marker pattern of core.file_cache.adopt_legacy_json_once: a marker
    (VLM_QUARANTINE_MARKER, inside the quarantine dir so no ``<prompts>/*.json``
    listing mistakes it for a prompt) records what moved and why; while it
    exists nothing runs again.  A move that fails leaves it unmarked, so the
    next start retries.  Logged, never raised: it runs at boot.

    Returns 'done' (marked before), 'none' (nothing to move), 'moved',
    'partial' (some moves failed) or 'failed'.
    """
    log = logging.getLogger(__name__)
    prompts_dir = os.path.abspath(prompts_dir or PROMPTS_DIR)
    qdir = os.path.join(prompts_dir, VLM_QUARANTINE_DIRNAME)
    marker = os.path.join(qdir, VLM_QUARANTINE_MARKER)
    try:
        if os.path.exists(marker):
            return 'done'
        if not os.path.isdir(prompts_dir):
            return 'none'
        todo = unproven_vlm_learnings(prompts_dir)
        moved, failed = {}, {}
        if todo:
            os.makedirs(qdir, exist_ok=True)
        for name, why in todo.items():
            dst = os.path.join(qdir, name)
            n = 1
            while os.path.exists(dst):          # never overwrite a kept copy
                dst = os.path.join(qdir, '%s.%d' % (name, n))
                n += 1
            try:
                os.replace(os.path.join(prompts_dir, name), dst)
                moved[name] = why
            except OSError as e:
                failed[name] = str(e)
        if failed:
            log.warning('[VLM-QUARANTINE] moved %d unproven VLM learning(s) to '
                        '%s; %d could not be moved and are retried next start: '
                        '%s', len(moved), qdir, len(failed), sorted(failed))
            return 'partial'
        atomic_json_write(marker, {
            'at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'from': prompts_dir, 'moved': moved}, indent=2)
        if moved:
            log.warning('[VLM-QUARANTINE] moved %d VLM learning(s) that do not '
                        'say which action they ran under to %s (restore by '
                        'moving them back; the loader refuses them either way)',
                        len(moved), qdir)
            return 'moved'
        return 'none'
    except Exception as e:
        log.warning('[VLM-QUARANTINE] not completed for %s: %s', prompts_dir, e)
        return 'failed'


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
