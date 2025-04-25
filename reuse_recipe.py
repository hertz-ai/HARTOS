import autogen
import os
import requests
from typing import Dict, Optional, Tuple
import uuid
import time
import re
import asyncio
from datetime import datetime
from typing import Annotated, Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import json
from collections import deque
import redis
import pickle
from PIL import Image
from langchain.memory import ZepMemory
from crossbarhttp import Client
from flask import current_app
from helper import topological_sort, ToolMessageHandler, strip_json_values, get_time_based_history, retrieve_json
import helper as helper_fun
from autogen.agentchat.contrib.capabilities import transform_messages, transforms
import threading


client = Client('http://aws_rasa.hertzai.com:8088/publish')
scheduler = BackgroundScheduler()
scheduler.start()
# logging_session_id = runtime_logging.start(config={"dbname": "logs.db"})
# Store user-specific agents & their chat history
user_agents: Dict[str, Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]] = {}
role_agents: Dict[str, Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]] = {}
agents_session = {}
recipes = {}
user_journey = {}
temp_users = {}
chat_joinees = {}
agents_roles = {}
llm_call_track = {}

redis_client = redis.StrictRedis(
    host='azure_all_vms.hertzai.com', port=6369, db=0)
agent_data = {}
# config_list = [{
#     "model": 'gpt-4o',
#     "api_type": "azure",
#     "api_key": '4xmi9X9pGCwRn2Pb0vldz6t6FQaAe29bUIkFjKRC7ytrVZ1Ni5cWJQQJ99BAACHYHv6XJ3w3AAABACOG99Zf',
#     "base_url": 'https://hertzai-gpt4.openai.azure.com/',
#     "api_version": "2024-02-15-preview",
#     "price": [0.0025, 0.01]
# }]

config_list = [{
    "model": "gpt-4o-mini",
    "api_type": "azure",
    "api_key": "4xmi9X9pGCwRn2Pb0vldz6t6FQaAe29bUIkFjKRC7ytrVZ1Ni5cWJQQJ99BAACHYHv6XJ3w3AAABACOG99Zf",
    "base_url": "https://hertzai-gpt4.openai.azure.com/",
    "api_version": "2024-02-15-preview",
    "price":[0.00015,0.0006]
}]

class Action:
    def __init__(self,actions):
        self.actions = actions
        self.current_action = 0
        self.fallback = False
        self.new_json = []
        self.recipe = False
    
    def get_action(self,current_action):
        try:
            return self.actions[current_action]
        except:
            raise IndexError("Custom message: Index is out of range!") 


class SubscriptionHandler:
    message = None

    async def on_rpc_response(self, session, msg):
        current_app.logger.info("Received RPC response: {}".format(msg))
        SubscriptionHandler.message = msg
        await component.stop()  # Stop the component after getting the response

async def subscribe_and_return(message,topic,time=8000):
    global component
    component = Component(
        transports="ws://aws_rasa.hertzai.com:8088/ws",
        realm="realm1",
    )

    @component.on_join
    async def join(session, details):
        current_app.logger.info("Making RPC Call...")
        try:
            response = await asyncio.wait_for(session.call(topic,message), timeout=time)
            await SubscriptionHandler().on_rpc_response(session, response)
            
        except Exception as e:
            current_app.logger.error(f"RPC call failed: {e}")
            SubscriptionHandler.message = None
        finally:
            await component.stop()

    await component.start()
    return SubscriptionHandler.message


database_url = 'https://mailer.hertzai.com'

def save_conversation_db(text,user_id,prompt_id,database_url,request_id):
    headers = {'Content-Type': 'application/json'}
    data = {
        "request": 'VIDEO GENERATION FROM GENERATE_VIDEO',
        "response": text.strip(),
        "user_id": int(user_id),
        "conv_bot_name": 'GPT-4o',
        "topic": f'{prompt_id}',
        "revision": False,
        "dialogue_id": None,
        "card_type": 'Custom GPT',
        "qid": None,
        "layout_id": None,
        "layout_list": '[]',
        "request_token": 0,
        "response_token": 0,
        "request_id": request_id,
        "historical_request_id": str('[]')
    }
    res = requests.post("{}/conversation".format(database_url),
                        data=json.dumps(data), headers=headers).json()
    conv_id = res['conv_id']
    return conv_id


def get_role(user_id,prompt_id):
    creator = True if f'{user_id}_{prompt_id}' in agents_session.keys() else False
    role = None
    if creator:
        for i in agents_session[f'{user_id}_{prompt_id}']:
            if i['user_id'] == user_id:
                role = i['role']
                break
    if not role:
        if user_id in chat_joinees.keys():
            chat_creator_user_id = f"{chat_joinees[user_id][prompt_id]}_{prompt_id}"
            for i in agents_session[f"{chat_creator_user_id}"]:
                if i['user_id'] == user_id:
                    role = i['role']
                    break
    if not role:
        role = 'user'
    return role
    
def send_message_to_user1(user_id,response,inp,prompt_id):
    user_prompt = f'{user_id}_{prompt_id}'
    request_id = f'{request_id_list[user_prompt]}-intermediate'
    url = 'http://aws_rasa.hertzai.com:9890/autogen_response'
    body = json.dumps({'user_id':user_id,'message':response,'inp':inp,'request_id':request_id})
    headers = {'Content-Type': 'application/json'}
    res = requests.post(url,data=body,headers=headers)

def execute_python_file(task_description:str,user_id: int,prompt_id:int,action_entry_point:int=0):
    headers = {'Content-Type': 'application/json'}
    url = 'http://localhost:6777/time_agent'
    data = json.dumps({'task_description':task_description,'user_id':user_id,'prompt_id':prompt_id,'action_entry_point':action_entry_point,'request_from':'Reuse'})
    res = requests.post(url,data=data,headers=headers)
    return 'done'

def call_visual_task(task_description:str,user_id: int,prompt_id:int):
    headers = {'Content-Type': 'application/json'}
    url = 'http://localhost:6777/visual_agent'
    data = json.dumps({'task_description':task_description,'user_id':user_id,'prompt_id':prompt_id,'request_from':'Reuse'})
    res = requests.post(url,data=data,headers=headers)
    return 'done'

def time_based_execution(task_description:str,user_id: int,prompt_id:int,action_entry_point:int):
    current_app.logger.info(f'INSIDE TIME_BASED_EXECUTION with action_entry_point"{action_entry_point}')
    user_prompt = f'{user_id}_{prompt_id}'
    if user_prompt not in user_agents:
        current_app.logger.info('user_id is not present')
    else:
        #TODO use action_entry_point to give actions via chatinstructor by changing currnt action
        assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group = user_agents[user_prompt]
        # author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_id]
        current_time = datetime.now()
        text = f'This is the time now {current_time}\n you must perform this task {task_description}'
        result = time_user.initiate_chat(manager_1, message=text,speaker_selection={"speaker": "assistant"}, clear_history=False)
        last_message = group_chat.messages[-1]
        if last_message['content'] == 'TERMINATE':
            last_message = group_chat.messages[-2]
        #sending response to receiver agent
        send_message_to_user1(user_id,last_message,task_description,prompt_id)
    return 'done'

def visual_based_execution(task_description:str,user_id: int,prompt_id:int):
    current_app.logger.info(f'INSIDE Visual_BASED_EXECUTION')
    user_prompt = f'{user_id}_{prompt_id}'
    if user_prompt not in user_agents:
        current_app.logger.info('user_id is not present')
    else:
        #TODO use action_entry_point to give actions via chatinstructor by changing currnt action
        assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group = user_agents[user_prompt]
        current_time = datetime.now()
        text = f'''This is the time now {current_time}\n
        Instruction: If you want to send some message or information to user you should respond in this format {{"message_2_user":"Message here"}}\n
        you must perform this task: {task_description}'''
        manager = visual_agent_group['manager_2']
        user = visual_agent_group['visual_user']
        chat = visual_agent_group['group_chat_2']
        result = user.initiate_chat(manager, message=text,speaker_selection={"speaker": "assistant"}, clear_history=False)
        last_message = chat.messages[-1]
        if last_message['content'] == 'TERMINATE':
            last_message = chat.messages[-2]
        #sending response to receiver agent
        # send_message_to_user1(user_id,last_message,task_description,prompt_id)
    return 'done'

def get_frame(user_id):
    serialized_frame = redis_client.get(user_id)

    try:
        if serialized_frame is not None:
            frame_bgr = pickle.loads(serialized_frame)
            current_app.logger.info(
                f"Frame for user_id {user_id} retrieved successfully.")
            frame = frame_bgr[:, :, ::-1]
            return frame
        else:
            current_app.logger.info(f"No frame found for user_id {user_id}.")
            return None
    except ModuleNotFoundError as e:
        raise e
    

