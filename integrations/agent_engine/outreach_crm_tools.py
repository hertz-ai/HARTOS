"""
Unified Agent Goal Engine - Outreach CRM Tools (Tier 2)

These tools are loaded ONLY when the agent is working on an 'outreach' goal.
They connect HARTOS agents to Erxes CRM for:
  - Lead/contact management
  - Deal pipeline tracking
  - Automated email outreach via cortext@hertzai.com
  - Follow-up sequence automation
  - Reply detection and pipeline stage updates

Integrates with:
  - Erxes GraphQL API (http://192.168.0.9:3300)
  - HertzAI email service (http://192.168.0.9:4000/sendEmail)

Tier 1 (Default): google_search, text_2_image, delegate_to_specialist, etc.
Tier 2 (Category): create_lead, send_outreach, check_replies, move_deal_stage
Tier 3 (Runtime): delegate_to_specialist for channel-specific follow-ups
"""
import html
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Annotated, Optional, Dict, List

logger = logging.getLogger('hevolve_outreach')

# ── Configuration ──
ERXES_API_URL = os.environ.get('ERXES_API_URL', 'http://localhost:3300')
EMAIL_SERVICE_URL = os.environ.get('EMAIL_SERVICE_URL', 'http://localhost:4000')

# ── Thread lock for prospect file access (daemon + request thread) ──
_prospect_lock = threading.Lock()

# ── Local prospect store (file-backed, works without Erxes) ──
_PROSPECT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '..', '..', 'agent_data', 'outreach_prospects.json'
)


