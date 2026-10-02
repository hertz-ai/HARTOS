"""
core/agent_tools.py — Canonical AutoGen tool definitions (single source of truth).

Every core tool is defined ONCE here. Both create_recipe.py and reuse_recipe.py
call build_core_tool_closures() + register_core_tools() instead of duplicating
function bodies and registration lines.

Pattern mirrors:
  - integrations/service_tools/media_agent.py   → register_media_tools()
  - integrations/channels/memory/agent_memory_tools.py → register_autogen_tools()
  - integrations/agent_engine/marketing_tools.py → register_marketing_tools()
"""
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Annotated, Any, List, Optional, Tuple

import requests
from json_repair import repair_json

from core.http_pool import pooled_get, pooled_post
from core.tool_traits import reads_persisted_state
from integrations.service_tools.model_catalog import ModelType

tool_logger = logging.getLogger('tool_execution')


def _bounded_observation(text, hint):
    """``text`` bounded to TOOL_OBSERVATION_MAX_CHARS for a tool result, the
    cut marked with ``hint`` (#104). The result goes to the model and, through
    the group chat's write-back, into memory."""
    from core.constants import TOOL_OBSERVATION_MAX_CHARS
    from core.token_utils import bound_text
    return bound_text(text, TOOL_OBSERVATION_MAX_CHARS, f'\n...[cut; {hint}]')


def _bounded_recall(contents, max_items, skip_oversize=True):
    """Recalled memories joined for a tool result, within one budget.

    Both legs of search_long_term_memory use it. On the MemoryGraph leg
    (``skip_oversize``) a row longer than MEMORY_ITEM_MAX_CHARS is skipped,
    not cut: every graph write is bounded to that since #104, so a longer row
    predates the bound. Rows like it (whole data stores, written back and
    recalled again) grew Guardian Convergence's graph to 28.6M chars and one
    recall to 3,386,616, and they rank high on any query because they hold so
    many terms. SimpleMem's item is an answer, not a stored row, so it is cut.
    """
    from core.constants import MEMORY_ITEM_MAX_CHARS, TOOL_OBSERVATION_MAX_CHARS
    from core.token_utils import bound_text
    picked, used, skipped = [], 0, 0
    for c in contents:
        if not isinstance(c, str) or not c.strip():
            continue
        if skip_oversize and len(c) > MEMORY_ITEM_MAX_CHARS:
            skipped += 1
            continue
        room = TOOL_OBSERVATION_MAX_CHARS - used
        if len(picked) >= max_items or room < 40:
            break
        piece = bound_text(c, room)
        picked.append(piece)
        used += len(piece)
    if skipped:
        tool_logger.info(f'[RECALL-BOUND] skipped {skipped} memory row(s) '
                         f'over {MEMORY_ITEM_MAX_CHARS} chars')
    return '\n'.join(picked)


# ---------------------------------------------------------------------------
# Generic registration helper
# ---------------------------------------------------------------------------

_INTERNAL_ERROR_MARKERS = ('context size', 'server_error', 'error code: 500',
                           'traceback', 'connection', 'timed out', 'timeout',
                           'memory slot')


def user_facing_error(e):
    """ONE user-facing string for an internal failure (#716).

    Three sites (reuse get_agent_response, create get_response_group,
    gather_agentdetails) returned raw exception text as the reply, so
    TTS spoke lines like 'Context size has been exceeded' to the user
    (observed live 2026-08-31, probe run 4).  Full tracebacks are
    already logged at every call site - the reply only needs to be
    honest and speakable.
    """
    text = str(e)
    low = text.lower()
    if any(m in low for m in _INTERNAL_ERROR_MARKERS) or len(text) > 200:
        return _SNAG_REPLY
    return f"{_COULD_NOT_FINISH_PREFIX}{text[:160]}"


# The two shapes user_facing_error() produces, named once so the code that has
# to RECOGNISE a failed turn reads the same strings the code that writes them
# does.  Changing the wording here changes both.
_SNAG_REPLY = ("I hit an internal snag finishing that - please try "
               "again in a moment.")
_COULD_NOT_FINISH_PREFIX = "I couldn't finish that: "


def is_user_facing_error(reply) -> bool:
    """True when ``reply`` is a failed turn dressed as an answer.

    A turn that fails does not raise to its caller: user_facing_error() turns
    the exception into a polite, speakable sentence and the pipeline returns
    it as the reply, and hart_intelligence_entry does the same with
    LLM_LOADING_REPLY / LLM_GENERIC_ERROR_REPLY, and create_recipe with
    BUILD_INCOMPLETE_REPLY when an agent build ends without its recipe.  That
    is right for a person reading it and wrong for any caller that has to
    decide whether WORK was done.  Measured on central 2026-09-13: the
    distributed worker submitted "I couldn't finish that: Error code: 429 -
    ... rate_limit_exceeded ..." as a hive task's result, and the coordinator
    marked the task completed; later the same day Hive Model Trainer's task
    was completed with BUILD_INCOMPLETE_REPLY.

    Recognises exactly the strings this codebase emits for a failure, by
    reference to where they are defined, so rewording one cannot silently stop
    this check from matching it.
    """
    if not isinstance(reply, str):
        return False
    text = reply.strip()
    if not text:
        return False
    if text == _SNAG_REPLY or text.startswith(_COULD_NOT_FINISH_PREFIX):
        return True
    from core.constants import (
        BUILD_INCOMPLETE_REPLY, LLM_GENERIC_ERROR_REPLY, LLM_LOADING_REPLY)
    # The whole sentence as a prefix: what follows it cannot turn a failed
    # build into work, and nothing shorter is matched.
    if text.startswith(BUILD_INCOMPLETE_REPLY.strip()):
        return True
    return text in (LLM_LOADING_REPLY.strip(), LLM_GENERIC_ERROR_REPLY.strip())


def is_help_pause(reply) -> bool:
    """True when ``reply`` says the turn's action was handed to a person or an
    expert (create_recipe._ask_for_help on an autonomous run).

    Neither a result nor a failure: the action is held, the goal is parked or
    handed to the expert, and the reply is the notice.  A caller deciding
    whether work was done (the hive worker) must not record it as a
    completion, and must not release it for a retry either, since that would
    run a paused goal.  Recognised by reference to the prefixes in
    core.constants so rewording one cannot silently stop this check.
    """
    if not isinstance(reply, str):
        return False
    text = reply.strip()
    if not text:
        return False
    from core.constants import HELP_EXPERT_REPLY_PREFIX, HELP_PAUSED_REPLY_PREFIX
    return text.startswith((HELP_PAUSED_REPLY_PREFIX, HELP_EXPERT_REPLY_PREFIX))


def is_action_error_reply(reply) -> bool:
    """True when ``reply`` is the CREATE pipeline's structured error envelope,
    {"status": "error", "action": ..., "action_id": ..., "message": ...} — the
    format create_recipe's prompt tells an agent to return when an action
    failed and self-heal did not work.  Like a help pause, an error is not a
    result: a caller deciding whether work was done must not record it as one.
    Measured 2026-09-15: the daemon counted these as successful dispatches, so
    a continuous goal whose every run ended in this envelope re-ran every
    5 minutes for five months (53,949 copilot sessions)."""
    if not isinstance(reply, str):
        return False
    text = reply.strip()
    if not text:
        return False
    if text.startswith('{'):
        try:
            import json
            d = json.loads(text)
            if isinstance(d, dict):
                return str(d.get('status', '')).lower() == 'error'
        except ValueError:
            pass
    # The envelope with prose around it: both protocol keys present.
    import re
    return '"action_id"' in text and re.search(r'"status"\s*:\s*"error"', text) is not None


# The tool menu, schema fitting, registration and runtime attach live in
# core.agent_tool_menu (split out when this module crossed the 3000-line
# god-module ratchet).  Re-exported so every `from core.agent_tools import X`
# and every `core.agent_tools.X` attribute read keeps working unchanged.
from core.agent_tool_menu import (  # noqa: E402,F401 -- re-export
    CREATE_ADVERTISED_TOOLS, CREATE_LEG_EXTRA_TOOLS, MAIN_LEG_CORE_TOOLS,
    REQUEST_TOOLS_DESCRIPTION, attach_for_names, attach_for_tags,
    create_helper_keep, defer_helper_schema, discover_and_attach,
    filter_service_tools, fit_schema_to_ctx, helper_tool_names,
    main_leg_core_tools, main_leg_tool_menu, register_core_tools,
    register_dual, register_request_tools, registered_tool_menu,
    _NEED_STOPWORDS, _attach_tool, _join_tool_menu, _need_names_tool,
    _schema_or_none,
)


# ---------------------------------------------------------------------------
# Core tool closure factory
# ---------------------------------------------------------------------------

# Neutral fallback receipt (#752) used until the user accepts a custom template.
# The accept-once flow overrides it via save_data_in_memory('receipt_template').
# {var} placeholders are filled by the existing TemplateEngine.
_DEFAULT_RECEIPT_TEMPLATE = (
    "RECEIPT\n"
    "{business_name}\n"
    "Date: {date}\n"
    "Received from: {client_name}\n"
    "For: {service}\n"
    "Event timing: {event_timing}\n"
    "Amount: {currency} {amount}\n"
    "Advance received: {currency} {advance}\n"
    "Balance due: {currency} {balance}\n"
    "{notes}\n"
    "Thank you for your business."
)


from core.game_sound_memo import (  # noqa: E402
    GAME_STATE_DURATIONS,
    GAME_STATES,
    game_state_key,
    game_sound_action,
    game_state_match,
    game_state_record,
    game_state_sound,
    record_verdict,
    rejected_take,
    set_game_state_sound,
    set_game_state_sound_at,
)


#: How long a timed-out submit is assumed to still be queued server-side
#: before this client will submit the same state again -- owned by the memo
#: module, which answers "composing" with it for the node's route as well.
from core.game_sound_memo import SUBMIT_COOLDOWN_S  # noqa: E402,F401

#: A task older than this is taken as lost, not slow.  AceStep keeps its job
#: store in memory, so a restart forgets every id, and it answers a forgotten
#: id exactly as it answers a queued one (hartos-3a F2).  The slowest job
#: MEASURED on a shared GPU averaged 907 s; twice that is past any real job.
TASK_STALE_S = 1800


def _pending_submit(games, game_id, state, level=None, user_id=None):
    """When this state's last submit went out with no id learned, else None.

    A record with 'submitted_at' and neither 'url' nor 'task_id' is a submit
    whose reply timed out.  The ladder ignores it (nothing to play, nothing to
    poll), so it is read at its own key."""
    record = game_state_record(games, game_id, state, level, user_id)
    if record.get('url') or record.get('task_id'):
        return None
    return record.get('submitted_at')


def offer_sound_for_review(user_id, prompt_id, game_id, state, record):
    """Put a newly composed game sound in front of the person, to hear.

    Creation is meant to be liquid: the reviewer hears the piece and
    answers it on the surface they are already looking at, rather than
    being told a URL.  This rides the existing agent-to-UI channel
    (LiquidUIService.agent_ui_update), which is allow-listed, audited and
    delivered to web, phone and desktop alike.

    Best-effort by design: a node without that service, or a hive the
    human has halted, must not stop a sound being composed and memoized.
    Returns True when the card itself was accepted for delivery -- and the
    person is told on their other surfaces either way, because a node with
    no screen attached is precisely when the phone matters most.
    """
    shown = False
    try:
        from core.platform.registry import get_registry
        service = get_registry().get('LiquidUIService')
        if service is not None:
            # The audio FIRST, as the declared 'media' component every
            # client already renders (props: type, src, alt, controls).  It
            # used to ride inside the approval card as an undeclared prop,
            # which no client reads -- so the card invited someone to "have
            # a listen" and gave them nothing to listen to.  'media_type'
            # as well as 'type' because the component's own type key is
            # 'media' and the clients read the modality from media_type.
            if record.get('url'):
                service.agent_ui_update(user_id, {
                    'type': 'media',
                    'agent_id': str(prompt_id),
                    'media_type': 'audio',
                    'src': record.get('url'),
                    'controls': True,
                    'alt': f'{state} sound for {game_id}',
                    'title': f'{state} sound for {game_id}',
                }, user_id=user_id)
            shown = bool(service.agent_ui_update(user_id, {
                'type': 'approval',
                'agent_id': str(prompt_id),
                'action': game_sound_action(game_id, state),
                'description': (
                    f"New {state} sound for {game_id}. Have a listen: keep "
                    f"it, or say what is wrong and I will compose another."
                ),
                'options': ['Keep it', 'Compose another'],
            }, user_id=user_id))
    except Exception as e:
        # never at the cost of the composition that just succeeded
        tool_logger.debug(f'game sound: no card on screen ({e})')
    if not shown:
        # Only when the card did NOT reach a screen.  A game has fourteen
        # states, so notifying regardless meant one game cost the person
        # fourteen phone pushes and fourteen unread rows -- for sounds they
        # were already being shown one by one.  'shown' was computed and
        # thrown away; it is the whole signal for whether they need telling
        # somewhere else.
        _tell_the_person_elsewhere(user_id, prompt_id, game_id, state, record)
    return shown


