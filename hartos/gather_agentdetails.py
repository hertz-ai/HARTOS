# PEP 563: annotations are stored as strings and never evaluated at def-time.
# REQUIRED here, not cosmetic. This module annotates with `autogen.AssistantAgent`
# (create_agents_for_user, get_agent_response). Python evaluates annotations when
# the `def` executes — i.e. at module import — so on a node without autogen the
# module died with
#     AttributeError: 'NoneType' object has no attribute 'AssistantAgent'
# before reaching ANY function body. The `if autogen is None: raise ImportError`
# guard below sat underneath the statement that killed it and could never run.
# Observed 300x in the 2026-08-08 nightly VMs, in every nixosTests shard.
from __future__ import annotations

from typing import Any, Dict, Tuple
import os
from flask import current_app

from core.optional_import import lazy_module

# ONE canonical way to bind an optional heavy dependency — the same helper
# create_recipe.py:26 uses. The previous `try: import autogen / except:
# autogen = None` was a second, ad-hoc mechanism for the same concern, and it
# is the one that broke: a None sentinel needs a None check at EVERY use site,
# including annotations, and one was missed. lazy_module has no sentinel to
# miss — it defers the import to first attribute access and raises loudly then,
# which is also what keeps autogen's ~12s import (google.api_core + flaml +
# llmlingua -> torch) off the backend boot path.  Its hook is every autogen
# proxy's (hartos.helper.on_autogen_import): the plan writer below runs code
# blocks, and it is handed the person's own /chat words, so its blocks go
# through the gated command path even when no CREATE or REUSE seat has been
# built yet (review of 0954bf864: it ran one with no consent, policy or audit).
from hartos.helper import on_autogen_import  # noqa: E402
autogen = lazy_module("autogen", on_import=on_autogen_import)

from hartos.helper import retrieve_json, retrieve_json, _is_terminate_msg
from hartos.cultural_wisdom import get_cultural_prompt
from core.platform_paths import get_coding_workspace_dir

# Feature flag — when set, append a self-explanatory tool-name catalog to the
# autonomous gather prompt so the LLM picks REAL tool names instead of
# inventing ones like "web_search" / "WebQueryTool" / "data_parse" (seen in
# the 2026-05-12 IPL refusal forensic).  Default off to keep current behavior
# untouched; flip via `HEVOLVE_AUTONOMOUS_GATHER_TOOL_MAP=1` once verified.
#
# Keep this catalog in sync with:
#   - core/agent_tools.py:build_core_tool_closures (the 28 core tools)
#   - reuse_recipe.py:2335-2354 service_tools block (crawl4ai, acestep, ...)
#   - create_recipe.py service_tools block (synced from reuse, same set)
AUTONOMOUS_TOOL_CATALOG = """
AVAILABLE TOOLS — the agent you create will only have access to these tools.
DO NOT invent new tool names. When an action needs a tool, use one of these EXACT
names so the recipe builder can wire it up downstream.

Web & data:
  - crawl4ai                    fetch a URL, return clean markdown (web scraping)
  - google_search               web/Google search for a query
  - data_extraction_from_url    extract structured data from a URL
  - get_text_from_image         OCR / Q&A on an image URL

Memory:
  - save_data_in_memory         store JSON value at a dot-notation key
  - get_data_by_key             read back a stored value
  - get_saved_metadata          list stored keys (no values)
  - save_to_long_term_memory    persist a fact across sessions
  - search_long_term_memory     semantic search over saved facts and chats
  - get_chat_history            chat history for a date range
  - search_visual_history       search past camera/screen descriptions
  - send_message_to_roles       ask another person's agent, answered from its notes (delivers no message)

Media generation:
  - text_2_image                generate an image from a text prompt
  - Generate_video              generate a talking-head video
  - send_presynthesized_video_to_user   deliver a previously generated video

User communication:
  - send_message_to_user        message the user immediately
  - send_message_in_seconds     message the user after a delay

Scheduling:
  - create_scheduled_jobs       schedule a recurring/cron job

Computer & device use:
  - execute_windows_or_android_command   drive desktop/phone via GUI automation
  - device_control              control a connected device by named action

Coding:
  - execute_coding_task         write/review/refactor/debug code
  - get_repository_map          tree-sitter map of a code repo
  - create_code_shard           call-chain context for a function

Identity & profile:
  - get_user_id, get_prompt_id, get_user_details
  - get_user_uploaded_file, get_user_camera_inp

Self-improvement:
  - consult_expert              get specialized guidance from a domain expert
  - request_resource            request additional resources from the hive
  - Suggest_Share_Worthy_Content, Observe_User_Experience, Self_Critique_And_Enhance
  - validate_json_response      validate/repair a JSON response

Quick mapping (use these defaults when the user describes intent in plain English):
  - "what did we discuss / recall / N days ago / earlier"  -> get_chat_history
  - "find what I saved / search my notes or past chats"    -> search_long_term_memory
  - "fetch a webpage / scrape a site"        -> crawl4ai
  - "search the internet / look up X online" -> google_search
  - "read text from an image / picture"      -> get_text_from_image
  - "schedule X / remind me at Y"            -> create_scheduled_jobs
  - "open an app / click / type / navigate a page (no tool fits)" -> execute_windows_or_android_command
  - "write/refactor/debug code"              -> execute_coding_task
"""