def _load_prospects() -> Dict:
    """Load prospect data from local JSON store. Thread-safe."""
    with _prospect_lock:
        try:
            with open(_PROSPECT_FILE, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {'prospects': {}, 'sequences': {}, 'sent_log': []}


def _save_prospects(data: Dict):
    """Save prospect data to local JSON store. Thread-safe."""
    with _prospect_lock:
        os.makedirs(os.path.dirname(_PROSPECT_FILE), exist_ok=True)
        with open(_PROSPECT_FILE, 'w') as f:
            json.dump(data, f, indent=2, default=str)


def _get_erxes():
    """Get the native Erxes CRM client (singleton). Returns None if unavailable."""
    try:
        from integrations.agent_engine.erxes_client import get_erxes_client
        return get_erxes_client()
    except Exception as e:
        logger.debug(f"Erxes client unavailable: {e}")
        return None


def _sync_prospect_to_crm(prospect: Dict) -> Dict:
    """Sync a prospect to Erxes CRM. Returns sync result with IDs."""
    erxes = _get_erxes()
    if not erxes:
        return {'synced': False, 'reason': 'erxes_unavailable'}
    try:
        result = erxes.sync_prospect_to_erxes(prospect)
        if result.get('synced'):
            logger.info(f"CRM synced: {prospect.get('company')} "
                        f"(customer={result.get('erxes_customer_id')}, "
                        f"deal={result.get('erxes_deal_id')})")
        return result
    except Exception as e:
        logger.debug(f"CRM sync failed: {e}")
        return {'synced': False, 'error': str(e)}


def _sync_stage_change(prospect: Dict, new_stage: str):
    """Sync a stage change to Erxes deal pipeline."""
    erxes = _get_erxes()
    if not erxes:
        return
    try:
        erxes.sync_stage_change(prospect, new_stage)
    except Exception as e:
        logger.debug(f"CRM stage sync failed: {e}")


def _sanitize_html(html_body: str) -> str:
    """Strip dangerous HTML tags (script, iframe, object) from email body."""
    if not html_body:
        return ''
    cleaned = re.sub(r'<(script|iframe|object|embed|form|input)[^>]*>.*?</\1>', '', html_body, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r'\s+on\w+\s*=\s*["\'][^"\']*["\']', '', cleaned, flags=re.IGNORECASE)
    return cleaned


def _send_email(to_email: str, subject: str, html_body: str) -> Dict:
    """Send email via the channel system (user's configured SMTP).

    Tries the email channel adapter first (user's own SMTP config).
    Falls back to the HertzAI mailer service if no email channel is configured.
    """
    sanitized_body = _sanitize_html(html_body)
    # Strip HTML for plain-text fallback
    plain_text = re.sub(r'<[^>]+>', '', sanitized_body).strip()

    # Tier 1: Email channel adapter (user's SMTP)
    try:
        from integrations.channels.registry import get_registry
        import asyncio
        registry = get_registry()
        adapter = registry.get_adapter('email')
        if adapter and hasattr(adapter, 'send_email'):
            loop = getattr(registry, '_loop', None)
            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    adapter.send_email(
                        to=to_email, subject=subject,
                        body=plain_text, html_body=sanitized_body,
                    ),
                    loop,
                )
                result = future.result(timeout=30)
                return {'success': result.success, 'via': 'email_channel',
                        'error': result.error if not result.success else None}
    except Exception as e:
        logger.debug(f"Email channel not available: {e}")

    # Tier 2: HertzAI mailer service (fallback)
    try:
        from core.http_pool import pooled_post
        resp = pooled_post(
            f'{EMAIL_SERVICE_URL}/sendEmail',
            json={
                'emailList': [to_email] if isinstance(to_email, str) else to_email,
                'subject': subject,
                'message': sanitized_body,
            },
            headers={'Content-Type': 'application/json'},
            timeout=15,
        )
        try:
            body = resp.json()
        except Exception:
            body = {'raw': resp.text[:200]}
        return {'success': resp.status_code < 400, 'via': 'mailer_service',
                'status_code': resp.status_code, 'response': body}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def register_outreach_tools(helper, assistant, user_id: str):
    """Register outreach CRM tools with the agent (Tier 2).

    Args:
        helper: AutoGen helper agent (registers for LLM)
        assistant: AutoGen assistant agent (registers for execution)
        user_id: Current user ID for ownership
    """

    def create_prospect(
        company: Annotated[str, "Company name"],
        contact_name: Annotated[str, "Contact person's name"],
        email: Annotated[str, "Contact email address"],
        title: Annotated[Optional[str], "Contact's job title"] = None,
        vertical: Annotated[Optional[str], "Industry vertical (humanoid|healthcare|industrial|cobot)"] = None,
        notes: Annotated[Optional[str], "Additional notes about the prospect"] = None,
        tier: Annotated[int, "Prospect tier (1=top priority, 2=secondary)"] = 1,
    ) -> str:
        """Create a new prospect in the outreach CRM.

        Stores in local JSON and syncs to Erxes if available.
        """
        data = _load_prospects()
        prospect_id = f"{company.lower().replace(' ', '_')}_{int(time.time())}"

        prospect = {
            'id': prospect_id,
            'company': company,
            'contact_name': contact_name,
            'email': email,
            'title': title or '',
            'vertical': vertical or 'general',
            'notes': notes or '',
            'tier': tier,
            'stage': 'new',  # new → contacted → replied → meeting → negotiation → won/lost
            'created_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat(),
            'created_by': user_id,
            'emails_sent': 0,
            'last_email_at': None,
            'last_reply_at': None,
        }

        data['prospects'][prospect_id] = prospect
        _save_prospects(data)

        # Sync to Erxes CRM (native client)
        crm_result = _sync_prospect_to_crm(prospect)
        if crm_result.get('synced'):
            prospect['erxes_customer_id'] = crm_result.get('erxes_customer_id')
            prospect['erxes_deal_id'] = crm_result.get('erxes_deal_id')
            data['prospects'][prospect_id] = prospect
            _save_prospects(data)

        return json.dumps({'success': True, 'prospect': prospect, 'crm_sync': crm_result})

    def send_outreach_email(
        prospect_id: Annotated[str, "Prospect ID to send email to"],
        subject: Annotated[str, "Email subject line"],
        body_html: Annotated[str, "Email body in HTML format"],
        sequence_step: Annotated[int, "Sequence step number (1=initial, 2=first follow-up, etc.)"] = 1,
    ) -> str:
        """Send an outreach email to a prospect and log it.

        Uses the HertzAI email service (cortext@hertzai.com).
        Updates prospect stage to 'contacted' on first email.
        """
        data = _load_prospects()
        prospect = data['prospects'].get(prospect_id)
        if not prospect:
            return json.dumps({'success': False, 'error': f'Prospect {prospect_id} not found'})

        # Send via HertzAI
        result = _send_email(
            to_email=prospect['email'],
            subject=subject,
            html_body=body_html,
        )

        # Log the send
        send_entry = {
            'prospect_id': prospect_id,
            'to': prospect['email'],
            'subject': subject,
            'sequence_step': sequence_step,
            'sent_at': datetime.utcnow().isoformat(),
            'send_result': result,
        }
        data['sent_log'].append(send_entry)

        # Update prospect state
        prospect['emails_sent'] = prospect.get('emails_sent', 0) + 1
        prospect['last_email_at'] = datetime.utcnow().isoformat()
        if prospect['stage'] == 'new':
            prospect['stage'] = 'contacted'
        prospect['updated_at'] = datetime.utcnow().isoformat()

        _save_prospects(data)
        return json.dumps({'success': True, 'send_result': result, 'prospect_stage': prospect['stage']})

    def create_followup_sequence(
        prospect_id: Annotated[str, "Prospect ID to create sequence for"],
        sequence_name: Annotated[str, "Name for this follow-up sequence"],
        steps: Annotated[str, "JSON array of sequence steps, each with: delay_days, subject, body_html"],
    ) -> str:
        """Create an automated follow-up sequence for a prospect.

        Each step has a delay (days after previous step), subject, and body.
        The agent daemon will check and execute pending steps daily.

        Example steps JSON:
        [
            {"delay_days": 3, "subject": "following up on HARTOS", "body_html": "<p>Hi...</p>"},
            {"delay_days": 5, "subject": "quick update", "body_html": "<p>Hey...</p>"},
            {"delay_days": 7, "subject": "last note", "body_html": "<p>Just wanted...</p>"}
        ]
        """
        data = _load_prospects()
        prospect = data['prospects'].get(prospect_id)
        if not prospect:
            return json.dumps({'success': False, 'error': f'Prospect {prospect_id} not found'})

        try:
            step_list = json.loads(steps) if isinstance(steps, str) else steps
        except json.JSONDecodeError:
            return json.dumps({'success': False, 'error': 'Invalid JSON for steps'})

        sequence_id = f"seq_{prospect_id}_{int(time.time())}"

        # Calculate scheduled dates from now
        base_time = datetime.utcnow()
        scheduled_steps = []
        cumulative_days = 0
        for i, step in enumerate(step_list):
            cumulative_days += step.get('delay_days', 3)
            scheduled_steps.append({
                'step_number': i + 2,  # step 1 was the initial outreach
                'delay_days': step['delay_days'],
                'scheduled_at': (base_time + timedelta(days=cumulative_days)).isoformat(),
                'subject': step['subject'],
                'body_html': step['body_html'],
                'status': 'pending',  # pending → sent → skipped (if replied)
                'sent_at': None,
            })

        sequence = {
            'id': sequence_id,
            'prospect_id': prospect_id,
            'name': sequence_name,
            'steps': scheduled_steps,
            'created_at': datetime.utcnow().isoformat(),
            'status': 'active',  # active → paused → completed
            'exit_on_reply': True,
        }

        data['sequences'][sequence_id] = sequence
        _save_prospects(data)

        return json.dumps({'success': True, 'sequence': sequence})

    def check_pending_followups() -> str:
        """Check all active sequences for follow-ups that are due.

        Sends emails for due steps, skips sequences where the prospect has replied.
        Returns summary of actions taken.
        """
        return _check_pending_followups_impl()

    def move_prospect_stage(
        prospect_id: Annotated[str, "Prospect ID"],
        new_stage: Annotated[str, "New stage: new|contacted|replied|meeting|negotiation|won|lost"],
        notes: Annotated[Optional[str], "Notes about the stage change"] = None,
    ) -> str:
        """Move a prospect to a new pipeline stage."""
        valid_stages = ['new', 'contacted', 'replied', 'meeting', 'negotiation', 'won', 'lost']
        if new_stage not in valid_stages:
            return json.dumps({'success': False, 'error': f'Invalid stage. Use: {valid_stages}'})

        data = _load_prospects()
        prospect = data['prospects'].get(prospect_id)
        if not prospect:
            return json.dumps({'success': False, 'error': f'Prospect {prospect_id} not found'})

        old_stage = prospect['stage']
        prospect['stage'] = new_stage
        prospect['updated_at'] = datetime.utcnow().isoformat()
        if notes:
            prospect['notes'] = prospect.get('notes', '') + f'\n[{datetime.utcnow().isoformat()}] {old_stage}→{new_stage}: {notes}'

        # If prospect replied, mark it
        if new_stage == 'replied':
            prospect['last_reply_at'] = datetime.utcnow().isoformat()

        _save_prospects(data)

        # Sync stage change to Erxes CRM
        _sync_stage_change(prospect, new_stage)

        return json.dumps({'success': True, 'prospect': prospect, 'transition': f'{old_stage}→{new_stage}'})

    def get_pipeline_status() -> str:
        """Get the full outreach pipeline status — all prospects grouped by stage.

        Merges local prospect data with Erxes CRM pipeline view.
        """
        data = _load_prospects()
        pipeline = {}
        for pid, prospect in data.get('prospects', {}).items():
            stage = prospect.get('stage', 'new')
            if stage not in pipeline:
                pipeline[stage] = []
            pipeline[stage].append({
                'id': pid,
                'company': prospect['company'],
                'contact': prospect['contact_name'],
                'email': prospect['email'],
                'emails_sent': prospect.get('emails_sent', 0),
                'last_email': prospect.get('last_email_at'),
                'tier': prospect.get('tier', 1),
                'erxes_deal_id': prospect.get('erxes_deal_id'),
            })

        # Count active sequences
        active_sequences = sum(
            1 for s in data.get('sequences', {}).values()
            if s.get('status') == 'active'
        )

        # Include Erxes CRM status if available
        erxes_status = None
        erxes = _get_erxes()
        if erxes:
            try:
                erxes_status = erxes.get_pipeline_status()
            except Exception as e:
                erxes_status = {'error': str(e)}

        return json.dumps({
            'success': True,
            'pipeline': pipeline,
            'total_prospects': len(data.get('prospects', {})),
            'active_sequences': active_sequences,
            'erxes_pipeline': erxes_status,
        })

    def list_sent_emails(
        prospect_id: Annotated[Optional[str], "Filter by prospect ID (optional)"] = None,
        limit: Annotated[int, "Max emails to return"] = 20,
    ) -> str:
        """List sent outreach emails, optionally filtered by prospect."""
        data = _load_prospects()
        log = data.get('sent_log', [])

        if prospect_id:
            log = [e for e in log if e.get('prospect_id') == prospect_id]

        # Most recent first
        log = sorted(log, key=lambda x: x.get('sent_at', ''), reverse=True)[:limit]
        return json.dumps({'success': True, 'emails': log, 'total': len(log)})

    # ── Register all tools ──
    from autogen import register_function

    for func in [
        create_prospect,
        send_outreach_email,
        create_followup_sequence,
        check_pending_followups,
        move_prospect_stage,
        get_pipeline_status,
        list_sent_emails,
    ]:
        register_function(
            func,
            caller=helper,
            executor=assistant,
            description=func.__doc__,
        )

    logger.info(f"Registered 7 outreach CRM tools for user {user_id}")


