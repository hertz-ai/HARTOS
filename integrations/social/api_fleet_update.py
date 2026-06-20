"""
Fleet OTA Update Endpoints — the SINGLE central-authority OTA backend for the
publish→pull→apply loop.

Surfaces, all on the EXISTING fleet_update_bp (no parallel blueprint):

  GET  /api/social/fleet/update-approved  — staged-rollout / canary gate (nodes poll)
  GET  /api/ota/latest?channel=<c>        — PUBLIC channel pointer the nodes poll
  POST /api/ota/publish                   — central-gated "publish to ALL my nodes"
  POST /api/ota/publish-regional          — regional-gated "publish to MY sub-fleet"
  GET  /api/ota/nodes?channel=<c>         — central-gated live per-node poll/apply view
  POST /api/ota/rollout                   — regional/central staged-rollout config

This is the ONE OTA HTTP backend (the node-side NixOS `hart-ota-check` timer,
the node-side `ota_push_listener`, and the Nunba "Update Control" panel all
bind to exactly these routes). A second, shadowed implementation of
``/api/ota/{latest,publish}`` (integrations/agent_engine/ota_api.py) was
removed during integration — Flask routed to whichever blueprint registered
first (this one, via social __init__), so that module was dead code; its two
genuinely-useful behaviours (kick the upgrade pipeline + immutable-audit the
publish) were folded INTO ``ota_publish`` below so there is one path, not two.

WHY this shape (and why nothing new is invented):

* The NixOS `hart-ota-check` timer already GETs
  ``<centralEndpoint>?channel=<channel>`` (default
  ``http://etime.hertzai.com:6777/api/ota/latest``) and expects
  ``{flake_ref, commit, channel}`` — see nixos/modules/hart-ota.nix.
  ``/api/ota/latest`` is the central half of that contract. The node, on a new
  commit, runs the existing 7-stage UpgradeOrchestrator + atomic
  ``nixos-rebuild switch`` with ``autoApply`` — so once a publish lands, every
  node auto-pulls and auto-applies on its next poll with NO USB and NO operator
  command.

* Fan-out + per-node state reuse FleetCommandService verbatim:
  ``push_broadcast('firmware_update', …)`` signs one FleetCommand per active
  PeerNode (Ed25519, central authority), delivered instantly over the
  MessageBus 'fleet.command' WAMP topic with a durable DB fallback. That signed
  command IS the trust carrier the node verifies (verify_command_signature);
  the realtime delivery to nodes is the per-command ``bus.publish('fleet.command')``
  push_command already does — the node-side receiver (core.peer_link
  .local_subscribers + crossbar_server) listens on exactly that topic. The
  node's embedded loop already drains+verifies+executes these
  (embedded_main.py) and ``_execute_firmware_update`` sets
  HEVOLVE_PENDING_UPDATE. ack_command flips the row pending→delivered
  (polled) →completed/failed (applied) — that IS the live node view.

* The channel→{flake_ref, commit} pointer is NOT a new table: the newest
  published ``firmware_update`` FleetCommand for a channel is the pointer.
  Publishing writes the commands; ``latest`` reads the newest one back.
  One writer, one source of truth.

* On publish, the CENTRAL node also kicks its OWN 7-stage UpgradeOrchestrator
  (the same entrypoint the NixOS timer uses) so the build→test→audit→benchmark
  →SIGN→CANARY→DEPLOY pipeline + canary gate + auto-rollback run unattended.
  ``start_upgrade`` refuses while a pipeline is already active, so we check
  ``get_status()`` first — NO second pipeline loop is added. Every publish is
  written to the immutable audit log (the account action).

Account gating is the canonical ``@require_central`` decorator (JWT → g.user
role check) — the steward/central-account role, NEVER the master private key.
``/api/ota/latest`` is deliberately public: fleet nodes poll it unauthenticated
via curl, exactly as the OS module does today. The pipeline's master-key SIGN
gate, canary, and atomic NixOS rollback all stay in place — publish never
force-applies past canary.
"""
import json
import logging
import time

