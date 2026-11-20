"""
Unified Agent Goal Engine - Outreach CRM Tools (Tier 2)

These tools are loaded ONLY when the agent is working on an 'outreach' goal.
They connect HARTOS agents to Erxes CRM for:
  - Lead/contact management
  - Deal pipeline tracking
  - Automated email outreach, each goal's from the mailbox its config names
    (sender_email; _send_email)
  - Follow-up sequence automation
  - Reply detection and pipeline stage updates

Integrates with:
  - Erxes GraphQL API (http://192.168.0.9:3300)
  - The node's email channel: one adapter per connected mailbox
  - HertzAI email service (EMAIL_SERVICE_URL), only for email that serves
    no goal on a node that holds no goal mailbox

Tier 1 (Default): google_search, text_2_image, delegate_to_specialist, etc.
Tier 2 (Category): create_lead, send_outreach, check_replies, move_deal_stage
Tier 3 (Runtime): delegate_to_specialist for channel-specific follow-ups
"""
import hashlib
import html
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Annotated, Optional, Dict, List, NamedTuple

logger = logging.getLogger('hevolve_outreach')

# ── Configuration ──
ERXES_API_URL = os.environ.get('ERXES_API_URL', 'http://localhost:3300')
EMAIL_SERVICE_URL = os.environ.get('EMAIL_SERVICE_URL', 'http://localhost:4000')

#: A planned outreach email (a follow-up step, or the journey's first email
#: draft) that the sender refuses is tried again later, up to this many
#: sends in all; then it is given up ('failed').  A refused send is not a
#: sent one, and a dead address must not be mailed forever from the owner's
#: account (review of 1c4b2b650: the first email was retried on every tick).
OUTREACH_SEND_ATTEMPTS = 3
#: Wait before the next try of a refused planned email: this times the tries
#: so far (1 h, then 2 h), so a short mail outage does not use up the email.
OUTREACH_RETRY_WAIT = timedelta(hours=1)
#: Longest sender error kept on a planned email.
_SEND_ERROR_CAP = 200
#: What a send refused for want of any sender tells the agent: what the
#: owner sets up, and what to do meanwhile.  The email channel's setup asks
#: for these four (integrations/channels/metadata.py, 'email').
NO_EMAIL_SENDER_FIX = (
    'To send email the owner sets up a sender on this node: connect its email '
    'channel (IMAP host, SMTP host, address and password), or point '
    'EMAIL_SERVICE_URL at a mailer this node can reach. Until then do not try '
    'to send again; keep finding and researching prospects.')

#: The channel each mailbox is an account of (ChannelRegistry accounts).
EMAIL_CHANNEL = 'email'
#: Longest goal title a refusal quotes, so the refusal fits _SEND_ERROR_CAP.
_TITLE_CAP = 60
#: How every per-goal sender refusal ends (owner, 2026-11-16: a goal's
#: email never falls back to another company's mailbox or the shared mailer).
_NEVER_ANOTHER_MAILBOX = (
    ' The email is never sent from another mailbox or the shared mailer '
    'instead. Do not try to send it again until then; keep researching '
    'prospects.')


class Sender(NamedTuple):
    """Whom a goal's email goes from: the goal (``goal_id``, ``title``) and
    the mailbox its config names under "sender_email" (``address``, None
    when it names none).  ``found`` is False when the goal is not on this
    node or could not be read.  ``creator`` is the account that made the
    goal (AgentGoal.created_by): which mailbox the goal may use depends on
    it (_may_use_mailbox).  ``verified``: the content hashes
    (step_content_hash) of the copy the owner has verified for this goal
    (config verified_copy, mark_goal_copy_verified): a follow-up sequence goes
    out only when every one of its steps' hashes is in it."""
    goal_id: str
    address: Optional[str]
    title: str = ''
    found: bool = True
    creator: Optional[str] = None
    verified: frozenset = frozenset()


def goal_sender(goal_id) -> Optional[Sender]:
    """The one reader of a goal's sender: the "sender_email" of its config
    (AgentGoal.config_json) and the account that made the goal.  None when
    ``goal_id`` is empty: the email serves no goal.

    Owner, 2026-11-16: central sends the email of every lane, each goal's
    from the company mailbox its config names, and never from another
    company's.  The owner moves a lane to another company by changing that
    one value.  The config is not the owner's alone (any signed-in account
    writes a goal's config through POST /api/goals), so naming a mailbox
    does not entitle a goal to it: _send_from_goal_mailbox lets a goal send
    from a mailbox only when _may_use_mailbox says its creator may (review
    of 88b6821f2, M2)."""
    if not goal_id:
        return None
    goal_id = str(goal_id)
    try:
        from integrations.social.models import AgentGoal, db_session
        with db_session(commit=False) as db:
            goal = db.query(AgentGoal).filter_by(id=goal_id).first()
            if goal is None:
                return Sender(goal_id, None, found=False)
            config = goal.config_json or {}
            address = config.get('sender_email')
            address = address.strip() if isinstance(address, str) else ''
            return Sender(goal_id, address or None, goal.title or '',
                          creator=str(goal.created_by) if goal.created_by else None,
                          verified=frozenset(
                              h for h in (config.get('verified_copy') or [])
                              if isinstance(h, str)))
    except Exception:
        logger.warning("Goal %s: the mailbox it sends from could not be read",
                       goal_id, exc_info=True)
        return Sender(goal_id, None, found=False)


def prospect_sender(prospect: Dict) -> Optional[Sender]:
    """Whom email to ``prospect`` goes from: the sender of the goal the
    prospect belongs to (its 'goal_id', stamped by the goal turn that
    created it: _turn_goal_id), or None for a prospect no goal holds.
    Every send to a prospect resolves its sender here: the agent's
    send_outreach_email, the follow-up checker and the journey engine."""
    return goal_sender((prospect or {}).get('goal_id'))


def _turn_goal_id() -> Optional[str]:
    """The goal the running turn works for, when this node's own dispatch
    started that turn and the goal is on this node; None in any other turn.
    A goal's turn runs under the request id daemon_<goal id>
    (core.chat_client.daemon_goal_id), read from the turn's own context
    (core.llm_outbound_logger.turn_request_id), never a thread's last
    request, which can name another turn (review of efd223acf).  The id
    alone proves nothing: any client can name daemon_<goal id> in its /chat
    body, so the turn must be the node's own (is_own_daemon_turn, fe18126df),
    or a stranger's turn could act as a goal and write from its mailbox."""
    from core.chat_client import daemon_goal_id, is_own_daemon_turn
    from core.llm_outbound_logger import turn_request_id
    request_id = turn_request_id()
    goal_id = daemon_goal_id(request_id)
    if not goal_id or not is_own_daemon_turn(request_id):
        return None
    sender = goal_sender(goal_id)
    return goal_id if sender is not None and sender.found else None


def _for_turn_goal(prospect: Dict, goal_id: Optional[str]) -> Dict:
    """``prospect`` as one of ``goal_id``'s (its 'goal_id', which
    prospect_sender reads), unchanged when the turn works for no goal."""
    if goal_id:
        prospect['goal_id'] = goal_id
    return prospect


def refuse_other_goals_prospect(tool: str, prospect: Dict) -> Optional[Dict]:
    """The reply of a tool that would write to ``prospect`` when the
    prospect belongs to a goal and the running turn is not that goal's own
    (_turn_goal_id: another goal's turn, a person's turn, or a turn that
    only names the goal), or when the prospect belongs to no goal and the
    turn is a goal's own; None when it may (the goal's own turn on its own
    prospect, or a turn of no goal on a prospect of no goal).

    A goal's turn never writes to a prospect no goal holds (#240, central
    2026-11-20: 87 of the store's 89 prospects were old agri-robotics leads
    of no goal, and the rice-export goal's turn set out to email each 'new'
    one); a person's turn still may.

    The words of an email come from the turn that writes them, the mailbox
    from the prospect's goal: a lashes turn writing to a speech prospect
    sent lashes copy from the Hevolve mailbox (review of ae390514f, P2).  So
    a goal's prospects are emailed, sequenced and messaged only in that
    goal's own turns.  send_outreach_email, create_followup_sequence,
    move_prospect_stage and the journey's send_prospect_message and
    advance_prospect_stage (whose stage actions email the prospect) ask
    here; the follow-up checker sends steps their goal's turn wrote."""
    owner_goal = (prospect or {}).get('goal_id') or None
    turn_goal = _turn_goal_id()
    if owner_goal == turn_goal:
        return None
    from core.tool_refusal import RETRY_AFTER_FIX, RETRY_NEVER, refusal_fields

    def _title(gid):
        sender = goal_sender(gid)
        return (sender.title if sender is not None and sender.title else gid)[:_TITLE_CAP]

    if owner_goal is None:
        # A goal's own turn and a prospect of no goal: its copy would go to
        # another product's lead.
        fields = refusal_fields(
            tool, 'goalless_prospect',
            f"prospect '{prospect.get('company', '?')}' belongs to no goal, "
            f"and this turn works for goal '{_title(turn_goal)}'",
            "Work on this goal's own prospects; add this goal's buyers with "
            "create_prospect (they are stamped with this goal)",
            retry=RETRY_AFTER_FIX)
        return dict(fields, success=False, sent=False, error=fields['note'])
    this_turn = (f"'{_title(turn_goal)}', the goal this turn works for"
                 if turn_goal else "this turn, which is not one of that goal's own")
    problem = (
        f"prospect '{prospect.get('company', '?')}' belongs to goal "
        f"'{_title(owner_goal)}', not to {this_turn}: a goal's prospects are "
        f"written to only in that goal's own turns, from its own mailbox")
    if turn_goal:
        # Another goal's turn: it can still work its own prospects.
        fields = refusal_fields(
            tool, 'another_goals_prospect', problem,
            "Work on this turn's own goal's prospects (get_pipeline_status "
            "shows each prospect's goal_id); that goal's own turns and its "
            "follow-ups write to this one",
            retry=RETRY_AFTER_FIX)
    else:
        # A person's turn (or one that only names a goal) has no goal of its
        # own: say what the person can do instead (owner rule 2026-11-09).
        fields = refusal_fields(
            tool, 'another_goals_prospect', problem,
            f"Tell the person: to reach or change this prospect, steer goal "
            f"'{_title(owner_goal)}' (ask it, from its page or with "
            f"steer_goal, to write to them); that goal's next turn does it "
            f"from its own mailbox, and its follow-ups continue meanwhile",
            retry=RETRY_NEVER)
    return dict(fields, success=False, sent=False, error=fields['note'])