def _check_pending_followups_impl() -> str:
    """Shared logic for checking pending follow-ups. Caller must hold _prospect_lock.

    Returns JSON string with actions taken. Used by both the tool-level
    check_pending_followups and the daemon-level check_pending_followups_daemon.
    """
    data = _load_prospects()
    now = datetime.utcnow()
    actions_taken = []

    for seq_id, sequence in data.get('sequences', {}).items():
        if sequence['status'] != 'active':
            continue

        prospect = data['prospects'].get(sequence['prospect_id'])
        if not prospect:
            continue

        # Exit condition: prospect already replied
        if sequence.get('exit_on_reply') and prospect.get('last_reply_at'):
            sequence['status'] = 'completed'
            actions_taken.append({
                'action': 'sequence_completed',
                'reason': 'prospect_replied',
                'prospect': prospect['company'],
            })
            continue

        # Check each pending step
        for step in sequence['steps']:
            if step['status'] != 'pending':
                continue

            scheduled = datetime.fromisoformat(step['scheduled_at'])
            if now >= scheduled:
                result = _send_email(
                    to_email=prospect['email'],
                    subject=step['subject'],
                    html_body=step['body_html'],
                )
                step['status'] = 'sent'
                step['sent_at'] = now.isoformat()

                prospect['emails_sent'] = prospect.get('emails_sent', 0) + 1
                prospect['last_email_at'] = now.isoformat()

                actions_taken.append({
                    'action': 'followup_sent',
                    'prospect': prospect['company'],
                    'step': step['step_number'],
                    'subject': step['subject'],
                    'result': result,
                })
                break  # Only send one step per check cycle

        # Check if all steps are done
        if all(s['status'] != 'pending' for s in sequence['steps']):
            sequence['status'] = 'completed'

    _save_prospects(data)
    return json.dumps({'success': True, 'actions': actions_taken, 'checked_at': now.isoformat()})


