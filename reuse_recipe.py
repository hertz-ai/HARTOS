from flask import Flask, request, jsonify
from typing import Dict, Tuple
import autogen
import os
import json


# Store user-specific agents and their chat history
user_agents: Dict[str, Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]] = {}

def create_agents_for_user(user_id: str,prompt_id: int) -> Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]:
    """Create new assistant and user proxy agents for a user with basic configuration."""
    config_list = [{
        "model": "hertzai-4o",
        "api_type": "azure",
        "api_key": "8f3cd49e1c3346128ba77d09ee9c824c",
        "base_url": "https://hertzai-gpt4.openai.azure.com/",
        "api_version": "2024-02-15-preview"
    }]

    # Create a basic function calling config
    llm_config = {
        "config_list": config_list,
        "seed": 42
    }
    
    with open(f'prompts/{prompt_id}.json') as config_file:
        config_data = json.load(config_file)
        actions = config_data['flows'][0]['actions']


    # Create the assistant agent with context awareness
    assistant = autogen.AssistantAgent(
        name=f"assistant_{user_id}",
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message=f'''You are a Helpful Assistant follow below action's to help user
        
        Actions: {actions}
        '''
    )

    # Create the user proxy agent
    user_proxy = autogen.UserProxyAgent(
        name=f"user_proxy_{user_id}",
        human_input_mode="NEVER",
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        max_consecutive_auto_reply=0,
        code_execution_config={"work_dir": "coding", "use_docker": False}
    )

    return assistant, user_proxy


def get_agent_response(assistant: autogen.AssistantAgent, user_proxy: autogen.UserProxyAgent, message: str) -> str:
    """Get a single response from the agent for the given message."""
    try:

        # Send message and get response
        response = user_proxy.initiate_chat(assistant, message=message, clear_history=False)
        
        key = list(user_proxy.chat_messages.keys())[0]
    
        
        return user_proxy.chat_messages[key][-1]['content']

    except Exception as e:
        return f"Error getting response: {str(e)}"


def chat_agent(user_id,text,prompt_id):
    try:
        use_recipe = True

        # Get or create agents for this user
        if user_id not in user_agents:
            user_agents[user_id] = create_agents_for_user(user_id,prompt_id)

        assistant, user_proxy = user_agents[user_id]
        prompt_id = int(prompt_id)
    
        response = get_agent_response(assistant, user_proxy, text)

        # Get chat history length for debugging
        history_length = len(user_proxy.chat_messages.get(assistant.name, []))

        return response
    except Exception as e:
        print(f'Some ERROR IN REUSE RECIPE {e}')
        raise
