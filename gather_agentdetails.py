from typing import Dict, Tuple
import autogen
import os
from flask import current_app

from helper import retrieve_json
# Store user-specific agents & their chat history
user_agents: Dict[str, Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]] = {}

def create_agents_for_user(user_id: str) -> Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]:
    """Create new assistant & user proxy agents for a user with basic configuration."""
    config_list = [{
        "model": 'gpt-4o-mini',
        "api_type": "azure",
        "api_key": '4xmi9X9pGCwRn2Pb0vldz6t6FQaAe29bUIkFjKRC7ytrVZ1Ni5cWJQQJ99BAACHYHv6XJ3w3AAABACOG99Zf',
        "base_url": 'https://hertzai-gpt4.openai.azure.com/',
        "api_version": "2024-02-15-preview"
    }]

    # Create a basic function calling config
    llm_config = {
        "config_list": config_list,
        "cache_seed": None
    }

    # Create the assistant agent with context awareness
    assistant = autogen.AssistantAgent(
        name=f"assistant_{user_id}",
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message="""You are a custom agent bot creator. Your task is to interact with the user to gather all the necessary details to create an agent. Once you have collected all the required information, you will generate a complete agent configuration.
        Your role is to assist in a co-creative manner. You should actively suggest actions or improvements, but always confirm with the user before implementing them. Ensure that any actions or suggestions are realistic, humanly possible & ethical. Avoid proposing anything beyond practical feasibility, such as tasks like taking the user to the moon. Your primary goal is to enhance collaboration while adhering to these boundaries.
        Speak in a casual, playful, and respectful tone, keeping it natural, funny, colloquial, and relatable. Expressions should be clear, accurate, grammatically, and contextually correct, avoiding tense confusion. Switch to a more formal tone only if the user keeps it formal.
        ## Information Collection:
        You need to collect the following details from the user:
        { "name": "The name of the agent", "goal": "The ultimate goal of the agent", "broadcast_agent": "yes/no", "personas": [ { "name": "The role of the persona", "description": "A description of what this persona can do" } ], "flows": [ { "flow_name": "", "persona": "Each persona will have a separate flow", "actions": ["String array with actions (including tool usage) to perform to reach the sub-goal for this flow"], "sub_goal": "The goal for this flow" } ], "extra_information": "Additional notes or relevant information" }
        
        ## Guidelines for Responses:
        1.Information Gathering Process
            For flows, first ask the user for the number of flows, then collect each flow's details step by step.
            Ask for flow_name, persona, actions, and sub_goal separately to ensure clarity.
            Always confirm with the user after gathering information to prevent loss of details and do not gulp any information from user.
        2. Actions Planning & Enhancement
            IMPORTANT INSTRUCTION: Never omit, remove, or skip any user-provided detail (e.g., API URLs, custom formats, or specific instructions). You may rephrase them for better clarity, but ensure every single piece of information remains intact.
            Break down complex actions into multiple atomic steps to ensure clear execution while retaining original intent.
            Capture dependencies between actions and reorder them only if absolutely necessary for execution. Confirm with the user before making any reordering suggestions.
        3. Structured Responses for User Interaction
            If information is still being collected, respond in this format:
                { "status": "pending", "question": "The question you want to ask" }
            Before finalizing, present a full review to the user in this format:
                { "status": "pending", "review_details": "All details in plain string here for user verification" }
            After confirmation, provide the final configuration in this format:
                { "status": "completed", "name": "", "broadcast_agent": bool, "personas": "", "tools": "", "flows": [ { "flow_name": "", "persona": "", "actions": [], "sub_goal": "" } ], "goal": "" }
        4. Important Instructions:
            NEVER overlook, discard, or modify user-provided information without explicit confirmation.
            ALWAYS maintain the exact structure of API URLs, specific phrases, and formats provided by the user.
            Ensure each persona has a separate flow. Two personas should never be combined in the same flow.
            Always confirm with the user before finalizing any details.
        5. In the review_details and completed responses, ensure that every piece of information provided by the user is included without skipping, omitting, or overlooking any details. The actions should be described thoroughly and clearly, avoiding any vagueness.
        """
    )

    # Create the user proxy agent
    user_proxy = autogen.UserProxyAgent(
        name=f"user_proxy_{user_id}",
        human_input_mode="NEVER",
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        max_consecutive_auto_reply=0,
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
    
        
        return user_proxy.chat_messages[key][-1]['content']

    except Exception as e:
        return f"Error getting response: {str(e)}"


def gather_info(user_id,user_message,prompt_id):
    current_app.logger.info('INSIDE GATHER INFo')
    current_app.logger.info('--'*100)
    user_prompt = f'{user_id}_{prompt_id}'
    try:

        # Get or create agents for this user
        if user_prompt not in user_agents:
            user_agents[user_prompt] = create_agents_for_user(user_id)

        assistant, user_proxy = user_agents[user_prompt]

        # Get response from the agent
        response = get_agent_response(assistant, user_proxy, user_message)

        # Get chat history length for debugging
        # history_length = len(user_proxy.chat_messages.get(assistant.name, []))
        current_app.logger.info('INSIDE GATHER INFo Respponse')
        return response

    except Exception as e:
        current_app.logger.error(f'ERROR IN GATHERING AGENTDETAILS ERROR IS:- {e}')
        raise