# ── Goal Type Registration ──

def build_outreach_prompt(goal_dict: Dict, product_dict: Dict = None) -> str:
    """Build the prompt for an outreach goal.

    This prompt is sent to /chat → CREATE/REUSE pipeline.
    """
    title = goal_dict.get('title', 'Sales Outreach')
    description = goal_dict.get('description', '')
    config = goal_dict.get('config', {})

    prospects_summary = ''
    data = _load_prospects()
    if data['prospects']:
        lines = []
        for pid, p in data['prospects'].items():
            lines.append(f"- {p['company']} ({p['contact_name']}, {p['email']}) — stage: {p['stage']}, emails sent: {p.get('emails_sent', 0)}")
        prospects_summary = '\n'.join(lines)

    return f"""You are a B2B sales outreach agent for HevolveAI.

PRODUCT: HARTOS (Hevolve Hive Agentic Runtime OS)
- On-device AI runtime for robotics: LLM inference, vision, speech, semantic memory, multi-agent orchestration
- Democratically evolving Hive network (not controlled by any single LLM vendor)
- Flywheel: more robots deployed → more data → better model → more developers → more robots
- Early partners shape how the intelligence evolves for their vertical

GOAL: {title}
{description}

CURRENT PIPELINE:
{prospects_summary or '(No prospects yet — use create_prospect to add them)'}

RULES:
1. Sound human. No em dashes. Casual lowercase subject lines. Short sentences.
2. Create FOMO: one partner per vertical, Q2 deadline, name competitors who locked into Big Tech
3. Follow-up sequence: day 3, day 7, day 14 — each shorter and more direct
4. If a prospect replies, move them to 'replied' stage and stop the sequence
5. Use the outreach CRM tools to track everything

CONFIG: {json.dumps(config)}
"""


