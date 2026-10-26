"""The ONE configured LLM: single source of truth for every LLM caller.

Nunba's key vault exports exactly three variables for the LLM the user
configured (desktop/ai_key_vault.py, export_to_env):

    HEVOLVE_LLM_ENDPOINT_URL   OpenAI-compatible base URL (".../v1")
    HEVOLVE_LLM_MODEL_NAME     the model the user picked
    HEVOLVE_LLM_API_KEY        its credential

Central's .env carries the same three.  Everything in HARTOS that needs an
LLM (autogen recipes, the langchain wrapper, the raw chat/completions posts
in hart_intelligence_entry, the speculative draft classifier, the model
registry, the Spark budget gate) derives its endpoint, model and auth from
here; nothing picks a model name or a port of its own (#69: central ran the
model a deploy script wrote into .env and half the callers ignored even that).

Two kinds of backend, nothing in between:
  api    the configured endpoint.  ONE model serves every role: draft = main
         = fast = expert (owner design: a node with no VRAM and an API key
         uses the single API for every LLM call).
  local  the node's own llama-server, via core.port_registry (flat nodes and
         regional nodes with no API key).  Draft is the local draft server
         when one listens, else the main one (get_local_draft_url).
"""
import logging
import os

log = logging.getLogger(__name__)

ENDPOINT_VAR = 'HEVOLVE_LLM_ENDPOINT_URL'
MODEL_VAR = 'HEVOLVE_LLM_MODEL_NAME'
API_KEY_VAR = 'HEVOLVE_LLM_API_KEY'

# api_key values that carry no credential: the openai SDK insists on a
# non-empty string and llama-server ignores it, so autogen gets 'dummy'
# while the raw-HTTP callers send no Authorization header at all.
PLACEHOLDER_KEYS = ('', 'dummy')

# The openai SDK's own default base, used only when the vault exported no
# endpoint for provider 'openai' (its CLOUD_PROVIDERS row has env_base_url
# None): the raw-HTTP callers then post where autogen's client already does,
# so the two never disagree.  Every other provider must export its
# OpenAI-compatible endpoint to be the single source; see resolve_llm_backend.
OPENAI_SDK_DEFAULT_BASE = 'https://api.openai.com/v1'

_reported = set()


def _report_once(key, msg):
    if key not in _reported:
        _reported.add(key)
        log.error(msg)


def configured_api_endpoint():
    """The endpoint this node's LLM calls go to when it runs on an API, or ''
    when it runs its own llama-server.

    The ONE answer to "local or api", read from the environment alone: no
    probe, no entry, no report.  resolve_llm_backend builds its entry on it;
    core.constants.canonical_served_by asks it where a reply nobody tagged
    was written (a node on an API sends every prompt off the box, and a
    reply from it is not on-device).
    """
    endpoint = os.environ.get(ENDPOINT_VAR, '')
    if os.environ.get('HEVOLVE_NODE_TIER', 'flat') in ('regional', 'central') and endpoint:
        return endpoint
    provider = os.environ.get('HEVOLVE_ACTIVE_CLOUD_PROVIDER', '')
    if provider and os.environ.get(API_KEY_VAR, '') and (endpoint or provider == 'openai'):
        return endpoint or OPENAI_SDK_DEFAULT_BASE
    return ''


