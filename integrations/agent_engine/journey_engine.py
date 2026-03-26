"""
Agentic User Journey Engine for HARTOS Marketing Flywheel

Orchestrates the full prospect lifecycle across channels:
  Discover -> Research -> Outreach -> Follow-up -> Reply -> Meeting -> Close

Each stage has:
  - Entry conditions (triggers)
  - Agentic actions (what HARTOS does proactively)
  - Exit conditions (what moves to next stage)
  - Channel preferences (where to reach the prospect)

The engine is:
  - Extensible: add stages/actions via register_stage()
  - Scalable: async execution via HARTOS workflow engine
  - Intuitive: config-driven, not code-driven
  - Agentic: HARTOS proactively researches, writes, follows up, schedules

Integrates with:
  - outreach_crm_tools.py (prospect store + Erxes CRM)
  - erxes_client.py (native CRM sync)
  - channels/ (Discord, Telegram, Slack, WhatsApp, Email)
  - automation/workflows.py (async workflow execution)
  - automation/triggers.py (event-based triggers)
  - agent_daemon.py (periodic tick-based checks)
  - marketing_tools.py (social posting, campaigns)
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger('hevolve_journey')

# ---- Journey Stage Definitions ----

STAGES = {
    'discover': {
        'order': 0,
        'description': 'Prospect identified but not yet researched',
        'crm_stage': 'new',
        'auto_actions': ['research_prospect'],
        'exit_to': 'research',
    },
    'research': {
        'order': 1,
        'description': 'Agent researching prospect (funding, tech, pain points)',
        'crm_stage': 'new',
        'auto_actions': ['deep_research', 'generate_personalized_email'],
        'exit_to': 'outreach',
    },
    'outreach': {
        'order': 2,
        'description': 'Initial personalized email sent',
        'crm_stage': 'contacted',
        'auto_actions': ['send_outreach_email'],
        'exit_to': 'nurture',
    },
    'nurture': {
        'order': 3,
        'description': 'Follow-up sequence running (day 3, 7, 14)',
        'crm_stage': 'contacted',
        'auto_actions': ['check_followups', 'try_alternate_channel'],
        'exit_to': 'engaged',
        'exit_on': 'reply_received',
    },
    'engaged': {
        'order': 4,
        'description': 'Prospect replied -- agent drafts response',
        'crm_stage': 'replied',
        'auto_actions': ['draft_response', 'notify_user', 'propose_meeting'],
        'exit_to': 'meeting',
    },
    'meeting': {
        'order': 5,
        'description': 'Meeting scheduled or in progress',
        'crm_stage': 'meeting',
        'auto_actions': ['prepare_demo_materials', 'send_calendar_invite'],
        'exit_to': 'negotiation',
    },
    'negotiation': {
        'order': 6,
        'description': 'Active deal negotiation',
        'crm_stage': 'negotiation',
        'auto_actions': ['track_deal_progress', 'generate_proposal'],
        'exit_to': 'won',
    },
    'won': {
        'order': 7,
        'description': 'Deal closed -- onboard partner',
        'crm_stage': 'won',
        'auto_actions': ['send_welcome_sequence', 'create_partner_channel'],
        'exit_to': None,
    },
    'lost': {
        'order': -1,
        'description': 'Deal lost -- schedule re-engagement',
        'crm_stage': 'lost',
        'auto_actions': ['schedule_reengagement', 'analyze_loss_reason'],
        'exit_to': None,
    },
}

# ---- Channel Strategy ----

CHANNEL_PRIORITY = {
    'outreach': ['email'],
    'nurture': ['email', 'linkedin'],
    'engaged': ['email', 'slack', 'discord'],
    'meeting': ['email', 'slack', 'discord', 'whatsapp'],
    'negotiation': ['email', 'slack', 'whatsapp'],
    'won': ['email', 'slack', 'discord', 'telegram'],
}

# ---- A/B Test Tracking ----

AB_VARIANTS = {
    'subject_direct': {
        'id': 'A',
        'template': 'quick question about {product_keyword}',
        'style': 'direct, curiosity-driven',
    },
    'subject_mutual': {
        'id': 'B',
        'template': '{company} + HARTOS -- better together?',
        'style': 'partnership-framed, collaborative',
    },
}


class JourneyEngine:
    """Orchestrates prospect journeys across the HARTOS flywheel."""

    def __init__(self):
        self._stages = dict(STAGES)
        self._action_handlers: Dict[str, Callable] = {}
        self._hooks: Dict[str, List[Callable]] = {}  # stage_enter, stage_exit, etc.
        self._lock = threading.Lock()
        self._register_default_actions()

    # ---- Stage Management ----

    def register_stage(self, name: str, config: Dict):
        """Add or override a journey stage."""
        self._stages[name] = config
        logger.info('Journey: registered stage "%s"', name)

    def register_action(self, name: str, handler: Callable):
        """Register an action handler.

        Handler signature: handler(prospect: Dict, context: Dict) -> Dict
        """
        self._action_handlers[name] = handler
        logger.info('Journey: registered action "%s"', name)

    def register_hook(self, event: str, handler: Callable):
        """Register a hook for journey events.

        Events: stage_enter, stage_exit, email_sent, reply_received,
                meeting_scheduled, deal_won, deal_lost
        """
        self._hooks.setdefault(event, []).append(handler)

    # ---- Journey Execution ----

    def advance_prospect(self, prospect: Dict, force_stage: str = None) -> Dict:
        """Move a prospect to the next stage and execute actions.

        Returns updated prospect with actions taken.
        """
        current = prospect.get('journey_stage', prospect.get('stage', 'discover'))
        stage_config = self._stages.get(current, {})

        # Determine target stage
        if force_stage:
            target = force_stage
        else:
            target = stage_config.get('exit_to', current)

        if not target or target == current:
            return {'moved': False, 'stage': current, 'actions': []}

        # Fire exit hooks
        self._fire_hooks('stage_exit', prospect=prospect, from_stage=current, to_stage=target)

        # Update prospect
        prospect['journey_stage'] = target
        prospect['stage'] = self._stages.get(target, {}).get('crm_stage', target)
        prospect['updated_at'] = datetime.utcnow().isoformat()

        # Fire enter hooks
        self._fire_hooks('stage_enter', prospect=prospect, stage=target)

        # Execute auto-actions for the new stage
        actions_taken = self._execute_stage_actions(prospect, target)

        # Sync to CRM
        self._sync_crm(prospect)

        logger.info('Journey: %s moved %s -> %s (%d actions)',
                     prospect.get('company', '?'), current, target, len(actions_taken))

        return {'moved': True, 'from': current, 'to': target, 'actions': actions_taken}

    def run_stage_actions(self, prospect: Dict) -> List[Dict]:
        """Execute pending actions for the prospect's current stage.

        Called by the daemon on each tick.
        """
        stage = prospect.get('journey_stage', prospect.get('stage', 'discover'))
        return self._execute_stage_actions(prospect, stage)

    def process_event(self, event_type: str, prospect: Dict, event_data: Dict = None) -> Dict:
        """Process an external event that may trigger a stage transition.

        Events:
          reply_received: prospect replied to email
          meeting_confirmed: meeting scheduled
          deal_won / deal_lost: final outcomes
          channel_message: message on any channel
          link_clicked: tracking pixel fired
        """
        current = prospect.get('journey_stage', prospect.get('stage', 'discover'))

        transitions = {
            'reply_received': {
                'from': ['outreach', 'nurture', 'contacted'],
                'to': 'engaged',
            },
            'meeting_confirmed': {
                'from': ['engaged', 'replied'],
                'to': 'meeting',
            },
            'deal_won': {
                'from': ['negotiation', 'meeting'],
                'to': 'won',
            },
            'deal_lost': {
                'from': ['negotiation', 'meeting', 'engaged', 'nurture'],
                'to': 'lost',
            },
        }

        transition = transitions.get(event_type)
        if transition and current in transition.get('from', []):
            self._fire_hooks(event_type, prospect=prospect, data=event_data)
            return self.advance_prospect(prospect, force_stage=transition['to'])

        return {'moved': False, 'stage': current, 'event': event_type}

    # ---- Tick-based Processing (called by daemon) ----

    def tick(self, all_prospects: Dict) -> Dict:
        """Process all prospects on a daemon tick.

        Checks for:
        - Pending follow-ups to send
        - Stage transitions that should happen
        - Prospects stuck in a stage too long
        - A/B test results to analyze

        Returns summary of actions taken.
        """
        summary = {'actions': 0, 'transitions': 0, 'followups_sent': 0}

        for pid, prospect in all_prospects.items():
            stage = prospect.get('journey_stage', prospect.get('stage', 'discover'))
            stage_config = self._stages.get(stage, {})

            # Check if prospect should auto-advance
            if stage == 'discover' and not prospect.get('researched'):
                result = self.advance_prospect(prospect, 'research')
                if result.get('moved'):
                    summary['transitions'] += 1

            elif stage == 'research' and prospect.get('email_draft'):
                result = self.advance_prospect(prospect, 'outreach')
                if result.get('moved'):
                    summary['transitions'] += 1

            elif stage in ('outreach', 'nurture', 'contacted'):
                # Check for due follow-ups
                actions = self.run_stage_actions(prospect)
                summary['actions'] += len(actions)

            # Check for stale prospects (no activity for 21+ days)
            last_activity = prospect.get('last_email_at') or prospect.get('updated_at', '')
            if last_activity:
                try:
                    last = datetime.fromisoformat(last_activity.replace('Z', '+00:00').replace('+00:00', ''))
                    days_stale = (datetime.utcnow() - last).days
                    if days_stale > 21 and stage in ('nurture', 'contacted'):
                        # Try alternate channel
                        self._try_alternate_channel(prospect)
                        summary['actions'] += 1
                except (ValueError, TypeError):
                    pass

        return summary

    # ---- A/B Testing ----

    def get_ab_stats(self, prospects: Dict) -> Dict:
        """Analyze A/B test results across all prospects."""
        stats = {'A': {'sent': 0, 'replied': 0, 'meetings': 0},
                 'B': {'sent': 0, 'replied': 0, 'meetings': 0}}

        for pid, p in prospects.items():
            variant = 'A' if 'variant: A' in p.get('notes', '') else 'B' if 'variant: B' in p.get('notes', '') else None
            if not variant:
                continue
            stats[variant]['sent'] += 1
            if p.get('last_reply_at'):
                stats[variant]['replied'] += 1
            if p.get('stage') in ('meeting', 'negotiation', 'won'):
                stats[variant]['meetings'] += 1

        # Calculate rates
        for v in ('A', 'B'):
            sent = stats[v]['sent'] or 1
            stats[v]['reply_rate'] = round(stats[v]['replied'] / sent * 100, 1)
            stats[v]['meeting_rate'] = round(stats[v]['meetings'] / sent * 100, 1)

        # Determine winner
        a_score = stats['A']['reply_rate'] + stats['A']['meeting_rate'] * 2
        b_score = stats['B']['reply_rate'] + stats['B']['meeting_rate'] * 2
        stats['winner'] = 'A' if a_score > b_score else 'B' if b_score > a_score else 'tie'
        stats['confidence'] = 'low' if (stats['A']['sent'] + stats['B']['sent']) < 20 else 'medium' if (stats['A']['sent'] + stats['B']['sent']) < 50 else 'high'

        return stats

    # ---- Channel Routing ----

    def get_channels_for_stage(self, stage: str) -> List[str]:
        """Get preferred channels for a journey stage."""
        return CHANNEL_PRIORITY.get(stage, ['email'])

    def send_via_channel(self, prospect: Dict, message: str, channel: str = None) -> Dict:
        """Send a message to a prospect via the best available channel."""
        stage = prospect.get('journey_stage', 'outreach')
        channels = [channel] if channel else self.get_channels_for_stage(stage)

        for ch in channels:
            result = self._send_channel_message(prospect, message, ch)
            if result.get('success'):
                return result

        return {'success': False, 'error': 'no channel available'}

    # ---- Internal Methods ----

    def _register_default_actions(self):
        """Register built-in action handlers."""
        self._action_handlers.update({
            'research_prospect': self._action_research,
            'deep_research': self._action_deep_research,
            'generate_personalized_email': self._action_generate_email,
            'send_outreach_email': self._action_send_email,
            'check_followups': self._action_check_followups,
            'try_alternate_channel': self._action_try_alternate_channel,
            'draft_response': self._action_draft_response,
            'notify_user': self._action_notify_user,
            'propose_meeting': self._action_propose_meeting,
            'prepare_demo_materials': self._action_noop,
            'send_calendar_invite': self._action_noop,
            'track_deal_progress': self._action_noop,
            'generate_proposal': self._action_noop,
            'send_welcome_sequence': self._action_noop,
            'create_partner_channel': self._action_noop,
            'schedule_reengagement': self._action_schedule_reengagement,
            'analyze_loss_reason': self._action_noop,
        })

    def _execute_stage_actions(self, prospect: Dict, stage: str) -> List[Dict]:
        """Execute all auto-actions for a stage."""
        stage_config = self._stages.get(stage, {})
        actions = stage_config.get('auto_actions', [])
        results = []

        for action_name in actions:
            handler = self._action_handlers.get(action_name)
            if handler:
                try:
                    result = handler(prospect, {})
                    results.append({'action': action_name, 'result': result})
                except Exception as e:
                    logger.error('Journey action %s failed: %s', action_name, e)
                    results.append({'action': action_name, 'error': str(e)})

        return results

    def _fire_hooks(self, event: str, **kwargs):
        """Fire all hooks for an event."""
        for hook in self._hooks.get(event, []):
            try:
                hook(**kwargs)
            except Exception as e:
                logger.error('Journey hook %s failed: %s', event, e)

    def _sync_crm(self, prospect: Dict):
        """Sync prospect state to Erxes CRM."""
        try:
            from integrations.agent_engine.erxes_client import get_erxes_client
            erxes = get_erxes_client()
            if erxes and prospect.get('erxes_deal_id'):
                crm_stage = prospect.get('stage', 'new')
                erxes.move_deal(prospect['erxes_deal_id'], crm_stage)
        except Exception as e:
            logger.debug('CRM sync failed: %s', e)

    def _send_channel_message(self, prospect: Dict, message: str, channel: str) -> Dict:
        """Send a message via a specific channel adapter."""
        if channel == 'email':
            try:
                from integrations.agent_engine.outreach_crm_tools import _send_email
                result = _send_email(prospect['email'], 'Update from HevolveAI', message)
                return {'success': result.get('success', False), 'channel': 'email'}
            except Exception as e:
                return {'success': False, 'error': str(e)}

        # Other channels via registry
        try:
            from integrations.channels.registry import get_registry
            registry = get_registry()
            adapter = registry.get_adapter(channel)
            if adapter:
                import asyncio
                loop = getattr(registry, '_loop', None)
                if loop and loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        adapter.send_message(
                            chat_id=prospect.get('channel_ids', {}).get(channel, ''),
                            text=message,
                        ),
                        loop,
                    )
                    result = future.result(timeout=15)
                    return {'success': result.success, 'channel': channel}
        except Exception as e:
            logger.debug('Channel %s send failed: %s', channel, e)

        return {'success': False, 'channel': channel, 'error': 'adapter unavailable'}

    def _try_alternate_channel(self, prospect: Dict):
        """Try reaching a prospect via a different channel than email."""
        stage = prospect.get('journey_stage', 'nurture')
        channels = self.get_channels_for_stage(stage)
        for ch in channels:
            if ch != 'email':
                message = (
                    "hey, sent you an email about HARTOS a few days ago. "
                    "just wanted to make sure it didn't get lost. "
                    "happy to chat here if that's easier."
                )
                result = self.send_via_channel(prospect, message, ch)
                if result.get('success'):
                    prospect['alternate_channel_tried'] = ch
                    prospect['updated_at'] = datetime.utcnow().isoformat()
                    return

    # ---- Action Handlers ----

    def _action_research(self, prospect: Dict, ctx: Dict) -> Dict:
        """Use Crawl4AI to research a prospect's company."""
        url = prospect.get('url', '')
        if not url:
            return {'skipped': True, 'reason': 'no url'}

        try:
            from urllib.request import Request, urlopen
            crawl_url = os.environ.get('CRAWL4AI_URL', 'http://172.17.0.1:8094')
            payload = json.dumps({'url': url, 'timeout': 60000}).encode('utf-8')
            req = Request(crawl_url + '/crawl', data=payload,
                          headers={'Content-Type': 'application/json'})
            resp = urlopen(req, timeout=120)
            data = json.loads(resp.read().decode('utf-8'))
            if data.get('markdown'):
                prospect['research'] = data['markdown'][:2000]
                prospect['researched'] = True
                return {'success': True, 'words': data.get('word_count', 0)}
        except Exception as e:
            logger.debug('Research crawl failed: %s', e)

        return {'success': False}

    def _action_deep_research(self, prospect: Dict, ctx: Dict) -> Dict:
        """Agent-powered deep research via HARTOS dispatch."""
        if prospect.get('deep_researched'):
            return {'skipped': True}

        try:
            from integrations.agent_engine.dispatch import dispatch_goal
            prompt = (
                "Research %s (%s). Find: funding stage, team size, tech stack, "
                "recent news, pain points that HARTOS could solve. "
                "Summarize in 3-4 bullet points."
                % (prospect.get('company', ''), prospect.get('url', ''))
            )
            dispatch_goal(
                user_id=prospect.get('created_by', 'system'),
                prompt=prompt,
                goal_tags=['research'],
            )
            prospect['deep_researched'] = True
            return {'dispatched': True}
        except Exception as e:
            return {'error': str(e)}

    def _action_generate_email(self, prospect: Dict, ctx: Dict) -> Dict:
        """Generate a personalized outreach email based on research."""
        research = prospect.get('research', '')
        company = prospect.get('company', '')
        product = prospect.get('product', prospect.get('notes', ''))

        # Simple template (agent can override with dispatch)
        prospect['email_draft'] = {
            'subject': 'quick question about %s' % company.lower(),
            'body': (
                '<p>hey,</p>'
                '<p>been looking at what %s is building. %s</p>'
                '<p>we have an open-source on-device AI runtime (HARTOS) '
                'that handles LLM inference, vision, speech, and multi-agent '
                'orchestration right on the hardware. no cloud dependency.</p>'
                '<p>worth a quick chat?</p>'
                '<p>sathish<br>founder, HevolveAI</p>'
            ) % (company, product[:100] if product else ''),
        }
        return {'generated': True}

    def _action_send_email(self, prospect: Dict, ctx: Dict) -> Dict:
        """Send the outreach email."""
        draft = prospect.get('email_draft')
        if not draft:
            return {'skipped': True, 'reason': 'no draft'}

        if prospect.get('emails_sent', 0) > 0:
            return {'skipped': True, 'reason': 'already sent'}

        try:
            from integrations.agent_engine.outreach_crm_tools import _send_email
            result = _send_email(prospect['email'], draft['subject'], draft['body'])
            if result.get('success') or result.get('via'):
                prospect['emails_sent'] = 1
                prospect['last_email_at'] = datetime.utcnow().isoformat()
                prospect['stage'] = 'contacted'
                return {'sent': True, 'result': result}
        except Exception as e:
            return {'error': str(e)}

        return {'sent': False}

    def _action_check_followups(self, prospect: Dict, ctx: Dict) -> Dict:
        """Check and send pending follow-ups (delegates to outreach tools)."""
        try:
            from integrations.agent_engine.outreach_crm_tools import check_pending_followups_daemon
            return check_pending_followups_daemon()
        except Exception as e:
            return {'error': str(e)}

    def _action_try_alternate_channel(self, prospect: Dict, ctx: Dict) -> Dict:
        """Try reaching via alternate channel if email isn't working."""
        if prospect.get('alternate_channel_tried'):
            return {'skipped': True}
        self._try_alternate_channel(prospect)
        return {'tried': prospect.get('alternate_channel_tried', 'none')}

    def _action_draft_response(self, prospect: Dict, ctx: Dict) -> Dict:
        """Dispatch agent to draft a response to prospect's reply."""
        try:
            from integrations.agent_engine.outreach_crm_tools import _dispatch_response_draft
            context = {
                'prospect': prospect,
                'their_reply': prospect.get('last_reply', {}),
                'our_emails': [],
            }
            _dispatch_response_draft(prospect, context)
            return {'dispatched': True}
        except Exception as e:
            return {'error': str(e)}

    def _action_notify_user(self, prospect: Dict, ctx: Dict) -> Dict:
        """Push notification to user about prospect activity."""
        try:
            from integrations.channels.response.router import get_response_router
            router = get_response_router()
            msg = "%s (%s) is now in stage: %s" % (
                prospect.get('company', '?'),
                prospect.get('email', '?'),
                prospect.get('journey_stage', prospect.get('stage', '?')),
            )
            router.route_response(
                user_id=prospect.get('created_by', 'system'),
                response_text=msg,
                fan_out=True,
            )
            return {'notified': True}
        except Exception as e:
            return {'error': str(e)}

    def _action_propose_meeting(self, prospect: Dict, ctx: Dict) -> Dict:
        """Agent proposes a meeting time to the prospect."""
        if prospect.get('meeting_proposed'):
            return {'skipped': True}

        try:
            from integrations.agent_engine.outreach_crm_tools import _send_email
            result = _send_email(
                prospect['email'],
                're: HARTOS partnership',
                '<p>great to hear back from you! would any of these work for a quick call?</p>'
                '<ul>'
                '<li>this week: Thursday or Friday, 2-4pm PT</li>'
                '<li>next week: Tuesday or Wednesday, morning PT</li>'
                '</ul>'
                '<p>happy to adjust to your timezone.</p>'
                '<p>sathish</p>',
            )
            prospect['meeting_proposed'] = True
            return {'proposed': True, 'result': result}
        except Exception as e:
            return {'error': str(e)}

    def _action_schedule_reengagement(self, prospect: Dict, ctx: Dict) -> Dict:
        """Schedule re-engagement for lost deals (try again in 60 days)."""
        prospect['reengagement_at'] = (datetime.utcnow() + timedelta(days=60)).isoformat()
        return {'scheduled': True, 'date': prospect['reengagement_at']}

    def _action_noop(self, prospect: Dict, ctx: Dict) -> Dict:
        """Placeholder for actions not yet implemented."""
        return {'noop': True}


