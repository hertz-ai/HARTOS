"""
Cross-node recipe REUSE over the Google A2A surface (outbound client).

Each HART OS node already advertises its banked recipes as A2A agents
(dynamic_agent_registry + the /a2a/<id>/... routes). Until now that
surface was INBOUND-only: no repo code ever called a peer's /a2a/.
This module is the outbound half, so the federated agents work by
agent-to-agent interaction:

    discover_peer_agent()  -> find a peer agent matching a local goal
    pull_recipe()          -> fetch + bank the peer's recipe bundle
    invoke_peer_agent()    -> execute remotely via the JSON-RPC contract
    try_peer_recipe_reuse()-> daemon orchestration (pull first, invoke
                              as fallback, bounded, never raises)

Identity across nodes (the md5-locality problem):
    prompt_id is md5(goal.id) and goal.id is a per-node UUID, so the
    SAME seeded goal has DIFFERENT prompt_ids on every node. Cards and
    agent_ids therefore never match across nodes. The directory served
    by GET /a2a/agents carries the goal linkage the serving node knows
    (bootstrap_slug / goal_type / title, joined via the single-source
    dispatch.prompt_id_for_goal hash), and discovery matches on:
      1. goal_slug   (exact; fleet-stable for seeded bootstrap goals)
      2. goal_title  (normalized exact + goal_type agreement)
      3. agent name  (normalized exact, last resort)
    Matching is deliberately conservative: replaying a WRONG peer
    recipe is worse than falling through to CREATE.

Decentralization-first: peers come from the gossip-admitted social
peer store (guardrail-hash + Ed25519 gated at admission); transport is
the existing HTTP :6777 fabric via core.http_pool. NO central, NO
Redis, NO new transport.

Privacy (mirrors the compute_mesh consent posture, fail-closed):
    Only recipes that are hive work products travel: a recipe is
    advertised/exported ONLY when its prompt_id maps to a local
    autonomous AgentGoal, or the agent's prompt definition carries an
    explicit broadcast_agent opt-in. Human-created personal agents are
    NEVER served to peers without that flag. The whole feature is
    gated by HEVOLVE_A2A_PEER_REUSE (default on: LAN hive peers are
    guardrail-admitted and the payloads are hive-internal artifacts).

Banking (DRY, one writer): bundles ride the canonical recipe_sync
wire envelope (schema_version 1) and land on disk through
core.recipe_sync.write_envelope_files, the SAME hardened atomic
writer the central pull path uses. The bundle is banked verbatim
under the PEER'S advertised prompt_id naming (byte-identical,
provenance + re-advertisement) AND aliased under the LOCAL goal's
prompt_id so agent_daemon's _flow_recipe_exists classifier flips the
goal to REUSE on the next tick and the loader (cache_loaders /
agent_baseline_service) finds it where it already looks.
"""

import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from core.http_pool import pooled_get, pooled_post
from core.recipe_sync import (
    SCHEMA_VERSION, build_envelope, envelope_checksum, write_envelope_files)

logger = logging.getLogger('hevolve_social')

#: Master gate for the whole peer-reuse leg (client AND server side).
PEER_REUSE_ENV = 'HEVOLVE_A2A_PEER_REUSE'

#: Bounded timeouts (seconds). The daemon leg additionally enforces a
#: shared per-tick deadline (peer_leg_budget_s) across all goals.
_DIRECTORY_TIMEOUT_S = 3.0
_PULL_TIMEOUT_S = 5.0
_INVOKE_TIMEOUT_S = 30.0

#: Per-goal negative-result cooldown so a 30s daemon tick does not
#: hammer peers for a goal nobody has a recipe for.
_lock = threading.Lock()
_attempt_cooldown: Dict[str, float] = {}

#: Small TTL cache for the local goal-identity map (protects the DB
#: from per-request rescans when peers poll the directory).
_GOAL_MAP_TTL_S = 30.0
_goal_map_cache: Tuple[float, Dict[str, Dict[str, str]]] = (0.0, {})

#: PROACTIVE advert layer (recipe capability mesh). A peer that BANKS an
#: exportable recipe gossip-broadcasts a 'recipe_available' advert; this
#: node caches admitted-peer adverts keyed by '{semantic_class}/{slug}'
#: with a TTL, and the daemon consults the cache BEFORE the O(peers)
#: discovery sweep. Guarded by _lock (shared with _attempt_cooldown).
#: Mirrors the RALT skill ANNOUNCE-then-PULL pattern
#: (world_model_bridge.distribute_skill_packet -> discovery.py receiver).
_advert_cache: Dict[str, Dict[str, Any]] = {}