def store_seen_by(data: Dict, goal_id: Optional[str]) -> Dict:
    """The prospect store ``data`` as a turn working for ``goal_id`` sees
    it: that goal's prospects only, with their follow-up sequences and sent
    emails; ``data`` itself when ``goal_id`` is None (a person's turn, the
    daemon's tick: the owner's view).  The ONE filter of what a goal's
    model is shown and what its batch tools work on: get_pipeline_status,
    check_pending_followups, list_sent_emails, the journey's
    view_journey_pipeline and run_journey_tick, and the sales prompt
    (journey_engine._build_sales_prompt, by the goal's id).

    Central, 2026-11-20 (#240): every one of those read the whole store, 87
    of whose 89 prospects were old agri-robotics leads of no goal; the
    rice-export goal planned to email "each new prospect" and named seven
    of those robotics companies as its targets.

    The prospects, sequences and log entries are the store's own dicts, not
    copies: a caller that changes one changes ``data``, which it saves."""
    if not goal_id:
        return data
    goal_id = str(goal_id)
    prospects = {pid: p for pid, p in (data.get('prospects') or {}).items()
                 if isinstance(p, dict) and p.get('goal_id') == goal_id}
    return dict(
        data, prospects=prospects,
        sequences={sid: s for sid, s in (data.get('sequences') or {}).items()
                   if s.get('prospect_id') in prospects},
        sent_log=[e for e in data.get('sent_log') or []
                  if e.get('prospect_id') in prospects])


#: A template blank left in an email: {POSTAL ADDRESS}, {{first_name}},
#: {product_keyword}.  Only a word inside braces, so CSS ({color: red}) is
#: not one.
_TEMPLATE_BLANK = re.compile(r'\{\{?\s*[A-Za-z_][A-Za-z0-9_ ]*\s*\}?\}')


def verify_outgoing_email(to_email: str, subject: str, html: str) -> Optional[Dict]:
    """The one check every OUTREACH email passes before it leaves, whatever
    the mailbox: None when it may go, else the refusal (_send_email's
    result shape: what is wrong and how to fix it).  Only the outreach
    sends reach it (send_outreach_email, the follow-up checker, the
    journey's emails: everything that goes through _send_email); the
    node-wide email adapter's chat replies, send_to_channel('email') and
    email_campaign do not, and the template-blank rule is a pattern, not
    a review: a promo code written in braces ("{SAVE10}") is refused, a
    blank written "[First Name]" or "%FIRSTNAME%" is not (review of
    88b6821f2, S5).

    Owner, 16 Nov 2026: "You verify all the outgoing emails on my behalf
    from central."  Checked here, deterministically:
      * no template blank is left in the subject or the body (the lanes'
        rule: never send an email whose footer still holds the
        {POSTAL ADDRESS} placeholder);
      * it is not addressed to one of this node's own mailboxes (two
        mailboxes must never write to each other; review of ae390514f).
    A review of the words against the goal's own rules (a model's, or the
    owner's delegate's) belongs in this same function, so it covers every
    send path at once; it is not built yet."""
    blank = _TEMPLATE_BLANK.search(f"{subject or ''}\n{html or ''}")
    if blank:
        return _refused_send(
            f"the email still holds the template blank {blank.group(0)}",
            f"Fill in or remove {blank.group(0)} in the subject or body, then "
            f"send it again. An email with a blank in it never leaves.",
            via="verification")
    from integrations.channels.registry import normalize_account
    if normalize_account(to_email) in _goal_mailboxes():
        return _refused_send(
            f"{to_email} is one of this node's own mailboxes",
            "Send only to prospects. The node's mailboxes never write to each "
            "other.", via="verification")
    return None


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
    """Save prospect data to local JSON store. Thread-safe.

    Safety: refuses to overwrite non-empty data with empty data.
    """
    with _prospect_lock:
        prospects = data.get('prospects', {})
        # Guard: never overwrite existing data with empty
        if not prospects:
            try:
                with open(_PROSPECT_FILE, 'r') as f:
                    existing = json.load(f)
                if existing.get('prospects'):
                    logger.warning('_save_prospects: refusing to overwrite %d prospects with empty data',
                                   len(existing['prospects']))
                    return
            except (FileNotFoundError, json.JSONDecodeError):
                pass
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


def _refused_send(problem: str, fix: str, via: str = 'no sender') -> Dict:
    """A send that was not tried, in _send_email's result shape: what was
    wrong (``error``) and what to do about it (``fix``), read by
    _send_error_text (owner rule 2026-11-09: a refusal says both)."""
    return {'success': False, 'via': via, 'error': problem, 'fix': fix}


def _goal_mailboxes() -> Dict:
    """The mailboxes this node holds for goals: {address: adapter}."""
    try:
        from integrations.channels.flask_integration import get_channel_integration
        boxes = get_channel_integration().registry.accounts(EMAIL_CHANNEL)
        return boxes if isinstance(boxes, dict) else {}
    except Exception:
        logger.warning("The node's goal mailboxes could not be read",
                       exc_info=True)
        return {}


def _may_use_mailbox(creator, address) -> bool:
    """Whether a goal made by account ``creator`` may send from the mailbox
    ``address``: the ONE rule of who may use a company mailbox (review of
    88b6821f2, M2).  Only when ``creator`` is

      * the account that CONNECTED that mailbox: its active binding row,
        the one register_channel writes under the connecting account
        (FlaskChannelIntegration.binding_connects_the_channel: a row of the
        channel's own account, not a person who writes in on it), or
      * an account only this node holds (voting_rules.node_holds, through
        ThoughtExperimentService.node_held_ids: the node's owner, or its
        system account).

    A goal's config names its mailbox, but any signed-in account writes a
    goal's config (POST /api/goals), and the daemon runs every active goal:
    without this a stranger's goal sent a stranger's words from the
    company's address.  False for a goal with no creator, and when the
    lookup fails (it fails closed)."""
    from integrations.channels.registry import normalize_account
    creator = str(creator) if creator else ''
    key = normalize_account(address)
    if not creator or key is None:
        return False
    try:
        from integrations.channels.flask_integration import FlaskChannelIntegration
        from integrations.social.models import db_session
        from integrations.social.thought_experiment_service import (
            ThoughtExperimentService)
        with db_session(commit=False) as db:
            if ThoughtExperimentService.node_held_ids(db, [creator]):
                return True
            return creator in FlaskChannelIntegration.accounts_connecting(
                db, EMAIL_CHANNEL, key)
    except Exception:
        logger.warning("Who may use the mailbox of a goal could not be read",
                       exc_info=True)
        return False


def _goal_mailbox(sender: Sender) -> tuple:
    """``(adapter, loop, None)``: the live mailbox a goal sends from, and the
    loop that owns it; or ``(None, None, refusal)`` saying what to fix when
    the goal is not here, names no mailbox, that mailbox is not connected,
    or the goal's creator may not use it (_may_use_mailbox).  Asked before an
    email is held as well as before one is sent: an email that could never
    leave is not put in front of the owner."""
    title = (sender.title or sender.goal_id)[:_TITLE_CAP]
    if not sender.found:
        return None, None, _refused_send(
            f"the goal this email is for ({sender.goal_id}) is not on this "
            f"node or could not be read, so the mailbox it goes from is unknown",
            "Create the prospect again in the turn of the goal it is for, so it "
            "carries that goal." + _NEVER_ANOTHER_MAILBOX)
    if not sender.address:
        return None, None, _refused_send(
            f"goal '{title}' names no mailbox: its config has no sender_email",
            "The owner adds \"sender_email\" (the address this goal's email "
            "goes from) to the goal's config, and connects that mailbox in "
            "the channel setup form." + _NEVER_ANOTHER_MAILBOX)
    adapter = loop = None
    try:
        from integrations.channels.flask_integration import get_channel_integration
        integration = get_channel_integration()
        adapter = integration.registry.get_account(EMAIL_CHANNEL, sender.address)
        loop = integration.running_loop()
    except Exception:
        logger.warning("The mailbox of goal %s could not be looked up",
                       sender.goal_id, exc_info=True)
    if (adapter is None or loop is None or not hasattr(adapter, 'send_email')
            or not adapter.is_running()):
        return None, None, _refused_send(
            f"the mailbox {sender.address} that goal '{title}' sends from is "
            f"not connected on this node",
            f"Connect {sender.address} in the channel setup form (Email: IMAP "
            f"server, SMTP server, address and password)."
            + _NEVER_ANOTHER_MAILBOX)
    if not _may_use_mailbox(sender.creator, sender.address):
        return None, None, _refused_send(
            f"goal '{title}' may not send from {sender.address}: the account "
            f"that made it did not connect that mailbox",
            f"A mailbox is used only by goals made by the account that "
            f"connected it, or by this node's owner. Have that account (or "
            f"the owner) create this goal, or connect {sender.address} from "
            f"the account that made it." + _NEVER_ANOTHER_MAILBOX)
    return adapter, loop, None