def select_autonomous_tool_catalog() -> str:
    """Tool catalog injected into the autonomous gather/plan prompt.

    Returned by default so the plan author KNOWS the tools at its disposal and
    plans with the right one per step (recall -> get_chat_history, web search ->
    google_search, code -> execute_coding_task) instead of forcing every step
    into GUI automation. Set HEVOLVE_AUTONOMOUS_GATHER_TOOL_MAP=0 to opt out
    (e.g. to A/B the prompt-byte / latency cost).
    """
    if os.environ.get('HEVOLVE_AUTONOMOUS_GATHER_TOOL_MAP', '').strip().lower() in ('0', 'false', 'no', 'off'):
        return ""
    return AUTONOMOUS_TOOL_CATALOG


#: Every plan step names the way it is done, so CREATE runs it and its recipe
#: replays it.  Owner 2026-11-04: "if there is a will there is way should be
#: the mantra for the agents"; "a tool could be a online github repo, a open
#: source software like ffmpeg etc, it could be anything but shd find the
#: recipe for reproducability".  Measured that day: Reel Studio (342) was
#: planned with no tool shown, got 9 steps no tool does one at a time, and
#: CREATE stalled on step 2 asking her for clips it already had.
#: No braces: the autonomous system message is an f-string.
PLAN_WAYS_RULE = """
EVERY STEP NAMES ITS WAY - if there is a will there is a way. Write each action as
the tool that does it, from the tools above (e.g. "get_chat_history: the last 15
days"). When no listed
tool does a step, name the way you know: an open-source program or library (ffmpeg,
OpenCV, a GitHub project) found with google_search or crawl4ai and run with
execute_coding_task, so the commands that worked are saved and replayed. Use as few
steps as the tools need. Never write a step with no way, and never ask for something
already given or that a tool can get.
"""


class _SchemaSeat:
    """Stands in for both agents a Tier-2 family registers on, and keeps
    (name, first paragraph of the description, on one line) of each tool it
    puts on the LLM schema.  Nothing is executed: autogen.register_function
    only calls register_for_llm and register_for_execution."""

    def __init__(self):
        self.tools = []

    def register_for_llm(self, name=None, description=None, **_):
        def keep(func):
            text = (description or func.__doc__ or '').strip()
            summary = ' '.join(text.split('\n\n')[0].split())
            self.tools.append((name or func.__name__, summary or name or func.__name__))
            return func
        return keep

    def register_for_execution(self, name=None, **_):
        return lambda func: func


def _registered_tools(register) -> list:
    """(name, description) of each tool a Tier-2 family's register function
    puts on an agent (register(helper, assistant, user_id)), read from that
    registration itself, so the plan writer's lines cannot drift from the
    schema the agent is built with."""
    seat = _SchemaSeat()
    register(seat, seat, '(described for the plan writer)')
    return seat.tools