from flask import Blueprint, request, jsonify, g

logger = logging.getLogger('hevolve_social')

fleet_update_bp = Blueprint('fleet_update', __name__)

# Fan-out targets a topology tier, not the capability tier. Central publishes
# OS updates to the flat/regional fleet it owns. '' = every active node.
_DEFAULT_TIER_FILTER = ''


@fleet_update_bp.route('/api/social/fleet/update-approved', methods=['GET'])
def check_update_approved():
    """Check if a version is approved for fleet rollout.

    Query params:
        v: version string to check (e.g., '1.2.3')

    Returns:
        {approved: bool, version: str}

    Regional hosts can override this with approval lists, staged rollout
    percentages, or canary checks. Default: approve all versions.
    """
    version = request.args.get('v', '')
    # For now: approve all versions for standalone nodes
    # Regional hosts can implement approval logic later
    return jsonify({'approved': True, 'version': version})


# ═══════════════════════════════════════════════════════════════
# Central OTA control — the "Update Control" panel's backend
# ═══════════════════════════════════════════════════════════════

def _latest_pointer_for_channel(db, channel: str):
    """Return the newest published firmware_update for a channel as a pointer.

    The pointer is derived from the FleetCommand log itself — the most recent
    ``firmware_update`` command whose params.channel matches. No separate
    pointer table (single source of truth = the command we already write on
    publish). Returns ``{flake_ref, commit, channel, published_at, ...}`` or
    None when the channel has never been published.
    """
    from .models import FleetCommand

    # Newest first; scan a bounded window so a busy fleet doesn't load the whole
    # table. params_json holds the channel — filter in Python (channel lives in
    # JSON, not a column) over the most recent firmware_update rows.
    rows = (
        db.query(FleetCommand)
        .filter(FleetCommand.cmd_type == 'firmware_update')
        .order_by(FleetCommand.created_at.desc())
        .limit(500)
        .all()
    )
    for row in rows:
        try:
            params = json.loads(row.params_json) if row.params_json else {}
        except (ValueError, TypeError):
            continue
        if (params.get('channel') or 'stable') != channel:
            continue
        return {
            'channel': channel,
            'flake_ref': params.get('update_url', ''),
            'commit': params.get('release_hash', ''),
            'published_at': row.created_at,
            'published_by': row.issued_by,
        }
    return None


def _kick_upgrade_pipeline(version: str, commit: str) -> bool:
    """Kick the CENTRAL node's own 7-stage UpgradeOrchestrator on publish.

    REUSES the singleton UpgradeOrchestrator (integrations.agent_engine
    .upgrade_orchestrator) — the SAME entrypoint the NixOS hart-ota-check timer
    drives. ``start_upgrade`` refuses while a pipeline is already active, so we
    check ``get_status()`` first and only start when idle/terminal — NO second
    pipeline loop. The existing timer / 'upgrade' goal advances it one stage per
    tick through canary→deploy; the master-key SIGN gate + canary + auto-rollback
    stay between SIGN and DEPLOY. Best-effort: a publish must still fan out even
    if the orchestrator is unavailable. Returns True iff a new pipeline started.
    """
    try:
        from integrations.agent_engine.upgrade_orchestrator import get_upgrade_orchestrator
        orch = get_upgrade_orchestrator()
        status = orch.get_status() or {}
        if status.get('stage') in ('idle', 'completed', 'rolled_back', 'failed', None):
            result = orch.start_upgrade(version or commit, commit or version) or {}
            return bool(result.get('success'))
        logger.info("OTA publish: pipeline already active (%s); pointer updated, "
                    "no second pipeline started", status.get('stage'))
        return False
    except Exception as e:
        logger.warning("OTA publish: pipeline kick skipped: %s", e)
        return False


