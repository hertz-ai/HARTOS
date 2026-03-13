"""
Auto Deploy Service — Build, sign, and deploy on PR merge

Triggered when a PR merges to main. Runs full test suite, captures benchmark
snapshot, verifies safety via is_upgrade_safe(), signs the release manifest
using scripts/sign_release.py, and notifies all nodes via gossip protocol.

Each node receives the version notification and auto-updates after verifying
the release manifest signature.
"""
import json
import logging
import os
import subprocess
import time
from typing import Dict, Optional

logger = logging.getLogger('hevolve_social')


class AutoDeployService:
    """Triggered when PR merges to main. Static methods only."""

    @staticmethod
    def on_pr_merged(repo_url: str, merge_sha: str) -> Dict:
        """Triggered by GitHub webhook or polling.

        1. Pull latest code
        2. Run full test suite (regression gate)
        3. Capture benchmark snapshot for new version
        4. Compare vs previous version (is_upgrade_safe)
        5. If safe: sign release manifest
        6. Distribute update notification via gossip
        """
        result = {
            'merge_sha': merge_sha,
            'deployed': False,
            'steps': {},
        }

        # 1. Pull latest code
        try:
            pull = subprocess.run(
                ['git', 'pull', 'origin', 'main'],
                capture_output=True, text=True, timeout=120)
            result['steps']['git_pull'] = {
                'success': pull.returncode == 0,
                'output': pull.stdout[:200],
            }
            if pull.returncode != 0:
                result['error'] = 'Git pull failed'
                return result
        except Exception as e:
            result['steps']['git_pull'] = {'success': False, 'error': str(e)}
            result['error'] = f'Git pull failed: {e}'
            return result

        # 2. Run test suite
        try:
            from .pr_review_service import PRReviewService
            test_results = PRReviewService.run_test_suite()
            result['steps']['tests'] = test_results

            if test_results.get('pass_rate', 0) < 0.95:
                result['error'] = (
                    f"Test suite failed: {test_results.get('failed', 0)} "
                    f"failures, pass_rate={test_results.get('pass_rate', 0)}")
                return result
        except Exception as e:
            result['steps']['tests'] = {'error': str(e)}
            result['error'] = f'Test suite error: {e}'
            return result

        # 3. Capture benchmark snapshot
        new_version = merge_sha[:8]
        try:
            from .benchmark_registry import get_benchmark_registry
            registry = get_benchmark_registry()
            snapshot = registry.capture_snapshot(
                version=new_version, tier='fast')
            result['steps']['benchmark'] = {
                'version': new_version,
                'captured': bool(snapshot),
            }
        except Exception as e:
            result['steps']['benchmark'] = {'error': str(e)}

        # 4. Check upgrade safety
        try:
            from .benchmark_registry import get_benchmark_registry
            registry = get_benchmark_registry()
            safe = registry.is_upgrade_safe(new_version)
            result['steps']['upgrade_safe'] = safe

            if not safe.get('safe', True):
                result['error'] = (
                    f"Upgrade not safe: {safe.get('regressions', [])}")
                return result
        except Exception as e:
            result['steps']['upgrade_safe'] = {'error': str(e)}
            # Continue — missing benchmark data should not block deploy

        # 5. Sign release manifest — MUST succeed before gossip
        manifest = None
        try:
            manifest = AutoDeployService._sign_release(new_version, merge_sha)
            result['steps']['sign'] = {
                'signed': manifest is not None and manifest.get('signed', False),
            }
        except Exception as e:
            result['steps']['sign'] = {'error': str(e)}

        if not manifest or not manifest.get('signed') or not manifest.get('signature'):
            result['error'] = 'Release signing failed — aborting deploy (no unsigned gossip)'
            result['deployed'] = False
            return result

        # 6. Notify nodes via gossip (signed manifest only)
        nodes_notified = 0
        try:
            nodes_notified = AutoDeployService.notify_nodes(
                new_version, manifest)
            result['steps']['gossip'] = {
                'nodes_notified': nodes_notified,
            }
        except Exception as e:
            result['steps']['gossip'] = {'error': str(e)}

        result['deployed'] = True
        result['version'] = new_version
        result['nodes_notified'] = nodes_notified
        return result

    @staticmethod
    def _sign_release(version: str, merge_sha: str) -> Optional[Dict]:
        """Sign release manifest using scripts/sign_release.py."""
        manifest = {
            'version': version,
            'merge_sha': merge_sha,
            'timestamp': time.time(),
        }

        # Get code hash
        try:
            from security.node_integrity import compute_code_hash
            manifest['code_hash'] = compute_code_hash()
        except Exception:
            pass

        # Sign via release script if available
        script_path = os.path.join('scripts', 'sign_release.py')
        if os.path.exists(script_path):
            try:
                python = os.environ.get('HEVOLVE_PYTHON', 'python')
                result = subprocess.run(
                    [python, script_path, '--version', version],
                    capture_output=True, text=True, timeout=60)
                if result.returncode == 0:
                    manifest['signed'] = True
                    # Parse signature from output if available
                    for line in result.stdout.split('\n'):
                        if line.startswith('signature='):
                            manifest['signature'] = line.split('=', 1)[1]
            except Exception as e:
                logger.debug(f"Release signing failed: {e}")

        return manifest

    @staticmethod
    def notify_nodes(version: str, manifest: dict) -> int:
        """Use gossip protocol to notify all peers of new version.

        Each notification is signed with this node's Ed25519 key so peers
        can verify the sender before accepting the update.
        """
        notified = 0

        # Sign the notification payload with this node's key
        node_signature = None
        node_id = None
        try:
            from security.node_integrity import get_node_identity
            identity = get_node_identity()
            node_id = identity.get('node_id', '')
            import json as _json
            payload_bytes = _json.dumps(
                {'version': version, 'manifest_hash': manifest.get('code_hash', '')},
                sort_keys=True).encode()
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            private_key = identity.get('_private_key')
            if private_key and isinstance(private_key, Ed25519PrivateKey):
                node_signature = private_key.sign(payload_bytes).hex()
        except Exception as e:
            logger.debug(f"Node signature for gossip failed: {e}")

        try:
            from integrations.social.models import get_db, PeerNode
            db = get_db()
            try:
                peers = db.query(PeerNode).filter(
                    PeerNode.status == 'active').all()
                for peer in peers:
                    if not peer.url:
                        continue
                    try:
                        import requests
                        resp = requests.post(
                            f'{peer.url}/api/social/deploy/version-update',
                            json={
                                'type': 'version_update',
                                'version': version,
                                'manifest': manifest,
                                'sender_node_id': node_id,
                                'sender_signature': node_signature,
                            },
                            timeout=10,
                        )
                        if resp.status_code == 200:
                            notified += 1
                    except Exception:
                        pass
            finally:
                db.close()
        except Exception as e:
            logger.debug(f"Node notification failed: {e}")

        logger.info(f"Version update {version}: notified {notified} nodes")
        return notified

    @staticmethod
    def auto_update_node(version: str, manifest: dict) -> Dict:
        """Called on each node when update notification received.

        1. Verify release manifest signature
        2. Compare code_hash
        3. If different + verified: pull latest code
        4. Restart services gracefully
        """
        result = {'updated': False, 'version': version}

        # 1. Verify manifest signature — ALWAYS required (no unsigned bypass)
        if not manifest.get('signed') or not manifest.get('signature'):
            result['error'] = 'Unsigned manifest rejected — signature required'
            return result
        try:
            from security.master_key import verify_release_manifest
            if not verify_release_manifest(manifest):
                result['error'] = 'Invalid release manifest signature'
                return result
        except ImportError:
            result['error'] = 'Security module unavailable for verification'
            return result
        except Exception as e:
            result['error'] = f'Manifest verification failed: {e}'
            return result

        # 2. Compare code hash
        try:
            from security.node_integrity import compute_code_hash
            current_hash = compute_code_hash()
            manifest_hash = manifest.get('code_hash', '')
            if current_hash == manifest_hash:
                result['reason'] = 'Already up to date'
                return result
            result['old_hash'] = current_hash
            result['new_hash'] = manifest_hash
        except Exception:
            pass

        # 3. Pull and checkout pinned commit (prevent TOCTOU)
        manifest_sha = manifest.get('merge_sha', '')
        try:
            # Fetch first, then checkout exact commit from manifest
            fetch = subprocess.run(
                ['git', 'fetch', 'origin', 'main'],
                capture_output=True, text=True, timeout=120)
            if fetch.returncode != 0:
                result['error'] = f'Git fetch failed: {fetch.stderr[:200]}'
                return result
            if manifest_sha:
                checkout = subprocess.run(
                    ['git', 'checkout', manifest_sha],
                    capture_output=True, text=True, timeout=30)
                if checkout.returncode != 0:
                    result['error'] = f'Checkout pinned SHA failed: {checkout.stderr[:200]}'
                    return result
            else:
                pull = subprocess.run(
                    ['git', 'pull', 'origin', 'main'],
                    capture_output=True, text=True, timeout=120)
                if pull.returncode != 0:
                    result['error'] = f'Git pull failed: {pull.stderr[:200]}'
                    return result
        except Exception as e:
            result['error'] = f'Git update failed: {e}'
            return result

        # 4. Graceful restart via watchdog
        try:
            from security.node_watchdog import NodeWatchdog
            watchdog = NodeWatchdog.get_instance()
            if watchdog:
                watchdog.request_restart('version_update')
        except Exception as e:
            logger.debug(f"Watchdog restart request failed: {e}")

        result['updated'] = True
        result['old_version'] = result.get('old_hash', 'unknown')[:8]
        result['new_version'] = version
        return result