#TODO Reset action order after it reaches end.
def create_agents_for_role(user_id: str,prompt_id):

    config_list = [{
        "model": 'gpt-4o',
        "api_type": "azure",
        "api_key": '4xmi9X9pGCwRn2Pb0vldz6t6FQaAe29bUIkFjKRC7ytrVZ1Ni5cWJQQJ99BAACHYHv6XJ3w3AAABACOG99Zf',
        "base_url": 'https://hertzai-gpt4.openai.azure.com/',
        "api_version": "2024-02-15-preview",
        "price": [0.0025, 0.01]
    }]
    current_app.logger.info('INSIDE create_agents_for_role')

    # Create a basic function calling config
    llm_config = {
        "config_list": config_list,
        "cache_seed": None,
    }
    
    personas = []
    try:
        with open(f"prompts/{prompt_id}.json", 'r') as f:
            config = json.load(f)
            personas = config['personas']
            current_app.logger.info(f'Available Personas {personas}')
    except Exception as e:
        current_app.logger.info(e)
    if len(personas)>1: # & also check if we have record in db/agents_session to reuser
        temp = personas.copy()
        # temp.append({"name":"user","description":"User who will use this app"})
        agent_prompt = f'''You are a Helpful Assistant follow below action's
        initiate the conversation by asking which persona they belong to among the available personas: {temp} // give the persona names & ask to select one
        And then create new chat by calling the "update_persona" tool to update the records in db & return TERMINATE
        Note: only consider answers from User agent & the tool name is "update_persona" do not hallucinate the tool name. 
        '''
        assistant = autogen.AssistantAgent(
            name=f"assistant",
            llm_config=llm_config,
            max_consecutive_auto_reply=10,
            is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
            code_execution_config={"work_dir": "coding", "use_docker": False},
            system_message=agent_prompt
        )
        user_proxy = autogen.UserProxyAgent(
            name=f"user",
            human_input_mode="NEVER",
            llm_config=False,
            is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
            max_consecutive_auto_reply=0,
            code_execution_config=False,
        )
        helper = autogen.AssistantAgent(
            name="Helper",
            llm_config=llm_config,
            code_execution_config={"work_dir": "coding", "use_docker": False},
            system_message="""You Help the assistant agent to complete the task, you are helper agent not user/n
            if you get any request related you user redicrect that conversation to user don't asumer anything or answer anything on your own""",
            is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        )
        
        
        @helper.register_for_execution()
        @assistant.register_for_llm(api_style="function",description="update the role/persona in db")
        def update_persona(name: Annotated[str, "The persona name user selected"],
                           description: Annotated[str, "The persona description user selected"],
                           new: Annotated[bool, "Wethere it is a new chat or no"],
                           contact_number: Annotated[str, "user's contact of which we will join conversation"]) -> str:
            current_app.logger.info('INSIDE update_persona')
            current_app.logger.info(f'agents_session {agents_session}')
            current_app.logger.info(f'chat_joinees {chat_joinees}')
            if new:
                current_app.logger.info('Creating new chat')
                if f"{user_id}_{prompt_id}" not in agents_session.keys():
                    agents_session[f"{user_id}_{prompt_id}"] = [{'agentInstanceID':f'com.hertzai.hevolve.chat.{prompt_id}.{user_id}',
                                                'user_id':user_id,'role':name,'deviceID':'something'}]
                    agents_roles[f"{user_id}_{prompt_id}"] = {user_id:name}
                else:
                    agents_session[f"{user_id}_{prompt_id}"].append({'agentInstanceID':f'com.hertzai.hevolve.chat.{prompt_id}.{user_id}',
                                                'user_id':user_id,'role':name,'deviceID':'something'})
                    agents_roles[f"{user_id}_{prompt_id}"][user_id] = name
                current_app.logger.info(f'After persona update {agents_session[f"{user_id}_{prompt_id}"]}')
                return 'terminate'
            else:
                current_app.logger.info('adding in existing chat')
                if contact_number in temp_users.keys():
                    current_app.logger.info('user found with contact number')
                    if f"{temp_users[contact_number]}_{prompt_id}" in agents_session.keys():
                        current_app.logger.info('user found with contact number in agents_sessiion')
                        agents_session[f"{temp_users[contact_number]}_{prompt_id}"].append({'agentInstanceID':f'com.hertzai.hevolve.chat.{prompt_id}.{user_id}',
                                                    'user_id':user_id,'role':name,'deviceID':'something'})
                        agents_roles[f"{user_id}_{prompt_id}"][user_id] = name
                        current_app.logger.info('after append in agent_sessions')
                        chat_joinees[user_id] = {prompt_id : temp_users[contact_number]}
                        
                        current_app.logger.info(f'agents_session {agents_session}')
                        current_app.logger.info(f'chat_joinees {chat_joinees}')
                        return 'terminate'
                    else:
                        return f'Ask the user with contact number:{contact_number} to create a new chat'
                else:
                    current_app.logger.info('user found not with contact number')
                    return f'Ask the user with contact number:{contact_number} to create a new chat'
        
        
        assistant.description = 'Agent that is designed ask the roles to the user agent'
        user_proxy.description = 'agent will act as user & perform task assigned to user'
        helper.description = 'Agent will only work with assistant agent if needs help with something which is not related to user'
        
        
        def state_transition(last_speaker, groupchat):
            messages = groupchat.messages
            if last_speaker == user_proxy:
                return assistant
            if 'TERMINATE' in messages[-1]["content"].upper():
                current_app.logger.info('TERMINATING BECAUSE OF TERMINATE')
                # retrieve: action 1 -> action 2
                return None
            return "auto"
            
        select_speaker_transforms = transform_messages.TransformMessages(
            transforms=[
                transforms.MessageHistoryLimiter(max_messages=5),
                transforms.MessageTokenLimiter(max_tokens=3000, max_tokens_per_message=500, min_tokens=300),
            ]
        )
        group_chat = autogen.GroupChat(
            agents=[assistant, helper, user_proxy],
            messages=[],
            max_round=3,
            select_speaker_transform_messages=select_speaker_transforms,
            speaker_selection_method=state_transition,  # using an LLM to decide
            allow_repeat_speaker=False,  # Prevent same agent speaking twice
            send_introductions=False
        )
        
        manager = autogen.GroupChatManager(
            groupchat=group_chat,
            llm_config={"cache_seed": None,"config_list": config_list}
        )
        
        
        

        return assistant, user_proxy, group_chat, manager, helper,False
    else:
        agents_session[f"{user_id}_{prompt_id}"] = [{'agentInstanceID':f'com.hertzai.hevolve.chat.{prompt_id}.{user_id}',
                                                'user_id':user_id,'role':personas[0]['name'],'deviceID':'something'}]
        
        agents_roles[f"{user_id}_{prompt_id}"] = {user_id:personas[0]['name']}
        return 'TERMINATE','TERMINATE','TERMINATE','TERMINATE','TERMINATE', True