def _send_from_goal_mailbox(sender: Sender, adapter, loop, to_email: str,
                            subject: str, plain_text: str, html: str) -> Dict:
    """Send a goal's email from its mailbox (_goal_mailbox: the one that
    names it and says whether it may) and from no other.  Never the mailer."""
    import asyncio
    try:
        result = asyncio.run_coroutine_threadsafe(
            adapter.send_email(to=to_email, subject=subject,
                               body=plain_text, html_body=html),
            loop).result(timeout=30)
    except Exception as e:
        return {'success': False, 'via': 'email_channel',
                'from': sender.address, 'error': str(e) or type(e).__name__}
    return {'success': result.success, 'via': 'email_channel',
            'from': sender.address,
            'error': result.error if not result.success else None}


def _send_email(to_email: str, subject: str, html_body: str,
                sender: Optional[Sender] = None,
                prospect_id: Optional[str] = None,
                verified: bool = False) -> Dict:
    """Send one outreach email from the mailbox it belongs to.

    ``sender``: the goal the email is for and the mailbox its config names
    (prospect_sender / goal_sender, the one reader of a goal's
    sender_email).  A goal's email goes from that mailbox only; when the
    goal names none, is not on this node, or its mailbox is not connected
    here, the send is refused with what to fix, and never sent from another
    mailbox or the mailer (owner, 2026-11-16).

    ``verified``: the words are the owner's to trust: a follow-up step whose
    exact copy the owner verified for its goal (send_due_followups), or a held
    draft the owner approved (approve_held_draft).  An email that is not is
    HELD, not sent: ``{'success': False, 'held': True, 'draft_id': ...}``,
    kept for the owner (_hold_email), which needs ``prospect_id`` (the
    prospect it is for).  The caller reads 'held' before it reads a refusal:
    a held email is not a refused try.

    No sender: the email serves no goal.  It goes through the node's own
    email channel, else the HertzAI mailer, as every send did before
    senders were per goal; refused on a node that holds goal mailboxes,
    since which one it should go from cannot be told.  Held too on a node
    that is not flat (_goalless_email_is_held: central, regional); a flat
    node is one person's machine, and the email is that person's own.

    Either way the email first passes verify_outgoing_email, the one check
    every outreach email passes (owner, 2026-11-16).
    """
    sanitized_body = _sanitize_html(html_body)
    # Strip HTML for plain-text fallback
    plain_text = re.sub(r'<[^>]+>', '', sanitized_body).strip()

    # Every email, from any mailbox, is verified before it leaves.
    refused = verify_outgoing_email(to_email, subject, sanitized_body)
    if refused is not None:
        return refused
    if sender is not None:
        adapter, loop, unable = _goal_mailbox(sender)
        if unable is not None:
            return unable
        if not verified:
            return _hold_email(sender.goal_id, prospect_id, subject, sanitized_body)
        return _send_from_goal_mailbox(sender, adapter, loop, to_email, subject,
                                       plain_text, sanitized_body)
    mailboxes = _goal_mailboxes()
    if mailboxes:
        return _refused_send(
            f"this email is for no goal, and this node sends only goals' "
            f"email, each from its goal's own mailbox ({len(mailboxes)} "
            f"connected)",
            "Create or import the prospect in the turn of the goal it is for; "
            "its email then goes from that goal's sender_email. No mailbox is "
            "guessed for it." + _NEVER_ANOTHER_MAILBOX)
    if not verified and _goalless_email_is_held():
        return _hold_email(None, prospect_id, subject, sanitized_body)

    # Tier 1: Email channel adapter (user's SMTP), run on the channel
    # integration's loop, which owns the adapters (ChannelRegistry has none).
    have_channel = False
    try:
        from integrations.channels.flask_integration import get_channel_integration
        import asyncio
        integration = get_channel_integration()
        adapter = integration.registry.get('email')
        loop = integration.running_loop()
        if adapter is not None and hasattr(adapter, 'send_email') and loop is not None:
            have_channel = True
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
        logger.warning(f"Email channel send failed, trying the mailer service: {e}")

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
        if have_channel:
            return {'success': False, 'error': str(e)}
        # Neither sender exists for this node: say so, and what the owner
        # sets up (owner rule 2026-11-09: a refusal says what was wrong and
        # how to fix it).  Central, 2026-11-12: no email channel and
        # EMAIL_SERVICE_URL unset, so every send ended on the default
        # localhost mailer's "connection refused", which told nobody what
        # to do (#280).
        return {'success': False, 'via': 'no sender',
                'error': (f'no email channel is connected and the mailer at '
                          f'{EMAIL_SERVICE_URL} did not answer ({type(e).__name__})'),
                'fix': NO_EMAIL_SENDER_FIX}


# ── One rule for a planned email that was, or was not, delivered ──
# A planned email is a follow-up step or the journey's first email draft:
# a dict the store keeps.  send_outreach_email, the follow-up checker and
# JourneyEngine._action_send_email all count a delivery and a refusal here.

def _send_error_text(result: Dict) -> str:
    """Why _send_email did not deliver, in words the agent can read, and
    what to do about it when _send_email knows (its 'fix')."""
    if result.get('error'):
        why = str(result['error'])[:_SEND_ERROR_CAP]
        return f"{why}. {result['fix']}" if result.get('fix') else why
    sender = result.get('via') or 'no sender'
    if result.get('status_code') is not None:
        return f"{sender} answered {result['status_code']}"
    return f"{sender} gave no reason"


def count_delivered(prospect: Dict, when: datetime):
    """One more email reached ``prospect`` (it also moves updated_at)."""
    prospect['emails_sent'] = prospect.get('emails_sent', 0) + 1
    prospect['last_email_at'] = when.isoformat()
    prospect['updated_at'] = when.isoformat()


def mark_contacted(prospect: Dict):
    """The ONE rule for 'contacted': a prospect nobody has reached ('new')
    becomes 'contacted' when an email REACHES them, and not before: not when
    an email is written, held for the owner, or refused.  send_outreach_email,
    approve_held_draft and the journey's first email all end here, so a held
    first email leaves the prospect 'new' by whichever path wrote it (review
    of a93ead3cf, S1: the journey path said 'contacted' with nothing sent)."""
    if prospect.get('stage') == 'new':
        prospect['stage'] = 'contacted'


def stage_for_move(prospect: Dict, crm_stage: str) -> str:
    """The CRM stage a journey move gives ``prospect``: ``crm_stage``, except
    'contacted' until an email has reached them (mark_contacted says when);
    until then the prospect keeps the stage it has."""
    if crm_stage == 'contacted' and not prospect.get('emails_sent'):
        return prospect.get('stage') or 'new'
    return crm_stage


def retry_due(planned: Dict, now: datetime) -> bool:
    """May a planned email that was refused before be tried again now?"""
    after = planned.get('retry_after')
    if not after:
        return True
    try:
        return now >= datetime.fromisoformat(after)
    except (TypeError, ValueError):
        return True


def note_refused(planned: Dict, result: Dict, now: datetime, what: str) -> bool:
    """Record and log a refused send of a planned email (``what`` names it
    in the log: "Shop, follow-up step 2").  Returns True when it is given
    up ('failed') after OUTREACH_SEND_ATTEMPTS sends; otherwise the next try
    waits OUTREACH_RETRY_WAIT times the tries so far (1 h, then 2 h).

    The one place a refusal is logged, so every path that sends a planned
    email (the daemon's checker, a journey tick, an agent tool) leaves the
    same WARNING (review of 6da8b09c7: the journey path logged nothing)."""
    planned['attempts'] = planned.get('attempts', 0) + 1
    planned['last_error'] = _send_error_text(result)
    planned['last_tried_at'] = now.isoformat()
    gave_up = planned['attempts'] >= OUTREACH_SEND_ATTEMPTS
    if gave_up:
        planned['status'] = 'failed'
        planned.pop('retry_after', None)
    else:
        planned['retry_after'] = (
            now + OUTREACH_RETRY_WAIT * planned['attempts']).isoformat()
    logger.warning("Outreach email NOT sent (%s), try %d of %d%s: %s",
                   what, planned['attempts'], OUTREACH_SEND_ATTEMPTS,
                   ', given up' if gave_up else
                   ', next try after ' + planned['retry_after'] + ' UTC',
                   planned['last_error'])
    return gave_up


