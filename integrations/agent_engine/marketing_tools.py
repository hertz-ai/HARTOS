"""
Unified Agent Goal Engine - Marketing Tools (Tier 2)

These tools are loaded ONLY when the agent is working on a marketing goal.
They wrap existing CampaignService, AdService, PostService, and channel adapters.

Tier 1 (Default): google_search, text_2_image, delegate_to_specialist, etc.
Tier 2 (Category): create_social_post, create_campaign, create_ad, post_to_channel
Tier 3 (Runtime): delegate_to_specialist finds agents with needed skills via A2A
"""
import json
import logging
import re
import threading
from types import MappingProxyType
from typing import Annotated, Optional

from core.intelligence_preference import IntelligencePreference

logger = logging.getLogger('hevolve_social')


# Consent type that gates EXTERNAL publishing. The platform keeps humans in
# control: nothing leaves to a third-party site under the operator's identity
# unless the operator has granted standing ``public_exposure`` consent. The
# tool docstrings have always *promised* this gate ("Gate every EXTERNAL post on
# operator consent before calling"); this makes the promise real + enforced
# instead of prose the LLM may ignore. The autonomous marketing goals post as
# the system identities, whose switch is HEVOLVE_AUTONOMOUS_MARKETING: 1 grants
# at boot, 0 revokes at boot (goal_seeding.apply_autonomous_marketing_flag);
# POST /api/social/consent only reaches the caller's own identity.
_EXTERNAL_POST_CONSENT = 'public_exposure'


def _external_post_allowed(user_id: str) -> bool:
    """True iff the operator granted standing ``public_exposure`` consent.

    Single gate for every EXTERNAL channel publish (``post_to_channel``,
    ``post_to_channel_via_browser``). Internal on-platform posts
    (``create_social_post``) are NOT gated — they never leave the platform.

    Fail-closed: any error (no consent row, DB unavailable) denies the external
    post, so the default posture is "do not post off-platform" until a human
    opts in. That is the correct posture for an autonomous agent that can reach
    the public internet under the operator's identity.
    """
    try:
        from integrations.social.consent_service import ConsentService
        from integrations.social.models import db_session
        with db_session() as db:
            return bool(ConsentService.check_consent(
                db, str(user_id), _EXTERNAL_POST_CONSENT, scope='*'))
    except Exception as e:
        logger.warning(
            "external-post consent check failed for user %s (denying): %s",
            user_id, e)
        return False


def _consent_required_result(channel: str) -> str:
    """Canonical 'blocked — needs operator consent' result for external posts."""
    return json.dumps({
        'success': False,
        'ok': False,
        'consent_required': _EXTERNAL_POST_CONSENT,
        'channel': channel,
        'error': (
            f"External posting to {channel} requires standing operator consent "
            f"('{_EXTERNAL_POST_CONSENT}'). The operator enables it: "
            f"HEVOLVE_AUTONOMOUS_MARKETING=1 for the system identities, or "
            f"POST /api/social/consent "
            f"{{'consent_type':'{_EXTERNAL_POST_CONSENT}','scope':'*'}} for "
            f"their own. Until then, post on-platform with create_social_post "
            f"and note the gap."
        ),
    })


def _is_video_url(url) -> bool:
    """Does ``url`` name a video file?  The channels' one media rule
    (integrations.channels.registry.media_kind)."""
    from integrations.channels.base import MessageType
    from integrations.channels.registry import media_kind
    kind = media_kind(url)
    return kind is not None and kind[1] == MessageType.VIDEO


def _not_posted(channel, error, **extra) -> str:
    """The answer for a post that did not happen."""
    return json.dumps(dict({'success': False, 'sent': False,
                            'channel': channel, 'error': error}, **extra))


#: What the agent is told while a post waits on the owner: the step is not
#: done, and calling again is how it carries on.
_NOT_DONE = ("Until it posts, this post has not happened; do not report it "
             "as done. Call post_to_channel again later: it carries on from "
             "the owner's answer.")

#: post_to_channel's own connects of a channel run one at a time:
#: _connect_or_ask holds the channel's lock from its "still connecting?"
#: check through register_channel, so two posts at once start it once
#: (review of 1feb76f14: both registered, the second rebuilding the adapter
#: mid-connect).  Connect_Channel, the connect form and OAuth do not take
#: this lock (#226).
_CONNECT_LOCKS = {}
_CONNECT_LOCKS_GUARD = threading.Lock()


def _connect_lock(channel: str) -> threading.Lock:
    with _CONNECT_LOCKS_GUARD:
        return _CONNECT_LOCKS.setdefault(channel, threading.Lock())


def _connecting_now(channel: str) -> bool:
    """Whether the latest start of ``channel`` (agent_tools.start_verdict)
    is still being watched.  Only Discord reports CONNECTING, so the watch
    is what says the others are mid-connect."""
    from integrations.channels.agent_tools import START_WATCHING, start_verdict
    return (start_verdict(channel) or {}).get('state') == START_WATCHING


def _owner_declined(owner, name) -> bool:
    """Whether credential ``name`` stands refused: the owner said no to
    providing it or took it back, or the vault stopped asking after the
    values they gave were refused too often (request_credential revokes the
    grant then).  The vault's own record: ConsentService.declined on scope
    'secret:NAME'."""
    try:
        from integrations.social.consent_service import (
            CREDENTIAL_SCOPE_PREFIX, ConsentService)
        from integrations.social.models import db_session
        with db_session(commit=False) as db:
            return bool(ConsentService.declined(
                db, str(owner), 'credential',
                scope=CREDENTIAL_SCOPE_PREFIX + name, agent_id=None))
    except Exception:
        logger.warning("credential %s: the owner's answer could not be read",
                       name, exc_info=True)
        return False


def _owner_entered_at(owner, name):
    """When the owner last entered credential ``name``: the newest entry
    row for scope 'secret:NAME', by the vault's own rule for what an entry
    is (ai_key_vault._recent_entries: granted_at set, not taken back with
    "Allow asking again").  None when they never did or it cannot be read."""
    try:
        from hartos.ai_key_vault import _reopened
        from integrations.social.consent_service import (
            CREDENTIAL_SCOPE_PREFIX, ConsentService)
        from integrations.social.models import db_session
        scope = CREDENTIAL_SCOPE_PREFIX + name
        with db_session(commit=False) as db:
            times = [row.granted_at for row in ConsentService.list_consents(
                         db, str(owner), 'credential')
                     if row.scope == scope and row.granted_at is not None
                     and not _reopened(row)]
        return max(times) if times else None
    except Exception:
        logger.warning("credential %s: when the owner entered it could not be "
                       "read", name, exc_info=True)
        return None


def _channel_turned_off(channel: str, meta: dict) -> Optional[str]:
    """The answer when the owner turned ``channel`` off in its channel
    settings (its admin entry says enabled false), else None.  Such a
    channel is not posted on, not reconnected (register_channel would turn
    it back on) and not asked about; before, it was asked about on every
    post (review of 506a79c1f)."""
    try:
        from integrations.channels.admin.api import get_api
        cfg = (getattr(get_api(), '_channels', {}) or {}).get(channel)
    except Exception:
        logger.warning("post_to_channel %s: channel settings unreadable",
                       channel, exc_info=True)
        return None
    if not isinstance(cfg, dict) or cfg.get('enabled', True) is not False:
        return None
    display = meta.get('display_name') or channel
    return _not_posted(
        channel, (f"{display} is turned off in its channel settings, so "
                  f"nothing was posted."),
        needs_owner=False, channel_off=True,
        next=(f"Nothing asked and nothing reconnected: the owner turned "
              f"{display} off. Do not post on {display} until they turn it "
              f"back on."))


