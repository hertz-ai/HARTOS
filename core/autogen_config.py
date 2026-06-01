"""Autogen LLM config_list — single source of truth.

Used by both create_recipe.py and reuse_recipe.py. Resolves the LLM
endpoint based on node tier and environment configuration:
  - regional/central with HEVOLVE_LLM_ENDPOINT_URL → cloud endpoint
  - flat with HEVOLVE_LLM_API_KEY → wizard-configured cloud
  - flat without cloud → local llama.cpp on get_local_llm_url()
"""
import os


def get_autogen_config_list() -> list:
    """Build the autogen config_list based on environment."""
    from core.port_registry import get_local_llm_url

    _node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
    _active_cloud = os.environ.get('HEVOLVE_ACTIVE_CLOUD_PROVIDER', '')

    if _node_tier in ('regional', 'central') and os.environ.get('HEVOLVE_LLM_ENDPOINT_URL'):
        cfgs = [{
            "model": os.environ.get('HEVOLVE_LLM_MODEL_NAME', 'gpt-4.1-mini'),
            "api_key": os.environ.get('HEVOLVE_LLM_API_KEY', 'dummy'),
            "base_url": os.environ['HEVOLVE_LLM_ENDPOINT_URL'],
            "price": [0.0025, 0.01]
        }]
    elif _active_cloud and os.environ.get('HEVOLVE_LLM_API_KEY'):
        _cloud_cfg = {
            "model": os.environ.get('HEVOLVE_LLM_MODEL_NAME', 'gpt-4o-mini'),
            "api_key": os.environ['HEVOLVE_LLM_API_KEY'],
            "price": [0.0025, 0.01],
        }
        if os.environ.get('HEVOLVE_LLM_ENDPOINT_URL'):
            _cloud_cfg["base_url"] = os.environ['HEVOLVE_LLM_ENDPOINT_URL']
        cfgs = [_cloud_cfg]
    else:
        cfgs = [{
            "model": os.environ.get('HEVOLVE_LOCAL_LLM_MODEL', 'local'),
            "api_key": 'dummy',
            "base_url": get_local_llm_url(),
            "price": [0, 0],
        }]

    # Attach the shared httpx client so autogen/openai reuse ONE SSL context
    # instead of reloading the whole CA bundle via ssl.create_default_context on
    # EVERY register_for_llm tool registration — the #1 GIL hog that made a bare
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