# ---- Singleton ----

_engine = None
_engine_lock = threading.Lock()


def get_journey_engine() -> JourneyEngine:
    """Get or create the singleton journey engine."""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = JourneyEngine()
            logger.info('Journey engine initialized with %d stages', len(_engine._stages))
        return _engine


# ---- Agent Tool Registration ----

def register_journey_tools(helper, assistant, user_id: str):
    """Register journey tools for the marketing/sales agent."""
    from autogen import register_function

    engine = get_journey_engine()

    def view_journey_pipeline() -> str:
        """View the full prospect journey pipeline with stage counts and A/B stats."""
        from integrations.agent_engine.outreach_crm_tools import _load_prospects
        data = _load_prospects()
        prospects = data.get('prospects', {})

        # Group by journey stage
        pipeline = {}
        for pid, p in prospects.items():
            stage = p.get('journey_stage', p.get('stage', 'unknown'))
            pipeline.setdefault(stage, []).append({
                'company': p.get('company'),
                'email': p.get('email'),
                'emails_sent': p.get('emails_sent', 0),
                'last_activity': p.get('last_email_at') or p.get('updated_at'),
            })

        # A/B stats
        ab = engine.get_ab_stats(prospects)

        return json.dumps({
            'pipeline': {k: {'count': len(v), 'prospects': v} for k, v in pipeline.items()},
            'total': len(prospects),
            'ab_test': ab,
        }, default=str)

    def advance_prospect_stage(
        prospect_id: str,
        target_stage: str = None,
    ) -> str:
        """Move a prospect to the next journey stage (or a specific stage).

        Executes all auto-actions for the new stage.
        """
        from integrations.agent_engine.outreach_crm_tools import _load_prospects, _save_prospects
        data = _load_prospects()
        prospect = data['prospects'].get(prospect_id)
        if not prospect:
            return json.dumps({'error': 'prospect not found'})

        result = engine.advance_prospect(prospect, force_stage=target_stage)
        _save_prospects(data)
        return json.dumps(result, default=str)

    def run_journey_tick() -> str:
        """Run one tick of the journey engine across all prospects.

        Checks follow-ups, stage transitions, stale prospects, and A/B results.
        """
        from integrations.agent_engine.outreach_crm_tools import _load_prospects, _save_prospects
        data = _load_prospects()
        summary = engine.tick(data.get('prospects', {}))
        _save_prospects(data)
        return json.dumps(summary, default=str)

    def send_prospect_message(
        prospect_id: str,
        message: str,
        channel: str = None,
    ) -> str:
        """Send a message to a prospect via the best available channel.

        Channels: email, slack, discord, telegram, whatsapp.
        If no channel specified, uses the best one for the prospect's journey stage.
        """
        from integrations.agent_engine.outreach_crm_tools import _load_prospects
        data = _load_prospects()
        prospect = data['prospects'].get(prospect_id)
        if not prospect:
            return json.dumps({'error': 'prospect not found'})

        result = engine.send_via_channel(prospect, message, channel)
        return json.dumps(result, default=str)

    for func in [view_journey_pipeline, advance_prospect_stage, run_journey_tick, send_prospect_message]:
        register_function(func, caller=helper, executor=assistant, description=func.__doc__)

    logger.info('Registered 4 journey tools for user %s', user_id)