def _connect_or_ask(channel: str, meta: dict, adapter=None) -> str:
    """``channel`` is not running here: connect it from what the owner
    entered, or ask the owner for it.  ``adapter`` is the channel's adapter
    in the live registry, if any.

    A channel mid-connect is left alone: an adapter reporting CONNECTING, or
    a start register_channel scheduled that is still being watched
    (_connecting_now).  Which channels can be connected from one value the
    owner types is integrations.channels.agent_tools.chat_connect_blocker,
    the rule register_channel starts adapters by; no card is filed for any
    other (a question whose answer can never start the channel).  The value
    is asked for through hartos.ai_key_vault.request_credential, the one
    credential ask: one card per credential, not one per agent.  Once the
    owner has entered it, the channel is connected through register_channel
    (the one registration path, as Connect_Channel calls it) under the
    owner's account, with the value itself: the model never holds it, and
    no unresolved {{secret:NAME}} ever becomes a token.

    The channel's latest start in this process (agent_tools.start_verdict:
    the value's fingerprint, the owner's stored value at that moment, when
    it started, how it ended) decides what the owner's value gets.  It is
    always tried when the owner entered it after that start, or when their
    stored value has changed since, by any path (review of b6dcae15e).
    Otherwise:
      * no start yet: connect with it;
      * started with this value and connected, since dropped: connect again;
      * started with this value and failed: not tried again; the owner is
        asked for a new one (request_credential's "rejected");
      * started with ANOTHER value (Connect_Channel, the connect form,
        OAuth): that newer choice is left alone and nothing is asked
        (review of 1feb76f14).
    Starts before a restart are not known here (#226).  A start
    register_channel did not schedule for this value (it refused, or the
    adapter could not be started here) asks nothing: no value the owner
    types would change it.  An owner who said no is not asked again.  A node
    with no owner connects nothing: there is nobody to connect it for.
    post_to_channel's own connects of a channel run one at a time
    (_connect_lock); other connect paths do not take that lock.
    """
    with _connect_lock(channel):
        return _connect_or_ask_locked(channel, meta, adapter)


def _connect_or_ask_locked(channel: str, meta: dict, adapter) -> str:
    """_connect_or_ask, under the channel's connect lock."""
    from integrations.channels.agent_tools import (
        LINKED_FROM_PHONE, SEVERAL_CREDENTIALS, START_FAILED,
        build_channel_tool_closures, chat_connect_blocker,
        credential_fingerprint, start_verdict)
    from integrations.channels.base import ChannelStatus
    from integrations.channels.metadata import required_setup_keys
    display = meta.get('display_name') or channel
    status = getattr(adapter, 'get_status', lambda: None)()
    if status == ChannelStatus.CONNECTING or _connecting_now(channel):
        # Started and not finished: a second register now would rebuild the
        # adapter mid-connect.
        return _not_posted(
            channel, f"{display} is still connecting, so nothing was posted yet.",
            connecting=True, next=_NOT_DONE)
    blocker = chat_connect_blocker(channel, meta)
    if blocker:
        kind, detail = blocker
        why = ({LINKED_FROM_PHONE: (f"{display} is linked from the owner's "
                                    f"phone (Connect_Channel starts it), not "
                                    f"from a value they type"),
                SEVERAL_CREDENTIALS: (f"{display} needs several credentials "
                                      f"({', '.join(detail or [])}), and "
                                      f"those cannot be connected from here")}
               .get(kind) or (f"this server cannot run {display} until the "
                              f"operator sets {', '.join(detail or [])}"))
        return _not_posted(
            channel, f"{display} is not connected, so nothing was posted: {why}.",
            needs_owner=False, cannot_connect_here=True,
            next=(f"Nothing asked: an answer would not start {display}. For a "
                  f"site the owner is signed in to on the desktop, "
                  f"post_to_channel_via_browser posts through their browser."))

    from hartos.ai_key_vault import get_ai_key_vault, request_credential
    from integrations.agent_engine.capability_setup import setup_owner
    owner = setup_owner()
    if not owner:
        return _not_posted(
            channel, (f"{display} is not connected, and this node has no owner "
                      f"to connect it for or to ask (HEVOLVE_OWNER_USER_ID is "
                      f"not set), so nothing was posted."),
            needs_owner=True,
            next=f"Nothing was asked. Do not report a post on {display}.")
    key = required_setup_keys(meta)[0]
    labels = {f.get('key'): f.get('label') or f.get('key')
              for f in meta.get('setup_fields') or []}
    label = f"{display} {labels.get(key) or key}"
    vault = get_ai_key_vault()
    name = vault.credential_name(key, channel)
    if _owner_declined(owner, name):
        return _not_posted(
            channel, (f"{display} is not connected, so nothing was posted: the "
                      f"owner said no to giving the {label} or took it back, "
                      f"or the values they gave were refused too many times "
                      f"and asking stopped."),
            needs_owner=False, owner_declined=True,
            next=(f"Do not post on {display} and do not ask for the {label} "
                  f"again. The owner can choose \"Allow asking again\" for it "
                  f"in Privacy settings."))

    def _ask(rejected=False):
        return request_credential(json.dumps(dict({
            'resource_type': 'channel_secret',
            'channel_type': channel,
            'key_name': key,
            'label': label,
            'used_by': 'post_to_channel',
            'description': f"Needed to post on {display}.",
        }, **({'rejected': True} if rejected else {}))), agent_id=None,
            tell_alias=False)

    value = vault.owner_credential(name)
    if value:
        fp = credential_fingerprint(value)
        latest = start_verdict(channel)
        entered = _owner_entered_at(owner, name)
        fresh = (latest is not None and entered is not None
                 and entered > latest['at'])
        owner_changed = (latest is not None
                         and latest.get('owner_fp') is not None
                         and latest['owner_fp'] != fp)
        if latest is not None and not fresh and not owner_changed:
            if latest.get('fp') != fp:
                # Last started with another value (Connect_Channel, the
                # connect form, OAuth), and the owner's own value is the
                # one they had then: writing it back would replace the
                # token that start chose, in the binding too (review of
                # 1feb76f14).  That start's own watcher told the owner how
                # it went.  An owner value changed since, by any path, is
                # theirs and is tried (owner_changed; review of b6dcae15e).
                return _not_posted(
                    channel, (f"{display} is not running, and it was last "
                              f"connected with a different {label} than the "
                              f"one the owner entered here, so nothing was "
                              f"posted."),
                    needs_owner=False, connected_elsewhere=True,
                    next=(f"Nothing asked and nothing reconnected: {display} "
                          f"was last connected another way, and that connect "
                          f"tells the owner how it went. Do not report a post "
                          f"on {display}."))
            if latest['state'] == START_FAILED:
                # This very value's start failed, and the owner has entered
                # nothing since: ask for another.
                return _not_posted(
                    channel, (f"{display} could not connect with the {label} "
                              f"the owner entered, so nothing was posted."),
                    needs_owner=True, asked_owner_for=name, value_refused=True,
                    owner_answer=_ask(rejected=True), next=_NOT_DONE)
            # It connected with this value and has since dropped.
        register = next(t[2] for t in build_channel_tool_closures(
            {'user_id': owner}) if t[0] == 'register_channel')
        started = vault.mask_secrets(register(channel, json.dumps({key: value})))
        after = start_verdict(channel)
        if (after is None or after.get('fp') != fp
                or after['start'] == (latest or {}).get('start')):
            # No start of this value was scheduled: register_channel
            # refused, or the adapter could not be started here.  Nothing
            # the owner types would change that, so nothing is asked.  A
            # start of another value landing meanwhile is not taken as
            # ours (review of 1feb76f14).  The agent never sees the value.
            return _not_posted(
                channel, (f"{display} could not be started here, so nothing "
                          f"was posted: {started}"),
                needs_owner=False, cannot_connect_here=True,
                next=(f"Nothing asked: a value from the owner would not "
                      f"start {display}. Do not report a post on {display}."))
        return _not_posted(
            channel, f"{display} was not connected, so nothing was posted yet.",
            connecting=True, register_channel=started,
            next=f"{display} is being connected with what the owner entered. "
                 + _NOT_DONE)
    return _not_posted(
        channel, f"{display} is not connected, so nothing was posted.",
        needs_owner=True, asked_owner_for=name, owner_answer=_ask(),
        next=_NOT_DONE)


#: post_to_channel's open question to the owner about where a channel's
#: posts go: channel -> ask id (integrations.agent_engine.person_ask keeps
#: the ask and its answer in this process, so this map lives beside it).
_DESTINATION_ASKS = {}
#: What the owner's last answer for a channel got when it could not be used
#: (set_announcement_target's refusal), told to them in the next question.
_DESTINATION_SAID = {}
_DESTINATION_LOCK = threading.Lock()