def create_agents_for_user(user_id: str,prompt_id) -> Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]:
    """Create new assistant & user proxy agents for a user with basic configuration."""
    user_prompt = f'{user_id}_{prompt_id}'
    # Create a basic function calling config
    llm_config = {
        "config_list": config_list,
        "cache_seed": None
    }
    
    personas = []
    # role = get_role(user_id,prompt_id)
    role_number,role = get_flow_number(user_id,prompt_id)
    
    with open(f"prompts/{prompt_id}_{role_number}_recipe.json", 'r') as f:
        config = json.load(f)
        recipes[user_prompt] = config
        final_recipe[prompt_id] = config
    goal = ''
    with open(f"prompts/{prompt_id}.json", 'r') as f:
            config = json.load(f)
            goal = config['goal']
    
    current_app.logger.info(f'Got goal as {goal}')
    role_actions = []
    actions = []
    current_app.logger.info(f'Getting role actions')
    for i in recipes[user_prompt]['actions']:
        current_app.logger.info(f'this is action persona:{i["persona"]} ')
        if i['persona'].lower() == role.lower():
            
            role_actions.append(i)
            actions.append(i['action'])
    # current_app.logger.info(f'role_actions: {role_actions}')
    # current_app.logger.info(f'will create timer agents with: {actions}')
    time_actions[user_prompt] = Action(actions)
    
    if len(role_actions) == 0:
        role_actions = recipes[user_prompt]['actions']
        
    # Perform topological sorting
    # sorted_actions = topological_sort(role_actions)
    user_tasks[user_prompt] = Action(role_actions)
    individual_recipe = []
    for i in range(1,(len(recipes[user_prompt]['actions'])+1)):
        current_app.logger.info(f'checking for prompts/{prompt_id}_{role_number}_{i}.json')
        try:
            with open(f"prompts/{prompt_id}_{role_number}_{i}.json", 'r') as f:
                config = json.load(f)
                individual_recipe.append(config)
        except Exception as e:
            current_app.logger.error(f'Got error as :{e} while checking for prompts/{prompt_id}_{role_number}_{i}.json')
    response_format = {"message_2_user": "Your message here"}
    agent_prompt = f'''You are a Helpful {role} Assistant. Your primary role is to assist the user efficiently while keeping all internal actions and processes hidden from the end user. Follow the guidelines below to perform tasks correctly:
        1. If you encounter a task you cannot perform, request assistance from the @Helper and @Executor agents. agent. If you need to run a tool, seek guidance from the @Helper agent. For code execution, ask the @Executor agent for assistance.
        2. Only execute actions where the persona is: {role}.
        3. Follow the steps below to achieve the goal: {goal}.
        4. Utilize the provided **Recipe** for all task-related details.
        5. After completing the current action, request the @statusVerifier agent to verify its completion. It will then provide the next action.
        6.  Always use the pre-tested steps and code from the provided Recipe—**do not create new implementations unless explicitly required**.
        7. **Scheduled, time-based, or continuous tasks should not be manually executed**—they are already handled by the system.
        8. **IMPORTANT CODING INSTRUCTION**: Avoid using `time.sleep` in any code.
        9. Tools Helper Agent can use [send_message_in_seconds,send_message_to_user,send_presynthesize_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory]
        10. **Never reveal actions, internal processes, or tools to the user**. Do not ask for user confirmation unless absolutely necessary(You can assume normal things like user's interests).
        11. **To communicate with the {role} user**, always use this format: `@user {response_format}`.
        12. All actions, recipes, and functions provided below have been reviewed and tested. Follow them exactly—do not make assumptions or modify them unless they fail or produce an error.
        13. Always request the next action from the @StatusVerifier agent—do not determine the next action on your own.
        14. If `can_perform_without_user_input` is `yes`, execute the action automatically without requesting user confirmation.
        15. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
        
        
        Actions: <actionsStart>{role_actions}<actionEnd>
        Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{individual_recipe}<generalized_functionsEnd><recipeEnd>
        When writing code, always print the final response just before returning it.
        Note: Other agents do not have access to these actions or recipe information. Ensure you provide them with the necessary context and related information to perform the required actions.
    '''
    if role == '':
        role = 'Assistant'
    else:
        role = f'{role}'
    assistant = autogen.AssistantAgent(
        name='Assistant',
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message=agent_prompt
    )
    
    # current_app.logger.info(f'creating agent with prompt {agent_prompt}')

    # Create the user proxy agent
    user_proxy = autogen.UserProxyAgent(
        name=f"User",
        human_input_mode="NEVER",
        llm_config=False,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        max_consecutive_auto_reply=0,
        code_execution_config=False,
    )
    helper = autogen.AssistantAgent(
        name="Helper",
        llm_config=llm_config,
        code_execution_config=False,
        system_message=f"""You are Helper Agent. Help the {role} agent to complete the task:
            1. Follow the steps below to achieve the goal: {goal}.
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than {role},Executor,multi_role_agent.
            4. Tools you have [txt2img, img2txt, save_data_in_memory, get_data_from_memory, get_user_id, get_prompt_id, Generate_video, get_user_uploaded_file, get_user_camera_inp, get_chat_history, create_scheduled_jobs, send_message_to_user,send_presynthesize_video_to_user] If a task cannot be completed using the available tools, first check the recipe. If no solution is found, create Python code to accomplish the task.
            5. Keep track of action and only ask for next action when the current action is completed successfully.
            6. Always use code from recipe given below.
            7. If there is any action which is like to perform a task continously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: @user {response_format}
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
            11. Always request the next action from the @StatusVerifier agent—do not determine the next action on your own.
            12. After completing the current action, request the @StatusVerifier agent to verify its completion. It will then provide the next action.
            
            Actions: <actionsStart>{role_actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{individual_recipe}<generalized_functionsEnd><recipeEnd>            
            
            When writing code, always print the final response just before returning it.
        """,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    executor = autogen.AssistantAgent(
        name="Executor",
        llm_config=llm_config,
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
        system_message=f'''You are a executor agent. focused solely on creating, running & debugging code.
            Your responsibilities:
            1. Follow the steps below to achieve the goal: {goal}.
            2. Use the provided Recipe for more details related to the actions.
            3. Ask @Helper to use the "send_message_to_roles" tool when contacting personas other than {role},Executor,multi_role_agent.
            4. Tools Helper Agent can use [send_message_in_seconds,send_message_to_user,send_presynthesize_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory]
            5. Keep track of action and only ask for next action when the current action is completed successfully.
            6. Always use code from recipe given below.
            7. If there is any action which is like to perform a task continously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: @user {response_format}
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
            11. Always request the next action from the @StatusVerifier agent—do not determine the next action on your own.
            12. After completing the current action, request the @StatusVerifier agent to verify its completion. It will then provide the next action.
            13. If you get any request to call a tool always ask @Helper to perfor it.
            Actions: <actionsStart>{role_actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{individual_recipe}<generalized_functionsEnd><recipeEnd>
        
            Note: Your Working Directory is "/home/hertzai2019/newauto/coding" use this if you need,
            Add proper error handling, logging.
            Always provide clear execution results or error messages to the assistant.
            if you get any conversation which is not related to coding ask the manager to route this conversation to user
            When writing code, always print the final response just before returning it.
        ''',
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    multi_role_agent = autogen.AssistantAgent(
        name="multi_role_agent",
        llm_config=llm_config,
        code_execution_config=False,
        system_message="""You will send message from multiple different personas your, job is to ask those question to assistant agent
        if you think some text was intent to give to some other agent but i came to you send the same message to user""",
    )
    verify = autogen.AssistantAgent(
        name="StatusVerifier",
        llm_config=llm_config,
        code_execution_config=False,
        system_message=""""You are an Status verification agent.
        Role: Track and verify the status of actions. Provide updates strictly in JSON format only when status is completed.
        Response formats:
            1. Action Completed Successfully: {"status": "completed","action": "current action","action_id": 1/2/3...,"message": "message here"}
            2. Action Error: {"status": "error","action": "current action","action_id": 1/2/3...,"message": "message here"}
            2. Action Pending: {"status": "pending","action": "current action","action_id": 1/2/3...,"message": "pending actions here"}
        Important Instructions:
            Only mark an action as "Completed" if the all the steps are successful completed. If any step is pending then mark the staus as pending and give the message.
            For pending tasks or ongoing actions, respond to helper to complete the task.
            Verify the action performed by assistant and make sure the action is performed correctly as per instructions. if action performed was not as per instructions give the pending actions to the helper agent.
            Report status only—do not perform actions yourself.
            
        """,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    chat_instructor = autogen.UserProxyAgent(
        name="ChatInstructor",
        human_input_mode="NEVER",
        max_consecutive_auto_reply=10,
        default_auto_reply="TERMINATE",
        code_execution_config=False,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    context_handling = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=50,keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=4000, max_tokens_per_message=1000, min_tokens=0),
            ToolMessageHandler(),
        ]
    )
    
    context_handling.add_to_agent(assistant)
    context_handling.add_to_agent(helper)
    context_handling.add_to_agent(executor)
    context_handling.add_to_agent(verify)
    
    # @executor.register_for_execution()
    # @helper.register_for_llm(api_style="function", description="sends message/ask questions to different roles/personas")
    # def send_message_to_roles(role: Annotated[str, "the role to which the message to send"], 
    #                         message: Annotated[str, "The question to ask or message to send"]) -> str:
    #     current_app.logger.info('INSIDE send_message_to_roles')
    #     if f"{user_id}_{prompt_id}" in agents_session.keys():
    #         for i in agents_session[f"{user_id}_{prompt_id}"]:
    #             if i['role'] == role:
    #                 current_app.logger.info(f'got role: {i}')
    #                 crossbar_message = i
    #                 crossbar_message['message'] = message
    #                 crossbar_message['caller_role'] = agents_roles[f"{user_id}_{prompt_id}"][user_id]
    #                 crossbar_message['caller_user_id'] = user_id
    #                 crossbar_message['caller_prompt_id'] = prompt_id
    #                 result = client.publish(
    #                     f"com.hertzai.hevolve.agent.multichat", crossbar_message)
    #                 current_app.logger.info('Published to chat')  
    #                 return 'Message sent Successfully'
    #         return 'Not able to send Message try again later' 
    #     elif user_id in chat_joinees.keys() and prompt_id in chat_joinees[user_id].keys():
    #         current_app.logger.info('contacting user with chat_joinees')
    #         current_app.logger.info(f'chat_joinees[user_id][prompt_id] {chat_joinees[user_id][prompt_id]}  prompt_id{prompt_id}')
    #         chat_creator_user_id = f"{chat_joinees[user_id][prompt_id]}_{prompt_id}"
    #         current_app.logger.info(f'chat_creator_user_id {chat_creator_user_id}')
    #         for i in agents_session[f"{chat_creator_user_id}"]:
    #             if i['role'] == role:
    #                 current_app.logger.info(f'got role: {i}')
    #                 crossbar_message = i
    #                 crossbar_message['message'] = message
    #                 crossbar_message['caller_role'] = agents_roles[chat_creator_user_id][user_id]
    #                 crossbar_message['caller_user_id'] = user_id
    #                 crossbar_message['caller_prompt_id'] = prompt_id
    #                 result = client.publish(
    #                     f"com.hertzai.hevolve.agent.multichat", crossbar_message)
    #                 current_app.logger.info(result)
    #                 current_app.logger.info('Published to chat') 
    #                 return 'Message sent Successfully'
    #         return 'Not able to send Message try again later' 
    # 
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Text to image Creator")
    def txt2img(text: Annotated[str, "Text to create image"]) -> str:
        current_app.logger.info('INSIDE txt2img')
        url = f"http://aws_rasa.hertzai.com:5459/txt2img?prompt={text}"

        payload = ""
        headers = {}

        response = requests.post(url, headers=headers, data=payload)
        return response.json()['img_url']
       
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Image to Text/Question Answering from image")
    def img2txt(image_url: Annotated[str, "image url of which you want text"],text: Annotated[str, "the details you want from image"]='Describe the Images & Text data in this image in detail') -> str:
        current_app.logger.info('INSIDE img2txt')
        url = "http://azurekong.hertzai.com:8000/llava/image_inference"

        payload = {
            'url': image_url,
            'prompt': text
        }
        files = []
        headers = {}

        response = requests.request(
            "POST", url, headers=headers, data=payload, files=files, timeout=300)
        if response.status_code == 200:
            return response.text
        else:
            return 'Not able to get this page details try later'
    
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Use this to Store and retrieve data using key-value storage system")
    def save_data_in_memory(key: Annotated[str, "Key path for storing data now & retrieving data later. Use dot notation for nested keys (e.g., 'user.info.name')."],
                            value: Annotated[Optional[str], "Value you want to store"] = None) -> str:
        current_app.logger.info('INSIDE save_data_in_memory')
        keys = key.split('.')
        d = agent_data.setdefault(prompt_id, {})
        
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value
        return f'{agent_data[prompt_id]}'
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Returns the schema of the json from internal memory with all keys but without actual values.")
    def get_saved_metadata() -> str:
        stripped_json = strip_json_values(agent_data[prompt_id])
        return f'{stripped_json}'

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Returns all data from the internal Memory using key")
    def get_data_by_key(key: Annotated[str, "Key path for retrieving data. Use dot notation for nested keys (e.g., 'user.info.name')."]) -> str:
        keys = key.split('.')
        d = agent_data.get(prompt_id, {})
        
        try:
            for k in keys:
                d = d[k]
            return f'{d}'
        except KeyError:
            return "Key not found in stored data."
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Returns the unique identifier (user_id) of the current user.")
    def get_user_id() -> str:
        current_app.logger.info('INSIDE get_user_id')
        return f'{user_id}'
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Returns the unique identifier (prompt_id) associated with the current prompt or conversation.")
    def get_prompt_id() -> str:
        current_app.logger.info('INSIDE get_prompt_id')
        return f'{prompt_id}'
    
    database_url = 'https://mailer.hertzai.com'
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Generate video with text and save it in database")
    def Generate_video(text: Annotated[str, "Text to be used for video generation"],
                       avatar_id: Annotated[str, "Unique identifier for the avatar"],
                       realtime: Annotated[bool,"If True, response is fast but less realistic by default it should be true; if False, response is realistic but slower"]) -> str:
        print('INSIDE Generate_video')
        database_url = 'https://mailer.hertzai.com'
        request_id = str(uuid.uuid4()).replace("-", "")[:11]
        print(f"avtar_id: {avatar_id}:\n{text[:10]}....\n")
        
        headers = {'Content-Type': 'application/json'}
        data = {}
        data["text"] = text
        data['flag_hallo'] = 'false'
        data['chattts'] = False
        data['openvoice'] = "false"
        try:
            res = requests.get("https://mailer.hertzai.com/get_image_by_id/{}".format(avatar_id))
            res = res.json()
            new_image_url = res["image_url"]
        except:
            data['openvoice'] = "true"
            new_image_url = None
            res = {'voice_id':None}
        data["cartoon_image"] = "True"
        data["bg_url"] = 'http://stream.mcgroce.com/txt/examples_cartoon/roy_bg.jpg'
        data['vtoonify'] = "false"
        data["image_url"] = new_image_url
        data['im_crop'] = "false"
        data['remove_bg'] = "false"
        data['hd_video'] = "false"
        data['uid'] = request_id
        data['gradient'] = "true"
        data['cus_bg'] = "false"
        data['solid_color'] = "false"
        data['inpainting'] = "false"
        data['prompt'] = ""
        data['gender'] = 'male'
        
        timeout = 60
        if not realtime:
            timeout = 600
            data['chattts'] = True
            data['flag_hallo'] = "true"
            data["cartoon_image"] = False
            
        if res['voice_id'] != None:
            voice_sample = requests.get(
                "{}/get_voice_sample_id/{}".format(database_url, res['voice_id']))
            voice_sample = voice_sample.json()
            data["audio_sample_url"] = voice_sample["voice_sample_url"]
            data['voice_id'] = res['voice_id']
        else:
            voice_sample = None
            data["audio_sample_url"] = None    
            data['voice_id'] = None
        conv_id = save_conversation_db(text,user_id,prompt_id,database_url,request_id)
        data['conv_id'] = conv_id
        data['avatar_id'] = avatar_id
        data['timeout'] = timeout
        try:
            video_link = requests.post("{}/video_generate_save".format(database_url),
                                        data=json.dumps(data), headers=headers, timeout=1)
        except:
            pass
        if data['chattts'] or data['flag_hallo'] == "true":
            return f"Video Generation task added to queue with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"
        else:
            return f"Video Generation completed with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"
        
        
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="get user's recent uploaded files")
    def get_user_uploaded_file() -> str:
        current_app.logger.info('INSIDE get_user_uploaded_file')
        if recent_file_id[user_id]:
            return f'Got user uploaded file the file_id is {recent_file_id[user_id]}'

        return 'No file uploaded from user'
    
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Get user's visual information to process somethings")
    def get_user_camera_inp(inp: Annotated[str, "The Question to check from visual context"]) -> str:
        request_id = 'Autogent_1234'
        current_app.logger.info('Using Vision to answer question')
        frame = get_frame(str(user_id))
        if frame is not None:
            image_path = f"output_images/{user_id}_{request_id}_call.jpg"
            # Ensure the directory exists
            directory = os.path.dirname(image_path)
            if not os.path.exists(directory):
                os.makedirs(directory)
            # Convert the frame (which is a NumPy array) to a PIL image
            image = Image.fromarray(frame)
            # Save the image
            image.save(image_path)
            url = "http://azurekong.hertzai.com:8000/minicpm/upload"
            payload = {
                'prompt': f'Instruction: Respond in second person point of view\ninput:-{inp}'}
            files = [
                ('file', ('call.jpg', open(image_path, 'rb'), 'image/jpeg'))
            ]
            headers = {}
            try:
                response = requests.post(
                    url, headers=headers, data=payload, files=files)
                current_app.logger.info(response.text)
                response = response.text

                return response
            except Exception as e:
                current_app.logger.info('ERROR: Got error in visal QA')
                return 'failed to get visual context ask user to check if the camera is turned on'
    

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Get Chat history based on text & start & end date")
    def get_chat_history(text: Annotated[str, "Text related to which you want history"],start: Annotated[str, "start date in format %Y-%m-%dT%H:%M:%S.%fZ"],end: Annotated[str, "end date in format %Y-%m-%dT%H:%M:%S.%fZ"]) -> str:
        current_app.logger.info('INSIDE get_chat_history')
        return get_time_based_history(text, f'user_{user_id}', start, end)
    
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Creates time-based jobs using APScheduler to schedule jobs")
    def create_scheduled_jobs(cron_expression: Annotated[str, "Cron expression for scheduling. Example: '0 9 * * 1-5' (Runs at 9:00 AM, Monday to Friday)."], 
                            job_description: Annotated[str, "Description of the job to be performed"]) -> str:
        current_app.logger.info('INSIDE create_scheduled_jobs')
        if not scheduler.running:
            scheduler.start()
        
        try:
            trigger = CronTrigger.from_crontab(cron_expression)
            job_id = f"job_{int(time.time())}"
            scheduler.add_job(execute_python_file, trigger=trigger, id=job_id, args=[job_description, user_id,prompt_id,0])
            current_app.logger.info('Successfully created scheduler job')
            return 'Successfully created scheduler job'
        except Exception as e:
            current_app.logger.info(f'Error in create_scheduled_jobs: {str(e)}')
            return f"Error creating scheduled job: {str(e)}"
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Sends a message/information to user. You can use this if you want to ask a question")
    def send_message_to_user(text: Annotated[str, "Text to send to the user"],
                         avatar_id: Annotated[Optional[str], "Unique identifier for the avatar"] = None,
                         response_type: Annotated[Optional[str], "Response mode: 'Realistic' (slower, better quality) or 'Realtime' (faster, lower quality)"] = 'Realtime') -> str:
        current_app.logger.info('INSIDE send_message_to_user')
        current_app.logger.info(f'SENDING DATA 2 user with values text:{text}, avatar_id:{avatar_id}, response_type:{response_type}')
        request_id = str(uuid.uuid4()).replace("-", "")[:11]
        #TODO add avatar_id and conv_id and response_type
        thread = threading.Thread(target=send_message_to_user1, args=(user_id, text, '', f'{request_id_list[user_prompt]}-intermediate'))
        thread.start()
        return f'Message sent successfully to user with request_id: {request_id_list[user_prompt]}-intermediate'
    
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Sends a presynthesized message/video/dialogue to user using conv_id from memory.")
    def send_presynthesize_video_to_user(conv_id: Annotated[str, "Conversation ID associated with the text from memory"]) -> str:
        current_app.logger.info('INSIDE send_presynthesize_video_to_user')
        current_app.logger.info(f'SENDING DATA 2 user with value: conv_id:{conv_id}.')
        return 'Message sent successfully to user'
    
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Sends a presynthesized message/video/dialogue to user using conv_id with a timer.")
    def send_message_in_seconds(text: Annotated[str, "text to send to user"],
                       delay: Annotated[int, "time to wait in seconds before sending text"],
                       conv_id: Annotated[Optional[int], "conv_id for this text if not available make it None"],) -> str:
        current_app.logger.info('INSIDE send_message_in_seconds')
        current_app.logger.info(f'with text:{text}. and waiting time: {delay} conv_id: {conv_id}')
        run_time = datetime.fromtimestamp(time.time() + delay)
        scheduler.add_job(send_message_to_user1, 'date', run_date=run_time, args=[user_id, text, '',prompt_id])
        return 'Message scheduled successfully'
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Retrieve the user's visual camera input from the past specified minutes.")
    def get_user_camera_inp_by_mins(minutes: Annotated[int, "Time range (in minutes) for fetching the camera visual data. for e.g. 5 will get you last 5 mins data"]) -> str:
        current_app.logger.info('INSIDE get user camera inp by mins')
        current_app.logger.info(f'CHECKING FOR VIDEO FOR PAST {minutes} MINS')
        visual_context = helper_fun.get_visual_context(user_id,minutes)
        current_app.logger.info(f'GOT RESPONSE AS {visual_context}')
        if not visual_context:
            visual_context = 'User\'s camera is not on. no visual data'
        return 'Message scheduled successfully'
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Processes user-defined commands on a personal Windows or Android system.")
    async def execute_windows_command(instructions: Annotated[str, "Command in plain English to execute on the Windows machine"])->str:
        """
        Executes a command on a Windows machine and returns the response within 500 seconds.
        """
        try:
            current_app.logger.info('INSIDE execute_windows_command')
            topic = f'com.hertzai.hevolve.action.{user_id}'
            current_app.logger.info(f'calling {topic} for 5 second')
            response = await subscribe_and_return({'prompt_id':prompt_id},topic,5)  # Wait for the RPC response
            current_app.logger.info(f'Response from call of {topic}: {response}')
            if not response:
                return 'Ask user to to go to hertzai.com login and start the windows companion app'
            crossbar_message = {
                'parent_request_id': request_id_list[user_prompt],
                'user_id': f'{user_id}',
                'prompt_id': '54',
                'instruction_to_vlm_agent': instructions,
                'os_to_control': 'Windows',
                'actions_available_in_os': [],
                'max_ETA_in_seconds': 500,
                'langchain_server':True
            }
            topic = 'com.hertzai.hevolve.action'
            current_app.logger.info(f'calling {topic} for 8000 second')
            response = await subscribe_and_return(crossbar_message,topic)  # Wait for the RPC response
            current_app.logger.info(f'THIS IS RESPONSE type: {type(response)} value: {response}')
            if response['status'] == 'success':
                return 'successfully ran the command in user\' computer.'
            else:
                return 'Not able to perform this action now please try later'
        except Exception as e:
            error_message = traceback.format_exc()  # Capture full traceback
            current_app.logger.error(f"Error executing command:\n{error_message}")
            return {"error": e}

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Get google search response")
    def google_search(text: Annotated[str, "Text which you want to search"]) -> str:
        current_app.logger.info('INSIDE google search')
        return helper_fun.top5_results(text)
    
    time_agent = autogen.AssistantAgent(
        name='time_agent',
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message="You are an helpful AI assistant used to perform time based tasks given to you. "
        f"""You can refer below details to perform task:
            Actions: <actionsStart>{role_actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{individual_recipe}<generalized_functionsEnd><recipeEnd>
        
        """
        f"When you want to communicate with {role} connect main agent using 'connect_time_main' tool."
        "Tools Helper Agent can use [send_message_in_seconds,send_message_to_user,send_presynthesize_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory.]"
        "if you have any task which is not doable by these tool check recipe first else create python code to do so"
        "the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video."
        f"IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: @user {response_format}"
        "Return 'TERMINATE' when the task is done."
    )
    
    time_user = autogen.UserProxyAgent(
        name=f"user_proxy_{user_id}",
        human_input_mode="NEVER",
        llm_config=False,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        max_consecutive_auto_reply=0,
        code_execution_config=False,
    )
    helper1 = autogen.AssistantAgent(
        name="Helper",
        llm_config=llm_config,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message=f"""You are Helper Agent. Help the {role} agent to complete the task:
            1. Follow the steps below to achieve the goal: {goal}.
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than {role},Executor,multi_role_agent.
            4. Tools you have [txt2img, img2txt, save_data_in_memory, get_data_from_memory, get_user_id, get_prompt_id, Generate_video, get_user_uploaded_file, get_user_camera_inp, get_chat_history, create_scheduled_jobs] if you have any task which is not doable by these tool check recipe first else create python code to do so
            5. Keep track of action and only go to next action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: @user {response_format}
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
            Actions: <actionsStart>{role_actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{individual_recipe}<generalized_functionsEnd><recipeEnd>            
            
            When writing code, always print the final response just before returning it.
        """,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    executor1 = autogen.AssistantAgent(
        name="Executor",
        llm_config=llm_config,
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
        system_message=f'''You are a executor agent. focused solely on creating, running & debugging code.
            Your responsibilities:
            1. Follow the steps below to achieve the goal: {goal}.
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than {role},Executor,multi_role_agent.
            4. Tools Helper Agent can use [send_message_in_seconds,send_message_to_user,send_presynthesize_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory]
            5. Keep track of action and only go to next action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: @user {response_format}
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
            Actions: <actionsStart>{role_actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{individual_recipe}<generalized_functionsEnd><recipeEnd>
        
            Note: Your Working Directory is "/home/hertzai2019/newauto/coding" use this if you need,
            Add proper error handling, logging.
            Always provide clear execution results or error messages to the assistant.
            if you get any conversation which is not related to coding ask the manager to route this conversation to user
            When writing code, always print the final response just before returning it.
        ''',
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    multi_role_agent1 = autogen.AssistantAgent(
        name="multi_role_agent",
        llm_config=llm_config,
        code_execution_config=False,
        system_message="""You will send message from multiple different personas your, job is to ask those question to assistant agent
        if you think some text was intent to give to some other agent but i came to you send the same message to user""",
    )
    verify1 = autogen.AssistantAgent(
        name="StatusVerifier",
        llm_config=llm_config,
        code_execution_config=False,
        system_message=""""You are an Status verification agent.
        Role: Track and verify the status of actions. Provide updates strictly in JSON format only when status is completed.
        Response formats:
            1. Action Completed Successfully: {"status": "completed","action": "current action","action_id": 1/2/3...,"message": "message here"}
            2. Action Error: {"status": "error","action": "current action","action_id": 1/2/3...,"message": "message here"}
            2. Action Pending: {"status": "pending","action": "current action","action_id": 1/2/3...,"message": "pending actions here"}
        Important Instructions:
            Only mark an action as "Completed" if the Assistant Agent confirms successful completion.
            For pending tasks or ongoing actions, respond to helper to complete the task.
            Verify the action performed by assistant and make sure the action is performed correctly as per instructions. if action performed was not as per instructions give the pending actions to the helper agent.
            Report status only—do not perform actions yourself.
            
        """,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    chat_instructor1 = autogen.UserProxyAgent(
        name="ChatInstructor",
        human_input_mode="NEVER",
        max_consecutive_auto_reply=10,
        default_auto_reply="TERMINATE",
        code_execution_config=False,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    context_handling = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=50,keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=4000, max_tokens_per_message=1000, min_tokens=0),
            ToolMessageHandler(),
        ]
    )
    context_handling.add_to_agent(time_agent)
    context_handling.add_to_agent(helper1)
    context_handling.add_to_agent(executor1)
    context_handling.add_to_agent(multi_role_agent1)
    context_handling.add_to_agent(verify1)
    
    ##Tools call
    helper1.register_for_llm(name="txt2img", description="Text to image Creator")(txt2img)
    time_agent.register_for_execution(name="txt2img")(txt2img)
    helper1.register_for_llm(name="img2txt", description="Image to Text/Question Answering from image")(img2txt)
    time_agent.register_for_execution(name="img2txt")(img2txt)  
    helper1.register_for_llm(name="save_data_in_memory", description="Use this to Store and retrieve data using key-value storage system")(save_data_in_memory)
    time_agent.register_for_execution(name="save_data_in_memory")(save_data_in_memory)  
    helper1.register_for_llm(name="get_data_by_key", description="Returns all data from the internal Memory")(get_data_by_key)
    time_agent.register_for_execution(name="get_data_by_key")(get_data_by_key)  
    helper1.register_for_llm(name="get_user_id", description="Returns the unique identifier (user_id) of the current user.")(get_user_id)
    time_agent.register_for_execution(name="get_user_id")(get_user_id)  
    helper1.register_for_llm(name="get_prompt_id", description="Returns the unique identifier (prompt_id) associated with the current prompt or conversation.")(get_prompt_id)
    time_agent.register_for_execution(name="get_prompt_id")(get_prompt_id)  
    helper1.register_for_llm(name="Generate_video", description="Generate video with text and save it in database")(Generate_video)
    time_agent.register_for_execution(name="Generate_video")(Generate_video)  
    helper1.register_for_llm(name="get_user_uploaded_file", description="get user's recent uploaded files")(get_user_uploaded_file)
    time_agent.register_for_execution(name="get_user_uploaded_file")(get_user_uploaded_file)  
    helper1.register_for_llm(name="get_user_camera_inp", description="Get user's visual information to process somethings")(get_user_camera_inp)
    time_agent.register_for_execution(name="get_user_camera_inp")(get_user_camera_inp)  
    helper1.register_for_llm(name="create_scheduled_jobs", description="Creates time-based jobs using APScheduler to schedule jobs")(create_scheduled_jobs)
    time_agent.register_for_execution(name="create_scheduled_jobs")(create_scheduled_jobs)  
    helper1.register_for_llm(name="get_chat_history", description="Get Chat history based on text & start & end date")(get_chat_history)
    time_agent.register_for_execution(name="get_chat_history")(get_chat_history)  
    helper1.register_for_llm(name="send_message_to_user", description="Send Message to User")(send_message_to_user)
    time_agent.register_for_execution(name="send_message_to_user")(send_message_to_user)  
    helper1.register_for_llm(name="send_presynthesize_video_to_user", description="Sends a presynthesized message/video/dialogue to user using conv_id.")(send_presynthesize_video_to_user)
    time_agent.register_for_execution(name="send_presynthesize_video_to_user")(send_presynthesize_video_to_user)
    helper1.register_for_llm(name="send_message_in_seconds", description="Sends a presynthesized message/video/dialogue to user using conv_id with a timer.")(send_message_in_seconds)
    time_agent.register_for_execution(name="send_message_in_seconds")(send_message_in_seconds)
    
    helper1.register_for_llm(name="execute_windows_command", description="Executes a command on a Windows machine and returns the response.")(execute_windows_command)
    time_agent.register_for_execution(name="execute_windows_command")(execute_windows_command)
    
    helper1.register_for_llm(name="google_search", description="Get google search response")(google_search)
    time_agent.register_for_execution(name="google_search")(google_search)
    
    
    def connect_time_main(message: Annotated[str, "The message time agent want to send to main agent"]) -> str:
        message = f"Role: Time Agent\n Message: {message}"
        print(f'user_id {user_id}')
        user_prompt = f'{user_id}_{prompt_id}'
        assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group = user_agents[user_prompt]
        response = multi_role_agent.initiate_chat(manager, message=message,speaker_selection={"speaker": "assistant"}, clear_history=False)
        last_message = group_chat.messages[-1]
        if last_message['content'] == 'TERMINATE':
            last_message = group_chat.messages[-2]
        #sending response to receiver agent
        send_message_to_user1(user_id,last_message,'',prompt_id)
        
        text = f'The Response from main Agent: {last_message}'
        result = time_user.initiate_chat(manager_1, message=text,speaker_selection={"speaker": "assistant"}, clear_history=False)
        last_message = group_chat.messages[-1]
        if last_message['content'] == 'TERMINATE':
            last_message = group_chat.messages[-2]
        send_message_to_user1(user_id,last_message,'',prompt_id)
        return 'Done'
        
    # Register the tool signature with the assistant agent.
    helper1.register_for_llm(name="Connect_to_main_agent", description="Connects time agent to main assistant agemt to perform actions which time agent cannot perform")(connect_time_main)

    # Register the tool function with the user proxy agent.
    time_agent.register_for_execution(name="Connect_to_main_agent")(connect_time_main)  
    
    
    visual_agent, visual_user, helper2, executor2, multi_role_agent2, verify2, chat_instructor2 = helper_fun.create_visual_agent(user_id,prompt_id)
    
    ##Tools call
    helper2.register_for_llm(name="txt2img", description="Text to image Creator")(txt2img)
    visual_agent.register_for_execution(name="txt2img")(txt2img)
    helper2.register_for_llm(name="img2txt", description="Image to Text/Question Answering from image")(img2txt)
    visual_agent.register_for_execution(name="img2txt")(img2txt)  
    helper2.register_for_llm(name="save_data_in_memory", description="Use this to Store and retrieve data using key-value storage system")(save_data_in_memory)
    visual_agent.register_for_execution(name="save_data_in_memory")(save_data_in_memory)  
    helper2.register_for_llm(name="get_data_by_key", description="Returns all data from the internal Memory")(get_data_by_key)
    visual_agent.register_for_execution(name="get_data_by_key")(get_data_by_key)  
    helper2.register_for_llm(name="get_user_id", description="Returns the unique identifier (user_id) of the current user.")(get_user_id)
    visual_agent.register_for_execution(name="get_user_id")(get_user_id)  
    helper2.register_for_llm(name="get_prompt_id", description="Returns the unique identifier (prompt_id) associated with the current prompt or conversation.")(get_prompt_id)
    visual_agent.register_for_execution(name="get_prompt_id")(get_prompt_id)  
    helper2.register_for_llm(name="Generate_video", description="Generate video with text and save it in database")(Generate_video)
    visual_agent.register_for_execution(name="Generate_video")(Generate_video)  
    helper2.register_for_llm(name="get_user_uploaded_file", description="get user's recent uploaded files")(get_user_uploaded_file)
    visual_agent.register_for_execution(name="get_user_uploaded_file")(get_user_uploaded_file)  
    helper2.register_for_llm(name="get_user_camera_inp", description="Get user's visual information to process somethings")(get_user_camera_inp)
    visual_agent.register_for_execution(name="get_user_camera_inp")(get_user_camera_inp)  
    helper2.register_for_llm(name="create_scheduled_jobs", description="Creates time-based jobs using APScheduler to schedule jobs")(create_scheduled_jobs)
    visual_agent.register_for_execution(name="create_scheduled_jobs")(create_scheduled_jobs)  
    helper2.register_for_llm(name="get_chat_history", description="Get Chat history based on text & start & end date")(get_chat_history)
    visual_agent.register_for_execution(name="get_chat_history")(get_chat_history)  
    helper2.register_for_llm(name="send_message_to_user", description="Send Message to User")(send_message_to_user)
    visual_agent.register_for_execution(name="send_message_to_user")(send_message_to_user)  
    helper2.register_for_llm(name="send_presynthesize_video_to_user", description="Sends a presynthesized message/video/dialogue to user using conv_id.")(send_presynthesize_video_to_user)
    visual_agent.register_for_execution(name="send_presynthesize_video_to_user")(send_presynthesize_video_to_user)
    helper2.register_for_llm(name="send_message_in_seconds", description="Sends a presynthesized message/video/dialogue to user using conv_id with a timer.")(send_message_in_seconds)
    visual_agent.register_for_execution(name="send_message_in_seconds")(send_message_in_seconds)
    helper2.register_for_llm(name="execute_windows_command", description="Executes a command on a Windows machine and returns the response.")(execute_windows_command)
    visual_agent.register_for_execution(name="execute_windows_command")(execute_windows_command)
    helper2.register_for_llm(name="google_search", description="Get google search response")(google_search)
    visual_agent.register_for_execution(name="google_search")(google_search)
    
    
    
    assistant.description = 'Designed to handle specific tasks by interacting directly with other agents or the user. It acts as the primary orchestrator for task management and ensures tasks are completed efficiently'
    user_proxy.description = 'Acts as a user, performing tasks assigned by the Assistant Agent. It simulates user actions and provides results or feedback as required.'
    helper.description = 'Athis is a helper agent that calls tools, facilitates task completion & assists other agents it cal perform tools/function like [send_message_in_seconds,send_message_to_user,send_presynthesize_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory] calls and supporting backend processes. '
    multi_role_agent.description = 'Acts as an external agent with multi-functional capabilities. Note: This agent should never be directly invoked.'
    executor.description = 'A specialized agent responsible for executing code and handling response management. It ensures computational tasks are performed accurately and returns results effectively.'
    verify.description = 'this is a verify status agent. which will verify the status of current action.'
    
    
    time_agent.description = 'Designed to handle specific tasks by interacting directly with other agents or the user. It acts as the primary orchestrator for task management and ensures tasks are completed efficiently'
    time_user.description = 'Acts as a user, performing tasks assigned by the Assistant Agent. It simulates user actions and provides results or feedback as required.'
    helper1.description = 'Athis is a helper agent that calls tools, facilitates task completion & assists other agents it cal perform tools/function like [send_message_in_seconds,send_message_to_user,send_presynthesize_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory] calls and supporting backend processes. '
    executor1.description = 'A specialized agent responsible for executing code and handling response management. It ensures computational tasks are performed accurately and returns results effectively.'
    
    
    visual_agent.description = 'Designed to handle specific tasks by interacting directly with other agents or the user. It acts as the primary orchestrator for task management and ensures tasks are completed efficiently'
    visual_user.description = 'Acts as a user, performing tasks assigned by the Assistant Agent. It simulates user actions and provides results or feedback as required.'
    helper2.description = 'Athis is a helper agent that calls tools, facilitates task completion & assists other agents it cal perform tools/function like [send_message_in_seconds,send_message_to_user,send_presynthesize_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory] calls and supporting backend processes. '
    executor2.description = 'A specialized agent responsible for executing code and handling response management. It ensures computational tasks are performed accurately and returns results effectively.'
    
    
    
    
    def state_transition(last_speaker, groupchat):        
        messages = groupchat.messages
        try:
            pattern = r'\{.*?\}' # getting all json from text
            matches = re.findall(pattern, messages[-1]["content"], re.DOTALL)   
            json_objects = [json.loads(match) for match in matches]
            current_app.logger.info(f'Got Json as {len(json_objects)}')
            if json_objects:
                last_json = json_objects[-1]
                current_app.logger.info(f'last json as {last_json}')
                if 'status' in last_json.keys() and last_json['status'].lower() == 'completed':
                    current_app.logger.info('GOT COMPLETED FOR ACTION')
                    try:
                        user_tasks[user_prompt].current_action = json_objects['action_id']
                    except:
                        current_app.logger.error('GOT ERROR WHILE UPDATING CURRENT ACTION')
                        user_tasks[user_prompt].current_action += 1
                    return chat_instructor
                    
                currentaction_id = last_json['action_id']
                if individual_recipe[currentaction_id-1]['can_perform_without_user_input'] == 'yes':
                    return assistant
        except Exception as e:
            current_app.logger.error(f'Got Error while getting json for current actionid: {e}')
            
        pattern3 = r"@statusverifier"
        if re.search(pattern3, messages[-1]["content"].lower()):
            current_app.logger.info("String contains @StatusVerifier returnig StatusVerifier")
            return verify
        pattern3 = r"@helper"
        if re.search(pattern3, messages[-1]["content"].lower()):
            current_app.logger.info("String contains @Helper returnig Helper")
            return helper
        pattern3 = r"@executor"
        if re.search(pattern3, messages[-1]["content"].lower()):
            current_app.logger.info("String contains @Executor returnig Executor")
            return executor
        
        # llm_call_track[user_prompt]['count'] +=1
        # current_app.logger.info(f"llm_call_track[user_prompt]['count']:{llm_call_track[user_prompt]['count']}")
        # if llm_call_track[user_prompt]['original_prompt'] == True:
        #     llm_call_track[user_prompt]['original_prompt'] = False
        #     assistant.update_system_message = agent_prompt
            
        # if llm_call_track[user_prompt]['count'] == 5:
        #     current_app.logger.info('LLM CALL COUNT IS 5')
        #     llm_call_track[user_prompt]['count'] = 0
        #     llm_call_track[user_prompt]['original_prompt'] = True
        #     assistant.update_system_message = f"You should return the response to the user on whatever you are doing now in response format {response_format}"
        #     current_app.logger.info('Updated prompt')
        #     return assistant
        current_app.logger.info(f'Inside state_transition with message :10 {messages[-1]["content"][:10]} & last_speaker {last_speaker.name}')
        if last_speaker.name == f"user_proxy_{user_id}" or last_speaker.name == "multi_role_agent" or last_speaker.name == "helper" or last_speaker.name == "Executor" or last_speaker.name == "ChatInstructor":
            return assistant
        current_app.logger.info(f'Checking for @user or @user in message')
        if 'message_2_user' in messages[-1]["content"].lower():
            current_app.logger.info('GOT @USER in message')
            temp_message = messages[-1]["content"]
            temp_message = temp_message.replace("'",'"')
            json_match = re.search(r'{[\s\S]*}', temp_message)
            if json_match:
                try:
                    current_app.logger.info('GOT Json')
                    current_app.logger.info(f'got json object')
                    json_part = json_match.group(0)
                    current_app.logger.info('Sending user the message')
                    json_obj = json.loads(json_part)
                    send_message_to_user1(user_id,json_obj['message_2_user'],'',prompt_id)
                except:
                    pass
                return "auto"
            
        if messages[-1]["role"] == 'function':
            current_app.logger.info('The last speaker was function returning assistant') 
            return assistant
        if 'exitcode:' in messages[-1]["content"]:
            current_app.logger.info('Got exitcode in text returning assistant')
            return assistant
        if 'TERMINATE' in messages[-1]["content"].upper():
            current_app.logger.info('TERMINATING BECAUSE OF TERMINATE')
            # retrieve: action 1 -> action 2
            return None
        return "auto"
    
    def state_transition1(last_speaker, groupchat):
        current_app.logger.info('INSIDE TIMER STATE TRANSITION')      
        messages = groupchat.messages
        # visual_context = helper_fun.get_visual_context(user_id)
        # if visual_context:
        #     groupchat.messages.insert(-1,{'content':visual_context,'role':'user','name':'helper'})
        try:
            pattern = r'\{.*?\}' # getting all json from text
            matches = re.findall(pattern, messages[-1]["content"], re.DOTALL)   
            json_objects = [json.loads(match) for match in matches]
            current_app.logger.info(f'Got Json as {len(json_objects)}')
            if json_objects:
                last_json = json_objects[-1]
                current_app.logger.info(f'last json as {last_json}')
                if 'status' in last_json.keys() and last_json['status'].lower() == 'completed':
                    current_app.logger.info('GOT COMPLETED FOR ACTION')
                    try:
                        time_actions[user_prompt].current_action += 1
                    except:
                        current_app.logger.error('GOT ERROR WHILE UPDATING CURRENT ACTION')
                        time_actions[user_prompt].current_action += 1
                    return chat_instructor1
                    
                currentaction_id = last_json['action_id']
                if final_recipe[prompt_id]['actions'][currentaction_id-1]['can_perform_without_user_input'] == 'yes':
                    return time_agent
        except Exception as e:
            current_app.logger.error(f'Got Error while getting json for current actionid: {e}')
            
        pattern3 = r"@statusverifier"
        if re.search(pattern3, messages[-1]["content"].lower()):
            current_app.logger.info("String contains @StatusVerifier returnig StatusVerifier")
            return verify1
    
        current_app.logger.info(f'Inside state_transition with message :10 {messages[-1]["content"][:10]} & last_speaker {last_speaker.name}')
        if last_speaker.name == f"user_proxy_{user_id}" or last_speaker.name == "multi_role_agent" or last_speaker.name == "Helper" or last_speaker.name == "Executor":
            return time_agent
        current_app.logger.info(f'Checking for @user or @user in message')
        if 'message_2_user' in messages[-1]["content"].lower():
            current_app.logger.info('GOT @USER in message')
            temp_message = messages[-1]["content"]
            temp_message = temp_message.replace("'",'"')
            json_match = re.search(r'{[\s\S]*}', temp_message)
            if json_match:
                try:
                    current_app.logger.info('GOT Json')
                    current_app.logger.info(f'got json object')
                    json_part = json_match.group(0)
                    current_app.logger.info('Sending user the message')
                    json_obj = json.loads(json_part)
                    send_message_to_user1(user_id,json_obj['message_2_user'],'',prompt_id)
                except:
                    pass
                return "auto"
            
        if messages[-1]["role"] == 'function':
            current_app.logger.info('The last speaker was function returning assistant') 
            return time_agent
        if 'exitcode:' in messages[-1]["content"]:
            current_app.logger.info('Got exitcode in text returning assistant')
            return time_agent
        if 'TERMINATE' in messages[-1]["content"].upper():
            current_app.logger.info('TERMINATING BECAUSE OF TERMINATE')
            # retrieve: action 1 -> action 2
            return None
        return "auto"
    
    def state_transition2(last_speaker, groupchat):
        current_app.logger.info('INSIDE VISUAL STATE TRANSITION')      
        messages = groupchat.messages
        # visual_context = helper_fun.get_visual_context(user_id)
        # if visual_context:
        #     groupchat.messages.insert(-1,{'content':visual_context,'role':'user','name':'helper'})
        
        # current_app.logger.info('CHECKING FOR VIDEO FOR PAST 5MINS')
        # visual_context = helper_fun.get_visual_context(user_id)
        # current_app.logger.info(f'GOT RESPONSE AS {visual_context}')
        # if visual_context:
        #     groupchat.messages.insert(-2,{'content':visual_context,'role':'user','name':'helper'})
        # current_app.logger.info(f'{messages[-1]}'
        current_app.logger.info(f'Checking for @user or @user in message')
        if 'message_2_user' in messages[-1]["content"].lower():
            current_app.logger.info('GOT @USER in message')
            temp_message = messages[-1]["content"]
            temp_message = temp_message.replace("'",'"')
            json_match = re.search(r'{[\s\S]*}', temp_message)
            if json_match:
                try:
                    current_app.logger.info('GOT Json')
                    current_app.logger.info(f'got json object')
                    json_part = json_match.group(0)
                    current_app.logger.info('Sending user the message')
                    json_obj = json.loads(json_part)
                    send_message_to_user1(user_id,json_obj['message_2_user'],'',prompt_id)
                except:
                    pass
        
        pattern3 = r"@statusverifier"
        if re.search(pattern3, messages[-1]["content"].lower()):
            current_app.logger.info("String contains @StatusVerifier returnig StatusVerifier")
            return verify2
    
        current_app.logger.info(f'Inside state_transition with message :10 {messages[-1]["content"][:10]} & last_speaker {last_speaker.name}')
        if last_speaker.name == f"UserProxy" or last_speaker.name == "multi_role_agent" or last_speaker.name == "Helper" or last_speaker.name == "Executor":
            return visual_agent
            
        if messages[-1]["role"] == 'function':
            current_app.logger.info('The last speaker was function returning assistant') 
            return visual_agent
        if 'exitcode:' in messages[-1]["content"]:
            current_app.logger.info('Got exitcode in text returning assistant')
            return visual_agent
        if 'TERMINATE' in messages[-1]["content"].upper():
            current_app.logger.info('TERMINATING BECAUSE OF TERMINATE')
            # retrieve: action 1 -> action 2
            return None
        return "auto"
    
    
    select_speaker_transforms = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=50,keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=4000, max_tokens_per_message=1000, min_tokens=0),
            ToolMessageHandler(),
        ]
    )
    group_chat = autogen.GroupChat(
        agents=[assistant, helper, user_proxy,multi_role_agent,executor,chat_instructor,verify],
        messages=[],
        max_round=10,
        select_speaker_transform_messages=select_speaker_transforms,
        speaker_selection_method=state_transition,  # using an LLM to decide
        allow_repeat_speaker=False,  # Prevent same agent speaking twice
        send_introductions=False
    )
    
    manager = autogen.GroupChatManager(
        groupchat=group_chat,
        llm_config={"cache_seed": None,"config_list": config_list}
    )
    
    group_chat_1 = autogen.GroupChat(
        agents=[time_agent, helper1, time_user,multi_role_agent1,executor1,chat_instructor1,verify1],
        messages=[],
        max_round=10,
        select_speaker_transform_messages=select_speaker_transforms,
        speaker_selection_method=state_transition1,  # using an LLM to decide
        allow_repeat_speaker=False,  # Prevent same agent speaking twice
        send_introductions=False
    )
    
    manager_1 = autogen.GroupChatManager(
        groupchat=group_chat_1,
        llm_config={"cache_seed": None,"config_list": config_list}
    )
    
    group_chat_2 = autogen.GroupChat(
        agents=[visual_agent, helper2, visual_user,multi_role_agent2,executor2,chat_instructor2,verify2],
        messages=[],
        max_round=10,
        select_speaker_transform_messages=select_speaker_transforms,
        speaker_selection_method=state_transition2,  # using an LLM to decide
        allow_repeat_speaker=False,  # Prevent same agent speaking twice
        send_introductions=False
    )
    
    manager_2 = autogen.GroupChatManager(
        groupchat=group_chat_2,
        llm_config={"cache_seed": None,"config_list": config_list}
    )
    
    visual_agent_group = {}
    visual_agent_group['visual_agent'] = visual_agent
    visual_agent_group['visual_user'] = visual_user
    visual_agent_group['helper2'] = helper2
    visual_agent_group['executor2'] = executor2
    visual_agent_group['multi_role_agent2'] = multi_role_agent2
    visual_agent_group['verify2'] = verify2
    visual_agent_group['chat_instructor2'] = chat_instructor2
    visual_agent_group['group_chat_2'] = group_chat_2
    visual_agent_group['manager_2'] = manager_2

    return assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group