def _audit_publish(actor: str, channel: str, commit: str, flake_ref: str,
                   node_count: int, pipeline_started: bool) -> bool:
    """Write the publish to the immutable audit log (the account action).

    REUSES security.immutable_audit_log — the same append-only log every other
    privileged account action records to. Best-effort: a publish must not fail
    because the audit log is unavailable. Returns True iff the entry was written.
    """
    try:
        from security.immutable_audit_log import get_audit_log
        get_audit_log().log_event(
            'ota_publish', actor,
            f'published {channel} -> {commit or flake_ref}',
            detail={'channel': channel, 'commit': commit, 'flake_ref': flake_ref,
                    'node_count': node_count, 'pipeline_started': pipeline_started})
        return True
    except Exception as e:
        logger.debug("OTA publish: audit log skipped: %s", e)
        return False


@fleet_update_bp.route('/api/ota/latest', methods=['GET'])
def ota_latest():
    """PUBLIC: the approved {flake_ref, commit, channel} pointer for a channel.

    This is the central half of the contract the NixOS hart-ota-check timer
    already speaks (it GETs ``<centralEndpoint>?channel=<channel>``). Nodes
    poll this unauthenticated; do NOT gate it. When a channel has never been
    published, return an empty pointer (``commit: ''``) so the node's jq
    ``.commit // ""`` check sees "nothing approved" and holds at its current
    generation instead of pulling github HEAD.

    Query params:
        channel: stable | testing | nightly (default: stable)

    Returns:
        {channel, flake_ref, commit, published_at}  (commit '' when none)
    """
    from .models import get_db

    channel = request.args.get('channel', 'stable')
    db = get_db()
    try:
        pointer = _latest_pointer_for_channel(db, channel)
    finally:
        db.close()

    if not pointer:
        # No publish yet for this channel — empty pointer = "stay put".
        return jsonify({'channel': channel, 'flake_ref': '', 'commit': '',
                        'published_at': None})
    return jsonify(pointer)


@fleet_update_bp.route('/api/ota/publish', methods=['POST'])
def ota_publish():
    """CENTRAL-ONLY: publish an update to every node this account owns.

    One operator action does the whole fan-out: sets the channel pointer
    (which ``/api/ota/latest`` now serves), signs+pushes a firmware_update
    FleetCommand to each active node, kicks the central node's own upgrade
    pipeline, and writes an immutable audit entry. After this, every node
    auto-pulls on its next poll and auto-applies (NixOS autoApply / embedded
    main loop) — no USB, no per-node command.

    Body:
        channel:   stable | testing | nightly  (default: stable)
        flake_ref: nix flake ref to switch to (e.g. github:hertz-ai/HARTOS/<sha>)
        commit:    the approved git SHA / release hash (REQUIRED)
        tier:      optional topology tier filter ('' = all owned nodes)

    Returns:
        {success, channel, commit, flake_ref, node_count, command_ids,
         pipeline_started, audited}
    """
    # Account gate — applied here (not as a static decorator) so the module
    # imports even if auth is unavailable; require_central wraps require_auth
    # which populates g.db + g.user.
    from .auth import require_central

    @require_central
    def _do_publish():
        from .fleet_command import FleetCommandService

        data = request.get_json(force=True, silent=True) or {}
        channel = (data.get('channel') or 'stable').strip()
        flake_ref = (data.get('flake_ref') or '').strip()
        commit = (data.get('commit') or '').strip()
        tier_filter = (data.get('tier') or _DEFAULT_TIER_FILTER).strip()

        if not commit:
            return jsonify({'success': False,
                            'error': 'commit (release hash) required'}), 400
        if channel not in ('stable', 'testing', 'nightly'):
            return jsonify({'success': False,
                            'error': 'channel must be stable|testing|nightly'}), 400

        # firmware_update executor reads update_url + release_hash; carry the
        # channel so /api/ota/latest can recover the pointer from the command.
        params = {
            'update_url': flake_ref,
            'release_hash': commit,
            'channel': channel,
            'published_at': time.time(),
        }

        commands = FleetCommandService.push_broadcast(
            g.db, 'firmware_update', params,
            tier_filter=tier_filter, issued_by=g.user_id,
        )
        # require_auth's wrapper commits g.db after the view returns.

        # Kick the central node's OWN 7-stage pipeline (reuse, never a second
        # loop) and audit the account action. Both are best-effort side-effects
        # — the fan-out above is the load-bearing step and has already happened.
        pipeline_started = _kick_upgrade_pipeline(commit, commit)
        audited = _audit_publish(g.user_id, channel, commit, flake_ref,
                                 len(commands), pipeline_started)

        logger.info(
            "OTA: %s published %s -> %s (nodes=%d, pipeline_started=%s)",
            g.user_id, channel, commit, len(commands), pipeline_started)

        return jsonify({
            'success': True,
            'channel': channel,
            'commit': commit,
            'flake_ref': flake_ref,
            'node_count': len(commands),
            'command_ids': [c.get('id') for c in commands],
            'pipeline_started': pipeline_started,
            'audited': audited,
        })

    return _do_publish()