def peer_reuse_enabled() -> bool:
    """One knob for the feature. Default ON: hive peers are admitted
    through the guardrail-hash + Ed25519 gossip gate before they ever
    reach the peer store, and everything exchanged here is a
    hive-internal work product (see module docstring)."""
    return os.environ.get(PEER_REUSE_ENV, '1').strip().lower() not in (
        '0', 'false', 'no', 'off')


def peer_leg_budget_s() -> float:
    """Total time budget for the daemon's peer leg per tick."""
    try:
        return float(os.environ.get('HEVOLVE_A2A_PEER_BUDGET_S', '10'))
    except ValueError as e:
        logger.warning(f'peer_reuse: bad HEVOLVE_A2A_PEER_BUDGET_S ({e}); '
                       f'using 10s')
        return 10.0


def _cooldown_s() -> float:
    try:
        return float(os.environ.get('HEVOLVE_A2A_PEER_COOLDOWN_S', '600'))
    except ValueError as e:
        logger.warning(f'peer_reuse: bad HEVOLVE_A2A_PEER_COOLDOWN_S ({e}); '
                       f'using 600s')
        return 600.0


def _advert_ttl_s() -> float:
    """Freshness window for a cached recipe advert. A hit older than this
    is evicted and treated as a miss so the daemon falls through to the
    reactive discovery sweep rather than pulling from a stale peer."""
    try:
        return float(os.environ.get('HEVOLVE_A2A_ADVERT_TTL_S', '900'))
    except ValueError as e:
        logger.warning(f'peer_reuse: bad HEVOLVE_A2A_ADVERT_TTL_S ({e}); '
                       f'using 900s')
        return 900.0


# ─── Local identity (also consumed by the /a2a/agents route) ─────────

def local_goal_identity_by_prompt_id() -> Dict[str, Dict[str, str]]:
    """Map local prompt_id -> goal identity for every AgentGoal.

    Uses the single-source dispatch.prompt_id_for_goal hash (the same
    formula the classifier and steering bridge use), so the mapping can
    never drift from what the daemon actually dispatches. Cached for
    _GOAL_MAP_TTL_S. Fail-closed: any failure returns {} (and is
    logged), which makes nothing exportable rather than everything.
    """
    global _goal_map_cache
    now = time.monotonic()
    with _lock:
        ts, cached = _goal_map_cache
        if now - ts < _GOAL_MAP_TTL_S:
            return cached
    try:
        from integrations.social.models import db_session, AgentGoal
        from integrations.agent_engine.dispatch import prompt_id_for_goal
    except Exception as e:
        logger.info(f'peer_reuse: goal-identity imports unavailable: {e}')
        return {}
    out: Dict[str, Dict[str, str]] = {}
    try:
        with db_session(commit=False) as db:
            for g in db.query(AgentGoal).all():
                cfg = g.config_json or {}
                out[prompt_id_for_goal(str(g.id))] = {
                    'goal_id': str(g.id),
                    'goal_slug': cfg.get('bootstrap_slug') or '',
                    'goal_type': g.goal_type or '',
                    'goal_title': g.title or '',
                }
    except Exception as e:
        logger.info(f'peer_reuse: goal-identity map unavailable: {e}')
        return {}
    with _lock:
        _goal_map_cache = (now, out)
    return out


def _broadcast_opt_in(prompt_id: str,
                      prompt_def: Optional[dict] = None) -> bool:
    """Explicit share flag on a (human-created) agent's prompt
    definition. Fail-closed: unreadable/absent means NOT shared."""
    if prompt_def is None:
        try:
            from core.platform_paths import get_recipe_prompts_dir
            path = os.path.join(get_recipe_prompts_dir(),
                                f'{prompt_id}.json')
            with open(path, 'r', encoding='utf-8') as f:
                prompt_def = json.load(f)
        except FileNotFoundError:
            return False
        except Exception as e:
            logger.info(
                f'peer_reuse: broadcast check failed for {prompt_id}: {e}')
            return False
    val = prompt_def.get('broadcast_agent', False)
    if val is True:
        return True
    return str(val).strip().lower() in ('yes', 'true', '1')


