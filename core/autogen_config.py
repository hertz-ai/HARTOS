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

    if node_tier in ('regional', 'central') and endpoint:
        if not model:
            _report_once('model', "%s is set but %s is empty: the configured "
                         "endpoint decides the model, HARTOS will not guess one"
                         % (ENDPOINT_VAR, MODEL_VAR))
        return 'api', {
            "model": model,
            "api_key": api_key or 'dummy',
            "base_url": endpoint,
            "price": [0.0025, 0.01],
        }

    if provider and api_key:
        if endpoint or provider == 'openai':
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
