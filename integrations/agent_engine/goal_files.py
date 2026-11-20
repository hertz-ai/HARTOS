"""The files a goal's own turn may open: those in the folder its config
names (config ``files_root``), read-only.

The owner's rule: HARTOS works like a person who opens the file they were
told to use.  Central's lane goals carry files_root '/app/agent_data/lanes'
and tell the agent "Facts only from EMAIL_FACTS.md and this goal"; goal
61e48abc's step "get_user_uploaded_file: load EMAIL_FACTS.md" looped and
paused needs_help on 2026-11-20 because no tool read a file from disk
(tests/unit/test_a_goal_reads_the_files_its_goal_names.py).

get_user_uploaded_file (core.agent_tools) reads through here when its
``name`` is given on a goal's turn, so the agent opens a goal's file with
the tool its plan already names for "load a file".  Who may:

  * only this node's own goal turn (core.chat_client.own_turn_goal_id): any
    client can name daemon_<goal id> in its /chat body;
  * only a goal made by an account this node holds (the owner or its system
    account, ThoughtExperimentService.node_held_ids, the rule
    outreach_crm_tools._may_use_mailbox reads): any signed-in account writes
    a goal's config through POST /api/goals, so a stranger's files_root '/'
    would otherwise open the node's own secrets;
  * only inside files_root after symlinks resolve (core.platform_paths
    .is_within: a sibling folder sharing the prefix is not inside).

Every refusal is a core.tool_refusal refusal saying what was wrong and what
to do; the ones no retry can fix let the step finish without the file.
"""
import logging
import os
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

#: The goal config key naming the folder a goal's files are in.
FILES_ROOT_KEY = 'files_root'

#: At most this many characters of a file are read: lane files are a few
#: KB, and a page of it goes to the model at a time (core.agent_tools).
MAX_READ_CHARS = 1_000_000

#: At most this many names are listed in a refusal.
_MAX_LISTED = 40


class GoalFile(NamedTuple):
    """What a goal's turn got for a file: its ``text``, or the ``refusal``."""
    text: Optional[str]
    refusal: Optional[str]


def _goal_record(goal_id):
    """``(title, config, creator_is_node_held)`` of goal ``goal_id``, or None
    when the goal is not on this node.  Raises when the DB cannot be read;
    the caller refuses then (fails closed)."""
    from integrations.social.models import AgentGoal, db_session
    from integrations.social.thought_experiment_service import (
        ThoughtExperimentService)
    with db_session(commit=False) as db:
        goal = db.query(AgentGoal).filter_by(id=str(goal_id)).first()
        if goal is None:
            return None
        creator = str(goal.created_by) if goal.created_by else ''
        held = bool(creator) and bool(
            ThoughtExperimentService.node_held_ids(db, [creator]))
        config = goal.config_json if isinstance(goal.config_json, dict) else {}
        return goal.title or '', dict(config), held


def _refuse(tool, problem, fix, retry):
    from core.tool_refusal import refuse
    return GoalFile(None, refuse(tool, problem, fix, retry=retry))


def _finish_without(tool, problem, fix="Carry on with the goal's own text "
                                       "instead of this file"):
    from core.tool_refusal import RETRY_FINISH_WITHOUT
    return _refuse(tool, problem, fix, RETRY_FINISH_WITHOUT)


def _shown(root, path):
    """``path`` as the agent passes it: relative to ``root``, with '/'."""
    rel = os.path.relpath(path, root)
    return '' if rel == '.' else rel.replace(os.sep, '/')


def _listing(root, folder):
    """The names in ``folder`` (inside ``root``) as the agent can pass them,
    a folder marked with a trailing '/', at most _MAX_LISTED of them."""
    try:
        entries = sorted(os.listdir(folder))
    except OSError:
        return ''
    names = []
    for entry in entries[:_MAX_LISTED]:
        full = os.path.join(folder, entry)
        names.append(_shown(root, full) + ('/' if os.path.isdir(full) else ''))
    more = ', ...' if len(entries) > _MAX_LISTED else ''
    return ', '.join(names) + more


def _nearest_folder(root, target):
    """The deepest existing folder at or above ``target`` inside ``root``."""
    from core.platform_paths import is_within
    folder = os.path.dirname(target)
    while is_within(folder, root) and not os.path.isdir(folder):
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return folder if is_within(folder, root) and os.path.isdir(folder) else root


def _holds(root, folder):
    where = _shown(root, folder)
    listed = _listing(root, folder) or 'nothing'
    return (f"the folder {where} holds: {listed}" if where
            else f"the goal's folder holds: {listed}")


