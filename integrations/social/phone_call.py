"""A person's phone starts and ends a voice call with their agent on this
desktop, over its device link.

Phone to desktop goes only over the PeerLink link (owner 2026-10-06), and a
call's media rides this desktop's LiveKit, its signalling tunnelled over that
link (livekit_link).  The social call routes cannot serve the phone: they
accept only tokens this node signed, and the gate leaves /api/social/ to
them.  So the phone asks here, on the same 'tunnel' channel, as the person
its device link was admitted for (link.user_id), and the desktop does what
those routes would:

  call_open {id, agent_id}  answered call_opened {id, call_id, url, token}
  call_end  {id, call_id}   answered call_ended {id, call_id}

or call_refused {id, reason[, detail]}: 'bad_request', 'calls_off'
(calls_v1 is off here), 'no_agent' (no such agent), 'not_allowed' (the call
or grant rules said no; detail says which), 'not_found', 'no_room' (no room
token could be made; detail says why), or 'error'.  A frame from anything
but a person's own phone (livekit_link.device_link: a device link that
names its person) is not answered.

call_open, in order: the person's DM with the agent (ConversationService.
create returns the one that exists); the agent's leave to speak in it
(CallService.grant_agent, whose rules decide: this machine's owner, or the
agent's owner -- a grant that already has can_voice is left as it is); the
DM's voice call (CallService.create returns the active one) with the person
in it; the agent in it (CallService.attach_agent); and the person's room
token (LiveKitService.issue_token -- with an agent in the call the room is
LiveKit's, as api_calls decides).  The token's url names this desktop's
loopback; the phone reaches it through its tunnel.  A refused grant leaves
the DM, as starting one from the conversations API would.
"""
import logging
from typing import Optional

from core.peer_link.channels import CALL_END, CALL_OPEN

from .livekit_link import device_link

logger = logging.getLogger('hevolve_social')

CALL_OPENED = 'call_opened'
CALL_ENDED = 'call_ended'
CALL_REFUSED = 'call_refused'
#: The device requests this module answers (the node's handshake names them).
ANSWERS = (CALL_OPEN, CALL_END)


def _refused(rid: str, reason: str, detail: Optional[str] = None) -> dict:
    reply = {'type': CALL_REFUSED, 'id': rid, 'reason': reason}
    if detail:
        reply['detail'] = detail
    return reply


def _calls_on(db) -> bool:
    """calls_v1, from the source the call routes read (auth.require_auth)."""
    from .feature_flags import get_flags_for_tenant
    return bool(get_flags_for_tenant(db, None).get('calls_v1'))


def _is_agent(db, agent_id: str) -> bool:
    from sqlalchemy import text
    row = db.execute(text("SELECT user_type FROM users WHERE id = :id"),
                     {'id': agent_id}).fetchone()
    return row is not None and row[0] == 'agent'


def _open(db, rid: str, person: str, agent_id) -> dict:
    from .call_service import CallError, CallService
    from .conversation_service import ConversationError, ConversationService
    from .livekit_service import LiveKitService
    if not isinstance(agent_id, str) or not agent_id:
        return _refused(rid, 'bad_request')
    if not _is_agent(db, agent_id):
        return _refused(rid, 'no_agent')
    try:
        conv = ConversationService.create(db, 'dm', [agent_id], person)
    except ConversationError as e:
        return _refused(rid, 'not_allowed', str(e))
    grant = CallService.get_active_grant(db, agent_id, 'conversation', conv['id'])
    scope = dict((grant or {}).get('scope') or {})
    try:
        if not scope.get('can_voice'):
            scope['can_voice'] = True
            CallService.grant_agent(db, agent_id, person, 'conversation',
                                    conv['id'], scope)
        call = CallService.create(db, 'conversation', conv['id'], person,
                                  kind='voice')
        CallService.join(db, call['id'], person)
        CallService.attach_agent(db, call['id'], agent_id)
    except CallError as e:
        return _refused(rid, 'not_allowed', str(e))
    token = LiveKitService.issue_token(call['id'], person, can_publish=True)
    if token.get('mode') != 'livekit' or not token.get('token'):
        # No one can join this room; the call holds the agent for nothing.
        CallService.end(db, call['id'], person)
        return _refused(rid, 'no_room', token.get('reason') or token.get('mode'))
    logger.info("Phone call %s opened for %s with agent %s", call['id'],
                person, agent_id)
    return {'type': CALL_OPENED, 'id': rid, 'call_id': call['id'],
            'url': token['url'], 'token': token['token']}


def _end(db, rid: str, person: str, call_id) -> dict:
    from .call_service import CallError, CallService
    if not isinstance(call_id, str) or not call_id:
        return _refused(rid, 'bad_request')
    try:
        CallService.end(db, call_id, person)
    except CallError as e:
        reason = 'not_found' if str(e) == 'not found' else 'not_allowed'
        return _refused(rid, reason, str(e))
    logger.info("Phone call %s ended by %s", call_id, person)
    return {'type': CALL_ENDED, 'id': rid, 'call_id': call_id}


def handle_call_frame(channel: str, data, peer_id: str) -> Optional[dict]:
    """The 'tunnel' handler for call_open / call_end.  Every other frame, and
    any frame from anything but a person's own phone, is not answered."""
    if not isinstance(data, dict) or data.get('type') not in ANSWERS:
        return None
    link = device_link(peer_id)
    if link is None:
        return None
    rid = data.get('id')
    if not isinstance(rid, str) or not 0 < len(rid) <= 64:
        return _refused('', 'bad_request')
    person = link.user_id     # get_device_link answers only a link with one
    from .models import db_session
    try:
        with db_session() as db:
            if not _calls_on(db):
                reply = _refused(rid, 'calls_off')
            elif data['type'] == CALL_OPEN:
                reply = _open(db, rid, person, data.get('agent_id'))
            else:
                reply = _end(db, rid, person, data.get('call_id'))
    except Exception as e:
        logger.warning("Phone call request %s from %s failed: %s", data['type'],
                       peer_id[:12], e, exc_info=True)
        return _refused(rid, 'error', str(e))
    if reply['type'] == CALL_REFUSED:
        logger.info("Phone call request %s from %s refused: %s %s", data['type'],
                    peer_id[:12], reply['reason'], reply.get('detail', ''))
    return reply