def _take_destination_answer(channel: str, answer, owner) -> Optional[str]:
    """Write the owner's answer for ``channel`` through
    set_announcement_target, the one writer.  None when the channel now has
    a destination, else what the writer said (kept for the next question)."""
    from integrations.channels.agent_tools import set_announcement_target
    chat_id = str(((answer or {}).get('values') or {}).get('chat_id') or '').strip()
    said = set_announcement_target(channel, chat_id, owner)
    if _announcement_targets(channel):
        with _DESTINATION_LOCK:
            _DESTINATION_SAID.pop(channel, None)
        return None
    with _DESTINATION_LOCK:
        _DESTINATION_SAID[channel] = said
    return said


def _apply_answer_when_given(channel: str, ask_id: str, owner) -> None:
    """Wait for the owner's answer to ``ask_id`` and write it the moment it
    comes.  person_ask keeps an answered ask for an hour and nothing across
    a restart, so leaving the answer for the agent's next call lost it when
    that call came later (review of f8fdb9c64).  Runs on a daemon thread."""
    from integrations.agent_engine import person_ask
    try:
        answer = person_ask.wait_for_answer(ask_id, person_ask.OPEN_KEEP_S)
        if answer is not None:
            _take_destination_answer(channel, answer, owner)
    except Exception:
        logger.warning("post_to_channel %s: the owner's destination answer "
                       "could not be saved", channel, exc_info=True)


def _announcement_targets(channel: str) -> list:
    """The chat ids ``channel``'s announcements go to, by
    announcement_broadcaster's destination rule (the owner's
    announce_chat_id; a one-to-one chat only with a subscription)."""
    from integrations.channels.announcement_broadcaster import (
        _collect_announcement_targets)
    return [chat_id for ch, chat_id in _collect_announcement_targets()
            if ch == channel]


def _ask_where_to_post(channel: str, meta: dict) -> Optional[str]:
    """No announcement destination is set for ``channel``: apply the owner's
    answer if they gave one (None then, so the post goes ahead), else ask
    them where posts go and say so.

    The question goes through person_ask (one card on every surface, a
    Firebase push, the answer back by its id); the answer is written the
    moment it comes (_apply_answer_when_given) by
    integrations.channels.agent_tools.set_announcement_target, the one
    writer of announce_chat_id, which refuses what the broadcaster would.
    A refused answer is named in the next question, so the owner knows why
    they are asked again."""
    from integrations.agent_engine import person_ask
    from integrations.agent_engine.capability_setup import setup_owner
    display = meta.get('display_name') or channel
    owner = setup_owner()
    if not owner:
        return _not_posted(
            channel, (f"{display} has no announcement destination and this "
                      f"node has no owner to ask (HEVOLVE_OWNER_USER_ID is not "
                      f"set), so nothing was posted."),
            needs_owner=True)
    with _DESTINATION_LOCK:
        ask_id = _DESTINATION_ASKS.get(channel)
    if ask_id:
        state = person_ask.ask_state(ask_id, owner)
        if state['status'] == 'open':
            return _not_posted(
                channel, (f"{display} has no announcement destination yet; "
                          f"the owner has been asked where posts go."),
                needs_owner=True, ask_id=ask_id, next=_NOT_DONE)
        with _DESTINATION_LOCK:
            _DESTINATION_ASKS.pop(channel, None)
        if state['status'] == 'answered' and _take_destination_answer(
                channel, state.get('answer'), owner) is None:
            return None
    with _DESTINATION_LOCK:
        refused = _DESTINATION_SAID.get(channel)
    description = (f"Your agent is ready to post on {display}. Give the group "
                   f"or channel it posts to.")
    if refused:
        description = f"Your last answer could not be used: {refused} " + description
    out = person_ask.ask(owner, 'post_to_channel', {
        'type': 'form',
        'title': f"Where should posts on {display} go?",
        'description': description,
        'fields': [{'name': 'chat_id', 'label': f"{display} group or channel",
                    'help': ("A group or channel people joined. Telegram groups "
                             "and channels have negative ids."),
                    'type': 'text'}],
        'submit_label': 'Save',
    })
    if not out.get('ok'):
        return _not_posted(
            channel, (f"{display} has no announcement destination, and the "
                      f"owner could not be asked ({out.get('refused')}), so "
                      f"nothing was posted."), needs_owner=True)
    new_ask = out['ask_id']
    with _DESTINATION_LOCK:
        first = _DESTINATION_ASKS.get(channel) != new_ask
        _DESTINATION_ASKS[channel] = new_ask
    if first and not out.get('reused'):
        threading.Thread(target=_apply_answer_when_given,
                         args=(channel, new_ask, owner),
                         name=f'post-destination-{channel}', daemon=True).start()
    return _not_posted(
        channel, (f"{display} has no announcement destination, so nothing was "
                  f"posted; the owner has been asked where posts go."),
        needs_owner=True, ask_id=new_ask, next=_NOT_DONE)


def _media_for(media_url):
    """The channel attachment for ``media_url``: a URL rides as a url, a
    file on this node as a file_path, through the reply path's [[MEDIA:]]
    rule (registry.extract_media_markers: files under the app's data dir
    only, a sendable type only).  A URL whose name does not say its type
    (no extension: a share page, a CDN id) is not an attachment; the post
    carries it as a link (_post_to_channel), as it did before the media
    rule, which refused the whole post for it (review of f8fdb9c64).
    Returns (attachments, error)."""
    if not media_url:
        return None, None
    from integrations.channels.base import MediaAttachment
    from integrations.channels.registry import extract_media_markers, media_kind
    text = str(media_url).strip()
    if text.lower().startswith(('http://', 'https://')):
        kind = media_kind(text)
        if kind is None:
            return None, None
        return [MediaAttachment(type=kind[1], url=text, mime_type=kind[0])], None
    _clean, media = extract_media_markers(f'[[MEDIA:{text}]]')
    if not media:
        return None, (f"{text} cannot be attached: a file on this node is sent "
                      f"only from the app's data folder, and only as an image, "
                      f"video, audio or PDF")
    return media, None


def _post_to_channel(channel, content, media_url=None) -> str:
    """Post ``content`` on ``channel``'s announcement destination, or ask
    the owner for what is missing (_connect_or_ask, _ask_where_to_post).

    The send goes through the live channel registry, the one every channel
    send uses (FlaskChannelIntegration.send_threadsafe), and each answer is
    the channel's own SendResult: a post is reported sent only when the
    channel says it was.  A send the channel has not confirmed within
    CHANNEL_SEND_WAIT_S may still go out, so it is reported pending with
    "do not post it again", never as not sent (a retry posted it twice).
    """
    import concurrent.futures
    from integrations.channels.flask_integration import (
        CHANNEL_SEND_WAIT_S, get_channel_integration)
    from integrations.channels.metadata import get_channel_metadata, list_all_channels
    channel = str(channel or '').strip().lower()
    meta = get_channel_metadata(channel)
    if not meta:
        return _not_posted(channel, (
            f"There is no {channel} channel to post through, so nothing was "
            f"posted. Channels: {', '.join(sorted(list_all_channels()))}. "
            f"For a site the owner is signed in to on the desktop, "
            f"post_to_channel_via_browser posts through their browser."))
    turned_off = _channel_turned_off(channel, meta)
    if turned_off:
        return turned_off
    from integrations.channels.announcement_broadcaster import (
        every_destination_needs_subscription)
    targets = _announcement_targets(channel)
    if not targets and every_destination_needs_subscription(channel):
        # No answer the owner can give on a card would be used, so nothing
        # is connected and nothing is asked (review of f8fdb9c64: every
        # answer was refused and asked again on every post).
        display = meta.get('display_name') or channel
        return _not_posted(
            channel, (f"{display} has no announcement destination, and on "
                      f"{display} announcements go only to a destination that "
                      f"subscribed to them, so nothing was posted."),
            needs_owner=False, cannot_post_here=True,
            next=(f"Nothing asked: no answer would let {display} post. Post on "
                  f"a channel with groups or channels people joined."))
    integration = get_channel_integration()
    adapter = integration.registry.get(channel)
    if adapter is None or not adapter.is_running():
        return _connect_or_ask(channel, meta, adapter)
    if not targets:
        waiting = _ask_where_to_post(channel, meta)
        if waiting is not None:
            return waiting
        targets = _announcement_targets(channel)
    media, problem = _media_for(media_url)
    if problem:
        return _not_posted(channel, f"Nothing was posted: {problem}.")
    if media_url and media is None:
        # A URL that is not a file of a sendable type: sent as a link.
        content = f"{content}\n{str(media_url).strip()}"
    posts = []
    for chat_id in targets:
        try:
            result = integration.send_threadsafe(
                channel, chat_id, content, wait=CHANNEL_SEND_WAIT_S, media=media)
        except concurrent.futures.TimeoutError:
            logger.warning("post_to_channel %s/%s: not confirmed after %ds; it "
                           "may still go out", channel, chat_id,
                           CHANNEL_SEND_WAIT_S)
            posts.append({'chat_id': chat_id, 'sent': None, 'pending': True})
            continue
        if result is None:
            return _not_posted(channel, (
                'The channel adapters are not running on this node, so nothing '
                'was posted.'), posts=posts)
        posts.append({'chat_id': chat_id, 'sent': bool(result.success),
                      'message_id': result.message_id, 'error': result.error})
    pending = any(p.get('pending') for p in posts)
    # None while a send may still land, as /channels/send's 202 answers it
    # (review of f8fdb9c64: a pending post was reported "sent": false).
    sent = (True if any(p['sent'] for p in posts)
            else None if pending else False)
    out = {'success': sent, 'sent': sent, 'pending': pending,
           'channel': channel, 'posts': posts}
    if pending:
        out['note'] = (f"{channel} has not confirmed every post yet; it may "
                       f"still appear. Do not post it again.")
    return json.dumps(out)