def family_tool_catalog(goal_tags) -> str:
    """The tools the agent's own family adds, by its goal tags, for the plan
    writer: what the agent will be built with, read from the family's own
    registration (one source), and only where the one tag rule keeps the tag
    (marketing_tools.resolve_goal_tags; reel_studio is inert on central).

    'outreach' and 'sales' are the CRM and journey families the CREATE
    builder registers for those tags (create_recipe's Tier-2 block).  A
    daemon goal turn's tags come from the one rule its builders use
    (goal_manager.daemon_turn_stored_tags, asked by
    _autonomous_gather_info).  Measured on central 2026-11-20, goal
    5ba50f98: its plan writer was shown neither family, so the plan sent
    its outreach steps to execute_coding_task."""
    from integrations.agent_engine.marketing_tools import REEL_STUDIO_TAG, resolve_goal_tags
    tags = resolve_goal_tags(list(goal_tags or ()), '') if goal_tags else []
    lines = []
    if REEL_STUDIO_TAG in tags:
        from integrations.reel_studio.tools import describe_reel_tools
        lines += describe_reel_tools()
    if 'outreach' in tags:
        from integrations.agent_engine.outreach_crm_tools import register_outreach_tools
        lines += _registered_tools(register_outreach_tools)
    if 'sales' in tags:
        from integrations.agent_engine.journey_engine import register_journey_tools
        lines += _registered_tools(register_journey_tools)
    if not lines:
        return ''
    return ("\nTHIS AGENT'S OWN TOOLS (its kind of agent also gets these):\n"
            + ''.join('  - %-22s %s\n' % (name, desc) for name, desc in lines))


def plan_writer_tools(goal_tags=()) -> str:
    """What every plan writer is shown about doing the work: the tool list
    (select_autonomous_tool_catalog, its opt-out kept), the agent's family
    tools, and PLAN_WAYS_RULE."""
    return select_autonomous_tool_catalog() + family_tool_catalog(goal_tags) + PLAN_WAYS_RULE


# Store user-specific agents & their chat history
user_agents: Dict[str, Tuple[Any, Any]] = {}
#: The writer's group in the turn registry (hartos.helper.TURNS_IN_FLIGHT),
#: beside create_recipe's 'create' and reuse_recipe's 'reuse' groups.
_GATHER_GROUP = 'gather'


def _writer_wait_s() -> float:
    """How long a gather waits for another running turn of its writer before
    it answers the hold (held_for_another_turn_reply) instead.

    The default is the median local model call on this node, 2.3 s: the
    p50 of 24,384 calls in dispatch.py's table (above _LOCAL_LLM_WAIT_S).  A
    turn behind an ordinary call is waited for; a person behind a long one
    is told to send again after 2.3 s, not queued for a call that may run
    600 s (core.http_pool's read budget).  The recent-window p50 (5.4 s) is
    not used: that table says it is inflated by a runaway loop.  1.0 s
    (0ad43fef4) held a turn behind most ordinary calls (review of
    0ad43fef4).  HEVOLVE_GATHER_WRITER_WAIT_S overrides it on a node whose
    model is faster or slower."""
    raw = os.environ.get('HEVOLVE_GATHER_WRITER_WAIT_S', '2.3')
    try:
        return max(0.0, float(raw))
    except ValueError:
        import logging
        logging.getLogger(__name__).warning(
            'HEVOLVE_GATHER_WRITER_WAIT_S=%r is not a number of seconds; '
            'the plan writer waits the default 2.3 s', raw)
        return 2.3


WRITER_WAIT_S = _writer_wait_s()