def get_agent_response(assistant: autogen.AssistantAgent,chat_instructor: autogen.UserProxyAgent, helper: autogen.AssistantAgent, user_proxy: autogen.UserProxyAgent,manager: autogen.GroupChatManager,group_chat:autogen.GroupChat, message: str, role:str,user_id:int, prompt_id:int) -> str:
    """Get a single response from the agent for the given message."""
    user_prompt = f'{user_id}_{prompt_id}'
    try:

        result = user_proxy.initiate_chat(manager, message=message,speaker_selection={"speaker": "assistant"}, clear_history=False)

        count = 0
        while True:
            current_app.logger.info('inside while1')
            if group_chat.messages[-1]['name'] == 'ChatInstructor' and group_chat.messages[-1]['content'] == 'TERMINATE':
                current_app.logger.info(f"group_chat.messages[-2]['content'] {group_chat.messages[-2]['content'][:10]}..")
                try:
                    json_obj = eval(group_chat.messages[-2]["content"])
                    current_app.logger.info(f'got json object {json_obj}')
                    if json_obj['status'].lower() == 'completed':
                        current_app.logger.info(f'UPDATIN CURRENT ACTION AS :{int(json_obj["action_id"])}')
                        user_tasks[user_prompt].current_action = int(json_obj['action_id'])
                        action_message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)['action']
                        steps = [{x['steps']:{'tool_name':x.get('tool_name',None),'code':x.get('generalized_functions',None)}} for x in recipes[user_prompt]['actions'][user_tasks[user_prompt].current_action]['recipe']]
                        user_message = f"Action {user_tasks[user_prompt].current_action+1}:{action_message}\n follow these steps: {steps}"
                        chat_instructor.initiate_chat(recipient=manager, message=user_message, clear_history=False,silent=False)
                        continue
                except IndexError as e:
                    current_app.logger.info(f"COmpleted ALL ACTIONS:") 
                    return 'All set! Your tasks are fully completed. Is there anything else you\'d like me to'
                except:
                    try:
                        json_match = re.search(r'{[\s\S]*}', group_chat.messages[-2]["content"])
                        if json_match:
                            json_part = json_match.group(0)
                            json_obj = json.loads(json_part)
                            current_app.logger.info(f'got json object {json_obj}')
                            if json_obj['status'].lower() == 'completed':
                                current_app.logger.info(f'UPDATIN CURRENT ACTION AS :{int(json_obj["action_id"])}')
                                user_tasks[user_prompt].current_action = int(json_obj['action_id'])
                                action_message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)['action']
                                steps = [{x['steps']:{'tool_name':x.get('tool_name',None),'code':x.get('generalized_functions',None)}} for x in recipes[user_prompt]['actions'][user_tasks[user_prompt].current_action]['recipe']]
                                user_message = f"Action {user_tasks[user_prompt].current_action+1}:{action_message}\n follow these steps: {steps}"
                                chat_instructor.initiate_chat(recipient=manager, message=user_message, clear_history=False,silent=False)
                                continue
                        else:
                            raise 'No json found'
                    except Exception as e:
                        current_app.logger.warning(f'it is not a json object the error is: {e}')
                        current_app.logger.info('it is not a json object You should ask status verifier to give response in proper format & not move ahead to next action')
                        actions_prompt = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)
                        message = 'Hey @StatusVerifier Agent, Please verify the status of the action '+f'{user_tasks[user_prompt].current_action+1}: {actions_prompt}'+'\n performed and Respond in the following format {"status": "status here","action": "current action","action_id": '+f'{user_tasks[user_prompt].current_action+1}'+',"message": "message here"}'
                        assistant.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                        continue
            try:
                if user_tasks[user_prompt].actions[user_tasks[user_prompt].current_action]['can_perform_without_user_input'] == 'yes':
                    current_app.logger.info('GOT can_perform_without_user_input as true')
                    message = 'You should complete this task independently. Feel free to make reasonable assumptions where necessary'
                    helper.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)    
            
                count +=1
                if count == 4:
                    break
            except Exception as e:
                current_app.logger.error(f'WE have some indec error here: {e}')
            last_message = group_chat.messages[-1]['content']
            if f'@user'.lower() not in last_message.lower():
                message = 'If you want to communicate from the user then send the response with @user\nIf you current action is completed and you want next action ask @StatusVerifier for next action\n if you can continue the task without user intervention you can proceed with the actions.'
                helper.initiate_chat(manager, message=message,speaker_selection={"speaker": "assistant"}, clear_history=False)
                continue
            else:
                current_app.logger.info(f'@user in last message')
                break
        # if individual_recipe[currentaction_id-1]['can_perform_without_user_input'] == 'yes':
        #     return assistant
        last_message = group_chat.messages[-1]
        if last_message['content'] == 'TERMINATE':
            last_message = group_chat.messages[-2]
        
        if f'message_2_user'.lower() in last_message['content'].lower():
            json_obj = retrieve_json(last_message["content"])
            if json_obj:
                try:
                    last_message['content'] = json_obj['message_2_user']
                except:
                    pass
        return last_message

    except Exception as e:
        current_app.logger.info(f'Got some error {e}')
        return f"Error getting response: {str(e)}"