def _publish_instagram_feed(user_id, media_url, caption='', *, _consent=None):
    """Publish media to the Instagram FEED (Content Publishing API).

    DISTINCT from post_to_channel, which is messaging (send_message = a DM).  A
    feed post is a different Meta product: a video URL becomes a Reel
    (``publish_reel``), an image becomes a feed photo (``publish_photo``) — the
    daemon video-editor's whole output is reels, which had no code path before.
    Gated on the same standing ``public_exposure`` consent as every external
    post.

    It posts through the live Instagram adapter in the channel registry (the
    one every channel send uses), on the channel loop; a post not confirmed
    within CHANNEL_SEND_WAIT_S is pending, never failed.  It used to build a
    fresh adapter from the extension factory, which nothing ever connected,
    so every call answered "Not connected" (review of f8fdb9c64).
    ``_consent`` is a test seam; production reads real operator consent.
    """
    import asyncio
    import concurrent.futures
    consent = _consent or _external_post_allowed
    if not consent(user_id):
        return _consent_required_result('instagram')
    if not media_url:
        return json.dumps({'success': False,
                           'error': 'media_url is required for a feed post'})
    try:
        from integrations.channels.flask_integration import (
            CHANNEL_SEND_WAIT_S, get_channel_integration)
        from integrations.channels.metadata import get_channel_metadata
        is_video = _is_video_url(media_url)
        meta = get_channel_metadata('instagram') or {}
        turned_off = _channel_turned_off('instagram', meta)
        if turned_off:
            return turned_off
        integration = get_channel_integration()
        adapter = integration.registry.get('instagram')
        loop = integration.running_loop()
        if adapter is None or not adapter.is_running() or loop is None:
            return _connect_or_ask('instagram', meta, adapter)
        coro = (adapter.publish_reel(media_url, caption or '')
                if is_video else
                adapter.publish_photo(media_url, caption or ''))
        try:
            res = asyncio.run_coroutine_threadsafe(coro, loop).result(
                timeout=CHANNEL_SEND_WAIT_S)
        except concurrent.futures.TimeoutError:
            return json.dumps({
                'success': None, 'sent': None, 'pending': True,
                'channel': 'instagram',
                'kind': 'reel' if is_video else 'photo',
                'note': ("Instagram has not confirmed the post yet; it may "
                         "still appear. Do not post it again.")})
        return json.dumps({
            'success': bool(getattr(res, 'success', False)),
            'channel': 'instagram',
            'kind': 'reel' if is_video else 'photo',
            'media_id': getattr(res, 'message_id', None),
            'error': getattr(res, 'error', None),
        })
    except Exception as e:
        return json.dumps({'success': False, 'error': str(e)})


def _channel_arg_text() -> str:
    """post_to_channel's channel argument, naming the channels where no
    announcement can go out (announcement_broadcaster.PERSON_TO_PERSON_CHANNELS,
    the one list)."""
    from integrations.channels.announcement_broadcaster import (
        PERSON_TO_PERSON_CHANNELS)
    return ("Chat channel to post on: telegram, discord, viber ... (not "
            f"{', '.join(sorted(PERSON_TO_PERSON_CHANNELS))}: there "
            "announcements reach only people who subscribed; for twitter, "
            "linkedin and other sites, use post_to_channel_via_browser)")