def register_outreach_goal_type():
    """Register 'outreach' as a goal type in the agent engine.

    Call this during HARTOS boot to enable outreach automation.
    """
    from integrations.agent_engine.goal_manager import register_goal_type
    register_goal_type(
        goal_type='outreach',
        build_prompt=build_outreach_prompt,
        tool_tags=['outreach', 'email', 'crm'],
    )
    logger.info("Registered 'outreach' goal type with prompt builder and tool tags")
    # Wire reply detection into email channel
    register_reply_handler()


def check_pending_followups_daemon() -> dict:
    """Module-level follow-up checker called by agent_daemon on each tick.

    Delegates to the tool-level check_pending_followups logic (DRY).
    Returns dict with 'sent' count.
    """
    result_json = _check_pending_followups_impl()
    result = json.loads(result_json)
    sent = len([a for a in result.get('actions', []) if a.get('action') == 'followup_sent'])
    for action in result.get('actions', []):
        if action.get('action') == 'followup_sent':
            logger.info(f"Follow-up sent: {action.get('prospect')} step {action.get('step')} subject='{action.get('subject')}'")
        elif action.get('action') == 'sequence_completed':
            logger.info(f"Sequence completed: {action.get('prospect')} ({action.get('reason')})")
    return {'sent': sent, 'checked_at': result.get('checked_at', '')}


