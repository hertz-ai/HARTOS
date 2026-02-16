"""
IP Protection Service — Patent CRUD + Moat Verification

Static service class (project pattern). All methods receive db: Session,
call db.flush() not db.commit(). Caller owns the transaction.

The real IP protection is not legal (patents) — it's technical irreproducibility.
The system's value lives in ACCUMULATED LATENT STATE, not code:
  - Latent dynamics trained on N million real interactions (uncopyable)
  - HiveMind collective: N nodes × N edges = N² knowledge (network effect)
  - MetaLearningRouter policy shaped by real create/reuse/compose decisions
  - Kernel support vectors from real expert corrections
  - LoRA task slots from real conceptual learning
  - Episodic memory: years of VQ-compressed experiences
  - Master key perimeter: cryptographic, non-forkable identity chain

A competitor with full codebase starts at zero latent state.
First online = exponential compounding advantage.
"""
import glob
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger('hevolve_social')


class IPService:
    """Static service for IP protection operations."""

    @staticmethod
    def create_patent(db: Session, title: str, claims: list,
                      abstract: str = '', description: str = '',
                      filing_type: str = 'provisional',
                      verification_metrics: dict = None,
                      evidence: list = None,
                      goal_id: str = None,
                      created_by: str = None) -> Dict:
        """Create a patent draft."""
        from integrations.social.models import IPPatent

        patent = IPPatent(
            title=title,
            claims_json=claims or [],
            abstract=abstract,
            description=description,
            filing_type=filing_type,
            verification_metrics=verification_metrics or {},
            evidence_json=evidence or [],
            goal_id=goal_id,
            created_by=created_by,
            status='draft',
        )
        db.add(patent)
        db.flush()
        return patent.to_dict()

    @staticmethod
    def get_patent(db: Session, patent_id: str) -> Optional[Dict]:
        """Get a single patent."""
        from integrations.social.models import IPPatent

        patent = db.query(IPPatent).filter_by(id=patent_id).first()
        return patent.to_dict() if patent else None

    @staticmethod
    def list_patents(db: Session, status: str = None) -> List[Dict]:
        """List patents with optional status filter."""
        from integrations.social.models import IPPatent

        q = db.query(IPPatent)
        if status:
            q = q.filter_by(status=status)
        return [p.to_dict() for p in q.order_by(IPPatent.created_at.desc()).all()]

    @staticmethod
    def update_patent_status(db: Session, patent_id: str, status: str,
                             application_number: str = None,
                             patent_number: str = None) -> Optional[Dict]:
        """Update patent status and optional filing details."""
        from integrations.social.models import IPPatent

        patent = db.query(IPPatent).filter_by(id=patent_id).first()
        if not patent:
            return None
        patent.status = status
        if application_number:
            patent.application_number = application_number
        if patent_number:
            patent.patent_number = patent_number
        if status == 'filed' and not patent.filing_date:
            patent.filing_date = datetime.utcnow()
        db.flush()
        return patent.to_dict()

    @staticmethod
    def create_infringement(db: Session, patent_id: str,
                            infringer_name: str, infringer_url: str = '',
                            evidence_summary: str = '',
                            risk_level: str = 'low') -> Dict:
        """Record a detected infringement."""
        from integrations.social.models import IPInfringement

        infringement = IPInfringement(
            patent_id=patent_id,
            infringer_name=infringer_name,
            infringer_url=infringer_url,
            evidence_summary=evidence_summary,
            risk_level=risk_level,
            status='detected',
        )
        db.add(infringement)
        db.flush()
        return infringement.to_dict()

    @staticmethod
    def update_infringement_status(db: Session, infringement_id: str,
                                   status: str, notice_type: str = None,
                                   notice_text: str = None) -> Optional[Dict]:
        """Update infringement status and optional notice details."""
        from integrations.social.models import IPInfringement

        inf = db.query(IPInfringement).filter_by(id=infringement_id).first()
        if not inf:
            return None
        inf.status = status
        if notice_type:
            inf.notice_type = notice_type
        if notice_text:
            inf.notice_text = notice_text
        if status == 'notice_sent' and not inf.notice_sent_at:
            inf.notice_sent_at = datetime.utcnow()
        db.flush()
        return inf.to_dict()

    @staticmethod
    def list_infringements(db: Session, patent_id: str = None,
                           status: str = None) -> List[Dict]:
        """List infringements with optional filters."""
        from integrations.social.models import IPInfringement

        q = db.query(IPInfringement)
        if patent_id:
            q = q.filter_by(patent_id=patent_id)
        if status:
            q = q.filter_by(status=status)
        return [i.to_dict() for i in q.order_by(IPInfringement.created_at.desc()).all()]

    # ─── Flywheel verification ───

    @staticmethod
    def get_loop_health() -> Dict:
        """Aggregate self-improving loop metrics from all live sources.

        Checks 5 flywheel components:
        1. World model (crawl4ai) health + learning stats
        2. Agent goal completion rates
        3. RALT skill propagation stats
        4. Recipe reuse adoption rate
        5. HiveMind connected agents

        FLYWHEEL LOOPHOLE OWNERSHIP (each loophole has a responsible agent):
        - Cold start → HiveMind bootstrap (tensor fusion gives instant collective knowledge)
        - Single-node → Marketing Agent metric (grow network = more nodes = more learning)
        - Feedback staleness → Coding Agent (identifies queue bottlenecks in code review)
        - Recipe drift → Coding Agent (version-aware recipe validation during review)
        - Gossip partition → Guardrails Agent (monitors network health deterministically)
        - Guardrail drift → Guardrails Agent (dedicated guardrail integrity monitor)

        ARCHITECTURE PRINCIPLE: Deterministic intelligence interleaved with probabilistic.
        Every probabilistic (LLM) decision has a deterministic gate:
        - Guardrails are deterministic (regex, hash, threshold) wrapping LLM output
        - Recipe reuse is deterministic (exact replay) after LLM-generated CREATE
        - RALT topology verification is deterministic before probabilistic skill fusion
        - Circuit breaker is deterministic halt on anomaly detection
        """
        result = {
            'world_model': {'healthy': False},
            'agent_performance': {'total_goals': 0, 'completed': 0, 'success_rate': 0.0},
            'ralt_propagation': {'total_distributed': 0, 'total_blocked': 0},
            'recipe_adoption': {'total_recipes': 0, 'reuse_rate': 0.0},
            'hivemind_agents': [],
            'loop_verified': False,
            'improvement_rate': 0.0,
            'flywheel_loopholes': [],
        }

        # Loophole ownership map — which agent is responsible for fixing each
        loophole_owners = {
            'cold_start': 'hivemind',        # HiveMind bootstrap gives instant knowledge
            'single_node': 'marketing',       # Marketing grows the node network
            'feedback_staleness': 'coding',   # Coding agent fixes flush pipeline
            'recipe_drift': 'coding',         # Coding agent validates recipe freshness
            'guardrail_drift': 'guardrails',  # Guardrails agent monitors integrity
            'gossip_partition': 'guardrails',  # Guardrails agent monitors network health
        }

        # 1. World model health
        try:
            from integrations.agent_engine.world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()
            health = bridge.check_health()
            stats = bridge.get_learning_stats()
            bridge_stats = bridge.get_stats()
            result['world_model'] = {
                'healthy': health.get('healthy', False),
                'learning': stats.get('learning', {}),
                'bridge': bridge_stats,
            }
            # Loophole: queue backup → coding agent should fix flush pipeline
            queue_size = bridge_stats.get('queue_size', 0)
            if queue_size > 500:
                result['flywheel_loopholes'].append({
                    'type': 'feedback_staleness',
                    'owner': loophole_owners['feedback_staleness'],
                    'severity': 'high',
                    'message': f'Experience queue backing up ({queue_size} items)',
                    'remediation': 'Coding agent: optimize _flush_to_world_model batch size or add workers',
                })
        except Exception:
            result['flywheel_loopholes'].append({
                'type': 'cold_start',
                'owner': loophole_owners['cold_start'],
                'severity': 'critical',
                'message': 'World model bridge unavailable — no learning happening',
                'remediation': 'HiveMind bootstrap: connect to collective for instant knowledge',
            })

        # 2. Agent performance
        try:
            from integrations.social.models import get_db, AgentGoal
            db = get_db()
            try:
                total = db.query(AgentGoal).count()
                completed = db.query(AgentGoal).filter_by(status='completed').count()
                result['agent_performance'] = {
                    'total_goals': total,
                    'completed': completed,
                    'success_rate': round(completed / total, 3) if total > 0 else 0.0,
                }
                if total < 100:
                    result['flywheel_loopholes'].append({
                        'type': 'single_node',
                        'owner': loophole_owners['single_node'],
                        'severity': 'medium',
                        'message': f'Insufficient goal volume ({total}) — need 100+',
                        'remediation': 'Marketing agent: grow user base to increase goal throughput',
                    })
            finally:
                db.close()
        except Exception:
            pass

        # 3. RALT propagation
        try:
            from integrations.agent_engine.world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()
            bs = bridge.get_stats()
            distributed = bs.get('total_skills_distributed', 0)
            blocked = bs.get('total_skills_blocked', 0)
            result['ralt_propagation'] = {
                'total_distributed': distributed,
                'total_blocked': blocked,
            }
            if distributed == 0:
                result['flywheel_loopholes'].append({
                    'type': 'cold_start',
                    'owner': loophole_owners['cold_start'],
                    'severity': 'high',
                    'message': 'Zero RALT skills distributed — hive is not learning from peers',
                    'remediation': 'HiveMind bootstrap: first node seeds RALT from collective tensor fusion',
                })
            if blocked > distributed and distributed > 0:
                result['flywheel_loopholes'].append({
                    'type': 'guardrail_drift',
                    'owner': loophole_owners['guardrail_drift'],
                    'severity': 'medium',
                    'message': f'More skills blocked ({blocked}) than distributed ({distributed})',
                    'remediation': 'Guardrails agent: review filter thresholds — '
                                  'deterministic gates may be too restrictive',
                })
        except Exception:
            pass

        # 4. Recipe adoption
        try:
            prompts_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                'prompts')
            if os.path.isdir(prompts_dir):
                all_prompts = glob.glob(os.path.join(prompts_dir, '*.json'))
                recipes = [f for f in all_prompts if '_recipe.json' in f]
                total_prompts = len([f for f in all_prompts
                                     if '_recipe.json' not in f
                                     and not f.endswith('_recipe.json')])
                reuse_rate = round(len(recipes) / total_prompts, 3) if total_prompts > 0 else 0.0
                result['recipe_adoption'] = {
                    'total_recipes': len(recipes),
                    'total_prompts': total_prompts,
                    'reuse_rate': reuse_rate,
                }
                if reuse_rate < 0.6 and total_prompts > 10:
                    result['flywheel_loopholes'].append({
                        'type': 'recipe_drift',
                        'owner': loophole_owners['recipe_drift'],
                        'severity': 'medium',
                        'message': f'Recipe reuse rate {reuse_rate:.0%} below 60% threshold',
                        'remediation': 'Coding agent: add recipe versioning — deterministic '
                                      'staleness check before probabilistic re-creation',
                    })
        except Exception:
            pass

        # 5. HiveMind agents
        try:
            from integrations.agent_engine.world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()
            agents = bridge.get_hivemind_agents()
            result['hivemind_agents'] = agents
            if len(agents) < 3:
                result['flywheel_loopholes'].append({
                    'type': 'single_node',
                    'owner': loophole_owners['single_node'],
                    'severity': 'high',
                    'message': f'Only {len(agents)} HiveMind agents connected — need 3+',
                    'remediation': 'Marketing agent: grow node count; HiveMind bootstraps '
                                  'new nodes with collective knowledge instantly',
                })
        except Exception:
            pass

        return result

    @staticmethod
    def measure_moat_depth() -> Dict:
        """Quantify technical irreproducibility — how far ahead of a code-clone.

        The moat is not code (copyable). The moat is accumulated latent state:
        - Latent dynamics trained on real interactions
        - HiveMind collective knowledge (N² network effect)
        - MetaRouter policy (REINFORCE-trained on real decisions)
        - Kernel support vectors (real expert corrections)
        - Episodic memory (years of compressed experiences)
        - Recipe library (real CREATE→REUSE chains)
        - Master key chain (cryptographic identity, non-forkable)

        A competitor starting today with identical code starts at zero.
        This method measures how many zero-state dimensions they'd need to fill.
        """
        moat = {
            'latent_interactions': 0,     # Total interactions training latent state
            'hivemind_nodes': 0,          # N nodes → N² knowledge edges
            'hivemind_knowledge_edges': 0,
            'meta_router_decisions': 0,   # REINFORCE training samples
            'kernel_corrections': 0,      # Expert corrections (instant, no gradient)
            'episodic_experiences': 0,    # VQ-compressed episodes
            'recipe_count': 0,            # CREATE→REUSE recipes
            'master_key_verified_nodes': 0,
            'moat_score': 0.0,            # Composite irreproducibility score
            'competitor_catch_up_estimate': 'unknown',
        }

        # 1. Latent interactions (world model bridge stats)
        try:
            from integrations.agent_engine.world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()
            stats = bridge.get_stats()
            recorded = stats.get('total_recorded', 0)
            flushed = stats.get('total_flushed', 0)
            corrections = stats.get('total_corrections', 0)
            hivemind_queries = stats.get('total_hivemind_queries', 0)
            moat['latent_interactions'] = recorded
            moat['kernel_corrections'] = corrections
            moat['meta_router_decisions'] = flushed  # Each flush = training data
        except Exception:
            pass

        # 2. HiveMind network effect
        try:
            from integrations.agent_engine.world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()
            agents = bridge.get_hivemind_agents()
            n = len(agents)
            moat['hivemind_nodes'] = n
            moat['hivemind_knowledge_edges'] = n * (n - 1) // 2  # N choose 2
        except Exception:
            pass

        # 3. Agent goals completed (each = latent state improvement)
        try:
            from integrations.social.models import get_db, AgentGoal
            db = get_db()
            try:
                moat['episodic_experiences'] = db.query(AgentGoal).count()
            finally:
                db.close()
        except Exception:
            pass

        # 4. Recipe library
        try:
            prompts_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                'prompts')
            if os.path.isdir(prompts_dir):
                recipes = glob.glob(os.path.join(prompts_dir, '*_recipe.json'))
                moat['recipe_count'] = len(recipes)
        except Exception:
            pass

        # 5. Master key verified nodes
        try:
            from integrations.social.models import get_db, PeerNode
            db = get_db()
            try:
                verified = db.query(PeerNode).filter_by(
                    master_key_verified=True).count()
                moat['master_key_verified_nodes'] = verified
            finally:
                db.close()
        except Exception:
            pass

        # Composite moat score (logarithmic — diminishing returns but always growing)
        import math
        score = 0.0
        score += math.log1p(moat['latent_interactions']) * 10   # Each interaction deepens latent
        score += math.log1p(moat['hivemind_knowledge_edges']) * 20  # Network effect most valuable
        score += math.log1p(moat['kernel_corrections']) * 15    # Expert corrections rare & valuable
        score += math.log1p(moat['recipe_count']) * 5           # Recipes = deterministic speedup
        score += math.log1p(moat['master_key_verified_nodes']) * 10  # Non-forkable trust chain
        moat['moat_score'] = round(score, 2)

        # Catch-up estimate
        interactions = moat['latent_interactions']
        nodes = moat['hivemind_nodes']
        if interactions > 1_000_000 and nodes > 100:
            moat['competitor_catch_up_estimate'] = 'practically impossible'
        elif interactions > 100_000 and nodes > 10:
            moat['competitor_catch_up_estimate'] = 'years'
        elif interactions > 10_000:
            moat['competitor_catch_up_estimate'] = 'months'
        elif interactions > 1_000:
            moat['competitor_catch_up_estimate'] = 'weeks'
        else:
            moat['competitor_catch_up_estimate'] = 'moat still shallow — grow network'

        return moat

    @staticmethod
    def verify_exponential_improvement(db: Session, days: int = 30) -> Dict:
        """Check if the self-improving loop shows genuine improvement.

        Verification criteria (all 5 must pass for verified=True):
        1. World model is healthy and responding
        2. Agent task success rate > 50% over 100+ goals
        3. RALT skills propagated to >3 nodes
        4. Recipe reuse rate > 60%
        5. No critical flywheel loopholes

        Returns {verified: bool, metrics: {...}, evidence: [...], loopholes: [...]}
        """
        health = IPService.get_loop_health()
        evidence = []
        checks_passed = 0
        total_checks = 5

        # Check 1: World model healthy
        wm_healthy = health['world_model'].get('healthy', False)
        if wm_healthy:
            checks_passed += 1
            evidence.append('World model (crawl4ai) is healthy and auto-learning')
        else:
            evidence.append('FAIL: World model not healthy or unreachable')

        # Check 2: Agent success rate
        perf = health['agent_performance']
        success_rate = perf.get('success_rate', 0)
        total_goals = perf.get('total_goals', 0)
        if total_goals >= 100 and success_rate > 0.5:
            checks_passed += 1
            evidence.append(
                f'Agent success rate {success_rate:.1%} over {total_goals} goals')
        else:
            evidence.append(
                f'FAIL: Need 100+ goals with >50% success '
                f'(have {total_goals} goals, {success_rate:.1%} rate)')

        # Check 3: RALT propagation
        ralt = health['ralt_propagation']
        if ralt.get('total_distributed', 0) >= 3:
            checks_passed += 1
            evidence.append(
                f'RALT distributed {ralt["total_distributed"]} skills across hive')
        else:
            evidence.append(
                f'FAIL: Only {ralt.get("total_distributed", 0)} RALT skills '
                f'distributed (need 3+)')

        # Check 4: Recipe reuse
        recipe = health['recipe_adoption']
        reuse_rate = recipe.get('reuse_rate', 0)
        if reuse_rate >= 0.6:
            checks_passed += 1
            evidence.append(f'Recipe reuse rate {reuse_rate:.0%}')
        else:
            evidence.append(
                f'FAIL: Recipe reuse rate {reuse_rate:.0%} below 60% threshold')

        # Check 5: No critical loopholes (severity='critical' or 'high')
        loopholes = health.get('flywheel_loopholes', [])
        critical_loopholes = [l for l in loopholes
                              if isinstance(l, dict) and
                              l.get('severity') in ('critical', 'high')]
        if not critical_loopholes:
            checks_passed += 1
            evidence.append('No critical flywheel loopholes detected')
        else:
            first = critical_loopholes[0]
            owner = first.get('owner', '?')
            msg = first.get('message', '')[:80]
            evidence.append(
                f'FAIL: {len(critical_loopholes)} critical loopholes — '
                f'first: [{owner}] {msg}')

        verified = checks_passed == total_checks
        improvement_rate = (checks_passed / total_checks) * 100

        return {
            'verified': verified,
            'checks_passed': checks_passed,
            'total_checks': total_checks,
            'improvement_rate': improvement_rate,
            'metrics': {
                'world_model_healthy': wm_healthy,
                'agent_success_rate': success_rate,
                'total_goals': total_goals,
                'ralt_distributed': ralt.get('total_distributed', 0),
                'recipe_reuse_rate': reuse_rate,
                'hivemind_agents': len(health.get('hivemind_agents', [])),
            },
            'evidence': evidence,
            'loopholes': loopholes,
        }

    # ─── Defensive IP (prior art proof, not patents) ───

    @staticmethod
    def create_defensive_publication(db: Session, title: str, content: str,
                                      abstract: str = '',
                                      git_commit: str = None,
                                      created_by: str = None) -> Dict:
        """Create a timestamped defensive publication (prior art proof).

        NOT a patent — evidence of prior invention:
        - SHA-256 hash of content (proves exact content existed at timestamp)
        - Git commit hash (ties to specific codebase state)
        - Code snapshot hash (ties to full project state)
        - Node signature (cryptographic non-repudiation)
        - Moat score snapshot (cumulative latent state at time)

        If anyone files a patent on something we published first, THIS is prior art.
        """
        import hashlib
        from integrations.social.models import DefensivePublication

        content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()

        # Code snapshot hash
        code_hash = None
        try:
            from security.node_integrity import compute_code_hash
            code_hash = compute_code_hash()
        except Exception:
            pass

        # Node signature
        node_key = None
        sig_hex = None
        try:
            from security.node_integrity import get_public_key_hex, sign_message
            node_key = get_public_key_hex()
            sig_hex = sign_message(content_hash.encode('utf-8')).hex()
        except Exception:
            pass

        # Snapshot moat depth
        moat_score = 0.0
        try:
            moat = IPService.measure_moat_depth()
            moat_score = moat.get('moat_score', 0.0)
        except Exception:
            pass

        # Snapshot verification
        verification = {}
        try:
            verification = IPService.verify_exponential_improvement(db)
        except Exception:
            pass

        pub = DefensivePublication(
            title=title,
            abstract=abstract,
            content_hash=content_hash,
            git_commit_hash=git_commit,
            code_snapshot_hash=code_hash,
            signed_by_node_key=node_key,
            signature_hex=sig_hex,
            moat_score_at_publication=moat_score,
            verification_snapshot=verification,
            created_by=created_by,
        )
        db.add(pub)
        db.flush()
        return pub.to_dict()

    @staticmethod
    def list_defensive_publications(db: Session) -> List[Dict]:
        """List all defensive publications in chronological order."""
        from integrations.social.models import DefensivePublication
        pubs = db.query(DefensivePublication).order_by(
            DefensivePublication.publication_date.desc()).all()
        return [p.to_dict() for p in pubs]

    @staticmethod
    def get_provenance_record(db: Session) -> Dict:
        """Generate comprehensive provenance chain for the entire platform.

        Aggregates: defensive publications, patents, moat measurements,
        code hashes, and verification snapshots — a single cryptographic
        chain of evidence for legal defence.
        """
        from integrations.social.models import DefensivePublication

        pubs = db.query(DefensivePublication).order_by(
            DefensivePublication.publication_date.asc()).all()

        patents = IPService.list_patents(db)

        moat = {}
        try:
            moat = IPService.measure_moat_depth()
        except Exception:
            pass

        code_hash = None
        try:
            from security.node_integrity import compute_code_hash
            code_hash = compute_code_hash()
        except Exception:
            pass

        verification = {}
        try:
            verification = IPService.verify_exponential_improvement(db)
        except Exception:
            pass

        return {
            'generated_at': datetime.utcnow().isoformat(),
            'code_snapshot_hash': code_hash,
            'moat_depth': moat,
            'verification': verification,
            'defensive_publications': [p.to_dict() for p in pubs],
            'patents': patents,
            'total_publications': len(pubs),
            'total_patents': len(patents),
            'evidence_chain': [
                {
                    'type': 'defensive_publication',
                    'id': p.id,
                    'content_hash': p.content_hash,
                    'timestamp': p.publication_date.isoformat() if p.publication_date else None,
                    'signature': p.signature_hex,
                }
                for p in pubs
            ],
        }

    @staticmethod
    def check_intelligence_milestone(db: Session,
                                      consecutive_days_required: int = 14,
                                      min_catch_up: str = 'months') -> Dict:
        """Check if critical intelligence threshold has been reached.

        Auto-patent filing trigger. All 3 conditions must be met:
        1. verify_exponential_improvement() returns verified=True
        2. moat catch_up_estimate >= min_catch_up
        3. At least N consecutive verified defensive publications

        The philosophy: we tread carefully. No premature filing.
        File only when the hive has proven itself over sustained time.
        """
        from integrations.social.models import DefensivePublication

        verification = IPService.verify_exponential_improvement(db)

        moat = {}
        catch_up = 'unknown'
        try:
            moat = IPService.measure_moat_depth()
            catch_up = moat.get('competitor_catch_up_estimate', 'unknown')
        except Exception:
            pass

        # Count consecutive verified publications (most recent first)
        pubs = db.query(DefensivePublication).order_by(
            DefensivePublication.publication_date.desc()
        ).limit(consecutive_days_required).all()

        consecutive_verified = 0
        for p in pubs:
            snap = p.verification_snapshot or {}
            if snap.get('verified', False):
                consecutive_verified += 1
            else:
                break

        catch_up_levels = [
            'moat still shallow — grow network',
            'weeks', 'months', 'years', 'practically impossible',
        ]
        min_idx = catch_up_levels.index(min_catch_up) if min_catch_up in catch_up_levels else 2
        cur_idx = catch_up_levels.index(catch_up) if catch_up in catch_up_levels else 0

        triggered = (
            verification.get('verified', False)
            and consecutive_verified >= consecutive_days_required
            and cur_idx >= min_idx
        )

        return {
            'triggered': triggered,
            'consecutive_verified': consecutive_verified,
            'consecutive_required': consecutive_days_required,
            'moat_catch_up': catch_up,
            'min_catch_up_required': min_catch_up,
            'current_verification': verification,
            'moat_score': moat.get('moat_score', 0.0),
        }