def register_marketing_tools(helper, assistant, user_id: str):
    """Register marketing-specific tools with the agent (Tier 2).

    These wrap existing services — no new logic, just tool interfaces
    that let the agent use Campaign/Ad/Post services and channel adapters.

    Args:
        helper: AutoGen helper agent (registers for LLM)
        assistant: AutoGen assistant agent (registers for execution)
        user_id: Current user ID for ownership
    """
    _CHANNEL_ARG = _channel_arg_text()

    def create_social_post(
        title: Annotated[str, "Post title"],
        content: Annotated[str, "Post content/body — write as one real person to another; never AI/marketing-speak (it gets scrolled past)"],
        community_id: Annotated[Optional[str], "Community to post in (optional)"] = None,
        media_url: Annotated[Optional[str], "URL of media attachment (optional)"] = None,
    ) -> str:
        """Create a post on the HART social platform."""
        try:
            from integrations.social.models import get_db, Post
            db = get_db()
            try:
                post = Post(
                    author_id=str(user_id),
                    title=title,
                    content=content,
                    community_id=community_id,
                    media_url=media_url or '',
                )
                db.add(post)
                db.commit()
                result = post.to_dict()
                return json.dumps({'success': True, 'post': result})
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def create_campaign(
        name: Annotated[str, "Campaign name"],
        description: Annotated[str, "Campaign description and strategy"],
        campaign_type: Annotated[str, "Campaign type: awareness|engagement|conversion|retention"] = 'awareness',
        target_communities: Annotated[Optional[str], "Comma-separated community IDs to target"] = None,
        budget: Annotated[int, "Spark budget for this campaign"] = 50,
    ) -> str:
        """Create a marketing campaign using the Campaign Service."""
        try:
            from integrations.social.campaign_service import CampaignService
            from integrations.social.models import get_db
            db = get_db()
            try:
                targets = [t.strip() for t in target_communities.split(',')] if target_communities else []
                result = CampaignService.create_campaign(
                    db,
                    creator_id=str(user_id),
                    name=name,
                    description=description,
                    campaign_type=campaign_type,
                    target_communities=targets,
                    budget_spark=budget,
                )
                db.commit()
                return json.dumps({'success': True, 'campaign': result})
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def create_ad(
        title: Annotated[str, "Ad title"],
        content: Annotated[str, "Ad content/copy — write like a person recommending something real; never AI/marketing-speak"],
        target_url: Annotated[str, "URL the ad links to"],
        ad_type: Annotated[str, "Ad type: banner|sponsored|native"] = 'native',
        budget: Annotated[int, "Spark budget for this ad"] = 50,
        target_audience: Annotated[Optional[str], "Target audience description"] = None,
    ) -> str:
        """Create a targeted ad unit using the Ad Service."""
        try:
            from integrations.social.ad_service import AdService
            from integrations.social.models import get_db
            db = get_db()
            try:
                result = AdService.create_ad(
                    db,
                    advertiser_id=str(user_id),
                    title=title,
                    content=content,
                    target_url=target_url,
                    ad_type=ad_type,
                    budget_spark=budget,
                    targeting_json={'audience': target_audience} if target_audience else {},
                )
                db.commit()
                return json.dumps({'success': True, 'ad': result})
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def post_to_channel(
        channel: Annotated[str, _CHANNEL_ARG],
        content: Annotated[str, "Content to post — write as one real person to another; never AI/marketing-speak (it gets scrolled past)"],
        media_url: Annotated[Optional[str], "Media to include (optional): a URL, or a file this node made"] = None,
        extra_config: Annotated[Optional[str], "Not used: posts go to the group or channel the owner chose for this channel"] = None,
    ) -> str:
        """Post content on an external channel, in the group or channel the
        owner chose for it.

        The answer says whether the channel accepted it (sent true/false, or
        pending: not confirmed yet, may still appear, do not post again).
        When the channel is not connected, or no destination is set, nothing
        is posted: the owner is asked for what is missing, and calling again
        later carries on from their answer.

        Gated on standing operator ``public_exposure`` consent — returns a
        consent_required result (no post) until a human opts in.
        """
        if not _external_post_allowed(user_id):
            return _consent_required_result(channel)
        try:
            return _post_to_channel(channel, content, media_url)
        except Exception as e:
            logger.warning("post_to_channel %s: not posted: %s", channel, e)
            return _not_posted(channel, str(e))

    def publish_to_instagram_feed(
        media_url: Annotated[str, "URL of the video (.mp4/.mov -> Reel) or image (-> feed photo) to publish"],
        caption: Annotated[str, "Caption (trimmed to Instagram's 2200-char limit)"] = "",
    ) -> str:
        """Publish a video Reel or a photo to the Instagram FEED (not a DM).

        The daemon video-editor's publish step: hand it the finished reel's URL.
        Feed publishing is Meta's Content Publishing API, distinct from
        post_to_channel (which is messaging). Gated on standing operator
        public_exposure consent; needs a connected Instagram business account.
        """
        return _publish_instagram_feed(user_id, media_url, caption)

    def create_referral_campaign(
        name: Annotated[str, "Campaign name"],
        description: Annotated[str, "Campaign description and referral strategy"],
        referral_message: Annotated[str, "Shareable referral message for users"] = "Join me on HART!",
        target_communities: Annotated[Optional[str], "Comma-separated community IDs to target"] = None,
        budget: Annotated[int, "Spark budget for this campaign"] = 100,
    ) -> str:
        """Create a referral-driven growth campaign with auto-generated referral code."""
        try:
            from integrations.social.campaign_service import CampaignService
            from integrations.social.distribution_service import DistributionService
            from integrations.social.models import get_db
            db = get_db()
            try:
                # Generate referral code for the campaign owner
                ref_result = DistributionService.get_or_create_referral_code(db, str(user_id))
                ref_code = ref_result.get('code', '')

                targets = [t.strip() for t in target_communities.split(',')] if target_communities else []
                result = CampaignService.create_campaign(
                    db,
                    creator_id=str(user_id),
                    name=name,
                    description=description,
                    campaign_type='conversion',
                    target_communities=targets,
                    budget_spark=budget,
                )
                # Store referral mechanics in strategy.  Use the canonical
                # ``invite_share_url`` builder (single source of truth) so
                # G1's Invite_Friend tool, this referral campaign, and any
                # future invite surface all emit the same shape.  Honors
                # ``HEVOLVE_INVITE_BASE_URL`` env override automatically.
                if result and isinstance(result, dict):
                    from integrations.social.distribution_service import (
                        invite_share_url,
                    )
                    result['referral_code'] = ref_code
                    result['referral_message'] = referral_message
                    result['referral_link'] = (
                        invite_share_url(ref_code) if ref_code else ''
                    )

                db.commit()
                return json.dumps({'success': True, 'campaign': result})
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def get_growth_metrics() -> str:
        """Get platform growth metrics including viral coefficient (K factor)."""
        try:
            from integrations.social.models import get_db, User
            from integrations.social.models import Campaign, Referral
            from datetime import datetime, timedelta
            db = get_db()
            try:
                now = datetime.utcnow()
                week_ago = now - timedelta(days=7)

                total_users = db.query(User).count()
                new_users_7d = db.query(User).filter(User.created_at >= week_ago).count()

                # Referral metrics
                total_referrals = db.query(Referral).count()
                recent_referrals = db.query(Referral).filter(
                    Referral.created_at >= week_ago
                ).count()

                # Viral coefficient K = avg_referrals_per_user * conversion_rate
                users_with_referrals = db.query(Referral.referrer_id).distinct().count()
                avg_referrals = total_referrals / max(users_with_referrals, 1)
                conversion_rate = total_referrals / max(total_users, 1)
                k_factor = avg_referrals * conversion_rate

                # Top campaigns
                top_campaigns = db.query(Campaign).order_by(
                    Campaign.created_at.desc()
                ).limit(5).all()

                db.commit()
                return json.dumps({
                    'success': True,
                    'metrics': {
                        'total_users': total_users,
                        'new_users_7d': new_users_7d,
                        'total_referrals': total_referrals,
                        'recent_referrals_7d': recent_referrals,
                        'users_who_referred': users_with_referrals,
                        'avg_referrals_per_referrer': round(avg_referrals, 2),
                        'conversion_rate': round(conversion_rate, 4),
                        'viral_coefficient_k': round(k_factor, 4),
                        'k_status': 'exponential' if k_factor > 1 else 'sub-viral',
                        'top_campaigns': [c.to_dict() for c in top_campaigns],
                    }
                })
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def record_demo_video(
        duration_s: Annotated[int, "Seconds to record (2-60). Open + arrange the app you want to demo FIRST."] = 15,
        fps: Annotated[int, "Frames per second (5-12 is plenty for a UI demo)."] = 8,
    ) -> str:
        """Record a short screen-capture DEMO VIDEO of whatever is on screen right
        now (e.g. Nunba / HART OS running) and return the saved file path.

        Arrange the demo BEFORE calling: open the Nunba window (ideally maximized),
        and either start a scripted interaction with the desktop executor or have
        the thing you want to show already visible.  This captures the primary
        screen for `duration_s` and assembles a shareable mp4 (gif fallback).

        Then publish it: pass the returned ``path`` as ``media_url`` to
        ``post_to_channel(...)`` (external) or ``create_social_post(...)`` (platform).
        Returns JSON {ok, path, format, frames, fps, duration_s} or {ok, error}.
        """
        try:
            from integrations.remote_desktop.frame_capture import FrameCapture, FrameConfig
            _fps = max(1, min(int(fps), 30))
            _dur = max(2, min(int(duration_s), 60))
            cap = FrameCapture(FrameConfig(max_fps=_fps, scale_factor=0.75))
            res = cap.record_to_video(duration_s=_dur, fps=_fps)
            return json.dumps(res)
        except Exception as e:
            return json.dumps({'ok': False, 'error': str(e)})

    def post_to_channel_via_browser(
        channel: Annotated[str, "Platform with a web composer the user is logged into: twitter|linkedin|reddit|hackernews"],
        content: Annotated[Optional[str], "Text to post (defaults to the canonical body for that channel from marketing/intents)"] = None,
    ) -> str:
        """Post to a channel through the user's LOGGED-IN BROWSER via the VLM loop —
        the credential-free path for channels that have no API token.  Generic
        across every platform in marketing/intents (NOT LinkedIn-only): it opens
        the platform's composer URL, types the content, and clicks Post.

        Use this when post_to_channel reports the channel cannot be connected
        here (or has no catalog entry) and the user is logged into that site in
        their desktop browser.  Gated on
        standing operator ``public_exposure`` consent — returns a consent_required
        result (no post) until a human opts in.  Returns JSON
        {ok, platform, code, status, detail}.
        """
        if not _external_post_allowed(user_id):
            return _consent_required_result(channel)
        try:
            from integrations.marketing.browser_poster import post_to_platform_via_browser
            return json.dumps(post_to_platform_via_browser(
                channel, body=content, user_id=str(user_id)))
        except Exception as e:
            return json.dumps({'ok': False, 'error': str(e)})

    def verify_facts(
        claims_json: Annotated[str, "JSON list of {claim, source_url} — the source_url must be a page you actually fetched, not one you recall"],
    ) -> str:
        """Check each claim against text fetched live from its cited source.

        Returns the grounded claims and, separately, every rejected one with
        the reason it failed. Call this BEFORE composing anything for an
        audience: a claim that does not appear here did not survive, and
        publishing it anyway is the failure that put 416 fabricated posts on
        this platform.

        A rejection is a real result, not an obstacle to route around. If most
        claims are rejected, the research step is inventing them, and that is
        the thing worth reporting rather than quietly posting the remainder.
        """
        try:
            from integrations.agent_engine.grounded_facts import verify_all
            candidates = json.loads(claims_json)
            if isinstance(candidates, dict):
                candidates = [candidates]
            grounded, rejected = verify_all(candidates)
            return json.dumps({
                'success': True,
                'grounded': [f.to_dict() for f in grounded],
                'rejected': rejected,
                'grounded_count': len(grounded),
                'rejected_count': len(rejected),
            })
        except Exception as e:
            # Fail closed and say so: an error here must NOT read as "nothing
            # was wrong with the claims".
            return json.dumps({'success': False, 'error': str(e),
                               'grounded': [], 'rejected': []})

    # Register all marketing tools
    tools = [
        ('verify_facts', 'Ground claims against their cited sources before publishing; returns grounded facts and rejected ones with reasons', verify_facts),
        ('create_social_post', 'Create a post on the HART social platform for marketing', create_social_post),
        ('create_campaign', 'Create a marketing campaign with strategy, targeting, and budget', create_campaign),
        ('create_ad', 'Create a targeted ad unit with budget and audience targeting', create_ad),
        ('post_to_channel', 'Post content on a connected external channel (Telegram, Discord, Slack, ...); when it is not connected, asks the owner for what connecting it needs', post_to_channel),
        ('publish_to_instagram_feed', 'Publish a video Reel or photo to the Instagram FEED (Content Publishing API) — the daemon video-editor publish step; consent-gated, needs a connected IG business account', publish_to_instagram_feed),
        ('create_referral_campaign', 'Create a referral-driven growth campaign with auto-generated referral code', create_referral_campaign),
        ('get_growth_metrics', 'Get platform growth metrics including viral coefficient (K factor)', get_growth_metrics),
        ('record_demo_video', 'Record a short screen demo video of the app running; returns a file path to attach via post_to_channel/create_social_post media_url', record_demo_video),
        ('post_to_channel_via_browser', 'Post to a channel via the logged-in browser (VLM loop) when it has no API token — generic across twitter/linkedin/reddit/hackernews', post_to_channel_via_browser),
    ]

    for name, desc, func in tools:
        helper.register_for_llm(name=name, description=desc)(func)
        assistant.register_for_execution(name=name)(func)

    logger.info(f"Registered {len(tools)} marketing tools for user {user_id}")

    # Register skills so OTHER agents can discover and delegate TO this agent
    try:
        from integrations.internal_comm.internal_agent_communication import register_agent_with_skills
        register_agent_with_skills(f"marketing_{user_id}", [
            {'name': 'social_posting', 'description': 'Create posts on HART platform', 'proficiency': 0.9},
            {'name': 'campaign_management', 'description': 'Create and manage marketing campaigns', 'proficiency': 0.9},
            {'name': 'ad_creation', 'description': 'Create targeted ad units', 'proficiency': 0.9},
            {'name': 'channel_distribution', 'description': 'Post to external channels', 'proficiency': 0.8},
            {'name': 'referral_campaigns', 'description': 'Create referral-driven growth campaigns', 'proficiency': 0.9},
            {'name': 'growth_analytics', 'description': 'Analyze growth metrics and viral coefficient', 'proficiency': 0.85},
            {'name': 'demo_recording', 'description': 'Record screen demo videos of apps running for marketing', 'proficiency': 0.8},
        ])
    except Exception as e:
        logger.debug(f"Marketing skill registration skipped: {e}")