@fleet_update_bp.route('/api/ota/publish-regional', methods=['POST'])
def ota_publish_regional():
    """REGIONAL-INITIATED: publish an update to THIS regional's OWN sub-fleet.

    Mirrors ``/api/ota/publish`` but gated by ``@require_regional`` and HARD
    SCOPED to the locals this regional hosts — it can NEVER publish beyond its
    region.  The member set is resolved server-side from the central-issued
    RegionAssignment table keyed on THIS node's id (``_get_self_node_id``); the
    region is NEVER taken from the request body, so a regional cannot spoof a
    different region or target central/other-region nodes.  The global
    all-nodes path stays exclusively behind ``/api/ota/publish`` +
    ``@require_central``.

    Each command is signed (Ed25519) by THIS regional's delegated cert and
    carries the same firmware_update params as the central path, so
    ``/api/ota/latest`` pointer recovery and the node executor are wire-
    unchanged.  If the regional hosts no locals, returns 200 with
    ``node_count: 0`` — it never falls through to a global broadcast.

    Body:
        channel:   stable | testing | nightly  (default: stable)
        flake_ref: nix flake ref to switch to
        commit:    the approved git SHA / release hash (REQUIRED)

    Returns:
        {success, channel, commit, flake_ref, region_node_id, member_count,
         node_count, command_ids, pipeline_started, audited}
    """
    from .auth import require_regional

    @require_regional
    def _do_publish_regional():
        from .fleet_command import FleetCommandService, _get_self_node_id
        from .hierarchy_service import HierarchyService

        data = request.get_json(force=True, silent=True) or {}
        channel = (data.get('channel') or 'stable').strip()
        flake_ref = (data.get('flake_ref') or '').strip()
        commit = (data.get('commit') or '').strip()

        if not commit:
            return jsonify({'success': False,
                            'error': 'commit (release hash) required'}), 400
        if channel not in ('stable', 'testing', 'nightly'):
            return jsonify({'success': False,
                            'error': 'channel must be stable|testing|nightly'}), 400

        # SUB-FLEET SCOPE — region derived from THIS node, never the request.
        self_node_id = _get_self_node_id()
        members = HierarchyService.region_member_node_ids(g.db, self_node_id)

        params = {
            'update_url': flake_ref,
            'release_hash': commit,
            'channel': channel,
            'published_at': time.time(),
        }

        # Scoped fan-out: the SAME signed-command path as central, but the
        # node_ids allowlist hard-caps it to this region's members. An empty
        # member set fans out to nobody (push_broadcast returns []), never to
        # the whole fleet.
        commands = FleetCommandService.push_broadcast(
            g.db, 'firmware_update', params,
            issued_by=g.user_id, node_ids=members,
        )

        # A regional publish does NOT kick the central node's pipeline (that is
        # the central account's job). It IS audited like any privileged action.
        audited = _audit_publish(g.user_id, channel, commit, flake_ref,
                                 len(commands), False)

        logger.info(
            "OTA-regional: %s published %s -> %s (region=%s, members=%d, nodes=%d)",
            g.user_id, channel, commit, self_node_id[:8] if self_node_id else '?',
            len(members), len(commands))

        return jsonify({
            'success': True,
            'channel': channel,
            'commit': commit,
            'flake_ref': flake_ref,
            'region_node_id': self_node_id,
            'member_count': len(members),
            'node_count': len(commands),
            'command_ids': [c.get('id') for c in commands],
            'pipeline_started': False,
            'audited': audited,
        })

    return _do_publish_regional()