def next_followup(sequence: Dict, now: datetime):
    """The one rule for a sequence's next follow-up: ``(step, state)`` for
    its FIRST pending step, state 'due' (scheduled time passed, no wait
    running), 'waiting' (refused before, its wait not over) or 'scheduled'
    (not due yet); ``(None, None)`` when no step is pending.  The steps
    after the first pending one wait for it.

    The checker (send_due_followups) sends by it and the sales prompt
    counts overdue steps by it, so the count is what a check would send
    (review of 5970e07a1: the prompt counted a step queued behind a waiting
    one as overdue)."""
    for step in sequence.get('steps', []):
        if step.get('status') != 'pending':
            continue
        if now < datetime.fromisoformat(step['scheduled_at']):
            return step, 'scheduled'
        return step, ('due' if retry_due(step, now) else 'waiting')
    return None, None


#: How many refused emails get_pipeline_status / view_journey_pipeline
#: list (the newest tries); the counts cover all of them.  Unbounded, 20
#: bounced prospects grew the journey view past the wire trim's cut, which
#: then dropped the A/B stats (review of 5970e07a1).
UNDELIVERED_LIST_LIMIT = 10


def _undelivered_entry(prospect_id, company, which, planned) -> Dict:
    """One refused planned email as the pipeline tools list it."""
    return {'prospect_id': prospect_id, 'company': company, 'which': which,
            'attempts': planned.get('attempts', 0),
            'given_up': planned.get('status') == 'failed',
            'retry_after': planned.get('retry_after'),
            'last_tried_at': planned.get('last_tried_at'),
            'last_error': planned.get('last_error')}


def undelivered_emails(data: Dict) -> List[Dict]:
    """Every planned email in the store that was refused and has still not
    reached its prospect: waiting to be tried again, or given up.

    A first email is listed only while the prospect has had no email at
    all (one delivered later, on a retry or by the agent's own
    send_outreach_email, is not undelivered).  A follow-up step is listed
    when it was given up, or while it waits in a sequence that is still
    active (a sequence ended by a reply sends nothing more, so its refused
    step is not 'waiting')."""
    prospects = data.get('prospects') or {}
    out = []
    for pid, p in prospects.items():
        draft = p.get('email_draft')
        if (isinstance(draft, dict) and draft.get('attempts')
                and not p.get('emails_sent')):
            out.append(_undelivered_entry(pid, p.get('company'),
                                          'first email', draft))
    for seq in (data.get('sequences') or {}).values():
        p = prospects.get(seq.get('prospect_id'), {})
        for step in seq.get('steps', []):
            if not step.get('attempts'):
                continue
            waiting = (step.get('status') == 'pending'
                       and seq.get('status') == 'active')
            if step.get('status') == 'failed' or waiting:
                out.append(_undelivered_entry(
                    seq.get('prospect_id'), p.get('company'),
                    f"follow-up step {step.get('step_number')}", step))
    return out


def undelivered_summary(data: Dict, limit: int = UNDELIVERED_LIST_LIMIT) -> Dict:
    """undelivered_emails as the pipeline tools show it: ``counts`` (given
    up, waiting) of all of them, and the ``limit`` most recently tried as
    ``latest``.

    The two go to different places in a tool's result.  The counts go at
    its HEAD: a long tool result meets two cuts, and both keep the head --
    the seats' token_limiter (hartos.helper, AUTOGEN_MESSAGE_TOKENS_PER_
    MESSAGE) keeps only the head, and the wire trim keeps the head and the
    tail.  The rows stay where they were.  Review of fe99feac3: counts
    moved to the tail survived the wire trim but reached the model less
    often than before through the seats' cut, which comes first."""
    entries = undelivered_emails(data)
    entries.sort(key=lambda e: e.get('last_tried_at') or '', reverse=True)
    given_up = sum(1 for e in entries if e['given_up'])
    return {'counts': {'given_up': given_up, 'waiting': len(entries) - given_up},
            'latest': entries[:limit]}


# ── Verified before it leaves: held drafts and goal copy ──
# Owner, 16 Nov 2026: "You verify all the outgoing emails on my behalf from
# central."  What a goal's email says is the owner's to approve, in two ways:
#   * the goal's TEMPLATES, the follow-up sequences its agent writes.  What the
#     owner verifies is the EXACT copy: the goal's config keeps the content
#     hash of each verified step (verified_copy; step_content_hash: subject
#     and body, whitespace normalised).  A sequence is created 'active' only
#     when every one of its steps' hashes is verified, else 'held', and its
#     steps show in the held list (list_held_sequence_steps).  The owner
#     verifies what is held for a goal (mark_goal_copy_verified): those
#     steps' hashes are added, and the held sequences whose steps are all
#     verified go 'active', the one state the follow-up checker sends.  It is
#     never a pass for the goal's future copy: new or changed text is held
#     again;
#   * every OTHER email -- words a model wrote this turn (send_outreach_email,
#     a reply), or the journey's own copy -- is kept as a held draft and not
#     sent, until the owner approves it (approve_held_draft), which sends
#     exactly that text.  On a node that is not flat this covers an email for
#     no goal too (_goalless_email_is_held).
# A held email costs no send attempt, and the agent is told it goes out once
# approved so it does not send it again.  The owner reads the held drafts
# (list_held_drafts) with subject and body, never the recipient's address.

#: What the agent that wrote a held email is told (the tool's 'note').
HELD_NOTE = (
    "This email is held for verification: it goes out once the owner "
    "approves it, exactly as written. It is not sent yet and nothing is "
    "lost. Do not send it again and do not send another version of it; keep "
    "working on other prospects.")
_HELD_FILE_NAME = 'outreach_held_drafts.json'
#: Longest reason a rejection keeps.
_REASON_CAP = 500
_held_lock = threading.RLock()


def _held_file() -> str:
    """The held drafts' file: beside the prospect store, in its own file, so
    a journey tick saving its older copy of the prospect store can never
    take a draft written meanwhile with it."""
    return os.path.join(os.path.dirname(os.path.abspath(_PROSPECT_FILE)),
                        _HELD_FILE_NAME)


def _read_held(path: str) -> Dict:
    """The held drafts file as a dict with 'drafts' (empty when there is
    none).  A file that cannot be read raises: reading it as empty and
    saving over it would lose the owner's drafts."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
    except FileNotFoundError:
        return {'drafts': {}}
    except (OSError, json.JSONDecodeError):
        logger.error("The held outreach drafts could not be read (%s); "
                     "nothing was changed", path, exc_info=True)
        raise
    if isinstance(loaded, dict) and isinstance(loaded.get('drafts'), dict):
        return loaded
    return {'drafts': {}}


@contextmanager
def _held_drafts():
    """The held drafts (a dict by draft id), locked for the caller's
    read-modify-write and saved when the block ends without raising."""
    with _held_lock:
        path = _held_file()
        data = _read_held(path)
        yield data['drafts']
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)


def _goalless_email_is_held() -> bool:
    """Whether an email for no goal is held for the owner like a goal's:
    on a node that is not flat (node_topology.is_flat_node: central,
    regional, or any tier the rule does not know), where the owner verifies
    what leaves.  A flat node is one person's machine and its email is that
    person's own."""
    from integrations.social.node_topology import is_flat_node
    return not is_flat_node()


def step_content_hash(subject, body_html) -> str:
    """The identity of one follow-up step's copy: a hash of its subject and
    body with whitespace normalised (runs of it are one space, ends cut), so
    re-wrapping the same words is the same copy and any other character is
    not.  What the owner verifies (config verified_copy) and what a
    sequence's steps are checked against."""
    normalised = '\n'.join(' '.join(str(part or '').split())
                           for part in (subject, body_html))
    return hashlib.sha256(normalised.encode('utf-8')).hexdigest()


def _steps_hashes(steps) -> set:
    """The content hashes of a sequence's steps (dicts with 'subject' and
    'body_html')."""
    return {step_content_hash(s.get('subject'), s.get('body_html'))
            for s in steps or []}


def _steps_verified(sender: Optional[Sender], steps, goalless_held: bool) -> bool:
    """Whether every step of a sequence is verified copy: its goal's
    ``verified`` hashes name them all.  A sequence of no goal has nothing to
    verify it against: fine on a flat node, never on one that holds
    goal-less email (``goalless_held``)."""
    if sender is None:
        return not goalless_held
    return sender.found and _steps_hashes(steps) <= sender.verified


def _pending_first_touch(drafts: Dict, prospect_id) -> Optional[str]:
    """The id of ``prospect_id``'s first email that is waiting on the owner
    (held, or being sent), in ``drafts``; None when there is none."""
    for draft_id, draft in drafts.items():
        if (draft.get('prospect_id') == prospect_id and draft.get('first_touch')
                and draft.get('status') in ('held', 'sending')):
            return draft_id
    return None


def held_first_touch(prospect_id) -> Optional[str]:
    """The id of ``prospect_id``'s first email waiting on the owner, or None:
    what the journey asks before it writes its own first email."""
    with _held_lock:
        return _pending_first_touch(_read_held(_held_file())['drafts'],
                                    prospect_id)