def get_flow_number(user_id,prompt_id):
    role = get_role(user_id,prompt_id)
    if not role:
        role = None
    current_app.logger.info(f'Got role as {role}')
    file_path = f'prompts/{prompt_id}.json'
    with open(file_path, 'r') as f:
        data = json.load(f)
        available_roles = [x['name'] for x in data['personas']]
        available_flows = data['flows']
    current_app.logger.info(f'Got available_roles as {available_roles}')
    if not role:
        role = available_roles[0]
    role_number = 0
    for num,i in enumerate(available_flows):
        if i['persona'].lower() == role.lower():
            role_number = num
            current_app.logger.info(f'GOT role index as {role_number}')
    return role_number, role

def create_schedule(prompt_id,user_id):
    current_app.logger.info('INSIDE Create Schedule')
    user_prompt = f'{user_id}_{prompt_id}'
    role_number,role = get_flow_number(user_id,prompt_id)
    with open(f"prompts/{prompt_id}_{role_number}_recipe.json", 'r') as f:
        config = json.load(f)
        recipes[user_prompt] = config
    try:
        if 'scheduled_tasks' in config and len(config['scheduled_tasks'])>0:
            current_app.logger.info('Creating scheduled tasks')
            for i in config['scheduled_tasks']:
                if role and i['persona'].lower() == role.lower():
                    trigger = CronTrigger.from_crontab(i['cron_expression'])
                    job_id = f"job_{int(time.time())}"
                    scheduler.add_job(execute_python_file, trigger=trigger, id=job_id,args=[i['job_description'],user_id,prompt_id,i['action_entry_point']])
                    current_app.logger.info(f'Successfully created scheduler job {i["persona"]}')
        
        current_app.logger.info('Creating Visual scheduled tasks')
        trigger = IntervalTrigger(seconds=int(10))
        job_id = f"job_{int(time.time())}"
        scheduler.add_job(call_visual_task, trigger=trigger, id=job_id,args=['get past 1 mins visual information',user_id,prompt_id])
        current_app.logger.info(f'Successfully created scheduler job')
        if 'visual_scheduled_tasks' in config and len(config['visual_scheduled_tasks'])>0:
            # current_app.logger.info('Creating Visual scheduled tasks')
            for i in config['visual_scheduled_tasks']:
                if role and i['persona'].lower() == role.lower():
                    trigger = CronTrigger.from_crontab(i['cron_expression'])
                    job_id = f"job_{int(time.time())}"
                    scheduler.add_job(call_visual_task, trigger=trigger, id=job_id,args=[i['job_description'],user_id,prompt_id])
                    current_app.logger.info(f'Successfully created scheduler job {i["persona"]}')
    except Exception as e:
        current_app.logger.error(f'Some Error in creating scheduled tasks error:{e}')