AGENT_CREATOR_SYSTEM_MESSAGE = """You are a custom agent bot creator. Your task is to interact with the user to gather all the necessary details to create an agent. Once you have collected all the required information, you will generate a complete agent configuration.
        Your role is to assist in a co-creative manner. You should actively suggest actions or improvements, but always confirm with the user before implementing them. Ensure that any actions or suggestions are realistic, humanly possible & ethical. Avoid proposing anything beyond practical feasibility, such as tasks like taking the user to the moon. Your primary goal is to enhance collaboration while adhering to these boundaries.
        Speak in a casual, playful, and respectful tone, keeping it natural, funny, colloquial, and relatable. Expressions should be clear, accurate, grammatically, and contextually correct, avoiding tense confusion. Switch to a more formal tone only if the user keeps it formal.
""" + get_cultural_prompt() + """
        ## Information Collection:
        You need to collect the following details from the user:
        { "name": "The name of the agent", "agent_name": "A unique 2-word dot-separated lowercase identifier like swift.falcon or calm.oracle (adjective.noun pattern)", "goal": "The ultimate goal of the agent", "broadcast_agent": "yes/no", "personas": [ { "name": "The role of the persona", "description": "A description of what this persona can do" } ], "flows": [ { "flow_name": "", "persona": "Each persona will have a separate flow", "actions": ["String array with actions (including tool usage) to perform to reach the sub-goal for this flow"], "sub_goal": "The goal for this flow" } ], "extra_information": "Additional notes or relevant information" }
        IMPORTANT: The "agent_name" field uses a 3-part format: skill.region.name
        - First word: the primary skill/capability (e.g., code, design, research, teach, write, data, market, health, game, art, ops, guard, lead, ally)
        - Second word: the HARTOS region the owner belongs to (default: "local" for local-first users)
        - Third word: a personal name the user chooses for their agent (like naming a pet or companion)
        Examples: code.local.aria, research.central.scout, design.local.muse, teach.local.sage
        Ask the user what they'd like to name their agent (the personal name part). The skill prefix is auto-detected from the agent's goal. If the user doesn't have a preference, suggest a creative name. All lowercase, dot-separated.

        ## Guidelines for Responses:
        1.Information Gathering Process
            For flows, first ask the user for the number of flows, then collect each flow's details step by step.
            Ask for flow_name, persona, actions, and sub_goal separately to ensure clarity.

        2. Actions Planning & Enhancement
            IMPORTANT INSTRUCTION: Never omit, remove, or skip any user-provided detail (e.g., API URLs, custom formats, or specific instructions). You may rephrase them for better clarity, but ensure every single piece of information remains intact.
            Break down complex actions into multiple atomic steps to ensure clear execution while retaining original intent.
            Capture dependencies between actions and reorder them only if absolutely necessary for execution. Confirm with the user before making any reordering suggestions.
        3. Important Instructions:
            Strictly follow the response format that I am providing to you while generating the response. No matter what type of question has been asked follow the same instructions.
            NEVER overlook, discard, or modify user-provided information without explicit confirmation.
            ALWAYS maintain the exact structure of API URLs, specific phrases, and formats provided by the user.
            Ensure each persona has a separate flow. Two personas should never be combined in the same flow.


        4. In the review_details and completed responses, ensure that every piece of information provided by the user is included without skipping, omitting, or overlooking any details. The actions should be described thoroughly and clearly, avoiding any vagueness.
        5. Structured Responses for User Interaction
            CRITICAL: You MUST respond with ONLY a valid JSON object. No prose, no explanation, no markdown. Just pure JSON.
            If information is still being collected, respond ONLY with:
                {"status": "pending", "question": "The question you want to ask"}
            Before finalizing, present a full review ONLY with:
                {"status": "pending", "review_details": "All details in plain string here for user verification"}
            After confirmation, provide the final configuration ONLY with:
                {"status": "completed", "name": "", "agent_name": "skill.region.name", "broadcast_agent": false, "personas": "", "tools": "", "flows": [{"flow_name": "", "persona": "", "actions": [], "sub_goal": ""}], "goal": "", "personality": {"primary_traits": ["3-5 cultural wisdom traits that match this agent's role, e.g. Meraki, Sisu, Aloha"], "tone": "warm-casual or focused-professional or playful-encouraging", "greeting_style": "A warm, personalized opening line for this agent", "identity": "A one-sentence description of who this agent IS (not what it does) - its character, like 'A patient mentor who celebrates every small win' or 'A sharp-eyed analyst who finds patterns others miss'"}}
            NEVER use em-dashes, smart quotes, or Unicode characters in your response. Use plain ASCII only.
            Your response must start with { and end with }. Nothing else.

        """