def _hold_email(goal_id: Optional[str], prospect_id: Optional[str], subject: str,
                body_html: str) -> Dict:
    """Keep an email (a goal's, ``goal_id``; None: no goal, on a node that
    holds those too) for the owner's approval instead of sending it:
    ``{'success': False, 'held': True, 'draft_id': ...}`` (_send_email's
    result shape).  The same email asked again is the same draft; one the
    owner already rejected or sent is refused, saying so.  The recipient's
    address is not kept (the prospect is: approve_held_draft resolves it)
    and is never logged."""
    if not prospect_id:
        return _refused_send(
            "this email names no prospect, so it cannot be held for the "
            "owner's verification",
            "Send it to a prospect of the goal (create_prospect first), so "
            "the owner can approve it." + _NEVER_ANOTHER_MAILBOX, via='held')
    with _held_drafts() as drafts:
        for draft_id, draft in drafts.items():
            if (draft.get('prospect_id'), draft.get('subject'),
                    draft.get('body_html')) != (prospect_id, subject, body_html):
                continue
            if draft.get('status') == 'held':
                return {'success': False, 'held': True, 'draft_id': draft_id,
                        'via': 'held'}
            if draft.get('status') == 'rejected':
                return _refused_send(
                    f"the owner rejected this exact email: "
                    f"{draft.get('reason') or 'no reason given'}",
                    "Write a different email that answers that, and send "
                    "that one.", via='held')
            return _refused_send(
                "this exact email was already sent to this prospect",
                "Write a different email, or move on to the next prospect.",
                via='held')
        # A prospect nobody has emailed has ONE pending first email: while it
        # is held it counts as that prospect's first touch, so the next turn
        # does not write a second (review of a93ead3cf, S2: both were listed,
        # and approving both sent two first emails to one person).
        first_touch = not ((_load_prospects().get('prospects') or {}).get(
            prospect_id) or {}).get('emails_sent')
        pending = _pending_first_touch(drafts, prospect_id) if first_touch else None
        if pending is not None:
            return dict(_refused_send(
                "a first email for this prospect is already held for the "
                "owner's approval",
                "Do not write another: it goes out once the owner approves "
                "it, exactly as written. If it is wrong, the owner rejects it "
                "with a reason and you then write a better one. Keep working "
                "on other prospects.", via='held'),
                pending_first_touch=True, draft_id=pending)
        draft_id = uuid.uuid4().hex[:12]
        drafts[draft_id] = {
            'draft_id': draft_id, 'goal_id': goal_id,
            'prospect_id': prospect_id, 'subject': subject,
            'body_html': body_html, 'status': 'held',
            'first_touch': first_touch,
            'created_at': datetime.utcnow().isoformat()}
    logger.info("Outreach email held for verification: draft %s of goal %s",
                draft_id, goal_id)
    return {'success': False, 'held': True, 'draft_id': draft_id, 'via': 'held'}


def list_held_drafts(goal_id: Optional[str] = None,
                     status: str = 'held') -> List[Dict]:
    """The held drafts (``status``: held, sent or rejected; ``goal_id``
    narrows to one goal), oldest first, as the owner reads them: the
    subject and body as written, the prospect's company, and for a rejected
    one its reason.  Never the recipient's address."""
    with _held_lock:
        drafts = _read_held(_held_file())['drafts']
    rows = [dict(d) for d in drafts.values()
            if d.get('status') == status
            and (goal_id is None or d.get('goal_id') == goal_id)]
    prospects = _load_prospects().get('prospects', {})
    for row in rows:
        row['company'] = (prospects.get(row.get('prospect_id')) or {}).get('company')
    rows.sort(key=lambda d: d.get('created_at') or '')
    return rows


def approve_held_draft(draft_id: str, approved_by: str) -> Dict:
    """The owner approves a held draft: its exact text is sent to its
    prospect through _send_email (verified: held no more), so the outgoing
    check and the goal's mailbox rules still apply.  ``{'sent': True, ...}``;
    otherwise ``{'sent': False, 'error': ...}`` and a draft that could not go
    stays held.  One writer: the admin route and anything else that
    approves comes here."""
    with _held_drafts() as drafts:
        draft = drafts.get(str(draft_id))
        if draft is None:
            return {'sent': False, 'error': f'no held draft {draft_id}'}
        if draft.get('status') != 'held':
            return {'sent': False, 'draft_id': draft['draft_id'],
                    'error': f"draft {draft_id} is {draft.get('status')}, not held"}
        draft['status'] = 'sending'      # a second approval cannot send it twice
        draft = dict(draft)
    sent = False
    try:
        data = _load_prospects()
        prospect = data['prospects'].get(draft['prospect_id'])
        if not prospect:
            return {'sent': False, 'draft_id': draft['draft_id'],
                    'error': 'its prospect is no longer in the store'}
        if draft.get('first_touch') and prospect.get('emails_sent'):
            # Another email reached this person since this one was held as
            # their first: sending it would be a second first email.
            return {'sent': False, 'draft_id': draft['draft_id'],
                    'error': 'this prospect has been emailed since this first '
                             'email was held, so it would be a second first '
                             'email: reject it with a reason, and the agent '
                             'can write a follow-up instead'}
        result = _send_email(prospect['email'], draft['subject'],
                             draft['body_html'], sender=prospect_sender(prospect),
                             prospect_id=draft['prospect_id'], verified=True)
        if not result.get('success'):
            return {'sent': False, 'draft_id': draft['draft_id'],
                    'error': _send_error_text(result)}
        sent = True
        now = datetime.utcnow()
        data = _load_prospects()
        prospect = data['prospects'].get(draft['prospect_id'])
        if prospect:
            data.setdefault('sent_log', []).append({
                'prospect_id': draft['prospect_id'], 'to': prospect['email'],
                'subject': draft['subject'], 'sequence_step': 1,
                'sent_at': now.isoformat(), 'send_result': result})
            count_delivered(prospect, now)
            mark_contacted(prospect)
            _save_prospects(data)
        return {'sent': True, 'draft_id': draft['draft_id'], 'send_result': result}
    finally:
        with _held_drafts() as drafts:
            kept = drafts.get(draft['draft_id'])
            if kept is not None:
                kept['status'] = 'sent' if sent else 'held'
                if sent:
                    kept['approved_by'] = str(approved_by)
                    kept['sent_at'] = datetime.utcnow().isoformat()


def reject_held_draft(draft_id: str, reason: Optional[str],
                      rejected_by: str) -> Dict:
    """The owner turns a held draft down; ``reason`` (required) is kept with
    it, and the agent that writes the same email again is told why."""
    reason = (reason or '').strip()[:_REASON_CAP]
    if not reason:
        return {'success': False,
                'error': 'a rejection needs its reason: say what is wrong '
                         'with the email, so the agent can write a better one'}
    with _held_drafts() as drafts:
        draft = drafts.get(str(draft_id))
        if draft is None:
            return {'success': False, 'error': f'no held draft {draft_id}'}
        if draft.get('status') != 'held':
            return {'success': False,
                    'error': f"draft {draft_id} is {draft.get('status')}, not held"}
        draft.update(status='rejected', reason=reason,
                     rejected_by=str(rejected_by),
                     rejected_at=datetime.utcnow().isoformat())
    return {'success': True, 'draft_id': str(draft_id), 'status': 'rejected'}


def _held_sequences(data: Dict, goal_id: str) -> List[Dict]:
    """The held follow-up sequences of ``goal_id``'s prospects, in ``data``."""
    prospects = data.get('prospects', {})
    return [seq for seq in data.get('sequences', {}).values()
            if seq.get('status') == 'held'
            and (prospects.get(seq.get('prospect_id')) or {}).get('goal_id') == goal_id]


def list_held_sequence_steps(goal_id: Optional[str] = None) -> List[Dict]:
    """The copy waiting for the owner's verification: one row per distinct
    step (its content hash) of the held follow-up sequences (``goal_id``
    narrows to one goal; None: every goal, and the sequences of prospects no
    goal holds): subject and body as written, how many held sequences use it,
    and the companies they are for.  Never an address.  Read-only: what the
    owner sees here is what mark_goal_copy_verified verifies."""
    data = _load_prospects()
    prospects = data.get('prospects', {})
    rows = {}
    for seq_id, seq in data.get('sequences', {}).items():
        if seq.get('status') != 'held':
            continue
        prospect = prospects.get(seq.get('prospect_id')) or {}
        owner_goal = prospect.get('goal_id')
        if goal_id is not None and owner_goal != goal_id:
            continue
        for step in seq.get('steps', []):
            content_hash = step_content_hash(step.get('subject'), step.get('body_html'))
            row = rows.setdefault((owner_goal, content_hash), {
                'goal_id': owner_goal, 'content_hash': content_hash,
                'subject': step.get('subject'), 'body_html': step.get('body_html'),
                'sequence_ids': set(), 'companies': set()})
            row['sequence_ids'].add(seq_id)
            if prospect.get('company'):
                row['companies'].add(prospect['company'])
    return [dict({k: v for k, v in row.items() if k not in ('sequence_ids', 'companies')},
                 sequences=len(row['sequence_ids']),
                 companies=sorted(row['companies']))
            for row in rows.values()]