recent_file_id = {}
recipes = {}
user_tasks = {}
request_id_list = {}
time_actions = {}
final_recipe = {}

def chat_agent(user_id,text,prompt_id,file_id,request_id):
    current_app.logger.info('--'*100)
    user_message = text
    user_prompt = f'{user_id}_{prompt_id}'
    
    request_id_list[user_prompt] = request_id
    try:
        if file_id:
            recent_file_id[user_id] = file_id

        # Get or create agents for this user
        if user_prompt not in user_agents:
            llm_call_track[user_prompt] = {'count':0,'original_prompt':False}
            if user_prompt not in user_journey:
                if prompt_id not in agent_data.keys():
                    agent_data[prompt_id] = {}
                role_agents[user_prompt] = create_agents_for_role(user_id,prompt_id)
                assistant, user_proxy, group_chat, manager, helper, stop = role_agents[user_prompt]
                if stop:
                    user_journey[user_prompt] = 'UseBot'
                    # action_message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)['action']
                    # user_message = f"Action {user_tasks[user_prompt].current_action+1}:{action_message}"
                else:
                    user_journey[user_prompt] = 'Roles'
            if user_journey[user_prompt] == 'UseBot':
                create_schedule(prompt_id,user_id)
                user_agents[user_prompt] = create_agents_for_user(user_id,prompt_id)
                user_journey[user_prompt] = 'UseBot'
        if user_journey[user_prompt] == 'Roles':
            assistant, user_proxy, group_chat, manager, helper, stop = role_agents[user_prompt]
            result = user_proxy.initiate_chat(manager, message=user_message,speaker_selection={"speaker": "assistant"}, clear_history=False)
            # Print the chat summary
            current_app.logger.info("\n=== Chat Summary ===")
            # current_app.logger.info(result.summary)

            
            # Print the full chat history
            # current_app.logger.info("\n=== Full response ===")
            # current_app.logger.info(result)
            
            last_message = group_chat.messages[-1]
            if 'terminate' in last_message['content'].lower():
                # with open(f"prompts/{prompt_id}_recipe.json", 'r') as f:
                #     config = json.load(f)
                #     recipes[user_prompt] = config
                user_agents[user_prompt] = create_agents_for_user(user_id,prompt_id)
                assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group = user_agents[user_prompt]
                user_journey[user_prompt] = 'UseBot'
                create_schedule(prompt_id,user_id)
                action_message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)['action']
                steps = [{x['steps']:{'tool_name':x.get('tool_name',None),'code':x.get('generalized_functions',None)}} for x in recipes[user_prompt]['actions'][user_tasks[user_prompt].current_action]['recipe']]
                message = f"Action {user_tasks[user_prompt].current_action+1}:{action_message}\n follow these steps: {steps}"
                # message = "let's perform the actions availabe in sequence\nIMP instruction: keep track of action id you are working on."
                result = chat_instructor.initiate_chat(manager, message=message,speaker_selection={"speaker": "assistant"}, clear_history=False)

                count = 0
                while True:
                    current_app.logger.info('inside while2')
                    if group_chat.messages[-1]['name'] == 'ChatInstructor' and group_chat.messages[-1]['content'] == 'TERMINATE':
                        current_app.logger.info(f"group_chat.messages[-2]['content'] {group_chat.messages[-2]['content'][:10]}..")
                        try:
                            json_obj = eval(group_chat.messages[-2]["content"])
                            current_app.logger.info(f'got json object {json_obj}')
                            if json_obj['status'].lower() == 'completed':
                                current_app.logger.info(f'UPDATIN CURRENT ACTION AS :{int(json_obj["action_id"])}')
                                user_tasks[user_prompt].current_action = int(json_obj['action_id'])
                                action_message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)['action']
                                steps = [{x['steps']:{'tool_name':x.get('tool_name',None),'code':x.get('generalized_functions',None)}} for x in recipes[user_prompt]['actions'][user_tasks[user_prompt].current_action]['recipe']]
                                user_message = f"Action {user_tasks[user_prompt].current_action+1}:{action_message}\n follow these steps: {steps}"
                                chat_instructor.initiate_chat(recipient=manager, message=user_message, clear_history=False,silent=False)
                                continue
                        except:
                            try:
                                json_match = re.search(r'{[\s\S]*}', group_chat.messages[-2]["content"])
                                if json_match:
                                    json_part = json_match.group(0)
                                    json_obj = json.loads(json_part)
                                    current_app.logger.info(f'got json object {json_obj}')
                                    if json_obj['status'].lower() == 'completed':
                                        current_app.logger.info(f'UPDATIN CURRENT ACTION AS :{int(json_obj["action_id"])}')
                                        user_tasks[user_prompt].current_action = int(json_obj['action_id'])
                                        action_message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)['action']
                                        steps = [{x['steps']:{'tool_name':x.get('tool_name',None),'code':x.get('generalized_functions',None)}} for x in recipes[user_prompt]['actions'][user_tasks[user_prompt].current_action]['recipe']]
                                        user_message = f"Action {user_tasks[user_prompt].current_action+1}:{action_message}\n follow these steps: {steps}"
                                        chat_instructor.initiate_chat(recipient=manager, message=user_message, clear_history=False,silent=False)
                                        continue
                                        
                                        
                                else:
                                    raise 'No json found'
                            except IndexError as e:
                                current_app.logger.info(f"COmpleted ALL ACTIONS:") 
                                return 'All set! Your tasks are fully completed. Is there anything else you\'d like me to'
                            except Exception as e:
                                current_app.logger.warning(f'it is not a json object the error is: {e}')
                                current_app.logger.info('it is not a json object You should ask status verifier to give response in proper format & not move ahead to next action')
                                actions_prompt = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)
                                message = 'Hey @StatusVerifier Agent, Please verify the status of the action '+f'{user_tasks[user_prompt].current_action+1}: {actions_prompt}'+'\n performed and Respond in the following format {"status": "status here","action": "current action","action_id": '+f'{user_tasks[user_prompt].current_action+1}'+',"message": "message here"}'
                                assistant.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                                continue
                    count +=1
                    if count == 4:
                        break
                    # role = get_role(user_id,prompt_id)
                    last_message = group_chat.messages[-1]['content']
                    if f'@user'.lower() not in last_message.lower():
                        message = 'If you want to communicate from the user then send the response with @user\nIf you current action is completed and you want next action ask @StatusVerifier for next action\n if you can continue the task without user intervention you can proceed with the actions.'
                        helper.initiate_chat(manager, message=message,speaker_selection={"speaker": "assistant"}, clear_history=False)
                        continue
                    else:
                        current_app.logger.info(f'@user in last message')
                        break
                last_message = group_chat.messages[-1]
                if last_message['content'] == 'TERMINATE':
                    last_message = group_chat.messages[-2]
                llm_call_track[user_prompt]['count'] = 0
                llm_call_track[user_prompt]['original_prompt'] = True
                if f'message_2_user'.lower() in last_message['content'].lower():
                    json_obj = retrieve_json(last_message["content"])
                    if json_obj:
                        try:
                            last_message['content'] = json_obj['message_2_user']
                        except:
                            pass
                return last_message['content']
        
            
            return last_message['content']
        else:
            assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group = user_agents[user_prompt]

            prompt_id = int(prompt_id)
            role = get_role(user_id,prompt_id)
            response = get_agent_response(assistant,chat_instructor, helper,user_proxy,manager,group_chat, user_message, role,user_id,prompt_id)
            llm_call_track[user_prompt]['count'] = 0
            llm_call_track[user_prompt]['original_prompt'] = True
            return response
    except Exception as e:
        current_app.logger.info(f'Some ERROR IN REUSE RECIPE {e}')
        raise
    