#: The goal tag of a Reel Studio agent: her own Instagram-reel editor
#: (integrations/reel_studio).
REEL_STUDIO_TAG = 'reel_studio'

#: Goal tags a client may ASK for when it creates an agent (the /chat body's
#: ``goal_tags``: the phone puts her agent's on every turn of it,
#: ReelInbox.markTurn, and stamp_goal_tags stamps the ones the turn that
#: saves the record carries; nothing holds them between turns).  Every other
#: tag is only ever detected from the goal text, never taken from a request.
#: Read through the one filter, user_assignable_goal_tags: Nunba's
#: turn_goal_tags forwards what that filter keeps of a turn's tags and keeps
#: no list or filter of its own.
USER_ASSIGNABLE_GOAL_TAGS = frozenset({REEL_STUDIO_TAG})

#: The inference preference a goal tag sets for its agent (decision U4), in
#: the node's one vocabulary (core.intelligence_preference).  A reel agent
#: handles her clients' faces: its turns keep to this node and its record is
#: not synced to the cloud prompt list.  Read through goal_tags_preference.
GOAL_TAG_PREFERENCES = MappingProxyType({REEL_STUDIO_TAG: IntelligencePreference.LOCAL_ONLY})

#: What her Reel Studio agent's own words trip by accident, and the phrases
#: that mean she really asked for that family (plan C8 / WS-8a step 6).  Her
#: goal names songs (media: music generation), her brand and engagement
#: (marketing: campaigns, ads) and following up with brides (outreach: an
#: email CRM); none of those families edits a reel, and each costs schema
#: room the reel family needs.  Applied only to an agent whose STORED tags
#: carry reel_studio (amendment A8 b).
_REEL_STUDIO_KEEPS = {
    'media': ('compose', 'melody', 'jingle', 'vocals', 'tts', 'voiceover',
              'voice over'),
    'marketing': ('campaign', 'advertis', 'lead gen', 'email marketing'),
    'outreach': ('email sequence', 'cold email', 'crm'),
}


def _reel_studio_inert() -> bool:
    """Is reel_studio meaningless on this node?  True on the CENTRAL node
    (amendment A8 d): central runs the network's own commerce, marketing and
    outreach goals, which must keep every tool family and their cloud sync,
    and no one edits her reels there.  There the tag grants no tools, quiets
    no family, keeps no agent local-only and is never stamped.  The node's
    topology is security.key_delegation.get_node_tier (central only with the
    master key), asked at call time.  The one test resolve_goal_tags,
    is_local_only and stamp_goal_tags ask."""
    from security.key_delegation import get_node_tier
    return get_node_tier() == 'central'


_WORD_START_CACHE = {}


def _mentions(lower: str, keywords) -> bool:
    """True if any keyword occurs at a WORD START in ``lower``.

    The detector used a plain ``kw in lower``, so a keyword fired from
    anywhere inside a longer word.  Measured over the 705 saved agent
    goals in ~/Documents/Nunba/data/prompts: 'ad ' matched inside
    "re[ad ]engagement" (282 goals -> marketing), 'sing' inside
    "u[sing]" (46 -> media), 'repo' inside "re[po]rt" (47 -> coding).
    361 spurious tag assignments, and the tag set decides which Tier-2
    tool pack create_recipe.py:2047-2084 registers -- so a mis-tagged
    goal is authored against the wrong toolset.

    Only the START is anchored.  Several keywords are deliberate stems
    ('market' -> "marketing", 'advertis' -> "advertising") and must keep
    matching.  Keywords are used VERBATIM, so the trailing space the
    lists already rely on in 'ad ' / 'ads ' / 'contact ' keeps serving
    as their word end.
    """
    for kw in keywords:
        pat = _WORD_START_CACHE.get(kw)
        if pat is None:
            pat = _WORD_START_CACHE[kw] = re.compile(
                r'(?<![a-z0-9])' + re.escape(kw))
        if pat.search(lower):
            return True
    return False