def export_allowed(prompt_id) -> bool:
    """Server-side gate for serving recipe BYTES to a peer.

    Allowed only when the recipe is a hive work product (prompt_id maps
    to a local AgentGoal) or the agent opted into broadcast_agent.
    Fail-closed on every error path (see local_goal_identity_by_
    prompt_id / _broadcast_opt_in)."""
    if not peer_reuse_enabled():
        return False
    pid = str(prompt_id)
    if pid in local_goal_identity_by_prompt_id():
        return True
    return _broadcast_opt_in(pid)


def build_agent_directory() -> List[Dict[str, Any]]:
    """Directory payload for GET /a2a/agents.

    Live rescan (fresh DynamicAgentDiscovery over the canonical recipe
    dir) so recipes banked AFTER boot are advertised without a restart,
    joined with the goal-identity map so peers can match across the
    md5-local prompt_id boundary. Privacy: only exportable agents
    (goal-linked or broadcast_agent) appear; what we will not serve we
    do not advertise."""
    from .dynamic_agent_registry import DynamicAgentDiscovery
    discovery = DynamicAgentDiscovery()
    discovery.discover_all_agents()
    goal_map = local_goal_identity_by_prompt_id()
    out: List[Dict[str, Any]] = []
    for agent in discovery.get_all_agents():
        pid = str(agent.prompt_id)
        prompt_def = discovery.prompt_definitions.get(agent.prompt_id, {})
        gi = goal_map.get(pid)
        if gi is None and not _broadcast_opt_in(pid, prompt_def):
            continue
        gi = gi or {}
        out.append({
            'agent_id': agent.agent_id,
            'prompt_id': pid,
            'flow_id': agent.flow_id,
            'name': prompt_def.get('name', ''),
            'agent_name': prompt_def.get('agent_name', ''),
            'flow_name': agent.flow_name,
            'sub_goal': agent.sub_goal,
            'status': agent.status,
            'goal_slug': gi.get('goal_slug', ''),
            'goal_type': gi.get('goal_type', ''),
            'goal_title': gi.get('goal_title', ''),
        })
    return out


# ─── Peers ───────────────────────────────────────────────────────────

def admitted_peers(limit: int = 8) -> List[Dict[str, str]]:
    """Admitted hive peers from the canonical social peer store.

    Same selection filter the integrity/gradient services use: active,
    not banned, not self. Rows only enter this table through the
    gossip admission gate (guardrail_hash + Ed25519), so presence here
    IS the trust rail. Returns [] on any failure (logged)."""
    self_id = None
    try:
        from integrations.social.peer_discovery import gossip
        self_id = getattr(gossip, 'node_id', None)
    except Exception as e:
        logger.debug(f'peer_reuse: gossip node_id unavailable: {e}')
    try:
        from integrations.social.models import db_session, PeerNode
        with db_session(commit=False) as db:
            q = db.query(PeerNode).filter(
                PeerNode.status == 'active',
                PeerNode.integrity_status != 'banned',
            )
            if self_id:
                q = q.filter(PeerNode.node_id != self_id)
            rows = q.limit(limit).all()
            return [{'node_id': r.node_id, 'url': r.url}
                    for r in rows if r.url]
    except Exception as e:
        logger.info(f'peer_reuse: peer store unavailable: {e}')
        return []


# ─── Discovery ───────────────────────────────────────────────────────

def _norm(text: str) -> str:
    return ' '.join(str(text or '').split()).casefold()


# ─── Capability identity (advert keys, reuses discovery's identity) ──

def _slugify(text: str) -> str:
    """Stable, rule-based slug (NOT ML): casefold, collapse every run of
    non-[a-z0-9] to a single dash, trim, cap length. Applied to the SAME
    goal identity peer_reuse discovery already matches on (goal_slug /
    normalized goal_title), so producer and consumer key the advert cache
    identically."""
    s = re.sub(r'[^a-z0-9]+', '-', _norm(text)).strip('-')
    return s[:64]


def _semantic_class_for(goal_type: str) -> str:
    """Rule/hash-based semantic class from the goal_type / goal category
    (NOT ML). Normalizes the goal_type token; falls back to 'general' so
    the topic namespace stays partitioned even without a goal_type."""
    return _slugify(goal_type) or 'general'