# ---- Daemon Integration ----

def journey_daemon_tick() -> Dict:
    """Called by agent_daemon.py on each tick.

    Runs the journey engine tick and returns summary.
    """
    try:
        from integrations.agent_engine.outreach_crm_tools import _load_prospects, _save_prospects
        engine = get_journey_engine()
        data = _load_prospects()
        summary = engine.tick(data.get('prospects', {}))
        _save_prospects(data)
        return summary
    except Exception as e:
        logger.error('Journey daemon tick failed: %s', e)
        return {'error': str(e)}


# ---- Goal Type Registration ----

def register_sales_goal_type():
    """Register 'sales' as a goal type in the agent engine.

    The sales agent proactively manages the entire flywheel:
    - Discovers and researches prospects
    - Writes personalized outreach
    - Manages follow-up sequences
    - Handles replies and schedules meetings
    - Tracks pipeline and A/B tests
    """
    try:
        from integrations.agent_engine.goal_manager import register_goal_type
        register_goal_type(
            goal_type='sales',
            build_prompt=_build_sales_prompt,
            tool_tags=['sales', 'outreach', 'marketing', 'email', 'crm'],
        )
        logger.info("Registered 'sales' goal type")
    except Exception as e:
        logger.error('Failed to register sales goal type: %s', e)