def create_agents_for_user(user_id: str, autonomous=False, initial_description=None,
                           goal_tags=()) -> Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]:
    """Create new assistant & user proxy agents for a user with basic configuration.

    Args:
        user_id: The user identifier
        autonomous: If True, the LLM answers its own questions (no human input)
        initial_description: When autonomous, the user's description of the desired agent
        goal_tags: the goal tags the agent will be saved with (the turn's
            creation hint), so its plan is written with its own tools
            (plan_writer_tools)
    """
    # The ONE configured LLM, decided in core.autogen_config: the configured
    # endpoint, or the local llama-server when none is configured.  The
    # inline copy this replaced carried its own model names ('gpt-4.1-mini',
    # 'Qwen3-VL-4B-Instruct'); audited before removal: nothing keys on the
    # local name (llm_outbound_logger only hands it to count_tokens_for_text,
    # local_loop sets its own), so the canonical entry is a drop-in (#69).
    # The turn's model comes first, read the way every CREATE and REUSE agent
    # reads it (hartos.helper.get_llm_config); the node's own model is used
    # only when the turn names none.  Central 2026-11-20, goal 5ba50f98: the
    # turn ran on the expert (the matcher and CREATE got 200 from Claude), but
    # this writer used openrouter/free and got 18 answers of 429 between
    # 12:14:50Z and 12:15:00Z.  So the turn had no plan.
    from core.autogen_config import get_autogen_config_list
    from hartos.helper import get_llm_config
    config_list = get_llm_config(get_autogen_config_list())['config_list']

    # Create a basic function calling config
    llm_config = {
        "config_list": config_list,
        "cache_seed": None
    }

    # Build system message — enrich for autonomous mode
    if autonomous and initial_description:
        # The planner MUST know the full tool set at its disposal so it plans
        # with the RIGHT tool per step instead of forcing GUI automation
        # (see select_autonomous_tool_catalog — ON by default).
        _tool_map = plan_writer_tools(goal_tags)
        # AUTONOMOUS MODE: REPLACE the interactive system message entirely.
        # Live test 2026-05-16 13:04 showed that APPENDING the PLAN_FIRST
        # suffix to AGENT_CREATOR_SYSTEM_MESSAGE was diluted by the older
        # "ask user one question at a time" instructions — the model still
        # emitted {"status":"pending","question":"name?"}.  Replacing the
        # whole system message in autonomous mode is the canonical fix.
        system_message = f"""You are HART OS plan author. The peer HARTOS reviewer (StatusVerifier) will auto-review your plan against quality gates and either approve or send refinement feedback. NEVER ask the user questions — the user is NOT in this loop.

Task description: '{initial_description}'

{_tool_map}
THREE-STAGE FLOW (strict, autonomous, no human-in-loop):

STAGE 1 — FIRST CALL: emit a COMPLETE proposed plan with atomic steps.
Response shape (EXACT keys, JSON only):
{{"status":"proposed_plan","name":"<short readable name>","agent_name":"skill.local.name","goal":"<one-line goal>","broadcast_agent":false,"personas":[{{"name":"Executor","description":"<one line>"}}],"flows":[{{"flow_name":"main","persona":"Executor","actions":["<atomic step 1>","<atomic step 2>","..."],"sub_goal":"<one line>"}}],"extra_information":"<optional>","review_required":true}}

FLOWS: "flows" is a list.  A simple task is ONE flow named "main".  A task
description that names its own flows or stages (e.g. "Flow 1 ... Flow 7")
gets ONE entry per named flow, in the order given, each with its own
flow_name, sub_goal and actions, and every one of them is kept: never fold
the named flows into one flow or into extra_information.  Each flow names
its own persona: the PERSON that flow talks with, as the task names them
(e.g. the child, a parent, a therapist), never the agent itself, because a
person picks their persona when they first chat; and "personas" lists every
persona a flow names.  Use "Executor" only when the task names no person
for that flow.  Each flow's sub_goal is a result one turn can show, so its
completion can be checked from that turn's evidence (e.g. "<child_name> says ball"
or "the parent sent three family photos"), never a vague aim ("improves
speech").

PEOPLE ARE BLANKS, NOT PART OF THE AGENT: the agent is built once and used by
many people.  A name or personal detail of a person it serves (the child's
name, a parent's name, the language spoken at home, the words the child
already knows) is never written into the name, goal, personas, sub_goals or
steps as one particular person's: write its blank instead (<child_name>,
<mother_name>, <father_name>, <home_language>, <known_words>), and keep a step
that gets the value from the person or their parent and saves it to memory.
The agent fills every blank per person when it runs.

RULES for the actions[] of EACH flow:
- ONE concrete observable action per item.  No "and then" compounds.
- USE THE RIGHT TOOL from "AVAILABLE TOOLS" above for each step — do NOT default
  to GUI automation. Map the intent to the dedicated tool: recall / "what did we
  discuss" / "N days ago" -> get_chat_history; search saved facts or past chats ->
  search_long_term_memory; search the web -> google_search; fetch/scrape a URL ->
  crawl4ai; OCR an image -> get_text_from_image; generate an image -> text_2_image;
  message the user -> send_message_to_user; schedule -> create_scheduled_jobs;
  write/refactor/debug code -> execute_coding_task. A step a tool can satisfy is
  ONE step that names that tool (e.g. "get_chat_history for the last 15 days").
- ONLY for a genuine GUI action that NO tool covers (open an app, click, type,
  scroll, navigate a page, screenshot) prefix the step with
  "execute_windows_or_android_command: " followed by plain English.  Examples:
    "execute_windows_or_android_command: bring Chrome window to foreground"
    "execute_windows_or_android_command: click the 'Start a post' input box near the top of the feed"
    "execute_windows_or_android_command: click the blue 'Post' button at the bottom-right of the compose modal"
    "execute_windows_or_android_command: take a screenshot to verify the post appeared in the feed"
- Use AS FEW steps as the task needs: a question / recall / lookup is often ONE
  tool step; only a genuine multi-step GUI task needs many (up to ~15 per flow).
  Do NOT pad a tool-satisfiable task into a long GUI sequence.
- Preserve EVERY detail of the task description in the plan (a person's name or personal detail is kept as its blank).

STAGE 2 — SUBSEQUENT CALL (incoming message is review verdict):
- If incoming message is "approved" or starts with "approved": re-emit the SAME plan but with "status":"completed" and remove the "review_required" key.
- Otherwise the message is refinement feedback: re-emit with "status":"proposed_plan" and apply the feedback to the actions[] of the flow it names (flows[0] when it names none), keeping every other flow as it was.

STAGE 3 — Downstream: on "completed", the dispatcher saves persona JSON and hands the atomic-step list to the autogen team (Helper + Executor), which calls the tool named in each step (get_chat_history, google_search, execute_coding_task, execute_windows_or_android_command, etc.).

ABSOLUTE RULES:
- Plain ASCII only.  No em-dashes, no smart quotes, no Unicode.
- Your response must START with {{ and END with }}.  Nothing else — no prose, no markdown, no code fence.
- NEVER use "status":"pending".  NEVER ask questions.  NEVER request user input.
- The user is sick / asleep / away.  You are talking to another agent.
"""
    else:
        # A person's plan is written with the same tools and rule as an
        # autonomous one: shown no tool, Reel Studio (342) got 9 tool-less
        # steps and its CREATE stalled (2026-11-04, see PLAN_WAYS_RULE).
        system_message = AGENT_CREATOR_SYSTEM_MESSAGE + plan_writer_tools(goal_tags)

    # Create the assistant agent with context awareness
    assistant = autogen.AssistantAgent(
        name=f"assistant_{user_id}",
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=_is_terminate_msg,
        code_execution_config={"work_dir": get_coding_workspace_dir(), "use_docker": False},
        system_message=system_message
    )

    # Create the user proxy agent
    # In autonomous mode: max_consecutive_auto_reply=10 allows self-completion
    # In interactive mode: max_consecutive_auto_reply=0 waits for human input
    user_proxy = autogen.UserProxyAgent(
        name=f"user_proxy_{user_id}",
        human_input_mode="NEVER",
        is_termination_msg=_is_terminate_msg,
        max_consecutive_auto_reply=10 if autonomous else 0,
        code_execution_config=False
    )

    return assistant, user_proxy


