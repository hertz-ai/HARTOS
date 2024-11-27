import autogen
from typing import Dict, Tuple
import os
import json


user_agents: Dict[str, Tuple[autogen.ConversableAgent, autogen.ConversableAgent]] = {}
config_list = [{
        "model": "hertzai-4o",
        "api_type": "azure",
        "api_key": "8f3cd49e1c3346128ba77d09ee9c824c",
        "base_url": "https://hertzai-gpt4.openai.azure.com/",
        "api_version": "2024-02-15-preview"
    }]



executor_config = {
    "llm_config": {
        "config_list": config_list,
        "temperature": 0.4,
    },
    "system_message": """You are a specialized executor agent focused solely on creating, running and debugging code.
    Your responsibilities:
    1. Execute code provided by the assistant
    2. Report execution results, errors, or output
    3. If there are errors:
       - Identify the issue
       - Propose and implement fixes
       - Report back to the assistant
    
    Note: Your Working Directory is "/home/hertzai2019/newauto/coding" use this if you need,
    if you get any sh base command create a bash script add all sh command in that file and run that file to execute it once
    Do not engage in general conversation - that's the assistant's role.
    Add proper error handling, logging.
    Always provide clear execution results or error messages to the assistant.
    if you get any conversation which is not related to coding pass the conversation to author"""
}


    # "code_execution_config":{"executor": executor_docker},

def state_transition(last_speaker, groupchat):
    messages = groupchat.messages

    # if last_speaker != 'author':
    #     print(last_speaker)
    
    if 'TERMINATE' in messages[-1]["content"].upper():
        print('TERMINATING BECAUSE OF TERMINATE')
        # retrieve: action 1 -> action 2
        return None
    else:
        return 'auto'

class Action:
    def __init__(self,actions):
        self.actions = actions
        self.current_action = 0
    
    def get_action(self,current_action):
        return self.actions[current_action]
        


