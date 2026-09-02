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
# llmlingua -> torch) off the backend boot path.
autogen = lazy_module("autogen")

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

Media generation:
  - text_2_image                generate an image from a text prompt
  - Generate_video              generate a talking-head video
  - send_presynthesized_video_to_user   deliver a previously generated video

User communication:
  - send_message_to_user        message the user immediately
  - send_message_in_seconds     message the user after a delay
  - send_message_to_roles       message a specific persona/role

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


# Store user-specific agents & their chat history
user_agents: Dict[str, Tuple[Any, Any]] = {}

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


def create_agents_for_user(user_id: str, autonomous=False, initial_description=None) -> Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]:
    """Create new assistant & user proxy agents for a user with basic configuration.

    Args:
        user_id: The user identifier
        autonomous: If True, the LLM answers its own questions (no human input)
        initial_description: When autonomous, the user's description of the desired agent
    """
    # The ONE configured LLM, decided in core.autogen_config: the configured
    # endpoint, or the local llama-server when none is configured.  The
    # inline copy this replaced carried its own model names ('gpt-4.1-mini',
    # 'Qwen3-VL-4B-Instruct'); audited before removal: nothing keys on the
    # local name (llm_outbound_logger only hands it to count_tokens_for_text,
    # local_loop sets its own), so the canonical entry is a drop-in (#69).
    from core.autogen_config import get_autogen_config_list
    config_list = get_autogen_config_list()

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
        _tool_map = select_autonomous_tool_catalog()
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

RULES for flows[0].actions[]:
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
  tool step; only a genuine multi-step GUI task needs many (up to ~15). Do NOT pad
  a tool-satisfiable task into a long GUI sequence.
- Preserve EVERY detail of the task description in the plan.

STAGE 2 — SUBSEQUENT CALL (incoming message is review verdict):
- If incoming message is "approved" or starts with "approved": re-emit the SAME plan but with "status":"completed" and remove the "review_required" key.
- Otherwise the message is refinement feedback: re-emit with "status":"proposed_plan" and apply the feedback to flows[0].actions[].

STAGE 3 — Downstream: on "completed", the dispatcher saves persona JSON and hands the atomic-step list to the autogen team (Helper + Executor), which calls the tool named in each step (get_chat_history, google_search, execute_coding_task, execute_windows_or_android_command, etc.).

ABSOLUTE RULES:
- Plain ASCII only.  No em-dashes, no smart quotes, no Unicode.
- Your response must START with {{ and END with }}.  Nothing else — no prose, no markdown, no code fence.
- NEVER use "status":"pending".  NEVER ask questions.  NEVER request user input.
- The user is sick / asleep / away.  You are talking to another agent.
"""
    else:
        system_message = AGENT_CREATOR_SYSTEM_MESSAGE

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


def get_agent_response(assistant: autogen.AssistantAgent, user_proxy: autogen.UserProxyAgent, message: str) -> str:
    """Get a single response from the agent for the given message."""
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
            pass
        
        return response

    except Exception as e:
        # #716: reply gets SPOKEN by TTS - never surface raw internals
        from core.agent_tools import user_facing_error
        return user_facing_error(e)


from core.llm_outbound_logger import with_source as _with_source


@_with_source('autogen.gather')
def gather_info(user_id, user_message, prompt_id, autonomous=False):
    """Gather agent details via autogen conversation.

    Args:
        user_id: The user identifier
        user_message: The user's message/description
        prompt_id: The prompt ID for this agent creation session
        autonomous: If True, LLM answers its own questions (no human input needed)
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
    try:

        # Get or create agents for this user
        if user_prompt not in user_agents:
            user_agents[user_prompt] = create_agents_for_user(
                user_id,
                autonomous=autonomous,
                initial_description=user_message if autonomous else None
            )

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

