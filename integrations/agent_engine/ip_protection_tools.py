"""
Unified Agent Goal Engine - IP Protection Tools (Tier 2)

Loaded ONLY when the agent is working on an ip_protection goal.
Follows exact same pattern as marketing_tools.py.

Tier 1 (Default): google_search, text_2_image, delegate_to_specialist, etc.
Tier 2 (Category): verify_loop, draft_claims, draft_patent, check_prior_art,
                    monitor_infringement, generate_cease_desist, get_loop_health
Tier 3 (Runtime): delegate_to_specialist finds agents with needed skills via A2A
"""
import json
import logging
from typing import Annotated, Optional

logger = logging.getLogger('hevolve_social')


def register_ip_protection_tools(helper, assistant, user_id: str):
    """Register IP protection tools with the agent (Tier 2).

    These wrap IPService + world model bridge — no new logic, just tool
    interfaces that let the agent verify the flywheel, draft patents,
    check prior art, and monitor infringement.

    Args:
        helper: AutoGen helper agent (registers for LLM)
        assistant: AutoGen assistant agent (registers for execution)
        user_id: Current user ID for ownership
    """

    def verify_self_improvement_loop(
        days: Annotated[int, "Number of days to analyze (default 30)"] = 30,
    ) -> str:
        """Verify the self-improving loop is working: world model health,
        agent success rates, RALT propagation, recipe reuse, HiveMind agents.
        Returns verification status with evidence and detected loopholes."""
        try:
            from integrations.social.models import get_db
            from .ip_service import IPService
            db = get_db()
            try:
                result = IPService.verify_exponential_improvement(db, days=days)
                return json.dumps(result)
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def draft_patent_claims(
        invention_title: Annotated[str, "Title of the invention"],
        claim_areas: Annotated[str, "Comma-separated claim areas: hive_distributed_inference,self_improving_loop,ralt_skill_propagation,recipe_pattern,guardian_angel_architecture"],
        evidence_summary: Annotated[str, "Summary of evidence supporting novelty"],
    ) -> str:
        """Draft formal patent claims in USPTO format. Saves as IPPatent
        with status='draft'. Returns the created patent record."""
        try:
            from integrations.social.models import get_db
            from .ip_service import IPService
            db = get_db()
            try:
                areas = [a.strip() for a in claim_areas.split(',')]
                claims = []
                for i, area in enumerate(areas, 1):
                    claims.append({
                        'claim_number': i,
                        'type': 'independent',
                        'area': area,
                        'text': f'[DRAFT] A method and system for {area.replace("_", " ")}...',
                    })
                result = IPService.create_patent(
                    db,
                    title=invention_title,
                    claims=claims,
                    abstract=f'Patent application for: {invention_title}',
                    description=evidence_summary,
                    filing_type='provisional',
                    evidence=[{'type': 'verification', 'summary': evidence_summary}],
                    created_by=str(user_id),
                )
                db.commit()
                return json.dumps({'success': True, 'patent': result})
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def draft_provisional_patent(
        patent_id: Annotated[str, "ID of existing draft patent to build into provisional"],
        inventors: Annotated[str, "Comma-separated inventor names"],
        assignee: Annotated[str, "Assignee organization name"] = 'Hevolve AI',
    ) -> str:
        """Build complete USPTO provisional patent application from a draft.
        Updates the patent with full provisional structure."""
        try:
            from integrations.social.models import get_db
            from .ip_service import IPService
            db = get_db()
            try:
                patent = IPService.get_patent(db, patent_id)
                if not patent:
                    return json.dumps({'success': False, 'error': 'Patent not found'})

                # Capture loop health as verification evidence
                health = IPService.get_loop_health()
                verification = IPService.verify_exponential_improvement(db)

                updated = IPService.update_patent_status(
                    db, patent_id, 'provisional')
                if updated:
                    from integrations.social.models import IPPatent
                    p = db.query(IPPatent).filter_by(id=patent_id).first()
                    if p:
                        p.verification_metrics = {
                            'loop_health': health,
                            'verification': verification,
                            'inventors': [i.strip() for i in inventors.split(',')],
                            'assignee': assignee,
                        }
                        db.flush()
                        updated = p.to_dict()
                db.commit()
                return json.dumps({'success': True, 'patent': updated})
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def check_prior_art(
        claim_text: Annotated[str, "The patent claim text to check for novelty"],
        search_scope: Annotated[str, "Search scope: domestic|worldwide"] = 'worldwide',
    ) -> str:
        """Search for prior art using HiveMind collective thinking.
        Queries the hive for distributed knowledge about similar patents
        and architectures. Returns novelty assessment."""
        try:
            # Use HiveMind for distributed prior art analysis
            from integrations.agent_engine.world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()

            collective = bridge.query_hivemind(
                f"Assess novelty of this patent claim against known distributed "
                f"computing and AI architectures. Scope: {search_scope}. "
                f"Claim: {claim_text[:1000]}",
                timeout_ms=5000,
            )

            result = {
                'claim_text': claim_text[:200],
                'search_scope': search_scope,
                'hivemind_analysis': collective,
                'note': 'HiveMind-assisted analysis. Manual USPTO/WIPO search '
                        'recommended for formal filing.',
            }
            return json.dumps(result)
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def monitor_infringement(
        architecture_fingerprint: Annotated[str, "Description of the architecture to protect"],
        scan_targets: Annotated[str, "Comma-separated scan targets: github.com,arxiv.org,huggingface.co"] = 'github.com,arxiv.org',
    ) -> str:
        """Scan for potential infringement of the hive architecture patents.
        Uses HiveMind for distributed scanning across connected agents.
        Returns matches with risk levels."""
        try:
            from integrations.agent_engine.world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()

            targets = [t.strip() for t in scan_targets.split(',')]
            collective = bridge.query_hivemind(
                f"Scan for similar architectures to: {architecture_fingerprint[:500]}. "
                f"Focus on: {', '.join(targets)}. "
                f"Look for: distributed hive compute using user machines, "
                f"self-improving agent loops, RALT-like skill propagation, "
                f"recipe-based agent execution patterns.",
                timeout_ms=10000,
            )

            result = {
                'architecture_fingerprint': architecture_fingerprint[:200],
                'scan_targets': targets,
                'hivemind_analysis': collective,
                'note': 'Automated scan — results require human review before '
                        'legal action.',
            }
            return json.dumps(result)
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def generate_cease_desist(
        infringer_name: Annotated[str, "Name of the infringing party"],
        infringer_url: Annotated[str, "URL of the infringing product/service"],
        patent_id: Annotated[str, "Patent ID being infringed"],
        evidence: Annotated[str, "Description of infringement evidence"],
    ) -> str:
        """Generate a cease-and-desist notice and record the infringement.
        Creates an IPInfringement record linked to the patent."""
        try:
            from integrations.social.models import get_db
            from .ip_service import IPService
            db = get_db()
            try:
                # Record infringement
                infringement = IPService.create_infringement(
                    db,
                    patent_id=patent_id,
                    infringer_name=infringer_name,
                    infringer_url=infringer_url,
                    evidence_summary=evidence,
                    risk_level='high',
                )

                # Get patent details for the notice
                patent = IPService.get_patent(db, patent_id)
                patent_title = patent['title'] if patent else 'Unknown'

                notice_text = (
                    f"CEASE AND DESIST NOTICE\n\n"
                    f"To: {infringer_name}\n"
                    f"Re: Infringement of Patent Application: {patent_title}\n\n"
                    f"We have identified that your product/service at {infringer_url} "
                    f"infringes upon our patent claims covering:\n"
                    f"- Distributed hive compute using user machines\n"
                    f"- Self-improving agent loop architecture\n"
                    f"- Reality-Anchored Latent Transfer (RALT) skill propagation\n\n"
                    f"Evidence: {evidence}\n\n"
                    f"We demand that you immediately cease and desist from all "
                    f"activities that infringe upon our intellectual property rights.\n\n"
                    f"[DRAFT — Requires legal review before sending]\n"
                )

                # Update with notice
                IPService.update_infringement_status(
                    db, infringement['id'], 'reviewed',
                    notice_type='cease_desist',
                    notice_text=notice_text,
                )

                db.commit()
                return json.dumps({
                    'success': True,
                    'infringement': infringement,
                    'notice_text': notice_text,
                    'warning': 'DRAFT notice — requires legal counsel review '
                               'before sending.',
                })
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def get_loop_health() -> str:
        """Get real-time dashboard of the self-improving loop:
        world model stats, agent performance, RALT propagation,
        recipe adoption, HiveMind agents, and detected flywheel loopholes."""
        try:
            from .ip_service import IPService
            health = IPService.get_loop_health()
            return json.dumps(health)
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def measure_moat() -> str:
        """Measure technical irreproducibility — how far ahead of a code clone.
        The moat is accumulated latent state, not code:
        - Latent dynamics trained on real interactions (uncopyable)
        - HiveMind N² network effect (more nodes = exponentially more knowledge)
        - MetaRouter REINFORCE policy (trained on real decisions)
        - Kernel support vectors (real expert corrections)
        - Episodic memory (years of compressed experiences)
        - Master key chain (cryptographic, non-forkable)
        Returns moat score and competitor catch-up estimate."""
        try:
            from .ip_service import IPService
            return json.dumps(IPService.measure_moat_depth())
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    # Register all IP protection tools
    tools = [
        ('verify_self_improvement_loop',
         'Verify the self-improving loop is working with evidence and loophole detection',
         verify_self_improvement_loop),
        ('draft_patent_claims',
         'Draft formal patent claims in USPTO format for the hive architecture',
         draft_patent_claims),
        ('draft_provisional_patent',
         'Build complete USPTO provisional patent from existing draft',
         draft_provisional_patent),
        ('check_prior_art',
         'Search for prior art using HiveMind collective intelligence',
         check_prior_art),
        ('monitor_infringement',
         'Scan for infringement of hive architecture patents',
         monitor_infringement),
        ('generate_cease_desist',
         'Generate cease-and-desist notice for detected infringement',
         generate_cease_desist),
        ('get_loop_health',
         'Get real-time self-improving loop health dashboard with loopholes',
         get_loop_health),
        ('measure_moat',
         'Measure technical irreproducibility — latent dynamics moat depth vs code clone',
         measure_moat),
    ]

    for name, desc, func in tools:
        helper.register_for_llm(name=name, description=desc)(func)
        assistant.register_for_execution(name=name)(func)

    logger.info(f"Registered {len(tools)} IP protection tools for user {user_id}")

    # Register skills for A2A discovery
    try:
        from integrations.internal_comm.internal_agent_communication import register_agent_with_skills
        register_agent_with_skills(f"ip_protection_{user_id}", [
            {'name': 'patent_filing', 'description': 'Draft and file patent applications', 'proficiency': 0.9},
            {'name': 'prior_art_search', 'description': 'Search for prior art and assess novelty', 'proficiency': 0.8},
            {'name': 'infringement_detection', 'description': 'Monitor for IP infringement', 'proficiency': 0.8},
            {'name': 'loop_verification', 'description': 'Verify self-improving loop health', 'proficiency': 0.95},
        ])
    except Exception as e:
        logger.debug(f"IP protection skill registration skipped: {e}")