# ═══════════════════════════════════════════════════════════════
# Inbound Reply Handler — wired into email channel adapter
# ═══════════════════════════════════════════════════════════════

def handle_inbound_email(sender_email: str, subject: str, body: str, message_id: str = '') -> Optional[Dict]:
    """Check if an inbound email matches a prospect. If so, update CRM and trigger response flow.

    Called by the email channel adapter's _process_email hook.
    Returns match info if this is a prospect reply, None otherwise.
    """
    data = _load_prospects()

    # Match sender to any prospect by email
    matched_prospect = None
    for pid, prospect in data.get('prospects', {}).items():
        if prospect.get('email', '').lower() == sender_email.lower():
            matched_prospect = prospect
            break

    if not matched_prospect:
        return None  # Not a prospect — normal email flow

    # ── Update prospect state ──
    matched_prospect['stage'] = 'replied'
    matched_prospect['last_reply_at'] = datetime.utcnow().isoformat()
    matched_prospect['updated_at'] = datetime.utcnow().isoformat()
    matched_prospect['notes'] = matched_prospect.get('notes', '') + (
        f"\n[{datetime.utcnow().isoformat()}] REPLY received: {subject}"
    )

    # ── Pause all active sequences for this prospect ──
    for seq_id, sequence in data.get('sequences', {}).items():
        if sequence.get('prospect_id') == matched_prospect['id'] and sequence['status'] == 'active':
            sequence['status'] = 'completed'
            logger.info(f"Sequence {seq_id} auto-completed: prospect {matched_prospect['company']} replied")

    _save_prospects(data)

    # ── Sync stage change to Erxes CRM ──
    _sync_stage_change(matched_prospect, 'replied')

    # ── Build context for the agent to draft a response ──
    # Gather full conversation history
    sent_emails = [
        e for e in data.get('sent_log', [])
        if e.get('prospect_id') == matched_prospect['id']
    ]
    sent_emails.sort(key=lambda x: x.get('sent_at', ''))

    context = {
        'prospect': matched_prospect,
        'their_reply': {'subject': subject, 'body': body[:2000], 'message_id': message_id},
        'our_emails': [
            {'subject': e.get('subject', ''), 'sent_at': e.get('sent_at', ''), 'step': e.get('sequence_step', 1)}
            for e in sent_emails[-5:]  # Last 5 emails we sent
        ],
        'campaign_stage': matched_prospect.get('stage', 'replied'),
        'total_emails_sent': matched_prospect.get('emails_sent', 0),
    }

    # ── Push notification to user ──
    _notify_prospect_replied(matched_prospect, subject, body, context)

    # ── Dispatch agent to draft response ──
    _dispatch_response_draft(matched_prospect, context)

    logger.info(
        f"Prospect reply detected: {matched_prospect['company']} ({sender_email}) "
        f"subject='{subject}' — stage moved to 'replied', sequences paused"
    )
    return context