#: A thought experiment's id (uuid4, ThoughtExperimentService.create_experiment).
_EXPERIMENT_ID_RE = re.compile(
    r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b')


def _names_a_known_experiment(lower: str) -> bool:
    """True when ``lower`` holds the id of a thought experiment that exists.
    Asked only after a vote word matched, so a turn without one never
    reaches the database.  A lookup that fails reads as False and is logged:
    the tool is then not attached for this turn, which request_tools can
    still undo."""
    ids = _EXPERIMENT_ID_RE.findall(lower)
    if not ids:
        return False
    try:
        from integrations.social.models import ThoughtExperiment, db_session
        with db_session(commit=False) as db:
            return db.query(ThoughtExperiment.id).filter(
                ThoughtExperiment.id.in_(ids)).first() is not None
    except Exception as e:
        logger.warning("experiment id lookup for a vote turn failed: %s", e)
        return False


def detect_goal_tags(prompt) -> list:
    """Detect goal type tags from a prompt for category-based tool loading.

    Returns list of tags like ['marketing'], ['coding'], or [] for general.

    Accepts any input — the autogen agent-creation path
    (``create_recipe.py:1735``) sometimes hands HARTOS's own
    ``helper.Action`` object (``helper.py:1363``, the multi-step task
    tracker) instead of a raw string.  Without coercion,
    ``prompt.lower()`` raises ``AttributeError: 'Action' object has no
    attribute 'lower'`` and the surrounding ``except`` swallows it
    silently — leaving the agent with NO goal-specific tools
    registered, so the LLM produces prose instead of taking real
    actions.  Root cause for ~6+ weeks of marketing goals "completing"
    without any outreach side-effects.
    """
    lower = str(prompt).lower()
    tags = []

    marketing_keywords = [
        'market', 'campaign', 'advertis', 'promotion', 'social media',
        'content marketing', 'brand', 'outbound', 'inbound', 'lead gen',
        'email marketing', 'seo', 'influencer', 'viral', 'engagement',
        'conversion', 'target audience', 'marketing goal', 'ad ', 'ads ',
    ]
    if _mentions(lower, marketing_keywords):
        tags.append('marketing')

    coding_keywords = [
        'github', 'repository', 'codebase', 'refactor', 'implement',
        'bug fix', 'pull request', 'commit', 'branch', 'repo ',
    ]
    if _mentions(lower, coding_keywords):
        tags.append('coding')

    ip_keywords = [
        'patent', 'trademark', 'copyright', 'intellectual property',
        'ip protection', 'infringement', 'prior art', 'cease and desist',
        'dmca', 'filing', 'provisional patent', 'claims',
    ]
    if _mentions(lower, ip_keywords):
        tags.append('ip_protection')

    self_build_keywords = [
        'self-build', 'self_build', 'nixos', 'nix package', 'install package',
        'runtime.nix', 'os rebuild', 'nixos-rebuild', 'system package',
        'hart-pkg', 'nix-env', 'system generation', 'rollback generation',
    ]
    if _mentions(lower, self_build_keywords):
        tags.append('self_build')

    outreach_keywords = [
        'outreach', 'prospect', 'cold email', 'follow up', 'follow-up',
        'sales email', 'pipeline', 'crm', 'lead gen', 'deal stage',
        'send email to', 'contact ', 'outbound email', 'drip', 'sequence',
        'partnership outreach', 'b2b', 'sales funnel',
    ]
    if _mentions(lower, outreach_keywords):
        tags.append('outreach')

    sales_keywords = [
        'sales agent', 'marketing agent', 'flywheel', 'user journey',
        'a/b test', 'ab test', 'prospect journey', 'sales pipeline',
        'meeting schedule', 'deal close', 'partner onboard',
        'channel outreach', 'multi-channel', 'nurture',
    ]
    if _mentions(lower, sales_keywords):
        tags.append('sales')

    # News curation / feed-refresh tools.  Keyed on distinctive phrases the
    # news prompt (_build_news_prompt) and the `bootstrap_herald_news_friend`
    # seed actually emit — deliberately NOT the bare substring 'news', which
    # would false-fire on a marketing 'newsletter' goal and needlessly load
    # feed tools onto an outreach agent.
    news_keywords = [
        'news curation', 'news agent', 'news feed', 'rss feed', 'atom feed',
        'push notification', 'fetch_news', 'newsworthy', 'breaking news',
        'mark_news_for_web', 'news notification', 'curate news',
    ]
    if _mentions(lower, news_keywords):
        tags.append('news')

    # Finance + revenue.  These two tags were CONSUMED but never PRODUCED:
    # reuse_recipe.py:2533 / create_recipe.py:2079 dispatch on 'revenue',
    # and nothing in this detector emitted it, so the branch was
    # unreachable; 'finance' was neither emitted nor dispatched, leaving
    # register_finance_tools with zero production callers.  Measured
    # 2026-09-07 on the live install: a Finance agent's action reported
    # 'error' twelve times because manage_invite_participation never
    # attached.
    #
    # The stored-tag arm cannot cover this: 1 of 2,296 saved agents carries
    # any goal_tags, and the repo's only writer emits 'research', which no
    # consumer tests for.  So the lexical detector is the ONLY live path.
    #
    # Keyword precision measured against the 884 real goals that carry text
    # (the discipline _mentions' docstring exists to enforce):
    #   'financial health' 8 (0.9%) | 'api revenue' 7 | 'finance agent' 4
    #   'expense' 4 | 'accounting' 3 | 'revenue split' 37 (4.2%)
    #   'pricing' 33 (3.7%)
    # DELIBERATELY EXCLUDED: 'budget' (180 goals, 20.4% -- "token budget"),
    # 'sustainab' (107, 12.1% -- generic), and bare 'revenue' (146, 16.5%),
    # which is an owner call because it grants adjust_pricing, a MUTATING
    # tool, to one agent in six.
    # THE UNDERSCORE FORMS ARE LOAD-BEARING, not decoration.  The first cut
    # of this block used only the space forms and was HALF VACUOUS: the real
    # seed (goal_seeding.py:263 bootstrap_finance_agent) writes its tools as
    # get_financial_health / track_revenue_split / assess_sustainability /
    # manage_invite_participation, and _mentions matches VERBATIM -- so the
    # seed's own description scored NO finance tag.  Only its title
    # ('Finance Agent Vijai') matched, making the fix work or not depending
    # on whether a caller happens to pass the title.  Measured before and
    # after: description-only -> [] then ['finance'].
    #
    # 'financial_health' also covers the get_ prefix: the lookbehind is
    # (?<![a-z0-9]) and '_' is not in that class, so it matches inside
    # get_financial_health.  Exact tool names are the most precise keyword
    # available -- 4 of 884 real goals (0.45%) each, vs 20.4% for 'budget'.
    finance_keywords = [
        'financial health', 'financial_health',
        'revenue split', 'revenue_split',
        'assess_sustainability', 'manage_invite_participation',
        'finance agent', 'expense', 'accounting',
        'cash flow', 'runway', 'profit and loss', 'invoice',
    ]
    if _mentions(lower, finance_keywords):
        tags.append('finance')

    revenue_keywords = [
        'api revenue', 'api_revenue', 'revenue split', 'revenue_split',
        'adjust_pricing', 'pricing', 'monetiz', 'subscription', 'billing',
    ]
    if _mentions(lower, revenue_keywords):
        tags.append('revenue')

    # McGroce commerce (integrations/commerce/commerce_tools).  PRECISE
    # phrases only -- the tag grants cart and checkout tools.  Measured
    # 2026-09-27 against the in-repo corpus (every SEED_BOOTSTRAP_GOALS title
    # + description and every registered goal type's built prompt, 118
    # docs): 'mcgroce' fires on the grocery seed + p2p_grocery +
    # mcgroce_shopper/merchant only (after the shared _P2P_TOOLS block
    # stopped naming it), 'order status' on p2p_grocery only, the rest 0.
    # 'shopping list' / 'shopping cart' / 'onboard my store' / 'list my
    # store' catch a person asking in their own words (0 corpus hits).
    # DELIBERATELY NOT bare 'checkout' ("git checkout <branch>" would hand
    # cart tools to a coding agent) nor the stem 'grocer', which fired on all
    # 13 P2P prompts (the shared block says "grocery backend").
    commerce_keywords = [
        'mcgroce', 'add to cart', 'checkout my cart', 'cart checkout',
        'proceed to checkout', 'order status', 'onboard merchant',
        'merchant onboarding', 'new sku', 'grocery order',
        'shopping list', 'shopping cart', 'onboard my store', 'list my store',
    ]
    if _mentions(lower, commerce_keywords):
        tags.append('commerce')

    # Reel Studio: her own reel editor (integrations/reel_studio).  ONLY
    # phrases that mean editing HER OWN videos (amendment A8): the tag grants
    # the reel tools.  Words about an audience or about marketing content --
    # 'makeup artist', 'bridal makeup', 'instagram reel', 'reel editor',
    # 'make a reel' -- are deliberately absent: a commerce, marketing or
    # outreach goal that sources makeup artists as leads, or posts reels to
    # sell them a product, is not a reel studio and keeps its own tools.
    # Measured against the in-repo goal corpus plus such goals: 0 hits
    # (tests/unit/test_reel_studio_goal_tag.py).  Detection alone makes no
    # agent hers: it never stamps the tag on a record, never quiets a family,
    # never keeps an agent on this node and never gets an agent the reel
    # tools -- only the stored tag, which only her explicit creation hint
    # stamps, does that (integrations.reel_studio.tools.
    # register_reel_studio_tools registers the family on her agent alone).
    reel_studio_keywords = [
        'reel studio', 'edit my reel', 'edit my videos', 'edit my clips',
    ]
    if _mentions(lower, reel_studio_keywords):
        tags.append(REEL_STUDIO_TAG)

    media_keywords = [
        'song', 'music', 'compose', 'melody', 'sing', 'vocals',
        'voice clone', 'clone my voice', 'text to speech', 'tts',
        'narrate', 'narration', 'audio book', 'audiobook', 'podcast',
        'sound effect', 'jingle', 'soundtrack',
    ]
    if _mentions(lower, media_keywords):
        tags.append('media')

    # goal_manager maps 'thought_experiment' to its tool tag, which unlocks
    # the agent's own vote (thought_experiment_tools.ExperimentVoteTool).
    # People ask to vote without the phrase "thought experiment" ("cast your
    # vote on experiment abc"), so a vote word paired with the word
    # "experiment", or with the id of an experiment that EXISTS, counts too
    # (review of d99b1aa88).  Not "proposal" (it tripped political and
    # business chat, review of a4dc8cf3b) and not any UUID after a vote
    # word.  A vote word alone ("vote for the best pizza") and an
    # experiment alone ("run an experiment") do not.
    thought_experiment_keywords = [
        'thought experiment', 'thought-experiment',
    ]
    vote_keywords = ['vote', 'voting', 'ballot']
    if (_mentions(lower, thought_experiment_keywords)
            or (_mentions(lower, vote_keywords)
                and (_mentions(lower, ['experiment'])
                     or _names_a_known_experiment(lower)))):
        tags.append('thought_experiment')

    return tags


def resolve_goal_tags(stored_tags, text) -> list:
    """Layered goal-tag resolution (owner ruling 2026-09-01: 'fuzzy +
    semantic would be better').

    ``stored_tags`` are the agent's semantic tags assigned at creation
    and persisted on its record (same goal-tag vocabulary this module's
    detector emits, so get_tool_tags/filter_service_tools downstream is
    untouched).  They UNION with the lexical detector: a legacy record
    without stored tags resolves to exactly ``detect_goal_tags(text)``
    — the pre-existing behavior — and a tagged record can only GAIN
    tags, never lose one.  Non-string entries in stored_tags are
    dropped, not raised on, and so is a stored value that is not a list
    of tags at all (the record is user-writable JSON: a bare string
    would otherwise unlock one tag per character).

    The exceptions to "only gain" are Reel Studio's (amendment A8):
    an agent whose STORED tags carry reel_studio -- her explicitly created
    reel agent -- loses the look-alike families its words trip by accident
    (_REEL_STUDIO_KEEPS), stored or detected alike, unless ``text`` really
    asks for them; a reel_studio that is only detected quiets nothing.  On
    the central node reel_studio is dropped and quiets nothing
    (_reel_studio_inert).
    """
    detected = detect_goal_tags(text)
    if not isinstance(stored_tags, (list, tuple, set, frozenset)):
        stored_tags = None
    stored = {t for t in (stored_tags or []) if isinstance(t, str) and t}
    tags = detected if not stored else sorted(stored | set(detected))
    if REEL_STUDIO_TAG not in tags:
        return tags
    if _reel_studio_inert():
        return [t for t in tags if t != REEL_STUDIO_TAG]
    if REEL_STUDIO_TAG not in stored:
        return tags
    lower = str(text).lower()
    return [t for t in tags
            if t not in _REEL_STUDIO_KEEPS or _mentions(lower, _REEL_STUDIO_KEEPS[t])]


def user_assignable_goal_tags(raw) -> list:
    """The tags of a client's creation hint that a client may assign.

    ``raw`` is the /chat body's ``goal_tags``: a list of names.  Returns the
    ones in USER_ASSIGNABLE_GOAL_TAGS, sorted, each once; anything that is
    not a list of names reads as no hint.
    """
    if not isinstance(raw, list):
        return []
    return sorted({t for t in raw if isinstance(t, str) and t in USER_ASSIGNABLE_GOAL_TAGS})


def goal_tags_preference(tags) -> Optional[IntelligencePreference]:
    """The inference preference these goal tags set for their agent
    (GOAL_TAG_PREFERENCES), or None when they set none.

    ``tags`` is a record's stored ``goal_tags`` or a turn's creation hint --
    never detected tags.  When several tags set one, the strictest wins
    (IntelligencePreference's own order: local_only, auto, hive_preferred).
    reel_studio sets none on the central node (_reel_studio_inert).  None
    for anything that is not a collection of tags.
    """
    if not isinstance(tags, (list, tuple, set, frozenset)):
        return None
    named = {t for t in tags if isinstance(t, str) and t in GOAL_TAG_PREFERENCES}
    if REEL_STUDIO_TAG in named and _reel_studio_inert():
        named.discard(REEL_STUDIO_TAG)
    asked = {GOAL_TAG_PREFERENCES[t] for t in named}
    return next((p for p in IntelligencePreference if p in asked), None)


def is_local_only(tags) -> bool:
    """Do these goal tags keep the agent on this node -- is their
    goal_tags_preference LOCAL_ONLY?  Its turns then run local-only and its
    record is not synced to the cloud prompt list."""
    return goal_tags_preference(tags) is IntelligencePreference.LOCAL_ONLY


def agent_is_local_only(prompt_id, hint=None) -> bool:
    """Do the goal tags of the agent ``prompt_id`` keep its turns on this
    node (is_local_only)?  Its saved record's ``goal_tags``, and ``hint``:
    the creation hint the turn itself carries for it (the /chat body's
    ``goal_tags``, read by user_assignable_goal_tags), which is all there is
    before that record exists.  Nothing is held between turns (amendment
    A19): the phone puts her agent's tags on every turn of it.  The
    per-agent half of /chat's one local-only rule.  False for no id, an
    unsafe id or no readable record and no hint."""
    if prompt_id is None or prompt_id == '':
        return False
    from core.cache_loaders import load_agent_config
    stored = (load_agent_config(prompt_id) or {}).get('goal_tags')
    return is_local_only(user_assignable_goal_tags(hint)
                         + (stored if isinstance(stored, list) else []))


def stamp_goal_tags(hint, stored=None) -> Optional[list]:
    """The ``goal_tags`` an agent record is saved with, or None when the
    record's own value stays exactly as it is.

    A user-assignable tag reaches a record ONLY from the explicit creation
    hint (user_assignable_goal_tags) -- never from what the record's words
    detect, nor from the gathered record itself (amendment A8 c): a stored
    reel_studio quiets families and keeps the agent on this node, so it must
    mean she asked for a Reel Studio agent.  ``stored`` is the gathered
    record's ``goal_tags``: its other tags are kept, its user-assignable ones
    dropped.  reel_studio is never stamped on the central node
    (_reel_studio_inert).  Sorted when it returns a list; None for a record
    with neither a hint nor an assignable tag of its own.
    """
    tags = set(user_assignable_goal_tags(hint))
    if REEL_STUDIO_TAG in tags and _reel_studio_inert():
        tags.discard(REEL_STUDIO_TAG)
    own = [t for t in stored if isinstance(t, str) and t] \
        if isinstance(stored, list) else []
    if not tags and not any(t in USER_ASSIGNABLE_GOAL_TAGS for t in own):
        return None
    return sorted(tags | {t for t in own if t not in USER_ASSIGNABLE_GOAL_TAGS})
