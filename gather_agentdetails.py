from typing import Dict, Tuple
import autogen
import os

# Store user-specific agents and their chat history
user_agents: Dict[str, Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]] = {}

def create_agents_for_user(user_id: str) -> Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]:
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

    # Create the assistant agent with context awareness
    assistant = autogen.AssistantAgent(
        name=f"assistant_{user_id}",
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message="""You are a custom agent bot creator. Your task is to interact with the user to gather all the necessary details to create an agent. Once you have collected all the required information, you will generate a complete agent configuration.

        The information you need to collect includes:

        {"name": "The name of the agent",
        "goal": "The ultimate goal of the agent",
        "conversable_agent":True/False,
        "number_of_persona":"no of agents to be create only ask if conversable_agent is true else make it 1"
        "flows": [{"flow_name":"","actions":['string array with actions(with tool usage) to perform to reach the sub goal for this flow'],"sub_goal":"the goal for this flow"]
        }
        Guidelines for Responses:

        for flows, first ask number of flows and then each flow name and actions.
        If you are still gathering information, your response should be formatted as: { "status": "pending", "question": "The question you want to ask" }
        if there is any python code to run please execute and test the code first and then go to next step
        after this you should give your response as { "status": "completed", "name": "","conversable_agent":bool,"number_of_persona":"" "tools": "", "flows": [{"flow_name", "actions": [],"sub_goal":"" }] "goal": ""}
        
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

        # Send message and get response
        response = user_proxy.send(
            enhanced_message,
            assistant,
            request_reply=True
        )
        
        key = list(user_proxy.chat_messages.keys())[0]
    
        
        return user_proxy.chat_messages[key][-1]['content']

    except Exception as e:
        return f"Error getting response: {str(e)}"


def gather_info(user_id,user_message):
    print('INSIDE GATHER INFo')
    try:

        # Get or create agents for this user
        if user_id not in user_agents:
            user_agents[user_id] = create_agents_for_user(user_id)

        assistant, user_proxy = user_agents[user_id]

        # Get response from the agent
        response = get_agent_response(assistant, user_proxy, user_message)

        # Get chat history length for debugging
        # history_length = len(user_proxy.chat_messages.get(assistant.name, []))
        print('INSIDE GATHER INFo Respponse')
        return response

    except Exception as e:
        print(f'ERROR IN GATHERING AGENTDETAILS ERROR IS:- {e}')
        raise

