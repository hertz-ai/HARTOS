"""Hive census -- what the network looks like from this node, with its sample.

This is the projection half of "central is a collection and projection point,
not an authority". `FederatedAggregator.receive_peer_delta()` already verifies
every delta it stores (version, freshness, guardrail hash, Ed25519, HMAC, origin
attestation). `hive_census()` counts what was accepted. This blueprint serves
that count and nothing else: it computes no figure of its own, so a node running
it cannot inflate the network's numbers by serving them.

The response always carries `nodes_reporting`, and the per-node figures sit
beside the totals so anyone holding the same deltas can recompute the aggregate
and check it was not invented here. A dashboard that renders the totals without
the denominator is publishing a statistic with no sample size, which is exactly
the defect OPEN_PROBLEMS.md problem 1 describes.

Read-only and unauthenticated by design. Everything here is already broadcast
between peers, and a hive that asks you to trust its own health metrics has
missed the point. Per-node entries are keyed by node id, which is public in the
federation; no user data, no endpoints, no keys.
"""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify

logger = logging.getLogger('hevolve_social')

# Mounted under /api/social because that is the only prefix Kong routes to this
# backend. /api/hive/* returns 404 at the gateway, including the older contest
# endpoints, so anything served there is unreachable from a browser. Same
# reasoning as the pages API: pick the route that already works rather than
# asking for a gateway change that has to be remembered on every redeploy.
hive_census_bp = Blueprint(
    'hive_census', __name__, url_prefix='/api/social/hive-census',
)


def _aggregator():
    """The live aggregator, or None if federation is not running here.

    Imported lazily: a node with federation disabled should serve an honest
    "not federating" rather than fail to start the whole social app.
    """
    try:
        from integrations.agent_engine.federated_aggregator import (
            get_federated_aggregator,
        )
        return get_federated_aggregator()
    except Exception as e:
        logger.debug('hive census: aggregator unavailable (%s)', e)
        return None


@hive_census_bp.get('')
@hive_census_bp.get('/')
def get_census():
    """Hive-wide learning figures, with the sample they came from.

    Distinguishes three states a caller must not conflate:

      not_federating  -- no aggregator on this node
      no_peers        -- federating, nobody has reported yet
      ok              -- figures, with nodes_reporting alongside

    A caller rendering 0.0 for the first two would show a healthy hive as a
    dead one. `/v1/hivemind/intelligence` in the learning core degrades the
    same way, deliberately.
    """
    agg = _aggregator()
    if agg is None:
        return jsonify({
            'status': 'not_federating',
            'reason': 'no federated aggregator on this node',
            'nodes_reporting': 0,
        })

    try:
        census = agg.hive_census()
    except Exception as e:
        logger.exception('hive census failed')
        return jsonify({'status': 'error', 'reason': str(e)}), 500

    if not census.get('nodes_reporting'):
        return jsonify({
            'status': 'no_peers',
            'reason': 'federating, but no peer deltas received yet',
            **census,
        })

    return jsonify({'status': 'ok', **census})