def _tell_the_person_elsewhere(user_id, prompt_id, game_id, state, record):
    """Reach the person who is not looking at the screen it was offered on.

    The card above lands where they are logged in; a sound composed while
    they are away from that screen would otherwise wait unheard.  This is
    the same pair the consent ask already uses
    (integrations/social/device_routing_service): a notification record,
    which every surface of theirs shows, and an FCM push to the phone.
    Both are best-effort and both no-op cleanly on a node with no push
    credential.
    """
    message = f"A new {state} sound for {game_id} is ready for you to hear."
    try:
        from integrations.social.services import NotificationService
        from integrations.social.models import db_session
        with db_session() as db:
            # target_type/target_id are the schema's own way of saying what
            # a notification is ABOUT, and the client routes on them.  Without
            # them the row is inert: it tells the person a sound is ready and
            # gives them no way to reach it.
            NotificationService.create(
                db, str(user_id), 'agent_game_sound_review',
                source_user_id=str(prompt_id), message=message,
                target_type='agent', target_id=str(prompt_id),
            )
    except Exception as e:
        tool_logger.debug(f'game sound: no notification record ({e})')
    try:
        from core.fcm_sync import send_fcm_push
        send_fcm_push(
            str(user_id),
            'A new game sound',
            message,
            data={
                'type': 'game_sound_review',
                'agent_id': str(prompt_id),
                'game_id': str(game_id),
                'state': str(state),
                'url': str(record.get('url') or ''),
                'topic_reply': f'com.hertzai.pupit.{user_id}',
            },
            # An offer can wait the 30-40 s central's relay takes; a node with
            # no FCM credential (every consumer install) reaches the phone
            # through central instead of not at all.
            relay=True,
        )
    except Exception as e:
        tool_logger.debug(f'game sound: no push to the phone ({e})')


