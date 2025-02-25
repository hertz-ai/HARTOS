from typing import Dict, Tuple
import autogen
import os
from flask import current_app
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
        Your role is to assist in a co-creative manner. You should actively suggest actions or improvements, but always confirm with the user before implementing them. Ensure that any actions or suggestions are realistic, humanly possible & ethical. Avoid proposing anything beyond practical feasibility, such as tasks like taking the user to the moon. Your primary goal is to enhance the collaboration while adhering to these boundaries.
        Speak in casual, playful, & respectful tone, while keeping it natural, funny, colloquial, & relatable. Expressions should be clear, accurate, grammatically, & contextually correct, avoiding tense confusion. Switch to a more formal tone only if the user keeps it formal.
        The information you need to collect includes:

        {"name": "The name of the agent",
        "goal": "The ultimate goal of the agent",
        "broadcast_agent":'yes/no', // ask yes or no
        "personas":[{"name":"the role of the person comes here","description":" description on what the person can do here"}] //if broadcast_agent is true then by deafult it should be blank [] else ask of number of persona/people involved in this agent
        "flows": [{"flow_name":"","actions":['string array with actions(with tool usage) to perform to reach the sub goal for this flow'],"sub_goal":"the goal for this flow"}],
        "extra_information":"Some extra information/note here"
        }
        Guidelines for Responses:

        for flows, first ask number of flows & then each flow name & actions.
        If you are still gathering information, your response should be formatted as: { "status": "pending", "question": "The question you want to ask" }
        after getting actions ask user please provide additional actions for this flow
        first get the actions & then suggest a flow name based on actions & ask if user is ok with this suggested name or ask for a new name
        plan and enhance actions considering saving to working memory for later reuse. Plan capturing the dependencies between actions & reorder actions if absolutely necessary for proper execution to meet the goal. IMPORTANT INSTRUCTIONS plan but do not override or overlook any of the user provided instructions/actions.
        before going to completed state give all the details to user so that user can review it once. the response format for this should be {"status":"pending","review_details":"details here"}
        after reviewing you should give your response as { "status": "completed", "name": "","broadcast_agent":bool,"personas":"" "tools": "", "flows": [{"flow_name", "actions": [],"sub_goal":"" }] "goal": ""}
        IMPORTANT INSTRUCTION: never skip any user given details like api url, or some information or data always have that in all of your responses.
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