def crossbar_multiagent(msg):
    current_app.logger.info("insde crossbar_multiagent")
    current_app.logger.info('--'*100)
    
    user_prompt = f"{msg['user_id']}_{msg['caller_prompt_id']}"
    assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group = user_agents[user_prompt]
    message = f"Role: {msg['caller_role']}\n Message: {msg['message']}"
    response = multi_role_agent.initiate_chat(manager, message=message,speaker_selection={"speaker": "assistant"}, clear_history=False)
    last_message = group_chat.messages[-1]
    if last_message['content'] == 'TERMINATE':
        last_message = group_chat.messages[-2]
        
    #sending response to receiver agent
    send_message_to_user1(msg['user_id'],last_message,msg['message'],msg['caller_prompt_id'])
    
    user_prompt = f"{msg['caller_user_id']}_{msg['caller_prompt_id']}"
    assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group = user_agents[user_prompt]
    message = f"Role: {msg['role']}\n Message: {last_message}"
    response = multi_role_agent.initiate_chat(manager, message=message,speaker_selection={"speaker": "assistant"}, clear_history=False)
    last_message = group_chat.messages[-1]
    if last_message['content'] == 'TERMINATE':
        last_message = group_chat.messages[-2]
    
    #sending response to caller agent
    send_message_to_user1(msg['caller_user_id'],last_message,msg['message'],msg['caller_prompt_id'])