@fleet_update_bp.route('/api/ota/nodes', methods=['GET'])
def ota_nodes():
    """CENTRAL-ONLY: live view of which nodes have polled / applied an update.

    Joins each active PeerNode with its most recent firmware_update
    FleetCommand and maps the command status to a rollout phase:

        (no command)         → idle
        pending              → queued   (not yet polled)
        delivered            → polled    (node fetched it, applying)
        completed            → applied
        failed / rejected    → failed

    Query params:
        channel: filter to one channel's rollout (default: all channels)

    Returns:
        {nodes: [{node_id, name, tier, status, version, target_commit,
                  rollout, result_message, polled_at, applied_at}], counts}
    """
    from .auth import require_central

    @require_central
    def _do_nodes():
        from .models import PeerNode, FleetCommand

        channel = request.args.get('channel', '')

        peers = g.db.query(PeerNode).all()

        # Newest firmware_update per node — one bounded scan, indexed by node.
        latest_cmd = {}
        cmd_rows = (
            g.db.query(FleetCommand)
            .filter(FleetCommand.cmd_type == 'firmware_update')
            .order_by(FleetCommand.created_at.desc())
            .limit(2000)
            .all()
        )
        for row in cmd_rows:
            if row.target_node_id in latest_cmd:
                continue  # already have the newest for this node
            if channel:
                try:
                    params = json.loads(row.params_json) if row.params_json else {}
                except (ValueError, TypeError):
                    params = {}
                if (params.get('channel') or 'stable') != channel:
                    continue
            latest_cmd[row.target_node_id] = row

        _ROLLOUT = {
            'pending': 'queued', 'delivered': 'polled',
            'completed': 'applied', 'failed': 'failed', 'rejected': 'failed',
        }
        counts = {'idle': 0, 'queued': 0, 'polled': 0, 'applied': 0, 'failed': 0}

        nodes = []
        for peer in peers:
            cmd = latest_cmd.get(peer.node_id)
            rollout = 'idle'
            target_commit = ''
            result_message = ''
            polled_at = None
            applied_at = None
            if cmd is not None:
                rollout = _ROLLOUT.get(cmd.status, 'queued')
                try:
                    cparams = json.loads(cmd.params_json) if cmd.params_json else {}
                except (ValueError, TypeError):
                    cparams = {}
                target_commit = cparams.get('release_hash', '')
                result_message = cmd.result_message or ''
                polled_at = cmd.delivered_at
                applied_at = cmd.completed_at
            counts[rollout] = counts.get(rollout, 0) + 1
            nodes.append({
                'node_id': peer.node_id,
                'name': peer.name,
                'hart_tag': getattr(peer, 'hart_tag', None) or peer.name,
                'tier': peer.tier,
                'status': peer.status,
                'version': peer.version,
                'target_commit': target_commit,
                'rollout': rollout,
                'result_message': result_message,
                'polled_at': polled_at,
                'applied_at': applied_at,
            })

        return jsonify({'nodes': nodes, 'counts': counts})

    return _do_nodes()