def mark_goal_copy_verified(goal_id: str, verified_by: str,
                            content_hashes=None) -> Dict:
    """The owner verifies the copy held for a goal: the content hashes of the
    steps of its HELD follow-up sequences, as they are at this moment, are
    added to the goal's config ``verified_copy`` (GoalManager.update_goal),
    and the held sequences whose steps are now all verified go 'active' (the
    follow-up checker sends only those).  ``content_hashes`` limits it to the
    steps the owner saw in list_held_sequence_steps, so a sequence made since
    is not verified unseen.  It is never a pass for the goal's future copy: a
    sequence made later is active only if each of its steps has the same
    copy (whitespace aside) as one verified; any other is held.  Emails the
    agent writes outside a sequence are held (held drafts), verified or
    not.

    The ONLY writer of the goal's verified_copy (and its who and when): its
    route is behind the admin gate (on central, the operator), and
    GoalManager.create_goal and update_goal keep every other caller from
    writing those keys, a POST /api/goals body included."""
    from integrations.agent_engine.goal_manager import GoalManager
    from integrations.social.models import AgentGoal, db_session
    goal_id = str(goal_id)
    data = _load_prospects()
    held = _held_sequences(data, goal_id)
    wanted = set()
    for seq in held:
        wanted |= _steps_hashes(seq.get('steps'))
    if content_hashes is not None:
        wanted &= {str(h) for h in content_hashes}
    with db_session() as db:
        goal = db.query(AgentGoal).filter_by(id=goal_id).first()
        if goal is None:
            return {'success': False, 'error': f'no goal {goal_id} on this node'}
        config = dict(goal.config_json or {})
        verified = set(config.get('verified_copy') or []) | wanted
        # The ONE writer of these keys: update_goal takes them only from here
        # (``_verified_copy``); any other caller's config keeps what is stored.
        updated = GoalManager.update_goal(
            db, goal_id, config_json=config,
            _verified_copy={'verified_copy': sorted(verified),
                            'copy_verified_by': str(verified_by),
                            'copy_verified_at': datetime.utcnow().isoformat()})
    if not updated.get('success'):
        return {'success': False, 'error': updated.get('error') or 'not updated'}
    released = 0
    for seq in held:
        if _steps_hashes(seq.get('steps')) <= verified:
            seq['status'] = 'active'
            released += 1
    if released:
        _save_prospects(data)
    logger.info("Goal %s: %d step(s) of copy verified, %d held sequence(s) "
                "released", goal_id, len(wanted), released)
    return {'success': True, 'goal_id': goal_id, 'steps_verified': len(wanted),
            'sequences_released': released}