def _notify_prospect_replied(prospect: Dict, subject: str, body: str, context: Dict):
    """Push notification to user that a prospect replied.

    Uses EventBus → Nunba notification channel.
    """
    try:
        from core.platform.events import emit_event
        emit_event('outreach.prospect_replied', {
            'prospect_id': prospect['id'],
            'company': prospect['company'],
            'contact': prospect['contact_name'],
            'email': prospect['email'],
            'subject': subject,
            'body_preview': body[:200],
            'stage': prospect['stage'],
            'emails_sent': prospect.get('emails_sent', 0),
        })
    except Exception as e:
        logger.debug(f"EventBus notification failed: {e}")

    # Also try direct Nunba push via channel system
    try:
        from integrations.channels.agent_tools import _get_user_id_from_threadlocal
        from integrations.channels.response.router import get_response_router
        user_id = prospect.get('created_by') or _get_user_id_from_threadlocal()
        router = get_response_router()
        notification = (
            f"📬 {prospect['company']} replied to your outreach!\n"
            f"From: {prospect['contact_name']} ({prospect['email']})\n"
            f"Subject: {subject}\n"
            f"Preview: {body[:150]}..."
        )
        router.route_response(user_id=user_id, response_text=notification, fan_out=True)
    except Exception as e:
        logger.debug(f"Push notification failed: {e}")


def _dispatch_response_draft(prospect: Dict, context: Dict):
    """Dispatch an agent task to draft a response to the prospect's reply.

    Uses the existing dispatch system — creates a goal that runs through
    the CREATE/REUSE pipeline with full conversation context.
    """
    try:
        from integrations.agent_engine.dispatch import dispatch_goal
        user_id = prospect.get('created_by', 'system')

        prompt = (
            f"A prospect has replied to our outreach. Draft a response.\n\n"
            f"PROSPECT: {prospect['company']} — {prospect['contact_name']} ({prospect['email']})\n"
            f"STAGE: {prospect.get('stage', 'replied')}\n"
            f"THEIR REPLY:\nSubject: {context['their_reply']['subject']}\n"
            f"Body: {context['their_reply']['body']}\n\n"
            f"OUR PREVIOUS EMAILS ({len(context['our_emails'])} total):\n"
        )
        for e in context['our_emails']:
            prompt += f"  - Step {e['step']}: \"{e['subject']}\" (sent {e['sent_at']})\n"

        prompt += (
            f"\nRULES:\n"
            f"1. Be conversational, not salesy. They replied — that's interest.\n"
            f"2. Reference what they said specifically.\n"
            f"3. Propose a concrete next step (call, demo, meeting link).\n"
            f"4. Keep it short — 3-5 sentences max.\n"
            f"5. Do NOT send the email automatically — present the draft for user approval.\n"
        )

        dispatch_goal(
            prompt=prompt,
            user_id=user_id,
            goal_id=f"outreach_reply_{prospect['id']}_{int(time.time())}",
            goal_type='outreach',
        )
    except Exception as e:
        logger.error(f"Failed to dispatch response draft: {e}")


def register_reply_handler():
    """Register the inbound email handler with the email channel adapter.

    Called during boot (from register_outreach_goal_type) to wire
    reply detection into the channel system.
    """
    try:
        from integrations.channels.registry import get_registry
        registry = get_registry()
        adapter = registry.get_adapter('email')
        if adapter and hasattr(adapter, 'on_message'):
            async def _outreach_reply_hook(message):
                """Post-process inbound emails for prospect matching."""
                sender = getattr(message, 'sender_id', '') or ''
                subject = getattr(message, 'metadata', {}).get('subject', '')
                body = getattr(message, 'text', '')
                msg_id = getattr(message, 'message_id', '')
                if '@' in sender:
                    handle_inbound_email(sender, subject, body, msg_id)

            adapter.on_message(_outreach_reply_hook)
            logger.info("Outreach reply handler registered with email adapter")
    except Exception as e:
        logger.debug(f"Could not register reply handler: {e}")