def build_core_tool_closures(ctx):
    """Build session-scoped tool closures.  Returns list of (name, desc, func).

    Args:
        ctx: dict with session variables:
            user_id, prompt_id, agent_data, helper_fun, user_prompt,
            request_id_list, recent_file_id, scheduler,
            simplemem_store (optional), memory_graph (optional),
            log_tool_execution (decorator), send_message_to_user1 (func),
            retrieve_json (func), strip_json_values (func),
            save_conversation_db (func)
    """
    # Unpack context -------------------------------------------------------
    user_id = ctx['user_id']
    prompt_id = ctx['prompt_id']
    agent_data = ctx['agent_data']
    helper_fun = ctx['helper_fun']
    user_prompt = ctx['user_prompt']
    request_id_list = ctx['request_id_list']
    recent_file_id = ctx['recent_file_id']
    scheduler = ctx['scheduler']
    simplemem_store = ctx.get('simplemem_store')
    memory_graph = ctx.get('memory_graph')
    # log_tool_execution: optional decorator (create_recipe.py has it, reuse_recipe.py may not)
    log_tool_execution = ctx.get('log_tool_execution') or (lambda f: f)
    send_message_to_user1 = ctx['send_message_to_user1']
    _retrieve_json = ctx['retrieve_json']
    _strip_json_values = ctx['strip_json_values']
    save_conversation_db = ctx['save_conversation_db']

    tools: List[Tuple[str, str, Any]] = []

    # ------------------------------------------------------------------
    # 1. text_2_image
    # ------------------------------------------------------------------
    @log_tool_execution
    def text_2_image(text: Annotated[str, "Text to create image"]) -> str:
        return helper_fun.txt2img(text)

    tools.append((
        "text_2_image",
        "Text to image Creator",
        text_2_image,
    ))
    # Alias — reuse_recipe.py main flow LLM prompts advertise `txt2img` (#510).
    # Same canonical closure (helper_fun.txt2img — local-first, sovereignty).
    tools.append((
        "txt2img",
        "Text to image Creator (alias of text_2_image)",
        text_2_image,
    ))

    # ------------------------------------------------------------------
    # 2. get_user_camera_inp
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_user_camera_inp(
        inp: Annotated[str, "A question about what the user's live camera shows right now, e.g. 'what is the user holding?'"],
    ) -> str:
        # No int() — user_id is a UUID on desktop installs and int() raised
        # on every call (152/152 failures across three log rotations,
        # 10/10 on 2026-09-07).  The callee never needs an int: helper.py:2163
        # does get_frame(str(user_id)) and :2165 interpolates it into a
        # filename.  An integer id still passes through unchanged.
        return helper_fun.get_user_camera_inp(inp, user_id, request_id_list[user_prompt])

    # The schema says what the tool does.  It read "Get user's visual
    # information to process somethings" with one argument "The Question to
    # check from visual context" -- on the main leg the only core tool besides
    # google_search that takes any question -- and goals whose own tool was
    # missing asked it about gradient sync and build status: 732 calls
    # 2026-09-22..29 on the MSI desktop, none with a frame (helper.py
    # get_user_camera_inp now raises for that).
    tools.append((
        "get_user_camera_inp",
        "Look through the user's live camera and answer a question about what "
        "it shows right now. Only for what is in front of the camera: it cannot "
        "answer anything else, and it fails when the user is not sharing a "
        "camera.",
        get_user_camera_inp,
    ))

    # ------------------------------------------------------------------
    # 3. save_data_in_memory
    # ------------------------------------------------------------------
    @log_tool_execution
    def save_data_in_memory(
        key: Annotated[str, "Key path for storing data now & retrieving data later. Use dot notation for nested keys (e.g., 'user.info.name')."],
        value: Annotated[Optional[Any], "Value you want to store; strictly should be one of int, float, bool, json array or json object."] = None,
    ) -> str:
        """Store data with validation to prevent corruption."""
        tool_logger.info('INSIDE save_data_in_memory')
        try:
            if isinstance(value, str) and (value.startswith('{') or value.startswith('[')):
                value = _retrieve_json(value)
                tool_logger.info(f"REPAIRED JSON STRING: {value}")
            if value is not None:
                json_str = json.dumps(value)
                validated_value = json.loads(json_str)
                tool_logger.info(f"VALIDATED VALUE (post JSON cycle): {validated_value}")
            else:
                validated_value = None

            keys = key.split('.')
            d = agent_data.setdefault(prompt_id, {})
            for k in keys[:-1]:
                d = d.setdefault(k, {})
            d[keys[-1]] = validated_value
            tool_logger.info(f"VALUES STORED IN AGENT DATA: {validated_value}")
            tool_logger.info(f"FULL AGENT DATA AT KEY: {d}")

            if helper_fun.save_agent_data_to_file(prompt_id, agent_data):
                tool_logger.info(f"[OK] Data persisted to file for prompt_id {prompt_id}")
            else:
                tool_logger.warning(f"Failed to persist data to file for prompt_id {prompt_id}")

            # Mirror to MemoryGraph (fire-and-forget).  Carried by
            # reuse_recipe's inline twin before the #743 migration, lost
            # in the swap, restored here (owner audit 2026-09-01).  Pairs
            # with get_data_by_key's [KV] recall fallback below.
            if memory_graph is not None:
                try:
                    threading.Thread(target=lambda: memory_graph.register(
                        f"[KV] {key} = {json.dumps(validated_value)[:200]}",
                        {'memory_type': 'fact', 'source_agent': 'helper',
                         'session_id': user_prompt, 'kv_key': key},
                    ), daemon=True).start()
                except Exception:
                    pass

            try:
                # The value as stored, not the tool's page of it.
                stored_value = _read_saved(key)
                tool_logger.info(f"VERIFICATION - READ BACK VALUE: {stored_value}")
                if stored_value == _KEY_NOT_FOUND:
                    tool_logger.error(f"VERIFICATION FAILED: Data not properly stored at key {key}")
                    return f"Error: {key} was written but could not be read back"
            except Exception as e:
                tool_logger.error(f"VERIFICATION ERROR: {str(e)}")

            # Report the save, not the store. This returned the whole
            # agent_data store on every call, so each save put all of it in
            # the model's context and, through the group chat's write-back,
            # into memory again: on central 2026-09-14 (#104) the large
            # MemoryGraph rows were all this repr, 0.9M to 3.96M chars each.
            return _bounded_observation(
                f'Saved at {key}: {json.dumps(validated_value)}',
                'the whole value was saved')
        except json.JSONDecodeError as je:
            error_msg = f"Invalid JSON structure in value: {str(je)}"
            tool_logger.error(error_msg)
            return f"Error: {error_msg} - Data not saved"
        except TypeError as te:
            error_msg = f"Type error in value: {str(te)}"
            tool_logger.error(error_msg)
            return f"Error: {error_msg} - Data not saved"
        except Exception as e:
            error_msg = f"Unexpected error saving data: {str(e)}"
            tool_logger.error(error_msg)
            return f"Error: {error_msg} - Data not saved"

    # ------------------------------------------------------------------
    # bind_game_sound — a game's sounds, composed once and kept
    # ------------------------------------------------------------------
    @log_tool_execution
    def bind_game_sound(
        game_id: Annotated[str, "The game's id as the app knows it (a game config's id, e.g. 'eng-spell-animals-01')"],
        mood: Annotated[str, "How the game should feel: happy, calm, adventurous, triumphant"] = "happy",
        description: Annotated[str, "What happens in the game, for the composer"] = "",
        state: Annotated[str, "Which state of the game: bgm for the music under the game, or correct, wrong, streak, complete, starEarned, intro, countdownTick, countdownEnd, cardFlip, matchFound, dragStart, dragDrop, tap"] = "bgm",
        level: Annotated[str, "Only when THIS level needs its own sound; leave empty so every level of the game shares one"] = "",
        scope: Annotated[str, "'agent' binds it for everyone who reuses this agent; 'mine' is a correction for this person alone"] = "agent",
    ) -> str:
        """Compose this game's background music and bind it to the game.

        Call it in CREATE for each game this agent plays with.  The music
        is composed by the node's media capability and recorded against
        this agent, so REUSE plays the same music rather than composing
        again, and the reviewer approves one piece of music per game.

        Idempotent: once a game is bound, calling it again returns the
        binding.  If the composer is still working, call it again later
        with the same game_id and it picks the task back up.
        """
        tool_logger.info(f'INSIDE bind_game_sound for game {game_id}')
        if not game_id or not str(game_id).strip():
            return "A game_id is required: use the game config's id."
        slot = str(game_id).strip()

        which = str(state or 'bgm').strip() or 'bgm'
        if which not in GAME_STATES:
            return (f"{which} is not one of a game's states. Use one of: "
                    f"{', '.join(sorted(GAME_STATES))}.")
        mine = user_id if str(scope or 'agent').strip() == 'mine' else None
        games = agent_data.setdefault(prompt_id, {}).setdefault('games', {})
        bound, matched = game_state_sound(games, slot, which, level, mine,
                                          own_only=bool(mine))
        if bound.get('url'):
            return json.dumps({
                'status': 'already_bound',
                'game_id': slot,
                'state': which,
                'matched': matched,
                'music': bound,
                'note': f'This game already has its {which}; it is never composed twice.',
            })
        # NO early return for a composition already under way.  It used to
        # return here saying "call again with the same arguments to finish
        # it" -- and calling again hit this same branch and said it again,
        # forever.  MEASURED 2026-09-22: twelve consecutive calls, the
        # composition finishing on the server in the middle of them, and the
        # memo never receiving the url.  The resume path below (`task_id =
        # bound.get('task_id')`, which skips the submit and polls the
        # existing task) was unreachable, so the note was a promise the code
        # could not keep.  Falling through IS the dedupe: an existing
        # task_id means poll it, never start a second composition.

        def _remember(record):
            set_game_state_sound(games, slot, which, record, level, mine)
            try:
                helper_fun.save_agent_data_to_file(prompt_id, agent_data)
            except Exception as e:
                tool_logger.warning(f'bind_game_sound could not persist: {e}')
            return record

        def _offer(record):
            """Hand a finished piece to the person, to hear and answer."""
            if record.get('url'):
                offer_sound_for_review(user_id, prompt_id, slot, which, record)
            return record

        try:
            from integrations.service_tools.media_agent import (
                MEDIA_FAILED_STATUSES,
                _reads_as_still_waking,
                check_media_status,
                generate_media,
            )
        except ImportError as e:
            tool_logger.warning(f'bind_game_sound: no media capability ({e})')
            return ("This node cannot compose music (the media capability is "
                    "not available here), so the game keeps no sound.")

        def _failure_kind(result):
            """Why a composer call failed, told apart by the module that wrote it.

            media_agent.classify_error is the reader that lives next to the
            returns it reads (hartos-94, HARTOS 11d0aebee), and it makes
            distinctions a prose match here could not: a node with NOTHING
            installed should be offered an install; an AceStep that is
            installed and merely not running -- or will not fit beside
            whatever holds the GPU -- must be waited for: not offered again
            (installing what is on the disk is its own defect) and not
            reported as a refusal.
            """
            try:
                from integrations.service_tools.media_agent import classify_error
                return classify_error(result)
            except Exception:
                return None

        def _no_composer_here(result):
            try:
                from integrations.service_tools.media_agent import ABSENT
            except Exception:
                return False
            return _failure_kind(result) == ABSENT

        def _composer_not_up(result):
            """Run 8, 2026-09-22: a 3 GB llama-server on the card made the
            runtime refuse to start the composer, and this tool told the
            agent the composer REFUSED the game's music.  Nothing was posted,
            so nothing is remembered; the next call simply asks again."""
            try:
                from integrations.service_tools.media_agent import UNREACHABLE
            except Exception:
                return False
            return _failure_kind(result) == UNREACHABLE

        def _ask_for_a_composer(why):
            """Offer to set a music model up, rather than failing quietly.

            A node with no composer cannot give a game its sounds, and
            silence tells the person nothing.  The ask is the canonical
            consent card, scoped to this one capability, and the owner's
            yes routes into the provisioning that already exists.
            """
            try:
                from integrations.agent_engine.capability_setup import (
                    request_capability_setup)
                outcome = request_capability_setup(
                    'music:acestep',
                    reason=(f"To give {slot} its {which} sound I need a music "
                            f"model on this computer. May I set one up?"),
                    category='subprocess.tool_load',
                    # NOT 'backend': that key is what the TTS venv repair
                    # tool reads, and its documented backends are TTS engine
                    # ids only (backend_repair_tools) -- so naming acestep
                    # there sent a granted consent into a repair path aimed
                    # at a tool that cannot install a music model.  With no
                    # backend, goal_manager routes tool_load to dependency
                    # remediation instead of a venv rebuild, which is what
                    # a missing music engine actually needs.
                    context={'tool': 'acestep', 'game_id': slot,
                             'state': which},
                )
            except Exception as ask_error:
                tool_logger.warning(f'could not offer a composer: {ask_error}')
                outcome = 'unavailable'
            return json.dumps({
                'status': 'needs_capability',
                'capability': 'music:acestep',
                'game_id': slot,
                'state': which,
                'asked': outcome,
                'why': why,
                'note': {
                    'provisioning': 'Setting the music model up now; ask again '
                                    'once it is ready.',
                    'asked': 'I have asked the owner of this computer whether '
                             'I may set a music model up.',
                    'declined': 'The owner said no to a music model, so this '
                                'game keeps the sounds it already has.',
                    'unavailable': 'There is nobody to ask on this node, so no '
                                   'sound can be composed here.',
                }.get(outcome, 'No music model is available on this node.'),
            })

        prompt = GAME_STATES[which].format(
            what=description or slot, mood=mood)
        # A take the reviewer rejected is composed again WITH the reason
        # they gave, and kept as the next variant (spec §6.1).
        rejected = rejected_take(games, slot, which, level, mine)
        variant = int(rejected.get('variant') or 1) + 1 if rejected else 1
        if rejected.get('rejected_reason'):
            prompt = f"{prompt}. Not like the last one: {rejected['rejected_reason']}"
        # Carried forward, because the new take REPLACES the rejected one at
        # this key: without this the previous audio would survive exactly
        # until the next composition and then vanish.
        previous_takes = list(rejected.get('previous_takes') or [])
        if rejected.get('rejected_url'):
            previous_takes.append({
                'url': rejected['rejected_url'],
                'variant': int(rejected.get('variant') or 1),
                'rejected_reason': rejected.get('rejected_reason') or '',
                'rejected_at': rejected.get('rejected_at'),
            })
        task_id = bound.get('task_id')

        def _forget_task(reason):
            """Drop a task the composer will never finish, keep the rest.

            hartos-3a F2: nothing ever cleared a dead task_id, so after a
            composer restart this state answered "composing" for good, and
            after a failure it answered "failed" for good; it could never be
            composed again.  The rejection history and variant stay.
            """
            kept = {k: v for k, v in game_state_record(
                        games, slot, which, level, mine).items()
                    if k not in ('task_id', 'task_since', 'submitted_at')}
            kept['lost_task'] = {'task_id': task_id, 'reason': reason,
                                 'at': time.time()}
            _remember(kept)

        # A record from before task_since existed has no age to judge; it is
        # polled as before rather than composed a second time.
        if (task_id and bound.get('task_since')
                and time.time() - float(bound['task_since']) > TASK_STALE_S):
            _forget_task('no answer within TASK_STALE_S')
            task_id = None
        try:
            if not task_id:
                # A submit whose RESPONSE timed out may still have been
                # ACCEPTED.  MEASURED 2026-09-22 on a live AceStep: two
                # 'warming_up' answers, then a third submit that got an id --
                # and /v1/stats reported FIVE jobs from this one caller
                # (2 succeeded, 1 running, 2 queued, avg 907s each).  Every
                # retry had enqueued a real job the client never learned the
                # id of, and the one id it did hold sat "queued" behind its
                # own orphans.  /release_task takes no idempotency key, so
                # the only dedupe is here: after a timed-out submit, do not
                # submit again for this state until a cooldown has passed.
                _pending = _pending_submit(games, slot, which, level, mine)
                if _pending and time.time() - _pending < SUBMIT_COOLDOWN_S:
                    return json.dumps({
                        'status': 'composing',
                        'game_id': slot,
                        'state': which,
                        'note': ("A submission for this state may already be "
                                 "in the composer's queue (the last one was "
                                 "accepted but its reply timed out); waiting "
                                 "for it rather than queueing a second."),
                    })
                started = json.loads(generate_media(
                    context=prompt,
                    output_modality='audio_music',
                    input_text=prompt,
                    # per state: a chime is two seconds, a loop is thirty (spec 3).
                    duration=GAME_STATE_DURATIONS.get(which, 30),
                    style=mood,
                ))
                if started.get('status') == 'completed':
                    results = started.get('results') or []
                    url = results[0].get('url') if results else None
                    if url:
                        record = _offer(_remember({'url': url, 'mood': mood,
                                            'prompt': prompt, 'state': which,
                                            'level': level or None,
                                            'variant': variant,
                                            'previous_takes': previous_takes,
                                            'composed_at': time.time(),
                                            'approved_at': None}))
                        return json.dumps({'status': 'bound', 'game_id': slot,
                                           'state': which, 'music': record})
                    return "The composer answered without any music; nothing bound."
                if started.get('status') == 'warming_up':
                    # Not a refusal: the composer is getting ready, which on
                    # a first run means downloading its model.  Saying it
                    # refused would be wrong AND would leave the game with
                    # nothing pending to come back to.  And the POST may have
                    # been accepted: remember WHEN it went out, so the next
                    # call waits instead of queueing a duplicate.  Written ON
                    # TOP of what this key already holds: a take the reviewer
                    # turned down keeps its rejected_url, its reason and its
                    # variant, so the next call still answers them.  MEASURED
                    # 2026-09-22 (hartos-14): replacing the record here erased
                    # all three -- on a node restarted overnight, which is
                    # exactly when a rejection is waiting.
                    _remember({'mood': mood, 'prompt': prompt, 'state': which,
                               'level': level or None, 'variant': variant,
                               'composed_at': None, 'approved_at': None,
                               **game_state_record(games, slot, which, level, mine),
                               'submitted_at': time.time()})
                    return json.dumps({
                        'status': 'composing',
                        'game_id': slot,
                        'state': which,
                        'note': started.get(
                            'message',
                            'The composer is starting up; ask again shortly.'),
                    })
                if started.get('status') != 'pending':
                    why = str(started.get('error', 'unknown reason'))
                    if _no_composer_here(started):
                        return _ask_for_a_composer(why)
                    if _composer_not_up(started):
                        return (f"The composer is installed but not running "
                                f"right now ({why}). Nothing was started; ask "
                                f"again in a while by calling bind_game_sound "
                                f"with the same game_id and state.")
                    return f"The composer refused this game's music: {why}"
                task_id = started.get('task_id')
                _remember({'task_id': task_id, 'task_since': time.time(),
                           'mood': mood, 'prompt': prompt,
                           'state': which, 'level': level or None,
                           'variant': variant,
                           'previous_takes': previous_takes,
                           'composed_at': None, 'approved_at': None})

            # Give it a while, then hand the task back rather than block.
            deadline = time.time() + 90
            while time.time() < deadline:
                time.sleep(3)
                progress = json.loads(check_media_status(task_id))
                state = progress.get('status')
                if state in ('complete', 'completed', 'done'):
                    results = progress.get('results') or []
                    url = (progress.get('url')
                           or (results[0].get('url') if results else None))
                    if not url:
                        return "The composer finished without any music; nothing bound."
                    record = _offer(_remember({'url': url, 'mood': mood, 'prompt': prompt,
                                        'state': which, 'level': level or None,
                                        'variant': variant,
                                        'previous_takes': previous_takes,
                                        'composed_at': time.time(),
                                        'approved_at': None}))
                    return json.dumps({'status': 'bound', 'game_id': slot,
                                       'state': which, 'music': record})
                if state in MEDIA_FAILED_STATUSES:
                    why = str(progress.get('error', 'unknown reason'))
                    if progress.get('unreachable') and _reads_as_still_waking(why):
                        # The POLL can be reset by a busy server just as the
                        # submit can (MEASURED 2026-09-22: attempts 9-10 of a
                        # live bind reported "failed" on ConnectionResetError
                        # 10054 while the composer was mid-generation and went
                        # on to finish).  Keep polling; the deadline below
                        # still hands the task back as 'composing'.
                        continue
                    if _no_composer_here(progress):
                        return _ask_for_a_composer(why)
                    # the task is over: the next call composes this state again
                    # (hartos-3a F2)
                    _forget_task(why)
                    return (f"The composer failed on this game: {why}. Call "
                            f"bind_game_sound again to compose it afresh.")
            return json.dumps({
                'status': 'composing',
                'game_id': slot,
                'state': which,
                'task_id': task_id,
                'note': 'Still composing. Call bind_game_sound again with the '
                        'same game_id and state to finish binding it.',
            })
        except Exception as e:
            tool_logger.warning(f'bind_game_sound failed for {slot}: {e}')
            return f"Could not bind this game's sound: {e}"

    tools.append((
        "bind_game_sound",
        "Compose a kids game's background music and bind it to that game for "
        "good, so every later run of this agent plays the same music. Pass the "
        "game's id, a mood and a short description. Call it once per game.",
        bind_game_sound,
    ))

    # ------------------------------------------------------------------
    # get_game_sound — what a game is bound to play
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_game_sound(
        game_id: Annotated[str, "The game's id as the app knows it"],
        state: Annotated[str, "Which state of the game: bgm, correct, wrong, complete, intro…"] = "bgm",
        level: Annotated[str, "The level being played, when levels have their own sounds"] = "",
    ) -> str:
        """A sound this game is bound to play. REUSE reads it; it never composes."""
        slot = str(game_id or '').strip()
        which = str(state or 'bgm').strip() or 'bgm'
        if which not in GAME_STATES:
            return (f"{which} is not one of a game's states. Use one of: "
                    f"{', '.join(sorted(GAME_STATES))}.")
        level = str(level or '').strip()
        bound, matched = game_state_sound(
            agent_data.get(prompt_id, {}).get('games', {}), slot, which, level, user_id)
        if bound.get('url'):
            return json.dumps({
                'status': 'bound',
                'game_id': slot,
                'state': which,
                'matched': matched,
                'approved': bool(bound.get('approved_at')),
                'music': bound,
            })
        if bound.get('task_id'):
            return json.dumps({'status': 'composing', 'game_id': slot,
                               'state': which, 'task_id': bound['task_id']})
        return json.dumps({'status': 'unbound', 'game_id': slot, 'state': which,
                           'note': f'No {which} is bound to this game yet.'})

    # ------------------------------------------------------------------
    # approve_game_sound — the reviewer's word on a game's music
    # ------------------------------------------------------------------
    @log_tool_execution
    def approve_game_sound(
        game_id: Annotated[str, "The game whose sound the reviewer just approved"],
        approved: Annotated[bool, "True when the reviewer accepts this sound, False to drop it so it can be composed again"] = True,
        state: Annotated[str, "Which state of the game: bgm, correct, wrong, complete, intro…"] = "bgm",
        reason: Annotated[str, "Why it was rejected, in the reviewer's words — the next take is composed to answer it"] = "",
        level: Annotated[str, "The level, when this sound belongs to one level"] = "",
        scope: Annotated[str, "'agent' is the reviewer deciding for everyone; 'mine' is this person correcting their own copy"] = "agent",
    ) -> str:
        """Record that the reviewer approved (or rejected) a game's music.

        The reviewer meets this agent in Evaluation Mode after creation and
        hears the game's music there.  Their word is recorded on the
        binding, so the person who reuses this agent gets the music that
        was approved.  A rejection clears the binding, and the next
        bind_game_sound composes a fresh one.
        """
        slot = str(game_id or '').strip()
        which = str(state or 'bgm').strip() or 'bgm'
        if which not in GAME_STATES:
            return (f"{which} is not one of a game's states. Use one of: "
                    f"{', '.join(sorted(GAME_STATES))}.")
        level = str(level or '').strip()
        mine = user_id if str(scope or 'agent').strip() == 'mine' else None
        games = agent_data.setdefault(prompt_id, {}).setdefault('games', {})
        # A verdict belongs to the memo it was GIVEN, and the ladder may have
        # found that under a different key than the one asked for: rejecting
        # while playing level 3 wrote at 'correct@3' and left 'correct' --
        # the take actually sounding -- playing on, url intact.  A person
        # correcting their own copy still writes in their own space.
        music, matched, write_key = record_verdict(
            games, slot, which, approved, reason, level, mine)
        if not music:
            return (f"No {which} is bound to {slot or 'that game'} yet, so "
                    f"there is nothing to approve.")
        try:
            helper_fun.save_agent_data_to_file(prompt_id, agent_data)
        except Exception as e:
            tool_logger.warning(f'approve_game_sound could not persist: {e}')
        return json.dumps({
            'status': 'approved' if approved else 'rejected',
            'game_id': slot,
            'state': which,
            'music': game_state_sound(games, slot, which, level, mine,
                                      own_only=bool(mine))[0] or None,
            'scope': 'mine' if mine else 'agent',
            # which memo the verdict landed on, so a caller can see that a
            # level-3 rejection marked the game-wide take that was playing
            'matched': matched,
            'key': write_key,
        })

    tools.append((
        "approve_game_sound",
        "Record the reviewer's decision on a kids game's music during review: "
        "approved keeps it for everyone who reuses this agent, rejected drops "
        "it so it can be composed again.",
        approve_game_sound,
    ))

    tools.append((
        "get_game_sound",
        "The music bound to a kids game by this agent, if any. Use it before "
        "playing a game so the sound stays the one the reviewer approved.",
        reads_persisted_state(get_game_sound),
    ))

    tools.append((
        "save_data_in_memory",
        "Use this to Store and retrieve data using key-value storage system",
        # Marked reads_persisted_state (#104): its result reports what is now
        # stored, so the group chat's write-back does not store it again. The
        # same mark goes on every tool below whose result is a read of state
        # HARTOS already keeps.
        reads_persisted_state(save_data_in_memory),
    ))

    # ------------------------------------------------------------------
    # 4. get_saved_metadata
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_saved_metadata() -> str:
        """Get metadata with automatic loading from persistent storage."""
        if prompt_id not in agent_data or not agent_data[prompt_id]:
            tool_logger.info(f"Loading agent data from file for get_saved_metadata, prompt_id {prompt_id}")
            helper_fun.load_agent_data_from_file(prompt_id, agent_data)
        stripped_json = _strip_json_values(agent_data[prompt_id])
        return f'{stripped_json}'

    tools.append((
        "get_saved_metadata",
        "Returns the schema of the json from internal memory with all keys but without actual values.",
        reads_persisted_state(get_saved_metadata),
    ))

    # ------------------------------------------------------------------
    # 5. get_data_by_key
    # ------------------------------------------------------------------
    _KEY_NOT_FOUND = "Key not found in stored data."

    def _read_saved(key):
        """The value saved at ``key``, whole, or the not-found sentinel.

        For code in this module that needs the value as stored (the receipt
        template, the save check). The get_data_by_key tool pages what the
        model reads; the receipt read its template through that tool, so a
        template longer than a page was cut and the page note was printed into
        the customer's receipt (#104 review).
        """
        if prompt_id not in agent_data or not agent_data[prompt_id]:
            tool_logger.info(f"Loading agent data from file for prompt_id {prompt_id}")
            helper_fun.load_agent_data_from_file(prompt_id, agent_data)
        keys = key.split('.')
        d = agent_data.get(prompt_id, {})
        try:
            for k in keys:
                d = d[k]
            return f'{d}'
        # TypeError too: a path that runs through a None, a string or a list
        # is as missing as an absent key. It used to escape as a tool
        # exception and skip the fallback below (central 2026-09-13, a hive
        # reuse turn asking for a nested key under a None value).
        except (KeyError, TypeError):
            # Fallback: check MemoryGraph for persisted [KV] data — the
            # read half of save_data_in_memory's dual-write, carried by
            # reuse_recipe's inline twin before the #743 migration and
            # restored here (owner audit 2026-09-01).
            if memory_graph is not None:
                try:
                    results = memory_graph.recall(f"[KV] {key}", mode='text', top_k=1)
                    if results:
                        return results[0].content
                except Exception:
                    pass
            return _KEY_NOT_FOUND

    @log_tool_execution
    def get_data_by_key(
        key: Annotated[str, "Key path for retrieving data. Use dot notation for nested keys (e.g., 'user.info.name')."],
        offset: Annotated[int, "Where to start reading a long value, in characters. Leave 0 to read from the start."] = 0,
    ) -> str:
        # One page of the value, not all of it (#104): a key like 'hive'
        # returned the whole subtree, which went to the model and back into
        # memory. A long value is read a page at a time, the way book pages
        # are, and the note names the offset of the next page.
        from core.constants import TOOL_OBSERVATION_MAX_CHARS as page_chars
        # A pointer the wire trim put where it elided text
        # ([elided:<id> ...], core.llm_outbound_logger): the original, whole,
        # from the store's `elided` namespace -- no per-item cap.
        from core.llm_outbound_logger import (
            ELIDED_KEY_PREFIX, elision_scope, read_elided)
        if str(key).strip().startswith(ELIDED_KEY_PREFIX):
            # Only this user's elided text (or, when the call that elided it
            # knew no user, this request's): never another user's.
            pid = str(key).strip()[len(ELIDED_KEY_PREFIX):]
            value = read_elided(pid, elision_scope(user_id=user_id))
            if value is None and request_id_list.get(user_prompt):
                value = read_elided(pid, elision_scope(
                    request_id=request_id_list.get(user_prompt)))
            if value is None:
                return (f'Nothing is stored for {key}: the elided text is kept '
                        f'for a day, and this one is gone or never existed.')
        else:
            value = _read_saved(key)
        try:
            start = max(0, int(offset or 0))
        except (TypeError, ValueError):
            start = 0
        if start and start >= len(value):
            return f'...[offset {start} is past the end of the value ({len(value)} chars)]'
        page = value[start:start + page_chars]
        end = start + len(page)
        if end >= len(value):
            return page
        return (f'{page}\n...[chars {start}-{end} of {len(value)}; call '
                f'get_data_by_key with offset={end} for the rest]')

    tools.append((
        "get_data_by_key",
        "Returns the data saved at a key. A long value comes back one page at a "
        "time; pass the offset the reply names to read the next page.",
        reads_persisted_state(get_data_by_key),
    ))
    # Alias — Helper system prompts in reuse_recipe.py advertise this name (#510).
    # Same closure → identical behavior under both names, the persisted-read
    # mark included.  Never remove a registered tool: phantom tool fixed by
    # adding a real registration.
    tools.append((
        "get_data_from_memory",
        "Returns the data saved at a key, a page at a time (alias of get_data_by_key)",
        get_data_by_key,
    ))

    # ------------------------------------------------------------------
    # 5b. generate_receipt (#752 — receipt for a service, over any channel)
    # ------------------------------------------------------------------
    @log_tool_execution
    def generate_receipt(
        service: Annotated[str, "Service provided, e.g. 'Bridal makeup'."],
        amount: Annotated[str, "Amount charged, e.g. '5000'."],
        client_name: Annotated[str, "Client's name."] = "",
        currency: Annotated[str, "Currency code or symbol, e.g. 'INR'."] = "INR",
        date: Annotated[str, "Receipt date; defaults to today when omitted."] = "",
        notes: Annotated[str, "Optional notes or line items."] = "",
        business_name: Annotated[str, "Your business or artist name."] = "",
        advance: Annotated[str, "Advance already received against the total, e.g. '5000'. Required every time; use '0' when none."] = "",
        event_timing: Annotated[str, "Event-day timing, e.g. 'ready by 6am, event at 11am'. Required every time."] = "",
        render: Annotated[str, "'text' (default) or 'image' for the branded PNG receipt with the saved logo."] = "text",
    ) -> str:
        """Fill the user's accepted receipt template with the given details.

        Balance is computed HERE (total - advance), never by the model -
        money math is deterministic code. render='image' additionally
        produces the branded PNG (logo saved once via set_receipt_logo)
        and appends a [[MEDIA:<path>]] marker that the channel reply path
        converts into a real image attachment on the same channel.

        Reuses the per-prompt KV store (save_data_in_memory / get_data_by_key)
        as the "template they accept": the agent proposes a template, the user
        confirms it, and it is saved under key 'receipt_template'.  Falls back to
        a neutral default when none is stored.  Renders via the existing
        TemplateEngine and returns the receipt text for the agent to send back
        over the same channel the request arrived on (WhatsApp, etc.).
        """
        from integrations.service_tools.receipt_image import (
            compute_balance, render_receipt_png)
        balance = compute_balance(amount, advance) or ""
        # The template as saved: the get_data_by_key tool pages what the model
        # reads, and a paged template printed its page note into the receipt.
        template = _read_saved("receipt_template")
        if not template or template == _KEY_NOT_FOUND:
            template = _DEFAULT_RECEIPT_TEMPLATE
        fields = {
            "business_name": business_name,
            "client_name": client_name,
            "service": service,
            "amount": amount,
            "currency": currency,
            "date": date or datetime.now().strftime("%Y-%m-%d"),
            "notes": notes,
            "advance": advance,
            "event_timing": event_timing,
            "balance": balance,
        }
        from integrations.channels.response.templates import TemplateEngine
        text = TemplateEngine().render(template, extra_vars=fields)
        if str(render).lower() != "image":
            return text
        logo_path = _read_saved("receipt_logo_path")
        if logo_path == _KEY_NOT_FOUND:
            logo_path = None
        png = render_receipt_png(fields, logo_path=logo_path)
        if not png:
            return (text + "\n(Image receipt unavailable on this install - "
                    "sent as text instead.)")
        return text + "\n[[MEDIA:" + png + "]]"

    tools.append((
        "generate_receipt",
        "Generate a receipt for a service the user provided, using their "
        "accepted receipt template. EVERY receipt must collect: total amount, "
        "date, event-day timing, and advance received (use '0' if none) - ask "
        "for any that are missing, confirm the parsed values back to the user, "
        "THEN call this. Balance due is computed automatically. On first use, "
        "propose a template; once the user confirms it, save it with "
        "save_data_in_memory under key 'receipt_template'. Pass render='image' "
        "to produce the branded PNG receipt (set the logo once via "
        "set_receipt_logo). Returns the receipt to send back to the user.",
        generate_receipt,
    ))

    # ------------------------------------------------------------------
    # 5c. set_receipt_logo (#752 leg B - capture the logo ONCE, durably)
    # ------------------------------------------------------------------
    @log_tool_execution
    def set_receipt_logo(
        file_path: Annotated[str, "Path to the uploaded logo image file on this machine."],
    ) -> str:
        """Persist the business logo for all future receipts.

        Inbound channel media lands in short-lived locations (the upload
        TTLCache lives 2 hours), so the logo is COPIED once to the durable
        data dir and its path stored in the per-prompt KV under
        'receipt_logo_path' - the same store the accepted template uses.
        """
        import shutil
        src = str(file_path or "").strip().strip('"')
        if not src or not os.path.isfile(src):
            return ("Logo file not found at '" + src + "'. Ask the user to "
                    "send the logo image again, then retry with its saved path.")
        ext = os.path.splitext(src)[1].lower()
        if ext not in ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'):
            return ("'" + ext + "' is not an image type I can put on a "
                    "receipt - please send PNG or JPG.")
        from core.platform_paths import get_data_dir
        dest_dir = os.path.join(get_data_dir(), 'receipt_assets', str(prompt_id))
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, 'logo' + ext)
        try:
            shutil.copy2(src, dest)
        except OSError as e:
            tool_logger.error("set_receipt_logo copy failed: %s" % e)
            return "Could not save the logo: %s" % e
        save_data_in_memory('receipt_logo_path', dest)
        return ("Logo saved for all future receipts (" + os.path.basename(dest)
                + "). It will appear on every image receipt from now on.")

    tools.append((
        "set_receipt_logo",
        "Save the user's business logo ONCE for all future receipts. Call this "
        "when the user sends their logo image (get the file path from the "
        "uploaded file). The logo is stored durably and composited onto every "
        "render='image' receipt.",
        set_receipt_logo,
    ))

    # ------------------------------------------------------------------
    # 6. get_user_id
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_user_id() -> str:
        tool_logger.info('INSIDE get_user_id')
        return f'{user_id}'

    tools.append((
        "get_user_id",
        "Returns the unique identifier (user_id) of the current user.",
        get_user_id,
    ))

    # ------------------------------------------------------------------
    # 7. get_prompt_id
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_prompt_id() -> str:
        tool_logger.info('INSIDE get_prompt_id')
        return f'{prompt_id}'

    tools.append((
        "get_prompt_id",
        "Returns the unique identifier (prompt_id) associated with the current prompt or conversation.",
        get_prompt_id,
    ))

    # ------------------------------------------------------------------
    # 8. Generate_video (canonical — full LTX-2 + avatar)
    # ------------------------------------------------------------------
    @log_tool_execution
    def Generate_video(
        text: Annotated[str, "Text to be used for video generation"],
        avatar_id: Annotated[int, "Unique identifier for the avatar (use 0 for LTX-2 text-to-video)"],
        realtime: Annotated[bool, "If True, response is fast but less realistic by default it should be true; if False, response is realistic but slower"],
        model: Annotated[str, "Video model to use: 'avatar' for avatar-based video, 'ltx2' for LTX-2 text-to-video generation"] = "avatar",
    ) -> str:
        tool_logger.info(f'INSIDE Generate_video with model={model}')

        # LTX-2 Text-to-Video Generation
        if model.lower() == "ltx2":
            tool_logger.info(f'Using LTX-2 for video generation: {text[:50]}...')
            LOCAL_COMFYUI_URL = "http://localhost:8188"
            LOCAL_LTX_URL = "http://localhost:5002"
            headers = {'Content-Type': 'application/json'}
            ltx_payload = {
                "prompt": text,
                "negative_prompt": "worst quality, inconsistent motion, blurry, jittery, distorted",
                "num_frames": 97,
                "width": 832,
                "height": 480,
                "num_inference_steps": 30 if realtime else 50,
                "guidance_scale": 3.0,
                "fps": 24,
            }

            # Fast health probe — skip dead servers instantly (0ms vs 10s timeout)
            def _is_server_up(url, name):
                try:
                    r = pooled_get(f"{url}/health", timeout=1.5)
                    return r.status_code < 500
                except Exception:
                    tool_logger.info(f"{name} not reachable — skipping instantly")
                    return False

            # Try local LTX-2 server first — only if alive
            if _is_server_up(LOCAL_LTX_URL, "LTX-2"):
                try:
                    tool_logger.info(f"LTX-2 server is UP, generating...")
                    response = pooled_post(f"{LOCAL_LTX_URL}/generate", json=ltx_payload, headers=headers, timeout=600)
                    if response.status_code == 200:
                        result = response.json()
                        video_url = result.get('video_url') or result.get('output_url') or result.get('video_path')
                        if video_url:
                            tool_logger.info(f"LTX-2 video generated: {video_url}")
                            return f"LTX-2 Video generated successfully. URL: {video_url}"
                except requests.exceptions.RequestException as e:
                    tool_logger.info(f"LTX-2 generation failed: {e}")

            # Try ComfyUI — only if alive
            if _is_server_up(LOCAL_COMFYUI_URL, "ComfyUI"):
                try:
                    tool_logger.info(f"ComfyUI is UP, submitting workflow...")
                    comfyui_workflow = {
                        "prompt": {
                            "1": {"class_type": "LTXVLoader", "inputs": {"ckpt_name": "ltx-video-2b-v0.9.safetensors"}},
                            "2": {"class_type": "LTXVConditioning", "inputs": {"positive": text, "negative": ltx_payload["negative_prompt"], "ltxv_model": ["1", 0]}},
                            "3": {"class_type": "LTXVSampler", "inputs": {"seed": int(time.time()) % 2147483647, "steps": ltx_payload["num_inference_steps"], "cfg": ltx_payload["guidance_scale"], "width": ltx_payload["width"], "height": ltx_payload["height"], "num_frames": ltx_payload["num_frames"], "ltxv_model": ["1", 0], "conditioning": ["2", 0]}},
                            "4": {"class_type": "LTXVDecode", "inputs": {"ltxv_model": ["1", 0], "samples": ["3", 0]}},
                            "5": {"class_type": "VHS_VideoCombine", "inputs": {"frame_rate": ltx_payload["fps"], "filename_prefix": "ltx2_output", "format": "video/h264-mp4", "images": ["4", 0]}},
                        }
                    }
                    response = pooled_post(f"{LOCAL_COMFYUI_URL}/prompt", json=comfyui_workflow, headers=headers, timeout=10)
                    if response.status_code == 200:
                        comfy_prompt_id = response.json().get('prompt_id')
                        tool_logger.info(f"ComfyUI LTX-2 job queued: {comfy_prompt_id}")
                        for _ in range(120):
                            time.sleep(5)
                            history_response = pooled_get(f"{LOCAL_COMFYUI_URL}/history/{comfy_prompt_id}")
                            if history_response.status_code == 200:
                                history = history_response.json()
                                if comfy_prompt_id in history:
                                    outputs = history[comfy_prompt_id].get('outputs', {})
                                    for node_id, output in outputs.items():
                                        for media_key in ('gifs', 'videos'):
                                            if media_key in output:
                                                filename = output[media_key][0].get('filename')
                                                if filename:
                                                    video_url = f"{LOCAL_COMFYUI_URL}/view?filename={filename}"
                                                    return f"LTX-2 Video generated via ComfyUI. URL: {video_url}"
                        return f"LTX-2 Video generation queued in ComfyUI (prompt_id: {comfy_prompt_id}). Check ComfyUI interface for output."
                except requests.exceptions.RequestException as e:
                    tool_logger.info(f"ComfyUI generation failed: {e}")

            # Local servers unavailable — try hive mesh peer with GPU
            try:
                from integrations.agent_engine.compute_config import get_compute_policy
                policy = get_compute_policy()
                if policy.get('compute_policy') != 'local_only':
                    from integrations.agent_engine.compute_mesh_service import get_compute_mesh
                    mesh = get_compute_mesh()
                    # user_id: the person this peer's compute is charged to
                    # (ComputeMeshService._charged).
                    result = mesh.offload_to_best_peer(
                        model_type=ModelType.VIDEO_GEN,
                        prompt=text,
                        options={'model': 'ltx2', 'timeout': 300,
                                 'user_id': str(user_id or '')},
                    )
                    if result and 'error' not in result:
                        video_url = result.get('response', result.get('video_url', ''))
                        peer = result.get('offloaded_to', 'hive_peer')
                        tool_logger.info(f"LTX-2 video generated via hive peer {peer}: {video_url}")
                        return f"LTX-2 Video generated via hive peer. URL: {video_url}"
                    tool_logger.info(f"Hive mesh video offload failed: {result.get('error')}")
            except Exception as e:
                tool_logger.info(f"Hive mesh offload not available: {e}")

            return ("LTX-2 video generation failed. No local GPU, no hive peers with GPU. "
                    "Options: (1) Pair a GPU device: hart compute pair <address>, "
                    "(2) Set HEVOLVE_COMPUTE_POLICY=any, "
                    "(3) Install local LTX-2 server with CUDA GPU")

        # Default: Avatar-based video generation
        from core.config_cache import get_db_url
        from core.teacher_avatar import lookup_avatar
        database_url = get_db_url() or 'https://mailer.hertzai.com'
        request_id = str(uuid.uuid4()).replace("-", "")[:11]
        tool_logger.info(f"avtar_id: {avatar_id}:\n{text[:10]}....\n")

        headers = {'Content-Type': 'application/json'}
        data = {
            "text": str(text),
            'flag_hallo': 'false',
            'chattts': False,
            'openvoice': "false",
        }

        # The avatar's image and voice sample: the one lookup a spoken reply
        # uses too (core/teacher_avatar.py).
        avatar = lookup_avatar(avatar_id, database_url)
        if avatar['openvoice']:
            data['openvoice'] = "true"

        data["cartoon_image"] = "True"
        data["bg_url"] = 'http://stream.mcgroce.com/txt/examples_cartoon/roy_bg.jpg'
        data['vtoonify'] = "false"
        data["image_url"] = avatar['image_url']
        data['im_crop'] = "false"
        data['remove_bg'] = "false"
        data['hd_video'] = "false"
        data['uid'] = str(request_id)
        data['gradient'] = "true"
        data['cus_bg'] = "false"
        data['solid_color'] = "false"
        data['inpainting'] = "false"
        data['prompt'] = ""
        data['gender'] = 'male'

        timeout = 60
        if not realtime:
            timeout = 600
            data['chattts'] = True
            data['flag_hallo'] = "true"
            data["cartoon_image"] = "False"

        data["audio_sample_url"] = avatar['audio_sample_url']
        data['voice_id'] = avatar['voice_id']

        conv_id = save_conversation_db(text, user_id, prompt_id, database_url, request_id)
        data['conv_id'] = int(conv_id)
        data['avatar_id'] = int(avatar_id)
        data['timeout'] = int(timeout)

        try:
            pooled_post(f"{database_url}/video_generate_save",
                          data=json.dumps(data), headers=headers, timeout=1)
        except Exception:
            pass

        if data['chattts'] or data['flag_hallo'] == "true":
            return f"Video Generation task added to queue with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"
        else:
            return f"Video Generation completed with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"

    tools.append((
        "Generate_video",
        "Generate video with text. Use model='ltx2' for AI text-to-video generation, or model='avatar' (default) for avatar-based video with voice synthesis.",
        Generate_video,
    ))

    # ------------------------------------------------------------------
    # 9. get_user_uploaded_file
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_user_uploaded_file() -> str:
        tool_logger.info('INSIDE get_user_uploaded_file')
        # .get(), not [] — recent_file_id is a TTLCache written only when a
        # file is actually uploaded, so a user who uploaded nothing has no
        # key and [] raised KeyError (44/44 failures, 4/4 on 2026-09-07).
        # That case is exactly the answer below, which was unreachable.
        file_id = recent_file_id.get(user_id)
        if file_id:
            return f'Got user uploaded file the file_id is {file_id}'
        return 'No file uploaded from user'

    tools.append((
        "get_user_uploaded_file",
        "get user's recent uploaded files",
        get_user_uploaded_file,
    ))

    # ------------------------------------------------------------------
    # 10. get_text_from_image (img2txt)
    # ------------------------------------------------------------------
    @log_tool_execution
    def img2txt(
        image_url: Annotated[str, "image url of which you want text"],
        text: Annotated[str, "the details you want from image"] = 'Describe the Images & Text data in this image in detail',
    ) -> str:
        tool_logger.info('INSIDE img2txt')
        # SSRF protection: validate image URL before fetching
        try:
            from security.sanitize import validate_url
            image_url = validate_url(image_url)
        except (ImportError, ValueError) as e:
            tool_logger.warning(f"Image URL blocked by SSRF filter: {image_url} — {e}")
            return f"Error: URL blocked by security filter: {e}"
        # Try local Qwen Vision first (bundled mode), fall back to cloud
        from core.config_cache import get_vision_api, is_bundled
        url = get_vision_api()
        if not url:
            tool_logger.warning("No LLAVA_API configured — vision inference may fail on no-GPU instances")
            url = "http://azurekong.hertzai.com:8000/llava/image_inference"

        if is_bundled():
            # Local: use Qwen Vision via upload/vision endpoint
            payload = json.dumps({'image_url': image_url, 'prompt': text})
            response = requests.post(url, data=payload,
                                     headers={'Content-Type': 'application/json'}, timeout=60)
        else:
            payload = {'url': image_url, 'prompt': text}
            response = requests.request("POST", url, headers={}, data=payload, files=[], timeout=300)
        if response.status_code == 200:
            return response.text
        else:
            return 'Not able to get this page details try later'

    tools.append((
        "get_text_from_image",
        "Image to Text/Question Answering from image",
        img2txt,
    ))
    # Alias — reuse_recipe.py main flow LLM prompts advertise `img2txt` (#510).
    # Same canonical closure (SSRF-validated, local Qwen Vision + cloud LLaVA fallback).
    tools.append((
        "img2txt",
        "Image to Text/Question Answering from image (alias of get_text_from_image)",
        img2txt,
    ))

    # ------------------------------------------------------------------
    # 11. create_scheduled_jobs
    # ------------------------------------------------------------------
    @log_tool_execution
    def create_scheduled_jobs(
        interval_sec: Annotated[int, "time between two Interval in seconds."],
        job_description: Annotated[str, "Description of the job to be performed"],
        cron_expression: Annotated[Optional[str], "Cron expression for scheduling. Example: '0 9 * * 1-5' (Runs at 9:00 AM, Monday to Friday). If the interval is greater than 60 seconds or it needs to be executed at a dynamic cron time this argument is Mandatory else None"] = None,
    ) -> str:
        tool_logger.info('INSIDE create_scheduled_jobs')
        return 'Added this schedule job in creation process will do it at the end. you can go ahead and mark this action as completed.'

    tools.append((
        "create_scheduled_jobs",
        "Creates time-based jobs using APScheduler to schedule jobs",
        create_scheduled_jobs,
    ))

    # ------------------------------------------------------------------
    # 12. send_message_to_user
    # ------------------------------------------------------------------
    # Guard absorbed from reuse_recipe's inline twin (#743 migration):
    # group-chat models sometimes route "@helper ..." steering text into
    # this tool — that internal chatter must never reach the user.
    _AGENT_MENTIONS = ("@statusverifier", "@status verifier", "@verification",
                      "@helper", "@executor")

    @log_tool_execution
    def send_message_to_user(
        text: Annotated[str, "Text you want to send to the user"],
        avatar_id: Annotated[Optional[str], "Unique identifier for the avatar"] = None,
        response_type: Annotated[Optional[str], "Response mode: 'Realistic' (slower, better quality) or 'Realtime' (faster, lower quality)"] = 'Realtime',
    ) -> str:
        low = text.lower()
        mention = next((m for m in _AGENT_MENTIONS if m in low), None)
        if mention is not None:
            tool_logger.info(
                f'Message directed to agent ({mention}), not sending to user: {text[:50]}...')
            return f'Message directed to {mention} agent, not sending to user'
        tool_logger.info('INSIDE send_message_to_user')
        tool_logger.info(f'SENDING DATA 2 user with values text:{text}, avatar_id:{avatar_id}, response_type:{response_type}')
        # The send runs HERE and its result is the tool's result.  This used
        # to start the send on a thread and return "sent successfully" before
        # it ran: Nunba gui_app.log 2026-09-26 00:02:10 the tool answered
        # sent, 00:02:19 the send failed (WinError 10061), and the agent
        # waited on a question the user never saw.  Both
        # send_message_to_user1 copies return "Message sent successfully ..."
        # or "Failed to send message ...".
        result = send_message_to_user1(user_id, text, '', prompt_id)
        if isinstance(result, str) and result:
            return result
        return 'Message handed to the sender; its delivery was not confirmed'

    tools.append((
        "send_message_to_user",
        "Sends a message/information to user. You can use this if you want to ask a question",
        send_message_to_user,
    ))

    # ------------------------------------------------------------------
    # 13. send_presynthesized_video_to_user
    # ------------------------------------------------------------------
    @log_tool_execution
    def send_presynthesized_video_to_user(
        conv_id: Annotated[str, "Conversation ID associated with the text from memory"],
    ) -> str:
        tool_logger.info('INSIDE send_presynthesized_video_to_user')
        tool_logger.info(f'SENDING DATA 2 user with value: conv_id:{conv_id}.')
        return 'Message sent successfully to user'

    tools.append((
        "send_presynthesized_video_to_user",
        "Sends a presynthesized message/video/dialogue to user using conv_id.",
        send_presynthesized_video_to_user,
    ))

    # ------------------------------------------------------------------
    # 14. send_message_in_seconds
    # ------------------------------------------------------------------
    @log_tool_execution
    def send_message_in_seconds(
        text: Annotated[str, "text to send to user"],
        delay: Annotated[int, "time to wait in seconds before sending text"],
        conv_id: Annotated[Optional[int], "conv_id for this text if not available make it None"] = None,
    ) -> str:
        tool_logger.info('INSIDE send_message_in_seconds')
        tool_logger.info(f'with text:{text}. and waiting time: {delay} conv_id: {conv_id}')
        run_time = datetime.fromtimestamp(time.time() + delay)
        scheduler.add_job(send_message_to_user1, 'date', run_date=run_time, args=[user_id, text, '', prompt_id])
        return 'Message scheduled successfully'

    tools.append((
        "send_message_in_seconds",
        "Sends a presynthesized message/video/dialogue to user using conv_id with a timer.",
        send_message_in_seconds,
    ))

    # ------------------------------------------------------------------
    # 15. get_chat_history
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_chat_history(
        text: Annotated[str, "Text related to which you want history"],
        start: Annotated[Optional[str], "start date in format %Y-%m-%dT%H:%M:%S.%fZ"] = None,
        end: Annotated[Optional[str], "end date in format %Y-%m-%dT%H:%M:%S.%fZ"] = None,
    ) -> str:
        tool_logger.info('INSIDE get_chat_history')
        return helper_fun.get_time_based_history(text, f'user_{user_id}', start, end)

    tools.append((
        "get_chat_history",
        "Get Chat history based on text & start & end date",
        reads_persisted_state(get_chat_history),
    ))

    # ------------------------------------------------------------------
    # 16. search_visual_history
    # ------------------------------------------------------------------
    @log_tool_execution
    def search_visual_history(
        query: Annotated[str, "What to search for in visual/screen descriptions"],
        minutes_back: Annotated[int, "How many minutes back to search (default 30)"] = 30,
        channel: Annotated[str, "Which feed: 'camera', 'screen', or 'both' (default)"] = "both",
    ) -> str:
        """Search past camera/screen descriptions. Use for questions about what happened earlier visually."""
        results = helper_fun.search_visual_history(user_id, query, mins=minutes_back, channel=channel)
        if results:
            return '\n'.join(results)
        return "No matching visual/screen descriptions found in the given time range."

    tools.append((
        "search_visual_history",
        "Search past camera and screen descriptions by keyword and time range.",
        reads_persisted_state(search_visual_history),
    ))

    # ------------------------------------------------------------------
    # 17. google_search
    # ------------------------------------------------------------------
    @log_tool_execution
    def google_search(
        text: Annotated[str, "Text/Query which you want to search"],
    ) -> str:
        tool_logger.info('INSIDE google search')
        return helper_fun.top5_results(text)

    tools.append((
        "google_search",
        "web/google/bing search api tool for a given query",
        google_search,
    ))

    # ------------------------------------------------------------------
    # Conditional: long-term memory — SimpleMem preferred, MemoryGraph fallback
    # ------------------------------------------------------------------
    # Gated on EITHER store, not SimpleMem alone.  SimpleMem only constructs
    # when `sm_config.enabled and sm_config.api_key` (reuse_recipe.py:963,
    # create_recipe.py:886 — byte-identical twins), so a local desktop with no
    # cloud key silently got neither tool, while nine prompt sites kept
    # instructing the model to call save_to_long_term_memory by name.  Measured
    # live 2026-09-05 (Auto Research 18088688973): of the 18 _MAIN_LEG_CORE
    # tools, these two — and only these two, the only two gated here — were
    # missing from all 6 wire bodies.  The model skipped the save step with no
    # error, then read an empty store 24x and asserted completion on nothing.
    #
    # MemoryGraph needs no key, initialized 27x in that same log with 0
    # failures (including for this agent), and was ALREADY the dual-write
    # target below and the read-back path in get_data_by_key.  So it backs the
    # tools when SimpleMem is absent rather than the capability disappearing.
    if simplemem_store is not None or memory_graph is not None:
        from core.event_loop import get_or_create_event_loop

        @log_tool_execution
        def search_long_term_memory(
            query: Annotated[str, "Natural language query to search long-term memory"],
        ) -> str:
            """Search compressed long-term memory using semantic retrieval."""
            if simplemem_store is not None:
                try:
                    loop = get_or_create_event_loop()
                    results = loop.run_until_complete(simplemem_store.search(query))
                    # SimpleMem's item is an answer, not a stored row: cut it,
                    # never skip it.
                    text = _bounded_recall(
                        [r.content for r in (results or [])], max_items=1,
                        skip_oversize=False)
                    return text or "No relevant memories found."
                except Exception as e:
                    tool_logger.info(f"SimpleMem search error: {e}")
                    return "Memory search unavailable."
            # MemoryGraph leg — same contract, local store, no API key.
            try:
                # Fetch past the 5 shown: rows _bounded_recall skips as
                # over-size must not leave real memories unreturned (#104).
                results = memory_graph.recall(query, mode='hybrid', top_k=10)
                text = _bounded_recall(
                    [r.content for r in (results or [])], max_items=5)
                return text or "No relevant memories found."
            except Exception as e:
                tool_logger.info(f"MemoryGraph search error: {e}")
                return "Memory search unavailable."

        tools.append((
            "search_long_term_memory",
            "Search long-term memory for past conversations, facts, and context using natural language query.",
            reads_persisted_state(search_long_term_memory),
        ))

        @log_tool_execution
        def save_to_long_term_memory(
            content: Annotated[str, "The information/fact to remember long-term"],
            speaker: Annotated[str, "Who said this (e.g. 'User', 'Assistant', 'System')"] = "System",
        ) -> str:
            """Save important information to compressed long-term memory."""
            if simplemem_store is None:
                # MemoryGraph leg.  SYNCHRONOUS on purpose: the dual-write
                # below can be fire-and-forget because SimpleMem already
                # persisted, but when the graph is the ONLY store, a detached
                # thread would let this return "Saved" for a write that never
                # landed — the fabricated success this whole fix exists to
                # remove.  Report what actually happened.
                try:
                    memory_graph.register(
                        content, {'memory_type': 'fact',
                                  'source_agent': speaker,
                                  'session_id': user_prompt,
                                  'source': 'memory_graph'},
                    )
                    return "Saved to long-term memory."
                except Exception as e:
                    tool_logger.info(f"MemoryGraph save error: {e}")
                    return "Failed to save to long-term memory."
            try:
                loop = get_or_create_event_loop()
                loop.run_until_complete(simplemem_store.add(content, {
                    "sender_name": speaker,
                    "user_id": user_id,
                    "prompt_id": prompt_id,
                }))
                # Dual-write to MemoryGraph (fire-and-forget).  Carried by
                # reuse_recipe's inline twin before the #743 migration and
                # lost in the swap — restored HERE so all three legs
                # (main/time/visual) get graph provenance, not just reuse.
                if memory_graph is not None:
                    try:
                        threading.Thread(target=lambda: memory_graph.register(
                            content, {'memory_type': 'fact',
                                      'source_agent': speaker,
                                      'session_id': user_prompt,
                                      'source': 'simplemem'},
                        ), daemon=True).start()
                    except Exception:
                        pass
                return "Saved to long-term memory."
            except Exception as e:
                tool_logger.info(f"SimpleMem save error: {e}")
                return "Failed to save to long-term memory."

        tools.append((
            "save_to_long_term_memory",
            "Save important facts or information to long-term memory for future retrieval across sessions.",
            save_to_long_term_memory,
        ))

    # ------------------------------------------------------------------
    # Suggest_Share_Worthy_Content
    # ------------------------------------------------------------------
    @log_tool_execution
    def suggest_share_worthy_content(
        query: Annotated[str, "Any text — not used for filtering, just provide context"] = "",
    ) -> str:
        """Find high-engagement posts that haven't been shared much and suggest sharing them."""
        try:
            from integrations.social.models import get_db, Post, ShareableLink
            from sqlalchemy import func as sa_func

            db = get_db()
            try:
                share_counts = (
                    db.query(
                        ShareableLink.resource_id,
                        sa_func.count(ShareableLink.id).label('link_count'),
                    )
                    .filter(ShareableLink.resource_type == 'post')
                    .group_by(ShareableLink.resource_id)
                    .subquery()
                )

                posts = (
                    db.query(Post, share_counts.c.link_count)
                    .outerjoin(share_counts, Post.id == share_counts.c.resource_id)
                    .filter(
                        Post.is_deleted == False,
                        Post.is_hidden == False,
                        Post.upvotes > 5,
                        Post.comment_count > 3,
                    )
                    .filter(
                        (share_counts.c.link_count == None) |  # noqa: E711
                        (share_counts.c.link_count < 3)
                    )
                    .order_by(Post.score.desc())
                    .limit(3)
                    .all()
                )

                if not posts:
                    return ("No under-shared high-engagement content found right now. "
                            "Keep creating great posts and the community will notice!")

                suggestions = []
                for post, link_count in posts:
                    title = (post.title or post.content or '')[:80].strip()
                    shares = link_count or 0
                    suggestions.append(
                        f"- \"{title}\" ({post.upvotes} upvotes, "
                        f"{post.comment_count} comments, only {shares} shares) "
                        f"[post_id: {post.id}]"
                    )

                header = ("These posts are resonating with the community but haven't "
                           "been shared much yet. Consider sharing them:\n")
                return header + "\n".join(suggestions)
            finally:
                db.close()
        except Exception as e:
            tool_logger.warning(f"Suggest_Share_Worthy_Content failed: {e}")
            return f"Could not fetch share-worthy content right now: {e}"

    tools.append((
        "Suggest_Share_Worthy_Content",
        "Find high-engagement posts that deserve wider reach but haven't been shared much. "
        "Use when the user asks about content worth sharing or to proactively suggest "
        "share-worthy community posts.",
        suggest_share_worthy_content,
    ))

    # ------------------------------------------------------------------
    # Observe_User_Experience
    # ------------------------------------------------------------------
    @log_tool_execution
    def observe_user_experience(
        input_text: Annotated[str, "JSON string with event, page, duration_ms, outcome fields"],
    ) -> str:
        """Record a user experience observation for self-improvement."""
        try:
            data = json.loads(input_text) if input_text.startswith('{') else {'event': input_text}
            event = data.get('event', 'interaction')
            page = data.get('page', '')
            duration_ms = data.get('duration_ms', 0)
            outcome = data.get('outcome', 'recorded')

            observation = f"User {event} on {page} ({duration_ms}ms): {outcome}"

            if memory_graph:
                session_key = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)
                memory_id = memory_graph.register(
                    content=observation,
                    metadata={
                        'memory_type': 'observation',
                        'source_agent': 'agent',
                        'session_id': session_key,
                        'page': page,
                        'event': event,
                    },
                    context_snapshot=f"UX observation during session {session_key}",
                )
                return f"Observation recorded (id: {memory_id}): {observation}"

            return f"Observation noted: {observation}"
        except Exception as e:
            tool_logger.warning(f"Observe_User_Experience failed: {e}")
            return f"Observation noted: {input_text}"

    tools.append((
        "Observe_User_Experience",
        "Record a user experience observation. Input: JSON with event, page, "
        "duration_ms, outcome. Used for self-improvement and understanding user "
        "behavior patterns.",
        observe_user_experience,
    ))

    # ------------------------------------------------------------------
    # Self_Critique_And_Enhance
    # ------------------------------------------------------------------
    @log_tool_execution
    def self_critique_and_enhance(
        input_text: Annotated[str, "Topic or area to critique"],
    ) -> str:
        """Review past suggestions and outcomes to improve future behavior."""
        try:
            if not memory_graph:
                return f"Self-critique on '{input_text}': Will adjust future behavior based on observations."

            session_key = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)

            # Recall past suggestions and observations
            suggestions = memory_graph.recall(
                input_text or 'suggestions made outcomes', mode='semantic', top_k=10,
            )
            observations = memory_graph.recall(
                'user experience observation', mode='semantic', top_k=10,
            )

            if not suggestions and not observations:
                return "No past interactions to critique yet. Will observe and learn."

            # Format findings for agent reasoning
            critique = "Self-critique findings:\n"
            if suggestions:
                critique += f"Past suggestions ({len(suggestions)}):\n"
                for s in suggestions[:5]:
                    critique += f"  - {s.content[:100]}\n"
            if observations:
                critique += f"User observations ({len(observations)}):\n"
                for o in observations[:5]:
                    critique += f"  - {o.content[:100]}\n"

            # Store the critique itself as an insight
            insight = f"Self-critique on: {input_text}"
            memory_graph.register(
                content=insight,
                metadata={
                    'memory_type': 'insight',
                    'source_agent': 'agent',
                    'session_id': session_key,
                    'type': 'self_critique',
                },
                context_snapshot=f"Self-critique during session {session_key}",
            )

            return critique
        except Exception as e:
            tool_logger.warning(f"Self_Critique_And_Enhance failed: {e}")
            return f"Self-critique on '{input_text}': Will adjust future behavior based on observations."

    tools.append((
        "Self_Critique_And_Enhance",
        "Review past agent suggestions and user behavior observations to improve "
        "future recommendations. Input: topic or area to critique. Helps the agent "
        "learn from its own interactions.",
        self_critique_and_enhance,
    ))

    # ------------------------------------------------------------------
    # device_control — Cross-device control via PeerLink (SAME_USER only)
    # ------------------------------------------------------------------
    @log_tool_execution
    def device_control(
        action: Annotated[str, "What to do: 'turn on light', 'check temperature', 'list files', 'run command ls -la'"],
        device_hint: Annotated[str, "Which device: 'phone', 'desktop', 'iot hub', or empty for default"] = '',
    ) -> str:
        """Control a device on the user's private network via PeerLink.

        Privacy-first: only targets the user's own devices (SAME_USER trust).
        Uses PeerLink dispatch channel with FleetCommandService fallback.
        """
        try:
            # Step 1: Find the target device via DeviceRoutingService
            target_device = None
            try:
                from integrations.social.models import db_session
                from integrations.social.device_routing_service import DeviceRoutingService
                with db_session(commit=False) as db:
                    if device_hint:
                        # Map hint to capability or form factor
                        capability = 'general'
                        if device_hint.lower() in ('phone', 'desktop', 'tablet', 'tv', 'embedded', 'robot'):
                            # Filter by form factor
                            devices = DeviceRoutingService.get_user_device_map(db, str(user_id))
                            for d in devices:
                                if d.get('form_factor', '') == device_hint.lower():
                                    target_device = d
                                    break
                        if not target_device:
                            target_device = DeviceRoutingService.pick_device(
                                db, str(user_id), required_capability=capability)
                    else:
                        target_device = DeviceRoutingService.pick_device(
                            db, str(user_id), required_capability='general')
            except Exception as e:
                tool_logger.debug(f"Device routing lookup failed: {e}")

            target_node_id = (target_device or {}).get('device_id', '')

            # Step 2: Try PeerLink dispatch channel (SAME_USER trust only)
            peerlink_sent = False
            if target_node_id:
                try:
                    from core.peer_link.link_manager import get_link_manager
                    from core.peer_link.link import TrustLevel
                    mgr = get_link_manager()
                    link = mgr.get_link(target_node_id)
                    if link and link.trust == TrustLevel.SAME_USER:
                        result = mgr.send(
                            target_node_id, 'dispatch',
                            {'type': 'device_control', 'action': action,
                             'user_id': str(user_id)},
                            wait_response=True, timeout=30.0,
                        )
                        if result is not None:
                            peerlink_sent = True
                            msg = result.get('message', str(result))
                            return f"Device control result: {msg}"
                    elif link and link.trust != TrustLevel.SAME_USER:
                        return ("Device control blocked: target device is not a SAME_USER "
                                "trusted device. Only your own devices can be controlled.")
                except Exception as e:
                    tool_logger.debug(f"PeerLink dispatch failed: {e}")

            # Step 3: Fallback to FleetCommandService
            if not peerlink_sent:
                try:
                    from integrations.social.models import db_session
                    from integrations.social.fleet_command import FleetCommandService
                    with db_session() as db:
                        cmd = FleetCommandService.push_command(
                            db, target_node_id or 'self',
                            'device_control',
                            {'action': action, 'device_hint': device_hint},
                        )
                        if cmd:
                            return f"Device control command queued (id={cmd.get('id', '?')}): {action}"
                        return "Device control: failed to queue command"
                except Exception as e:
                    tool_logger.debug(f"Fleet command fallback failed: {e}")

            # Step 4: Local execution as last resort (this IS the target device)
            try:
                from integrations.social.fleet_command import FleetCommandService
                result = FleetCommandService.execute_command(
                    'device_control', {'action': action})
                if result.get('success'):
                    return f"Device control (local): {result.get('message', 'OK')}"
                return f"Device control failed: {result.get('message', 'Unknown error')}"
            except Exception as e:
                return f"Device control unavailable: {e}"

        except Exception as e:
            tool_logger.warning(f"device_control failed: {e}")
            return f"Device control error: {e}"

    tools.append((
        "device_control",
        "Control a device on the user's private network. Actions: turn on/off lights, "
        "check temperature, list files, run commands. Privacy-first: only your own devices.",
        device_control,
    ))

    # ------------------------------------------------------------------
    # data_extraction_from_url — Parity with LangChain Data_Extraction_From_URL
    # ------------------------------------------------------------------
    @log_tool_execution
    def data_extraction_from_url(
        url: Annotated[str, "The URL to extract content from"],
        url_type: Annotated[str, "Type of URL: 'pdf' or 'website'"] = "website",
    ) -> str:
        """Extract content from a URL (PDF or website). Uses Crawl4AI or direct parsing."""
        try:
            from hartos.threadlocal import thread_local_data as _tld
            _uid = _tld.get_user_id() if hasattr(_tld, 'get_user_id') else user_id
            _rid = _tld.get_request_id() if hasattr(_tld, 'get_request_id') else None

            # Try Crawl4AI service first
            try:
                from integrations.service_tools import service_tool_registry
                crawl_tool = service_tool_registry.get_tool('Crawl4AI')
                if crawl_tool:
                    result = crawl_tool.execute(url)
                    if result:
                        return f"Extracted from {url}:\n{str(result)[:5000]}"
            except Exception:
                pass

            # Fallback: direct requests
            import requests as _req
            resp = _req.get(url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
            if url_type == 'pdf':
                return f"PDF downloaded ({len(resp.content)} bytes). Use a PDF parser for full extraction."
            text = resp.text[:5000]
            # Strip HTML tags naively
            import re
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            return f"Extracted from {url}:\n{text[:4000]}"
        except Exception as e:
            return f"URL extraction failed: {e}"

    tools.append((
        "data_extraction_from_url",
        "Extract content from a URL (PDF or website). Input: URL and type ('pdf' or 'website'). "
        "Uses Crawl4AI for rich extraction with fallback to direct HTTP fetch.",
        data_extraction_from_url,
    ))

    # ------------------------------------------------------------------
    # get_user_details — Parity with LangChain User_details_tool
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_user_details() -> str:
        """Get current user's profile details."""
        try:
            uid = user_id
            # Try local DB first
            try:
                from integrations.social.models import get_db, User
                db = get_db()
                try:
                    user = db.query(User).filter_by(id=str(uid)).first()
                    if user:
                        return json.dumps(user.to_dict(), default=str)
                finally:
                    db.close()
            except Exception:
                pass

            # Fallback: cloud API
            import requests as _req
            resp = _req.post(
                'https://azurekong.hertzai.com:8443/db/getstudent_by_user_id',
                json={'user_id': uid}, timeout=10,
            )
            return f"User details: {resp.text}"
        except Exception as e:
            return f"Could not fetch user details: {e}"

    tools.append((
        "get_user_details",
        "Get the current user's profile information (name, email, preferences, etc.). "
        "Use when the user asks about their profile or when you need user context.",
        reads_persisted_state(get_user_details),
    ))

    # ------------------------------------------------------------------
    # request_resource — Parity with LangChain Request_Resource
    # ------------------------------------------------------------------
    @log_tool_execution
    def request_resource(
        resource_description: Annotated[str, "JSON or plain text describing the needed resource. JSON format: {\"resource_type\": \"api_key\", \"key_name\": \"GOOGLE_API_KEY\", \"label\": \"Google API Key\", \"used_by\": \"search tool\", \"description\": \"needed for web search\"}"],
    ) -> str:
        """Request an API key, credential, token, or config value that is not currently available."""
        from hartos.ai_key_vault import request_credential
        return request_credential(resource_description, agent_id=prompt_id)

    tools.append((
        "request_resource",
        "Request an API key, credential, token, or config value. Checks vault/env first, "
        "then prompts the user if not found. Handles: API keys (OpenAI, Google, Slack), "
        "OAuth tokens, channel secrets, service credentials. "
        "Input: JSON with resource_type, key_name, label, used_by, description.",
        request_resource,
    ))

    # ------------------------------------------------------------------
    # observe_user_experience — Parity with LangChain Observe_User_Experience
    # ------------------------------------------------------------------
    @log_tool_execution
    def observe_user_experience(
        event: Annotated[str, "What happened (e.g. 'clicked', 'scrolled', 'left page')"],
        page: Annotated[str, "Which page or screen"] = "",
        outcome: Annotated[str, "What was the result or user reaction"] = "",
        duration_ms: Annotated[int, "How long the interaction lasted in ms"] = 0,
    ) -> str:
        """Record a user experience observation for self-improvement."""
        observation = f"User {event} on {page} ({duration_ms}ms): {outcome}"
        try:
            if memory_graph:
                session_id = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)
                mid = memory_graph.register(
                    content=observation,
                    metadata={'memory_type': 'observation', 'source_agent': 'agent',
                              'session_id': session_id, 'page': page, 'event': event},
                    context_snapshot=f"UX observation during session {session_id}",
                )
                return f"Observation recorded (id: {mid}): {observation}"
        except Exception:
            pass
        return f"Observation noted: {observation}"

    tools.append((
        "observe_user_experience",
        "Record a user experience observation. Use to track behavior patterns "
        "for self-improvement. Input: event, page, outcome, duration_ms.",
        observe_user_experience,
    ))

    # ------------------------------------------------------------------
    # self_critique_and_enhance — Parity with LangChain Self_Critique_And_Enhance
    # ------------------------------------------------------------------
    @log_tool_execution
    def self_critique_and_enhance(
        topic: Annotated[str, "Topic or area to critique (e.g. 'my recommendations', 'search quality')"] = "",
    ) -> str:
        """Review past agent suggestions and user observations to improve future behavior."""
        try:
            if not memory_graph:
                return "Self-critique unavailable: no memory graph for this session."

            suggestions = memory_graph.recall(topic or 'suggestions made outcomes', mode='semantic', top_k=10)
            observations = memory_graph.recall('user experience observation', mode='semantic', top_k=10)

            if not suggestions and not observations:
                return "No past interactions to critique yet. Will observe and learn."

            critique = "Self-critique findings:\n"
            if suggestions:
                critique += f"Past suggestions ({len(suggestions)}):\n"
                for s in suggestions[:5]:
                    critique += f"  - {s.content[:100]}\n"
            if observations:
                critique += f"User observations ({len(observations)}):\n"
                for o in observations[:5]:
                    critique += f"  - {o.content[:100]}\n"

            session_id = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)
            memory_graph.register(
                content=f"Self-critique on: {topic}",
                metadata={'memory_type': 'insight', 'source_agent': 'agent',
                          'session_id': session_id, 'type': 'self_critique'},
                context_snapshot=f"Self-critique during session {session_id}",
            )
            return critique
        except Exception as e:
            return f"Self-critique unavailable: {e}"

    tools.append((
        "self_critique_and_enhance",
        "Review past agent suggestions and user behavior observations to improve "
        "future recommendations. Input: topic or area to critique.",
        self_critique_and_enhance,
    ))

    # ------------------------------------------------------------------
    # Browser Research tools — T3 (no auth, no browser).
    # ------------------------------------------------------------------
    # Single canonical entry point — integrations.browser_research.tools.dispatch.
    # Every invocation logs to web_research_audit.log and surfaces a
    # `connection_mechanism` field so the agent can describe to the user how
    # it accessed the resource (public_http, obscura_b2_cdp_user_chrome, ...).
    #
    # T2 platform tools (Twitter / Reddit / LinkedIn / Bilibili / XHS / Weibo)
    # land via the same dispatcher in C4+ — agent_tools wiring stays here so
    # there is exactly one tool-registration surface for both LangChain and
    # autogen consumers (no parallel registry).
    try:
        from integrations.browser_research import tools as br_tools_module
    except ImportError as _br_imp_err:
        tool_logger.warning("browser_research unavailable, skipping registration: %s",
                            _br_imp_err)
        br_tools_module = None

    if br_tools_module is not None:
        @log_tool_execution
        def YouTube_Transcript(
            url: Annotated[str, "Full YouTube URL (watch / youtu.be / shorts)."],
            language: Annotated[str, "Preferred subtitle language code, e.g. 'en'."] = "en",
        ) -> str:
            """Fetch a YouTube video's transcript text without authentication.

            Returns a JSON string with success/text/segment_count/connection_mechanism.
            Domain-locked to youtube.com / youtu.be by the dispatcher's allowlist.

            Note: this is distinct from `data_extraction_from_url` because YouTube
            transcripts use the youtube_transcript_api endpoint (captions/cc), not
            page-content scraping.  Generic URL fetch belongs in
            data_extraction_from_url (Crawl4AI → web_crawler.py).
            """
            result = br_tools_module.dispatch(
                tool='YouTube_Transcript', user_id=str(user_id),
                url=url, language=language,
            )
            return json.dumps(result, ensure_ascii=False)

        tools.append((
            "YouTube_Transcript",
            "Fetch a YouTube video's transcript text via the captions/cc endpoint. "
            "No login, no API key. Input: full YouTube URL (watch/youtu.be/shorts) "
            "+ optional language code. For generic web pages use "
            "data_extraction_from_url instead — Crawl4AI is the canonical URL fetch.",
            YouTube_Transcript,
        ))

        # T2: cookie-authenticated read on user's logged-in platform sessions.
        # Routes through web_crawler.crawl_url_with_cookies (extending the
        # canonical crawler) with AccountVault cookies + optional CDP attach.
        @log_tool_execution
        def Search_Platform(
            platform: Annotated[str, "twitter / reddit / linkedin / bilibili / xiaohongshu / weibo"],
            query: Annotated[str, "Search keywords/phrase."],
            handle: Annotated[Optional[str], "Vault handle whose cookies to use; first if omitted."] = None,
        ) -> str:
            """Search a platform using the user's logged-in session cookies.

            Returns JSON with success/markdown/connection_mechanism — the
            connection_mechanism tells the agent (and user) how access happened
            (obscura_b2_cdp_user_chrome / obscura_b1_headless_profile).
            Consent-gated on `web_research:<platform>`.
            """
            result = br_tools_module.dispatch(
                tool='Search_Platform', user_id=str(user_id),
                platform=platform, query=query, handle=handle,
            )
            return json.dumps(result, ensure_ascii=False)

        tools.append((
            "Search_Platform",
            "Search a social platform (twitter/reddit/linkedin/bilibili/xiaohongshu/weibo) "
            "using the user's logged-in session. Input: platform, query, optional handle. "
            "Returns JSON with markdown content + connection_mechanism describing how the "
            "agent accessed it (your Chrome, headless profile, or public). Consent-gated.",
            Search_Platform,
        ))

        @log_tool_execution
        def Read_Timeline(
            platform: Annotated[str, "twitter / reddit / linkedin / bilibili / xiaohongshu / weibo"],
            target_handle: Annotated[str, "Whose timeline to read (e.g. '@elonmusk')."],
            handle: Annotated[Optional[str], "Vault handle whose cookies to use; first if omitted."] = None,
        ) -> str:
            """Read another user's public timeline via your logged-in browser session."""
            result = br_tools_module.dispatch(
                tool='Read_Timeline', user_id=str(user_id),
                platform=platform, target_handle=target_handle, handle=handle,
            )
            return json.dumps(result, ensure_ascii=False)

        tools.append((
            "Read_Timeline",
            "Read someone's public timeline on a platform via your logged-in session. "
            "Input: platform, target_handle. Returns markdown + connection_mechanism. "
            "Consent-gated on web_research:<platform>.",
            Read_Timeline,
        ))

        @log_tool_execution
        def Post_As_User(
            platform: Annotated[str, "twitter / reddit / linkedin / bilibili / xiaohongshu / weibo"],
            content: Annotated[str, "Text to post on the user's behalf."],
            handle: Annotated[Optional[str], "Vault handle to post as; first if omitted."] = None,
            dry_run: Annotated[bool, "TRUE returns a preview card; FALSE actually posts (requires prior confirm)."] = True,
        ) -> str:
            """Post on the user's behalf on a social platform.

            **Preview-confirm gate**: dry_run defaults to TRUE.  First invocation
            returns a `liquid_ui: post_preview` component for the UI to render
            with explicit Cancel/Confirm buttons.  The agent MUST re-invoke with
            dry_run=False after the user taps Confirm.  Same canonical pattern
            as Invite_Friend and channel_send.

            Consent-gated on `web_research:<platform>`.
            """
            result = br_tools_module.dispatch(
                tool='Post_As_User', user_id=str(user_id),
                platform=platform, content=content, handle=handle, dry_run=dry_run,
            )
            return json.dumps(result, ensure_ascii=False)

        tools.append((
            "Post_As_User",
            "Post on the user's behalf on a social platform via their logged-in "
            "browser session. PREVIEW-CONFIRM gated: dry_run=True (default) returns "
            "a preview card the user must explicitly confirm before the actual post "
            "happens with dry_run=False. Consent-gated on web_research:<platform>. "
            "Input: platform, content, optional handle, dry_run.",
            Post_As_User,
        ))

        # NOTE: Read_Webpage was removed 2026-06-08 as a parallel-path violation.
        # The canonical "fetch a URL's content" tool is `data_extraction_from_url`
        # above (line ~1008), which already delegates to Crawl4AI →
        # integrations/web_crawler.py (Playwright/Chromium headless with a
        # requests+BeautifulSoup fallback).  T2 (cookie-authenticated) work in
        # commits C4+ EXTENDS web_crawler.py with cookie injection + B2 CDP
        # attach, instead of building a parallel driver.  See
        # memory/project_browser_research_subsystem.md for the corrected plan.

    @log_tool_execution
    def validate_json_response(response: Annotated[str, "The response from a tool that should be JSON"]) -> str:
        """
        Validates and repairs JSON response from tools.

        Args:
            response: string responses from a tool that should be JSON formatted
        Returns:
            Valid JSON string or the original string if not repairable
        """
        tool_logger.info("INSIDE validate json response")
        try:
            # First try to parse as is
            json_obj = json.loads(response)
            return json.dumps(json_obj)
        except json.JSONDecodeError:
            try:

                # If parsing fails, try to repair
                repaired_json = repair_json(response)
                # Verify the repaired JSON is valid
                json_obj = json.loads(repaired_json)
                return json.dumps(json_obj)
            except Exception as e:
                # If repair filas, return the original with a warning
                tool_logger.info("JSON repair has failed")
                return f"{response}"

    tools.append((
        "validate_json_response",
        "Checks and corrects if the tool response is not JSON but expected to be.",
        validate_json_response,
    ))

    # ------------------------------------------------------------------
    # Coding-agent leg
    #
    # These four were inline closures in create_recipe.create_agents
    # (register_dual, L1674-1816) until 2026-09-10.  CREATE advertised them
    # to the recipe-authoring LLM while REUSE — which builds its tools from
    # THIS factory (reuse_recipe.py:2238) — held no copy.  Two consequences,
    # and the second is the one that hid the first:
    #   * a saved action naming one could never execute; and
    #   * _reuse_fabricated_tools could not see the name as `referenced`
    #     (that helper intersects the action text with names REGISTERED ON
    #     THE AGENTS), so it returned [] at its second early-return, before
    #     its log line.  A tool the leg cannot run was indistinguishable
    #     from an action naming no tool, and the action advanced silently.
    # Measured live 2026-09-10, agent 88719487304 action 4: FAB-GUARD
    # watermark 23 in, 23 out — zero tool calls in the whole window — no
    # verdict line at all, both subtasks closed, parent terminated in 14s.
    # 36 of the 185 saved recipes on that box name such a tool; 87 actions
    # name execute_coding_task alone.
    #
    # Deliberately NOT added to MAIN_LEG_CORE_TOOLS: reuse reaches them via
    # attach_for_names, for the action whose own recipe names one, so the
    # always-on schema stays 18 tools / ~1,859 tokens against the 12,288
    # slot (#730).  Same rule reuse_recipe.py:2429-2442 already states for
    # execute_windows_or_android_command — that closure captures 33 locals
    # of its defining function so ctx cannot build it and it is handed over
    # inline; these four capture nothing but user_id, which ctx supplies.
    # ------------------------------------------------------------------
    async def execute_coding_task(
        task: Annotated[str, "The coding task to execute (e.g., 'review this function for bugs', 'implement a login form')"],
        task_type: Annotated[str, "Task type: code_review, feature, bug_fix, refactor, app_build, debugging, multi_session"] = "feature",
        preferred_tool: Annotated[str, "Optional tool override: kilocode, claude_code, opencode, aider_native, or claw_native (empty = auto-select best)"] = "",
        working_dir: Annotated[str, "Working directory / repo path for the coding task (empty = use HEVOLVE_CODING_WORKDIR env or cwd)"] = "",
    ) -> str:
        """Execute a coding task using the best available coding agent tool (KiloCode, Claude Code, OpenCode, or AiderNative).

        Routes to the best tool based on benchmarks and task type.
        This is for writing, reviewing, refactoring, or debugging code —
        NOT for GUI automation (use execute_windows_or_android_command for that).
        """
        try:
            from integrations.coding_agent.orchestrator import get_coding_orchestrator
            orchestrator = get_coding_orchestrator()
            result = orchestrator.execute(
                task=task,
                task_type=task_type,
                preferred_tool=preferred_tool,
                user_id=user_id,
                model=os.environ.get('HEVOLVE_CODING_MODEL', ''),
                working_dir=working_dir or os.environ.get('HEVOLVE_CODING_WORKDIR', ''),
            )
            return json.dumps(result, indent=2)
        except Exception as e:
            tool_logger.exception("execute_coding_task failed: %s", e)
            return f"Coding task execution error: {e}"

    tools.append((
        "execute_coding_task",
        "Execute a coding task (write, review, refactor, debug code) using the best available coding agent tool. Routes to KiloCode, Claude Code, OpenCode, AiderNative, or ClawNative (Rust) based on benchmarks. Pass working_dir for the target repo path.",
        execute_coding_task,
    ))

    # Repository map tool — tree-sitter based code understanding.
    # Import-gated exactly as create_recipe had it: absent, not broken,
    # when aider_core is not installed.
    try:
        from integrations.coding_agent.recipe_bridge import CodingRecipeBridge

        async def get_repository_map(
            working_dir: Annotated[str, "Directory to map (default: current directory)"] = ".",
            max_tokens: Annotated[int, "Maximum tokens for the map output"] = 2048,
        ) -> str:
            """Generate a tree-sitter based repository map showing key functions, classes, and their relationships.

            Use this to understand a codebase's structure before making changes.
            Returns a ranked summary of the most important code symbols.
            """
            return CodingRecipeBridge.get_repository_map(working_dir, max_tokens)

        tools.append((
            "get_repository_map",
            "Generate a tree-sitter repository map showing key functions, classes, and structure. Use before coding tasks to understand the codebase.",
            get_repository_map,
        ))
    except ImportError:
        tool_logger.debug("Repository map tool not available (aider_core not installed)")

    # Shard Engine: Call-chain context for coding tasks.
    # Target function + upstream callers + downstream callees = FULL source.
    # Everything else = interfaces only. Exposure proportional to task.
    # Call graph from Trueflow MCP (IDE) or AST fallback (headless).
    try:
        async def create_code_shard(
            task: Annotated[str, "Description of the coding task"],
            target_file: Annotated[str, "Relative path to the file containing the target function"],
            target_function: Annotated[str, "Name of the function to modify"],
            repo_path: Annotated[str, "Path to the repository (default: HART OS install dir)"] = "",
        ) -> str:
            """Create a code shard with call-chain context for a coding task.

            Returns:
            - Target function: FULL source (what you're modifying)
            - Upstream callers: FULL source (who calls it, input contracts)
            - Downstream callees: FULL source (what it calls, output contracts)
            - Everything else: Interfaces only (signatures + types)

            Call graph sourced from Trueflow MCP (when IDE running) or AST fallback.
            Security: exposure proportional to the task. E2E encrypted for peer offload.
            Use execute_coding_task with working_dir to actually apply edits.
            """
            from integrations.agent_engine.shard_engine import ShardEngine
            engine = ShardEngine(code_root=repo_path) if repo_path else ShardEngine()
            shard = engine.create_call_chain_shard(
                task=task, target_file=target_file,
                target_function=target_function)
            return json.dumps({
                'shard_id': shard.shard_id,
                'task': shard.task_description,
                'scope': shard.scope.value,
                'target_files': shard.target_files,
                'call_chain_source': shard.full_content,
                'interfaces': [{'file': s.file_path, 'functions': s.functions,
                               'classes': s.classes} for s in shard.interface_specs],
            }, indent=2, default=str)

        tools.append((
            "create_code_shard",
            "Create a code shard with call-chain context: target function + upstream callers + downstream callees (FULL source), everything else interfaces only.",
            create_code_shard,
        ))
    except Exception:
        tool_logger.debug("Shard engine tool not available")

    # Benchmark Tracker: Query which coding tool performs best for each task type
    try:
        async def get_coding_benchmarks(
            task_type: Annotated[str, "Task type to check (code_review, feature, bug_fix, refactor, app_build, debugging, multi_session, or 'all')"] = "all",
        ) -> str:
            """Get coding tool benchmarks — which tool (KiloCode, Claude Code, OpenCode, AiderNative) performs best.

            Returns success rates, average times, and sample counts per tool per task type.
            Includes both local benchmarks and hive-aggregated intelligence from peers.
            """
            from integrations.coding_agent.benchmark_tracker import get_benchmark_tracker
            tracker = get_benchmark_tracker()
            result = {'local': {}, 'hive': {}}

            if task_type == 'all':
                delta = tracker.export_learning_delta()
                result['local'] = delta.get('coding_benchmarks', {})
            else:
                best = tracker.get_best_tool(task_type)
                if best:
                    result['local'][task_type] = {
                        'best_tool': best[0], 'success_rate': best[1],
                        'avg_time_s': best[2],
                    }
                hive_best = tracker.get_hive_best_tool(task_type)
                if hive_best:
                    result['hive'][task_type] = {
                        'best_tool': hive_best[0], 'success_rate': hive_best[1],
                        'avg_time_s': hive_best[2],
                    }
            return json.dumps(result, indent=2, default=str)

        tools.append((
            "get_coding_benchmarks",
            "Query coding tool benchmarks — which tool performs best per task type. Includes local and hive-aggregated data.",
            get_coding_benchmarks,
        ))
    except Exception:
        tool_logger.debug("Benchmark tracker tool not available")

    # ------------------------------------------------------------------
    # Book / learning navigation — appended HERE, not via a separate
    # register_*_if_available() registrar.
    #
    # register_remote_desktop_tools_if_available (below) has NO production
    # caller — only tests/unit/test_remote_desktop_agent_tools.py:235 — so a
    # tool registered that way never reaches a live turn.  The live path is
    # build_core_tool_closures() -> register_core_tools(), called from
    # create_recipe.py:1096/1116 and reuse_recipe.py:2238/2264.  Appending to
    # `tools` is therefore the only wiring that actually runs.
    # ------------------------------------------------------------------
    try:
        from integrations.learning.book_tools import build_book_tools
        _book = build_book_tools(ctx)
        if _book:
            tools.extend(_book)
            tool_logger.info("Book navigation tools registered (%d)", len(_book))
    except ImportError:
        pass
    except Exception as e:
        tool_logger.warning("Book tools registration failed: %s", e)

    return tools