def create_agents(user_id: str) -> Tuple[autogen.ConversableAgent, autogen.ConversableAgent]:
    """Create new assistant and user agents for a given user_id"""
    
    llm_config = {
        "temperature": 0.7,
        "config_list": [{
        "model": "hertzai-4o",
        "api_type": "azure",
        "api_key": "8f3cd49e1c3346128ba77d09ee9c824c",
        "base_url": "https://hertzai-gpt4.openai.azure.com/",
        "api_version": "2024-02-15-preview"
    }],
    }
    
    
    
    # Create assistant agent
    assistant = autogen.AssistantAgent(
        name="assistant",
        llm_config=llm_config,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message="""You are a General agent which will perform some task/action provided by the chat_instructor
        You should get the clear understanding of the task/action that you are given, you should you complete the task/action on your own
        If you want to run some code create the code and pass the conversation to the executor agent, executor agent will run the code and return back ressponse
        If you need any information or having some issues or getting some error you should contact author agent and pass the conversation to author agent
        
        Flow after getting the task/action from chat_instructor
        1. If your task in completed:
            i. evaluate the task
            ii. if there is any kind of error give the error to the author and ask for help
            iii. if there is no error give the confirmation to the author that you are moving ahead with next action and wait for author response
            iv. after author confirmation ask the chat_instructor for new task/action
        2. If you are not able to complete the task:
            i. if there are any error then send all those error summary to the Author and ask help to author
            ii. never assume anything on your own ask for author help if needed
            
                    
        
        Remember: Maintain clear communication, prioritize accuracy over speed, and ensure proper handoff when delegating tasks.
        Note: If you need any information first ask the information and then route the request to Author
        if there are any sh based commands give all commands in a bash scripts, if you want to add some content in some file consider how will you do that
        """,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    helper = autogen.AssistantAgent(
        name="helper",
        llm_config=llm_config,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message="""Help the assistant agent to complete the task do not coordinate with other agents""",
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    author = autogen.UserProxyAgent(
        name="Author",
        human_input_mode="NEVER",
        is_termination_msg=lambda x: True if x.get("content").strip()=='' else False,
        max_consecutive_auto_reply=0,
        code_execution_config=False,
    )
    
    executor = autogen.AssistantAgent(
        name="executor",
        code_execution_config={"work_dir": "coding", "use_docker": False},
        **executor_config
    )
    
    chat_instructor = autogen.UserProxyAgent(
        name="chat_instructor",
        human_input_mode="NEVER",
        max_consecutive_auto_reply=10,
        default_auto_reply="TERMINATE",
        code_execution_config=False,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    assistant.description = 'General-purpose agent that coordinates and executes requested tasks and actions'
    executor.description = 'Specialized agent for code execution and response handling'
    author.description = 'Agent focused on user support, error resolution, contextual information connect this agent when you need somthing'
    chat_instructor.description = 'Strategic agent that provides step-by-step action plans for task execution'
    helper.description = 'Support agent that facilitates task completion and assists other agents'
    
    group_chat = autogen.GroupChat(
        agents=[assistant, executor, author, chat_instructor,helper],
        messages=[],
        max_round=15,
        # speaker_selection_method="auto",  # using an LLM to decide
        speaker_selection_method=state_transition,  # using an LLM to decide
        allow_repeat_speaker=False,  # Prevent same agent speaking twice
        send_introductions=True
    )
    
    manager = autogen.GroupChatManager(
        groupchat=group_chat,
        llm_config={"config_list": config_list}
    )
    
    
    # @executor.register_for_execution()
    # @assistant.register_for_llm(description="Contact parents of the user.")
    # def contact_parents(
    #     message: Annotated[str, "Message to send to parents"],
    #     user_id: Annotated[str, "ID of the user whose parents need to be contacted"]
    # ) -> str:
    #     """
    #     Contacts the parents of a user by retrieving their information and sending a message.
        
    #     Args:
    #         message: The message to be sent to parents
    #         user_id: The ID of the user whose parents need to be contacted
        
    #     Returns:
    #         str: Confirmation message
    #     """
    #     try:
    #         # Get parents' email from the API
    #         response = requests.get(f"https://mailer.hertzai.com/getparents_mail", params={"user_id": user_id})
            
    #         if response.status_code != 200:
    #             raise ValueError(f"Failed to get parents' information. Status code: {response.status_code}")
            
    #         parents_data = response.json()
    #         print(f"Parents' details for user {user_id}:")
    #         print(parents_data)
            
    #         # Here you would typically implement the actual email sending logic
    #         # For now, we just print the information
            
    #         return "emailed parents successfully"
        
    #     except Exception as e:
    #         raise ValueError(f"Error contacting parents: {str(e)}")
    # @executor.register_for_execution()
    # @assistant.register_for_llm(description="Save json data as a JSON file")
    # def save_json(
    #     json: Annotated[dict, "Dictionary to save as a json"],
    #     name: Annotated[str, "name of json"]
    # ) -> str:
    #     """
    #     Saves a dictionary as a JSON file.
        
    #     Args:
    #         json: Dictionary to be saved as JSON
    #         name: Name of the JSON file (without extension)
        
    #     Returns:
    #         str: Path where the JSON file was saved
    #     """
    #     try:
    #         # Ensure the name has .json extension
    #         if not name.endswith('.json'):
    #             name = f"{name}.json"
                
    #         # Save the JSON file
    #         with open(name, 'w', encoding='utf-8') as f:
    #             json_lib.dump(json, f, indent=4)
                
    #         # Get the absolute path
    #         file_path = os.path.abspath(name)
            
    #         return f"JSON saved successfully at {file_path}"
        
    #     except Exception as e:
    #         raise ValueError(f"Error saving JSON: {str(e)}")

    return author, assistant, executor, group_chat, manager, chat_instructor

def get_response_group(user_id,text,prompt_id):
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
        message = '''start the actions'''
        author.initiate_chat(
            manager,
            message=message,
            speaker_selection={"speaker": "assistant"},
            silent=False
        )
    
    with open(f'prompts/{prompt_id}.json') as config_file:
        config_data = json.load(config_file)
        actions = config_data['flows'][0]['actions']
        actions = Action(actions)
    
    while True:
        print('inside while')
        if group_chat.messages[-1]['name'] == 'chat_instructor' and group_chat.messages[-1]['content'] == 'TERMINATE':
            print('resuming chat')
            if actions.current_action==len(actions.actions):
                message = '''Reflect on the sequence and create a recipe containing all the steps
                necessary and name for it. Suggest well-documented, generalized python function(s)
                to perform similar tasks for coding steps in future, never store information given from the author directly in recipe instead create a placeholder for that variable and use it.
                Make sure coding steps and non-coding steps are never mixed in one function. In the docstr of the function(s),
                clarify what non-coding steps are needed to use the language skill of the assistant.'''
                print(f'{message}')
                author.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
            else:
                message = actions.get_action(actions.current_action)
                actions.current_action += 1
                message = f'Action {actions.current_action}: {message} '
                print(f'{message}')
                chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
        else:
            break
            
        if actions.current_action >len(actions.actions):
            print(f'current action {actions.current_action} is greater than legth {len(actions.actions)}')
            break
            
            

    messages[user_id] = group_chat.messages
    last_message = group_chat.messages[-1]
    if last_message['content'] == 'TERMINATE':
        last_message = group_chat.messages[-2]
    return last_message
messages = {}

def recipe(user_id, text,prompt_id):
    try:
        useagent = False
        
        last_response = get_response_group(user_id,text,prompt_id)
        
        return last_response['content']
        
    except Exception as e:
        print(f"Error occurred in create Recipe: {str(e)}")  # Add logging for debugging
        raise