def llm_stays_on_premises():
    """True when this node's own LLM runs on this machine or this LAN: its own
    llama-server, or an API endpoint whose host is loopback or a non-global
    address (a LAN box).  A public endpoint (openrouter.ai, api.openai.com)
    is not, and neither is a host NAME other than localhost: it is not
    provably local, and saying 'on-device' about bytes that left is the
    wrong way to be wrong.  Pure: no probe."""
    api = configured_api_endpoint()
    if not api:
        return True
    from urllib.parse import urlparse
    import ipaddress
    host = (urlparse(api).hostname or '').lower()
    if host == 'localhost':
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def resolve_llm_backend():
    """Return ``(kind, entry)``.

    ``kind`` is ``'api'`` or ``'local'``; ``entry`` is a fresh autogen
    config dict (``model``, ``api_key``, ``price``, ``base_url`` when known,
    ``max_retries`` for local).  There are no default model names: with an
    endpoint configured the model is HEVOLVE_LLM_MODEL_NAME and an empty
    name is reported as the configuration error it is, never papered over
    with a guess (this is a plain return so create_recipe can still import).
    """
    from core.port_registry import get_local_llm_url

    node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
    provider = os.environ.get('HEVOLVE_ACTIVE_CLOUD_PROVIDER', '')
    endpoint = os.environ.get(ENDPOINT_VAR, '')
    api_key = os.environ.get(API_KEY_VAR, '')
    model = os.environ.get(MODEL_VAR, '')
    # The kind is configured_api_endpoint's answer; the branches below only
    # build the entry for it.
    api_endpoint = configured_api_endpoint()

    if api_endpoint and node_tier in ('regional', 'central') and endpoint:
        if not model:
            _report_once('model', "%s is set but %s is empty: the configured "
                         "endpoint decides the model, HARTOS will not guess one"
                         % (ENDPOINT_VAR, MODEL_VAR))
        entry = {
            "model": model,
            "api_key": api_key or 'dummy',
            "base_url": endpoint,
            "price": [0.0025, 0.01],
        }
        # Thinking off for agent calls to the configured endpoint.  Measured on
        # central 2026-09-13 (task #93) with a real agent turn, 895 prompt
        # tokens: thinking on spent 5,685 completion tokens and 47 s (24.7k
        # chars of reasoning) before answering, and under any token cap the
        # reasoning used the whole budget and the reply came back empty
        # (finish=length, content ''), which is what StatusVerifier's empty
        # replies were.  Thinking off answered the same turn in 196 tokens and
        # 3.2 s.  Local llama-server already runs this way (Nunba sets it at
        # spawn); LLM_THINKING_OFF_KWARGS travels with the request, and autogen
        # hands extra_body to the openai client's create().  OpenAI and Azure
        # OpenAI reject an unknown request field, so they are left alone.
        # OpenRouter takes the switch in its own `reasoning` field.
        from urllib.parse import urlparse
        host = (urlparse(endpoint).hostname or '').lower()
        if host == 'openrouter.ai':
            from core.constants import LLM_THINKING_OFF_REASONING
            entry["extra_body"] = {
                "reasoning": dict(LLM_THINKING_OFF_REASONING)}
        elif host != 'api.openai.com' and not host.endswith('.openai.azure.com'):
            from core.constants import LLM_THINKING_OFF_KWARGS
            entry["extra_body"] = {
                "chat_template_kwargs": dict(LLM_THINKING_OFF_KWARGS)}
        return 'api', entry

    if api_endpoint:
        if not model:
            _report_once('model', "provider %s is active but %s is empty: "
                         "HARTOS will not guess a model" % (provider, MODEL_VAR))
        entry = {
            "model": model,
            "api_key": api_key,
            "price": [0.0025, 0.01],
        }
        if endpoint:
            entry["base_url"] = endpoint
        return 'api', entry

    if provider and api_key:
        # anthropic / google_gemini / groq with no endpoint exported: only the
        # vendor SDK ladder in get_llm() can reach them.  autogen and the raw
        # callers speak OpenAI-compatible chat/completions only, and posting
        # this vendor's key to api.openai.com (what an entry without base_url
        # would do) is a guaranteed 401 with the credential on the wrong wire.
        _report_once('endpoint', "provider %s is active but exported no %s; "
                     "autogen and the chat/completions callers run local until "
                     "the vault exports its OpenAI-compatible endpoint"
                     % (provider, ENDPOINT_VAR))

    return 'local', {
        "model": os.environ.get('HEVOLVE_LOCAL_LLM_MODEL', 'local'),
        "api_key": 'dummy',
        "base_url": get_local_llm_url(),
        "price": [0, 0],
        # Local llama-server runs ONE slot (--parallel 1) on 2 cores. The
        # openai SDK's default max_retries=2 is catastrophic there, not
        # helpful: a "failure" is contention/slowness, never a transient
        # network blip, so a retry cannot succeed where the first attempt is
        # still working: it only RESUBMITS the same multi-KB prompt into the
        # busy slot, forcing a full kv-cache recompute and doubling the load
        # on the one slot. Measured on .69 (image 94c0fd9): a cold recipe
        # CREATE stalled at gather turn 6/12 for 10 min with repeated
        # "openai._base_client: Retrying request" and only 1 completion
        # served; the retries WERE the stall. Zero retries lets a slow-but-
        # progressing generation finish (the shared http_client already
        # grants a 600s read budget) and surfaces a real failure cleanly.
        # This also just extends the house policy already stated for the
        # requests path in core.http_pool ("localhost: 0 retries ... fail
        # instantly, not block") to the openai/httpx path, which was the one
        # local caller it never reached. autogen 0.2.35 routes max_retries to
        # OpenAI.__init__ (a keyword-only ctor arg on every openai>=1.x), so
        # it lands on the client, not on .create(). The api kind keeps the
        # default retries: a real cloud 429/5xx genuinely is transient.
        "max_retries": 0,
    }