def _turn_goal_folder(tool):
    """``(root, None)``: the real path of the folder the running goal turn's
    goal names; ``(None, refusal)`` when it names none that may be opened;
    None when the running turn is not this node's own goal turn."""
    from core.chat_client import own_turn_goal_id
    goal_id = own_turn_goal_id()
    if not goal_id:
        return None
    try:
        record = _goal_record(goal_id)
    except Exception as e:
        logger.warning("goal %s unreadable, its files are not opened: %s",
                       goal_id, type(e).__name__)
        return None, _finish_without(
            tool, f"the goal this turn works for could not be read "
                  f"({type(e).__name__})").refusal
    if record is None:
        return None
    title, config, held = record
    name = (title or goal_id)[:80]
    if not held:
        return None, _finish_without(
            tool, f"goal '{name}' was made by an account this node does not "
                  f"hold, so its config cannot open files on this node",
            "Carry on with the goal's own text instead of this file; the "
            "node's owner can make the goal under their own account to give "
            "it files").refusal
    raw = config.get(FILES_ROOT_KEY)
    raw = raw.strip() if isinstance(raw, str) else ''
    if not raw:
        return None, _finish_without(
            tool, f"goal '{name}' names no folder: its config has no "
                  f"{FILES_ROOT_KEY}",
            "Carry on with the goal's own text instead of this file; the "
            f"owner adds \"{FILES_ROOT_KEY}\" (the folder the goal's files "
            "are in) to the goal's config to give it files").refusal
    if not os.path.isabs(raw):
        return None, _finish_without(
            tool, f"goal '{name}' has {FILES_ROOT_KEY} {raw!r}, which is not "
                  f"an absolute folder",
            "Carry on with the goal's own text instead of this file; the "
            f"owner sets {FILES_ROOT_KEY} in the goal's config to the "
            "folder's absolute path").refusal
    root = os.path.realpath(raw)
    if not os.path.isdir(root):
        return None, _finish_without(
            tool, f"goal '{name}' names the folder {raw!r} in "
                  f"{FILES_ROOT_KEY}, and there is no such folder on this node",
            "Carry on with the goal's own text instead of this file; the "
            "owner copies the goal's files to that folder").refusal
    return root, None


def goal_folder_listing(tool) -> Optional[str]:
    """What ``tool`` answers on this node's own goal turn when it was given
    no file name and the goal names a folder: a refusal naming the files in
    it, to be called again with one.  None in any other case (the caller
    answers as before)."""
    got = _turn_goal_folder(tool)
    if got is None or got[0] is None:
        return None
    from core.tool_refusal import RETRY_AFTER_FIX
    root = got[0]
    return _refuse(tool, f"no file name was given; {_holds(root, root)}",
                   "Pass name: the file the goal names, relative to its "
                   "folder, such as EMAIL_FACTS.md", RETRY_AFTER_FIX).refusal


def read_goal_file(tool, name) -> Optional[GoalFile]:
    """File ``name`` of the running goal turn's own goal, read-only, as
    ``tool`` answers it; None when the running turn is not this node's own
    goal turn (the caller answers as before).  ``name`` is relative to the
    goal's files_root, or an absolute path inside it."""
    got = _turn_goal_folder(tool)
    if got is None:
        return None
    root, refusal = got
    if refusal is not None:
        return GoalFile(None, refusal)
    from core.platform_paths import is_within
    from core.tool_refusal import RETRY_AFTER_FIX
    asked = str(name or '').strip()
    target = os.path.realpath(os.path.join(root, asked))
    if not is_within(target, root):
        return _refuse(
            tool, f"{asked} is outside the goal's folder ({FILES_ROOT_KEY}); "
                  f"{_holds(root, root)}",
            "Pass a file inside the goal's folder, relative to it, such as "
            "EMAIL_FACTS.md", RETRY_AFTER_FIX)
    if os.path.isdir(target):
        return _refuse(tool, f"{asked} is a folder, not a file; "
                             f"{_holds(root, target)}",
                       "Pass one of the files in it by that name",
                       RETRY_AFTER_FIX)
    if not os.path.isfile(target):
        return _finish_without(
            tool, f"there is no file {asked} in the goal's folder; "
                  f"{_holds(root, _nearest_folder(root, target))}",
            "Carry on with the goal's own text instead of this file (the "
            "names above are the files there)")
    try:
        with open(target, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read(MAX_READ_CHARS)
    except OSError as e:
        return _finish_without(tool, f"{asked} could not be read "
                                     f"({type(e).__name__})")
    if not text.strip():
        # An empty answer reads as nothing happened, and is called again.
        return _finish_without(tool, f"the file {asked} is empty")
    return GoalFile(text, None)