def _new_sequence_status(prospect: Dict, steps) -> str:
    """The status a follow-up sequence of ``prospect`` with these ``steps``
    starts in: 'active' only when every step's copy is verified for its goal
    (_steps_verified), else 'held' (a goal that is not on this node counts as
    unverified)."""
    sender = prospect_sender(prospect)
    goalless_held = _goalless_email_is_held() if sender is None else False
    return 'active' if _steps_verified(sender, steps, goalless_held) else 'held'


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

        prospect = _for_turn_goal({
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
        }, _turn_goal_id())

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

        Sends from the mailbox of the prospect's goal (the sender_email its
        config names); a prospect no goal holds goes through this node's own
        email sender.  A goal's email is HELD for the owner's verification
        and goes out once approved, exactly as written: the reply says so, and
        the email is not to be sent again.  A refused send says why and what
        the owner sets up.  Updates prospect stage to 'contacted' on first
        email (when it is sent).
        """
        data = _load_prospects()
        prospect = data['prospects'].get(prospect_id)
        if not prospect:
            return json.dumps({'success': False, 'error': f'Prospect {prospect_id} not found'})
        refused = refuse_other_goals_prospect('send_outreach_email', prospect)
        if refused is not None:
            return json.dumps(refused)

        result = _send_email(
            to_email=prospect['email'],
            subject=subject,
            html_body=body_html,
            sender=prospect_sender(prospect),
            prospect_id=prospect_id,
        )
        if result.get('held'):
            # Kept for the owner, not sent and not refused: the prospect
            # stays as it was, and the agent is told not to send it again.
            return json.dumps({
                'success': True, 'sent': False, 'held': True,
                'draft_id': result['draft_id'], 'note': HELD_NOTE,
                'prospect_stage': prospect['stage']})
        if not result.get('success'):
            # Not sent: the prospect stays as it was and the agent reads
            # why.  Counting it moved prospects nobody had reached to
            # 'contacted' and used up their follow-ups.
            return json.dumps({
                'success': False, 'sent': False, 'send_result': result,
                'prospect_stage': prospect['stage'],
                'error': 'the email was not sent: ' + _send_error_text(result),
            })

        # Log the send
        now = datetime.utcnow()
        send_entry = {
            'prospect_id': prospect_id,
            'to': prospect['email'],
            'subject': subject,
            'sequence_step': sequence_step,
            'sent_at': now.isoformat(),
            'send_result': result,
        }
        data['sent_log'].append(send_entry)

        # Update prospect state
        count_delivered(prospect, now)
        mark_contacted(prospect)

        _save_prospects(data)
        return json.dumps({'success': True, 'sent': True, 'send_result': result,
                           'prospect_stage': prospect['stage']})

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
        refused = refuse_other_goals_prospect('create_followup_sequence', prospect)
        if refused is not None:
            return json.dumps(refused)

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
                # pending -> sent, or failed after OUTREACH_SEND_ATTEMPTS
                # refused sends (note_refused), which ends the sequence;
                # a reply completes the whole sequence.
                'status': 'pending',
                'sent_at': None,
            })

        sequence = {
            'id': sequence_id,
            'prospect_id': prospect_id,
            'name': sequence_name,
            'steps': scheduled_steps,
            'created_at': datetime.utcnow().isoformat(),
            # Active only when every step's exact copy is verified for the
            # goal (_new_sequence_status), else held until the owner verifies
            # it (mark_goal_copy_verified); only an active one is sent.
            # active -> completed (every step done, or a reply), or
            # failed (a step given up: the address or the sender is not
            # taking mail, so later steps are not sent to it).
            'status': _new_sequence_status(prospect, scheduled_steps),
            'exit_on_reply': True,
        }

        data['sequences'][sequence_id] = sequence
        _save_prospects(data)

        reply = {'success': True, 'sequence': sequence}
        if sequence['status'] == 'held':
            reply['note'] = (
                "This follow-up sequence is held for verification: the owner "
                "sees its steps, and its emails are sent once the owner "
                "verifies that copy. Nothing is lost; do not create it again. "
                "Changed text is held again, so reuse the verified wording "
                "where you can."
                + ("" if prospect.get('goal_id') else
                   " This prospect belongs to no goal, so on this node it "
                   "cannot be verified: create it in the turn of the goal it "
                   "is for."))
        return json.dumps(reply)

    def check_pending_followups() -> str:
        """Check the active sequences for follow-ups that are due (in a
        goal's turn, only that goal's prospects' sequences).

        Sends emails for due steps, skips sequences where the prospect has replied.
        Returns summary of actions taken.
        """
        return _check_pending_followups_impl(_turn_goal_id())

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
        # A goal's prospects are written to only in that goal's own turns:
        # another goal's (or a person's) turn moved a speech prospect to
        # 'won' / 'lost' (review of 88b6821f2, S6).
        refused = refuse_other_goals_prospect('move_prospect_stage', prospect)
        if refused is not None:
            return json.dumps(refused)

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
        """Get the outreach pipeline status — the prospects grouped by stage
        (in a goal's turn, only that goal's own).

        Merges local prospect data with Erxes CRM pipeline view.
        """
        turn_goal = _turn_goal_id()
        data = store_seen_by(_load_prospects(), turn_goal)
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
                # The goal it belongs to: a goal writes only to its own.
                'goal_id': prospect.get('goal_id'),
            })

        # Count active sequences
        active_sequences = sum(
            1 for s in data.get('sequences', {}).values()
            if s.get('status') == 'active'
        )

        # Include Erxes CRM status if available.  It holds every goal's
        # deals and cannot be told apart by goal: a goal's turn is not shown it.
        erxes_status = None
        erxes = _get_erxes() if turn_goal is None else None
        if erxes:
            try:
                erxes_status = erxes.get_pipeline_status()
            except Exception as e:
                erxes_status = {'error': str(e)}

        # Refused emails, waiting to be tried again or given up: the counts
        # at the head, where every cut keeps them (undelivered_summary).
        not_delivered = undelivered_summary(data)
        return json.dumps({
            'success': True,
            'not_delivered_count': not_delivered['counts'],
            'pipeline': pipeline,
            'total_prospects': len(data.get('prospects', {})),
            'active_sequences': active_sequences,
            'not_delivered': not_delivered['latest'],
            'erxes_pipeline': erxes_status,
        })

    def bulk_import_prospects(
        prospects_json: Annotated[str, "JSON array of prospect dicts: [{company, contact_name, email, title?, vertical?, notes?, tier?}, …]"],
        dedupe_by_email: Annotated[bool, "Skip rows whose email already exists in the prospect store"] = True,
        attach_sequence: Annotated[Optional[str], "Optional JSON array of follow-up sequence steps (delay_days, subject, body_html); attached to every imported prospect"] = None,
    ) -> str:
        """Bulk-import a batch of prospects in one shot.

        Volume lever for the marketing flywheel: a single tool call seeds
        the outreach daemon with dozens of leads + their follow-up
        sequence.  Without this, the agent has to call create_prospect +
        create_followup_sequence individually for each lead — too many
        round-trips for any practical campaign.

        Each row must have at least: company, contact_name, email.
        Optional: title, vertical, notes, tier (1=top priority, 2=secondary).

        When `attach_sequence` is provided, an identical follow-up
        sequence is attached to every newly imported prospect (the
        sequence runs independently per prospect).  Use this to set up
        a 3-touch campaign (day 3 / day 7 / day 14) across the whole
        batch in one tool call.

        Returns counts of imported, skipped (deduped), and failed rows,
        plus any sequence-ids that were created.
        """
        try:
            rows = json.loads(prospects_json)
            if not isinstance(rows, list):
                return json.dumps({'success': False, 'error': 'prospects_json must be a JSON array'})
        except json.JSONDecodeError as e:
            return json.dumps({'success': False, 'error': f'Invalid JSON: {e}'})

        seq_steps = None
        if attach_sequence:
            try:
                seq_steps = json.loads(attach_sequence)
                if not isinstance(seq_steps, list):
                    return json.dumps({'success': False, 'error': 'attach_sequence must be a JSON array'})
            except json.JSONDecodeError as e:
                return json.dumps({'success': False, 'error': f'Invalid attach_sequence JSON: {e}'})

        data = _load_prospects()
        existing_emails = {
            (p.get('email') or '').strip().lower()
            for p in data.get('prospects', {}).values()
        }

        imported, skipped, failed = [], [], []
        created_sequences = []
        # Every row imported in a goal's turn is that goal's prospect: its
        # email goes from the goal's own mailbox (prospect_sender).
        turn_goal = _turn_goal_id()
        # What every sequence of this batch starts as (they share the goal
        # and the steps): held until the owner verifies that copy.
        new_sequence_status = (_new_sequence_status({'goal_id': turn_goal}, seq_steps)
                               if seq_steps else 'active')

        for idx, row in enumerate(rows):
            try:
                company = (row.get('company') or '').strip()
                contact_name = (row.get('contact_name') or '').strip()
                email = (row.get('email') or '').strip()
                if not (company and contact_name and email):
                    failed.append({'row': idx, 'reason': 'missing company/contact_name/email'})
                    continue
                if dedupe_by_email and email.lower() in existing_emails:
                    skipped.append({'row': idx, 'email': email, 'reason': 'duplicate'})
                    continue

                prospect_id = f"{company.lower().replace(' ', '_')}_{int(time.time()*1000)}_{idx}"
                prospect = _for_turn_goal({
                    'id': prospect_id,
                    'company': company,
                    'contact_name': contact_name,
                    'email': email,
                    'title': (row.get('title') or '').strip(),
                    'vertical': (row.get('vertical') or 'general').strip(),
                    'notes': (row.get('notes') or '').strip(),
                    'tier': int(row.get('tier', 1)),
                    'stage': 'new',
                    'created_at': datetime.utcnow().isoformat(),
                    'updated_at': datetime.utcnow().isoformat(),
                    'created_by': user_id,
                    'emails_sent': 0,
                    'last_email_at': None,
                    'last_reply_at': None,
                }, turn_goal)
                data['prospects'][prospect_id] = prospect
                existing_emails.add(email.lower())
                imported.append({'row': idx, 'id': prospect_id,
                                 'company': company, 'email': email})

                # Optional: attach the same follow-up sequence to every
                # imported prospect.  The daemon's check_pending_followups
                # walks all active sequences and fires the due steps.
                if seq_steps:
                    seq_id = f"seq_{prospect_id}_{int(time.time()*1000)}"
                    base_time = datetime.utcnow()
                    cumulative_days = 0
                    scheduled = []
                    for step_idx, step in enumerate(seq_steps):
                        delay = int(step.get('delay_days', 3))
                        cumulative_days += delay
                        scheduled.append({
                            'step_number': step_idx + 2,
                            'delay_days': delay,
                            'scheduled_at': (base_time + timedelta(days=cumulative_days)).isoformat(),
                            'subject': step.get('subject', ''),
                            'body_html': step.get('body_html', ''),
                            'status': 'pending',
                            'sent_at': None,
                        })
                    data['sequences'][seq_id] = {
                        'id': seq_id,
                        'prospect_id': prospect_id,
                        'name': f'Bulk-import sequence for {company}',
                        'steps': scheduled,
                        'created_at': datetime.utcnow().isoformat(),
                        'status': new_sequence_status,
                        'exit_on_reply': True,
                    }
                    created_sequences.append(seq_id)
            except Exception as row_err:
                failed.append({'row': idx, 'reason': str(row_err)})

        _save_prospects(data)

        # Sync each newly imported prospect to Erxes CRM in background-
        # safe sequence — failures here don't abort the import, just
        # report sync state per row.
        sync_results = []
        for row in imported:
            prospect = data['prospects'][row['id']]
            sync = _sync_prospect_to_crm(prospect)
            sync_results.append({'id': row['id'], 'synced': sync.get('synced', False)})
            if sync.get('synced'):
                prospect['erxes_customer_id'] = sync.get('erxes_customer_id')
                prospect['erxes_deal_id'] = sync.get('erxes_deal_id')
        _save_prospects(data)

        return json.dumps({
            'success': True,
            'imported_count': len(imported),
            'skipped_count': len(skipped),
            'failed_count': len(failed),
            'sequences_created': len(created_sequences),
            'imported': imported,
            'skipped': skipped,
            'failed': failed,
            'erxes_sync': sync_results,
        })

    def list_sent_emails(
        prospect_id: Annotated[Optional[str], "Filter by prospect ID (optional)"] = None,
        limit: Annotated[int, "Max emails to return"] = 20,
    ) -> str:
        """List sent outreach emails, optionally filtered by prospect (in a
        goal's turn, only those to that goal's own prospects)."""
        data = store_seen_by(_load_prospects(), _turn_goal_id())
        log = data.get('sent_log', [])

        if prospect_id:
            log = [e for e in log if e.get('prospect_id') == prospect_id]

        # Most recent first
        log = sorted(log, key=lambda x: x.get('sent_at', ''), reverse=True)[:limit]
        return json.dumps({'success': True, 'emails': log, 'total': len(log)})

    # ── Register all tools ──
    # Routed through core.labeled_autogen_function so the UI spinner
    # shows a tool-specific label (publish_chat_stage('tool_call', …))
    # for autogen-invoked tools too.  Same shape as LangChain's
    # _with_tool_logging chokepoint.  UI labels live in
    # core/constants.py:TOOL_LABELS (single source of truth).
    from core.labeled_autogen_function import register_labeled_function
    from core.constants import TOOL_LABELS

    for func in [
        create_prospect,
        bulk_import_prospects,
        send_outreach_email,
        create_followup_sequence,
        check_pending_followups,
        move_prospect_stage,
        get_pipeline_status,
        list_sent_emails,
    ]:
        register_labeled_function(
            func,
            caller=helper,
            executor=assistant,
            description=func.__doc__,
            ui_label=TOOL_LABELS.get(func.__name__, f'Running {func.__name__}…'),
        )

    logger.info(f"Registered 8 outreach CRM tools for user {user_id}")


def _check_pending_followups_impl(goal_id: Optional[str] = None) -> str:
    """Send the follow-ups that are due, on the store as it is on disk now:
    only ``goal_id``'s prospects' (a goal's own turn; store_seen_by), or
    every prospect's when None (the daemon's tick, a person's turn).

    Returns JSON string with actions taken. Used by both the tool-level
    check_pending_followups and the daemon-level check_pending_followups_daemon.
    """
    data = _load_prospects()
    now = datetime.utcnow()
    actions_taken = send_due_followups(store_seen_by(data, goal_id), now)
    _save_prospects(data)
    return json.dumps({'success': True, 'actions': actions_taken, 'checked_at': now.isoformat()})


def send_due_followups(data: Dict, now: datetime,
                       prospect_id: Optional[str] = None) -> List[Dict]:
    """Send the follow-up steps that are due in ``data`` (the prospect store
    as the caller holds it), one step per sequence; only ``prospect_id``'s
    sequences when given.  Changes ``data`` in place and saves nothing: the
    caller that loaded the store saves it.

    The journey engine runs this on the copy it is ticking (review of
    1c4b2b650): the checker loading and saving the file on its own inside
    a journey tick had its writes overwritten by the tick's older copy, so
    a delivered step went out again and a refused one was counted twice
    as often as OUTREACH_SEND_ATTEMPTS allows.
    """
    actions_taken = []
    senders = {}        # goal id -> Sender, one read per goal per check
    goalless_held = None    # _goalless_email_is_held, read once when needed

    for seq_id, sequence in data.get('sequences', {}).items():
        if sequence['status'] != 'active':
            continue
        if prospect_id is not None and sequence.get('prospect_id') != prospect_id:
            continue

        prospect = data['prospects'].get(sequence['prospect_id'])
        if not prospect:
            continue

        # Only a sequence whose every step is verified copy is sent (owner,
        # 16 Nov: every outgoing email is verified before it leaves): the
        # goal's verified hashes name each step's exact text.  One made
        # active before that rule, by hand, or with its text changed since,
        # waits too, and its steps use no send attempt.  A prospect of no
        # goal is sent only on a flat node (_steps_verified).
        goal_id = prospect.get('goal_id')
        if goal_id and goal_id not in senders:
            senders[goal_id] = prospect_sender(prospect)
        sender = senders.get(goal_id) if goal_id else None
        if goalless_held is None and sender is None:
            goalless_held = _goalless_email_is_held()
        if not _steps_verified(sender, sequence.get('steps'), bool(goalless_held)):
            sequence['status'] = 'held'
            actions_taken.append({
                'action': 'sequence_held',
                'reason': 'copy_not_verified',
                'prospect': prospect['company'],
            })
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

        # The sequence's next step (only its first pending one), once per
        # check cycle (next_followup: the one rule the prompt counts by).
        step, state = next_followup(sequence, now)
        if state == 'waiting':
            # Refused before and waiting out its wait: said, so a check
            # that sends nothing tells the agent why.
            actions_taken.append({
                'action': 'followup_waiting',
                'prospect': prospect['company'],
                'step': step['step_number'],
                'attempts': step.get('attempts', 0),
                'retry_after': step.get('retry_after'),
                'error': step.get('last_error'),
            })
        elif state == 'due':
            result = _send_email(
                to_email=prospect['email'],
                subject=step['subject'],
                html_body=step['body_html'],
                sender=sender,
                prospect_id=sequence['prospect_id'],
                verified=True,
            )
            if result.get('success'):
                step['status'] = 'sent'
                step['sent_at'] = now.isoformat()
                count_delivered(prospect, now)
                actions_taken.append({
                    'action': 'followup_sent',
                    'prospect': prospect['company'],
                    'step': step['step_number'],
                    'subject': step['subject'],
                    'result': result,
                })
            else:
                # Not sent: tried again after a wait, then given up
                # (note_refused, which logs it).  A step given up ends the
                # sequence: the address or the sender is not taking mail,
                # and the later steps would only be refused the same way.
                gave_up = note_refused(
                    step, result, now,
                    f"{prospect['company']}, follow-up step {step['step_number']}")
                if gave_up:
                    sequence['status'] = 'failed'
                actions_taken.append({
                    'action': 'followup_failed',
                    'prospect': prospect['company'],
                    'step': step['step_number'],
                    'attempts': step['attempts'],
                    'gave_up': gave_up,
                    'error': step['last_error'],
                })

        # Check if all steps are done
        if (sequence['status'] == 'active'
                and all(s['status'] != 'pending' for s in sequence['steps'])):
            sequence['status'] = 'completed'

    return actions_taken


# ── Goal Type Registration ──

def build_outreach_prompt(goal_dict: Dict, product_dict: Dict = None) -> str:
    """The prompt of an 'outreach' goal: the sales agent's
    (journey_engine._build_sales_prompt).

    'outreach' and 'sales' were two goal types with two builders for one
    job, and both pitched HARTOS-for-robotics only (#233/#234); this one
    also listed every prospect's email address in the prompt.  One builder
    now serves both goal types: it pitches the goal's own product and
    keeps the goal's title and description, which this one showed."""
    from integrations.agent_engine.journey_engine import _build_sales_prompt
    return _build_sales_prompt(goal_dict, product_dict)


def register_outreach_goal_type():
    """Register 'outreach' as a goal type in the agent engine.

    Call this during HARTOS boot to enable outreach automation.
    """
    from integrations.agent_engine.goal_manager import register_goal_type
    # 'sales' too: this goal type's prompt IS the sales prompt
    # (build_outreach_prompt), which tells the agent to call the journey tools
    # (view_journey_pipeline, run_journey_tick) beside the CRM ones, and its
    # goal turns get exactly these tags (goal_manager.daemon_turn_stored_tags,
    # #284).  No ServiceToolRegistry tool carries a 'sales' tag, so the Tier-1
    # filter unlocks nothing new for a detected 'outreach'.
    register_goal_type(
        goal_type='outreach',
        build_prompt=build_outreach_prompt,
        tool_tags=['outreach', 'sales', 'email', 'crm'],
    )
    logger.info("Registered 'outreach' goal type with prompt builder and tool tags")
    # Wire reply detection into email channel
    register_reply_handler()


def check_pending_followups_daemon() -> dict:
    """Module-level follow-up checker called by agent_daemon on each tick.

    Delegates to the tool-level check_pending_followups logic (DRY).
    Returns dict with 'sent' count.  Also attaches the reply hook once the
    email channel exists: boot wires it before channels are restored.
    """
    register_reply_handler()
    result_json = _check_pending_followups_impl()
    result = json.loads(result_json)
    sent = len([a for a in result.get('actions', []) if a.get('action') == 'followup_sent'])
    for action in result.get('actions', []):
        # A refused step is logged where it is refused (note_refused).
        if action.get('action') == 'followup_sent':
            logger.info(f"Follow-up sent: {action.get('prospect')} step {action.get('step')} subject='{action.get('subject')}'")
        elif action.get('action') == 'sequence_completed':
            logger.info(f"Sequence completed: {action.get('prospect')} ({action.get('reason')})")
    return {'sent': sent, 'checked_at': result.get('checked_at', '')}


# ═══════════════════════════════════════════════════════════════
# Inbound Reply Handler — wired into email channel adapter
# ═══════════════════════════════════════════════════════════════

def _prospect_for_reply(prospects: Dict, sender_email: str,
                        mailbox: Optional[str]) -> Optional[Dict]:
    """The prospect an inbound email from ``sender_email`` answers.  One
    address can be a prospect of two goals (two companies): a reply that
    came to a goal's mailbox answers that goal's prospect, else a prospect
    no goal holds; never another goal's.  ``mailbox`` None (the node's own
    email channel): the first prospect with that address, as before."""
    candidates = [p for p in prospects.values()
                  if p.get('email', '').lower() == sender_email.lower()]
    if mailbox is None:
        return candidates[0] if candidates else None
    from integrations.channels.registry import normalize_account
    for prospect in candidates:
        sender = prospect_sender(prospect)
        if sender is not None and normalize_account(sender.address) == mailbox:
            return prospect
    return next((p for p in candidates if not p.get('goal_id')), None)


def handle_inbound_email(sender_email: str, subject: str, body: str,
                         message_id: str = '', mailbox: Optional[str] = None) -> Optional[Dict]:
    """Check if an inbound email matches a prospect. If so, update CRM and trigger response flow.

    Called by the email channel adapter's _process_email hook; ``mailbox``
    is the goal mailbox it came to (None: the node's own email channel).
    Returns match info if this is a prospect reply, None otherwise.
    """
    data = _load_prospects()

    matched_prospect = _prospect_for_reply(
        data.get('prospects', {}), sender_email, mailbox)

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
        if (sequence.get('prospect_id') == matched_prospect['id']
                and sequence['status'] in ('active', 'held')):
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
            f"5. Write the reply with send_outreach_email for this prospect. "
            f"It is held for the owner's approval and goes out only once "
            f"approved, so it is safe to write; do not send it again.\n"
        )

        # A prospect of a goal is answered in that goal's own turn: only
        # there may a tool write to its prospects, and the reply is held for
        # the owner like any email the goal's agent writes.  One no goal
        # holds keeps a turn of its own (it can write nothing to a goal).
        dispatch_goal(
            prompt=prompt,
            user_id=user_id,
            goal_id=(prospect.get('goal_id')
                     or f"outreach_reply_{prospect['id']}_{int(time.time())}"),
            goal_type='outreach',
        )
    except Exception as e:
        logger.error(f"Failed to dispatch response draft: {e}")


async def _outreach_reply_hook(message, mailbox: Optional[str] = None):
    """Post-process an inbound email for prospect matching: the Message the
    email adapter builds, the subject in ``raw``, the message id in ``id``.
    ``mailbox``: the goal mailbox it came to.  A prospect's reply it takes
    is marked HANDLED, so a mailbox that leaves its owner's mail unread
    marks only that one read."""
    sender = getattr(message, 'sender_id', '') or ''
    raw = getattr(message, 'raw', None)
    subject = (raw or {}).get('subject', '')
    body = getattr(message, 'text', '') or ''
    msg_id = getattr(message, 'id', '') or ''
    if '@' not in sender:
        return
    if handle_inbound_email(sender, subject, body, msg_id, mailbox=mailbox) is not None \
            and isinstance(raw, dict):
        from integrations.channels.extensions.email_adapter import HANDLED
        raw[HANDLED] = True


def register_reply_handler():
    """Register the inbound email handler with every email adapter: the
    node's own email channel and each goal mailbox (ChannelRegistry
    accounts), once per adapter, so a reply to any of them reaches its
    prospect.

    Called at boot (from register_outreach_goal_type), before channels are
    restored, and again on every outreach tick
    (check_pending_followups_daemon), which is what attaches it once a
    mailbox is up, and to each mailbox connected later.
    """
    try:
        import functools
        from integrations.channels.registry import get_registry
        registry = get_registry()
        hooks = [(registry.get(EMAIL_CHANNEL), _outreach_reply_hook)]
        hooks += [(adapter, functools.partial(_outreach_reply_hook, mailbox=address))
                  for address, adapter in registry.accounts(EMAIL_CHANNEL).items()]
        for adapter, hook in hooks:
            if adapter is None or not hasattr(adapter, 'on_message'):
                continue
            if getattr(adapter, '_outreach_reply_hooked', False):
                continue
            adapter.on_message(hook)
            adapter._outreach_reply_hooked = True
            logger.info("Outreach reply handler registered with an email adapter")
    except Exception as e:
        logger.warning(f"Could not register the outreach reply handler: {e}")