def _history_lengths(agent) -> dict:
    """{peer: length} of each conversation history ``agent`` holds."""
    return {peer: len(history)
            for peer, history in (getattr(agent, 'chat_messages', None) or {}).items()}


def _take_back_history(agent, lengths, keep=None) -> None:
    """Cut each of ``agent``'s histories back to ``lengths``
    (_history_lengths).  A history begun after them is emptied.  ``keep``,
    the message a failed send carried, stays where that send put it, once
    and unanswered; only what came after it is taken back."""
    for peer, history in (getattr(agent, 'chat_messages', None) or {}).items():
        base = lengths.get(peer, 0)
        if keep is not None and len(history) > base and history[base].get('content') == keep:
            base += 1
        del history[base:]


def _drop_unanswered(agent, message, role) -> None:
    """Take ``message`` out of the unanswered messages at the end of
    ``agent``'s histories (the trailing run of entries in ``role``, which
    failed sends left there), so the send about to carry it again puts it
    back once, last.  The run keeps at most one copy of each text.  Review of
    b03f244e, F2: checking only the last entry, the daemon's task and the
    page's "proceed", sent in turn, piled up 6 copies in 7 failed runs.  An
    answered message is never touched."""
    for history in (getattr(agent, 'chat_messages', None) or {}).values():
        start = len(history)
        while start > 0 and history[start - 1].get('role') == role:
            start -= 1
        history[start:] = [m for m in history[start:] if m.get('content') != message]


