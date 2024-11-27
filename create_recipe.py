from flask import Flask, request, jsonify
import autogen
from typing import Dict, Tuple
import os


user_agents: Dict[str, Tuple[autogen.ConversableAgent, autogen.ConversableAgent]] = {}
config_list = [{
        "model": os.getenv("deployment_name"),
        "api_type": "azure",
        "api_key": os.getenv("azure_api_key"),
        "base_url": os.getenv("azure_endpoint"),
        "api_version": "2024-02-15-preview"
    }]

executor_config = {
    "llm_config": {
        "config_list": config_list,
        "temperature": 0.4,
    },
    "system_message": """You are a specialized executor agent focused solely on running and debugging Python code.
    Your responsibilities:
    1. Execute code provided by the assistant
    2. Report execution results, errors, or output
    3. If there are errors:
       - Identify the issue
       - Propose and implement fixes
       - Report back to the assistant
    
    Do not engage in general conversation - that's the assistant's role.
    Always provide clear execution results or error messages to the assistant.
    if you get any conversation which is not related to coding send that to author"""
}

def state_transition(last_speaker, groupchat):
    messages = groupchat.messages

    if last_speaker != 'author':
        pass
    
    if 'TERMINATE' in messages[-1]["content"].upper():
        print('TERMINATING BECAUSE OF TERMINATE')
        # retrieve: action 1 -> action 2
        return None
    else:
        return 'auto'


def create_agents(user_id: str) -> Tuple[autogen.ConversableAgent, autogen.ConversableAgent]:
    """Create new assistant and user agents for a given user_id"""
    
    llm_config = {
        "temperature": 0.7,
        "config_list": [{
            "model": os.getenv("deployment_name"),
            "api_type": "azure",
            "api_key": os.getenv("azure_api_key"),
            "base_url": os.getenv("azure_endpoint"),
            "api_version": "2024-02-15-preview"
        }],
    }
    
    
    # Create assistant agent
    assistant = autogen.AssistantAgent(
        name="assistant",
        llm_config=llm_config,
        system_message="""You are a diligent assistant. Your task is to follow the author's instructions step by step to perform a series of actions.

        1. For each task in the sequence:
            1.1. Attempt to execute the task exactly as described.
            1.2. If you cannot perform a task or if additional information is required, ask the author for clarification before proceeding.
        2. If you complete some tasks but fail on one, inform the author about the failure and restart the entire sequence from the beginning.

        3.Continue following this process until all tasks are successfully completed in order.

        4.Always confirm successful completion of each task and notify the author when the entire sequence is done.
        
        Note: if you need any information which you are not able to get or you dont have ask the author agent pass the conversation to author, if you want to run some code pass the conversation to executor,
        if you need any steps/actions related information pass the conversation to the chat_instructor, You should write the code and give the code to executor
        
        """,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    author = autogen.UserProxyAgent(
        name="Author",
        human_input_mode="NEVER",
        is_termination_msg=lambda x: True if x.get("content").strip()=='' else False,
        max_consecutive_auto_reply=0,
        code_execution_config={
            "work_dir": "work_dir",
            "use_docker": False,
        },
    )
    
    executor = autogen.AssistantAgent(
        name="executor",
        code_execution_config={"work_dir": "coding", "use_docker": False},
        **executor_config
    )
    
    chat_instructor = autogen.AssistantAgent(
        name="chat_instructor",
        llm_config=llm_config,
        system_message="""You are a diligent assistant. 
        INSTRUCTIONS: 1. For each task in the sequence:
            1.1. Attempt to execute the task exactly as described.
            1.2. If you cannot perform a task or if additional information is required, ask the author for clarification before proceeding.
        2. If you complete some tasks but fail on one, inform the author about the failure and restart the entire sequence from the beginning.

        3.Continue following this process until all tasks are successfully completed in order.

        4.Always confirm successful completion of each task and notify the author when the entire sequence is done.
        
        below are the actions to follow:
        1. get the user_id from the author in conversation style
        2. hit the api 'https://mailer.hertzai.com/getstudent_by_user_id' with a post request and a request body with key as user_id and value as the id you got from author
        3. format the response of api in a table
        
        after completing all actions successfully Reflect on the sequence and create a recipe containing all the steps
        necessary and name for it. Suggest well-documented, generalized python function(s)
        to perform similar tasks for coding steps in future, never store information given from the author directly in recipe instead create a placeholder for that variable and use it.
        Make sure coding steps and non-coding steps are never mixed in one function. In the docstr of the function(s),
        clarify what non-coding steps are needed to use the language skill of the assistant.
        after creating the recipe save the recipe in a json with the recipe name 
        and after saving json give TERMNATE at end and return the conversation to the author
        
        """,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    group_chat = autogen.GroupChat(
        agents=[assistant, executor, author, chat_instructor],
        messages=[],
        max_round=15,
        # speaker_selection_method="auto",  # using an LLM to decide
        speaker_selection_method=state_transition,  # using an LLM to decide
        allow_repeat_speaker=False  # Prevent same agent speaking twice
    )
    
    manager = autogen.GroupChatManager(
        groupchat=group_chat,
        llm_config={"config_list": config_list}
    )

    return author, assistant, executor, group_chat, manager, chat_instructor


def get_response(user_id):
    # Get or create agents for this user
    if user_id not in user_agents:
        user_agent, assistant_agent = create_agents(user_id)
        user_agents[user_id] = (user_agent, assistant_agent)
    else:
        user_agent, assistant_agent = user_agents[user_id]

    # Send message and get response
    # user_agent.send(text, assistant_agent, request_reply=True)
    user_agent.initiate_chat(assistant_agent, message=text,clear_history=False)
    
    # Get the last message from the assistant's chat history
    chat_history = assistant_agent.chat_messages.get(user_agent, [])
    last_response = chat_history[-1]['content'] if chat_history else "No response"
    return last_response

def get_response_group(user_id,text):
    # Get or create agents for this user
    if user_id not in user_agents:
        author, assistant_agent, executor, group_chat, manager, chat_instructor = create_agents(user_id)
        user_agents[user_id] = (author, assistant_agent, executor, group_chat, manager, chat_instructor)
        messages[user_id] = []
    else:
        author, assistant_agent, executor, group_chat, manager, chat_instructor = user_agents[user_id]

    if len(messages[user_id])>0:
        last_agent, last_message = manager.resume(messages=messages[user_id])
        author.initiate_chat(recipient=manager, message=text, clear_history=False,silent=False)
    else:
        chat_instructor.initiate_chat(
            manager,
            message='start the actions',
            speaker_selection={"speaker": "assistant"},
            silent=False
        )
    
    messages[user_id] = group_chat.messages
    last_message = group_chat.messages[-1]
    return last_message

messages = {}

def recipe(user_id, text):
    try:
        useagent = False
        
        last_response = get_response_group(user_id,text)
        
        return last_response['content']
        
    except Exception as e:
        print(f"Error occurred in create Recipe: {str(e)}")  # Add logging for debugging
        raise