def llm_http_target(draft=False):
    """``(url, headers, model)`` for the raw chat/completions callers.

    The same decision as the autogen entry, so a recipe agent and a direct
    POST never talk to different models.  ``draft=True`` asks for the draft
    slot: on the api kind the main model serves as draft too; locally it is
    the draft server when one listens, else the main one.
    """
    kind, entry = resolve_llm_backend()
    headers = {}
    if kind == 'api':
        base = entry.get('base_url') or OPENAI_SDK_DEFAULT_BASE
        if entry['api_key'] not in PLACEHOLDER_KEYS:
            headers['Authorization'] = 'Bearer ' + entry['api_key']
    elif draft:
        from core.port_registry import get_local_draft_url
        base = get_local_draft_url()
    else:
        base = entry['base_url']
    return base.rstrip('/') + '/chat/completions', headers, entry['model']


def llm_target(entry) -> tuple:
    """(base_url, model) of a config_list entry, to tell two backends apart.
    A trailing /v1 is dropped: the same endpoint is written both ways.  The
    one "is this the same LLM?" rule for the expert: the model registry's
    offer (ensure_configured_expert_registered) and the wire hook's Claude day
    cap (core.llm_outbound_logger._holds_the_expert) both read it, so they
    never disagree on whether the expert is the node's own model."""
    entry = entry or {}
    url = str(entry.get('base_url') or '').rstrip('/')
    if url.endswith('/v1'):
        url = url[:-3]
    return (url, str(entry.get('model') or ''))


def own_llm_target() -> tuple:
    """(base_url, model) of the LLM this node's own turns run on.

    Read from resolve_llm_backend(), whose entry also carries the API key, so
    only these two fields leave this function and nothing here is logged.
    """
    try:
        _kind, entry = resolve_llm_backend()
    except Exception:
        return ('', '')
    return llm_target(entry)


def _same_endpoint(a: dict, b: dict) -> bool:
    """Two config entries that would dial the same model at the same place."""
    def _norm(e):
        return ((e or {}).get('base_url') or '').rstrip('/'), (e or {}).get('model')
    return _norm(a) == _norm(b)


def with_local_fallback(config_list) -> list:
    """``config_list`` plus this node's own backend as the TERMINAL entry.

    autogen's ``OpenAIWrapper.create`` walks config_list IN ORDER and re-raises
    only when the LAST client fails (autogen/oai/client.py:687 ``last =
    len(self._clients) - 1``; :783 ``except APIError ... if i == last: raise``).
    A second entry therefore IS the engine's own fallback ladder, and it fires
    on exactly the exception a failing tier raises — no second dispatch path is
    needed to degrade one.

    The dispatcher hands the inner /chat a ONE-entry list naming the tier it
    selected (``ModelBackend.to_config_list`` returns ``[entry]``;
    speculative_dispatcher:1721, dispatch.py:904), and chat() stores it as the
    per-request override every autogen agent then runs on.  With one entry
    there is nothing to fall to.  Measured live 2026-09-06: the claude-code
    EXPERT endpoint answered 503 — both "claude-code at capacity" (its own
    BoundedSemaphore, i.e. ordinary concurrency) and "claude not on PATH" — and
    every reuse turn died on openai.InternalServerError, which
    reuse_recipe.get_agent_response swallows.  claude_code_endpoint's
    ``_FAIL_STATUS`` states the intended contract verbatim ("a lapsed
    subscription must not error the OS; it degrades to local", "caller falls
    back to local"); nothing implemented it for the inner group chat.  This is
    that fallback, built from the engine's existing mechanism.

    Routing still belongs to the dispatcher: the selected tier stays FIRST and
    is always tried first, so this never hardwires a tier — it only gives the
    chosen one somewhere to land.  An override that already ends at this node's
    backend gets no duplicate: re-dialling the endpoint that just failed is a
    retry, not a fallback.  A falsy override stays falsy, because every consumer
    spells its default as ``override or config_list``.
    """
    if not config_list:
        return config_list
    entries = list(config_list)
    for own in get_autogen_config_list():
        if not any(_same_endpoint(own, e) for e in entries):
            entries.append(own)
    return entries


def get_autogen_config_list() -> list:
    """The autogen ``config_list``: one entry, the configured backend."""
    _kind, entry = resolve_llm_backend()
    cfgs = [entry]

    # Attach the shared httpx client so autogen/openai reuse ONE SSL context
    # instead of reloading the whole CA bundle via ssl.create_default_context on
    # EVERY register_for_llm tool registration, the #1 GIL hog that made a bare
    # 'hi' take minutes (py-spy 2026-06-01).  See core.http_pool.
    # get_llm_http_client.  Best-effort: never block agent construction on the
    # optimisation, and use setdefault so an explicit per-config client wins.
    try:
        from core.http_pool import get_llm_http_client
        _client = get_llm_http_client()
        for _c in cfgs:
            _c.setdefault("http_client", _client)
    except Exception:
        pass
    return cfgs