def register_remote_desktop_tools_if_available(ctx, helper, executor):
    """Register remote desktop tools if the module is available.

    Gracefully skips if integrations/remote_desktop is not installed.
    """
    try:
        from integrations.remote_desktop.agent_tools import (
            build_remote_desktop_tools, register_remote_desktop_tools,
        )
        rd_tools = build_remote_desktop_tools(ctx)
        register_remote_desktop_tools(rd_tools, helper, executor)
        tool_logger.info(f"Remote desktop tools registered ({len(rd_tools)} tools)")
    except ImportError:
        pass
    except Exception as e:
        tool_logger.warning(f"Remote desktop tools registration failed: {e}")


def register_memory_graph_tools(memory_graph, helper, executor, user_id, user_prompt):
    """Register MemoryGraph provenance tools if memory_graph is available.

    Delegates to the existing agent_memory_tools module.
    """
    if memory_graph is None:
        return
    try:
        from integrations.channels.memory.agent_memory_tools import create_memory_tools, register_autogen_tools
        mem_tools = create_memory_tools(memory_graph, str(user_id), user_prompt)
        register_autogen_tools(mem_tools, executor, helper)
        tool_logger.info(f"MemoryGraph tools registered for {user_prompt}")
    except Exception as e:
        tool_logger.warning(f"MemoryGraph tools registration failed: {e}")