def get_agent_response(assistant: autogen.AssistantAgent, user_proxy: autogen.UserProxyAgent, message: str) -> str:
    """Get a single response from the agent for the given message.

    A send whose model call fails keeps the message it carried, once and
    unanswered, and takes back everything after it.  Sending that message
    again does not add a second copy.  The writer keeps its task and the
    person's request (a person's writer holds it only in its history), and
    a retry carries one copy.  Review of 38c753ea, F1, measured: 4 failed
    gathers left 4 copies of the prompt.  Review of 86e5185d, F1, measured:
    taken back whole, a person's request was gone when they typed "try
    again".  A failed "proper format" re-ask is taken back whole (F3)."""
    # autogen keeps a sent message in the sender's history as 'assistant'
    # and in the receiver's as 'user'.
    _sides = ((user_proxy, 'assistant'), (assistant, 'user'))
    for agent, role in _sides:
        _drop_unanswered(agent, message, role)
    _held = [(agent, _history_lengths(agent)) for agent, _role in _sides]
    try:
        # # Get the current chat history
        # current_chat = user_proxy.chat_messages.get(assistant.name, [])
        
        # # Create context from previous messages (last 5 messages for efficiency)
        # context = current_chat[-5:] if current_chat else []
        # context_str = "\n".join([f"{msg['role']}: {msg['content']}" for msg in context])
        
        # # Append context to the message if there's history
        enhanced_message = message
        # # if context:
        # #     enhanced_message = f"Previous conversation:\n{context_str}\n\nCurrent message: {message}"

        # Send message & get response
        response = user_proxy.send(
            enhanced_message,
            assistant,
            request_reply=True
        )
        
        key = list(user_proxy.chat_messages.keys())[0]
        response = user_proxy.chat_messages[key][-1]['content']
        _asked = [(agent, _history_lengths(agent)) for agent, _role in _sides]
        try:
            new_res = retrieve_json(response)
            if new_res['status'].lower() == 'completed':
                if 'flows' not in new_res:
                    response = user_proxy.send(
                        'please give the response in proper format: { "status": "completed", "name": "", "agent_name": "two.word.name", "broadcast_agent": bool, "personas": "", "tools": "", "flows": [ { "flow_name": "", "persona": "", "actions": [], "sub_goal": "" } ], "goal": "" } where flows should be outer key. agent_name must be a creative 2-word dot-separated lowercase name like swift.falcon. \n\n             Strictly follow the response format that I am providing to you while generating the response. No matter what type of question has been asked follow the same instructions.  ',
                        assistant,
                        request_reply=True
                    )
                    key = list(user_proxy.chat_messages.keys())[0]
                    response = user_proxy.chat_messages[key][-1]['content']
        except Exception:
            # The reply stands as it came.  A re-ask whose send failed is
            # taken back whole: it was this function's own message, not the
            # person's (review of 86e5185d, F3: both histories went 4 to 7).
            for agent, lengths in _asked:
                _take_back_history(agent, lengths)
        
        return response

    except Exception as e:
        for agent, lengths in _held:
            _take_back_history(agent, lengths, keep=message)
        # #716: reply gets SPOKEN by TTS - never surface raw internals
        from core.agent_tools import user_facing_error
        return user_facing_error(e)