# ─── The configured EXPERT (owner policy 2026-10-12) ──────────────────────
# A second configured trio, for the one model the owner wants spent only where
# it drives revenue (Claude on central).  It is NOT a vendor side door (#69):
# nothing here reads ANTHROPIC_API_KEY or any vendor variable; the owner names
# the endpoint and the model explicitly, and on central the endpoint is the
# same gateway the node's own LLM uses (Kong -> gpt3), so one wire, one set of
# fuses.  The registry offers it as the EXPERT tier to the lanes in
# HEVOLVE_EXPERT_LANES only (default: revenue); every other lane never sees it.
EXPERT_ENDPOINT_VAR = 'HEVOLVE_EXPERT_LLM_ENDPOINT_URL'
EXPERT_MODEL_VAR = 'HEVOLVE_EXPERT_LLM_MODEL_NAME'
EXPERT_API_KEY_VAR = 'HEVOLVE_EXPERT_LLM_API_KEY'
EXPERT_LANES_VAR = 'HEVOLVE_EXPERT_LANES'
DEFAULT_EXPERT_LANES = 'revenue'
# Dollars per 1k tokens (input, output) by model family, list prices.  The
# ONE price table: the expert entry's autogen ``price`` below and the
# budget gate's dollar meter (integrations.agent_engine.budget_gate) both
# read it, so the ledger and the registry never disagree on what Claude costs.
EXPERT_USD_PER_1K = {
    'claude': (0.003, 0.015),     # Sonnet-class
}
OTHER_MODEL_FAMILY = 'other'


def model_family(model_name) -> str:
    """The priced family a model name belongs to ('claude'), else 'other'.
    Substring match, the ONE rule the dollar meter, the day cap and the wire
    header X-HARTOS-Model-Family share."""
    lower = str(model_name or '').lower()
    for family in EXPERT_USD_PER_1K:
        if family in lower:
            return family
    return OTHER_MODEL_FAMILY


def family_usd_per_1k(family):
    """(input, output) dollars per 1k tokens for a priced family, with the
    owner's pin HEVOLVE_<FAMILY>_USD_PER_1K_IN / _OUT applied; None for an
    unpriced one.  The ONE place a family's price is read: the expert entry's
    autogen ``price`` below and the budget gate's meter both come here, so an
    owner's pin reaches the ledger and the registry alike (review of
    8947e2753: a pin used to move one and not the other)."""
    base = EXPERT_USD_PER_1K.get(family)
    if base is None:
        return None

    def _pinned(name, default):
        try:
            return float(os.environ.get(name, '') or default)
        except (TypeError, ValueError):
            return default
    upper = str(family).upper()
    return (_pinned('HEVOLVE_%s_USD_PER_1K_IN' % upper, base[0]),
            _pinned('HEVOLVE_%s_USD_PER_1K_OUT' % upper, base[1]))


def resolve_expert_backend():
    """A fresh autogen entry for the configured expert, or None when the owner
    configured none.  Endpoint AND model are required; a half-set pair is
    reported once and treated as none (nothing is guessed, as for the main
    trio).  No thinking-off kwargs: this entry is for a hosted frontier model,
    and an unknown request field is how a provider says 400."""
    endpoint = os.environ.get(EXPERT_ENDPOINT_VAR, '').strip()
    model = os.environ.get(EXPERT_MODEL_VAR, '').strip()
    if not endpoint and not model:
        return None
    # Topology agnostic on purpose: no node-tier branch here or anywhere this
    # tier is read.  The owner wants it on central only, and central is the
    # only node whose configuration carries the trio; a desktop or regional
    # node without it runs exactly as before.  Configuration, never a tier
    # check, is what decides (owner ruling 2026-10-12).
    if not endpoint or not model:
        _report_once('expert', "%s and %s must both be set for the expert "
                     "tier; one alone configures nothing"
                     % (EXPERT_ENDPOINT_VAR, EXPERT_MODEL_VAR))
        return None
    entry = {
        "model": model,
        "api_key": os.environ.get(EXPERT_API_KEY_VAR, '') or 'dummy',
        "base_url": endpoint,
    }
    # autogen's ``price`` is optional: a priced family gets the table's (pinned)
    # price, an unpriced one gets no invented number.
    price = family_usd_per_1k(model_family(model))
    if price:
        entry["price"] = list(price)
    return entry


def expert_lanes() -> frozenset:
    """The lanes the configured expert is offered to."""
    raw = os.environ.get(EXPERT_LANES_VAR, '') or DEFAULT_EXPERT_LANES
    return frozenset(p.strip() for p in raw.split(',') if p.strip())
