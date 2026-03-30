from typing import Any, Dict, Tuple
import os
from flask import current_app

try:
    import autogen
except ImportError:
    autogen = None

from helper import retrieve_json, retrieve_json, _is_terminate_msg
from cultural_wisdom import get_cultural_prompt
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
    # Mode-aware config_list: cloud/regional use external LLM, flat uses local llama.cpp
    _node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
    if _node_tier in ('regional', 'central') and os.environ.get('HEVOLVE_LLM_ENDPOINT_URL'):
        config_list = [{
            "model": os.environ.get('HEVOLVE_LLM_MODEL_NAME', 'gpt-4.1-mini'),
            "api_key": os.environ.get('HEVOLVE_LLM_API_KEY', 'dummy'),
            "base_url": os.environ['HEVOLVE_LLM_ENDPOINT_URL'],
            "price": [0.0025, 0.01]
        }]
    else:
        from core.port_registry import get_local_llm_url
        config_list = [{
            "model": 'Qwen3-VL-4B-Instruct',
            "api_key": 'dummy',
            "base_url": get_local_llm_url(),
            "price": [0, 0]
        }]

    # Create a basic function calling config
    llm_config = {
        "config_list": config_list,
        "cache_seed": None
    }

    # Build system message — enrich for autonomous mode
    system_message = AGENT_CREATOR_SYSTEM_MESSAGE
    if autonomous and initial_description:
        system_message += f"""

AUTONOMOUS MODE INSTRUCTIONS:
The user wants you to create an agent autonomously based on this description: '{initial_description}'.
You must fill in ALL required fields yourself without asking questions.
Generate appropriate name, agent_name (skill.region.name format), goal, broadcast_agent, personas, flows (with flow_name, persona, actions, sub_goal), and extra_information.
Return ONLY a valid JSON object with status="completed". No prose, no explanation, no markdown.
Do NOT ask any questions. Do NOT use em-dashes or smart quotes. Plain ASCII only.
Your response must start with {{ and end with }}. Nothing else.
"""

    # Create the assistant agent with context awareness
    assistant = autogen.AssistantAgent(
        name=f"assistant_{user_id}",
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=_is_terminate_msg,
        code_execution_config={"work_dir": "coding", "use_docker": False},
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
        except:
            pass
        
        return response

    except Exception as e:
        return f"Error getting response: {str(e)}"


def gather_info(user_id, user_message, prompt_id, autonomous=False):
    """Gather agent details via autogen conversation.

    Args:
        user_id: The user identifier
        user_message: The user's message/description
        prompt_id: The prompt ID for this agent creation session
        autonomous: If True, LLM answers its own questions (no human input needed)
    """
    if autogen is None:
        raise ImportError(
            "Agent creation requires the 'pyautogen' package. "
            "Install it with: pip install pyautogen"
        )
    current_app.logger.info('INSIDE GATHER INFo')
    current_app.logger.info('--'*100)
    # Push thinking to UI
    try:
        from create_recipe import _push_thinking
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
            from create_recipe import _push_thinking
            _push_thinking(user_id, 'Agent blueprint ready. Starting execution...')
        except Exception:
            pass
        return response

    except Exception as e:
        current_app.logger.error(f'ERROR IN GATHERING AGENTDETAILS ERROR IS:- {e}')
        raise

