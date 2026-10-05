"""
Coding Agent Orchestrator — Unified entry point for coding tool execution.

Singleton orchestrator that:
- Auto-detects installed tools
- Routes tasks to the best tool via CodingToolRouter
- Records benchmarks for distributed learning
- Supports compute-aware hive offload when local compute is insufficient
- Exposes list_tools() and get_benchmarks() for Nunba settings UI

This is a LEAF tool — it calls external CLI subprocesses, never /chat.
This eliminates callback loops and double-dispatch antipatterns.
"""
import logging
import os
import threading
from typing import Dict, Optional

logger = logging.getLogger('hevolve.coding_agent')


class CodingAgentOrchestrator:
    """Singleton coding agent orchestrator."""

    def __init__(self):
        self._lock = threading.Lock()

    def execute(self, task: str, task_type: str = 'feature',
                preferred_tool: str = '', user_id: str = '',
                model: str = '', working_dir: str = '',
                data_scope: str = '', prompt_id: str = '') -> Dict:
        """Execute a coding task using the best available tool.

        This is a terminal operation — calls subprocess, never /chat.
        Safe to invoke from within an AutoGen agent's tool execution.

        Respects data classification:
          - edge_only / user_devices → local execution only
          - trusted_peer → shard with INTERFACES scope, E2E to trusted peers
          - federated / public → shard with FULL_FILE, fan out to N peers
          - (empty) → auto-classify based on file content (DLP scan)

        Args:
            task: The coding task description
            task_type: code_review, feature, bug_fix, refactor, app_build
            preferred_tool: User override, any key of tool_backends.BACKENDS
                (kilocode, claude_code, opencode, pi, hermes, aider_native, claw_native)
            user_id: For benchmark tracking
            model: LLM model override (empty = use tool's default)
            working_dir: Working directory for the coding tool
            data_scope: Privacy scope override (empty = auto-classify)
            prompt_id: The agent asking (its turn's prompt_id); empty = the
                one stamped on this thread.  The owner's permission and the
                ribbon name it (see _execute_local).

        Returns:
            {success, output, tool, execution_time_s, task_type, error?}
        """
        # ── Step 1: Classify data scope ──
        scope = self._classify_scope(task, working_dir, data_scope)
        logger.info(f"[CLASSIFY] task_type={task_type}, scope={scope}")

        # ── Step 2: Route by scope ──
        if scope in ('edge_only', 'user_devices'):
            # Private — never leaves this device (or user's own devices)
            return self._execute_local(task, task_type, preferred_tool,
                                        user_id, model, working_dir,
                                        prompt_id=prompt_id)

        if not self._can_run_locally():
            # Node can't run locally — distribute with scope-aware sharding
            return self._distribute_to_hive(task, task_type, preferred_tool,
                                             user_id, model, working_dir, scope)

        return self._execute_local(task, task_type, preferred_tool,
                                    user_id, model, working_dir,
                                    prompt_id=prompt_id)

    def _classify_scope(self, task: str, working_dir: str,
                         override: str = '') -> str:
        """Classify the privacy scope of a coding task.

        Order: explicit override > DLP scan > default (trusted_peer).
        """
        if override:
            return override

        # Auto-classify by scanning target files for secrets/PII
        try:
            from security.dlp_engine import get_dlp_engine

            if not working_dir:
                return 'trusted_peer'  # Default: shareable with trusted peers

            # Scan a sample of files for PII/secrets
            dlp = get_dlp_engine()
            has_secrets = False
            files_checked = 0
            for root, _, files in os.walk(working_dir):
                for fname in files:
                    if fname.endswith(('.env', '.pem', '.key', 'credentials.json',
                                       'config.json', '.secret')):
                        has_secrets = True
                        break
                    if files_checked >= 5:
                        break
                    if fname.endswith(('.py', '.rs', '.js', '.ts')):
                        fpath = os.path.join(root, fname)
                        try:
                            with open(fpath, 'r', errors='ignore') as f:
                                sample = f.read(4096)
                            findings = dlp.scan(sample)
                            if findings:
                                has_secrets = True
                                break
                        except IOError:
                            pass
                        files_checked += 1
                if has_secrets:
                    break

            if has_secrets:
                return 'edge_only'  # Secrets found — local only
            return 'trusted_peer'  # Safe for trusted peers
        except Exception:
            return 'trusted_peer'  # Default if classification fails

    def _execute_local(self, task: str, task_type: str,
                        preferred_tool: str, user_id: str,
                        model: str, working_dir: str,
                        prompt_id: str = '') -> Dict:
        """Run one coding task on THIS computer.

        Every local coding run starts here: the agent tool
        (core.agent_tools.execute_coding_task), MCP `code`, POST
        /coding/execute, a peer's shard (api.py) and `hart code`.  A coding run
        writes files and, through an agentic CLI, runs commands, so it is
        computer use and passes what every other computer-use dispatcher
        (the shell tool, _handle_shell_command_tool, and the VLM loop)
        already passes:
          1. integrations.vlm.safety.computer_operation_refusal: never power,
             reset, erase or format, and asks nobody about it;
          2. integrations.vlm.safety.computer_control_block: the owner's
             computer_control permission, asked through ConsentService when
             absent (a daemon run does not wait);
          3. integrations.vlm.activity_stream.current_run: the AI-control
             ribbon and the companion window show the run while it happens
             and close it after.
        It works in vlm_adapter.resolve_task_workspace: the caller's
        directory, else the agent's goal repo_path, else the user-data coding
        workspace, never the process cwd.  Measured 2026-10-05: with no
        working_dir the in-process backend worked in '.', the installed app's
        own folder, leaving a 14 MB repo-map cache in C:\\Program Files
        (x86)\\HevolveAI\\Nunba and creating folders inside the install.
        """
        from .tool_router import CodingToolRouter
        from integrations.vlm.safety import (
            computer_control_block, computer_operation_refusal)

        refusal = computer_operation_refusal(task)
        if refusal:
            return self._not_run(task_type, f'Coding task refused: {refusal}')

        router = CodingToolRouter()
        backend = router.route(task, task_type, preferred_tool)

        if backend is None:
            from .installer import TOOL_REGISTRY
            return {
                'success': False,
                'output': '',
                'tool': 'none',
                'task_type': task_type,
                'error': 'No coding tools installed. Install one: '
                         + ', '.join(TOOL_REGISTRY) + '.',
            }

        agent, thread_user = _thread_identity()
        agent = prompt_id or agent
        refusal = computer_control_block(agent)
        if refusal:
            return self._not_run(task_type, refusal, tool=backend.name)

        from integrations.vlm.vlm_adapter import resolve_task_workspace
        working_dir = resolve_task_workspace(prompt_id=agent, explicit=working_dir)

        # Build context for the backend
        context = {'working_dir': working_dir}
        if model:
            context['model'] = model

        # Execute (leaf operation, no /chat re-entry), announced as it runs
        from integrations.vlm.activity_stream import current_run
        run = current_run(user_id=user_id or thread_user or '',
                          prompt_id=agent or '')
        caption = f'Coding: {task[:60]}'
        run.step(iteration=1, action='coding', phase='executing', caption=caption)
        phase, error = 'failed', ''
        try:
            result = backend.execute(task, context)
            if result.get('success'):
                phase = 'completed'
            else:
                error = str(result.get('error') or 'the coding run did not succeed')
        except Exception as e:
            error = f'{type(e).__name__}: {e}'
            raise
        finally:
            run.step(iteration=1, action='coding', phase=phase,
                     caption=caption, error=error[:160])
            # Closes only a run this call opened; inside a VLM run the loop
            # still owns the ribbon and the ledger task.
            run.finish(exit_reason='done' if phase == 'completed' else 'action_error',
                       iteration=1, action='coding', error=error[:160])
        result['task_type'] = task_type

        self._record_benchmark(
            task_type=task_type,
            tool_name=result.get('tool', backend.name),
            completion_time_s=result.get('execution_time_s', 0),
            success=result.get('success', False),
            model_name=model,
            user_id=user_id,
        )

        # Capture successful edits as recipe steps for REUSE mode
        if result.get('success') and result.get('edits'):
            try:
                from .recipe_bridge import CodingRecipeBridge
                bridge = CodingRecipeBridge()
                bridge.capture_edit_as_recipe_step(
                    task=task,
                    tool_name=result.get('tool', backend.name),
                    file_edits=result['edits'],
                    working_dir=working_dir,
                )
            except Exception as e:
                logger.debug(f"Recipe capture: {e}")

        # Broadcast to EventBus — UI, ledger, Agent Lightning all subscribe
        try:
            from core.platform.events import emit_event
            emit_event('coding.task_completed', {
                'tool': result.get('tool', backend.name),
                'task_type': task_type,
                'success': result.get('success', False),
                'execution_time_s': result.get('execution_time_s', 0),
                'user_id': user_id,
                'working_dir': working_dir,
                'task_summary': task[:200],
            })
        except Exception:
            pass  # EventBus optional — don't block on it

        return result

    @staticmethod
    def _not_run(task_type: str, reason: str, tool: str = 'none') -> Dict:
        """A coding run that did not start: nothing ran, nothing to record."""
        return {'success': False, 'output': '', 'tool': tool,
                'task_type': task_type, 'execution_time_s': 0,
                'error': reason}

    def _can_run_locally(self) -> bool:
        """Check if this node has sufficient compute for coding tools.

        Coding tools are CLI subprocesses — they mostly need network
        (for API calls) and disk space, not GPU. So the gate is lenient.
        """
        try:
            from security.system_requirements import (
                get_tier, _TIER_RANK, NodeTierLevel, FEATURE_TIER_MAP
            )
            feature_entry = FEATURE_TIER_MAP.get('coding_aggregator')
            if not feature_entry:
                return True  # Feature not in map yet, allow
            min_tier, _ = feature_entry
            current = get_tier()
            return _TIER_RANK[current] >= _TIER_RANK[min_tier]
        except Exception:
            return True  # If system_requirements unavailable, allow

    @staticmethod
    def _record_benchmark(**fields) -> None:
        """Write one benchmark row; a failed write is logged, never raised.

        Every caller has a finished coding result in hand when it records.
        A raise here used to discard that result: on the installed desktop
        "attempt to write a readonly database" replaced the output of a
        completed `claude -p` run, and on the hive paths it re-ran the task
        locally.  The row is telemetry; the result is what the user asked for.
        """
        try:
            from .benchmark_tracker import get_benchmark_tracker
            get_benchmark_tracker().record(**fields)
        except Exception as e:
            logger.warning(
                "Benchmark row not recorded for %s/%s (%s: %s); "
                "the coding result is returned regardless",
                fields.get('task_type'), fields.get('tool_name'),
                type(e).__name__, e, exc_info=True)

    def _distribute_to_hive(self, task: str, task_type: str,
                             preferred_tool: str, user_id: str,
                             model: str, working_dir: str,
                             scope: str = 'trusted_peer') -> Dict:
        """Shard task, fan out to N peers in parallel, merge results.

        Flow:
          1. ShardEngine.decompose_task() → N shards
          2. ScopeGuard.check_egress() on each shard
          3. Fan out shards to available trusted peers (concurrent)
          4. Merge diffs, validate, record benchmarks
          5. Fall back to single-peer or local on failure

        Scope determines shard visibility:
          - trusted_peer → INTERFACES (signatures only, not implementations)
          - federated/public → FULL_FILE (peer sees everything)
        """
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed

        start = time.time()

        try:
            from integrations.agent_engine.shard_engine import ShardEngine, ShardScope
            from integrations.agent_engine.compute_mesh_service import get_compute_mesh
            from security.channel_encryption import encrypt_json_for_peer, decrypt_json_from_peer
            from security.edge_privacy import PrivacyScope, ScopeGuard
            from core.http_pool import pooled_post

            # ── Map scope to shard visibility ──
            # trusted_peer uses FULL_FILE because the payload is E2E encrypted
            # to the specific peer — the content is as private as the transport.
            # INTERFACES scope would send empty files, making the peer useless.
            shard_scope = {
                'trusted_peer': ShardScope.FULL_FILE,
                'federated': ShardScope.FULL_FILE,
                'public': ShardScope.FULL_FILE,
            }.get(scope, ShardScope.FULL_FILE)

            # ── Decompose task into shards ──
            engine = ShardEngine(code_root=working_dir) if working_dir else ShardEngine()
            shards = engine.decompose_task(task, scope=shard_scope, max_files_per_shard=5)

            if not shards:
                logger.info("[DISTRIBUTE] No shards — falling back to single-peer offload")
                return self._offload_to_hive(task, task_type, preferred_tool,
                                              user_id, model, working_dir)

            logger.info(f"[DISTRIBUTE] Decomposed into {len(shards)} shards (scope={shard_scope.value})")

            # ── Check egress on each shard ──
            guard = ScopeGuard()
            dest_privacy = {
                'trusted_peer': PrivacyScope.TRUSTED_PEER,
                'federated': PrivacyScope.FEDERATED,
                'public': PrivacyScope.PUBLIC,
            }.get(scope, PrivacyScope.TRUSTED_PEER)

            cleared_shards = []
            for shard in shards:
                shard_data = {
                    '_privacy_scope': scope,
                    'task': shard.task_description,
                    'files': list(shard.full_content.keys()) if shard.full_content else [],
                }
                allowed, reason = guard.check_egress(shard_data, dest_privacy)
                if allowed:
                    cleared_shards.append(shard)
                else:
                    logger.warning(f"[DISTRIBUTE] Shard blocked by ScopeGuard: {reason}")

            if not cleared_shards:
                logger.info("[DISTRIBUTE] All shards blocked — executing locally")
                return self._execute_local(task, task_type, preferred_tool,
                                            user_id, model, working_dir)

            # ── Get available peers ──
            mesh = get_compute_mesh()
            peers = mesh.get_available_peers()
            trusted_peers = [p for p in peers if self._is_code_trusted(p)]

            if not trusted_peers:
                logger.info("[DISTRIBUTE] No trusted peers — executing locally")
                return self._execute_local(task, task_type, preferred_tool,
                                            user_id, model, working_dir)

            # ── Fan out: assign shards to peers round-robin, execute in parallel ──
            assignments = []
            for i, shard in enumerate(cleared_shards):
                peer = trusted_peers[i % len(trusted_peers)]
                assignments.append((shard, peer))

            logger.info(f"[DISTRIBUTE] Dispatching {len(assignments)} shards to "
                        f"{len(trusted_peers)} peers")

            merged_output = []
            merged_diffs = {}
            failures = 0

            # Get our own public key so peers can encrypt responses back to us
            try:
                from security.channel_encryption import get_x25519_public_hex
                our_public_key = get_x25519_public_hex()
            except Exception:
                our_public_key = None

            def _dispatch_shard(shard, peer):
                """Send one shard to one peer, return result."""
                peer_pub = peer.get('x25519_public_hex')
                peer_url = peer.get('url', '')
                if not peer_pub or not peer_url:
                    return None

                # Convert interface_specs to serializable dicts
                iface_dicts = []
                for spec in (shard.interface_specs or []):
                    try:
                        from dataclasses import asdict
                        iface_dicts.append(asdict(spec))
                    except Exception:
                        pass

                payload = {
                    'task': shard.task_description,
                    'task_type': task_type,
                    'preferred_tool': preferred_tool,
                    'model': model,
                    'file_content': shard.full_content or {},
                    'interfaces': iface_dicts,
                    'shard_scope': shard.scope.value,
                    'sender_public_key': our_public_key,  # Inside envelope — MITM can't replace
                }
                envelope = encrypt_json_for_peer(payload, peer_pub)
                try:
                    resp = pooled_post(
                        f'{peer_url.rstrip("/")}/coding/execute',
                        json={'encrypted': envelope},
                        timeout=300,
                    )
                    if resp.status_code == 200:
                        resp_data = resp.json()
                        encrypted_result = resp_data.get('encrypted')
                        if encrypted_result:
                            return decrypt_json_from_peer(encrypted_result)
                        # Fallback: unencrypted result (SAME_USER trust)
                        return resp_data.get('result')
                except Exception as e:
                    logger.warning(f"[DISTRIBUTE] Peer {peer.get('node_id','?')} failed: {e}")
                return None

            with ThreadPoolExecutor(max_workers=min(len(assignments), 8)) as pool:
                futures = {
                    pool.submit(_dispatch_shard, shard, peer): (shard, peer)
                    for shard, peer in assignments
                }
                for future in as_completed(futures):
                    shard, peer = futures[future]
                    result = future.result()
                    peer_id = peer.get('node_id', 'unknown')
                    if result and result.get('success'):
                        merged_output.append(result.get('output', ''))
                        # Merge diffs (validate: only files in the shard's target list)
                        allowed_files = set(shard.target_files or [])
                        if shard.full_content:
                            allowed_files.update(shard.full_content.keys())
                        for fpath, diff in result.get('diffs', {}).items():
                            if not allowed_files or fpath in allowed_files:
                                merged_diffs[fpath] = diff
                            else:
                                logger.warning(f"[DISTRIBUTE] Peer {peer_id} "
                                               f"returned diff for unauthorized file: {fpath}")
                        self._record_peer_trust(peer_id, success=True)
                    else:
                        failures += 1
                        self._record_peer_trust(peer_id, success=False)

            elapsed = time.time() - start

            # ── Record benchmark ──
            self._record_benchmark(
                task_type=task_type,
                tool_name='distributed',
                completion_time_s=elapsed,
                success=failures < len(assignments),
                model_name=model,
                user_id=user_id,
                offloaded=True,
            )

            # ── Emit event ──
            try:
                from core.platform.events import emit_event
                emit_event('coding.distributed_complete', {
                    'shards': len(cleared_shards),
                    'peers_used': len(trusted_peers),
                    'failures': failures,
                    'elapsed_s': elapsed,
                    'scope': scope,
                    'task_type': task_type,
                })
            except Exception:
                pass

            return {
                'success': failures < len(assignments),
                'output': '\n---\n'.join(merged_output),
                'diffs': merged_diffs,
                'tool': 'distributed',
                'task_type': task_type,
                'execution_time_s': elapsed,
                'shards_total': len(cleared_shards),
                'shards_succeeded': len(cleared_shards) - failures,
                'peers_used': min(len(trusted_peers), len(cleared_shards)),
                'scope': scope,
            }

        except Exception as e:
            logger.warning(f"[DISTRIBUTE] Failed ({e}), falling back to single-peer")
            return self._offload_to_hive(task, task_type, preferred_tool,
                                          user_id, model, working_dir)

    def _offload_to_hive(self, task: str, task_type: str,
                          preferred_tool: str, user_id: str,
                          model: str, working_dir: str) -> Dict:
        """Offload to a trusted hive peer with sufficient compute.

        Security: E2E encryption (X25519 + AES-256-GCM) with full source context.
        Trust: Only offload code tasks to peers with sufficient trust score.
        Accuracy > Security theater: peers get full file content (encrypted),
        not interface stubs. An LLM without full context produces broken code.

        Autotrust: peers earn trust through successful task completion.
        After 5+ validated code tasks, a peer auto-promotes to code-trusted.
        """
        try:
            from integrations.agent_engine.compute_mesh_service import get_compute_mesh
            from security.channel_encryption import (
                encrypt_json_for_peer, decrypt_json_from_peer
            )
            from core.http_pool import pooled_post

            mesh = get_compute_mesh()
            peers = mesh.get_available_peers()

            if not peers:
                logger.info("No hive peers available, attempting local execution")
                return self._execute_local(task, task_type, preferred_tool,
                                            user_id, model, working_dir)

            # Filter to code-trusted peers only
            trusted_peers = [
                p for p in peers
                if self._is_code_trusted(p)
            ]

            if not trusted_peers:
                logger.info("No code-trusted peers, executing locally")
                return self._execute_local(task, task_type, preferred_tool,
                                            user_id, model, working_dir)

            # Pick best trusted peer (by compute score)
            best_peer = max(trusted_peers, key=lambda p: mesh.score(p))
            peer_pub = best_peer.get('x25519_public_hex')
            peer_url = best_peer.get('url', '')

            if not peer_pub or not peer_url:
                return self._execute_local(task, task_type, preferred_tool,
                                            user_id, model, working_dir)

            # Include full source context for target files (encrypted)
            # Accuracy > security theater: the peer needs full context to code well
            file_content = {}
            if working_dir:
                file_content = self._read_target_files(task, working_dir)

            # Encrypt full payload (forward secrecy via ephemeral keys)
            payload = {
                'task': task,
                'task_type': task_type,
                'preferred_tool': preferred_tool,
                'model': model,
                'file_content': file_content,
                'working_dir_name': os.path.basename(working_dir) if working_dir else '',
            }
            envelope = encrypt_json_for_peer(payload, peer_pub)

            # POST to peer's /coding/execute endpoint
            resp = pooled_post(
                f'{peer_url.rstrip("/")}/coding/execute',
                json={'encrypted': envelope},
                timeout=300,
            )

            peer_id = best_peer.get('node_id', 'unknown')
            if resp.status_code == 200:
                encrypted_result = resp.json().get('encrypted')
                if encrypted_result:
                    result = decrypt_json_from_peer(encrypted_result)
                    if result:
                        result['offloaded'] = True
                        result['peer_id'] = peer_id

                        # Validate diffs only touch expected files
                        diffs = result.get('diffs', {})
                        if file_content and diffs:
                            unauthorized = [
                                f for f in diffs
                                if f not in file_content
                            ]
                            if unauthorized:
                                logger.warning(
                                    f"Peer {peer_id} returned diffs for "
                                    f"unauthorized files: {unauthorized}")
                                result['success'] = False
                                result['error'] = 'Unauthorized file modifications'

                        self._record_benchmark(
                            task_type=task_type,
                            tool_name=result.get('tool', 'unknown'),
                            completion_time_s=result.get('execution_time_s', 0),
                            success=result.get('success', False),
                            model_name=model,
                            user_id=user_id,
                            offloaded=True,
                        )

                        # Autotrust: successful validated tasks build trust
                        if result.get('success'):
                            self._record_peer_trust(peer_id, success=True)
                        return result

            # Peer failed — record for trust scoring
            self._record_peer_trust(peer_id, success=False)

        except Exception as e:
            logger.warning(f"Hive offload failed ({e}), falling back to local")

        # Fallback to local
        return self._execute_local(task, task_type, preferred_tool,
                                    user_id, model, working_dir)

    @staticmethod
    def _is_code_trusted(peer: Dict) -> bool:
        """Check if a peer is trusted for code tasks.

        Trust sources (any one is sufficient):
        - SAME_USER: peer belongs to the same user (their other machine)
        - Explicit grant: peer has 'code_trusted' flag
        - Autotrust: peer has 5+ successful validated code tasks
        """
        if peer.get('trust_level') == 'SAME_USER':
            return True
        if peer.get('code_trusted'):
            return True
        # Autotrust: earned through track record
        successful_tasks = peer.get('successful_code_tasks', 0)
        return successful_tasks >= 5

    @staticmethod
    def _record_peer_trust(peer_id: str, success: bool):
        """Record a peer's code task result for autotrust scoring."""
        try:
            from integrations.social.models import db_session, PeerNode
            with db_session() as db:
                node = db.query(PeerNode).filter_by(node_id=peer_id).first()
                if node and hasattr(node, 'successful_code_tasks'):
                    if success:
                        node.successful_code_tasks = (
                            node.successful_code_tasks or 0) + 1
                    else:
                        node.successful_code_tasks = max(
                            0, (node.successful_code_tasks or 0) - 2)
        except Exception:
            pass

    @staticmethod
    def _read_target_files(task: str, working_dir: str) -> Dict[str, str]:
        """Read target files for the task from the working directory.

        Uses the shard engine's keyword matching to find relevant files,
        limited to a reasonable context window.
        """
        file_content = {}
        max_files = 10
        max_total_chars = 200_000  # ~50K tokens
        total_chars = 0
        try:
            from integrations.agent_engine.shard_engine import ShardEngine
            engine = ShardEngine(code_root=working_dir)
            imap = engine.get_interface_map()

            # Score files by task keyword relevance
            task_lower = task.lower()
            scored = []
            for rel_path, spec in imap.items():
                names = (
                    [f['name'] for f in spec.functions] +
                    [c['name'] for c in spec.classes] +
                    [rel_path]
                )
                score = sum(1 for n in names if n.lower() in task_lower)
                if score > 0:
                    scored.append((rel_path, score))
            scored.sort(key=lambda x: -x[1])

            for rel_path, _ in scored[:max_files]:
                full_path = os.path.join(working_dir, rel_path)
                if os.path.exists(full_path):
                    try:
                        with open(full_path, 'r', encoding='utf-8',
                                  errors='ignore') as f:
                            content = f.read()
                        if total_chars + len(content) > max_total_chars:
                            break
                        file_content[rel_path] = content
                        total_chars += len(content)
                    except IOError:
                        pass
        except Exception as e:
            logger.debug(f"Target file reading: {e}")
        return file_content

    def list_tools(self) -> Dict:
        """List all tools with install status, capabilities, and benchmarks."""
        from .installer import get_tool_info
        from .benchmark_tracker import get_benchmark_tracker

        tools = get_tool_info()
        try:
            benchmarks = get_benchmark_tracker().get_summary()
        except Exception:
            benchmarks = {'total_benchmarks': 0, 'by_tool': [], 'by_task_type': []}

        return {
            'tools': tools,
            'benchmarks': benchmarks,
            'can_run_locally': self._can_run_locally(),
        }

    def get_benchmarks(self) -> Dict:
        """Get benchmark dashboard data."""
        from .benchmark_tracker import get_benchmark_tracker
        return get_benchmark_tracker().get_summary()


def _thread_identity():
    """(prompt_id, user_id) stamped on this thread by the turn running it,
    each None when absent (a CLI or route caller has no turn)."""
    try:
        from hartos.threadlocal import thread_local_data
        return thread_local_data.get_prompt_id(), thread_local_data.get_user_id()
    except Exception:
        return None, None


# ─── Module-level singleton ───
_orchestrator = None
_orchestrator_lock = threading.Lock()


def get_coding_orchestrator() -> CodingAgentOrchestrator:
    """Get or create the singleton CodingAgentOrchestrator."""
    global _orchestrator
    if _orchestrator is None:
        with _orchestrator_lock:
            if _orchestrator is None:
                _orchestrator = CodingAgentOrchestrator()
    return _orchestrator