from core.llm_outbound_logger import with_source as _with_source


@_with_source('autogen.gather')
def gather_info(user_id, user_message, prompt_id, autonomous=False, goal_tags=()):
    """Gather agent details via autogen conversation.

    Args:
        user_id: The user identifier
        user_message: The user's message/description
        prompt_id: The prompt ID for this agent creation session
        autonomous: If True, LLM answers its own questions (no human input needed)
        goal_tags: the turn's creation hint, the tags the agent is saved with;
            the plan writer is shown that family's tools (plan_writer_tools)
    """
    # lazy_module never yields None, so the old `if autogen is None` check is not
    # just dead — it would silently stop guarding. Touch one attribute to force
    # the deferred import here, where a clear message can still be given, rather
    # than letting it surface from inside agent construction further down.
    try:
        autogen.AssistantAgent
    except ImportError as exc:
        raise ImportError(
            "Agent creation requires the 'pyautogen' package. "
            "Install it with: pip install pyautogen"
        ) from exc
    current_app.logger.info('INSIDE GATHER INFo')
    current_app.logger.info('--'*100)
    # Push thinking to UI
    try:
        from hartos.create_recipe import _push_thinking
        _push_thinking(user_id, 'Designing agent personas and planning actions...')
    except Exception:
        pass
    user_prompt = f'{user_id}_{prompt_id}'
    # One turn at a time per writer, whatever its model: the turn registry's
    # exclusive mode (helper.exclusive_turn), the registry every group turn
    # is marked in.  A writer is one conversation between two autogen agents
    # and cannot take two turns at once: two gathers on one writer and one
    # model ran together, and the older one's take-back after a failure
    # removed the other's answered exchange (review of b03f244e, F1).  A
    # gather that cannot have the writer within WRITER_WAIT_S does not queue:
    # it answers the hold, which the hive worker defers, the daemon keeps
    # with no strike, and the creation chat shows the person (review of
    # 49d11c02a: a person waited 4.0 s behind one daemon gather; before, 0 s).
    from core.agent_tools import held_for_another_turn_reply
    from hartos.helper import exclusive_turn, group_turn
    with exclusive_turn(user_prompt, _GATHER_GROUP, WRITER_WAIT_S) as alone:
        if not alone:
            current_app.logger.info(
                f'gather_info {user_prompt}: another turn of its writer is still '
                f'running; held, not queued')
            return held_for_another_turn_reply()
        try:

            # Get or create agents for this user
            if user_prompt not in user_agents:
                user_agents[user_prompt] = create_agents_for_user(
                    user_id,
                    autonomous=autonomous,
                    initial_description=user_message if autonomous else None,
                    goal_tags=goal_tags,
                )

            # The writer is picked up the one way a turn picks up a cached group
            # (helper.group_turn): it runs on THIS turn's model (#172; create_recipe
            # does the same for its group).  Built on an earlier turn, it kept
            # that turn's model: 4 failed gathers for system_daemon_89682852097
            # stayed on ['local'] with attempts 3 and 4 on the expert (review of
            # 38c753ea, F1, measured).  Kept, not rebuilt, so it keeps its task and
            # the person's conversation (review of 30fb3b75).  Inside the
            # exclusive turn no other turn of this writer runs, so group_turn
            # never holds here (the hold above is the live one); its False is
            # answered only because group_turn's contract asks every caller to.
            from core.autogen_config import get_autogen_config_list
            with group_turn(user_agents[user_prompt], get_autogen_config_list(),
                            user_prompt, _GATHER_GROUP) as may_run:
                if not may_run:
                    return held_for_another_turn_reply()
                assistant, user_proxy = user_agents[user_prompt]

                # Get response from the agent
                response = get_agent_response(assistant, user_proxy, user_message)

            # Get chat history length for debugging
            # history_length = len(user_proxy.chat_messages.get(assistant.name, []))
            current_app.logger.info('INSIDE GATHER INFo Respponse')
            try:
                from hartos.create_recipe import _push_thinking
                _push_thinking(user_id, 'Agent blueprint ready. Starting execution...')
            except Exception:
                pass
            return response

        except Exception as e:
            current_app.logger.error(f'ERROR IN GATHERING AGENTDETAILS ERROR IS:- {e}')
            raise