def _semantic_identity(identity: Dict[str, str]) -> Tuple[str, str]:
    """(semantic_class, slug) for an advert, derived from the goal
    identity dict discovery uses. slug prefers goal_slug (bootstrap_slug),
    else a normalized goal_title; an empty slug means the recipe is not
    addressable on the mesh and the caller skips it."""
    slug = _slugify(
        identity.get('goal_slug') or identity.get('goal_title') or '')
    return _semantic_class_for(identity.get('goal_type') or ''), slug


def _match_entry(identity: Dict[str, str],
                 entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Match a local goal identity against a peer's directory.

    Only completed recipes qualify. Match order (conservative, exact):
    goal_slug, then normalized goal_title + goal_type agreement, then
    normalized card name. Flow-0 entries win ties (the classifier keys
    on the flow-0 recipe)."""
    done = [e for e in entries
            if str(e.get('status', '')).lower() in ('done', 'completed')]
    if not done:
        return None

    def _prefer_flow0(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
        for m in matches:
            if m.get('flow_id') == 0:
                return m
        return matches[0]

    slug = identity.get('goal_slug') or ''
    if slug:
        matches = [e for e in done if e.get('goal_slug') == slug]
        if matches:
            return _prefer_flow0(matches)

    title = _norm(identity.get('goal_title'))
    gtype = identity.get('goal_type') or ''
    if title:
        matches = [
            e for e in done
            if _norm(e.get('goal_title')) == title
            and (not gtype or not e.get('goal_type')
                 or e.get('goal_type') == gtype)]
        if matches:
            return _prefer_flow0(matches)
        matches = [e for e in done if _norm(e.get('name')) == title]
        if matches:
            return _prefer_flow0(matches)
    return None


def discover_peer_agent(
    identity: Dict[str, str],
    peers: Optional[List[Dict[str, str]]] = None,
    timeout: float = _DIRECTORY_TIMEOUT_S,
    deadline: Optional[float] = None,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Find an admitted peer advertising an agent for *identity*.

    Iterates the peers' GET /a2a/agents directories and matches per
    _match_entry. Returns (peer_url, entry) or None. Bounded: per-peer
    timeout + optional monotonic *deadline* across the sweep. Every
    failure is logged and skipped; never raises."""
    if peers is None:
        peers = admitted_peers()
    for peer in peers:
        if deadline is not None and time.monotonic() >= deadline:
            logger.info('peer_reuse: discovery stopped at deadline')
            return None
        url = (peer.get('url') or '').rstrip('/')
        if not url:
            continue
        try:
            resp = pooled_get(f'{url}/a2a/agents', timeout=timeout)
            if resp.status_code != 200:
                logger.info(
                    f'peer_reuse: directory {url} returned '
                    f'{resp.status_code}')
                continue
            entries = resp.json().get('agents') or []
        except Exception as e:
            logger.info(f'peer_reuse: directory fetch from {url} failed: {e}')
            continue
        entry = _match_entry(identity, entries)
        if entry:
            logger.info(
                f"peer_reuse: matched agent {entry.get('agent_id')} "
                f"on {url} (slug={identity.get('goal_slug') or '-'})")
            return url, entry
    return None


# ─── Invoke (JSON-RPC message/send, the existing contract) ──────────

def invoke_peer_agent(peer_url: str, agent_id: str, prompt: str,
                      timeout: float = _INVOKE_TIMEOUT_S) -> Optional[dict]:
    """POST the existing /a2a/<id>/jsonrpc message/send contract.

    Returns the A2A task envelope (id/contextId/state/content) or None
    on any transport / JSON-RPC failure (logged). NOTE: an envelope
    with state == 'failed' is returned as-is; callers decide."""
    url = f"{peer_url.rstrip('/')}/a2a/{agent_id}/jsonrpc"
    rpc = {
        'jsonrpc': '2.0',
        'id': uuid.uuid4().hex,
        'method': 'message/send',
        'params': {
            'message': {
                'messageId': uuid.uuid4().hex,
                'contextId': uuid.uuid4().hex,
                'parts': [{'type': 'text', 'text': prompt}],
            }
        },
    }
    try:
        resp = pooled_post(url, json=rpc, timeout=timeout)
    except Exception as e:
        logger.info(f'peer_reuse: invoke {agent_id} on {peer_url} '
                    f'failed: {e}')
        return None
    if resp.status_code != 200:
        logger.info(f'peer_reuse: invoke {agent_id} on {peer_url} '
                    f'returned {resp.status_code}')
        return None
    try:
        body = resp.json()
    except ValueError as e:
        logger.info(f'peer_reuse: invoke {agent_id} returned '
                    f'non-JSON: {e}')
        return None
    if body.get('error'):
        logger.info(f"peer_reuse: invoke {agent_id} JSON-RPC error: "
                    f"{body['error']}")
        return None
    return body.get('result')


def _result_text(result: Optional[dict]) -> str:
    """Extract the text parts from an A2A task envelope's content."""
    try:
        content = (result or {}).get('content') or {}
        parts = content.get('parts') or []
        return '\n'.join(p.get('text', '') for p in parts if p.get('text'))
    except Exception as e:
        logger.debug(f'peer_reuse: result text extraction failed: {e}')
        return ''


# ─── Pull + bank ─────────────────────────────────────────────────────

def _alias_envelope_for_local(envelope: dict, local_prompt_id: str) -> dict:
    """Re-key a peer's envelope under the LOCAL goal's prompt_id.

    Filenames get the prefix swapped; the {pid}.json prompt definition
    additionally gets its internal prompt_id field rewritten (type
    preserved) so the /chat pipeline sees a consistent identity. All
    other file contents are untouched (they do not embed prompt_id)."""
    peer_pid = str(envelope.get('prompt_id', ''))
    def_name = f'{peer_pid}.json'
    files_out: Dict[str, str] = {}
    for fname, content in (envelope.get('files') or {}).items():
        if fname == def_name:
            new_name = f'{local_prompt_id}.json'
            try:
                pdef = json.loads(content)
                old = pdef.get('prompt_id')
                if isinstance(old, int) and local_prompt_id.isdigit():
                    pdef['prompt_id'] = int(local_prompt_id)
                else:
                    pdef['prompt_id'] = local_prompt_id
                content = json.dumps(pdef, indent=1)
            except (ValueError, TypeError) as e:
                logger.warning(
                    f'peer_reuse: could not rewrite prompt_id inside '
                    f'{fname} ({e}); aliasing filename only')
        elif fname.startswith(f'{peer_pid}_'):
            new_name = local_prompt_id + fname[len(peer_pid):]
        else:
            new_name = fname
        files_out[new_name] = content
    return {
        'schema_version': envelope.get('schema_version', SCHEMA_VERSION),
        'prompt_id': local_prompt_id,
        'user_id': envelope.get('user_id', ''),
        'files': files_out,
        'checksum': envelope_checksum(files_out),
        'uploaded_at': envelope.get('uploaded_at', int(time.time())),
    }


def pull_recipe(peer_url: str, agent_id: str,
                local_prompt_id: Optional[str] = None,
                timeout: float = _PULL_TIMEOUT_S) -> bool:
    """Fetch a peer agent's recipe bundle and bank it locally.

    GET {peer}/a2a/{agent_id}/recipe (canonical recipe_sync envelope),
    verify schema + checksum, then bank byte-identical under the
    peer's advertised naming via the ONE envelope writer. When
    *local_prompt_id* differs from the peer's, additionally bank an
    alias bundle under the local naming so _flow_recipe_exists flips
    the goal to REUSE next tick. Returns True only when the local
    REUSE precondition was actually established. Never raises."""
    url = f"{peer_url.rstrip('/')}/a2a/{agent_id}/recipe"
    try:
        resp = pooled_get(url, timeout=timeout)
    except Exception as e:
        logger.info(f'peer_reuse: recipe pull from {url} failed: {e}')
        return False
    if resp.status_code != 200:
        logger.info(f'peer_reuse: recipe pull from {url} returned '
                    f'{resp.status_code}')
        return False
    try:
        envelope = resp.json()
    except ValueError as e:
        logger.info(f'peer_reuse: recipe pull from {url} returned '
                    f'non-JSON: {e}')
        return False
    if envelope.get('schema_version') != SCHEMA_VERSION:
        logger.warning(
            f'peer_reuse: schema mismatch from {url} '
            f'(peer={envelope.get("schema_version")}, '
            f'local={SCHEMA_VERSION}); skipping')
        return False
    files = envelope.get('files') or {}
    if not files:
        logger.info(f'peer_reuse: empty bundle from {url}')
        return False
    if envelope_checksum(files) != envelope.get('checksum'):
        logger.warning(
            f'peer_reuse: checksum mismatch from {url}; refusing to bank')
        return False

    try:
        from core.platform_paths import get_recipe_prompts_dir
        prompts_dir = get_recipe_prompts_dir()
    except Exception as e:
        logger.warning(f'peer_reuse: prompts-dir resolver failed: {e}; '
                       f'falling back to relative prompts/')
        prompts_dir = 'prompts'

    written = write_envelope_files(prompts_dir, envelope)
    if not written:
        logger.warning(f'peer_reuse: banking bundle from {url} wrote 0 files')
        return False
    peer_pid = str(envelope.get('prompt_id', ''))
    logger.info(
        f'peer_reuse: banked {written} file(s) from {peer_url} under '
        f'peer prompt_id={peer_pid}')

    if local_prompt_id and str(local_prompt_id) != peer_pid:
        alias = _alias_envelope_for_local(envelope, str(local_prompt_id))
        alias_written = write_envelope_files(prompts_dir, alias)
        if not alias_written:
            logger.warning(
                f'peer_reuse: local alias bank for prompt_id='
                f'{local_prompt_id} wrote 0 files; REUSE will not flip')
            return False
        logger.info(
            f'peer_reuse: aliased bundle to local prompt_id='
            f'{local_prompt_id} ({alias_written} file(s)); goal flips '
            f'to REUSE next classification')
    return True


# ─── Capability mesh: proactive advert layer (announce → cache → consume)

def announce_recipe_available(
        prompt_id, flow_id,
        identity: Optional[Dict[str, str]] = None) -> bool:
    """Gossip-broadcast a 'recipe_available' advert after an exportable
    recipe is banked (the PROACTIVE half of the capability mesh).

    Mirrors world_model_bridge.distribute_skill_packet EXACTLY: same
    gossip handle, bounded (gossip.broadcast carries its own per-peer
    timeout), every except logged, best-effort. A broadcast failure NEVER
    fails the caller. The receiver (discovery.py 'recipe_available' branch
    -> on_recipe_available_advert) caches the advert so an admitted peer
    can pull the bytes directly, skipping the O(peers) discovery sweep.

    GATE: the CALLER must check export_allowed(prompt_id) first; this
    function does not re-gate (single gate at the bank site, no parallel
    path). Returns True when a broadcast was attempted, False when the
    advert was skipped (disabled / no addressable slug) or failed."""
    if not peer_reuse_enabled():
        return False
    pid = str(prompt_id)
    if identity is None:
        identity = local_goal_identity_by_prompt_id().get(pid) or {}
    semantic_class, slug = _semantic_identity(identity)
    if not slug:
        logger.info(f'peer_reuse: no addressable slug for prompt_id={pid}; '
                    f'skipping recipe advert')
        return False

    # Local node identity (source_node / source_api_url): mirror of the
    # RALT advert. gossip holds the canonical node_id + advertised URL.
    source_node, source_api_url = '', ''
    try:
        from integrations.social.peer_discovery import gossip
        source_node = getattr(gossip, 'node_id', '') or ''
        source_api_url = (getattr(gossip, 'base_url', '') or '').rstrip('/')
    except Exception as e:
        logger.info(f'peer_reuse: gossip identity for advert unavailable: {e}')

    # Checksum over the banked bundle (canonical recipe_sync envelope) so a
    # receiver can dedup / verify. Best-effort: absence must not block the
    # advert.
    checksum = ''
    try:
        from core.platform_paths import get_recipe_prompts_dir
        envelope = build_envelope(get_recipe_prompts_dir(), pid)
        if envelope:
            checksum = envelope.get('checksum', '')
    except Exception as e:
        logger.info(f'peer_reuse: advert checksum for {pid} unavailable: {e}')

    advert = {
        'type': 'recipe_available',
        'capability': {
            'semantic_class': semantic_class,
            'slug': slug,
            'goal_type': identity.get('goal_type') or '',
            'title': identity.get('goal_title') or '',
            'agent_id': f'{pid}_{flow_id}',
            'prompt_id': pid,
            'flow_id': flow_id,
        },
        'source_node': source_node,
        'source_api_url': source_api_url,
        'checksum': checksum,
        'timestamp': time.time(),
    }
    try:
        from integrations.social.peer_discovery import gossip
        sent = gossip.broadcast(advert)
        logger.info(
            f'peer_reuse: announced recipe {semantic_class}/{slug} '
            f'(agent {pid}_{flow_id}) to {sent} peer(s)')
        return True
    except Exception as e:
        logger.info(f'peer_reuse: recipe advert broadcast failed for '
                    f'{pid}_{flow_id}: {e}')
        return False


def on_recipe_available_advert(message: dict) -> dict:
    """Receive a peer's 'recipe_available' gossip advert (INBOUND half of
    the capability mesh). Invoked by the discovery.py
    '/api/social/peers/broadcast' dispatcher.

    Trust: the sender MUST be an admitted hive peer; presence in the
    gossip-admitted PeerNode store IS the trust rail, the SAME admission
    the reactive pull path relies on. Echo-skips our own adverts. On
    success caches the advert keyed by '{semantic_class}/{slug}' with a
    TTL so the daemon can pull directly. Rate limiting is enforced
    upstream by the endpoint (_check_announce_rate), same as the RALT
    branch. Returns a structured dict; never raises."""
    if not peer_reuse_enabled():
        return {'success': False, 'reason': 'peer_reuse_disabled'}
    cap = (message or {}).get('capability') or {}
    source_node = (message or {}).get('source_node', '') or ''
    source_api_url = (
        (message or {}).get('source_api_url', '') or '').rstrip('/')
    semantic_class = cap.get('semantic_class') or ''
    slug = cap.get('slug') or ''
    agent_id = cap.get('agent_id') or ''
    if not slug or not source_api_url or not agent_id:
        return {'success': False, 'reason': 'incomplete_advert'}

    # Echo prevention: never cache our own advert.
    local_node = None
    try:
        from integrations.social.peer_discovery import gossip
        local_node = getattr(gossip, 'node_id', None)
    except Exception as e:
        logger.info(f'peer_reuse: gossip node_id for advert ingest '
                    f'unavailable: {e}')
    if local_node and source_node == local_node:
        return {'success': False, 'reason': 'echo_skip'}

    # Trust gate: sender must be an admitted peer (match on node_id first,
    # then advertised URL). Same rail the reactive pull path uses.
    peers = admitted_peers()
    trusted = any(
        (source_node and p.get('node_id') == source_node)
        or (p.get('url') or '').rstrip('/') == source_api_url
        for p in peers)
    if not trusted:
        logger.info(
            f'peer_reuse: rejected recipe advert from non-admitted peer '
            f'(node={source_node or "?"}, url={source_api_url or "?"})')
        return {'success': False, 'reason': 'peer_not_admitted'}

    key = f'{semantic_class}/{slug}'
    entry = {
        'peer_url': source_api_url,
        'agent_id': agent_id,
        'prompt_id': cap.get('prompt_id') or '',
        'flow_id': cap.get('flow_id'),
        'checksum': (message or {}).get('checksum', ''),
        'ts': time.time(),
    }
    with _lock:
        _advert_cache[key] = entry
    logger.info(
        f'peer_reuse: cached recipe advert {key} from {source_api_url} '
        f'(agent {agent_id})')
    return {'success': True, 'cached': key}


def advert_for(identity: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Fresh cached advert for a goal identity, or None on miss/stale.

    Derives the SAME '{semantic_class}/{slug}' key announce/ingest use.
    Entries older than the TTL are evicted and treated as a miss so the
    caller falls through to the reactive path."""
    semantic_class, slug = _semantic_identity(identity)
    if not slug:
        return None
    key = f'{semantic_class}/{slug}'
    now = time.time()
    with _lock:
        entry = _advert_cache.get(key)
        if not entry:
            return None
        if now - entry.get('ts', 0.0) > _advert_ttl_s():
            _advert_cache.pop(key, None)
            logger.info(f'peer_reuse: recipe advert {key} stale; evicted')
            return None
        return dict(entry)


def consume_advert(identity: Dict[str, str], local_prompt_id,
                   timeout: float = _PULL_TIMEOUT_S) -> Optional[str]:
    """Proactive consume: if an admitted peer has advertised a recipe for
    this goal identity, pull it DIRECTLY from the advertised peer, skipping
    the O(peers) discover_peer_agent sweep.

    Returns 'pulled' when the local REUSE precondition was established,
    else None so the caller falls through to the reactive
    try_peer_recipe_reuse floor (UNCHANGED). Never raises."""
    if not peer_reuse_enabled():
        return None
    advert = advert_for(identity)
    if not advert:
        return None
    peer_url = (advert.get('peer_url') or '').rstrip('/')
    agent_id = advert.get('agent_id') or ''
    if not peer_url or not agent_id:
        logger.info('peer_reuse: cached advert missing peer_url/agent_id; '
                    'falling through to reactive path')
        return None
    try:
        if pull_recipe(peer_url, agent_id,
                       local_prompt_id=str(local_prompt_id),
                       timeout=timeout):
            logger.info(
                f'peer_reuse: advert hit -> pulled {agent_id} from '
                f'{peer_url} (skipped discovery sweep)')
            return 'pulled'
    except Exception as e:
        logger.info(f'peer_reuse: advert-driven pull from {peer_url} '
                    f'failed: {e}')
    return None


# ─── Daemon orchestration ────────────────────────────────────────────

def try_peer_recipe_reuse(identity: Dict[str, str], local_prompt_id: str,
                          deadline: Optional[float] = None) -> Optional[str]:
    """One bounded peer-reuse attempt for a goal with no local recipe.

    Order: discovery -> pull-then-local-REUSE (preferred: the local
    replay feeds record_interaction and spark normally) -> remote
    invoke as a best-effort fallback within the remaining budget.

    Returns 'pulled' (recipe banked, classify as REUSE), 'invoked'
    (work happened remotely this tick, outcome recorded, skip local
    dispatch), or None (fall through to CREATE exactly as today).
    Per-goal cooldown stops tick-frequency hammering. Never raises."""
    if not peer_reuse_enabled():
        logger.debug('peer_reuse: disabled via %s', PEER_REUSE_ENV)
        return None
    key = str(local_prompt_id)
    now = time.monotonic()
    with _lock:
        # Absence means "never attempted", NOT "attempted at t=0". time.monotonic()
        # is seconds since boot on Linux, so `now - 0.0 < cooldown` is true on any
        # host that has been up for less than the cooldown window -- and the whole
        # feature silently returned None, never contacting a peer, for the first
        # 600 seconds of every boot. That is exactly the window in which a node
        # joining the hive most wants to pull a recipe, and nothing logged it:
        # the caller cannot tell "cooling down" from "no peer had it".
        last = _attempt_cooldown.get(key)
        if last is not None and now - last < _cooldown_s():
            return None
        _attempt_cooldown[key] = now

    peers = admitted_peers()
    if not peers:
        logger.debug('peer_reuse: no admitted peers')
        return None

    found = discover_peer_agent(identity, peers, deadline=deadline)
    if not found:
        return None
    peer_url, entry = found
    agent_id = entry.get('agent_id') or ''
    if not agent_id:
        logger.info(f'peer_reuse: matched entry on {peer_url} has no '
                    f'agent_id; skipping')
        return None

    if pull_recipe(peer_url, agent_id, local_prompt_id=key):
        return 'pulled'

    # Pull unavailable (e.g. peer will execute but not share bytes):
    # remote-invoke within the remaining budget and record the outcome.
    remaining = _INVOKE_TIMEOUT_S
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            logger.info('peer_reuse: no budget left for remote invoke')
            return None
    prompt_text = (identity.get('goal_description')
                   or identity.get('goal_title') or '')
    if not prompt_text:
        logger.info('peer_reuse: no prompt text for remote invoke')
        return None
    result = invoke_peer_agent(
        peer_url, agent_id, prompt_text,
        timeout=min(_INVOKE_TIMEOUT_S, remaining))
    if not result or result.get('state') != 'completed':
        if result:
            logger.info(
                f"peer_reuse: remote invoke of {agent_id} ended in "
                f"state={result.get('state')}")
        return None
    _record_remote_outcome(identity, key, prompt_text, result)
    logger.info(
        f'peer_reuse: goal handled remotely by {agent_id} on {peer_url}')
    return 'invoked'


def _record_remote_outcome(identity: Dict[str, str], local_prompt_id: str,
                           prompt_text: str, result: dict) -> None:
    """Feed a remote execution's outcome through the ONE HevolveAI
    bridge, same as dispatch.py does for local goal returns."""
    try:
        from integrations.agent_engine.world_model_bridge import (
            get_world_model_bridge)
        get_world_model_bridge().record_interaction(
            user_id=identity.get('owner_id') or 'hive_peer',
            prompt_id=local_prompt_id,
            prompt=prompt_text,
            response=_result_text(result),
            goal_id=identity.get('goal_id') or '',
        )
    except Exception as e:
        logger.info(f'peer_reuse: record_interaction for remote outcome '
                    f'failed: {e}')