def _build_sales_prompt(goal_dict: Dict, product_dict: Dict = None) -> str:
    """Build the system prompt for the sales agent."""
    from integrations.agent_engine.outreach_crm_tools import _load_prospects
    data = _load_prospects()
    prospects = data.get('prospects', {})

    engine = get_journey_engine()
    ab_stats = engine.get_ab_stats(prospects)

    # Pipeline summary
    pipeline = {}
    for pid, p in prospects.items():
        stage = p.get('journey_stage', p.get('stage', 'unknown'))
        pipeline.setdefault(stage, []).append(p.get('company', '?'))

    pipeline_text = '\n'.join(
        '  %s: %d [%s]' % (stage, len(companies), ', '.join(companies[:5]))
        for stage, companies in sorted(pipeline.items())
    )

    return """You are the HARTOS Sales & Marketing Agent.

PRODUCT: HARTOS (Hevolve Hive Agentic Runtime OS)
- Open-source on-device AI runtime for robotics
- LLM inference, vision, speech, multi-agent orchestration on hardware
- Hive network: more robots = better models for everyone
- Early partners shape the intelligence for their vertical

YOUR JOB: Proactively manage the entire sales flywheel.
1. DISCOVER new robotics companies that could use HARTOS
2. RESEARCH each one (funding, tech stack, pain points)
3. WRITE personalized outreach (human tone, no em dashes, casual)
4. FOLLOW UP at day 3, 7, 14 -- each shorter and more direct
5. HANDLE REPLIES -- draft responses, propose meetings
6. TRACK PIPELINE -- monitor A/B tests, optimize what works
7. CLOSE DEALS -- prepare demos, proposals, onboard partners

CURRENT PIPELINE:
%s

A/B TEST STATUS:
  Variant A (direct): %d sent, %.1f%% reply rate
  Variant B (mutual): %d sent, %.1f%% reply rate
  Winner so far: %s (confidence: %s)

RULES:
- Sound human. No em dashes. Casual lowercase subjects.
- Create FOMO: one partner per vertical, Q2 deadline.
- Use tools proactively -- don't wait to be asked.
- When a prospect replies, respond within minutes.
- After 3 follow-ups with no response, try alternate channel.
- Always sync to Erxes CRM after any pipeline change.

CHANNELS AVAILABLE: email, discord, telegram, slack, whatsapp
""" % (
        pipeline_text or '  (empty)',
        ab_stats['A']['sent'], ab_stats['A']['reply_rate'],
        ab_stats['B']['sent'], ab_stats['B']['reply_rate'],
        ab_stats['winner'], ab_stats['confidence'],
    )
